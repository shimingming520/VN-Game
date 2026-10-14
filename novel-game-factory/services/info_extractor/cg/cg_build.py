"""
CG 表构建器 (Build CG Table)

Plot 区间标注 (annotate_cg_for_node_async) 会先把完整 CG 标注草稿写入 cg 表，
plots 只保留 `cg` 区间标记。之后本模块扫描 plots 中的区间标记，补全人物变体池：

  - source_node   节点 id（如 chapter_1）
  - beat_range    [首 beat_id, 尾 beat_id]，该 CG 覆盖区间的两个端点（闭区间）
  - characters    区间内 + 区间前后窗口收集到的 (真名, 变体) 人物参考池
                  （排除旁白；不同变体算不同人；最多 12 个）
  - cg_visual_entities  CG 标注阶段识别出的实际可见人物（包括无台词人物）
  - visual_asset_requirements  后续 CG 提示词规划要补全的身份参考需求

后续步骤（scene_img_att / init_description / cg_prompt）由 cg_prompt.py 填；
char_img_att 仅保留为历史兼容空字段；新 CG 合同使用逐人物独立参考图；
最终出图由 scene_painter/cg_generator.py 完成。

用法：
    python services/info_extractor/cg/cg_build.py --task-id <task_id>
"""

import os
import sys
import json
import re
import logging
import argparse
from pathlib import Path
from typing import Dict, List, Optional, Tuple

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

service_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if service_dir not in sys.path:
    sys.path.insert(0, service_dir)

from sqlalchemy import create_engine, text

from common.config import PLATFORM_DATABASE_URL
from common.cg_data import load_cg_contract

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("cg_build")

NARRATOR_IDS = {"PangBai", "Narrator", "旁白"}

CHAR_LOOKBACK_BEATS = 30   # 人物补充窗口：区间首 beat 往前看的 raw beat 数
CHAR_LOOKAHEAD_BEATS = 10  # 人物补充窗口：区间尾 beat 往后看的 raw beat 数
CHAR_MAX_VARIANTS = 12     # 人物收集上限：最多 12 个 (name, variant)

# 早期 CG 补偿：当区间首 raw 位置 < CHAR_EARLY_BOUNDARY 时，
# 改为从故事开头扫，避免向前取不到人物。
CHAR_EARLY_BOUNDARY = 30
CHAR_EARLY_TOTAL = 40

DB_URL = PLATFORM_DATABASE_URL

_CG_TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS cg (
        id          SERIAL PRIMARY KEY,
        task_id     VARCHAR(255) NOT NULL UNIQUE,
        data        JSONB NOT NULL DEFAULT '{}'::jsonb,
        create_time TIMESTAMP NOT NULL DEFAULT NOW(),
        update_time TIMESTAMP NOT NULL DEFAULT NOW()
    )
"""


def _get_engine():
    return create_engine(DB_URL, pool_pre_ping=True)


def _load_table(engine, table: str, task_id: str) -> Optional[Dict]:
    if table == "cg":
        return load_cg_contract(task_id, db_engine=engine)
    with engine.connect() as conn:
        row = conn.execute(
            text(f"SELECT data FROM {table} WHERE task_id = :tid"), {"tid": task_id}
        ).fetchone()
    if not row:
        return None
    data = row[0]
    return json.loads(data) if isinstance(data, str) else data


def ensure_cg_table(engine) -> None:
    """确保 cg 表存在（首次运行自动建表）。"""
    with engine.begin() as conn:
        conn.execute(text(_CG_TABLE_DDL))


def upsert_cg_data(connection, task_id: str, data: Dict) -> None:
    """Upsert one task's CG payload on the caller's existing transaction."""
    connection.execute(text(_CG_TABLE_DDL))
    connection.execute(
        text(
            "INSERT INTO cg (task_id, data, create_time, update_time) "
            "VALUES (:tid, cast(:d as jsonb), NOW(), NOW()) "
            "ON CONFLICT (task_id) DO UPDATE SET "
            "data = EXCLUDED.data, update_time = NOW()"
        ),
        {"tid": task_id, "d": json.dumps(data, ensure_ascii=False)},
    )


def _upsert_cg(engine, task_id: str, data: Dict) -> None:
    with engine.begin() as conn:
        upsert_cg_data(conn, task_id, data)


def build_cg_annotation_draft(plots_data: Dict) -> Dict[str, Dict]:
    """Build the authoritative CG annotation draft before plots are persisted.

    ``plots_data`` is still the in-memory annotation result at this point, so it
    contains ``cg_visual_entities``.  The returned payload owns those details;
    persisted plots will retain only the lightweight ``cg`` range marker.
    """
    flat = _flatten_in_node_order(plots_data or {})
    groups = _group_cg_ranges(flat)
    draft: Dict[str, Dict] = {}
    for cg_id, group in groups.items():
        visual_entities = list(group.get("visual_entities") or [])
        draft[cg_id] = {
            "source_node": group["source_node"],
            "beat_range": [group["start_beat_id"], group["end_beat_id"]],
            "characters": [],
            "cg_visual_entities": visual_entities,
            "visual_asset_requirements": _initial_visual_asset_requirements(
                visual_entities
            ),
            "char_img_att": [],
            "scene_img_att": [],
            "init_description": "",
            "cg_prompt": "",
        }
    return draft


def strip_cg_details_from_plots(plots_data: Dict) -> int:
    """Remove duplicated CG detail fields while retaining each beat's cg marker."""
    removed = 0
    for beats in (plots_data or {}).values():
        if not isinstance(beats, list):
            continue
        for beat in beats:
            if isinstance(beat, dict) and "cg_visual_entities" in beat:
                beat.pop("cg_visual_entities", None)
                removed += 1
    return removed


