import unittest
from unittest.mock import MagicMock, patch

from services.story_planner.core import novel_reader


class BranchTypeDetectionTests(unittest.TestCase):
    def test_worldview_romance_definition_requires_multiple_routes_and_core_drive(self):
        self.assertIn("≥2位明确的恋爱对象，且", novel_reader._WORLDVIEW_FORMAT)
        self.assertNotIn("≥2位明确的恋爱对象，或", novel_reader._WORLDVIEW_FORMAT)

    @patch.object(novel_reader, "LLMClient")
    def test_investigation_dominance_overrides_worldview_romance_flag(self, client_cls):
        client = MagicMock()
        client.chat_completion_json.return_value = {
            "scores": {
                "default": 0.04,
                "death_corridor": 0.12,
                "romance": 0.16,
                "fantasy": 0.06,
                "modular": 0.62,
            },
            "eligibility": {
                "death_corridor": False,
                "romance": False,
                "fantasy": False,
                "modular": True,
            },
            "reason": "调查死亡真相、搜集证据和破解民俗规则贯穿主要章节",
        }
        client_cls.return_value = client
        worldview = {
            "genre": "民俗怪谈、悬疑解谜",
            "is_romance_oriented": True,
            "narrative_arc": "主角追查献祭、剥皮案与引魂规则的真相。",
            "romance_targets": [
                {"name": "苏勉", "role_label": "小柳线男1"},
                {"name": "四九", "role_label": "小柳线男2"},
                {"name": "帝君", "role_label": "宋操线男1"},
            ],
        }

        detected = novel_reader.detect_branch_type(
            worldview,
            "阴冷的民俗怪谈文风",
            "openai",
            chapter_summaries=[
                "【核心事件】调查瓮中仙献祭真相；寻找死者证词。",
                "【核心事件】验尸取证；追查漂姑案幕后凶手。",
            ],
        )

        self.assertEqual("modular", detected)
        client.chat_completion_json.assert_called_once()

    @patch.object(novel_reader, "LLMClient")
    def test_close_specialized_scores_fall_back_to_default(self, client_cls):
        client = MagicMock()
        client.chat_completion_json.return_value = {
            "scores": {
                "default": 0.10,
                "death_corridor": 0.05,
                "romance": 0.34,
                "fantasy": 0.13,
                "modular": 0.38,
            },
            "eligibility": {
                "death_corridor": False,
                "romance": True,
                "fantasy": False,
                "modular": True,
            },
            "reason": "调查和恋爱机制占比接近",
        }
        client_cls.return_value = client

        detected = novel_reader.detect_branch_type(
            {"is_romance_oriented": True, "romance_targets": [{}, {}]},
            "",
            "openai",
            chapter_summaries=["调查案件，同时推进感情关系。"],
        )

        self.assertEqual("default", detected)

    @patch.object(novel_reader, "LLMClient")
    def test_romance_requires_explicit_route_eligibility(self, client_cls):
        client = MagicMock()
        client.chat_completion_json.return_value = {
            "scores": {
                "default": 0.05,
                "death_corridor": 0.04,
                "romance": 0.72,
                "fantasy": 0.03,
                "modular": 0.16,
            },
            "eligibility": {
                "death_corridor": False,
                "romance": True,
                "fantasy": False,
                "modular": False,
            },
            "reason": "同一主角可在同一阶段选择两位恋爱对象",
        }
        client_cls.return_value = client
        worldview = {
            "is_romance_oriented": True,
            "romance_targets": [
                {"name": "甲", "role_label": "男1"},
                {"name": "乙", "role_label": "男2"},
            ],
        }

        detected = novel_reader.detect_branch_type(
            worldview,
            "",
            "openai",
            chapter_summaries=["主角持续在甲与乙之间作出关系选择。"],
        )

        self.assertEqual("romance", detected)

    @patch.object(novel_reader, "LLMClient")
    def test_high_romance_share_without_same_route_scope_falls_back_to_default(
        self,
        client_cls,
    ):
        client = MagicMock()
        client.chat_completion_json.return_value = {
            "scores": {
                "default": 0.05,
                "death_corridor": 0.04,
                "romance": 0.72,
                "fantasy": 0.03,
                "modular": 0.16,
            },
            "eligibility": {
                "death_corridor": False,
                "romance": False,
                "fantasy": False,
                "modular": False,
            },
            "reason": "恋爱对象分属不同主角和不同时间线，不能形成可选择路线",
        }
        client_cls.return_value = client

        detected = novel_reader.detect_branch_type(
            {
                "is_romance_oriented": True,
                "romance_targets": [
                    {"name": "甲", "role_label": "小柳线男1"},
                    {"name": "乙", "role_label": "宋操线男1"},
                ],
            },
            "",
            "openai",
            chapter_summaries=["两位主角分别拥有各自的情感关系。"],
        )

        self.assertEqual("default", detected)

    @patch.object(novel_reader, "LLMClient")
    def test_long_novel_detection_keeps_evidence_from_last_chapter(self, client_cls):
        client = MagicMock()
        client.chat_completion_json.return_value = {
            "scores": {
                "default": 0.20,
                "death_corridor": 0.05,
                "romance": 0.05,
                "fantasy": 0.05,
                "modular": 0.65,
            },
            "eligibility": {
                "death_corridor": False,
                "romance": False,
                "fantasy": False,
                "modular": True,
            },
            "reason": "调查主导",
        }
        client_cls.return_value = client
        summaries = [
            f"第{index + 1}章摘要：" + ("调查线索。" * 120)
            for index in range(200)
        ]
        summaries[-1] = "最终章唯一标记：真相公开，案件收束。" + ("结案。" * 100)

        novel_reader.detect_branch_type(
            {"genre": "悬疑"},
            "",
            "openai",
            chapter_summaries=summaries,
        )

        prompt = client.chat_completion_json.call_args.kwargs["user_prompt"]
        self.assertIn("【第200章】", prompt)
        self.assertIn("最终章唯一标记", prompt)

    @patch.object(novel_reader, "LLMClient")
    def test_percentage_scores_are_normalized_before_selection(self, client_cls):
        client = MagicMock()
        client.chat_completion_json.return_value = {
            "scores": {
                "default": 4,
                "death_corridor": 12,
                "romance": 16,
                "fantasy": 6,
                "modular": 62,
            },
            "eligibility": {
                "death_corridor": False,
                "romance": False,
                "fantasy": False,
                "modular": True,
            },
            "reason": "调查主导",
        }
        client_cls.return_value = client

        detected = novel_reader.detect_branch_type(
            {"genre": "民俗悬疑"},
            "",
            "openai",
            chapter_summaries=["持续调查怪谈真相"],
        )

        self.assertEqual("modular", detected)

    @patch.object(novel_reader, "LLMClient")
    def test_clear_forty_percent_specialized_lead_beats_default(self, client_cls):
        client = MagicMock()
        client.chat_completion_json.return_value = {
            "scores": {
                "default": 0.34,
                "death_corridor": 0.08,
                "romance": 0.08,
                "fantasy": 0.06,
                "modular": 0.44,
            },
            "eligibility": {
                "death_corridor": False,
                "romance": False,
                "fantasy": False,
                "modular": True,
            },
            "reason": "调查机制明确领先，但作品也有较多人物命运内容",
        }
        client_cls.return_value = client

        detected = novel_reader.detect_branch_type(
            {"genre": "民俗悬疑"},
            "",
            "openai",
            chapter_summaries=["调查怪谈规则和死亡真相"],
        )

        self.assertEqual("modular", detected)

    @patch.object(novel_reader, "detect_branch_type", return_value="modular")
    @patch.object(novel_reader, "_extract_behavior_anchors", return_value="anchors")
    @patch.object(novel_reader, "_extract_style_samples_llm", return_value="style")
    @patch.object(novel_reader, "_summarize_one_chapter", return_value="summary")
    @patch.object(novel_reader, "LLMClient")
    def test_non_romance_branch_type_disables_romance_runtime_behavior(
        self,
        _client,
        _summarize,
        _style,
        _anchors,
        detect_type,
    ):
        worldview = {
            "is_romance_oriented": True,
            "characters": [
                {
                    "name": "小柳",
                    "role": "主角",
                    "fatal_flaw": "执着追查父亲死因",
                    "secrets": ["秘密一", "秘密二", "秘密三"],
                }
            ],
            "persona_axes": [
                {"key": "affection_su_mian"},
                {"key": "affection_si_jiu"},
            ],
            "ending_dimensions": ["案件真相", "主角归宿"],
            "factions": [{"name": "阴司"}],
            "hidden_threads": [{"name": "瓮中仙"}],
        }

        with patch("builtins.print"):
            context = novel_reader.analyze_novel(
                chapters=["第一章"],
                full_text="第一章",
                provider="openai",
                worldview_override=worldview,
            )

        self.assertEqual("modular", context.branch_type)
        self.assertFalse(context.is_romance_oriented)
        detect_type.assert_called_once()
        self.assertEqual("style", detect_type.call_args.args[1])
        self.assertEqual("openai", detect_type.call_args.args[2])
        self.assertEqual(["summary"], detect_type.call_args.kwargs["chapter_summaries"])

    @patch.object(novel_reader, "detect_branch_type", return_value="modular")
    def test_valid_cached_context_refreshes_outdated_branch_type(self, detect_type):
        worldview = {
            "is_romance_oriented": True,
            "characters": [
                {
                    "name": "小柳",
                    "role": "主角",
                    "fatal_flaw": "执着追查父亲死因",
                    "secrets": ["秘密一", "秘密二", "秘密三"],
                }
            ],
            "persona_axes": [
                {"key": "affection_su_mian"},
                {"key": "affection_si_jiu"},
            ],
            "ending_dimensions": ["案件真相", "主角归宿"],
            "factions": [{"name": "阴司"}],
            "hidden_threads": [{"name": "瓮中仙"}],
        }
        context = novel_reader.NovelContext(
            worldview=worldview,
            chapter_summaries=["调查瓮中仙献祭真相"],
            style_samples="民俗悬疑",
            branch_type="romance",
            is_romance_oriented=True,
        )

        with patch("builtins.print"):
            refreshed = novel_reader.ensure_novel_context_worldview(
                context,
                full_text="原文",
                provider="openai",
            )

        self.assertEqual("modular", refreshed.branch_type)
        self.assertFalse(refreshed.is_romance_oriented)
        detect_type.assert_called_once()

    @patch.object(novel_reader, "detect_branch_type")
    def test_current_strategy_cache_does_not_repeat_llm_detection(self, detect_type):
        worldview = {
            "characters": [
                {
                    "name": "小柳",
                    "role": "主角",
                    "fatal_flaw": "执着追查父亲死因",
                    "secrets": ["秘密一", "秘密二", "秘密三"],
                }
            ],
            "persona_axes": [{"key": "clarity"}, {"key": "courage"}],
            "ending_dimensions": ["案件真相", "主角归宿"],
            "factions": [{"name": "阴司"}],
            "hidden_threads": [{"name": "瓮中仙"}],
        }
        context = novel_reader.NovelContext(
            worldview=worldview,
            chapter_summaries=["调查瓮中仙献祭真相"],
            branch_type="modular",
            branch_type_strategy_version=novel_reader._BRANCH_TYPE_STRATEGY_VERSION,
        )

        refreshed = novel_reader.ensure_novel_context_worldview(
            context,
            full_text="原文",
            provider="openai",
        )

        self.assertEqual("modular", refreshed.branch_type)
        detect_type.assert_not_called()


if __name__ == "__main__":
    unittest.main()
