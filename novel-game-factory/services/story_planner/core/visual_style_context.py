"""Build a bounded, whole-book context for delayed visual-style planning.

This module is deliberately deterministic: it reuses analysis already produced by
``story_planner`` and samples the source text without making another LLM request.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Dict, Iterable, Mapping, Sequence

from common.global_style import VISUAL_STYLE_CONTEXT_SCHEMA

DEFAULT_CONTEXT_CHARS = 22000
DEFAULT_EXCERPT_CHARS = 7000

_VISUAL_WORLDVIEW_KEYS = (
    "title",
    "genre",
    "tone",
    "style_notes",
    "narrative_arc",
    "world_rules",
    "history",
    "geography",
    "economy",
    "social_structure",
    "power_system",
    "magic_tech_rules",
    "key_locations",
    "key_events",
    "factions",
)


def _normalize_text(value: Any) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def _truncate_text(value: Any, limit: int) -> str:
    text = _normalize_text(value)
    if limit <= 0 or len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1].rstrip() + "…"


def _even_indices(total: int, wanted: int) -> list[int]:
    if total <= 0 or wanted <= 0:
        return []
    if total <= wanted:
        return list(range(total))
    if wanted == 1:
        return [total // 2]
    indices = {
        round(index * (total - 1) / (wanted - 1))
        for index in range(wanted)
    }
    return sorted(indices)


def _compact_value(
    value: Any,
    *,
    string_limit: int,
    item_limit: int,
    depth: int = 0,
) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _truncate_text(value, string_limit)
    if depth >= 4:
        return _truncate_text(
            json.dumps(value, ensure_ascii=False, default=str),
            string_limit,
        )
    if isinstance(value, Mapping):
        compact: Dict[str, Any] = {}
        for key in list(value)[:item_limit]:
            compact[str(key)] = _compact_value(
                value[key],
                string_limit=string_limit,
                item_limit=item_limit,
                depth=depth + 1,
            )
        return compact
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        indices = _even_indices(len(value), item_limit)
        return [
            _compact_value(
                value[index],
                string_limit=string_limit,
                item_limit=item_limit,
                depth=depth + 1,
            )
            for index in indices
        ]
    return _truncate_text(value, string_limit)


def _fit_json_value(value: Any, max_chars: int) -> Any:
    """Keep a JSON-compatible value inside a predictable serialized budget."""
    for string_limit, item_limit in (
        (1000, 16),
        (700, 12),
        (450, 9),
        (250, 6),
    ):
        compact = _compact_value(
            value,
            string_limit=string_limit,
            item_limit=item_limit,
        )
        if len(json.dumps(compact, ensure_ascii=False, default=str)) <= max_chars:
            return compact
    serialized = json.dumps(value, ensure_ascii=False, default=str)
    return {"bounded_summary": _truncate_text(serialized, max(1, max_chars - 30))}


def _visual_worldview(worldview: Any) -> Dict[str, Any]:
    if not isinstance(worldview, Mapping):
        return {}
    selected = {
        key: worldview[key]
        for key in _VISUAL_WORLDVIEW_KEYS
        if key in worldview and worldview[key] not in (None, "", [], {})
    }
    return _fit_json_value(selected, 4800) if selected else {}


def _sample_chapter_summaries(
    summaries: Iterable[Any],
    *,
    max_chars: int = 3800,
) -> list[Dict[str, Any]]:
    normalized = []
    for summary in summaries or []:
        text = _normalize_text(summary)
        if text:
            normalized.append(text)
    if not normalized:
        return []
    selected_indices = _even_indices(len(normalized), min(len(normalized), 10))
    per_summary = max(180, max_chars // max(1, len(selected_indices)))
    return [
        {
            "chapter_index": index + 1,
            "summary": _truncate_text(normalized[index], per_summary),
        }
        for index in selected_indices
    ]


def _excerpt_label(index: int, count: int) -> str:
    if count <= 1:
        return "full_text"
    if index == 0:
        return "beginning"
    if index == count - 1:
        return "ending"
    if index * 2 == count - 1:
        return "middle"
    return f"{round(index * 100 / (count - 1))}%"


def _representative_excerpts(
    source_text: str,
    *,
    max_chars: int,
    chapters: Sequence[str] | None = None,
) -> tuple[str, list[Dict[str, Any]]]:
    text = _normalize_text(source_text)
    if not text:
        return "analysis_only", []
    if len(text) <= max_chars:
        return "full_text", [
            {
                "position": "full_text",
                "position_ratio": 0.0,
                "text": text,
            }
        ]

    normalized_chapters = []
    for chapter in chapters or []:
        chapter_text = _normalize_text(chapter)
        if chapter_text:
            normalized_chapters.append(chapter_text)
    if len(normalized_chapters) > 1:
        excerpt_count = min(7, len(normalized_chapters))
        chapter_indices = _even_indices(len(normalized_chapters), excerpt_count)
        excerpt_chars = max(500, max_chars // max(1, len(chapter_indices)))
        excerpts = []
        for selection_index, chapter_index in enumerate(chapter_indices):
            chapter = normalized_chapters[chapter_index]
            if len(chapter) <= excerpt_chars:
                excerpt = chapter
            elif selection_index == 0:
                excerpt = chapter[:excerpt_chars]
            elif selection_index == len(chapter_indices) - 1:
                excerpt = chapter[-excerpt_chars:]
            else:
                start = max(0, (len(chapter) - excerpt_chars) // 2)
                excerpt = chapter[start : start + excerpt_chars]
            ratio = chapter_index / max(1, len(normalized_chapters) - 1)
            excerpts.append(
                {
                    "position": _excerpt_label(
                        selection_index,
                        len(chapter_indices),
                    ),
                    "position_ratio": round(ratio, 3),
                    "chapter_index": chapter_index + 1,
                    "text": excerpt.strip(),
                }
            )
        return f"evenly_spaced_{excerpt_count}_chapters", excerpts

    excerpt_count = min(7, max(3, len(text) // max(1, max_chars) + 2))
    excerpt_chars = max(500, max_chars // excerpt_count)
    usable = max(0, len(text) - excerpt_chars)
    excerpts = []
    for index in range(excerpt_count):
        ratio = index / (excerpt_count - 1)
        start = round(usable * ratio)
        end = min(len(text), start + excerpt_chars)
        excerpt = text[start:end].strip()
        excerpts.append(
            {
                "position": _excerpt_label(index, excerpt_count),
                "position_ratio": round(ratio, 3),
                "text": excerpt,
            }
        )
    return f"evenly_spaced_{excerpt_count}_excerpts", excerpts


def build_visual_style_context(
    *,
    source_text: str,
    story_mode: Any,
    chapters: Sequence[str] | None = None,
    worldview: Mapping[str, Any] | None = None,
    chapter_summaries: Iterable[Any] | None = None,
    narrative_arc: str = "",
    style_samples: str = "",
    world_rules: str = "",
    narrative_structure: Mapping[str, Any] | None = None,
    additional_context: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    """Return a bounded context that represents the whole source novel.

    Short works are stored in full. Long works use evenly spaced excerpts while
    retaining already-computed whole-book analysis, so this step adds no Provider
    request and only performs linear string processing.
    """
    normalized_source = _normalize_text(source_text)
    configured_context_chars = max(
        8000,
        int(os.getenv("VISUAL_STYLE_CONTEXT_MAX_CHARS", DEFAULT_CONTEXT_CHARS)),
    )
    global_style_input_chars = max(
        1000,
        int(os.getenv("GLOBAL_STYLE_SAMPLE_CHARS", "24000")),
    )
    max_context_chars = min(
        configured_context_chars,
        max(8000, global_style_input_chars - 256),
    )
    excerpt_budget = min(
        max_context_chars // 2,
        max(
            3000,
            int(
                os.getenv(
                    "VISUAL_STYLE_CONTEXT_EXCERPT_CHARS",
                    DEFAULT_EXCERPT_CHARS,
                )
            ),
        ),
    )
    strategy, excerpts = _representative_excerpts(
        normalized_source,
        max_chars=excerpt_budget,
        chapters=chapters,
    )

    mode_value = getattr(story_mode, "value", story_mode)
    context: Dict[str, Any] = {
        "schema_version": VISUAL_STYLE_CONTEXT_SCHEMA,
        "story_mode": str(mode_value or ""),
        "source_char_count": len(normalized_source),
        "source_sha256": hashlib.sha256(
            normalized_source.encode("utf-8")
        ).hexdigest(),
        "chapter_count": len(chapters or []),
        "coverage_strategy": strategy,
        "sampled_char_count": sum(len(item["text"]) for item in excerpts),
        "worldview": _visual_worldview(worldview),
        "chapter_summaries": _sample_chapter_summaries(
            chapter_summaries or []
        ),
        "narrative_arc": _truncate_text(narrative_arc, 1200),
        "style_samples": _truncate_text(style_samples, 2400),
        "world_rules": _truncate_text(world_rules, 1500),
        "narrative_structure": _fit_json_value(
            dict(narrative_structure or {}),
            2600,
        ),
        "representative_excerpts": excerpts,
    }
    if additional_context:
        context["additional_context"] = _fit_json_value(
            dict(additional_context),
            1200,
        )

    # Drop empty optional fields before persistence and LLM serialization.
    context = {
        key: value
        for key, value in context.items()
        if value not in (None, "", [], {})
        or key
        in {
            "schema_version",
            "story_mode",
            "source_char_count",
            "source_sha256",
            "chapter_count",
            "coverage_strategy",
            "sampled_char_count",
        }
    }

    serialized = json.dumps(context, ensure_ascii=False, default=str)
    if len(serialized) > max_context_chars:
        # Analysis fields are already independently bounded. If an unusually
        # dense source still exceeds the final budget, shrink only excerpts so
        # beginning/middle/end coverage remains intact.
        overflow = len(serialized) - max_context_chars
        excerpt_items = context.get("representative_excerpts", [])
        if excerpt_items:
            per_item_cut = overflow // len(excerpt_items) + 8
            for item in excerpt_items:
                text = str(item.get("text") or "")
                item["text"] = _truncate_text(
                    text,
                    max(200, len(text) - per_item_cut),
                )
            context["sampled_char_count"] = sum(
                len(str(item.get("text") or "")) for item in excerpt_items
            )
    return context
