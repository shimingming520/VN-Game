import copy
import unittest

from services.story_planner.core.chapter_branch_artifacts import (
    ChapterBranchArtifactValidationError,
    validate_story_tree_language_bundle,
    validate_completed_chapter_branch_artifacts,
)
from services.story_planner.core.chapter_branch_runtime import PLAN_VERSION
from services.story_planner.core.chapter_branch_runtime import (
    CHAPTER_BRANCH_STEP_ORDER,
)
from services.story_planner.table.story_tree_to_db import (
    calculate_publication_fingerprint,
)


class ChapterBranchCompletedArtifactTests(unittest.TestCase):
    def setUp(self):
        self.md_artifacts = [
            ("chapter_1", "# 第一章\n\n列车终于驶离了车站。"),
            ("ending_a", "# 结局\n\n他们共同选择了新的生活。"),
        ]
        self.story_tree = {
            "root_id": "chapter_1",
            "story_mode": "chapter_branch",
            "book_title": "测试作品",
            "book_summary": "这是一个用于校验发布契约的测试作品。",
            "nodes": {
                "chapter_1": {
                    "id": "chapter_1",
                    "depth": 1,
                    "parent_id": None,
                    "is_original": True,
                    "title": "第一章",
                    "summary": "列车驶离车站。",
                    "content_file": "chapter_1.md",
                    "children": [{"child_id": "ending_a", "choice_text": "Go"}],
                },
                "ending_a": {
                    "id": "ending_a",
                    "depth": 2,
                    "parent_id": "chapter_1",
                    "is_original": False,
                    "title": "结局",
                    "summary": "他们共同选择新的生活。",
                    "content_file": "ending_a.md",
                    "children": [],
                },
            },
            "quality_gate": {"passed": True, "score": 92, "errors": []},
        }
        self.plan = {
            "version": PLAN_VERSION,
            "task_id": "task-1",
            "run_id": "run-current",
            "run_started_at": "2026-08-25T01:00:00+00:00",
            "nodes": {
                "ch1": {"status": "completed", "real_id": "chapter_1"},
                "ending": {"status": "completed", "real_id": "ending_a"},
            },
            "steps": {
                step: {
                    "status": "completed",
                    "input_digest": "input",
                    "output_digest": "output",
                    "schema_version": 1,
                    "producer_version": "producer",
                }
                for step in CHAPTER_BRANCH_STEP_ORDER
            },
        }
        self.summary = {
            "status": 1,
            "data": {
                "outline": "A complete outline.",
                "background": "A complete background.",
                "one_liner": "One decisive journey.",
            },
        }
        self.worldview = {
            "status": 1,
            "data": {
                "worldview": {"era": "modern"},
                "char_cards": "A：主角。",
                "relationship_map": "A 与 B 是同伴。",
                "world_rules": "Rules are explicit.",
                "narrative_arc": "The protagonist completes the journey.",
            },
        }
        self._seal_publication()
        self.plan["steps"]["story_publish"]["output_digest"] = self.story_tree[
            "publication"
        ]["fingerprint"]

    def _seal_publication(self):
        publication_free = copy.deepcopy(self.story_tree)
        publication_free.pop("publication", None)
        self.story_tree["publication"] = {
            "run_id": "run-current",
            "run_started_at": "2026-08-25T01:00:00+00:00",
            "fingerprint": calculate_publication_fingerprint(
                dict(self.md_artifacts), publication_free
            ),
        }

    def _validate(self):
        return validate_completed_chapter_branch_artifacts(
            task_id="task-1",
            story_tree_data=self.story_tree,
            md_artifacts=self.md_artifacts,
            plan=self.plan,
            summary=self.summary,
            worldview=self.worldview,
        )

    def test_core_publication_can_be_validated_before_summary_repair(self):
        self.summary = None

        result = validate_completed_chapter_branch_artifacts(
            task_id="task-1",
            story_tree_data=self.story_tree,
            md_artifacts=self.md_artifacts,
            plan=self.plan,
            summary=self.summary,
            worldview=self.worldview,
            require_summary=False,
        )

        self.assertEqual(self.story_tree["publication"]["fingerprint"], result.fingerprint)

    def test_accepts_only_a_complete_current_publication(self):
        result = self._validate()

        self.assertEqual({"chapter_1", "ending_a"}, result.node_ids)
        self.assertEqual(self.story_tree["publication"]["fingerprint"], result.fingerprint)

    def test_accepts_a_published_multilingual_bundle(self):
        english_tree = copy.deepcopy(self.story_tree)
        japanese_tree = copy.deepcopy(self.story_tree)
        japanese_tree["book_title"] = "日本語の物語"
        bundle_without_publication = {
            "en-US": english_tree,
            "ja-JP": japanese_tree,
        }
        bundle_without_publication["en-US"].pop("publication", None)
        bundle_without_publication["ja-JP"].pop("publication", None)
        published_bundle = copy.deepcopy(bundle_without_publication)
        published_bundle["publication"] = {
            "run_id": "run-current",
            "run_started_at": "2026-08-25T01:00:00+00:00",
            "fingerprint": calculate_publication_fingerprint(
                dict(self.md_artifacts), bundle_without_publication
            ),
        }
        self.plan["steps"]["story_publish"]["output_digest"] = published_bundle[
            "publication"
        ]["fingerprint"]

        result = validate_completed_chapter_branch_artifacts(
            task_id="task-1",
            story_tree_data=published_bundle,
            md_artifacts=self.md_artifacts,
            plan=self.plan,
            summary=self.summary,
            worldview=self.worldview,
        )

        self.assertEqual({"chapter_1", "ending_a"}, result.node_ids)
        self.assertEqual(
            published_bundle["publication"]["fingerprint"], result.fingerprint
        )

    def test_detects_locale_routing_structure_mismatch(self):
        english_tree = copy.deepcopy(self.story_tree)
        japanese_tree = copy.deepcopy(self.story_tree)
        japanese_tree["nodes"]["ending_a"]["parent_id"] = "wrong-parent"
        japanese_tree["nodes"]["ending_a"]["is_original"] = True
        errors = validate_story_tree_language_bundle(
            {"en-US": english_tree, "ja-JP": japanese_tree}
        )
        self.assertTrue(any("parent_id" in error for error in errors))
        self.assertTrue(any("is_original" in error for error in errors))

    def test_requires_title_and_summary_metadata_in_every_locale(self):
        english_tree = copy.deepcopy(self.story_tree)
        japanese_tree = copy.deepcopy(self.story_tree)
        english_tree["book_title"] = "English title"
        english_tree["book_summary"] = "English summary"
        japanese_tree["book_title"] = "日本語タイトル"
        japanese_tree.pop("book_summary", None)
        errors = validate_story_tree_language_bundle(
            {"en-US": english_tree, "ja-JP": japanese_tree}
        )
        self.assertTrue(any("book_summary" in error for error in errors))

    def test_rejects_a_multilingual_bundle_with_incomplete_locale_nodes(self):
        english_tree = copy.deepcopy(self.story_tree)
        japanese_tree = copy.deepcopy(self.story_tree)
        japanese_tree["nodes"].pop("ending_a")
        bundle = {"en-US": english_tree, "ja-JP": japanese_tree}
        bundle["publication"] = {
            "run_id": "run-current",
            "run_started_at": "2026-08-25T01:00:00+00:00",
            "fingerprint": "0" * 64,
        }

        with self.assertRaisesRegex(
            ChapterBranchArtifactValidationError, "ja-JP.*nodes"
        ):
            validate_completed_chapter_branch_artifacts(
                task_id="task-1",
                story_tree_data=bundle,
                md_artifacts=self.md_artifacts,
                plan=self.plan,
                summary=self.summary,
                worldview=self.worldview,
            )

    def test_accepts_english_md_ending_with_ascii_period(self):
        self.md_artifacts = [
            ("chapter_1", "# Chapter One\n\nThe train finally left the station."),
            ("ending_a", "# Ending\n\nThey chose a new life together."),
        ]
        self._seal_publication()
        self.plan["steps"]["story_publish"]["output_digest"] = self.story_tree[
            "publication"
        ]["fingerprint"]

        result = self._validate()

        self.assertEqual({"chapter_1", "ending_a"}, result.node_ids)

    def test_rejects_missing_md(self):
        self.md_artifacts.pop()

        with self.assertRaisesRegex(ChapterBranchArtifactValidationError, "缺少 MD"):
            self._validate()

    def test_rejects_extra_or_duplicate_md(self):
        self.md_artifacts.append(("orphan", "# Orphan\n\nUnused but valid prose."))
        self.md_artifacts.append(("chapter_1", "# Duplicate\n\nDuplicate prose."))

        with self.assertRaises(ChapterBranchArtifactValidationError) as raised:
            self._validate()

        message = str(raised.exception)
        self.assertIn("额外 MD", message)
        self.assertIn("重复 MD", message)

    def test_rejects_empty_or_damaged_md(self):
        self.md_artifacts[1] = ("ending_a", "  ")

        with self.assertRaisesRegex(ChapterBranchArtifactValidationError, "正文无效"):
            self._validate()

    def test_rejects_content_file_mismatch(self):
        self.story_tree["nodes"]["ending_a"]["content_file"] = "wrong.md"

        with self.assertRaisesRegex(ChapterBranchArtifactValidationError, "content_file"):
            self._validate()

    def test_rejects_failed_quality_gate(self):
        self.story_tree["quality_gate"]["passed"] = False

        with self.assertRaisesRegex(ChapterBranchArtifactValidationError, "质量门禁"):
            self._validate()

    def test_rejects_publication_fingerprint_mismatch(self):
        self.story_tree["publication"]["fingerprint"] = "0" * 64

        with self.assertRaisesRegex(ChapterBranchArtifactValidationError, "发布指纹"):
            self._validate()

    def test_rejects_incomplete_plan_node(self):
        self.plan["nodes"]["ending"]["status"] = "failed"

        with self.assertRaisesRegex(ChapterBranchArtifactValidationError, "断点计划"):
            self._validate()

    def test_rejects_incomplete_required_pipeline_step(self):
        self.plan["steps"]["summary_publish"]["status"] = "failed"

        with self.assertRaisesRegex(ChapterBranchArtifactValidationError, "步骤"):
            self._validate()

    def test_core_validation_allows_only_post_publish_steps_to_be_incomplete(self):
        self.plan["steps"]["summary_publish"]["status"] = "failed"
        self.plan["steps"]["stage_complete"]["status"] = "pending"

        result = validate_completed_chapter_branch_artifacts(
            task_id="task-1",
            story_tree_data=self.story_tree,
            md_artifacts=self.md_artifacts,
            plan=self.plan,
            summary=None,
            worldview=self.worldview,
            require_summary=False,
        )

        self.assertEqual(self.story_tree["publication"]["fingerprint"], result.fingerprint)

    def test_rejects_missing_required_summary_field(self):
        self.summary["data"]["one_liner"] = ""

        with self.assertRaisesRegex(ChapterBranchArtifactValidationError, "story_summary"):
            self._validate()

    def test_rejects_unconfirmed_worldview(self):
        self.worldview["status"] = 0

        with self.assertRaisesRegex(ChapterBranchArtifactValidationError, "world_bible"):
            self._validate()

    def test_accepts_valid_work_content_warnings(self):
        self.story_tree["content_warnings"] = {
            "schema_version": 1,
            "work": {
                "status": "detected",
                "has_sensitive_content": True,
                "severity": "high",
                "categories": [
                    {
                        "code": "gore",
                        "presence": "depicted",
                        "severity": "high",
                        "confidence": 0.9,
                        "evidence_chapters": [1],
                    }
                ],
                "display_policy": {
                    "show_at_work_entry": True,
                    "requires_acknowledgement": True,
                    "dismissible": True,
                    "allow_continue": True,
                    "allow_return": True,
                },
                "localized": {
                    "title": "Content Warning",
                    "summary": "Sensitive themes may be present.",
                    "items": [
                        {
                            "code": "gore",
                            "label": "Gore",
                            "description": "Bloody or disturbing imagery.",
                        }
                    ],
                    "acknowledge_text": "I understand. Continue",
                    "return_text": "Return",
                },
                "detector": {
                    "version": "work-content-warning-v1",
                    "method": "rules",
                    "review_required": False,
                    "revision": "revision",
                },
            },
            "chapters": {},
        }
        self._seal_publication()
        self.plan["steps"]["story_publish"]["output_digest"] = self.story_tree[
            "publication"
        ]["fingerprint"]

        result = self._validate()

        self.assertEqual(self.story_tree["publication"]["fingerprint"], result.fingerprint)

    def test_rejects_malformed_work_content_warnings(self):
        self.story_tree["content_warnings"] = {
            "schema_version": 1,
            "work": {
                "status": "detected",
                "has_sensitive_content": True,
                "severity": "high",
                "categories": [],
                "display_policy": {"show_at_work_entry": False},
                "localized": {},
                "detector": {},
            },
            "chapters": {},
        }
        self._seal_publication()
        self.plan["steps"]["story_publish"]["output_digest"] = self.story_tree[
            "publication"
        ]["fingerprint"]

        with self.assertRaisesRegex(ChapterBranchArtifactValidationError, "content_warnings"):
            self._validate()

    def test_rejects_multilingual_content_warning_code_mismatch(self):
        english_tree = copy.deepcopy(self.story_tree)
        japanese_tree = copy.deepcopy(self.story_tree)
        warning = {
            "schema_version": 1,
            "work": {
                "status": "detected",
                "has_sensitive_content": True,
                "severity": "high",
                "categories": [
                    {
                        "code": "gore",
                        "presence": "depicted",
                        "severity": "high",
                        "confidence": 0.9,
                        "evidence_chapters": [1],
                    }
                ],
                "display_policy": {
                    "show_at_work_entry": True,
                    "requires_acknowledgement": True,
                    "dismissible": True,
                    "allow_continue": True,
                    "allow_return": True,
                },
                "localized": {
                    "title": "Content Warning",
                    "summary": "Sensitive themes may be present.",
                    "items": [
                        {"code": "gore", "label": "Gore", "description": "Gore."}
                    ],
                    "acknowledge_text": "Continue",
                    "return_text": "Return",
                },
                "detector": {
                    "version": "work-content-warning-v1",
                    "method": "rules",
                    "review_required": False,
                    "revision": "revision",
                },
            },
            "chapters": {},
        }
        english_tree["content_warnings"] = copy.deepcopy(warning)
        japanese_warning = copy.deepcopy(warning)
        japanese_warning["work"]["categories"][0]["code"] = "violence"
        japanese_warning["work"]["localized"]["items"] = [
            {"code": "violence", "label": "暴力", "description": "暴力。"}
        ]
        japanese_tree["content_warnings"] = japanese_warning
        bundle = {"en-US": english_tree, "ja-JP": japanese_tree}
        bundle_free = copy.deepcopy(bundle)
        bundle["publication"] = {
            "run_id": "run-current",
            "run_started_at": "2026-08-25T01:00:00+00:00",
            "fingerprint": calculate_publication_fingerprint(
                dict(self.md_artifacts), bundle_free
            ),
        }
        self.plan["steps"]["story_publish"]["output_digest"] = bundle["publication"][
            "fingerprint"
        ]

        with self.assertRaisesRegex(ChapterBranchArtifactValidationError, "content_warnings"):
            validate_completed_chapter_branch_artifacts(
                task_id="task-1",
                story_tree_data=bundle,
                md_artifacts=self.md_artifacts,
                plan=self.plan,
                summary=self.summary,
                worldview=self.worldview,
            )

    def test_language_bundle_validation_checks_warning_policy_before_publish(self):
        english_tree = copy.deepcopy(self.story_tree)
        japanese_tree = copy.deepcopy(self.story_tree)
        warning = {
            "schema_version": 1,
            "work": {
                "status": "detected",
                "has_sensitive_content": True,
                "severity": "high",
                "categories": [{
                    "code": "gore",
                    "presence": "depicted",
                    "severity": "high",
                    "confidence": 0.9,
                    "evidence_chapters": [1],
                }],
                "display_policy": {
                    "show_at_work_entry": True,
                    "requires_acknowledgement": True,
                    "dismissible": True,
                    "allow_continue": True,
                    "allow_return": True,
                },
                "localized": {
                    "title": "Content Warning",
                    "summary": "Sensitive themes may be present.",
                    "items": [{"code": "gore", "label": "Gore", "description": "Blood."}],
                    "acknowledge_text": "I understand. Continue",
                    "return_text": "Return",
                },
                "detector": {
                    "version": "work-content-warning-v1",
                    "method": "rules",
                    "review_required": False,
                    "revision": "revision",
                },
            },
            "chapters": {},
        }
        english_tree["content_warnings"] = copy.deepcopy(warning)
        japanese_tree["content_warnings"] = copy.deepcopy(warning)
        japanese_tree["content_warnings"]["work"]["display_policy"]["requires_acknowledgement"] = False

        errors = validate_story_tree_language_bundle({"en-US": english_tree, "ja-JP": japanese_tree})

        self.assertTrue(errors)
        self.assertTrue(any("content_warnings" in error for error in errors))

    def test_rejects_multilingual_content_warning_machine_policy_mismatch(self):
        english_tree = copy.deepcopy(self.story_tree)
        japanese_tree = copy.deepcopy(self.story_tree)
        warning = {
            "schema_version": 1,
            "work": {
                "status": "detected",
                "has_sensitive_content": True,
                "severity": "high",
                "categories": [
                    {
                        "code": "gore",
                        "presence": "depicted",
                        "severity": "high",
                        "confidence": 0.9,
                        "evidence_chapters": [1],
                    }
                ],
                "display_policy": {
                    "show_at_work_entry": True,
                    "requires_acknowledgement": True,
                    "dismissible": True,
                    "allow_continue": True,
                    "allow_return": True,
                },
                "localized": {
                    "title": "Content Warning",
                    "summary": "Sensitive themes may be present.",
                    "items": [
                        {"code": "gore", "label": "Gore", "description": "Gore."}
                    ],
                    "acknowledge_text": "Continue",
                    "return_text": "Return",
                },
                "detector": {
                    "version": "work-content-warning-v1",
                    "method": "rules",
                    "review_required": False,
                    "revision": "revision",
                },
            },
            "chapters": {},
        }
        english_tree["content_warnings"] = copy.deepcopy(warning)
        japanese_warning = copy.deepcopy(warning)
        japanese_warning["work"]["display_policy"]["dismissible"] = False
        japanese_warning["work"]["localized"] = {
            "title": "注意",
            "summary": "注意が必要な内容を含みます。",
            "items": [
                {"code": "gore", "label": "流血", "description": "流血表現。"}
            ],
            "acknowledge_text": "理解しました",
            "return_text": "戻る",
        }
        japanese_tree["content_warnings"] = japanese_warning
        bundle = {"en-US": english_tree, "ja-JP": japanese_tree}
        bundle_free = copy.deepcopy(bundle)
        bundle["publication"] = {
            "run_id": "run-current",
            "run_started_at": "2026-08-25T01:00:00+00:00",
            "fingerprint": calculate_publication_fingerprint(
                dict(self.md_artifacts), bundle_free
            ),
        }
        self.plan["steps"]["story_publish"]["output_digest"] = bundle[
            "publication"
        ]["fingerprint"]

        with self.assertRaisesRegex(ChapterBranchArtifactValidationError, "机器字段"):
            validate_completed_chapter_branch_artifacts(
                task_id="task-1",
                story_tree_data=bundle,
                md_artifacts=self.md_artifacts,
                plan=self.plan,
                summary=self.summary,
                worldview=self.worldview,
            )


if __name__ == "__main__":
    unittest.main()
