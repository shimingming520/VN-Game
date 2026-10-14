"""Story-tree loading helpers shared by info_extractor stages.

The planner publishes a language bundle whose locale entries are complete,
independent story trees.  Info stages should select one locale at their input
boundary and keep using the historical single-tree shape internally.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from typing import Any, Mapping, Optional

from sqlalchemy import text


_LANGUAGE_ALIASES = {
    "en": "en-US",
    "en-us": "en-US",
    "ja": "ja-JP",
    "jp": "ja-JP",
    "ja-jp": "ja-JP",
    "zh": "zh-CN",
    "zh-cn": "zh-CN",
    "ko": "ko-KR",
    "ko-kr": "ko-KR",
    "ru": "ru-RU",
    "ru-ru": "ru-RU",
}


def normalize_story_tree_language(language: Optional[str]) -> str:
    """Normalize supported language aliases to the stored locale key."""
    raw = str(language or "").strip().replace("_", "-")
    return _LANGUAGE_ALIASES.get(raw.lower(), raw)


def story_tree_language_key(
    payload: Mapping[str, Any],
    language: Optional[str],
) -> Optional[str]:
    """Return the matching/fallback locale key for a multilingual payload."""
    if not isinstance(payload, Mapping) or isinstance(payload.get("nodes"), dict):
        return None

    candidates = [
        (str(key), value)
        for key, value in payload.items()
        if key != "publication"
        and isinstance(value, Mapping)
        and isinstance(value.get("nodes"), dict)
    ]
    if not candidates:
        return None

    requested = normalize_story_tree_language(language)
    for key, _ in candidates:
        if normalize_story_tree_language(key) == requested:
            return key
    for key, _ in candidates:
        if normalize_story_tree_language(key) == "en-US":
            return key
    return candidates[0][0]


def decode_story_tree_payload(value: Any) -> dict:
    """Decode a JSONB/text value into a dictionary, or return an empty object."""
    if isinstance(value, (str, bytes, bytearray)):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def _environment_language() -> str:
    """Return an explicit process-level language override, if configured."""
    for name in (
        "INFO_EXTRACTOR_STORY_TREE_LANGUAGE",
        "STORY_PLANNER_STORY_TREE_PLAYER_LANGUAGE",
    ):
        value = os.getenv(name, "").strip()
        if value:
            return value
    # Info extractor has no player-language parameter of its own by default.
    # Keep the default and fallback policy aligned with planner:
    # requested language -> en-US -> first valid locale.
    return "en-US"


def get_story_tree_language(
    task_id: Optional[str] = None,
    *,
    db_engine=None,
    logger=None,
) -> str:
    """Resolve the requested story-tree locale for an info stage.

    The task's ``novel_tasks.output_language`` is authoritative when available.
    An explicit info/planner environment override is used only when the task
    row cannot be read, preserving standalone/offline compatibility.
    """
    if db_engine is not None and task_id:
        try:
            with db_engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT output_language FROM novel_tasks "
                        "WHERE task_id = :tid LIMIT 1"
                    ),
                    {"tid": task_id},
                ).fetchone()
            if row and row[0] and str(row[0]).strip():
                return str(row[0]).strip()
        except Exception as exc:
            if logger:
                logger.warning(
                    "读取任务 output_language 失败，使用环境/默认语言: %s", exc
                )
    return _environment_language()


def load_story_tree_for_player(
    payload: Any,
    language: Optional[str] = None,
    *,
    task_id: Optional[str] = None,
    db_engine=None,
    logger=None,
) -> dict:
    """Return a selected single-language tree, with historical-format support."""
    decoded = decode_story_tree_payload(payload)
    requested = language or get_story_tree_language(
        task_id,
        db_engine=db_engine,
        logger=logger,
    )
    # 兼容历史单语言 story_tree.json；新版 bundle 才需要按语言选择。
    if isinstance(decoded.get("nodes"), dict):
        return deepcopy(decoded)
    locale_key = story_tree_language_key(decoded, requested)
    tree = (
        deepcopy(decoded[locale_key])
        if locale_key is not None
        else {}
    )
    if logger and tree:
        logger.info(
            "story_tree language selected: requested=%s, actual=%s",
            requested,
            tree.get("player_language", "unknown"),
        )
    return tree


def has_story_tree_title(payload: Any) -> bool:
    """Check whether a flat tree or any valid locale has a book title."""
    decoded = decode_story_tree_payload(payload)
    if isinstance(decoded.get("book_title"), str) and decoded["book_title"].strip():
        return True
    for value in decoded.values():
        if (
            isinstance(value, dict)
            and isinstance(value.get("nodes"), dict)
            and isinstance(value.get("book_title"), str)
            and value["book_title"].strip()
        ):
            return True
    return False


def is_story_tree_bundle(payload: Any) -> bool:
    """Whether payload contains at least one valid locale tree."""
    decoded = decode_story_tree_payload(payload)
    return any(
        key != "publication"
        and isinstance(value, dict)
        and isinstance(value.get("nodes"), dict)
        for key, value in decoded.items()
    )


def update_story_tree_field(payload: Any, field: str, value: Any) -> dict:
    """Update one internal field without flattening a locale bundle.

    Fields discovered by an info stage, such as ``hook_id``, belong in every
    complete locale tree in the new format.  ``publication`` is metadata and is
    preserved untouched.  Historical flat trees keep the old top-level layout.
    """
    decoded = decode_story_tree_payload(payload)
    updated = deepcopy(decoded)
    if not isinstance(field, str) or not field.strip():
        return updated

    locale_count = 0
    for locale, locale_tree in list(updated.items()):
        if (
            locale == "publication"
            or not isinstance(locale_tree, dict)
            or not isinstance(locale_tree.get("nodes"), dict)
        ):
            continue
        locale_tree[field] = deepcopy(value)
        locale_count += 1

    if locale_count == 0 and isinstance(updated.get("nodes"), dict):
        updated[field] = deepcopy(value)
    return updated
