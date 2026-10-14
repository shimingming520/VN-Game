"""Shared-schema repair for the platform novel_tasks table."""

from sqlalchemy import text

from common.platform_db import platform_engine
from common.task_models import engine as business_engine


_COLUMNS = {
    "pipeline_name": "TEXT",
    "group_name": "TEXT NOT NULL DEFAULT 'group-ref'",
    "output_language": "TEXT NOT NULL DEFAULT 'zh-CN'",
    "art_style": "TEXT NOT NULL DEFAULT 'ink_chinese'",
    "story_mode": "TEXT NOT NULL DEFAULT 'original'",
    "gameplay": "TEXT",
    "text_model": "TEXT",
    "image_model": "TEXT",
    "audio_model": "TEXT",
    "resolution": "TEXT",
    "upload_type": "TEXT",
    "generated_project_name": "TEXT",
    "info_confirm_stage": "TEXT",
    "story_planner_status": "INTEGER NOT NULL DEFAULT 0",
    "info_extractor_status": "INTEGER NOT NULL DEFAULT 0",
    "sound_arranger_status": "INTEGER NOT NULL DEFAULT 0",
    "char_designer_status": "INTEGER NOT NULL DEFAULT 0",
    "audio_producer_status": "INTEGER NOT NULL DEFAULT 0",
    "scene_painter_status": "INTEGER NOT NULL DEFAULT 0",
    "game_builder_status": "INTEGER NOT NULL DEFAULT 0",
    "overall_status": "INTEGER NOT NULL DEFAULT 0",
    "progress_percentage": "INTEGER NOT NULL DEFAULT 0",
    "error_message": "TEXT",
    "celery_chain_id": "TEXT",
    "completed_at": "TIMESTAMP",
    "is_deleted": "BOOLEAN NOT NULL DEFAULT FALSE",
    "is_purged": "BOOLEAN NOT NULL DEFAULT FALSE",
    "is_published": "BOOLEAN NOT NULL DEFAULT FALSE",
    "create_time": "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP",
    "update_time": "TIMESTAMP",
    "remake_task_ids": "JSONB NOT NULL DEFAULT '[]'::jsonb",
    "editors": "JSONB NOT NULL DEFAULT '[]'::jsonb",
}


_BACKFILLS = (
    "UPDATE public.novel_tasks SET output_language = 'zh-CN' WHERE output_language IS NULL",
    "UPDATE public.novel_tasks SET art_style = 'ink_chinese' WHERE art_style IS NULL",
    "UPDATE public.novel_tasks SET story_mode = 'original' WHERE story_mode IS NULL",
    "UPDATE public.novel_tasks SET story_planner_status = 0 WHERE story_planner_status IS NULL",
    "UPDATE public.novel_tasks SET info_extractor_status = 0 WHERE info_extractor_status IS NULL",
    "UPDATE public.novel_tasks SET sound_arranger_status = 0 WHERE sound_arranger_status IS NULL",
    "UPDATE public.novel_tasks SET char_designer_status = 0 WHERE char_designer_status IS NULL",
    "UPDATE public.novel_tasks SET audio_producer_status = 0 WHERE audio_producer_status IS NULL",
    "UPDATE public.novel_tasks SET scene_painter_status = 0 WHERE scene_painter_status IS NULL",
    "UPDATE public.novel_tasks SET game_builder_status = 0 WHERE game_builder_status IS NULL",
    "UPDATE public.novel_tasks SET overall_status = 0 WHERE overall_status IS NULL",
    "UPDATE public.novel_tasks SET progress_percentage = 0 WHERE progress_percentage IS NULL",
    "UPDATE public.novel_tasks SET is_deleted = FALSE WHERE is_deleted IS NULL",
    "UPDATE public.novel_tasks SET is_purged = FALSE WHERE is_purged IS NULL",
    "UPDATE public.novel_tasks SET is_published = FALSE WHERE is_published IS NULL",
    "UPDATE public.novel_tasks SET remake_task_ids = '[]'::jsonb WHERE remake_task_ids IS NULL",
    "UPDATE public.novel_tasks SET editors = '[]'::jsonb WHERE editors IS NULL",
)


def _ensure_for_engine(engine) -> None:
    with engine.begin() as connection:
        for name, definition in _COLUMNS.items():
            connection.execute(text(
                f"ALTER TABLE public.novel_tasks ADD COLUMN IF NOT EXISTS {name} {definition}"
            ))
        for statement in _BACKFILLS:
            connection.execute(text(statement))
        connection.execute(text(
            "ALTER TABLE public.novel_tasks ALTER COLUMN remake_task_ids "
            "SET DEFAULT '[]'::jsonb"
        ))
        connection.execute(text(
            "ALTER TABLE public.novel_tasks ALTER COLUMN editors "
            "SET DEFAULT '[]'::jsonb"
        ))


def ensure_novel_tasks_schema() -> None:
    """Make the shared table usable after deployments that predate new columns."""
    _ensure_for_engine(platform_engine)
    _ensure_for_engine(business_engine)
