"""PostgreSQL fencing for long-running stage workers.

Redis leases answer whether a worker is still live, but Redis and PostgreSQL
cannot participate in one transaction.  A worker that pauses immediately
before its final database write can therefore outlive its Redis lease and
overwrite a recovery worker.  This module makes PostgreSQL the serialization
point for terminal stage writes:

* every successful execution claim receives a monotonically increasing token;
* claim/takeover and the corresponding ``novel_tasks`` row are locked together;
* terminal writes must present the current owner/token in the same transaction;
* a small trigger also rejects terminal writes from rolling-upgrade workers
  that predate the Python-side fence protocol.

The schema is installed lazily and idempotently.  Existing tasks without a
fence row retain their legacy behaviour until an upgraded worker claims them.
"""

from __future__ import annotations

import threading
import weakref
from dataclasses import dataclass
from typing import FrozenSet, Optional

from sqlalchemy import text


VALID_STAGE_NAMES = (
    "story_planner",
    "info_extractor",
    "sound_arranger",
    "char_designer",
    "audio_producer",
    "scene_painter",
    "game_builder",
)

_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY_ENGINES: "weakref.WeakKeyDictionary[object, bool]" = (
    weakref.WeakKeyDictionary()
)

_CREATE_FENCE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS public.stage_execution_fences (
    task_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    owner TEXT NOT NULL,
    fencing_token BIGINT NOT NULL CHECK (fencing_token > 0),
    claimed_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (task_id, stage),
    CONSTRAINT ck_stage_execution_fences_stage CHECK (
        stage IN (
            'story_planner',
            'info_extractor',
            'sound_arranger',
            'char_designer',
            'audio_producer',
            'scene_painter',
            'game_builder'
        )
    )
)
"""


def _trigger_checks_sql() -> str:
    checks = []
    for stage in VALID_STAGE_NAMES:
        column = f"{stage}_status"
        checks.append(
            f"""
    IF NEW.{column} IS DISTINCT FROM OLD.{column}
       AND NEW.{column} IN (2, 3) THEN
        SELECT owner, fencing_token
          INTO expected_owner, expected_token
          FROM public.stage_execution_fences
         WHERE task_id = NEW.task_id AND stage = '{stage}';

        IF FOUND AND (
            transaction_owner IS DISTINCT FROM expected_owner
            OR transaction_token IS DISTINCT FROM expected_token
        ) THEN
            RAISE EXCEPTION
                'stale stage terminal write rejected: task_id=%, stage=%',
                NEW.task_id, '{stage}'
                USING ERRCODE = '55000';
        END IF;
    END IF;
"""
        )
    return "".join(checks)


_CREATE_TRIGGER_FUNCTION_SQL = f"""
CREATE OR REPLACE FUNCTION public.novel_game_enforce_stage_fence()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
DECLARE
    expected_owner TEXT;
    expected_token BIGINT;
    transaction_owner TEXT;
    transaction_token BIGINT;
BEGIN
    transaction_owner := current_setting(
        'novel_game.stage_fence_owner', true
    );
    transaction_token := NULLIF(
        current_setting('novel_game.stage_fence_token', true), ''
    )::BIGINT;
{_trigger_checks_sql()}
    RETURN NEW;
END;
$function$
"""

_CREATE_TRIGGER_SQL = """
DO $block$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_trigger
         WHERE tgname = 'trg_novel_game_stage_fence'
           AND tgrelid = 'public.novel_tasks'::regclass
           AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_novel_game_stage_fence
        BEFORE UPDATE ON public.novel_tasks
        FOR EACH ROW
        EXECUTE FUNCTION public.novel_game_enforce_stage_fence();
    END IF;
END;
$block$
"""

_UPSERT_FENCE_SQL = text("""
INSERT INTO public.stage_execution_fences (
    task_id, stage, owner, fencing_token, claimed_at, updated_at
)
VALUES (
    :task_id, :stage, :owner, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
)
ON CONFLICT (task_id, stage) DO UPDATE
SET owner = EXCLUDED.owner,
    fencing_token = stage_execution_fences.fencing_token + 1,
    claimed_at = CURRENT_TIMESTAMP,
    updated_at = CURRENT_TIMESTAMP
RETURNING fencing_token
""")

_LOCK_FENCE_SQL = text("""
SELECT owner, fencing_token
  FROM public.stage_execution_fences
 WHERE task_id = :task_id AND stage = :stage
 FOR UPDATE
