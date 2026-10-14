import json
import logging
from datetime import datetime, timezone
import os
import unittest
from unittest.mock import MagicMock, patch

import common.progress_utils as progress_utils
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlmodel import SQLModel, Session, select

from services.story_planner.table.models import ChapterBranchPlan

from services.story_planner.core import runtime_observability as observability


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class RuntimeObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger(f"test.runtime.{id(self)}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.handler = _ListHandler()
        self.logger.addHandler(self.handler)

    def tearDown(self):
        observability.stop_all_runtime_heartbeats()
        self.logger.handlers.clear()

    def test_structured_log_keeps_context_and_redacts_sensitive_fields(self):
        payload = observability.structured_log(
            self.logger,
            "node_started",
            task_id="task-1",
            run_id="run-1",
            topo_id="topo-1",
            real_id="node-1",
            branch_type="romance",
            api_key="secret",
            prompt="小说正文",
        )

        logged = json.loads(self.handler.messages[-1])
        self.assertEqual("node_started", logged["event"])
        self.assertEqual("task-1", logged["task_id"])
        self.assertEqual("topo-1", logged["topo_id"])
        self.assertNotIn("api_key", logged)
        self.assertNotIn("prompt", logged)
        self.assertEqual(logged, payload)

    def test_phase_timer_records_duration_and_completion_event(self):
        ticks = iter([10.0, 12.5])
        metrics = {"phase_seconds": {}}

        with observability.phase_timer(
            metrics,
            "quality_gate",
            logger=self.logger,
            log_fields={"task_id": "task-1"},
            clock=lambda: next(ticks),
        ):
            pass

        self.assertEqual(2.5, metrics["phase_seconds"]["quality_gate"])
        events = [json.loads(message)["event"] for message in self.handler.messages]
        self.assertEqual(["phase_started", "phase_completed"], events)

    def test_heartbeat_updates_plan_and_stage_lease(self):
        updates = []
        renewals = []
        ticks = iter([100.0, 103.25])
        heartbeat = observability.RuntimeHeartbeat(
            task_id="task-1",
            run_id="run-1",
            branch_type="default",
            topo_id="topo-1",
            real_id="node-1",
            interval_seconds=60,
            plan_updater=lambda payload: updates.append(payload),
            lease_renewer=lambda task_id, stage: renewals.append((task_id, stage)),
            logger=self.logger,
            clock=lambda: next(ticks),
            utcnow=lambda: datetime(2026, 8, 11, 2, 0, tzinfo=timezone.utc),
        )

        heartbeat.beat(status="running")

        self.assertEqual([("task-1", "story_planner")], renewals)
        self.assertEqual("topo-1", updates[0]["topo_id"])
        self.assertEqual(3.25, updates[0]["elapsed_seconds"])
        self.assertEqual("node_heartbeat", json.loads(self.handler.messages[-1])["event"])

    def test_detect_stalled_node_returns_structured_error(self):
        node = {
            "status": "running",
            "heartbeat_at": "2026-08-11T01:00:00+00:00",
        }

        issue = observability.detect_stalled_node(
            "topo-1",
            node,
            now=datetime(2026, 8, 11, 1, 31, tzinfo=timezone.utc),
            timeout_seconds=1800,
        )

        self.assertEqual("node_stalled", issue["error_category"])
        self.assertEqual("topo-1", issue["topo_id"])
        self.assertEqual(1860, issue["stalled_seconds"])

    def test_completed_or_fresh_node_is_not_stalled(self):
        now = datetime(2026, 8, 11, 1, 10, tzinfo=timezone.utc)
        self.assertIsNone(
            observability.detect_stalled_node(
                "done",
                {"status": "completed", "heartbeat_at": "2026-08-11T00:00:00+00:00"},
                now=now,
                timeout_seconds=60,
            )
        )
        self.assertIsNone(
            observability.detect_stalled_node(
                "fresh",
                {"status": "running", "heartbeat_at": "2026-08-11T01:09:30+00:00"},
                now=now,
                timeout_seconds=60,
            )
        )

    def test_exhausted_consistency_repair_is_classified_as_release_gate(self):
        category = observability.classify_runtime_error(
            ValueError("结局节点一致性门禁失败：开放悬念仍存在硬冲突")
        )

        self.assertEqual("release_gate", category)

    def test_public_stage_lease_renewal_extends_only_owned_lease(self):
        task_id = "task-lease"
        stage = "story_planner"
        key = progress_utils._stage_lease_key(task_id, stage)
        stop = __import__("threading").Event()
        fake_redis = MagicMock()
        fake_redis.eval.return_value = progress_utils._LEASE_RENEWED

        with patch.object(progress_utils, "_stage_redis", fake_redis), patch.dict(
            progress_utils._active_leases,
            {key: ("owner-1", stop)},
            clear=True,
        ):
            renewed = progress_utils.renew_stage_lease(task_id, stage)

        self.assertTrue(renewed)
        fake_redis.eval.assert_called_once_with(
            progress_utils._COMPARE_AND_EXPIRE_SCRIPT,
            1,
            key,
            "owner-1",
            progress_utils._STAGE_LEASE_TTL,
        )

    def test_plan_heartbeat_persists_current_node_without_accepting_stale_run(self):
        os.environ["STORY_PLANNER_SMOKE_MODE"] = "1"
        from services.story_planner import tasks

        engine = create_engine("sqlite://", future=True).execution_options(
            schema_translate_map={"public": None}
        )
        SQLModel.metadata.create_all(engine, tables=[ChapterBranchPlan.__table__])
        with Session(engine) as session:
            session.add(
                ChapterBranchPlan(
                    task_id="task-1",
                    topology_json={
                        "run_id": "run-new",
                        "nodes": {"topo-1": {"status": "running"}},
                    },
                )
            )
            session.commit()

        payload = {
            "run_id": "run-new",
            "topo_id": "topo-1",
            "real_id": "node-1",
            "stage": "story_planner",
            "status": "running",
            "started_at": "2026-08-11T01:00:00+00:00",
            "heartbeat_at": "2026-08-11T01:01:00+00:00",
            "elapsed_seconds": 60.0,
        }
        self.assertTrue(
            tasks._record_chapter_branch_heartbeat(
                "task-1", payload, db_engine=engine
            )
        )
        stale = dict(payload, run_id="run-old", heartbeat_at="2099-01-01T00:00:00+00:00")
        self.assertFalse(
            tasks._record_chapter_branch_heartbeat(
                "task-1", stale, db_engine=engine
            )
        )

        with Session(engine) as session:
            row = session.exec(
                select(ChapterBranchPlan).where(ChapterBranchPlan.task_id == "task-1")
            ).one()
        self.assertEqual(
            "2026-08-11T01:01:00+00:00",
            row.topology_json["nodes"]["topo-1"]["heartbeat_at"],
        )
        self.assertEqual("topo-1", row.topology_json["runtime"]["current_topo_id"])


if __name__ == "__main__":
    unittest.main()
