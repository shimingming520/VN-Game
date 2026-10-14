"""
CG 提示词提取器（区间模型）

读取 info_extractor/cg/cg_build.py 已写入的 source_node / beat_range / characters / cg_visual_entities，
按照当前 Plot 的 CG 区间模型构造三段式上下文
为每张 CG 生成一个结构化的单一瞬间视觉设计，再由代码建立唯一主场景、
逐人物身份参考需求与 cg_prompt。最终 Image N 编号由 scene_painter 在确认
参考文件实际存在后确定。

依赖：
    plots          所有 beat（用于上下文窗口、环境锚）
    scenes         最近 2 个 substantive 场景的 image_prompt（用于环境/光照一致性参考）
    characters     角色外观（character_brief；仅作识别参考，外观由参考图锁定）
    story_tree_json global_style（用于后处理拼最终 cg_prompt）

用法：
    python novel-game-factory/services/info_extractor/cg/cg_prompt.py --task-id <task_id>
"""

import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

service_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if service_dir not in sys.path:
    sys.path.insert(0, service_dir)

import json
import re
import asyncio
import yaml
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from sqlalchemy import create_engine, text
from openai import AsyncOpenAI, BadRequestError
from tqdm.asyncio import tqdm
from tenacity import retry, stop_after_attempt, wait_random_exponential

from utils.logger_config import setup_logger
from common.config import DATABASE_URL, PLATFORM_DATABASE_URL
from common.artifact_io import load_local_json_artifact
from common.cg_data import load_cg_contract, write_local_cg_contract
from common.visual_style import (
    ASSET_SCENE,
    STYLE_MODE_LLM,
    get_asset_style,
    load_visual_style_config,
)

try:
    from info_extractor.utils.scene_id_restore import (
        collect_variant_scene_ids,
        resolve_base_scene_id,
    )
except ImportError:
    from utils.scene_id_restore import (  # type: ignore
        collect_variant_scene_ids,
        resolve_base_scene_id,
    )

try:
    from common.usage_tracker import tracker as _usage_tracker
except Exception:
    _usage_tracker = None

logger = setup_logger("cg_extractor", "cg_extractor.log")

# cg/plots/characters/novel_tasks 已迁平台库；scenes 仍在业务库
DB_URL = PLATFORM_DATABASE_URL
API_KEY = os.getenv("GPT_IMAGE_API_KEY") or os.getenv("OPENAI_API_KEY")
BASE_URL = os.getenv("GPT_IMAGE_BASE_URL", "https://ai.farmmx.com").rstrip("/")
if not BASE_URL.endswith("/v1"):
    BASE_URL = f"{BASE_URL}/v1"
MODEL_NAME = "gpt-5.6-sol"

MAX_CONCURRENT_REQUESTS = 5

# §5.1 substantive beat 窗口
PLOT_BEFORE = 20
PLOT_AFTER = 10
PLOT_EARLY_BOUNDARY = 30   # 全局 raw 下标 < 30 时 → 早期补偿
RECENT_SCENES_FOR_REF = 2  # 最近 2 个非 cg 场景作环境/光照参考
CG_REFERENCE_CONTRACT_SCHEMA = "cg-reference-v1"

CONTROL_BEAT_TYPES = {"branch_start", "branch_end", "option", "variable_update", "cg"}
NARRATOR_IDS = {"PangBai", "Narrator", "旁白"}

_PROMPT_DIR = Path(__file__).parent.parent / "prompts" / "cg_prompts"

# 中性质量约束，不覆盖 global_style 的媒介、笔触、轮廓和质感。
CG_CLEAN_STYLE_SUFFIX = (
    "high clarity, coherent lighting, clear well-defined complete shapes, "
    "stable anatomy and spatial relationships"
)


def _load_prompt(filename: str) -> str:
    with open(_PROMPT_DIR / filename, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)["content"].strip()


def _compose_cg_prompt(global_style: str, init_description: str) -> str:
    """把 LLM 的 init_description 拼成最终图生图提示词。

    形如: "<global_style>, <init_description>, <neutral quality suffix>"。
    """
    parts = []
    if global_style:
        parts.append(global_style)
    if init_description:
        parts.append(init_description.strip().rstrip(",").strip())
    parts.append(CG_CLEAN_STYLE_SUFFIX)
    return ", ".join(p for p in parts if p)


def _chinese_to_camel(name: str) -> str:
    """中文真名转拼音驼峰（与 char_designer/cg_write_char.py 对齐）。"""
    if not name:
        return ""
    try:
        from pypinyin import pinyin, Style as PinyinStyle  # type: ignore
        if re.search(r"[一-鿿]", name):
            parts = pinyin(name, style=PinyinStyle.NORMAL)
            return "".join(p[0].capitalize() for p in parts if p)
    except ImportError:
        pass
    words = re.sub(r"[^a-zA-Z0-9\s]", "", name).split()
    return "".join(w.capitalize() for w in words) if words else name


def _combination_to_variant(combination: str) -> str:
    """'凌妙妙-1-10-5' → 'LingMiaoMiao_1-10-5_calm'（与 cg_write_char 完全一致）。"""
    if not combination:
        return ""
    parts = combination.rsplit("-", 3)
    if len(parts) != 4:
        return ""
    name, p, b, c = parts
    safe_name = _chinese_to_camel(name)
    return f"{safe_name}_{p}-{b}-{c}_calm"


# ============================================================================
# DB 读取
# ============================================================================

def _load_json(value: Any) -> Any:
    """jsonb 列驱动可能已解析成 dict/list，字符串/字节才需要 json.loads。"""
    if isinstance(value, (str, bytes, bytearray)):
        return json.loads(value)
    return value