def cg_data_ready(task_id: str) -> bool:
    """Return whether cg.data is absent-by-design or has complete text prompts.

    A non-empty annotation draft is deliberately *not* ready: Step 12 still has
    to populate scene references, the structured reference contract and
    ``cg_prompt``.  An existing empty object represents a task with no CG.
    """
    engine = _get_engine()
    try:
        ensure_cg_table(engine)
    except Exception as exc:
        logger.warning("cg 表不可用，按共享 cg.json 检查完成度: %s", exc)
    data = _load_table(engine, "cg", task_id)
    if data is None:
        return False
    if not data:
        plots_data = _load_table(engine, "plots", task_id) or {}
        has_cg_marker = any(
            _normalize_cg_id(beat.get("cg"))
            for beats in plots_data.values()
            if isinstance(beats, list)
            for beat in beats
            if isinstance(beat, dict)
        )
        return not has_cg_marker
    if not isinstance(data, dict):
        return False
    return all(
        isinstance(spec, dict) and str(spec.get("cg_prompt") or "").strip()
        for spec in data.values()
    )


# ============================================================
# 人名 / 变体转换（本地实现，不跨服务导入）
# ============================================================

def _to_camel(name: str) -> str:
    """中文名转拼音驼峰，英文名去空格首字母大写。

    与 char_designer/generate_calm.chinese_to_camel_case 对齐：中文按每字拼音
    首字母大写拼接，英文清洗特殊符号后按词首字母大写拼接。
    """
    if not name:
        return ""
    try:
        from pypinyin import pinyin, Style as PinyinStyle
        if re.search(r"[一-鿿]", name):
            parts = pinyin(name, style=PinyinStyle.NORMAL)
            return "".join(p[0].capitalize() for p in parts if p)
    except ImportError:
        pass
    words = re.sub(r"[^a-zA-Z0-9\s]", "", name).split()
    return "".join(w.capitalize() for w in words) if words else name


def _combination_to_variant(combination: str) -> str:
    """'凌妙妙-1-10-5' -> 'LingMiaoMiao_1-10-5_calm'。"""
    if not combination:
        return ""
    parts = combination.rsplit("-", 3)
    if len(parts) != 4:
        return ""
    name, p, b, c = parts
    safe_name = _to_camel(name)
    return f"{safe_name}_{p}-{b}-{c}_calm"


def _build_speaker_to_name(chars_data: Dict) -> Dict[str, str]:
    """{speaker_id 候选: 中文真名}。

    plots 里 speaker_id 用拼音驼峰（如 'CenJin'），characters 表 key 是中文真名（如 '岑矜'）。
    把每个 (中文 key / name 字段 / speaker_id 字段 / 中文 key 的拼音驼峰 / name 的拼音驼峰)
    都映射到中文真名，反查时全部命中即可。
    """
    mapping: Dict[str, str] = {}
    for key, char_entry in (chars_data or {}).items():
        char_data = char_entry[0] if isinstance(char_entry, list) and char_entry else char_entry
        if not isinstance(char_data, dict):
            continue
        real_name = char_data.get("name") or key
        mapping.setdefault(key, real_name)
        if real_name:
            mapping.setdefault(real_name, real_name)
        speaker_id = char_data.get("speaker_id")
        if speaker_id:
            mapping.setdefault(speaker_id, real_name)
        camel_key = _to_camel(key)
        if camel_key:
            mapping.setdefault(camel_key, real_name)
        camel_name = _to_camel(real_name)
        if camel_name:
            mapping.setdefault(camel_name, real_name)
    return mapping


# ============================================================
# beat 摊平与区间分组
# ============================================================

