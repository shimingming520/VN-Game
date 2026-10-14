from __future__ import annotations

from dataclasses import dataclass
import logging
import re
import threading
from typing import Callable

import psycopg2

from common.config import PLATFORM_DATABASE_URL


logger = logging.getLogger(__name__)

DEFAULT_QUOTA_LIMIT = 300
LEGACY_QUOTA_LIMIT = 500
# Kept for callers that imported the old name.  Runtime limits come from the
# quota row's ``quota_limit`` column, never from this compatibility alias.
MAX_SUCCESSFUL_GENERATIONS = DEFAULT_QUOTA_LIMIT
SUCCESS_COLUMN = {
    "bgm": "bgm_success_count",
    "sfx": "sfx_success_count",
    "ambience": "ambience_success_count",
}


class QuotaSchemaError(RuntimeError):
    """The quota table exists but violates the expected schema contract."""

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS public.sound_generation_quota (
    task_id VARCHAR(255) PRIMARY KEY,
    bgm_success_count INTEGER NOT NULL DEFAULT 0
        CHECK (bgm_success_count >= 0),
    sfx_success_count INTEGER NOT NULL DEFAULT 0
        CHECK (sfx_success_count >= 0),
    ambience_success_count INTEGER NOT NULL DEFAULT 0
        CHECK (ambience_success_count >= 0),
    reserved_count INTEGER NOT NULL DEFAULT 0
        CHECK (reserved_count >= 0),
    quota_limit INTEGER NOT NULL DEFAULT 300,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT sound_generation_quota_limit_check
        CHECK (quota_limit IN (300, 500)),
    CONSTRAINT sound_generation_quota_total_check CHECK (
        bgm_success_count
        + sfx_success_count
        + ambience_success_count
        + reserved_count <= quota_limit
    )
);
"""

SCHEMA_LOCK_SQL = """
SELECT pg_advisory_xact_lock(
    hashtextextended('sound_generation_quota_schema_v2', 0)
);
"""

ADD_QUOTA_LIMIT_COLUMN_SQL = """
ALTER TABLE public.sound_generation_quota
ADD COLUMN IF NOT EXISTS quota_limit INTEGER;
"""

BACKFILL_QUOTA_LIMIT_SQL = """
UPDATE public.sound_generation_quota
SET quota_limit = 500
WHERE quota_limit IS NULL;
"""

SET_QUOTA_LIMIT_NOT_NULL_SQL = """
ALTER TABLE public.sound_generation_quota
ALTER COLUMN quota_limit SET NOT NULL;
"""

SET_QUOTA_LIMIT_DEFAULT_SQL = """
ALTER TABLE public.sound_generation_quota
ALTER COLUMN quota_limit SET DEFAULT 300;
"""

CONSTRAINTS_SQL = """
SELECT c.conname, pg_get_constraintdef(c.oid)
FROM pg_constraint AS c
JOIN pg_class AS r ON r.oid = c.conrelid
JOIN pg_namespace AS n ON n.oid = r.relnamespace
WHERE n.nspname = 'public'
  AND r.relname = 'sound_generation_quota'
  AND c.contype = 'c';
"""

INSERT_TASK_SQL = """
INSERT INTO public.sound_generation_quota (task_id, quota_limit)
VALUES (%s, 300)
ON CONFLICT (task_id) DO NOTHING;
"""

RESERVE_SQL = """
UPDATE public.sound_generation_quota
SET reserved_count = reserved_count + 1,
    updated_at = NOW()
WHERE task_id = %s
  AND bgm_success_count
      + sfx_success_count
      + ambience_success_count
      + reserved_count < quota_limit
RETURNING bgm_success_count,
          sfx_success_count,
          ambience_success_count,
          reserved_count,
          quota_limit;
"""

RELEASE_SQL = """
UPDATE public.sound_generation_quota
SET reserved_count = GREATEST(reserved_count - 1, 0),
    updated_at = NOW()
WHERE task_id = %s
RETURNING bgm_success_count,
          sfx_success_count,
          ambience_success_count,
          reserved_count,
          quota_limit;
