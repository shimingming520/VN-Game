import os
import threading
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from common.stage_fencing import (
    StageFenceOwnershipError,
    claim_stage_fence,
    ensure_stage_fencing_schema,
    lock_and_validate_stage_fence,
    reset_schema_cache_for_tests,
)


class _FakeRedis:
    def __init__(self, value):
        self.value = value
        self.expired = False

    def eval(self, script, _keys, _key, owner, *args):
        if "redis.call('expire'" in script:
            if self.value is None:
                return 0
            if self.value != owner:
                return -1
            self.expired = True
            return 1
        if "redis.call('del'" in script:
            if self.value != owner:
                return 0
            self.value = None
            return 1
        raise AssertionError("unexpected script")


class RedisLeaseAtomicContractTests(unittest.TestCase):
    def test_auxiliary_stage_keeps_legacy_non_fenced_result_contract(self):
        from common import progress_utils

        with patch.object(
            progress_utils, "update_task_progress", return_value=False
        ) as update:
            self.assertFalse(
                progress_utils.complete_stage("task-aux", "char_redraw", 20)
            )
            self.assertFalse(
                progress_utils.fail_stage(
                    "task-aux", "char_redraw", 10, "redraw failed"
                )
            )
        self.assertEqual(2, update.call_count)

    def test_stale_owner_cannot_renew_or_release_recovery_owner(self):
        from common import progress_utils

        redis_client = _FakeRedis("worker-b")
        key = progress_utils._stage_lease_key("task-1", "story_planner")
        with patch.object(progress_utils, "_stage_redis", redis_client):
            self.assertEqual(
                progress_utils._LEASE_FOREIGN_OWNER,
                progress_utils._compare_and_expire_state(key, "worker-a"),
            )
            self.assertFalse(
                progress_utils._compare_and_delete(key, "worker-a")
            )
        self.assertEqual("worker-b", redis_client.value)
        self.assertFalse(redis_client.expired)

    def test_loss_is_permanent_for_owner_even_after_same_key_is_reused(self):
        from common import progress_utils

        key = progress_utils._stage_lease_key("task-2", "scene_painter")
        stop = threading.Event()
        with progress_utils._lease_guard:
            progress_utils._active_leases[key] = ("worker-a", stop)
            progress_utils._lost_stage_leases.clear()
        progress_utils._mark_stage_lease_lost(key, "worker-a")
        with progress_utils._lease_guard:
            progress_utils._active_leases[key] = ("worker-b", threading.Event())
            progress_utils._lost_stage_leases.discard(key)
            self.assertIn(
                (key, "worker-a"), progress_utils._lost_stage_leases
            )
        self.assertTrue(stop.is_set())


class StageFenceSQLiteContractTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        with self.engine.begin() as connection:
            connection.execute(text("""
                CREATE TABLE novel_tasks (
                    task_id TEXT PRIMARY KEY,
                    story_planner_status INTEGER NOT NULL,
                    overall_status INTEGER NOT NULL DEFAULT 0,
                    progress_percentage INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT
                )
            """))
            connection.execute(
                text(
                    "INSERT INTO novel_tasks (task_id, story_planner_status) "
                    "VALUES ('task-1', 0)"
                )
            )
        reset_schema_cache_for_tests()

    def tearDown(self):
        self.engine.dispose()
        reset_schema_cache_for_tests()

    def test_takeover_increments_token_and_permanently_fences_old_owner(self):
        first = claim_stage_fence(
            self.engine,
            task_id="task-1",
            stage="story_planner",
            owner="worker-a",
            allowed_statuses=frozenset({0}),
            processing_status=1,
        ).identity
        second = claim_stage_fence(
            self.engine,
            task_id="task-1",
            stage="story_planner",
            owner="worker-b",
            allowed_statuses=frozenset({1}),
            processing_status=1,
        ).identity

        self.assertEqual(first.token + 1, second.token)
        with Session(self.engine) as session:
            with self.assertRaises(StageFenceOwnershipError):
                lock_and_validate_stage_fence(session, first)
            session.rollback()
            lock_and_validate_stage_fence(session, second)

    def test_pending_claim_failure_is_persisted_as_terminal_failure(self):
        from common import progress_utils

        with patch.object(progress_utils, "engine", self.engine):
            self.assertTrue(
                progress_utils.fail_pending_stage_claim(
                    "task-1", "story_planner", 10, "redis unavailable"
                )
            )
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT story_planner_status, overall_status, error_message "
                    "FROM novel_tasks WHERE task_id='task-1'"
                )
            ).one()
        self.assertEqual((3, 3, "redis unavailable"), tuple(row))


