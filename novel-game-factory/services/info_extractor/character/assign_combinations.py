"""
人物状态组合分配器 (Character Combination Assigner)

根据剧情 beats 为每个人物分配合适的状态组合（性格/身体/服装变体），
再根据 plots 中实际写入的 speaker_combination 反推 is_active。

流程：
1. 读取 plots、characters 和 story_tree_json 数据
2. 单组合人物直接逐 beat 回填；多组合人物按“人物 + node”调用 LLM，
   一次提供该节点全部相关 beats、真实父节点尾部固定剧情区间、
   全部组合及其性格/身体/服装详情。
3. refs 只作为高权重证据，不作为候选硬筛选；每个可见 beat 都必须独立得到组合。
4. 扫描 plots[*].speaker_combination，精确反推每个组合的 is_active。
5. 在同一数据库事务中写回 plots 与 characters。

用法（在 novel-game-factory 目录下执行）：

# 指定 task_id
python -m novel-game-factory.services.info_extractor.character.assign_combinations --task-id <task_id>

# 强制重新分配（即使 plots 已有 speaker_combination 也覆盖重来）
python -m novel-game-factory.services.info_extractor.character.assign_combinations --force --task-id

说明：不加 --force 时，只有所有可见人物 beat 都已有合法组合，且 is_active 与
plots 实际使用完全一致，才视为主管道 Step 8 已完成；使用 --force 可强制重新分配。
本脚本原地更新 plots（beat 的 speaker_combination）与 characters（组合 is_active 标记）两张表。

"""

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_project_root = Path(__file__).resolve().parent.parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

_char_designer_dir = Path(__file__).resolve().parent.parent
_utils_dir = _char_designer_dir / "utils"
if str(_utils_dir) not in sys.path:
    sys.path.insert(0, str(_utils_dir))

from common.config import PLATFORM_DATABASE_URL
from sqlalchemy import create_engine, text
from logger_config import setup_logger
from llm_client import LLMClient

try:
    from info_extractor.utils.story_tree_json_parse import load_story_tree_for_player
except ImportError:
    from utils.story_tree_json_parse import load_story_tree_for_player  # type: ignore

try:
    from pypinyin import pinyin, Style as PinyinStyle
    _HAS_PYPINYIN = True
except ImportError:
    _HAS_PYPINYIN = False


def _to_camel(name: str) -> str:
    """中文名转拼音驼峰，英文名去空格首字母大写。"""
    if not name:
        return ""
    if _HAS_PYPINYIN:
        import re
        if re.search(r'[一-鿿]', name):
            parts = pinyin(name, style=PinyinStyle.NORMAL)
            return "".join(p[0].capitalize() for p in parts if p)
    # 英文：去掉非字母数字，驼峰
    import re
    words = re.sub(r'[^a-zA-Z0-9\s]', '', name).split()
    return "".join(w.capitalize() for w in words) if words else name

logger = setup_logger("AssignCombinations", "assign_combinations.log")

NARRATOR_IDS = {"PangBai", "Narrator", "旁白", "XiTong", "系统", "System"}
PARENT_CONTEXT_TAIL_BEATS = 20
PARENT_CONTEXT_MAX_CHARS = 3500
PARENT_SUMMARY_MAX_CHARS = 600
EDGE_CHOICE_MAX_CHARS = 300
CONTEXT_BEAT_CONTENT_MAX_CHARS = 500


def _get_engine():
    return create_engine(PLATFORM_DATABASE_URL, pool_pre_ping=True)


def _load_plots(engine, task_id: str) -> Dict:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT data FROM plots WHERE task_id = :tid"), {"tid": task_id}
        ).fetchone()
    if not row:
        raise ValueError(f"plots 表中未找到 task_id={task_id}")
    data = row[0]
    return json.loads(data) if isinstance(data, str) else data


def _load_characters(engine, task_id: str) -> Dict:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT data FROM characters WHERE task_id = :tid"), {"tid": task_id}
        ).fetchone()
    if not row:
        raise ValueError(f"characters 表中未找到 task_id={task_id}")
    data = row[0]
    return json.loads(data) if isinstance(data, str) else data


def _load_story_tree(engine, task_id: str) -> Dict:
    """读取故事树；缺失时允许退化为仅使用当前 node 剧情。"""
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT data FROM story_tree_json WHERE task_id = :tid LIMIT 1"),
            {"tid": task_id},
        ).fetchone()
    if not row or not row[0]:
        logger.warning(
            f"story_tree_json 中未找到 task_id={task_id}，人物组合判断将不注入父节点上下文"
        )
        return {}
    return load_story_tree_for_player(
        row[0], task_id=task_id, db_engine=engine, logger=logger
    )


def _save_assignment_results(
    engine,
    task_id: str,
    plots_data: Dict,
    chars_data: Dict,
) -> None:
    """在同一平台库事务中原子写回 plots 与 characters。"""
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE plots SET data = cast(:d as jsonb), update_time = NOW() "
                "WHERE task_id = :tid"
            ),
            {"d": json.dumps(plots_data, ensure_ascii=False), "tid": task_id},
        )
        conn.execute(
            text(
                "UPDATE characters SET data = cast(:d as jsonb), update_time = NOW() "
                "WHERE task_id = :tid"
            ),
            {"d": json.dumps(chars_data, ensure_ascii=False), "tid": task_id},
        )


def _timeline_item(beat: Dict, pos: int, content_limit: int) -> Dict:
    """把 plots beat 转为供上下文展示的稳定精简结构。"""
    return {
        "pos": pos,
        "beat_id": beat.get("beat_id"),
        "speaker_id": beat.get("speaker_id", "") or "-",
        "speaker_name": beat.get("speaker_name") or "",
        "full_name": beat.get("full_name") or "",
        "beat_type": beat.get("beat_type", "") or "-",
        "scene_id": beat.get("scene_id"),
        "emotion": beat.get("emotion") or "",
        "content": str(beat.get("content") or "")[:content_limit],
        "src_lines": beat.get("_src_lines") or [None, None],
    }


