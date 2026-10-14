import unittest

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlmodel import SQLModel, Session, select

from services.story_planner.core.chapter_branch_checkpoint import (
    ChapterBranchPlanReadError,
    StalePlannerExecutionError,
    load_chapter_branch_plan,
    save_chapter_branch_plan,
)
from services.story_planner.table.models import ChapterBranchPlan
from services.story_planner.core.chapter_branch_runtime import PLAN_VERSION


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


class ChapterBranchCheckpointStoreTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", future=True)
        self.engine = engine.execution_options(
            schema_translate_map={"public": None}
        )
        SQLModel.metadata.create_all(
            self.engine,
            tables=[ChapterBranchPlan.__table__],
        )

    def _plan(self, run_id: str) -> dict:
        return {
            "version": PLAN_VERSION,
            "task_id": "task-1",
            "run_id": run_id,
            "topology_result": {"topology": []},
            "nodes": {},
        }

    def test_missing_plan_is_the_only_case_returning_empty(self):
        self.assertEqual(
            {},
            load_chapter_branch_plan("task-1", db_engine=self.engine),
        )

    def test_damaged_plan_is_not_treated_as_first_run(self):
        with Session(self.engine) as session:
            session.add(ChapterBranchPlan(task_id="task-1", topology_json=[]))
            session.commit()

        with self.assertRaisesRegex(ChapterBranchPlanReadError, "损坏"):
            load_chapter_branch_plan("task-1", db_engine=self.engine)

    def test_database_read_error_is_not_treated_as_first_run(self):
        broken_engine = create_engine("sqlite:///Z:/missing/checkpoints.db")

        with self.assertRaises(ChapterBranchPlanReadError):
            load_chapter_branch_plan("task-1", db_engine=broken_engine)

    def test_current_run_can_save_repeatedly(self):
        plan = self._plan("run-1")
        save_chapter_branch_plan(
            "task-1",
            plan,
            expected_run_id="",
            db_engine=self.engine,
        )
        plan["heartbeat_at"] = "now"
        save_chapter_branch_plan(
            "task-1",
            plan,
            expected_run_id="run-1",
            db_engine=self.engine,
        )

        loaded = load_chapter_branch_plan("task-1", db_engine=self.engine)
        self.assertEqual("run-1", loaded["run_id"])
        self.assertEqual("now", loaded["heartbeat_at"])

    def test_new_execution_must_explicitly_take_over_loaded_run(self):
        save_chapter_branch_plan(
            "task-1",
            self._plan("run-1"),
            expected_run_id="",
            db_engine=self.engine,
        )

        save_chapter_branch_plan(
            "task-1",
            self._plan("run-2"),
            expected_run_id="run-1",
            allow_run_takeover=True,
            db_engine=self.engine,
        )

        self.assertEqual(
            "run-2",
            load_chapter_branch_plan("task-1", db_engine=self.engine)["run_id"],
        )

    def test_old_execution_cannot_overwrite_new_execution(self):
        save_chapter_branch_plan(
            "task-1",
            self._plan("run-1"),
            expected_run_id="",
            db_engine=self.engine,
        )
        save_chapter_branch_plan(
            "task-1",
            self._plan("run-2"),
            expected_run_id="run-1",
            allow_run_takeover=True,
            db_engine=self.engine,
        )

        with self.assertRaises(StalePlannerExecutionError):
            save_chapter_branch_plan(
                "task-1",
                self._plan("run-1"),
                expected_run_id="run-1",
                db_engine=self.engine,
            )

        self.assertEqual(
            "run-2",
            load_chapter_branch_plan("task-1", db_engine=self.engine)["run_id"],
        )

    def test_takeover_uses_compare_and_swap_on_loaded_run(self):
        save_chapter_branch_plan(
            "task-1",
            self._plan("run-2"),
            expected_run_id="",
            db_engine=self.engine,
        )

        with self.assertRaises(StalePlannerExecutionError):
            save_chapter_branch_plan(
                "task-1",
                self._plan("run-3"),
                expected_run_id="run-1",
                allow_run_takeover=True,
                db_engine=self.engine,
            )

    def test_ownership_is_rechecked_inside_save_transaction(self):
        checks = []

        save_chapter_branch_plan(
            "task-1",
            self._plan("run-1"),
            expected_run_id="",
            db_engine=self.engine,
            ownership_check=lambda: checks.append("checked"),
        )

        self.assertEqual(["checked"], checks)


if __name__ == "__main__":
    unittest.main()
