from copy import deepcopy
import unittest

from services.story_planner.core import chapter_branch_quality as quality


evaluate_artifacts = quality.evaluate_artifacts


class ChapterBranchQualityTests(unittest.TestCase):
    def setUp(self):
        self.story_tree = {
            "root_id": "chapter_1",
            "branch_type": "default",
            "nodes": {
                "chapter_1": {
                    "title": "第一章",
                    "content_file": "chapter_1.md",
                    "children": [
                        {"child_id": "ending_a", "choice_text": "进入新结局"}
                    ],
                    "parent_ids": [],
                    "is_original": True,
                    "summary": "沈月在雨夜得知真相。",
                    "terminal_states": {"沈月": {"alive": True}},
                    "generated_at": "2026-08-11T01:00:00",
                },
                "ending_a": {
                    "title": "新结局",
                    "content_file": "ending_a.md",
                    "children": [],
                    "parent_ids": ["chapter_1"],
                    "is_original": False,
                    "summary": "沈月活着完成复仇，最终释然。",
                    "terminal_states": {"沈月": {"alive": True}},
                    "generated_at": "2026-08-11T01:01:00",
                },
            },
            "variable_defs": [
                {"key": "courage", "init": 50, "threshold": 53}
            ],
            "metrics": {"cache_hits": 0},
        }
        prose = "夜雨敲在窗上，沈月握紧剑柄，终于决定面对真相。" * 30
        self.md = {
            "chapter_1": "# 第一章\n\n" + prose,
            "ending_a": (
                "# 新结局\n\n"
                + prose
                + "\n<!-- effects_a={'courage': 3} effects_b={'courage': -3} -->"
            ),
        }

    def test_valid_artifacts_pass_core_contracts(self):
        report = evaluate_artifacts(self.story_tree, self.md)

        self.assertTrue(report["passed"], report["errors"])
        self.assertEqual(report["stats"]["node_count"], 2)

    def test_release_quality_violations_are_warnings_and_score_is_only_blocker(self):
        """Content-quality findings must not trigger a second release gate."""
        self.story_tree["nodes"]["chapter_1"]["terminal_states"]["沈月"]["alive"] = False
        self.md["ending_a"] += (
            "\n<!-- effects_a={'invented': 99} effects_b={'invented': 99} -->"
        )

        report = quality.validate_chapter_branch_release(self.story_tree, self.md)

        self.assertTrue(report["passed"], report)
        self.assertGreaterEqual(report["score"], 30)
        self.assertEqual([], report["errors"])
        self.assertTrue(any("复活" in item for item in report["warnings"]))
        self.assertTrue(any("effects" in item for item in report["warnings"]))
        self.assertEqual("min_score_only", report["blocking_policy"])
        self.assertEqual(30, report["blocking_threshold"])

    def test_release_quality_score_below_thirty_is_the_only_quality_blocker(self):
        """A sufficiently damaged artifact is blocked only by its score."""
        tree = deepcopy(self.story_tree)
        tree["nodes"] = {
            f"broken_{index}": {"title": "", "children": []}
            for index in range(12)
        }

        tree["root_id"] = "broken_0"
        report = quality.validate_chapter_branch_release(tree, {})

        self.assertLess(report["score"], 30)
        self.assertFalse(report["passed"])
        self.assertEqual(1, len(report["errors"]))
        self.assertIn("质量评分", report["errors"][0])
        self.assertTrue(report["warnings"])

    def test_multilingual_quality_defaults_to_english_locale(self):
        english = deepcopy(self.story_tree)
        chinese = deepcopy(self.story_tree)
        english["nodes"]["chapter_1"]["summary"] = "English summary"
        chinese["nodes"]["chapter_1"]["summary"] = "中文摘要"
        selected = quality._select_story_tree_locale(
            {"zh-CN": chinese, "en-US": english, "publication": {"default_language": "zh-CN"}}
        )
        self.assertEqual("English summary", selected["nodes"]["chapter_1"]["summary"])

    def test_release_threshold_is_fixed_at_thirty_even_if_caller_passes_legacy_value(self):
        """Legacy callers cannot silently restore the retired 75-point gate."""
        report = quality.validate_chapter_branch_release(
            self.story_tree,
            self.md,
            thresholds={"min_score": 99},
        )

        self.assertEqual(30, report["thresholds"]["min_score"])
        self.assertEqual(30, report["blocking_threshold"])
        self.assertEqual("min_score_only", report["blocking_policy"])

    def test_post_gate_warnings_recompute_score_and_passed(self):
        """Warnings discovered after the initial gate must affect final score."""
        report = quality.validate_chapter_branch_release(self.story_tree, self.md)
        report = quality.append_quality_warnings(
            report,
            ["多语言 story_tree 告警：ja-JP 缺少 content_warnings" for _ in range(25)],
        )

        self.assertEqual(25, len(report["warnings"]))
        self.assertEqual(25, report["score"])
        self.assertFalse(report["passed"])
        self.assertEqual(1, len(report["errors"]))
        self.assertIn("质量评分", report["errors"][0])

    def test_precomputed_score_error_is_not_counted_as_a_second_warning(self):
        tree = deepcopy(self.story_tree)
        tree["nodes"] = {
            f"broken_{index}": {"title": "", "children": []}
            for index in range(40)
        }
        tree["root_id"] = "broken_0"

        report = quality.validate_chapter_branch_release(tree, {})

        self.assertFalse(report["passed"])
        self.assertEqual(1, len(report["errors"]))
        self.assertFalse(
            any(item.startswith("质量告警：质量评分") for item in report["warnings"])
        )

    def test_multilingual_story_tree_bundle_is_evaluated_from_default_locale(self):
        bundle = {
            "en-US": deepcopy(self.story_tree),
            "ja-JP": deepcopy(self.story_tree),
            "zh-CN": deepcopy(self.story_tree),
            "publication": {"default_language": "zh-CN"},
        }

        report = evaluate_artifacts(bundle, self.md)

        self.assertTrue(report["passed"], report["errors"])
        self.assertEqual(2, report["stats"]["node_count"])
        self.assertNotIn("nodes 为空或格式错误", report["errors"])
        self.assertNotIn("story_tree 未包含运行 metrics", report["warnings"])

    def test_release_plan_allows_explicitly_omitted_optional_whatif(self):
        plan = {
            "nodes": {
                "chapter_1": {"status": "completed", "real_id": "chapter_1"},
                "ending_a": {"status": "completed", "real_id": "ending_a"},
                "ch1_alt": {
                    "status": "omitted",
                    "real_id": "node_alt",
                    "error": "收束硬规则重试耗尽",
                },
            },
            "topology_result": {
                "topology": [
                    {"node_id": "chapter_1", "is_original": True, "type": "chapter"},
                    {
                        "node_id": "ch1_alt",
                        "is_original": False,
                        "type": "chapter",
                        "omitted_branch": True,
                        "omission_reason": "收束硬规则重试耗尽",
                    },
                    {"node_id": "ending_a", "is_original": False, "type": "ending"},
                ]
            },
        }

        report = quality.validate_chapter_branch_release(
            self.story_tree,
            self.md,
            plan=plan,
        )

        self.assertNotIn("断点计划存在未完成节点", "；".join(report["errors"]))
        self.assertEqual(1, report["stats"]["omitted_whatif_count"])

    def test_offline_causality_check_does_not_reject_by_fatal_keywords(self):
        ok, reason = quality.validate_ending_variable_causality(
            {
                "key": "tenderness",
                "name": "柔软",
                "high_desc": "主动表达需求，相信自己值得被爱",
                "ending_hint": "主角学会求助并继续生活",
            },
            "李迟舒最终病逝。沈抱山整理他的遗物，替他寄出旧信。",
        )

        self.assertTrue(ok)
        self.assertIn("未配置大模型语义审查器", reason)

    def test_positive_persona_ending_accepts_protagonist_action_and_payoff(self):
        ok, reason = quality.validate_ending_variable_causality(
            {
                "key": "tenderness",
                "name": "柔软",
                "high_desc": "主动表达需求，相信自己值得被爱",
                "ending_hint": "主角学会求助并继续生活",
            },
            "李迟舒主动说出自己的需求，接受治疗，也终于相信自己值得被爱。此后他继续生活。",
        )

        self.assertTrue(ok, reason)

    def test_romance_ending_uses_semantic_review_for_natural_payoff(self):
        """Natural relationship payoffs must be accepted from semantic evidence."""
        reviews = []

        def reviewer(payload):
            reviews.append(payload)
            return {
                "passed": True,
                "confidence": 0.95,
                "reason": "正文明确写出许定陪伴主角共同出国并规划共同生活",
                "core_chain": {
                    "variable_behavior_linked": True,
                    "target_linked": True,
                    "observable_payoff": True,
                    "causal_payoff": True,
                    "relationship_confirmation": False,
                    "shared_future": True,
                    "fatal_state_consistent": True,
                },
                "evidence": {
                    "evidence_quote": "她和许定一起登上飞往海外的航班，决定在新的城市共同生活",
                    "evidence_type": "共同未来安排",
                    "artifact_name": "共同出国与生活规划",
                    "creator": "许定与主角",
                    "action": "共同出国并规划生活",
                    "verifier": "正文叙事",
                    "verification_method": "明确行动与后续安排",
                    "story_effect": "关系从暧昧进入稳定伴侣阶段",
                },
            }

        ok, reason = quality.validate_ending_variable_causality(
            {
                "key": "affection_xuding",
                "description": "玩家持续选择信任许定并接受他的陪伴，最终走向共同未来",
            },
            "她和许定一起登上飞往海外的航班，决定在新的城市共同生活。",
            semantic_reviewer=reviewer,
        )

        self.assertTrue(ok, reason)
        self.assertEqual(1, len(reviews))
        self.assertEqual("affection_xuding", reviews[0]["variable"]["key"])

    def test_release_gate_accepts_semantic_ending_reviewer_without_keyword_match(self):
        self.story_tree["branch_type"] = "romance"
        self.story_tree["variable_defs"] = [
            {"key": "affection_xuding", "init": 50, "threshold": 53}
        ]
        self.story_tree["branch_thresholds"] = {
            "ending_a": {"variable": "affection_xuding", "threshold": 53}
        }
        self.story_tree["nodes"]["ending_a"]["title"] = "结局·许定线"
        natural_payoff = "她陪许定搬到海边，约好每年春天都一起回来。"
        self.md["ending_a"] = (
            "# 许定结局\n\n" + (natural_payoff * 45)
            + "\n<!-- effects_a={'affection_xuding': 3} effects_b={'affection_xuding': -3} -->"
        )

        def reviewer(payload):
            self.assertEqual("affection_xuding", payload["variable"]["key"])
            return {
                "passed": True,
                "confidence": 0.92,
                "core_chain": {
                    "variable_behavior_linked": True,
                    "observable_payoff": True,
                    "causal_payoff": True,
                    "route_specific": True,
                    "target_linked": True,
                    "relationship_confirmation": False,
                    "shared_future": True,
                    "fatal_state_consistent": True,
                },
                "evidence": {"evidence_quote": natural_payoff},
            }

        report = quality.validate_chapter_branch_release(
            self.story_tree,
            self.md,
            ending_semantic_reviewer=reviewer,
        )

        self.assertTrue(report["checks"]["ending_variable_causality"], report["errors"])

    def test_romance_reviewer_must_confirm_relationship_or_shared_future(self):
        def reviewer(_payload):
            return {
                "passed": True,
                "confidence": 0.95,
                "core_chain": {
                    "variable_behavior_linked": True,
                    "observable_payoff": True,
                    "causal_payoff": True,
                    "route_specific": True,
                    "target_linked": True,
                    "relationship_confirmation": False,
                    "shared_future": False,
                    "fatal_state_consistent": True,
                },
                "evidence": {"evidence_quote": "她替许定挡下那一刀，改变了战局"},
            }

        ok, reason = quality.validate_ending_variable_causality(
            {"key": "affection_xuding", "description": "与许定建立关系"},
            "她替许定挡下那一刀，改变了战局。",
            semantic_reviewer=reviewer,
        )

        self.assertFalse(ok)
        self.assertIn("target_linked_and_relationship_or_shared_future", reason)

    def test_semantic_review_requires_explicit_fatal_state_consistency(self):
        def reviewer(_payload):
            return {
                "passed": True,
                "confidence": 0.95,
                "core_chain": {
                    "variable_behavior_linked": True,
                    "observable_payoff": True,
                    "causal_payoff": True,
                    "route_specific": True,
                },
                "evidence": {"evidence_quote": "她把门推开，所有人都看见了真相"},
            }

        ok, reason = quality.validate_ending_variable_causality(
            {"key": "courage", "description": "主动面对真相"},
            "她把门推开，所有人都看见了真相。",
            semantic_reviewer=reviewer,
        )

        self.assertFalse(ok)
        self.assertIn("fatal_state_consistent", reason)

    def test_playfulness_accepts_observable_humor_payoff_without_generic_keywords(self):
        reviews = []

        def reviewer(payload):
            reviews.append(payload)
            return {
                "passed": True,
                "confidence": 0.94,
                "reason": "主角用临场玩笑缓和僵局，改变了对方的决定",
                "core_chain": {
                    "variable_behavior_linked": True,
                    "observable_payoff": True,
                    "causal_payoff": True,
                    "route_specific": True,
                    "fatal_state_consistent": True,
                },
                "evidence": {
                    "evidence_quote": "她把严肃的誓言说成一句俏皮话，屋里的人全都笑了起来",
                },
            }

        ok, reason = quality.validate_ending_variable_causality(
            {
                "key": "playfulness",
                "name": "玩心",
                "description": "玩家持续选择用幽默感打破拘谨",
                "high_desc": "在困局里保留轻盈和童心",
                "ending_hint": "用玩笑化解最后的隔阂",
            },
            "她把严肃的誓言说成一句俏皮话，屋里的人全都笑了起来。",
            semantic_reviewer=reviewer,
        )

        self.assertTrue(ok, reason)
        self.assertEqual(1, len(reviews))

    def test_playfulness_text_is_not_decided_by_keywords_without_semantic_review(self):
        ok, reason = quality.validate_ending_variable_causality(
            {
                "key": "playfulness",
                "name": "玩心",
                "description": "玩家持续选择用幽默感打破拘谨",
                "high_desc": "在困局里保留轻盈和童心",
                "ending_hint": "用玩笑化解最后的隔阂",
            },
            "她开了个玩笑，随后独自离开，现场没有任何决定或关系改变。",
        )

        self.assertTrue(ok)
        self.assertIn("未配置大模型语义审查器", reason)

    def test_legacy_keywords_are_never_an_offline_release_decision(self):
        assessment = quality.assess_ending_variable_legacy(
            "courage",
            "她留下署名信，收信人核验笔迹后据此公开真相。",
            ui_mode="Ancient",
        )

        self.assertTrue(assessment["passed"])
        self.assertEqual("review_required", assessment["status"])
        self.assertEqual("manual_review", assessment["channel"])
        self.assertIn("未配置大模型语义审查器", assessment["reason"])

    def test_empty_persisted_legacy_review_is_not_treated_as_a_failed_review(self):
        assessment = quality.assess_ending_variable_legacy(
            "courage",
            "她留下署名信，收信人核验笔迹后据此公开真相。",
            semantic_review={},
        )

        self.assertTrue(assessment["passed"])
        self.assertEqual("manual_review", assessment["channel"])
        self.assertEqual("review_required", assessment["status"])

    def test_offline_legacy_review_requirement_is_reported_as_warning(self):
        self.story_tree["branch_thresholds"] = {
            "ending_a": {"variable": "courage", "threshold": 53},
        }

        report = evaluate_artifacts(self.story_tree, self.md)

        self.assertTrue(report["checks"]["ending_variable_legacy"])
        self.assertTrue(any("人工复核" in item for item in report["warnings"]))

    def test_playfulness_still_rejects_ending_without_related_behavior_or_payoff(self):
        def reviewer(_payload):
            return {
                "passed": False,
                "confidence": 0.96,
                "reason": "正文没有呈现玩心行为带来的可观察结局回报",
                "core_chain": {
                    "variable_behavior_linked": False,
                    "observable_payoff": False,
                    "causal_payoff": False,
                    "route_specific": False,
                    "fatal_state_consistent": True,
                },
                "evidence": {},
            }

        ok, reason = quality.validate_ending_variable_causality(
            {
                "key": "playfulness",
                "name": "玩心",
                "description": "玩家持续选择用幽默感打破拘谨",
                "high_desc": "在困局里保留轻盈和童心",
                "ending_hint": "用玩笑化解最后的隔阂",
            },
            "会议按既定流程结束。她整理完文件，独自离开了办公楼。",
            semantic_reviewer=reviewer,
        )

        self.assertFalse(ok)
        self.assertIn("玩心", reason)

    def test_disabled_numeric_route_is_excluded_from_ending_causality_gate(self):
        """A route disabled by reachability calibration must not block release."""
        self.story_tree["variable_defs"] = [
            {
                "key": "clarity",
                "name": "clarity",
                "description": "disabled route",
                "route_enabled": False,
                "init": 50,
                "threshold": 70,
            }
        ]
        self.story_tree["branch_thresholds"] = {
            "ending_a": {"variable": "clarity", "threshold": 70}
        }
        reviews = []

        def reviewer(_payload):
            reviews.append(True)
            return {
                "passed": False,
                "confidence": 1.0,
                "reason": "disabled route must not be reviewed",
                "core_chain": {},
                "evidence": {},
            }

        report = evaluate_artifacts(
            self.story_tree,
            self.md,
            ending_semantic_reviewer=reviewer,
        )

        self.assertEqual([], reviews)
        self.assertTrue(report["checks"]["ending_variable_causality"], report["errors"])
        self.assertFalse(any("clarity" in error for error in report["errors"]))

    def test_disabled_numeric_route_validator_returns_without_calling_reviewer(self):
        calls = []

        def reviewer(_payload):
            calls.append(True)
            raise AssertionError("disabled routes must not invoke the reviewer")

        ok, reason = quality.validate_ending_variable_causality(
            {
                "key": "clarity",
                "route_enabled": False,
                "description": "disabled route",
            },
            "任意结局正文",
            semantic_reviewer=reviewer,
        )

        self.assertTrue(ok)
        self.assertEqual([], calls)
        self.assertIn("路线已禁用", reason)

    def test_unknown_persona_axis_keyword_mismatch_is_warning_not_hard_failure(self):
        ok, reason = quality.validate_ending_variable_causality(
            {
                "key": "improvisation",
                "name": "即兴感",
                "description": "玩家持续选择不按预案行动",
                "high_desc": "临场创造新的解决方式",
                "ending_hint": "以意外方案打开新局面",
            },
            "她临时改了路线，从侧门带大家离开，避开了大厅里的冲突。",
        )

        self.assertTrue(ok)
        self.assertIn("降级为人工审核提示", reason)

    def test_placeholders_illegal_effects_and_resurrection_are_warnings(self):
        self.md["ending_a"] = (
            "# 新结局\n\n"
            + "故事在此延伸。" * 80
            + "\n<!-- effects_a={'invented': 99} effects_b={'invented': 99} -->"
        )
        self.story_tree["nodes"]["chapter_1"]["terminal_states"]["沈月"]["alive"] = False

        report = evaluate_artifacts(self.story_tree, self.md)

        self.assertTrue(report["passed"], report)
        joined = "；".join(report["warnings"])
        self.assertIn("占位", joined)
        self.assertIn("effects", joined)
        self.assertIn("复活", joined)

    def test_dead_character_hallucination_is_not_reported_as_resurrection(self):
        parent = self.story_tree["nodes"]["chapter_1"]["terminal_states"]["沈月"]
        child = self.story_tree["nodes"]["ending_a"]["terminal_states"]["沈月"]
        parent.update(
            {
                "alive": False,
                "presence_type": "hallucination",
                "reality_layer": "therapy_reconstruction",
            }
        )
        child.update(
            {
                "alive": False,
                "presence_type": "hallucination",
                "reality_layer": "therapy_reconstruction",
            }
        )

        report = evaluate_artifacts(self.story_tree, self.md)

        self.assertNotIn("复活", "；".join(report["warnings"]))

    def test_incomplete_worldview_is_schema_warning(self):
        report = evaluate_artifacts(
            self.story_tree,
            self.md,
            worldview={
                "characters": [{"name": "沈月", "role": "主角"}],
                "ending_dimensions": ["主角生死", "复仇成败"],
            },
        )

        self.assertTrue(report["passed"], report)
        self.assertFalse(report["checks"]["worldview_schema"])
        self.assertIn("fatal_flaw", "；".join(report["warnings"]))

    def test_variable_driven_mode_without_soft_branches_is_warning(self):
        self.md["ending_a"] = "# 新结局\n\n" + ("雨夜之后，众人终于抵达山门。" * 40)

        report = evaluate_artifacts(self.story_tree, self.md)

        self.assertTrue(report["passed"], report)
        self.assertFalse(report["checks"]["soft_branches"])
        self.assertIn("未生成软分支", "；".join(report["warnings"]))

    def test_modular_mode_accepts_empty_effects_without_variables(self):
        self.story_tree["branch_type"] = "modular"
        self.story_tree["variable_defs"] = []
        prose = "李莲花沿着窗棂检查香灰，随后回到原本的案情推进位置。" * 30
        self.md["ending_a"] = (
            "# 调查节点\n\n"
            + prose
            + "\n<!-- effects_a={} effects_b={} -->"
        )

        report = evaluate_artifacts(self.story_tree, self.md)

        self.assertTrue(report["checks"]["effects"], report["errors"])
        self.assertNotIn("effects 不合规", "；".join(report["warnings"]))

    def test_modular_report_counts_original_nodes_and_original_endings(self):
        self.story_tree["branch_type"] = "modular"
        self.story_tree["variable_defs"] = []
        self.story_tree["nodes"]["ending_a"]["is_original"] = True
        self.story_tree["nodes"]["ending_a"]["title"] = "原著结局"
        self.md["ending_a"] = "# 原著结局\n\n" + ("沈月回到故乡，案件终于收束。" * 40)

        report = evaluate_artifacts(self.story_tree, self.md)

        self.assertEqual(2, report["stats"]["original_node_count"])
        self.assertEqual(1, report["stats"]["ending_count"])
        self.assertEqual(0, report["stats"]["generated_node_count"])

    def test_modular_clue_flags_reject_unknown_or_unsourced_fact_types(self):
        self.story_tree["branch_type"] = "modular"
        self.story_tree["variable_defs"] = []
        self.md["chapter_1"] += (
            "\n<!-- effects_a={} effects_b={} "
            "clue_flags_a={'x': {'source_type': 'invented', 'source_excerpt': '', 'category': 'physical'}} "
            "clue_flags_b={'y': {'source_type': 'source_fact', 'source_excerpt': '', 'category': 'testimony'}} -->"
        )

        report = evaluate_artifacts(self.story_tree, self.md)

        self.assertFalse(report["checks"]["clue_flags"])
        self.assertEqual(2, report["stats"]["invalid_clue_flag_count"])

    def test_transition_drop_rate_warns_when_above_warning_line_but_within_release_limit(self):
        self.story_tree["branch_type"] = "modular"
        self.story_tree["variable_defs"] = []
        self.story_tree["metrics"] = {
            "soft_branches": {
                "total": 20,
                "transition_conflicts": 2,
                "transition_repairs": 2,
                "transition_dropped": 2,
            }
        }

        report = quality.validate_chapter_branch_release(self.story_tree, self.md)

        self.assertTrue(report["checks"]["soft_branch_transition_quality"])
        self.assertIn("衔接失败/删除率", "；".join(report["warnings"]))
        self.assertEqual(0.1, report["stats"]["soft_branch_transition_drop_rate"])

    def test_transition_drop_rate_40_percent_is_allowed(self):
        self.story_tree["branch_type"] = "modular"
        self.story_tree["variable_defs"] = []
        self.story_tree["metrics"] = {
            "soft_branches": {
                "total": 20,
                "transition_conflicts": 8,
                "transition_repairs": 8,
                "transition_dropped": 8,
            }
        }

        report = quality.validate_chapter_branch_release(self.story_tree, self.md)

        self.assertTrue(report["checks"]["soft_branch_transition_quality"])
        self.assertNotIn("衔接失败/删除率", "；".join(report["errors"]))
        self.assertEqual(0.4, report["stats"]["soft_branch_transition_drop_rate"])
        self.assertEqual(
            0.65,
            report["thresholds"]["soft_branch_transition_drop_limit"],
        )

    def test_transition_drop_rate_at_65_percent_is_allowed_but_above_it_blocks(self):
        self.story_tree["branch_type"] = "modular"
        self.story_tree["variable_defs"] = []
        self.story_tree["metrics"] = {
            "soft_branches": {
                "total": 20,
                "transition_conflicts": 13,
                "transition_repairs": 13,
                "transition_dropped": 13,
            }
        }

        report = quality.validate_chapter_branch_release(self.story_tree, self.md)
        self.assertTrue(report["checks"]["soft_branch_transition_quality"])
        self.assertNotIn("超过发布上限", "；".join(report["warnings"]))

        self.story_tree["metrics"]["soft_branches"]["transition_dropped"] = 14
        report = quality.validate_chapter_branch_release(self.story_tree, self.md)
        self.assertFalse(report["checks"]["soft_branch_transition_quality"])
        self.assertTrue(report["passed"], report)
        self.assertIn("超过发布上限 65%", "；".join(report["warnings"]))

    def test_transition_drop_limit_is_shared_by_all_chapter_branch_modes(self):
        for branch_type in ("default", "death_corridor", "romance", "fantasy", "modular"):
            with self.subTest(branch_type=branch_type):
                self.story_tree["branch_type"] = branch_type
                self.story_tree["variable_defs"] = []
                self.story_tree["metrics"] = {
                    "soft_branches": {
                        "total": 10,
                        "transition_dropped": 6,
                    }
                }
                report = quality.validate_chapter_branch_release(self.story_tree, self.md)
                self.assertTrue(report["checks"]["soft_branch_transition_quality"])

    def test_modular_view_and_ask_button_density_reduces_quality_score(self):
        self.story_tree["branch_type"] = "modular"
        self.story_tree["variable_defs"] = []
        content = (
            "# 第一章\n\n" + ("李莲花在案发现场继续调查。" * 30)
            + "\n▶ 选项A：查看现场\nA路\n▶ 选项B：询问证人\nB路"
            + "\n<!-- effects_a={} effects_b={} -->"
            + "\n▶ 选项A：查看门窗\nA路\n▶ 选项B：询问守卫\nB路"
            + "\n<!-- effects_a={} effects_b={} -->"
        )
        self.md["chapter_1"] = content

        report = quality.validate_chapter_branch_release(self.story_tree, self.md)

        self.assertEqual(1.0, report["stats"]["modular_view_ask_button_ratio"])
        self.assertFalse(report["checks"]["modular_button_diversity"])
        self.assertTrue(report["passed"], report)
        self.assertIn("按钮模板集中度", "；".join(report["warnings"]))

    def test_saved_effects_must_reach_every_ending_threshold(self):
        self.story_tree["variable_defs"][0]["threshold"] = 55

        report = evaluate_artifacts(self.story_tree, self.md)

        self.assertTrue(report["passed"], report)
        self.assertFalse(report["checks"]["route_reachability"])
        self.assertIn("真实 effects 不可达", "；".join(report["warnings"]))

    def test_effect_validation_uses_generation_cap_snapshot_after_threshold_calibration(self):
        self.story_tree["variable_defs"][0]["threshold"] = 51
        self.story_tree["effect_caps"] = {"courage": 4}
        self.md["ending_a"] = (
            "# 新结局\n\n"
            + ("夜雨敲在窗上，沈月终于决定面对真相。" * 30)
            + "\n<!-- effects_a={'courage': 4} effects_b={'courage': -4} -->"
        )

        report = evaluate_artifacts(self.story_tree, self.md)

        self.assertTrue(report["passed"], report["errors"])

    def test_named_romance_choice_must_give_named_character_positive_effect(self):
        self.story_tree["branch_type"] = "romance"
        self.story_tree["variable_defs"] = [
            {"key": "affection_liufuyi", "init": 50, "threshold": 54},
            {"key": "affection_musheng", "init": 50, "threshold": 54},
        ]
        self.story_tree["nodes"]["ending_a"]["title"] = "结局·柳拂衣线"
        self.story_tree["nodes"]["ending_b"] = {
            "title": "结局·慕声线",
            "children": [],
            "parent_ids": ["chapter_1"],
            "is_original": False,
            "summary": "慕声结局",
            "terminal_states": {"沈月": {"alive": True}},
        }
        self.story_tree["nodes"]["chapter_1"]["children"].append(
            {"child_id": "ending_b"}
        )
        self.story_tree["branch_thresholds"] = {
            "ending_a": {"variable": "affection_liufuyi", "threshold": 54},
            "ending_b": {"variable": "affection_musheng", "threshold": 54},
        }
        prose = "雨夜里，她终于作出了选择。" * 40
        self.md["chapter_1"] = (
            "# 第一章\n\n"
            + prose
            + "\n▶ 选项A：我先信柳拂衣\nA路\n"
            + "▶ 选项B：我偏想慕声\nB路\n"
            + "<!-- effects_a={'affection_musheng': 4} "
            + "effects_b={'affection_musheng': -4} -->"
        )
        self.md["ending_b"] = "# 慕声结局\n\n" + prose

        report = evaluate_artifacts(self.story_tree, self.md)

        self.assertTrue(report["passed"], report)
        self.assertFalse(report["checks"]["romance_effect_alignment"])
        self.assertIn("标题点名", "；".join(report["warnings"]))

    def test_should_not_approach_named_character_is_a_negative_romance_choice(self):
        self.story_tree["branch_type"] = "romance"
        self.story_tree["variable_defs"] = [
            {"key": "affection_liufuyi", "init": 50, "threshold": 54},
        ]
        self.story_tree["nodes"]["ending_a"]["title"] = "结局·柳拂衣线"
        self.story_tree["branch_thresholds"] = {
            "ending_a": {"variable": "affection_liufuyi", "threshold": 54},
        }
        prose = "雨夜里，她终于作出了选择。" * 40
        self.md["chapter_1"] = (
            "# 第一章\n\n"
            + prose
            + "\n▶ 选项A：我先信柳拂衣\nA路\n"
            + "▶ 选项B：我不该靠近柳拂衣\nB路\n"
            + "<!-- effects_a={'affection_liufuyi': 4} "
            + "effects_b={'affection_liufuyi': -4} -->"
        )

        report = evaluate_artifacts(self.story_tree, self.md)

        self.assertTrue(report["checks"]["romance_effect_alignment"], report["errors"])

    def test_courage_ending_requires_verifiable_courage_legacy(self):
        self.story_tree["branch_thresholds"] = {
            "ending_a": {"variable": "courage", "threshold": 53},
        }

        report = evaluate_artifacts(self.story_tree, self.md)

        self.assertTrue(report["checks"]["ending_variable_legacy"])
        self.assertEqual(
            1,
            report["stats"]["ending_legacy_channels"].get("manual_review", 0),
        )

    def test_courage_ending_passes_with_signed_letter_legacy(self):
        self.story_tree["branch_thresholds"] = {
            "ending_a": {"variable": "courage", "threshold": 53},
        }
        self.md["ending_a"] += (
            "\n沈月生前留下署名信，收信人核验笔迹后，才决定公开真相。"
        )

        report = evaluate_artifacts(self.story_tree, self.md)

        self.assertTrue(report["checks"]["ending_variable_legacy"], report["errors"])

    def test_self_worth_ending_requires_clarification_and_public_record(self):
        self.story_tree["variable_defs"] = [
            {"key": "self_worth", "init": 50, "threshold": 53},
        ]
        self.story_tree["branch_thresholds"] = {
            "ending_a": {"variable": "self_worth", "threshold": 53},
        }
        self.md["ending_a"] = self.md["ending_a"].replace("courage", "self_worth")
        self.md["ending_a"] += "\n沈月主动澄清身世和流言，但没有留下任何公开记录。"

        report = evaluate_artifacts(self.story_tree, self.md)

        self.assertTrue(report["checks"]["ending_variable_legacy"])
        self.assertEqual(
            1,
            report["stats"]["ending_legacy_channels"].get("manual_review", 0),
        )

    def test_self_worth_ending_passes_with_school_testimony(self):
        self.story_tree["variable_defs"] = [
            {"key": "self_worth", "init": 50, "threshold": 53},
        ]
        self.story_tree["branch_thresholds"] = {
            "ending_a": {"variable": "self_worth", "threshold": 53},
        }
        self.md["ending_a"] = self.md["ending_a"].replace("courage", "self_worth")
        self.md["ending_a"] += "\n沈月主动澄清身世与流言，并留下校方证词供众人核验。"

        report = evaluate_artifacts(self.story_tree, self.md)

        self.assertTrue(report["checks"]["ending_variable_legacy"], report["errors"])

    def test_self_worth_ending_accepts_structured_verifiable_legacy(self):
        self.story_tree["variable_defs"] = [
            {"key": "self_worth", "init": 50, "threshold": 53},
        ]
        self.story_tree["branch_thresholds"] = {
            "ending_a": {"variable": "self_worth", "threshold": 53},
        }
        self.story_tree["nodes"]["ending_a"]["legacy_evidence"] = [
            {
                "variable": "self_worth",
                "evidence_type": "official_record",
                "artifact_name": "公开说明与校方记录",
                "creator": "沈月",
                "action": "主动公开澄清",
                "verifier": "校方",
                "verification_method": "核对档案并盖章",
                "story_effect": "恢复名誉并推动学校撤销错误处分",
            }
        ]
        self.md["ending_a"] = self.md["ending_a"].replace("courage", "self_worth")

        report = evaluate_artifacts(self.story_tree, self.md)

        self.assertTrue(report["checks"]["ending_variable_legacy"], report["errors"])

    def test_infer_self_worth_legacy_from_signed_note_and_school_archive(self):
        flags = quality.infer_ending_legacy_flags(
            "self_worth",
            "江渡留下亲笔说明信，学校处理记录、档案章和老师便条都可供核验。",
        )

        self.assertTrue(flags["signed_clarification"])
        self.assertTrue(flags["school_record_preserved"])

    def test_ancient_courage_route_accepts_blood_letter_and_official_payoff(self):
        assessment = quality.assess_ending_variable_legacy(
            "courage",
            (
                "小柳持玉佩血书闯入苏府求援，管事凭玉佩和血书印证了她的身份。"
                "苏府因此派人救回苏勉，县衙也依据供词重审旧案。"
            ),
            ui_mode="Ancient",
        )

        self.assertTrue(assessment["passed"], assessment)
        self.assertEqual("manual_review", assessment["channel"])
        self.assertEqual("review_required", assessment["status"])

    def test_ancient_self_worth_route_accepts_contract_ledger_and_agency_payoff(self):
        assessment = quality.assess_ending_variable_legacy(
            "self_worth",
            (
                "小柳拒绝再被母亲卖作体面，当众签下供酱契书，又在账本上署下曹柳儿。"
                "契书由掌柜按印留档，她自收银钱、立下新规，最终成为曹记糟坊东家。"
            ),
            ui_mode="Ancient",
        )

        self.assertTrue(assessment["passed"], assessment)
        self.assertEqual("manual_review", assessment["channel"])
        self.assertEqual("review_required", assessment["status"])

    def test_structured_legacy_requires_the_complete_core_evidence_chain(self):
        incomplete = quality.assess_ending_variable_legacy(
            "courage",
            "她把一封信放在桌上，随后离开。",
            legacy_evidence=[
                {
                    "variable": "courage",
                    "evidence_type": "signed_message",
                    "artifact_name": "亲笔信",
                    "creator": "沈月",
                    "action": "主动写信",
                }
            ],
        )

        self.assertFalse(incomplete["passed"])
        self.assertEqual("repairable", incomplete["status"])
        self.assertIn("verification", incomplete["missing"])
        self.assertIn("story_effect", incomplete["missing"])

    def test_semantic_review_is_an_or_fallback_not_an_extra_required_gate(self):
        calls = []

        def reviewer(payload):
            calls.append(payload)
            return {
                "passed": True,
                "confidence": 0.93,
                "core_chain": {
                    "evidence_exists": True,
                    "protagonist_origin": True,
                    "verifiable": True,
                    "causal_payoff": True,
                },
                "evidence": {
                    "variable": "courage",
                    "evidence_type": "ritual_token",
                    "artifact_name": "鱼灯暗记",
                    "creator": "小柳",
                    "action": "主动留下暗记",
                    "verifier": "引魂人",
                    "verification_method": "按旧约核对灯纹",
                    "story_effect": "据此打开救援路线",
                    "evidence_quote": "小柳依旧约留下鱼灯暗记",
                },
            }

        assessment = quality.assess_ending_variable_legacy(
            "courage",
            "小柳依旧约留下鱼灯暗记，引魂人看懂后改道相救。",
            ui_mode="Ancient",
            semantic_reviewer=reviewer,
        )

        self.assertTrue(assessment["passed"], assessment)
        self.assertEqual("passed", assessment["status"])
        self.assertEqual("semantic_review", assessment["channel"])
        self.assertEqual(1, len(calls))

    def test_semantic_review_cannot_pass_with_a_quote_absent_from_the_prose(self):
        assessment = quality.assess_ending_variable_legacy(
            "courage",
            "沈月独自离开了院子，没有留下任何信息。",
            semantic_review={
                "passed": True,
                "confidence": 0.95,
                "core_chain": {
                    "evidence_exists": True,
                    "protagonist_origin": True,
                    "verifiable": True,
                    "causal_payoff": True,
                },
                "evidence": {
                    "artifact_name": "不存在的密信",
                    "evidence_quote": "沈月留下密信让官府查案",
                },
            },
        )

        self.assertFalse(assessment["passed"])
        self.assertEqual("failed", assessment["status"])
        self.assertIn("evidence_quote", assessment["missing"])

    def test_semantic_reviewer_failure_becomes_a_controlled_failed_assessment(self):
        def reviewer(_payload):
            raise TimeoutError("gateway timeout")

        assessment = quality.assess_ending_variable_legacy(
            "courage",
            "她独自离开，没有留下可验证信息。",
            semantic_reviewer=reviewer,
        )

        self.assertFalse(assessment["passed"])
        self.assertEqual("failed", assessment["status"])
        self.assertEqual("semantic_review", assessment["channel"])
        self.assertIn("gateway timeout", assessment["reason"])

    def test_malformed_semantic_review_confidence_cannot_crash_the_gate(self):
        assessment = quality.assess_ending_variable_legacy(
            "courage",
            "她独自离开，没有留下可验证信息。",
            semantic_review={
                "passed": True,
                "confidence": "high",
                "core_chain": {},
                "evidence": {},
            },
        )

        self.assertFalse(assessment["passed"])
        self.assertEqual("failed", assessment["status"])

    def test_string_false_values_from_semantic_review_are_not_treated_as_true(self):
        assessment = quality.assess_ending_variable_legacy(
            "courage",
            "沈月留下密信，但无人核验，也没有改变后续行动。",
            semantic_review={
                "passed": "false",
                "confidence": 0.95,
                "core_chain": {
                    "evidence_exists": "true",
                    "protagonist_origin": "true",
                    "verifiable": "false",
                    "causal_payoff": "false",
                },
                "evidence": {
                    "evidence_quote": "沈月留下密信",
                },
            },
        )

        self.assertFalse(assessment["passed"])
        self.assertFalse(assessment["core_chain"]["verifiable"])
        self.assertFalse(assessment["core_chain"]["causal_payoff"])

    def test_semantic_review_overrides_a_keyword_channel_pass(self):
        calls = []

        def reviewer(_payload):
            calls.append(True)
            return {
                "passed": False,
                "confidence": 0.95,
                "reason": "关键词虽然出现，但没有形成主角主动创建、核验并推动行动的完整证据链",
                "core_chain": {
                    "evidence_exists": True,
                    "protagonist_origin": False,
                    "verifiable": True,
                    "causal_payoff": False,
                },
                "evidence": {"evidence_quote": "沈月留下署名信"},
            }

        assessment = quality.assess_ending_variable_legacy(
            "courage",
            "沈月留下署名信，收信人核验笔迹后决定公开真相。",
            semantic_reviewer=reviewer,
        )

        self.assertFalse(assessment["passed"], assessment)
        self.assertEqual("semantic_review", assessment["channel"])
        self.assertEqual(1, len(calls))

    def test_current_semantic_reviewer_cannot_be_bypassed_by_stale_review(self):
        calls = []

        def reviewer(_payload):
            calls.append(True)
            return {
                "passed": False,
                "confidence": 0.95,
                "reason": "当前正文没有形成可验证因果链",
                "core_chain": {
                    "evidence_exists": True,
                    "protagonist_origin": False,
                    "verifiable": False,
                    "causal_payoff": False,
                },
                "evidence": {"evidence_quote": "沈月留下署名信"},
            }

        assessment = quality.assess_ending_variable_legacy(
            "courage",
            "沈月留下署名信，但收信人没有采取任何行动。",
            semantic_review={
                "passed": True,
                "confidence": 0.99,
                "core_chain": {
                    "evidence_exists": True,
                    "protagonist_origin": True,
                    "verifiable": True,
                    "causal_payoff": True,
                },
                "evidence": {"evidence_quote": "沈月留下署名信"},
            },
            semantic_reviewer=reviewer,
        )

        self.assertFalse(assessment["passed"])
        self.assertEqual("semantic_review", assessment["channel"])
        self.assertEqual(1, len(calls))

    def test_release_gate_uses_story_ui_mode_for_ancient_evidence_mapping(self):
        self.story_tree["UI_mode"] = "Ancient"
        self.story_tree["branch_thresholds"] = {
            "ending_a": {"variable": "courage", "threshold": 53},
        }
        self.md["ending_a"] += (
            "\n沈月持血书和家印求援，官差核验印记后据此重审旧案并救出证人。"
        )

        report = evaluate_artifacts(self.story_tree, self.md)

        self.assertTrue(report["checks"]["ending_variable_legacy"], report["errors"])
        self.assertEqual(
            1,
            report["stats"]["ending_legacy_channels"].get("manual_review", 0),
        )

    def test_legacy_flags_alone_cannot_bypass_origin_and_causal_payoff(self):
        assessment = quality.assess_ending_variable_legacy(
            "self_worth",
            "众人照常散去，故事没有发生任何改变。",
            legacy_flags={
                "signed_clarification": True,
                "school_record_preserved": True,
            },
        )

        self.assertTrue(assessment["passed"])
        self.assertEqual("manual_review", assessment["channel"])
        self.assertEqual("review_required", assessment["status"])

    def test_generated_title_and_prose_completeness_are_warnings(self):
        self.story_tree["nodes"]["ending_a"]["title"] = (
            "第6章（What-If：接受劝说，提前押注极限武馆自）"
        )
        self.md["ending_a"] = (
            "# 残缺标题\n\n"
            + "夜雨落在窗棂上，沈月终于决定面对真相。" * 30
            + "\n\n“小峰啊。”罗洪国说道，“还有几天就高考了，心态放平。"
        )

        report = evaluate_artifacts(self.story_tree, self.md)

        self.assertTrue(report["passed"], report)
        joined = "；".join(report["warnings"])
        self.assertIn("标题", joined)
        self.assertIn("引号", joined)
        self.assertFalse(report["checks"]["content_completeness"])


