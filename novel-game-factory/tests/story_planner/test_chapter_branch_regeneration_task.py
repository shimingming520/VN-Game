import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch


os.environ["STORY_PLANNER_SMOKE_MODE"] = "1"

from common.task_models import StoryMode
from services.story_planner import tasks
from services.story_planner.core.chapter_branch_runtime import (
    content_sha256,
    initialize_chapter_branch_plan,
    update_plan_node,
)


class ChapterBranchRegenerationTaskTests(unittest.TestCase):
    def test_task_reopens_requested_nodes_and_runs_chapter_branch_parser(self):
        topology = {
            "topology": [
                {
                    "node_id": "original",
                    "type": "chapter",
                    "is_original": True,
                    "next": ["whatif"],
                },
                {
                    "node_id": "whatif",
                    "type": "chapter",
                    "is_original": False,
                    "next": ["whatif_ending"],
                },
                {
                    "node_id": "whatif_ending",
                    "type": "ending",
                    "is_original": False,
                    "next": [],
                },
            ],
            "variable_defs": [],
        }
        plan = initialize_chapter_branch_plan(
            "task-1",
            topology,
            branch_type="default",
            source_fingerprint=content_sha256("原文"),
        )
        for node_id in plan["nodes"]:
            update_plan_node(
                plan,
                node_id,
                status="completed",
                artifact_sha256=content_sha256(f"{node_id} 正文"),
            )

        with TemporaryDirectory() as raw_dir:
            novel_path = Path(raw_dir) / "task-1.txt"
            novel_path.write_text("原文", encoding="utf-8")
            usage_tracker = Mock()
            usage_tracker.set_context.return_value = "regen-run-1"
            self.assertTrue(
                hasattr(tasks, "regenerate_chapter_branch_nodes"),
                "尚未提供章节分支节点定向重生成任务",
            )
            with (
                patch.object(tasks, "RAW_NOVELS_PATH", raw_dir),
                patch.object(
                    tasks,
                    "_get_task_config",
                    return_value={
                        "novel_filename": "测试小说.txt",
                        "story_mode": StoryMode.CHAPTER_BRANCH,
                    },
                ),
                patch.object(tasks, "_load_chapter_branch_plan", return_value=plan),
                patch.object(tasks, "_save_chapter_branch_plan") as save_plan,
                patch.object(tasks, "_parse_chapter_branch_mode", return_value="task-1") as parse,
                patch.object(tasks, "start_stage") as start_stage,
                patch.object(tasks, "fail_stage") as fail_stage,
                patch.object(tasks, "_usage_tracker", usage_tracker),
            ):
                result = tasks.regenerate_chapter_branch_nodes.run(
                    "task-1",
                    "whatif",
                    include_non_original_descendants=True,
                    reason="人工修复失败节点",
                )

        self.assertEqual(result, "task-1")
        self.assertEqual(plan["nodes"]["original"]["status"], "completed")
        self.assertEqual(plan["nodes"]["whatif"]["status"], "pending")
        self.assertEqual(plan["nodes"]["whatif_ending"]["status"], "pending")
        self.assertEqual(
            plan["regeneration_request"]["node_ids"],
            ["whatif", "whatif_ending"],
        )
        save_plan.assert_called_once_with(
            "task-1",
            plan,
            require_stage_ownership=False,
        )
        start_stage.assert_called_once_with("task-1", "story_planner", 18)
        parse.assert_called_once_with(novel_path, "测试小说.txt", "task-1")
        fail_stage.assert_not_called()
        usage_tracker.set_context.assert_called_once_with(
            "task-1",
            "story_planner",
            force_new_run=True,
        )
        usage_tracker.flush.assert_called_once_with(run_id="regen-run-1")
        usage_tracker.clear_context.assert_called_once_with("regen-run-1")


if __name__ == "__main__":
    unittest.main()
