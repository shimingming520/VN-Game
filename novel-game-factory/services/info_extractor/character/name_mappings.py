"""
人名多语言映射表生成器 (Name Mappings Builder)

流程：
1. 从 characters 表读取指定 task_id 的角色主名
2. 生成剧情提取阶段需要的原名映射，并预留空的 change_name：
   - original_name.conversion: 原主名 -> 原始 PascalCase 拼音 ID
   - change_name:              空骨架；在 important_rank 生成后由独立改名步骤填写
3. 同时落两份：
   - 本地文件 OUTPUTS_PATH/<task_id>/name_mappings.json （与 story_tree.json 同级）
   - 云端表 name_mappings(task_id, data jsonb) upsert

使用方法：
# 默认：已存在则跳过
python novel-game-factory/services/info_extractor/character/name_mappings.py --task-id

# 强制重新生成（覆盖本地文件 + 数据库记录）
python novel-game-factory/services/info_extractor/character/name_mappings.py --force --task-id

"""

import re
import sys
import json
import logging
import argparse
from pathlib import Path
from typing import Dict, List, Set, Mapping, Optional

_current_dir = Path(__file__).resolve().parent
_project_root = _current_dir.parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from common.config import OUTPUTS_PATH, PLATFORM_DATABASE_URL
from common.name_mapping_utils import get_change_conversion

try:
    from sqlalchemy import create_engine, text
except ImportError:
    raise ImportError("请安装 sqlalchemy: pip install sqlalchemy")

try:
    from pypinyin import lazy_pinyin
except ImportError:
    raise ImportError("请安装 pypinyin: pip install pypinyin")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ─────────────────────────── name id helpers ───────────────────────

def to_pascal_case_id(raw_text: str) -> str:
    """中文转 PascalCase 拼音；英文按非字母数字切词后 PascalCase。

    与 extract_plots_to_db.to_pascal_case_speaker_id 行为一致，避免下游冲突。
    """
    if not raw_text:
        return ""

    if re.search(r'[一-龥]', raw_text):
        return "".join(p.capitalize() for p in lazy_pinyin(raw_text))

    raw_text = re.sub(r"['‘’]s\b", "", raw_text, flags=re.IGNORECASE)
    raw_text = re.sub(r"['‘’]", "", raw_text)
    raw_text = re.sub(r"[^a-zA-Z0-9]", " ", raw_text)
    return "".join(p.capitalize() for p in raw_text.split())


# ─────────────────────────── name extraction ───────────────────────

def _collect_all_names(characters_data: Dict) -> List[str]:
    """从 characters data 仅收集角色主名（去重并保持首次出现顺序）。"""
    seen: Set[str] = set()
    ordered: List[str] = []

    def _add(name: str):
        n = (name or "").strip()
        if not n or n in seen:
            return
        seen.add(n)
        ordered.append(n)

    for char_key, char_val in characters_data.items():
        entry = char_val[0] if isinstance(char_val, list) and char_val else char_val
        if not isinstance(entry, dict):
            _add(char_key)
            continue
        _add(entry.get("name") or char_key)

    return ordered


def build_mappings(names: List[str]) -> Dict[str, Dict]:
    """只生成原名 ID；change_name 由 important_rank 改名步骤填写。

    本步骤不调用模型、不翻译、不生成改名，纯拼音转换。
    """
    original_conversion = _build_conversion_for_names(names)

    return {
        "original_name": {
            "conversion": original_conversion,
        },
        "change_name": {
            "new-name": {},
            "conversion": {},
            "zh-CN": {},
            "en-US": {},
            "ja-JP": {},
        },
    }


def _build_conversion_for_names(names: List[str]) -> Dict[str, str]:
    """为旧数据迁移补齐原始人名 ID，并保证 ID 唯一。"""
    conversion: Dict[str, str] = {}
    used_ids: Set[str] = set()
    for name in names:
        base_id = to_pascal_case_id(name)
        if not base_id:
            continue
        pid = base_id
        suffix = 2
        while pid in used_ids:
            pid = f"{base_id}_{suffix}"
            suffix += 1
        used_ids.add(pid)
        conversion[name] = pid
    return conversion


def _normalise_existing_mappings(data: Optional[Mapping]) -> Dict[str, Dict]:
    """把已入库的旧扁平格式迁移为 original_name/change_name。"""
    source = dict(data or {})
    if isinstance(source.get("change_name"), Mapping):
        change_source = dict(source.get("change_name") or {})
        original_source = dict(source.get("original_name") or {})
    else:
        change_source = source
        original_source = {}

    new_name = dict(change_source.get("new-name") or {})
    conversion = dict(change_source.get("conversion") or {})
    if not new_name and "new-name" not in change_source:
        new_name = {name: name for name in conversion}

    original_conversion = dict(original_source.get("conversion") or {})
    if not original_conversion:
        original_names = list(new_name.keys()) or list(conversion.keys())
        original_conversion = _build_conversion_for_names(original_names)

    return {
        "original_name": {
            "conversion": original_conversion,
        },
        "change_name": {
            "new-name": new_name,
            "conversion": conversion,
            "zh-CN": dict(change_source.get("zh-CN") or {}),
            "en-US": dict(change_source.get("en-US") or {}),
            "ja-JP": dict(change_source.get("ja-JP") or {}),
        },
    }


# ─────────────────────────── persistence ───────────────────────────

def _get_engine():
    return create_engine(PLATFORM_DATABASE_URL)