def _format_timeline_item(item: Dict, marker: str) -> str:
    src = item.get("src_lines") or [None, None]
    line_label = f"L{src[0]}-{src[1]}" if src[0] is not None else "L?"
    display_name = item.get("full_name") or item.get("speaker_name") or "-"
    return (
        f"{marker} [{item.get('pos')}] {line_label} "
        f"scene={item.get('scene_id') or '-'} type={item.get('beat_type') or '-'} "
        f"speaker={item.get('speaker_id') or '-'} name={display_name} "
        f"emotion={item.get('emotion') or '-'}: "
        f"{item.get('content') or ''}"
    )


def _collect_char_beats(plots_data: Dict) -> Dict[str, List[Dict]]:
    """
    按 speaker_id 收集所有 beats，并附带每条 beat 在原节点中的位置 (`pos`)
    以及节点全部 beats 的快照 (`node_seq`)。供下游构造剧情上下文窗口。
    """
    char_beats: Dict[str, List[Dict]] = {}
    for node_id, beats in plots_data.items():
        if not isinstance(beats, list):
            continue
        # 节点级快照保留完整顺序，供 LLM 判断人物状态连续性和显式/隐式切换信号。
        node_seq = [
            _timeline_item(b, i, CONTEXT_BEAT_CONTENT_MAX_CHARS)
            for i, b in enumerate(beats)
            if isinstance(b, dict)
        ]

        for i, beat in enumerate(beats):
            sid = beat.get("speaker_id", "")
            if (
                not sid
                or sid in NARRATOR_IDS
                or beat.get("beat_type") == "nvl_thought"
            ):
                continue
            char_beats.setdefault(sid, []).append({
                "node_id": node_id,
                "beat_id": beat.get("beat_id"),
                "speaker_name": beat.get("speaker_name"),
                "scene_id": beat.get("scene_id"),
                "emotion": beat.get("emotion"),
                "beat_type": beat.get("beat_type"),
                "content": (beat.get("content") or "")[:800],
                "src_lines": beat.get("_src_lines") or [None, None],
                "pos": i,
                "node_seq": node_seq,
            })
    return char_beats


def _build_parent_tail(beats: Any) -> List[Dict]:
    """读取父节点最后固定 N 条 beat，并从较早端裁剪到字符预算内。"""
    if not isinstance(beats, list):
        return []

    start = max(0, len(beats) - PARENT_CONTEXT_TAIL_BEATS)
    items = [
        _timeline_item(beat, pos, CONTEXT_BEAT_CONTENT_MAX_CHARS)
        for pos, beat in enumerate(beats[start:], start=start)
        if isinstance(beat, dict)
    ]

    selected_reversed: List[Dict] = []
    used_chars = 0
    for item in reversed(items):
        line = _format_timeline_item(item, "  parent")
        extra = len(line) + (1 if selected_reversed else 0)
        if selected_reversed and used_chars + extra > PARENT_CONTEXT_MAX_CHARS:
            break
        # 单条 beat 最多只保留 500 字正文，因此即使它独占预算也应被保留。
        selected_reversed.append(item)
        used_chars += extra
    return list(reversed(selected_reversed))


def _build_parent_contexts(plots_data: Dict, story_tree: Dict) -> Dict[str, Dict]:
    """按 story_tree_json 为每个 plot node 建立分支安全的父节点尾部上下文。"""
    nodes = story_tree.get("nodes") if isinstance(story_tree, dict) else None
    if not isinstance(nodes, dict):
        return {}

    incoming: Dict[str, List[str]] = {}
    edge_choices: Dict[Tuple[str, str], str] = {}
    for parent_id, node_info in nodes.items():
        if not isinstance(node_info, dict):
            continue
        for child in node_info.get("children") or []:
            if not isinstance(child, dict):
                continue
            child_id = child.get("child_id")
            if not isinstance(child_id, str) or not child_id:
                continue
            parents = incoming.setdefault(child_id, [])
            if parent_id not in parents:
                parents.append(parent_id)
            choice_text = str(child.get("choice_text") or "").strip()
            if choice_text:
                edge_choices[(parent_id, child_id)] = choice_text[:EDGE_CHOICE_MAX_CHARS]

    contexts: Dict[str, Dict] = {}
    for node_id in plots_data:
        node_info = nodes.get(node_id)
        node_info = node_info if isinstance(node_info, dict) else {}
        current_title = str(node_info.get("title") or "").strip()

        explicit_parent = node_info.get("parent_id")
        parent_id: Optional[str] = None
        ambiguous_parents: List[str] = []
        if (
            isinstance(explicit_parent, str)
            and explicit_parent
            and explicit_parent != node_id
            and (explicit_parent in nodes or explicit_parent in plots_data)
        ):
            parent_id = explicit_parent
        else:
            parents = incoming.get(node_id, [])
            if len(parents) == 1:
                parent_id = parents[0]
            elif len(parents) > 1:
                ambiguous_parents = list(parents)
                logger.warning(
                    f"node={node_id} 存在多个父节点且无明确 parent_id：{parents}；"
                    "不注入父节点尾部，避免串分支"
                )

        parent_info = nodes.get(parent_id) if parent_id else {}
        parent_info = parent_info if isinstance(parent_info, dict) else {}
        parent_summary = str(parent_info.get("summary") or "").strip()
        parent_beats = plots_data.get(parent_id) if parent_id else None
        contexts[node_id] = {
            "current_node_id": node_id,
            "current_title": current_title,
            "parent_node_id": parent_id,
            "parent_title": str(parent_info.get("title") or "").strip(),
            "parent_summary": parent_summary[:PARENT_SUMMARY_MAX_CHARS],
            "edge_choice_text": edge_choices.get((parent_id, node_id), "")
            if parent_id
            else "",
            "parent_tail_beats": _build_parent_tail(parent_beats),
            "ambiguous_parents": ambiguous_parents,
        }
    return contexts


def _parse_character_ref(ref: Any) -> Tuple[Optional[str], Optional[int], Optional[int]]:
    """解析 `node_id_startLine_endLine`，其中 node_id 可以包含下划线。"""
    if not isinstance(ref, str):
        return None, None, None
    parts = ref.rsplit("_", 2)
    if len(parts) != 3:
        return None, None, None
    try:
        return parts[0], int(parts[1]), int(parts[2])
    except (TypeError, ValueError):
        return None, None, None


