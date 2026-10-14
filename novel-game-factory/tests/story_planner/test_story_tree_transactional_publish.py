import unittest
from datetime import timezone

from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlmodel import SQLModel, Session, select

from services.story_planner.table.platform_models import StoryTreeJson, StoryTreeMd
from services.story_planner.table import story_tree_to_db


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


class StoryTreeTransactionalPublishTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", future=True)
        translated = self.engine.execution_options(
            schema_translate_map={"public": None}
        )
        SQLModel.metadata.create_all(
            translated,
            tables=[StoryTreeMd.__table__, StoryTreeJson.__table__],
        )
        self.engine = translated

    def _publish(self, task_id="task-a", *, started_at="2026-08-11T01:00:00+00:00"):
        return story_tree_to_db.publish_story_tree_transactionally(
            task_id=task_id,
            md_contents={"chapter_1": "正文一", "ending_a": "正文二"},
            json_data={
                "root_id": "chapter_1",
                "nodes": {
                    "chapter_1": {"children": [{"child_id": "ending_a"}]},
                    "ending_a": {"children": []},
                },
            },
            expected_node_ids={"chapter_1", "ending_a"},
            run_id="run-current",
            run_started_at=started_at,
            db_engine=self.engine,
        )

    def test_story_tree_create_times_are_timezone_aware(self):
        self.assertIsNotNone(StoryTreeMd(task_id="task-a", name="chapter_1").create_time.tzinfo)
        self.assertIsNotNone(StoryTreeJson(task_id="task-a", data={}).create_time.tzinfo)
        self.assertEqual(
            timezone.utc,
            StoryTreeMd(task_id="task-a", name="chapter_1").create_time.tzinfo,
        )

    def test_repeated_publish_is_idempotent(self):
        first = self._publish()
        second = self._publish()

        with Session(self.engine) as session:
            md_rows = session.exec(
                select(StoryTreeMd).where(StoryTreeMd.task_id == "task-a")
            ).all()
            json_rows = session.exec(
                select(StoryTreeJson).where(StoryTreeJson.task_id == "task-a")
            ).all()

        self.assertEqual(2, len(md_rows))
        self.assertEqual(1, len(json_rows))
        self.assertEqual(2, first.md_count)
        self.assertEqual(2, second.md_count)

    def test_publish_removes_only_current_task_orphans(self):
        with Session(self.engine) as session:
            session.add(StoryTreeMd(task_id="task-a", name="old-node", data="旧正文"))
            session.add(StoryTreeMd(task_id="task-b", name="old-node", data="其他任务正文"))
            session.commit()

        result = self._publish()

        with Session(self.engine) as session:
            task_a_names = {
                row.name
                for row in session.exec(
                    select(StoryTreeMd).where(StoryTreeMd.task_id == "task-a")
                ).all()
            }
            task_b_names = {
                row.name
                for row in session.exec(
                    select(StoryTreeMd).where(StoryTreeMd.task_id == "task-b")
                ).all()
            }

        self.assertEqual({"chapter_1", "ending_a"}, task_a_names)
        self.assertEqual({"old-node"}, task_b_names)
        self.assertEqual(1, result.orphan_md_deleted)

    def test_json_failure_rolls_back_md_updates_and_orphan_cleanup(self):
        with Session(self.engine) as session:
            session.add(StoryTreeMd(task_id="task-a", name="old-node", data="旧正文"))
            session.commit()

        def fail_json_insert(conn, cursor, statement, parameters, context, executemany):
            if "INSERT INTO main.story_tree_json" in statement or "INSERT INTO story_tree_json" in statement:
                raise RuntimeError("模拟 JSON 保存失败")

        event.listen(self.engine, "before_cursor_execute", fail_json_insert)
        try:
            with self.assertRaisesRegex(RuntimeError, "模拟 JSON 保存失败"):
                self._publish()
        finally:
            event.remove(self.engine, "before_cursor_execute", fail_json_insert)

        with Session(self.engine) as session:
            md_rows = session.exec(
                select(StoryTreeMd).where(StoryTreeMd.task_id == "task-a")
            ).all()
            json_row = session.exec(
                select(StoryTreeJson).where(StoryTreeJson.task_id == "task-a")
            ).first()

        self.assertEqual(["old-node"], [row.name for row in md_rows])
        self.assertIsNone(json_row)

    def test_older_run_cannot_overwrite_newer_publication(self):
        self._publish(started_at="2026-08-11T02:00:00+00:00")

        with self.assertRaises(story_tree_to_db.StaleStoryTreePublicationError):
            self._publish(started_at="2026-08-11T01:00:00+00:00")

        with Session(self.engine) as session:
            row = session.exec(
                select(StoryTreeJson).where(StoryTreeJson.task_id == "task-a")
            ).one()
        self.assertEqual(
            "2026-08-11T02:00:00+00:00",
            row.data["publication"]["run_started_at"],
        )

    def test_strict_md_lookup_propagates_database_errors(self):
        broken_engine = create_engine("sqlite:///Z:/missing/story-tree.db")

        with self.assertRaises(Exception):
            story_tree_to_db.get_story_md_by_name_strict(
                "task-a",
                "chapter_1",
                db_engine=broken_engine,
            )


if __name__ == "__main__":
    unittest.main()
