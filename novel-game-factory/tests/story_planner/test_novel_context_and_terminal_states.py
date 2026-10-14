import unittest
from unittest.mock import MagicMock, patch

from services.story_planner.core import novel_reader


class NovelContextTests(unittest.TestCase):
    @patch.object(novel_reader, "LLMClient")
    def test_chapter_summary_prompt_uses_only_relevant_characters(self, client_cls):
        client = MagicMock()
        client.chat_completion.return_value = "结构化摘要"
        client_cls.return_value = client
        worldview = {
            "characters": [
                {"name": "李莲花", "role": "主角"},
                {"name": "方多病", "role": "重要配角"},
                {"name": "笛飞声", "role": "重要配角"},
                {"name": "无关路人", "role": "配角"},
            ],
            "world_rules": [],
            "persona_axes": [],
            "ending_dimensions": [],
        }

        novel_reader._summarize_one_chapter(
            "李莲花与方多病进入山庄查案。",
            0,
            worldview,
            client,
        )

        prompt = client.chat_completion.call_args.kwargs["user_prompt"]
        kwargs = client.chat_completion.call_args.kwargs
        self.assertIn("李莲花", prompt)
        self.assertIn("方多病", prompt)
        self.assertNotIn("笛飞声", prompt)
        self.assertNotIn("无关路人", prompt)
        self.assertIn("目标 350-500 字", prompt)
        self.assertLessEqual(kwargs["max_tokens"], 1100)

    @patch.object(novel_reader, "LLMClient")
    def test_segmented_worldview_retries_invalid_json_instead_of_skipping_segment(
        self,
        client_cls,
    ):
        client = MagicMock()
        client.chat_completion_json.side_effect = [
            None,
            {"title": "第一段"},
            {"title": "第二段"},
            {"title": "合并结果", "characters": []},
        ]
        client_cls.return_value = client
        full_text = "甲" * (novel_reader._WORLDVIEW_SEG_SIZE + 10)

        result = novel_reader._analyze_worldview(full_text, provider="openai")

        self.assertEqual("合并结果", result["title"])
        self.assertEqual(4, client.chat_completion_json.call_count)

    def test_narrative_reality_contract_is_inferred_once_from_worldview(self):
        worldview = {
            "genre": "现代重生校园纯爱",
            "world_rules": "现实中的爱人已经去世，主角在治疗中重构高中时期。",
            "narrative_arc": "故事主要发生在治疗重构出的高中时间线。",
        }

        contract = novel_reader.infer_narrative_reality_contract(worldview)

        self.assertEqual("reconstructed_world", contract["primary_reality_domain"])
        self.assertEqual("therapy_reconstruction", contract["primary_reconstruction_type"])
        self.assertIn("dream", contract["compatible_reconstruction_types"])

    def test_legacy_worldview_fields_are_migrated_to_current_schema(self):
        legacy = {
            "is_romance_oriented": True,
            "protagonist": {
                "id": "hero",
                "name": "沈月",
                "role": "主角",
                "fatal_flaw": "害怕失去同伴",
                "persona_axes": {
                    "affection_guchen": "对顾沉的信任",
                    "affection_linxue": "对林雪的依恋",
                    "clarity": "面对谜团时保持清醒",
                },
            },
            "characters": [{"id": "hero", "name": "沈月", "role": "主角"}],
            "secrets": [
                {"name": "秘密一", "content": "内容一"},
                {"name": "秘密二", "content": "内容二"},
                {"name": "秘密三", "content": "内容三"},
            ],
            "ending_dimensions": ["主角生死", "同伴存亡"],
            "factions": [{"name": "山门"}],
            "hidden_threads": [{"name": "旧案"}],
        }

        migrated = novel_reader.normalize_legacy_worldview_schema(legacy)

        protagonist = migrated["characters"][0]
        self.assertEqual("害怕失去同伴", protagonist["fatal_flaw"])
        self.assertEqual(3, len(protagonist["secrets"]))
        self.assertEqual(
            ["affection_guchen", "affection_linxue", "clarity"],
            [axis["key"] for axis in migrated["persona_axes"]],
        )
        self.assertEqual([], novel_reader.worldview_schema_issues(migrated))

    def test_character_aliases_are_merged_into_one_canonical_card(self):
        worldview = {
            "characters": [
                {
                    "name": "李莲花",
                    "aliases": ["李相夷"],
                    "role": "主角",
                    "fatal_flaw": "碧茶之毒",
                    "secrets": ["曾是四顾门门主"],
                },
                {
                    "name": "李相夷",
                    "role": "重要配角",
                    "background": "四顾门门主",
                    "secrets": ["化名李莲花"],
                },
            ],
            "persona_axes": [{"key": "clarity"}, {"key": "mercy"}],
            "ending_dimensions": ["主角生死", "旧案真相"],
            "factions": [{"name": "四顾门"}],
            "hidden_threads": [{"name": "身份旧事"}],
        }

        normalized = novel_reader.normalize_legacy_worldview_schema(worldview)

        self.assertEqual(1, len(normalized["characters"]))
        character = normalized["characters"][0]
        self.assertEqual("李莲花", character["name"])
        self.assertIn("李相夷", character["aliases"])
        self.assertEqual("四顾门门主", character["background"])
        self.assertEqual(2, len(character["secrets"]))

    def test_alias_named_as_a_separate_relation_is_not_merged(self):
        worldview = {
            "characters": [
                {
                    "name": "刘如京",
                    "aliases": ["王忠"],
                    "role": "重要配角",
                    "fatal_flaw": "执着旧门",
                    "secrets": ["秘密一", "秘密二", "秘密三"],
                    "key_relations": ["与王忠的关系：旧兄弟"],
                },
                {
                    "name": "王忠",
                    "role": "重要配角",
                    "background": "刘如京的旧兄弟",
                },
            ],
            "persona_axes": [{"key": "clarity"}, {"key": "mercy"}],
            "ending_dimensions": ["主角生死", "旧案真相"],
            "factions": [{"name": "四顾门"}],
            "hidden_threads": [{"name": "身份旧事"}],
        }

        normalized = novel_reader.normalize_legacy_worldview_schema(worldview)

        self.assertEqual(2, len(normalized["characters"]))
        liu = next(item for item in normalized["characters"] if item["name"] == "刘如京")
        self.assertNotIn("王忠", liu["aliases"])
        self.assertTrue(normalized["character_alias_conflicts"])
        self.assertTrue(
            any("王忠" in issue for issue in novel_reader.worldview_schema_issues(normalized))
        )

    def test_shared_alias_cards_are_not_merged_or_randomly_mapped(self):
        worldview = {
            "characters": [
                {
                    "name": "甲角色",
                    "aliases": ["小明"],
                    "role": "重要配角",
                },
                {
                    "name": "乙角色",
                    "aliases": ["小明"],
                    "role": "重要配角",
                },
            ],
            "persona_axes": [{"key": "affection_jia"}],
            "ending_dimensions": ["关系选择", "身份真相"],
        }

        normalized = novel_reader.normalize_legacy_worldview_schema(worldview)

        self.assertEqual(2, len(normalized["characters"]))
        self.assertNotIn("小明", normalized["character_alias_map"])
        self.assertTrue(
            any(item.get("alias") == "小明" for item in normalized["character_alias_conflicts"])
        )
        shared_conflict = next(
            item
            for item in normalized["character_alias_conflicts"]
            if item.get("alias") == "小明"
        )
        self.assertEqual("warning", shared_conflict.get("severity"))
        self.assertFalse(any("小明" in issue for issue in novel_reader.worldview_schema_issues(normalized)))

    def test_shared_alias_conflict_is_warning_not_schema_blocker(self):
        """An ambiguous generic称呼 disables matching but must not fail worldview validation."""
        worldview = {
            "characters": [
                {"name": "甲角色", "aliases": ["老师"], "role": "重要配角", "secrets": ["甲的秘密"]},
                {"name": "乙角色", "aliases": ["老师"], "role": "重要配角", "secrets": ["乙的秘密"]},
                {"name": "主角", "role": "主角", "fatal_flaw": "害怕失去选择", "secrets": ["主角的秘密"]},
            ],
            "persona_axes": [
                {"key": "courage"},
                {"key": "self_worth"},
            ],
            "ending_dimensions": ["选择", "关系"],
            "secrets": [],
            "factions": ["校园"],
            "hidden_threads": ["旧信"],
            "character_alias_conflicts": [
                {
                    "character": "甲角色、乙角色",
                    "alias": "老师",
                    "reason": "多个角色共享该称呼，无法唯一归属",
                }
            ],
        }

        self.assertEqual([], novel_reader.worldview_schema_issues(worldview))

    def test_explicit_warning_alias_conflict_is_not_schema_blocker(self):
        worldview = {
            "characters": [
                {"name": "主角", "role": "主角", "fatal_flaw": "害怕失去", "secrets": ["秘密一", "秘密二", "秘密三"]},
            ],
            "persona_axes": [{"key": "courage"}, {"key": "clarity"}],
            "ending_dimensions": ["选择", "真相"],
            "factions": ["校园"],
            "hidden_threads": ["旧信"],
            "character_alias_conflicts": [
                {
                    "character": "甲、乙",
                    "alias": "老师",
                    "severity": "warning",
                    "reason": "称呼可能对应多个实体",
                }
            ],
        }

        self.assertEqual([], novel_reader.worldview_schema_issues(worldview))

    @patch.object(novel_reader, "LLMClient")
    def test_targeted_worldview_repair_preserves_unreturned_complete_fields(
        self,
        client_cls,
    ):
        client = MagicMock()
        client.chat_completion_json.return_value = {
            "characters": [
                {"name": "刘如京", "aliases": [], "role": "重要配角"},
                {"name": "王忠", "aliases": [], "role": "重要配角"},
            ],
            "persona_axes": [],
            "hidden_threads": [],
        }
        client_cls.return_value = client
        original = {
            "characters": [{"name": "刘如京", "aliases": ["王忠"]}],
            "persona_axes": [{"key": "clarity"}, {"key": "mercy"}],
            "hidden_threads": [{"name": "旧案"}],
            "factions": [{"name": "四顾门"}],
        }

        repaired = novel_reader._repair_worldview(
            original,
            reason="角色别名冲突",
            text_sample="原文",
            provider="openai",
        )

        self.assertEqual(2, len(repaired["characters"]))
        self.assertEqual(original["persona_axes"], repaired["persona_axes"])
        self.assertEqual(original["hidden_threads"], repaired["hidden_threads"])
        self.assertEqual(original["factions"], repaired["factions"])

    @patch.object(novel_reader, "detect_branch_type", return_value="default")
    @patch.object(novel_reader, "_extract_behavior_anchors", return_value="anchors")
    @patch.object(novel_reader, "_extract_style_samples_llm", return_value="style")
    @patch.object(novel_reader, "_summarize_one_chapter", return_value="summary")
    @patch.object(novel_reader, "_analyze_worldview")
    @patch.object(novel_reader, "LLMClient")
    def test_analyze_novel_preserves_character_secrets(
        self,
        _client,
        analyze_worldview,
        _summarize,
        _style,
        _anchors,
        _branch_type,
    ):
        worldview = {
            "characters": [
                {
                    "name": "沈月",
                    "role": "主角",
                    "fatal_flaw": "过度信任",
                    "secrets": ["她其实记得前世", "秘密二", "秘密三"],
                }
            ],
            "persona_axes": [
                {"key": "courage"},
                {"key": "trust"},
            ],
            "ending_dimensions": ["主角生死", "真相揭露"],
            "factions": [{"name": "山门"}],
            "hidden_threads": [{"name": "旧案"}],
        }
        analyze_worldview.return_value = worldview

        with patch("builtins.print"):
            context = novel_reader.analyze_novel(
                chapters=["正文"],
                full_text="正文",
                provider="openai",
                worldview_override=worldview,
            )

        self.assertIn("她其实记得前世", context.character_secrets_text)
        analyze_worldview.assert_not_called()

    @patch.object(novel_reader, "detect_branch_type", return_value="romance")
    @patch.object(novel_reader, "_repair_worldview")
    def test_cached_context_worldview_is_repaired_and_derived_fields_are_refreshed(
        self,
        repair_worldview,
        _branch_type,
    ):
        context = novel_reader.NovelContext(
            worldview={"characters": [{"name": "沈月", "role": "主角"}]},
            chapter_summaries=["第一章摘要"],
            style_samples="文风",
        )
        repair_worldview.return_value = {
            "characters": [
                {
                    "name": "沈月",
                    "role": "主角",
                    "fatal_flaw": "害怕再次失去同伴",
                    "secrets": ["秘密一", "秘密二", "秘密三"],
                }
            ],
            "persona_axes": [
                {"key": "affection_guchen"},
                {"key": "affection_linxue"},
            ],
            "ending_dimensions": ["主角生死", "同伴存亡"],
            "factions": [{"name": "山门"}],
            "hidden_threads": [{"name": "旧案"}],
            "is_romance_oriented": True,
            "romance_targets": [{"name": "顾沉"}],
        }

        repaired = novel_reader.ensure_novel_context_worldview(
            context,
            full_text="原文",
            provider="openai",
        )

        self.assertIs(repaired, context)
        self.assertEqual("害怕再次失去同伴", context.worldview["characters"][0]["fatal_flaw"])
        self.assertEqual("romance", context.branch_type)
        self.assertTrue(context.is_romance_oriented)
        self.assertIn("秘密一", context.character_secrets_text)