def _normalize_refs(value: Any) -> List[str]:
    refs = value if isinstance(value, list) else [value] if value else []
    result: List[str] = []
    for ref in refs:
        if isinstance(ref, str) and ref and ref not in result:
            result.append(ref)
    return result


def _character_data(char_entry: Any) -> Optional[Dict]:
    data = char_entry[0] if isinstance(char_entry, list) and char_entry else char_entry
    return data if isinstance(data, dict) else None


def _normalize_combination_entries(char_data: Dict) -> List[Dict]:
    """兼容旧字符串格式，并保证后续可统一写入 is_active。"""
    normalized: List[Dict] = []
    seen = set()
    for entry in char_data.get("valid_status_combinations") or []:
        if isinstance(entry, str):
            entry = {
                "combination": entry,
                "description": "",
                "info": "",
                "refs": [],
                "is_active": False,
            }
        if not isinstance(entry, dict):
            continue
        combo_id = entry.get("combination")
        if not isinstance(combo_id, str) or not combo_id or combo_id in seen:
            continue
        entry["refs"] = _normalize_refs(entry.get("refs"))
        normalized.append(entry)
        seen.add(combo_id)
    char_data["valid_status_combinations"] = normalized
    return normalized


def _variant_by_id(items: Any, id_field: str, wanted_id: Optional[str]) -> Dict:
    if wanted_id is None:
        return {}
    for item in items or []:
        if isinstance(item, dict) and str(item.get(id_field)) == str(wanted_id):
            return item
    return {}


def _build_combination_candidates(char_data: Dict, combinations: List[Dict]) -> List[Dict]:
    """把组合 ID 解析为完整的性格、身体和服装候选记录。"""
    candidates: List[Dict] = []
    for order, combo in enumerate(combinations):
        combo_id = combo.get("combination", "")
        parts = combo_id.rsplit("-", 3)
        personality_id = whole_body_id = clothing_id = None
        if len(parts) == 4:
            _, personality_id, whole_body_id, clothing_id = parts

        personality = _variant_by_id(
            char_data.get("personality_variants"), "personality_id", personality_id
        )
        whole_body = _variant_by_id(
            char_data.get("whole_body_variants"), "whole_body_id", whole_body_id
        )
        clothing = _variant_by_id(
            char_data.get("clothing_variants"), "clothing_id", clothing_id
        )
        candidates.append({
            "order": order,
            "combination": combo_id,
            "combination_data": combo,
            "personality": personality,
            "whole_body": whole_body,
            "clothing": clothing,
            "refs_by_source": {
                "combination": _normalize_refs(combo.get("refs")),
                "whole_body": _normalize_refs(whole_body.get("refs")),
                "clothing": _normalize_refs(clothing.get("refs")),
                "personality": _normalize_refs(personality.get("refs")),
            },
        })
    return candidates


def _beat_line_span(beat: Dict) -> Optional[Tuple[int, int]]:
    src = beat.get("src_lines") or [None, None]
    if not isinstance(src, (list, tuple)) or len(src) < 2 or src[0] is None:
        return None
    try:
        start, end = int(src[0]), int(src[1])
    except (TypeError, ValueError):
        return None
    return (end, start) if start > end else (start, end)


def _source_ref_signal(refs: List[str], beat: Dict) -> Dict[str, Any]:
    same_node_ranges: List[Tuple[int, int]] = []
    for ref in refs:
        node_id, start, end = _parse_character_ref(ref)
        if node_id != beat.get("node_id") or start is None or end is None:
            continue
        same_node_ranges.append((end, start) if start > end else (start, end))

    if not refs:
        return {"status": "no_refs", "ranges": [], "distance": None}
    if not same_node_ranges:
        return {"status": "different_node", "ranges": [], "distance": None}

    beat_span = _beat_line_span(beat)
    if beat_span is None:
        return {"status": "same_node", "ranges": same_node_ranges, "distance": None}

    beat_start, beat_end = beat_span
    distances = []
    for start, end in same_node_ranges:
        if max(beat_start, start) <= min(beat_end, end):
            return {"status": "overlap", "ranges": same_node_ranges, "distance": 0}
        distances.append(start - beat_end if beat_end < start else beat_start - end)
    return {
        "status": "same_node",
        "ranges": same_node_ranges,
        "distance": min(distances) if distances else None,
    }


def _candidate_ref_signal(candidate: Dict, beat: Dict) -> Dict[str, Any]:
    details = {
        source: _source_ref_signal(refs, beat)
        for source, refs in candidate.get("refs_by_source", {}).items()
    }
    source_weights = {
        "combination": 4,
        "whole_body": 3,
        "clothing": 3,
        "personality": 1,
    }
    score = 0
    for source, detail in details.items():
        weight = source_weights[source]
        if detail["status"] == "overlap":
            score += weight * 1000
        elif detail["status"] == "same_node":
            distance = detail.get("distance")
            score += weight * max(1, 100 - min(distance or 0, 99))
    return {"details": details, "fallback_score": score}


def _format_ref_signal(candidate: Dict, beat: Dict) -> str:
    signal = _candidate_ref_signal(candidate, beat)
    pieces = []
    labels = {
        "combination": "组合",
        "whole_body": "身体",
        "clothing": "服装",
        "personality": "性格",
    }
    for source, detail in signal["details"].items():
        status = detail["status"]
        if status == "overlap":
            desc = "与当前 beat 行号重叠"
        elif status == "same_node":
            ranges = ",".join(f"{s}-{e}" for s, e in detail["ranges"])
            distance = detail.get("distance")
            suffix = f"，最近距离 {distance} 行" if distance is not None else ""
            desc = f"同节点但不重叠({ranges}{suffix})"
        elif status == "different_node":
            desc = "refs 在其他节点"
        else:
            desc = "无 refs"
        pieces.append(f"{labels[source]}={desc}")
    return "；".join(pieces)


def _format_candidate(candidate: Dict) -> str:
    def _dump(value: Dict) -> str:
        return json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"))

    # is_active 是本步骤最终反推的结果，不能作为本轮判断输入形成循环偏置。
    combination_data = {
        key: value
        for key, value in candidate["combination_data"].items()
        if key != "is_active"
    }
    return (
        f"### {candidate['combination']}\n"
        f"组合 info: {combination_data.get('info') or '-'}\n"
        f"组合 description: {combination_data.get('description') or '-'}\n"
        f"组合记录: {_dump(combination_data)}\n"
        f"对应身体变体: {_dump(candidate['whole_body'])}\n"
        f"对应服装变体: {_dump(candidate['clothing'])}\n"
        f"对应性格变体（低权重，仅辅助理解人物）: {_dump(candidate['personality'])}"
    )


