"""Offline quality evaluation for chapter_branch artifacts."""

from __future__ import annotations

import ast
from difflib import SequenceMatcher
import re
from typing import Any, Callable

from services.story_planner.core.chapter_branch_runtime import (
    validate_generated_content,
    validate_release_integrity,
    validate_title_completeness,
)
from services.story_planner.core.effects_validator import (
    calculate_actual_effect_reachability,
    calculate_effect_caps,
    sanitize_effect_pair,
    sanitize_romance_effect_pair,
    simulate_ending_route_reachability,
)
from services.story_planner.core.novel_reader import worldview_schema_issues


_EFFECT_PAIR_RE = re.compile(
    r"effects_a=(\{[^}]*\})\s+effects_b=(\{[^}]*\})"
)
_ROMANCE_BRANCH_RE = re.compile(
    r"▶ 选项A：([^\n]+).*?▶ 选项B：([^\n]+).*?"
    r"effects_a=(\{[^}]*\})\s+effects_b=(\{[^}]*\})",
    re.DOTALL,
)
_SOFT_BRANCH_CHOICE_RE = re.compile(r"▶\s*选项[AB]：([^\n]+)")
_CONTROL_COMMENT_RE = re.compile(r"<!--(.*?)-->", re.DOTALL)
_BRANCH_KIND_RE = re.compile(r"\bbranch_kind=([A-Za-z0-9_-]+)")
_NEGATIVE_CHOICE_MARKERS = (
    "不想", "不愿", "不该", "拒绝", "远离", "避开", "放下", "离开", "离去", "松手",
    "不回应", "不追", "不见", "告别", "算了", "保持距离", "退回朋友", "不能贪", "不能只", "不能靠近", "别靠近",
)
_LEGACY_CORE_CHAIN_FIELDS = (
    "evidence_exists",
    "protagonist_origin",
    "verifiable",
    "causal_payoff",
)

# Optional soft branches are allowed to be dropped when their generated prose
# cannot be made compatible with the surrounding original text.  Keep this as
# a named release-gate threshold so the policy is explicit in quality reports
# and can be overridden by callers for controlled experiments.  The limit is
# intentionally specific to transition drops; fallback and hard-rule drop
# thresholds below remain unchanged.
DEFAULT_SOFT_BRANCH_TRANSITION_DROP_LIMIT = 0.65
# A score of five is the minimum local What-If transition gate. The task
# retries only the current branch when the score is lower; the release report
# keeps the finding for diagnostics and the aggregate score remains the only
# release-level blocker.
DEFAULT_WHATIF_TRANSITION_MIN_SCORE = 5


def _extract_marker_dict(comment: str, field: str) -> dict:
    marker = f"{field}="
    start = str(comment or "").find(marker)
    if start < 0:
        return {}
    brace_start = str(comment).find("{", start + len(marker))
    if brace_start < 0:
        return {}
    depth = 0
    quote = ""
    escaped = False
    for index in range(brace_start, len(comment)):
        char = comment[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    value = ast.literal_eval(comment[brace_start : index + 1])
                except (SyntaxError, ValueError):
                    return {}
                return value if isinstance(value, dict) else {}
    return {}


def _extract_marker_list(comment: str, field: str) -> list:
    """Safely read a list-valued control-comment field."""
    marker = f"{field}="
    start = str(comment or "").find(marker)
    if start < 0:
        return []
    value_start = start + len(marker)
    tail = str(comment or "")[value_start:].lstrip()
    if not tail.startswith("["):
        return []
    depth = 0
    quote = ""
    escaped = False
    for index, char in enumerate(tail):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                try:
                    value = ast.literal_eval(tail[: index + 1])
                except (SyntaxError, ValueError):
                    return []
                return value if isinstance(value, list) else []
    return []


def _normalize_review_bool(value: Any) -> bool:
    """Coerce an LLM boolean without accepting arbitrary truthy strings."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"true", "1", "yes", "y", "是", "通过"}


def _normalize_review_text(value: Any) -> str:
    """Normalize an evidence quote for grounded substring verification."""
    return re.sub(r"[\s，。、；：！？'\"“”‘’（）()【】<>《》]+", "", str(value or ""))


def _semantic_variable_causality_assessment(
    review: Any,
    content: str,
    variable_key: str,
) -> dict[str, Any] | None:
    """Normalize a model-based causality review for any numeric route.

    The release gate must reason over the narrative chain instead of looking
    for a vocabulary list.  The only deterministic check left here is that a
    quote supplied by the model is grounded in the generated artifact.
    """
    if not isinstance(review, dict):
        return None
    core = review.get("core_chain", {})
    core = core if isinstance(core, dict) else {}
    behavior = _normalize_review_bool(
        core.get(
            "variable_behavior_linked",
            core.get("behavior_linked", core.get("behavior_observable")),
        )
    )
    observable = _normalize_review_bool(
        core.get("observable_payoff", core.get("payoff_observable"))
    )
    causal = _normalize_review_bool(core.get("causal_payoff"))
    route_specific = _normalize_review_bool(
        core.get(
            "route_specific",
            core.get(
                "route_specific_payoff",
                core.get("payoff_specific_to_variable"),
            ),
        )
    )
    target_linked = _normalize_review_bool(
        core.get("target_linked", core.get("target_character_linked"))
    )
    relationship = _normalize_review_bool(core.get("relationship_confirmation"))
    future = _normalize_review_bool(core.get("shared_future"))
    fatal_value = core.get("fatal_state_consistent")
    if fatal_value is None:
        fatal_value = core.get("terminal_state_consistent")
    # The field is part of the semantic-review contract.  Treating an omitted
    # value as ``true`` would allow a truncated/malformed model response to
    # pass the release gate.
    fatal_consistent = _normalize_review_bool(fatal_value)
    evidence = review.get("evidence", {})
    evidence = evidence if isinstance(evidence, dict) else {}
    quote = _normalize_review_text(evidence.get("evidence_quote"))
    normalized_content = _normalize_review_text(content)
    quote_verified = bool(len(quote) >= 8 and quote in normalized_content)

    key = str(variable_key or "").lower()
    if key.startswith("affection_"):
        # Romance routes need an explicit target and relationship/future
        # payoff.  Other axes use route_specific as the corresponding check.
        # Naming the target is not evidence that the player's accumulated
        # behavior reached the ending; both facts must be independently
        # established by the reviewer.
        # Do not let a generic ``route_specific=true`` assertion bypass the
        # romance-specific contract: the ending must explicitly confirm the
        # relationship or establish a shared future with the named target.
        route_specific = target_linked and (relationship or future)

    try:
        confidence = float(review.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    passed = bool(
        _normalize_review_bool(review.get("passed"))
        and confidence >= 0.80
        and behavior
        and observable
        and causal
        and route_specific
        and fatal_consistent
        and quote_verified
    )
    missing = []
    if not behavior:
        missing.append("variable_behavior_linked")
    if not observable:
        missing.append("observable_payoff")
    if not causal:
        missing.append("causal_payoff")
    if not route_specific:
        missing.append(
            "target_linked_and_relationship_or_shared_future"
            if key.startswith("affection_")
            else "route_specific"
        )
    if not fatal_consistent:
        missing.append("fatal_state_consistent")
    if not quote_verified:
        missing.append("grounded_evidence_quote")
    return {
        "passed": passed,
        "confidence": confidence,
        "core_chain": {
            "variable_behavior_linked": behavior,
            "observable_payoff": observable,
            "causal_payoff": causal,
            "route_specific": route_specific,
            "target_linked": target_linked,
            "relationship_confirmation": relationship,
            "shared_future": future,
            "fatal_state_consistent": fatal_consistent,
        },
        "evidence": evidence,
        "missing": missing,
        "reason": str(review.get("reason") or ""),
        "quote_verified": quote_verified,
    }


def validate_ending_variable_causality(
    variable_def: dict[str, Any],
    ending_content: str,
    *,
    semantic_reviewer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    semantic_context: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Check that a high-score persona route produces an aligned, observable payoff."""
    variable = variable_def if isinstance(variable_def, dict) else {}
    content = str(ending_content or "")
    if variable.get("route_enabled") is False:
        return True, "路线已禁用，跳过结局变量因果校验"
    expected = " ".join(
        str(variable.get(field) or "")
        for field in ("name", "description", "high_desc", "ending_hint")
    )
    if not expected.strip():
        return True, ""
    if not content.strip():
        return False, "结局正文为空，无法完成语义因果校验"

    variable_key = str(variable.get("key") or "").lower()
    if semantic_reviewer is not None:
        try:
            raw_review = semantic_reviewer({
                "variable": variable,
                "expected": expected,
                "content": content,
                "context": semantic_context if isinstance(semantic_context, dict) else {},
            })
        except Exception as exc:
            raw_review = {"passed": False, "confidence": 0.0, "reason": f"结局变量语义审查调用失败：{exc}"}
        assessment = _semantic_variable_causality_assessment(
            raw_review,
            content,
            variable_key,
        )
        if assessment and assessment["passed"]:
            return True, ""
        detail = (assessment or {}).get("reason") or "、".join((assessment or {}).get("missing", []))
        return False, "结局变量语义校验未通过" + (f"：{detail}" if detail else "")

    # Offline callers (for example, a diagnostics-only smoke report) do not
    # have a semantic reviewer.  Never infer pass/fail from a fixed vocabulary;
    # mark the result for human review instead.  The production release path
    # always supplies the reviewer above and therefore remains fail-closed.
    if variable_key:
        return True, (
            "该变量未配置大模型语义审查器；已降级为人工审核提示，不阻断发布"
        )
    return True, ""


def _normalize_legacy_ui_mode(ui_mode: str | None) -> str:
    return "Ancient" if str(ui_mode or "").strip().lower() == "ancient" else "Other"


def _coerce_review_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"true", "1", "yes", "y"}