@unittest.skipUnless(
    os.getenv("STAGE_FENCE_TEST_DSN")
    and os.getenv("STAGE_FENCE_TEST_ALLOW_DROP") == "1",
    "set an isolated STAGE_FENCE_TEST_DSN and STAGE_FENCE_TEST_ALLOW_DROP=1",
)
class StageFencePostgreSQLRollingUpgradeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dsn = os.environ["STAGE_FENCE_TEST_DSN"]
        database_name = make_url(dsn).database or ""
        if not database_name.startswith("stage_test"):
            raise RuntimeError(
                "destructive fence integration test requires an isolated "
                "database whose name starts with 'stage_test'"
            )
        cls.engine = create_engine(dsn)
        with cls.engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS public.novel_tasks CASCADE"))
            connection.execute(text("DROP TABLE IF EXISTS public.stage_execution_fences CASCADE"))
            connection.execute(text("DROP FUNCTION IF EXISTS public.novel_game_enforce_stage_fence() CASCADE"))
            columns = ",".join(
                f"{stage}_status INTEGER NOT NULL DEFAULT 0"
                for stage in (
                    "story_planner", "info_extractor", "sound_arranger",
                    "char_designer", "audio_producer", "scene_painter",
                    "game_builder",
                )
            )
            connection.execute(text(
                f"CREATE TABLE public.novel_tasks (task_id TEXT PRIMARY KEY, {columns})"
            ))
        reset_schema_cache_for_tests()
        ensure_stage_fencing_schema(cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        reset_schema_cache_for_tests()

    def setUp(self):
        with self.engine.begin() as connection:
            connection.execute(text("TRUNCATE public.stage_execution_fences, public.novel_tasks"))

    def test_legacy_unfenced_allowed_old_owner_rejected_new_owner_succeeds(self):
        with self.engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO public.novel_tasks (task_id) VALUES ('legacy'), ('fenced')"
            ))
            # A rolling-upgrade task never claimed by new code remains compatible.
            connection.execute(text(
                "UPDATE public.novel_tasks SET story_planner_status=2 WHERE task_id='legacy'"
            ))

        identity = claim_stage_fence(
            self.engine,
            task_id="fenced",
            stage="story_planner",
            owner="new-worker",
            allowed_statuses=frozenset({0}),
            processing_status=1,
        ).identity

        with self.assertRaises(Exception):
            with self.engine.begin() as connection:
                # Simulates an old image: no transaction-local owner/token.
                connection.execute(text(
                    "UPDATE public.novel_tasks SET story_planner_status=2 WHERE task_id='fenced'"
                ))

        with Session(self.engine) as session:
            lock_and_validate_stage_fence(session, identity)
            session.execute(text(
                "UPDATE public.novel_tasks SET story_planner_status=2 WHERE task_id='fenced'"
            ))
            session.commit()

        with self.engine.connect() as connection:
            self.assertEqual(2, connection.execute(text(
                "SELECT story_planner_status FROM public.novel_tasks WHERE task_id='fenced'"
            )).scalar_one())
            # SET LOCAL cannot leak through the pooled connection.
            self.assertIn(
                connection.execute(text(
                    "SELECT current_setting('novel_game.stage_fence_owner', true)"
                )).scalar_one_or_none(),
                (None, ""),
            )


if __name__ == "__main__":
    unittest.main()
