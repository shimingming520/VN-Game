"""Bounded text chunking and hierarchical reduction for large novels."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Callable, Mapping, Optional, Sequence


DEFAULT_ANALYSIS_SEGMENT_CHARS = 13_000
DEFAULT_SUMMARY_MERGE_BATCH_SIZE = 8
DEFAULT_WORLDVIEW_SEGMENT_CHARS = 40_000
DEFAULT_WORLDVIEW_MERGE_BATCH_SIZE = 5
DEFAULT_FALLBACK_MERGE_CHARS = 6_000


@dataclass(frozen=True)
class HierarchicalProcessingSettings:
    analysis_segment_chars: int
    summary_merge_batch_size: int
    worldview_segment_chars: int
    worldview_merge_batch_size: int


def _bounded_int(
    source: Mapping[str, str],
    key: str,
    default: int,
    minimum: int,
) -> int:
    try:
        value = int(source.get(key, ""))
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def resolve_hierarchical_settings(
    env: Optional[Mapping[str, str]] = None,
) -> HierarchicalProcessingSettings:
    source = os.environ if env is None else env
    return HierarchicalProcessingSettings(
        analysis_segment_chars=_bounded_int(
            source,
            "STORY_PLANNER_ANALYSIS_SEGMENT_CHARS",
            DEFAULT_ANALYSIS_SEGMENT_CHARS,
            minimum=1_000,
        ),
        summary_merge_batch_size=_bounded_int(
            source,
            "STORY_PLANNER_SUMMARY_MERGE_BATCH_SIZE",
            DEFAULT_SUMMARY_MERGE_BATCH_SIZE,
            minimum=2,
        ),
        worldview_segment_chars=_bounded_int(
            source,
            "STORY_PLANNER_WORLDVIEW_SEGMENT_CHARS",
            DEFAULT_WORLDVIEW_SEGMENT_CHARS,
            minimum=5_000,
        ),
        worldview_merge_batch_size=_bounded_int(
            source,
            "STORY_PLANNER_WORLDVIEW_MERGE_BATCH_SIZE",
            DEFAULT_WORLDVIEW_MERGE_BATCH_SIZE,
            minimum=2,
        ),
    )


HIERARCHICAL_SETTINGS = resolve_hierarchical_settings()


def split_text_at_boundaries(
    text: str,
    max_chars: int,
    boundary_window: int = 800,
) -> list[str]:
    """Split without data loss, preferring nearby paragraph/sentence boundaries."""
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if not text:
        return []

    segments = []
    start = 0
    total = len(text)
    boundary_chars = ("\n", "。", "！", "？", ".", "!", "?")
    while start < total:
        end = min(start + max_chars, total)
        if end < total:
            search_start = max(start + 1, end - max(1, boundary_window))
            boundary = max(
                text.rfind(marker, search_start, end)
                for marker in boundary_chars
            )
            if boundary >= search_start:
                end = boundary + 1
        if end <= start:
            end = min(start + max_chars, total)
        segments.append(text[start:end])
        start = end
    return segments


def compact_text_items(items: Sequence[str], max_chars: int) -> str:
    """Deterministically bound fallback text when an LLM merge fails."""
    values = [str(item).strip() for item in items if str(item).strip()]
    if not values or max_chars <= 0:
        return ""
    separator = "\n\n---\n\n"
    available = max_chars - len(separator) * max(len(values) - 1, 0)
    per_item = max(1, available // len(values))
    return separator.join(value[:per_item] for value in values)[:max_chars]


def hierarchical_reduce(
    items: Sequence[str],
    merge_batch_size: int,
    merge_group: Callable[[list[str], int, int], str],
    fallback_max_chars: int = DEFAULT_FALLBACK_MERGE_CHARS,
) -> str:
    """Reduce items through bounded merge batches until one result remains."""
    if merge_batch_size < 2:
        raise ValueError("merge_batch_size must be at least 2")
    current = [str(item).strip() for item in items if str(item).strip()]
    if not current:
        return ""

    level = 0
    while len(current) > 1:
        next_level = []
        for batch_index, start in enumerate(range(0, len(current), merge_batch_size)):
            group = current[start : start + merge_batch_size]
            if len(group) == 1:
                next_level.append(group[0])
                continue
            try:
                merged = str(merge_group(group, level, batch_index) or "").strip()
            except Exception:
                merged = ""
            if not merged:
                merged = compact_text_items(group, fallback_max_chars)
            next_level.append(merged)
        current = next_level
        level += 1
    return current[0]
