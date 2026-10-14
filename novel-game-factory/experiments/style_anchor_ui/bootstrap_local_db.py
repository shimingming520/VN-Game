"""Create the minimum local database schema required by the visual pipeline."""

from sqlalchemy import text
from sqlmodel import SQLModel

from common.platform_db import platform_engine
from common.platform_task_models import NovelTask
from common.task_models import MaterialRegenSession
from common.usage_tracker import ApiUsage
from services.story_planner.table.models import (
    ChapterBranchPlan,
    Outline,
    StoryRelease,
    StorySummary,
    Worldview,
    engine as business_engine,
)
from services.story_planner.table.platform_models import Plot, StoryTreeJson, StoryTreeMd


SQLModel.metadata.create_all(
    platform_engine,
    tables=[
        NovelTask.__table__,
        Plot.__table__,
        StoryTreeMd.__table__,
        StoryTreeJson.__table__,
        ApiUsage.__table__,
    ],
)
SQLModel.metadata.create_all(
    business_engine,
    tables=[
        Worldview.__table__,
        Outline.__table__,
        StoryRelease.__table__,
        StorySummary.__table__,
        ChapterBranchPlan.__table__,
        MaterialRegenSession.__table__,
    ],
)

generic_ddl = """
    CREATE TABLE IF NOT EXISTS {table_name} (
        id SERIAL PRIMARY KEY,
        task_id TEXT NOT NULL UNIQUE,
        data JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        create_time TIMESTAMP NOT NULL DEFAULT NOW(),
        update_time TIMESTAMP NOT NULL DEFAULT NOW()
    )
"""

with platform_engine.begin() as connection:
    for table_name in (
        "characters",
        "name_mappings",
        "character_styles",
        "character_prompts",
        "scene_prompts",
        "cg",
        "backgrounds",
        "sound",
        "atmosphere",
    ):
        connection.execute(text(generic_ddl.format(table_name=table_name)))
    connection.execute(
        text("CREATE UNIQUE INDEX IF NOT EXISTS uq_plots_task_id_local ON plots(task_id)")
    )
    connection.execute(text("ALTER TABLE api_usage ALTER COLUMN music_seconds SET DEFAULT 0"))

with business_engine.begin() as connection:
    for table_name in ("character_position", "ultimate", "voice_mappings"):
        connection.execute(text(generic_ddl.format(table_name=table_name)))
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS scenes (
                id SERIAL PRIMARY KEY,
                task_id TEXT NOT NULL UNIQUE,
                data JSONB NOT NULL DEFAULT '{}'::jsonb,
                raw JSONB,
                failed_scenes JSONB,
                create_time TIMESTAMP NOT NULL DEFAULT NOW(),
                update_time TIMESTAMP NOT NULL DEFAULT NOW()
            )
            """
        )
    )

print("STYLE_ANCHOR_LOCAL_DB_READY")
