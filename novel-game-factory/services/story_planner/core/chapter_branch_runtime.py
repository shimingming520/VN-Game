"""Deterministic runtime helpers for the chapter_branch pipeline."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
import hmac
import json
import re
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple


PLAN_VERSION = 6
STEP_SCHEMA_VERSION = 1
CHAPTER_BRANCH_STEP_ORDER = (
    "novel_analysis",
    "topology",
    "terminal_states",
    "node_generation",
    "release_gate",
    "story_publish",
    "summary_publish",
    "stage_complete",
)
CHAPTER_BRANCH_STEP_VERSIONS = {
    "novel_analysis": "analysis-v1",
    "topology": "topology-v1",
    "terminal_states": "terminal-state-v2",
    "node_generation": "node-generation-v3",
    "release_gate": "release-gate-v3",
    "story_publish": "story-publish-v2",
    "summary_publish": "summary-publish-v1",
    "stage_complete": "stage-complete-v1",
}


def build_run_metrics(
    previous_metrics: Optional[Dict[str, Any]],
    attempt_metrics: Optional[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Keep per-attempt counters separate from task-level cumulative metrics.

    A resumed task should report how much work happened in the current attempt
    without losing the historical totals used by quality diagnostics.  Scalar
    counters and phase durations accumulate; nested soft-branch counters use
    their canonical merger; descriptive fields from the latest attempt win.
    """
    previous = deepcopy(previous_metrics) if isinstance(previous_metrics, dict) else {}
    # Newer checkpoints store the historical aggregate under this explicit
    # key; older checkpoints only had one flat metrics object.
    historical = previous.get("cumulative_metrics")
    if isinstance(historical, dict):
        previous = historical
    attempt = deepcopy(attempt_metrics) if isinstance(attempt_metrics, dict) else {}
    cumulative = deepcopy(previous)

    from services.story_planner.core.soft_branch import merge_soft_branch_metrics

    previous_soft = previous.get("soft_branches", {})
    attempt_soft = attempt.get("soft_branches", {})
    cumulative["soft_branches"] = merge_soft_branch_metrics(
        previous_soft if isinstance(previous_soft, dict) else {},
        attempt_soft if isinstance(attempt_soft, dict) else {},
    )

    previous_phases = previous.get("phase_seconds", {})
    attempt_phases = attempt.get("phase_seconds", {})
    phase_totals = dict(previous_phases) if isinstance(previous_phases, dict) else {}
    if isinstance(attempt_phases, dict):
        for name, value in attempt_phases.items():
            try:
                phase_totals[str(name)] = float(phase_totals.get(str(name), 0) or 0) + float(value or 0)
            except (TypeError, ValueError):
                phase_totals[str(name)] = value
    cumulative["phase_seconds"] = phase_totals

    nested_keys = {"soft_branches", "phase_seconds", "cache", "cache_miss_reasons"}
    for key, value in attempt.items():
        if key in nested_keys:
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            old = cumulative.get(key, 0)
            if isinstance(old, (int, float)) and not isinstance(old, bool):
                cumulative[key] = old + value
            else:
                cumulative[key] = value
        else:
            cumulative[key] = deepcopy(value)

    total_cache_events = int(cumulative.get("cache_hits", 0) or 0) + int(
        cumulative.get("cache_misses", 0) or 0
    )
    if total_cache_events:
        cumulative["cache_hit_rate"] = round(
            int(cumulative.get("cache_hits", 0) or 0) / total_cache_events,
            8,
        )

    cumulative["attempt_count"] = int(previous.get("attempt_count", 0) or 0) + 1
    return {
        "attempt_metrics": attempt,
        "cumulative_metrics": cumulative,
    }


class NodeSelfHealRequired(RuntimeError):
    """Signal that one failed node was reopened and the pipeline should resume."""


class ReleaseQualityRepairRequired(RuntimeError):
    """Signal that release-quality repair was scheduled and the pipeline should resume."""


class ReleaseGateError(RuntimeError):
    """Expected business failure: candidate exists but release publication was blocked."""

    def __init__(self, quality_report: Dict[str, Any] | str):
        if isinstance(quality_report, dict):
            report = deepcopy(quality_report)
            errors = [str(item) for item in report.get("errors", []) if str(item)]
            message = "发布质量门禁失败：" + ("；".join(errors) or "未提供具体原因")
        else:
            report = {"passed": False, "errors": [str(quality_report)]}
            message = "发布质量门禁失败：" + str(quality_report)
        super().__init__(message)
        self.status = "release_gate_failed"
        self.candidate_generated = True
        self.published = False
        self.quality_report = report