""")

_SET_TRANSACTION_FENCE_SQL = text("""
SELECT
    set_config('novel_game.stage_fence_owner', :owner, true),
    set_config('novel_game.stage_fence_token', :token, true)
""")


class StageFenceError(RuntimeError):
    """Base error for PostgreSQL stage-fence failures."""


class StageFenceOwnershipError(StageFenceError):
    """The caller's owner/token is no longer the database owner."""


class StageFenceClaimError(StageFenceError):
    """The task row cannot be safely claimed for execution."""


@dataclass(frozen=True)
class StageFenceIdentity:
    task_id: str
    stage: str
    owner: str
    token: int


@dataclass(frozen=True)
class StageFenceClaimResult:
    status: int
    identity: Optional[StageFenceIdentity]

    @property
    def claimed(self) -> bool:
        return self.identity is not None


def validate_stage_name(stage: str) -> str:
    if stage not in VALID_STAGE_NAMES:
        raise ValueError(f"unsupported fenced stage: {stage!r}")
    return stage


def ensure_stage_fencing_schema(engine) -> None:
    """Install the fence table and rolling-upgrade trigger once per engine.

    PostgreSQL is the production database.  SQLite is intentionally handled as
    a trigger-less test/development branch: Python-side owner/token checks still
    apply, while the PostgreSQL rolling-upgrade contract is exercised against a
    real temporary PostgreSQL instance.
    """

    with _SCHEMA_LOCK:
        if _SCHEMA_READY_ENGINES.get(engine):
            return

        dialect = engine.dialect.name
        if dialect == "sqlite":
            with engine.begin() as connection:
                connection.execute(text("""
                    CREATE TABLE IF NOT EXISTS stage_execution_fences (
                        task_id TEXT NOT NULL,
                        stage TEXT NOT NULL,
                        owner TEXT NOT NULL,
                        fencing_token BIGINT NOT NULL,
                        claimed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (task_id, stage)
                    )
                """))
            _SCHEMA_READY_ENGINES[engine] = True
            return

        if dialect != "postgresql":
            raise StageFenceError(
                f"stage fencing requires PostgreSQL, got {dialect!r}"
            )

        # The transaction-scoped advisory lock makes concurrent first starts
        # deterministic across Pods.  It is released automatically on commit
        # or rollback and does not leak through pooled connections.
        with engine.begin() as connection:
            connection.execute(text(
                "SELECT pg_advisory_xact_lock(1735357402, 20260818)"
            ))
            connection.execute(text(_CREATE_FENCE_TABLE_SQL))
            connection.execute(text(_CREATE_TRIGGER_FUNCTION_SQL))
            connection.execute(text(_CREATE_TRIGGER_SQL))

        _SCHEMA_READY_ENGINES[engine] = True


def reset_schema_cache_for_tests() -> None:
    with _SCHEMA_LOCK:
        _SCHEMA_READY_ENGINES.clear()


def claim_stage_fence(
    engine,
    *,
    task_id: str,
    stage: str,
    owner: str,
    allowed_statuses: FrozenSet[int],
    processing_status: int,
) -> StageFenceClaimResult:
    """Atomically validate the task state, advance its token, and mark running.

    The ``novel_tasks`` row is locked before the fence row.  Terminal writers
    use the same order, so a takeover and a completion have a single PostgreSQL
    linearization order even though the liveness lease itself is in Redis.
    """

    stage = validate_stage_name(stage)
    ensure_stage_fencing_schema(engine)
    status_column = f"{stage}_status"
    sqlite = engine.dialect.name == "sqlite"
    task_table = "novel_tasks" if sqlite else "public.novel_tasks"
    fence_table = (
        "stage_execution_fences"
        if sqlite else "public.stage_execution_fences"
    )
    lock_suffix = "" if sqlite else " FOR UPDATE"

    with engine.begin() as connection:
        current = connection.execute(
            text(
                f"SELECT {status_column} FROM {task_table} "
                f"WHERE task_id = :task_id{lock_suffix}"
            ),
            {"task_id": task_id},
        ).scalar_one_or_none()
        if current is None:
            raise StageFenceClaimError(
                f"[{stage}] task_id={task_id} does not exist"
            )

        current = int(current)
        if current not in allowed_statuses:
            return StageFenceClaimResult(status=current, identity=None)

        upsert = _UPSERT_FENCE_SQL if not sqlite else text(f"""
            INSERT INTO {fence_table} (
                task_id, stage, owner, fencing_token, claimed_at, updated_at
            ) VALUES (
                :task_id, :stage, :owner, 1,
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            ON CONFLICT (task_id, stage) DO UPDATE SET
                owner = excluded.owner,
                fencing_token = fencing_token + 1,
                claimed_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            RETURNING fencing_token
        """)
        token = int(connection.execute(
            upsert,
            {"task_id": task_id, "stage": stage, "owner": owner},
        ).scalar_one())

        if current != processing_status:
            connection.execute(
                text(
                    f"UPDATE {task_table} SET {status_column} = :status "
                    "WHERE task_id = :task_id"
                ),
                {"status": processing_status, "task_id": task_id},
            )

    return StageFenceClaimResult(
        status=processing_status,
        identity=StageFenceIdentity(
            task_id=task_id,
            stage=stage,
            owner=owner,
            token=token,
        ),
    )


