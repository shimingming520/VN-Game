"""
角色替换器 (Character Replacer)

流程：
1. 从 mapping 表读取指定 task_id 的 name_map（{"原名": "新名"}）
2. 用正则批量替换 characters 表中的角色数据
3. 用正则批量替换 story_tree_md 表中所有对应的 markdown 文本

使用方法：
  python services/char_designer/core/char_replace.py --task-id <task-id>
  python services/char_designer/core/char_replace.py --restore --task-id <task-id>
  python services/char_designer/core/char_replace.py --try --task-id <task-id>
"""

import re
import sys
import json
import logging
import argparse
from pathlib import Path
from typing import Dict, List, Tuple

_current_dir = Path(__file__).resolve().parent
_project_root = _current_dir.parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from common.config import DATABASE_URL, PLATFORM_DATABASE_URL
from sqlalchemy import create_engine, text

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ─────────────────────────── DB helpers ────────────────────────────

def _get_engine():
    return create_engine(PLATFORM_DATABASE_URL)


def _load_name_map(engine, task_id: str) -> Dict[str, str]:
    """从 mapping 表读取 name_map"""
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT name_map FROM mapping WHERE task_id = :tid"),
            {"tid": task_id}
        ).fetchone()
    if not row:
        raise ValueError(f"mapping 表中未找到 task_id={task_id}，请先运行 char_rename.py")
    data = row[0]
    if isinstance(data, str):
        data = json.loads(data)
    return data


def _load_characters(engine, task_id: str) -> Dict:
    """从 characters 表读取角色数据"""
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT data FROM characters WHERE task_id = :tid"),
            {"tid": task_id}
        ).fetchone()
    if not row:
        raise ValueError(f"characters 表中未找到 task_id={task_id}")
    data = row[0]
    if isinstance(data, str):
        data = json.loads(data)
    return data


def _load_story_tree_mds(engine, task_id: str) -> List[Tuple[int, str, str]]:
    """从 story_tree_md 表读取所有 markdown，返回 [(id, name, data), ...]"""
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT id, name, data FROM story_tree_md WHERE task_id = :tid ORDER BY id"),
            {"tid": task_id}
        ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def _save_characters(engine, task_id: str, data: Dict, dry_run: bool):
    if dry_run:
        return
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE characters SET data = cast(:d as jsonb), update_time = NOW() WHERE task_id = :tid"),
            {"tid": task_id, "d": json.dumps(data, ensure_ascii=False)}
        )


def _save_story_tree_md(engine, md_id: int, data: str, dry_run: bool):
    if dry_run:
        return
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE story_tree_md SET data = :d, update_time = NOW() WHERE id = :id"),
            {"id": md_id, "d": data}
        )


# ─────────────────────────── replace logic ─────────────────────────

def _build_replacer(name_map: Dict[str, str]) -> Tuple[re.Pattern, Dict[str, str]]:
    """
    构建一个合并的正则，按名字长度降序排列（避免短名先匹配截断长名）。
    返回 (pattern, mapping)
    """
    # 过滤掉原名==新名的条目（无需替换）
    effective = {k: v for k, v in name_map.items() if k != v and k.strip()}
    if not effective:
        return None, {}

    # 按长度降序，保证长名优先匹配
    sorted_keys = sorted(effective.keys(), key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(k) for k in sorted_keys))
    return pattern, effective


def _apply_replacer(text: str, pattern: re.Pattern, mapping: Dict[str, str]) -> Tuple[str, int]:
    """对文本执行替换，返回 (新文本, 替换次数)"""
    count = 0

    def repl(m):
        nonlocal count
        count += 1
        return mapping[m.group(0)]

    new_text = pattern.sub(repl, text)
    return new_text, count


def _replace_in_json_str(json_str: str, pattern: re.Pattern, mapping: Dict[str, str]) -> Tuple[str, int]:
    """对 JSON 字符串内容执行替换（直接对序列化后的字符串操作）"""
    return _apply_replacer(json_str, pattern, mapping)