def fetch_task_context(target_task_id: Optional[str]) -> Tuple[Optional[str], Optional[Dict], Optional[Dict], Optional[Dict], Optional[Dict], str]:
    """一次性把 cg / plots / scenes / characters / global_style 拉出来。

    Returns:
        task_id, cg_data, plots_data, scenes_data, characters_data, global_style
    """
    engine = create_engine(DB_URL)
    with engine.connect() as conn:
        if not target_task_id:
            row = conn.execute(text("SELECT task_id FROM cg ORDER BY id DESC LIMIT 1")).fetchone()
            if not row:
                logger.warning("⚠️ cg 表为空，需要先跑 info_extractor/cg/cg_build.py")
                return None, None, None, None, None, ""
            target_task_id = row[0]
            logger.info(f"自动锁定最新任务: {target_task_id}")

        cg_data = load_cg_contract(target_task_id, db_engine=engine)
        if cg_data is None:
            logger.warning(f"⚠️ task_id={target_task_id} 没有可用 CG 合同")
            return target_task_id, None, None, None, None, ""

        plots_row = conn.execute(text("SELECT data FROM plots WHERE task_id = :tid"), {"tid": target_task_id}).fetchone()
        plots_data = (
            _load_json(plots_row[0])
            if plots_row
            else load_local_json_artifact(target_task_id, "plots.json", logger=logger)
        )
        if not plots_data:
            logger.error(f"❌ plots 表与共享 plots.json 均无 task_id={target_task_id}")
            return target_task_id, cg_data, None, None, None, ""

        scenes_data = {}
        with create_engine(DATABASE_URL).connect() as biz_conn:
            scenes_row = biz_conn.execute(text("SELECT data FROM scenes WHERE task_id = :tid"), {"tid": target_task_id}).fetchone()
        if scenes_row:
            scenes_raw = _load_json(scenes_row[0])
            scenes_data = scenes_raw or {}

        chars_row = conn.execute(text("SELECT data FROM characters WHERE task_id = :tid"), {"tid": target_task_id}).fetchone()
        characters_data = {}
        if chars_row:
            characters_data = _load_json(chars_row[0])

    content_only_llm_route = (
        load_visual_style_config(target_task_id).get("mode") == STYLE_MODE_LLM
    )
    if content_only_llm_route:
        global_style = ""
        style_log = "(automatic-anchor route; content-only CG prompt)"
    else:
        global_style = get_asset_style(target_task_id, ASSET_SCENE)
        style_log = global_style
    logger.info(
        f"🚀 锁定任务 Task ID: {target_task_id} | "
        f"scene_global_style={style_log} | cg 占位 {len(cg_data or {})} 条"
    )
    return target_task_id, cg_data, plots_data, scenes_data, characters_data, global_style


def save_cg_data(task_id: str, cg_data: Dict[str, Any]) -> None:
    """整张覆盖写回 cg 表的 data 字段。"""
    payload = json.dumps(cg_data, ensure_ascii=False)
    engine = create_engine(DB_URL)
    try:
        with engine.begin() as conn:
            result = conn.execute(
                text(
                    "UPDATE cg SET data = cast(:d as jsonb), "
                    "update_time = NOW() WHERE task_id = :tid"
                ),
                {"d": payload, "tid": task_id},
            )
            if result.rowcount != 1:
                raise RuntimeError(f"cg UPDATE rowcount={result.rowcount}")
    except Exception as exc:
        logger.warning("CG 数据库写回失败，发布共享 cg.json: %s", exc)
        write_local_cg_contract(task_id, cg_data)
        return
    logger.info(f"💾 [CG] 已写回 cg 表（task_id={task_id}）")


# ============================================================================
# 上下文窗口
# ============================================================================

def _flatten_in_node_order(plots_data: Dict) -> List[Tuple[str, Dict]]:
    """与 cg_write_char._flatten_in_node_order 对齐：按 (节点字典序, beat 顺序) 摊平。"""
    out: List[Tuple[str, Dict]] = []
    for node_id in sorted(plots_data.keys()):
        beats = plots_data.get(node_id) or []
        if not isinstance(beats, list):
            continue
        for beat in beats:
            out.append((node_id, beat))
    return out


def _is_substantive(beat: Dict) -> bool:
    """跳过控制类与空 content 的 beat。"""
    btype = beat.get("beat_type") or ""
    if btype in CONTROL_BEAT_TYPES:
        return False
    content = (beat.get("content") or "").strip()
    return bool(content)


def _find_cg_range(
    flat: List[Tuple[str, Dict]], source_node: str, beat_range: List[str]
) -> Tuple[int, int]:
    """用闭区间两个 beat_id 定位 CG 在摊平序列中的 [start, end]。"""
    if not isinstance(beat_range, list) or len(beat_range) != 2:
        return -1, -1
    start_id, end_id = str(beat_range[0]), str(beat_range[1])
    start_idx = end_idx = -1
    for idx, (node_id, beat) in enumerate(flat):
        if node_id != source_node:
            continue
        beat_id = str(beat.get("beat_id") or beat.get("id") or "")
        if beat_id == start_id and start_idx < 0:
            start_idx = idx
        if beat_id == end_id:
            end_idx = idx
    if start_idx < 0 or end_idx < start_idx:
        return -1, -1
    return start_idx, end_idx


def _format_beat_for_context(beat: Dict) -> str:
    """单条 beat 渲染成一行：

        [emotion] <full_name>[ (variant: ...)]: <content> [(env)]

    - 角色名优先取 full_name（双名机制下的稳定主名），缺失时回退 speaker_name，再回退"旁白"。
    - 非旁白 beat 若有 speaker_combination，附 (variant: <variant>)，方便 LLM 把对白和拼接图里的 tile 对上。
    - 环境锚 (location/time/feature) 附在末尾，未知/none 不附。
    """
    full_name = (beat.get("full_name") or "").strip()
    speaker_name = (beat.get("speaker_name") or "").strip()
    speaker_id = (beat.get("speaker_id") or "").strip()
    display_name = full_name or speaker_name or "旁白"

    content = (beat.get("content") or "").strip()
    emotion = beat.get("emotion") or "calm"

    variant_suffix = ""
    if speaker_id and speaker_id not in NARRATOR_IDS and display_name not in NARRATOR_IDS:
        combination = (beat.get("speaker_combination") or "").strip()
        variant = _combination_to_variant(combination)
        if variant:
            variant_suffix = f" (variant: {variant})"

    location = beat.get("location") or ""
    time_of_day = beat.get("time") or ""
    feature = beat.get("feature") or ""
    env_bits = [v for v in (location, time_of_day, feature) if v and v != "none" and v != "unknown"]
    env = f" ({'/'.join(env_bits)})" if env_bits else ""

    beat_id = str(beat.get("beat_id") or beat.get("id") or "unknown")
    return f"[{beat_id}] [{emotion}] {display_name}{variant_suffix}: {content}{env}"


