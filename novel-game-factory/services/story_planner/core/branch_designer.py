"""branch_designer.py - 故事拓扑规划 + What-If章节生成"""
import json
import logging
import math
import os
import re
from typing import Callable, Optional
from services.story_planner.core.llm_client import LLMClient, get_lang_instruction


# 单次摘要的最大字数（与 1w 章长方案对齐）
_SUMMARY_SEG_SIZE = 13000
logger = logging.getLogger(__name__)

# What-If 的末场必须达到这个最低衔接分数。分数是局部生成门禁：低于
# 阈值只重试当前 What-If，耗尽次数后由调用方删除/终止该可选分支，不会
# 触发整条 chapter_branch 流程重跑。
DEFAULT_WHATIF_TRANSITION_MIN_SCORE = 5


_TRANSITION_HARD_CONFLICT_PATTERNS = (
    r"(?:死亡|死后).{0,12}(?:复活|重新活着|仍然活着)",
    r"(?:确认死亡|已经死亡|生死状态).{0,18}(?:不一致|冲突|矛盾)",
    r"现实层.{0,12}(?:不一致|冲突|矛盾|不同)",
    r"(?:梦境|治疗重构).{0,12}(?:现实|主线).{0,12}(?:不一致|冲突|矛盾|不同)",
    r"(?:身份冲突|身份不一致|不是同一人|核心身份).{0,12}(?:冲突|不一致|矛盾|不同)",
    r"(?:提前揭露|提前揭示).{0,12}(?:关键事实|关键证据)",
    r"核心事实.{0,12}(?:冲突|不一致|矛盾)",
)