_PLACEHOLDER_MARKERS = (
    "故事在此延伸",
    "故事在此延续",
    "黑暗吞噬了一切",
    "核心冲突：",
    "core_conflict",
    "replace_me",
    "your_api_key",
)
_DANGLING_TITLE_ENDINGS = frozenset(
    "的和与并为把被在从向自对将而或及"
)
_DANGLING_TITLE_SUFFIXES = (
    "共同生", "走到无", "走向无", "陷入无", "来自另一段", "避免把",
    "为了让", "从而让", "选择把", "修复创",
)
_DANGLING_SENTENCE_ENDINGS = frozenset(
    "的了着和与并来去为把被在从向自对将而或及地得"
)
_STRONG_SENTENCE_ENDINGS = frozenset("。！？!?…")
_CLOSING_PUNCTUATION = "”’）)]】》〉」』"
_PAIRED_DELIMITERS = (
    ("（", "）", "括号"),
    ("(", ")", "括号"),
    ("【", "】", "括号"),
    ("[", "]", "括号"),
    ("《", "》", "书名号"),
    ("“", "”", "引号"),
    ("‘", "’", "引号"),
    ("「", "」", "引号"),
    ("『", "』", "引号"),
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def deterministic_node_id(
    task_id: str,
    topology_node_id: str,
    source_chapter_idx: Optional[int] = None,
) -> str:
    """Return an idempotent tree node id for a topology node."""
    if source_chapter_idx == 0:
        return "chapter_1"
    digest = hashlib.sha256(
        f"{task_id}\0{topology_node_id}".encode("utf-8")
    ).hexdigest()[:12]
    return f"node_{digest}"


def content_sha256(content: str) -> str:
    """Return a stable checksum for a persisted node artifact or source text."""
    return hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()


ENDING_SEMANTIC_REVIEW_VERSION = "ending-causality-semantic-v1"


def ending_semantic_review_fingerprint(
    *,
    variable: Mapping[str, Any] | None,
    content: str,
    provider: str,
    model: str,
    context: Mapping[str, Any] | None = None,
    reviewer_version: str = ENDING_SEMANTIC_REVIEW_VERSION,
) -> str:
    """Hash every input that can change a semantic ending review.

    The same prose and variable can occur in more than one ending.  The stable
    node identity participates in the cache key so two routes cannot share a
    decision.  Only identity fields are included from ``context``: the
    generation and release-gate callers intentionally provide different
    auxiliary fields, and including those would cause a duplicate LLM review
    for the same artifact.
    """

    return content_sha256(
        json.dumps(
            {
                "variable": dict(variable or {}),
                "content_sha256": content_sha256(content),
                "provider": str(provider or ""),
                "model": str(model or ""),
                "context": {
                    key: str((context or {}).get(key) or "")
                    for key in ("topology_node_id", "ending_id")
                    if str((context or {}).get(key) or "").strip()
                },
                "reviewer_version": str(reviewer_version or ""),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    )


def record_ending_semantic_review(
    plan: Dict[str, Any],
    topology_node_id: str,
    *,
    fingerprint: str,
    review: Mapping[str, Any],
) -> Dict[str, Any]:
    """Persist a complete semantic review on exactly one node checkpoint."""

    return record_node_semantic_review(
        plan,
        topology_node_id,
        review_kind="ending_variable_causality",
        fingerprint=fingerprint,
        review=review,
    )


def record_node_semantic_review(
    plan: Dict[str, Any],
    topology_node_id: str,
    *,
    review_kind: str,
    fingerprint: str,
    review: Mapping[str, Any],
) -> Dict[str, Any]:
    """Persist one namespaced semantic review on a node checkpoint."""

    node_id = str(topology_node_id or "").strip()
    entry = plan.setdefault("nodes", {}).setdefault(node_id, {})
    timestamp = _now_iso()
    kind = str(review_kind or "").strip()
    reviews = entry.get("semantic_reviews")
    if not isinstance(reviews, dict):
        reviews = {}
        entry["semantic_reviews"] = reviews
    reviews[kind] = {
        "fingerprint": str(fingerprint or ""),
        "review": deepcopy(dict(review or {})),
        "reviewed_at": timestamp,
    }
    # Keep the original flat fields for plans written during the first rollout
    # so older diagnostic tooling can still inspect the latest review.
    if kind == "ending_variable_causality":
        entry["ending_semantic_review"] = deepcopy(dict(review or {}))
        entry["ending_semantic_review_fingerprint"] = str(fingerprint or "")
        entry["ending_semantic_reviewed_at"] = timestamp
    entry["updated_at"] = timestamp
    plan["updated_at"] = timestamp
    return entry


def get_reusable_ending_semantic_review(
    plan: Mapping[str, Any] | None,
    topology_node_id: str,
    *,
    fingerprint: str,
) -> Dict[str, Any] | None:
    """Return a review only when the node still has the exact input identity."""

    return get_reusable_node_semantic_review(
        plan,
        topology_node_id,
        review_kind="ending_variable_causality",
        fingerprint=fingerprint,
    )


def get_reusable_node_semantic_review(
    plan: Mapping[str, Any] | None,
    topology_node_id: str,
    *,
    review_kind: str,
    fingerprint: str,
) -> Dict[str, Any] | None:
    """Return one namespaced review only for an exact identity match."""

    value = plan if isinstance(plan, Mapping) else {}
    nodes = value.get("nodes", {})
    entry = nodes.get(str(topology_node_id or ""), {}) if isinstance(nodes, Mapping) else {}
    if not isinstance(entry, Mapping):
        return None
    reviews = entry.get("semantic_reviews", {})
    cached = reviews.get(str(review_kind or ""), {}) if isinstance(reviews, Mapping) else {}
    if not cached and str(review_kind or "") == "ending_variable_causality":
        cached = {
            "fingerprint": entry.get("ending_semantic_review_fingerprint"),
            "review": entry.get("ending_semantic_review"),
        }
    if not isinstance(cached, Mapping):
        return None
    if cached.get("fingerprint") != str(fingerprint or ""):
        return None
    review = cached.get("review")
    return deepcopy(dict(review)) if isinstance(review, Mapping) else None


def stable_payload_digest(payload: Any) -> str:
    """Return a deterministic digest for JSON-compatible step inputs."""

    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return content_sha256(serialized)


def _new_plan_step(
    step_name: str,
    *,
    input_digest: str = "",
    producer_version: str = "",
) -> Dict[str, Any]:
    return {
        "status": "pending",
        "input_digest": str(input_digest or ""),
        "output_digest": "",
        "schema_version": STEP_SCHEMA_VERSION,
        "producer_version": str(
            producer_version
            or CHAPTER_BRANCH_STEP_VERSIONS.get(step_name, "unknown")
        ),
        "attempts": 0,
        "completed_at": "",
        "updated_at": _now_iso(),
        "error": "",
    }


def update_plan_step(
    plan: Dict[str, Any],
    step_name: str,
    *,
    status: str,
    output_digest: str | None = None,
    error: str | None = None,
) -> Dict[str, Any]:
    """Update one internal pipeline step without changing the public story tree."""

    if step_name not in CHAPTER_BRANCH_STEP_ORDER:
        raise ValueError(f"unsupported chapter_branch step: {step_name}")
    if status not in {"pending", "running", "completed", "failed"}:
        raise ValueError(f"unsupported chapter_branch step status: {status}")
    steps = plan.setdefault("steps", {})
    entry = steps.setdefault(step_name, _new_plan_step(step_name))
    entry["status"] = status
    entry["updated_at"] = _now_iso()
    if status == "running":
        entry["attempts"] = int(entry.get("attempts", 0) or 0) + 1
    if status == "completed":
        entry["completed_at"] = _now_iso()
        entry["error"] = ""
    elif error is not None:
        entry["error"] = str(error)
    if output_digest is not None:
        entry["output_digest"] = str(output_digest)
    plan["updated_at"] = _now_iso()
    return plan


def can_reuse_plan_step(
    plan: Dict[str, Any],
    step_name: str,
    *,
    input_payload: Any,
    producer_version: str | None = None,
) -> bool:
    """Return true only for a completed step produced from identical inputs."""

    entry = (plan.get("steps", {}) or {}).get(step_name, {})
    version = str(
        producer_version
        or CHAPTER_BRANCH_STEP_VERSIONS.get(step_name, "unknown")
    )
    return bool(
        isinstance(entry, dict)
        and entry.get("status") == "completed"
        and int(entry.get("schema_version", 0) or 0) == STEP_SCHEMA_VERSION
        and entry.get("producer_version") == version
        and entry.get("input_digest") == stable_payload_digest(input_payload)
        and str(entry.get("output_digest") or "")
    )


def record_plan_step_output(
    plan: Dict[str, Any],
    step_name: str,
    *,
    input_payload: Any,
    output: Any,
    producer_version: str | None = None,
) -> Dict[str, Any]:
    """Persist one complete, self-validating step output in the DB-backed plan."""

    if step_name not in CHAPTER_BRANCH_STEP_ORDER:
        raise ValueError(f"unsupported chapter_branch step: {step_name}")
    version = str(
        producer_version
        or CHAPTER_BRANCH_STEP_VERSIONS.get(step_name, "unknown")
    )
    output_copy = deepcopy(output)
    recorded_at = _now_iso()
    entry = {
        "status": "completed",
        "input_digest": stable_payload_digest(input_payload),
        "output_digest": stable_payload_digest(output_copy),
        "schema_version": STEP_SCHEMA_VERSION,
        "producer_version": version,
        "output": output_copy,
        "completed_at": recorded_at,
        "updated_at": recorded_at,
        "error": "",
    }
    plan.setdefault("step_outputs", {})[step_name] = entry
    steps = plan.setdefault("steps", {})
    step = steps.setdefault(
        step_name,
        _new_plan_step(
            step_name,
            input_digest=entry["input_digest"],
            producer_version=version,
        ),
    )
    step.update(
        {
            key: entry[key]
            for key in (
                "status",
                "input_digest",
                "output_digest",
                "schema_version",
                "producer_version",
                "completed_at",
                "updated_at",
                "error",
            )
        }
    )
    plan["updated_at"] = recorded_at
    return entry


def get_reusable_plan_step_output(
    plan: Dict[str, Any],
    step_name: str,
    *,
    input_payload: Any,
    producer_version: str | None = None,
) -> Any | None:
    """Return a verified DB-backed step output; reject partial or corrupt data."""

    entry = (plan.get("step_outputs", {}) or {}).get(step_name, {})
    output = entry.get("output") if isinstance(entry, dict) else None
    version = str(
        producer_version
        or CHAPTER_BRANCH_STEP_VERSIONS.get(step_name, "unknown")
    )
    if not (
        isinstance(entry, dict)
        and entry.get("status") == "completed"
        and int(entry.get("schema_version", 0) or 0) == STEP_SCHEMA_VERSION
        and entry.get("producer_version") == version
        and entry.get("input_digest") == stable_payload_digest(input_payload)
        and output is not None
        and entry.get("output_digest") == stable_payload_digest(output)
    ):
        return None
    return deepcopy(output)


def has_reusable_topology_checkpoint(
    plan: Mapping[str, Any] | None,
    *,
    branch_type: str,
    analysis_step_reusable: bool,
    topology_step_reusable: bool,
) -> bool:
    """Reject provisional/empty topology plans even when their schema is valid."""

    value = plan if isinstance(plan, Mapping) else {}
    topology_result = value.get("topology_result")
    topology = (
        topology_result.get("topology")
        if isinstance(topology_result, Mapping)
        else None
    )
    return bool(
        isinstance(topology, list)
        and topology
        and int(value.get("version", 0) or 0) in {5, PLAN_VERSION}
        and analysis_step_reusable
        and topology_step_reusable
        and (
            not value.get("branch_type")
            or value.get("branch_type") == branch_type
        )
    )


def reconcile_plan_steps(
    plan: Dict[str, Any],
    input_payloads: Mapping[str, Any],
    *,
    producer_versions: Mapping[str, str] | None = None,
) -> list[str]:
    """Reconcile versioned step identities and invalidate only affected downstream."""

    versions = dict(CHAPTER_BRANCH_STEP_VERSIONS)
    versions.update(dict(producer_versions or {}))
    existing_steps = plan.get("steps", {})
    if not isinstance(existing_steps, dict):
        existing_steps = {}

    first_invalid_index: int | None = None
    reconciled: Dict[str, Dict[str, Any]] = {}
    for index, step_name in enumerate(CHAPTER_BRANCH_STEP_ORDER):
        input_digest = stable_payload_digest(input_payloads.get(step_name, {}))
        producer_version = str(versions.get(step_name, "unknown"))
        previous = deepcopy(existing_steps.get(step_name, {}))
        if not previous:
            previous = _new_plan_step(
                step_name,
                input_digest=input_digest,
                producer_version=producer_version,
            )
        identity_changed = bool(
            previous.get("input_digest")
            and (
                previous.get("input_digest") != input_digest
                or previous.get("producer_version") != producer_version
                or int(previous.get("schema_version", 0) or 0)
                != STEP_SCHEMA_VERSION
            )
        )
        if identity_changed and first_invalid_index is None:
            first_invalid_index = index
        previous["input_digest"] = input_digest
        previous["producer_version"] = producer_version
        previous["schema_version"] = STEP_SCHEMA_VERSION
        previous.setdefault("attempts", 0)
        previous.setdefault("completed_at", "")
        previous.setdefault("output_digest", "")
        previous.setdefault("error", "")
        previous.setdefault("updated_at", _now_iso())
        reconciled[step_name] = previous

    invalidated: list[str] = []
    if first_invalid_index is not None:
        invalidated = list(CHAPTER_BRANCH_STEP_ORDER[first_invalid_index:])
        invalidated_at = _now_iso()
        for step_name in invalidated:
            entry = reconciled[step_name]
            entry.update(
                {
                    "status": "pending",
                    "output_digest": "",
                    "completed_at": "",
                    "updated_at": invalidated_at,
                    "error": "上游输入或生产版本变化，等待重新执行",
                }
            )
        step_outputs = plan.get("step_outputs", {})
        if isinstance(step_outputs, dict):
            for step_name in invalidated:
                step_outputs.pop(step_name, None)
    plan["steps"] = reconciled
    plan["updated_at"] = _now_iso()
    return invalidated


def invalidate_checkpoint_outputs_for_steps(
    plan: Dict[str, Any],
    invalidated_steps: Iterable[str],
    *,
    target_node_ids: Iterable[str] | None = None,
) -> list[str]:
    """Clear outputs owned by invalidated generation steps.

    ``invalidated_steps`` means a persisted step identity (input, schema or
    producer version) changed. ``node_generation`` normally applies to every
    node, so a genuine global identity change reopens every node. A node-local
    self-heal carries an explicit target and only that node is reopened. This
    prevents a local POV/quality repair from causing a full novel regeneration.
    """

    invalidated = {str(step) for step in invalidated_steps or []}
    if "node_generation" not in invalidated:
        return []
    requested_targets = {
        str(node_id).strip()
        for node_id in (target_node_ids or [])
        if str(node_id).strip()
    }
    valid_targets = {
        node_id
        for node_id in requested_targets
        if node_id in (plan.get("nodes", {}) or {})
    }
    # Changes in analysis/topology/terminal-state inputs affect downstream
    # context for every node and must invalidate globally.  A target scope is
    # safe only when the invalidation starts at node generation or later.
    upstream_invalidated = bool(
        invalidated.intersection({"novel_analysis", "topology", "terminal_states"})
    )
    scoped = bool(valid_targets) and not upstream_invalidated
    reopened: list[str] = []
    invalidated_at = _now_iso()
    for node_id, entry in (plan.get("nodes", {}) or {}).items():
        if scoped and str(node_id) not in valid_targets:
            continue
        if not isinstance(entry, dict) or entry.get("status") == "omitted":
            continue
        entry.update(
            {
                "status": "pending",
                "terminal_states": {},
                "summary": "",
                "error": "节点生成步骤输入或版本变化，等待重新执行",
                "artifact_sha256": "",
                "whatif_scene_checkpoint": {},
                "soft_branch_gap_checkpoint": {},
                "semantic_reviews": {},
                "ending_semantic_review": {},
                "ending_semantic_review_fingerprint": "",
                "updated_at": invalidated_at,
            }
        )
        reopened.append(str(node_id))
    plan["updated_at"] = invalidated_at
    return reopened


def record_chapter_substep(
    plan: Dict[str, Any],
    step_name: str,
    chapter_index: int,
    *,
    input_payload: Any,
    output: Any,
    producer_version: str,
) -> Dict[str, Any]:
    """Atomically describe one completed chapter-sized substep in the plan."""

    index = int(chapter_index)
    output_copy = deepcopy(output)
    recorded_at = _now_iso()
    entry = {
        "status": "completed",
        "input_digest": stable_payload_digest(input_payload),
        "output_digest": stable_payload_digest(output_copy),
        "schema_version": STEP_SCHEMA_VERSION,
        "producer_version": str(producer_version),
        "output": output_copy,
        "completed_at": recorded_at,
        "updated_at": recorded_at,
        "error": "",
    }
    plan.setdefault("chapter_substeps", {}).setdefault(step_name, {})[
        str(index)
    ] = entry
    plan["updated_at"] = recorded_at
    return entry


def get_reusable_chapter_substep(
    plan: Dict[str, Any],
    step_name: str,
    chapter_index: int,
    *,
    input_payload: Any,
    producer_version: str,
) -> Any | None:
    """Return a verified chapter output, or ``None`` for any partial/stale row."""

    entry = (
        ((plan.get("chapter_substeps", {}) or {}).get(step_name, {}) or {}).get(
            str(int(chapter_index)),
            {},
        )
    )
    output = entry.get("output") if isinstance(entry, dict) else None
    if not (
        isinstance(entry, dict)
        and entry.get("status") == "completed"
        and int(entry.get("schema_version", 0) or 0) == STEP_SCHEMA_VERSION
        and entry.get("producer_version") == str(producer_version)
        and entry.get("input_digest") == stable_payload_digest(input_payload)
        and output is not None
        and entry.get("output_digest") == stable_payload_digest(output)
    ):
        return None
    return deepcopy(output)


def invalidate_chapter_substeps_from(
    plan: Dict[str, Any],
    step_name: str,
    start_index: int,
) -> list[int]:
    """Remove one chapter and every dependent later chapter from a state chain."""

    entries = (plan.get("chapter_substeps", {}) or {}).get(step_name, {})
    if not isinstance(entries, dict):
        return []
    start = int(start_index)
    removed = sorted(
        int(key)
        for key in list(entries)
        if str(key).isdigit() and int(key) >= start
    )
    for index in removed:
        entries.pop(str(index), None)
    if removed:
        plan["updated_at"] = _now_iso()
    return removed


def initialize_soft_branch_gap_checkpoint(
    existing: Optional[Dict[str, Any]],
    *,
    gap_count: int,
    input_payload: Any,
) -> Dict[str, Any]:
    """Normalize a gap-level checkpoint and keep only a verified prefix."""

    expected_digest = stable_payload_digest(input_payload)
    value = deepcopy(existing or {})
    if value.get("input_digest") != expected_digest:
        value = {}
    normalized: list[Dict[str, Any]] = []
    seen: set[int] = set()
    for item in value.get("completed_gaps", []) or []:
        if not isinstance(item, dict):
            continue
        index = item.get("gap_index")
        omitted = bool(item.get("omitted"))
        marker = str(item.get("marker") or "")
        stored_marker_sha256 = str(item.get("marker_sha256") or "")
        if (
            not isinstance(index, int)
            or not 0 <= index < max(int(gap_count), 0)
            or index in seen
            or (not omitted and not marker.strip())
            or (
                not omitted
                and (
                    not stored_marker_sha256
                    or not hmac.compare_digest(
                        stored_marker_sha256,
                        content_sha256(marker),
                    )
                )
            )
        ):
            continue
        seen.add(index)
        normalized.append(
            {
                "gap_index": index,
                "marker": marker,
                "marker_sha256": stored_marker_sha256 or content_sha256(marker),
                "omitted": omitted,
                "completed_at": str(item.get("completed_at") or _now_iso()),
            }
        )
    normalized.sort(key=lambda item: item["gap_index"])
    contiguous: list[Dict[str, Any]] = []
    for expected_index, item in enumerate(normalized):
        if item["gap_index"] != expected_index:
            break
        contiguous.append(item)
    count = max(int(gap_count), 0)
    return {
        "schema_version": STEP_SCHEMA_VERSION,
        "input_digest": expected_digest,
        "gap_count": count,
        "completed_gaps": contiguous,
        "status": "completed" if len(contiguous) >= count else "running",
        "updated_at": _now_iso(),
    }


def get_soft_branch_gap(
    state: Dict[str, Any],
    gap_index: int,
) -> Dict[str, Any] | None:
    """Return one verified gap record from a normalized checkpoint."""

    index = int(gap_index)
    for item in (state or {}).get("completed_gaps", []) or []:
        if not isinstance(item, dict) or item.get("gap_index") != index:
            continue
        marker = str(item.get("marker") or "")
        if item.get("omitted") or hmac.compare_digest(
            str(item.get("marker_sha256") or ""),
            content_sha256(marker),
        ):
            return deepcopy(item)
    return None


def record_soft_branch_gap(
    state: Dict[str, Any],
    *,
    gap_index: int,
    marker: str,
    omitted: bool = False,
) -> Dict[str, Any]:
    """Append or replace one completed soft-branch gap checkpoint."""

    index = int(gap_index)
    count = max(int(state.get("gap_count", 0) or 0), 0)
    if not 0 <= index < count:
        raise ValueError(f"soft branch gap index out of range: {index}/{count}")
    value = str(marker or "")
    if not omitted and not value.strip():
        raise ValueError("completed soft branch gap marker cannot be empty")
    entry = {
        "gap_index": index,
        "marker": value,
        "marker_sha256": content_sha256(value),
        "omitted": bool(omitted),
        "completed_at": _now_iso(),
    }
    existing = [
        item
        for item in (state.get("completed_gaps", []) or [])
        if isinstance(item, dict) and item.get("gap_index") != index
    ]
    existing.append(entry)
    existing.sort(key=lambda item: item.get("gap_index", -1))
    state["completed_gaps"] = existing
    state["status"] = "completed" if len(existing) >= count else "running"
    state["updated_at"] = _now_iso()
    return state


def get_node_regeneration_reason(entry: Dict[str, Any] | None) -> str:
    """Return the repair directive that regenerated nodes must receive in their prompt."""
    value = entry if isinstance(entry, dict) else {}
    for field in ("release_repair_reason", "node_self_heal_reason"):
        reason = str(value.get(field) or "").strip()
        if reason:
            return reason
    if int(value.get("regeneration_count", 0) or 0) > 0:
        return str(value.get("error") or "").strip()
    return ""


def _release_repair_fingerprint(directive: Dict[str, Any] | None) -> str:
    value = directive if isinstance(directive, dict) else {}
    payload = {
        "mode": str(value.get("mode") or "none"),
        "target_node_ids": sorted(
            str(item) for item in (value.get("target_node_ids", []) or []) if str(item)
        ),
        "rebuild_topology": bool(value.get("rebuild_topology")),
        "reason": str(value.get("reason") or "").strip(),
    }
    return content_sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


_TOPOLOGY_RUNTIME_FIELDS = frozenset(
    {
        # Filled after the graph is designed and therefore not part of the
        # graph identity.  Their values can legitimately differ when a plan
        # is resumed from a pre-generation checkpoint.
        "summary",
        "plan_direction",
        "parent_ids",
        "choice_text",
    }
)
_TARGET_REPAIR_METADATA_FIELDS = frozenset(
    {
        "title",
        "branch_threshold",
        "branch_ratio_threshold",
        "branch_margin_ratio",
        "effective_choice_count",
        "minimum_effective_choices",
        "route_enabled",
        "condition",
    }
)


def _canonicalize_topology_for_fingerprint(
    topology_result: Dict[str, Any] | None,
) -> Dict[str, Any]:
    """Return only topology identity data for checkpoint comparisons.

    The persisted topology is enriched during assembly with summaries,
    reverse parent links and player-facing labels.  Those fields are useful
    output, but changing them must not make a targeted release repair reopen
    every completed chapter.  Keep the actual graph and route constraints in
    the fingerprint while excluding diagnostic generation metadata.
    """
    canonical = deepcopy(topology_result or {})
    if not isinstance(canonical, dict):
        return {}
    canonical.pop("topology_generation", None)
    topology = canonical.get("topology")
    if isinstance(topology, list):
        for node in topology:
            if isinstance(node, dict):
                for field in _TOPOLOGY_RUNTIME_FIELDS:
                    node.pop(field, None)
    return canonical


def topology_fingerprint(topology_result: Dict[str, Any]) -> str:
    """Hash canonical topology data so derived UI normalization is idempotent.

    Ending edges are automatic routes and are always normalized to an empty
    ``choice_text``.  What-If merge metadata is also filled deterministically
    at runtime.  Those transformations must not look like a new graph on
    resume; otherwise a release repair would invalidate every completed node.
    Actual graph changes (node ids, edges, chapter mapping, route variables)
    remain part of the fingerprint and still invalidate checkpoints.
    """
    canonical = _canonicalize_topology_for_fingerprint(topology_result)
    topology = canonical.get("topology") if isinstance(canonical, dict) else None
    if isinstance(topology, list):
        for node in topology:
            if not isinstance(node, dict):
                continue
            if node.get("type") == "ending":
                node["choice_text"] = ""
                continue
            if node.get("type") != "chapter" or node.get("is_original", True):
                continue
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
                delay = max(1, min(int(node.get("merge_after_chapters") or 1), 3))
            except (TypeError, ValueError):
                delay = 1
            if policy != "terminal" and delay > 1:
                policy = "delayed"
            node["merge_after_chapters"] = delay
            node["merge_policy"] = policy
    serialized = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return content_sha256(serialized)


def refresh_plan_topology_fingerprint(plan: Dict[str, Any]) -> str:
    """Keep the saved fingerprint aligned with runtime-enriched topology data."""
    topology_result = plan.get("topology_result", {}) if isinstance(plan, dict) else {}
    fingerprint = topology_fingerprint(
        topology_result if isinstance(topology_result, dict) else {}
    )
    if isinstance(plan, dict):
        plan["topology_fingerprint"] = fingerprint
    return fingerprint


def _target_scoped_topology_change(
    existing_topology: Mapping[str, Any] | None,
    current_topology: Mapping[str, Any] | None,
    target_node_ids: Iterable[str],
) -> bool:
    """Tell whether a topology refresh is limited to requested repair nodes.

    Release repairs may update a target's title/summary/route metadata while
    rebuilding its prose.  In that case completed upstream nodes remain
    valid.  Any change to a non-target node, edge, variable definition, or
    node membership is treated as a real graph change and still invalidates
    the full checkpoint.
    """
    if not isinstance(existing_topology, Mapping) or not isinstance(current_topology, Mapping):
        return False
    targets = {str(item).strip() for item in target_node_ids if str(item).strip()}
    if not targets:
        return False
    old_nodes = {
        str(item.get("node_id")): item
        for item in (existing_topology.get("topology") or [])
        if isinstance(item, Mapping) and item.get("node_id")
    }
    new_nodes = {
        str(item.get("node_id")): item
        for item in (current_topology.get("topology") or [])
        if isinstance(item, Mapping) and item.get("node_id")
    }
    if set(old_nodes) != set(new_nodes):
        return False
    old_root = _canonicalize_topology_for_fingerprint(existing_topology)
    new_root = _canonicalize_topology_for_fingerprint(current_topology)
    if old_root.get("variable_defs", []) != new_root.get("variable_defs", []):
        return False
    for node_id in old_nodes:
        old_node = next(
            item for item in (old_root.get("topology") or [])
            if isinstance(item, Mapping) and str(item.get("node_id")) == node_id
        )
        new_node = next(
            item for item in (new_root.get("topology") or [])
            if isinstance(item, Mapping) and str(item.get("node_id")) == node_id
        )
        if node_id in targets:
            old_node = dict(old_node)
            new_node = dict(new_node)
            for field in _TARGET_REPAIR_METADATA_FIELDS:
                old_node.pop(field, None)
                new_node.pop(field, None)
        if old_node != new_node:
            return False
    return True


def initialize_chapter_branch_plan(
    task_id: str,
    topology_result: Dict[str, Any],
    branch_type: str,
    existing_plan: Optional[Dict[str, Any]] = None,
    source_fingerprint: Optional[str] = None,
) -> Dict[str, Any]:
    """Create or reconcile a resumable chapter_branch checkpoint document."""
    existing = deepcopy(existing_plan or {})
    current_topology_fingerprint = topology_fingerprint(topology_result)
    existing_topology = existing.get("topology_result")
    if not isinstance(existing_topology, dict) and isinstance(existing.get("topology"), list):
        existing_topology = existing
    # Recompute from the persisted topology instead of trusting the stored
    # digest.  Older plans were hashed before deterministic normalization
    # (notably ending ``choice_text`` and What-If merge metadata), so their
    # saved digest can differ even though the graph is semantically identical.
    # The topology payload itself is the source of truth and is inexpensive to
    # canonicalize here.
    existing_topology_fingerprint = (
        topology_fingerprint(existing_topology)
        if isinstance(existing_topology, dict)
        else existing.get("topology_fingerprint")
    )
    source_changed = bool(
        source_fingerprint
        and existing.get("source_fingerprint")
        and source_fingerprint != existing.get("source_fingerprint")
    )
    existing_has_real_topology = bool(
        isinstance(existing_topology, dict)
        and isinstance(existing_topology.get("topology"), list)
        and existing_topology.get("topology")
    )
    topology_changed = bool(
        existing_has_real_topology
        and existing_topology_fingerprint
        and existing_topology_fingerprint != current_topology_fingerprint
    )
    active_repair = existing.get("regeneration_request")
    active_repair_targets = (
        [
            str(node_id).strip()
            for node_id in (active_repair.get("node_ids", []) or [])
            if str(node_id).strip()
        ]
        if isinstance(active_repair, Mapping)
        and str(active_repair.get("status") or "pending").lower()
        not in {"completed", "cancelled", "inactive"}
        else []
    )
    active_self_heal = existing.get("node_self_heal_request")
    active_self_heal_targets = (
        [str(active_self_heal.get("node_id") or "").strip()]
        if isinstance(active_self_heal, Mapping)
        and str(active_self_heal.get("status") or "pending").lower()
        not in {"completed", "cancelled", "inactive"}
        and str(active_self_heal.get("node_id") or "").strip()
        else []
    )
    targeted_repair_targets = list(
        dict.fromkeys(active_repair_targets + active_self_heal_targets)
    )
    target_scoped_topology_change = topology_changed and _target_scoped_topology_change(
        existing_topology,
        topology_result,
        targeted_repair_targets,
    )
    branch_type_changed = bool(
        existing.get("branch_type")
        and existing.get("branch_type") != branch_type
    )
    existing_plan_version = int(existing.get("version", 0) or 0)
    plan_schema_upgrade = bool(
        existing and PLAN_VERSION == 6 and existing_plan_version == 5
    )
    plan_version_changed = bool(
        existing
        and existing_plan_version != PLAN_VERSION
        and not plan_schema_upgrade
    )
    checkpoints_invalidated = (
        source_changed
        or topology_changed
        or branch_type_changed
        or plan_version_changed
    )
    existing_steps = existing.get("steps", {})
    upstream_steps_completed = bool(
        isinstance(existing_steps, Mapping)
        and all(
            isinstance(existing_steps.get(step_name), Mapping)
            and existing_steps[step_name].get("status") in {"completed", "omitted"}
            for step_name in ("novel_analysis", "topology", "terminal_states")
        )
    )
    preserve_targeted_repair = (
        target_scoped_topology_change
        and not source_changed
        and not branch_type_changed
        and not plan_version_changed
        and upstream_steps_completed
    )
    existing_nodes = (
        existing.get("nodes", {})
        if isinstance(existing, dict) and (not checkpoints_invalidated or preserve_targeted_repair)
        else {}
    )
    nodes: Dict[str, Dict[str, Any]] = {}
    real_id_map: Dict[str, str] = {}

    for node in topology_result.get("topology", []):
        if not isinstance(node, dict) or not node.get("node_id"):
            continue
        topology_node_id = node["node_id"]
        previous = deepcopy(existing_nodes.get(topology_node_id, {}))
        previous_status = previous.get("status", "pending")
        if previous_status in ("running", "failed"):
            previous_status = "pending"
        real_id = previous.get("real_id") or deterministic_node_id(
            task_id,
            topology_node_id,
            node.get("source_chapter_idx"),
        )
        previous.update(
            {
                "status": previous_status,
                "real_id": real_id,
                "type": node.get("type", "chapter"),
                "updated_at": previous.get("updated_at") or _now_iso(),
            }
        )
        previous.setdefault("terminal_states", {})
        previous.setdefault("summary", "")
        previous.setdefault("error", "")
        previous.setdefault("attempts", 0)
        previous.setdefault("artifact_sha256", "")
        previous.setdefault("regeneration_count", 0)
        nodes[topology_node_id] = previous
        real_id_map[topology_node_id] = real_id

    steps = (
        deepcopy(existing.get("steps", {}))
        if isinstance(existing.get("steps"), dict)
        and (not checkpoints_invalidated or preserve_targeted_repair)
        else {}
    )
    for step_name in CHAPTER_BRANCH_STEP_ORDER:
        steps.setdefault(step_name, _new_plan_step(step_name))

    return {
        "version": PLAN_VERSION,
        "task_id": task_id,
        "branch_type": branch_type,
        "topology_result": deepcopy(topology_result),
        "topology_fingerprint": current_topology_fingerprint,
        "source_fingerprint": source_fingerprint or existing.get("source_fingerprint", ""),
        "real_id_map": real_id_map,
        "nodes": nodes,
        "chapter_substeps": (
            deepcopy(existing.get("chapter_substeps", {}))
            if isinstance(existing.get("chapter_substeps"), dict)
            and not source_changed
            and not plan_version_changed
            else {}
        ),
        "step_outputs": (
            deepcopy(existing.get("step_outputs", {}))
            if isinstance(existing.get("step_outputs"), dict)
            and not source_changed
            and not plan_version_changed
            else {}
        ),
        "global_style_checkpoint": (
            deepcopy(existing.get("global_style_checkpoint", {}))
            if isinstance(existing.get("global_style_checkpoint"), dict)
            and not source_changed
            and not plan_version_changed
            else {}
        ),
        "preprocessing_checkpoint": (
            deepcopy(existing.get("preprocessing_checkpoint", {}))
            if isinstance(existing.get("preprocessing_checkpoint"), dict)
            and (not source_changed or preserve_targeted_repair)
            else {}
        ),
        "regeneration_request": (
            deepcopy(existing.get("regeneration_request", {}))
            if isinstance(existing.get("regeneration_request"), dict)
            and (not checkpoints_invalidated or preserve_targeted_repair)
            else {}
        ),
        "node_self_heal_request": (
            deepcopy(existing.get("node_self_heal_request", {}))
            if isinstance(existing.get("node_self_heal_request"), dict)
            and (not checkpoints_invalidated or preserve_targeted_repair)
            else {}
        ),
        "steps": {step: steps[step] for step in CHAPTER_BRANCH_STEP_ORDER},
        "metrics": deepcopy(existing.get("metrics", {}))
        if (not checkpoints_invalidated or preserve_targeted_repair)
        else {},
        "release_repair_round": int(existing.get("release_repair_round", 0) or 0),
        "release_repair_history": deepcopy(existing.get("release_repair_history", [])),
        "checkpoint_invalidation_reason": (
            "plan_schema_upgraded"
            if plan_schema_upgrade
            else "plan_version_changed"
            if plan_version_changed
            else "source_changed"
            if source_changed
            else "targeted_topology_changed"
            if preserve_targeted_repair
            else "topology_changed"
            if topology_changed
            else "branch_type_changed"
            if branch_type_changed
            else ""
        ),
        "created_at": existing.get("created_at") or _now_iso(),
        "updated_at": _now_iso(),
    }


def update_plan_node(
    plan: Dict[str, Any],
    topology_node_id: str,
    *,
    status: str,
    real_id: Optional[str] = None,
    terminal_states: Optional[Dict[str, Any]] = None,
    summary: Optional[str] = None,
    error: Optional[str] = None,
    artifact_sha256: Optional[str] = None,
) -> Dict[str, Any]:
    """Mutate one node checkpoint and return the updated plan."""
    if status not in {
        "pending",
        "running",
        "persisting",
        "completed",
        "failed",
        "omitted",
    }:
        raise ValueError(f"unsupported node status: {status}")
    entry = plan.setdefault("nodes", {}).setdefault(topology_node_id, {})
    entry["status"] = status
    entry["updated_at"] = _now_iso()
    if status == "running":
        entry["attempts"] = int(entry.get("attempts", 0)) + 1
    if real_id is not None:
        entry["real_id"] = real_id
        plan.setdefault("real_id_map", {})[topology_node_id] = real_id
    if terminal_states is not None:
        entry["terminal_states"] = deepcopy(terminal_states)
    if summary is not None:
        entry["summary"] = summary
    if error is not None:
        entry["error"] = error
    elif status == "completed":
        entry["error"] = ""
    if artifact_sha256 is not None:
        entry["artifact_sha256"] = str(artifact_sha256)
    plan["updated_at"] = _now_iso()
    return plan


def get_active_regeneration_targets(plan: Mapping[str, Any] | None) -> list[str]:
    """Return the currently active targeted-repair node scope."""
    value = plan if isinstance(plan, Mapping) else {}
    request = value.get("regeneration_request")
    if not isinstance(request, Mapping):
        return []
    if str(request.get("status") or "pending").lower() in {
        "completed",
        "cancelled",
        "inactive",
    }:
        return []
    return list(
        dict.fromkeys(
            str(node_id).strip()
            for node_id in (request.get("node_ids", []) or [])
            if str(node_id).strip()
        )
    )


def get_active_node_self_heal_targets(plan: Mapping[str, Any] | None) -> list[str]:
    """Return active node-local self-heal targets for checkpoint reconciliation."""
    value = plan if isinstance(plan, Mapping) else {}
    request = value.get("node_self_heal_request")
    if not isinstance(request, Mapping):
        return []
    if str(request.get("status") or "pending").lower() in {
        "completed",
        "cancelled",
        "inactive",
    }:
        return []
    node_id = str(request.get("node_id") or "").strip()
    return [node_id] if node_id else []


def mark_node_self_heal_completed(
    plan: Dict[str, Any],
    topology_node_id: str,
    *,
    status: str = "completed",
) -> bool:
    """Close a self-heal request after its target is generated or omitted."""
    request = plan.get("node_self_heal_request") if isinstance(plan, dict) else None
    if not isinstance(request, dict):
        return False
    node_id = str(topology_node_id or "").strip()
    if str(request.get("node_id") or "").strip() != node_id:
        return False
    recorded_at = _now_iso()
    request["status"] = str(status or "completed")
    request["completed_at"] = recorded_at
    plan["updated_at"] = recorded_at
    return True


def mark_regeneration_request_completed(
    plan: Dict[str, Any],
    *,
    completed_node_ids: Iterable[str] | None = None,
) -> bool:
    """Close a targeted regeneration request after all selected nodes finish."""
    request = plan.get("regeneration_request") if isinstance(plan, dict) else None
    if not isinstance(request, dict):
        return False
    if str(request.get("status") or "pending").lower() == "completed":
        return False
    requested = {
        str(node_id).strip()
        for node_id in (request.get("node_ids", []) or [])
        if str(node_id).strip()
    }
    if not requested:
        return False
    if completed_node_ids is not None:
        completed = {
            str(node_id).strip()
            for node_id in completed_node_ids
            if str(node_id).strip()
        }
        if not requested.issubset(completed):
            return False
    else:
        nodes = plan.get("nodes", {}) if isinstance(plan, dict) else {}
        if not all(
            isinstance(nodes.get(node_id), dict)
            and nodes[node_id].get("status") in {"completed", "omitted"}
            for node_id in requested
        ):
            return False
    request["status"] = "completed"
    request["completed_at"] = _now_iso()
    plan["updated_at"] = _now_iso()
    return True


def can_resume_node_checkpoint(entry: Dict[str, Any], content: str) -> bool:
    """Validate completed or crash-interrupted artifact before skipping generation."""
    if not isinstance(entry, dict) or entry.get("status") not in {
        "completed",
        "persisting",
    }:
        return False
    value = str(content or "")
    if not value.strip():
        return False
    expected_hash = str(entry.get("artifact_sha256") or "")
    if not expected_hash:
        return False
    return hmac.compare_digest(content_sha256(value), expected_hash)


def reopen_plan_nodes(
    plan: Dict[str, Any],
    topology_node_ids: Iterable[str],
    *,
    include_non_original_descendants: bool = True,
    clear_scene_checkpoint: bool = False,
    reason: str = "定向重生成",
) -> list[str]:
    """Reopen selected checkpoints without invalidating unrelated original nodes."""
    requested = [str(node_id) for node_id in topology_node_ids if str(node_id)]
    nodes = plan.get("nodes", {}) if isinstance(plan, dict) else {}
    missing = [node_id for node_id in requested if node_id not in nodes]
    if missing:
        raise ValueError("定向重生成节点不存在：" + ", ".join(missing))

    topology_result = plan.get("topology_result", {})
    topology = topology_result.get("topology", []) if isinstance(topology_result, dict) else []
    topology_by_id = {
        node.get("node_id"): node
        for node in topology
        if isinstance(node, dict) and node.get("node_id")
    }
    reopen_set = set(requested)
    if include_non_original_descendants:
        queue = list(requested)
        while queue:
            current_id = queue.pop(0)
            current = topology_by_id.get(current_id, {})
            for child_id in current.get("next", []) or []:
                child = topology_by_id.get(child_id)
                if not child or child.get("is_original", True) or child_id in reopen_set:
                    continue
                reopen_set.add(child_id)
                queue.append(child_id)

    ordered_ids = [
        node.get("node_id")
        for node in topology
        if isinstance(node, dict) and node.get("node_id") in reopen_set
    ]
    ordered_ids.extend(node_id for node_id in requested if node_id not in ordered_ids)
    requested_at = _now_iso()
    stale_self_heal = plan.get("node_self_heal_request")
    if isinstance(stale_self_heal, dict) and str(
        stale_self_heal.get("status") or "pending"
    ).lower() not in {"completed", "cancelled", "inactive"}:
        stale_self_heal["status"] = "inactive"
        stale_self_heal["inactive_at"] = requested_at
    for node_id in ordered_ids:
        entry = nodes[node_id]
        entry.update(
            {
                "status": "pending",
                "terminal_states": {},
                "summary": "",
                "error": str(reason),
                "release_repair_reason": str(reason),
                "artifact_sha256": "",
                "soft_branch_gap_checkpoint": {},
                "semantic_reviews": {},
                "ending_semantic_review": {},
                "ending_semantic_review_fingerprint": "",
                "regeneration_count": int(entry.get("regeneration_count", 0)) + 1,
                "regenerate_requested_at": requested_at,
                "updated_at": requested_at,
            }
        )
        if clear_scene_checkpoint:
            entry["whatif_scene_checkpoint"] = {}
    plan["regeneration_request"] = {
        "node_ids": ordered_ids,
        "reason": str(reason),
        "requested_at": requested_at,
        "status": "pending",
    }
    plan["updated_at"] = requested_at
    return ordered_ids


def schedule_node_self_heal(
    plan: Dict[str, Any],
    topology_node_id: str,
    reason: str,
    *,
    max_attempts: int = 3,
) -> Dict[str, Any]:
    """Reopen one failed node with a bounded, increasingly broad repair policy."""
    node_id = str(topology_node_id or "").strip()
    nodes = plan.get("nodes", {}) if isinstance(plan, dict) else {}
    if node_id not in nodes:
        raise ValueError(f"节点自愈目标不存在：{node_id}")

    entry = nodes[node_id]
    limit = max(0, int(max_attempts or 0))
    previous_attempts = int(entry.get("node_self_heal_attempts", 0) or 0)
    if previous_attempts >= limit:
        recorded_at = _now_iso()
        entry.update(
            {
                "status": "failed",
                "error": str(reason),
                "node_self_heal_reason": str(reason),
                "updated_at": recorded_at,
            }
        )
        plan["updated_at"] = recorded_at
        return {
            "scheduled": False,
            "exhausted": True,
            "attempt": previous_attempts,
            "node_id": node_id,
            "cleared_scene_checkpoint": False,
        }

    attempt = previous_attempts + 1
    cleared_scene_checkpoint = attempt >= 2
    requested_at = _now_iso()
    entry.update(
        {
            "status": "pending",
            "terminal_states": {},
            "summary": "",
            "error": str(reason),
            "artifact_sha256": "",
            "soft_branch_gap_checkpoint": {},
            "node_self_heal_attempts": attempt,
            "semantic_reviews": {},
            "ending_semantic_review": {},
            "ending_semantic_review_fingerprint": "",
            "node_self_heal_reason": str(reason),
            "regeneration_count": int(entry.get("regeneration_count", 0) or 0) + 1,
            "regenerate_requested_at": requested_at,
            "updated_at": requested_at,
        }
    )
    if cleared_scene_checkpoint:
        entry["whatif_scene_checkpoint"] = {}
    plan["node_self_heal_request"] = {
        "node_id": node_id,
        "attempt": attempt,
        "reason": str(reason),
        "requested_at": requested_at,
    }
    plan["updated_at"] = requested_at
    return {
        "scheduled": True,
        "exhausted": False,
        "attempt": attempt,
        "node_id": node_id,
        "cleared_scene_checkpoint": cleared_scene_checkpoint,
    }


def record_optional_branch_failure(
    plan: Dict[str, Any],
    topology_node_id: str,
    reason: str,
    *,
    max_attempts: int = 3,
) -> Dict[str, Any]:
    """Record a failed optional branch without scheduling node self-heal.

    What-If/Bad-End nodes are disposable descendants.  Their local generation
    retries must never populate ``node_self_heal_request`` because that signal
    restarts the outer chapter-branch pipeline and can invalidate unrelated
    terminal-state checkpoints.  Callers may use ``retry`` for diagnostics;
    once the local budget is exhausted they should omit only this branch.
    """
    node_id = str(topology_node_id or "").strip()
    nodes = plan.get("nodes", {}) if isinstance(plan, dict) else {}
    if node_id not in nodes:
        raise ValueError(f"optional branch target does not exist: {node_id}")
    entry = nodes[node_id]
    if not isinstance(entry, dict):
        raise ValueError(f"optional branch checkpoint is invalid: {node_id}")
    limit = max(1, int(max_attempts or 1))
    attempt = int(entry.get("optional_branch_attempts", 0) or 0) + 1
    recorded_at = _now_iso()
    history = entry.setdefault("optional_branch_failure_history", [])
    if not isinstance(history, list):
        history = []
        entry["optional_branch_failure_history"] = history
    history.append(
        {
            "attempt": attempt,
            "reason": str(reason or "optional_branch_failed"),
            "recorded_at": recorded_at,
        }
    )
    entry.update(
        {
            "optional_branch_attempts": attempt,
            "optional_branch_last_error": str(reason or "optional_branch_failed"),
            "updated_at": recorded_at,
        }
    )
    plan["updated_at"] = recorded_at
    exhausted = attempt >= limit
    return {
        "node_id": node_id,
        "attempt": attempt,
        "max_attempts": limit,
        "retry": not exhausted,
        "exhausted": exhausted,
        "scheduled": False,
    }


def new_optional_branch_attempt_budget(max_attempts: int = 3) -> Dict[str, Any]:
    """Create a per-optional-branch budget shared by generation and repairs."""
    return {
        "max_attempts": max(1, min(int(max_attempts or 3), 3)),
        "used": 0,
        "history": [],
    }


def consume_optional_branch_attempt(
    budget: Dict[str, Any],
    *,
    phase: str,
) -> Dict[str, Any]:
    """Consume one branch-local attempt without ever exceeding its cap."""
    if not isinstance(budget, dict):
        return {"allowed": False, "attempt": 0, "remaining": 0}
    maximum = max(1, min(int(budget.get("max_attempts", 3) or 3), 3))
    used = max(0, int(budget.get("used", 0) or 0))
    if used >= maximum:
        budget["max_attempts"] = maximum
        return {
            "allowed": False,
            "attempt": used,
            "remaining": 0,
        }
    used += 1
    budget["max_attempts"] = maximum
    budget["used"] = used
    history = budget.setdefault("history", [])
    if isinstance(history, list):
        history.append({"attempt": used, "phase": str(phase or "unknown")})
    return {
        "allowed": True,
        "attempt": used,
        "remaining": maximum - used,
    }


def remaining_optional_branch_attempts(budget: Dict[str, Any]) -> int:
    """Return the number of unused attempts in a branch-local budget."""
    if not isinstance(budget, dict):
        return 0
    maximum = max(1, min(int(budget.get("max_attempts", 3) or 3), 3))
    used = max(0, int(budget.get("used", 0) or 0))
    return max(0, maximum - used)


def schedule_release_quality_repair(
    plan: Dict[str, Any],
    directive: Dict[str, Any],
    *,
    max_rounds: int = 2,
) -> Dict[str, Any]:
    """Schedule one bounded release-level repair round on the resumable plan."""
    mode = str((directive or {}).get("mode") or "none")
    limit = max(0, int(max_rounds or 0))
    current_round = int(plan.get("release_repair_round", 0) or 0)
    history = plan.setdefault("release_repair_history", [])
    reason = str((directive or {}).get("reason") or "发布质量门禁未通过")
    # A release-gate decision supersedes any stale node-local self-heal
    # request left by an earlier interrupted attempt.  Keeping both active
    # would make the next resume choose an obsolete target and could reopen
    # the wrong node or cause an unnecessary extra pass.
    stale_self_heal = plan.get("node_self_heal_request")
    if isinstance(stale_self_heal, dict) and str(
        stale_self_heal.get("status") or "pending"
    ).lower() not in {"completed", "cancelled", "inactive"}:
        stale_self_heal["status"] = "inactive"
        stale_self_heal["inactive_at"] = _now_iso()
    repair_fingerprint = _release_repair_fingerprint(directive)
    previous_scheduled = next(
        (
            item for item in reversed(history)
            if isinstance(item, dict) and item.get("scheduled")
        ),
        None,
    )
    if (
        previous_scheduled
        and previous_scheduled.get("repair_fingerprint") == repair_fingerprint
    ):
        recorded_at = _now_iso()
        history.append(
            {
                "round": current_round,
                "mode": mode,
                "scheduled": False,
                "exhausted": True,
                "repeated": True,
                "repair_fingerprint": repair_fingerprint,
                "reason": reason,
                "recorded_at": recorded_at,
            }
        )
        plan["updated_at"] = recorded_at
        return {
            "scheduled": False,
            "exhausted": True,
            "repeated": True,
            "round": current_round,
            "mode": mode,
        }

    if mode == "none" or current_round >= limit:
        recorded_at = _now_iso()
        history.append(
            {
                "round": current_round,
                "mode": mode,
                "scheduled": False,
                "exhausted": True,
                "repeated": False,
                "repair_fingerprint": repair_fingerprint,
                "reason": reason,
                "recorded_at": recorded_at,
            }
        )
        plan["updated_at"] = recorded_at
        return {
            "scheduled": False,
            "exhausted": True,
            "repeated": False,
            "round": current_round,
            "mode": mode,
        }

    repair_round = current_round + 1
    rebuild_topology = bool((directive or {}).get("rebuild_topology"))
    reopened_node_ids: list[str] = []
    requested_target_ids = [
        str(node_id).strip()
        for node_id in ((directive or {}).get("target_node_ids", []) or [])
        if str(node_id).strip()
    ]
    # A targeted repair without a concrete node is a malformed directive.  Do
    # not record a successful no-op that merely reopens the release tail and
    # loops forever; fall back to the bounded full-prose repair policy.
    if mode == "targeted" and not requested_target_ids:
        mode = "full"
        reason = f"{reason}；未提供有效目标节点，降级为有界全量正文修复"
    if mode == "targeted":
        reopened_node_ids = reopen_plan_nodes(
            plan,
            requested_target_ids,
            # Release directives are already node-localized by the quality
            # report.  Do not widen the scope to descendants implicitly: a
            # dependency that also needs repair must be listed explicitly in
            # the directive, otherwise its completed checkpoint is reusable.
            include_non_original_descendants=False,
            clear_scene_checkpoint=True,
            reason=reason,
        )
        # Mark only the generation/release tail as pending.  Keeping the
        # completed upstream steps intact lets the next delivery resume the
        # selected node instead of being interpreted as a full pipeline reset.
        for step_name in ("node_generation", "release_gate", "story_publish", "summary_publish", "stage_complete"):
            step = (plan.get("steps", {}) or {}).get(step_name)
            if isinstance(step, dict):
                step.update(
                    {
                        "status": "pending",
                        "output_digest": "",
                        "completed_at": "",
                        "error": reason,
                        "updated_at": _now_iso(),
                    }
                )
            step_outputs = plan.get("step_outputs", {})
            if isinstance(step_outputs, dict):
                step_outputs.pop(step_name, None)
    elif mode == "full" and rebuild_topology:
        if isinstance(plan.get("regeneration_request"), dict):
            plan["regeneration_request"]["status"] = "inactive"
            plan["regeneration_request"]["inactive_at"] = _now_iso()
        plan["topology_result"] = {}
        plan["topology_fingerprint"] = ""
        plan["nodes"] = {}
        plan["real_id_map"] = {}
        plan.pop("effect_caps", None)
    elif mode == "full":
        if isinstance(plan.get("regeneration_request"), dict):
            plan["regeneration_request"]["status"] = "inactive"
            plan["regeneration_request"]["inactive_at"] = _now_iso()
        requested_at = _now_iso()
        for node_id, entry in (plan.get("nodes", {}) or {}).items():
            if not isinstance(entry, dict):
                continue
            entry.update(
                {
                    "status": "pending",
                    "terminal_states": {},
                    "summary": "",
                    "error": reason,
                    "artifact_sha256": "",
                    "whatif_scene_checkpoint": {},
                    "soft_branch_gap_checkpoint": {},
                    "semantic_reviews": {},
                    "ending_semantic_review": {},
                    "ending_semantic_review_fingerprint": "",
                    "regeneration_count": int(entry.get("regeneration_count", 0) or 0) + 1,
                    "regenerate_requested_at": requested_at,
                    "updated_at": requested_at,
                }
            )
            reopened_node_ids.append(str(node_id))
    else:
        raise ValueError(f"不支持的发布修复模式：{mode}")

    recorded_at = _now_iso()
    plan["release_repair_round"] = repair_round
    history.append(
        {
            "round": repair_round,
            "mode": mode,
            "scheduled": True,
            "rebuild_topology": rebuild_topology,
            "target_node_ids": requested_target_ids,
            "reopened_node_ids": reopened_node_ids,
            "reason": reason,
            "repair_fingerprint": repair_fingerprint,
            "recorded_at": recorded_at,
        }
    )
    plan["updated_at"] = recorded_at
    return {
        "scheduled": True,
        "exhausted": False,
        "round": repair_round,
        "mode": mode,
        "rebuild_topology": rebuild_topology,
        "reopened_node_ids": reopened_node_ids,
    }


def mark_running_nodes_failed(plan: Dict[str, Any], error: str) -> bool:
    """Mark every in-flight checkpoint failed so operators can see the abort reason."""
    changed = False
    for topology_node_id, entry in (plan.get("nodes", {}) or {}).items():
        if isinstance(entry, dict) and entry.get("status") == "running":
            update_plan_node(
                plan,
                topology_node_id,
                status="failed",
                error=str(error),
            )
            changed = True
    return changed


def estimate_soft_branch_counts(
    topology: Iterable[Dict[str, Any]],
    chapters: list[str],
    branch_type: str = "default",
) -> list[int]:
    """Estimate branch counts using the same adaptive policy as generation.

    Original chapters use their own source text to reduce gaps in short or
    sparsely segmented chapters.  Generated What-If chapters continue to use
    the average-chapter adaptive budget, and no global branch cap is applied.
    """
    counts: list[int] = []
    chapter_count = len(chapters)
    average_chars = sum(len(chapter) for chapter in chapters) // max(chapter_count, 1)
    for node in topology or []:
        if not isinstance(node, dict):
            counts.append(0)
            continue
        node_type = node.get("type", "chapter")
        is_original = node.get("is_original", True)
        chapter_idx = node.get("source_chapter_idx")
        scene_count = 0
        if node.get("omitted_branch"):
            scene_count = 0
        elif branch_type in {"fantasy", "death_corridor"}:
            scene_count = 0
        elif is_original and isinstance(chapter_idx, int) and 0 <= chapter_idx < chapter_count:
            scene_count = plan_original_chapter_scene_count(
                node_type,
                chapter_text=chapters[chapter_idx],
            )
        elif not is_original and node_type == "chapter":
            scene_count = plan_whatif_budget(average_chars)["scene_count"]
        counts.append(max(scene_count - 1, 0))
    return counts


def _effective_scene_count(chapter_text: str, requested_scene_count: int) -> int:
    """Estimate how many natural scene units the splitter can actually use.

    ``soft_branch._split_scenes`` first prefers blank-line paragraphs and then
    falls back to non-empty lines when a source file has no blank lines.  Keep
    this lightweight mirror here so the preflight estimate and real injection
    do not budget branches for scene gaps that cannot exist in the source.
    """
    requested = max(int(requested_scene_count or 1), 1)
    text = str(chapter_text or "").strip()
    if not text:
        return 1

    paragraphs = [
        part.strip()
        for part in re.split(r"\n\s*\n", text)
        if part.strip()
    ]
    if len(paragraphs) <= requested:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) > len(paragraphs):
            paragraphs = lines
    return max(1, min(requested, len(paragraphs) or 1))


def plan_original_chapter_scene_count(
    node_type: str = "chapter",
    *,
    chapter_text: str = "",
    chapter_chars: int | None = None,
    effective_scene_count: int | None = None,
) -> int:
    """Return an adaptive scene budget for an original chapter.

    A normal chapter of roughly ten thousand characters receives three soft
    branches (four scenes).  Short chapters receive fewer branches, while
    longer chapters stay capped at three.  Explicit branch-point chapters may
    receive one additional branch, but no chapter can exceed four soft
    branches.  The final scene count is capped by the number of natural scene
    units available in the source text.

    ``chapter_chars`` and ``effective_scene_count`` are accepted for callers
    that already computed those values; ``chapter_text`` is preferred because
    it keeps the natural-scene calculation aligned with actual injection.
    """
    if node_type == "ending":
        return 0

    # Preserve the historical helper contract for callers that only provide a
    # node type.  Production chapter_branch callers now pass chapter content,
    # which activates the adaptive policy below.
    if not str(chapter_text or "").strip() and chapter_chars is None and effective_scene_count is None:
        return 5 if node_type == "branch_point" else 4

    text = str(chapter_text or "")
    chars = int(chapter_chars) if chapter_chars is not None else len(text)

    if chars <= 1200:
        target_branches = 0
        requested_scene_count = 1
    elif chars <= 4000:
        target_branches = 1
        requested_scene_count = 2
    elif chars <= 7000:
        target_branches = 2
        requested_scene_count = 3
    else:
        target_branches = 3
        requested_scene_count = 4

    # Keep high-value explicit branch points slightly richer than ordinary
    # prose, but apply the same short-chapter reduction.  A short branch point
    # gets at most one extra branch; long chapters remain capped at four.
    if node_type == "branch_point" and target_branches > 0:
        target_branches = min(4, target_branches + 1)
        requested_scene_count = target_branches + 1

    if effective_scene_count is None:
        # When only a character count is available, there is no source layout
        # from which to infer natural scene units; use the length budget and
        # let the real splitter make the final safety reduction.
        available_scenes = (
            _effective_scene_count(text, requested_scene_count)
            if text
            else requested_scene_count
        )
    else:
        available_scenes = max(int(effective_scene_count or 1), 1)
        available_scenes = min(available_scenes, requested_scene_count)

    return max(1, min(target_branches + 1, available_scenes))


def plan_whatif_budget(average_chapter_chars: int) -> Dict[str, int]:
    """Scale What-If prose to the source chapter instead of forcing long-form output."""
    average = max(int(average_chapter_chars or 0), 1)
    target_chars = max(600, min(12000, round(average * 1.5)))
    if target_chars <= 1200:
        scene_count = 1
    elif target_chars <= 3000:
        scene_count = 2
    elif target_chars <= 6000:
        scene_count = 3
    else:
        scene_count = 4
    return {
        "target_chars": target_chars,
        "scene_count": scene_count,
        "soft_branch_count": max(0, scene_count - 1),
    }


def omit_optional_topology_branch(
    topology: Iterable[Dict[str, Any]],
    topology_node_id: str,
    *,
    reason: str,
) -> Dict[str, Any]:
    """Remove one optional generated branch from graph edges while preserving mainline.

    Non-original parallel endings are optional too: when their bounded local
    generation budget is exhausted, removing only that edge keeps the original
    ending/mainline playable and avoids reopening the whole task.
    """
    items = [item for item in topology or [] if isinstance(item, dict)]
    node_id = str(topology_node_id or "").strip()
    target = next((item for item in items if str(item.get("node_id")) == node_id), None)
    if (
        not target
        or target.get("is_original", True)
        or (
            target.get("type", "chapter") == "ending"
            and target.get("is_original", True)
        )
    ):
        return {
            "omitted": False,
            "node_id": node_id,
            "reason": str(reason or ""),
            "omitted_node_ids": [],
        }

    working = deepcopy(items)
    working_by_id = {
        str(item.get("node_id")): item
        for item in working
        if item.get("node_id")
    }
    omission_reason = str(reason or "").strip() or "optional_branch_failed"
    queue = [node_id]
    omitted_node_ids: list[str] = []
    while queue:
        current_id = queue.pop(0)
        if current_id in omitted_node_ids:
            continue
        current = working_by_id.get(current_id)
        if (
            not current
            or current.get("is_original", True)
            or (
                current.get("type", "chapter") == "ending"
                and current.get("is_original", True)
            )
        ):
            return {
                "omitted": False,
                "node_id": node_id,
                "reason": "optional_branch_has_required_descendant",
                "omitted_node_ids": [],
            }
        child_ids = [str(value) for value in (current.get("next", []) or [])]
        omitted_node_ids.append(current_id)
        current["omitted_branch"] = True
        current["omission_reason"] = omission_reason
        current["next"] = []
        for item in working:
            item["next"] = [
                child_id
                for child_id in (item.get("next", []) or [])
                if str(child_id) != current_id
            ]
            if item is not current:
                item["parent_ids"] = [
                    parent_id
                    for parent_id in (item.get("parent_ids", []) or [])
                    if str(parent_id) != current_id
                ]
        for child_id in child_ids:
            child = working_by_id.get(child_id)
            if not child or child.get("parent_ids"):
                continue
            if child.get("is_original", True) or (
                child.get("type") == "ending"
                and child.get("is_original", True)
            ):
                return {
                    "omitted": False,
                    "node_id": node_id,
                    "reason": "optional_branch_has_required_descendant",
                    "omitted_node_ids": [],
                }
            queue.append(child_id)

    for original, updated in zip(items, working):
        original.clear()
        original.update(updated)
    return {
        "omitted": True,
        "node_id": node_id,
        "reason": omission_reason,
        "omitted_node_ids": omitted_node_ids,
    }


def estimate_total_soft_branches(
    topology: Iterable[Dict[str, Any]],
    chapters: list[str],
    branch_type: str = "default",
) -> int:
    """Estimate the exact branch count using the same scene policy as generation."""
    return sum(estimate_soft_branch_counts(topology, chapters, branch_type=branch_type))


def initialize_whatif_scene_checkpoint(
    existing: Optional[Dict[str, Any]],
    *,
    scene_count: int,
) -> Dict[str, Any]:
    """Normalize resumable scene state and discard invalid/out-of-range entries."""
    state = deepcopy(existing or {})
    normalized = []
    seen = set()
    for item in state.get("completed_scenes", []) or []:
        if not isinstance(item, dict):
            continue
        index = item.get("scene_index")
        content = str(item.get("content") or "").strip()
        if not isinstance(index, int) or not 0 <= index < max(int(scene_count), 0):
            continue
        if index in seen or not content:
            continue
        seen.add(index)
        normalized.append({
            "scene_index": index,
            "content": content,
            "snapshot": str(item.get("snapshot") or ""),
            "accumulated_summary": str(item.get("accumulated_summary") or ""),
        })
    normalized.sort(key=lambda item: item["scene_index"])
    contiguous = []
    for expected, item in enumerate(normalized):
        if item["scene_index"] != expected:
            break
        contiguous.append(item)
    state["scene_count"] = max(int(scene_count), 0)
    state["completed_scenes"] = contiguous
    state["status"] = "completed" if len(contiguous) >= state["scene_count"] else "running"
    return state


def next_whatif_scene_index(state: Dict[str, Any]) -> int:
    """Return the first scene index that has not been checkpointed."""
    return len((state or {}).get("completed_scenes", []) or [])


def record_whatif_scene_checkpoint(
    state: Dict[str, Any],
    *,
    scene_index: int,
    content: str,
    snapshot: str = "",
    accumulated_summary: str = "",
) -> Dict[str, Any]:
    """Append one contiguous completed scene to a resumable checkpoint."""
    expected = next_whatif_scene_index(state)
    if scene_index != expected:
        raise ValueError(f"scene checkpoint must be contiguous: expected {expected}, got {scene_index}")
    text = str(content or "").strip()
    if not text:
        raise ValueError("scene checkpoint content cannot be empty")
    state.setdefault("completed_scenes", []).append({
        "scene_index": scene_index,
        "content": text,
        "snapshot": str(snapshot or ""),
        "accumulated_summary": str(accumulated_summary or ""),
    })
    state["status"] = (
        "completed"
        if next_whatif_scene_index(state) >= int(state.get("scene_count") or 0)
        else "running"
    )
    return state


def join_whatif_scene_content(state: Dict[str, Any]) -> str:
    """Join checkpointed scenes exactly as the final chapter writer does."""
    return "\n\n".join(
        str(item.get("content") or "").strip()
        for item in (state or {}).get("completed_scenes", []) or []
        if str(item.get("content") or "").strip()
    )


def _strip_non_prose_tail(content: str) -> str:
    """Remove trailing diagnostic comments before checking the prose ending."""
    text = str(content or "").strip()
    previous = None
    while text != previous:
        previous = text
        text = re.sub(r"<!--.*?-->\s*$", "", text, flags=re.DOTALL).rstrip()
        text = re.sub(r"```\s*$", "", text).rstrip()
    return text


def _delimiter_issue(text: str) -> str:
    for opening, closing, label in _PAIRED_DELIMITERS:
        if text.count(opening) != text.count(closing):
            return f"正文{label}未闭合：{opening}{closing} 数量不一致"
    if text.count('"') % 2:
        return "正文英文双引号未闭合"
    return ""


def repair_unbalanced_prose_delimiters(content: str) -> Tuple[str, bool, str]:
    """Safely close one directional prose quote opened in the final paragraph."""
    original = str(content or "")
    text = original.rstrip()
    trailing = original[len(text):]
    if not text:
        return original, False, "正文为空，无法安全修复"

    issues = []
    for opening, closing, label in _PAIRED_DELIMITERS:
        opening_count = text.count(opening)
        closing_count = text.count(closing)
        if opening_count != closing_count:
            issues.append((opening, closing, label, opening_count, closing_count))
    if text.count('"') % 2:
        issues.append(('"', '"', "英文双引号", text.count('"'), text.count('"')))

    if not issues:
        return original, True, "正文符号已闭合，无需修复"
    if len(issues) != 1:
        return original, False, "存在多组未闭合符号，无法安全修复"

    opening, closing, label, opening_count, closing_count = issues[0]
    if label != "引号" or opening_count != closing_count + 1:
        return original, False, f"正文{label}不是唯一缺失闭符号，无法安全修复"

    unmatched_openings = []
    for index, char in enumerate(text):
        if char == opening:
            unmatched_openings.append(index)
        elif char == closing:
            if not unmatched_openings:
                return original, False, "闭引号出现在开引号之前，无法安全修复"
            unmatched_openings.pop()
    if len(unmatched_openings) != 1:
        return original, False, "无法唯一定位未闭合引号，无法安全修复"

    final_paragraph_match = list(re.finditer(r"\n\s*\n", text))
    final_paragraph_start = (
        final_paragraph_match[-1].end() if final_paragraph_match else 0
    )
    if unmatched_openings[0] < final_paragraph_start:
        return original, False, "未闭合引号不在末段，拒绝自动补齐"

    return text + closing + trailing, True, f"已补齐末段唯一缺失的闭引号 {closing}"


def validate_title_completeness(title: str) -> Tuple[bool, str]:
    """Reject clipped generated titles while allowing normal chapter numbering."""
    text = str(title or "").strip()
    if not text:
        return False, "标题为空"
    for opening, closing, _label in _PAIRED_DELIMITERS[:5]:
        if text.count(opening) != text.count(closing):
            return False, f"标题括号或书名号未闭合：{opening}{closing}"
    semantic_tail = text.rstrip("，。；：,.!?！？…" + _CLOSING_PUNCTUATION).strip()
    if semantic_tail and semantic_tail[-1] in _DANGLING_TITLE_ENDINGS:
        return False, f"标题疑似残句：以“{semantic_tail[-1]}”结尾"
    if semantic_tail.endswith(_DANGLING_TITLE_SUFFIXES):
        suffix = next(
            item for item in _DANGLING_TITLE_SUFFIXES if semantic_tail.endswith(item)
        )
        return False, f"标题疑似残句：以“{suffix}”结尾"
    return True, ""


def validate_text_ending_completeness(content: str) -> Tuple[bool, str]:
    """Validate the final prose sentence after removing trailing metadata comments."""
    text = _strip_non_prose_tail(content)
    if not text:
        return False, "正文末句为空"
    delimiter_issue = _delimiter_issue(text)
    if delimiter_issue:
        return False, delimiter_issue
    semantic_tail = text.rstrip(_CLOSING_PUNCTUATION).rstrip()
    if not semantic_tail:
        return False, "正文末句为空"
    final_char = semantic_tail[-1]
    if final_char in _DANGLING_SENTENCE_ENDINGS:
        return False, f"正文末句疑似截断：以“{final_char}”结尾"
    if final_char not in _STRONG_SENTENCE_ENDINGS:
        return False, f"正文末句缺少完整结束标点：当前以“{final_char}”结尾"
    return True, ""


def validate_generated_content(content: str, min_chars: int = 300) -> Tuple[bool, str]:
    """Reject empty, truncated, placeholder, or instruction-leaking generated prose."""
    text = (content or "").strip()
    if len(text) < max(1, int(min_chars)):
        return False, f"正文长度不足：{len(text)} < {min_chars}"
    lowered = text.lower()
    matched = [marker for marker in _PLACEHOLDER_MARKERS if marker in lowered]
    if matched:
        return False, f"正文包含占位或内部指令文本：{matched[0]}"
    ending_ok, ending_reason = validate_text_ending_completeness(text)
    if not ending_ok:
        return False, ending_reason
    return True, ""


def prepare_generated_content_for_release(
    content: str,
    min_chars: int = 300,
) -> Tuple[str, bool, str, bool]:
    """Validate generated prose, applying only a deterministic safe delimiter repair."""
    candidate = str(content or "")
    content_ok, content_reason = validate_generated_content(candidate, min_chars=min_chars)
    if content_ok:
        return candidate, True, "", False
    if "未闭合" not in content_reason:
        return candidate, False, content_reason, False

    repaired, repaired_ok, _repair_reason = repair_unbalanced_prose_delimiters(candidate)
    if not repaired_ok:
        return candidate, False, content_reason, False
    repaired_ok, repaired_reason = validate_generated_content(repaired, min_chars=min_chars)
    if not repaired_ok:
        return repaired, False, repaired_reason, repaired != candidate
    return repaired, True, "", repaired != candidate


def _as_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"false", "0", "no", "dead", "已死", "死亡"}:
            return False
        if normalized in {"true", "1", "yes", "alive", "活", "存活"}:
            return True
    return default


def _as_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _as_nonnegative_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return max(0, int(default))


def _as_confidence(value: Any, default: float = 1.0) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return float(default)


def _is_unknown_state_value(value: Any) -> bool:
    normalized = _as_text(value).lower()
    if normalized in {
        "",
        "none",
        "null",
        "n/a",
        "na",
        "not mentioned",
        "not specified",
        "未明确",
        "无",
        "unknown",
        "未知",
        "unspecified",
        "未提及",
        "本章未提及",
        "本章未提及，沿用上一节点状态",
        "本章未提及，沿用上一章节状态",
        "未出场",
        "位置未明",
        "当前未说明",
        "未说明",
    }:
        return True
    return normalized.startswith(("本章未提及", "本章未说明", "未提及："))


_NARRATIVE_STATE_FIELDS = (
    "presence_type",
    "reality_layer",
    "timeline",
    "location",
    "identity_status",
)

_RECONSTRUCTED_LAYERS = {
    "dream",
    "therapy_reconstruction",
    "rebirth",
    "hallucination",
}
_LOCAL_MEMORY_LAYERS = {"memory", "hallucination"}
_INACTIVE_PARTICIPATION = {"mentioned", "absent", "offscreen", "unknown"}
_LIFE_STATUSES = {
    "alive_confirmed",
    "death_reported",
    "death_suspected",
    "death_confirmed",
    "fake_death_revealed",
    "revived_confirmed",
    "unknown",
}
_UNCERTAIN_DEATH_STATUSES = {"death_reported", "death_suspected", "unknown"}
_LIVING_LIFE_STATUSES = {
    "alive_confirmed",
    "fake_death_revealed",
    "revived_confirmed",
}
_STRONG_DEATH_EVIDENCE = {
    "direct",
    "witnessed",
    "body_verified",
    "official_record",
    "explicit_scene",
}
_STRICT_DEATH_EVIDENCE = {
    "direct",
    "witnessed",
    "body_verified",
    "official_record",
    "explicit_scene",
}


def _canonical_life_status(
    value: Any,
    state: Optional[Dict[str, Any]] = None,
    default: str = "unknown",
) -> str:
    text = _as_text(value).lower()
    aliases = {
        "alive": "alive_confirmed",
        "living": "alive_confirmed",
        "存活": "alive_confirmed",
        "确认存活": "alive_confirmed",
        "传闻死亡": "death_reported",
        "被传死亡": "death_reported",
        "疑似死亡": "death_suspected",
        "生死未卜": "death_suspected",
        "dead": "death_confirmed",
        "死亡": "death_confirmed",
        "确认死亡": "death_confirmed",
        "假死": "fake_death_revealed",
        "假死揭示": "fake_death_revealed",
        "复活": "revived_confirmed",
        "确认复活": "revived_confirmed",
        "未知": "unknown",
    }
    normalized = aliases.get(text, text)
    if normalized in _LIFE_STATUSES:
        return normalized
    state = state if isinstance(state, dict) else {}
    if "outer_alive" in state or "alive" in state:
        return (
            "alive_confirmed"
            if _as_bool(state.get("outer_alive", state.get("alive")), default=True)
            else "death_confirmed"
        )
    return default if default in _LIFE_STATUSES else "unknown"


def _canonical_death_evidence(value: Any) -> str:
    text = _as_text(value).lower()
    aliases = {
        "传闻": "hearsay",
        "转述": "hearsay",
        "间接": "indirect",
        "旁证": "indirect",
        "直接": "direct",
        "目击": "witnessed",
        "亲眼目睹": "witnessed",
        "遗体确认": "body_verified",
        "尸体确认": "body_verified",
        "官方记录": "official_record",
        "正文明确死亡": "explicit_scene",
        "明确揭示": "explicit_reveal",
        "无": "none",
        "未知": "unknown",
    }
    return aliases.get(text, text) or "unknown"


def _normalize_death_sources(value: Any) -> list:
    if not isinstance(value, list):
        return []
    normalized = []
    for item in value:
        if isinstance(item, dict):
            cleaned = {
                str(key): item_value
                for key, item_value in item.items()
                if _as_text(item_value)
            }
            if cleaned:
                normalized.append(cleaned)
        elif _as_text(item):
            normalized.append({"claim": _as_text(item)})
    return normalized


def _death_is_strongly_confirmed(
    evidence_level: str,
    confidence: float,
    contract: Dict[str, Any],
) -> bool:
    strict = _as_text(contract.get("branch_type")).lower() == "death_corridor"
    allowed = _STRICT_DEATH_EVIDENCE if strict else _STRONG_DEATH_EVIDENCE
    threshold = 0.85 if strict else 0.75
    return evidence_level in allowed and confidence >= threshold


def _fake_death_is_resolved(current: Dict[str, Any], evidence_level: str) -> bool:
    resolution = _as_text(current.get("previous_death_resolution")).lower()
    return (
        resolution in {"fake_death", "假死", "misreported_death", "误报死亡"}
        and evidence_level in {"explicit_reveal", "direct", "witnessed"}
    )


def _resurrection_is_authorized(
    current: Dict[str, Any],
    contract: Dict[str, Any],
) -> bool:
    return (
        _as_text(contract.get("branch_type")).lower() == "fantasy"
        and bool(contract.get("allow_physical_resurrection"))
        and bool(_as_text(current.get("resurrection_rule_id")))
    )


def _canonical_reality_layer(value: Any) -> str:
    text = _as_text(value).lower()
    aliases = {
        "现实": "reality",
        "现实世界": "reality",
        "梦": "dream",
        "梦境": "dream",
        "治疗重构": "therapy_reconstruction",
        "治疗重建": "therapy_reconstruction",
        "重生": "rebirth",
        "回忆": "memory",
        "记忆": "memory",
        "幻觉": "hallucination",
    }
    return aliases.get(text, text)


def _semantic_reality_fields(
    current: Dict[str, Any],
    previous: Optional[Dict[str, Any]] = None,
    narrative_contract: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    previous = previous if isinstance(previous, dict) else {}
    contract = narrative_contract if isinstance(narrative_contract, dict) else {}
    layer = _canonical_reality_layer(current.get("reality_layer"))
    if _is_unknown_state_value(layer):
        layer = _canonical_reality_layer(previous.get("reality_layer"))
    presence = _as_text(current.get("presence_type")).lower()
    if _is_unknown_state_value(presence):
        presence = _as_text(previous.get("presence_type")).lower()

    explicit_domain = _as_text(current.get("reality_domain")).lower()
    previous_domain = _as_text(previous.get("reality_domain")).lower()
    if explicit_domain:
        domain = explicit_domain
    elif layer == "reality":
        domain = "physical_reality"
    elif layer in _RECONSTRUCTED_LAYERS:
        domain = "reconstructed_world"
    elif layer in _LOCAL_MEMORY_LAYERS:
        domain = previous_domain or _as_text(
            contract.get("primary_reality_domain")
        ).lower() or "unknown"
    else:
        domain = previous_domain or "unknown"

    explicit_type = _as_text(current.get("reconstruction_type")).lower()
    previous_type = _as_text(previous.get("reconstruction_type")).lower()
    contract_type = _as_text(contract.get("primary_reconstruction_type")).lower()
    if domain != "reconstructed_world":
        reconstruction_type = "none"
    elif contract_type and contract_type != "unknown":
        reconstruction_type = contract_type
    elif explicit_type:
        reconstruction_type = explicit_type
    elif layer in {"dream", "therapy_reconstruction", "rebirth"}:
        reconstruction_type = layer
    else:
        reconstruction_type = previous_type or "unknown"

    explicit_frame = _as_text(current.get("local_frame")).lower()
    if explicit_frame:
        local_frame = explicit_frame
    elif presence in {"memory", "hallucination"}:
        local_frame = presence
    elif layer in _LOCAL_MEMORY_LAYERS:
        local_frame = layer
    else:
        local_frame = "present"

    explicit_participation = _as_text(current.get("participation")).lower()
    if _is_unknown_state_value(explicit_participation):
        explicit_participation = _as_text(previous.get("participation")).lower()
    if presence in {"absent", "offscreen", "unknown"}:
        # Extraction occasionally emits ``presence_type=absent`` together with
        # the stale default ``participation=active``.  Presence is the safer
        # source of truth for a character that did not act in this scene.
        if explicit_participation == "mentioned":
            participation = "mentioned"
        elif presence in {"absent", "offscreen"}:
            participation = "offscreen"
        else:
            participation = "unknown"
    elif presence in {"mentioned", "memory"} and explicit_participation in {
        "",
        "active",
        "physical",
    }:
        participation = "mentioned" if presence == "mentioned" else "offscreen"
    elif explicit_participation:
        participation = explicit_participation
    elif presence in {"unknown", "absent"}:
        participation = presence
    else:
        mention_text = " ".join(
            _as_text(current.get(key))
            for key in ("condition", "timeline", "location", "note")
        )
        if presence == "memory" and re.search(r"仅|提及|日记|回忆|电话", mention_text):
            participation = "mentioned"
        else:
            participation = "active"

    return {
        "reality_domain": domain,
        "reconstruction_type": reconstruction_type,
        "local_frame": local_frame,
        "participation": participation,
    }


def _normalized_state_field(
    current: Dict[str, Any],
    previous: Dict[str, Any],
    field: str,
    default: str = "",
) -> str:
    """Resolve a terminal-state field with inheritance for unknown values.

    Extractors commonly emit an explicit empty/unknown marker when a character
    is not present in the final scene.  That marker is not new narrative
    evidence, so prefer a known value from the inherited state before falling
    back to the caller's default.
    """
    current_value = _as_text(current.get(field))
    if current_value and not _is_unknown_state_value(current_value):
        return current_value
    previous_value = _as_text(previous.get(field))
    if previous_value and not _is_unknown_state_value(previous_value):
        return previous_value
    return _as_text(default)


def normalize_terminal_states(
    candidate_states: Optional[Dict[str, Any]],
    inherited_states: Optional[Dict[str, Any]],
    narrative_contract: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Dict[str, Any]], list]:
    """Normalize evidence-based life state while preserving legacy booleans."""
    candidate = candidate_states if isinstance(candidate_states, dict) else {}
    inherited = inherited_states if isinstance(inherited_states, dict) else {}
    contract = narrative_contract if isinstance(narrative_contract, dict) else {}
    normalized: Dict[str, Dict[str, Any]] = {}
    issues = []

    for name in sorted(set(inherited) | set(candidate)):
        previous = inherited.get(name) if isinstance(inherited.get(name), dict) else {}
        current = candidate.get(name) if isinstance(candidate.get(name), dict) else {}
        if not current and previous:
            current = previous

        previous_status = _canonical_life_status(
            previous.get("life_status"), previous, "alive_confirmed"
        )
        previous_outer_alive = (
            False
            if previous_status == "death_confirmed"
            else True
            if previous_status
            in _LIVING_LIFE_STATUSES | {"death_reported", "death_suspected"}
            else _as_bool(
                previous.get("outer_alive", previous.get("alive")), default=True
            )
        )

        explicit_life_status = "life_status" in current
        if explicit_life_status:
            life_status = _canonical_life_status(
                current.get("life_status"), current, previous_status
            )
        elif "outer_alive" in current or "alive" in current:
            requested_outer = _as_bool(
                current.get("outer_alive", current.get("alive")),
                default=previous_outer_alive,
            )
            life_status = (
                previous_status
                if requested_outer == previous_outer_alive
                else "alive_confirmed"
                if requested_outer
                else "death_confirmed"
            )
        else:
            life_status = previous_status

        life_confidence = _as_confidence(
            current.get(
                "life_status_confidence",
                current.get(
                    "state_confidence",
                    previous.get("life_status_confidence", 1.0),
                ),
            )
        )
        death_evidence = _canonical_death_evidence(
            current.get(
                "death_evidence_level",
                previous.get("death_evidence_level", "unknown"),
            )
        )
        death_sources = _normalize_death_sources(
            current.get("death_sources", previous.get("death_sources", []))
        )
        previous_death_resolution = _as_text(
            current.get(
                "previous_death_resolution",
                previous.get("previous_death_resolution", ""),
            )
        )
        resurrection_rule_id = _as_text(
            current.get(
                "resurrection_rule_id",
                previous.get("resurrection_rule_id", ""),
            )
        )

        death_confirmation_requires_evidence = explicit_life_status or (
            _as_text(current.get("state_source")).lower() == "llm"
            and previous_status != "death_confirmed"
        )
        if (
            death_confirmation_requires_evidence
            and life_status == "death_confirmed"
            and not _death_is_strongly_confirmed(
                death_evidence, life_confidence, contract
            )
        ):
            downgraded = (
                "death_reported" if death_evidence == "hearsay" else "death_suspected"
            )
            issues.append(
                f"角色{name}传闻证据不足以确认死亡，已降级为{downgraded}"
            )
            life_status = downgraded

        presence_type = _as_text(current.get("presence_type")).lower()
        reality_layer = _canonical_reality_layer(current.get("reality_layer"))
        nonphysical_presence = presence_type in {
            "hallucination",
            "memory",
            "dream",
            "corpse",
            "absent",
        }
        nonphysical_layer = reality_layer in {
            "dream",
            "therapy_reconstruction",
            "memory",
            "hallucination",
            "other",
        }

        current_outer_alive = previous_outer_alive
        if previous_status == "death_confirmed":
            if life_status == "fake_death_revealed" and _fake_death_is_resolved(
                current, death_evidence
            ):
                current_outer_alive = True
            elif life_status == "revived_confirmed" and _resurrection_is_authorized(
                current, contract
            ):
                current_outer_alive = True
            elif life_status != "death_confirmed":
                life_status = "death_confirmed"
                current_outer_alive = False
                if nonphysical_presence or nonphysical_layer:
                    issues.append(
                        f"角色{name}仅在非现实层出现，不视为复活，已保持外层死亡"
                    )
                else:
                    issues.append(f"角色{name}出现无依据复活，已回退到继承末态")
                    current = previous
            else:
                current_outer_alive = False
        elif life_status == "death_confirmed":
            current_outer_alive = False
        elif life_status in _UNCERTAIN_DEATH_STATUSES:
            # A report or suspicion is information about death, not proof of death.
            current_outer_alive = previous_outer_alive
        elif life_status == "fake_death_revealed":
            if _fake_death_is_resolved(current, death_evidence):
                current_outer_alive = True
            else:
                issues.append(f"角色{name}假死揭示缺少明确依据，保持既有生命状态")
                life_status = previous_status
                current_outer_alive = previous_outer_alive
        elif life_status == "revived_confirmed":
            if _resurrection_is_authorized(current, contract):
                current_outer_alive = True
            else:
                issues.append(f"角色{name}复活缺少可验证世界规则，保持既有生命状态")
                life_status = previous_status
                current_outer_alive = previous_outer_alive
        else:
            current_outer_alive = True

        explicit_outer_state = "outer_alive" in current
        current_layer_alive = _as_bool(
            current.get("current_layer_alive"),
            default=current_outer_alive,
        )
        if not current_outer_alive and not explicit_outer_state and "current_layer_alive" not in current:
            current_layer_alive = False
        default_presence = "physical" if current_outer_alive else "absent"
        presence_value = _normalized_state_field(
            current, previous, "presence_type", default_presence
        )
        if (
            _is_unknown_state_value(current.get("presence_type"))
            and _is_unknown_state_value(previous.get("presence_type"))
        ):
            presence_value = "unknown"
        semantic_fields = _semantic_reality_fields(
            current,
            previous,
            narrative_contract,
        )
        normalized[name] = {
            # alive remains a backward-compatible alias for outer_alive.
            "alive": bool(current_outer_alive),
            "outer_alive": bool(current_outer_alive),
            "current_layer_alive": bool(current_layer_alive),
            "life_status": life_status,
            "life_status_confidence": life_confidence,
            "death_evidence_level": death_evidence,
            "death_sources": death_sources,
            "previous_death_resolution": previous_death_resolution,
            "resurrection_rule_id": resurrection_rule_id,
            "condition": _as_text(current.get("condition", previous.get("condition", ""))),
            "presence_type": presence_value,
            "reality_layer": (
                _normalized_state_field(current, previous, "reality_layer", "unknown")
                if not _is_unknown_state_value(current.get("reality_layer"))
                or not _is_unknown_state_value(previous.get("reality_layer"))
                else "unknown"
            ),
            "timeline": _normalized_state_field(current, previous, "timeline"),
            "location": _normalized_state_field(current, previous, "location"),
            "identity_status": _normalized_state_field(
                current, previous, "identity_status"
            ),
            "note": _as_text(current.get("note", previous.get("note", ""))),
            **semantic_fields,
            "state_confidence": _as_confidence(
                current.get(
                    "state_confidence",
                    previous.get("state_confidence", 1.0),
                ),
            ),
            "evidence": _as_text(current.get("evidence", previous.get("evidence", ""))),
            "state_source": _as_text(
                current.get("state_source", previous.get("state_source", "llm"))
            ) or "llm",
            "offscreen_streak": _as_nonnegative_int(
                current.get(
                    "offscreen_streak",
                    previous.get("offscreen_streak", 0),
                )
            ),
        }
    return normalized, issues


def validate_convergence_states(
    branch_states: Optional[Dict[str, Any]],
    sibling_states: Optional[Dict[str, Any]],
) -> Tuple[bool, list]:
    """Separate hard contradictions from state gaps and bridgeable differences.

    Terminal-state extraction is partial by design: a character can be off-screen,
    mentioned only in dialogue, or omitted by an otherwise valid model response.
    A missing record on one side is therefore returned as a diagnostic warning and
    treated as inheritable state, not as a convergence failure.  An empty record on
    both sides is ignored.  Hard conflicts still require two concrete state records
    and are limited to explicit life, reality-domain, presence, or identity
    contradictions.
    """
    result = evaluate_convergence_states(branch_states, sibling_states)
    return bool(result["compatible"]), list(result["issues"])


def evaluate_convergence_states(
    branch_states: Optional[Dict[str, Any]],
    sibling_states: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Return a layered convergence decision for a What-If terminal state.

    ``issues`` is retained for backwards-compatible diagnostics.  Callers that
    decide whether an optional branch must be discarded should use
    ``hard_issues`` only; missing/unknown fields and normal time/place changes
    belong to ``warnings`` or ``bridge_requirements``.
    """
    branch = branch_states if isinstance(branch_states, dict) else {}
    sibling = sibling_states if isinstance(sibling_states, dict) else {}
    issues: list[str] = []
    hard_issues: list[str] = []
    warnings: list[str] = []
    bridge_requirements: list[str] = []

    def _identity_conflicts(left_value: str, right_value: str) -> bool:
        if _is_unknown_state_value(left_value) or _is_unknown_state_value(right_value):
            return False
        left_normalized = re.sub(r"[\s，。！？、,.!?·・:：]", "", left_value)
        right_normalized = re.sub(r"[\s，。！？、,.!?·・:：]", "", right_value)
        if not left_normalized or not right_normalized:
            return False
        if left_normalized in right_normalized or right_normalized in left_normalized:
            return False
        # Identity descriptions are often different levels of detail (for
        # example, “班上女生” vs “同班同学”).  Treat only explicit impossible
        # identity changes as hard conflicts; fuzzy similarity is diagnostic,
        # not a release gate.
        death_markers = ("逝者", "尸体", "死者", "亡魂", "已死亡", "遗体")
        if any(marker in left_normalized for marker in death_markers) != any(
            marker in right_normalized for marker in death_markers
        ):
            return True
        reconstructed_markers = ("重构", "梦境", "幻觉", "治疗")
        if any(marker in left_normalized for marker in reconstructed_markers) != any(
            marker in right_normalized for marker in reconstructed_markers
        ):
            return False
        return False

    def _semantic(state: Dict[str, Any]) -> Dict[str, str]:
        semantic = _semantic_reality_fields(state)
        for key in semantic:
            explicit = _as_text(state.get(key)).lower()
            if explicit:
                semantic[key] = explicit
        return semantic

    def _is_inactive(state: Dict[str, Any]) -> bool:
        presence = _as_text(state.get("presence_type")).lower()
        participation = _semantic(state).get("participation")
        return presence in _INACTIVE_PARTICIPATION or participation in _INACTIVE_PARTICIPATION

    def _life_status(state: Dict[str, Any]) -> str:
        return _canonical_life_status(state.get("life_status"), state)

    def _life_state_is_confirmed(state: Dict[str, Any]) -> bool:
        status = _life_status(state)
        confidence = _as_confidence(
            state.get(
                "life_status_confidence",
                state.get("state_confidence", 1.0),
            )
        )
        return status in _LIVING_LIFE_STATUSES | {"death_confirmed"} and confidence >= 0.7

    def _is_hard_conflict(
        field: str,
        left_value: Any,
        right_value: Any,
        left_state: Dict[str, Any],
        right_state: Dict[str, Any],
    ) -> bool:
        if field == "alive":
            if _is_inactive(left_state) or _is_inactive(right_state):
                return False
            if not (
                _life_state_is_confirmed(left_state)
                and _life_state_is_confirmed(right_state)
            ):
                return False
            return (
                _life_status(left_state) == "death_confirmed"
            ) != (
                _life_status(right_state) == "death_confirmed"
            )
        if _is_inactive(left_state) or _is_inactive(right_state):
            return False
        left_text = _as_text(left_value).lower()
        right_text = _as_text(right_value).lower()
        if field == "presence_type":
            physical = {"physical", "corpse"}
            nonphysical = {"hallucination", "memory", "dream", "therapy_reconstruction", "rebirth"}
            if left_text in {"", "unknown", "absent", "offscreen", "mentioned"} or right_text in {
                "", "unknown", "absent", "offscreen", "mentioned"
            }:
                return False
            return (
                (left_text in physical and right_text in nonphysical)
                or (right_text in physical and left_text in nonphysical)
                or {left_text, right_text} == {"physical", "corpse"}
            )
        if field == "reality_layer":
            left_domain = _semantic(left_state).get("reality_domain")
            right_domain = _semantic(right_state).get("reality_domain")
            return (
                left_domain not in {"", "unknown"}
                and right_domain not in {"", "unknown"}
                and left_domain != right_domain
            )
        if field == "identity_status":
            return _identity_conflicts(_as_text(left_value), _as_text(right_value))
        # Timeline and location naturally move between the end of a What-If and
        # the next original chapter. They require a bridge, not a terminal branch.
        return False

    for name in sorted(set(branch) | set(sibling)):
        raw_left = branch.get(name)
        raw_right = sibling.get(name)
        left = raw_left if isinstance(raw_left, dict) else {}
        right = raw_right if isinstance(raw_right, dict) else {}
        left_missing = not left
        right_missing = not right
        if left_missing or right_missing:
            # Terminal extraction is intentionally conservative: a character
            # may be absent because the scene only mentions other characters,
            # keeps them off-screen, or the extractor returned an incomplete
            # result.  Absence on one side is therefore not evidence of a
            # contradiction.  The downstream writer can inherit the known
            # state and bridge any required transition.  Only compare fields
            # when both sides have an actual state record.
            if not (left_missing and right_missing):
                present_side = "What-If" if not left_missing else "主线"
                issues.append(
                    f"{name}.state 缺失（{present_side}未抽取，收束时继承已有状态）"
                )
                warnings.append(issues[-1])
            continue
        left_life_status = _life_status(left)
        right_life_status = _life_status(right)
        if left_life_status != right_life_status:
            issue = (
                f"{name}.life_status 不一致：What-If={left_life_status!r}，"
                f"主线={right_life_status!r}"
            )
            issues.append(issue)
            left_confidence = _as_confidence(
                left.get("life_status_confidence", left.get("state_confidence", 1.0))
            )
            right_confidence = _as_confidence(
                right.get("life_status_confidence", right.get("state_confidence", 1.0))
            )
            # A mention/memory/off-screen record and low-confidence life
            # extraction are not authoritative evidence of a resurrection or
            # death.  Keep the discrepancy for diagnostics, but do not let it
            # discard an otherwise usable optional branch.
            inactive_or_uncertain = (
                _is_inactive(left)
                or _is_inactive(right)
                or left_confidence < 0.7
                or right_confidence < 0.7
                or left_life_status in _UNCERTAIN_DEATH_STATUSES
                or right_life_status in _UNCERTAIN_DEATH_STATUSES
            )
            if inactive_or_uncertain:
                warnings.append(issue)
            else:
                hard_issues.append(issue)
        for field in ("alive",) + _NARRATIVE_STATE_FIELDS:
            if field == "alive":
                left_value = _as_bool(left.get(field), default=True)
                right_value = _as_bool(right.get(field), default=True)
            else:
                left_value = _as_text(left.get(field))
                right_value = _as_text(right.get(field))
                if _is_unknown_state_value(left_value) or _is_unknown_state_value(right_value):
                    continue
                if not left_value and not right_value:
                    continue
            if left_value != right_value:
                issue = (
                    f"{name}.{field} 不一致：What-If={left_value!r}，主线={right_value!r}"
                )
                issues.append(issue)
                if _is_hard_conflict(field, left_value, right_value, left, right):
                    hard_issues.append(issue)
                elif field in {"timeline", "location", "presence_type"}:
                    bridge_requirements.append(issue)
                else:
                    warnings.append(issue)
    return {
        "compatible": not hard_issues,
        "issues": issues,
        "hard_issues": hard_issues,
        "warnings": warnings,
        "bridge_requirements": bridge_requirements,
    }


def classify_convergence_repair(
    state_issues: Iterable[Any] | None,
    *,
    transition_issues: Iterable[Any] | None = None,
) -> str:
    """Choose the smallest safe repair for a What-If convergence failure."""
    states = [str(item) for item in (state_issues or []) if str(item).strip()]
    transitions = [
        str(item) for item in (transition_issues or []) if str(item).strip()
    ]
    joined_states = "；".join(states).lower()
    joined_transitions = "；".join(transitions).lower()

    if any(
        marker in joined_states
        for marker in (
            ".alive",
            "death_confirmed",
            "无依据复活",
            "永久身份",
            "现实层活人",
        )
    ):
        return "terminal"
    if (
        "reality_layer" in joined_states
        and "'reality'" in joined_states
        and any(
            marker in joined_states
            for marker in ("therapy_reconstruction", "dream", "rebirth")
        )
    ):
        return "terminal"

    structural_markers = (
        "动作链",
        "脑补",
        "出发",
        "到达",
        "住宿",
        "转场",
        "地点转换",
        "时间推进缺失",
        "关键过程",
    )
    if transitions and (
        len(transitions) >= 3
        or any(marker in joined_transitions for marker in structural_markers)
    ):
        return "bridge_scene"

    semantic_only_fields = (
        "reality_layer",
        "reconstruction_type",
        "local_frame",
        "participation",
    )
    if states and all(
        any(field in issue for field in semantic_only_fields) for issue in states
    ):
        return "normalize_only"
    if transitions:
        return "tail_rewrite"
    if len(states) > 2:
        return "regenerate_last_scene"
    return "tail_rewrite"


def escalate_state_repair_strategy(strategy: str, *, attempt: int) -> str:
    """Give hard state conflicts two full-scene rewrites before terminal downgrade."""
    normalized = str(strategy or "tail_rewrite").strip().lower()
    if normalized == "terminal" and int(attempt) < 2:
        return "regenerate_last_scene"
    if int(attempt) >= 1 and normalized not in {"normalize_only", "terminal"}:
        return "regenerate_last_scene"
    return normalized


def mark_incompatible_whatif_terminal(
    topology_node: Dict[str, Any],
    issues: Iterable[str],
) -> Dict[str, Any]:
    """Fail closed: an incompatible What-If becomes terminal instead of bad merge."""
    topology_node["next"] = []
    topology_node["terminal_branch"] = True
    topology_node["merge_policy"] = "terminal"
    topology_node["convergence_status"] = "terminal"
    topology_node["convergence_issues"] = [str(issue) for issue in issues]
    return topology_node


def should_force_whatif_convergence(
    topology_node: Dict[str, Any],
    choices: Iterable[Any] | None,
) -> bool:
    """Return whether the legacy safety convergence may repair an empty What-If edge."""
    if not isinstance(topology_node, dict):
        return False
    if topology_node.get("terminal_branch") or topology_node.get("convergence_status") == "terminal":
        return False
    return not list(choices or [])


def build_route_metadata(
    topology: Iterable[Dict[str, Any]],
    node_id_map: Dict[str, str],
    variable_defs: Iterable[Dict[str, Any]],
) -> Tuple[Dict[str, int], Dict[str, Dict[str, Any]]]:
    """Build initial variables and child-id keyed ending routes without overwriting."""
    player_variables = {
        item["key"]: int(item.get("init", 50))
        for item in variable_defs or []
        if isinstance(item, dict) and item.get("key")
    }
    routes: Dict[str, Dict[str, Any]] = {}
    for node in topology or []:
        if not isinstance(node, dict) or node.get("type") != "ending":
            continue
        variable = node.get("branch_variable")
        threshold = node.get("branch_threshold")
        real_id = node_id_map.get(node.get("node_id"))
        if not variable or threshold is None or not real_id:
            continue
        player_variables.setdefault(variable, 50)
        routes[real_id] = {
            "variable": variable,
            "threshold": int(threshold),
        }
        if node.get("branch_ratio_threshold") is not None:
            routes[real_id]["ratio_threshold"] = float(
                node["branch_ratio_threshold"]
            )
        if node.get("branch_margin_ratio") is not None:
            routes[real_id]["margin_ratio"] = float(
                node["branch_margin_ratio"]
            )
        if node.get("effective_choice_count") is not None:
            routes[real_id]["effective_choice_count"] = int(
                node["effective_choice_count"]
            )
        if node.get("minimum_effective_choices") is not None:
            routes[real_id]["minimum_effective_choices"] = int(
                node["minimum_effective_choices"]
            )
        if node.get("route_enabled") is not None:
            routes[real_id]["route_enabled"] = bool(node["route_enabled"])
        required_flags = []
        for flag in node.get("legacy_requirements", []) or []:
            clean_flag = str(flag or "").strip()
            if clean_flag and clean_flag not in required_flags:
                required_flags.append(clean_flag)
        if required_flags:
            routes[real_id]["required_legacy_flags"] = required_flags
    return player_variables, routes


def validate_release_integrity(
    json_data: Dict[str, Any],
    saved_md_ids: Iterable[str],
) -> Tuple[bool, list]:
    """Validate the publishable JSON/MD set before the final database write."""
    errors = []
    # Content warnings are optional for historical trees, but a newly emitted
    # field must be structurally complete before it can reach the player.  The
    # import is lazy to keep this low-level runtime helper usable by standalone
    # checkpoint tools.
    if isinstance(json_data, dict) and "content_warnings" in json_data:
        from services.story_planner.core.content_warnings import (
            validate_content_warnings_schema,
        )

        errors.extend(
            f"content_warnings: {issue}"
            for issue in validate_content_warnings_schema(
                json_data.get("content_warnings"),
                require_localized=True,
            )
        )
    nodes = json_data.get("nodes", {}) if isinstance(json_data, dict) else {}
    if not isinstance(nodes, dict) or not nodes:
        return False, ["nodes 为空或格式错误"]
    root_id = json_data.get("root_id")
    if root_id not in nodes:
        errors.append(f"root_id '{root_id}' 不存在于 nodes")

    saved = set(saved_md_ids or [])
    for node_id, node in nodes.items():
        if node_id not in saved:
            errors.append(f"节点 '{node_id}' 缺少对应 MD")
        children = node.get("children", []) if isinstance(node, dict) else []
        if not isinstance(children, list):
            errors.append(f"节点 '{node_id}' 的 children 不是列表")
            continue
        for child in children:
            child_id = child.get("child_id") if isinstance(child, dict) else None
            if child_id not in nodes:
                errors.append(
                    f"节点 '{node_id}' 的 child_id '{child_id}' 不存在于 nodes"
                )
    return not errors, errors