def _collect_window_beats(
    flat: List[Tuple[str, Dict]], start_idx: int, end_idx: int
) -> Tuple[List[Dict], List[Dict], List[Dict]]:
    """返回 BEFORE / CG RANGE / AFTER 三段；前后只计 substantive beat。"""
    before: List[Dict] = []
    for i in range(start_idx - 1, -1, -1):
        beat = flat[i][1]
        if not _is_substantive(beat):
            continue
        before.append(beat)
        if len(before) >= PLOT_BEFORE:
            break
    before.reverse()

    after_target = PLOT_AFTER
    if start_idx < PLOT_EARLY_BOUNDARY:
        after_target = max(PLOT_AFTER, 30 - len(before))

    after: List[Dict] = []
    for i in range(end_idx + 1, len(flat)):
        beat = flat[i][1]
        if not _is_substantive(beat):
            continue
        after.append(beat)
        if len(after) >= after_target:
            break

    cg_range = [flat[i][1] for i in range(start_idx, end_idx + 1) if _is_substantive(flat[i][1])]
    return before, cg_range, after


def _build_context_text(before: List[Dict], cg_range: List[Dict], after: List[Dict]) -> str:
    """把窗口渲染成 BEFORE / CG RANGE / AFTER 三段。"""
    before_text = "\n".join(_format_beat_for_context(b) for b in before) or "(no preceding substantive beats)"
    range_text = "\n".join(_format_beat_for_context(b) for b in cg_range) or "(empty CG range)"
    after_text = "\n".join(_format_beat_for_context(b) for b in after) or "(no following substantive beats)"
    return f"=== BEFORE CG ===\n{before_text}\n\n=== CG RANGE ===\n{range_text}\n\n=== AFTER CG ===\n{after_text}"


