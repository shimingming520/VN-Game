"""
人物台词 Beat 统计器 (Character Beat Statistics)

统计 plots 表中，某一特定 task_id 下，不同人物说的话分别占据了多少 beats。

使用方法：
    # 基本统计
    python novel-game-factory/services/char_designer/core/beat_stats.py --task-id

    # 排除旁白
    python novel-game-factory/services/char_designer/core/beat_stats.py --exclude-narrator --task-id

    # 按名字排序
    python novel-game-factory/services/char_designer/core/beat_stats.py --sort count --task-id
"""

import sys
import json
import logging
import argparse
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Tuple

_current_dir = Path(__file__).resolve().parent
_project_root = _current_dir.parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from common.config import DATABASE_URL, PLATFORM_DATABASE_URL

try:
    from sqlalchemy import create_engine, text
except ImportError:
    raise ImportError("请安装 sqlalchemy: pip install sqlalchemy")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

NARRATOR_IDS = {"PangBai", "Narrator", "旁白"}


# ─────────────────────────── DB helpers ────────────────────────────

def _get_engine():
    return create_engine(PLATFORM_DATABASE_URL)


def _load_plots(engine, task_id: str) -> Dict:
    """从 plots 表读取指定 task_id 的数据"""
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT data FROM plots WHERE task_id = :tid"),
            {"tid": task_id}
        ).fetchone()
    if not row:
        raise ValueError(f"plots 表中未找到 task_id={task_id}")
    data = row[0]
    if isinstance(data, str):
        data = json.loads(data)
    return data


def _load_characters(engine, task_id: str) -> Dict:
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


def _save_characters(engine, task_id: str, data: Dict):
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE characters SET data = cast(:d as jsonb), update_time = NOW() WHERE task_id = :tid"),
            {"d": json.dumps(data, ensure_ascii=False), "tid": task_id}
        )


# ─────────────────────────── Core logic ────────────────────────────

def calc_important_rank(count: int, pct: float) -> int:
    """根据台词数量和占比计算 important_rank（数字越大重要性越高，最低为1）"""
    if count <= 119:
        base = 1
    elif count <= 250:
        base = 2
    elif count <= 400:
        base = 3
    elif count <= 600:
        base = 4
    else:
        base = 5

    if pct > 20.0 and base < 5:
        return base + 1
    return base


def update_important_rank_in_db(engine, task_id: str, counts: Dict[str, int]) -> int:
    """根据 counts 计算 important_rank 并更新 characters 表，返回更新的角色数"""
    chars_data = _load_characters(engine, task_id)

    # 先为每个角色解析出实际 beat 数，再统一算 total 避免重复计数
    char_counts: Dict[str, int] = {}
    for char_key, char_entry in chars_data.items():
        char_data = char_entry[0] if isinstance(char_entry, list) and char_entry else char_entry
        char_name = char_data.get("name", "") if isinstance(char_data, dict) else ""

        count = counts.get(char_key) or counts.get(char_name) or 0
        if count == 0:
            try:
                from .extract_raw import chinese_to_camel_case
                camel = chinese_to_camel_case(char_name or char_key)
                count = counts.get(camel, 0)
            except Exception:
                pass
        char_counts[char_key] = count

    total = sum(char_counts.values()) or 1

    updated = 0
    for char_key, char_entry in chars_data.items():
        count = char_counts.get(char_key, 0)
        pct = count / total * 100
        rank = calc_important_rank(count, pct)

        if isinstance(char_entry, list) and char_entry:
            char_entry[0]["important_rank"] = rank
        elif isinstance(char_entry, dict):
            char_entry["important_rank"] = rank
        else:
            continue
        updated += 1
        if count > 0:
            logger.info(f"  {char_key}: beats={count}, pct={pct:.1f}%, rank={rank}")

    _save_characters(engine, task_id, chars_data)
    logger.info(f"已更新 {updated} 个角色的 important_rank")
    return updated

def count_beats_by_character(
    plots_data: Dict,
    exclude_narrator: bool = False,
) -> Dict[str, int]:
    """
    统计每个角色的 beat 数量。
    返回两个维度的计数，key 同时包含 speaker_name 和 speaker_id，
    方便后续与 characters 表的不同 key 格式匹配。
    """
    counts: Dict[str, int] = defaultdict(int)

    for node_id, beats in plots_data.items():
        if not isinstance(beats, list):
            continue
        for beat in beats:
            speaker_id = beat.get("speaker_id", "")
            speaker_name = beat.get("speaker_name") or speaker_id or "未知"

            if exclude_narrator and speaker_id in NARRATOR_IDS:
                continue

            # 同时以 speaker_name 和 speaker_id 为 key 累计，方便后续匹配
            if speaker_name:
                counts[speaker_name] += 1
            if speaker_id and speaker_id != speaker_name:
                counts[speaker_id] += 1

    return dict(counts)


def sorted_stats(
    counts: Dict[str, int],
    sort_by: str = "count",
) -> List[Tuple[str, int]]:
    """
    排序统计结果。

    Args:
        sort_by: "count"（按数量降序）或 "name"（按名字升序）
    """
    if sort_by == "name":
        return sorted(counts.items(), key=lambda x: x[0])
    return sorted(counts.items(), key=lambda x: x[1], reverse=True)


# ─────────────────────────── Public API ────────────────────────────

def run(
    task_id: str,
    exclude_narrator: bool = False,
    sort_by: str = "count",
) -> Dict[str, int]:
    """
    统计指定 task_id 的人物 beat 数量，并更新 characters 表的 important_rank。

    Returns:
        {speaker_name: beat_count}（含 speaker_id 副本，供 update_important_rank_in_db 匹配）
    """
    engine = _get_engine()
    plots_data = _load_plots(engine, task_id)

    counts = count_beats_by_character(plots_data, exclude_narrator=exclude_narrator)

    # 打印时只展示 speaker_name 维度（过滤掉纯英文驼峰的 speaker_id 副本）
    import re
    display_counts = {k: v for k, v in counts.items() if not re.match(r'^[A-Z][a-zA-Z]+$', k)}
    total = sum(display_counts.values()) or 1

    ranked = sorted_stats(display_counts, sort_by=sort_by)

    print(f"\n任务 {task_id} 人物 Beat 统计（共 {total} beats）：")
    print(f"{'排名':<5} {'人物':<20} {'beats':<8} {'占比':<8} {'important_rank'}")
    print("-" * 55)
    for rank, (name, count) in enumerate(ranked, 1):
        pct = count / total * 100
        ir = calc_important_rank(count, pct)
        print(f"{rank:<5} {name:<20} {count:<8} {pct:<7.1f}% {ir}")

    update_important_rank_in_db(engine, task_id, counts)

    return counts


# ─────────────────────────── CLI ───────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="统计 plots 表中各人物的 beat 数量")
    parser.add_argument("--task-id", required=True, help="任务 ID")
    parser.add_argument("--exclude-narrator", action="store_true", help="排除旁白")
    parser.add_argument("--sort", choices=["count", "name"], default="count",
                        help="排序方式：count（按数量，默认）或 name（按名字）")
    args = parser.parse_args()

    try:
        run(task_id=args.task_id, exclude_narrator=args.exclude_narrator, sort_by=args.sort)
        sys.exit(0)
    except Exception as e:
        logger.error(f"❌ 执行失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
