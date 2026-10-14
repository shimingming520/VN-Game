import importlib
import importlib.util
from pathlib import Path
import unittest


class NovelPreflightTests(unittest.TestCase):
    def _module(self):
        module_name = "services.story_planner.core.novel_preflight"
        self.assertIsNotNone(
            importlib.util.find_spec(module_name),
            "缺少大文本预检模块",
        )
        return importlib.import_module(module_name)

    def test_accepts_six_hundred_thousand_character_chapter_branch_novel(self):
        module = self._module()
        chapters = ["文" * 10000 for _ in range(60)]

        report = module.run_novel_preflight(
            text="".join(chapters),
            chapters=chapters,
            story_mode="chapter_branch",
        )

        self.assertEqual(report.char_count, 600000)
        self.assertEqual(report.chapter_count, 60)
        self.assertEqual(report.worldview_segments, 15)
        self.assertEqual(report.chapter_summary_segments, 60)
        self.assertGreater(report.estimated_soft_branches, 0)
        self.assertGreater(report.estimated_llm_calls_max, report.estimated_llm_calls_min)
        self.assertGreater(report.estimated_runtime_minutes_max, 0)
        self.assertTrue(any("大文本" in warning for warning in report.warnings))

    def test_rejects_novel_above_default_character_limit(self):
        module = self._module()

        with self.assertRaisesRegex(module.NovelPreflightError, "1500000"):
            module.run_novel_preflight(
                text="文" * 1500001,
                chapters=["文" * 1500001],
                story_mode="chapter_branch",
            )

    def test_rejects_novel_above_default_chapter_limit(self):
        module = self._module()
        chapters = ["一"] * 3001

        with self.assertRaisesRegex(module.NovelPreflightError, "3000"):
            module.run_novel_preflight(
                text="".join(chapters),
                chapters=chapters,
                story_mode="chapter_branch",
            )

    def test_rejects_empty_or_badly_decoded_text(self):
        module = self._module()

        with self.assertRaisesRegex(module.NovelPreflightError, "有效正文"):
            module.run_novel_preflight(" \n\t ", [], "original")

        corrupted = ("正文" * 100) + ("�" * 5)
        with self.assertRaisesRegex(module.NovelPreflightError, "解码替换字符"):
            module.run_novel_preflight(corrupted, [corrupted], "original")

    def test_estimates_known_chapter_branch_workload_deterministically(self):
        module = self._module()
        chapters = ["\n\n".join(["文" * 2500] * 4) for _ in range(10)]

        report = module.run_novel_preflight(
            text="".join(chapters),
            chapters=chapters,
            story_mode="chapter_branch",
        )

        self.assertEqual(report.worldview_segments, 3)
        self.assertEqual(report.chapter_summary_segments, 10)
        self.assertEqual(report.estimated_soft_branches, 33)
        self.assertEqual(report.estimated_llm_calls_min, 51)
        self.assertEqual(report.estimated_llm_calls_max, 137)
        self.assertEqual(report.estimated_runtime_minutes_min, 13)
        self.assertEqual(report.estimated_runtime_minutes_max, 103)

    def test_short_chapters_use_adaptive_preflight_branch_counts(self):
        module = self._module()
        chapters = [
            "\n\n".join(["短章" * 750] * 2),
            "\n\n".join(["中章" * 1000] * 3),
            "\n\n".join(["长章" * 2500] * 4),
        ]

        report = module.run_novel_preflight(
            text="".join(chapters),
            chapters=chapters,
            story_mode="chapter_branch",
        )

        self.assertEqual(3, report.estimated_soft_branches)

    def test_topology_calibration_uses_chapter_text_for_original_nodes(self):
        module = self._module()
        chapters = [
            "\n\n".join(["短章" * 750] * 2),
            "\n\n".join(["长章" * 2500] * 4),
            "结局" * 1000,
        ]
        report = module.run_novel_preflight(
            text="".join(chapters),
            chapters=chapters,
            story_mode="chapter_branch",
        )
        topology = [
            {
                "node_id": "ch1",
                "type": "chapter",
                "is_original": True,
                "source_chapter_idx": 0,
            },
            {
                "node_id": "ch2",
                "type": "chapter",
                "is_original": True,
                "source_chapter_idx": 1,
            },
            {
                "node_id": "ch3",
                "type": "ending",
                "is_original": True,
                "source_chapter_idx": 2,
            },
        ]

        calibrated = module.calibrate_chapter_branch_preflight(
            report,
            topology=topology,
            branch_type="default",
            chapters=chapters,
        )

        self.assertEqual(4, calibrated.estimated_soft_branches)

    def test_original_mode_reports_storage_work_without_branch_calls(self):
        module = self._module()
        chapters = ["文" * 10000 for _ in range(5)]

        report = module.run_novel_preflight(
            text="".join(chapters),
            chapters=chapters,
            story_mode="original",
        )

        self.assertEqual(report.estimated_output_nodes_min, 5)
        self.assertEqual(report.estimated_output_nodes_max, 5)
        self.assertEqual(report.estimated_soft_branches, 0)
        self.assertEqual(report.estimated_llm_calls_min, 1)
        self.assertEqual(report.estimated_llm_calls_max, 1)

    def test_environment_can_raise_limits_and_adjust_time_estimate(self):
        module = self._module()
        limits = module.resolve_preflight_limits(
            {
                "STORY_PLANNER_MAX_NOVEL_CHARS": "2000000",
                "STORY_PLANNER_MAX_CHAPTERS": "5000",
                "STORY_PLANNER_ESTIMATED_LLM_SECONDS_PER_CALL": "40",
            }
        )

        self.assertEqual(limits.max_novel_chars, 2000000)
        self.assertEqual(limits.max_chapters, 5000)
        self.assertEqual(limits.estimated_llm_seconds_per_call, 40)

    def test_report_is_serializable_and_human_readable(self):
        module = self._module()
        report = module.run_novel_preflight(
            text="正文" * 5000,
            chapters=["正文" * 5000],
            story_mode="chapter_branch",
        )

        data = report.to_dict()
        rendered = report.format_text()

        self.assertEqual(data["char_count"], 10000)
        self.assertIn("预计 LLM 调用", rendered)
        self.assertIn("预计耗时", rendered)

    def test_fantasy_calibration_removes_soft_branches_and_keeps_whatif_calls(self):
        module = self._module()
        chapters = ["\n\n".join(["文" * 2500] * 4) for _ in range(10)]
        report = module.run_novel_preflight(
            text="".join(chapters),
            chapters=chapters,
            story_mode="chapter_branch",
        )
        topology = [
            *[
                {
                    "node_id": f"ch{index + 1}",
                    "type": "ending" if index == 9 else "chapter",
                    "is_original": True,
                    "source_chapter_idx": index,
                }
                for index in range(10)
            ],
            {"node_id": "whatif_1", "type": "chapter", "is_original": False},
            {"node_id": "whatif_2", "type": "chapter", "is_original": False},
        ]

        calibrated = module.calibrate_chapter_branch_preflight(
            report,
            topology=topology,
            branch_type="fantasy",
        )

        self.assertEqual(0, calibrated.estimated_soft_branches)
        self.assertEqual(12, calibrated.estimated_output_nodes_min)
        self.assertGreater(calibrated.estimated_llm_calls_min, 17)
        self.assertGreater(calibrated.estimated_llm_calls_max, 0)


class NovelPreflightIntegrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tasks_source = (
            Path(__file__).resolve().parents[2]
            / "services"
            / "story_planner"
            / "tasks.py"
        ).read_text(encoding="utf-8")

    def test_original_preflight_runs_before_database_writes(self):
        source = self.tasks_source.split("def _parse_single_mode", 1)[1].split(
            "SOFT_BRANCH_OPEN", 1
        )[0]
        preflight_pos = source.find("run_novel_preflight")
        save_pos = source.find("save_story_md_to_db(")

        self.assertGreaterEqual(preflight_pos, 0)
        self.assertGreater(save_pos, preflight_pos)

    def test_chapter_branch_preflight_runs_before_novel_analysis(self):
        source = self.tasks_source.split("def _parse_chapter_branch_mode", 1)[1].split(
            "@celery_app.task", 1
        )[0]
        preflight_pos = source.find("run_novel_preflight")
        analyze_pos = source.find("analyze_novel(")

        self.assertGreaterEqual(preflight_pos, 0)
        self.assertGreater(analyze_pos, preflight_pos)
        self.assertIn('_chapter_plan["preflight"] = _preflight_report.to_dict()', source)


if __name__ == "__main__":
    unittest.main()
