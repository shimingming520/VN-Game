from copy import deepcopy
import importlib.util
import inspect
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


def _load_branch_designer():
    module_path = (
        Path(__file__).resolve().parents[2]
        / "services"
        / "story_planner"
        / "core"
        / "branch_designer.py"
    )
    llm_client_stub = types.ModuleType("services.story_planner.core.llm_client")
    llm_client_stub.LLMClient = object
    llm_client_stub.get_lang_instruction = lambda _language: ""
    package_stubs = {
        "services": types.ModuleType("services"),
        "services.story_planner": types.ModuleType("services.story_planner"),
        "services.story_planner.core": types.ModuleType("services.story_planner.core"),
        "services.story_planner.core.llm_client": llm_client_stub,
    }
    spec = importlib.util.spec_from_file_location(
        "branch_designer_under_test",
        module_path,
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, package_stubs):
        spec.loader.exec_module(module)
    return module


branch_designer = _load_branch_designer()


def _original_node(index, total, next_ids):
    return {
        "node_id": f"ch{index + 1}",
        "type": "ending" if index == total - 1 else "chapter",
        "title": f"第{index + 1}章",
        "is_original": True,
        "source_chapter_idx": index,
        "next": next_ids,
    }


class _StaticTopologyClient:
    def __init__(self, result):
        self.result = result
        self.calls = 0
        self.last_kwargs = {}

    def chat_completion_json(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        return deepcopy(self.result)


class _SequenceTopologyClient:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def chat_completion_json(self, **kwargs):
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self.results) - 1)
        return deepcopy(self.results[index])


class NonDefaultTopologyValidationTests(unittest.TestCase):
    def _design(self, branch_type, topology):
        result = {
            "marker": branch_type,
            "variable_defs": [],
            "topology": topology,
        }
        fake_client = _StaticTopologyClient(result)
        chapters = [f"chapter {i}" for i in range(len(topology))]
        # 非原著节点会使 topology 数量大于章节数，因此按原著节点数决定输入章节数。
        original_count = sum(1 for node in topology if node.get("is_original"))
        chapters = chapters[:original_count]
        summaries = [f"summary {i}" for i in range(original_count)]

        with (
            patch.object(branch_designer, "LLMClient", return_value=fake_client),
            patch("builtins.print"),
        ):
            designed = branch_designer.design_story_topology(
                chapters=chapters,
                summaries=summaries,
                provider="openai",
                branch_type=branch_type,
            )

        self.assertEqual(
            designed.get("marker"),
            branch_type,
            f"有效的 {branch_type} 拓扑不应被异常替换为 fallback",
        )
        self.assertEqual(fake_client.calls, 1)

    def test_death_corridor_accepts_valid_topology(self):
        total = 5
        topology = [
            _original_node(0, total, ["ch2"]),
            _original_node(1, total, ["ch3", "bad_end_1"]),
            _original_node(2, total, ["ch4"]),
            _original_node(3, total, ["ch5"]),
            _original_node(4, total, []),
            {
                "node_id": "bad_end_1",
                "type": "bad_ending",
                "title": "错误选择",
                "is_original": False,
                "source_chapter_idx": None,
                "next": [],
            },
        ]

        self._design("death_corridor", topology)

    def test_fantasy_accepts_valid_diamond_topology(self):
        fake_client = _StaticTopologyClient(
            {
                "whatif_specs": [
                    {
                        "pivot_chapter": 2,
                        "decision": "是否离开师门",
                        "alternative_choice": "留下直面冲突",
                        "consequence": "失去退路但保住同伴",
                        "choice_text": "留下直面师门冲突",
                    }
                ]
            }
        )
        with (
            patch.object(branch_designer, "LLMClient", return_value=fake_client),
            patch("builtins.print"),
        ):
            result = branch_designer.design_story_topology(
                chapters=["a", "b", "c", "d", "e"],
                summaries=["a", "b", "c", "d", "e"],
                provider="openai",
                branch_type="fantasy",
            )

        self.assertEqual("compact_specs", result["topology_generation"]["mode"])
        self.assertEqual(1, fake_client.calls)
        self.assertEqual([], result["variable_defs"])

    def test_modular_accepts_valid_linear_topology(self):
        total = 4
        topology = [
            _original_node(0, total, ["ch2"]),
            _original_node(1, total, ["ch3"]),
            _original_node(2, total, ["ch4"]),
            _original_node(3, total, []),
        ]

        self._design("modular", topology)

    def test_invalid_modular_response_uses_modular_fallback(self):
        invalid_result = {
            "variable_defs": [],
            "topology": [
                _original_node(0, 3, ["missing"]),
                _original_node(1, 3, ["ch3"]),
                _original_node(2, 3, []),
            ],
        }
        fake_client = _StaticTopologyClient(invalid_result)

        with (
            patch.object(branch_designer, "LLMClient", return_value=fake_client),
            patch("builtins.print"),
        ):
            designed = branch_designer.design_story_topology(
                chapters=["a", "b", "c"],
                summaries=["a", "b", "c"],
                provider="openai",
                branch_type="modular",
            )

        self.assertEqual(fake_client.calls, 4)
        self.assertEqual(designed.get("variable_defs"), [])
        self.assertTrue(all(node.get("is_original") for node in designed["topology"]))
        self.assertEqual(
            [node["node_id"] for node in designed["topology"] if node.get("type") == "ending"],
            ["ch3"],
        )


