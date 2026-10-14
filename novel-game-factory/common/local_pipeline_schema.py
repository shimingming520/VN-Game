"""Schema bootstrap for the standalone local visual pipeline.

The local Docker stack intentionally uses one PostgreSQL database for both the
platform and business connections.  Older databases may already contain the
original ``scenes(id, data, create_time)`` table, so initialization must be an
idempotent migration rather than a destructive reset.
"""

from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy import text


LOCAL_VISUAL_TABLES = (
    "characters",
    "name_mappings",
    "character_styles",
    "character_prompts",
    "scene_prompts",
    "cg",
    "backgrounds",
    "sound",
    "atmosphere",
)

LOCAL_BUSINESS_TABLES = (
    "character_position",
    "ultimate",
    "voice_mappings",
)

SCENES_REQUIRED_COLUMNS = frozenset(
    {"id", "task_id", "data", "raw", "failed_scenes", "create_time", "update_time"}
)
CHARACTERS_REQUIRED_COLUMNS = frozenset(
    {"id", "task_id", "data", "raw", "create_time", "update_time"}
)

_GENERIC_TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS {table_name} (
        id SERIAL PRIMARY KEY,
        task_id TEXT NOT NULL UNIQUE,
        data JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        create_time TIMESTAMP NOT NULL DEFAULT NOW(),
        update_time TIMESTAMP NOT NULL DEFAULT NOW()
    )
"""


def _execute_many(connection: Any, statements: Iterable[str]) -> None:
    for statement in statements:
        connection.execute(text(statement))


def _ensure_scenes_table(connection: Any) -> None:
    """Create or upgrade the task-scoped scenes handoff table."""

    _execute_many(
        connection,
        (
            """
            CREATE TABLE IF NOT EXISTS scenes (
                id SERIAL PRIMARY KEY,
                task_id TEXT,
                data JSONB NOT NULL DEFAULT '{}'::jsonb,
                raw JSONB,
                failed_scenes JSONB,
                create_time TIMESTAMP NOT NULL DEFAULT NOW(),
                update_time TIMESTAMP NOT NULL DEFAULT NOW()
            )
            """,
            "ALTER TABLE scenes ADD COLUMN IF NOT EXISTS task_id TEXT",
            "ALTER TABLE scenes ADD COLUMN IF NOT EXISTS raw JSONB",
            "ALTER TABLE scenes ADD COLUMN IF NOT EXISTS failed_scenes JSONB",
            "ALTER TABLE scenes ADD COLUMN IF NOT EXISTS update_time TIMESTAMP NOT NULL DEFAULT NOW()",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_scenes_task_id_local ON scenes(task_id)",
        ),
    )


def _ensure_name_anonymizer_state(connection: Any) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS name_anonymizer_state (
                id SERIAL PRIMARY KEY,
                task_id TEXT NOT NULL,
                artifact TEXT NOT NULL,
                mappings_digest TEXT NOT NULL,
                content_digest TEXT NOT NULL,
                replacement_count INTEGER NOT NULL DEFAULT 0,
                verified BOOLEAN NOT NULL DEFAULT FALSE,
                renamed_at TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE (task_id, artifact)
            )
            """
        )
    )


def ensure_local_pipeline_schema(
    *,
    platform_db_engine: Any = None,
    business_db_engine: Any = None,
) -> None:
    """Create and migrate all tables required by the local visual pipeline."""

    if platform_db_engine is None:
        from common.platform_db import platform_engine as platform_db_engine
    if business_db_engine is None:
        from common.task_models import engine as business_db_engine

    with platform_db_engine.begin() as connection:
        _execute_many(
            connection,
            (
                *(
                    _GENERIC_TABLE_DDL.format(table_name=table_name)
                    for table_name in LOCAL_VISUAL_TABLES
                ),
                "ALTER TABLE characters ADD COLUMN IF NOT EXISTS raw JSONB",
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_plots_task_id_local ON plots(task_id)",
            ),
        )
        _ensure_name_anonymizer_state(connection)

    with business_db_engine.begin() as connection:
        _execute_many(
            connection,
            (
                *(
                    _GENERIC_TABLE_DDL.format(table_name=table_name)
                    for table_name in LOCAL_BUSINESS_TABLES
                ),
            ),
        )
        _ensure_scenes_table(connection)