def infer_ending_legacy_flags(
    variable_key: str,
    content: str,
    ui_mode: str | None = None,
) -> dict[str, bool]:
    """Extract route evidence from generated prose without relying on one exact phrase."""
    key = str(variable_key or "").lower()
    text = str(content or "")
    mode = _normalize_legacy_ui_mode(ui_mode)
    if key == "courage":
        signed_markers = ["署名信", "亲笔信", "签名信"]
        if mode == "Ancient":
            signed_markers.extend(["血书", "求援信", "告发状", "状纸", "亲笔书信"])
        return {
            "signed_letter": any(marker in text for marker in signed_markers),
            "recording": "录音" in text,
            "treatment_request": any(marker in text for marker in ("治疗请求", "求医", "接受治疗")),
        }
    if key == "self_worth":
        clarification_markers = [
            "澄清身世", "澄清流言", "亲笔说明", "说明信", "允许公开",
            "不是她的错", "不该由她承担", "不应成为同学议论",
        ]
        record_markers = [
            "校方证词", "校方证明", "校方记录", "学校处理记录", "档案章",
            "谈话记录", "教师便条", "老师便条", "档案室",
        ]
        if mode == "Ancient":
            clarification_markers.extend(
                ["自己的名字", "署下", "自立", "拒绝", "堂堂正正", "洗清", "清白"]
            )
            record_markers.extend(
                ["契书", "契约", "字据", "账本", "族谱", "家谱", "户籍", "地契", "县衙卷宗"]
            )
        return {
            "signed_clarification": any(marker in text for marker in clarification_markers),
            "school_record_preserved": any(marker in text for marker in record_markers),
        }
    if key == "honesty":
        return {
            "illness_disclosed": any(
                marker in text for marker in ("病历", "病情", "病逝", "治疗", "药费单")
            ),
            "truth_record_preserved": any(
                marker in text for marker in ("亲笔", "遗信", "回信", "记录", "扫描件", "复印件")
            ),
        }
    return {}


def _structured_legacy_assessment(
    variable_key: str,
    legacy_evidence: list[dict[str, Any]] | dict[str, Any] | None,
) -> dict[str, Any] | None:
    if isinstance(legacy_evidence, dict):
        candidates = [legacy_evidence]
    elif isinstance(legacy_evidence, list):
        candidates = [item for item in legacy_evidence if isinstance(item, dict)]
    else:
        candidates = []
    if not candidates:
        return None

    key = str(variable_key or "").lower()
    best: dict[str, Any] | None = None
    for evidence in candidates:
        evidence_variable = str(evidence.get("variable") or key).lower()
        if evidence_variable and evidence_variable != key:
            continue
        checks = {
            "evidence_exists": bool(
                evidence.get("artifact_name")
                or evidence.get("evidence_type")
                or evidence.get("evidence_quote")
            ),
            "protagonist_origin": bool(
                (evidence.get("creator") or evidence.get("source_character"))
                and (evidence.get("action") or evidence.get("choice_origin"))
            ),
            "verifiable": bool(
                evidence.get("verifier") and evidence.get("verification_method")
            ),
            "causal_payoff": bool(evidence.get("story_effect")),
        }
        missing = [
            {
                "evidence_exists": "evidence",
                "protagonist_origin": "protagonist_origin",
                "verifiable": "verification",
                "causal_payoff": "story_effect",
            }[field]
            for field in _LEGACY_CORE_CHAIN_FIELDS
            if not checks[field]
        ]
        result = {
            "passed": not missing,
            "status": "passed" if not missing else "repairable",
            "channel": "structured_evidence",
            "core_chain": checks,
            "missing": missing,
            "evidence": evidence,
            "confidence": 1.0 if not missing else 0.0,
        }
        if result["passed"]:
            return result
        if best is None or len(missing) < len(best["missing"]):
            best = result
    return best


