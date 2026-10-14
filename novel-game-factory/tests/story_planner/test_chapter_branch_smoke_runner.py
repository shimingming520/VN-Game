import json
import inspect
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from services.story_planner.run_chapter_branch_smoke import (
    LocalArtifactStore,
    LocalUsageTracker,
    run_smoke,
)


class LocalArtifactStoreTests(unittest.TestCase):
    def test_store_refreshes_topology_fingerprint_before_saving_plan(self):
        with TemporaryDirectory() as temp_dir:
            store = LocalArtifactStore(Path(temp_dir))
            plan = {
                "topology_result": {
                    "topology": [{"node_id": "ch1", "summary": "运行后摘要"}],
                    "variable_defs": [],
                },
                "topology_fingerprint": "stale",
                "nodes": {},
            }

            store.save_plan("task", plan)
            saved = store.load_plan("task")

            self.assertNotEqual("stale", saved["topology_fingerprint"])

    def test_store_round_trips_md_json_plan_and_summary(self):
        with TemporaryDirectory() as temp_dir:
            store = LocalArtifactStore(Path(temp_dir))

            md_record = store.save_md("task", "chapter_1", "# 第一章\n\n正文")
            store.save_story_tree("task", {"root_id": "chapter_1", "nodes": {}})
            store.save_plan("task", {"version": 2, "nodes": {}})
            store.save_summary("task", outline="大纲", background="背景", one_liner="一句话")

            self.assertEqual(md_record.data, "# 第一章\n\n正文")
            self.assertEqual(store.get_md("task", "chapter_1").data, md_record.data)
            self.assertEqual(store.load_plan("task")["version"], 2)
            summary = json.loads((Path(temp_dir) / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["outline"], "大纲")

    def test_store_summary_accepts_stage_ownership_keyword(self):
        """The local adapter must mirror the production summary-save signature."""
        with TemporaryDirectory() as temp_dir:
            store = LocalArtifactStore(Path(temp_dir))

            store.save_summary(
                "task",
                outline="大纲",
                background="背景",
                one_liner="一句话",
                require_stage_ownership=True,
            )

            summary = json.loads(
                (Path(temp_dir) / "summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["task_id"], "task")

    def test_store_loads_summary_as_production_shaped_row(self):
        with TemporaryDirectory() as temp_dir:
            store = LocalArtifactStore(Path(temp_dir))
            store.save_summary(
                "task",
                outline="大纲",
                background="背景",
                one_liner="一句话",
            )

            row = store.load_summary("task")

            self.assertIsNotNone(row)
            self.assertEqual(row.status, 1)
            self.assertEqual(row.data["outline"], "大纲")

    def test_store_keeps_latest_worldview_for_quality_evaluation(self):
        class Context:
            branch_type = "default"
            worldview = {"characters": [{"name": "沈月", "role": "主角"}]}
            chapter_summaries = []

        with TemporaryDirectory() as temp_dir:
            store = LocalArtifactStore(Path(temp_dir))

            store.save_worldview("task", Context(), "ink_chinese")

            self.assertEqual(Context.worldview, store.latest_worldview)

    def test_store_saves_candidate_tree_and_quality_report_before_publish(self):
        with TemporaryDirectory() as temp_dir:
            store = LocalArtifactStore(Path(temp_dir))
            candidate = {"root_id": "chapter_1", "nodes": {"chapter_1": {}}}
            report = {"passed": False, "score": 76, "errors": ["质量门禁失败"]}

            store.save_diagnostics("task", candidate, report)

            saved_candidate = json.loads(
                (Path(temp_dir) / "candidate_story_tree.json").read_text(encoding="utf-8")
            )
            saved_report = json.loads(
                (Path(temp_dir) / "quality_report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(candidate, saved_candidate)
            self.assertEqual(report, saved_report)

    def test_local_usage_tracker_records_llm_usage_without_database(self):
        with TemporaryDirectory() as temp_dir:
            tracker = LocalUsageTracker(Path(temp_dir))
            run_id = tracker.set_context(
                "smoke_novel", "story_planner", force_new_run=True
            )

            recorded = tracker.add_llm_usage(
                SimpleNamespace(prompt_tokens=120, completion_tokens=30, total_tokens=150),
                model="gpt-5.6-sol",
                provider="openai_compatible",
            )
            tracker.flush(run_id=run_id)
            tracker.clear_context(run_id)

            self.assertTrue(recorded)
            report = json.loads(
                (Path(temp_dir) / "usage_report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(1, report["totals"]["call_count"])
            self.assertEqual(150, report["totals"]["total_tokens"])
            self.assertEqual("gpt-5.6-sol", report["calls"][0]["model"])

    def test_local_usage_tracker_appends_existing_resume_usage(self):
        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "usage_report.json").write_text(
                json.dumps(
                    {
                        "totals": {"call_count": 1, "total_tokens": 100},
                        "calls": [
                            {
                                "task_id": "smoke_novel",
                                "service": "story_planner",
                                "run_id": "old-run",
                                "model": "gpt-5.6-sol",
                                "provider": "openai_compatible",
                                "prompt_tokens": 80,
                                "completion_tokens": 20,
                                "cached_input_tokens": 0,
                                "cache_write_tokens": 0,
                                "reasoning_tokens": 0,
                                "total_tokens": 100,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            tracker = LocalUsageTracker(output_dir)
            run_id = tracker.set_context("smoke_novel", "story_planner")
            tracker.add_llm(30, 10, model="gpt-5.6-sol", provider="openai_compatible")

            tracker.flush(run_id=run_id)

            report = json.loads(
                (output_dir / "usage_report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(2, report["totals"]["call_count"])
            self.assertEqual(140, report["totals"]["total_tokens"])
            self.assertEqual({"old-run", run_id}, {call["run_id"] for call in report["calls"]})

    def test_store_emulates_transactional_publish_and_removes_orphans(self):
        with TemporaryDirectory() as temp_dir:
            store = LocalArtifactStore(Path(temp_dir))
            store.save_md("task", "orphan", "旧正文")

            result = store.publish_story_tree_transactionally(
                task_id="task",
                md_contents={"chapter_1": "# 第一章\n\n正文"},
                json_data={"root_id": "chapter_1", "nodes": {"chapter_1": {}}},
                expected_node_ids={"chapter_1"},
                run_id="run-1",
                run_started_at="2026-08-11T01:00:00+00:00",
            )

            self.assertFalse((store.md_dir / "orphan.md").exists())
            self.assertTrue((store.md_dir / "chapter_1.md").exists())
            payload = json.loads(
                (Path(temp_dir) / "story_tree.json").read_text(encoding="utf-8")
            )
            self.assertEqual("run-1", payload["publication"]["run_id"])
            self.assertEqual(1, result.orphan_md_deleted)

    def test_smoke_runner_patches_new_publish_and_heartbeat_boundaries(self):
        source = inspect.getsource(run_smoke)

        self.assertIn("publish_story_tree_transactionally", source)
        self.assertIn("_record_chapter_branch_heartbeat", source)

    def test_smoke_runner_patches_task_bound_database_aliases(self):
        """Patching the source module is insufficient after tasks imports aliases."""
        source = inspect.getsource(run_smoke)

        self.assertIn(
            'patch.object(tasks, "save_story_md_to_db", side_effect=store.save_md)',
            source,
        )
        self.assertIn(
            'patch.object(\n            tasks,\n            "Session",',
            source,
        )
        self.assertIn(
            'patch.object(\n            tasks,\n            "publish_story_tree_transactionally",',
            source,
        )
        self.assertIn("get_story_md_by_name_strict", source)

    def test_smoke_runner_marks_running_nodes_failed_on_pipeline_error(self):
        source = inspect.getsource(run_smoke)

        self.assertIn("mark_running_nodes_failed", source)

    def test_smoke_runner_injects_usage_tracker_into_task_and_llm_client(self):
        source = inspect.getsource(run_smoke)

        self.assertIn('patch.object(tasks, "_usage_tracker", usage_tracker)', source)
        self.assertIn('patch.object(llm_client, "_usage_tracker", usage_tracker)', source)

    def test_smoke_runner_patches_failure_diagnostic_boundary(self):
        source = inspect.getsource(run_smoke)

        self.assertIn('"_save_chapter_branch_diagnostics"', source)
        self.assertIn("store.save_diagnostics", source)


if __name__ == "__main__":
    unittest.main()
