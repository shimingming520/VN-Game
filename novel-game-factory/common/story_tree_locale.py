"""Small compatibility layer for locale-keyed ``story_tree.json`` payloads.

Planner owns the multilingual bundle format.  Visual consumers only need one
locale snapshot at a time, while older tasks still contain a single tree at the
payload root.  Keeping that boundary here prevents each consumer from growing
its own subtly different fallback rules.
"""

from __future__ import annotations

from copy import deepcopy
import logging
import os
import re
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

_LANGUAGE_TAG_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
_LANGUAGE_ALIASES = {
    "en": "en-US",
    "ja": "ja-JP",
    "jp": "ja-JP",
    "zh": "zh-CN",
    "ko": "ko-KR",
    "ru": "ru-RU",
}


def _normalize_language_tag(value: Any) -> str:
    """Normalize the small BCP-47-like locale set used by visual consumers."""
    raw = str(value or "").strip().replace("_", "-")
    if not raw:
        return ""
    alias = _LANGUAGE_ALIASES.get(raw.lower())
    if alias:
        return alias
    if not _LANGUAGE_TAG_RE.fullmatch(raw):
        return ""
    parts = raw.split("-")
    return "-".join(
        [
            parts[0].lower(),
            *(
                part.upper() if len(part) in (2, 3) else part
                for part in parts[1:]
            ),
        ]
    )


def _configured_player_language() -> str:
    """Read the consumer language without importing the planner service."""
    return (
        os.getenv("STORY_TREE_PLAYER_LANGUAGE")
        or os.getenv("STORY_PLANNER_STORY_TREE_PLAYER_LANGUAGE")
        or "en-US"
    ).strip() or "en-US"


def _locale_candidates(payload: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    """Return locale snapshots while treating publication as non-locale data."""
    candidates: list[tuple[str, Mapping[str, Any]]] = []
    for key, value in payload.items():
        if key == "publication" or not isinstance(value, Mapping):
            continue
        if isinstance(value.get("nodes"), dict):
            candidates.append((str(key), value))
    return candidates


def _select_locale_snapshot(
    payload: Mapping[str, Any],
    requested: str,
) -> dict[str, Any]:
    """Select requested locale, then en-US, then the first valid locale."""
    candidates = _locale_candidates(payload)
    normalized_requested = _normalize_language_tag(requested)
    normalized_candidates = {
        _normalize_language_tag(key): value
        for key, value in candidates
        if _normalize_language_tag(key)
    }
    selected = normalized_candidates.get(normalized_requested)
    if selected is None:
        selected = normalized_candidates.get("en-US")
    if selected is None and candidates:
        selected = candidates[0][1]
    return deepcopy(dict(selected)) if isinstance(selected, Mapping) else {}


def requested_story_tree_language(language: Optional[str] = None) -> str:
    """Return the consumer language without introducing a new task argument."""
    return (
        str(
            language
            or _configured_player_language()
            or "en-US"
        ).strip()
        or "en-US"
    )


def select_story_tree_for_consumer(
    payload: Any,
    language: Optional[str] = None,
) -> dict[str, Any]:
    """Select one locale tree, while preserving historical single-tree data."""
    if not isinstance(payload, Mapping):
        return {}

    raw = dict(payload)
    has_locale_candidates = any(
        isinstance(value, Mapping) and isinstance(value.get("nodes"), dict)
        for key, value in raw.items()
        if key != "publication"
    )
    if isinstance(raw.get("nodes"), dict) and (
        raw.get("nodes") or not has_locale_candidates
    ):
        selected = deepcopy(raw)
        actual = selected.get("player_language") or "legacy"
        logger.info(
            "story_tree language selected: requested=%s, actual=%s, format=legacy",
            requested_story_tree_language(language),
            actual,
        )
        return selected

    requested = requested_story_tree_language(language)
    if not has_locale_candidates:
        # Some historical style-only callers persisted only global_style and
        # did not include nodes in the small payload passed to this helper.
        # Preserve that compatibility; visual preflight still validates nodes.
        return deepcopy(raw)

    selected = _select_locale_snapshot(raw, requested)
    if not isinstance(selected, dict) or not isinstance(selected.get("nodes"), dict):
        return {}

    actual = selected.get("player_language") or "unknown"
    logger.info(
        "story_tree language selected: requested=%s, actual=%s, format=bundle",
        requested,
        actual,
    )
    return selected


def story_tree_selection_metadata(
    payload: Any,
    language: Optional[str] = None,
) -> dict[str, Any]:
    """Return the selected tree plus stable publication/language metadata."""
    selected = select_story_tree_for_consumer(payload, language)
    raw = dict(payload) if isinstance(payload, Mapping) else {}
    publication = raw.get("publication")
    return {
        "tree": selected,
        "requested_language": requested_story_tree_language(language),
        "actual_language": str(selected.get("player_language") or "unknown"),
        "publication": deepcopy(publication) if isinstance(publication, Mapping) else {},
    }


def update_story_tree_locale_fields(
    payload: Any,
    updates: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply invariant fields to every locale without touching publication."""
    if not isinstance(payload, Mapping):
        raise ValueError("story_tree payload 必须是 JSON object")
    result = deepcopy(dict(payload))
    has_locale_candidates = any(
        isinstance(value, Mapping) and isinstance(value.get("nodes"), dict)
        for key, value in result.items()
        if key != "publication"
    )
    if isinstance(result.get("nodes"), dict) and (
        result.get("nodes") or not has_locale_candidates
    ):
        result.update(deepcopy(dict(updates)))
        return result

    if any(
        key in result
        for key in ("global_style", "visual_style_context", "root_id")
    ):
        # Keep the small legacy style-only payloads used by older callers and
        # migrations.  Full visual preflight still requires a valid nodes map.
        result.update(deepcopy(dict(updates)))
        return result

    locale_count = 0
    for key, value in result.items():
        if key == "publication" or not isinstance(value, Mapping):
            continue
        if not isinstance(value.get("nodes"), dict):
            continue
        locale = deepcopy(dict(value))
        locale.update(deepcopy(dict(updates)))
        result[key] = locale
        locale_count += 1

    if locale_count == 0:
        raise ValueError("story_tree payload 没有可用的语言版本")
    return result