def _collect_scene_img_att(
    cg_range: List[Dict],
    before: List[Dict],
    after: List[Dict],
    known_variant_ids: set,
) -> List[str]:
    """区间内场景优先选择，忽略区域后缀后最多返回两张二级原图文件名。"""
    def _safe(scene_id: str) -> str:
        return re.sub(r"[^\w\-]", "_", scene_id) + ".png"

    def _scene_id_of(beat: Dict) -> str:
        sid = (beat.get("scene_id") or "").strip()
        if not sid or sid.startswith("cg_"):
            return ""
        # scene_fangjian_7_1 / scene_fangjian_7_2 都使用二级原图 scene_fangjian_7。
        return resolve_base_scene_id(sid, known_variant_ids) or sid

    inside: List[str] = []
    for beat in cg_range:
        sid = _scene_id_of(beat)
        if sid and sid not in inside:
            inside.append(sid)

    def _nearest(beats) -> str:
        for beat in beats:
            sid = _scene_id_of(beat)
            if sid:
                return sid
        return ""

    if len(inside) == 0:
        picked = [_nearest(reversed(before)), _nearest(after)]
    elif len(inside) == 1:
        picked = [inside[0], _nearest(reversed(before)), _nearest(after)]
    elif len(inside) == 2:
        picked = inside
    else:
        middle_sid = _scene_id_of(cg_range[len(cg_range) // 2]) or inside[len(inside) // 2]
        last_sid = _scene_id_of(cg_range[-1]) or inside[-1]
        picked = [middle_sid, last_sid]

    unique = []
    for sid in picked:
        if sid and sid not in unique:
            unique.append(sid)
        if len(unique) >= 2:
            break
    return [_safe(sid) for sid in unique]


def _scene_id_for_reference(beat: Mapping[str, Any], known_variant_ids: set) -> str:
    raw_scene_id = str(beat.get("scene_id") or "").strip()
    if not raw_scene_id or raw_scene_id.startswith("cg_"):
        return ""
    return resolve_base_scene_id(raw_scene_id, known_variant_ids) or raw_scene_id


def _select_primary_scene_reference(
    selected_beat_ids: List[str],
    cg_range: List[Dict],
    before: List[Dict],
    after: List[Dict],
    known_variant_ids: set,
) -> Dict[str, Any]:
    """Select exactly one existing logical scene for the chosen CG instant."""
    selected = {str(value).strip() for value in selected_beat_ids if str(value).strip()}
    ordered_candidates: List[Dict] = []
    ordered_candidates.extend(
        beat
        for beat in cg_range
        if str(beat.get("beat_id") or beat.get("id") or "") in selected
    )
    ordered_candidates.extend(cg_range)
    ordered_candidates.extend(reversed(before))
    ordered_candidates.extend(after)

    seen = set()
    for beat in ordered_candidates:
        marker = id(beat)
        if marker in seen:
            continue
        seen.add(marker)
        scene_id = _scene_id_for_reference(beat, known_variant_ids)
        if not scene_id:
            continue
        filename = re.sub(r"[^\w\-]", "_", scene_id) + ".png"
        return {
            "scene_id": scene_id,
            "entity_name": str(
                beat.get("location") or scene_id
            ).strip(),
            "filename": filename,
            "source_beat_id": str(beat.get("beat_id") or beat.get("id") or ""),
        }

    return {
        "scene_id": "",
        "entity_name": "CG primary location",
        "filename": "",
        "source_beat_id": "",
    }


def _collect_window_speakers(beats: List[Dict]) -> List[str]:
    """从 BEFORE+AFTER 窗口里收集出现过的 full_name（去旁白、去重，保留首次出现顺序）。"""
    seen: set = set()
    names: List[str] = []
    for beat in beats:
        full_name = (beat.get("full_name") or "").strip()
        speaker_id = (beat.get("speaker_id") or "").strip()
        # 取 full_name 优先，兜底用 speaker_name —— 旁白排除
        name = full_name or (beat.get("speaker_name") or "").strip()
        if not name or name in NARRATOR_IDS or speaker_id in NARRATOR_IDS:
            continue
        if name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def _build_scene_anchor(flat: List[Tuple[str, Dict]], cg_index: int) -> str:
    """取 CG 之前最近一条普通 beat 的 location/time/feature，作为环境锚。"""
    for i in range(cg_index - 1, -1, -1):
        beat = flat[i][1]
        if beat.get("beat_type") in CONTROL_BEAT_TYPES:
            continue
        location = beat.get("location") or "unknown"
        time_of_day = beat.get("time") or "unknown"
        feature = beat.get("feature") or "none"
        return f"location: {location}\ntime: {time_of_day}\nfeature: {feature}"
    return "location: unknown\ntime: unknown\nfeature: none"


def _normalize_scene_variants(scenes_data: Any) -> List[Tuple[str, str, str]]:
    """把 scenes 表（list of scene，每个含 variants）摊平成 (source_node, scene_id, prompt)。

    - scenes 中登记的是 variant 二级原图 ID；beat.scene_id 可能额外带区域后缀。
    - 视觉描述取 variant 的 shot 字段。
    - source_node 从 variant.refs[0] 反解（格式 {source_node}_{start}_{end}）。
    """
    items: List[Tuple[str, str, str]] = []
    if not isinstance(scenes_data, list):
        return items
    for scene in scenes_data:
        if not isinstance(scene, dict):
            continue
        for variant in scene.get("variants") or []:
            if not isinstance(variant, dict):
                continue
            sid = (variant.get("scene_id") or "").strip()
            if not sid or sid.startswith("cg_"):
                continue
            prompt = (variant.get("shot") or "").strip()
            if not prompt:
                continue
            node = ""
            refs = variant.get("refs") or []
            if refs:
                m = re.match(r"^(.*)_\d+_\d+$", str(refs[0]))
                if m:
                    node = m.group(1)
            items.append((node, sid, prompt))
    return items


def _build_recent_scene_prompts(
    scenes_data: Any,
    cg_source_node: str,
    cg_range: List[Dict],
    global_style: str = "",
) -> str:
    """区间内场景 prompt 优先，不足时再用前序场景补足到两条。

    scenes 表里每条 entry 有 variants，scene_id 不直接对应 beat_id，
    所以按 (source_node, scene_id 字典序) 排序后，过滤出当前 cg 之前的同/前章场景。
    """
    if not scenes_data:
        return "(no scenes available)"

    items = _normalize_scene_variants(scenes_data)

    # 排序：先按 source_node 字典序，再按 scene_id 字典序
    items.sort(key=lambda x: (x[0], x[1]))

    # 过滤：保留 source_node 字典序 <= cg_source_node 的（同章 / 前章）
    candidates = [it for it in items if it[0] <= cg_source_node]
    if not candidates:
        candidates = items  # 早期 CG 没有前置场景就退化为整体最近

    by_scene = {sid: (node, prompt) for node, sid, prompt in items}
    known_variant_ids = set(by_scene)
    picked: List[Tuple[str, str, str]] = []
    for beat in cg_range:
        raw_sid = (beat.get("scene_id") or "").strip()
        sid = resolve_base_scene_id(raw_sid, known_variant_ids) or raw_sid
        if sid in by_scene and not any(old_sid == sid for _, old_sid, _ in picked):
            node, prompt = by_scene[sid]
            picked.append((node, sid, prompt))
        if len(picked) >= RECENT_SCENES_FOR_REF:
            break
    for item in reversed(candidates):
        if len(picked) >= RECENT_SCENES_FOR_REF:
            break
        if not any(old_sid == item[1] for _, old_sid, _ in picked):
            picked.append(item)
    if not picked:
        return "(no scenes available)"
    lines = []
    # scene image_prompt 可能来自旧任务或外部模块。这里把全局画风前缀剥离，
    # 只向 CG 构思 LLM 提供环境、光照和空间线索，避免它复制/再决定画风。
    style_prefix = str(global_style or "").strip()
    for node, sid, prompt in picked:
        clean_prompt = prompt.strip()
        if style_prefix and clean_prompt.startswith(style_prefix):
            clean_prompt = clean_prompt[len(style_prefix):].lstrip(" ,.;")
        lines.append(f"- [{node} / {sid}] {clean_prompt}")
    return "\n".join(lines)


_CHAR_FIELDS = [
    "gender", "age", "age_category", "hair", "eye_color", "eye_expression",
    "facial", "body_type", "body_detail", "skin", "outfit", "accessories",
]


def _build_char_lookup(characters_data: Dict) -> Dict[str, Dict]:
    """从 characters 表 data 里建一个 {name -> char_obj} 的查找表。

    characters 表 key 通常就是中文真名；同时把 char_obj.name 字段也注册进去
    （应对 key 与 name 不一致的少数情况）。
    """
    lookup: Dict[str, Dict] = {}
    for key, entry in (characters_data or {}).items():
        char_obj = entry[0] if isinstance(entry, list) and entry else entry
        if not isinstance(char_obj, dict):
            continue
        lookup[key] = char_obj
        name_field = char_obj.get("name")
        if name_field:
            lookup.setdefault(name_field, char_obj)
        speaker_id = str(char_obj.get("speaker_id") or "").strip()
        if speaker_id:
            lookup.setdefault(speaker_id, char_obj)
        aliases = char_obj.get("other_names") or char_obj.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        for alias in aliases:
            alias = str(alias).strip()
            if alias:
                lookup.setdefault(alias, char_obj)
    return lookup


def _format_char_attrs(char: Dict) -> str:
    """把 characters 表里某条人物记录拍平成 'gender=...; age=...; hair=...' 形式。"""
    if not char:
        return ""
    appearance = char.get("appearance") or {}
    clothing = char.get("clothing") or {}
    expression = char.get("expression") or {}
    attrs = []
    for f in _CHAR_FIELDS:
        val = char.get(f) or appearance.get(f) or clothing.get(f) or expression.get(f)
        if isinstance(val, list):
            val = ", ".join(str(v) for v in val if v)
        if val:
            attrs.append(f"{f}={val}")
    return "; ".join(attrs)


def _string_list(value: Any) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        value = [value]
    result: List[str] = []
    for item in value:
        text_value = str(item or "").strip()
        if text_value and text_value not in result:
            result.append(text_value)
    return result


def _active_character_variant(char: Mapping[str, Any]) -> str:
    combinations = char.get("valid_status_combinations") or []
    active = [
        str(item.get("combination") or "").strip()
        for item in combinations
        if isinstance(item, Mapping)
        and item.get("is_active")
        and str(item.get("combination") or "").strip()
    ]
    if not active:
        return ""
    combination = sorted(active)[0]
    parts = combination.rsplit("-", 3)
    if len(parts) != 4:
        return ""
    speaker_id = str(char.get("speaker_id") or "").strip()
    identity = speaker_id or _chinese_to_camel(parts[0])
    return f"{identity}_{parts[1]}-{parts[2]}-{parts[3]}_calm"


def _normalize_visible_characters(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: List[Dict[str, Any]] = []
    seen = set()
    for raw in value:
        if isinstance(raw, str):
            raw = {"name": raw}
        if not isinstance(raw, Mapping):
            continue
        name = str(raw.get("name") or raw.get("entity_name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(
            {
                "name": name,
                "aliases": _string_list(raw.get("aliases")),
                "entity_type": str(raw.get("entity_type") or "human").strip(),
                "placement": str(raw.get("placement") or "").strip(),
                "action": str(raw.get("action") or "").strip(),
                "expression": str(raw.get("expression") or "").strip(),
                "body_language": str(raw.get("body_language") or "").strip(),
                "costume_state": str(raw.get("costume_state") or "").strip(),
                "identity_description": str(
                    raw.get("identity_description")
                    or raw.get("identity_reference")
                    or ""
                ).strip(),
                "evidence_beat_ids": _string_list(raw.get("evidence_beat_ids")),
                "evidence_quote": str(raw.get("evidence_quote") or "").strip(),
            }
        )
    return result


def _normalize_cg_visual_spec(
    parsed: Mapping[str, Any],
    declared_entities: List[Dict],
    cg_range: List[Dict],
) -> Optional[Dict[str, Any]]:
    """Validate the LLM decision and preserve annotation-declared silent people."""
    structured_keys = {
        "selected_beat_ids",
        "required_visible_characters",
        "optional_visible_characters",
        "offscreen_characters",
    }
    if not structured_keys.issubset(parsed.keys()):
        return None
    init_description = str(
        parsed.get("init_description") or parsed.get("image_prompt") or ""
    ).strip()
    if not init_description:
        return None

    required = _normalize_visible_characters(parsed.get("required_visible_characters"))
    optional = _normalize_visible_characters(parsed.get("optional_visible_characters"))
    offscreen = _normalize_visible_characters(parsed.get("offscreen_characters"))

    required_names = {item["name"] for item in required}
    optional_names = {item["name"] for item in optional}
    offscreen_names = {item["name"] for item in offscreen}
    for entity in declared_entities or []:
        if not isinstance(entity, Mapping):
            continue
        name = str(entity.get("name") or "").strip()
        policy = str(entity.get("reference_policy") or "required_identity").strip()
        if not name or policy == "prompt_only":
            continue
        record = {
            "name": name,
            "aliases": _string_list(entity.get("aliases")),
            "entity_type": str(entity.get("entity_type") or "human"),
            "placement": "",
            "action": "",
            "expression": "",
            "body_language": "",
            "costume_state": "",
            "identity_description": "",
            "evidence_beat_ids": [],
            "evidence_quote": "",
        }
        if policy == "required_identity" and name not in required_names:
            optional = [item for item in optional if item["name"] != name]
            offscreen = [item for item in offscreen if item["name"] != name]
            required.append(record)
            required_names.add(name)
            optional_names.discard(name)
            offscreen_names.discard(name)
        elif (
            policy == "recommended_identity"
            and name not in required_names
            and name not in optional_names
            and name not in offscreen_names
        ):
            optional.append(record)
            optional_names.add(name)

    optional = [item for item in optional if item["name"] not in required_names]
    visible_names = required_names | {item["name"] for item in optional}
    offscreen = [item for item in offscreen if item["name"] not in visible_names]

    valid_beat_ids = [
        str(beat.get("beat_id") or beat.get("id") or "").strip()
        for beat in cg_range
        if str(beat.get("beat_id") or beat.get("id") or "").strip()
    ]
    selected = [
        beat_id
        for beat_id in _string_list(parsed.get("selected_beat_ids"))
        if beat_id in valid_beat_ids
    ]
    if not selected and valid_beat_ids:
        selected = [valid_beat_ids[len(valid_beat_ids) // 2]]

    def _mapping(field: str) -> Dict[str, Any]:
        value = parsed.get(field)
        return dict(value) if isinstance(value, Mapping) else {}

    return {
        "selected_beat_ids": selected,
        "single_moment": str(parsed.get("single_moment") or "").strip(),
        "init_description": init_description,
        "required_visible_characters": required,
        "optional_visible_characters": optional,
        "offscreen_characters": offscreen,
        "environment": _mapping("environment"),
        "camera": _mapping("camera"),
        "lighting": _mapping("lighting"),
        "mood": _string_list(parsed.get("mood")),
        "hard_constraints": _string_list(parsed.get("hard_constraints")),
        "negative_constraints": _string_list(parsed.get("negative_constraints")),
    }


def _identity_reference_prompt(
    character: Mapping[str, Any],
    fallback_identity: str,
) -> str:
    name = str(character.get("name") or "character").strip()
    entity_type = str(character.get("entity_type") or "human").strip()
    identity = str(character.get("identity_description") or fallback_identity or "").strip()
    costume = str(character.get("costume_state") or "").strip()
    identity_text = identity or (
        "use only source-supported stable identity details; keep unspecified facial, hair, "
        "body and clothing details simple, restrained and contextually plausible"
    )
    costume_text = costume or "stable source-supported base clothing only"
    return (
        "Create one reusable full-body visual-novel identity reference image. "
        f"Identity: {name}. Entity type: {entity_type}. "
        f"Stable appearance: {identity_text}. Costume: {costume_text}. "
        "Exactly one person, complete body visible from head to toe including both feet, "
        "front or three-quarter view, neutral standing pose, clear face and silhouette, "
        "plain pure white background, generous empty margin below the feet, no environment, "
        "no action scene, no duplicate views, no character-sheet panels, no text, no watermark. "
        "This is the canonical identity reference for all later CG images; preserve facial "
        "features, age, hairstyle, body proportions, base costume, palette and silhouette."
    )


def _resolve_character_requirement(
    character: Mapping[str, Any],
    *,
    required: bool,
    characters_data: Dict,
    cg_characters: List[Dict],
) -> Dict[str, Any]:
    lookup = _build_char_lookup(characters_data)
    requested_name = str(character.get("name") or "").strip()
    raw_character = lookup.get(requested_name) or {}
    canonical_name = str(raw_character.get("name") or requested_name).strip()
    speaker_id = str(raw_character.get("speaker_id") or "").strip()
    aliases = _string_list(
        raw_character.get("other_names") or raw_character.get("aliases")
    )
    aliases.extend(
        value for value in _string_list(character.get("aliases")) if value not in aliases
    )

    candidate_names = {requested_name, canonical_name, speaker_id, *aliases}
    variant = ""
    for item in cg_characters or []:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("name") or "").strip() in candidate_names:
            variant = str(item.get("variant") or "").strip()
            if variant:
                break
    if not variant and raw_character:
        variant = _active_character_variant(raw_character)
    elif variant and speaker_id:
        suffix_match = re.search(r"_(\d+-\d+-\d+)_calm$", variant)
        if suffix_match:
            variant = f"{speaker_id}_{suffix_match.group(1)}_calm"

    entity_id = speaker_id or _chinese_to_camel(canonical_name or requested_name)
    entity_id = re.sub(r"[^\w\-]", "_", entity_id).strip("_") or "CgIdentity"
    file_candidates: List[str] = []
    if variant:
        calm_variant = variant if variant.endswith("_calm") else f"{variant}_calm"
        file_candidates.append(f"images/characters/{calm_variant}_LS.png")
    identity_path = f"images/references/{entity_id}.png"
    if identity_path not in file_candidates:
        file_candidates.append(identity_path)

    fallback_identity = _format_char_attrs(raw_character) if raw_character else ""
    identity_description = str(
        character.get("identity_description") or fallback_identity
    ).strip()
    return {
        "role": "character",
        "entity_id": entity_id,
        "entity_name": canonical_name or requested_name,
        "entity_type": str(character.get("entity_type") or "human").strip(),
        "required": required,
        "reference_policy": "required_identity" if required else "recommended_identity",
        "speaker_id": speaker_id,
        "variant": variant,
        "aliases": aliases,
        "file_candidates": file_candidates,
        "identity_description": identity_description,
        "identity_prompt": _identity_reference_prompt(character, fallback_identity),
        "preserve": identity_description or (
            "face, age, hairstyle, body proportions, base costume, palette and silhouette"
        ),
        "placement": str(character.get("placement") or "").strip(),
        "action": str(character.get("action") or "").strip(),
        "expression": str(character.get("expression") or "").strip(),
        "body_language": str(character.get("body_language") or "").strip(),
        "costume_state": str(character.get("costume_state") or "").strip(),
        "evidence_beat_ids": _string_list(character.get("evidence_beat_ids")),
        "evidence_quote": str(character.get("evidence_quote") or "").strip(),
    }


def _build_reference_requirements(
    visual_spec: Mapping[str, Any],
    *,
    scene_reference: Mapping[str, Any],
    characters_data: Dict,
    cg_characters: List[Dict],
) -> List[Dict[str, Any]]:
    scene_id = str(scene_reference.get("scene_id") or "").strip()
    scene_filename = str(scene_reference.get("filename") or "").strip()
    requirements: List[Dict[str, Any]] = [
        {
            "role": "scene",
            "entity_id": scene_id,
            "entity_name": str(
                scene_reference.get("entity_name") or scene_id or "CG primary location"
            ).strip(),
            "required": True,
            "file_candidates": (
                [f"images/scenes/{scene_filename}"] if scene_filename else []
            ),
            "preserve": (
                "location identity, architecture, spatial layout, materials, palette and lighting continuity"
            ),
            "source_beat_id": str(scene_reference.get("source_beat_id") or ""),
        }
    ]
    for character in visual_spec.get("required_visible_characters") or []:
        requirements.append(
            _resolve_character_requirement(
                character,
                required=True,
                characters_data=characters_data,
                cg_characters=cg_characters,
            )
        )
    for character in visual_spec.get("optional_visible_characters") or []:
        requirements.append(
            _resolve_character_requirement(
                character,
                required=False,
                characters_data=characters_data,
                cg_characters=cg_characters,
            )
        )
    return requirements


def _build_character_brief(
    characters_data: Dict,
    cg_characters: List[Dict],
    window_speakers: List[str],
) -> str:
    """构造分两段的 character_brief：

      [REFERENCED]  在 cg_characters 里、已分配 variant、有独立 calm LS 参考 → 标 variant
      [UNREFERENCED] 出现在 BEFORE/AFTER 窗口但没进 cg_characters → 不带 variant，列详细外观字段供 LLM 描绘

    REFERENCED 段告诉 LLM "这些人已经有独立身份参考图"；
    UNREFERENCED 段告诉 LLM "这些人如果实际入画，就按字段详细描绘外貌"。
    """
    lookup = _build_char_lookup(characters_data)

    referenced_names: set = set()
    referenced_lines: List[str] = []
    for c in cg_characters or []:
        name = (c.get("name") or "").strip()
        variant = (c.get("variant") or "").strip()
        if not name or name in referenced_names:
            continue
        referenced_names.add(name)
        if variant:
            referenced_lines.append(
                f"- {name} (variant: {variant}) — 已有独立 calm LS 身份参考图；"
                "仍需明确该角色在 CG 中的位置、动作、表情和肢体语言"
            )
        else:
            # 理论上 cg_characters 里都有 variant；没有的话仍归入"已知"，但提示无 variant
            referenced_lines.append(f"- {name} (no variant assigned)")

    unreferenced_lines: List[str] = []
    for name in window_speakers:
        if name in referenced_names:
            continue
        char = lookup.get(name) or {}
        attrs_text = _format_char_attrs(char)
        if attrs_text:
            unreferenced_lines.append(
                f"- {name} (NO reference image) — 若入画请按以下字段详细描绘外貌: {attrs_text}"
            )
        else:
            unreferenced_lines.append(
                f"- {name} (NO reference image, characters table 无外观记录) — 若入画请按剧情上下文合理推断外貌后详细描绘"
            )

    parts: List[str] = []
    if referenced_lines:
        parts.append("[REFERENCED — 已有独立人物参考图]\n" + "\n".join(referenced_lines))
    else:
        parts.append("[REFERENCED — 有参考图]\n(无)")

    if unreferenced_lines:
        parts.append("[UNREFERENCED — 无参考图，若入画请按字段详细描绘外貌]\n" + "\n".join(unreferenced_lines))
    else:
        parts.append("[UNREFERENCED — 无参考图]\n(无)")

    return "\n\n".join(parts)


def _build_declared_visual_entities(
    entities: List[Dict], characters_data: Dict
) -> str:
    """Describe annotation-declared silent people to the CG planning LLM."""
    if not entities:
        return "(none declared; still inspect CG RANGE for silent visible people)"
    lookup = _build_char_lookup(characters_data)
    lines: List[str] = []
    for entity in entities:
        if not isinstance(entity, Mapping):
            continue
        name = str(entity.get("name") or "").strip()
        if not name:
            continue
        aliases = _string_list(entity.get("aliases"))
        attrs = _format_char_attrs(lookup.get(name) or {})
        bits = [
            f"name={name}",
            f"entity_type={entity.get('entity_type') or 'human'}",
            f"reference_policy={entity.get('reference_policy') or 'required_identity'}",
        ]
        if aliases:
            bits.append(f"aliases={', '.join(aliases)}")
        if attrs:
            bits.append(f"known_identity={attrs}")
        lines.append("- " + "; ".join(bits))
    return "\n".join(lines) or "(none declared)"


# ============================================================================
# LLM 调用
# ============================================================================

def _robust_json_extract(content: str) -> str:
    match = re.search(r"```json\s*(.*?)\s*```", content, re.DOTALL)
    if match:
        return match.group(1).strip()
    start = content.find("{")
    end = content.rfind("}")
    if start != -1 and end != -1:
        return content[start:end + 1]
    return content.strip()


def _render_user_prompt(template: str, fields: Dict[str, str]) -> str:
    text_out = template
    for key, val in fields.items():
        text_out = text_out.replace("{" + key + "}", val)
    return text_out


@retry(stop=stop_after_attempt(5), wait=wait_random_exponential(min=1, max=60))
async def _generate_cg_prompt(client: AsyncOpenAI, cg_id: str, fields: Dict[str, str], semaphore: asyncio.Semaphore):
    system_prompt = _load_prompt("cg_extractor_sys_en.yaml")
    user_template = _load_prompt("cg_extractor_user_en.yaml")
    user_message = _render_user_prompt(user_template, fields)

    async with semaphore:
        try:
            response = await client.chat.completions.create(
                model=MODEL_NAME,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                temperature=0.4,
            )
            if _usage_tracker and response.usage:
                _usage_tracker.add_llm(response.usage.prompt_tokens, response.usage.completion_tokens, MODEL_NAME)
            return cg_id, response.choices[0].message.content
        except BadRequestError as e:
            logger.warning(f"⚠️ CG {cg_id} 内容审核拦截: {e}；尝试用 scene_extractor 安全版兜底")
            try:
                safe_sys = _load_prompt("../scene_prompts/scene_extractor_safe_sys_en.yaml")
                safe_user = _load_prompt("../scene_prompts/scene_extractor_safe_user_en.yaml")
                # safe 模板要求 {scene_id} / {context_text}，复用 cg_id 与 BEFORE/AFTER 上下文
                safe_message = safe_user.replace("{scene_id}", cg_id).replace(
                    "{context_text}", fields.get("context_text", "")
                )
                response = await client.chat.completions.create(
                    model=MODEL_NAME,
                    messages=[
                        {"role": "system", "content": safe_sys},
                        {"role": "user", "content": safe_message},
                    ],
                    temperature=0.4,
                )
                if _usage_tracker and response.usage:
                    _usage_tracker.add_llm(response.usage.prompt_tokens, response.usage.completion_tokens, MODEL_NAME)
                logger.info(f"✅ CG {cg_id} 安全版兜底成功")
                return cg_id, response.choices[0].message.content
            except Exception as inner:
                logger.error(f"❌ CG {cg_id} 安全版兜底失败: {inner}")
                return cg_id, None


# ============================================================================
# 主流程
# ============================================================================

async def main(target_task_id: Optional[str] = None):
    task_id, cg_data, plots_data, scenes_data, characters_data, global_style = fetch_task_context(target_task_id)

    if task_id is None:
        return  # 未指定 task 且 cg 表为空
    if not cg_data:
        logger.info("无 CG 占位，跳过")
        return
    if not plots_data:
        logger.error("plots 缺失，无法构造上下文，终止")
        return

    if _usage_tracker:
        _usage_tracker.set_context(task_id, "info_extractor")

    flat = _flatten_in_node_order(plots_data)
    known_variant_ids = collect_variant_scene_ids(scenes_data or [])

    # 为每张 CG 构造一组 fields
    pending: List[Tuple[str, Dict[str, str]]] = []
    planning_contexts: Dict[str, Dict[str, Any]] = {}
    skipped: List[str] = []
    for cg_id, spec in cg_data.items():
        source_node = spec.get("source_node") or ""
        if not source_node:
            logger.warning(f"[{cg_id}] 缺 source_node，跳过")
            skipped.append(cg_id)
            continue
        beat_range = spec.get("beat_range") or []
        start_idx, end_idx = _find_cg_range(flat, source_node, beat_range)
        if start_idx < 0:
            logger.warning(f"[{cg_id}] 在 plots 中找不到 beat_range={beat_range}（node={source_node}），跳过")
            skipped.append(cg_id)
            continue

        before, cg_range, after = _collect_window_beats(flat, start_idx, end_idx)
        context_text = _build_context_text(before, cg_range, after)
        scene_anchor = _build_scene_anchor(flat, start_idx)
        recent_scenes = _build_recent_scene_prompts(
            scenes_data or {},
            source_node,
            cg_range,
            global_style,
        )
        window_speakers = _collect_window_speakers(before + cg_range + after)
        character_brief = _build_character_brief(
            characters_data or {},
            spec.get("characters") or [],
            window_speakers,
        )
        scene_img_att = _collect_scene_img_att(
            cg_range,
            before,
            after,
            known_variant_ids,
        )
        spec["scene_img_att"] = scene_img_att

        fields = {
            "cg_id": cg_id,
            "scene_anchor": scene_anchor,
            "character_brief": character_brief,
            "declared_visual_entities": _build_declared_visual_entities(
                spec.get("cg_visual_entities") or [], characters_data or {}
            ),
            "recent_scene_prompts": recent_scenes,
            "context_text": context_text,
        }
        planning_contexts[cg_id] = {
            "before": before,
            "cg_range": cg_range,
            "after": after,
        }
        pending.append((cg_id, fields))

    if not pending:
        logger.warning("没有可处理的 CG（全部缺少 source_node 或 plots 锚点）")
        return

    # 每个 info_extractor 步骤都由独立的 asyncio.run() 驱动。必须在当前事件
    # 循环关闭前释放 AsyncOpenAI/httpx 连接，否则客户端稍后被 GC 回收时会触发
    # "Task exception was never retrieved: Event loop is closed"。
    async with AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL) as client:
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

        async def _safe(cg_id: str, fields: Dict[str, str]):
            try:
                return await _generate_cg_prompt(client, cg_id, fields, semaphore)
            except Exception as e:
                logger.error(f"❌ CG {cg_id} 重试耗尽，跳过: {e}")
                return cg_id, None

        tasks = [_safe(cg_id, fields) for cg_id, fields in pending]

        logger.info(f"--- 开始生成 {len(tasks)} 张 CG 的 init_description ---")
        success = 0
        failed: List[str] = []

        for completed in tqdm(asyncio.as_completed(tasks), total=len(tasks), desc="Generating CG Prompts"):
            cg_id, ai_output = await completed
            if not ai_output:
                failed.append(cg_id)
                continue

            clean_json = _robust_json_extract(ai_output)
            try:
                parsed = json.loads(clean_json)
                # 正常 CG prompt 返回结构化视觉设计；内容审核兜底仍可能只返回
                # image_prompt，此时保留旧数据路径，不写入新参考契约。
                init_description = (
                    parsed.get("init_description") or parsed.get("image_prompt") or ""
                ).strip()
            except Exception as e:
                logger.error(f"❌ 解析 {cg_id} 的 JSON 失败: {e}\n原文: {clean_json}")
                failed.append(cg_id)
                continue

            if not init_description:
                logger.warning(f"⚠️ {cg_id} init_description 为空")
                failed.append(cg_id)
                continue

            cg_prompt = _compose_cg_prompt(global_style, init_description)

            spec = cg_data.get(cg_id, {})
            spec["init_description"] = init_description
            spec["cg_prompt"] = cg_prompt
            context = planning_contexts.get(cg_id) or {}
            visual_spec = _normalize_cg_visual_spec(
                parsed,
                spec.get("cg_visual_entities") or [],
                context.get("cg_range") or [],
            )
            if visual_spec is not None:
                scene_reference = _select_primary_scene_reference(
                    visual_spec.get("selected_beat_ids") or [],
                    context.get("cg_range") or [],
                    context.get("before") or [],
                    context.get("after") or [],
                    known_variant_ids,
                )
                reference_requirements = _build_reference_requirements(
                    visual_spec,
                    scene_reference=scene_reference,
                    characters_data=characters_data or {},
                    cg_characters=spec.get("characters") or [],
                )
                spec["reference_contract_schema"] = CG_REFERENCE_CONTRACT_SCHEMA
                spec["cg_visual_spec"] = visual_spec
                spec["scene_reference"] = scene_reference
                spec["reference_requirements"] = reference_requirements
                # cg-reference-v1 只使用逐人物独立参考图。清空可能由
                # 历史任务留下的拼接图附件，避免新合同继续携带旧语义。
                spec["char_img_att"] = []
                spec["visual_asset_requirements"] = [
                    requirement
                    for requirement in reference_requirements
                    if requirement.get("role") == "character"
                ]
                spec["scene_img_att"] = (
                    [scene_reference["filename"]]
                    if scene_reference.get("filename")
                    else []
                )
                logger.info(
                    "[%s] CG 参考契约已建立: scene=%s required=%s optional=%s",
                    cg_id,
                    scene_reference.get("scene_id") or "(missing)",
                    len(visual_spec.get("required_visible_characters") or []),
                    len(visual_spec.get("optional_visible_characters") or []),
                )
            else:
                spec.pop("reference_contract_schema", None)
                spec.pop("cg_visual_spec", None)
                spec.pop("scene_reference", None)
                spec.pop("reference_requirements", None)
                logger.warning(
                    "[%s] LLM 未返回完整结构化 CG 设计，保留旧 char_img_att/scene_img_att 路径",
                    cg_id,
                )
            cg_data[cg_id] = spec
            success += 1

    save_cg_data(task_id, cg_data)
    logger.info(
        f"✅ CG 提示词生成完成: 成功 {success}/{len(tasks)}, 失败 {len(failed)}, 跳过 {len(skipped)}"
    )
    if failed:
        logger.warning(f"失败的 CG: {failed}")
    if skipped:
        logger.warning(f"跳过的 CG: {skipped}")

    if _usage_tracker:
        _usage_tracker.flush()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="CG 提示词提取器")
    parser.add_argument("--task-id", type=str, default=None, help="指定task_id")
    args = parser.parse_args()
    asyncio.run(main(args.task_id))