def _format_node_context(node_seq: List[Dict], target_beats: List[Dict]) -> str:
    target_index_by_pos = {beat["pos"]: i + 1 for i, beat in enumerate(target_beats)}
    lines = []
    for item in node_seq:
        target_index = target_index_by_pos.get(item["pos"])
        marker = f">> TARGET#{target_index}" if target_index else "  context"
        lines.append(_format_timeline_item(item, marker))
    return "\n".join(lines)


def _format_parent_context(parent_context: Optional[Dict]) -> str:
    """格式化固定父节点尾部上下文；没有可靠父节点时明确说明原因。"""
    if not parent_context:
        return "无：story_tree_json 未提供当前节点的父节点信息。"

    ambiguous = parent_context.get("ambiguous_parents") or []
    if ambiguous:
        return (
            "无：当前节点存在多个父节点且没有明确 parent_id，"
            f"为避免串分支未注入父节点尾部。候选父节点={ambiguous}"
        )

    parent_id = parent_context.get("parent_node_id")
    if not parent_id:
        return "无：当前节点是根节点，或 story_tree_json 中没有可确认的父节点。"

    lines = [
        f"当前节点标题：{parent_context.get('current_title') or '-'}",
        f"父节点：{parent_id}",
        f"父节点标题：{parent_context.get('parent_title') or '-'}",
        f"进入当前节点的选择/路径：{parent_context.get('edge_choice_text') or '-'}",
    ]
    summary = parent_context.get("parent_summary")
    if summary:
        lines.append(f"父节点摘要（低权重辅助，若与 plots 冲突以 plots 为准）：{summary}")

    tail = parent_context.get("parent_tail_beats") or []
    if not tail:
        lines.append("父节点末尾 beats：无可用 plots 数据")
    else:
        lines.append(
            f"父节点末尾固定剧情区间（最后最多 {PARENT_CONTEXT_TAIL_BEATS} 条，"
            f"字符安全上限 {PARENT_CONTEXT_MAX_CHARS}）："
        )
        lines.extend(_format_timeline_item(item, "  parent") for item in tail)
    return "\n".join(lines)


def _fallback_combination(
    candidates: List[Dict],
    beat: Dict,
    previous_combination: Optional[str],
) -> str:
    """仅在 LLM 输出缺失/非法时使用：连续性 → refs 软证据 → 稳定首项。"""
    valid_ids = {candidate["combination"] for candidate in candidates}
    if previous_combination in valid_ids:
        return previous_combination

    scored = [
        (_candidate_ref_signal(candidate, beat)["fallback_score"], -candidate["order"], candidate)
        for candidate in candidates
    ]
    score, _, best = max(scored, key=lambda item: (item[0], item[1]))
    return best["combination"] if score > 0 else candidates[0]["combination"]


def _parse_llm_assignments(result: Any, beat_count: int, valid_ids: set) -> Dict[int, str]:
    if not isinstance(result, dict):
        return {}
    items = result.get("assignments")
    if not isinstance(items, list):
        return {}
    assignments: Dict[int, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("index")) - 1
        except (TypeError, ValueError):
            continue
        combo_id = item.get("combination")
        if 0 <= index < beat_count and combo_id in valid_ids and index not in assignments:
            assignments[index] = combo_id
    return assignments


