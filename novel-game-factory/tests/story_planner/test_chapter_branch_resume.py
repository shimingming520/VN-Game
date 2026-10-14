import tempfile
import unittest
from unittest.mock import patch

from services.story_planner.core.chapter_branch_resume import (
    build_novel_analysis_step_input,
    build_global_style_checkpoint,
    build_summary_from_checkpoint,
    deserialize_novel_context,
    serialize_novel_context,
    get_reusable_global_style,
    build_chapter_processing_checkpoint,
    get_reusable_chapter_processing_checkpoint,
    upgrade_legacy_completed_plan,
)
from services.story_planner.core.novel_reader import (
    NARRATIVE_POV_CONTRACT_VERSION,
    NovelContext,
)
from services.story_planner.core.chapter_branch_runtime import (
    CHAPTER_BRANCH_STEP_ORDER,
    PLAN_VERSION,
)
from common.task_models import StoryMode


class ChapterProcessingCheckpointTests(unittest.TestCase):
    def test_processing_checkpoint_reuses_exactly_matching_chapters(self):
        checkpoint = build_chapter_processing_checkpoint(
            "raw novel",
            ["Chapter 1\nbody", "Chapter 2\nbody"],
        )

        reused = get_reusable_chapter_processing_checkpoint(
            checkpoint,
            raw_text="raw novel",
        )

        self.assertEqual(["Chapter 1\nbody", "Chapter 2\nbody"], reused)

    def test_processing_checkpoint_rejects_changed_source_or_tampered_chapter(self):
        checkpoint = build_chapter_processing_checkpoint(
            "raw novel",
            ["Chapter 1\nbody", "Chapter 2\nbody"],
        )
        checkpoint["chapters"][0] = "tampered"

        self.assertIsNone(
            get_reusable_chapter_processing_checkpoint(
                checkpoint,
                raw_text="raw novel",
            )
        )

    def test_processing_checkpoint_uses_atomic_shared_artifact_and_compact_plan_metadata(self):
        from services.story_planner import tasks

        checkpoint = build_chapter_processing_checkpoint(
            "raw novel",
            ["Chapter 1\nbody", "Chapter 2\nbody"],
        )
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            tasks, "RAW_NOVELS_PATH", temp_dir
        ):
            metadata = tasks._persist_chapter_processing_checkpoint(
                "task-1", checkpoint
            )
            loaded = tasks._load_chapter_processing_checkpoint(
                "task-1", metadata
            )
            loaded_from_metadata = tasks._load_chapter_processing_checkpoint(
                "task-1", {"artifact_path": metadata["artifact_path"], "artifact_sha256": metadata["artifact_sha256"]}
            )

        self.assertNotIn("chapters", metadata)
        self.assertEqual(
            ["Chapter 1\nbody", "Chapter 2\nbody"],
            get_reusable_chapter_processing_checkpoint(
                loaded,
                raw_text="raw novel",
            ),
        )
        self.assertEqual(loaded, loaded_from_metadata)
        self.assertIsNone(
            get_reusable_chapter_processing_checkpoint(
                build_chapter_processing_checkpoint(
                    "raw novel",
                    ["Chapter 1\nbody", "Chapter 2\nbody"],
                ),
                raw_text="changed novel",
            )
        )
