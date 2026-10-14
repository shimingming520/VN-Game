"""Runtime limits for long-running story_planner jobs."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Mapping, Optional


DEFAULT_TASK_SOFT_TIME_LIMIT = 38 * 60 * 60
DEFAULT_TASK_TIME_LIMIT = 48 * 60 * 60
DEFAULT_AUXILIARY_TASK_SOFT_TIME_LIMIT = 15 * 60
DEFAULT_AUXILIARY_TASK_TIME_LIMIT = 20 * 60
DEFAULT_CONFIRMATION_TIMEOUT = 6 * 60 * 60
DEFAULT_CACHE_DIR = str(
    Path(__file__).resolve().parents[1] / ".cache" / "chapter_branch"
)
DEFAULT_CACHE_VERSION = "chapter-branch-v1"
DEFAULT_CACHE_MAX_AGE_DAYS = 30
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 60
DEFAULT_NODE_STALL_TIMEOUT_SECONDS = 30 * 60


@dataclass(frozen=True)
class StoryPlannerRuntimeSettings:
    task_soft_time_limit: int
    task_time_limit: int
    auxiliary_task_soft_time_limit: int
    auxiliary_task_time_limit: int
    confirmation_timeout: int
    cache_dir: str
    cache_version: str
    cache_max_age_days: int
    heartbeat_interval_seconds: int
    node_stall_timeout_seconds: int


def _positive_int(source: Mapping[str, str], key: str, default: int) -> int:
    try:
        value = int(source.get(key, ""))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def resolve_runtime_settings(
    env: Optional[Mapping[str, str]] = None,
) -> StoryPlannerRuntimeSettings:
    """Resolve large-novel task limits from the story_planner namespace."""
    source = os.environ if env is None else env
    hard_limit = _positive_int(
        source,
        "STORY_PLANNER_TASK_TIME_LIMIT",
        DEFAULT_TASK_TIME_LIMIT,
    )
    soft_limit = _positive_int(
        source,
        "STORY_PLANNER_TASK_SOFT_TIME_LIMIT",
        DEFAULT_TASK_SOFT_TIME_LIMIT,
    )
    if soft_limit >= hard_limit:
        soft_limit = max(1, hard_limit - 60 * 60)

    auxiliary_hard_limit = _positive_int(
        source,
        "STORY_PLANNER_AUXILIARY_TASK_TIME_LIMIT",
        DEFAULT_AUXILIARY_TASK_TIME_LIMIT,
    )
    auxiliary_soft_limit = _positive_int(
        source,
        "STORY_PLANNER_AUXILIARY_TASK_SOFT_TIME_LIMIT",
        DEFAULT_AUXILIARY_TASK_SOFT_TIME_LIMIT,
    )
    if auxiliary_soft_limit >= auxiliary_hard_limit:
        auxiliary_soft_limit = max(1, auxiliary_hard_limit - 60)

    cache_dir = str(source.get("STORY_PLANNER_CACHE_DIR", DEFAULT_CACHE_DIR) or "").strip()
    if not cache_dir:
        cache_dir = DEFAULT_CACHE_DIR
    cache_version = str(
        source.get("STORY_PLANNER_CACHE_VERSION", DEFAULT_CACHE_VERSION) or ""
    ).strip()
    if not cache_version:
        cache_version = DEFAULT_CACHE_VERSION

    return StoryPlannerRuntimeSettings(
        task_soft_time_limit=soft_limit,
        task_time_limit=hard_limit,
        auxiliary_task_soft_time_limit=auxiliary_soft_limit,
        auxiliary_task_time_limit=auxiliary_hard_limit,
        confirmation_timeout=_positive_int(
            source,
            "STORY_PLANNER_CONFIRMATION_TIMEOUT",
            DEFAULT_CONFIRMATION_TIMEOUT,
        ),
        cache_dir=cache_dir,
        cache_version=cache_version,
        cache_max_age_days=_positive_int(
            source,
            "STORY_PLANNER_CACHE_MAX_AGE_DAYS",
            DEFAULT_CACHE_MAX_AGE_DAYS,
        ),
        heartbeat_interval_seconds=_positive_int(
            source,
            "STORY_PLANNER_HEARTBEAT_INTERVAL_SECONDS",
            DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        ),
        node_stall_timeout_seconds=_positive_int(
            source,
            "STORY_PLANNER_NODE_STALL_TIMEOUT_SECONDS",
            DEFAULT_NODE_STALL_TIMEOUT_SECONDS,
        ),
    )


RUNTIME_SETTINGS = resolve_runtime_settings()