def _semantic_legacy_assessment(
    review: dict[str, Any] | None,
    content: str,
) -> dict[str, Any] | None:
    if not isinstance(review, dict) or not review:
        return None
    core_chain = review.get("core_chain", {})
    core_chain = core_chain if isinstance(core_chain, dict) else {}
    checks = {
        field: _coerce_review_bool(core_chain.get(field))
        for field in _LEGACY_CORE_CHAIN_FIELDS
    }
    evidence = review.get("evidence", {})
    evidence = evidence if isinstance(evidence, dict) else {}
    evidence_quote = str(evidence.get("evidence_quote") or "").strip()
    normalized_quote = re.sub(r"[\s，。；：、！？!?'\"“”‘’（）()]+", "", evidence_quote)
    normalized_content = re.sub(
        r"[\s，。；：、！？!?'\"“”‘’（）()]+",
        "",
        str(content or ""),
    )
    quote_verified = bool(
        len(normalized_quote) >= 4 and normalized_quote in normalized_content
    )
    try:
        confidence = float(review.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    passed = (
        _coerce_review_bool(review.get("passed"))
        and confidence >= 0.8
        and all(checks.values())
        and quote_verified
    )
    missing = [field for field, value in checks.items() if not value]
    if not quote_verified:
        missing.append("evidence_quote")
    return {
        "passed": passed,
        "status": (
            "passed"
            if passed
            else "repairable"
            if quote_verified
            and confidence >= 0.55
            and sum(1 for value in checks.values() if value) >= 2
            else "failed"
        ),
        "channel": "semantic_review",
        "core_chain": checks,
        "missing": missing,
        "evidence": evidence,
        "confidence": confidence,
        "reason": str(review.get("reason") or ""),
    }


def assess_ending_variable_legacy(
    variable_key: str,
    content: str,
    *,
    ui_mode: str | None = None,
    legacy_flags: dict[str, Any] | None = None,
    legacy_evidence: list[dict[str, Any]] | dict[str, Any] | None = None,
    semantic_review: dict[str, Any] | None = None,
    semantic_reviewer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    semantic_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate one route with semantic review as the production authority.

    Structured evidence and historical reviews are retained for diagnostics,
    but a production reviewer always re-evaluates the current prose.  Fixed
    vocabulary is never an automatic pass/fail channel.
    """
    structured = _structured_legacy_assessment(variable_key, legacy_evidence)

    if semantic_reviewer is not None:
        payload = {
            "variable": str(variable_key or "").lower(),
            "ui_mode": _normalize_legacy_ui_mode(ui_mode),
            "content": str(content or ""),
            # Keep prior results for audit/debugging only.  The active
            # reviewer must re-evaluate the current artifact and cannot reuse
            # a stale positive decision after the prose changed.
            "previous_semantic_review": semantic_review if isinstance(semantic_review, dict) else {},
            "context": semantic_context if isinstance(semantic_context, dict) else {},
        }
        try:
            raw_review = semantic_reviewer(payload)
        except Exception as exc:
            raw_review = {
                "passed": False,
                "confidence": 0.0,
                "reason": f"语义复核失败：{exc}",
                "core_chain": {},
                "evidence": {},
            }
        reviewed = _semantic_legacy_assessment(raw_review, content)
        if reviewed is not None:
            return reviewed
        return {
            "passed": False,
            "status": "failed",
            "channel": "semantic_review",
            "core_chain": {},
            "missing": list(_LEGACY_CORE_CHAIN_FIELDS) + ["evidence_quote"],
            "evidence": {},
            "confidence": 0.0,
            "reason": "语义复核返回格式无效",
        }

    reviewed = _semantic_legacy_assessment(semantic_review, content)
    if reviewed and reviewed["passed"]:
        return reviewed

    if structured is not None:
        return structured
    if reviewed is not None:
        return reviewed
    # Without an active reviewer this function is used by local diagnostics.
    # Do not turn a vocabulary hit into an automatic release decision: wording
    # differs across genres and keyword rules are inherently prone to false
    # positives.  The production release path always injects the GPT reviewer
    # above; offline callers receive an explicit manual-review state.
    return {
        "passed": True,
        "status": "review_required",
        "channel": "manual_review",
        "core_chain": {},
        "missing": list(_LEGACY_CORE_CHAIN_FIELDS),
        "evidence": {},
        "confidence": 0.0,
        "reason": "未配置大模型语义审查器；关键词规则仅保留为历史审计信息，不作为自动通过依据",
    }


def _ending_has_variable_legacy(
    variable_key: str,
    content: str,
    legacy_flags: dict[str, Any] | None = None,
    *,
    ui_mode: str | None = None,
    legacy_evidence: list[dict[str, Any]] | dict[str, Any] | None = None,
    semantic_review: dict[str, Any] | None = None,
) -> bool:
    """Check that personality-driven endings contain route-specific evidence."""
    key = str(variable_key or "").lower()
    if key not in {"courage", "self_worth"}:
        return True
    return bool(
        assess_ending_variable_legacy(
            key,
            content,
            ui_mode=ui_mode,
            legacy_flags=legacy_flags,
            legacy_evidence=legacy_evidence,
            semantic_review=semantic_review,
        )["passed"]
    )


def _text_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left[:12000], right[:12000]).ratio()


def _check_dag(nodes: dict[str, Any], root_id: str) -> list[str]:
    errors: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visiting:
            errors.append(f"DAG 存在循环：{node_id}")
            return
        if node_id in visited or node_id not in nodes:
            return
        visiting.add(node_id)
        for child in nodes[node_id].get("children", []):
            if isinstance(child, dict) and child.get("child_id"):
                visit(str(child["child_id"]))
        visiting.remove(node_id)
        visited.add(node_id)

    visit(root_id)
    unreachable = sorted(set(nodes) - visited)
    if unreachable:
        errors.append(f"DAG 存在不可达节点：{unreachable}")
    return errors


def _select_story_tree_locale(story_tree: dict[str, Any]) -> dict[str, Any]:
    """Unwrap the multilingual story-tree bundle used by publication output.

    Runtime publication files keep one canonical tree per locale (for example
    ``zh-CN``/``en-US``) alongside a small ``publication`` descriptor.  The
    quality evaluator operates on one tree at a time.  Publication consumers
    default to English, and ``publication`` is fencing metadata only; it must
    never change which player-facing locale is evaluated.
    """
    if not isinstance(story_tree, dict):
        return {}
    if isinstance(story_tree.get("nodes"), dict):
        return story_tree

    preferred: list[str] = ["en-US"]
    seen: set[str] = set()
    candidates: list[tuple[str, dict[str, Any]]] = []
    for locale in preferred:
        if locale in seen:
            continue
        seen.add(locale)
        candidate = story_tree.get(locale)
        if isinstance(candidate, dict) and isinstance(candidate.get("nodes"), dict):
            candidates.append((locale, candidate))
    if candidates:
        return candidates[0][1]
    for locale, candidate in story_tree.items():
        if locale == "publication" or locale in seen:
            continue
        if isinstance(candidate, dict) and isinstance(candidate.get("nodes"), dict):
            return candidate
    return story_tree


def append_quality_warnings(
    quality_report: dict[str, Any],
    extra_warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Append post-gate diagnostics and recompute the score-only decision.

    Some checks (notably multilingual bundle validation) run after the main
    quality evaluation but before publication.  They must use the exact same
    release policy as the initial gate: every finding is advisory and the only
    blocker is a final score below 30.  The report is updated in place and
    returned for convenient call-site chaining.
    """
    if not isinstance(quality_report, dict):
        quality_report = {}
    warnings = quality_report.setdefault("warnings", [])
    if not isinstance(warnings, list):
        warnings = []
        quality_report["warnings"] = warnings
    if extra_warnings:
        warnings.extend(
            str(item).strip()
            for item in extra_warnings
            if str(item).strip()
        )
    score = max(0, 100 - len(warnings) * 3)
    quality_report["score"] = score
    quality_report["blocking_policy"] = "min_score_only"
    quality_report["blocking_threshold"] = 30
    thresholds = quality_report.get("thresholds")
    if not isinstance(thresholds, dict):
        thresholds = {}
    thresholds = dict(thresholds)
    thresholds["min_score"] = 30
    quality_report["thresholds"] = thresholds
    quality_report["passed"] = score >= 30
    quality_report["errors"] = (
        []
        if quality_report["passed"]
        else [f"质量评分 {score} 低于发布阈值 30"]
    )
    return quality_report


def _soft_branch_metric_snapshots(
    story_tree: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the current-attempt and cumulative metric snapshots.

    Resumed chapter-branch runs keep both snapshots under ``metrics``.  Older
    artifacts only have one flat bucket, which is treated as both snapshots so
    the report remains backwards compatible.
    """
    metrics = story_tree.get("metrics", {}) if isinstance(story_tree, dict) else {}
    metrics = metrics if isinstance(metrics, dict) else {}
    cumulative = metrics.get("cumulative_metrics")
    cumulative = cumulative if isinstance(cumulative, dict) else metrics
    attempt = metrics.get("attempt_metrics")
    attempt = attempt if isinstance(attempt, dict) else metrics
    return attempt, cumulative


def _metric_bucket(metrics: dict[str, Any]) -> dict[str, Any]:
    bucket = metrics.get("soft_branches", {}) if isinstance(metrics, dict) else {}
    return bucket if isinstance(bucket, dict) else {}


def _non_negative_metric(bucket: dict[str, Any], key: str) -> int:
    try:
        return max(int(bucket.get(key, 0) or 0), 0)
    except (TypeError, ValueError):
        return 0


def evaluate_artifacts(
    story_tree: dict[str, Any],
    md_contents: dict[str, str],
    min_generated_chars: int = 300,
    worldview: dict[str, Any] | None = None,
    legacy_semantic_reviewer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    romance_semantic_reviewer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ending_semantic_reviewer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate publish integrity, prose quality, effects, state and route diversity.

    ``ending_semantic_reviewer`` is the production reviewer for every
    variable-driven ending.  ``romance_semantic_reviewer`` remains as a
    backwards-compatible alias for callers upgraded in stages.
    """
    story_tree = _select_story_tree_locale(story_tree)
    errors: list[str] = []
    warnings: list[str] = []
    nodes = story_tree.get("nodes", {}) if isinstance(story_tree, dict) else {}
    nodes = nodes if isinstance(nodes, dict) else {}
    attempt_metrics, cumulative_metrics = _soft_branch_metric_snapshots(story_tree)
    # Release statistics describe the whole task, including nodes restored from
    # checkpoints.  Keep the current attempt separately for observability.
    runtime_metrics = cumulative_metrics
    soft_branch_metrics = _metric_bucket(cumulative_metrics)
    attempt_soft_branch_metrics = _metric_bucket(attempt_metrics)

    release_ok, release_errors = validate_release_integrity(
        story_tree,
        saved_md_ids=md_contents.keys(),
    )
    if not release_ok:
        errors.extend(release_errors)
    errors.extend(_check_dag(nodes, str(story_tree.get("root_id", ""))))

    effect_pairs: list[tuple[str, dict, dict, bool]] = []
    generated_nodes: list[str] = []
    original_nodes: list[str] = []
    clue_flag_count = 0
    decisive_clue_count = 0
    invalid_clue_flag_count = 0
    flavor_effect_count = 0
    completeness_errors: list[str] = []
    persisted_validation_warnings: list[str] = []
    for node_id, node in nodes.items():
        content = md_contents.get(node_id, "")
        is_original = bool(node.get("is_original", False)) if isinstance(node, dict) else False
        if isinstance(node, dict):
            metadata = node.get("director_metadata", {})
            metadata = metadata if isinstance(metadata, dict) else {}
            warning_values = list(node.get("validation_warnings") or [])
            warning_values.extend(metadata.get("validation_warnings") or [])
            for warning in warning_values:
                warning_text = str(warning or "").strip()
                if warning_text:
                    persisted_validation_warnings.append(
                        f"节点 {node_id}：{warning_text}"
                    )
        title_ok, title_reason = validate_title_completeness(
            node.get("title", "") if isinstance(node, dict) else ""
        )
        if not title_ok:
            completeness_errors.append(f"节点 {node_id} {title_reason}")
        if not is_original:
            generated_nodes.append(node_id)
            ok, reason = validate_generated_content(content, min_generated_chars)
            if not ok:
                completeness_errors.append(f"节点 {node_id} 正文质量失败：{reason}")
        else:
            original_nodes.append(node_id)
            # 原著也禁止最终产物混入系统占位标记，但不强制最低长度。
            ok, reason = validate_generated_content(content, 1)
            if not ok and "占位" in reason:
                errors.append(f"节点 {node_id} 正文包含占位文本：{reason}")

        for match in _EFFECT_PAIR_RE.finditer(content):
            try:
                effects_a = ast.literal_eval(match.group(1))
                effects_b = ast.literal_eval(match.group(2))
            except (SyntaxError, ValueError):
                errors.append(f"节点 {node_id} effects 无法被下游解析")
                continue
            is_flavor_marker = False
            comment_start = content.rfind("<!--", 0, match.start())
            comment_end = content.find("-->", match.end())
            if comment_start >= 0 and comment_end >= 0:
                kind_match = _BRANCH_KIND_RE.search(content[comment_start:comment_end])
                is_flavor_marker = bool(
                    kind_match and kind_match.group(1).lower() == "flavor"
                )
            effect_pairs.append((node_id, effects_a, effects_b, is_flavor_marker))
            if is_flavor_marker:
                flavor_effect_count += 1
        for control_match in _CONTROL_COMMENT_RE.finditer(content):
            comment = control_match.group(1)
            for warning in _extract_marker_list(comment, "validation_warnings"):
                warning_text = str(warning or "").strip()
                if warning_text:
                    persisted_validation_warnings.append(
                        f"节点 {node_id}：{warning_text}"
                    )
            for field in ("clue_flags_a", "clue_flags_b"):
                clue_flags = _extract_marker_dict(comment, field)
                for clue in clue_flags.values():
                    if not isinstance(clue, dict):
                        invalid_clue_flag_count += 1
                        continue
                    clue_flag_count += 1
                    source_type = str(clue.get("source_type") or "")
                    source_excerpt = str(clue.get("source_excerpt") or "").strip()
                    if source_type not in {
                        "source_fact",
                        "safe_observation",
                        "decisive_clue",
                    }:
                        invalid_clue_flag_count += 1
                    elif source_type in {"source_fact", "decisive_clue"} and not source_excerpt:
                        invalid_clue_flag_count += 1
                    if source_type == "decisive_clue":
                        decisive_clue_count += 1
    errors.extend(completeness_errors)
    warnings.extend(dict.fromkeys(persisted_validation_warnings))

    variable_defs = story_tree.get("variable_defs", []) or []
    branch_type = str(story_tree.get("branch_type", "default"))
    allows_empty_effects = branch_type == "modular" and not variable_defs
    route_defs = story_tree.get("branch_thresholds", {}) or {}
    cap_snapshot = story_tree.get("effect_caps", {})
    if isinstance(cap_snapshot, dict) and cap_snapshot:
        caps = {
            str(key): int(value)
            for key, value in cap_snapshot.items()
        }
    else:
        caps = calculate_effect_caps(variable_defs, max(len(effect_pairs), 1))
    fallback_key = next(
        (
            item.get("key")
            for item in variable_defs
            if isinstance(item, dict) and item.get("key")
        ),
        None,
    )
    for node_id, effects_a, effects_b, is_flavor_marker in effect_pairs:
        if (allows_empty_effects or is_flavor_marker) and not effects_a and not effects_b:
            continue
        if branch_type == "romance":
            clean_a, clean_b, issues = sanitize_romance_effect_pair(
                effects_a,
                effects_b,
                variable_defs,
                caps,
                fallback_key=fallback_key,
            )
        else:
            clean_a, clean_b, issues = sanitize_effect_pair(
                effects_a,
                effects_b,
                variable_defs,
                caps,
                fallback_key=fallback_key,
            )
        if issues or clean_a != effects_a or clean_b != effects_b:
            errors.append(
                f"节点 {node_id} effects 不合规：{'；'.join(issues) or '值被规范化'}"
            )

    soft_branches_required = bool(variable_defs) and branch_type not in {
        "death_corridor",
        "fantasy",
    }
    if soft_branches_required and not effect_pairs:
        errors.append("数值驱动模式未生成软分支 effects marker")

    saved_effect_pairs = [
        (effects_a, effects_b) for _, effects_a, effects_b, _ in effect_pairs
    ]
    normalized_reachability = simulate_ending_route_reachability(
        variable_defs,
        saved_effect_pairs,
    )
    legacy_reachability = calculate_actual_effect_reachability(
        variable_defs,
        saved_effect_pairs,
    )
    enabled_route_keys = {
        str(item.get("key"))
        for item in variable_defs
        if isinstance(item, dict)
        and item.get("key")
        and bool(item.get("route_enabled", True))
    }
    normalized_route_keys = {
        str(item.get("key"))
        for item in variable_defs
        if isinstance(item, dict)
        and item.get("key")
        and all(
            field in item
            for field in (
                "ratio_threshold",
                "margin_ratio",
                "minimum_effective_choices",
            )
        )
    }
    unreachable_routes: dict[str, dict[str, Any]] = {}
    for key in enabled_route_keys:
        if key in normalized_route_keys:
            route = normalized_reachability.get(key, {})
            if not route.get("reachable"):
                unreachable_routes[key] = route
                errors.append(
                    f"变量 {key} 不存在满足最终结局规则的真实选择路径："
                    f"{route.get('reason', 'unknown')}"
                )
        else:
            route = legacy_reachability.get(key, {})
            if not route.get("reachable"):
                unreachable_routes[key] = route
                errors.append(
                    f"变量 {key} 真实 effects 不可达：最大 "
                    f"{route.get('max_value')} < 阈值 {route.get('threshold')}"
                )

    romance_alignment_errors: list[str] = []
    if branch_type == "romance":
        romance_name_to_key: dict[str, str] = {}
        for ending_id, route in route_defs.items():
            if not isinstance(route, dict) or not route.get("variable"):
                continue
            ending = nodes.get(ending_id, {})
            title = str(ending.get("title", "")) if isinstance(ending, dict) else ""
            name = re.sub(r"^结局[·・:]?", "", title)
            name = re.sub(r"线$", "", name).strip()
            if name:
                romance_name_to_key[name] = str(route["variable"])
        for node_id, content in md_contents.items():
            for match in _ROMANCE_BRANCH_RE.finditer(content):
                marker_start = content.find("<!--", match.end())
                marker_end = content.find("-->", marker_start + 4) if marker_start >= 0 else -1
                if marker_start >= 0 and marker_end >= 0:
                    kind_match = _BRANCH_KIND_RE.search(content[marker_start:marker_end])
                    if kind_match and kind_match.group(1).lower() == "flavor":
                        continue
                for label, title, raw_effects in (
                    ("A", match.group(1).strip(), match.group(3)),
                    ("B", match.group(2).strip(), match.group(4)),
                ):
                    hits = [name for name in romance_name_to_key if name in title]
                    if len(hits) != 1:
                        continue
                    name = hits[0]
                    key = romance_name_to_key[name]
                    try:
                        effects = ast.literal_eval(raw_effects)
                        value = int((effects or {}).get(key, 0))
                    except (SyntaxError, ValueError, TypeError):
                        value = 0
                    expects_positive = not any(marker in title for marker in _NEGATIVE_CHOICE_MARKERS)
                    aligned = value > 0 if expects_positive else value < 0
                    if not aligned:
                        direction = "正向" if expects_positive else "负向"
                        romance_alignment_errors.append(
                            f"节点 {node_id} 选项{label}标题点名{name}，"
                            f"但 {key} 未体现{direction}效果"
                        )
        errors.extend(romance_alignment_errors)

    ending_legacy_errors: list[str] = []
    ending_legacy_channels: dict[str, int] = {}
    topology_id_by_real_id = {}
    if isinstance(plan, dict) and isinstance(plan.get("nodes"), dict):
        topology_id_by_real_id = {
            str(entry.get("real_id")): str(topology_id)
            for topology_id, entry in plan["nodes"].items()
            if isinstance(entry, dict) and entry.get("real_id")
        }
    ui_mode = _normalize_legacy_ui_mode(story_tree.get("UI_mode"))
    for ending_id, route in route_defs.items():
        if not isinstance(route, dict):
            continue
        variable_key = str(route.get("variable", "")).lower()
        if variable_key not in {"courage", "self_worth"}:
            continue
        ending_node = nodes.get(str(ending_id), {})
        ending_node = ending_node if isinstance(ending_node, dict) else {}
        linked_variable = next(
            (
                item for item in variable_defs
                if isinstance(item, dict) and str(item.get("key") or "").lower() == variable_key
            ),
            None,
        )
        if (
            isinstance(linked_variable, dict)
            and linked_variable.get("route_enabled") is False
        ):
            warnings.append(
                f"结局 {ending_id} 的变量 {variable_key} 路线已禁用，跳过可验证遗产门禁"
            )
            continue
        assessment = assess_ending_variable_legacy(
            variable_key,
            md_contents.get(str(ending_id), ""),
            ui_mode=ui_mode,
            legacy_flags=ending_node.get("legacy_flags", {}),
            legacy_evidence=ending_node.get("legacy_evidence", []),
            semantic_review=ending_node.get("legacy_review", {}),
            semantic_reviewer=legacy_semantic_reviewer,
            semantic_context={
                "ending_id": str(ending_id),
                "topology_node_id": topology_id_by_real_id.get(str(ending_id), ""),
            },
        )
        ending_node["legacy_validation"] = assessment
        channel = str(assessment.get("channel") or "none")
        ending_legacy_channels[channel] = ending_legacy_channels.get(channel, 0) + 1
        if assessment.get("status") == "review_required":
            warnings.append(
                f"结局 {ending_id} 的变量 {variable_key} 未执行大模型语义审查，"
                "仅标记为人工复核提示"
            )
        if assessment.get("passed") and assessment.get("evidence"):
            existing_evidence = ending_node.get("legacy_evidence", [])
            if not isinstance(existing_evidence, list):
                existing_evidence = []
            if not existing_evidence:
                ending_node["legacy_evidence"] = [assessment["evidence"]]
            if channel == "semantic_review":
                ending_node["legacy_review"] = assessment
        if not assessment.get("passed"):
            label = "勇气" if variable_key == "courage" else "自我价值"
            missing = "、".join(str(item) for item in assessment.get("missing", []))
            detail = f"（缺少：{missing}）" if missing else ""
            ending_legacy_errors.append(
                f"结局 {ending_id} 的{label}路线缺少可验证遗产{detail}"
            )
    errors.extend(ending_legacy_errors)

    variable_by_key = {
        str(item.get("key")): item
        for item in variable_defs
        if isinstance(item, dict) and item.get("key")
    }
    ending_causality_errors: list[str] = []
    causality_reviewer = ending_semantic_reviewer or romance_semantic_reviewer
    for ending_id, route in route_defs.items():
        if not isinstance(route, dict):
            continue
        variable_key = str(route.get("variable") or "")
        variable_def = variable_by_key.get(variable_key)
        if not variable_def:
            continue
        ending_node = nodes.get(str(ending_id), {})
        ending_node = ending_node if isinstance(ending_node, dict) else {}
        # Reachability calibration can intentionally disable a numeric route
        # when the source novel has too few effective choices.  Such a route
        # is metadata only; it must not be treated as a publishable ending
        # and must not invoke the semantic causality gate.  Otherwise a short
        # novel can fail release on an ending the player cannot ever reach.
        if (
            variable_def.get("route_enabled") is False
            or route.get("route_enabled") is False
            or ending_node.get("route_enabled") is False
        ):
            warnings.append(
                f"缁撳眬 {ending_id} 鐨勫彉閲?{variable_key} 路线已禁用，跳过因果门禁"
            )
            continue
        causality_ok, causality_reason = validate_ending_variable_causality(
            variable_def,
            md_contents.get(str(ending_id), ""),
            semantic_reviewer=causality_reviewer,
            semantic_context={
                "ending_id": str(ending_id),
                "topology_node_id": topology_id_by_real_id.get(str(ending_id), ""),
                "ending_node": ending_node,
                "route": route,
            },
        )
        if not causality_ok:
            ending_causality_errors.append(
                f"结局 {ending_id} 与变量 {variable_key} 因果不一致：{causality_reason}"
            )
        elif causality_reason:
            warnings.append(
                f"结局 {ending_id} 与变量 {variable_key} 因果待人工抽检：{causality_reason}"
            )
    errors.extend(ending_causality_errors)

    for node_id, node in nodes.items():
        if not isinstance(node, dict):
            continue
        child_states = node.get("terminal_states", {}) or {}
        for parent_id in node.get("parent_ids", []) or []:
            parent = nodes.get(parent_id, {})
            parent_states = parent.get("terminal_states", {}) if isinstance(parent, dict) else {}
            for name, parent_state in parent_states.items():
                child_state = child_states.get(name, {}) if isinstance(child_states, dict) else {}
                if (
                    isinstance(parent_state, dict)
                    and parent_state.get("alive") is False
                    and isinstance(child_state, dict)
                    and child_state.get("alive") is True
                ):
                    errors.append(f"节点 {node_id} 中角色{name}出现死亡后复活")

    # What-If 与原著兄弟：同父且同 children 的平行节点不应高度复刻。
    node_items = list(nodes.items())
    for node_id, node in node_items:
        if not isinstance(node, dict) or node.get("is_original", True):
            continue
        if not node.get("children"):
            continue
        for sibling_id, sibling in node_items:
            if not isinstance(sibling, dict) or not sibling.get("is_original"):
                continue
            if (
                set(node.get("parent_ids", []) or [])
                == set(sibling.get("parent_ids", []) or [])
                and {
                    item.get("child_id") for item in node.get("children", []) if isinstance(item, dict)
                }
                == {
                    item.get("child_id") for item in sibling.get("children", []) if isinstance(item, dict)
                }
            ):
                ratio = _text_similarity(md_contents.get(node_id, ""), md_contents.get(sibling_id, ""))
                if ratio >= 0.85:
                    errors.append(
                        f"What-If 节点 {node_id} 与原著兄弟 {sibling_id} 内容过于相似（{ratio:.2f}）"
                    )

    ending_nodes = [
        (node_id, node)
        for node_id, node in node_items
        if isinstance(node, dict)
        and not node.get("children")
    ]
    generated_endings = [
        (node_id, node)
        for node_id, node in ending_nodes
        if not node.get("is_original", True)
    ]
    for index, (left_id, left_node) in enumerate(generated_endings):
        for right_id, right_node in generated_endings[index + 1 :]:
            summary_equal = (
                str(left_node.get("summary", "")).strip()
                == str(right_node.get("summary", "")).strip()
            )
            ratio = _text_similarity(md_contents.get(left_id, ""), md_contents.get(right_id, ""))
            if summary_equal or ratio >= 0.9:
                errors.append(f"平行结局 {left_id}/{right_id} 区分度不足（相似度 {ratio:.2f}）")

    if "metrics" not in story_tree:
        warnings.append("story_tree 未包含运行 metrics")

    if invalid_clue_flag_count:
        errors.append(
            f"软分支存在 {invalid_clue_flag_count} 条缺少来源的 clue_flags"
        )

    modular_choice_texts: list[str] = []
    if branch_type == "modular":
        for content in md_contents.values():
            modular_choice_texts.extend(
                match.group(1).strip()
                for match in _SOFT_BRANCH_CHOICE_RE.finditer(str(content or ""))
            )
    modular_view_ask_count = sum(
        text.startswith(("查看", "询问")) for text in modular_choice_texts
    )
    modular_view_ask_ratio = (
        modular_view_ask_count / len(modular_choice_texts)
        if modular_choice_texts
        else 0.0
    )
    modular_button_errors: list[str] = []
    if branch_type == "modular" and modular_view_ask_ratio > 0.5:
        modular_button_errors.append(
            f"按钮模板集中度过高：查看/询问占比 {modular_view_ask_ratio:.1%}"
        )
    elif branch_type == "modular" and modular_view_ask_ratio > 0.35:
        warnings.append(
            f"按钮模板集中度偏高：查看/询问占比 {modular_view_ask_ratio:.1%}"
        )
    errors.extend(modular_button_errors)

    worldview_issues = worldview_schema_issues(worldview) if worldview is not None else []
    errors.extend(f"世界观 schema 不完整：{issue}" for issue in worldview_issues)

    final_unique_soft_branch_count = len(effect_pairs)
    attempt_soft_branch_processed_count = _non_negative_metric(
        attempt_soft_branch_metrics,
        "total",
    )
    attempt_soft_branch_generated_count = _non_negative_metric(
        attempt_soft_branch_metrics,
        "generated_branch_count",
    )
    cumulative_soft_branch_processed_count = _non_negative_metric(
        soft_branch_metrics,
        "total",
    )
    cumulative_soft_branch_generated_count = _non_negative_metric(
        soft_branch_metrics,
        "generated_branch_count",
    )

    report = {
        # ``evaluate_artifacts`` is also used directly by smoke diagnostics.
        # Keep the same score-only release policy as the full gate.
        "passed": True,
        "errors": errors,
        "warnings": warnings,
        "stats": {
            "node_count": len(nodes),
            "original_node_count": len(original_nodes),
            "generated_node_count": len(generated_nodes),
            # These two fields describe the final artifact, never retry
            # attempts.  The explicit alias makes the output self-documenting.
            "soft_branch_count": final_unique_soft_branch_count,
            "soft_branch_unique_output_count": final_unique_soft_branch_count,
            "soft_branch_generated_count": final_unique_soft_branch_count,
            "soft_branch_attempt_processed_count": attempt_soft_branch_processed_count,
            "soft_branch_attempt_generated_count": attempt_soft_branch_generated_count,
            "soft_branch_cumulative_processed_count": cumulative_soft_branch_processed_count,
            "soft_branch_cumulative_generated_count": cumulative_soft_branch_generated_count,
            "numeric_soft_branch_count": max(final_unique_soft_branch_count - flavor_effect_count, 0),
            "flavor_soft_branch_count": flavor_effect_count,
            "ending_count": len(ending_nodes),
            "generated_ending_count": len(generated_endings),
            "ending_legacy_channels": ending_legacy_channels,
            "clue_flag_count": clue_flag_count,
            "decisive_clue_count": decisive_clue_count,
            "invalid_clue_flag_count": invalid_clue_flag_count,
            "modular_button_count": len(modular_choice_texts),
            "modular_view_ask_button_count": modular_view_ask_count,
            "modular_view_ask_button_ratio": round(modular_view_ask_ratio, 4),
            "soft_branch_theoretical_gap_count": int(soft_branch_metrics.get("theoretical_gap_count", 0) or 0),
            "soft_branch_eligible_gap_count": int(soft_branch_metrics.get("eligible_gap_count", 0) or 0),
            "soft_branch_skipped_no_route_target_count": int(soft_branch_metrics.get("skipped_no_route_target", 0) or 0),
            "soft_branch_flavor_count": int(soft_branch_metrics.get("flavor_branch_count", 0) or 0),
            "soft_branch_attempt_theoretical_gap_count": _non_negative_metric(
                attempt_soft_branch_metrics, "theoretical_gap_count"
            ),
            "soft_branch_attempt_eligible_gap_count": _non_negative_metric(
                attempt_soft_branch_metrics, "eligible_gap_count"
            ),
            "soft_branch_attempt_validation_drop_count": _non_negative_metric(
                attempt_soft_branch_metrics, "validation_dropped"
            ),
            "soft_branch_cumulative_theoretical_gap_count": _non_negative_metric(
                soft_branch_metrics, "theoretical_gap_count"
            ),
            "soft_branch_cumulative_eligible_gap_count": _non_negative_metric(
                soft_branch_metrics, "eligible_gap_count"
            ),
            "soft_branch_cumulative_validation_drop_count": _non_negative_metric(
                soft_branch_metrics, "validation_dropped"
            ),
            "soft_branch_validation_drop_count": int(soft_branch_metrics.get("validation_dropped", 0) or 0),
            "soft_branch_deleted_count": int(
                soft_branch_metrics.get("deleted_branch_count", soft_branch_metrics.get("validation_dropped", 0)) or 0
            ),
            "persisted_validation_warning_count": len(set(persisted_validation_warnings)),
        },
        "checks": {
            "release_integrity": release_ok,
            "dag": not any("DAG" in error for error in errors),
            "content": not any("正文" in error or "占位" in error for error in errors),
            "content_completeness": not completeness_errors,
            "effects": not any("effects" in error for error in errors),
            "soft_branches": not soft_branches_required or bool(effect_pairs),
            "route_reachability": not unreachable_routes,
            "romance_effect_alignment": not romance_alignment_errors,
            "ending_variable_legacy": not ending_legacy_errors,
            "ending_variable_causality": not ending_causality_errors,
            "terminal_states": not any("复活" in error for error in errors),
            "route_diversity": not any("相似" in error or "区分度" in error for error in errors),
            "worldview_schema": not worldview_issues,
            "clue_flags": invalid_clue_flag_count == 0,
            "modular_button_diversity": not modular_button_errors,
        },
    }

    # A smoke evaluation runs after publication and receives the published
    # quality gate from story_tree.json.  Preserve that authoritative score so
    # quality_report.json cannot silently lose the final release result.  For
    # standalone evaluations without a gate, expose a deterministic provisional
    # score for diagnostics.
    quality_gate = story_tree.get("quality_gate") if isinstance(story_tree, dict) else None
    quality_gate = quality_gate if isinstance(quality_gate, dict) else {}
    try:
        published_score = int(quality_gate["score"])
    except (KeyError, TypeError, ValueError):
        published_score = None
    if errors:
        warnings.extend(f"质量告警：{item}" for item in errors)
    errors = []
    report["score"] = (
        max(0, min(100, published_score))
        if published_score is not None
        else max(0, 100 - len(warnings) * 3)
    )
    report["passed"] = report["score"] >= 30
    if report["score"] < 30:
        report["errors"] = [f"质量评分 {report['score']} 低于发布阈值 30"]
    else:
        report["errors"] = []
    report["warnings"] = warnings
    report["blocking_policy"] = "min_score_only"
    report["blocking_threshold"] = 30
    gate_thresholds = quality_gate.get("thresholds")
    report["thresholds"] = dict(gate_thresholds) if isinstance(gate_thresholds, dict) else {}
    return report


def build_release_repair_directive(
    quality_report: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    """Classify release-gate failures into bounded targeted or full repairs."""
    errors = [
        str(error).strip()
        for error in (quality_report or {}).get("errors", [])
        if str(error).strip()
    ]
    actionable_errors = [error for error in errors if not error.startswith("质量评分 ")]
    reason_errors = actionable_errors or errors
    reason = "；".join(reason_errors[:8]) or "发布质量门禁未通过"
    if not errors:
        return {
            "mode": "none",
            "target_node_ids": [],
            "rebuild_topology": False,
            "reason": reason,
        }

    # The aggregate score is the sole release blocker.  It has no safe,
    # node-local target, so scheduling a full release repair would merely
    # replay completed chapters and make a low-scoring run loop.  Leave the
    # diagnostics for operators instead of reopening the pipeline.
    if not actionable_errors:
        return {
            "mode": "none",
            "target_node_ids": [],
            "rebuild_topology": False,
            "reason": reason,
        }

    if any("模式契约冲突" in error for error in actionable_errors):
        return {
            "mode": "none",
            "target_node_ids": [],
            "rebuild_topology": False,
            "reason": reason,
        }

    rebuild_topology_markers = (
        "拓扑使用全量 fallback",
        "拓扑 fallback 比例",
        "What-If 终止比例",
        "原著章节映射",
        "DAG",
    )
    if any(marker in error for error in actionable_errors for marker in rebuild_topology_markers):
        return {
            "mode": "full",
            "target_node_ids": [],
            "rebuild_topology": True,
            "reason": reason,
        }

    full_regeneration_markers = (
        "不存在满足最终结局规则的真实选择路径",
        "真实选择路径仍无法触发",
        "路线不可达",
        "没有可提取的软分支 effects",
        "JSON 节点与 completed 断点不一致",
        "断点计划存在未完成节点",
    )
    if any(marker in error for error in actionable_errors for marker in full_regeneration_markers):
        return {
            "mode": "full",
            "target_node_ids": [],
            "rebuild_topology": False,
            "reason": reason,
        }

    plan_nodes = plan.get("nodes", {}) if isinstance(plan, dict) else {}
    topology = (
        plan.get("topology_result", {}).get("topology", [])
        if isinstance(plan.get("topology_result", {}), dict)
        else []
    )
    ordered_topology_ids = [
        str(item.get("node_id"))
        for item in topology
        if isinstance(item, dict) and item.get("node_id")
    ]
    real_to_topology = {
        str(entry.get("real_id")): str(topology_id)
        for topology_id, entry in plan_nodes.items()
        if isinstance(entry, dict) and entry.get("real_id")
    }
    matched = set()
    for error in actionable_errors:
        for topology_id in plan_nodes:
            if str(topology_id) and re.search(
                rf"(?<![A-Za-z0-9_-]){re.escape(str(topology_id))}(?![A-Za-z0-9_-])",
                error,
            ):
                matched.add(str(topology_id))
        for real_id, topology_id in real_to_topology.items():
            if real_id and re.search(
                rf"(?<![A-Za-z0-9_-]){re.escape(real_id)}(?![A-Za-z0-9_-])",
                error,
            ):
                matched.add(topology_id)

    if matched:
        target_node_ids = [node_id for node_id in ordered_topology_ids if node_id in matched]
        target_node_ids.extend(sorted(matched - set(target_node_ids)))
        return {
            "mode": "targeted",
            "target_node_ids": target_node_ids,
            "rebuild_topology": False,
            "reason": reason,
        }

    # Unlocalized release errors still receive one bounded full prose repair round.
    return {
        "mode": "full",
        "target_node_ids": [],
        "rebuild_topology": False,
        "reason": reason,
    }


def validate_chapter_branch_release(
    story_tree: dict[str, Any],
    md_contents: dict[str, str],
    *,
    plan: dict[str, Any] | None = None,
    worldview: dict[str, Any] | None = None,
    thresholds: dict[str, Any] | None = None,
    extra_warnings: list[str] | None = None,
    legacy_semantic_reviewer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    romance_semantic_reviewer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ending_semantic_reviewer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run the complete release gate and return a structured quality report.

    The ending causality gate is model-semantic when a reviewer is supplied;
    fixed vocabulary is never used as a production pass condition.
    """
    story_tree = _select_story_tree_locale(story_tree)
    configured = {
        "min_generated_chars": 300,
        "min_endings": 1,
        # Quality findings are advisory.  The only release quality blocker is
        # the aggregate score, intentionally kept low enough for a complete
        # but imperfect novel to remain publishable for later review.
        "min_score": 30,
        "soft_branch_transition_drop_limit": DEFAULT_SOFT_BRANCH_TRANSITION_DROP_LIMIT,
        "whatif_transition_min_score": DEFAULT_WHATIF_TRANSITION_MIN_SCORE,
    }
    configured.update(thresholds or {})
    # The release policy intentionally has exactly one blocker: score >= 30.
    # Keep accepting the thresholds argument for backwards-compatible callers,
    # but never allow an old environment variable or caller to restore the
    # retired 75/90/99 point hard gate.
    configured["min_score"] = 30
    configured["whatif_transition_min_score"] = max(
        0,
        min(10, int(configured["whatif_transition_min_score"])),
    )
    report = evaluate_artifacts(
        story_tree,
        md_contents,
        min_generated_chars=max(1, int(configured["min_generated_chars"])),
        worldview=worldview,
        legacy_semantic_reviewer=legacy_semantic_reviewer,
        romance_semantic_reviewer=romance_semantic_reviewer,
        ending_semantic_reviewer=ending_semantic_reviewer,
        plan=plan,
    )
    errors = list(report["errors"])
    warnings = list(report["warnings"])
    if extra_warnings:
        warnings.extend(
            str(item).strip()
            for item in extra_warnings
            if str(item).strip()
        )
    checks = dict(report["checks"])
    nodes = story_tree.get("nodes", {}) if isinstance(story_tree, dict) else {}
    nodes = nodes if isinstance(nodes, dict) else {}

    plan_nodes = plan.get("nodes", {}) if isinstance(plan, dict) else {}
    topology_result = (
        plan.get("topology_result", {}) if isinstance(plan, dict) else {}
    )
    topology = (
        topology_result.get("topology", [])
        if isinstance(topology_result, dict)
        else []
    )
    topology = [item for item in topology if isinstance(item, dict)]
    topology_by_real_id: dict[str, dict[str, Any]] = {}
    for topology_node in topology:
        checkpoint = plan_nodes.get(topology_node.get("node_id"), {})
        real_id = checkpoint.get("real_id") if isinstance(checkpoint, dict) else None
        if real_id:
            topology_by_real_id[str(real_id)] = topology_node

    if not topology_by_real_id:
        for candidate_id, candidate in nodes.items():
            if not isinstance(candidate, dict):
                continue
            children = candidate.get("children", [])
            for child in children if isinstance(children, list) else []:
                if not isinstance(child, dict):
                    continue
                child_id = str(child.get("child_id", ""))
                target = nodes.get(child_id, {})
                if child_id and isinstance(target, dict) and not target.get("children"):
                    topology_by_real_id[child_id] = {"type": "ending"}

    plan_errors: list[str] = []
    if plan is not None:
        incomplete = sorted(
            f"{topo_id}:{checkpoint.get('status', 'missing')}"
            for topo_id, checkpoint in plan_nodes.items()
            if not isinstance(checkpoint, dict)
            or checkpoint.get("status") not in {"completed", "omitted"}
        )
        if incomplete:
            plan_errors.append(f"断点计划存在未完成节点：{incomplete}")
        completed_real_ids = {
            str(checkpoint.get("real_id"))
            for checkpoint in plan_nodes.values()
            if isinstance(checkpoint, dict)
            and checkpoint.get("status") == "completed"
            and checkpoint.get("real_id")
        }
        json_ids = set(nodes)
        if completed_real_ids != json_ids:
            plan_errors.append(
                "JSON 节点与 completed 断点不一致："
                f"json_only={sorted(json_ids - completed_real_ids)}, "
                f"plan_only={sorted(completed_real_ids - json_ids)}"
            )
    errors.extend(plan_errors)
    checks["plan_consistency"] = not plan_errors

    topology_generation = (
        topology_result.get("topology_generation", {})
        if isinstance(topology_result, dict)
        else {}
    )
    topology_fallback_errors: list[str] = []
    if isinstance(topology_generation, dict) and topology_generation:
        if topology_generation.get("full_fallback"):
            topology_fallback_errors.append("拓扑使用全量 fallback，禁止正式发布")
        fallback_ratio = float(topology_generation.get("fallback_ratio") or 0)
        if fallback_ratio > 0.2:
            topology_fallback_errors.append(
                f"拓扑 fallback 比例 {fallback_ratio:.1%} 超过发布上限 20%"
            )
        elif fallback_ratio > 0.1:
            warnings.append(f"拓扑 fallback 比例 {fallback_ratio:.1%}，建议人工复核")
    errors.extend(topology_fallback_errors)
    checks["topology_generation"] = not topology_fallback_errors

    whatif_nodes = [
        item
        for item in topology
        if item.get("type") == "chapter" and not item.get("is_original", True)
        and not item.get("omitted_branch")
    ]
    omitted_whatifs = [
        item
        for item in topology
        if item.get("type") == "chapter"
        and not item.get("is_original", True)
        and item.get("omitted_branch")
    ]
    if omitted_whatifs:
        warnings.append(
            f"已省略 {len(omitted_whatifs)} 个重试后仍不合规的可选 What-If 分支"
        )
    terminal_whatifs = [
        item
        for item in whatif_nodes
        if item.get("terminal_branch")
        or item.get("convergence_status") == "terminal"
    ]
    terminal_ratio = len(terminal_whatifs) / max(len(whatif_nodes), 1)
    convergence_errors: list[str] = []
    if whatif_nodes and terminal_ratio > 0.4:
        convergence_errors.append(
            f"What-If 终止比例 {terminal_ratio:.1%} 超过发布上限 40%"
        )
    elif whatif_nodes and terminal_ratio > 0.3:
        warnings.append(f"What-If 终止比例 {terminal_ratio:.1%}，建议人工复核")
    errors.extend(convergence_errors)
    checks["whatif_convergence"] = not convergence_errors

    runtime_metrics = (
        story_tree.get("metrics", {})
        if isinstance(story_tree, dict) and isinstance(story_tree.get("metrics", {}), dict)
        else {}
    )
    if isinstance(runtime_metrics.get("cumulative_metrics"), dict):
        runtime_metrics = runtime_metrics["cumulative_metrics"]
    soft_branch_metrics = (
        runtime_metrics.get("soft_branches", {})
        if isinstance(runtime_metrics.get("soft_branches", {}), dict)
        else {}
    )
    soft_branch_total = max(int(soft_branch_metrics.get("total", 0) or 0), 0)
    simple_fallback_count = max(
        int(soft_branch_metrics.get("simple_llm_fallback", 0) or 0),
        0,
    )
    hardcoded_fallback_count = max(
        int(soft_branch_metrics.get("hardcoded_fallback", 0) or 0),
        0,
    )
    simple_fallback_ratio = (
        simple_fallback_count / soft_branch_total if soft_branch_total else 0.0
    )
    hardcoded_fallback_ratio = (
        hardcoded_fallback_count / soft_branch_total if soft_branch_total else 0.0
    )
    transition_conflict_count = max(
        int(soft_branch_metrics.get("transition_conflicts", 0) or 0),
        0,
    )
    transition_repair_count = max(
        int(soft_branch_metrics.get("transition_repairs", 0) or 0),
        0,
    )
    transition_repair_success_count = max(
        int(soft_branch_metrics.get("transition_repair_success", 0) or 0),
        0,
    )
    transition_drop_count = max(
        int(soft_branch_metrics.get("transition_dropped", 0) or 0),
        0,
    )
    transition_drop_ratio = (
        transition_drop_count / soft_branch_total if soft_branch_total else 0.0
    )
    validation_drop_count = max(
        int(soft_branch_metrics.get("validation_dropped", 0) or 0),
        0,
    )
    validation_drop_ratio = (
        validation_drop_count / soft_branch_total if soft_branch_total else 0.0
    )
    soft_branch_fallback_errors: list[str] = []
    if hardcoded_fallback_ratio > 0.03:
        soft_branch_fallback_errors.append(
            f"软分支硬编码兜底率 {hardcoded_fallback_ratio:.1%} 超过发布上限 3%"
        )
    elif hardcoded_fallback_ratio > 0.01:
        warnings.append(
            f"软分支硬编码兜底率 {hardcoded_fallback_ratio:.1%}，建议抽检模板化内容"
        )
    if simple_fallback_ratio > 0.10:
        warnings.append(
            f"软分支质朴版兜底率 {simple_fallback_ratio:.1%}，超过高风险线 10%"
        )
    elif simple_fallback_ratio > 0.05:
        warnings.append(
            f"软分支质朴版兜底率 {simple_fallback_ratio:.1%}，建议检查校验失败分布"
        )
    errors.extend(soft_branch_fallback_errors)
    checks["soft_branch_fallback_quality"] = not soft_branch_fallback_errors
    transition_quality_errors: list[str] = []
    transition_drop_limit = max(
        0.0,
        min(1.0, float(configured["soft_branch_transition_drop_limit"])),
    )
    if transition_drop_ratio > transition_drop_limit:
        transition_quality_errors.append(
            f"软分支衔接失败/删除率 {transition_drop_ratio:.1%} 超过发布上限 {transition_drop_limit:.0%}"
        )
    elif transition_drop_ratio >= 0.05:
        warnings.append(
            f"软分支衔接失败/删除率 {transition_drop_ratio:.1%}，建议抽检原著接续"
        )
    errors.extend(transition_quality_errors)
    checks["soft_branch_transition_quality"] = not transition_quality_errors
    if validation_drop_ratio > 0.10:
        warnings.append(
            f"软分支硬规则失败/删除率 {validation_drop_ratio:.1%} 偏高，建议检查失败规则分布"
        )
    elif validation_drop_ratio >= 0.05:
        warnings.append(
            f"软分支硬规则失败/删除率 {validation_drop_ratio:.1%}，建议检查失败规则分布"
        )
    checks["soft_branch_validation_drop_quality"] = True
    report["stats"].update(
        {
            "soft_branch_generation_metrics": soft_branch_metrics,
            "soft_branch_simple_fallback_rate": round(simple_fallback_ratio, 4),
            "soft_branch_hardcoded_fallback_rate": round(hardcoded_fallback_ratio, 4),
            "soft_branch_transition_conflict_count": transition_conflict_count,
            "soft_branch_transition_repair_count": transition_repair_count,
            "soft_branch_transition_repair_success_count": transition_repair_success_count,
            "soft_branch_transition_drop_count": transition_drop_count,
            "soft_branch_transition_drop_rate": round(transition_drop_ratio, 4),
            "soft_branch_validation_drop_count": validation_drop_count,
            "soft_branch_validation_drop_rate": round(validation_drop_ratio, 4),
            "persisted_validation_warning_count": int(
                report["stats"].get("persisted_validation_warning_count", 0) or 0
            ),
        }
    )

    actual_transition_errors: list[str] = []
    for item in whatif_nodes:
        if item.get("terminal_branch") or item.get("convergence_status") == "terminal":
            continue
        review = item.get("transition_review")
        if not isinstance(review, dict):
            actual_transition_errors.append(
                f"What-If 节点 {item.get('node_id')} 缺少实际衔接复检结果"
            )
            continue
        try:
            score = int(review.get("score", 0))
        except (TypeError, ValueError):
            score = 0
        transition_accepted = bool(
            review.get("compatible") or review.get("accepted_with_warning")
        )
        if (
            bool(review.get("hard_conflict"))
            or not transition_accepted
            or score < int(configured["whatif_transition_min_score"])
        ):
            issues = review.get("issues", [])
            if isinstance(issues, list):
                issue_text = "；".join(str(issue) for issue in issues[:3])
            else:
                issue_text = str(issues or "")
            actual_transition_errors.append(
                f"What-If 节点 {item.get('node_id')} 实际衔接复检未通过"
                f"（{score}/10，发布最低分 {configured['whatif_transition_min_score']}）"
                f"{('：' + issue_text) if issue_text else ''}"
            )
    errors.extend(actual_transition_errors)
    checks["whatif_actual_transition"] = not actual_transition_errors
    report["stats"]["whatif_count"] = len(whatif_nodes)
    report["stats"]["omitted_whatif_count"] = len(omitted_whatifs)
    report["stats"]["terminal_whatif_count"] = len(terminal_whatifs)
    report["stats"]["terminal_whatif_ratio"] = round(terminal_ratio, 4)
    merge_policy_counts: dict[str, int] = {}
    repair_strategy_counts: dict[str, int] = {}
    for item in whatif_nodes:
        merge_policy = str(item.get("merge_policy") or "immediate")
        merge_policy_counts[merge_policy] = merge_policy_counts.get(merge_policy, 0) + 1
        repair_strategy = str(item.get("convergence_repair_strategy") or "").strip()
        if repair_strategy:
            repair_strategy_counts[repair_strategy] = (
                repair_strategy_counts.get(repair_strategy, 0) + 1
            )
    report["stats"]["whatif_merge_policies"] = merge_policy_counts
    report["stats"]["whatif_repair_strategies"] = repair_strategy_counts

    structure_errors: list[str] = []
    for node_id, node in nodes.items():
        if not isinstance(node, dict):
            structure_errors.append(f"节点 {node_id} 格式错误")
            continue
        for field, label in (
            ("title", "标题"),
            ("content_file", "正文文件"),
            ("summary", "摘要"),
            ("generated_at", "生成时间"),
        ):
            if not str(node.get(field, "")).strip():
                structure_errors.append(f"节点 {node_id} 缺少{label}")
        if not isinstance(node.get("terminal_states", {}), dict):
            structure_errors.append(f"节点 {node_id} terminal_states 格式错误")

        children = node.get("children", [])
        topology_node = topology_by_real_id.get(node_id, {})
        is_ending = topology_node.get("type") == "ending"
        is_terminal_branch = bool(
            topology_node.get("terminal_branch")
            or topology_node.get("convergence_status") == "terminal"
            or node.get("terminal_branch")
            or node.get("convergence_status") == "terminal"
        )
        if not is_ending and not is_terminal_branch and topology_by_real_id and not children:
            structure_errors.append(f"非结局节点 {node_id} 没有有效 children")
        choice_texts: list[str] = []
        for child in children if isinstance(children, list) else []:
            choice_text = (
                str(child.get("choice_text", "")).strip()
                if isinstance(child, dict)
                else ""
            )
            child_id = str(child.get("child_id", "")) if isinstance(child, dict) else ""
            child_topology = topology_by_real_id.get(child_id, {})
            targets_ending = child_topology.get("type") == "ending"
            if not choice_text and not targets_ending:
                structure_errors.append(f"节点 {node_id} 存在空选项文本")
            elif choice_text:
                choice_texts.append(choice_text)
        duplicates = sorted(
            {text for text in choice_texts if choice_texts.count(text) > 1}
        )
        if duplicates:
            structure_errors.append(
                f"节点 {node_id} 选项文本重复：{duplicates}"
            )

    ending_ids = {
        real_id
        for real_id, topology_node in topology_by_real_id.items()
        if topology_node.get("type") == "ending"
    }
    if not topology_by_real_id:
        ending_ids = {
            node_id
            for node_id, node in nodes.items()
            if isinstance(node, dict) and not node.get("children")
        }
    min_endings = max(1, int(configured["min_endings"]))
    if len(ending_ids) < min_endings:
        structure_errors.append(
            f"可发布结局数量不足：{len(ending_ids)} < {min_endings}"
        )
    missing_endings = sorted(ending_ids - set(nodes))
    if missing_endings:
        structure_errors.append(f"计划中的结局未进入 JSON：{missing_endings}")

    original_indices = [
        item.get("source_chapter_idx")
        for item in topology
        if item.get("is_original") and item.get("source_chapter_idx") is not None
    ]
    if original_indices:
        normalized_indices = [int(index) for index in original_indices]
        expected_indices = list(range(max(normalized_indices) + 1))
        if (
            len(normalized_indices) != len(set(normalized_indices))
            or sorted(normalized_indices) != expected_indices
        ):
            structure_errors.append(
                "原著章节映射不连续或重复："
                f"actual={sorted(normalized_indices)}, expected={expected_indices}"
            )

    errors.extend(structure_errors)
    checks["node_structure"] = not structure_errors
    checks["ending_requirements"] = not any(
        "结局" in error for error in structure_errors
    )
    checks["source_chapter_mapping"] = not any(
        "原著章节映射" in error for error in structure_errors
    )

    # All artifact/content rules are non-blocking release diagnostics.  Keep
    # their individual check results above, but move every finding to the
    # warning channel before calculating the score.  This prevents one malformed
    # marker, state mismatch, or transition review from scheduling a full
    # chapter_branch restart.  Infrastructure failures (network/database/
    # lease/file I/O) are raised by the caller before this quality report.
    # ``evaluate_artifacts`` may already have emitted a score blocker when the
    # warning count is large.  That derived blocker is not a new finding and
    # must not be copied back into warnings (which would deduct points again).
    quality_errors = [
        error
        for error in errors
        if not str(error).startswith("质量评分 ")
    ]
    if quality_errors:
        warnings.extend(f"质量告警：{item}" for item in quality_errors)
    errors = []

    score = max(0, 100 - len(warnings) * 3)
    min_score = max(0, min(100, int(configured["min_score"])))
    if score < min_score:
        errors.append(f"质量评分 {score} 低于发布阈值 {min_score}")
    checks["quality_score"] = score >= min_score

    report.update(
        {
            "passed": score >= min_score,
            "errors": errors,
            "warnings": warnings,
            "score": score,
            "thresholds": configured,
            "checks": checks,
            "blocking_policy": "min_score_only",
            "blocking_threshold": min_score,
        }
    )
    report["stats"].update(
        {
            "plan_completed_count": sum(
                1
                for checkpoint in plan_nodes.values()
                if isinstance(checkpoint, dict)
                and checkpoint.get("status") == "completed"
            ),
            "required_ending_count": min_endings,
        }
    )
    return report