class TopologyGraphValidationTests(unittest.TestCase):
    def _validate(self, result, total):
        self.assertTrue(
            hasattr(branch_designer, "_validate_topology_graph"),
            "尚未实现可独立测试的 DAG 结构校验",
        )
        return branch_designer._validate_topology_graph(result, total)

    def _linear_result(self, total=3):
        topology = []
        for index in range(total):
            next_ids = [] if index == total - 1 else [f"ch{index + 2}"]
            topology.append(_original_node(index, total, next_ids))
        return {"variable_defs": [], "topology": topology}

    def test_rejects_duplicate_node_ids(self):
        result = self._linear_result()
        result["topology"][1]["node_id"] = "ch1"

        ok, reason = self._validate(result, total=3)

        self.assertFalse(ok)
        self.assertIn("node_id 重复", reason)

    def test_rejects_next_target_that_does_not_exist(self):
        result = self._linear_result()
        result["topology"][0]["next"] = ["missing"]

        ok, reason = self._validate(result, total=3)

        self.assertFalse(ok)
        self.assertIn("不存在节点", reason)

    def test_rejects_multiple_roots(self):
        result = self._linear_result()
        result["topology"][0]["next"] = []

        ok, reason = self._validate(result, total=3)

        self.assertFalse(ok)
        self.assertIn("根节点数量", reason)

    def test_rejects_nodes_unreachable_from_the_only_root(self):
        result = self._linear_result(total=2)
        result["topology"].extend(
            [
                {
                    "node_id": "orphan_a",
                    "type": "chapter",
                    "is_original": False,
                    "source_chapter_idx": None,
                    "next": ["orphan_b"],
                },
                {
                    "node_id": "orphan_b",
                    "type": "chapter",
                    "is_original": False,
                    "source_chapter_idx": None,
                    "next": ["orphan_a"],
                },
            ]
        )

        ok, reason = self._validate(result, total=2)

        self.assertFalse(ok)
        self.assertIn("不可达", reason)

    def test_rejects_reachable_cycle(self):
        result = self._linear_result(total=4)
        result["topology"][0]["next"] = ["ch2", "ch4"]
        result["topology"][1]["next"] = ["ch3"]
        result["topology"][2]["next"] = ["ch2"]

        ok, reason = self._validate(result, total=4)

        self.assertFalse(ok)
        self.assertIn("循环", reason)

    def test_rejects_incomplete_original_chapter_coverage(self):
        result = self._linear_result(total=3)
        result["topology"][1]["source_chapter_idx"] = 0

        ok, reason = self._validate(result, total=3)

        self.assertFalse(ok)
        self.assertIn("原著章节索引异常", reason)

    def test_requires_original_last_chapter_to_be_terminal_ending(self):
        result = self._linear_result(total=3)
        result["topology"][-1]["type"] = "chapter"

        ok, reason = self._validate(result, total=3)

        self.assertFalse(ok)
        self.assertIn("原著末章", reason)

    def test_rejects_non_terminal_ending(self):
        result = self._linear_result(total=3)
        result["topology"].append(
            {
                "node_id": "ending_alt",
                "type": "ending",
                "is_original": False,
                "source_chapter_idx": None,
                "next": ["ch3"],
            }
        )
        result["topology"][1]["next"].append("ending_alt")

        ok, reason = self._validate(result, total=3)

        self.assertFalse(ok)
        self.assertIn("终止节点", reason)

    def test_rejects_branch_variable_missing_from_variable_defs(self):
        result = self._linear_result(total=3)
        result["topology"].append(
            {
                "node_id": "ending_alt",
                "type": "ending",
                "is_original": False,
                "source_chapter_idx": None,
                "branch_variable": "unknown_axis",
                "branch_threshold": 65,
                "next": [],
            }
        )
        result["topology"][1]["next"].append("ending_alt")

        ok, reason = self._validate(result, total=3)

        self.assertFalse(ok)
        self.assertIn("未在 variable_defs 定义", reason)