def _assign_node_beats(
    llm: LLMClient,
    char_name: str,
    character_profile: Dict,
    node_id: str,
    beats: List[Dict],
    candidates: List[Dict],
    parent_context: Optional[Dict],
) -> Tuple[Dict[Tuple[str, int], str], str]:
    """一次 LLM 调用判断一个人物在一个节点中的每个可见 beat。"""
    valid_ids = {candidate["combination"] for candidate in candidates}
    candidates_text = "\n\n".join(_format_candidate(c) for c in candidates)
    node_context = _format_node_context(beats[0].get("node_seq", []), beats)
    parent_context_text = _format_parent_context(parent_context)
    beat_blocks = []
    for index, beat in enumerate(beats, start=1):
        ref_signals = "\n".join(
            f"- {candidate['combination']}: {_format_ref_signal(candidate, beat)}"
            for candidate in candidates
        )
        beat_blocks.append(
            f"### TARGET#{index}\n"
            f"beat_id={beat.get('beat_id')} pos={beat.get('pos')} "
            f"原文行={beat.get('src_lines')} scene={beat.get('scene_id') or '-'} "
            f"emotion={beat.get('emotion') or '-'}\n"
            f"内容: {beat.get('content') or ''}\n"
            f"各组合 refs 软证据（只影响判断权重，绝不限制候选）:\n{ref_signals}"
        )

    system = (
        "你是视觉小说人物状态连续性与立绘分配专家。你必须为给出的每一个 TARGET beat "
        "准确选择当时人物可见的身体、年龄/身份阶段、伤病/生死状态、服装、配饰和武器组合。\n"
        "判断规则：\n"
        "1. 每个 beat 都要独立判断，同时阅读当前节点完整时间线和父节点末尾固定剧情区间，理解人物在当前时刻应以哪种状态出现。\n"
        "2. 必须把当前剧情语义与每个组合的 info、description，以及对应身体和服装的完整详情逐一比较；不能只寻找“换装”“受伤”等字面关键词。\n"
        "3. 默认保持前后剧情合理连续，但剧情阶段、时间跨度、人物身份、活动场合、重新登场方式等强隐性语义足以说明状态变化时，即使没有直接写出换装动作，也必须切换到更符合当前时刻的组合。\n"
        "4. 当前 beat、旁白和上下文中的年龄、身体、伤病、生死、身份、服装、配饰和武器语义优先级最高；性格只作低权重辅助，普通情绪变化本身绝不能触发身体或服装组合切换。\n"
        "5. 所有候选组合始终都可选。refs 是高权重定位证据，但抽取范围可能偏移或缺失，绝不能用 refs 排除候选；剧情语义、组合 info/description 和身体服装详情可以推翻 refs。\n"
        "6. 父节点摘要仅为低权重辅助；父节点尾部 plots 和当前节点剧情优先。父节点上下文也不能作为硬绑定状态。\n"
        "7. 必须恰好返回每个 TARGET 一条结果，不得遗漏、重复或返回候选之外的 ID。"
    )
    beats_text = "\n\n".join(beat_blocks)
    base_user = (
        f"角色：{char_name}\n节点：{node_id}\n"
        f"人物基础资料：{json.dumps(character_profile, ensure_ascii=False)}\n"
        f"父节点末尾固定剧情上下文（仅参与综合语义判断）：\n{parent_context_text}\n\n"
        f"全部候选组合（共 {len(candidates)} 个，全部可选）：\n{candidates_text}\n\n"
        f"节点完整 beat 时间线（TARGET 为本角色需要回填的可见 beat）：\n{node_context}\n\n"
        f"逐 beat 判断材料：\n{beats_text}\n\n"
        "返回 JSON：{\"assignments\":["
        "{\"index\":1,\"combination\":\"候选ID\","
        "\"reason\":\"概括决定状态的剧情证据\"}, ...]}。"
    )

    accepted: Dict[int, str] = {}
    for attempt in range(2):
        correction = ""
        if attempt:
            missing = [i + 1 for i in range(len(beats)) if i not in accepted]
            correction = (
                f"\n\n上一次输出未通过完整性校验。请重新返回全部 {len(beats)} 个 TARGET，"
                f"尤其检查索引 {missing}；只能使用这些 ID：{sorted(valid_ids)}。"
            )
        try:
            result = llm.chat_completion_json(
                system,
                base_user + correction,
                temperature=0.1,
                max_tokens=min(10000, max(3000, 220 * len(beats) + 1200)),
            )
        except Exception as exc:
            logger.warning(f"LLM 分配失败 ({char_name}, {node_id}, attempt={attempt + 1}): {exc}")
            break
        accepted.update(_parse_llm_assignments(result, len(beats), valid_ids))
        if len(accepted) == len(beats):
            break
        logger.warning(
            f"LLM 分配不完整 ({char_name}, {node_id}): "
            f"合法结果 {len(accepted)}/{len(beats)}，准备校正"
        )

    result_map: Dict[Tuple[str, int], str] = {}
    previous: Optional[str] = None
    for index, beat in enumerate(beats):
        combo_id = accepted.get(index)
        if combo_id is None:
            combo_id = _fallback_combination(candidates, beat, previous)
            logger.warning(
                f"  {char_name}/{node_id}/pos={beat['pos']} 使用兜底组合 {combo_id}"
            )
        result_map[(node_id, beat["pos"])] = combo_id
        previous = combo_id
    return result_map, previous or candidates[0]["combination"]


def _assign_beats_to_combinations(
    llm: Optional[LLMClient],
    char_name: str,
    char_data: Dict,
    beats: List[Dict],
    parent_contexts: Optional[Dict[str, Dict]] = None,
) -> Dict[Tuple[str, int], str]:
    """为每个可见 beat 分配组合；多组合时按人物 + node 调用 LLM。"""
    combinations = _normalize_combination_entries(char_data)
    if not combinations or not beats:
        return {}

    candidates = _build_combination_candidates(char_data, combinations)
    if len(candidates) == 1:
        combo_id = candidates[0]["combination"]
        return {(beat["node_id"], beat["pos"]): combo_id for beat in beats}
    if llm is None:
        raise ValueError("多组合人物需要 LLMClient")

    node_groups: Dict[str, List[Dict]] = {}
    for beat in beats:
        node_groups.setdefault(beat["node_id"], []).append(beat)

    result_map: Dict[Tuple[str, int], str] = {}
    character_profile = {
        "name": char_data.get("name") or char_name,
        "other_names": char_data.get("other_names") or [],
        "info": char_data.get("info") or "",
    }
    for node_id, node_beats in node_groups.items():
        # 父节点只能来自 story_tree_json 的 parent_id / children 反向索引，
        # 不能把 plots 字典序或某个兄弟节点误当成上一段剧情。
        node_result, _ = _assign_node_beats(
            llm,
            char_name,
            character_profile,
            node_id,
            node_beats,
            candidates,
            (parent_contexts or {}).get(node_id),
        )
        result_map.update(node_result)
    return result_map


def _build_character_id_map(chars_data: Dict) -> Dict[str, str]:
    """构建 plots speaker_id/name/拼音 ID 到 characters key 的映射。

    仅使用 key、name 及其拼音驼峰。刻意不纳入 other_names——别名可能属于不同
    角色，用别名做映射会错误合并。别名/称呼类未命中的归属交由
    _resolve_unmapped_speakers 的 LLM 语义兜底处理（结合上下文剧情 + 置信度阈值）。
    """
    id_to_key: Dict[str, str] = {}
    for key, char_entry in (chars_data or {}).items():
        char_data = _character_data(char_entry)
        if char_data is None:
            continue
        id_to_key[key] = key
        name = char_data.get("name", "")
        if name:
            id_to_key[name] = key
        camel_key = _to_camel(key)
        if camel_key:
            id_to_key[camel_key] = key
        if name:
            camel_name = _to_camel(name)
            if camel_name:
                id_to_key[camel_name] = key
    return id_to_key


ATTRIBUTION_CONFIDENCE_THRESHOLD = 0.8
ATTRIBUTION_CONTEXT_MAX_CHARS = 2500


def _has_assignable_character(chars_data: Dict, char_key: Optional[str]) -> bool:
    """char_key 对应角色存在且有可用的状态组合。"""
    if not char_key:
        return False
    char_data = _character_data((chars_data or {}).get(char_key))
    return char_data is not None and bool(_normalize_combination_entries(char_data))