class TerminalStateTests(unittest.TestCase):
    def test_terminal_prompt_distinguishes_unknown_inheritance_from_hard_conflict(self):
        prompt = novel_reader.format_terminal_states_for_prompt(
            {"主角": {"alive": True, "life_status": "alive_confirmed"}}
        )

        self.assertIn("未提及/未知字段应继承已有状态", prompt)
        self.assertIn("时间线和地点变化可以通过过渡场景解决", prompt)

    @patch.object(novel_reader, "LLMClient")
    def test_unique_first_person_protagonist_is_active_even_when_named_supporting_characters_are_active(
        self, client_cls
    ):
        client = MagicMock()
        client.chat_completion_json.return_value = {
            "states": {
                "栗本燕": {
                    "alive": True,
                    "outer_alive": True,
                    "current_layer_alive": True,
                    "participation": "active",
                    "state_confidence": 0.9,
                    "evidence": "我沿着走廊走到教室",
                },
                "圣良": {
                    "alive": True,
                    "outer_alive": True,
                    "current_layer_alive": True,
                    "participation": "active",
                    "state_confidence": 0.9,
                    "evidence": "圣良推门进来",
                },
                "织音": {
                    "alive": True,
                    "outer_alive": True,
                    "current_layer_alive": True,
                    "participation": "active",
                    "state_confidence": 0.9,
                    "evidence": "织音递来便当",
                },
            }
        }
        client_cls.return_value = client

        states = novel_reader.extract_terminal_states(
            chapter_text="我沿着走廊走到教室，心里仍想着午餐。圣良推门进来。织音把便当递给我。",
            chapter_title="第一章",
            provider="openai",
            worldview={
                "style_notes": "叙述以栗本燕第一人称内心独白为主，部分章节切换视角。",
                "characters": [
                    {"name": "栗本燕", "role": "主角"},
                    {"name": "圣良", "role": "重要配角"},
                    {"name": "织音", "role": "重要配角"},
                ],
            },
        )

        self.assertEqual("active", states["栗本燕"]["participation"])
        prompt = client.chat_completion_json.call_args.kwargs["user_prompt"]
        self.assertIn("栗本燕", prompt.split("## 仅简略记录角色", 1)[0])

    @patch.object(novel_reader, "LLMClient")
    def test_first_person_protagonist_is_not_forced_when_chapter_explicitly_switches_pov(
        self, client_cls
    ):
        client = MagicMock()
        client.chat_completion_json.return_value = {
            "states": {
                "圣良": {
                    "alive": True,
                    "outer_alive": True,
                    "current_layer_alive": True,
                    "participation": "active",
                    "state_confidence": 0.9,
                    "evidence": "我（圣良）走进教室",
                }
            }
        }
        client_cls.return_value = client

        states = novel_reader.extract_terminal_states(
            chapter_text="【圣良视角】我走进教室，看见栗本燕正在窗边。",
            chapter_title="第二章",
            provider="openai",
            worldview={
                "style_notes": "叙述以栗本燕第一人称内心独白为主，部分章节切换视角。",
                "characters": [
                    {"name": "栗本燕", "role": "主角"},
                    {"name": "圣良", "role": "重要配角"},
                ],
            },
        )

        self.assertNotEqual("active", states.get("栗本燕", {}).get("participation"))

    @patch.object(novel_reader, "LLMClient")
    def test_first_person_mapping_accepts_boolean_protagonist_flag(self, client_cls):
        client = MagicMock()
        client.chat_completion_json.return_value = {
            "states": {
                "栗本燕": {
                    "alive": True,
                    "outer_alive": True,
                    "current_layer_alive": True,
                    "participation": "active",
                    "state_confidence": 0.9,
                    "evidence": "我走进教室并坐下",
                }
            }
        }
        client_cls.return_value = client

        states = novel_reader.extract_terminal_states(
            chapter_text="我走进教室并坐下，我随后翻开课本。",
            chapter_title="第六章",
            provider="openai",
            worldview={
                "style_notes": "全书采用第一人称叙述。",
                "characters": [
                    {"name": "栗本燕", "role": "主人公", "is_protagonist": True}
                ],
            },
        )

        self.assertEqual("active", states["栗本燕"]["participation"])

    @patch.object(novel_reader, "LLMClient")
    def test_first_person_mapping_accepts_third_person_dialogue_note(self, client_cls):
        """Dialogue wording must not disable first-person protagonist fallback."""
        client = MagicMock()
        client.chat_completion_json.return_value = {
            "states": {
                "栗本燕": {
                    "alive": True,
                    "outer_alive": True,
                    "current_layer_alive": True,
                    "participation": "active",
                    "state_confidence": 0.9,
                    "evidence": "我走进教室并关上门",
                }
            }
        }
        client_cls.return_value = client

        states = novel_reader.extract_terminal_states(
            chapter_text="我走进教室并关上门，我随后翻开课本。",
            chapter_title="第二章",
            provider="openai",
            worldview={
                "style_notes": "全文采用第一人称叙述，角色对白偶尔使用第三人称转述。",
                "characters": [
                    {"name": "栗本燕", "role": "主人公", "is_protagonist": True}
                ],
            },
        )

        self.assertEqual("active", states["栗本燕"]["participation"])

    @patch.object(novel_reader, "LLMClient")
    def test_first_person_mapping_accepts_protagonist_role_without_boolean_flag(
        self, client_cls
    ):
        """The common ``主人公`` role label is sufficient for fallback mapping."""
        client = MagicMock()
        client.chat_completion_json.return_value = {
            "states": {
                "栗本燕": {
                    "alive": True,
                    "outer_alive": True,
                    "current_layer_alive": True,
                    "participation": "active",
                    "state_confidence": 0.9,
                    "evidence": "我走进教室并关上门",
                }
            }
        }
        client_cls.return_value = client

        states = novel_reader.extract_terminal_states(
            chapter_text="我走进教室并关上门，我随后翻开课本。",
            chapter_title="第二章",
            provider="openai",
            worldview={
                "style_notes": "全书采用第一人称叙述。",
                "characters": [{"name": "栗本燕", "role": "主人公"}],
            },
        )

        self.assertEqual("active", states["栗本燕"]["participation"])

    @patch.object(novel_reader, "LLMClient")
    def test_long_term_inherited_passive_characters_are_pruned_but_core_and_dead_states_remain(
        self, client_cls
    ):
        client_cls.return_value = MagicMock()
        states = novel_reader.extract_terminal_states(
            chapter_text="本章只有环境描写，没有角色出场。",
            chapter_title="第三章",
            provider="openai",
            worldview={
                "characters": [
                    {"name": "栗本燕", "role": "主角"},
                    {"name": "核心同伴", "role": "重要配角"},
                    {"name": "路人甲", "role": "普通配角"},
                    {"name": "反派", "role": "反派"},
                ]
            },
            inherited_states={
                "栗本燕": {"alive": True, "state_source": "llm", "offscreen_streak": 4},
                "核心同伴": {"alive": True, "state_source": "llm", "offscreen_streak": 4},
                "路人甲": {"alive": True, "state_source": "llm", "offscreen_streak": 2},
                "反派": {
                    "alive": False,
                    "life_status": "death_confirmed",
                    "state_source": "llm",
                    "offscreen_streak": 8,
                },
            },
        )

        self.assertIn("栗本燕", states)
        self.assertIn("核心同伴", states)
        self.assertIn("反派", states)
        self.assertNotIn("路人甲", states)

    @patch.object(novel_reader, "LLMClient")
    def test_reappearing_mentioned_character_resets_offscreen_streak(self, client_cls):
        client = MagicMock()
        client_cls.return_value = client
        states = novel_reader.extract_terminal_states(
            chapter_text="公告里只有路人甲这个名字。",
            chapter_title="第四章",
            provider="openai",
            worldview={"characters": [{"name": "路人甲", "role": "普通配角"}]},
            inherited_states={
                "路人甲": {"alive": True, "offscreen_streak": 5, "state_source": "llm"}
            },
        )

        self.assertIn("路人甲", states)
        self.assertEqual("mentioned", states["路人甲"]["participation"])
        self.assertEqual(0, states["路人甲"]["offscreen_streak"])

    @patch.object(novel_reader, "LLMClient")
    def test_reappearing_active_character_resets_offscreen_streak(self, client_cls):
        client = MagicMock()
        client.chat_completion_json.return_value = {
            "states": {
                "路人甲": {
                    "alive": True,
                    "outer_alive": True,
                    "current_layer_alive": True,
                    "participation": "active",
                    "state_confidence": 0.9,
                    "evidence": "路人甲推门进来并开口",
                }
            }
        }
        client_cls.return_value = client

        states = novel_reader.extract_terminal_states(
            chapter_text="路人甲推门进来并开口说话。",
            chapter_title="第五章",
            provider="openai",
            worldview={"characters": [{"name": "路人甲", "role": "普通配角"}]},
            inherited_states={
                "路人甲": {"alive": True, "offscreen_streak": 5, "state_source": "llm"}
            },
        )

        self.assertEqual("active", states["路人甲"]["participation"])
        self.assertEqual(0, states["路人甲"]["offscreen_streak"])

    def test_terminal_tracking_classifies_active_mentioned_memory_and_offscreen(self):
        modes = novel_reader.classify_terminal_state_characters(
            chapter_text=(
                "李莲花推门进来，说要继续查案。\n"
                "王京京想起初中同桌谭凯，那时总借作业给她。\n"
                "她又从电话里听见顾沉的声音。"
            ),
            worldview={
                "characters": [
                    {"name": "李莲花"},
                    {"name": "谭凯"},
                    {"name": "顾沉"},
                ]
            },
        )

        self.assertEqual("active", modes["李莲花"])
        self.assertEqual("memory", modes["谭凯"])
        self.assertEqual("offscreen", modes["顾沉"])

    def test_terminal_tracking_keeps_memory_actor_active(self):
        modes = novel_reader.classify_terminal_state_characters(
            chapter_text="顾沉想起旧案，随后开口回答李莲花的问题。",
            worldview={"characters": [{"name": "顾沉"}]},
        )

        self.assertEqual("active", modes["顾沉"])

    def test_terminal_tracking_marks_brief_background_reference_as_mentioned(self):
        modes = novel_reader.classify_terminal_state_characters(
            chapter_text=(
                "王京京的初中同桌叫谭凯，她过去常借谭凯的数学作业。"
            ),
            worldview={"characters": [{"name": "谭凯", "role": "路人配角"}]},
        )

        self.assertEqual({"谭凯": "mentioned"}, modes)

    def test_terminal_tracking_excludes_unstable_generic_titles(self):
        modes = novel_reader.classify_terminal_state_characters(
            chapter_text=(
                "班长去找主任，老师没有回答。许老师随后走进教室并开口说话。"
            ),
            worldview={
                "characters": [
                    {"name": "班长"},
                    {"name": "主任"},
                    {"name": "老师"},
                    {"name": "许老师", "aliases": ["小许"]},
                ]
            },
        )

        self.assertNotIn("班长", modes)
        self.assertNotIn("主任", modes)
        self.assertNotIn("老师", modes)
        self.assertEqual("active", modes["许老师"])

    def test_terminal_tracking_does_not_confuse_short_name_with_long_name(self):
        modes = novel_reader.classify_terminal_state_characters(
            chapter_text="罗洪国笑着说道，随后父亲罗洪国走过来。",
            worldview={
                "characters": [
                    {"name": "洪", "role": "背景角色"},
                    {"name": "罗洪国", "role": "重要配角"},
                ]
            },
        )

        self.assertEqual("active", modes["罗洪国"])
        self.assertNotIn("洪", modes)

    def test_terminal_tracking_keeps_independent_single_character_name_active(self):
        modes = novel_reader.classify_terminal_state_characters(
            chapter_text="洪看向罗峰并说道：‘准备出发。’",
            worldview={"characters": [{"name": "洪", "role": "背景角色"}]},
        )

        self.assertEqual("active", modes["洪"])

    def test_terminal_tracking_ignores_single_character_name_inside_common_word(self):
        modes = novel_reader.classify_terminal_state_characters(
            chapter_text="洪泽湖发生了洪水，众人只在地图上标记了洪泽湖。",
            worldview={"characters": [{"name": "洪", "role": "背景角色"}]},
        )

        self.assertNotIn("洪", modes)

    def test_terminal_tracking_ignores_quoted_place_name_but_keeps_quoted_character(self):
        place_modes = novel_reader.classify_terminal_state_characters(
            chapter_text="地图上标着‘洪泽湖’三个字。",
            worldview={"characters": [{"name": "洪", "role": "背景角色"}]},
        )
        character_modes = novel_reader.classify_terminal_state_characters(
            chapter_text="‘洪’抬手示意众人停下。",
            worldview={"characters": [{"name": "洪", "role": "背景角色"}]},
        )

        self.assertNotIn("洪", place_modes)
        self.assertEqual("active", character_modes["洪"])

    def test_terminal_tracking_keeps_single_character_name_in_observation_and_offscreen_context(self):
        observed = novel_reader.classify_terminal_state_characters(
            chapter_text="罗峰看到洪站在门口。",
            worldview={"characters": [{"name": "洪", "role": "背景角色"}]},
        )
        mentioned = novel_reader.classify_terminal_state_characters(
            chapter_text="罗峰远远看见洪。",
            worldview={"characters": [{"name": "洪", "role": "背景角色"}]},
        )
        offscreen = novel_reader.classify_terminal_state_characters(
            chapter_text="洪在电话里说今晚不会回来。",
            worldview={"characters": [{"name": "洪", "role": "背景角色"}]},
        )

        self.assertEqual("active", observed["洪"])
        self.assertEqual("mentioned", mentioned["洪"])
        self.assertEqual("offscreen", offscreen["洪"])

    def test_terminal_tracking_does_not_treat_title_alias_as_character_action(self):
        modes = novel_reader.classify_terminal_state_characters(
            chapter_text=(
                "世界第一强者‘洪’，世界第二强者‘雷神’，分别创出极限武馆和雷电武馆。"
            ),
            worldview={
                "characters": [
                    {
                        "name": "洪",
                        "role": "背景角色",
                        "aliases": ["世界第一强者"],
                    },
                    {
                        "name": "雷神",
                        "role": "背景角色",
                        "aliases": ["世界第二强者"],
                    },
                ]
            },
        )

        self.assertEqual("mentioned", modes["洪"])
        self.assertEqual("mentioned", modes["雷神"])

    def test_terminal_tracking_does_not_treat_historical_title_attribution_as_active(self):
        modes = novel_reader.classify_terminal_state_characters(
            chapter_text="世界第一强者就说过，不想当将军的士兵不是好士兵。",
            worldview={
                "characters": [
                    {
                        "name": "洪",
                        "role": "背景角色",
                        "aliases": ["世界第一强者"],
                    }
                ]
            },
        )

        self.assertEqual("mentioned", modes["洪"])

    def test_terminal_tracking_historical_alias_attribution_variants_are_not_active(self):
        worldview = {
            "characters": [
                {
                    "name": "洪",
                    "role": "背景角色",
                    "aliases": ["世界第一强者"],
                }
            ]
        }

        for sentence in (
            "世界第一强者曾说，真正的强者要守护弱者。",
            "世界第一强者曾经说过，真正的强者要守护弱者。",
            "世界第一强者曾说了，真正的强者要守护弱者。",
            "世界第一强者过去说过这句话。",
        ):
            with self.subTest(sentence=sentence):
                modes = novel_reader.classify_terminal_state_characters(
                    chapter_text=sentence,
                    worldview=worldview,
                )
                self.assertEqual("mentioned", modes["洪"])

    def test_terminal_tracking_historical_alias_with_quoted_canonical_name_is_not_active(self):
        worldview = {
            "characters": [
                {
                    "name": "洪",
                    "role": "背景角色",
                    "aliases": ["世界第一强者"],
                }
            ]
        }

        for sentence in (
            "世界第一强者‘洪’就说过，不想当将军的士兵不是好士兵。",
            "世界第一强者“洪”曾说过，真正的强者要守护弱者。",
            "世界第一强者‘洪’曾说了，真正的强者要守护弱者。",
        ):
            with self.subTest(sentence=sentence):
                modes = novel_reader.classify_terminal_state_characters(
                    chapter_text=sentence,
                    worldview=worldview,
                )
                self.assertEqual("mentioned", modes["洪"])

    def test_terminal_tracking_current_alias_with_quoted_canonical_name_remains_active(self):
        modes = novel_reader.classify_terminal_state_characters(
            chapter_text="世界第一强者‘洪’说：‘准备出发。’",
            worldview={
                "characters": [
                    {
                        "name": "洪",
                        "role": "背景角色",
                        "aliases": ["世界第一强者"],
                    }
                ]
            },
        )

        self.assertEqual("active", modes["洪"])

    def test_terminal_tracking_current_alias_action_remains_active(self):
        modes = novel_reader.classify_terminal_state_characters(
            chapter_text="世界第一强者抬手示意众人停下。",
            worldview={
                "characters": [
                    {
                        "name": "洪",
                        "role": "背景角色",
                        "aliases": ["世界第一强者"],
                    }
                ]
            },
        )

        self.assertEqual("active", modes["洪"])

    def test_terminal_tracking_current_alias_speech_remains_active(self):
        worldview = {
            "characters": [
                {
                    "name": "洪",
                    "role": "背景角色",
                    "aliases": ["世界第一强者"],
                }
            ]
        }

        for sentence in (
            "世界第一强者说：‘准备出发。’",
            "世界第一强者说着话走进大厅。",
        ):
            with self.subTest(sentence=sentence):
                modes = novel_reader.classify_terminal_state_characters(
                    chapter_text=sentence,
                    worldview=worldview,
                )
                self.assertEqual("active", modes["洪"])

    @patch.object(novel_reader, "LLMClient")
    def test_mentioned_character_does_not_trigger_detailed_state_retry(self, client_cls):
        client = MagicMock()
        client.chat_completion_json.return_value = {
            "states": {
                "李莲花": {
                    "alive": True,
                    "outer_alive": True,
                    "current_layer_alive": True,
                    "participation": "active",
                    "state_confidence": 0.9,
                    "evidence": "李莲花推门进来并开口说话",
                    "condition": "清醒",
                    "location": "教室",
                }
            }
        }
        client_cls.return_value = client

        states = novel_reader.extract_terminal_states(
            chapter_text=(
                "李莲花推门进来并开口说话。王京京的初中同桌叫谭凯，"
                "她过去常借谭凯的作业。"
            ),
            chapter_title="测试章",
            provider="openai",
            worldview={
                "characters": [
                    {"name": "李莲花"},
                    {"name": "谭凯", "role": "路人配角"},
                ]
            },
        )

        self.assertEqual(1, client.chat_completion_json.call_count)
        self.assertEqual("active", states["李莲花"]["participation"])
        self.assertEqual("mentioned", states["谭凯"]["participation"])
        self.assertEqual("", states["谭凯"]["condition"])
        self.assertEqual("", states["谭凯"]["location"])
        prompt = client.chat_completion_json.call_args.kwargs["user_prompt"]
        self.assertIn("详细末态角色", prompt)
        self.assertIn("仅简略记录角色", prompt)

    @patch.object(novel_reader, "LLMClient")
    def test_memory_and_offscreen_characters_inherit_previous_state(self, client_cls):
        client = MagicMock()
        client.chat_completion_json.return_value = {
            "states": {
                "李莲花": {
                    "alive": True,
                    "participation": "active",
                    "state_confidence": 0.9,
                    "evidence": "李莲花继续查案",
                }
            }
        }
        client_cls.return_value = client

        states = novel_reader.extract_terminal_states(
            chapter_text=(
                "李莲花继续查案。他想起顾沉当年在山门说过的话，"
                "又从电话里听见沈月的声音。"
            ),
            chapter_title="测试章",
            provider="openai",
            worldview={
                "characters": [
                    {"name": "李莲花"},
                    {"name": "顾沉"},
                    {"name": "沈月"},
                ]
            },
            inherited_states={
                "顾沉": {
                    "alive": False,
                    "outer_alive": False,
                    "life_status": "death_confirmed",
                    "condition": "已故",
                    "location": "古墓",
                    "state_confidence": 0.95,
                    "state_source": "llm",
                },
                "沈月": {
                    "alive": True,
                    "outer_alive": True,
                    "life_status": "alive_confirmed",
                    "condition": "轻伤休养",
                    "location": "杭州",
                    "state_confidence": 0.9,
                    "state_source": "llm",
                },
            },
        )

        self.assertEqual("offscreen", states["顾沉"]["participation"])
        self.assertEqual("memory", states["顾沉"]["presence_type"])
        self.assertEqual("已故", states["顾沉"]["condition"])
        self.assertEqual("古墓", states["顾沉"]["location"])
        self.assertEqual("offscreen", states["沈月"]["participation"])
        self.assertEqual("轻伤休养", states["沈月"]["condition"])
        self.assertEqual("杭州", states["沈月"]["location"])

    @patch.object(novel_reader, "LLMClient")
    def test_terminal_state_prompt_tracks_current_and_required_inherited_characters_only(self, client_cls):
        client = MagicMock()
        client.chat_completion_json.return_value = {
            "states": {
                "李莲花": {
                    "alive": True,
                    "outer_alive": True,
                    "current_layer_alive": True,
                    "participation": "active",
                    "state_confidence": 0.9,
                    "evidence": "李莲花走进现场",
                },
                "顾沉": {
                    "alive": False,
                    "outer_alive": False,
                    "current_layer_alive": False,
                    "participation": "offscreen",
                    "state_confidence": 0.9,
                    "evidence": "上一章确认死亡",
                },
            }
        }
        client_cls.return_value = client
        worldview = {
            "characters": [
                {"name": "李莲花"},
                {"name": "顾沉"},
                {"name": "方多病"},
                {"name": "笛飞声"},
            ]
        }

        states = novel_reader.extract_terminal_states(
            chapter_text="李莲花走进现场。",
            chapter_title="测试章",
            provider="openai",
            worldview=worldview,
            inherited_states={
                "顾沉": {
                    "alive": False,
                    "outer_alive": False,
                    "current_layer_alive": False,
                    "participation": "offscreen",
                    "state_confidence": 0.9,
                    "evidence": "上一章确认死亡",
                }
            },
        )

        prompt = client.chat_completion_json.call_args.kwargs["user_prompt"]
        self.assertIn("李莲花", prompt)
        self.assertIn("顾沉", prompt)
        self.assertNotIn("方多病", prompt)
        self.assertNotIn("笛飞声", prompt)
        self.assertEqual({"李莲花", "顾沉"}, set(states))

    @patch.object(novel_reader, "LLMClient")
    def test_modular_terminal_state_extraction_uses_lightweight_budget(self, client_cls):
        client = MagicMock()
        client.chat_completion_json.return_value = {
            "states": {
                "李莲花": {
                    "alive": True,
                    "outer_alive": True,
                    "current_layer_alive": True,
                    "participation": "active",
                    "state_confidence": 0.9,
                    "evidence": "李莲花仍在查案",
                }
            }
        }
        client_cls.return_value = client

        novel_reader.extract_terminal_states(
            chapter_text="李莲花仍在查案。",
            chapter_title="测试章",
            provider="openai",
            worldview={"characters": [{"name": "李莲花"}]},
            lightweight=True,
        )

        kwargs = client.chat_completion_json.call_args.kwargs
        self.assertLessEqual(kwargs["max_tokens"], 800)
        self.assertIn("轻量末态", kwargs["user_prompt"])

    @patch.object(novel_reader, "LLMClient")
    def test_terminal_state_empty_response_retries_before_fallback(self, client_cls):
        client = MagicMock()
        client.chat_completion_json.side_effect = [
            None,
            {
                "states": {
                    "李莲花": {
                        "alive": True,
                        "outer_alive": True,
                        "current_layer_alive": True,
                        "presence_type": "physical",
                        "reality_layer": "reality",
                        "state_confidence": 0.9,
                        "evidence": "李莲花仍在现场查案",
                    }
                }
            },
        ]
        client_cls.return_value = client

        states = novel_reader.extract_terminal_states(
            chapter_text="李莲花仍在现场查案。",
            chapter_title="测试章",
            provider="openai",
            worldview={"characters": [{"name": "李莲花"}]},
        )

        self.assertEqual(2, client.chat_completion_json.call_count)
        self.assertEqual("llm", states["李莲花"]["state_source"])

    def test_terminal_state_cache_rejects_fallback_or_low_confidence_states(self):
        self.assertFalse(
            novel_reader.terminal_states_are_cacheable(
                {"李莲花": {"state_source": "inherited_fallback", "state_confidence": 0.25}}
            )
        )
        self.assertFalse(
            novel_reader.terminal_states_are_cacheable(
                {"李莲花": {"state_source": "llm", "state_confidence": 0.4}}
            )
        )
        self.assertTrue(
            novel_reader.terminal_states_are_cacheable(
                {"李莲花": {"state_source": "llm", "state_confidence": 0.9}}
            )
        )
        self.assertTrue(
            novel_reader.terminal_states_are_cacheable(
                {
                    "李莲花": {
                        "state_source": "llm",
                        "state_confidence": 0.9,
                        "participation": "active",
                    },
                    "谭凯": {
                        "state_source": "deterministic_passive",
                        "state_confidence": 0.25,
                        "participation": "mentioned",
                    },
                }
            )
        )

    @patch.object(novel_reader, "LLMClient")
    def test_terminal_state_timeout_uses_marked_low_confidence_fallback(self, client_cls):
        client = MagicMock()
        client.chat_completion_json.side_effect = TimeoutError("gateway timeout")
        client_cls.return_value = client

        states = novel_reader.extract_terminal_states(
            chapter_text="江渡走进教室并开口说话。",
            chapter_title="测试章",
            provider="openai",
            worldview={"characters": [{"name": "江渡"}]},
            inherited_states={
                "江渡": {
                    "alive": False,
                    "outer_alive": False,
                    "current_layer_alive": True,
                    "presence_type": "memory",
                }
            },
        )

        kwargs = client.chat_completion_json.call_args.kwargs
        self.assertGreaterEqual(kwargs["timeout"], 900)
        self.assertEqual("inherited_fallback", states["江渡"]["state_source"])
        self.assertLessEqual(states["江渡"]["state_confidence"], 0.25)
        self.assertFalse(states["江渡"]["outer_alive"])
        self.assertTrue(states["江渡"]["current_layer_alive"])

    @patch.object(novel_reader, "LLMClient")
    def test_extract_terminal_states_normalizes_types_and_prevents_resurrection(self, client_cls):
        client = MagicMock()
        client.chat_completion_json.return_value = {
            "states": {
                "沈月": {
                    "alive": "yes",
                    "condition": "轻伤",
                    "location": "山门",
                    "note": "归来",
                },
                "顾沉": {
                    "alive": "alive",
                    "condition": "痊愈",
                    "location": "山门",
                    "note": "重新出现",
                },
            }
        }
        client_cls.return_value = client

        states = novel_reader.extract_terminal_states(
            chapter_text="沈月回到了山门。",
            chapter_title="测试章",
            provider="openai",
            worldview={"characters": [{"name": "沈月"}, {"name": "顾沉"}]},
            inherited_states={
                "顾沉": {
                    "alive": False,
                    "condition": "已死亡",
                    "location": "古墓",
                    "note": "上一章死亡",
                }
            },
        )

        self.assertIs(states["沈月"]["alive"], True)
        self.assertIs(states["顾沉"]["alive"], False)
        self.assertEqual(states["顾沉"]["location"], "古墓")

    @patch.object(novel_reader, "LLMClient")
    def test_extract_terminal_states_uses_worldview_reality_contract(self, client_cls):
        client = MagicMock()
        client.chat_completion_json.return_value = {
            "states": {
                "李迟舒": {
                    "alive": False,
                    "presence_type": "dream",
                    "reality_layer": "therapy_reconstruction",
                    "timeline": "高三",
                    "location": "学校",
                    "identity_status": "高中生形象",
                    "local_frame": "present",
                    "participation": "active",
                    "state_confidence": 0.9,
                    "evidence": "现实已故，重构中以高中生形象出现",
                }
            }
        }
        client_cls.return_value = client

        states = novel_reader.extract_terminal_states(
            chapter_text="现实中的李迟舒已经去世，治疗重构中他仍在高中。",
            chapter_title="测试章",
            provider="openai",
            worldview={
                "characters": [{"name": "李迟舒"}],
                "narrative_reality_contract": {
                    "primary_reality_domain": "reconstructed_world",
                    "primary_reconstruction_type": "dream",
                    "compatible_reconstruction_types": [
                        "dream",
                        "therapy_reconstruction",
                    ],
                },
            },
        )

        self.assertEqual("reconstructed_world", states["李迟舒"]["reality_domain"])
        self.assertEqual("dream", states["李迟舒"]["reconstruction_type"])
        prompt = client.chat_completion_json.call_args.kwargs["user_prompt"]
        self.assertIn("participation", prompt)
        self.assertIn("local_frame", prompt)
        self.assertIn("全书统一叙事层基准", prompt)


if __name__ == "__main__":
    unittest.main()