def _flatten_in_node_order(plots_data: Dict) -> List[Tuple[str, Dict]]:
    """把所有 beat 按 (node_id 字典序, beat 在节点内顺序) 摊平。返回 [(node_id, beat), ...]。"""
    out: List[Tuple[str, Dict]] = []
    for node_id in sorted(plots_data.keys()):
        beats = plots_data.get(node_id) or []
        if not isinstance(beats, list):
            continue
        for beat in beats:
            out.append((node_id, beat))
    return out


def _normalize_cg_id(cg_val) -> str:
    """把 beat 的 cg 字段值规整为有效 cg_id 字符串；空/null/none/false 视为无。"""
    if cg_val is None:
        return ""
    s = str(cg_val).strip()
    if s.lower() in ("", "null", "none", "false"):
        return ""
    return s


def _merge_visual_entities(target: List[Dict], incoming) -> None:
    """Merge normalized CG-visible people without changing their first order."""
    if not isinstance(incoming, list):
        return
    existing = {
        (str(item.get("name") or "").strip(), str(item.get("entity_type") or "human"))
        for item in target
        if isinstance(item, dict)
    }
    for raw in incoming:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or raw.get("entity_name") or "").strip()
        entity_type = str(raw.get("entity_type") or "human").strip().lower()
        if not name or entity_type not in {"human", "humanoid"}:
            continue
        policy = str(raw.get("reference_policy") or "required_identity").strip().lower()
        if policy not in {"required_identity", "recommended_identity", "prompt_only"}:
            policy = "required_identity"
        aliases = raw.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        aliases = [str(value).strip() for value in aliases if str(value).strip()]
        key = (name, entity_type)
        if key in existing:
            continue
        existing.add(key)
        target.append(
            {
                "name": name,
                "aliases": aliases,
                "entity_type": entity_type,
                "reference_policy": policy,
            }
        )


def _group_cg_ranges(flat: List[Tuple[str, Dict]]) -> Dict[str, Dict]:
    """按 (node, cg_id) 分组，产出 {cg_id: {source_node, start_pos, end_pos}}。

    - cg_id 全局唯一且只出现在单个节点内（annotate 保证）。
    - start_pos / end_pos 是在 flat 中的全局下标（区间首尾）。
    - 同一 cg_id 内的 beat 应连续；这里以首次/末次出现的全局下标为端点，
      即便中间夹结构 beat 也能正确覆盖。
    """
    groups: Dict[str, Dict] = {}
    for idx, (node_id, beat) in enumerate(flat):
        cg_id = _normalize_cg_id(beat.get("cg"))
        if not cg_id:
            continue
        g = groups.get(cg_id)
        if g is None:
            groups[cg_id] = {
                "source_node": node_id,
                "start_pos": idx,
                "end_pos": idx,
                "start_beat_id": str(beat.get("beat_id") or beat.get("id") or ""),
                "end_beat_id": str(beat.get("beat_id") or beat.get("id") or ""),
                "visual_entities": [],
            }
            _merge_visual_entities(
                groups[cg_id]["visual_entities"], beat.get("cg_visual_entities")
            )
        else:
            # 若同一 cg_id 跨节点（异常），只保留首个节点，忽略后续
            if node_id != g["source_node"]:
                logger.warning(f"[{cg_id}] 出现在多个节点（{g['source_node']} / {node_id}），忽略后者")
                continue
            g["end_pos"] = idx
            g["end_beat_id"] = str(beat.get("beat_id") or beat.get("id") or "")
            _merge_visual_entities(g["visual_entities"], beat.get("cg_visual_entities"))
    return groups


# ============================================================
# 人物收集
# ============================================================

def _collect_characters_for_range(
    flat: List[Tuple[str, Dict]],
    start_pos: int,
    end_pos: int,
    speaker_to_name: Dict[str, str],
) -> List[Dict[str, str]]:
    """收集一张 CG 的人物参考池 (真名, 变体)。

    主来源：区间 [start_pos, end_pos] 内所有 beat 的 speaker_combination。
    补充：区间首往前 CHAR_LOOKBACK_BEATS 条 raw beat、区间尾往后 CHAR_LOOKAHEAD_BEATS 条。
    早期补偿：区间首 < CHAR_EARLY_BOUNDARY 时，窗口起点收敛到 0，并保证覆盖到
             max(CHAR_EARLY_TOTAL, end_pos + 1 + CHAR_LOOKAHEAD_BEATS)。

    扫描优先级固定为：区间内 -> 前置窗口 -> 后置窗口；各段内部保持剧情顺序。
    因此即使人物超过上限，也优先保留这场高光区间真正出现的人。
    """
    n = len(flat)
    if start_pos < CHAR_EARLY_BOUNDARY:
        window_start = 0
        window_end = min(n, max(CHAR_EARLY_TOTAL, end_pos + 1 + CHAR_LOOKAHEAD_BEATS))
    else:
        window_start = max(0, start_pos - CHAR_LOOKBACK_BEATS)
        window_end = min(n, end_pos + 1 + CHAR_LOOKAHEAD_BEATS)

    scan_positions = (
        list(range(start_pos, end_pos + 1))
        + list(range(window_start, start_pos))
        + list(range(end_pos + 1, window_end))
    )
    seen: set = set()
    result: List[Dict[str, str]] = []
    for i in scan_positions:
        _, beat = flat[i]
        speaker_id = (beat.get("speaker_id") or "").strip()
        if not speaker_id or speaker_id in NARRATOR_IDS:
            continue
        combination = (beat.get("speaker_combination") or "").strip()
        if not combination:
            continue
        variant = _combination_to_variant(combination)
        if not variant:
            continue
        real_name = speaker_to_name.get(speaker_id) or speaker_id
        key = (real_name, variant)
        if key in seen:
            continue
        seen.add(key)
        result.append({"name": real_name, "variant": variant})
        if len(result) >= CHAR_MAX_VARIANTS:
            break
    return result