def _format_attribution_context(char_beats: Dict[str, List[Dict]], sid: str) -> str:
    """为待归属 speaker 拼出「所在节点时间线 + 该 speaker 的台词位置」上下文。

    速配不上人名时，仅靠称呼不够，必须结合上下文剧情、周围旁白与对话，才能
    顺着语义判断这个 speaker 到底是谁。这里给 LLM 提供其出现的节点完整时间线，
    并标出该 speaker 的行。
    """
    beats = char_beats.get(sid) or []
    if not beats:
        return "（无上下文）"

    blocks: List[str] = []
    seen_nodes = set()
    used_chars = 0
    for beat in beats:
        node_id = beat.get("node_id")
        if node_id in seen_nodes:
            continue
        seen_nodes.add(node_id)
        target_positions = {
            b.get("pos") for b in beats if b.get("node_id") == node_id
        }
        lines = [f"节点 {node_id}（★ 标记为该 speaker 的台词）："]
        for item in beat.get("node_seq") or []:
            marker = "  ★" if item.get("pos") in target_positions else "   "
            lines.append(_format_timeline_item(item, marker))
        block = "\n".join(lines)
        if used_chars and used_chars + len(block) > ATTRIBUTION_CONTEXT_MAX_CHARS:
            break
        blocks.append(block)
        used_chars += len(block)
    return "\n\n".join(blocks)


def _resolve_unmapped_speakers(
    llm: Optional[LLMClient],
    unmapped: List[str],
    char_beats: Dict[str, List[Dict]],
    chars_data: Dict,
) -> Dict[str, str]:
    """查表未命中的 speaker_id，用 LLM 结合上下文剧情语义判断其归属到哪个已建档角色。

    喂给 LLM 的不只是原始 speaker_id 和称呼，还有该 speaker 出现处的完整节点
    时间线（周围旁白与对话），让它顺着剧情理解这个 speaker 是谁。

    只接受高置信度（>= 阈值）且明确指向某个有组合的候选角色的结果；判不准则
    返回 null 并被跳过——宁可漏也不要把不同角色错误合并。群体称呼（如「同事们」）
    没有单一对应角色，会因低置信度/null 而被安全跳过。
    """
    if not unmapped or llm is None:
        return {}

    candidates = []
    for key, char_entry in (chars_data or {}).items():
        char_data = _character_data(char_entry)
        if char_data is None or not _normalize_combination_entries(char_data):
            continue
        candidates.append(
            {
                "char_key": key,
                "name": char_data.get("name") or key,
                "other_names": char_data.get("other_names") or [],
                "info": (char_data.get("info") or "")[:300],
            }
        )
    if not candidates:
        return {}

    valid_keys = {c["char_key"] for c in candidates}
    target_blocks = []
    for sid in unmapped:
        beats = char_beats.get(sid) or []
        speaker_name = next(
            (b.get("speaker_name") for b in beats if b.get("speaker_name")), None
        )
        context = _format_attribution_context(char_beats, sid)
        target_blocks.append(
            f"### speaker_id={sid}  称呼={speaker_name or '-'}  台词条数={len(beats)}\n"
            f"上下文剧情：\n{context}"
        )

    system = (
        "你是视觉小说角色归属专家。给定一批无法直接匹配的 speaker（含原始 speaker_id、"
        "称呼、以及其出现处的上下文剧情时间线）和一份已建档角色候选表（含 name、别名 "
        "other_names、info），结合上下文剧情语义判断每个 speaker 是否就是某个候选角色本人。\n"
        "严格规则：\n"
        "1. 不要只看称呼字面，要结合上下文剧情、周围旁白与对话，理解这个 speaker 在剧情里是谁。\n"
        "2. 只有当 speaker 明确、唯一地指向某一个候选角色本人时才归属；称呼是该角色的"
        "别名/简称/全称/剧情指代变体，属于同一人。\n"
        "3. 群体/复数称呼（如「同事们」「众人」）不对应任一单个角色，必须返回 char_key=null。\n"
        "4. 无法确定、可能指向多个不同角色、或候选表里根本没有此人时，一律返回 char_key=null。"
        "宁可返回 null 也不要勉强归属。\n"
        "5. confidence 为 0~1 的置信度；只有非常确定时才给高分。\n"
        "6. char_key 必须原样取自候选表，不得杜撰。"
    )
    targets_text = "\n\n".join(target_blocks)
    user = (
        f"待归属 speaker（共 {len(unmapped)} 个）：\n{targets_text}\n\n"
        f"已建档角色候选：\n{json.dumps(candidates, ensure_ascii=False, indent=2)}\n\n"
        "返回 JSON：{\"attributions\":["
        "{\"speaker_id\":\"...\",\"char_key\":\"候选表中的key或null\","
        "\"confidence\":0.0,\"reason\":\"结合上下文的归属依据\"}, ...]}。"
    )

    try:
        result = llm.chat_completion_json(system, user, temperature=0.1, max_tokens=3000)
    except Exception as exc:
        logger.warning(f"语义归属兜底 LLM 调用失败，全部跳过: {exc}")
        return {}

    resolved: Dict[str, str] = {}
    items = result.get("attributions") if isinstance(result, dict) else None
    for item in items or []:
        if not isinstance(item, dict):
            continue
        sid = item.get("speaker_id")
        char_key = item.get("char_key")
        try:
            confidence = float(item.get("confidence"))
        except (TypeError, ValueError):
            confidence = 0.0
        if sid not in char_beats or char_key not in valid_keys:
            continue
        if confidence < ATTRIBUTION_CONFIDENCE_THRESHOLD:
            logger.info(
                f"语义归属跳过 {sid}: 置信度 {confidence:.2f} < "
                f"{ATTRIBUTION_CONFIDENCE_THRESHOLD}（{item.get('reason') or '-'}）"
            )
            continue
        resolved[sid] = char_key
        logger.info(
            f"语义归属命中 {sid} -> {char_key} "
            f"(置信度 {confidence:.2f}: {item.get('reason') or '-'})"
        )
    return resolved


