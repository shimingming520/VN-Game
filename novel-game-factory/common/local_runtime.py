"""Runtime switches for the local-only pipeline."""

from __future__ import annotations

import os
from typing import Any, Callable, Optional


_TRUE_VALUES = {"1", "true", "yes", "on"}


def env_flag(name: str) -> bool:
    """Return whether an environment flag is explicitly enabled."""
    return os.getenv(name, "").strip().lower() in _TRUE_VALUES


def oss_disabled() -> bool:
    """Return True because all persisted assets are local-only."""
    return True


def local_pipeline_enabled() -> bool:
    """Whether Gateway confirmation should use the local no-audio Celery chain."""
    return env_flag("LOCAL_PIPELINE_MODE")


def submit_local_visual_pipeline(
    celery_app: Any,
    task_id: str,
    *,
    chain_factory: Optional[Callable[..., Any]] = None,
    group_factory: Optional[Callable[..., Any]] = None,
    chord_factory: Optional[Callable[..., Any]] = None,
) -> Any:
    """Submit the local visual-only pipeline and return its AsyncResult.

    This experiment intentionally omits ``audio_producer`` and ``sound_arranger``.
    Character and scene services start together after info extraction; the scene
    service joins the character checkpoint only when it reaches CG generation.
    """
    if chain_factory is None:
        from celery import chain as chain_factory
    if group_factory is None:
        from celery import group as group_factory
    if chord_factory is None:
        from celery import chord as chord_factory

    prefix = (
        celery_app.signature(
            "services.story_planner.tasks.parse_novel_to_draft",
            args=[task_id],
            queue="story_queue",
        ),
        celery_app.signature(
            "services.info_extractor.tasks.extract_info",
            queue="extract_queue",
        ),
    )
    visual_join = chord_factory(
        group_factory(
            celery_app.signature(
                "services.char_designer.tasks.generate_characters",
                queue="char_queue",
            ),
            celery_app.signature(
                "services.scene_painter.tasks.generate_scenes",
                queue="scene_queue",
            ),
        ),
        celery_app.signature(
            "services.game_builder.tasks.build_game_package",
            args=[task_id],
            queue="build_queue",
            immutable=True,
        ),
    )
    return chain_factory(*prefix, visual_join).apply_async()
