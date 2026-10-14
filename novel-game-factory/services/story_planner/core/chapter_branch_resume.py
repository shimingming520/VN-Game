"""Deterministic recovery helpers for already-published chapter branches."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from services.story_planner.core.chapter_branch_runtime import (
    CHAPTER_BRANCH_STEP_ORDER,
    PLAN_VERSION,
    initialize_chapter_branch_plan,
    stable_payload_digest,
    update_plan_step,
)


# Version 2 adds the persisted narrative POV contract.  Old checkpoints are
# intentionally rejected so a resumed run cannot silently generate prose with
# a different narrator contract.
_NOVEL_CONTEXT_SCHEMA_VERSION = 2
_GLOBAL_STYLE_SCHEMA_VERSION = 1
_GLOBAL_STYLE_PRODUCER_VERSION = "chapter-branch-global-style-v1"
_NOVEL_CONTEXT_FIELD_TYPES = {
    "worldview": dict,
    "chapter_summaries": list,
    "style_samples": str,
    "char_cards": str,
    "relationship_map": str,
    "world_rules": str,
    "narrative_arc": str,
    "persona_axes": list,
    "is_romance_oriented": bool,
    "factions_text": str,
    "hidden_threads_text": str,
    "ending_dimensions": list,
    "character_secrets_text": str,
    "character_behavior_anchors": str,
    "branch_type": str,
    "branch_type_strategy_version": int,
    "narrative_pov_contract": dict,
}
_SUPPORTED_BRANCH_TYPES = {
    "default",
    "death_corridor",
    "romance",
    "fantasy",
    "modular",
}
_CHAPTER_PROCESSING_SCHEMA_VERSION = 1
_CHAPTER_PROCESSING_PRODUCER_VERSION = "chapter-processing-v1"


def build_chapter_processing_checkpoint(
    raw_text: str,
    chapters: list[str],
    *,
    artifact_path: str = "",
    artifact_sha256: str = "",
) -> dict[str, Any]:
    """Build an integrity-checked checkpoint for deterministic chapter preprocessing.

    The chapter bodies may be kept in a shared-PVC artifact; callers can omit
    them from the persisted plan and pass them back to the validator after
    loading that artifact.
    """

    from services.story_planner.core.chapter_branch_runtime import content_sha256

    normalized = [str(chapter or "") for chapter in chapters]
    return {
        "schema_version": _CHAPTER_PROCESSING_SCHEMA_VERSION,
        "producer_version": _CHAPTER_PROCESSING_PRODUCER_VERSION,
        "raw_source_fingerprint": content_sha256(str(raw_text or "")),
        "chapter_count": len(normalized),
        "chapter_sha256": [content_sha256(chapter) for chapter in normalized],
        "cleaned_source_fingerprint": content_sha256("".join(normalized)),
        "artifact_path": str(artifact_path or ""),
        "artifact_sha256": str(artifact_sha256 or ""),
        "chapters": normalized,
    }


def get_reusable_chapter_processing_checkpoint(
    checkpoint: Mapping[str, Any] | None,
    *,
    raw_text: str,
    chapters: list[str] | None = None,
) -> list[str] | None:
    """Return verified processed chapters, rejecting stale/corrupt checkpoints."""

    from services.story_planner.core.chapter_branch_runtime import content_sha256

    value = checkpoint if isinstance(checkpoint, Mapping) else {}
    stored = value.get("chapters") if chapters is None else chapters
    if not isinstance(stored, list) or not stored:
        return None
    normalized = [str(chapter or "") for chapter in stored]
    expected_hashes = value.get("chapter_sha256")
    if not isinstance(expected_hashes, list) or len(expected_hashes) != len(normalized):
        return None
    if not (
        int(value.get("schema_version", 0) or 0) == _CHAPTER_PROCESSING_SCHEMA_VERSION
        and value.get("producer_version") == _CHAPTER_PROCESSING_PRODUCER_VERSION
        and value.get("raw_source_fingerprint") == content_sha256(str(raw_text or ""))
        and int(value.get("chapter_count", 0) or 0) == len(normalized)
        and value.get("cleaned_source_fingerprint")
        == content_sha256("".join(normalized))
        and all(
            str(expected_hashes[index] or "") == content_sha256(chapter)
            for index, chapter in enumerate(normalized)
        )
    ):
        return None
    return normalized


def build_global_style_checkpoint(
    style: str,
    *,
    source_fingerprint: str,
    model: str,
) -> dict[str, Any]:
    """Build a self-validating chapter_branch global-style checkpoint."""

    from services.story_planner.core.chapter_branch_runtime import content_sha256

    value = str(style or "").strip()
    if not value:
        raise ValueError("global_style checkpoint cannot be empty")
    return {
        "schema_version": _GLOBAL_STYLE_SCHEMA_VERSION,
        "producer_version": _GLOBAL_STYLE_PRODUCER_VERSION,
        "source_fingerprint": str(source_fingerprint or ""),
        "model": str(model or ""),
        "style": value,
        "style_sha256": content_sha256(value),
    }


def get_reusable_global_style(
    plan: Mapping[str, Any] | None,
    *,
    source_fingerprint: str,
    model: str,
) -> str | None:
    """Return a matching untampered style checkpoint, otherwise regenerate."""

    from services.story_planner.core.chapter_branch_runtime import content_sha256

    value = plan if isinstance(plan, Mapping) else {}
    checkpoint = value.get("global_style_checkpoint")
    if not isinstance(checkpoint, Mapping):
        return None
    style = str(checkpoint.get("style") or "").strip()
    if not (
        int(checkpoint.get("schema_version", 0) or 0)
        == _GLOBAL_STYLE_SCHEMA_VERSION
        and checkpoint.get("producer_version") == _GLOBAL_STYLE_PRODUCER_VERSION
        and checkpoint.get("source_fingerprint") == str(source_fingerprint or "")
        and checkpoint.get("model") == str(model or "")
        and style
        and checkpoint.get("style_sha256") == content_sha256(style)
    ):
        return None
    return style


def build_novel_analysis_step_input(
    *,
    source_fingerprint: str,
    chapters: list[str],
    output_language: str,
    provider: str,
    model: str,
) -> dict[str, Any]:
    """Build the full identity of the chapter-aligned novel analysis step."""

    from services.story_planner.core.chapter_branch_runtime import content_sha256
    from services.story_planner.core.novel_reader import NARRATIVE_POV_CONTRACT_VERSION

    normalized_chapters = [str(chapter or "") for chapter in chapters]
    return {
        "source_fingerprint": str(source_fingerprint or ""),
        "chapter_count": len(normalized_chapters),
        "chapter_sha256": [
            content_sha256(chapter) for chapter in normalized_chapters
        ],
        "output_language": output_language,
        "provider": provider,
        "model": model,
        # POV contract is now part of the analysis output and generation
        # identity; invalidate pre-contract analysis checkpoints on resume.
        "analysis_cache_version": 5,
        "summary_schema_version": 2,
        "narrative_pov_contract_version": NARRATIVE_POV_CONTRACT_VERSION,
    }


def serialize_novel_context(novel_context: Any) -> dict[str, Any]:
    """Serialize the generation-relevant NovelContext contract to JSON data."""

    payload = {
        field: getattr(novel_context, field)
        for field in _NOVEL_CONTEXT_FIELD_TYPES
    }
    return {
        "schema_version": _NOVEL_CONTEXT_SCHEMA_VERSION,
        **json.loads(json.dumps(payload, ensure_ascii=False, default=str)),
    }


def deserialize_novel_context(payload: Mapping[str, Any] | None) -> Any | None:
    """Restore only complete, correctly typed NovelContext checkpoints."""

    if not isinstance(payload, Mapping):
        return None
    value = dict(payload)
    if int(value.get("schema_version", 0) or 0) != _NOVEL_CONTEXT_SCHEMA_VERSION:
        return None
    for field, expected_type in _NOVEL_CONTEXT_FIELD_TYPES.items():
        if field not in value or not isinstance(value[field], expected_type):
            return None
    if not value["worldview"] or not value["chapter_summaries"]:
        return None
    if not all(isinstance(item, str) and item.strip() for item in value["chapter_summaries"]):
        return None
    if value["branch_type"] not in _SUPPORTED_BRANCH_TYPES:
        return None
    from services.story_planner.core.novel_reader import NARRATIVE_POV_CONTRACT_VERSION

    pov_contract = value.get("narrative_pov_contract")
    # Empty contracts are valid for legacy/minimal analyses where POV could
    # not be inferred.  Once a contract is present, however, an unknown or
    # stale version makes the whole checkpoint unsafe to resume.
    if pov_contract:
        if not isinstance(pov_contract, Mapping):
            return None
        try:
            pov_version = int(pov_contract.get("contract_version", 0) or 0)
        except (TypeError, ValueError):
            return None
        if pov_version != NARRATIVE_POV_CONTRACT_VERSION:
            return None
    if not all(isinstance(item, dict) for item in value["persona_axes"]):
        return None
    if not all(isinstance(item, dict) for item in value["ending_dimensions"]):
        return None

    from services.story_planner.core.novel_reader import NovelContext

    return NovelContext(
        **{field: value[field] for field in _NOVEL_CONTEXT_FIELD_TYPES}
    )


def _first_sentence(text: str, limit: int) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    sentence = re.split(r"(?<=[。！？!?])\s*", value, maxsplit=1)[0].strip()
    sentence = sentence[:limit].rstrip("，,；;：: ")
    if sentence and sentence[-1] not in "。！？!?":
        sentence += "。"
    return sentence


def _original_summaries(plan: Mapping[str, Any]) -> list[str]:
    topology_result = plan.get("topology_result", {})
    topology = topology_result.get("topology", []) if isinstance(topology_result, dict) else []
    indexed: list[tuple[int, str]] = []
    for node in topology or []:
        if not isinstance(node, dict) or not node.get("is_original"):
            continue
        index = node.get("source_chapter_idx")
        summary = str(node.get("summary") or "").strip()
        if isinstance(index, int) and summary:
            indexed.append((index, summary))
    indexed.sort(key=lambda item: item[0])
    deduplicated: list[str] = []
    for _index, summary in indexed:
        if not deduplicated or summary != deduplicated[-1]:
            deduplicated.append(summary)
    return deduplicated


def build_summary_from_checkpoint(
    plan: Mapping[str, Any],
    worldview: Mapping[str, Any],
) -> dict[str, str]:
    """Build required summary fields from persisted data without an LLM call."""

    summaries = _original_summaries(plan)
    narrative_arc = str(worldview.get("narrative_arc") or "").strip()
    outline_parts: list[str] = []
    if summaries:
        first_match = re.search(r"【核心事件】([^【]*)", summaries[0])
        if first_match:
            outline_parts.append(first_match.group(1).strip().replace("\n", " "))
        last_match = re.search(r"【与结局的关联】([^【]*)", summaries[-1])
        if last_match:
            outline_parts.append(last_match.group(1).strip().replace("\n", " "))
    if not outline_parts and summaries:
        outline_parts = [_first_sentence(summaries[0], 300)]
        if len(summaries) > 1:
            outline_parts.append(_first_sentence(summaries[-1], 300))
    outline = " ".join(part for part in outline_parts if part).strip()[:600]
    if not outline:
        outline = _first_sentence(narrative_arc, 600)
    if not outline:
        outline = "故事沿既定章节推进，并在关键选择后完成收束。"

    background = str(worldview.get("world_rules") or "").strip()[:400]
    if not background:
        worldview_payload = worldview.get("worldview")
        if isinstance(worldview_payload, (dict, list)) and worldview_payload:
            background = json.dumps(
                worldview_payload,
                ensure_ascii=False,
                sort_keys=True,
            )[:400]
        else:
            background = _first_sentence(narrative_arc, 400) or outline[:400]

    one_liner = _first_sentence(narrative_arc, 120)
    if not one_liner:
        one_liner = _first_sentence(outline, 120)
    return {
        "outline": outline,
        "background": background,
        "one_liner": one_liner,
    }


def upgrade_legacy_completed_plan(
    plan: Mapping[str, Any],
    *,
    publication_fingerprint: str,
) -> dict[str, Any]:
    """Migrate a verified v5 publication without discarding completed nodes."""

    value = dict(plan)
    if int(value.get("version", 0) or 0) == PLAN_VERSION:
        return value
    topology_result = value.get("topology_result")
    if not isinstance(topology_result, dict):
        raise ValueError("legacy chapter_branch plan has no topology_result")
    upgraded = initialize_chapter_branch_plan(
        str(value.get("task_id") or ""),
        topology_result,
        str(value.get("branch_type") or "default"),
        existing_plan=value,
        source_fingerprint=str(value.get("source_fingerprint") or ""),
    )
    for field in (
        "run_id",
        "run_started_at",
        "heartbeat_at",
        "preflight",
        "effect_caps",
        "quality_gate",
    ):
        if field in value:
            upgraded[field] = value[field]
    for step_name in CHAPTER_BRANCH_STEP_ORDER[:6]:
        output_digest = (
            str(publication_fingerprint)
            if step_name == "story_publish"
            else stable_payload_digest(
                {"legacy_completed_step": step_name, "plan_version": 5}
            )
        )
        update_plan_step(
            upgraded,
            step_name,
            status="completed",
            output_digest=output_digest,
        )
    return upgraded