def _assignment_status(plots_data: Dict, chars_data: Dict) -> Dict[str, int]:
    """校验逐 beat 分配完整性，以及 is_active 是否与 plots 实际使用完全一致。"""
    id_to_key = _build_character_id_map(chars_data)
    combo_ids_by_key: Dict[str, set] = {}
    combination_total = 0
    for char_key, char_entry in (chars_data or {}).items():
        char_data = _character_data(char_entry)
        if char_data is None:
            continue
        combo_ids = {
            combo.get("combination")
            for combo in _normalize_combination_entries(char_data)
            if combo.get("combination")
        }
        combo_ids_by_key[char_key] = combo_ids
        combination_total += len(combo_ids)

    total = assigned = missing = invalid = unmapped = non_visual = 0
    for beats in (plots_data or {}).values():
        if not isinstance(beats, list):
            continue
        for beat in beats:
            if not isinstance(beat, dict):
                continue
            if not _is_visible_character_beat(beat):
                if beat.get("speaker_combination") is not None:
                    non_visual += 1
                continue
            total += 1
            char_key = id_to_key.get(beat.get("speaker_id"))
            valid_ids = combo_ids_by_key.get(char_key or "", set())
            if not valid_ids:
                unmapped += 1
                continue
            combo_id = beat.get("speaker_combination")
            if not combo_id:
                missing += 1
            elif combo_id not in valid_ids:
                invalid += 1
            else:
                assigned += 1

    used_ids = _used_combination_ids(plots_data)
    active = activity_mismatches = 0
    for char_entry in (chars_data or {}).values():
        char_data = _character_data(char_entry)
        if char_data is None:
            continue
        for combo in _normalize_combination_entries(char_data):
            expected = combo["combination"] in used_ids
            if combo.get("is_active") is True:
                active += 1
            if combo.get("is_active") is not expected:
                activity_mismatches += 1

    return {
        "total": total,
        "assigned": assigned,
        "missing": missing,
        "invalid": invalid,
        "unmapped": unmapped,
        "non_visual": non_visual,
        "combinations": combination_total,
        "active": active,
        "activity_mismatches": activity_mismatches,
        "complete": int(
            missing == 0
            and invalid == 0
            and unmapped == 0
            and non_visual == 0
            and activity_mismatches == 0
        ),
    }


def get_assignment_status(task_id: str) -> Dict[str, int]:
    """从数据库读取 Step 4 完整度，供 main.py 和独立脚本共用。"""
    engine = _get_engine()
    return _assignment_status(_load_plots(engine, task_id), _load_characters(engine, task_id))


def _is_visible_character_beat(beat: Dict) -> bool:
    speaker_id = beat.get("speaker_id")
    return bool(
        speaker_id
        and speaker_id not in NARRATOR_IDS
        and beat.get("beat_type") != "nvl_thought"
    )


def _used_combination_ids(plots_data: Dict) -> set:
    """plots 的逐 beat speaker_combination 是人物组合使用情况的权威来源。"""
    used = set()
    for beats in (plots_data or {}).values():
        if not isinstance(beats, list):
            continue
        for beat in beats:
            if not isinstance(beat, dict) or not _is_visible_character_beat(beat):
                continue
            combo_id = beat.get("speaker_combination")
            if isinstance(combo_id, str) and combo_id:
                used.add(combo_id)
    return used


def _sync_combination_activity(plots_data: Dict, chars_data: Dict) -> Tuple[int, int]:
    """严格按 plots 实际使用反推全部组合的 is_active。"""
    used_ids = _used_combination_ids(plots_data)
    total = active = 0
    for char_entry in (chars_data or {}).values():
        char_data = _character_data(char_entry)
        if char_data is None:
            continue
        for combo in _normalize_combination_entries(char_data):
            combo["is_active"] = combo["combination"] in used_ids
            total += 1
            if combo["is_active"]:
                active += 1
    return total, active


def _clear_non_visual_assignments(plots_data: Dict) -> int:
    """清除旁白/内心独白等不展示立绘的历史残留组合。"""
    cleared = 0
    for beats in (plots_data or {}).values():
        if not isinstance(beats, list):
            continue
        for beat in beats:
            if (
                isinstance(beat, dict)
                and not _is_visible_character_beat(beat)
                and beat.get("speaker_combination") is not None
            ):
                beat["speaker_combination"] = None
                cleared += 1
    return cleared


def _reset_assignment_state(plots_data: Dict, chars_data: Dict) -> Tuple[int, int]:
    """强制重跑前把上一轮的分配痕迹还原到干净基线（原地修改传入的 dict）。

    - characters：所有 valid_status_combinations 的 is_active 置 False
    - plots：所有 beat 的 speaker_combination 还原为 None（与 extract_plots_to_db 初始值一致）

    返回 (清空的 is_active 数, 清空的 speaker_combination 数)，仅用于日志。
    """
    combos_reset = 0
    for char_entry in (chars_data or {}).values():
        char_data = _character_data(char_entry)
        if char_data is None:
            continue
        for combo in char_data.get("valid_status_combinations") or []:
            if isinstance(combo, dict) and combo.get("is_active"):
                combo["is_active"] = False
                combos_reset += 1

    beats_reset = 0
    for node_beats in (plots_data or {}).values():
        if not isinstance(node_beats, list):
            continue
        for beat in node_beats:
            if isinstance(beat, dict) and beat.get("speaker_combination") is not None:
                beat["speaker_combination"] = None
                beats_reset += 1

    return combos_reset, beats_reset