# ─────────────────────────── main pipeline ─────────────────────────

def run(task_id: str, action: str = "replace", dry_run: bool = False) -> Dict:
    """
    执行替换或还原。

    action: "replace" 原名->新名 | "restore" 新名->原名
    dry_run: True 时只统计不写库

    Returns: {"characters_changes": int, "md_changes": {"name": count}}
    """
    if action not in ("replace", "restore"):
        raise ValueError(f"action 必须是 replace 或 restore，当前: {action}")

    engine = _get_engine()

    # 1. 读取 name_map
    name_map = _load_name_map(engine, task_id)
    logger.info(f"读取到 {len(name_map)} 条 name_map")

    # 2. 根据方向决定映射
    if action == "replace":
        effective_map = {k: v for k, v in name_map.items() if k != v}
        direction_label = "原名 -> 新名"
    else:  # restore
        effective_map = {v: k for k, v in name_map.items() if k != v}
        direction_label = "新名 -> 原名"

    if not effective_map:
        logger.warning("没有需要替换的映射（所有原名==新名），退出")
        return {"characters_changes": 0, "md_changes": {}}

    logger.info(f"方向：{direction_label}，有效映射 {len(effective_map)} 条")
    if dry_run:
        logger.info("[DRY-RUN 模式] 只统计，不写库")

    pattern, mapping = _build_replacer(effective_map)

    stats = {"characters_changes": 0, "md_changes": {}}

    # 3. 替换 characters 表
    logger.info("处理 characters 表...")
    chars_data = _load_characters(engine, task_id)
    chars_json_str = json.dumps(chars_data, ensure_ascii=False)
    new_chars_str, chars_count = _replace_in_json_str(chars_json_str, pattern, mapping)
    stats["characters_changes"] = chars_count

    if chars_count > 0:
        new_chars_data = json.loads(new_chars_str)
        _save_characters(engine, task_id, new_chars_data, dry_run)
        logger.info(f"  characters: {chars_count} 处替换{'（dry-run，未写库）' if dry_run else ''}")
    else:
        logger.info("  characters: 无变化")

    # 4. 替换 story_tree_md 表
    logger.info("处理 story_tree_md 表...")
    md_rows = _load_story_tree_mds(engine, task_id)
    logger.info(f"  共 {len(md_rows)} 条 markdown 记录")

    for md_id, md_name, md_data in md_rows:
        if not md_data:
            continue
        new_md, md_count = _apply_replacer(md_data, pattern, mapping)
        if md_count > 0:
            stats["md_changes"][md_name] = md_count
            _save_story_tree_md(engine, md_id, new_md, dry_run)
            logger.info(f"  [{md_name}]: {md_count} 处替换{'（dry-run）' if dry_run else ''}")

    total_md = sum(stats["md_changes"].values())
    logger.info(f"story_tree_md 共替换 {total_md} 处，涉及 {len(stats['md_changes'])} 个文件")

    return stats


# ─────────────────────────── CLI ───────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="角色替换器 - 根据 mapping 表的 name_map 批量替换/还原人名"
    )
    parser.add_argument("--restore", action="store_true", help="还原：新名 -> 原名")
    parser.add_argument("--try", dest="dry_run", action="store_true", help="只统计替换次数，不实际写库")
    parser.add_argument("--task-id", required=True, help="任务 ID")
    args = parser.parse_args()

    action = "restore" if args.restore else "replace"

    try:
        stats = run(task_id=args.task_id, action=action, dry_run=args.dry_run)

        print(f"\n{'[DRY-RUN] ' if args.dry_run else ''}替换完成")
        print(f"  characters 表：{stats['characters_changes']} 处")
        print(f"  story_tree_md 表：{sum(stats['md_changes'].values())} 处，涉及 {len(stats['md_changes'])} 个文件")

        if stats["md_changes"]:
            print("\n  各文件替换详情：")
            for name, count in sorted(stats["md_changes"].items()):
                print(f"    {name}: {count} 处")

        sys.exit(0)
    except Exception as e:
        logger.error(f"执行失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