class ChapterBranchReleaseGateTests(ChapterBranchQualityTests):
    def setUp(self):
        super().setUp()
        self.plan = {
            "nodes": {
                "chapter_topo": {
                    "status": "completed",
                    "real_id": "chapter_1",
                },
                "ending_topo": {
                    "status": "completed",
                    "real_id": "ending_a",
                },
            },
            "topology_result": {
                "topology": [
                    {
                        "node_id": "chapter_topo",
                        "type": "chapter",
                        "is_original": True,
                        "source_chapter_idx": 0,
                    },
                    {
                        "node_id": "ending_topo",
                        "type": "ending",
                        "is_original": False,
                    },
                ]
            },
        }

    def test_valid_release_passes_plan_and_score_gate(self):
        report = quality.validate_chapter_branch_release(
            self.story_tree,
            self.md,
            plan=self.plan,
            thresholds={"min_score": 90, "min_endings": 1},
        )

        self.assertTrue(report["passed"], report["errors"])
        self.assertGreaterEqual(report["score"], 90)
        self.assertTrue(report["checks"]["plan_consistency"])

    def test_pending_plan_node_and_json_set_mismatch_are_warnings(self):
        self.plan["nodes"]["ending_topo"]["status"] = "pending"
        self.plan["nodes"]["ghost_topo"] = {
            "status": "completed",
            "real_id": "ghost",
        }

        report = quality.validate_chapter_branch_release(
            self.story_tree,
            self.md,
            plan=self.plan,
        )

        self.assertTrue(report["passed"], report)
        self.assertFalse(report["checks"]["plan_consistency"])
        self.assertIn("pending", "；".join(report["warnings"]))
        self.assertIn("ghost", "；".join(report["warnings"]))

    def test_non_ending_leaf_and_duplicate_choice_text_block_release(self):
        tree = deepcopy(self.story_tree)
        tree["nodes"]["chapter_1"]["children"] = [
            {"child_id": "ending_a", "choice_text": "继续"},
            {"child_id": "ending_a", "choice_text": "继续"},
        ]
        tree["nodes"]["chapter_leaf"] = {
            "title": "断裂章节",
            "content_file": "chapter_leaf.md",
            "children": [],
            "parent_ids": ["chapter_1"],
            "is_original": True,
            "summary": "没有通向任何结局。",
            "terminal_states": {},
            "generated_at": "2026-08-11T01:02:00",
        }
        md = dict(self.md, chapter_leaf="# 断裂章节\n\n正文")
        self.plan["nodes"]["leaf_topo"] = {
            "status": "completed",
            "real_id": "chapter_leaf",
        }
        self.plan["topology_result"]["topology"].insert(
            1,
            {
                "node_id": "leaf_topo",
                "type": "chapter",
                "is_original": True,
                "source_chapter_idx": 1,
            },
        )

        report = quality.validate_chapter_branch_release(tree, md, plan=self.plan)

        self.assertTrue(report["passed"], report)
        joined = "；".join(report["warnings"])
        self.assertIn("非结局节点", joined)
        self.assertIn("选项文本重复", joined)

    def test_terminal_whatif_leaf_is_allowed_without_children(self):
        tree = deepcopy(self.story_tree)
        tree["nodes"]["ending_a"]["terminal_branch"] = True
        tree["nodes"]["ending_a"]["convergence_status"] = "terminal"
        tree["nodes"]["ending_a"]["children"] = []
        self.plan["topology_result"]["topology"][1].update(
            {
                "type": "chapter",
                "terminal_branch": True,
                "convergence_status": "terminal",
            }
        )

        report = quality.validate_chapter_branch_release(tree, self.md, plan=self.plan)

        self.assertNotIn("没有有效 children", "；".join(report["errors"]))

    def test_excessive_terminal_whatif_ratio_is_warning(self):
        tree = deepcopy(self.story_tree)
        for index in range(4):
            node_id = f"whatif_{index}"
            tree["nodes"][node_id] = {
                "id": node_id,
                "title": f"What-If {index}",
                "content_file": f"{node_id}.md",
                "summary": "分支摘要",
                "generated_at": "2026-08-11T01:02:00",
                "children": [],
                "is_original": False,
                "terminal_states": {},
                "terminal_branch": index < 2,
                "convergence_status": "terminal" if index < 2 else "converged",
            }
            self.plan["nodes"][f"{node_id}_topo"] = {
                "status": "completed",
                "real_id": node_id,
            }
            self.plan["topology_result"]["topology"].append(
                {
                    "node_id": f"{node_id}_topo",
                    "type": "chapter",
                    "is_original": False,
                    "terminal_branch": index < 2,
                    "convergence_status": "terminal" if index < 2 else "converged",
                }
            )
        md = dict(self.md)
        md.update({f"whatif_{index}": "正文" * 400 for index in range(4)})

        report = quality.validate_chapter_branch_release(tree, md, plan=self.plan)

        self.assertFalse(report["checks"]["whatif_convergence"])
        self.assertTrue(report["passed"], report)
        self.assertIn("终止比例", "；".join(report["warnings"]))

    def test_terminal_whatif_ratio_of_exactly_forty_percent_is_allowed(self):
        tree = deepcopy(self.story_tree)
        md = dict(self.md)
        for index in range(5):
            node_id = f"whatif_boundary_{index}"
            is_terminal = index < 2
            tree["nodes"][node_id] = {
                "id": node_id,
                "title": f"What-If 边界 {index}",
                "content_file": f"{node_id}.md",
                "summary": "分支摘要",
                "generated_at": "2026-08-11T01:02:00",
                "children": [] if is_terminal else [{"child_id": "ending_a", "choice_text": "继续"}],
                "is_original": False,
                "terminal_states": {},
                "terminal_branch": is_terminal,
                "convergence_status": "terminal" if is_terminal else "converged",
            }
            self.plan["nodes"][f"{node_id}_topo"] = {
                "status": "completed",
                "real_id": node_id,
            }
            self.plan["topology_result"]["topology"].append(
                {
                    "node_id": f"{node_id}_topo",
                    "type": "chapter",
                    "is_original": False,
                    "terminal_branch": is_terminal,
                    "convergence_status": "terminal" if is_terminal else "converged",
                    "transition_review": {"compatible": True, "score": 8, "issues": []},
                }
            )
            md[node_id] = "正文完整结束。" * 100

        report = quality.validate_chapter_branch_release(tree, md, plan=self.plan)

        self.assertTrue(report["checks"]["whatif_convergence"], report["errors"])
        self.assertNotIn("超过发布上限", "；".join(report["warnings"]))

    def test_nonterminal_whatif_transition_review_failure_is_warning(self):
        tree = deepcopy(self.story_tree)
        tree["nodes"]["whatif"] = {
            "title": "第1章（What-If：沈月选择坦白）",
            "content_file": "whatif.md",
            "children": [{"child_id": "ending_a", "choice_text": "继续"}],
            "parent_ids": ["chapter_1"],
            "is_original": False,
            "summary": "沈月选择坦白后回到主线。",
            "terminal_states": {"沈月": {"alive": True}},
            "generated_at": "2026-08-11T01:00:30",
        }
        tree["nodes"]["chapter_1"]["children"].append(
            {"child_id": "whatif", "choice_text": "若沈月选择坦白"}
        )
        md = dict(self.md, whatif="# What-If\n\n" + "沈月在雨中说出真相。" * 60)
        self.plan["nodes"]["whatif_topo"] = {
            "status": "completed",
            "real_id": "whatif",
        }
        self.plan["topology_result"]["topology"].insert(
            1,
            {
                "node_id": "whatif_topo",
                "type": "chapter",
                "is_original": False,
                "next": ["ending_topo"],
                "transition_review": {
                    "compatible": False,
                    "score": 5,
                    "issues": ["地点与下游开头冲突"],
                },
            },
        )

        report = quality.validate_chapter_branch_release(tree, md, plan=self.plan)

        self.assertTrue(report["passed"], report)
        self.assertFalse(report["checks"]["whatif_actual_transition"])
        self.assertIn("实际衔接复检", "；".join(report["warnings"]))

    def test_nonterminal_whatif_score_six_is_allowed_after_relaxation(self):
        tree = deepcopy(self.story_tree)
        tree["nodes"]["whatif"] = {
            "title": "第1章（What-If：沈月选择坦白）",
            "content_file": "whatif.md",
            "children": [{"child_id": "ending_a", "choice_text": "继续"}],
            "parent_ids": ["chapter_1"],
            "is_original": False,
            "summary": "沈月选择坦白后回到主线。",
            "terminal_states": {"沈月": {"alive": True}},
            "generated_at": "2026-08-11T01:00:30",
        }
        tree["nodes"]["chapter_1"]["children"].append(
            {"child_id": "whatif", "choice_text": "若沈月选择坦白"}
        )
        md = dict(self.md, whatif="# What-If\n\n" + "沈月在雨中说出真相。" * 60)
        self.plan["nodes"]["whatif_topo"] = {
            "status": "completed",
            "real_id": "whatif",
        }
        self.plan["topology_result"]["topology"].insert(
            1,
            {
                "node_id": "whatif_topo",
                "type": "chapter",
                "is_original": False,
                "next": ["ending_topo"],
                "transition_review": {
                    "compatible": True,
                    "score": 6,
                    "issues": ["存在可用一两句过渡解释的轻微地点差异"],
                },
            },
        )

        report = quality.validate_chapter_branch_release(tree, md, plan=self.plan)

        self.assertTrue(report["checks"]["whatif_actual_transition"], report["errors"])
        self.assertTrue(report["passed"], report["errors"])

    def test_nonterminal_whatif_bridge_warning_is_allowed_at_relaxed_score(self):
        tree = deepcopy(self.story_tree)
        tree["nodes"]["whatif"] = {
            "title": "第1章（What-If：沈月选择坦白）",
            "content_file": "whatif.md",
            "children": [{"child_id": "ending_a", "choice_text": "继续"}],
            "parent_ids": ["chapter_1"],
            "is_original": False,
            "summary": "沈月选择坦白后回到主线。",
            "terminal_states": {"沈月": {"alive": True}},
            "generated_at": "2026-08-11T01:00:30",
        }
        tree["nodes"]["chapter_1"]["children"].append(
            {"child_id": "whatif", "choice_text": "若沈月选择坦白"}
        )
        md = dict(self.md, whatif="# What-If\n\n" + "沈月在雨中说出真相。" * 60)
        self.plan["nodes"]["whatif_topo"] = {
            "status": "completed",
            "real_id": "whatif",
        }
        self.plan["topology_result"]["topology"].insert(
            1,
            {
                "node_id": "whatif_topo",
                "type": "chapter",
                "is_original": False,
                "next": ["ending_topo"],
                "transition_review": {
                    "compatible": False,
                    "accepted_with_warning": True,
                    "hard_conflict": False,
                    "score": 6,
                    "issues": ["需要一两句过渡解释"],
                },
            },
        )

        report = quality.validate_chapter_branch_release(tree, md, plan=self.plan)

        self.assertTrue(report["checks"]["whatif_actual_transition"], report["errors"])
        self.assertTrue(report["passed"], report["errors"])

    def test_nonterminal_whatif_hard_conflict_is_warning_at_relaxed_score(self):
        tree = deepcopy(self.story_tree)
        tree["nodes"]["whatif"] = {
            "title": "第1章（What-If：沈月选择坦白）",
            "content_file": "whatif.md",
            "children": [{"child_id": "ending_a", "choice_text": "继续"}],
            "parent_ids": ["chapter_1"],
            "is_original": False,
            "summary": "沈月选择坦白后回到主线。",
            "terminal_states": {"沈月": {"alive": True}},
            "generated_at": "2026-08-11T01:00:30",
        }
        tree["nodes"]["chapter_1"]["children"].append(
            {"child_id": "whatif", "choice_text": "若沈月选择坦白"}
        )
        md = dict(self.md, whatif="# What-If\n\n" + "沈月在雨中说出真相。" * 60)
        self.plan["nodes"]["whatif_topo"] = {
            "status": "completed",
            "real_id": "whatif",
        }
        self.plan["topology_result"]["topology"].insert(
            1,
            {
                "node_id": "whatif_topo",
                "type": "chapter",
                "is_original": False,
                "next": ["ending_topo"],
                "transition_review": {
                    "compatible": True,
                    "hard_conflict": True,
                    "score": 6,
                    "issues": ["已确认死亡角色在现实层复活"],
                },
            },
        )

        report = quality.validate_chapter_branch_release(tree, md, plan=self.plan)

        self.assertFalse(report["checks"]["whatif_actual_transition"])
        self.assertTrue(report["passed"], report)
        self.assertIn("实际衔接复检", "；".join(report["warnings"]))

    def test_quality_report_records_whatif_merge_and_repair_strategies(self):
        tree = deepcopy(self.story_tree)
        tree["nodes"]["whatif"] = {
            "title": "第1章（What-If：沈月选择坦白）",
            "content_file": "whatif.md",
            "children": [{"child_id": "ending_a", "choice_text": "继续"}],
            "parent_ids": ["chapter_1"],
            "is_original": False,
            "summary": "沈月完成旅行桥接后回到主线。",
            "terminal_states": {"沈月": {"alive": True}},
            "generated_at": "2026-08-11T01:00:30",
        }
        tree["nodes"]["chapter_1"]["children"].append(
            {"child_id": "whatif", "choice_text": "若沈月选择坦白"}
        )
        self.plan["nodes"]["whatif_topo"] = {
            "status": "completed",
            "real_id": "whatif",
        }
        self.plan["topology_result"]["topology"].insert(
            1,
            {
                "node_id": "whatif_topo",
                "type": "chapter",
                "is_original": False,
                "next": ["ending_topo"],
                "merge_policy": "bridge",
                "convergence_repair_strategy": "bridge_scene",
                "transition_review": {
                    "compatible": True,
                    "score": 9,
                    "issues": [],
                },
            },
        )
        md = dict(self.md, whatif="# What-If\n\n" + "沈月完成出发、到达和安顿。" * 80)

        report = quality.validate_chapter_branch_release(tree, md, plan=self.plan)

        self.assertEqual({"bridge": 1}, report["stats"]["whatif_merge_policies"])
        self.assertEqual(
            {"bridge_scene": 1},
            report["stats"]["whatif_repair_strategies"],
        )

    def test_empty_choice_is_allowed_only_when_target_is_ending(self):
        tree = deepcopy(self.story_tree)
        tree["nodes"]["chapter_1"]["children"] = [
            {"child_id": "ending_a", "choice_text": ""}
        ]
        report = quality.validate_chapter_branch_release(tree, self.md, plan=self.plan)

        self.assertNotIn("存在空选项文本", "；".join(report["errors"]))

    def test_empty_choice_to_leaf_ending_is_allowed_without_plan(self):
        tree = deepcopy(self.story_tree)
        tree["nodes"]["chapter_1"]["children"] = [
            {"child_id": "ending_a", "choice_text": ""}
        ]

        report = quality.validate_chapter_branch_release(tree, self.md)

        self.assertNotIn("存在空选项文本", "；".join(report["errors"]))

    def test_multiple_empty_automatic_ending_routes_are_not_duplicates(self):
        tree = deepcopy(self.story_tree)
        tree["nodes"]["ending_b"] = deepcopy(tree["nodes"]["ending_a"])
        tree["nodes"]["ending_b"]["id"] = "ending_b"
        tree["nodes"]["ending_b"]["title"] = "第二结局"
        tree["nodes"]["chapter_1"]["children"] = [
            {"child_id": "ending_a", "choice_text": ""},
            {"child_id": "ending_b", "choice_text": ""},
        ]
        self.plan["nodes"]["ending_b_topo"] = {
            "status": "completed",
            "real_id": "ending_b",
        }
        self.plan["topology_result"]["topology"].append(
            {"node_id": "ending_b_topo", "type": "ending", "next": []}
        )
        md = dict(self.md, ending_b=self.md["ending_a"] + "\n另一结局。")

        report = quality.validate_chapter_branch_release(tree, md, plan=self.plan)

        self.assertNotIn("选项文本重复：['']", "；".join(report["errors"]))

    def test_source_chapter_mapping_is_warning_when_not_unique_and_contiguous(self):
        self.plan["topology_result"]["topology"].insert(
            1,
            {
                "node_id": "chapter_duplicate",
                "type": "chapter",
                "is_original": True,
                "source_chapter_idx": 2,
            },
        )

        report = quality.validate_chapter_branch_release(
            self.story_tree,
            self.md,
            plan=self.plan,
        )

        self.assertTrue(report["passed"], report)
        self.assertIn("原著章节映射不连续", "；".join(report["warnings"]))

    def test_quality_score_below_configured_threshold_blocks_release(self):
        self.story_tree.pop("metrics")

        report = quality.validate_chapter_branch_release(
            self.story_tree,
            self.md,
            plan=self.plan,
            thresholds={"min_score": 99},
        )

        self.assertTrue(report["passed"], report)
        self.assertEqual(97, report["score"])
        self.assertEqual([], report["errors"])

    def test_default_release_quality_score_threshold_is_thirty(self):
        report = quality.validate_chapter_branch_release(
            self.story_tree,
            self.md,
            plan=self.plan,
        )

        self.assertEqual(30, report["thresholds"]["min_score"])

    def test_release_repair_directive_targets_node_specific_errors(self):
        report = {
            "errors": ["节点 ending_a 正文质量失败：正文末句截断"],
        }

        directive = quality.build_release_repair_directive(report, self.plan)

        self.assertEqual("targeted", directive["mode"])
        self.assertEqual(["ending_topo"], directive["target_node_ids"])

    def test_release_repair_directive_does_not_match_node_id_by_substring(self):
        self.plan["nodes"]["ending_topology"] = {
            "real_id": "node-ending-topology"
        }
        report = {
            "errors": ["节点 ending_topology 正文质量失败：正文末句截断"],
        }

        directive = quality.build_release_repair_directive(report, self.plan)

        self.assertEqual(["ending_topology"], directive["target_node_ids"])

    def test_release_repair_directive_uses_full_regeneration_for_route_failure(self):
        report = {
            "errors": ["变量 courage 不存在满足最终结局规则的真实选择路径"],
        }

        directive = quality.build_release_repair_directive(report, self.plan)

        self.assertEqual("full", directive["mode"])
        self.assertFalse(directive["rebuild_topology"])

    def test_release_repair_directive_does_not_regenerate_mode_contract_errors(self):
        report = {
            "errors": ["模式契约冲突：modular 无变量模式不要求非零 effects"],
        }

        directive = quality.build_release_repair_directive(report, self.plan)

        self.assertEqual("none", directive["mode"])
        self.assertEqual([], directive["target_node_ids"])

    def test_full_topology_fallback_is_warning(self):
        self.plan["topology_result"]["topology_generation"] = {
            "mode": "deterministic_fallback",
            "full_fallback": True,
            "fallback_branch_count": 2,
            "fallback_ratio": 1.0,
        }

        report = quality.validate_chapter_branch_release(
            self.story_tree,
            self.md,
            plan=self.plan,
        )

        self.assertTrue(report["passed"], report)
        self.assertIn("拓扑使用全量 fallback", "；".join(report["warnings"]))

    def test_excessive_partial_topology_fallback_is_warning(self):
        self.plan["topology_result"]["topology_generation"] = {
            "mode": "compact_specs",
            "full_fallback": False,
            "fallback_branch_count": 4,
            "fallback_ratio": 0.25,
        }

        report = quality.validate_chapter_branch_release(
            self.story_tree,
            self.md,
            plan=self.plan,
        )

        self.assertTrue(report["passed"], report)
        self.assertIn("fallback 比例", "；".join(report["warnings"]))

    def test_simple_soft_branch_fallback_rate_is_reported_as_warning(self):
        self.story_tree["metrics"]["soft_branches"] = {
            "total": 100,
            "simple_llm_fallback": 12,
            "hardcoded_fallback": 0,
        }

        report = quality.validate_chapter_branch_release(
            self.story_tree,
            self.md,
            plan=self.plan,
        )

        self.assertTrue(report["checks"]["soft_branch_fallback_quality"])
        self.assertIn("质朴版兜底率 12.0%", "；".join(report["warnings"]))
        self.assertEqual(0.12, report["stats"]["soft_branch_simple_fallback_rate"])

    def test_release_stats_use_cumulative_metrics_after_resume(self):
        self.story_tree["metrics"] = {
            "soft_branches": {
                "total": 1,
                "simple_llm_fallback": 1,
                "hardcoded_fallback": 0,
            },
            "cumulative_metrics": {
                "soft_branches": {
                    "total": 100,
                    "simple_llm_fallback": 12,
                    "hardcoded_fallback": 0,
                }
            },
        }

        report = quality.validate_chapter_branch_release(
            self.story_tree,
            self.md,
            plan=self.plan,
        )

        self.assertEqual(0.12, report["stats"]["soft_branch_simple_fallback_rate"])

    def test_soft_branch_stats_distinguish_eligible_flavor_generated_and_dropped(self):
        self.story_tree["metrics"]["soft_branches"] = {
            "theoretical_gap_count": 10,
            "eligible_gap_count": 8,
            "skipped_no_route_target": 2,
            "flavor_branch_count": 3,
            "generated_branch_count": 7,
            "validation_dropped": 1,
            "total": 8,
        }

        report = evaluate_artifacts(self.story_tree, self.md)

        stats = report["stats"]
        self.assertEqual(10, stats["soft_branch_theoretical_gap_count"])
        self.assertEqual(8, stats["soft_branch_eligible_gap_count"])
        self.assertEqual(2, stats["soft_branch_skipped_no_route_target_count"])
        self.assertEqual(3, stats["soft_branch_flavor_count"])
        self.assertEqual(1, stats["soft_branch_generated_count"])
        self.assertEqual(7, stats["soft_branch_cumulative_generated_count"])
        self.assertEqual(1, stats["soft_branch_validation_drop_count"])

    def test_soft_branch_report_separates_unique_output_from_attempt_counters(self):
        """Published stats must not present resumed-attempt counters as final output."""
        self.story_tree["metrics"] = {
            "soft_branches": {
                "total": 666,
                "generated_branch_count": 666,
            },
            "attempt_metrics": {
                "soft_branches": {
                    "total": 2,
                    "generated_branch_count": 2,
                },
            },
            "cumulative_metrics": {
                "soft_branches": {
                    "total": 666,
                    "generated_branch_count": 666,
                },
            },
        }

        report = evaluate_artifacts(self.story_tree, self.md)

        stats = report["stats"]
        self.assertEqual(1, stats["soft_branch_count"])
        self.assertEqual(1, stats["soft_branch_unique_output_count"])
        self.assertEqual(2, stats["soft_branch_attempt_processed_count"])
        self.assertEqual(2, stats["soft_branch_attempt_generated_count"])
        self.assertEqual(666, stats["soft_branch_cumulative_processed_count"])
        self.assertEqual(666, stats["soft_branch_cumulative_generated_count"])

    def test_evaluate_artifacts_preserves_published_quality_score(self):
        self.story_tree["quality_gate"] = {
            "passed": True,
            "score": 97,
            "thresholds": {"min_score": 75},
        }

        report = evaluate_artifacts(self.story_tree, self.md)

        self.assertEqual(97, report["score"])
        self.assertEqual({"min_score": 75}, report["thresholds"])

    def test_romance_flavor_marker_with_empty_effects_is_valid_and_counted(self):
        self.story_tree["branch_type"] = "romance"
        self.story_tree["variable_defs"] = []
        self.md["ending_a"] = "# 新结局\n\n" + ("结局正文。" * 80)
        self.md["chapter_1"] += (
            "\n▶ 选项A：我先把茶端稳\nA路正文\n"
            "▶ 选项B：我故意逗他笑\nB路正文\n"
            "<!-- effects_a={} effects_b={} branch_kind=flavor "
            "legacy_flags_a={} legacy_flags_b={} clue_flags_a={} clue_flags_b={} -->\n"
        )

        report = evaluate_artifacts(self.story_tree, self.md)

        self.assertTrue(report["passed"], report["errors"])
        self.assertEqual(1, report["stats"]["flavor_soft_branch_count"])
        self.assertEqual(0, report["stats"]["numeric_soft_branch_count"])

    def test_mixed_flavor_and_numeric_markers_in_one_node_are_counted_independently(self):
        self.story_tree["branch_type"] = "romance"
        self.story_tree["variable_defs"] = [
            {"key": "affection_liufuyi", "init": 50, "threshold": 53}
        ]
        self.md["chapter_1"] = (
            "# 第一章\n\n"
            "前置剧情。\n\n"
            "▶ 选项A：我先把茶端稳\nA路趣味正文\n"
            "▶ 选项B：我故意逗他笑\nB路趣味正文\n"
            "<!-- effects_a={} effects_b={} branch_kind=flavor -->\n\n"
            "▶ 选项A：我把窗户关上\nA路趣味正文\n"
            "▶ 选项B：我留一条缝\nB路趣正文\n"
            "<!-- effects_a={} effects_b={} branch_kind=flavor -->\n\n"
            "▶ 选项A：我回应柳拂衣\nA路数值正文\n"
            "▶ 选项B：我避开柳拂衣\nB路数值正文\n"
            "<!-- effects_a={'affection_liufuyi': 3} effects_b={'affection_liufuyi': -3} -->"
        )

        report = evaluate_artifacts(self.story_tree, self.md)

        self.assertEqual(4, report["stats"]["soft_branch_count"])
        self.assertEqual(2, report["stats"]["flavor_soft_branch_count"])
        self.assertEqual(2, report["stats"]["numeric_soft_branch_count"])

    def test_occasional_soft_branch_validation_drops_are_reported_not_blocked(self):
        self.story_tree["metrics"]["soft_branches"] = {
            "total": 100,
            "validation_dropped": 6,
        }

        report = quality.validate_chapter_branch_release(
            self.story_tree,
            self.md,
            plan=self.plan,
        )

        self.assertTrue(report["checks"]["soft_branch_validation_drop_quality"])
        self.assertIn("硬规则失败/删除率 6.0%", "；".join(report["warnings"]))
        self.assertEqual(0.06, report["stats"]["soft_branch_validation_drop_rate"])

    def test_hardcoded_soft_branch_fallback_rate_above_three_percent_is_warning(self):
        self.story_tree["metrics"]["soft_branches"] = {
            "total": 100,
            "simple_llm_fallback": 8,
            "hardcoded_fallback": 4,
        }

        report = quality.validate_chapter_branch_release(
            self.story_tree,
            self.md,
            plan=self.plan,
        )

        self.assertTrue(report["passed"], report)
        self.assertFalse(report["checks"]["soft_branch_fallback_quality"])
        self.assertIn("硬编码兜底率 4.0%", "；".join(report["warnings"]))


if __name__ == "__main__":
    unittest.main()