def run(task_id: str, provider: str = "qwen", model: Optional[str] = None, force: bool = False) -> int:
    """
    主入口：为 task_id 的所有 beats 分配 speaker_combination，激活对应组合。
    返回处理的 beat 总数。

    force=False 且逐 beat 分配与 is_active 均完整一致时跳过并返回 0；
    force=True 则强制重新分配并覆盖旧的 speaker_combination 与 is_active 标记。
    """
    engine = _get_engine()
    plots_data = _load_plots(engine, task_id)
    chars_data = _load_characters(engine, task_id)

    if not force:
        status = _assignment_status(plots_data, chars_data)
        if status["complete"]:
            logger.info(
                f"✅ Step 8 已完整执行：{status['assigned']}/{status['total']} 个可见 beat "
                f"已有合法组合，{status['active']} 个组合实际启用，跳过分配"
            )
            return 0
        logger.info(
            f"Step 8 需要执行：可见 beat 共 {status['total']} 个，"
            f"missing={status['missing']} invalid={status['invalid']} "
            f"unmapped={status['unmapped']} "
            f"non_visual={status['non_visual']} "
            f"is_active_mismatch={status['activity_mismatches']}"
        )
    else:
        # 强制重跑：先把上一轮残留的 is_active / speaker_combination 全量还原，
        # 避免本轮不再命中的角色或 beat 保留旧分配导致脏数据。
        combos_reset, beats_reset = _reset_assignment_state(plots_data, chars_data)
        logger.info(
            f"🧹 强制重跑：已还原 {combos_reset} 个 is_active 标记、"
            f"{beats_reset} 个 beat 的 speaker_combination"
        )

    cleared = _clear_non_visual_assignments(plots_data)
    if cleared:
        logger.info(f"🧹 已清除 {cleared} 个非可视 beat 的历史 speaker_combination")

    story_tree = _load_story_tree(engine, task_id)
    parent_contexts = _build_parent_contexts(plots_data, story_tree)
    parent_context_count = sum(
        1
        for context in parent_contexts.values()
        if context.get("parent_node_id") and context.get("parent_tail_beats")
    )
    logger.info(
        f"父节点剧情上下文：{parent_context_count}/{len(parent_contexts)} 个 plot nodes "
        f"读取到父节点尾部（最多 {PARENT_CONTEXT_TAIL_BEATS} beats）"
    )

    llm: Optional[LLMClient] = None

    # 按 speaker_id 收集 beats
    char_beats = _collect_char_beats(plots_data)

    # 构建 speaker_id -> char_key 映射（characters 表的 key 可能是名字或 ID）
    # 尝试直接匹配，再尝试通过 name 字段匹配
    id_to_key = _build_character_id_map(chars_data)

    logger.info(f"plots 中发现角色 speaker_id: {list(char_beats.keys())[:10]}")
    logger.info(f"characters 表 key 样本: {list(id_to_key.keys())[:20]}")

    def _is_unmapped(speaker_id: str) -> bool:
        char_key = id_to_key.get(speaker_id)
        char_data = _character_data(chars_data.get(char_key)) if char_key else None
        return char_data is None or not _normalize_combination_entries(char_data)

    # 主映射刻意排除 other_names（别名可能属不同角色，字面合并会错并）。
    # 别名/描述性称呼未命中的 speaker，交给 LLM 结合别名 + info + 上下文剧情
    # 语义归属（带置信度阈值，群体称呼安全跳过），命中后并入 id_to_key，
    # 由后续主循环按原始 speaker_id 正常回填组合。plots 的 speaker 字段不动。
    unmapped = [sid for sid in char_beats if _is_unmapped(sid)]
    if unmapped:
        if llm is None:
            llm = LLMClient(
                provider=provider,
                model=model,
                temperature=0.1,
                max_tokens=4000,
            )
        resolved = _resolve_unmapped_speakers(llm, unmapped, char_beats, chars_data)
        for sid, char_key in resolved.items():
            id_to_key[sid] = char_key
            logger.info(f"语义兜底并入映射: speaker_id={sid} -> characters key={char_key}")

    unassignable = [sid for sid in char_beats if _is_unmapped(sid)]
    if unassignable:
        logger.error(
            "以下可见人物 beat 无法找到 characters/valid_status_combinations，"
            f"将跳过这些角色、不中断流程：{unassignable}"
        )

    total_assigned = 0

    for speaker_id, beats in char_beats.items():
        char_key = id_to_key.get(speaker_id)
        if not char_key:
            logger.debug(f"跳过未知角色: {speaker_id}")
            continue

        char_data = _character_data(chars_data[char_key])
        if char_data is None:
            logger.warning(f"跳过格式异常的角色数据: {char_key}")
            continue
        combinations = _normalize_combination_entries(char_data)
        if not combinations:
            logger.warning(f"{speaker_id} 无 valid_status_combinations，跳过")
            continue

        char_name = char_data.get("name", speaker_id)
        logger.info(f"处理 {char_name}: beats={len(beats)}, combos={len(combinations)}")

        # 单组合无需 LLM；多组合按“人物 + node”逐 beat 综合判断。
        if len(combinations) > 1 and llm is None:
            llm = LLMClient(
                provider=provider,
                model=model,
                temperature=0.1,
                max_tokens=4000,
            )
        assignment = _assign_beats_to_combinations(
            llm,
            char_name,
            char_data,
            beats,
            parent_contexts=parent_contexts,
        )

        # 逐位置回写，避免 beat_id 缺失或重复造成覆盖。
        for node_id, node_beats in plots_data.items():
            if not isinstance(node_beats, list):
                continue
            for pos, beat in enumerate(node_beats):
                if beat.get("speaker_id") != speaker_id:
                    continue
                if beat.get("beat_type") == "nvl_thought":
                    continue
                key = (node_id, pos)
                if key in assignment:
                    beat["speaker_combination"] = assignment[key]
                    total_assigned += 1

    # Scene 同款权威链路：先看 plots 实际用了什么，再精确反推 is_active。
    combination_total, active_total = _sync_combination_activity(plots_data, chars_data)
    final_status = _assignment_status(plots_data, chars_data)
    if not final_status["complete"]:
        logger.error(
            "人物组合回填完整性校验未通过（不中断流程，跳过无法匹配的 beat）："
            f"missing={final_status['missing']} invalid={final_status['invalid']} "
            f"unmapped={final_status['unmapped']} "
            f"non_visual={final_status['non_visual']} "
            f"is_active_mismatch={final_status['activity_mismatches']}"
        )

    _save_assignment_results(engine, task_id, plots_data, chars_data)
    logger.info(
        f"✅ 完成：逐 beat 分配 {total_assigned} 条；"
        f"组合实际启用 {active_total}/{combination_total} (task_id={task_id})"
    )
    return total_assigned


if __name__ == "__main__":
    import argparse

    try:
        from dotenv import load_dotenv
        dotenv_path = _project_root / "novel-game-factory" / ".env"
        if dotenv_path.exists():
            load_dotenv(dotenv_path=dotenv_path)
    except ImportError:
        pass

    parser = argparse.ArgumentParser(description="为 beats 分配人物状态组合")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--provider", default="qwen", choices=["qwen", "openai", "claude"])
    parser.add_argument("--model", default=None)
    parser.add_argument("--force", action="store_true", help="强制重新分配，即使 plots 已有 speaker_combination")
    args = parser.parse_args()

    count = run(task_id=args.task_id, provider=args.provider, model=args.model, force=args.force)
    print(f"完成，共分配 {count} 个 beats")