class ChapterBranchPublishedResumeTests(unittest.TestCase):
    def test_content_warning_source_artifact_survives_translation_and_restarts(self):
        from services.story_planner import tasks

        source_text = "第一章\n房间里出现碎尸。"
        translated_text = "Chapter 1\nA dismembered body was found."
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(tasks, "RAW_NOVELS_PATH", temp_dir):
                self.assertIsNone(
                    tasks._load_content_warning_source("task-1", source_text)
                )
                self.assertTrue(
                    tasks._persist_content_warning_source(
                        "task-1",
                        source_text=source_text,
                        working_text=source_text,
                        source_language="zh",
                        target_language="zh",
                    )
                )
                self.assertTrue(
                    tasks._persist_content_warning_source(
                        "task-1",
                        source_text=source_text,
                        working_text=translated_text,
                        source_language="zh",
                        target_language="en",
                    )
                )
                self.assertEqual(
                    source_text,
                    tasks._load_content_warning_source("task-1", translated_text),
                )
                self.assertIsNone(
                    tasks._load_content_warning_source(
                        "task-1", "a newly uploaded novel"
                    )
                )

    def test_novel_context_checkpoint_round_trip_preserves_narrative_pov_contract(self):
        pov_contract = {
            "contract_version": NARRATIVE_POV_CONTRACT_VERSION,
            "perspective": "third_person_limited",
            "protagonist_name": "沈砚",
            "narrator_pronouns": ["他", "她"],
            "allow_switch": False,
            "rule_text": "保持第三人称限知叙事，不得切换为第一人称。",
        }
        context = NovelContext(
            worldview={"genre": "mystery"},
            chapter_summaries=["第一章摘要"],
            narrative_pov_contract=pov_contract,
        )

        restored = deserialize_novel_context(serialize_novel_context(context))

        self.assertIsNotNone(restored)
        self.assertEqual(pov_contract, restored.narrative_pov_contract)

    def test_novel_context_checkpoint_rejects_stale_narrative_pov_contract(self):
        context = NovelContext(
            worldview={"genre": "mystery"},
            chapter_summaries=["第一章摘要"],
            narrative_pov_contract={
                "contract_version": 999,
                "perspective": "third_person_limited",
                "protagonist_name": "沈砚",
            },
        )

        restored = deserialize_novel_context(serialize_novel_context(context))

        self.assertIsNone(restored)

    def test_novel_analysis_step_identity_contains_pov_contract_version(self):
        step_input = build_novel_analysis_step_input(
            source_fingerprint="source-v1",
            chapters=["第一章正文"],
            output_language="zh-CN",
            provider="openai",
            model="gpt-5.6-sol",
        )

        self.assertEqual(
            NARRATIVE_POV_CONTRACT_VERSION,
            step_input["narrative_pov_contract_version"],
        )

    def test_global_style_checkpoint_reuses_only_matching_untampered_style(self):
        checkpoint = build_global_style_checkpoint(
            "cinematic urban folk horror",
            source_fingerprint="source-v1",
            model="gpt-5.6-sol",
        )
        plan = {"global_style_checkpoint": checkpoint}

        self.assertEqual(
            "cinematic urban folk horror",
            get_reusable_global_style(
                plan,
                source_fingerprint="source-v1",
                model="gpt-5.6-sol",
            ),
        )
        self.assertIsNone(
            get_reusable_global_style(
                plan,
                source_fingerprint="source-v2",
                model="gpt-5.6-sol",
            )
        )
        plan["global_style_checkpoint"]["style"] = "tampered style"
        self.assertIsNone(
            get_reusable_global_style(
                plan,
                source_fingerprint="source-v1",
                model="gpt-5.6-sol",
            )
        )

    def test_chapter_branch_style_resolver_does_not_call_llm_for_valid_checkpoint(self):
        from services.story_planner import tasks

        checkpoint = build_global_style_checkpoint(
            "persisted style",
            source_fingerprint=tasks._chapter_branch_source_fingerprint("第一章\n正文。"),
            model="gpt-5.6-sol",
        )
        with patch.object(
            tasks,
            "_load_chapter_branch_plan",
            return_value={"global_style_checkpoint": checkpoint},
        ), patch.object(
            tasks,
            "_generate_global_style",
            side_effect=AssertionError("恢复时不应重新调用画风 LLM"),
        ):
            style = tasks._resolve_chapter_branch_global_style(
                "task-1",
                "第一章\n正文。",
                provider="openai",
            )

        self.assertEqual("persisted style", style)

    def test_chapter_branch_style_resolver_persists_new_style_before_analysis(self):
        from services.story_planner import tasks

        saved = {}

        def capture_save(task_id, plan, **kwargs):
            saved["task_id"] = task_id
            saved["plan"] = plan
            saved["kwargs"] = kwargs

        with patch.object(
            tasks,
            "_load_chapter_branch_plan",
            return_value={},
        ), patch.object(
            tasks,
            "_save_chapter_branch_plan",
            side_effect=capture_save,
        ), patch.object(
            tasks,
            "_generate_global_style",
            return_value="generated stable style",
        ):
            style = tasks._resolve_chapter_branch_global_style(
                "task-1",
                "第一章\n正文。",
                provider="openai",
            )

        self.assertEqual("generated stable style", style)
        self.assertEqual("task-1", saved["task_id"])
        checkpoint = saved["plan"]["global_style_checkpoint"]
        self.assertEqual("generated stable style", checkpoint["style"])
        self.assertRegex(checkpoint["style_sha256"], r"^[0-9a-f]{64}$")

    def test_novel_analysis_identity_changes_when_chapter_boundaries_change(self):
        common = {
            "source_fingerprint": "same-source",
            "output_language": "zh-CN",
            "provider": "openai",
            "model": "gpt-5.6-sol",
        }

        first = build_novel_analysis_step_input(
            chapters=["甲乙", "丙丁"],
            **common,
        )
        rebalanced = build_novel_analysis_step_input(
            chapters=["甲", "乙丙丁"],
            **common,
        )

        self.assertNotEqual(first["chapter_sha256"], rebalanced["chapter_sha256"])
        self.assertEqual(2, first["chapter_count"])

    def test_novel_context_checkpoint_round_trip_preserves_generation_inputs(self):
        context = NovelContext(
            worldview={"genre": "mystery", "characters": [{"name": "甲"}]},
            chapter_summaries=["第一章摘要", "第二章摘要"],
            style_samples="风格样本",
            char_cards="角色卡",
            relationship_map="关系图",
            world_rules="规则",
            narrative_arc="叙事弧",
            persona_axes=[{"key": "courage"}],
            is_romance_oriented=False,
            factions_text="势力",
            hidden_threads_text="伏笔",
            ending_dimensions=[{"key": "ending"}],
            character_secrets_text="秘密",
            character_behavior_anchors="行为锚点",
            branch_type="modular",
            branch_type_strategy_version=3,
        )

        restored = deserialize_novel_context(serialize_novel_context(context))

        self.assertIsInstance(restored, NovelContext)
        self.assertEqual(context.__dict__, restored.__dict__)

    def test_novel_context_checkpoint_rejects_partial_or_wrong_typed_payload(self):
        self.assertIsNone(deserialize_novel_context({"worldview": {}}))
        payload = serialize_novel_context(
            NovelContext(
                worldview={"genre": "mystery"},
                chapter_summaries=["摘要"],
            )
        )
        payload["chapter_summaries"] = "不是列表"
        self.assertIsNone(deserialize_novel_context(payload))

    def test_upgrades_version_five_completed_plan_for_summary_only_resume(self):
        plan = {
            "version": 5,
            "task_id": "task-1",
            "branch_type": "default",
            "source_fingerprint": "source",
            "topology_result": {
                "topology": [
                    {"node_id": "ch1", "type": "chapter", "is_original": True}
                ]
            },
            "nodes": {
                "ch1": {
                    "status": "completed",
                    "real_id": "chapter_1",
                    "artifact_sha256": "node-digest",
                }
            },
        }

        upgraded = upgrade_legacy_completed_plan(
            plan,
            publication_fingerprint="publication-digest",
        )

        self.assertEqual(PLAN_VERSION, upgraded["version"])
        self.assertEqual("completed", upgraded["nodes"]["ch1"]["status"])
        self.assertTrue(
            all(
                upgraded["steps"][step]["status"] == "completed"
                for step in CHAPTER_BRANCH_STEP_ORDER[:6]
            )
        )
        self.assertEqual(
            "publication-digest",
            upgraded["steps"]["story_publish"]["output_digest"],
        )
    def test_builds_all_required_summary_fields_without_llm(self):
        plan = {
            "topology_result": {
                "topology": [
                    {
                        "node_id": "ch1",
                        "is_original": True,
                        "source_chapter_idx": 0,
                        "summary": "【核心事件】主角登上末班车。",
                    },
                    {
                        "node_id": "ch2",
                        "is_original": True,
                        "source_chapter_idx": 1,
                        "summary": "【与结局的关联】主角最终打破循环。",
                    },
                ]
            }
        }
        worldview = {
            "world_rules": "末班车遵循不可回头的规则。",
            "narrative_arc": "主角从恐惧逃避走向直面真相。",
        }

        summary = build_summary_from_checkpoint(plan, worldview)

        self.assertIn("登上末班车", summary["outline"])
        self.assertIn("打破循环", summary["outline"])
        self.assertEqual("末班车遵循不可回头的规则。", summary["background"])
        self.assertEqual("主角从恐惧逃避走向直面真相。", summary["one_liner"])

    def test_uses_deterministic_fallbacks_when_labeled_sections_are_absent(self):
        plan = {
            "topology_result": {
                "topology": [
                    {
                        "node_id": "ch1",
                        "is_original": True,
                        "source_chapter_idx": 0,
                        "summary": "主角发现了隐藏线索。",
                    }
                ]
            }
        }
        worldview = {
            "worldview": {"genre": "mystery"},
            "world_rules": "",
            "narrative_arc": "主角追查谜团并承担后果",
        }

        summary = build_summary_from_checkpoint(plan, worldview)

        self.assertTrue(summary["outline"])
        self.assertTrue(summary["background"])
        self.assertTrue(summary["one_liner"].endswith("。"))

    def test_parse_task_checks_source_then_returns_before_generation_llm(self):
        from services.story_planner import tasks

        with patch.object(tasks, "is_stage_already_running", return_value=False), patch.object(
            tasks,
            "_get_task_config",
            return_value={
                "story_mode": StoryMode.CHAPTER_BRANCH,
                "novel_filename": "novel.txt",
                "output_language": "zh-CN",
            },
        ), patch.object(
            tasks,
            "_try_resume_published_chapter_branch",
            return_value=True,
        ) as resume, patch.object(
            tasks,
            "_read_and_normalize_novel",
            return_value="第一章\n原始正文。",
        ), patch.object(
            tasks.Path,
            "exists",
            return_value=True,
        ), patch.object(
            tasks,
            "_detect_novel_language",
            return_value="zh",
        ), patch.object(
            tasks,
            "_need_translation",
            return_value=False,
        ), patch.object(
            tasks,
            "_generate_global_style",
            side_effect=AssertionError("不应调用生成 LLM"),
        ), patch.object(tasks, "_usage_tracker", None), patch.object(
            tasks,
            "_stop_runtime_heartbeat",
        ):
            result = tasks.parse_novel_to_draft.run("task-1")

        self.assertEqual("task-1", result)
        resume.assert_called_once()
        self.assertEqual("task-1", resume.call_args.args[0])
        self.assertRegex(resume.call_args.kwargs["source_fingerprint"], r"^[0-9a-f]{64}$")

    def test_parse_task_persists_canonical_warning_source_before_chapter_branch_resume(self):
        """The task entry point must preserve source text before an early resume return."""
        from services.story_planner import tasks

        source_text = "第一章\n房间里发现了尸体。"
        with patch.object(tasks, "is_stage_already_running", return_value=False), patch.object(
            tasks,
            "_get_task_config",
            return_value={
                "story_mode": StoryMode.CHAPTER_BRANCH,
                "novel_filename": "novel.txt",
                "output_language": "zh-CN",
            },
        ), patch.object(tasks.Path, "exists", return_value=True), patch.object(
            tasks,
            "_read_and_normalize_novel",
            return_value=source_text,
        ), patch.object(tasks, "_detect_novel_language", return_value="zh"), patch.object(
            tasks,
            "_need_translation",
            return_value=False,
        ), patch.object(
            tasks,
            "_load_content_warning_source",
            return_value=None,
        ), patch.object(
            tasks,
            "_persist_content_warning_source",
        ) as persist_source, patch.object(
            tasks,
            "_try_resume_published_chapter_branch",
            return_value=True,
        ), patch.object(tasks, "_usage_tracker", None), patch.object(
            tasks, "_stop_runtime_heartbeat"
        ):
            result = tasks.parse_novel_to_draft.run("task-source-persist")

        self.assertEqual("task-source-persist", result)
        persist_source.assert_called_once_with(
            "task-source-persist",
            source_text=source_text,
            working_text=source_text,
            source_language="zh",
            target_language="zh",
        )

    def test_parse_task_updates_warning_source_sidecar_after_translation(self):
        """A translated working file must retain the pre-translation source sidecar."""
        from services.story_planner import tasks

        source_text = "第一章\n房间里发现了尸体。"
        translated_text = "Chapter 1\nA body was found in the room."
        with patch.object(tasks, "is_stage_already_running", return_value=False), patch.object(
            tasks,
            "_get_task_config",
            return_value={
                "story_mode": StoryMode.CHAPTER_BRANCH,
                "novel_filename": "novel.txt",
                "output_language": "en-US",
            },
        ), patch.object(tasks.Path, "exists", return_value=True), patch.object(
            tasks,
            "_read_and_normalize_novel",
            return_value=source_text,
        ), patch.object(tasks, "_detect_novel_language", return_value="zh"), patch.object(
            tasks,
            "_need_translation",
            return_value=True,
        ), patch.object(
            tasks,
            "_load_content_warning_source",
            return_value=None,
        ), patch.object(
            tasks,
            "_translate_novel_text",
            return_value=translated_text,
        ), patch.object(tasks, "_atomic_write_utf8_text"), patch.object(
            tasks,
            "_persist_content_warning_source",
        ) as persist_source, patch.object(
            tasks,
            "_try_resume_published_chapter_branch",
            return_value=True,
        ), patch.object(tasks, "_usage_tracker", None), patch.object(
            tasks, "_stop_runtime_heartbeat"
        ):
            result = tasks.parse_novel_to_draft.run("task-source-translation")

        self.assertEqual("task-source-translation", result)
        persist_source.assert_called_once_with(
            "task-source-translation",
            source_text=source_text,
            working_text=translated_text,
            source_language="zh",
            target_language="en",
        )

    def test_checkpoint_read_failure_during_error_reporting_does_not_mask_primary_error(self):
        from services.story_planner import tasks

        with patch.object(tasks, "is_stage_already_running", return_value=False), patch.object(
            tasks,
            "_get_task_config",
            return_value={
                "story_mode": StoryMode.CHAPTER_BRANCH,
                "novel_filename": "novel.txt",
                "output_language": "zh-CN",
            },
        ), patch.object(tasks.Path, "exists", return_value=True), patch.object(
            tasks,
            "_read_and_normalize_novel",
            return_value="第一章\n原始正文。",
        ), patch.object(tasks, "_detect_novel_language", return_value="zh"), patch.object(
            tasks,
            "_need_translation",
            return_value=False,
        ), patch.object(
            tasks,
            "_try_resume_published_chapter_branch",
            return_value=False,
        ), patch.object(
            tasks,
            "_resolve_chapter_branch_global_style",
            return_value="style",
        ), patch.object(
            tasks,
            "_parse_chapter_branch_mode",
            side_effect=ValueError("primary generation failure"),
        ), patch.object(
            tasks,
            "_load_chapter_branch_plan",
            side_effect=RuntimeError("checkpoint database unavailable"),
        ), patch.object(tasks, "fail_stage") as fail_stage, patch.object(
            tasks,
            "_usage_tracker",
            None,
        ), patch.object(tasks, "_stop_runtime_heartbeat"):
            with self.assertRaisesRegex(ValueError, "primary generation failure"):
                tasks.parse_novel_to_draft.run("task-1")

        fail_stage.assert_called_once()


if __name__ == "__main__":
    unittest.main()