def _load_characters(engine, task_id: str) -> Dict:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT data FROM characters WHERE task_id = :tid"),
            {"tid": task_id},
        ).fetchone()
    if not row:
        raise ValueError(f"characters 表中未找到 task_id={task_id}")
    data = row[0]
    if isinstance(data, str):
        data = json.loads(data)
    return data


def _save_to_db(engine, task_id: str, mappings: Dict):
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO name_mappings (task_id, data, create_time, update_time)
            VALUES (:tid, cast(:data as jsonb), NOW(), NOW())
            ON CONFLICT (task_id)
            DO UPDATE SET data = EXCLUDED.data, update_time = NOW()
        """), {"tid": task_id, "data": json.dumps(mappings, ensure_ascii=False)})
    logger.info(f"name_mappings 已写入数据库，task_id={task_id}")


def _save_to_local(
    task_id: str, mappings: Dict, outputs_path: str = OUTPUTS_PATH
) -> Path:
    out_dir = Path(outputs_path) / task_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "name_mappings.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(mappings, f, ensure_ascii=False, indent=2)
    logger.info(f"name_mappings.json 已落盘: {out_path}")
    return out_path


def _save_replacements_to_local(
    task_id: str,
    replacements: Dict[str, str],
    outputs_path: str = OUTPUTS_PATH,
) -> Path:
    out_dir = Path(outputs_path) / task_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "name_replacements.json"
    changed_only = {
        old: new for old, new in replacements.items()
        if isinstance(old, str) and isinstance(new, str)
        and old.strip() and new.strip() and old.strip() != new.strip()
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(changed_only, f, ensure_ascii=False, indent=2)
    logger.info(f"name_replacements.json 已落盘: {out_path}")
    return out_path


def check_exists(task_id: str) -> bool:
    """判断 name_mappings 表是否已存在该 task_id 的记录。"""
    try:
        engine = _get_engine()
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT 1 FROM name_mappings WHERE task_id = :tid LIMIT 1"),
                {"tid": task_id},
            ).fetchone()
        return row is not None
    except Exception as e:
        logger.error(f"检查 name_mappings 存在性失败: {e}")
        return False


def load_conversion(task_id: str) -> Dict[str, str]:
    """加载 change_name.conversion（DB 优先，本地文件降级）。"""
    try:
        engine = _get_engine()
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT data FROM name_mappings WHERE task_id = :tid"),
                {"tid": task_id},
            ).fetchone()
        if row:
            data = row[0]
            if isinstance(data, str):
                data = json.loads(data)
            return get_change_conversion(data)
    except Exception as e:
        logger.warning(f"从数据库加载 name_mappings 失败: {e}")
    local_path = Path(OUTPUTS_PATH) / task_id / "name_mappings.json"
    if local_path.exists():
        with open(local_path, encoding="utf-8") as f:
            return get_change_conversion(json.load(f))
    return {}


# ─────────────────────────── main pipeline ─────────────────────────

def run(task_id: str, force: bool = False) -> Dict[str, Dict]:
    """完整流程：读 characters -> 构建映射 -> 落盘 + 入库。

    Args:
        task_id: 任务 ID
        force:   True=强制覆盖；False=已存在则直接读回返回（不重新生成）
    """
    engine = _get_engine()

    if not force and check_exists(task_id):
        logger.info(f"name_mappings 已存在，跳过生成（task_id={task_id}）。如需重写请加 --force")
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT data FROM name_mappings WHERE task_id = :tid"),
                {"tid": task_id},
            ).fetchone()
        data = row[0] if row else {}
        if isinstance(data, str):
            data = json.loads(data)
        original_data = dict(data) if isinstance(data, dict) else data
        if isinstance(data, dict):
            data.pop("alias-new-name", None)
            data.pop("alias-owner", None)
        # 旧任务自动迁移为 original_name/change_name 新结构。
        normalised = _normalise_existing_mappings(data)
        if normalised != original_data:
            _save_to_local(task_id, normalised)
            _save_to_db(engine, task_id, normalised)
        return normalised

    logger.info(f"读取 task_id={task_id} 的角色数据...")
    characters_data = _load_characters(engine, task_id)
    logger.info(f"共读取到 {len(characters_data)} 个角色条目")

    names = _collect_all_names(characters_data)
    if not names:
        raise ValueError("未从 characters 表中提取到任何角色名")
    logger.info(f"共收集到 {len(names)} 个角色主名（已去重）")

    mappings = build_mappings(names)
    logger.info(
        f"原名映射构建完成：original_name.conversion "
        f"{len(mappings['original_name']['conversion'])} 条；change_name 已预留为空"
    )

    _save_to_local(task_id, mappings)
    _save_replacements_to_local(task_id, {})
    _save_to_db(engine, task_id, mappings)

    return mappings


# ─────────────────────────── CLI ───────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="原名 ID 映射生成器 - change_name 由后续 ranked rename 步骤填写"
    )
    parser.add_argument("--task-id", required=True, help="任务 ID")
    parser.add_argument("--force", action="store_true", help="已存在时强制覆盖本地文件和数据库记录")
    args = parser.parse_args()

    try:
        mappings = run(task_id=args.task_id, force=args.force)
        print(
            f"\n✅ 完成，original_name.conversion "
            f"{len(mappings['original_name']['conversion'])} 条，change_name 为空"
        )
        sys.exit(0)
    except Exception as e:
        logger.error(f"❌ 执行失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