"""

SNAPSHOT_SQL = """
SELECT bgm_success_count,
       sfx_success_count,
       ambience_success_count,
       reserved_count,
       quota_limit
FROM public.sound_generation_quota
WHERE task_id = %s;
"""

EXPECTED_TOTAL_CONSTRAINT = "sound_generation_quota_total_check"
EXPECTED_LIMIT_CONSTRAINT = "sound_generation_quota_limit_check"
_COUNT_COLUMNS = (
    "bgm_success_count",
    "sfx_success_count",
    "ambience_success_count",
    "reserved_count",
)


def _constraint_identifier(name: str) -> str:
    """Quote a PostgreSQL identifier returned by the catalog."""
    return '"' + str(name).replace('"', '""') + '"'


def _is_fixed_total_constraint(definition: object) -> bool:
    """Return whether a CHECK is the old hard-coded total <= 500 check."""
    normalized = re.sub(r"\s+", "", str(definition).lower())
    compact = normalized.replace("check", "", 1).replace("(", "").replace(")", "")
    return (
        compact
        == "bgm_success_count+sfx_success_count+ambience_success_count+reserved_count<=500"
    )


def _is_expected_total_constraint(definition: object) -> bool:
    normalized = re.sub(r"\s+", "", str(definition).lower())
    compact = normalized.replace("check", "", 1).replace("(", "").replace(")", "")
    return compact == (
        "bgm_success_count+sfx_success_count+ambience_success_count+"
        "reserved_count<=quota_limit"
    )


def _is_expected_limit_constraint(definition: object) -> bool:
    normalized = re.sub(r"\s+", "", str(definition).lower())
    if normalized.startswith("check"):
        normalized = normalized[len("check") :]

    # pg_get_constraintdef() may wrap the expression in several redundant
    # parentheses.  Remove only balanced outer wrappers so the remaining
    # expression can be compared in full; do not accept a valid fragment with
    # an additional predicate appended to it.
    while (
        len(normalized) >= 2
        and normalized[0] == "("
        and normalized[-1] == ")"
    ):
        depth = 0
        encloses_entire_expression = True
        for index, char in enumerate(normalized):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0 and index != len(normalized) - 1:
                    encloses_entire_expression = False
                    break
        if not encloses_entire_expression or depth != 0:
            break
        normalized = normalized[1:-1]

    if normalized == "quota_limitin(300,500)":
        return True

    return re.fullmatch(
        r"quota_limit=any\(array\[300,500\]"
        r"(?:::[a-z_][a-z0-9_.]*(?:\[\])?)?\)",
        normalized,
    ) is not None


def ensure_quota_schema(
    connection_factory: Callable[[], object] | None = None,
) -> bool:
    """Create or idempotently upgrade the per-task quota table.

    Existing rows are deliberately backfilled to the legacy 500 limit before
    the new row-level CHECK is installed.  The advisory lock makes the DDL
    safe when several workers start at the same time.
    """
    factory = connection_factory or (lambda: psycopg2.connect(PLATFORM_DATABASE_URL))
    conn = None
    try:
        conn = factory()
        with conn.cursor() as cur:
            cur.execute(SCHEMA_LOCK_SQL)
            cur.execute(CREATE_TABLE_SQL)
            cur.execute(ADD_QUOTA_LIMIT_COLUMN_SQL)
            cur.execute(BACKFILL_QUOTA_LIMIT_SQL)
            cur.execute(SET_QUOTA_LIMIT_NOT_NULL_SQL)
            cur.execute(SET_QUOTA_LIMIT_DEFAULT_SQL)

            cur.execute(CONSTRAINTS_SQL)
            constraints = cur.fetchall() or []
            by_name = {
                str(row[0]): row[1]
                for row in constraints
                if row and len(row) >= 2
            }

            existing_limit_definition = by_name.get(EXPECTED_LIMIT_CONSTRAINT)
            if (
                existing_limit_definition is not None
                and not _is_expected_limit_constraint(existing_limit_definition)
            ):
                raise QuotaSchemaError(
                    f"unexpected definition for {EXPECTED_LIMIT_CONSTRAINT}: "
                    f"{existing_limit_definition!r}"
                )
            existing_total_definition = by_name.get(EXPECTED_TOTAL_CONSTRAINT)
            if (
                existing_total_definition is not None
                and not _is_expected_total_constraint(existing_total_definition)
            ):
                raise QuotaSchemaError(
                    f"unexpected definition for {EXPECTED_TOTAL_CONSTRAINT}: "
                    f"{existing_total_definition!r}"
                )

            for name, definition in by_name.items():
                if _is_fixed_total_constraint(definition):
                    cur.execute(
                        "ALTER TABLE public.sound_generation_quota "
                        f"DROP CONSTRAINT IF EXISTS {_constraint_identifier(name)};"
                    )

            limit_definition = by_name.get(EXPECTED_LIMIT_CONSTRAINT)
            if limit_definition is None:
                cur.execute(
                    "ALTER TABLE public.sound_generation_quota "
                    f"ADD CONSTRAINT {_constraint_identifier(EXPECTED_LIMIT_CONSTRAINT)} "
                    "CHECK (quota_limit IN (300, 500));"
                )

            total_definition = by_name.get(EXPECTED_TOTAL_CONSTRAINT)
            if total_definition is None:
                cur.execute(
                    "ALTER TABLE public.sound_generation_quota "
                    f"ADD CONSTRAINT {_constraint_identifier(EXPECTED_TOTAL_CONSTRAINT)} "
                    "CHECK (bgm_success_count + sfx_success_count "
                    "+ ambience_success_count + reserved_count <= quota_limit);"
                )
        conn.commit()
        return True
    except Exception:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        if conn is not None:
            conn.close()


@dataclass(frozen=True)
class QuotaSnapshot:
    bgm_success_count: int
    sfx_success_count: int
    ambience_success_count: int
    reserved_count: int
    quota_limit: int = DEFAULT_QUOTA_LIMIT

    @property
    def success_counts(self) -> dict[str, int]:
        return {
            "bgm": self.bgm_success_count,
            "sfx": self.sfx_success_count,
            "ambience": self.ambience_success_count,
        }

    @property
    def successful_count(self) -> int:
        return sum(self.success_counts.values())

    @property
    def remaining(self) -> int:
        return max(
            self.quota_limit
            - self.successful_count
            - self.reserved_count,
            0,
        )


class GenerationQuotaLease:
    def __init__(
        self,
        quota: "SoundGenerationQuota",
        audio_type: str,
        event_id: str,
    ):
        self.quota = quota
        self.audio_type = audio_type
        self.event_id = event_id
        self.state = "pending"
        self._state_lock = threading.Lock()

    def consume(self) -> bool:
        with self._state_lock:
            if self.state != "pending":
                return False
            if self.quota._consume(self.audio_type):
                self.state = "consumed"
                return True
            self.state = "provider_success_uncommitted"
            return False

    def release(self) -> bool:
        with self._state_lock:
            if self.state != "pending":
                return False
            if not self.quota._release():
                return False
            self.state = "released"
            return True

    def release_if_pending(self) -> bool:
        return self.release()


class SoundGenerationQuota:
    def __init__(
        self,
        task_id: str,
        *,
        connection_factory: Callable[[], object] | None = None,
    ):
        self.task_id = str(task_id)
        self._connection_factory = connection_factory or (
            lambda: psycopg2.connect(PLATFORM_DATABASE_URL)
        )
        self._table_ready = False
        self._table_lock = threading.Lock()

    def _connect(self):
        return self._connection_factory()

    @staticmethod
    def _snapshot_from_row(row) -> QuotaSnapshot | None:
        if not row:
            return None
        # A four-column row can only come from a pre-migration test/connection;
        # such rows use the legacy limit until schema alignment completes.
        quota_limit = (
            int(row[4]) if len(row) > 4 and row[4] is not None
            else LEGACY_QUOTA_LIMIT
        )
        return QuotaSnapshot(
            bgm_success_count=int(row[0]),
            sfx_success_count=int(row[1]),
            ambience_success_count=int(row[2]),
            reserved_count=int(row[3]),
            quota_limit=quota_limit,
        )

    def _ensure_table(self) -> bool:
        if self._table_ready:
            return True
        with self._table_lock:
            if self._table_ready:
                return True
            try:
                ready = ensure_quota_schema(self._connection_factory)
                if ready:
                    self._table_ready = True
                return ready
            except QuotaSchemaError as exc:
                logger.critical(
                    "sound_generation_quota_schema_fatal: task_id=%s error=%s",
                    self.task_id,
                    exc,
                )
                return False
            except Exception as exc:
                logger.error(
                    "sound_generation_quota_unavailable: task_id=%s error=%s",
                    self.task_id,
                    exc,
                )
                return False

    def _initialize_task_row(self, conn) -> None:
        with conn.cursor() as cur:
            cur.execute(INSERT_TASK_SQL, (self.task_id,))

    def snapshot(self) -> QuotaSnapshot | None:
        if not self._ensure_table():
            return None
        conn = None
        try:
            conn = self._connect()
            self._initialize_task_row(conn)
            with conn.cursor() as cur:
                cur.execute(SNAPSHOT_SQL, (self.task_id,))
                row = cur.fetchone()
            conn.commit()
            return self._snapshot_from_row(row)
        except Exception as exc:
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
            logger.error(
                "sound_generation_quota_snapshot_failed: task_id=%s error=%s",
                self.task_id,
                exc,
            )
            return None
        finally:
            if conn is not None:
                conn.close()

    def try_reserve(
        self,
        audio_type: str,
        event_id: str,
    ) -> GenerationQuotaLease | None:
        if audio_type not in SUCCESS_COLUMN:
            logger.error(
                "sound_generation_quota_invalid_audio_type: task_id=%s "
                "audio_type=%s event_id=%s",
                self.task_id,
                audio_type,
                event_id,
            )
            return None
        if not self._ensure_table():
            return None

        conn = None
        try:
            conn = self._connect()
            self._initialize_task_row(conn)
            with conn.cursor() as cur:
                cur.execute(RESERVE_SQL, (self.task_id,))
                row = cur.fetchone()
            conn.commit()
            if row is None:
                return None
            return GenerationQuotaLease(self, audio_type, str(event_id))
        except Exception as exc:
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
            logger.error(
                "sound_generation_quota_reserve_failed: task_id=%s "
                "audio_type=%s event_id=%s error=%s",
                self.task_id,
                audio_type,
                event_id,
                exc,
            )
            return None
        finally:
            if conn is not None:
                conn.close()

    def _release(self) -> bool:
        if not self._ensure_table():
            return False
        conn = None
        try:
            conn = self._connect()
            with conn.cursor() as cur:
                cur.execute(RELEASE_SQL, (self.task_id,))
                row = cur.fetchone()
            conn.commit()
            return row is not None
        except Exception as exc:
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
            logger.error(
                "sound_generation_quota_release_failed: task_id=%s error=%s",
                self.task_id,
                exc,
            )
            return False
        finally:
            if conn is not None:
                conn.close()

    def _consume(self, audio_type: str) -> bool:
        column = SUCCESS_COLUMN.get(audio_type)
        if column is None or not self._ensure_table():
            return False
        sql = f"""
        UPDATE public.sound_generation_quota
        SET reserved_count = reserved_count - 1,
            {column} = {column} + 1,
            updated_at = NOW()
        WHERE task_id = %s
          AND reserved_count > 0
        RETURNING bgm_success_count,
                  sfx_success_count,
                  ambience_success_count,
                  reserved_count,
                  quota_limit;
        """
        conn = None
        try:
            conn = self._connect()
            with conn.cursor() as cur:
                cur.execute(sql, (self.task_id,))
                row = cur.fetchone()
            conn.commit()
            return row is not None
        except Exception as exc:
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
            logger.error(
                "sound_generation_quota_consume_failed: task_id=%s "
                "audio_type=%s error=%s",
                self.task_id,
                audio_type,
                exc,
            )
            return False
        finally:
            if conn is not None:
                conn.close()