def lock_and_validate_stage_fence(session, identity: StageFenceIdentity) -> None:
    """Lock and validate a fence, then install transaction-local trigger GUCs."""

    validate_stage_name(identity.stage)
    bind = session.get_bind()
    sqlite = bind.dialect.name == "sqlite"
    lock_sql = _LOCK_FENCE_SQL if not sqlite else text("""
        SELECT owner, fencing_token
          FROM stage_execution_fences
         WHERE task_id = :task_id AND stage = :stage
    """)
    row = session.execute(
        lock_sql,
        {"task_id": identity.task_id, "stage": identity.stage},
    ).first()
    if row is None:
        raise StageFenceOwnershipError(
            f"[{identity.stage}] task_id={identity.task_id} fence is missing"
        )

    owner, token = str(row[0]), int(row[1])
    if owner != identity.owner or token != identity.token:
        raise StageFenceOwnershipError(
            f"[{identity.stage}] task_id={identity.task_id} fence ownership lost"
        )

    # ``is_local=true`` is essential: the settings disappear at transaction
    # end and therefore cannot leak when SQLAlchemy returns the connection to
    # its pool.  Values are bound parameters, never interpolated into SQL.
    if not sqlite:
        session.execute(
            _SET_TRANSACTION_FENCE_SQL,
            {"owner": identity.owner, "token": str(identity.token)},
        )


def lock_and_validate_stage_fence_connection(connection, identity: StageFenceIdentity) -> None:
    """Connection-level equivalent used by fenced checkpoint upserts.

    Child image-generator processes inherit the parent's owner/token.  Their
    checkpoint transaction must verify that identity directly because the
    parent's thread-local SQLModel session is not available after ``fork/exec``.
    """

    validate_stage_name(identity.stage)
    sqlite = connection.dialect.name == "sqlite"
    table = "stage_execution_fences" if sqlite else "public.stage_execution_fences"
    lock_suffix = "" if sqlite else " FOR UPDATE"
    row = connection.execute(
        text(
            f"SELECT owner, fencing_token FROM {table} "
            f"WHERE task_id = :task_id AND stage = :stage{lock_suffix}"
        ),
        {"task_id": identity.task_id, "stage": identity.stage},
    ).first()
    if row is None:
        raise StageFenceOwnershipError(
            f"[{identity.stage}] task_id={identity.task_id} fence is missing"
        )
    owner, token = str(row[0]), int(row[1])
    if owner != identity.owner or token != identity.token:
        raise StageFenceOwnershipError(
            f"[{identity.stage}] task_id={identity.task_id} fence ownership lost"
        )
    if not sqlite:
        connection.execute(
            _SET_TRANSACTION_FENCE_SQL,
            {"owner": identity.owner, "token": str(identity.token)},
        )


def read_stage_fence(engine, task_id: str, stage: str):
    """Read-only diagnostic helper used by ownership checks and tests."""

    stage = validate_stage_name(stage)
    ensure_stage_fencing_schema(engine)
    table = (
        "stage_execution_fences"
        if engine.dialect.name == "sqlite"
        else "public.stage_execution_fences"
    )
    with engine.connect() as connection:
        row = connection.execute(
            text(f"""
                SELECT owner, fencing_token
                  FROM {table}
                 WHERE task_id = :task_id AND stage = :stage
            """),
            {"task_id": task_id, "stage": stage},
        ).first()
    if row is None:
        return None
    return str(row[0]), int(row[1])
