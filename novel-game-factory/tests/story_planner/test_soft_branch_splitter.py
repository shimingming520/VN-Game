import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from services.story_planner.core import soft_branch
from services.story_planner.core.soft_branch import _split_scenes


class SoftBranchSceneSplitterTests(unittest.TestCase):
    def test_romance_lookup_ignores_scalar_affection_key_input(self):
        """A malformed scalar ``aff_keys`` value must not be split into characters."""
        lookup = soft_branch._build_romance_name_to_key(
            [{"name": "甲角色"}],
            {
                "characters": [{"name": "甲角色", "role": "重要角色"}],
            },
            [{"key": "affection_a", "description": "甲角色好感"}],
            "affection_a",
        )

        self.assertEqual({}, lookup)

    def test_romance_fallback_never_treats_declared_protagonist_as_route_target(self):
        lookup = soft_branch._build_romance_name_to_key(
            [],
            {
                "characters": [
                    {"name": "林晚", "role": "女主角", "is_protagonist": True},
                    {"name": "许定", "role": "重要角色"},
                ],
            },
            [
                {"key": "affection_linwan", "description": "林晚好感度"},
                {"key": "affection_xuding", "description": "许定好感度"},
            ],
            ["affection_linwan", "affection_xuding"],
        )

        self.assertNotIn("林晚", lookup)
        self.assertEqual("affection_xuding", lookup["许定"])

    def test_romance_fallback_recognizes_extended_protagonist_role_labels(self):
        lookup = soft_branch._build_romance_name_to_key(
            [],
            {
                "characters": [
                    {"name": "林晚", "role": "女主角（叙事主视角）"},
                    {"name": "许定", "role": "hero"},
                    {"name": "梁今越", "role": "重要配角"},
                ],
            },
            [
                {"key": "affection_xuding", "description": "许定好感度"},
                {"key": "affection_liang", "description": "梁今越好感度"},
            ],
            ["affection_xuding", "affection_liang"],
        )

        self.assertNotIn("林晚", lookup)
        self.assertNotIn("许定", lookup)
        self.assertEqual("affection_liang", lookup["梁今越"])

    def test_romance_lookup_accepts_legacy_alias_conflict_shapes(self):
        """Both normalized and legacy conflict encodings must disable an alias."""
        worldview = {
            "characters": [
                {"name": "甲角色", "role": "重要角色"},
                {"name": "乙角色", "role": "重要角色"},
            ],
            "character_alias_map": {},
            "character_alias_conflicts": {"小明": ["甲角色", "乙角色"]},
        }

        lookup = soft_branch._build_romance_name_to_key(
            [{"name": "甲角色", "aliases": ["小明"]}, {"name": "乙角色"}],
            worldview,
            [
                {"key": "affection_jia", "description": "甲角色好感"},
                {"key": "affection_yi", "description": "乙角色好感"},
            ],
            ["affection_jia", "affection_yi"],
        )

        self.assertNotIn("小明", lookup)

    def test_romance_lookup_accepts_alias_list_conflict_shape(self):
        """A conflict item may expose ``aliases`` instead of one ``alias``."""
        worldview = {
            "characters": [
                {"name": "甲角色", "role": "重要角色"},
                {"name": "乙角色", "role": "重要角色"},
            ],
            "character_alias_map": {},
            "character_alias_conflicts": [
                {"aliases": ["小明"], "characters": ["甲角色", "乙角色"]},
            ],
        }

        lookup = soft_branch._build_romance_name_to_key(
            [{"name": "甲角色", "aliases": ["小明"]}, {"name": "乙角色"}],
            worldview,
            [
                {"key": "affection_jia", "description": "甲角色好感"},
                {"key": "affection_yi", "description": "乙角色好感"},
            ],
            ["affection_jia", "affection_yi"],
        )

        self.assertNotIn("小明", lookup)

    def test_romance_lookup_accepts_single_conflict_object_shape(self):
        worldview = {
            "characters": [
                {"name": "甲角色", "role": "重要角色"},
                {"name": "乙角色", "role": "重要角色"},
            ],
            "character_alias_map": {},
            "character_alias_conflicts": {
                "alias": "小明",
                "character": "甲角色、乙角色",
                "reason": "共享称呼",
            },
        }

        lookup = soft_branch._build_romance_name_to_key(
            [{"name": "甲角色", "aliases": ["小明"]}, {"name": "乙角色"}],
            worldview,
            [
                {"key": "affection_jia", "description": "甲角色好感"},
                {"key": "affection_yi", "description": "乙角色好感"},
            ],
            ["affection_jia", "affection_yi"],
        )

        self.assertNotIn("小明", lookup)

    def test_romance_lookup_disables_alias_that_collides_with_a_canonical_name(self):
        worldview = {
            "characters": [
                {"name": "小明", "role": "重要角色"},
                {"name": "乙角色", "role": "重要角色"},
            ],
            "character_alias_map": {"小明": "乙角色"},
        }

        lookup = soft_branch._build_romance_name_to_key(
            [{"name": "乙角色", "aliases": ["小明"]}],
            worldview,
            [{"key": "affection_yi", "description": "乙角色好感"}],
            ["affection_yi"],
        )

        self.assertNotIn("小明", lookup)
        self.assertEqual("affection_yi", lookup["乙角色"])

    def test_romance_generation_identity_includes_oriented_flag(self):
        """Romance-oriented checkpoints cannot be reused as generic defaults."""
        self.assertFalse(
            soft_branch.plan_has_current_soft_branch_generation_identity(
                {"branch_type": "default", "is_romance_oriented": True}
            )
        )
        self.assertTrue(
            soft_branch.plan_has_current_soft_branch_generation_identity(
                {
                    "branch_type": "default",
                    "is_romance_oriented": True,
                    "soft_branch_generation_identity": {
                        "original_scene_budget_version": 2,
                        "romance_presence_rule_version": 4,
                    },
                }
            )
        )

    def test_soft_branch_generation_identity_versions_by_branch_type(self):
        self.assertEqual(
            {
                "original_scene_budget_version": 2,
                "protagonist_presence_rule_version": 4,
            },
            soft_branch.soft_branch_generation_identity("modular"),
        )
        self.assertEqual(
            {"original_scene_budget_version": 2},
            soft_branch.soft_branch_generation_identity("default"),
        )
        self.assertEqual(
            {
                "original_scene_budget_version": 2,
                "romance_presence_rule_version": 4,
            },
            soft_branch.soft_branch_generation_identity("romance"),
        )
        self.assertFalse(
            soft_branch.plan_has_current_soft_branch_generation_identity(
                {"branch_type": "modular"}
            )
        )
        self.assertTrue(
            soft_branch.plan_has_current_soft_branch_generation_identity(
                {
                    "branch_type": "modular",
                    "soft_branch_generation_identity": {
                        "original_scene_budget_version": 2,
                        "protagonist_presence_rule_version": 4
                    },
                }
            )
        )
        self.assertFalse(
            soft_branch.plan_has_current_soft_branch_generation_identity(
                {"branch_type": "default"}
            )
        )
        self.assertTrue(
            soft_branch.plan_has_current_soft_branch_generation_identity(
                {
                    "branch_type": "default",
                    "soft_branch_generation_identity": {
                        "original_scene_budget_version": 2,
                    },
                }
            )
        )
        self.assertFalse(
            soft_branch.plan_has_current_soft_branch_generation_identity(
                {"branch_type": "romance"}
            )
        )
        self.assertTrue(
            soft_branch.plan_has_current_soft_branch_generation_identity(
                {
                    "branch_type": "romance",
                    "soft_branch_generation_identity": {
                        "original_scene_budget_version": 2,
                        "romance_presence_rule_version": 4
                    },
                }
            )
        )

    def test_transition_validator_rejects_object_location_conflict(self):
        candidate = {
            "content_a": "我把那张鲜红拜帖揣进袖中，准备继续往前走。",
            "content_b": "我先把拜帖收好，再回头观察门口。",
            "clue_flags_a": {},
            "clue_flags_b": {},
        }

        issues = soft_branch.validate_soft_branch_transition(
            candidate,
            upper_text="我在门口看见一张鲜红拜帖。",
            downstream_text="扫帚上正卡着那张鲜红拜帖。",
        )

        self.assertTrue(any(issue["category"] == "object_state_conflict" for issue in issues))

    def test_transition_validator_rejects_premature_action_and_decisive_clue(self):
        candidate = {
            "content_a": "我拆开窗锁，随后从夹层里取出翡翠断齿。",
            "content_b": "我已经确认窗锁后的翡翠断齿就是关键证据。",
            "clue_flags_a": {
                "window_scratch": {
                    "source_type": "decisive_clue",
                    "source_excerpt": "本章没有对应原文证据",
                }
            },
            "clue_flags_b": {},
        }

        issues = soft_branch.validate_soft_branch_transition(
            candidate,
            upper_text="我来到偏屋窗下，暂时只看见窗框上的划痕。",
            downstream_text="众人拆开窗锁，才从夹层里发现那枚翡翠断齿。",
        )

        categories = {issue["category"] for issue in issues}
        self.assertIn("downstream_action_completed", categories)
        self.assertIn("decisive_clue_leaked", categories)

    def test_transition_validator_rejects_character_state_conflict(self):
        candidate = {
            "content_a": "方多病转身离开山庄，独自回城报信。",
            "content_b": "我让方多病先行离开山庄。",
            "clue_flags_a": {},
            "clue_flags_b": {},
        }

        issues = soft_branch.validate_soft_branch_transition(
            candidate,
            upper_text="方多病与我一同站在山庄正厅。",
            downstream_text="方多病仍站在厅中，当即开口质问管家。",
        )

        self.assertTrue(any(issue["category"] == "character_state_conflict" for issue in issues))

    def test_clue_flags_require_source_for_decisive_clues(self):
        normalized, issues = soft_branch.normalize_clue_flags(
            {"x": {"source_type": "decisive_clue", "category": "physical_evidence"}},
            source_text="前文只有模糊划痕，没有发现关键证据。",
        )

        self.assertEqual({}, normalized)
        self.assertTrue(any(item["category"] == "clue_source_missing" for item in issues))

    def test_marker_persists_clue_flags_for_each_route(self):
        marker = soft_branch._build_soft_branch_marker(
            choice_a="查看门框划痕",
            content_a="我只记录下门框上已经公开的划痕。",
            effects_a={},
            choice_b="询问值守人员",
            content_b="我向值守人员核对时间。",
            effects_b={},
            clue_flags_a={
                "window_scratch": {
                    "source_type": "source_fact",
                    "source_excerpt": "门框上有一道旧划痕",
                    "category": "physical_evidence",
                }
            },
            clue_flags_b={},
        )

        self.assertIn("clue_flags_a=", marker)
        self.assertIn("source_fact", marker)

    def test_strip_soft_branch_blocks_restores_mainline_for_state_recovery(self):
        marker = soft_branch._build_soft_branch_marker(
            choice_a="我开口说明",
            content_a="A 路心理正文。",
            effects_a={"courage": 3},
            choice_b="我保持沉默",
            content_b="B 路心理正文。",
            effects_b={"courage": -3},
            bridge="B 路收束。",
        )
        content = "# 第一章\n\n主线前半段。" + marker + "主线后半段。"

        stripped = soft_branch.strip_soft_branch_blocks(content)

        self.assertIn("主线前半段。", stripped)
        self.assertIn("主线后半段。", stripped)
        self.assertNotIn("软分支选择", stripped)
        self.assertNotIn("A 路心理正文", stripped)

    def test_normalizes_safe_unclosed_quote_in_soft_branch_field(self):
        payload = {
            "content_a": "我听见他说：“别回头。",
            "content_b": "我没有回答，只是继续向前。",
            "bridge": "风从身后追了上来。",
        }

        normalized, ok, reason = soft_branch.normalize_soft_branch_prose_fields(payload)

        self.assertTrue(ok, reason)
        self.assertEqual("我听见他说：“别回头。”", normalized["content_a"])

    def test_rejects_unclosed_quote_outside_last_soft_branch_paragraph(self):
        payload = {
            "content_a": "我听见他说：“别回头。\n\n我没有回答，只是继续向前。",
            "content_b": "我把手收了回来。",
            "bridge": "风从身后追了上来。",
        }

        normalized, ok, reason = soft_branch.normalize_soft_branch_prose_fields(payload)

        self.assertFalse(ok)
        self.assertEqual(payload["content_a"], normalized["content_a"])
        self.assertIn("content_a", reason)

    @patch.object(soft_branch, "LLMClient")
    def test_injection_repairs_safe_quote_before_building_marker(self, client_cls):
        client = MagicMock()
        client.chat_completion_json.return_value = {
            "choice_a": "我决定开口说明",
            "content_a": "我听见他说：“别回头。",
            "effects_a": {"courage": 3},
            "choice_b": "我暂时保持沉默",
            "content_b": "我把手收了回来。",
            "effects_b": {"courage": -3},
            "bridge": "风从身后追了上来。",
        }
        client_cls.return_value = client
        chapter = "\n".join(f"这是第{i}段正文。" for i in range(1, 7))

        content = soft_branch.inject_soft_branches(
            chapter_text=chapter,
            chapter_idx=0,
            provider="openai",
            scenes_count=2,
            variable_defs=[{"key": "courage", "init": 50, "threshold": 70}],
            total_branches=1,
        )

        self.assertIn("我听见他说：“别回头。”", content)

    @patch.object(soft_branch, "LLMClient")
    def test_injection_retries_only_current_branch_for_ambiguous_quote(self, client_cls):
        client = MagicMock()
        client.chat_completion_json.side_effect = [
            {
                "choice_a": "我决定开口说明",
                "content_a": "我听见他说：“别回头。\n\n我没有回答。",
                "effects_a": {"courage": 3},
                "choice_b": "我暂时保持沉默",
                "content_b": "我把手收了回来。",
                "effects_b": {"courage": -3},
                "bridge": "风从身后追了上来。",
            },
            {
                "choice_a": "我决定开口说明",
                "content_a": "我听见他说：“别回头。”",
                "effects_a": {"courage": 3},
                "choice_b": "我暂时保持沉默",
                "content_b": "我把手收了回来。",
                "effects_b": {"courage": -3},
                "bridge": "风从身后追了上来。",
            },
        ]
        client_cls.return_value = client
        chapter = "\n".join(f"这是第{i}段正文。" for i in range(1, 7))

        content = soft_branch.inject_soft_branches(
            chapter_text=chapter,
            chapter_idx=0,
            provider="openai",
            scenes_count=2,
            variable_defs=[{"key": "courage", "init": 50, "threshold": 70}],
            total_branches=1,
        )

        self.assertEqual(2, client.chat_completion_json.call_count)
        self.assertNotIn("我没有回答", content)
        self.assertIn("我听见他说：“别回头。”", content)

    @patch.object(soft_branch, "LLMClient")
    def test_invalid_soft_branch_uses_one_simple_fallback_after_three_attempts(self, client_cls):
        client = MagicMock()
        client.chat_completion_json.side_effect = [
            {},
            {},
            {},
            {
                "choice_a": "我决定开口说明",
                "content_a": "我把事情完整地说明了。",
                "effects_a": {"courage": 3},
                "choice_b": "我暂时保持沉默",
                "content_b": "我把话咽了回去。",
                "effects_b": {"courage": -3},
                "bridge": "我最终让自己平静下来。",
            },
        ]
        client_cls.return_value = client
        metrics = {}
        chapter = "\n".join(f"这是第{i}段正文。" for i in range(1, 7))

        content = soft_branch.inject_soft_branches(
            chapter_text=chapter,
            chapter_idx=0,
            provider="openai",
            scenes_count=2,
            variable_defs=[{"key": "courage", "init": 50, "threshold": 70}],
            total_branches=1,
            metrics=metrics,
        )

        self.assertIn("软分支选择", content)
        self.assertEqual(4, client.chat_completion_json.call_count)
        self.assertEqual(1, metrics["simple_llm_fallback"])
        self.assertEqual(4, metrics["validation_attempted"])
        self.assertEqual(0, metrics["validation_dropped"])
        for index in range(1, 7):
            self.assertIn(f"这是第{index}段正文。", content)

    @patch.object(soft_branch, "LLMClient")
    def test_gap_checkpoint_resumes_without_recalling_llm(self, client_cls):
        client = MagicMock()
        client_cls.return_value = client
        chapter = "\n".join(f"这是第{i}段正文。" for i in range(1, 7))
        marker = soft_branch._build_soft_branch_marker(
            choice_a="我决定开口说明",
            content_a="我把事情完整地说明了。",
            effects_a={"courage": 3},
            choice_b="我暂时保持沉默",
            content_b="我把话咽了回去。",
            effects_b={"courage": -3},
            bridge="我最终让自己平静下来。",
        )
        checkpoint_input = {
            "chapter_sha256": soft_branch.content_sha256(chapter),
                    "scene_count": 2,
                    "branch_type": "default",
                    "original_scene_budget_version": 2,
                    "output_language": None,
            "variable_defs": [{"key": "courage", "init": 50, "threshold": 70}],
            "required_effect_keys": [],
            "current_chapter_summary": "",
        }
        state = soft_branch.initialize_soft_branch_gap_checkpoint(
            None,
            gap_count=1,
            input_payload=checkpoint_input,
        )
        soft_branch.record_soft_branch_gap(
            state,
            gap_index=0,
            marker=marker,
        )

        content = soft_branch.inject_soft_branches(
            chapter_text=chapter,
            chapter_idx=0,
            provider="openai",
            scenes_count=2,
            variable_defs=[{"key": "courage", "init": 50, "threshold": 70}],
            total_branches=1,
            resume_state=state,
        )

        self.assertIn("我决定开口说明", content)
        client.chat_completion_json.assert_not_called()

    def test_soft_branch_marker_assigns_route_legacy_flags_without_granting_all_at_once(self):
        first = soft_branch._build_soft_branch_marker(
            choice_a="主动留下说明",
            content_a="我把想说的话写了下来。",
            effects_a={"self_worth": 4},
            choice_b="继续保持沉默",
            content_b="我最终没有开口。",
            effects_b={"self_worth": -4},
            legacy_slot=0,
        )
        second = soft_branch._build_soft_branch_marker(
            choice_a="请老师保存记录",
            content_a="我请老师把处理经过留档。",
            effects_a={"self_worth": 4},
            choice_b="把记录收起来",
            content_b="我决定不再追究。",
            effects_b={"self_worth": -4},
            legacy_slot=1,
        )

        self.assertIn("legacy_flags_a={'signed_clarification': 1}", first)
        self.assertIn("legacy_flags_b={}", first)
        self.assertNotIn("school_record_preserved': 1", first)
        self.assertIn("legacy_flags_a={'school_record_preserved': 1}", second)

    def test_recent_semantic_duplicates_are_rewritten_with_observable_behaviors(self):
        recent = [
            "我想把自己藏起来",
            "我想把自己藏回去",
            "我想缩回影子里",
        ]

        rewritten = soft_branch.ensure_distinct_choice_text(
            "我想躲回人群里",
            recent,
            variable_key="courage",
            polarity="negative",
        )

        self.assertNotIn(rewritten, recent)
        self.assertNotIn("藏", rewritten)
        self.assertNotIn("躲", rewritten)
        self.assertTrue(any(word in rewritten for word in ("沉默", "后退", "移开", "交给")))

    def test_choice_history_keeps_full_run_exact_dedupe_window(self):
        history = []
        for index in range(40):
            soft_branch.remember_choice_text(history, f"选择{index}")

        self.assertEqual(40, len(history))
        self.assertEqual("选择0", history[0])

    def test_resumed_markdown_restores_choice_history(self):
        history = ["已有选项"]
        content = (
            "▶ 选项A：我主动问清她的心意\n"
            "▶ 选项B：我把没说完的话咽下\n"
        )

        soft_branch.remember_choices_from_markdown(history, content)

        self.assertEqual(
            ["已有选项", "我主动问清她的心意", "我把没说完的话咽下"],
            history,
        )

    def test_template_exhaustion_never_appends_numeric_suffix(self):
        history = [
            "我先沉默下来",
            "我后退一步观察",
            "我移开视线等待",
            "我把决定交给别人",
        ]

        rewritten = soft_branch.ensure_distinct_choice_text(
            "我先沉默下来",
            history,
            variable_key="honesty",
            polarity="negative",
        )

        self.assertNotRegex(rewritten, r"\d+$")
        self.assertNotIn(rewritten, history)

    def test_exact_duplicates_are_rewritten_even_when_older_than_semantic_window(self):
        history = [f"旧选项{i}" for i in range(40)]
        history.insert(0, "我主动问清她的心意")

        rewritten = soft_branch.ensure_distinct_choice_text(
            "我主动问清她的心意",
            history,
            variable_key="courage",
            polarity="positive",
        )

        self.assertNotEqual("我主动问清她的心意", rewritten)
        self.assertNotIn(rewritten, history)

    def test_passive_story_description_is_rewritten_to_player_action(self):
        rewritten = soft_branch.ensure_distinct_choice_text(
            "织音的声音刺回来",
            [],
            variable_key="courage",
            polarity="positive",
        )

        self.assertNotEqual("织音的声音刺回来", rewritten)
        self.assertTrue(rewritten.startswith("我"))

    def test_passive_first_person_result_is_rewritten_to_player_action(self):
        rewritten = soft_branch.ensure_distinct_choice_text(
            "我被织音同学拉住",
            [],
            variable_key="courage",
            polarity="positive",
        )

        self.assertNotEqual("我被织音同学拉住", rewritten)
        self.assertNotIn("被", rewritten[:3])

    def test_recent_choice_prefix_density_is_limited(self):
        recent = ["我想先说清楚", "我想再确认一次", "我想把话咽回去"]

        rewritten = soft_branch.ensure_distinct_choice_text(
            "我想看着她离开",
            recent,
            variable_key="courage",
            polarity="positive",
        )

        self.assertFalse(rewritten.startswith("我想"))

    def test_romance_names_match_affection_keys_from_variable_descriptions(self):
        result = soft_branch._match_names_to_affection_keys(
            ["凌妙妙", "凌虞", "柳拂衣", "慕瑶", "慕声"],
            [
                "affection_liufuyi",
                "affection_muyao",
                "affection_musheng",
            ],
            [
                {
                    "key": "affection_liufuyi",
                    "description": (
                        "选择信任柳拂衣并在慕瑶危机中共同承担，"
                        "会累积柳拂衣好感度并触发柳拂衣线"
                    ),
                },
                {
                    "key": "affection_muyao",
                    "description": "优先保护慕瑶会累积慕瑶好感度",
                },
                {
                    "key": "affection_musheng",
                    "description": "理解慕声守护慕瑶的执念，会累积慕声好感度",
                },
            ],
        )

        self.assertEqual("affection_liufuyi", result["柳拂衣"])
        self.assertEqual("affection_muyao", result["慕瑶"])
        self.assertEqual("affection_musheng", result["慕声"])
        self.assertNotIn("凌妙妙", result)
        self.assertNotIn("凌虞", result)

    def test_romance_composite_target_merges_character_card_aliases(self):
        worldview = {
            "romance_targets": [
                {"name": "许定（明镜非台/青山不许）"},
            ],
            "characters": [
                {
                    "name": "许定",
                    "aliases": ["明镜", "明镜非台", "青山不许"],
                },
            ],
            "character_alias_map": {
                "许定": "许定",
                "明镜": "许定",
                "明镜非台": "许定",
                "青山不许": "许定",
            },
        }

        result = soft_branch._build_romance_name_to_key(
            worldview["romance_targets"],
            worldview,
            [
                {
                    "key": "affection_xuding",
                    "description": "许定好感度与许定线结局",
                }
            ],
            ["affection_xuding"],
        )

        for alias in ("许定", "明镜", "明镜非台", "青山不许"):
            self.assertEqual("affection_xuding", result[alias])
        self.assertEqual(
            ["affection_xuding"],
            soft_branch._romance_keys_present_in_text(
                "她抬头看见明镜非台，终于没有再躲开。", result
            ),
        )

    def test_romance_target_alias_list_is_flattened_without_bracket_artifacts(self):
        result = soft_branch._build_romance_name_to_key(
            [
                {
                    "name": "顾沉",
                    "aliases": ["阿沉", "顾先生"],
                }
            ],
            {"characters": []},
            [{"key": "affection_guchen", "description": "顾沉好感度"}],
            ["affection_guchen"],
        )

        self.assertEqual("affection_guchen", result["阿沉"])
        self.assertEqual("affection_guchen", result["顾先生"])
        self.assertFalse(any("[" in alias or "]" in alias for alias in result))

    def test_romance_route_matches_variable_description_by_explicit_alias(self):
        result = soft_branch._build_romance_name_to_key(
            [
                {"name": "许定（明镜非台）"},
                {"name": "梁今越"},
            ],
            {"characters": []},
            [
                {
                    "key": "affection_route_a",
                    "description": "明镜非台路线好感度",
                },
                {
                    "key": "affection_route_b",
                    "description": "梁今越路线好感度",
                },
            ],
            ["affection_route_b", "affection_route_a"],
        )

        self.assertEqual("affection_route_a", result["许定"])
        self.assertEqual("affection_route_a", result["明镜非台"])
        self.assertEqual("affection_route_b", result["梁今越"])

    def test_romance_alias_description_mapping_does_not_depend_on_key_order(self):
        result = soft_branch._build_romance_name_to_key(
            [
                {"name": "许定（明镜非台）"},
                {"name": "梁今越"},
            ],
            {"characters": []},
            [
                {
                    "key": "affection_route_a",
                    "description": "明镜非台路线好感度",
                },
                {
                    "key": "affection_route_b",
                    "description": "梁今越路线好感度",
                },
            ],
            # Deliberately reverse the key order: alias evidence must win.
            ["affection_route_b", "affection_route_a"],
        )

        self.assertEqual("affection_route_a", result["许定"])
        self.assertEqual("affection_route_b", result["梁今越"])

    def test_romance_alias_only_descriptions_map_composite_targets(self):
        result = soft_branch._build_romance_name_to_key(
            [
                {"name": "许定（明镜非台）"},
                {"name": "梁今越（旧月）"},
            ],
            {"characters": []},
            [
                {
                    "key": "affection_route_a",
                    "description": "明镜非台路线好感度",
                },
                {
                    "key": "affection_route_b",
                    "description": "旧月路线好感度",
                },
            ],
            ["affection_route_b", "affection_route_a"],
        )

        self.assertEqual("affection_route_a", result["许定"])
        self.assertEqual("affection_route_b", result["梁今越"])

    def test_romance_alias_pinyin_maps_to_affection_key_before_positional_fallback(self):
        pinyin = {
            "许定": ["xu", "ding"],
            "明镜非台": ["ming", "jing", "fei", "tai"],
            "梁今越": ["liang", "jin", "yue"],
        }
        fake_pypinyin = SimpleNamespace(lazy_pinyin=lambda value: pinyin[value])
        with patch.dict(sys.modules, {"pypinyin": fake_pypinyin}):
            result = soft_branch._build_romance_name_to_key(
                [
                    {"name": "许定（明镜非台）"},
                    {"name": "梁今越"},
                ],
                {"characters": []},
                [
                    {
                        "key": "affection_mingjingfeitai",
                        "description": "第一条路线",
                    },
                    {
                        "key": "affection_liangjinyue",
                        "description": "第二条路线",
                    },
                ],
                # Reverse order so only alias pinyin can identify the first route.
                ["affection_liangjinyue", "affection_mingjingfeitai"],
            )

        self.assertEqual("affection_mingjingfeitai", result["许定"])
        self.assertEqual("affection_liangjinyue", result["梁今越"])

    def test_ambiguous_romance_alias_is_never_bound_to_a_route(self):
        result = soft_branch._build_romance_name_to_key(
            [{"name": "小明"}],
            {
                "characters": [
                    {"name": "甲角色", "aliases": ["小明"]},
                    {"name": "乙角色", "aliases": ["小明"]},
                ],
            },
            [
                {"key": "affection_jia", "description": "甲角色路线"},
                {"key": "affection_yi", "description": "乙角色路线"},
            ],
            ["affection_jia", "affection_yi"],
        )

        self.assertNotIn("小明", result)
        self.assertIn("甲角色", result)
        self.assertIn("乙角色", result)
        self.assertEqual([], soft_branch._romance_keys_present_in_text("小明出现了", result))

    def test_legacy_suffix_matching_only_applies_to_canonical_names(self):
        lookup = soft_branch._RomanceRouteLookup(
            {
                "顾沉": "affection_guchen",
                "顾先生": "affection_guchen",
            },
            canonical_names={"顾沉"},
        )

        self.assertEqual([], soft_branch._romance_route_hits("先生走进房间", lookup))
        self.assertEqual(
            [("affection_guchen", "顾沉")],
            soft_branch._romance_route_hits("顾沉走进房间", lookup),
        )

    @patch.object(soft_branch, "_split_scenes")
    @patch.object(soft_branch, "LLMClient")
    def test_romance_presence_checks_first_three_paragraphs_of_downstream_scene(
        self, client_cls, split_scenes
    ):
        client = MagicMock()
        client.chat_completion_json.return_value = {
            "choice_a": "我主动回应许定",
            "content_a": "我把话说到一半，停下来等他接住。",
            "effects_a": {"affection_xuding": 3},
            "choice_b": "我暂时避开许定",
            "content_b": "我移开视线，把没有说完的话暂时收回。",
            "effects_b": {"affection_xuding": -3},
            "bridge": "我最终停在接续点之前。",
        }
        client_cls.return_value = client
        context = SimpleNamespace(
            is_romance_oriented=True,
            worldview={
                "characters": [{"name": "主角", "role": "主角"}, {"name": "许定"}],
                "romance_targets": [{"name": "许定"}],
            },
            chapter_summaries=[],
        )
        upper = "\n\n".join(
            [
                "主角在走廊尽头停下。",
                "窗外下着雨，谁也没有先开口。",
            ]
        )
        lower = "\n\n".join(
            [
                "下半场开头仍是空荡的教室。",
                "许定从门外回来，把伞放在桌角。",
                "主角抬头看了他一眼。",
            ]
        )
        split_scenes.return_value = [upper, lower]
        chapter = upper + "\n\n" + lower

        content = soft_branch.inject_soft_branches(
            chapter_text=chapter,
            chapter_idx=0,
            provider="openai",
            novel_context=context,
            scenes_count=2,
            variable_defs=[
                {
                    "key": "affection_xuding",
                    "init": 50,
                    "threshold": 65,
                    "description": "许定好感度",
                }
            ],
            total_branches=1,
            branch_type="romance",
        )

        client.chat_completion_json.assert_called_once()
        prompt = client.chat_completion_json.call_args.kwargs["user_prompt"]
        self.assertIn("相邻正文已确认的恋爱路线角色", prompt)
        self.assertIn("软分支选择", content)

    def test_negative_named_romance_choice_keeps_named_character_negative_effect(self):
        result = soft_branch._fix_romance_effect_direction(
            {
                "choice_a": "我先顺着柳拂衣",
                "choice_b": "我不该靠近柳拂衣",
                "effects_a": {
                    "affection_liufuyi": 3,
                    "affection_musheng": -3,
                },
                "effects_b": {
                    "affection_liufuyi": -4,
                    "affection_musheng": 4,
                },
            },
            {
                "柳拂衣": "affection_liufuyi",
                "慕声": "affection_musheng",
            },
        )

        self.assertGreater(result["effects_a"]["affection_liufuyi"], 0)
        self.assertLess(result["effects_b"]["affection_liufuyi"], 0)

    def test_romance_pov_check_ignores_first_person_button_labels(self):
        payload = {
            "choice_a": "我和许定一起留下",
            "choice_b": "我握住许定的手",
            "content_a": "旁白把话说到一半，停下来等他回答。",
            "content_b": "旁白移开视线，把没有说完的话收回。",
            "bridge": "旁白最终停在接续点之前。",
        }

        self.assertEqual(
            "",
            soft_branch._romance_pov_drift_reason(payload, ["许定"]),
        )

    def test_negative_romance_semantics_allow_more_than_old_markers(self):
        payload = {
            "choice_a": "我离开柳拂衣",
            "effects_a": {"affection_liufuyi": -3},
            "choice_b": "我回应柳拂衣",
            "effects_b": {"affection_liufuyi": 3},
        }

        self.assertEqual(
            "",
            soft_branch._romance_effect_direction_issue(
                payload,
                {"柳拂衣": "affection_liufuyi"},
            ),
        )

    def test_targeted_repair_prompt_contains_choices_and_scene_context(self):
        prompt = soft_branch._build_targeted_repair_prompt(
            {
                "choice_a": "我先信柳拂衣",
                "choice_b": "我暂时避开柳拂衣",
                "effects_a": {"affection_liufuyi": -3},
                "effects_b": {"affection_liufuyi": 3},
            },
            reason="choice_a 明确倾向柳拂衣，但 effects 方向错误",
            fields=["choice_a", "effects_a"],
            variable_hint="affection_liufuyi=柳拂衣",
            scene_context="前置：柳拂衣在门口等候。接续：主角准备开口。",
        )

        self.assertIn("choice_a", prompt)
        self.assertIn("柳拂衣在门口等候", prompt)
        self.assertIn("接续：主角准备开口", prompt)

    @patch.object(soft_branch, "LLMClient")
    def test_romance_without_route_character_generates_flavor_branch(self, client_cls):
        client = MagicMock()
        client.chat_completion_json.return_value = {
            "choice_a": "我先把茶端稳",
            "content_a": "主角看着桌边晃动的茶盏，先把手腕放稳。",
            "effects_a": {},
            "choice_b": "我故意逗他笑",
            "content_b": "主角故意学了一句夸张的腔调，屋里的紧张松开了一点。",
            "effects_b": {},
            "bridge": "主角收起笑意，回到接续点前。",
        }
        client_cls.return_value = client
        context = SimpleNamespace(
            is_romance_oriented=True,
            worldview={
                "characters": [{"name": "主角", "role": "主角"}],
                "romance_targets": [{"name": "柳拂衣"}],
            },
            chapter_summaries=[],
        )
        chapter = "\n\n".join(["主角独自在厨房准备茶水。", "门外传来脚步声。"])
        metrics = {}

        content = soft_branch.inject_soft_branches(
            chapter_text=chapter,
            chapter_idx=0,
            provider="openai",
            novel_context=context,
            scenes_count=2,
            variable_defs=[
                {"key": "affection_liufuyi", "init": 50, "threshold": 65}
            ],
            total_branches=1,
            branch_type="romance",
            metrics=metrics,
        )

        self.assertIn("软分支选择", content)
        self.assertIn("branch_kind=flavor", content)
        self.assertNotIn("当前场景没有可用恋爱路线角色", content)
        self.assertEqual(1, metrics["theoretical_gap_count"])
        self.assertEqual(1, metrics["eligible_gap_count"])
        self.assertEqual(1, metrics["flavor_branch_count"])
        self.assertEqual(1, metrics["generated_branch_count"])
        self.assertEqual(0, metrics["skipped_no_route_target"])
        request = client.chat_completion_json.call_args
        self.assertIn("普通趣味分支", request.kwargs["system_prompt"])
        self.assertNotIn(
            "effects_a 与 effects_b **绝对不能为空对象**",
            request.kwargs["system_prompt"],
        )
        self.assertNotIn(
            "第五步：effects 叙事因果（必须填，不准为空）",
            request.kwargs["user_prompt"],
        )

    @patch.object(soft_branch, "LLMClient")
    def test_romance_direction_is_fixed_before_validation_retry(self, client_cls):
        client = MagicMock()
        client.chat_completion_json.return_value = {
            "choice_a": "我先信柳拂衣",
            "content_a": "我认真听完柳拂衣的话，决定把线索交给他。",
            "effects_a": {"affection_liufuyi": -4, "affection_musheng": 4},
            "choice_b": "我暂时保持沉默",
            "content_b": "我退开一步，暂时不回应任何人。",
            "effects_b": {"affection_liufuyi": 4, "affection_musheng": -4},
            "bridge": "我把两种心情都压回沉默里。",
        }
        client_cls.return_value = client
        context = SimpleNamespace(
            is_romance_oriented=True,
            worldview={
                "characters": [{"name": "小柳", "role": "主角"}],
                "romance_targets": [{"name": "柳拂衣"}, {"name": "慕声"}],
            },
            chapter_summaries=[],
        )
        metrics = {}
        chapter = "\n".join(f"小柳在柳拂衣和慕声之间犹豫第{i}次。" for i in range(1, 7))

        content = soft_branch.inject_soft_branches(
            chapter_text=chapter,
            chapter_idx=0,
            provider="openai",
            novel_context=context,
            scenes_count=2,
            variable_defs=[
                {"key": "affection_liufuyi", "init": 50, "threshold": 65, "description": "柳拂衣好感"},
                {"key": "affection_musheng", "init": 50, "threshold": 65, "description": "慕声好感"},
            ],
            total_branches=1,
            branch_type="romance",
            metrics=metrics,
        )

        self.assertEqual(1, client.chat_completion_json.call_count)
        self.assertIn("软分支选择", content)
        self.assertEqual(0, metrics["simple_llm_fallback"])

    def test_two_named_romance_choices_give_each_named_character_positive_effect(self):
        result = soft_branch._fix_romance_effect_direction(
            {
                "choice_a": "我先信柳拂衣",
                "choice_b": "我偏想慕声",
                "effects_a": {"affection_musheng": 4},
                "effects_b": {"affection_musheng": -4},
            },
            {
                "柳拂衣": "affection_liufuyi",
                "慕声": "affection_musheng",
            },
        )

        self.assertGreater(result["effects_a"]["affection_liufuyi"], 0)
        self.assertGreater(result["effects_b"]["affection_musheng"], 0)

    def test_single_newline_novel_paragraphs_are_split_into_requested_scenes(self):
        paragraphs = [
            f"　　这是第{i}段正文，包含足够的信息用于场景切分。"
            for i in range(1, 10)
        ]
        chapter = "\n".join(paragraphs)

        scenes = _split_scenes(chapter, scene_count=3)

        self.assertEqual(3, len(scenes))
        combined = "\n".join(scenes)
        for paragraph in paragraphs:
            self.assertIn(paragraph.strip(), combined)

    @patch.object(soft_branch, "LLMClient")
    def test_required_effect_schedule_is_enforced_in_saved_marker(self, client_cls):
        client = MagicMock()
        client.chat_completion_json.return_value = {
            "choice_a": "我选择直面恐惧",
            "content_a": "我压住迟疑，准备向前。" * 30,
            "effects_a": {"courage": 6},
            "choice_b": "我仍愿相信同伴",
            "content_b": "我想起同伴，决定再信一次。" * 30,
            "effects_b": {"courage": -6},
            "bridge": "我最终收住脚步。",
        }
        client_cls.return_value = client
        chapter = "\n".join(f"这是第{i}段正文。" for i in range(1, 7))

        content = soft_branch.inject_soft_branches(
            chapter_text=chapter,
            chapter_idx=0,
            provider="openai",
            scenes_count=2,
            variable_defs=[
                {"key": "courage", "init": 50, "threshold": 60},
                {"key": "trust", "init": 50, "threshold": 55},
            ],
            total_branches=1,
            required_effect_keys=["trust"],
        )

        self.assertRegex(content, r"'trust': [1-9]")

    @patch.object(soft_branch, "LLMClient")
    def test_recoverable_effect_issues_are_normalized_without_retry(self, client_cls):
        client = MagicMock()
        client.chat_completion_json.return_value = {
            "choice_a": "我决定直面眼前的人",
            "content_a": "我压住迟疑，准备把自己的决定说清楚。",
            "effects_a": {"courage": 99, "invented_axis": 4},
            "choice_b": "我暂时把话咽回去",
            "content_b": "我移开视线，把那句话重新压回心底。",
            "effects_b": {"courage": -99},
            "bridge": "我想起仍需保护的人，最终停在原地。",
        }
        client_cls.return_value = client
        metrics = {}
        chapter = "\n".join(f"这是第{i}段正文。" for i in range(1, 7))

        content = soft_branch.inject_soft_branches(
            chapter_text=chapter,
            chapter_idx=0,
            provider="openai",
            scenes_count=2,
            variable_defs=[{"key": "courage", "init": 50, "threshold": 70}],
            total_branches=1,
            metrics=metrics,
        )

        self.assertEqual(1, client.chat_completion_json.call_count)
        self.assertIn("'courage': 15", content)
        self.assertNotIn("invented_axis", content)
        self.assertEqual(1, metrics["normalized_success"])
        self.assertEqual(0, metrics["simple_llm_fallback"])

    @patch.object(soft_branch, "LLMClient")
    def test_romance_two_target_mirror_is_accepted_without_generic_axis_rejection(self, client_cls):
        client = MagicMock()
        client.chat_completion_json.return_value = {
            "choice_a": "我先追上苏勉",
            "content_a": "我叫住苏勉，把没有说完的话交给他。",
            "effects_a": {"affection_su_mian": 3, "affection_si_jiu": -3},
            "choice_b": "我留下陪四九",
            "content_b": "我停在四九身边，认真回应他的守候。",
            "effects_b": {"affection_su_mian": -3, "affection_si_jiu": 3},
            "bridge": "两个名字在心里各自落定，我终于承认自己必须作出选择。",
        }
        client_cls.return_value = client
        metrics = {}
        context = SimpleNamespace(
            is_romance_oriented=True,
            worldview={
                "characters": [{"name": "小柳", "role": "主角"}],
                "romance_targets": [{"name": "苏勉"}, {"name": "四九"}],
            },
            chapter_summaries=[],
        )
        chapter = "\n".join(
            f"小柳在苏勉和四九之间回想第{i}次相遇。" for i in range(1, 7)
        )

        content = soft_branch.inject_soft_branches(
            chapter_text=chapter,
            chapter_idx=0,
            provider="openai",
            novel_context=context,
            scenes_count=2,
            variable_defs=[
                {"key": "affection_su_mian", "init": 50, "threshold": 65, "description": "苏勉好感"},
                {"key": "affection_si_jiu", "init": 50, "threshold": 65, "description": "四九好感"},
            ],
            total_branches=1,
            branch_type="romance",
            required_effect_keys=["affection_su_mian"],
            metrics=metrics,
        )

        self.assertEqual(1, client.chat_completion_json.call_count)
        self.assertRegex(content, r"effects_a=\{[^}]*'affection_su_mian': [1-9]")
        self.assertRegex(content, r"effects_b=\{[^}]*'affection_si_jiu': [1-9]")
        self.assertEqual(0, metrics["simple_llm_fallback"])

    @patch.object(soft_branch, "LLMClient")
    def test_romance_gap_without_route_target_is_skipped_instead_of_forcing_affection(self, client_cls):
        client_cls.return_value = MagicMock()
        metrics = {}
        context = SimpleNamespace(
            is_romance_oriented=True,
            worldview={
                "characters": [{"name": "小柳", "role": "主角"}],
                "romance_targets": [{"name": "苏勉"}, {"name": "四九"}],
            },
            chapter_summaries=[],
        )
        chapter = "\n".join(
            f"小柳独自在旧宅检查第{i}只陶瓮。" for i in range(1, 7)
        )

        content = soft_branch.inject_soft_branches(
            chapter_text=chapter,
            chapter_idx=0,
            provider="openai",
            novel_context=context,
            scenes_count=2,
            variable_defs=[
                {"key": "affection_su_mian", "init": 50, "threshold": 65, "description": "苏勉好感"},
                {"key": "affection_si_jiu", "init": 50, "threshold": 65, "description": "四九好感"},
            ],
            total_branches=1,
            branch_type="romance",
            required_effect_keys=["affection_su_mian"],
            metrics=metrics,
        )

        client_cls.return_value.chat_completion_json.assert_not_called()
        self.assertNotIn("软分支选择", content)
        self.assertEqual(1, metrics["skipped_no_route_target"])

    @patch.object(soft_branch, "LLMClient")
    def test_romance_route_target_list_in_summary_is_not_treated_as_scene_presence(self, client_cls):
        client_cls.return_value = MagicMock()
        metrics = {}
        context = SimpleNamespace(
            is_romance_oriented=True,
            worldview={
                "characters": [{"name": "小柳", "role": "主角"}],
                "romance_targets": [{"name": "苏勉"}, {"name": "四九"}],
            },
            chapter_summaries=[],
        )
        chapter = "\n".join(
            f"小柳独自在旧宅检查第{i}只陶瓮。" for i in range(1, 7)
        )

        content = soft_branch.inject_soft_branches(
            chapter_text=chapter,
            chapter_idx=0,
            provider="openai",
            novel_context=context,
            current_chapter_summary="路线状态：苏勉未明示，四九未明示；本章由小柳独自调查。",
            scenes_count=2,
            variable_defs=[
                {"key": "affection_su_mian", "init": 50, "threshold": 65, "description": "苏勉好感"},
                {"key": "affection_si_jiu", "init": 50, "threshold": 65, "description": "四九好感"},
            ],
            total_branches=1,
            branch_type="romance",
            required_effect_keys=["affection_su_mian"],
            metrics=metrics,
        )

        client_cls.return_value.chat_completion_json.assert_not_called()
        self.assertNotIn("软分支选择", content)
        self.assertEqual(1, metrics["skipped_no_route_target"])

    @patch.object(soft_branch, "LLMClient")
    def test_romance_required_key_is_rescheduled_to_target_present_in_gap(self, client_cls):
        client = MagicMock()
        client.chat_completion_json.return_value = {
            "choice_a": "我留下陪四九",
            "content_a": "我停在四九身边，决定回应他的守候。",
            "effects_a": {"affection_si_jiu": 3},
            "choice_b": "我暂时避开四九",
            "content_b": "我没有接住四九的目光，把想说的话压了回去。",
            "effects_b": {"affection_si_jiu": -3},
            "bridge": "我想起彼此尚未说开的旧事，最终停住脚步。",
        }
        client_cls.return_value = client
        metrics = {}
        context = SimpleNamespace(
            is_romance_oriented=True,
            worldview={
                "characters": [{"name": "小柳", "role": "主角"}],
                "romance_targets": [{"name": "苏勉"}, {"name": "四九"}],
            },
            chapter_summaries=[],
        )
        chapter = "\n".join(
            f"小柳和四九在旧宅核对第{i}件往事。" for i in range(1, 7)
        )

        content = soft_branch.inject_soft_branches(
            chapter_text=chapter,
            chapter_idx=0,
            provider="openai",
            novel_context=context,
            scenes_count=2,
            variable_defs=[
                {"key": "affection_su_mian", "init": 50, "threshold": 65, "description": "苏勉好感"},
                {"key": "affection_si_jiu", "init": 50, "threshold": 65, "description": "四九好感"},
            ],
            total_branches=1,
            branch_type="romance",
            required_effect_keys=["affection_su_mian"],
            metrics=metrics,
        )

        prompt = client.chat_completion_json.call_args.kwargs["user_prompt"]
        self.assertIn("本次必须让变量 affection_si_jiu", prompt)
        self.assertIn("'affection_si_jiu': 3", content)
        self.assertEqual(1, metrics["rescheduled_effect_key"])

    @patch.object(soft_branch, "LLMClient")
    def test_retry_uses_targeted_field_patch_and_low_temperature(self, client_cls):
        client = MagicMock()
        client.chat_completion_json.side_effect = [
            {
                "choice_a": "我决定开口说明",
                "content_a": "我听见他说：“别回头。\n\n我没有回答。",
                "effects_a": {"courage": 3},
                "choice_b": "我暂时保持沉默",
                "content_b": "我把手收了回来。",
                "effects_b": {"courage": -3},
                "bridge": "风从身后追了上来。",
            },
            {"content_a": "我听见他说：“别回头。”"},
        ]
        client_cls.return_value = client
        chapter = "\n".join(f"这是第{i}段正文。" for i in range(1, 7))

        content = soft_branch.inject_soft_branches(
            chapter_text=chapter,
            chapter_idx=0,
            provider="openai",
            scenes_count=2,
            variable_defs=[{"key": "courage", "init": 50, "threshold": 70}],
            total_branches=1,
        )

        self.assertIn("我听见他说：“别回头。”", content)
        self.assertEqual(2, client.chat_completion_json.call_count)
        second = client.chat_completion_json.call_args_list[1].kwargs
        self.assertLessEqual(second["temperature"], 0.3)
        self.assertIn("只返回以下字段", second["user_prompt"])
        self.assertIn("content_a", second["user_prompt"])

    @patch.object(soft_branch, "structured_log")
    @patch.object(soft_branch, "LLMClient")
    def test_each_validation_failure_emits_category_and_attempt(
        self,
        client_cls,
        structured_log_mock,
    ):
        bad = {
            "choice_a": "我决定开口说明",
            "content_a": "我听见他说：“别回头。\n\n我没有回答。",
            "effects_a": {"courage": 3},
            "choice_b": "我暂时保持沉默",
            "content_b": "我把手收了回来。",
            "effects_b": {"courage": -3},
            "bridge": "风从身后追了上来。",
        }
        fallback = {
            "choice_a": "我决定开口说明",
            "content_a": "我把事情想清楚，准备开口。",
            "effects_a": {"courage": 3},
            "choice_b": "我暂时保持沉默",
            "content_b": "我把手收回来，暂时没有说话。",
            "effects_b": {"courage": -3},
            "bridge": "我想起仍需保护的人，最终停住脚步。",
        }
        client = MagicMock()
        client.chat_completion_json.side_effect = [
            bad,
            {"content_a": bad["content_a"]},
            {"content_a": bad["content_a"]},
            {},
        ]
        client_cls.return_value = client
        metrics = {}
        chapter = "\n".join(f"这是第{i}段正文。" for i in range(1, 7))

        content = soft_branch.inject_soft_branches(
            chapter_text=chapter,
            chapter_idx=28,
            provider="openai",
            scenes_count=2,
            variable_defs=[{"key": "courage", "init": 50, "threshold": 70}],
            total_branches=1,
            metrics=metrics,
        )

        failures = [
            call.kwargs
            for call in structured_log_mock.call_args_list
            if len(call.args) >= 2 and call.args[1] == "soft_branch_validation_failed"
        ]
        self.assertEqual([1, 2, 3], [item["attempt"] for item in failures])
        self.assertEqual({"prose_delimiter"}, {item["category"] for item in failures})
        self.assertTrue(all(item["chapter"] == 29 for item in failures))
        self.assertEqual(3, metrics["validation_failures"])
        self.assertEqual(3, metrics["validation_failures_by_category"]["prose_delimiter"])
        self.assertEqual(4, client.chat_completion_json.call_count)
        self.assertEqual(1, metrics["simple_llm_fallback"])
        self.assertEqual(1, metrics["validation_dropped"])
        self.assertEqual(1, metrics["deleted_branch_count"])
        self.assertNotIn("choice_a", content)

    @patch.object(soft_branch, "LLMClient")
    def test_modular_choices_also_use_shared_distinct_text_history(self, client_cls):
        client = MagicMock()
        client.chat_completion_json.return_value = {
            "choice_a": "继续躲起来",
            "content_a": "你查看了现场留下的痕迹。",
            "effects_a": {"courage": 3},
            "choice_b": "继续躲起来",
            "content_b": "你询问了知情人。",
            "effects_b": {"courage": -3},
        }
        client_cls.return_value = client
        history = ["我想把自己藏起来"]
        chapter = "\n".join(f"这是第{i}段正文。" for i in range(1, 8))

        content = soft_branch.inject_soft_branches(
            chapter_text=chapter,
            chapter_idx=0,
            provider="openai",
            scenes_count=2,
            variable_defs=[{"key": "courage", "init": 50, "threshold": 70}],
            total_branches=1,
            branch_type="modular",
            choice_text_history=history,
        )

        self.assertTrue(
            any(
                behavior in content
                for behavior in ("复问", "现场", "旧案", "跟踪", "物证", "走访", "口供")
            )
        )
        self.assertNotEqual(content.count("继续躲起来"), 2)

    @patch.object(soft_branch, "LLMClient")
    def test_modular_prompt_fixes_player_identity_and_hides_future_summary(self, client_cls):
        client = MagicMock()
        client.chat_completion_json.return_value = {
            "choice_a": "复问证人证词",
            "content_a": "我重新核对证词，只确认了现场已经公开的脚印。",
            "effects_a": {},
            "choice_b": "回到现场复验",
            "content_b": "我回到现场，只检查眼前可见的门窗痕迹。",
            "effects_b": {},
        }
        client_cls.return_value = client
        context = SimpleNamespace(
            worldview={"characters": [{"name": "李莲花", "role": "主角"}]},
            char_cards="李莲花：莲花楼楼主。",
            chapter_summaries=[],
        )
        chapter = "\n".join(
            f"李莲花在客栈检查第{i}处已经发现的痕迹。" for i in range(1, 8)
        )

        soft_branch.inject_soft_branches(
            chapter_text=chapter,
            chapter_idx=0,
            provider="openai",
            novel_context=context,
            scenes_count=2,
            variable_defs=[],
            branch_type="modular",
            current_chapter_summary="未来才揭晓：双层窗纸与萤火虫构成机关真相。",
        )

        kwargs = client.chat_completion_json.call_args.kwargs
        prompt = kwargs["system_prompt"] + kwargs["user_prompt"]
        self.assertIn("玩家第一人称始终固定为主角李莲花", prompt)
        self.assertIn("不得提前揭露", prompt)
        self.assertNotIn("双层窗纸与萤火虫构成机关真相", prompt)

    @patch.object(soft_branch, "LLMClient")
    def test_modular_prompt_lists_downstream_actions_that_must_not_be_completed(self, client_cls):
        client = MagicMock()
        client.chat_completion_json.return_value = {
            "choice_a": "比对窗框灰尘",
            "content_a": "我只记录眼前已经可见的灰尘分布。",
            "effects_a": {},
            "clue_flags_a": {},
            "choice_b": "复问门房时辰",
            "content_b": "我只核对门房已经说过的时辰。",
            "effects_b": {},
            "clue_flags_b": {},
        }
        client_cls.return_value = client
        context = SimpleNamespace(
            worldview={"characters": [{"name": "李莲花", "role": "主角"}]},
            chapter_summaries=[],
        )
        chapter = (
            "李莲花来到偏屋窗下，只看见窗框上的灰尘。\n\n"
            "他暂时没有触碰窗锁。\n\n"
            "李莲花随后拆开窗锁，才从夹层里发现翡翠断齿。\n\n"
            "方多病接过断齿仔细查看。"
        )

        soft_branch.inject_soft_branches(
            chapter_text=chapter,
            chapter_idx=0,
            provider="openai",
            novel_context=context,
            scenes_count=2,
            variable_defs=[],
            branch_type="modular",
        )

        prompt = client.chat_completion_json.call_args_list[0].kwargs["user_prompt"]
        self.assertIn("下游禁止提前完成清单", prompt)
        self.assertIn("拆开窗锁", prompt)

    @patch.object(soft_branch, "LLMClient")
    def test_modular_skips_gap_when_protagonist_is_not_in_either_scene(self, client_cls):
        client_cls.return_value = MagicMock()
        context = SimpleNamespace(
            worldview={"characters": [{"name": "李莲花", "role": "主角"}]},
            char_cards="李莲花：莲花楼楼主。",
            chapter_summaries=[],
        )
        chapter = "\n".join(
            f"方多病独自在山庄追问第{i}名证人。" for i in range(1, 8)
        )

        content = soft_branch.inject_soft_branches(
            chapter_text=chapter,
            chapter_idx=0,
            provider="openai",
            novel_context=context,
            scenes_count=2,
            variable_defs=[],
            branch_type="modular",
        )

        client_cls.return_value.chat_completion_json.assert_not_called()
        self.assertNotIn("软分支选择", content)

    @patch.object(soft_branch, "LLMClient")
    def test_modular_checks_bounded_downstream_window_for_delayed_protagonist(
        self, client_cls
    ):
        client = MagicMock()
        client.chat_completion_json.return_value = {
            "choice_a": "复查门边灰尘",
            "content_a": "我只记录门边已经可见的灰尘分布。",
            "effects_a": {},
            "clue_flags_a": {},
            "choice_b": "核对走廊灯光",
            "content_b": "我只核对走廊里已经出现的灯光变化。",
            "effects_b": {},
            "clue_flags_b": {},
        }
        client_cls.return_value = client
        context = SimpleNamespace(
            worldview={"characters": [{"name": "李莲花", "role": "主角"}]},
            chapter_summaries=[],
        )
        chapter = "\n\n".join(
            [
                "偏屋门口只剩一盏昏灯。",
                "风从廊下穿过去，灰尘贴在门槛边。",
                "李莲花从转角走来，没有惊动屋里的人。",
                "他停在门前，先看了一眼锁扣。",
                "门内没有声音。",
                "走廊尽头的灯又灭了一盏。",
            ]
        )

        content = soft_branch.inject_soft_branches(
            chapter_text=chapter,
            chapter_idx=0,
            provider="openai",
            novel_context=context,
            scenes_count=2,
            variable_defs=[],
            branch_type="modular",
        )

        client.chat_completion_json.assert_called_once()
        self.assertIn("软分支选择", content)

    @patch.object(soft_branch, "LLMClient")
    def test_modular_offscreen_gap_can_generate_one_safe_flavor_branch(self, client_cls):
        client = MagicMock()
        client.chat_completion_json.return_value = {
            "choice_a": "记下广播时间",
            "content_a": "阿木不在现场，玩家只整理已经听见的广播时间。",
            "effects_a": {},
            "clue_flags_a": {},
            "choice_b": "留意窗外灯色",
            "content_b": "阿木不在现场，玩家只观察已经出现的灯光变化。",
            "effects_b": {},
            "clue_flags_b": {},
        }
        client_cls.return_value = client
        context = SimpleNamespace(
            worldview={
                "characters": [{"name": "阿木", "role": "主角"}],
                "narrative_pov_contract": {
                    "perspective": "third_person_limited",
                    "protagonist_name": "阿木",
                },
            },
            narrative_pov_contract={
                "perspective": "third_person_limited",
                "protagonist_name": "阿木",
            },
            chapter_summaries=[],
        )
        chapter = "\n\n".join(
            [
                "古河在空教室里整理已经公开的试卷。",
                "窗外的广播重复播报停电时间。",
                "走廊没有人经过。",
                "远处的灯光又暗了一格。",
                "古河把记录收进课本。",
                "教室重新安静下来。",
            ]
        )
        metrics = {}

        content = soft_branch.inject_soft_branches(
            chapter_text=chapter,
            chapter_idx=0,
            provider="openai",
            novel_context=context,
            scenes_count=2,
            variable_defs=[],
            branch_type="modular",
            metrics=metrics,
        )

        client.chat_completion_json.assert_called_once()
        self.assertIn("branch_kind=flavor", content)
        self.assertEqual(1, metrics["flavor_branch_count"])

    def test_modular_presence_evidence_distinguishes_action_mention_and_memory(self):
        worldview = {
            "characters": [{"name": "阿木", "role": "主角"}],
        }
        active, _ = soft_branch._modular_protagonist_presence_status(
            upper="阿木推开门，走进了值班室。",
            lower="桌上的灯光忽明忽暗。",
            protagonist_names=["阿木"],
            worldview=worldview,
            chapter_text="阿木推开门，走进了值班室。",
        )
        mentioned, _ = soft_branch._modular_protagonist_presence_status(
            upper="墙上的值班记录只提到阿木的名字。",
            lower="记录页没有新的笔迹。",
            protagonist_names=["阿木"],
            worldview=worldview,
            chapter_text="墙上的值班记录只提到阿木的名字。",
        )
        memory, _ = soft_branch._modular_protagonist_presence_status(
            upper="旧照片里，阿木站在已经拆掉的车站前。",
            lower="照片边缘已经发黄。",
            protagonist_names=["阿木"],
            worldview=worldview,
            chapter_text="旧照片里，阿木站在已经拆掉的车站前。",
        )

        self.assertEqual("active", active)
        self.assertEqual("mentioned", mentioned)
        self.assertEqual("memory", memory)

    def test_modular_presence_action_wins_over_incidental_record_word(self):
        status, _ = soft_branch._modular_protagonist_presence_status(
            upper="阿木检查记录，随后把纸页收进文件夹。",
            lower="门外的灯光闪了一下。",
            protagonist_names=["阿木"],
            worldview={"characters": [{"name": "阿木", "role": "主角"}]},
            chapter_text="阿木检查记录，随后把纸页收进文件夹。",
        )

        self.assertEqual("active", status)

    def test_modular_presence_does_not_treat_negated_departure_as_offscreen(self):
        status, _ = soft_branch._modular_protagonist_presence_status(
            upper="阿木没有离开现场，仍然站在门边。",
            lower="门外的灯光闪了一下。",
            protagonist_names=["阿木"],
            worldview={"characters": [{"name": "阿木", "role": "主角"}]},
            chapter_text="阿木没有离开现场，仍然站在门边。",
        )

        self.assertEqual("active", status)

    def test_modular_presence_treats_explicit_not_arrived_as_offscreen(self):
        status, _ = soft_branch._modular_protagonist_presence_status(
            upper="阿木没有来，值班室里只有古河。",
            lower="桌上的灯光忽明忽暗。",
            protagonist_names=["阿木"],
            worldview={"characters": [{"name": "阿木", "role": "主角"}]},
            chapter_text="阿木没有来，值班室里只有古河。",
        )

        self.assertEqual("offscreen", status)

    @patch.object(soft_branch, "LLMClient")
    def test_modular_presence_uses_semantic_judgment_for_ambiguous_named_reference(
        self, client_cls
    ):
        client = MagicMock()
        client.chat_completion_json.side_effect = [
            {
                "status": "active",
                "confidence": 0.92,
                "evidence": "阿木在门边停留，属于当前场景中的实际存在。",
                "source_span": "阿木在门边。",
            },
            {
                "choice_a": "复查门边灰尘",
                "content_a": "我只记录门边已经可见的灰尘分布。",
                "effects_a": {},
                "clue_flags_a": {},
                "choice_b": "核对走廊灯光",
                "content_b": "我只核对走廊里已经出现的灯光变化。",
                "effects_b": {},
                "clue_flags_b": {},
            },
        ]
        client_cls.return_value = client
        context = SimpleNamespace(
            worldview={"characters": [{"name": "阿木", "role": "主角"}]},
            chapter_summaries=[],
        )
        chapter = "\n\n".join(
            [
                "阿木在门边。窗外的灯光忽明忽暗。",
                "走廊尽头传来一声轻响。",
                "桌面上的灰尘没有被碰动。",
                "值班室重新安静下来。",
            ]
        )

        content = soft_branch.inject_soft_branches(
            chapter_text=chapter,
            chapter_idx=0,
            provider="openai",
            novel_context=context,
            scenes_count=2,
            variable_defs=[],
            branch_type="modular",
        )

        self.assertIn("软分支选择", content)
        self.assertEqual(2, client.chat_completion_json.call_count)
        semantic_prompt = client.chat_completion_json.call_args_list[0].kwargs[
            "user_prompt"
        ]
        self.assertIn("active", semantic_prompt)
        self.assertIn("阿木在门边", semantic_prompt)

    def test_modular_presence_semantic_failure_degrades_to_unknown(self):
        client = MagicMock()
        client.chat_completion_json.side_effect = RuntimeError("gateway timeout")
        status, reason = soft_branch._modular_protagonist_presence_status(
            upper="阿木停在门边，具体动作无法从当前片段确定。",
            lower="走廊尽头传来一声轻响。",
            protagonist_names=["阿木"],
            worldview={"characters": [{"name": "阿木", "role": "主角"}]},
            chapter_text="阿木停在门边，具体动作无法从当前片段确定。",
            semantic_client=client,
        )

        self.assertEqual("unknown", status)
        self.assertEqual("semantic_error", reason)

    def test_modular_presence_does_not_call_semantic_for_explicit_memory(self):
        client = MagicMock()
        status, reason = soft_branch._modular_protagonist_presence_status(
            upper="旧照片里，阿木站在已经拆掉的车站前。",
            lower="照片边缘已经发黄。",
            protagonist_names=["阿木"],
            worldview={"characters": [{"name": "阿木", "role": "主角"}]},
            chapter_text="旧照片里，阿木站在已经拆掉的车站前。",
            semantic_client=client,
        )

        self.assertEqual("memory", status)
        self.assertEqual("memory_or_record_reference", reason)
        client.chat_completion_json.assert_not_called()

    def test_modular_presence_semantic_failure_for_ambiguous_name_is_safe(self):
        client = MagicMock()
        client.chat_completion_json.side_effect = RuntimeError("gateway timeout")
        status, reason = soft_branch._modular_protagonist_presence_status(
            upper="阿木在门边。",
            lower="走廊尽头传来一声轻响。",
            protagonist_names=["阿木"],
            worldview={"characters": [{"name": "阿木", "role": "主角"}]},
            chapter_text="阿木在门边。",
            semantic_client=client,
        )

        self.assertEqual("unknown", status)
        self.assertEqual("semantic_error", reason)

    def test_modular_presence_marks_late_active_entry_as_delayed(self):
        worldview = {
            "characters": [{"name": "阿木", "role": "主角"}],
        }
        status, reason = soft_branch._modular_protagonist_presence_status(
            upper="走廊尽头只剩一盏灯。",
            lower="\n\n".join(
                [
                    "风从门缝里吹进来。",
                    "灰尘落在台阶上。",
                    "远处传来一声轻响。",
                    "阿木随后推门进入值班室。",
                ]
            ),
            protagonist_names=["阿木"],
            worldview=worldview,
            chapter_text="阿木随后推门进入值班室。",
        )

        self.assertEqual("delayed", status)
        self.assertEqual("downstream_late_entry", reason)

    @patch.object(soft_branch, "LLMClient")
    def test_modular_accepts_first_person_narrator_as_named_protagonist(self, client_cls):
        client = MagicMock()
        client.chat_completion_json.return_value = {
            "choice_a": "复查末班车座位",
            "content_a": "我沿着车厢重新核对座位，只记录眼前已经出现的泥水痕迹。",
            "effects_a": {},
            "clue_flags_a": {},
            "choice_b": "询问车队值班员",
            "content_b": "我向值班员核对发车时间，只确认此前已经公开的排班记录。",
            "effects_b": {},
            "clue_flags_b": {},
        }
        client_cls.return_value = client
        context = SimpleNamespace(
            worldview={
                "style_notes": "全文采用第一人称口语化叙述。",
                "characters": [
                    {
                        "name": "李耀",
                        "aliases": ["小李"],
                        "role": "主角",
                    }
                ],
            },
            chapter_summaries=[],
        )
        chapter = "\n".join(
            f"我开着末班车检查第{i}排座位留下的泥水痕迹。"
            for i in range(1, 9)
        )

        content = soft_branch.inject_soft_branches(
            chapter_text=chapter,
            chapter_idx=0,
            provider="openai",
            novel_context=context,
            scenes_count=2,
            variable_defs=[],
            branch_type="modular",
        )

        client.chat_completion_json.assert_called()
        self.assertIn("软分支选择", content)

    @patch.object(soft_branch, "LLMClient")
    def test_modular_accepts_first_person_with_third_person_dialogue_note(
        self, client_cls
    ):
        """A third-person dialogue note must not disable protagonist detection."""
        client = MagicMock()
        client.chat_completion_json.return_value = {
            "choice_a": "复查末班车座位",
            "content_a": "我沿着车厢重新核对座位，只记录眼前已经出现的泥水痕迹。",
            "effects_a": {},
            "choice_b": "询问车队值班员",
            "content_b": "我向值班员核对发车时间，只确认此前已经公开的排班记录。",
            "effects_b": {},
        }
        client_cls.return_value = client
        context = SimpleNamespace(
            worldview={
                "style_notes": "全文采用第一人称叙述，角色对白偶尔使用第三人称转述。",
                "characters": [{"name": "李耀", "role": "主角"}],
            },
            chapter_summaries=[],
        )
        chapter = "\n".join(
            f"我开着末班车检查第{i}排座位留下的泥水痕迹。"
            for i in range(1, 9)
        )

        content = soft_branch.inject_soft_branches(
            chapter_text=chapter,
            chapter_idx=0,
            provider="openai",
            novel_context=context,
            scenes_count=2,
            variable_defs=[],
            branch_type="modular",
        )

        client.chat_completion_json.assert_called()
        self.assertIn("软分支选择", content)

    @patch.object(soft_branch, "LLMClient")
    def test_modular_accepts主人公_role_for_first_person_protagonist_detection(
        self, client_cls
    ):
        """The common ``主人公`` role label must not suppress modular branches."""
        client = MagicMock()
        client.chat_completion_json.return_value = {
            "choice_a": "复查末班车座位",
            "content_a": "我沿着车厢重新核对座位，只记录眼前已经出现的泥水痕迹。",
            "effects_a": {},
            "choice_b": "询问车队值班员",
            "content_b": "我向值班员核对发车时间，只确认此前已经公开的排班记录。",
            "effects_b": {},
        }
        client_cls.return_value = client
        context = SimpleNamespace(
            worldview={
                "style_notes": "全文采用第一人称叙述。",
                "characters": [{"name": "李耀", "role": "主人公"}],
            },
            chapter_summaries=[],
        )
        self.assertEqual(["李耀"], soft_branch._protagonist_names_from_worldview(context.worldview))
        chapter = "\n".join(
            f"我开着末班车检查第{i}排座位留下的泥水痕迹。"
            for i in range(1, 9)
        )

        content = soft_branch.inject_soft_branches(
            chapter_text=chapter,
            chapter_idx=0,
            provider="openai",
            novel_context=context,
            scenes_count=2,
            variable_defs=[],
            branch_type="modular",
        )

        client.chat_completion_json.assert_called()
        self.assertIn("软分支选择", content)

    @patch.object(soft_branch, "LLMClient")
    def test_modular_does_not_treat_side_character_dialogue_as_protagonist_pov(self, client_cls):
        client_cls.return_value = MagicMock()
        context = SimpleNamespace(
            worldview={
                "style_notes": "全文采用第一人称叙述，但本场仅有配角对白。",
                "characters": [{"name": "李莲花", "role": "主角"}],
            },
            chapter_summaries=[],
        )
        chapter = "\n".join(
            f"方多病独自在山庄查第{i}处线索，说：“我会继续追查。”"
            for i in range(1, 9)
        )

        content = soft_branch.inject_soft_branches(
            chapter_text=chapter,
            chapter_idx=0,
            provider="openai",
            novel_context=context,
            scenes_count=2,
            variable_defs=[],
            branch_type="modular",
        )

        client_cls.return_value.chat_completion_json.assert_not_called()
        self.assertNotIn("软分支选择", content)

    @patch.object(soft_branch, "LLMClient")
    def test_modular_recomputes_omitted_gap_from_older_protagonist_rule(self, client_cls):
        client = MagicMock()
        client.chat_completion_json.return_value = {
            "choice_a": "复查车厢脚印",
            "content_a": "我重新核对车厢脚印，只记录已经可见的泥水方向。",
            "effects_a": {},
            "clue_flags_a": {},
            "choice_b": "核对当夜排班",
            "content_b": "我核对值班表，只确认已经公开的发车时刻。",
            "effects_b": {},
            "clue_flags_b": {},
        }
        client_cls.return_value = client
        context = SimpleNamespace(
            worldview={
                "style_notes": "全文采用第一人称口语化叙述。",
                "characters": [{"name": "李耀", "role": "主角"}],
            },
            chapter_summaries=[],
        )
        chapter = "\n".join(
            f"我检查第{i}排座位附近留下的泥水。" for i in range(1, 9)
        )
        old_input = {
            "chapter_sha256": soft_branch.content_sha256(chapter),
            "scene_count": 2,
            "branch_type": "modular",
            "output_language": None,
            "variable_defs": [],
            "required_effect_keys": [],
            "current_chapter_summary": "",
        }
        old_state = soft_branch.initialize_soft_branch_gap_checkpoint(
            None,
            gap_count=1,
            input_payload=old_input,
        )
        soft_branch.record_soft_branch_gap(
            old_state,
            gap_index=0,
            marker="",
            omitted=True,
        )

        content = soft_branch.inject_soft_branches(
            chapter_text=chapter,
            chapter_idx=0,
            provider="openai",
            novel_context=context,
            scenes_count=2,
            variable_defs=[],
            branch_type="modular",
            resume_state=old_state,
        )

        client.chat_completion_json.assert_called()
        self.assertIn("软分支选择", content)

    @patch.object(soft_branch, "LLMClient")
    def test_modular_duplicate_buttons_use_investigation_actions(self, client_cls):
        client = MagicMock()
        client.chat_completion_json.return_value = {
            "choice_a": "我决定主动开口",
            "content_a": "我检查了现场痕迹。",
            "effects_a": {},
            "choice_b": "我决定主动开口",
            "content_b": "我询问了在场证人。",
            "effects_b": {},
        }
        client_cls.return_value = client
        context = SimpleNamespace(
            worldview={"characters": [{"name": "李莲花", "role": "主角"}]},
            chapter_summaries=[],
        )
        chapter = "\n".join(f"李莲花检查第{i}处线索。" for i in range(1, 8))

        content = soft_branch.inject_soft_branches(
            chapter_text=chapter,
            chapter_idx=0,
            provider="openai",
            novel_context=context,
            scenes_count=2,
            variable_defs=[],
            branch_type="modular",
            choice_text_history=["我决定主动开口"],
        )

        self.assertTrue(
            any(
                action in content
                for action in ("复问证人", "回到现场", "查阅旧案", "跟踪可疑", "核对物证")
            ),
            content,
        )
        self.assertNotIn("我向前一步说清楚", content)

    def test_modular_button_template_density_rewrites_view_or_ask_variants(self):
        recent = ["查看现场", "查看门窗", "询问证人", "询问守卫", "查看旧案"]

        rewritten = soft_branch.ensure_distinct_modular_choice_text("查看线索", recent)

        self.assertNotIn("查看", rewritten)
        self.assertNotIn("询问", rewritten)

    @patch.object(soft_branch, "LLMClient")
    def test_modular_transition_repairs_once_then_drops_conflicting_group(self, client_cls):
        conflict = {
            "choice_a": "检查窗锁",
            "content_a": "我拆开窗锁，从夹层里取出翡翠断齿。",
            "effects_a": {},
            "clue_flags_a": {
                "broken_tooth": {
                    "source_type": "decisive_clue",
                    "source_excerpt": "翡翠断齿",
                    "category": "physical_evidence",
                }
            },
            "choice_b": "复核窗框",
            "content_b": "我也拆开窗锁，确认里面藏着翡翠断齿。",
            "effects_b": {},
            "clue_flags_b": {},
        }
        client = MagicMock()
        client.chat_completion_json.side_effect = [conflict, conflict]
        client_cls.return_value = client
        metrics = {}
        context = SimpleNamespace(
            worldview={"characters": [{"name": "李莲花", "role": "主角"}]},
            chapter_summaries=[],
        )
        chapter = (
            "李莲花来到偏屋窗下，只看见窗框上的一道划痕。\n\n"
            "他暂时没有碰窗锁，只记下划痕的位置。\n\n"
            "众人随后拆开窗锁，才从夹层里发现那枚翡翠断齿。\n\n"
            "李莲花把断齿交给方多病，继续追查来历。"
        )

        content = soft_branch.inject_soft_branches(
            chapter_text=chapter,
            chapter_idx=0,
            provider="openai",
            novel_context=context,
            scenes_count=2,
            variable_defs=[],
            branch_type="modular",
            metrics=metrics,
        )

        self.assertEqual(2, client.chat_completion_json.call_count)
        self.assertNotIn("软分支选择", content)
        self.assertEqual(1, metrics["transition_conflicts"])
        self.assertEqual(1, metrics["transition_repairs"])
        self.assertEqual(1, metrics["transition_dropped"])


if __name__ == "__main__":
    unittest.main()