def _coerce_bool(value: object, default: bool = False) -> bool:
    """Parse JSON booleans defensively across permissive gateways."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "y", "是", "有"}:
        return True
    if text in {"false", "0", "no", "n", "否", "无", ""}:
        return False
    return bool(default)


def _transition_has_hard_conflict(issues: list[str], explicit: object = None) -> bool:
    """Classify only explicit narrative contradictions as repair-blocking.

    The reviewer is allowed to report a soft difference (tone, emotion, or a
    small transition detail).  Those differences should be recorded as a
    bridge warning, while the separate numeric score gate decides whether the
    current What-If needs a bounded local retry.
    """
    joined = "；".join(str(item or "") for item in issues).lower()
    inferred = any(
        re.search(pattern, joined) for pattern in _TRANSITION_HARD_CONFLICT_PATTERNS
    )
    if inferred:
        return True
    if explicit is not None and str(explicit).strip() != "":
        return _coerce_bool(explicit)
    return False


def is_acceptable_whatif_transition_review(
    review: object,
    *,
    min_score: int | None = None,
) -> bool:
    """Return whether a transition review can keep the optional branch.

    The score is a local What-If gate. A score below five is not accepted even
    when the model says ``compatible``; the caller then retries only this
    branch. Non-hard differences at or above the threshold may still be
    accepted with a warning.
    """
    if not isinstance(review, dict):
        return False
    issues = review.get("issues", [])
    if isinstance(issues, str):
        issues = [issues]
    elif not isinstance(issues, list):
        issues = [str(issues)] if issues else []
    if _transition_has_hard_conflict(
        [str(item) for item in issues],
        review.get("hard_conflict"),
    ):
        return False
    # A transport/LLM failure is not a valid narrative score. Preserve the
    # generated branch and expose the outage as a warning; the client-level
    # resilience policy is responsible for retrying the request itself.
    if (
        _coerce_bool(review.get("review_error"))
        and _coerce_bool(review.get("accepted_with_warning"))
    ):
        return True
    if min_score is None:
        try:
            minimum_score = int(
                os.getenv(
                    "STORY_PLANNER_TRANSITION_MIN_SCORE",
                    str(DEFAULT_WHATIF_TRANSITION_MIN_SCORE),
                )
            )
        except (TypeError, ValueError):
            minimum_score = DEFAULT_WHATIF_TRANSITION_MIN_SCORE
    else:
        try:
            minimum_score = int(min_score)
        except (TypeError, ValueError):
            minimum_score = DEFAULT_WHATIF_TRANSITION_MIN_SCORE
    minimum_score = max(0, min(10, minimum_score))
    try:
        score = int(review.get("score", 0) or 0)
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(10, score))
    if score < minimum_score:
        return False
    if _coerce_bool(review.get("compatible")):
        return True
    if _coerce_bool(review.get("accepted_with_warning")):
        return True
    # ``hard_conflict=false`` is an explicit non-fatal classification.  Keep
    # legacy callers that omit the field on the conservative repair path.
    if "hard_conflict" in review and ("score" in review or bool(review.get("issues"))):
        return True
    return False


_WHATIF_FATAL_VALIDATION_MARKERS = (
    "确认死亡后复活",
    "死亡后复活",
    "已确认死亡角色复活",
    "已死角色复活",
    "无规则复活",
    "confirmed death revival",
    "death revival",
    "无依据复活",
    "核心身份冲突",
    "现实层硬冲突",
    "不是同一人",
    "DAG 损坏",
    "拓扑循环",
    "正文为空",
    "占位文本",
    "内部指令文本",
    "下游节点不存在",
)
_WHATIF_FATAL_VALIDATION_CATEGORIES = {
    "empty_content",
    "invalid_candidate",
    "dag_corrupt",
    "stage_lease_lost",
    "database_write",
    "file_io",
    "hard_convergence",
}


def classify_whatif_validation_issue(
    reason: str = "",
    *,
    content: str = "",
    category: str | None = None,
    review: dict | None = None,
) -> str:
    """Classify a What-If failure after bounded local repair.

    Optional branch prose is kept when there is usable content and the finding
    is a quality defect (POV, length, wording, or a score at/above the local
    threshold).  A missing/empty artifact, infrastructure failure, explicit
    permanent contradiction, or a score below the transition threshold after
    retries is fatal. This policy is intentionally shared by the chapter task
    so a low score only deletes the current optional branch, never the whole
    chapter_branch run.
    """
    text = str(reason or "")
    if isinstance(review, dict):
        review_issues = review.get("issues", [])
        if isinstance(review_issues, str):
            review_issues = [review_issues]
        if _transition_has_hard_conflict(
            [str(item) for item in (review_issues or [])],
            review.get("hard_conflict"),
        ):
            return "fatal"
        if _coerce_bool(review.get("review_error")):
            return "warning" if str(content or "").strip() else "fatal"
        try:
            minimum_score = int(
                os.getenv(
                    "STORY_PLANNER_TRANSITION_MIN_SCORE",
                    str(DEFAULT_WHATIF_TRANSITION_MIN_SCORE),
                )
            )
        except (TypeError, ValueError):
            minimum_score = DEFAULT_WHATIF_TRANSITION_MIN_SCORE
        minimum_score = max(0, min(10, minimum_score))
        try:
            score = int(review.get("score", 0) or 0)
        except (TypeError, ValueError):
            score = 0
        # A low transition score is fatal only at the end of the local retry
        # budget (this classifier is called after generation/review retries).
        # It therefore causes omission of this optional branch, never a full
        # pipeline restart.
        if score < minimum_score:
            return "fatal"
    if str(category or "").strip().lower() in _WHATIF_FATAL_VALIDATION_CATEGORIES:
        return "fatal"
    if not str(content or "").strip():
        return "fatal"
    if any(marker.lower() in text.lower() for marker in _WHATIF_FATAL_VALIDATION_MARKERS):
        return "fatal"
    return "warning"


def _narrative_pov_prompt_block(contract: dict | None) -> str:
    """Render the canonical POV contract for generated prose prompts.

    This is deliberately optional so historical callers that do not yet carry
    a contract retain their previous prompt behavior.  Choice labels remain UI
    copy; the rule applies to prose only.
    """
    if not isinstance(contract, dict):
        return ""
    perspective = str(
        contract.get("perspective")
        or contract.get("mode")
        or contract.get("pov")
        or "unknown"
    ).strip()
    protagonist = str(
        contract.get("protagonist_name")
        or contract.get("pov_character")
        or "主角"
    ).strip()
    rule_text = str(contract.get("rule_text") or "").strip()
    if not rule_text:
        if perspective == "first_person":
            rule_text = (
                f"保持主角{protagonist}的第一人称叙事；正文中的‘我’只能指向{protagonist}，"
                "不得切换为其他角色的第一人称。"
            )
        elif perspective == "third_person_limited":
            rule_text = (
                f"保持围绕{protagonist}的第三人称限知叙事；正文叙述使用姓名、他/她等第三人称，"
                "不得把叙述者改成‘我’。"
            )
        elif perspective == "third_person_omniscient":
            rule_text = f"保持围绕{protagonist}的第三人称全知叙事；不得无铺垫切换成第一人称旁白。"
        elif perspective == "mixed":
            rule_text = "原著允许有限多视角；每个场景必须明确跟随一个视角，不得无标记切换人称。"
        else:
            rule_text = "原著视角未确定；优先模仿相邻原文，不得在同一场景无理由切换人称。"
    switch_rule = (
        "如原著允许章节级切换，只能在明确的章节/场景边界切换。"
        if bool(contract.get("allow_switch"))
        else "当前契约禁止章节或场景内切换叙事视角。"
    )
    return (
        "【叙事视角契约（仅约束正文，不约束 choice_text UI 文案）】\n"
        f"perspective={perspective}; protagonist={protagonist}\n"
        f"{rule_text}\n{switch_rule}\n"
        "对白、书信、日记中的人称按原文保留；不要把按钮文案的‘我’写进第三人称正文。\n\n"
    )


def _context_narrative_pov_contract(novel_context, explicit: dict | None = None) -> dict:
    """Resolve a POV contract from an explicit argument or NovelContext."""
    if isinstance(explicit, dict):
        return explicit
    contract = getattr(novel_context, "narrative_pov_contract", None)
    if isinstance(contract, dict) and contract:
        return contract
    worldview = getattr(novel_context, "worldview", {}) if novel_context else {}
    contract = worldview.get("narrative_pov_contract") if isinstance(worldview, dict) else None
    return contract if isinstance(contract, dict) else {}


class WhatIfContentBudgetError(ValueError):
    """A generated optional What-If cannot satisfy its adaptive length budget.

    ``content`` carries the last usable candidate so the chapter task can keep
    it as a warning after the local retry budget is exhausted instead of
    deleting an otherwise playable optional branch.
    """

    def __init__(self, message: str, *, content: str = ""):
        super().__init__(message)
        self.content = str(content or "")

_INTERNAL_VARIABLE_LABELS = {
    "family_duty": "为家人承担风险",
    "ambition": "追逐力量突破",
    "calmness": "先稳住情绪再行动",
    "courage": "鼓起勇气面对真相",
    "self_worth": "为自己争取尊严",
    "honesty": "坦白真相",
}

_DANGLING_SEMANTIC_SUFFIXES = (
    "共同生", "走到无", "走向无", "陷入无", "来自另一段", "避免把",
    "为了让", "从而让", "选择把", "修复创",
)


def compact_complete_choice_text(text: str, max_chars: int = 28) -> str:
    """Compress generated menu copy at clause boundaries instead of raw slicing."""
    limit = max(6, int(max_chars or 28))
    value = re.sub(r"\s+", "", str(text or "")).strip("，。；：")
    for key, label in _INTERNAL_VARIABLE_LABELS.items():
        value = re.sub(re.escape(key), label, value, flags=re.IGNORECASE)
    value = re.sub(r"^若选择(?:该线)?推动", "", value)
    value = re.sub(r"^若选择该线", "", value)
    if len(value) <= limit and not value.endswith(_DANGLING_SEMANTIC_SUFFIXES):
        return value or "做出不同选择"

    clauses = [
        item.strip("，。；：、")
        for item in re.split(r"[，；。！？、]", value)
        if item.strip("，。；：、")
    ]
    for clause in clauses:
        if 4 <= len(clause) <= limit and not clause.endswith(_DANGLING_SEMANTIC_SUFFIXES):
            return clause

    first = clauses[0] if clauses else value
    for marker in ("避免", "从而", "以便", "为了", "并且", "但是", "却", "并"):
        position = first.find(marker, 4, limit + 1)
        if position >= 4:
            first = first[:position]
            break
    if len(first) <= limit and not first.endswith(_DANGLING_SEMANTIC_SUFFIXES):
        return first

    if first.startswith("坦白"):
        subject = re.sub(r"^坦白(?:这些|自己)?", "", first)
        subject = re.split(r"来自|源自|因为|背后", subject, maxsplit=1)[0]
        candidate = f"坦白{subject[: max(2, limit - 8)]}的真实原因"
        if len(candidate) <= limit:
            return candidate
    if first.startswith(("承认", "说明", "告诉")):
        verb = next(marker for marker in ("承认", "说明", "告诉") if first.startswith(marker))
        subject = re.sub(r"^(?:承认|说明|告诉)(?:自己)?", "", first)
        subject = re.split(r"再次|来自|因为|避免", subject, maxsplit=1)[0]
        candidate = f"{verb}{subject[: max(2, limit - len(verb) - 4)]}的真实担忧"
        if len(candidate) <= limit:
            return candidate

    clipped = first[:limit].rstrip("，。；：并而与及和的地得来向自把被为从")
    while clipped.endswith(_DANGLING_SEMANTIC_SUFFIXES):
        clipped = clipped[:-1].rstrip("，。；：并而与及和的地得来向自把被为从")
    return clipped or "做出不同选择"


def compact_whatif_title_phrase(text: str, max_chars: int = 16) -> str:
    """Shorten a title at a semantic boundary and remove dangling connectors."""
    return compact_complete_choice_text(text, max_chars=max_chars)


def _normalize_topology_choice_texts(result: dict) -> dict:
    """Ending edges are automatic routes and must never render menu buttons."""
    if not isinstance(result, dict):
        return result
    for node in result.get("topology", []) or []:
        if isinstance(node, dict) and node.get("type") == "ending":
            node["choice_text"] = ""
        elif (
            isinstance(node, dict)
            and node.get("type") == "chapter"
            and not node.get("is_original")
        ):
            normalize_whatif_merge_policy(node)
    return result


def normalize_topology_choice_texts(result: dict) -> dict:
    """Public idempotent wrapper used when loading a persisted topology checkpoint."""
    return _normalize_topology_choice_texts(result)


_DECISION_MARKERS = {
    "决定": 5,
    "选择": 5,
    "拒绝": 5,
    "坦白": 5,
    "离开": 4,
    "留下": 4,
    "答应": 4,
    "承认": 4,
    "揭露": 4,
    "隐瞒": 3,
    "放弃": 4,
    "背叛": 5,
    "救": 3,
    "杀": 3,
}


def _decision_value(summary: str) -> int:
    text = str(summary or "")
    score = sum(weight * text.count(marker) for marker, weight in _DECISION_MARKERS.items())
    score += min(text.count("代价") * 3 + text.count("后果") * 2, 8)
    return score


def _decision_phrase(summary: str, max_chars: int = 24) -> str:
    text = re.sub(r"【[^】]+】", "", str(summary or "")).strip()
    clauses = [item.strip(" ，。；：") for item in re.split(r"[。；！!?\n]", text) if item.strip()]
    chosen = next(
        (clause for clause in clauses if any(marker in clause for marker in _DECISION_MARKERS)),
        clauses[0] if clauses else "做出另一种选择",
    )
    chosen = re.sub(r"^(主角|他|她|他们)", "", chosen).strip(" ，。；：")
    if "却" in chosen and len(chosen) > max_chars:
        chosen = chosen.split("却", 1)[0].rstrip("，")
    return chosen[:max_chars] or "做出另一种选择"


def _clean_event_text(value: object) -> str:
    return re.sub(
        r"[\s，。；：、,.!?！？（）()\[\]【】‘’“”'\"]+",
        "",
        str(value or ""),
    ).lower()


def _event_semantically_overlaps(left: object, right: object) -> bool:
    """Conservatively detect whether two short Chinese event phrases refer to one fact."""
    left_text = _clean_event_text(left)
    right_text = _clean_event_text(right)
    if not left_text or not right_text:
        return False
    if left_text in right_text or right_text in left_text:
        return True
    left_bigrams = {left_text[index:index + 2] for index in range(len(left_text) - 1)}
    right_bigrams = {right_text[index:index + 2] for index in range(len(right_text) - 1)}
    if not left_bigrams or not right_bigrams:
        return False
    overlap = len(left_bigrams & right_bigrams)
    return overlap >= 2 and overlap / min(len(left_bigrams), len(right_bigrams)) >= 0.5


def _as_nonempty_text_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _infer_invalidated_events(spec: dict) -> list[str]:
    explicit = _as_nonempty_text_list(spec.get("invalidated_events"))
    if explicit:
        return explicit
    alternative = str(spec.get("alternative_choice") or "").strip()
    if not re.search(r"不再|暂缓|拒绝|放弃|取消|不去|不做|隐瞒|没有|未", alternative):
        return []
    simplified = re.sub(
        r"^(?:选择|决定|主动)?(?:暂缓|拒绝|放弃|取消|不再|不去|不做|隐瞒|没有|未)",
        "",
        alternative,
    ).strip(" ，。；：")
    return [simplified or alternative]


def preflight_whatif_merge_feasibility(
    *,
    spec: dict,
    summaries: list,
    max_delay: int = 3,
) -> dict:
    """Pick the earliest downstream chapter whose required facts survive the What-If."""
    pivot_chapter = int(spec.get("pivot_chapter") or 0)
    supported_delay = max(1, min(int(max_delay or 1), 3))
    invalidated_events = _infer_invalidated_events(spec)
    raw_prerequisites = spec.get("downstream_prerequisites", [])
    prerequisites_by_delay: dict[int, list[str]] = {}
    if isinstance(raw_prerequisites, list):
        for item in raw_prerequisites:
            if not isinstance(item, dict):
                continue
            try:
                delay = int(item.get("merge_after_chapters") or item.get("delay") or 0)
            except (TypeError, ValueError):
                continue
            if 1 <= delay <= supported_delay:
                prerequisites_by_delay[delay] = _as_nonempty_text_list(
                    item.get("requires", item.get("prerequisites", []))
                )

    conflicts: dict[int, list[str]] = {}
    for delay in range(1, supported_delay + 1):
        target_chapter = pivot_chapter + delay + 1
        if target_chapter > len(summaries):
            conflicts[delay] = ["目标章节超出原著范围"]
            continue
        requirements = list(prerequisites_by_delay.get(delay, []))
        if not requirements and 1 <= target_chapter <= len(summaries):
            requirements = [str(summaries[target_chapter - 1] or "").strip()]
        delay_conflicts = [
            requirement
            for requirement in requirements
            if any(
                _event_semantically_overlaps(event, requirement)
                for event in invalidated_events
            )
        ]
        if delay_conflicts:
            conflicts[delay] = delay_conflicts
            continue
        return {
            "feasible": True,
            "merge_policy": "delayed" if delay > 1 else (
                "bridge" if spec.get("required_transitions") else "immediate"
            ),
            "merge_after_chapters": delay,
            "merge_target_chapter": target_chapter,
            "invalidated_events": invalidated_events,
            "downstream_prerequisites": raw_prerequisites if isinstance(raw_prerequisites, list) else [],
            "conflicting_delays": sorted(conflicts),
        }

    return {
        "feasible": False,
        "merge_policy": "reselect",
        "merge_after_chapters": None,
        "merge_target_chapter": None,
        "invalidated_events": invalidated_events,
        "downstream_prerequisites": raw_prerequisites if isinstance(raw_prerequisites, list) else [],
        "conflicting_delays": sorted(conflicts),
        "conflicts": conflicts,
    }


def refresh_whatif_choice_text(topology_node: dict, actual_summary: str) -> str:
    """Replace generic What-If copy with the concrete generated decision and cost."""
    phrase = _decision_phrase(actual_summary)
    for key, label in _INTERNAL_VARIABLE_LABELS.items():
        phrase = re.sub(re.escape(key), label, phrase, flags=re.IGNORECASE)
    phrase = re.sub(r"^(?:若)?选择[:：]?", "", phrase).strip(" ：:，,。")
    if re.search(r"[A-Za-z]+_[A-Za-z_]+", phrase):
        chinese_clause = next(
            (
                item.strip(" ：:，,。")
                for item in re.split(r"[：:；;。\n]", str(actual_summary or ""))
                if re.search(r"[\u4e00-\u9fff]", item)
                and not re.search(r"[A-Za-z]+_[A-Za-z_]+", item)
            ),
            "做出不同选择并承担代价",
        )
        phrase = chinese_clause[:24]
    choice_text = compact_complete_choice_text(f"若选择{phrase}", max_chars=28)
    topology_node["choice_text"] = choice_text
    return choice_text


def normalize_whatif_merge_policy(topology_node: dict) -> dict:
    """Normalize runtime merge metadata without changing the graph shape."""
    node = topology_node if isinstance(topology_node, dict) else {}
    required = node.get("required_transitions", [])
    if isinstance(required, str):
        required = [required] if required.strip() else []
    elif not isinstance(required, list):
        required = []
    node["required_transitions"] = [
        str(item).strip() for item in required if str(item).strip()
    ]
    policy = str(node.get("merge_policy", "")).strip().lower()
    if policy not in {"immediate", "bridge", "delayed", "terminal"}:
        if node.get("terminal_branch") or node.get("convergence_status") == "terminal":
            policy = "terminal"
        elif node["required_transitions"]:
            policy = "bridge"
        else:
            policy = "immediate"
    try:
        merge_after_chapters = max(
            1,
            min(int(node.get("merge_after_chapters") or 1), 3),
        )
    except (TypeError, ValueError):
        merge_after_chapters = 1
    if policy != "terminal" and merge_after_chapters > 1:
        policy = "delayed"
    node["merge_after_chapters"] = merge_after_chapters
    node["merge_policy"] = policy
    return node


def resolve_whatif_merge_window(topology_node: dict, topology: list[dict]) -> dict:
    """Resolve the original chapter span replaced by a What-If and its merge anchor."""
    node = normalize_whatif_merge_policy(dict(topology_node or {}))
    by_id = {
        str(item.get("node_id")): item
        for item in (topology or [])
        if isinstance(item, dict) and item.get("node_id")
    }
    target_node_id = str(node.get("merge_target_node_id") or "").strip()
    if not target_node_id:
        target_node_id = next(
            (
                str(node_id)
                for node_id in node.get("next", []) or []
                if by_id.get(str(node_id), {}).get("is_original")
            ),
            "",
        )
    target = by_id.get(target_node_id, {})
    target_idx = target.get("source_chapter_idx")
    try:
        target_idx = int(target_idx)
    except (TypeError, ValueError):
        target_idx = None
    delay = int(node.get("merge_after_chapters") or 1)
    replaced = []
    state_anchor_idx = None
    if target_idx is not None:
        replaced = list(range(max(0, target_idx - delay), target_idx))
        state_anchor_idx = target_idx - 1 if target_idx > 0 else None
    return {
        "target_node_id": target_node_id,
        "target_source_chapter_idx": target_idx,
        "state_anchor_source_chapter_idx": state_anchor_idx,
        "replaced_source_chapter_indices": replaced,
        "merge_after_chapters": delay,
    }


def enforce_whatif_content_budget(
    content: str,
    *,
    min_chars: int = 8000,
    max_chars: int = 12000,
    preserve_ending: bool = False,
) -> str:
    """Keep What-If prose near its target range with a 15% acceptance margin."""
    text = str(content or "").strip()
    floor = max(int(min_chars), 1)
    ceiling = max(int(max_chars), 1)
    required_min = min(floor, ceiling)
    accepted_min = max(1, (required_min * 85 + 99) // 100)
    accepted_ceiling = max(accepted_min, (ceiling * 115) // 100)
    shortfall_tolerance = required_min - accepted_min
    overflow_tolerance = accepted_ceiling - ceiling
    if len(text) < accepted_min:
        raise WhatIfContentBudgetError(
            f"What-If 正文长度 {len(text)} 低于最低要求 {required_min}"
            f"（15% 容差下限 {accepted_min}）",
            content=text,
        )
    if len(text) < required_min:
        logger.warning(
            "What-If 正文长度 %s 略低于最低要求 %s（容差 %s），保留完整正文",
            len(text),
            required_min,
            shortfall_tolerance,
        )
    if ceiling < len(text) <= accepted_ceiling:
        logger.warning(
            "What-If 正文长度 %s 略高于最高要求 %s（容差 %s），保留完整正文",
            len(text),
            ceiling,
            overflow_tolerance,
        )
    if len(text) <= accepted_ceiling:
        return text

    paragraphs = [item.strip() for item in re.split(r"\n\s*\n", text) if item.strip()]
    preserved_ending = ""
    if preserve_ending and paragraphs:
        tail_sentences = [
            item.strip()
            for item in re.split(r"(?<=[。！？!?])\s*", paragraphs[-1])
            if item.strip()
        ]
        preserved_ending = tail_sentences[-1] if tail_sentences else paragraphs[-1][-240:]
    ending_reserve = len(preserved_ending) + (2 if preserved_ending else 0)
    body_ceiling = max(1, accepted_ceiling - ending_reserve)
    body_paragraphs = paragraphs
    if preserved_ending and paragraphs:
        tail_without_ending = paragraphs[-1][:-len(preserved_ending)].strip()
        body_paragraphs = paragraphs[:-1] + ([tail_without_ending] if tail_without_ending else [])
    kept = []
    current_length = 0
    for paragraph in body_paragraphs:
        separator = 2 if kept else 0
        if current_length + separator + len(paragraph) <= body_ceiling:
            kept.append(paragraph)
            current_length += separator + len(paragraph)
            continue
        remaining = body_ceiling - current_length - separator
        if remaining > 0:
            sentences = [
                item.strip()
                for item in re.split(r"(?<=[。！？!?])\s*", paragraph)
                if item.strip()
            ]
            partial = []
            partial_length = 0
            for sentence in sentences:
                if partial_length + len(sentence) > remaining:
                    break
                partial.append(sentence)
                partial_length += len(sentence)
            if partial:
                kept.append("".join(partial))
        break
    if preserved_ending:
        kept.append(preserved_ending)
    result = "\n\n".join(kept).strip()
    if len(result) < accepted_min:
        boundary = max(
            text.rfind("。", 0, accepted_ceiling),
            text.rfind("！", 0, accepted_ceiling),
            text.rfind("？", 0, accepted_ceiling),
        )
        if boundary >= accepted_min - 1:
            result = text[: boundary + 1].strip()
    return result


def review_whatif_downstream_transition(
    *,
    whatif_text: str,
    downstream_text: str,
    provider: str,
    output_language: str | None = None,
    tail_chars: int = 1800,
    opening_chars: int = 1800,
    narrative_pov_contract: dict | None = None,
) -> dict:
    """Review the final persisted What-If tail against its actual downstream opening."""
    branch_text = str(whatif_text or "").strip()
    next_text = str(downstream_text or "").strip()
    if not branch_text or not next_text:
        return {
            "compatible": False,
            "score": 0,
            "issues": ["What-If 正文或下游章节正文为空，无法执行实际衔接复检"],
            "hard_conflict": True,
            "accepted_with_warning": False,
        }
    client = LLMClient(provider=provider)
    try:
        result = client.chat_completion_json(
            system_prompt=(
                "你是严格的叙事衔接质检员。只依据提供的实际正文判断，不补写不存在的情节。"
                + _narrative_pov_prompt_block(narrative_pov_contract)
                + get_lang_instruction(output_language)
            ),
            user_prompt=(
                "请检查 A 的真实结尾能否直接进入 B 的真实开头。必须同时检查："
                "末句是否完整、时间推进、地点、在场人物、正在发生的动作、情绪和已揭示事实。\n"
                "只有生死、现实层、核心身份、提前揭露关键事实或缺失关键动作链等硬冲突才判为不兼容；"
                "情绪轻微差异、可用一两句过渡解释的差异应标记为可过渡警告。\n\n"
                "衔接评分为 0-10 分，5 分是最低通过线；0-4 分即使没有硬冲突也视为未达标，"
                "需要重试当前 What-If，5-10 分才可保留。\n\n"
                f"【A：What-If 最后 {max(int(tail_chars), 400)} 字】\n"
                f"{branch_text[-max(int(tail_chars), 400):]}\n\n"
                f"【B：下游章节开头 {max(int(opening_chars), 400)} 字】\n"
                f"{next_text[:max(int(opening_chars), 400)]}\n\n"
                "只输出严格 JSON："
                '{"compatible": true/false, "hard_conflict": true/false, "score": 0-10, '
                '"issues": ["具体问题"], "transition_hint": "最小修复建议"}'
            ),
            temperature=0.0,
            max_tokens=800,
        )
        score = max(0, min(10, int(result.get("score", 0))))
        raw_issues = result.get("issues", [])
        if isinstance(raw_issues, str):
            issues = [raw_issues.strip()] if raw_issues.strip() else []
        elif isinstance(raw_issues, list):
            issues = [str(item).strip() for item in raw_issues if str(item).strip()]
        else:
            issues = [str(raw_issues).strip()] if raw_issues else []
        hard_conflict = _transition_has_hard_conflict(
            issues,
            result.get("hard_conflict"),
        )
        model_compatible = _coerce_bool(result.get("compatible"))
        # Keep the model's raw compatibility assessment separate from the
        # local score gate.  ``is_acceptable_whatif_transition_review`` adds
        # the configured minimum score before a branch is accepted; retaining
        # the raw score here keeps the retry/deletion reason auditable.
        accepted_with_warning = bool(not hard_conflict and not model_compatible)
        compatible = bool(model_compatible or not hard_conflict)
        if not compatible and not issues:
            issues = [f"实际衔接评分仅 {score}/10"]
        return {
            "compatible": compatible,
            "score": score,
            "issues": issues,
            "transition_hint": str(result.get("transition_hint", "")).strip(),
            "hard_conflict": hard_conflict,
            "accepted_with_warning": accepted_with_warning and not model_compatible,
        }
    except Exception as exc:
        logger.warning("What-If 实际衔接复检失败: %s", exc)
        return {
            # A failed review request has no valid score, so the local score
            # gate treats it as 0 and allows the bounded What-If retry path to
            # decide whether this branch should be omitted. It never reopens
            # unrelated nodes or restarts the whole chapter pipeline.
            "compatible": False,
            "score": 0,
            "issues": [f"实际衔接复检失败：{exc}"],
            "hard_conflict": False,
            "accepted_with_warning": True,
            "review_error": True,
        }


def split_tail_segment(content: str, target_chars: int = 1800) -> tuple[str, str]:
    """Split text near the requested tail size without cutting a sentence.

    What-If convergence repair should replace only the smallest useful suffix.
    Paragraph-only splitting is unreliable because generated prose often puts
    several scenes in one paragraph.  Prefer a sentence boundary close to the
    target; if no boundary is available, fall back to the nearest safe
    punctuation or the exact character boundary.
    """
    text = str(content or "").strip()
    target = max(200, int(target_chars or 0))
    if len(text) <= target:
        return "", text
    desired_start = len(text) - target
    boundary_chars = "。！？!?；;\n"
    candidates = [
        index + 1
        for index, char in enumerate(text)
        if char in boundary_chars
        and max(0, desired_start - int(target * 0.25)) <= index + 1 <= min(
            len(text), desired_start + int(target * 0.15)
        )
    ]
    if not candidates:
        candidates = [
            index + 1
            for index, char in enumerate(text)
            if char in boundary_chars and index + 1 < len(text)
        ]
    cut = (
        min(candidates, key=lambda position: abs(position - desired_start))
        if candidates
        else desired_start
    )
    prefix = text[:cut].rstrip()
    tail = text[cut:].lstrip()
    return prefix, tail


def repair_whatif_convergence_tail(
    content: str,
    *,
    issues: list[str],
    sibling_states: dict,
    provider: str,
    output_language: str | None = None,
    tail_chars: int = 1800,
    downstream_opening: str = "",
    narrative_pov_contract: dict | None = None,
) -> str:
    """Rewrite only a sentence-aligned tail to repair a hard merge conflict."""
    text = str(content or "").strip()
    if not text or not issues:
        return text
    prefix, original_tail = split_tail_segment(text, target_chars=tail_chars)
    if not prefix or not original_tail:
        return text
    client = LLMClient(provider=provider)
    replacement = client.chat_completion(
        system_prompt=(
            "你是视觉小说收束编辑。只重写给出的尾声，不改前文，不新增无关事件。"
            + _narrative_pov_prompt_block(narrative_pov_contract)
            + get_lang_instruction(output_language)
        ),
        user_prompt=(
            "请把 What-If 最后一段改成能自然进入下一原著章节的过渡尾声。\n"
            "必须修复生死、现实层和核心身份冲突；可以明确写出时间推进、地点转移、"
            "重新进入梦境/治疗重构或暂不揭示真相。不要解释修改过程。\n\n"
            f"冲突：{json.dumps(issues, ensure_ascii=False)}\n"
            f"下游目标状态：{json.dumps(sibling_states, ensure_ascii=False)}\n"
            + (
                f"下游章节实际开头：\n{str(downstream_opening)[:1800]}\n\n"
                if str(downstream_opening or "").strip()
                else ""
            )
            + f"前文结尾参考：{prefix[-max(int(tail_chars), 200):]}\n\n"
            f"待重写尾声：{original_tail}"
        ),
        temperature=0.2,
        max_tokens=1200,
    ).strip()
    return f"{prefix}\n\n{replacement}" if replacement else text


def repair_whatif_convergence_segment(
    content: str,
    *,
    issues: list[str],
    sibling_states: dict,
    provider: str,
    output_language: str | None = None,
    downstream_opening: str = "",
    strategy: str = "tail_rewrite",
    narrative_pov_contract: dict | None = None,
) -> str:
    """Repair the smallest useful ending segment for the diagnosed issue class."""
    strategy = str(strategy or "tail_rewrite").strip().lower()
    if strategy in {"normalize_only", "terminal"}:
        return str(content or "").strip()
    if strategy == "tail_rewrite":
        return repair_whatif_convergence_tail(
            content,
            issues=issues,
            sibling_states=sibling_states,
            provider=provider,
            output_language=output_language,
            downstream_opening=downstream_opening,
            narrative_pov_contract=narrative_pov_contract,
        )

    text = str(content or "").strip()
    # Keep repairs local: bridge scenes need enough room for the missing
    # action chain, while a last-scene rewrite should not replace half of a
    # What-If chapter and trigger the underlong safeguard.
    target_tail_chars = 2800 if strategy == "bridge_scene" else 3200
    prefix, original_segment = split_tail_segment(
        text,
        target_chars=target_tail_chars,
    )
    if not prefix or not original_segment:
        return text
    client = LLMClient(provider=provider)
    replacement = client.chat_completion(
        system_prompt=(
            "你是视觉小说结构修复编辑。只重写给出的末场区段，保留前文既成事实，"
            "补齐真实动作链并自然进入下一原著章节。"
            + _narrative_pov_prompt_block(narrative_pov_contract)
            + get_lang_instruction(output_language)
        ),
        user_prompt=(
            f"修复策略：{strategy}\n"
            "必须完整写出接受/拒绝决定、出发、时间推进、地点转移、到达和安顿等缺失步骤；"
            "不得用一句话跳过关键过程，不得倒带、失忆或声称 What-If 从未发生。\n\n"
            f"已发现问题：{json.dumps(issues, ensure_ascii=False)}\n"
            f"下游目标状态：{json.dumps(sibling_states, ensure_ascii=False)}\n"
            f"下游章节实际开头：\n{str(downstream_opening)[:2400]}\n\n"
            f"保留前文末尾参考：\n{prefix[-1800:]}\n\n"
            f"需要整体重写的末场区段：\n{original_segment}"
        ),
        temperature=0.25,
        max_tokens=3200,
    ).strip()
    return f"{prefix}\n\n{replacement}" if prefix and replacement else (replacement or text)


def try_repair_whatif_convergence_segment(
    content: str,
    **kwargs,
) -> dict:
    """Keep the node alive when a transient LLM repair remains unavailable."""
    original = str(content or "").strip()
    minimum_total_chars = max(0, int(kwargs.pop("minimum_total_chars", 0) or 0))
    preservation_ratio = max(
        0.0,
        min(1.0, float(kwargs.pop("minimum_preservation_ratio", 0.75) or 0.75)),
    )
    try:
        repaired = repair_whatif_convergence_segment(content, **kwargs)
        required_chars = max(
            minimum_total_chars,
            math.ceil(len(original) * preservation_ratio),
        )
        if len(str(repaired or "").strip()) < required_chars:
            logger.warning(
                "What-If 收束修复结果过短，拒绝覆盖原正文并降为终止分支: "
                "candidate=%s, required=%s, original=%s",
                len(str(repaired or "").strip()),
                required_chars,
                len(original),
            )
            return {
                "content": original,
                "repair_failed": True,
                "error_category": "underlong_repair",
                "error": (
                    f"修复结果仅 {len(str(repaired or '').strip())} 字，"
                    f"低于防缩水下限 {required_chars} 字"
                ),
                "required_chars": required_chars,
            }
        return {
            "content": repaired,
            "repair_failed": False,
            "error_category": "",
            "error": "",
            "required_chars": required_chars,
        }
    except Exception as exc:
        from services.story_planner.core import llm_resilience

        classification = llm_resilience.classify_llm_error(exc)
        if not classification.retryable:
            raise
        logger.warning(
            "What-If 收束修复 LLM 重试耗尽，保留原正文并交由终止分支降级: "
            "category=%s, error=%s",
            classification.category,
            exc,
        )
        return {
            "content": original,
            "repair_failed": True,
            "error_category": classification.category,
            "error": str(exc),
            "required_chars": max(
                minimum_total_chars,
                math.ceil(len(original) * preservation_ratio),
            ),
        }


def repair_whatif_until_compatible(
    *,
    content: str,
    downstream_opening: str,
    sibling_states: dict,
    provider: str,
    output_language: str | None = None,
    max_attempts: int = 1,
    min_score: int | None = None,
    minimum_total_chars: int = 0,
    minimum_preservation_ratio: float = 0.75,
    review_callback: Optional[Callable] = None,
    repair_callback: Optional[Callable] = None,
    narrative_pov_contract: dict | None = None,
) -> dict:
    """Review and repair a What-If ending with a bounded score gate."""
    reviewer = review_callback or review_whatif_downstream_transition
    repairer = repair_callback or repair_whatif_convergence_segment
    current = str(content or "").strip()
    history: list[dict] = []
    repairs = 0
    max_repairs = max(0, min(int(max_attempts or 0), 3))
    minimum_total_chars = max(0, int(minimum_total_chars or 0))
    preservation_ratio = max(
        0.0,
        min(1.0, float(minimum_preservation_ratio or 0.75)),
    )

    while True:
        review = reviewer(
            whatif_text=current,
            downstream_text=downstream_opening,
            provider=provider,
            output_language=output_language,
            narrative_pov_contract=narrative_pov_contract,
        )
        review = review if isinstance(review, dict) else {}
        history.append({
            "attempt": repairs,
            "score": int(review.get("score", 0) or 0),
            "compatible": bool(review.get("compatible")),
            "issues": list(review.get("issues") or []),
            "strategy": "review" if repairs == 0 else history[-1].get("next_strategy", "review"),
        })
        # A review explicitly marked as bridgeable is accepted without
        # spending another generation request.  Some gateways/models
        # return ``compatible=false`` while also reporting that no hard
        # conflict exists; ``accepted_with_warning`` is the authoritative
        # signal in that case.
        review_acceptable = is_acceptable_whatif_transition_review(
            review,
            min_score=min_score,
        )
        if review_acceptable or repairs >= max_repairs:
            return {
                "content": current,
                "compatible": review_acceptable,
                "review": review,
                "attempts": repairs,
                "strategy_history": history,
            }

        issues = list(review.get("issues") or [])
        if repairs == 0:
            from services.story_planner.core.chapter_branch_runtime import (
                classify_convergence_repair,
            )
            strategy = classify_convergence_repair([], transition_issues=issues)
            if strategy in {"terminal", "normalize_only"}:
                strategy = "tail_rewrite"
        else:
            strategy = "regenerate_last_scene"
        history[-1]["next_strategy"] = strategy
        try:
            candidate = repairer(
                current,
                issues=issues,
                sibling_states=sibling_states,
                provider=provider,
                output_language=output_language,
                downstream_opening=downstream_opening,
                strategy=strategy,
                narrative_pov_contract=narrative_pov_contract,
            )
        except Exception as exc:
            from services.story_planner.core import llm_resilience

            classification = llm_resilience.classify_llm_error(exc)
            if not classification.retryable:
                raise
            logger.warning(
                "What-If 多轮收束修复 LLM 重试耗尽，保留原正文并停止当前节点修复: "
                "category=%s, error=%s",
                classification.category,
                exc,
            )
            return {
                "content": current,
                "compatible": False,
                "review": review,
                "attempts": repairs,
                "strategy_history": history,
                "repair_error": {
                    "category": classification.category,
                    "message": str(exc),
                },
            }
        candidate = str(candidate or "").strip()
        required_chars = max(
            minimum_total_chars,
            math.ceil(len(current) * preservation_ratio),
        )
        if len(candidate) < required_chars:
            logger.warning(
                "What-If 多轮末场修复结果过短，保留上一版本并停止当前节点修复: "
                "candidate=%s, required=%s, previous=%s",
                len(candidate),
                required_chars,
                len(current),
            )
            return {
                "content": current,
                "compatible": False,
                "review": review,
                "attempts": repairs,
                "strategy_history": history,
                "repair_error": {
                    "category": "underlong_repair",
                    "message": (
                        f"修复结果仅 {len(candidate)} 字，"
                        f"低于防缩水下限 {required_chars} 字"
                    ),
                    "required_chars": required_chars,
                },
            }
        current = candidate
        repairs += 1


def resolve_whatif_scenes(resume_state: Optional[dict], generator: Callable[[], list]) -> list:
    """Reuse the persisted outline so resumed prose keeps the same scene contract."""
    saved = (resume_state or {}).get("scenes", [])
    if isinstance(saved, list) and saved:
        return saved
    generated = generator()
    return generated if isinstance(generated, list) else []


def should_refresh_accumulated_summary(scene_index: int) -> bool:
    """Refresh the chapter-level summary every two scenes to reduce LLM calls."""
    return int(scene_index) % 2 == 1


def _select_fallback_pivots(total: int, branch_count: int, summaries: list) -> list[int]:
    eligible = list(range(1, max(1, total - 2)))
    ranked = sorted(
        eligible,
        key=lambda index: (-_decision_value(summaries[index] if index < len(summaries) else ""), index),
    )
    selected = []
    for index in ranked:
        if all(abs(index - existing) >= 2 for existing in selected):
            selected.append(index)
        if len(selected) >= branch_count:
            break
    if len(selected) < branch_count:
        for index in eligible:
            if index not in selected:
                selected.append(index)
            if len(selected) >= branch_count:
                break
    return sorted(selected)


def _validate_topology_graph(result: dict, total: int) -> tuple:
    """校验所有 chapter_branch 模式共享的 DAG 结构约束。"""
    if not isinstance(result, dict) or "topology" not in result:
        return False, "缺少 topology 字段"

    topo = result["topology"]
    if not isinstance(topo, list) or not topo:
        return False, "topology 必须是非空列表"

    node_ids = []
    for index, node in enumerate(topo):
        if not isinstance(node, dict):
            return False, f"topology[{index}] 不是对象"
        node_id = node.get("node_id")
        if not isinstance(node_id, str) or not node_id.strip():
            return False, f"topology[{index}] 缺少有效 node_id"
        node_ids.append(node_id)

    duplicate_ids = sorted(
        node_id for node_id in set(node_ids) if node_ids.count(node_id) > 1
    )
    if duplicate_ids:
        return False, f"node_id 重复：{duplicate_ids}"

    node_by_id = {node["node_id"]: node for node in topo}
    indegree = {node_id: 0 for node_id in node_by_id}
    for node in topo:
        next_ids = node.get("next", [])
        if not isinstance(next_ids, list):
            return False, f"节点 '{node['node_id']}' 的 next 不是列表"
        for next_id in next_ids:
            if next_id not in node_by_id:
                return False, (
                    f"节点 '{node['node_id']}' 的 next 指向不存在节点 '{next_id}'"
                )
            indegree[next_id] += 1

    roots = sorted(node_id for node_id, degree in indegree.items() if degree == 0)
    if len(roots) != 1:
        return False, f"根节点数量必须为 1，实得 {len(roots)}：{roots}"

    reachable = set()
    stack = [roots[0]]
    while stack:
        node_id = stack.pop()
        if node_id in reachable:
            continue
        reachable.add(node_id)
        stack.extend(node_by_id[node_id].get("next", []))
    unreachable = sorted(set(node_by_id) - reachable)
    if unreachable:
        return False, f"存在从根节点不可达的节点：{unreachable}"

    visiting = set()
    visited = set()

    def _has_cycle(node_id: str) -> bool:
        if node_id in visiting:
            return True
        if node_id in visited:
            return False
        visiting.add(node_id)
        for next_id in node_by_id[node_id].get("next", []):
            if _has_cycle(next_id):
                return True
        visiting.remove(node_id)
        visited.add(node_id)
        return False

    if _has_cycle(roots[0]):
        return False, "拓扑中存在循环，不是有效 DAG"

    orig_idxs = sorted(
        node["source_chapter_idx"]
        for node in topo
        if node.get("is_original")
        and node.get("source_chapter_idx") is not None
    )
    if orig_idxs != list(range(total)):
        return False, f"原著章节索引异常（期望{list(range(total))}，实得{orig_idxs}）"

    last_orig = next(
        (
            node
            for node in topo
            if node.get("is_original")
            and node.get("source_chapter_idx") == total - 1
        ),
        None,
    )
    if not last_orig or last_orig.get("type") != "ending" or last_orig.get("next"):
        return False, "原著末章必须是 type=ending 且 next=[] 的终止节点"

    for node in topo:
        if node.get("type") in ("ending", "bad_ending") and node.get("next"):
            return False, f"节点 '{node['node_id']}' 必须是 next=[] 的终止节点"

    var_keys = {
        item.get("key")
        for item in result.get("variable_defs", [])
        if isinstance(item, dict) and item.get("key")
    }
    for node in topo:
        branch_variable = node.get("branch_variable")
        if branch_variable and branch_variable not in var_keys:
            return False, (
                f"节点 '{node['node_id']}' 的 branch_variable '{branch_variable}' "
                "未在 variable_defs 定义"
            )

    return True, ""


def _build_linear_original_nodes(total: int) -> list[dict]:
    return [
        {
            "node_id": f"ch{index + 1}",
            "type": "ending" if index == total - 1 else "chapter",
            "title": "原著结局" if index == total - 1 else "未命名章节",
            "is_original": True,
            "source_chapter_idx": index,
            "choice_text": "" if index == total - 1 else ("故事开始" if index == 0 else "继续原著路线"),
            "summary": None,
            "branch_variable": None,
            "branch_threshold": None,
            "next": [] if index == total - 1 else [f"ch{index + 2}"],
        }
        for index in range(total)
    ]


def _validate_whatif_spec(
    spec: dict,
    *,
    total: int,
    used_pivots: set[int],
) -> tuple[bool, str]:
    if not isinstance(spec, dict):
        return False, "分支规格不是对象"
    try:
        pivot_chapter = int(spec.get("pivot_chapter"))
    except (TypeError, ValueError):
        return False, "pivot_chapter 不是整数"
    if pivot_chapter < 2 or pivot_chapter > total - 2:
        return False, f"pivot_chapter={pivot_chapter} 超出第2章到倒数第3章范围"
    if pivot_chapter in used_pivots:
        return False, f"pivot_chapter={pivot_chapter} 与已有岔口重复"
    if any(abs(pivot_chapter - existing) < 2 for existing in used_pivots):
        return False, f"pivot_chapter={pivot_chapter} 与已有岔口距离不足2章"
    for field in ("decision", "alternative_choice", "consequence", "choice_text"):
        if not str(spec.get(field) or "").strip():
            return False, f"缺少 {field}"
    return True, ""


def _normalize_whatif_spec(spec: dict) -> dict:
    required = spec.get("required_transitions", [])
    if isinstance(required, str):
        required = [required] if required.strip() else []
    elif not isinstance(required, list):
        required = []
    required = [str(item).strip() for item in required if str(item).strip()]
    merge_policy = str(spec.get("merge_policy") or "").strip().lower()
    if merge_policy not in {"immediate", "bridge", "delayed"}:
        merge_policy = "bridge" if required else "immediate"
    try:
        merge_after_chapters = max(1, min(int(spec.get("merge_after_chapters") or 1), 3))
    except (TypeError, ValueError):
        merge_after_chapters = 1
    if merge_after_chapters > 1:
        merge_policy = "delayed"
    downstream_prerequisites = spec.get("downstream_prerequisites", [])
    if not isinstance(downstream_prerequisites, list):
        downstream_prerequisites = []
    return {
        "pivot_chapter": int(spec["pivot_chapter"]),
        "decision": str(spec.get("decision") or "").strip(),
        "alternative_choice": str(spec.get("alternative_choice") or "").strip(),
        "consequence": str(spec.get("consequence") or "").strip(),
        "choice_text": compact_complete_choice_text(
            str(spec.get("choice_text") or "").strip(),
            max_chars=28,
        ),
        "merge_policy": merge_policy,
        "merge_after_chapters": merge_after_chapters,
        "merge_target_chapter": spec.get("merge_target_chapter"),
        "required_transitions": required,
        "invalidated_events": _as_nonempty_text_list(spec.get("invalidated_events")),
        "downstream_prerequisites": downstream_prerequisites,
    }


def _fallback_whatif_spec(
    summaries: list,
    *,
    total: int,
    used_pivots: set[int],
) -> dict:
    candidates = _select_fallback_pivots(total, max(1, total), summaries)
    preferred = [
        index + 1 for index in candidates
        if index + 1 not in used_pivots
        and all(abs((index + 1) - existing) >= 2 for existing in used_pivots)
    ]
    relaxed = [
        chapter
        for chapter in range(2, max(3, total - 1))
        if chapter not in used_pivots
    ]
    pivot_chapter = preferred[0] if preferred else (
        relaxed[0] if relaxed else candidates[0] + 1
    )
    summary = summaries[pivot_chapter - 1] if pivot_chapter - 1 < len(summaries) else ""
    phrase = _decision_phrase(summary)
    return {
        "pivot_chapter": pivot_chapter,
        "decision": phrase,
        "alternative_choice": phrase,
        "consequence": "承担与原著不同的具体代价并改变人物关系",
        "choice_text": compact_complete_choice_text(phrase, max_chars=28),
        "merge_policy": "bridge",
        "merge_after_chapters": 1,
        "required_transitions": [
            "覆盖被替代原著章节的完整时间跨度",
            "到达共同下游所需的时间、地点和人物状态",
        ],
    }


def _build_variable_defs(persona_axes: list) -> tuple[list[dict], list[dict]]:
    axes = [axis for axis in (persona_axes or []) if isinstance(axis, dict) and axis.get("key")]
    defaults = [
        {"key": "courage", "name": "勇气", "ending_hint": "主动留下真实证言"},
        {"key": "self_worth", "name": "自我价值", "ending_hint": "公开澄清流言"},
    ]
    while len(axes) < 2:
        axes.append(defaults[len(axes)])
    variable_defs = []
    ending_specs = []
    for index, axis in enumerate(axes[:2]):
        threshold = 65 if index == 0 else 60
        key = axis["key"]
        name = axis.get("name", key)
        hint = axis.get("ending_hint", f"高分指向{name}结局")
        ending_id = f"ending_{index + 1}"
        variable_defs.append({
            "key": key,
            "name": name,
            "init": 50,
            "description": hint,
            "low_desc": axis.get("low_desc", ""),
            "high_desc": axis.get("high_desc", ""),
            "ending_hint": hint,
            "affects_node": ending_id,
            "threshold": threshold,
            "condition": f">= {threshold} → {name}结局",
        })
        ending_specs.append({
            "node_id": ending_id,
            "name": name,
            "summary": hint,
            "key": key,
            "threshold": threshold,
            "legacy_requirements": (
                ["signed_clarification", "school_record_preserved"]
                if key == "self_worth"
                else ["illness_disclosed", "truth_record_preserved"]
                if key == "honesty"
                else []
            ),
        })
    return variable_defs, ending_specs


def _assemble_compact_topology(
    *,
    total: int,
    specs: list[dict],
    persona_axes: list,
    branch_type: str,
    metadata: dict,
) -> dict:
    nodes = _build_linear_original_nodes(total)
    for spec in specs:
        pivot_index = int(spec["pivot_chapter"]) - 1
        sibling = nodes[pivot_index + 1]
        pivot = nodes[pivot_index]
        whatif_id = f"{sibling['node_id']}_alt"
        try:
            merge_after_chapters = max(
                1,
                min(int(spec.get("merge_after_chapters") or 1), 3),
            )
        except (TypeError, ValueError):
            merge_after_chapters = 1
        target_chapter = spec.get("merge_target_chapter")
        try:
            target_chapter = int(target_chapter)
        except (TypeError, ValueError):
            target_chapter = int(spec["pivot_chapter"]) + merge_after_chapters + 1
        target_chapter = max(1, min(target_chapter, total))
        merge_target_node_id = f"ch{target_chapter}"
        pivot["type"] = "branch_point"
        pivot["next"].append(whatif_id)
        nodes.append({
            "node_id": whatif_id,
            "type": "chapter",
            "title": f"What-If：{compact_whatif_title_phrase(spec['alternative_choice'])}",
            "is_original": False,
            "source_chapter_idx": None,
            "choice_text": spec["choice_text"],
            "summary": f"{spec['alternative_choice']}；{spec['consequence']}",
            "branch_variable": None,
            "branch_threshold": None,
            "merge_policy": spec.get("merge_policy", "immediate"),
            "merge_after_chapters": merge_after_chapters,
            "merge_target_node_id": merge_target_node_id,
            "required_transitions": list(spec.get("required_transitions") or []),
            "invalidated_events": list(spec.get("invalidated_events") or []),
            "downstream_prerequisites": list(spec.get("downstream_prerequisites") or []),
            "next": [merge_target_node_id],
        })
    variable_defs = []
    if branch_type == "default":
        variable_defs, endings = _build_variable_defs(persona_axes)
        pivot = nodes[total - 2]
        pivot["type"] = "branch_point"
        for ending in endings:
            pivot["next"].append(ending["node_id"])
            nodes.append({
                "node_id": ending["node_id"],
                "type": "ending",
                "title": f"结局·{ending['name']}",
                "is_original": False,
                "source_chapter_idx": None,
                "choice_text": "",
                "summary": ending["summary"],
                "branch_variable": ending["key"],
                "branch_threshold": ending["threshold"],
                "legacy_requirements": ending.get("legacy_requirements", []),
                "next": [],
            })
    return {
        "topology": nodes,
        "variable_defs": variable_defs,
        "topology_generation": metadata,
    }


def _design_compact_whatif_topology(
    *,
    client,
    total: int,
    summaries: list,
    branch_count: int,
    persona_axes: list,
    branch_type: str,
    output_language: str | None,
) -> dict:
    recommended_pivots = [index + 1 for index in _select_fallback_pivots(
        total, branch_count, summaries
    )]
    compact_prompt = (
        f"为共{total}章的小说选择{branch_count}个高价值 What-If 岔口。"
        f"必须严格输出{branch_count}项，不多不少。"
        "只输出紧凑 JSON，不要输出原著节点、next 或完整拓扑。"
        "pivot_chapter 是岔口所在原著章节号，范围第2章到倒数第3章；岔口间隔至少2章。"
        f"代码按章节决策价值计算的推荐岔口章节为：{recommended_pivots}；"
        "优先从中选择，只有摘要明确显示更高价值决定时才替换。"
        "每项必须包含 pivot_chapter、decision、alternative_choice、consequence、choice_text、"
        "merge_policy、merge_after_chapters、required_transitions、invalidated_events、"
        "downstream_prerequisites。merge_policy 可为 immediate、bridge 或 delayed；"
        "merge_after_chapters 只能为1到3，表示 What-If 覆盖多少个后续原著章节后再收束。"
        "invalidated_events 列出该选择否定的原著既成事件；downstream_prerequisites 按"
        "merge_after_chapters=1/2/3 列出候选下游开头依赖的既成事实。"
        "若从岔口到共同下游涉及跨日、出发、旅行、到达、住宿或人物重新会合，必须选 bridge 并逐项列出。"
        "choice_text 必须是具体中文玩家动作，不得包含 honesty、self_worth 等内部变量名。"
        "严格输出形如 {\"whatif_specs\": [{\"pivot_chapter\": 2, "
        "\"decision\": \"是否坦白\", \"alternative_choice\": \"主动坦白\", "
        "\"consequence\": \"承担关系变化的代价\", \"choice_text\": \"主动坦白病情\", "
        "\"merge_policy\": \"bridge\", \"merge_after_chapters\": 1, "
        "\"required_transitions\": [\"完成关系调整\", \"回到下游地点\"], "
        "\"invalidated_events\": [], \"downstream_prerequisites\": "
        "[{\"merge_after_chapters\": 1, \"requires\": [\"下游已成立事实\"]}]}]}。\n\n"
        + "\n".join(f"第{index + 1}章：{summary}" for index, summary in enumerate(summaries))
    )
    response = client.chat_completion_json(
        system_prompt="你是视觉小说分支策划，只选择有主动决定和显著代价的剧情岔口。",
        user_prompt=compact_prompt,
        temperature=0.3,
        max_tokens=max(1800, branch_count * 220),
    )
    if isinstance(response, dict) and isinstance(response.get("topology"), list):
        graph_ok, graph_reason = _validate_topology_graph(response, total)
        if graph_ok:
            return response
        raise ValueError(f"历史完整拓扑响应不合法：{graph_reason}")
    raw_specs = []
    if isinstance(response, dict):
        raw_specs = next(
            (
                response.get(key)
                for key in ("whatif_specs", "specs", "branches", "items")
                if isinstance(response.get(key), list)
            ),
            [],
        )
    valid_specs = []
    used_pivots: set[int] = set()
    initial_valid_count = 0
    code_assigned_pivot_count = 0
    repaired_count = 0
    fallback_count = 0
    delayed_merge_count = 0
    preflight_reselected_count = 0
    for index in range(branch_count):
        candidate = dict(raw_specs[index]) if index < len(raw_specs) and isinstance(raw_specs[index], dict) else {}
        raw_ok, _ = _validate_whatif_spec(
            candidate,
            total=total,
            used_pivots=used_pivots,
        )
        if raw_ok:
            initial_valid_count += 1
        required_copy_complete = all(
            str(candidate.get(field) or "").strip()
            for field in ("decision", "alternative_choice", "consequence", "choice_text")
        )
        try:
            supplied_pivot = int(candidate.get("pivot_chapter"))
        except (TypeError, ValueError):
            supplied_pivot = 0
        pivot_is_usable = (
            2 <= supplied_pivot <= total - 2
            and supplied_pivot not in used_pivots
            and all(abs(supplied_pivot - existing) >= 2 for existing in used_pivots)
        )
        if required_copy_complete and not pivot_is_usable:
            assigned_pivot = next(
                (
                    pivot for pivot in recommended_pivots
                    if pivot not in used_pivots
                    and all(abs(pivot - existing) >= 2 for existing in used_pivots)
                ),
                None,
            )
            if assigned_pivot is not None:
                candidate["pivot_chapter"] = assigned_pivot
                code_assigned_pivot_count += 1
        ok, reason = _validate_whatif_spec(candidate, total=total, used_pivots=used_pivots)
        if not ok:
            for _ in range(2):
                repair = client.chat_completion_json(
                    system_prompt="你只修复一个 What-If 分支规格，只输出 JSON。",
                    user_prompt=(
                        f"只修复这一条第{index + 1}个分支规格。错误：{reason}\n"
                        f"已占用岔口：{sorted(used_pivots)}，合法范围：2-{total - 2}。\n"
                        f"原规格：{json.dumps(candidate, ensure_ascii=False)}\n"
                        "输出 {\"spec\": {pivot_chapter, decision, alternative_choice, consequence, choice_text, merge_policy, merge_after_chapters, required_transitions, invalidated_events, downstream_prerequisites}}"
                    ),
                    temperature=0.2,
                    max_tokens=500,
                )
                candidate = repair.get("spec", {}) if isinstance(repair, dict) else {}
                ok, reason = _validate_whatif_spec(candidate, total=total, used_pivots=used_pivots)
                if ok:
                    repaired_count += 1
                    break
        if not ok:
            candidate = _fallback_whatif_spec(
                summaries,
                total=total,
                used_pivots=used_pivots,
            )
            fallback_count += 1
        normalized = _normalize_whatif_spec(candidate)
        preflight = preflight_whatif_merge_feasibility(
            spec=normalized,
            summaries=summaries,
            max_delay=3,
        )
        if not preflight["feasible"]:
            rejected_pivot = normalized["pivot_chapter"]
            candidate = _fallback_whatif_spec(
                summaries,
                total=total,
                used_pivots=used_pivots | {rejected_pivot},
            )
            normalized = _normalize_whatif_spec(candidate)
            preflight = preflight_whatif_merge_feasibility(
                spec=normalized,
                summaries=summaries,
                max_delay=3,
            )
            fallback_count += 1
            preflight_reselected_count += 1
        normalized.update({
            "merge_policy": preflight["merge_policy"],
            "merge_after_chapters": preflight["merge_after_chapters"],
            "merge_target_chapter": preflight["merge_target_chapter"],
            "invalidated_events": preflight["invalidated_events"],
            "downstream_prerequisites": preflight["downstream_prerequisites"],
        })
        if normalized["merge_after_chapters"] > 1:
            delayed_merge_count += 1
        used_pivots.add(normalized["pivot_chapter"])
        valid_specs.append(normalized)
    metadata = {
        "mode": "compact_specs",
        "full_fallback": fallback_count == branch_count and branch_count > 0,
        "branch_count": branch_count,
        "initial_valid_branch_count": initial_valid_count,
        "initial_valid_ratio": round(initial_valid_count / max(branch_count, 1), 4),
        "code_assigned_pivot_count": code_assigned_pivot_count,
        "repaired_branch_count": repaired_count,
        "fallback_branch_count": fallback_count,
        "fallback_ratio": round(fallback_count / max(branch_count, 1), 4),
        "delayed_merge_count": delayed_merge_count,
        "preflight_reselected_count": preflight_reselected_count,
    }
    return _assemble_compact_topology(
        total=total,
        specs=valid_specs,
        persona_axes=persona_axes,
        branch_type=branch_type,
        metadata=metadata,
    )


def _summarize_single(client, chapter_text: str, chapter_no: int, system_prompt: str) -> str:
    """对单章全文生成摘要；超长时分段摘要再合并。"""
    ch = chapter_text.strip()
    prompt_prefix = (
        f"请分析第{chapter_no}章（150-200字）：\n"
        "【核心事件】2-4个关键事件（每条20字以内）\n"
        "【人物决策】重要决定及直接后果\n"
        "【情感基调】整体情绪走向及章末状态\n"
        "【转折信号】是否存在抉择苗头或命运预兆\n"
        "【潜在岔路】若主角当时做了不同选择，最可能产生的不同走向（1句话）\n\n"
    )
    if len(ch) <= _SUMMARY_SEG_SIZE:
        # 全文一次读完
        user_prompt = prompt_prefix + f"第{chapter_no}章全文：\n{ch}"
        return client.chat_completion(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.3,
            max_tokens=500,
        ).strip()
    else:
        # 分段摘要，再合并压缩
        seg_summaries = []
        for seg_start in range(0, len(ch), _SUMMARY_SEG_SIZE):
            seg = ch[seg_start:seg_start + _SUMMARY_SEG_SIZE]
            seg_no = seg_start // _SUMMARY_SEG_SIZE + 1
            seg_prompt = (
                f"第{chapter_no}章第{seg_no}段内容（{len(seg)}字），请用100字以内概括核心事件、人物决策和转折信号：\n{seg}"
            )
            try:
                seg_sum = client.chat_completion(
                    system_prompt=system_prompt,
                    user_prompt=seg_prompt,
                    temperature=0.3,
                    max_tokens=200,
                ).strip()
            except Exception:
                seg_sum = f"第{chapter_no}章第{seg_no}段"
            seg_summaries.append(seg_sum)
        # 合并各段摘要，再压缩成最终摘要
        merge_prompt = (
            f"以下是第{chapter_no}章各段的摘要，请综合压缩为150-200字的完整章节分析：\n\n"
            + "\n---\n".join(seg_summaries)
            + "\n\n" + prompt_prefix
        )
        return client.chat_completion(
            system_prompt=system_prompt,
            user_prompt=merge_prompt,
            temperature=0.3,
            max_tokens=500,
        ).strip()


def summarize_chapters(chapters: list, provider: str, novel_context=None) -> list:
    """逐章生成摘要（全文读取，超长时分段合并），返回与 chapters 等长的 list[str]"""
    client = LLMClient(provider=provider)
    total = len(chapters)
    summaries = []
    system_prompt = (
        "你是专业文学编辑，为视觉小说改编提供精准章节分析。"
        "重点关注：人物决策与后果、情感冲突、命运岔路口暗示。"
    )
    for i, ch in enumerate(chapters):
        try:
            summary = _summarize_single(client, ch, i + 1, system_prompt)
        except Exception as e:
            print(f"   ⚠️  第{i+1}章摘要失败（{e}），使用占位摘要")
            summary = f"第{i+1}章（{len(ch)}字）"
        summaries.append(summary)
        print(f"   ✅ 第{i+1}/{total}章摘要完成")
    return summaries


def design_story_topology(
    chapters: list,
    summaries: list,
    provider: str,
    persona_axes: list = None,
    output_language: str = None,
    is_romance_oriented: bool = False,
    romance_targets: list = None,
    ending_dimensions: list = None,
    branch_type: str = "default",
) -> dict:
    """
    LLM 规划故事拓扑图。

    返回 dict，包含 topology 列表，每个节点字段：
      node_id, type(chapter/branch_point/ending/bad_ending), title,
      is_original, source_chapter_idx(原著节点用), summary(衍生节点用),
      branch_variable(触发分支的变量), branch_threshold(阈值),
      is_bad_ending(死亡回廊/玄幻Bad End标记), next([node_id])

    branch_type 控制拓扑结构：
      default        — 软分支+What-If菱形DAG+数值多结局
      death_corridor — 1-2个Bad End短分支（terminal），无软分支，无多结局
      romance        — 无What-If，多结局（affection变量路由）
      fantasy        — What-If菱形DAG，无多结局（只有原著结局），无数值变量
      modular        — 仅线性原著主线（无What-If，无多结局）
    """
    total = len(chapters)
    # branch_count：不同类型的 What-If 节点数量
    if branch_type in ("romance", "modular"):
        branch_count = 0
    elif branch_type == "death_corridor":
        # 死亡回廊：1-2个 Bad End，按篇幅弹性
        branch_count = 0 if total < 4 else min(2, max(1, total // 5))
    elif branch_type == "fantasy":
        branch_count = 0 if total < 5 else max(1, (total - 2) // 3)
    else:
        # default
        branch_count = 0 if total < 5 else max(1, (total - 2) // 3)
    client = LLMClient(provider=provider)
    lang_instr = get_lang_instruction(output_language)

    if branch_type in ("default", "fantasy"):
        try:
            compact = _design_compact_whatif_topology(
                client=client,
                total=total,
                summaries=summaries,
                branch_count=branch_count,
                persona_axes=persona_axes or [],
                branch_type=branch_type,
                output_language=output_language,
            )
            return _normalize_topology_choice_texts(compact)
        except Exception as exc:
            print(f"   ⚠️  紧凑拓扑规格生成失败（{exc}），使用确定性 fallback")
            fallback = _make_fallback_topology(
                total,
                persona_axes=persona_axes,
                branch_type=branch_type,
                romance_targets=romance_targets,
                summaries=summaries,
            )
            fallback["topology_generation"] = {
                "mode": "deterministic_fallback",
                "full_fallback": True,
                "branch_count": branch_count,
                "repaired_branch_count": 0,
                "fallback_branch_count": branch_count,
                "fallback_ratio": 1.0,
                "error": str(exc),
            }
            return _normalize_topology_choice_texts(fallback)

    summaries_text = "\n\n".join(
        f"【第{i+1}章】\n{s}" for i, s in enumerate(summaries)
    )

    # 结局矩阵规则：恋爱向走「角色向」，人格向走「命运矩阵」
    if is_romance_oriented and romance_targets:
        targets_text = "\n".join(
            f"  - {t.get('role_label', '角色'+str(i+1))}：{t.get('name','')}（{t.get('affection_arc','')}）"
            for i, t in enumerate(romance_targets) if isinstance(t, dict)
        )
        ending_matrix_rule = (
            "   - 【结局矩阵铁律 — 恋爱向】本作 is_romance_oriented=true，结局走『角色向』：\n"
            f"{targets_text}\n"
            "     ① 每个 romance_targets 角色对应一个独立结局（结局 title 形如『XX线·〈情境短语〉』，如『沈清舒线·归隐山林』）；\n"
            "     ② 每个角色结局的 branch_variable 必须是 affection_<对应角色拼音>，threshold ∈ [60, 70]；\n"
            "     ③ 结局之间必须在『主角与谁结局』『结局基调』两个维度上完全错开（不能两个结局都以主角与同一人收尾）；\n"
            "     ④ 可以额外加 1 个 Bad End 或真结局（branch_variable 用人格轴，或所有 affection 都低时触发）；\n"
            "     ⑤ summary 必须包含三项：①主角与谁结局 + 那个角色的最终命运；②事件走向；③情感基调。\n"
            "        示例：「主角与女1沈清舒共渡余生，沈清舒放弃修炼陪主角归隐山林，复仇放下——温情而怅惘」\n"
        )
    else:
        # 命运向：把世界观提取的 ending_dimensions 强制接入
        ed_lines = []
        for d in (ending_dimensions or []):
            if isinstance(d, dict):
                ed_lines.append(f"     - {d.get('dimension','')}：{', '.join(d.get('options', []))}")
        ed_text = "\n".join(ed_lines)
        if ed_text:
            ed_block = (
                "   - 【命运维度（来自世界观分析，必须严格基于此设计结局矩阵）】\n"
                f"{ed_text}\n"
                "     ① 你的平行结局必须从上述维度的不同象限中各取一个；\n"
                "     ② 每个结局至少在 2 个维度上与原著结局相反或正交；\n"
                "     ③ 不得引入上述维度之外的结局走向。\n"
            )
        else:
            ed_block = (
                "   - 【命运维度】（世界观未提供，请你自己从原著推导至少 2 个不重叠的命运维度作为结局矩阵骨架）\n"
            )
        ending_matrix_rule = (
            "   - 【结局矩阵铁律 — 命运向】先在内部列出本故事的核心命运维度（至少 2 个），再从不同象限各取一个结局。\n"
            + ed_block +
            "     例：原著是「主角活·复仇成·配角死」，则平行结局应覆盖「主角死·配角死（共赴）」「主角活·配角活·复仇成（共生）」「主角活·复仇败（失败/和解）」等不同象限。\n"
            "     禁止两个结局都以主角与同一人物并肩/在一起收尾，禁止复现相同的情感基调，禁止所有结局都是「主角独活」。\n"
            "   - 【结局 summary 格式铁律】每个平行结局的 summary 必须包含三项：①核心人物生死（明确写出谁活谁死）；②事件走向（复仇成败/和解/牺牲等）；③情感基调（悲壮/温情/苦涩/释然等）。\n"
            "     示例：「主角与配角共赴死亡，复仇在最后一刻完成，但两人均未能生还——悲壮」「主角活，配角活，复仇成功——苦涩中带有温情」。禁止写成「走向孤独但无悔的结局」这种空洞描述。\n"
        )

    # ── 按 branch_type 构建分支规则说明 ────────────────────────────────
    if branch_type == "death_corridor":
        branch_rules = (
            f"2. 【死亡回廊模式】在原著中找 {branch_count} 个生死危机岔口（branch_point），\n"
            f"   每个岔口衍生 1 个 Bad End 节点（type=bad_ending, is_original=false, is_bad_ending=true）。\n"
            "   Bad End 是死路终点，next=[]（不收束回主线，玩家死亡/失败/游戏结束）。\n"
            "   Bad End 内容短而有力，2000-3500字，主角在此处死亡或致命失败。\n"
            "   choice_text 写「[Bad End] + 一句死亡/失败情境短语（10-15字）」。\n"
            "3. 不生成平行结局节点（is_original=false 的 ending 节点），只保留原著末章作为唯一结局。\n"
            "4. variable_defs 必须为空列表：variable_defs: []\n"
            "5. 所有原著节点 next=[] 为空时为原著结局，branch_variable=null, branch_threshold=null。\n"
        )
        var_defs_rule = "7. variable_defs 必须为空列表 []，本模式无数值系统。\n"
        whatif_dag_rule = (
            f"5. 【Bad End 拓扑结构】恰好生成 {branch_count} 个 Bad End 节点（type=bad_ending）：\n"
            "   - 岔口章节（branch_point）的 next 包含两条路径：A) 原著下一章（继续主线）；B) Bad End节点（dead end）。\n"
            "   - Bad End 节点 next=[] （终止节点，不收束）。\n"
            "   - 岔口位置：主角面临明确生死抉择的章节（第2章到倒数第2章之间）。\n"
            "   - choice_text：Bad End 节点写「[Bad End] + 死亡/失败情境短语」；原著子节点写「继续原著路线」。\n"
        )
        ending_rule = (
            "3. 原著末章为唯一结局节点（type=ending, is_original=true, next=[]）。不生成任何 is_original=false 的 ending 节点。\n"
        )
    elif branch_type == "romance":
        branch_rules = (
            "2. 【恋爱向模式】不生成任何 What-If 章节（is_original=false 的 chapter 节点），主线完整保留。\n"
            "3. 生成2-4个平行结局节点，每个对应一个可攻略角色，branch_variable=affection_<角色拼音>，threshold∈[60,70]。\n"
        )
        var_defs_rule = (
            "7. 【variable_defs 铁律】输出 JSON 必须包含顶层字段 variable_defs，每个变量对应一个攻略角色的 affection 轴：\n"
            "   - key 格式：affection_<角色拼音>（如 affection_shenqingshu），init=50，threshold∈[60,70]。\n"
            "   - 每个角色结局由对应的 affection 变量高分触发。\n"
            "   - description 必须写明：玩家怎样的行为会累积该角色好感度，以及高分时触发哪个角色结局。\n"
        )
        whatif_dag_rule = ""  # romance 无 What-If，不需要这条规则
        ending_rule = ""  # 由 ending_matrix_rule 覆盖
    elif branch_type == "fantasy":
        branch_rules = (
            f"2. 【玄幻修仙模式】在原著中选 {branch_count} 个命运岔路口（branch_point），"
            f"每个岔口衍生1章 What-If 内容（is_original=false, type=chapter），与原著下一章平级，菱形 DAG 收束。\n"
        )
        var_defs_rule = "7. variable_defs 必须为空列表 []，本模式不使用数值变量路由。\n"
        whatif_dag_rule = (
            f"5. 【What-If 拓扑铁律 — 菱形 DAG】恰好生成 {branch_count} 个 What-If 节点，\n"
            "   每个与原著兄弟章节共享同一父节点（岔口），两者 next 集合相同（收束到下游同一章节）。\n"
            "   位置约束：岔口在第2章到倒数第3章之间。choice_text 写该 What-If 走向的情境短语。\n"
        )
        ending_rule = (
            "3. 原著末章为唯一结局节点（type=ending, is_original=true, next=[]）。\n"
            "   不生成任何 is_original=false 的 ending 节点——本模式无变量路由多结局。\n"
        )
    elif branch_type == "modular":
        branch_rules = (
            "2. 【模块化轮毂模式】不生成任何 What-If 章节（is_original=false 的 chapter 节点）。\n"
            "3. 不生成平行结局节点，原著末章是唯一结局。\n"
            "4. variable_defs: []\n"
        )
        var_defs_rule = "7. variable_defs 必须为空列表 []，本模式无数值系统。\n"
        whatif_dag_rule = ""
        ending_rule = "3. 原著末章为唯一结局节点（type=ending, is_original=true, next=[]）。不生成 is_original=false 的 ending 节点。\n"
    else:
        # default
        branch_rules = (
            (f"2. 在原著中选{branch_count}个命运岔路口（branch_point），每个岔口衍生1章 What-If 内容（is_original=false），与原著下一章【平级】，两者并行后在【再下一章】收束（菱形 DAG）。\n"
             if branch_count > 0 else
             f"2. 【章节数不足，禁止生成任何 What-If 分支】本作仅{total}章，拓扑中不得出现任何 is_original=false 的中期分支章节，只有原著主线章节和末章分叉出的平行结局。\n")
        )
        var_defs_rule = (
            "7. 【variable_defs 铁律】输出 JSON 必须包含顶层字段 variable_defs，列出全局数值变量定义表。\n"
            "   - 每个变量对应一条人格维度轴（key/name/low_desc/high_desc/ending_hint 见下方 persona_axes），初始值50，取值范围0-100。\n"
            "   - 每个变量必须明确绑定到哪个节点（affects_node）、判断条件（condition）、阈值（threshold）。\n"
            "   - 这些变量是软分支 effects 唯一允许使用的 key，软分支只能修改这里定义的变量，不得自造新 key。\n"
            "   - 变量数量与 persona_axes 一致（2-4个），每个平行结局至少由一条维度轴高分触发，原著结局为默认兜底。\n"
            "   - 【description 必填内容】每个变量的 description 必须明确写明：玩家怎样的行为模式会累积此维度，以及高分时指向哪个结局走向。\n"
        )
        whatif_dag_rule = (
            f"5. 【What-If 拓扑铁律 — 菱形 DAG，不得违反】必须恰好生成 {branch_count} 个 What-If 节点。每个 What-If 节点的拓扑关系如下：\n"
            "   - 设岔口章节为第 N 章（branch_point，is_original=true，source_chapter_idx=N-1），它的 next 必须包含两条路径：\n"
            "     A) 原著第 N+1 章（is_original=true，source_chapter_idx=N）\n"
            "     B) 一个 What-If 节点（is_original=false，type=chapter）\n"
            "   - What-If 节点【替代】的是原著第 N+1 章，title 只写面向玩家的语义标题（例如『What-If：另一种选择』），不要把章节编号写进 title。\n"
            "   - What-If 节点的 next 【必须严格等于】原著第 N+1 章的 next（通常是第 N+2 章），实现菱形收束——两条路径汇入同一个下游节点。\n"
            "   - What-If 节点必须输出 merge_policy=immediate 或 bridge，以及 required_transitions 数组。"
            "如果从岔口到下游需要接受邀请、出发、旅行、到达、住宿或跨日，必须使用 bridge 并逐项列出。\n"
            "   - 也就是说：What-If 节点和原著第 N+1 章是【兄弟】，两者深度相同，下游收束节点比它们深一层。\n"
            "   - 【位置约束】岔路口章节必须在第 2 章到倒数第 3 章之间（保证 N+1 < total-1，否则收束节点不存在）。\n"
            "   【岔路口选择三要素——必须同时满足】\n"
            "   ① 主角主动性：岔路口章节中主角面临真实的、可以不同选择的决定（不是被动受害或被迫接受）。\n"
            "   ② 后果差异最大化：选择走原著第 N+1 章 vs 选择走 What-If 后续，后续世界状态差异最大。\n"
            "   ③ 衔接合理：What-If 节点收束到原著第 N+2 章时，主角的处境/情绪/同行人物必须能够无缝接到第 N+2 章开头。\n"
        )
        ending_rule = (
            f"3. 【结局深度铁律，不得违反】原著共 {total} 章，所有结局节点（含原著末章和平行结局）的深度统一为第 {total} 层，不得更深也不得更浅。\n"
            f"   - 原著第 {total} 章（is_original=true, source_chapter_idx={total-1}）是原著结局，next=[]，玩完即游戏结束。\n"
            f"   - 平行结局（is_original=false）从原著第 {total-1} 章直接分叉，与原著末章完全平级（同层），next=[]。\n"
            "   - 平行结局不是原著末章的续集，不能挂在末章之后，必须是末章的兄弟节点。\n"
        )

    system_prompt = (
        "你是顶尖视觉小说主架构师，将单线小说重构为具备分支的叙事游戏。\n"
        "法则：\n"
        "1. 原著主线完整保留（is_original=true）。\n"
        "   所有节点 title 都是面向玩家的语义标题；章节编号由 depth/章节分组提供，title 不得重复写第N章、Chapter N 或 NO.N。\n"
        + branch_rules
        + (ending_rule if branch_type not in ("default",) else "")
        + (ending_matrix_rule if branch_type in ("default", "romance") else "")
        + (ending_rule if branch_type == "default" else "")
        + "4. branch_variable 必须从本小说的人格维度轴（persona_axes）中选取 key，不得使用角色名_维度格式，不得自造新变量名。\n"
        + (whatif_dag_rule if whatif_dag_rule else "")
        + "6. 所有 next=[] 的节点为终止节点（游戏结束）。\n"
        + var_defs_rule
        + "8. 【choice_text 规则——这是玩家在上一章末尾看到的跳转按钮文字】\n"
        "   - 普通章节（单个 next）：根据本章 summary 提炼一句引导玩家进入本章的情境短语（10-20字），要有画面感和情绪感染力，如「踏入宫门的一刻，前尘往事已成镜中灰」。\n"
        "   - What-If 分岔口的原著子节点：写「沿原著之路而行」或根据原著下一章 summary 提炼的具体短语（10-20字）。\n"
        "   - What-If 节点本身（is_original=false）：写一句概括该 What-If 走向的情境短语（10-20字），体现「另一种选择」的代价与可能，如「若那夜选择留下，与命运正面交锋」。\n"
        "   - 所有结局节点（type=ending，含原著末章）：choice_text 必须填空字符串 \"\"，游戏引擎会按数值自动路由，不展示按钮。\n"
        "仅输出严格 JSON，不要任何额外文字."
        + ("\n" + lang_instr if lang_instr else "")
    )

    # ── 格式示例（按 branch_type 提供匹配示例，帮助 LLM 输出正确结构）────
    if branch_type == "death_corridor":
        example = (
            '{"variable_defs":[],'
            '"topology":['
            '{"node_id":"ch1","type":"chapter","title":"序幕落下","is_original":true,"source_chapter_idx":0,"choice_text":"序幕落下，危机悄然而至","summary":null,"branch_variable":null,"branch_threshold":null,"is_bad_ending":false,"next":["ch2"]},'
            '{"node_id":"ch2","type":"branch_point","title":"深夜走廊","is_original":true,"source_chapter_idx":1,"choice_text":"深夜走廊，前方有声响","summary":null,"branch_variable":null,"branch_threshold":null,"is_bad_ending":false,"next":["ch3","bad_end_1"]},'
            '{"node_id":"ch3","type":"chapter","title":"沿原著前行","is_original":true,"source_chapter_idx":2,"choice_text":"继续原著路线","summary":null,"branch_variable":null,"branch_threshold":null,"is_bad_ending":false,"next":["ch4"]},'
            '{"node_id":"bad_end_1","type":"bad_ending","title":"Bad End·黑暗吞噬","is_original":false,"source_chapter_idx":null,"choice_text":"[Bad End] 独自踏入黑暗，再未归来","summary":"主角独自探查，中途失去意识，被困死于密室——Bad End。","branch_variable":null,"branch_threshold":null,"is_bad_ending":true,"next":[]},'
            '{"node_id":"ch4","type":"ending","title":"原著结局","is_original":true,"source_chapter_idx":3,"choice_text":"","summary":null,"branch_variable":null,"branch_threshold":null,"is_bad_ending":false,"next":[]}'
            ']}'
        )
    elif branch_type == "romance":
        example = (
            '{"variable_defs":['
            '{"key":"affection_liqing","init":50,"description":"与李青的互动选项累积；>=65时解锁李青线结局","affects_node":"ending_liqing","threshold":65,"condition":">= 65 → 李青线结局"},'
            '{"key":"affection_weiyu","init":50,"description":"与魏宇的互动选项累积；>=65时解锁魏宇线结局","affects_node":"ending_weiyu","threshold":65,"condition":">= 65 → 魏宇线结局"}'
            '],'
            '"topology":['
            '{"node_id":"ch1","type":"chapter","title":"初入校园","is_original":true,"source_chapter_idx":0,"choice_text":"初入校园，命运的齿轮开始转动","summary":null,"branch_variable":null,"branch_threshold":null,"next":["ch2"]},'
            '{"node_id":"ch2","type":"chapter","title":"两段情缘","is_original":true,"source_chapter_idx":1,"choice_text":"两段情缘同时浮现","summary":null,"branch_variable":null,"branch_threshold":null,"next":["ch3"]},'
            '{"node_id":"ch3","type":"branch_point","title":"命运终点","is_original":true,"source_chapter_idx":2,"choice_text":"各自的心意走向终局","summary":null,"branch_variable":null,"branch_threshold":null,"next":["ch4_original","ending_liqing","ending_weiyu"]},'
            '{"node_id":"ch4_original","type":"ending","title":"原著结局","is_original":true,"source_chapter_idx":3,"choice_text":"","summary":null,"branch_variable":null,"branch_threshold":null,"next":[]},'
            '{"node_id":"ending_liqing","type":"ending","title":"结局·李青线","is_original":false,"source_chapter_idx":null,"choice_text":"","summary":"主角与李青相守，实现情感归宿——温情。","branch_variable":"affection_liqing","branch_threshold":65,"next":[]},'
            '{"node_id":"ending_weiyu","type":"ending","title":"结局·魏宇线","is_original":false,"source_chapter_idx":null,"choice_text":"","summary":"主角选择了魏宇，两人共度余生——苦涩温暖。","branch_variable":"affection_weiyu","branch_threshold":65,"next":[]}'
            ']}'
        )
    elif branch_type == "fantasy":
        example = (
            '{"variable_defs":[],'
            '"topology":['
            '{"node_id":"ch1","type":"chapter","title":"踏上修仙路","is_original":true,"source_chapter_idx":0,"choice_text":"少年踏上修仙之路","summary":null,"branch_variable":null,"branch_threshold":null,"next":["ch2"]},'
            '{"node_id":"ch2","type":"branch_point","title":"命运岔口","is_original":true,"source_chapter_idx":1,"choice_text":"师门决裂，两条路摆在眼前","summary":null,"branch_variable":null,"branch_threshold":null,"next":["ch3","ch3_alt"]},'
            '{"node_id":"ch3","type":"chapter","title":"沿原著前行","is_original":true,"source_chapter_idx":2,"choice_text":"忍辱负重，遵循原著之路","summary":null,"branch_variable":null,"branch_threshold":null,"next":["ch4"]},'
            '{"node_id":"ch3_alt","type":"chapter","title":"What-If：另一种选择","is_original":false,"source_chapter_idx":null,"choice_text":"若他当年斩断师门，独走天道","summary":"主角斩断与师门的联系，独自闯荡，付出惨重代价却获得意外机缘。","branch_variable":null,"branch_threshold":null,"next":["ch4"]},'
            '{"node_id":"ch4","type":"ending","title":"原著结局","is_original":true,"source_chapter_idx":3,"choice_text":"","summary":null,"branch_variable":null,"branch_threshold":null,"next":[]}'
            ']}'
        )
    elif branch_type == "modular":
        example = (
            '{"variable_defs":[],'
            '"topology":['
            '{"node_id":"ch1","type":"chapter","title":"神秘委托","is_original":true,"source_chapter_idx":0,"choice_text":"一封神秘委托，案件的起点","summary":null,"branch_variable":null,"branch_threshold":null,"next":["ch2"]},'
            '{"node_id":"ch2","type":"chapter","title":"线索汇聚","is_original":true,"source_chapter_idx":1,"choice_text":"线索汇聚，真相渐露轮廓","summary":null,"branch_variable":null,"branch_threshold":null,"next":["ch3"]},'
            '{"node_id":"ch3","type":"ending","title":"原著结局","is_original":true,"source_chapter_idx":2,"choice_text":"","summary":null,"branch_variable":null,"branch_threshold":null,"next":[]}'
            ']}'
        )
    else:
        # default
        example = (
            '{"variable_defs":['
            '{"key":"humanity","init":50,"description":"玩家在软分支中选择体恤他人/保留人性的选项时累积；>=65时主角在结局关头做出有人性的抉择，解锁人性觉醒结局","affects_node":"ending_A","threshold":65,"condition":">= 65 → 人性觉醒结局"},'
            '{"key":"clarity","init":50,"description":"玩家在软分支中选择看清他人牺牲的选项时累积；>=60时主角在终局岔口选择和解","affects_node":"ending_B","threshold":60,"condition":">= 60 → 静水深流结局"}'
            '],'
            '"topology": ['
            '{"node_id":"ch1","type":"chapter","title":"孤身入京","is_original":true,"source_chapter_idx":0,"choice_text":"少年孤身入京，命运齿轮悄然咬合","summary":null,"branch_variable":null,"branch_threshold":null,"next":["ch2"]},'
            '{"node_id":"ch2","type":"branch_point","title":"宫宴惊变","is_original":true,"source_chapter_idx":1,"choice_text":"宫宴惊变，杀机在觥筹交错间浮现","summary":null,"branch_variable":null,"branch_threshold":null,"next":["ch3","ch3_alt"]},'
            '{"node_id":"ch3","type":"chapter","title":"沿原著前行","is_original":true,"source_chapter_idx":2,"choice_text":"沿原著之路，忍下这口血与火","summary":null,"branch_variable":null,"branch_threshold":null,"next":["ch4"]},'
            '{"node_id":"ch3_alt","type":"chapter","title":"What-If：另一种选择","is_original":false,"source_chapter_idx":null,"choice_text":"若那夜他选择留下，与命运正面交锋","summary":"主角选择留下，与命运正面交锋，导致罗网暗桩暴露但保全亲人。","branch_variable":null,"branch_threshold":null,"next":["ch4"]},'
            '{"node_id":"ch4","type":"branch_point","title":"命运终点","is_original":true,"source_chapter_idx":3,"choice_text":"血月之夜，万千布局走向最后的收束","summary":null,"branch_variable":null,"branch_threshold":null,"next":["ch5_original","ending_A","ending_B"]},'
            '{"node_id":"ch5_original","type":"ending","title":"原著结局","is_original":true,"source_chapter_idx":4,"choice_text":"","summary":null,"branch_variable":null,"branch_threshold":null,"next":[]},'
            '{"node_id":"ending_A","type":"ending","title":"结局·人性觉醒","is_original":false,"source_chapter_idx":null,"choice_text":"","summary":"与原著截然不同的平行结局：主角坚守人性，走向孤独但无悔的结局。","branch_variable":"humanity","branch_threshold":65,"next":[]},'
            '{"node_id":"ending_B","type":"ending","title":"结局·静水深流","is_original":false,"source_chapter_idx":null,"choice_text":"","summary":"与原著截然不同的平行结局：主角放下执念走向和解，故事在某个清晨平静收束。","branch_variable":"clarity","branch_threshold":60,"next":[]}'
            ']}'
        )

    axes = persona_axes or []
    axes_text = ""
    if axes and branch_type in ("default", "romance"):
        axes_lines = [
            f"  - {a.get('key')}（{a.get('name')}）：低分={a.get('low_desc')}，高分={a.get('high_desc')}，结局指向={a.get('ending_hint')}"
            for a in axes
        ]
        axes_text = "\n【本小说人格维度轴（variable_defs 必须覆盖以下 key）】\n" + "\n".join(axes_lines) + "\n\n"

    # branch_type 专属任务指令
    if branch_type == "death_corridor":
        task_instruction = (
            f"现在请为本小说（{total}章）输出完整的 JSON 拓扑图，"
            f"生成 {branch_count} 个 Bad End 节点（type=bad_ending, is_bad_ending=true, next=[]）。\n"
            "【岔口选择】找主角面临明确生死威胁的章节（第2章到倒数第2章之间），"
            "Bad End 节点挂在该章 next 里，与原著下一章平级，但 next=[] 为终止节点。\n"
            "【格式要求】variable_defs 必须为 []。所有非分支原著章节 branch_variable=null。"
        )
    elif branch_type == "romance":
        task_instruction = (
            f"现在请为本小说（{total}章）输出完整的 JSON 拓扑图，\n"
            "不生成任何 What-If 章节（is_original=false 的 chapter 节点）。\n"
            "生成 2-4 个平行结局，每个对应一个攻略角色，branch_variable=affection_<角色拼音>，threshold∈[60,70]。\n"
            f"【结局深度铁律】所有结局节点深度统一为第 {total} 层（原著末章的兄弟节点）。"
        )
    elif branch_type == "fantasy":
        task_instruction = (
            f"现在请为本小说（{total}章）输出完整的 JSON 拓扑图，"
            f"生成 {branch_count} 个 What-If 章节（菱形 DAG，收束到下一章）。\n"
            "不生成任何平行结局节点（is_original=false 的 ending 节点），原著末章是唯一结局。\n"
            "variable_defs 必须为 []。"
        )
    elif branch_type == "modular":
        task_instruction = (
            f"现在请为本小说（{total}章）输出完整的 JSON 拓扑图，\n"
            "不生成任何 What-If 章节，不生成平行结局节点，variable_defs 为 []。\n"
            "纯线性原著主线，所有章节 is_original=true，末章 type=ending。"
        )
    else:
        task_instruction = (
            f"现在请为本小说（{total}章）输出完整的 JSON 拓扑图，"
            f"设计 {branch_count} 个中期 What-If 分支 + 2-3 个平行结局节点。\n"
            f"【岔路口选择提示】请根据上方各章摘要，找出主角主动做决定、且不同选择后果差异最大的章节作为岔路口。"
            f"优先选择摘要中出现「决定」「选择」「拒绝」「答应」等主动行为词的章节，避免选择主角完全被动的章节。\n"
            f"【结局深度硬约束】所有结局（原著末章 + 平行结局）depth 统一为第 {total} 层。"
            f"平行结局必须是原著第 {total-1} 章的子节点（兄弟于原著末章），绝不能是原著末章的子节点。"
            "原著末章 next=[]，平行结局 next=[]，两者平级，游戏结束后不再延伸。"
        )

    user_prompt = (
        f"以下是原著共 {total} 章的摘要：\n\n{summaries_text}\n\n"
        f"{axes_text}"
        f"请参考以下格式示例（{total}章时节点数量和分支位置请自行调整）：\n{example}\n\n"
        + task_instruction
    )

    persona_keys = {a.get("key") for a in (persona_axes or []) if isinstance(a, dict) and a.get("key")}

    def _validate_topology(result: dict) -> tuple:
        """返回 (ok: bool, reason: str)，ok=False 时 reason 给 LLM 做反例提示"""
        graph_ok, graph_reason = _validate_topology_graph(result, total)
        if not graph_ok:
            return False, graph_reason
        topo = result["topology"]
        # 校验1：按模式校验 What-If / Bad End 数量和菱形 DAG
        if branch_type in ("death_corridor",):
            # 死亡回廊：校验 bad_ending 数量
            bad_end_nodes = [t for t in topo if t.get("type") == "bad_ending"]
            if len(bad_end_nodes) != branch_count:
                return False, f"Bad End 节点数量不符（期望 {branch_count}，实得 {len(bad_end_nodes)}）"
            # bad_ending 节点必须是终止节点
            for be in bad_end_nodes:
                if be.get("next"):
                    return False, f"Bad End 节点 '{be['node_id']}' 的 next 不为空（应为终止节点）"
        elif branch_type not in ("romance", "modular"):
            # default / fantasy：校验 What-If 节点数量
            whatif_nodes = [t for t in topo if not t.get("is_original") and t.get("type") not in ("ending", "bad_ending")]
            if len(whatif_nodes) != branch_count:
                return False, f"What-If 节点数量不符（期望 {branch_count}，实得 {len(whatif_nodes)})"
            # 菱形 DAG 校验（fantasy 同样需要菱形收束）
            node_by_id = {t["node_id"]: t for t in topo}
            for wf in whatif_nodes:
                wf_id = wf["node_id"]
                parents = [t for t in topo if wf_id in t.get("next", [])]
                if not parents:
                    return False, f"What-If 节点 '{wf_id}' 没有父节点（不在任何 branch_point 的 next 里）"
                pivot = parents[0]
                orig_siblings = [
                    node_by_id[sid] for sid in pivot.get("next", [])
                    if sid != wf_id and sid in node_by_id
                    and node_by_id[sid].get("is_original")
                    and node_by_id[sid].get("type") not in ("ending", "bad_ending")
                ]
                if not orig_siblings:
                    return False, (
                        f"What-If 节点 '{wf_id}' 的父节点 '{pivot['node_id']}' 没有平级原著兄弟章节，"
                        f"违反菱形 DAG 结构"
                    )
                sib = orig_siblings[0]
                wf_next = sorted(wf.get("next", []))
                sib_next = sorted(sib.get("next", []))
                if wf_next != sib_next:
                    return False, (
                        f"What-If 节点 '{wf_id}'.next={wf_next} 与原著兄弟 '{sib['node_id']}'.next={sib_next} 不一致，"
                        f"违反菱形 DAG"
                    )
                # Chapter numbering is represented by depth/source_chapter_idx;
                # title is player-facing copy and must not be rejected merely
                # because it omits a duplicated numeric prefix.
                if not str(wf.get("title") or "").strip():
                    return False, f"What-If 节点 '{wf_id}' 缺少有效 title"
        # 校验4：variable_defs 校验（仅 default / romance 需要非空）
        var_defs = result.get("variable_defs", [])
        if branch_type in ("death_corridor", "fantasy", "modular"):
            # 这三种类型不使用数值变量，variable_defs 必须为空
            if var_defs:
                return False, f"branch_type={branch_type} 不允许 variable_defs（应为 []），当前非空"
        else:
            if not isinstance(var_defs, list) or not var_defs:
                return False, "variable_defs 为空"
            var_keys = {v.get("key") for v in var_defs if isinstance(v, dict) and v.get("key")}
            if persona_keys and var_keys != persona_keys:
                return False, (
                    f"variable_defs 的 key 集合与 persona_axes 不一致："
                    f"persona_axes 需要 {sorted(persona_keys)}，variable_defs 给的是 {sorted(var_keys)}。"
                    f"必须严格使用 persona_axes 的全部 key，且不得多出新 key。"
                )
        # 校验5：阈值可达性（仅 default/romance 有 variable_defs，才做此校验）
        warnings = []
        if branch_type in ("death_corridor", "fantasy", "modular"):
            if warnings:
                print(f"   ⚠️  阈值警告（不阻塞）：{warnings}")
            return True, ""
        est_total_branches = max(branch_count * 4, total * 4)
        for v in var_defs:
            if not isinstance(v, dict):
                continue
            init = int(v.get("init", 50))
            thr = v.get("threshold")
            if thr is None:
                continue
            try:
                thr = int(thr)
            except (TypeError, ValueError):
                return False, f"变量 {v.get('key')} 的 threshold 非整数：{thr}"
            needed = abs(thr - init)
            # cap clamped to [3, 15]; 玩家假设 65% 同方向，max 累积 = est_total_branches * 0.65 * cap
            # 反过来：单支理论最大累积 ≈ 12（cap=15 取 0.8 折）
            theoretical_max = int(est_total_branches * 0.65 * 12)
            if needed > theoretical_max:
                return False, (
                    f"变量 {v.get('key')} 阈值不可达：init={init}, threshold={thr}, 距离={needed}，"
                    f"全书 {est_total_branches} 个软分支理论最大累积 {theoretical_max}。"
                    f"请将阈值调整到 [init±{theoretical_max}] 区间内（建议距离 init 在 8-25 之间）。"
                )
            if needed < 5:
                warnings.append(f"{v.get('key')} 阈值距离 init 仅 {needed}，太容易触发")
        # 校验6：同一变量的多结局阈值间距 ≥ 10（避免互斥的双高阈值）
        var_thr_map = {}
        for t in topo:
            if t.get("type") == "ending":
                bv = t.get("branch_variable")
                bt = t.get("branch_threshold")
                if bv and bt is not None:
                    var_thr_map.setdefault(bv, []).append(int(bt))
        for bv, thrs in var_thr_map.items():
            thrs_sorted = sorted(thrs)
            for i in range(len(thrs_sorted) - 1):
                if thrs_sorted[i + 1] - thrs_sorted[i] < 10:
                    return False, (
                        f"变量 {bv} 的多个结局阈值间距过近（{thrs_sorted}），"
                        f"间距应 ≥ 10，否则玩家很难精准触发其中一个。"
                    )
        if warnings:
            print(f"   ⚠️  阈值警告（不阻塞）：{warnings}")
        return True, ""

    last_failure_reason = ""
    previous_result = None
    for attempt in range(4):
        retry_hint = ""
        if attempt > 0 and last_failure_reason:
            retry_hint = (
                "\n\n## 局部修复任务\n"
                "上次输出不合规：" + last_failure_reason +
                "\n请保留所有合法节点、变量定义和连线，只修复导致校验失败的节点或字段。"
                "不要重新设计全部拓扑，不要改变已经合法的 node_id。"
                "\n上一次候选 JSON：\n"
                + json.dumps(previous_result, ensure_ascii=False)
            )
        try:
            result = client.chat_completion_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt + retry_hint,
                temperature=0.4,
                max_tokens=3000,
            )
            ok, reason = _validate_topology(result)
            if ok:
                result = _normalize_topology_choice_texts(result)
                print(f"   ✅ 拓扑图规划完成，共 {len(result['topology'])} 个节点（attempt={attempt+1}）")
                return result
            print(f"   🔁 拓扑校验未通过（attempt={attempt+1}）：{reason}")
            last_failure_reason = reason
            previous_result = result
        except Exception as e:
            print(f"   ⚠️  拓扑生成异常（attempt={attempt+1}）：{e}")
            last_failure_reason = f"上次生成抛异常：{e}"

    print(f"   ⚠️  拓扑四次均未通过校验，使用 fallback")
    return _normalize_topology_choice_texts(_make_fallback_topology(
        total,
        persona_axes=axes,
        branch_type=branch_type,
        romance_targets=romance_targets,
        summaries=summaries,
    ))


def _make_fallback_topology(
    total: int,
    persona_axes: list = None,
    branch_type: str = "default",
    romance_targets: list = None,
    summaries: list = None,
) -> dict:
    """按 branch_type 生成结构合法、可继续执行的确定性 fallback。"""
    if total <= 0:
        return {"topology": [], "variable_defs": []}

    nodes = []
    for index in range(total):
        nodes.append({
            "node_id": f"ch{index + 1}",
            "type": "ending" if index == total - 1 else "chapter",
            "title": "原著结局" if index == total - 1 else "未命名章节",
            "is_original": True,
            "source_chapter_idx": index,
            "choice_text": (
                "" if index == total - 1
                else ("继续原著路线" if index > 0 else "故事开始")
            ),
            "summary": None,
            "branch_variable": None,
            "branch_threshold": None,
            "next": [] if index == total - 1 else [f"ch{index + 2}"],
        })

    variable_defs = []

    if branch_type == "death_corridor":
        branch_count = 0 if total < 4 else min(2, max(1, total // 5))
        for branch_index in range(branch_count):
            pivot_index = min(1 + branch_index * 3, total - 2)
            bad_end_id = f"bad_end_{branch_index + 1}"
            pivot = nodes[pivot_index]
            pivot["type"] = "branch_point"
            pivot["next"].append(bad_end_id)
            nodes.append({
                "node_id": bad_end_id,
                "type": "bad_ending",
                "title": f"Bad End {branch_index + 1}",
                "is_original": False,
                "source_chapter_idx": None,
                "choice_text": "[Bad End] 走向致命选择",
                "summary": "主角在错误选择后遭遇致命失败。",
                "branch_variable": None,
                "branch_threshold": None,
                "is_bad_ending": True,
                "next": [],
            })
        return {"topology": nodes, "variable_defs": variable_defs}

    if branch_type in ("default", "fantasy"):
        branch_count = 0 if total < 5 else max(1, (total - 2) // 3)
        fallback_summaries = list(summaries or [])
        pivot_indexes = _select_fallback_pivots(total, branch_count, fallback_summaries)
        for branch_index, pivot_index in enumerate(pivot_indexes):
            if pivot_index + 2 >= total:
                break
            sibling = nodes[pivot_index + 1]
            whatif_id = f"{sibling['node_id']}_alt"
            pivot = nodes[pivot_index]
            pivot["type"] = "branch_point"
            pivot["next"].append(whatif_id)
            nodes.append({
                "node_id": whatif_id,
                "type": "chapter",
                "title": "What-If：另一种选择",
                "is_original": False,
                "source_chapter_idx": None,
                "choice_text": "若选择" + _decision_phrase(
                    fallback_summaries[pivot_index]
                    if pivot_index < len(fallback_summaries) else "做出另一种选择"
                ),
                "summary": (
                    f"主角在第{pivot_index + 1}章的关键决定上采取不同选择，"
                    "承担新的代价，并在后续节点尝试重新收束至主线。"
                ),
                "branch_variable": None,
                "branch_threshold": None,
                "merge_policy": "bridge",
                "required_transitions": [
                    "覆盖被替代原著章节的完整时间跨度",
                    "到达共同下游所需的时间、地点和人物状态",
                ],
                "next": list(sibling["next"]),
            })
        if branch_type == "fantasy":
            return {"topology": nodes, "variable_defs": variable_defs}

    if branch_type == "modular" or total < 2:
        return {"topology": nodes, "variable_defs": variable_defs}

    if branch_type == "romance":
        targets = [
            target for target in (romance_targets or [])
            if isinstance(target, dict)
        ][:4]
        if len(targets) < 2:
            targets = [
                {"name": "角色一", "role_label": "攻略角色一", "variable_key": "affection_target_1"},
                {"name": "角色二", "role_label": "攻略角色二", "variable_key": "affection_target_2"},
            ]
        ending_specs = []
        for index, target in enumerate(targets):
            explicit_key = str(target.get("variable_key") or "").strip()
            pinyin = "".join(
                ch for ch in str(target.get("pinyin") or "").lower()
                if ch.isascii() and (ch.isalnum() or ch == "_")
            )
            variable_key = explicit_key or (
                f"affection_{pinyin}" if pinyin else f"affection_target_{index + 1}"
            )
            ending_specs.append({
                "key": variable_key,
                "name": target.get("name") or target.get("role_label") or f"角色{index + 1}",
                "hint": f"与{target.get('name') or target.get('role_label') or f'角色{index + 1}'}共同走向结局",
                "threshold": 65,
            })
    else:
        axes = [
            axis for axis in (persona_axes or [])
            if isinstance(axis, dict) and axis.get("key")
        ]
        while len(axes) < 2:
            defaults = [
                {"key": "courage", "name": "孤勇", "ending_hint": "高分指向主角孤勇直面命运的结局"},
                {"key": "calmness", "name": "静水", "ending_hint": "高分指向主角放下执念走向和解的结局"},
            ]
            axes.append(defaults[len(axes)])
        ending_specs = [
            {
                "key": axis["key"],
                "name": axis.get("name", axis["key"]),
                "hint": axis.get("ending_hint", f"高分指向{axis['key']}结局"),
                "threshold": 65 if index == 0 else 60,
            }
            for index, axis in enumerate(axes[:2])
        ]

    pivot = nodes[total - 2]
    pivot["type"] = "branch_point"
    for index, spec in enumerate(ending_specs):
        ending_id = f"ending_{index + 1}"
        pivot["next"].append(ending_id)
        nodes.append({
            "node_id": ending_id,
            "type": "ending",
            "title": f"结局·{spec['name']}",
            "is_original": False,
            "source_chapter_idx": None,
            "choice_text": "",
            "summary": spec["hint"],
            "branch_variable": spec["key"],
            "branch_threshold": spec["threshold"],
            "next": [],
        })
        variable_defs.append({
            "key": spec["key"],
            "init": 50,
            "description": spec["hint"],
            "affects_node": ending_id,
            "threshold": spec["threshold"],
            "condition": f">= {spec['threshold']} → {spec['name']}结局",
        })

    return {"topology": nodes, "variable_defs": variable_defs}


def _extract_pivot_anchors(pivot_chapter_text: str) -> tuple:
    """
    提取 pivot 章节的"硬起点"——用于 What-If 第一场必须紧接此后发生。
    返回 (last_paragraph: str, last_sentence: str)
    """
    text = (pivot_chapter_text or "").strip()
    if not text:
        return "", ""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    last_paragraph = paragraphs[-1] if paragraphs else text
    # 取最后一句（按 。！？!?\n 切）
    import re as _re
    sents = _re.split(r"(?<=[。！？!?])\s*", last_paragraph)
    sents = [s.strip() for s in sents if s and s.strip()]
    last_sentence = sents[-1] if sents else last_paragraph
    return last_paragraph, last_sentence


def _extract_convergence_anchors(convergence_opening: str) -> tuple:
    """
    提取主线收束章节的"硬终点"——What-If 末场必须能续到这里。
    返回 (first_paragraph: str, first_sentence: str)
    """
    text = (convergence_opening or "").strip()
    if not text:
        return "", ""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    first_paragraph = paragraphs[0] if paragraphs else text
    import re as _re
    sents = _re.split(r"(?<=[。！？!?])\s*", first_paragraph)
    sents = [s.strip() for s in sents if s and s.strip()]
    first_sentence = sents[0] if sents else first_paragraph
    return first_paragraph, first_sentence


def build_fixed_fact_summary_context(
    chapter_summaries: list,
    pivot_summary: str,
) -> str:
    """为既定事实抽取选择岔口附近章节，并召回少量相关历史决策。"""
    if not chapter_summaries:
        return ""
    from services.story_planner.core.chapter_context import build_layered_summary_block

    pivot_index = next(
        (index for index, summary in enumerate(chapter_summaries) if summary == pivot_summary),
        0,
    )
    return build_layered_summary_block(
        chapter_summaries,
        current_index=pivot_index,
        query=pivot_summary,
        neighbor_radius=2,
        remote_limit=2,
    )


def resolve_whatif_budget(
    target_chars: int,
    *,
    already_budgeted: bool = False,
) -> dict:
    """Resolve one What-If budget exactly once across caller and writer boundaries."""
    from services.story_planner.core.chapter_branch_runtime import plan_whatif_budget

    if already_budgeted:
        normalized_target = max(600, min(12000, int(target_chars or 600)))
        if normalized_target <= 1200:
            scene_count = 1
        elif normalized_target <= 3000:
            scene_count = 2
        elif normalized_target <= 6000:
            scene_count = 3
        else:
            scene_count = 4
        return {
            "target_chars": normalized_target,
            "scene_count": scene_count,
            "soft_branch_count": max(0, scene_count - 1),
        }
    return plan_whatif_budget(target_chars)


def _extract_fixed_facts(
    pivot_full_text: str,
    pivot_summary: str,
    chapter_summaries: list,
    provider: str,
    output_language: str = None,
) -> str:
    """
    从 pivot 章节全文 + 全书摘要 抽取"既定事实清单"——What-If 大纲/正文绝对不能逆转。
    返回一段已格式化的 markdown 文本（若失败返回空串）。
    """
    if not pivot_full_text:
        return ""
    client = LLMClient(provider=provider)
    lang_instr = get_lang_instruction(output_language)
    summaries_block = build_fixed_fact_summary_context(
        chapter_summaries or [],
        pivot_summary,
    )
    sys_p = (
        "你是叙事结构分析师。给你一章原文和全书摘要，你要列出一份『既定事实清单』——"
        "这些是 What-If 平行线绝对不可改变的事实（角色生死、已发生的关键事件、时空设定、关键人物身份与立场）。"
        "请只列出最具结构性约束力的 6-12 条，每条一行，简短直接。"
        + ("\n" + lang_instr if lang_instr else "")
    )
    user_p = (
        f"{summaries_block}"
        f"## 岔路口章节全文\n{pivot_full_text}\n\n"
        f"## 岔路口章节摘要\n{pivot_summary}\n\n"
        "请输出严格 JSON：{\"facts\": [\"事实1\", \"事实2\", ...]}"
    )
    try:
        res = client.chat_completion_json(
            system_prompt=sys_p,
            user_prompt=user_p,
            temperature=0.2,
            max_tokens=1200,
        )
        facts = res.get("facts") or res.get("data") or []
        if not isinstance(facts, list) or not facts:
            return ""
        return "## 既定事实清单（What-If 绝对不可逆转）\n" + "\n".join(
            f"  {i+1}. {str(f).strip()}" for i, f in enumerate(facts)
        ) + "\n\n"
    except Exception as e:
        print(f"   ⚠️  既定事实抽取失败（{e}），跳过")
        return ""


def generate_whatif_chapter(
    node: dict,
    style_ref: str,
    pivot_chapter_text: str,
    provider: str,
    pivot_summary: str = "",
    novel_context=None,
    convergence_opening: str = "",
    convergence_summary: str = "",
    convergence_tone: str = "",
    original_sibling_summary: str = "",
    sibling_chapter_text: str = "",
    sibling_chapter_title: str = "",
    trigger_variable_info: str = "",
    chapter_summaries: list = None,
    output_language: str = None,
    target_chars: int = 10000,
    variable_defs: list = None,
    parent_terminal_states: dict = None,
    sibling_terminal_states: dict = None,
    bad_ending_mode: bool = False,
    behavior_anchor_chapter_index: int | None = None,
    target_chars_already_budgeted: bool = False,
    checkpoint_callback: Optional[Callable[[dict], None]] = None,
    resume_state: Optional[dict] = None,
    narrative_pov_contract: dict | None = None,
) -> str:
    """
    为 What-If 章节生成内容（分场大纲 → 循环扩写，每场2000-3000字）。
    node: topology 节点（is_original=false, type=chapter）
    style_ref: 原著风格样本（来自 NovelContext.style_samples，已是 LLM 精选）
    pivot_chapter_text: 岔路口所在章节的完整原文（不截断），提供情境衔接
    pivot_summary: 岔路口章节的 LLM 摘要，提供核心矛盾
    novel_context: NovelContext，提供角色卡、关系线
    convergence_opening: 主线收束章节的完整原文（不截断），用于衔接判断
    convergence_summary: 主线收束章节的 LLM 摘要
    original_sibling_summary: 平级原著兄弟章节摘要（What-If 替代的那一章）——防撞车锚点
    sibling_chapter_text: 平级原著兄弟章节全文（What-If 实际"替代"的那一章原文）——
        必须给 LLM 看，否则它不知道在替换什么剧情，写出来会乱。
    sibling_chapter_title: 兄弟章节标题（用于 prompt 标注）
    trigger_variable_info: 触发此分支的数值条件（如 '沈清舒_affection >= 60'）
    chapter_summaries: 各章节摘要列表，供 LLM 了解全书剧情脉络
    """
    node = normalize_whatif_merge_policy(node)
    client = LLMClient(provider=provider)
    from services.story_planner.core.chapter_context import build_layered_summary_block

    narrative_pov_contract = _context_narrative_pov_contract(
        novel_context,
        narrative_pov_contract,
    )
    narrative_pov_block = _narrative_pov_prompt_block(narrative_pov_contract)

    whatif_budget = resolve_whatif_budget(
        target_chars,
        already_budgeted=target_chars_already_budgeted,
    )
    target_chars = whatif_budget["target_chars"]
    summary = node.get("summary", "")
    title = node.get("title", "What-If 章节")
    merge_policy = node.get("merge_policy", "immediate")
    required_transitions = list(node.get("required_transitions") or [])
    lang_instr = get_lang_instruction(output_language)

    # 构建角色/世界锚点（防止 LLM 虚构角色或违背设定）
    char_cards = getattr(novel_context, "char_cards", "") if novel_context else ""
    relationship_map = getattr(novel_context, "relationship_map", "") if novel_context else ""
    world_rules = getattr(novel_context, "world_rules", "") if novel_context else ""
    style_samples = getattr(novel_context, "style_samples", "") if novel_context else ""
    factions_text = getattr(novel_context, "factions_text", "") if novel_context else ""
    hidden_threads_text = getattr(novel_context, "hidden_threads_text", "") if novel_context else ""
    character_secrets_text = getattr(novel_context, "character_secrets_text", "") if novel_context else ""
    narrative_arc = getattr(novel_context, "narrative_arc", "") if novel_context else ""
    character_behavior_anchors = getattr(novel_context, "character_behavior_anchors", "") if novel_context else ""
    if character_behavior_anchors:
        from services.story_planner.core.novel_reader import (
            filter_behavior_anchors_for_bad_end,
            filter_behavior_anchors_for_whatif,
        )
        if bad_ending_mode:
            character_behavior_anchors = filter_behavior_anchors_for_bad_end(
                character_behavior_anchors,
                pivot_chapter_index=behavior_anchor_chapter_index,
            )
        else:
            character_behavior_anchors = filter_behavior_anchors_for_whatif(
                character_behavior_anchors,
                pivot_chapter_index=behavior_anchor_chapter_index,
            )
    is_romance_oriented = bool(getattr(novel_context, "is_romance_oriented", False)) if novel_context else False
    worldview = getattr(novel_context, "worldview", {}) if novel_context else {}
    romance_targets = worldview.get("romance_targets", []) if isinstance(worldview, dict) else []
    romance_lookup_text = ""
    if is_romance_oriented and romance_targets:
        lines = [
            f"  - {t.get('name','')}（{t.get('role_label','')}）→ affection_<{t.get('name','')}拼音>，关键互动：{', '.join(t.get('key_scenes', [])[:2])}"
            for t in romance_targets if isinstance(t, dict)
        ]
        if lines:
            romance_lookup_text = "【恋爱向角色变量映射 — 你写 What-If 内容时，请明确指出哪条 affection 轴对应哪个人，并让该角色在剧情中真实出现并互动】\n" + "\n".join(lines) + "\n\n"

    anchor_block = ""
    if narrative_arc:
        anchor_block += f"【固定世界观摘要/全书叙事弧线】\n{str(narrative_arc)[:3000]}\n\n"
    if world_rules:
        anchor_block += f"【世界规则/设定（严格遵守）】\n{str(world_rules)[:3000]}\n\n"
    if factions_text:
        anchor_block += f"{factions_text}\n\n"
    if hidden_threads_text:
        anchor_block += f"{hidden_threads_text}\n\n"
    if character_secrets_text:
        anchor_block += f"{character_secrets_text}\n\n"
    if char_cards:
        anchor_block += f"【核心人物卡】\n{str(char_cards)[:6000]}\n\n"
    if relationship_map:
        anchor_block += f"{str(relationship_map)[:2500]}\n\n"
    if style_samples:
        anchor_block += f"【语言风格样本（严格模仿）】\n{str(style_samples)[:2500]}\n\n"
    if character_behavior_anchors:
        anchor_block += f"{str(character_behavior_anchors)[:3000]}\n\n"

    # 分层剧情上下文：当前章 ±2 + 少量相关历史决策召回，不再全量塞入全书摘要。
    summaries_block = ""
    if chapter_summaries:
        pivot_idx = None
        for i, s in enumerate(chapter_summaries):
            if pivot_summary and s == pivot_summary:
                pivot_idx = i
                break
        summaries_block = build_layered_summary_block(
            chapter_summaries,
            current_index=pivot_idx if pivot_idx is not None else 0,
            query=" ".join(
                item for item in (pivot_summary, summary, trigger_variable_info) if item
            ),
        )

    # What-If 正文按原著平均章长自适应，短篇不再被强制扩写为长篇。
    scene_count = whatif_budget["scene_count"]
    chars_per_scene = max(400, round(target_chars / scene_count))

    # Step 1: 生成场次大纲
    anti_collision = (
        f"\n🚨 【防撞车强警告 — 原著兄弟章节走向】这本 What-If【替代】的是原著《{sibling_chapter_title or '兄弟章节'}》。"
        f"原著这一章实际发生的剧情是：【{original_sibling_summary}】。\n"
        "你的大纲必须让主角的决策过程、人物互动、局部事件和情绪体验与原著明显不同；"
        "但不得为了制造差异而永久改变共同下游所依赖的生死、核心身份和外层世界线。"
        "What-If 的影响可以保留，但必须让下游章节仍然成立。\n"
        if original_sibling_summary else ""
    )
    stat_checkout = (
        f"\n【核心约束 - 数值情感结算】玩家是因为长期积累了【{trigger_variable_info}】才解锁此分支的。"
        "这意味着玩家在全程软分支中反复做出了与该维度一致的选择，主角的内心已被这个方向深度塑造。"
        "你的大纲高潮必须让这种积累产生具体的、不可替代的叙事后果——"
        "不是抽象地「展现维度」，而是写出一个只有走过这条路的主角才会做出的决定或反应，"
        "让玩家感受到「正是因为我一直这样选择，才有了这一刻」。\n"
        if trigger_variable_info else ""
    )
    outline_system = (
        "你是视觉小说编剧，负责将一个 What-If 命运走向规划成可逐场撰写的剧本大纲。"
        "所有角色行为必须符合角色卡设定，不得创造原著中不存在的角色，不得违背世界规则。"
        + narrative_pov_block
        + anti_collision
        + stat_checkout
        + ("\n" + lang_instr if lang_instr else "")
    )
    pivot_context = (
        f"【章节摘要（核心矛盾）】\n{pivot_summary}\n\n" if pivot_summary else ""
    )
    pivot_full = pivot_chapter_text or ""
    pivot_last_paragraph, pivot_last_sentence = _extract_pivot_anchors(pivot_full)
    convergence_first_paragraph, convergence_first_sentence = _extract_convergence_anchors(convergence_opening)
    convergence_summary_block = (
        f"\n## 主线收束章节摘要\n{convergence_summary}\n" if convergence_summary else ""
    )

    # 抽取既定事实清单（结构性硬约束，What-If 不可逆转）
    fixed_facts_block = _extract_fixed_facts(
        pivot_full_text=pivot_full,
        pivot_summary=pivot_summary,
        chapter_summaries=chapter_summaries or [],
        provider=provider,
        output_language=output_language,
    )

    # 角色生死硬约束：parent_terminal_states 是 What-If 起点状态（不可违反），
    # sibling_terminal_states 是兄弟章节末态，作为"What-If 末场需要对齐的下游入口"——
    # 因为 What-If 与兄弟章共用收束节点，结束时核心人物的生死必须与兄弟章末一致，
    # 否则收束章会出现"已死之人在新一章活着"或反过来的穿帮。
    from services.story_planner.core.novel_reader import format_terminal_states_for_prompt
    parent_states_block = format_terminal_states_for_prompt(
        parent_terminal_states or {},
        label="What-If 起点：岔路口章节末的角色状态（本 What-If 第一场必须沿用此状态）",
    )
    sibling_states_block = format_terminal_states_for_prompt(
        sibling_terminal_states or {},
        label=(
            "What-If 共同下游的永久硬状态参考（生死、外层现实域、"
            "核心身份必须兼容；局部时间/地点/出场差异需通过正文过渡）"
        ),
    )
    merge_contract_block = (
        "## What-If 收束契约\n"
        f"merge_policy={merge_policy}\n"
        f"required_transitions={json.dumps(required_transitions, ensure_ascii=False)}\n"
        "过程必须不同，但永久末态必须允许下游成立；不得通过倒带、失忆或宣称 What-If 从未发生来归零影响。\n\n"
    )

    pivot_block = ""
    if pivot_full:
        pivot_block += (
            "## 岔路口章节末段（What-If 必须紧接此章发生，不得改写已发生的事实）\n"
            f"{pivot_full[-6000:]}\n\n"
        )
    if pivot_last_paragraph:
        pivot_block += (
            f"## 【硬起点】岔路口章节最后一段（What-If 第一场必须紧接此后发生，不得回放、不得改写）\n"
            f"{pivot_last_paragraph}\n\n"
            f"## 【硬起点·最后一句】\n> 「{pivot_last_sentence}」\n\n"
        )

    convergence_block = ""
    if convergence_opening:
        convergence_block += (
            "## 主线收束章节开头（What-If 末场结尾必须能自然续到这里）\n"
            f"{convergence_opening[:6000]}\n\n"
        )
    if convergence_first_paragraph:
        convergence_block += (
            f"## 【硬终点】主线收束章节第一段（What-If 末场结尾必须无缝衔接此段）\n"
            f"{convergence_first_paragraph}\n\n"
            f"## 【硬终点·第一句】\n> 「{convergence_first_sentence}」\n\n"
        )

    # 平级原著兄弟章节全文：What-If 替代的就是这一章，必须给 LLM 看到具体被替代的剧情，
    # 否则它只能根据摘要凭空想象，写出来既无法与原著形成强对比，也接不上下游的收束章。
    sibling_block = ""
    if sibling_chapter_text:
        sibling_block = (
            f"## 【★平级原著兄弟章节全文】《{sibling_chapter_title or '原著下一章'}》\n"
            "本 What-If 章节是这一章的【平行替代版本】，玩家选了 What-If 路径就不会读到这一章。\n"
            "你必须通读这一章原文，做到：\n"
            "  ① 知道原著这一章具体发生了什么（关键事件、人物决定、情感走向）；\n"
            "  ② 你写的 What-If 在【人物决定、互动过程、局部事件、情感体验】上必须与本章明显不同——"
            "但不得破坏【既定事实清单】和共同下游所依赖的永久状态；\n"
            "  ③ 末场结尾必须能让主角的处境/情绪/位置自然衔接到下一章（收束章）开头，与原著兄弟收束到同一个下游节点。\n\n"
            f"{sibling_chapter_text[:6000]}\n\n"
        )

    # ── Bad End 专用路径（death_corridor 类型的终态节点）────────────────
    if bad_ending_mode:
        be_target = min(target_chars, 3500)
        be_chars_per_scene = 1700
        be_scene_count = max(2, min(3, round(be_target / be_chars_per_scene)))

        be_outline_system = (
            "你是恐怖/悬疑视觉小说编剧，专门设计让玩家感到震撼的 Bad End（坏结局节点）。\n"
            + narrative_pov_block
            + "Bad End 设计原则：\n"
            + "1. 主角必须在本结局中死亡或遭遇不可逆的毁灭性失败——这是硬要求，不得回避、不得给出转机。\n"
            + "2. 死亡不是随机意外，而是主角在关键时刻做出了某个『错误』抉择的必然结果，让玩家回想时懊悔。\n"
            + "3. 这不是『另一条路』，而是死路——宿命逻辑的完成，压迫感从第一场到最后一句必须不断升级。\n"
            + "4. 所有角色行为必须符合角色卡设定，不得创造新角色，不得违背世界规则。\n"
            + ("\n" + lang_instr if lang_instr else "")
        )
        be_outline_user = (
            f"{narrative_pov_block}"
            f"{anchor_block}"
            f"{summaries_block}"
            f"## 原著风格参考\n{style_ref}\n\n"
            f"{parent_states_block}"
            f"{pivot_block}"
            f"## Bad End 走向\n{summary}\n\n"
            f"## 任务\n"
            f"请为《{title}》规划 {be_scene_count} 个场次的 Bad End 剧本大纲，总字数目标约 {be_target} 字。\n"
            "每场必须包含：时间/地点、出场人物、核心冲突（具体到人物动作和情绪）、动作脉络（3-5条）、情感落点。\n"
            "整体结构要求：\n"
            "  - 首场：压迫感起点，主角陷入绝境的开端，预示不可逃脱的命运。\n"
            "  - 中场（若有）：主角关键错误抉择 / 恐怖逻辑升级，窒息感持续加强。\n"
            "  - 末场：主角死亡或毁灭性失败，画面必须具体，不得用空洞词语带过。\n"
            "【严禁】出现任何希望、转机、或「其实还有出路」的暗示——这是死路，不是支线。\n\n"
            "输出严格 JSON：\n"
            '{"scenes": [{"scene_no": 1, "time_location": "", "characters": [], '
            '"core_conflict": "", "action_beats": "", "emotional_note": ""}]}'
        )
        try:
            be_outline = client.chat_completion_json(
                system_prompt=be_outline_system,
                user_prompt=be_outline_user,
                temperature=0.55,
                max_tokens=1500,
            )
            be_scenes = be_outline if isinstance(be_outline, list) else be_outline.get("scenes", [])
            if not be_scenes:
                raise ValueError("Bad End 大纲为空")
            print(f"   ✅ {title} Bad End 大纲完成，共{len(be_scenes)}场")
        except Exception as e:
            print(f"   ⚠️  {title} Bad End 大纲失败（{e}），使用骨架兜底")
            be_scenes = [
                {
                    "scene_no": 1, "time_location": "（根据前情推导）", "characters": [],
                    "core_conflict": f"压迫感起点：{summary}", "action_beats": "主角陷入绝境，无路可退",
                    "emotional_note": "恐惧、绝望",
                },
                {
                    "scene_no": 2, "time_location": "（根据前情推导）", "characters": [],
                    "core_conflict": "主角死亡或毁灭性失败", "action_beats": "宿命的终点",
                    "emotional_note": "寂灭",
                },
            ]

        be_write_system = (
            "你是恐怖/悬疑视觉小说作家，正在撰写一个 Bad End（坏结局）章节。\n"
            + narrative_pov_block
            + "写作铁律：\n"
            + "1. 每场严格 1500-2000 字，节奏压抑——短句、停顿、感官细节（冰冷的触感、血腥气、黑暗中的喘息声）。\n"
            + "2. 绝对不写希望，不给出路，不暗示主角可以得救——每一句都是压力的叠加。\n"
            + "3. 主角死亡画面必须具体：身体感知、最后一个清醒的念头、意识消散的过程，三者缺一不可。\n"
            + "4. 对话要简短、破碎——恐惧让人说话不完整，句子之间可以只剩沉默。\n"
            + "5. 不写场景标题，不写总结，直接输出正文，以压迫感画面或最后一个念头戛然而止。\n"
            + "6. 严格遵守角色卡设定，不得创造新角色。"
            + ("\n" + lang_instr if lang_instr else "")
        )
        be_final_content = ""
        be_prev_context = pivot_chapter_text.strip() if pivot_chapter_text else pivot_summary
        for i, be_scene in enumerate(be_scenes):
            is_death_scene = i == len(be_scenes) - 1
            be_write_user = (
                f"{narrative_pov_block}"
                f"{anchor_block}"
                f"{parent_states_block}"
                f"## 原著风格参考\n{style_ref}\n\n"
                f"## 前情提要（必须自然衔接，不要复述）\n{be_prev_context}\n\n"
                f"## 本场次拍摄指南\n"
                f"时间/地点：{be_scene.get('time_location', '')}\n"
                f"出场人物：{', '.join(be_scene.get('characters', []))}\n"
                f"核心冲突：{be_scene.get('core_conflict', '')}\n"
                f"动作脉络：{be_scene.get('action_beats', '')}\n"
                f"情感落点：{be_scene.get('emotional_note', '')}\n\n"
                f"## 写作要求\n"
                "全力撰写本场正文，1500-2000字。节奏压抑，画面具体，绝不给出路。\n"
                + (
                    "【这是最后一场·死亡必须发生】主角在此场死亡或遭遇毁灭性失败，"
                    "写清楚死亡画面（身体感知 + 最后一个念头 + 意识消散），"
                    "以一个令人窒息的画面或最后一个破碎念头作为全章收束，不要给出任何余地。"
                    if is_death_scene else
                    "不要写结尾总结，只写透当下压迫感切片，保持窒息节奏，让下一场自然接续。"
                )
            )
            be_scene_text = ""
            for attempt in range(2):
                retry_note = (
                    f"\n\n⚠️ 上次输出仅 {len(be_scene_text)} 字，低于 1500 字目标。"
                    "请重新撰写，必须达到字数要求，禁止跳跃叙述。"
                    if attempt > 0 and be_scene_text else ""
                )
                try:
                    candidate = client.chat_completion(
                        system_prompt=be_write_system,
                        user_prompt=be_write_user + retry_note,
                        temperature=0.85,
                        max_tokens=4000,
                    ).strip()
                except Exception as e:
                    print(f"   ⚠️  {title} Bad End 第{i+1}场失败（attempt={attempt+1}, {e}）")
                    continue
                if len(candidate) >= 1200:
                    be_scene_text = candidate
                    print(f"   ✅ {title} Bad End 第{i+1}/{len(be_scenes)}场完成（{len(be_scene_text)}字, attempt={attempt+1}）")
                    break
                be_scene_text = candidate
                print(f"   🔁 {title} Bad End 第{i+1}场字数不足（{len(candidate)}字），retry...")
            if not be_scene_text:
                raise RuntimeError(
                    f"{title} Bad End 第{i+1}场两次生成均失败，禁止写入占位正文"
                )
            be_final_content += be_scene_text + "\n\n"
            be_prev_context = be_scene_text

        result_text = be_final_content.strip()
        if result_text and char_cards:
            from services.story_planner.core.novel_reader import repair_content_consistency_or_raise
            result_text = repair_content_consistency_or_raise(
                result_text,
                title,
                char_cards,
                character_behavior_anchors,
                provider,
                output_language,
            )
        return result_text
    # ── Bad End 专用路径结束 ─────────────────────────────────────────

    outline_user = (
        f"{narrative_pov_block}"
        f"{anchor_block}"
        f"{romance_lookup_text}"
        f"{summaries_block}"
        f"## 原著风格参考\n{style_ref}\n\n"
        f"## 岔路口前情摘要\n{pivot_context}"
        f"{convergence_summary_block}"
        f"{fixed_facts_block}"
        f"{parent_states_block}"
        f"{sibling_states_block}"
        f"{merge_contract_block}"
        f"{pivot_block}"
        f"{sibling_block}"
        f"{convergence_block}"
        f"## What-If 走向\n{summary}\n\n"
        f"## 任务\n"
        f"请为《{title}》设计{scene_count}个场次（Scene）的详细剧本大纲，每场承载约{chars_per_scene}字内容。\n"
        f"总字数目标：{target_chars}字（与原著章节等长）。\n"
        "每个场次必须有独立的时间/地点/冲突，不得重复，场次之间要有明确的叙事推进。\n"
        "【硬约束】\n"
        "  ① 第一场必须紧接【硬起点·最后一句】之后发生，不得回放、不得改写已发生事件；\n"
        "  ② 末场结尾必须能让主角的情绪/位置/同行人物自然过渡到【硬终点·第一句】之前；\n"
        "  ③ 全部场次不得违反【既定事实清单】中的任何一条；\n"
        "  ④ 人物决定、互动过程和情感体验必须与【平级原著兄弟章节】明显不同；永久生死、核心身份和外层世界线不得破坏共同下游；\n"
        "  ⑤ 角色状态必须遵循上方【What-If 起点】与【共同下游永久硬状态】——"
        "起点状态是第一场的事实基础；末场的 life_status、alive、reality_domain 和核心 identity_status 必须与共同下游兼容。"
        "dream 与 therapy_reconstruction 若同属 reconstructed_world，不得当作生死或世界线冲突；"
        "时间、地点和局部参与差异可以通过过渡解释，但必须把 required_transitions 写进正文。"
        "仅在日记、回忆、电话中被提及或低置信度的角色，不要强行对齐局部位置和时间。"
        "原著兄弟章末确认存活的现实层活跃角色，本 What-If 不得无铺垫写死；只有 death_confirmed 才是死亡硬约束，"
        "death_reported/death_suspected 不得擅自坐实；确认死亡角色不得在现实层复活，除非契约明确允许且正文触发可验证复活规则。"
        "若剧情已经揭示角色死亡，而共同下游仍以现实层活人身份继续，必须遵循全书统一叙事层基准写出明确过渡；"
        "不得自行在 dream、therapy_reconstruction、rebirth 之间改名。无法合理过渡时，应明确报告无法收束，不得硬接共同下游。\n"
        "输出严格 JSON：\n"
        '{"scenes": [{"scene_no": 1, "time_location": "时间/地点", "characters": ["角色"], "core_conflict": "核心冲突（2-3句话，具体到人物动作和情绪）", "action_beats": "具体动作脉络（5-8条，每条一句话）", "emotional_arc": "本场情绪走向（开头→结尾）"}]}'
    )
    def _generate_scenes() -> list:
        try:
            outline = client.chat_completion_json(
                system_prompt=outline_system,
                user_prompt=outline_user,
                temperature=0.5,
                max_tokens=3000,
            )
            return outline.get("scenes", [])
        except Exception as e:
            print(f"   ⚠️  {title} 大纲生成失败（{e}），使用单场兜底")
            return [{
                "scene_no": 1,
                "time_location": "未知",
                "characters": [],
                "core_conflict": summary,
                "action_beats": summary,
            }]

    scenes = resolve_whatif_scenes(resume_state, _generate_scenes)
    if checkpoint_callback is not None and not (resume_state or {}).get("scenes"):
        checkpoint_callback({
            "scenes": scenes,
            "scene_count": len(scenes),
            "completed_scenes": [],
            "status": "running",
        })

    # Step 2: 循环扩写每场
    # 构建 variable 钩子约束（让正文自然埋下软分支效果的情节依据）
    var_hooks_block = ""
    if variable_defs:
        hooks = [
            f"  - {v.get('key')}（{v.get('name', v.get('key'))}）：{v.get('description', '')}"
            for v in variable_defs
        ]
        var_hooks_block = (
            "\n【人格维度轴钩子要求】本章正文必须为以下变量各创造至少1个自然的情节钩子——"
            "即主角面临与该维度直接相关的抉择或情绪时刻，让后续软分支的数值效果有叙事依据：\n"
            + "\n".join(hooks) + "\n"
        )

    write_system = (
        "你是顶级视觉小说作家，正在撰写一个 What-If 章节。"
        + narrative_pov_block
        + f"你的文字必须：细节具体、对话自然有张力、心理描写深入、节奏沉浸——每场约 {chars_per_scene} 字。"
        + "要求：句子短促有力，多用感官细节（声音/气味/触感），对话后必须跟心理活动，"
        + "动作描写要分解到每一个细微动作，不得跳跃叙述。"
        + "禁止总结、禁止写场景标题，直接输出正文。"
        + var_hooks_block
        + ("\n" + lang_instr if lang_instr else "")
    )

    from services.story_planner.core.chapter_branch_runtime import (
        initialize_whatif_scene_checkpoint,
        join_whatif_scene_content,
        next_whatif_scene_index,
        record_whatif_scene_checkpoint,
    )

    scene_checkpoint = initialize_whatif_scene_checkpoint(
        resume_state,
        scene_count=len(scenes),
    )
    scene_checkpoint["scenes"] = scenes
    resume_index = next_whatif_scene_index(scene_checkpoint)
    final_content = join_whatif_scene_content(scene_checkpoint)
    if final_content:
        final_content += "\n\n"
    # 首场前情：使用岔路章节全文 + 摘要 + 硬起点最后一段，不再截断
    prev_context_parts = []
    if pivot_summary:
        prev_context_parts.append(f"【核心矛盾】{pivot_summary}")
    if pivot_full:
        prev_context_parts.append(f"【岔路口章节末段】\n{pivot_full[-6000:]}")
    if pivot_last_paragraph:
        prev_context_parts.append(
            f"【硬起点·最后一段（你的第一场必须紧接此后发生，不得回放、不得改写）】\n{pivot_last_paragraph}"
        )
    prev_context = "\n\n".join(prev_context_parts).strip()
    if resume_index:
        previous = scene_checkpoint["completed_scenes"][resume_index - 1]
        resumed_context = []
        if previous.get("snapshot"):
            resumed_context.append(f"【上一场叙事状态快照】\n{previous['snapshot']}")
        if previous.get("accumulated_summary"):
            resumed_context.append(
                f"【本章已写完内容的累计摘要】\n{previous['accumulated_summary']}"
            )
        resumed_context.append(f"【上一场全文】\n{previous['content']}")
        prev_context = "\n\n".join(resumed_context)

    last_scene_no = scenes[-1].get("scene_no") if scenes else None
    for scene_index, scene in enumerate(scenes):
        if scene_index < resume_index:
            continue
        is_last_scene = scene.get("scene_no") == last_scene_no
        convergence_hint = ""
        if is_last_scene and convergence_opening.strip():
            tone_line = (
                f"\n情绪锚点：主线下一章开头的主角状态是「{convergence_tone}」，"
                "你的结尾必须让主角处于与此相近的情绪/处境，不要求完全一致，但不能截然相反。"
                if convergence_tone else ""
            )
            convergence_summary_line = (
                f"\n【主线收束章节摘要】{convergence_summary}" if convergence_summary else ""
            )
            convergence_hint = (
                f"\n\n## 【关键】最后一场的收束要求（硬终点）\n"
                f"本场是 What-If 章节的最后一场，结束后玩家将直接进入下方主线内容。\n"
                f"你的结尾必须做到：主角的情绪、处境、所处地点，与下方主线开头完全兼容——"
                f"不要直接复制原文；What-If 的影响可以保留，但必须完成所有必要转场并让下游事实成立。\n"
                + tone_line
                + convergence_summary_line
                + (f"\n【硬终点·第一句】\n> 「{convergence_first_sentence}」\n你的结尾最后 200 字必须铺好衔接到这一句的桥。\n"
                   if convergence_first_sentence else "")
                + f"\n【主线收束章节开头（你的结尾必须能接上它）】\n{convergence_opening[:6000]}"
            )
        # 平级原著兄弟章节全文：让每场都看到自己在替代什么剧情，避免漂移
        sibling_write_block = ""
        if sibling_chapter_text:
            sibling_write_block = (
                f"\n## 【★防撞车参考·平级原著兄弟章节】《{sibling_chapter_title or '原著兄弟章'}》\n"
                "（本 What-If 是这一章的【平行替代】。你写的内容必须与下面这一章在【关键事件、人物决定、情感走向】上截然不同，"
                "但下游收束章一致——也就是说，过程不同，殊途同归。）\n"
                f"{sibling_chapter_text[:6000]}\n\n"
            )
        write_user = (
            f"{narrative_pov_block}"
            f"{anchor_block}"
            f"{romance_lookup_text}"
            f"{summaries_block}"
            f"## 原著风格参考\n{style_ref}\n\n"
            f"{parent_states_block}"
            f"{sibling_states_block}"
            f"{merge_contract_block}"
            f"{sibling_write_block}"
            f"## 前情提要（必须自然衔接）\n{prev_context}\n\n"
            f"## 本场次拍摄指南\n"
            f"时间/地点：{scene.get('time_location', '')}\n"
            f"出场人物：{', '.join(scene.get('characters', []))}\n"
            f"核心冲突：{scene.get('core_conflict', '')}\n"
            f"动作脉络：{scene.get('action_beats', '')}\n"
            f"情绪走向：{scene.get('emotional_arc', '')}\n\n"
            f"## 写作要求\n"
            f"请撰写本场戏正文，目标约 {chars_per_scene} 字，控制在 "
            f"{max(300, chars_per_scene - 350)}-{chars_per_scene + 350} 字。"
            "极度细致地展开动作描写、对话和心理活动，节奏要慢，画面要足。"
            "每句对话后必须有心理活动或动作反应，不得连续对话超过3句。"
            "不得新增角色卡中不存在的人物，不得违背世界规则。"
            "本场必须遵循上方状态约束：生死、外层现实域和核心身份不得硬冲突；"
            "出现方式、局部叙事框架、时间和地点变化必须用完整动作链过渡；"
            "确认死亡者不得在现实层无规则复活，传闻/疑似死亡不得擅自坐实，确认存活者不得无铺垫死亡。"
            + ("不要给这场戏写结尾总结，只写透当下切片，让下一场自然接续。" if not is_last_scene else "")
            + convergence_hint
        )
        # 最后一场启用收束 retry：LLM 裁判打分达到 5 分才视为达标，最多 3 次
        if is_last_scene and convergence_opening.strip():
            scene_text = ""
            previous_failed_endings = []
            for attempt in range(3):
                retry_hint = ""
                if previous_failed_endings:
                    retry_hint = (
                        "\n\n⚠️ 上次的结尾与主线下一章衔接得不好（评分过低）。\n"
                        "上次失败的结尾全文（不要重复这个写法）：\n"
                        f"{previous_failed_endings[-1]}\n"
                        "请重写：主角的情绪、处境、所处地点必须与主线下一章开头三者中至少 2 个匹配，"
                        "结尾最后 200 字必须铺好衔接的桥。"
                    )
                attempt_user = write_user + retry_hint
                try:
                    candidate = client.chat_completion(
                        system_prompt=write_system,
                        user_prompt=attempt_user,
                        temperature=0.85,
                        max_tokens=5000,
                    ).strip()
                except Exception as e:
                    print(f"   ⚠️  {title} 最后一场第{attempt+1}次生成失败（{e}）")
                    continue
                transition_review = review_whatif_downstream_transition(
                    whatif_text=candidate,
                    downstream_text=convergence_opening,
                    provider=provider,
                    output_language=output_language,
                    narrative_pov_contract=narrative_pov_contract,
                )
                score = int(transition_review.get("score", 0))
                judge_issues = transition_review.get("issues", [])
                # ``accepted_with_warning`` means the reviewer found no hard
                # contradiction and only a bridgeable difference.  Treat it
                # as success for the local last-scene loop; otherwise a soft
                # warning is incorrectly retried three times and the caller
                # may discard an otherwise usable optional branch.
                transition_accepted = is_acceptable_whatif_transition_review(
                    transition_review
                )
                if transition_accepted:
                    scene_text = candidate
                    print(f"   ✅ {title} 最后一场衔接通过（attempt={attempt+1}, score={score}）")
                    break
                else:
                    print(f"   🔁 {title} 最后一场衔接不达标（attempt={attempt+1}, score={score}），retry...")
                    print(f"      裁判反馈：{judge_issues}")
                    previous_failed_endings.append(candidate)
                    # 把裁判反馈拼进下次 prompt
                    write_user_with_judge = write_user + (
                        f"\n\n📋 上次裁判反馈：{json.dumps(judge_issues, ensure_ascii=False)}"
                        "\n请针对这些扣分点重写收束段，并补齐所有时间、地点和动作链。"
                    )
                    write_user = write_user_with_judge
                    scene_text = candidate  # 留作兜底
        else:
            try:
                scene_text = client.chat_completion(
                    system_prompt=write_system,
                    user_prompt=write_user,
                    temperature=0.85,
                    max_tokens=5000,
                ).strip()
            except Exception as e:
                print(f"   ⚠️  {title} 第{scene.get('scene_no','')}场生成失败（{e}）")
                raise RuntimeError(
                    f"{title} 第{scene.get('scene_no','')}场生成失败，禁止写入占位正文"
                ) from e

        final_content += scene_text + "\n\n"

        # 提取叙事状态快照：让 LLM 压缩本场的主角状态，供下一场使用
        # 比纯文本截断更能保留跨场的人物动机和情绪连续性
        if not is_last_scene:
            try:
                snapshot = client.chat_completion(
                    system_prompt="你是叙事分析师，用极简语言提炼当前叙事状态。",
                    user_prompt=(
                        f"请用100字以内提炼以下场景结束时的叙事状态，格式：\n"
                        "主角情绪：[词语] | 所处位置：[地点] | 已做决定：[1句话] | 未解张力：[1句话]\n\n"
                        f"场景全文：\n{scene_text}"
                    ),
                    temperature=0.2,
                    max_tokens=200,
                ).strip()
            except Exception:
                snapshot = ""
            # 累计摘要：把本章已写完的所有场提炼成一段累计概要（让下一场看到全章已经发生了什么）
            accumulated_summary = ""
            if should_refresh_accumulated_summary(scene_index):
                try:
                    accumulated_summary = client.chat_completion(
                        system_prompt="你是叙事分析师，提炼章节当前已完成内容的累计概要。",
                        user_prompt=(
                            f"以下是本 What-If 章节截至当前已写完的全部内容，请用 200 字以内概括：\n"
                            f"已发生的关键事件、主角情绪轨迹、未解张力。\n\n"
                            f"已写完正文：\n{final_content}"
                        ),
                        temperature=0.2,
                        max_tokens=400,
                    ).strip()
                except Exception:
                    accumulated_summary = ""
            elif scene_checkpoint.get("completed_scenes"):
                accumulated_summary = str(
                    scene_checkpoint["completed_scenes"][-1].get("accumulated_summary") or ""
                )
            prev_context_parts = []
            if snapshot:
                prev_context_parts.append(f"【上一场叙事状态快照】\n{snapshot}")
            if accumulated_summary:
                prev_context_parts.append(f"【本章已写完内容的累计摘要】\n{accumulated_summary}")
            prev_context_parts.append(f"【上一场全文】\n{scene_text}")
            prev_context = "\n\n".join(prev_context_parts)

        record_whatif_scene_checkpoint(
            scene_checkpoint,
            scene_index=scene_index,
            content=scene_text,
            snapshot=snapshot if not is_last_scene else "",
            accumulated_summary=accumulated_summary if not is_last_scene else "",
        )
        if checkpoint_callback is not None:
            checkpoint_callback(scene_checkpoint)

    result_text = final_content.strip()

    # 一致性质量门禁：检查 -> 定点修复 -> 再检查，复检失败则阻止发布。
    if result_text and char_cards:
        from services.story_planner.core.novel_reader import repair_content_consistency_or_raise
        fixed_facts = character_behavior_anchors
        result_text = repair_content_consistency_or_raise(
            result_text,
            title,
            char_cards,
            fixed_facts,
            provider,
            output_language,
        )

    return enforce_whatif_content_budget(
        result_text,
        min_chars=min(8000, target_chars),
        max_chars=target_chars,
        preserve_ending=bool(convergence_opening.strip()),
    )