class FallbackTopologyTests(unittest.TestCase):
    def _fallback(self, total, branch_type, **kwargs):
        result = branch_designer._make_fallback_topology(
            total,
            branch_type=branch_type,
            **kwargs,
        )
        ok, reason = branch_designer._validate_topology_graph(result, total)
        self.assertTrue(ok, reason)
        return result

    def test_default_fallback_uses_persona_axes_and_parallel_endings(self):
        axes = [
            {"key": "courage", "name": "勇气", "ending_hint": "直面命运"},
            {"key": "mercy", "name": "仁慈", "ending_hint": "选择宽恕"},
        ]

        result = self._fallback(5, "default", persona_axes=axes)

        self.assertEqual(
            {item["key"] for item in result["variable_defs"]},
            {"courage", "mercy"},
        )
        self.assertEqual(
            len([node for node in result["topology"] if not node.get("is_original") and node.get("type") == "ending"]),
            2,
        )

    def test_modular_fallback_is_linear_original_story(self):
        result = self._fallback(4, "modular")

        self.assertEqual(result["variable_defs"], [])
        self.assertTrue(all(node.get("is_original") for node in result["topology"]))
        self.assertEqual(result["topology"][-1]["type"], "ending")
        self.assertEqual(result["topology"][-1]["choice_text"], "")

    def test_topology_normalizer_clears_choice_text_for_every_ending(self):
        result = {
            "topology": [
                {
                    "node_id": "ch48",
                    "type": "ending",
                    "is_original": True,
                    "choice_text": "继续原著路线",
                    "next": [],
                },
                {
                    "node_id": "ending_a",
                    "type": "ending",
                    "is_original": False,
                    "choice_text": "进入平行结局",
                    "next": [],
                },
            ]
        }

        normalized = branch_designer._normalize_topology_choice_texts(result)

        self.assertTrue(
            all(node["choice_text"] == "" for node in normalized["topology"])
        )

    def test_fallback_prefers_high_value_decision_chapters(self):
        summaries = [
            "日常上课，没有关键选择。",
            "主角决定坦白身世，并拒绝继续隐瞒，关系发生转折。",
            "众人整理房间。",
            "主角选择离开故乡，承担失去亲友的代价。",
            "平静过渡。",
            "故事收束。",
        ]

        result = branch_designer._make_fallback_topology(
            6,
            branch_type="default",
            summaries=summaries,
        )

        branch_points = [
            node["source_chapter_idx"]
            for node in result["topology"]
            if node.get("type") == "branch_point" and node.get("is_original")
        ]
        self.assertIn(1, branch_points)
        whatif = next(
            node for node in result["topology"]
            if not node.get("is_original") and node.get("type") == "chapter"
        )
        self.assertNotEqual("若当时做出另一种选择", whatif["choice_text"])
        self.assertIn("坦白", whatif["choice_text"])

    def test_choice_text_can_be_refilled_from_generated_whatif_summary(self):
        node = {
            "type": "chapter",
            "is_original": False,
            "choice_text": "若当时做出另一种选择",
        }

        branch_designer.refresh_whatif_choice_text(
            node,
            "江渡拒绝隐瞒病情，主动留下署名录音，却因此失去最后的平静。",
        )

        self.assertIn("拒绝隐瞒病情", node["choice_text"])
        self.assertLessEqual(len(node["choice_text"]), 28)

    def test_choice_text_compaction_keeps_a_complete_player_action(self):
        text = branch_designer.compact_complete_choice_text(
            "若选择该线推动沈抱山走向尊重李迟舒意愿、以真相修复创伤的长期陪伴路线，避免把关系再次变成控制",
            max_chars=28,
        )

        self.assertLessEqual(len(text), 28)
        self.assertFalse(text.endswith(("避免把", "修复创", "走到无")))
        self.assertIn("李迟舒", text)

    def test_refresh_whatif_choice_text_hides_internal_variable_keys(self):
        node = {"type": "chapter", "is_original": False, "choice_text": ""}

        branch_designer.refresh_whatif_choice_text(
            node,
            "若选择family_duty：为家人忍下羞辱并独自承担风险。",
        )

        self.assertNotIn("family_duty", node["choice_text"])
        self.assertIn("家人", node["choice_text"])

    def test_refresh_whatif_choice_text_hides_honesty_key(self):
        node = {"type": "chapter", "is_original": False, "choice_text": ""}

        branch_designer.refresh_whatif_choice_text(
            node,
            "honesty：魏清越隐瞒实情，纠结真话时机。",
        )

        self.assertNotIn("honesty", node["choice_text"])
        self.assertIn("坦白", node["choice_text"])

    def test_compact_whatif_title_does_not_end_with_dangling_connector(self):
        title = branch_designer.compact_whatif_title_phrase(
            "选择暂时放弃追查，转而保护同伴并整理证据",
            max_chars=16,
        )

        self.assertLessEqual(len(title), 16)
        self.assertNotRegex(title, r"[并而与及和的地得来向]$")

    def test_compact_whatif_title_removes_truncated_self_connector(self):
        title = branch_designer.compact_whatif_title_phrase(
            "接受杨武劝说提前押注极限武馆自身前途",
            max_chars=15,
        )

        self.assertNotRegex(title, r"自$")

    def test_whatif_content_budget_trims_at_paragraph_boundary(self):
        content = "第一段。\n\n" + ("第二段内容。" * 5000) + "\n\n第三段不应出现。"

        trimmed = branch_designer.enforce_whatif_content_budget(
            content,
            min_chars=8000,
            max_chars=12000,
        )

        self.assertLessEqual(len(trimmed), 13800)
        self.assertNotIn("第三段不应出现", trimmed)
        self.assertTrue(trimmed.endswith(("。", "！", "？")))

    def test_whatif_content_budget_rejects_underlong_body(self):
        with self.assertRaisesRegex(ValueError, "低于最低要求"):
            branch_designer.enforce_whatif_content_budget(
                "过短正文。" * 100,
                min_chars=8000,
                max_chars=12000,
            )

    def test_whatif_content_budget_accepts_tiny_shortfall(self):
        content = "文" * 7994

        accepted = branch_designer.enforce_whatif_content_budget(
            content,
            min_chars=8000,
            max_chars=12000,
        )

        self.assertEqual(accepted, content)

    def test_whatif_content_budget_accepts_fifteen_percent_lower_tolerance(self):
        content = "文" * 6800

        accepted = branch_designer.enforce_whatif_content_budget(
            content,
            min_chars=8000,
            max_chars=12000,
        )

        self.assertEqual(accepted, content)

    def test_whatif_content_budget_rejects_below_fifteen_percent_lower_tolerance(self):
        with self.assertRaisesRegex(ValueError, "低于最低要求"):
            branch_designer.enforce_whatif_content_budget(
                "文" * 6799,
                min_chars=8000,
                max_chars=12000,
            )

    def test_whatif_content_budget_accepts_fifteen_percent_upper_tolerance(self):
        content = "完整。" * 4300

        accepted = branch_designer.enforce_whatif_content_budget(
            content,
            min_chars=8000,
            max_chars=12000,
        )

        self.assertEqual(len(accepted), len(content))
        self.assertLessEqual(len(accepted), 13800)

    def test_actual_whatif_transition_uses_final_tail_and_downstream_opening(self):
        client = _StaticTopologyClient(
            {"compatible": True, "score": 8, "issues": []}
        )
        whatif_text = "前文。" * 1000 + "最终过渡尾声。"
        downstream_text = "下游章节开头。" + "后文。" * 1000

        with patch.object(branch_designer, "LLMClient", return_value=client):
            review = branch_designer.review_whatif_downstream_transition(
                whatif_text=whatif_text,
                downstream_text=downstream_text,
                provider="openai",
            )

        self.assertTrue(review["compatible"])
        self.assertEqual(review["score"], 8)
        prompt = client.last_kwargs["user_prompt"]
        self.assertIn("最终过渡尾声", prompt)
        self.assertIn("下游章节开头", prompt)

    def test_actual_whatif_transition_keeps_content_when_reviewer_errors(self):
        class _BrokenClient:
            def chat_completion_json(self, **_kwargs):
                raise RuntimeError("gateway unavailable")

        with patch.object(branch_designer, "LLMClient", return_value=_BrokenClient()):
            review = branch_designer.review_whatif_downstream_transition(
                whatif_text="完整结尾。",
                downstream_text="下一章开头。",
                provider="openai",
            )

        self.assertFalse(review["compatible"])
        self.assertEqual(review["score"], 0)
        self.assertIn("复检失败", "；".join(review["issues"]))
        self.assertTrue(review["accepted_with_warning"])

    def test_transition_review_accepts_high_score_without_hard_conflict_with_warning(self):
        client = _StaticTopologyClient(
            {
                "compatible": False,
                "score": 8,
                "hard_conflict": False,
                "issues": ["情绪差异可由一两句过渡解释"],
            }
        )

        with patch.object(branch_designer, "LLMClient", return_value=client):
            review = branch_designer.review_whatif_downstream_transition(
                whatif_text="完整的 What-If 结尾。",
                downstream_text="下一章开头。",
                provider="openai",
            )

        self.assertTrue(review["compatible"])
        self.assertTrue(review["accepted_with_warning"])
        self.assertIn("情绪差异", review["issues"][0])

    def test_transition_review_preserves_low_score_for_local_gate(self):
        client = _StaticTopologyClient(
            {
                "compatible": False,
                "score": 2,
                "hard_conflict": False,
                "issues": ["需要补一处普通过渡"],
            }
        )

        with patch.object(branch_designer, "LLMClient", return_value=client):
            review = branch_designer.review_whatif_downstream_transition(
                whatif_text="完整的 What-If 结尾。",
                downstream_text="下一章开头。",
                provider="openai",
            )

        self.assertTrue(review["compatible"])
        self.assertTrue(review["accepted_with_warning"])
        self.assertFalse(review["hard_conflict"])
        self.assertFalse(
            branch_designer.is_acceptable_whatif_transition_review(review)
        )

    def test_transition_review_accepts_high_score_without_issues_even_if_model_says_false(self):
        client = _StaticTopologyClient(
            {
                "compatible": False,
                "score": 8,
                "hard_conflict": False,
                "issues": [],
            }
        )

        with patch.object(branch_designer, "LLMClient", return_value=client):
            review = branch_designer.review_whatif_downstream_transition(
                whatif_text="完整的 What-If 结尾。",
                downstream_text="下一章开头。",
                provider="openai",
            )

        self.assertTrue(review["compatible"])
        self.assertTrue(review["accepted_with_warning"])
        self.assertEqual([], review["issues"])

    def test_transition_repair_stops_on_bridge_warning_without_extra_llm_repair(self):
        repair_calls = []

        result = branch_designer.repair_whatif_until_compatible(
            content="原始正文。",
            downstream_opening="下一章开头。",
            sibling_states={},
            provider="openai",
            review_callback=lambda **_kwargs: {
                "compatible": False,
                "accepted_with_warning": True,
                "hard_conflict": False,
                "score": 8,
                "issues": ["只需补一处过渡提示"],
            },
            repair_callback=lambda *_args, **_kwargs: repair_calls.append(True),
            max_attempts=3,
        )

        self.assertTrue(result["compatible"])
        self.assertEqual([], repair_calls)
        self.assertEqual(0, result["attempts"])

    def test_transition_repair_default_uses_one_targeted_attempt(self):
        repair_calls = []

        result = branch_designer.repair_whatif_until_compatible(
            content="原始正文。",
            downstream_opening="下一章开头。",
            sibling_states={},
            provider="openai",
            review_callback=lambda **_kwargs: {
                "compatible": False,
                "hard_conflict": True,
                "score": 2,
                "issues": ["现实层冲突"],
            },
            repair_callback=lambda content, **_kwargs: (
                repair_calls.append(True) or f"{content}\n修复后的正文。"
            ),
        )

        self.assertFalse(result["compatible"])
        self.assertEqual(1, result["attempts"])
        self.assertEqual(1, len(repair_calls))

    def test_transition_review_keeps_explicit_hard_conflict_incompatible(self):
        client = _StaticTopologyClient(
            {
                "compatible": False,
                "score": 8,
                "hard_conflict": True,
                "issues": ["现实层发生硬冲突"],
            }
        )

        with patch.object(branch_designer, "LLMClient", return_value=client):
            review = branch_designer.review_whatif_downstream_transition(
                whatif_text="完整的 What-If 结尾。",
                downstream_text="下一章开头。",
                provider="openai",
            )

        self.assertFalse(review["compatible"])
        self.assertFalse(review["accepted_with_warning"])

    def test_transition_review_normalizes_string_boolean_fields(self):
        client = _StaticTopologyClient(
            {
                "compatible": "false",
                "score": 8,
                "hard_conflict": "false",
                "issues": ["只需补一处过渡提示"],
            }
        )

        with patch.object(branch_designer, "LLMClient", return_value=client):
            review = branch_designer.review_whatif_downstream_transition(
                whatif_text="完整的 What-If 结尾。",
                downstream_text="下一章开头。",
                provider="openai",
            )

        self.assertTrue(review["compatible"])
        self.assertTrue(review["accepted_with_warning"])
        self.assertFalse(review["hard_conflict"])

    def test_transition_review_does_not_promote_bridgeable_action_gap_to_hard_conflict(self):
        client = _StaticTopologyClient(
            {
                "compatible": False,
                "score": 8,
                "issues": ["动作链只需补一两句过渡即可"],
            }
        )

        with patch.object(branch_designer, "LLMClient", return_value=client):
            review = branch_designer.review_whatif_downstream_transition(
                whatif_text="完整的 What-If 结尾。",
                downstream_text="下一章开头。",
                provider="openai",
            )

        self.assertTrue(review["compatible"])
        self.assertTrue(review["accepted_with_warning"])
        self.assertFalse(review["hard_conflict"])

    def test_transition_review_does_not_treat_a_death_mention_as_state_conflict(self):
        client = _StaticTopologyClient(
            {
                "compatible": False,
                "score": 8,
                "issues": ["结尾提到对死亡的恐惧，但人物生死状态一致"],
            }
        )

        with patch.object(branch_designer, "LLMClient", return_value=client):
            review = branch_designer.review_whatif_downstream_transition(
                whatif_text="完整的 What-If 结尾。",
                downstream_text="下一章开头。",
                provider="openai",
            )

        self.assertTrue(review["compatible"])
        self.assertTrue(review["accepted_with_warning"])
        self.assertFalse(review["hard_conflict"])

    def test_transition_review_detects_explicit_death_resurrection_conflict_without_flag(self):
        client = _StaticTopologyClient(
            {
                "compatible": False,
                "score": 8,
                "issues": ["人物死亡后复活，生死状态不一致"],
            }
        )

        with patch.object(branch_designer, "LLMClient", return_value=client):
            review = branch_designer.review_whatif_downstream_transition(
                whatif_text="完整的 What-If 结尾。",
                downstream_text="下一章开头。",
                provider="openai",
            )

        self.assertFalse(review["compatible"])
        self.assertTrue(review["hard_conflict"])

    def test_transition_review_keeps_detected_hard_conflict_when_model_flag_is_false(self):
        client = _StaticTopologyClient(
            {
                "compatible": False,
                "score": 9,
                "hard_conflict": False,
                "issues": ["人物死亡后复活，生死状态不一致"],
            }
        )

        with patch.object(branch_designer, "LLMClient", return_value=client):
            review = branch_designer.review_whatif_downstream_transition(
                whatif_text="完整的 What-If 结尾。",
                downstream_text="下一章开头。",
                provider="openai",
            )

        self.assertFalse(review["compatible"])
        self.assertTrue(review["hard_conflict"])

    def test_whatif_budget_can_preserve_convergence_ending(self):
        ending = "主角终于回到主线约定的地点。"
        content = "开场。\n\n" + ("中段铺陈。" * 4000) + "\n\n" + ending

        trimmed = branch_designer.enforce_whatif_content_budget(
            content,
            min_chars=8000,
            max_chars=12000,
            preserve_ending=True,
        )

        self.assertLessEqual(len(trimmed), 13800)
        self.assertTrue(trimmed.endswith(ending))

    def test_whatif_budget_preserves_tail_when_last_scene_is_large(self):
        ending = "最终衔接句。"
        content = (
            ("前段叙事。" * 3000)
            + "\n\n"
            + ("末场铺垫。" * 3000)
            + ending
        )

        trimmed = branch_designer.enforce_whatif_content_budget(
            content,
            min_chars=8000,
            max_chars=12000,
            preserve_ending=True,
        )

        self.assertGreaterEqual(len(trimmed), 8000)
        self.assertLessEqual(len(trimmed), 13800)
        self.assertTrue(trimmed.endswith(ending))

    def test_convergence_repair_rewrites_only_tail(self):
        client = _StaticTopologyClient({})
        client.chat_completion = lambda **_kwargs: "修复后的过渡尾声。"
        content = ("前文保持不变。" * 500) + "\n\n原来的冲突尾声。"

        with patch.object(branch_designer, "LLMClient", return_value=client):
            repaired = branch_designer.repair_whatif_convergence_tail(
                content,
                issues=["江渡.reality_layer 不一致"],
                sibling_states={"江渡": {"alive": False, "reality_layer": "memory"}},
                provider="openai",
                output_language="zh",
            )

        self.assertTrue(repaired.startswith("前文保持不变。"))
        self.assertTrue(repaired.endswith("修复后的过渡尾声。"))
        self.assertNotIn("原来的冲突尾声", repaired)

    def test_bridge_scene_repair_rewrites_a_structural_tail_segment(self):
        client = _StaticTopologyClient({})
        client.chat_completion = lambda **_kwargs: (
            "李迟舒答应邀请。两人由蒋驰送到乡下租房，安顿住宿后进入第二天清晨。"
        )
        content = (
            ("学校里的前文保持不变。" * 500)
            + "\n\n仍在楼梯口讨论邀请。"
            + "\n\n缺失的突然睡着结尾。"
        )

        with patch.object(branch_designer, "LLMClient", return_value=client):
            repaired = branch_designer.repair_whatif_convergence_segment(
                content,
                issues=["缺少接受邀请、出发、到达和住宿等关键动作链"],
                sibling_states={"李迟舒": {"location": "乡下租房"}},
                provider="openai",
                output_language="zh",
                downstream_opening="第二天清晨，两人在乡下租房醒来。",
                strategy="bridge_scene",
            )

        self.assertTrue(repaired.startswith("学校里的前文保持不变。"))
        self.assertTrue(repaired.endswith("进入第二天清晨。"))
        self.assertNotIn("突然睡着结尾", repaired)

    def test_transition_repair_escalates_to_full_last_scene_before_giving_up(self):
        reviews = iter(
            [
                {"compatible": False, "score": 2, "issues": ["缺少出发和到达"]},
                {"compatible": False, "score": 4, "issues": ["转场仍过快"]},
                {"compatible": True, "score": 8, "issues": []},
            ]
        )
        strategies = []

        result = branch_designer.repair_whatif_until_compatible(
            content="原始正文。",
            downstream_opening="第二天清晨，两人在乡下醒来。",
            sibling_states={},
            provider="openai",
            review_callback=lambda **_kwargs: next(reviews),
            repair_callback=lambda content, **kwargs: (
                strategies.append(kwargs["strategy"])
                or content + f"\n\n第{len(strategies)}轮修复。"
            ),
            max_attempts=3,
        )

        self.assertTrue(result["compatible"])
        self.assertEqual(2, result["attempts"])
        self.assertEqual("bridge_scene", strategies[0])
        self.assertEqual("regenerate_last_scene", strategies[1])
        self.assertEqual(3, len(result["strategy_history"]))

    def test_retryable_transition_repair_failure_preserves_original_content(self):
        from services.story_planner.core.llm_resilience import (
            LLMInvalidResponseError,
        )

        result = branch_designer.repair_whatif_until_compatible(
            content="原始正文。",
            downstream_opening="下一章开头。",
            sibling_states={},
            provider="openai",
            review_callback=lambda **_kwargs: {
                "compatible": False,
                "score": 4,
                "issues": ["缺少必要转场"],
            },
            repair_callback=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                LLMInvalidResponseError("choices 为空")
            ),
            max_attempts=3,
        )

        self.assertFalse(result["compatible"])
        self.assertEqual("原始正文。", result["content"])
        self.assertEqual(0, result["attempts"])
        self.assertEqual("invalid_response", result["repair_error"]["category"])

    def test_safe_segment_repair_keeps_content_after_retryable_llm_failure(self):
        from services.story_planner.core.llm_resilience import (
            LLMInvalidResponseError,
        )

        with patch.object(
            branch_designer,
            "repair_whatif_convergence_segment",
            side_effect=LLMInvalidResponseError("choices 为空"),
        ):
            result = branch_designer.try_repair_whatif_convergence_segment(
                "原始正文。",
                issues=["角色末态冲突"],
                sibling_states={},
                provider="openai",
                strategy="regenerate_last_scene",
            )

        self.assertTrue(result["repair_failed"])
        self.assertEqual("原始正文。", result["content"])
        self.assertEqual("invalid_response", result["error_category"])

    def test_safe_segment_repair_rejects_large_length_collapse(self):
        original = "原" * 9198
        with patch.object(
            branch_designer,
            "repair_whatif_convergence_segment",
            return_value="短" * 4645,
        ):
            result = branch_designer.try_repair_whatif_convergence_segment(
                original,
                issues=["角色末态冲突"],
                sibling_states={},
                provider="openai",
                strategy="regenerate_last_scene",
                minimum_total_chars=6800,
            )

        self.assertTrue(result["repair_failed"])
        self.assertEqual(original, result["content"])
        self.assertEqual("underlong_repair", result["error_category"])
        self.assertGreaterEqual(result["required_chars"], 6800)

    def test_tail_segment_split_preserves_sentence_boundary_and_limits_rewrite(self):
        text = "第一段已经发生的事实。" * 2000

        prefix, tail = branch_designer.split_tail_segment(text, target_chars=3200)

        self.assertTrue(prefix)
        self.assertTrue(tail)
        self.assertLessEqual(len(tail), 3300)
        self.assertGreaterEqual(len(tail), 2800)
        self.assertTrue(prefix.endswith(("。", "！", "？")))
        self.assertTrue(tail[0] != "。")

    def test_underlong_whatif_uses_specific_optional_branch_error(self):
        with self.assertRaises(branch_designer.WhatIfContentBudgetError):
            branch_designer.enforce_whatif_content_budget(
                "短" * 400,
                min_chars=600,
                max_chars=600,
            )

    def test_transition_repair_rejects_candidate_below_adaptive_minimum(self):
        original = "原" * 9198
        result = branch_designer.repair_whatif_until_compatible(
            content=original,
            downstream_opening="下一章开头。",
            sibling_states={},
            provider="openai",
            review_callback=lambda **_kwargs: {
                "compatible": False,
                "score": 4,
                "issues": ["缺少必要转场"],
            },
            repair_callback=lambda *_args, **_kwargs: "短" * 4645,
            max_attempts=3,
            minimum_total_chars=6800,
        )

        self.assertFalse(result["compatible"])
        self.assertEqual(original, result["content"])
        self.assertEqual("underlong_repair", result["repair_error"]["category"])

    def test_prebudgeted_short_whatif_does_not_restore_eight_thousand_floor(self):
        budget = branch_designer.resolve_whatif_budget(
            600,
            already_budgeted=True,
        )

        self.assertEqual(600, budget["target_chars"])
        self.assertEqual(1, budget["scene_count"])
        self.assertEqual(0, budget["soft_branch_count"])

    def test_merge_policy_defaults_to_bridge_when_required_transitions_exist(self):
        node = {
            "node_id": "ch7_alt",
            "type": "chapter",
            "is_original": False,
            "next": ["ch8"],
            "required_transitions": ["接受邀请", "出发", "到达乡下租房"],
        }

        normalized = branch_designer.normalize_whatif_merge_policy(node)

        self.assertEqual("bridge", normalized["merge_policy"])
        self.assertEqual(3, len(normalized["required_transitions"]))

    def test_whatif_generation_uses_the_same_strict_transition_reviewer(self):
        source = inspect.getsource(branch_designer.generate_whatif_chapter)

        self.assertIn("review_whatif_downstream_transition(", source)
        self.assertNotIn("【B. 主线下一章全文】", source)

    def test_whatif_prompt_does_not_require_all_terminal_fields_to_match_exactly(self):
        source = inspect.getsource(branch_designer.generate_whatif_chapter)

        self.assertNotIn("末完全一致", source)
        self.assertIn("时间、地点和局部参与差异可以通过过渡解释", source)

    def test_fallback_whatif_declares_merge_contract(self):
        result = self._fallback(8, "default")
        whatif = next(
            node
            for node in result["topology"]
            if node.get("type") == "chapter" and not node.get("is_original")
        )

        self.assertEqual("bridge", whatif["merge_policy"])
        self.assertTrue(whatif["required_transitions"])

    def test_resume_whatif_reuses_checkpointed_outline(self):
        generated = []
        expected = [{"scene_no": 1, "core_conflict": "已保存场次"}]

        scenes = branch_designer.resolve_whatif_scenes(
            {"scenes": expected},
            lambda: generated.append(True) or [],
        )

        self.assertEqual(expected, scenes)
        self.assertEqual([], generated)

    def test_accumulated_summary_is_refreshed_every_two_scenes(self):
        self.assertFalse(branch_designer.should_refresh_accumulated_summary(0))
        self.assertTrue(branch_designer.should_refresh_accumulated_summary(1))
        self.assertFalse(branch_designer.should_refresh_accumulated_summary(2))
        self.assertTrue(branch_designer.should_refresh_accumulated_summary(3))

    def test_death_corridor_fallback_contains_only_terminal_bad_end_branch(self):
        result = self._fallback(5, "death_corridor")
        bad_ends = [node for node in result["topology"] if node.get("type") == "bad_ending"]
        parallel_endings = [
            node for node in result["topology"]
            if node.get("type") == "ending" and not node.get("is_original")
        ]
        self.assertEqual(result["variable_defs"], [])
        self.assertEqual(len(bad_ends), 1)
        self.assertTrue(all(node.get("next") == [] for node in bad_ends))
        self.assertEqual(parallel_endings, [])

    def test_fantasy_fallback_contains_diamond_what_if_without_parallel_ending(self):
        result = self._fallback(5, "fantasy")
        whatifs = [
            node for node in result["topology"]
            if node.get("type") == "chapter" and not node.get("is_original")
        ]
        parallel_endings = [
            node for node in result["topology"]
            if node.get("type") == "ending" and not node.get("is_original")
        ]
        self.assertEqual(result["variable_defs"], [])
        self.assertEqual(len(whatifs), 1)
        self.assertEqual(parallel_endings, [])
        whatif = whatifs[0]
        parents = [node for node in result["topology"] if whatif["node_id"] in node.get("next", [])]
        self.assertEqual(len(parents), 1)
        siblings = [
            node for node in result["topology"]
            if node["node_id"] in parents[0]["next"] and node.get("is_original")
        ]
        self.assertEqual(len(siblings), 1)
        self.assertEqual(whatif["next"], siblings[0]["next"])

    def test_romance_fallback_routes_each_target_to_its_own_ending(self):
        targets = [
            {"name": "李青", "role_label": "女主"},
            {"name": "魏宇", "role_label": "男主"},
        ]
        result = self._fallback(4, "romance", romance_targets=targets)
        self.assertFalse(
            any(
                node.get("type") == "chapter" and not node.get("is_original")
                for node in result["topology"]
            )
        )
        self.assertEqual(len(result["variable_defs"]), 2)
        self.assertEqual(
            len([node for node in result["topology"] if node.get("type") == "ending" and not node.get("is_original")]),
            2,
        )