def _initial_visual_asset_requirements(visual_entities: List[Dict]) -> List[Dict]:
    """Create the logical identity obligations before actual files exist."""
    requirements: List[Dict] = []
    for entity in visual_entities or []:
        if not isinstance(entity, dict):
            continue
        name = str(entity.get("name") or "").strip()
        if not name:
            continue
        policy = str(entity.get("reference_policy") or "required_identity").strip()
        requirements.append(
            {
                "role": "character",
                "entity_id": _to_camel(name),
                "entity_name": name,
                "entity_type": entity.get("entity_type") or "human",
                "reference_policy": policy,
                "required": policy == "required_identity",
                "aliases": list(entity.get("aliases") or []),
            }
        )
    return requirements


# ============================================================
# 主流程
# ============================================================

def run(task_id: str) -> int:
    """为所有区间 CG 写 cg 表初始记录（source_node / beat_range / characters）。

    Returns:
        本次写入/刷新的 CG 条数。
    """
    engine = _get_engine()
    plots_data = _load_table(engine, "plots", task_id)
    if not plots_data:
        logger.error(f"plots 表中未找到 task_id={task_id}，无法构建 cg 表")
        return 0
    chars_data = _load_table(engine, "characters", task_id) or {}
    existing = _load_table(engine, "cg", task_id) or {}

    speaker_to_name = _build_speaker_to_name(chars_data)
    flat = _flatten_in_node_order(plots_data)
    groups = _group_cg_ranges(flat)

    if not groups:
        logger.info(f"plots 中没有任何带 cg 标注的 beat（task_id={task_id}），cg 表不写入")
        # 仍确保表存在，便于下游查询
        ensure_cg_table(engine)
        return 0

    cg_data: Dict[str, Dict] = {}

    written = 0
    for cg_id, g in groups.items():
        spec = dict(existing.get(cg_id) or {})
        characters = list(spec.get("characters") or [])
        if not characters:
            characters = _collect_characters_for_range(
                flat, g["start_pos"], g["end_pos"], speaker_to_name
            )
        if "cg_visual_entities" in spec:
            visual_entities = list(spec.get("cg_visual_entities") or [])
        else:
            # Historical compatibility: old plots duplicated this field.  New
            # tasks never reach this fallback because the annotation draft in
            # cg.data is authoritative.
            visual_entities = list(g.get("visual_entities") or [])
        spec["source_node"] = g["source_node"]
        spec["beat_range"] = [g["start_beat_id"], g["end_beat_id"]]
        spec["characters"] = characters
        spec["cg_visual_entities"] = visual_entities
        spec["visual_asset_requirements"] = _initial_visual_asset_requirements(
            spec["cg_visual_entities"]
        )
        # 占位字段，后续模块填
        spec.setdefault("char_img_att", [])
        spec.setdefault("scene_img_att", [])
        spec.setdefault("init_description", "")
        spec.setdefault("cg_prompt", "")
        cg_data[cg_id] = spec
        written += 1
        logger.info(
            f"[{cg_id}] node={g['source_node']} range={spec['beat_range']} "
            f"characters={len(characters)} "
            f"visible_entities={len(spec['cg_visual_entities'])}"
        )

    _upsert_cg(engine, task_id, cg_data)
    logger.info(f"✅ 完成：cg 表写入 {written} 条 (task_id={task_id})")
    return written


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        dotenv_path = Path(project_root) / ".env"
        if dotenv_path.exists():
            load_dotenv(dotenv_path=dotenv_path)
    except ImportError:
        pass

    parser = argparse.ArgumentParser(description="为区间 CG 写 cg 表初始记录")
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args()

    count = run(task_id=args.task_id)
    print(f"完成，共写入 {count} 条 CG 规格")