class TopologyRetryTests(unittest.TestCase):
    def test_invalid_topology_is_repaired_locally_for_up_to_four_attempts(self):
        invalid = {
            "variable_defs": [],
            "topology": [
                _original_node(0, 3, ["ch2"]),
                _original_node(1, 3, ["ch3"]),
                _original_node(1, 3, []),
            ],
        }
        valid = {
            "variable_defs": [],
            "topology": [
                _original_node(0, 3, ["ch2"]),
                _original_node(1, 3, ["ch3"]),
                _original_node(2, 3, []),
            ],
        }
        client = _SequenceTopologyClient([invalid, valid])

        with (
            patch.object(branch_designer, "LLMClient", return_value=client),
            patch("builtins.print"),
        ):
            result = branch_designer.design_story_topology(
                chapters=["a", "b", "c"],
                summaries=["a", "b", "c"],
                provider="openai",
                branch_type="modular",
            )

        self.assertEqual(2, len(client.calls))
        repair_prompt = client.calls[1]["user_prompt"]
        self.assertIn("局部修复", repair_prompt)
        self.assertIn("上一次候选 JSON", repair_prompt)
        self.assertEqual("ch3", result["topology"][-1]["node_id"])


class CompactTopologySpecTests(unittest.TestCase):
    def test_merge_preflight_delays_when_immediate_downstream_requires_cancelled_event(self):
        result = branch_designer.preflight_whatif_merge_feasibility(
            spec={
                "pivot_chapter": 19,
                "alternative_choice": "暂缓行动，先不去初中部贴报",
                "invalidated_events": ["去初中部贴报"],
                "downstream_prerequisites": [
                    {
                        "merge_after_chapters": 1,
                        "requires": ["已经去初中部贴报", "监控拍到贴报过程"],
                    },
                    {
                        "merge_after_chapters": 2,
                        "requires": ["风波已经发生"],
                    },
                ],
            },
            summaries=["普通章节"] * 24,
            max_delay=3,
        )

        self.assertTrue(result["feasible"])
        self.assertEqual("delayed", result["merge_policy"])
        self.assertEqual(2, result["merge_after_chapters"])
        self.assertEqual(22, result["merge_target_chapter"])

    def test_merge_preflight_rejects_spec_when_all_supported_targets_conflict(self):
        result = branch_designer.preflight_whatif_merge_feasibility(
            spec={
                "pivot_chapter": 6,
                "alternative_choice": "拒绝公开病情",
                "invalidated_events": ["公开病情"],
                "downstream_prerequisites": [
                    {"merge_after_chapters": 1, "requires": ["病情已经公开"]},
                    {"merge_after_chapters": 2, "requires": ["公开病情后的处分"]},
                    {"merge_after_chapters": 3, "requires": ["因公开病情转院"]},
                ],
            },
            summaries=["普通章节"] * 12,
            max_delay=3,
        )

        self.assertFalse(result["feasible"])
        self.assertEqual("reselect", result["merge_policy"])
        self.assertEqual([1, 2, 3], result["conflicting_delays"])

    def test_compact_topology_routes_delayed_whatif_to_explicit_later_chapter(self):
        result = branch_designer._assemble_compact_topology(
            total=8,
            specs=[
                {
                    "pivot_chapter": 2,
                    "decision": "是否立即公开病情",
                    "alternative_choice": "暂缓公开病情",
                    "consequence": "先私下处理关系",
                    "choice_text": "暂缓公开病情",
                    "merge_policy": "delayed",
                    "merge_after_chapters": 2,
                    "merge_target_chapter": 5,
                    "required_transitions": ["完成私下沟通", "推进到处分风波之后"],
                }
            ],
            persona_axes=[],
            branch_type="fantasy",
            metadata={},
        )

        whatif = next(node for node in result["topology"] if node["node_id"] == "ch3_alt")
        self.assertEqual(["ch5"], whatif["next"])
        self.assertEqual("ch5", whatif["merge_target_node_id"])
        self.assertEqual(2, whatif["merge_after_chapters"])
        self.assertEqual("delayed", whatif["merge_policy"])

        window = branch_designer.resolve_whatif_merge_window(
            whatif,
            result["topology"],
        )
        self.assertEqual("ch5", window["target_node_id"])
        self.assertEqual(4, window["target_source_chapter_idx"])
        self.assertEqual(3, window["state_anchor_source_chapter_idx"])
        self.assertEqual([2, 3], window["replaced_source_chapter_indices"])

    def test_compact_request_exception_returns_marked_full_fallback(self):
        class _FailingClient:
            def chat_completion_json(self, **_kwargs):
                raise RuntimeError("gateway unavailable")

        with (
            patch.object(branch_designer, "LLMClient", return_value=_FailingClient()),
            patch("builtins.print"),
        ):
            result = branch_designer.design_story_topology(
                chapters=[f"chapter {index}" for index in range(8)],
                summaries=[f"summary {index}" for index in range(8)],
                provider="openai",
                persona_axes=[
                    {"key": "courage", "name": "勇气"},
                    {"key": "self_worth", "name": "自我价值"},
                ],
                branch_type="default",
            )

        self.assertTrue(result["topology_generation"]["full_fallback"])
        self.assertEqual("deterministic_fallback", result["topology_generation"]["mode"])

    def test_default_mode_assembles_deterministic_graph_from_compact_specs(self):
        client = _StaticTopologyClient(
            {
                "whatif_specs": [
                    {
                        "pivot_chapter": 2,
                        "decision": "是否坦白病情",
                        "alternative_choice": "主动说明真实情况",
                        "consequence": "关系提前改变并承担失去平静的代价",
                        "choice_text": "主动坦白病情",
                        "merge_policy": "bridge",
                        "required_transitions": ["完成坦白后的关系调整", "回到下游约定地点"],
                    },
                    {
                        "pivot_chapter": 5,
                        "decision": "是否独自离开",
                        "alternative_choice": "留下接受帮助",
                        "consequence": "必须面对他人的关心与自己的脆弱",
                        "choice_text": "留下接受帮助",
                    },
                ]
            }
        )
        axes = [
            {"key": "courage", "name": "勇气", "ending_hint": "主动留下真实证言"},
            {"key": "self_worth", "name": "自我价值", "ending_hint": "公开澄清流言"},
        ]

        with (
            patch.object(branch_designer, "LLMClient", return_value=client),
            patch("builtins.print"),
        ):
            result = branch_designer.design_story_topology(
                chapters=[f"chapter {index}" for index in range(8)],
                summaries=[f"summary {index}" for index in range(8)],
                provider="openai",
                persona_axes=axes,
                branch_type="default",
            )

        ok, reason = branch_designer._validate_topology_graph(result, 8)
        self.assertTrue(ok, reason)
        self.assertEqual(1, client.calls)
        self.assertIn("whatif_specs", client.result)
        self.assertEqual("compact_specs", result["topology_generation"]["mode"])
        self.assertEqual(0, result["topology_generation"]["fallback_branch_count"])
        self.assertEqual(2, result["topology_generation"]["initial_valid_branch_count"])
        whatifs = [
            node for node in result["topology"]
            if not node.get("is_original") and node.get("type") == "chapter"
        ]
        self.assertEqual(["ch3_alt", "ch6_alt"], [node["node_id"] for node in whatifs])
        self.assertEqual("主动坦白病情", whatifs[0]["choice_text"])
        self.assertEqual("bridge", whatifs[0]["merge_policy"])
        self.assertEqual(2, len(whatifs[0]["required_transitions"]))
        prompt = client.last_kwargs["user_prompt"]
        self.assertIn("必须严格输出2项", prompt)
        self.assertIn('{"whatif_specs": [', prompt)
        self.assertIn("推荐岔口章节", prompt)

    def test_parallel_endings_declare_route_specific_legacy_requirements(self):
        variable_defs, endings = branch_designer._build_variable_defs(
            [
                {"key": "self_worth", "name": "自尊"},
                {"key": "honesty", "name": "坦白"},
            ]
        )

        self.assertEqual(
            ["signed_clarification", "school_record_preserved"],
            endings[0]["legacy_requirements"],
        )
        self.assertEqual(
            ["illness_disclosed", "truth_record_preserved"],
            endings[1]["legacy_requirements"],
        )

    def test_invalid_spec_is_repaired_without_regenerating_valid_specs(self):
        client = _SequenceTopologyClient(
            [
                {
                    "whatif_specs": [
                        {
                            "pivot_chapter": 2,
                            "decision": "是否坦白",
                            "alternative_choice": "主动坦白",
                            "consequence": "关系改变",
                            "choice_text": "主动坦白",
                        },
                        {
                            "pivot_chapter": 99,
                            "decision": "非法位置",
                            "alternative_choice": "错误选择",
                            "consequence": "无",
                            "choice_text": "",
                        },
                    ]
                },
                {
                    "spec": {
                        "pivot_chapter": 5,
                        "decision": "是否离开",
                        "alternative_choice": "选择留下",
                        "consequence": "直面关系裂痕",
                        "choice_text": "选择留下",
                    }
                },
            ]
        )

        with (
            patch.object(branch_designer, "LLMClient", return_value=client),
            patch("builtins.print"),
        ):
            result = branch_designer.design_story_topology(
                chapters=[f"chapter {index}" for index in range(8)],
                summaries=[f"summary {index}" for index in range(8)],
                provider="openai",
                persona_axes=[
                    {"key": "courage", "name": "勇气"},
                    {"key": "self_worth", "name": "自我价值"},
                ],
                branch_type="default",
            )

        self.assertEqual(2, len(client.calls))
        self.assertIn("只修复这一条", client.calls[1]["user_prompt"])
        self.assertEqual(1, result["topology_generation"]["repaired_branch_count"])
        self.assertEqual(0, result["topology_generation"]["fallback_branch_count"])

    def test_missing_pivots_are_assigned_from_recommended_slots_without_repair(self):
        client = _StaticTopologyClient(
            {
                "whatif_specs": [
                    {
                        "decision": "是否坦白",
                        "alternative_choice": "主动坦白",
                        "consequence": "关系提前改变",
                        "choice_text": "主动坦白病情",
                    },
                    {
                        "decision": "是否离开",
                        "alternative_choice": "选择留下",
                        "consequence": "直面关系裂痕",
                        "choice_text": "留下接受帮助",
                    },
                ]
            }
        )

        with (
            patch.object(branch_designer, "LLMClient", return_value=client),
            patch("builtins.print"),
        ):
            result = branch_designer.design_story_topology(
                chapters=[f"chapter {index}" for index in range(8)],
                summaries=[
                    "普通章节",
                    "主角决定坦白并承担后果",
                    "普通章节",
                    "普通章节",
                    "主角拒绝离开并选择留下",
                    "普通章节",
                    "普通章节",
                    "结局",
                ],
                provider="openai",
                persona_axes=[
                    {"key": "courage", "name": "勇气"},
                    {"key": "self_worth", "name": "自我价值"},
                ],
                branch_type="default",
            )

        self.assertEqual(1, client.calls)
        self.assertEqual(0, result["topology_generation"]["repaired_branch_count"])
        self.assertEqual(2, result["topology_generation"]["code_assigned_pivot_count"])

    def test_single_failed_spec_uses_only_one_deterministic_fallback(self):
        invalid = {"spec": {"pivot_chapter": 99, "choice_text": ""}}
        client = _SequenceTopologyClient(
            [
                {
                    "whatif_specs": [
                        {
                            "pivot_chapter": 2,
                            "decision": "是否坦白",
                            "alternative_choice": "主动坦白",
                            "consequence": "关系改变",
                            "choice_text": "主动坦白",
                        },
                        invalid["spec"],
                    ]
                },
                invalid,
                invalid,
            ]
        )

        with (
            patch.object(branch_designer, "LLMClient", return_value=client),
            patch("builtins.print"),
        ):
            result = branch_designer.design_story_topology(
                chapters=[f"chapter {index}" for index in range(8)],
                summaries=[
                    "普通章节",
                    "主角决定坦白并承担后果",
                    "普通章节",
                    "普通章节",
                    "主角拒绝离开并选择留下",
                    "普通章节",
                    "普通章节",
                    "结局",
                ],
                provider="openai",
                persona_axes=[
                    {"key": "courage", "name": "勇气"},
                    {"key": "self_worth", "name": "自我价值"},
                ],
                branch_type="default",
            )

        metadata = result["topology_generation"]
        self.assertEqual(1, metadata["fallback_branch_count"])
        self.assertFalse(metadata["full_fallback"])
        self.assertEqual(0.5, metadata["fallback_ratio"])


if __name__ == "__main__":
    unittest.main()
