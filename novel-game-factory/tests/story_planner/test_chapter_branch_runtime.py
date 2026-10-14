import importlib.util
import inspect
import json
from pathlib import Path
import unittest


def _load_runtime():
    module_path = (
        Path(__file__).resolve().parents[2]
        / "services"
        / "story_planner"
        / "core"
        / "chapter_branch_runtime.py"
    )
    if not module_path.exists():
        return None
    spec = importlib.util.spec_from_file_location(
        "chapter_branch_runtime_under_test",
        module_path,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runtime = _load_runtime()


class DeterministicNodeIdTests(unittest.TestCase):
    def test_same_task_and_topology_node_produce_same_id(self):
        self.assertIsNotNone(runtime, "chapter_branch_runtime 尚未实现")

        first = runtime.deterministic_node_id("task-1", "whatif_3")
        second = runtime.deterministic_node_id("task-1", "whatif_3")

        self.assertEqual(first, second)
        self.assertTrue(first.startswith("node_"))

    def test_different_tasks_do_not_share_non_root_ids(self):
        self.assertIsNotNone(runtime, "chapter_branch_runtime 尚未实现")

        self.assertNotEqual(
            runtime.deterministic_node_id("task-1", "ending_a"),
            runtime.deterministic_node_id("task-2", "ending_a"),
        )

    def test_original_first_chapter_keeps_public_root_id(self):
        self.assertIsNotNone(runtime, "chapter_branch_runtime 尚未实现")

        self.assertEqual(
            runtime.deterministic_node_id("task-1", "ch1", source_chapter_idx=0),
            "chapter_1",
        )


class WhatIfBudgetTests(unittest.TestCase):
    def test_whatif_budget_caps_body_and_soft_branches(self):
        budget = runtime.plan_whatif_budget(average_chapter_chars=30000)

        self.assertEqual(12000, budget["target_chars"])
        self.assertEqual(4, budget["scene_count"])
        self.assertEqual(3, budget["soft_branch_count"])

    def test_whatif_budget_scales_down_for_a_four_hundred_char_short_story(self):
        budget = runtime.plan_whatif_budget(average_chapter_chars=400)

        self.assertEqual(600, budget["target_chars"])
        self.assertEqual(1, budget["scene_count"])
        self.assertEqual(0, budget["soft_branch_count"])

    def test_whatif_budget_scales_with_short_normal_chapters(self):
        budget = runtime.plan_whatif_budget(average_chapter_chars=2672)

        self.assertEqual(4008, budget["target_chars"])
        self.assertEqual(3, budget["scene_count"])
        self.assertEqual(2, budget["soft_branch_count"])


class OptionalBranchOmissionTests(unittest.TestCase):
    def test_optional_branch_attempt_budget_is_shared_by_generation_and_repairs(self):
        budget = runtime.new_optional_branch_attempt_budget(max_attempts=3)

        first = runtime.consume_optional_branch_attempt(budget, phase="generate")
        second = runtime.consume_optional_branch_attempt(budget, phase="state_repair")
        third = runtime.consume_optional_branch_attempt(budget, phase="transition_repair")
        exhausted = runtime.consume_optional_branch_attempt(budget, phase="pov_repair")

        self.assertTrue(first["allowed"])
        self.assertTrue(second["allowed"])
        self.assertTrue(third["allowed"])
        self.assertFalse(exhausted["allowed"])
        self.assertEqual(3, budget["used"])
        self.assertEqual(0, runtime.remaining_optional_branch_attempts(budget))
        self.assertEqual(
            ["generate", "state_repair", "transition_repair"],
            [entry["phase"] for entry in budget["history"]],
        )

    def test_optional_branch_failures_are_local_and_bounded(self):
        plan = {
            "nodes": {
                "ch1_alt": {
                    "status": "pending",
                    "terminal_states": {"A": {"alive": True}},
                }
            }
        }

        first = runtime.record_optional_branch_failure(
            plan,
            "ch1_alt",
            reason="末场衔接未通过",
            max_attempts=3,
        )
        second = runtime.record_optional_branch_failure(
            plan,
            "ch1_alt",
            reason="末态冲突",
            max_attempts=3,
        )
        third = runtime.record_optional_branch_failure(
            plan,
            "ch1_alt",
            reason="POV 不一致",
            max_attempts=3,
        )

        self.assertTrue(first["retry"])
        self.assertTrue(second["retry"])
        self.assertFalse(third["retry"])
        self.assertTrue(third["exhausted"])
        self.assertEqual(3, plan["nodes"]["ch1_alt"]["optional_branch_attempts"])
        self.assertEqual(3, len(plan["nodes"]["ch1_alt"]["optional_branch_failure_history"]))
        self.assertNotIn("node_self_heal_request", plan)
        self.assertEqual(
            {"A": {"alive": True}},
            plan["nodes"]["ch1_alt"]["terminal_states"],
        )

    def test_omitting_whatif_removes_incoming_edge_and_shared_child_parent(self):
        topology = [
            {
                "node_id": "ch1",
                "type": "chapter",
                "is_original": True,
                "next": ["ch2", "ch1_alt"],
                "parent_ids": [],
            },
            {
                "node_id": "ch2",
                "type": "chapter",
                "is_original": True,
                "next": ["ch3"],
                "parent_ids": ["ch1"],
            },
            {
                "node_id": "ch1_alt",
                "type": "chapter",
                "is_original": False,
                "next": ["ch3"],
                "parent_ids": ["ch1"],
            },
            {
                "node_id": "ch3",
                "type": "chapter",
                "is_original": True,
                "next": [],
                "parent_ids": ["ch2", "ch1_alt"],
            },
        ]

        result = runtime.omit_optional_topology_branch(
            topology,
            "ch1_alt",
            reason="收束硬规则重试耗尽",
        )

        self.assertTrue(result["omitted"])
        self.assertEqual(["ch2"], topology[0]["next"])
        self.assertEqual(["ch2"], topology[3]["parent_ids"])
        self.assertTrue(topology[2]["omitted_branch"])
        self.assertEqual("收束硬规则重试耗尽", topology[2]["omission_reason"])

    def test_omitting_parallel_generated_ending_keeps_original_route(self):
        topology = [
            {
                "node_id": "pivot",
                "type": "branch_point",
                "is_original": True,
                "next": ["ending_original", "ending_alt"],
                "parent_ids": [],
            },
            {
                "node_id": "ending_original",
                "type": "ending",
                "is_original": True,
                "next": [],
                "parent_ids": ["pivot"],
            },
            {
                "node_id": "ending_alt",
                "type": "ending",
                "is_original": False,
                "next": [],
                "parent_ids": ["pivot"],
            },
        ]

        result = runtime.omit_optional_topology_branch(
            topology,
            "ending_alt",
            reason="结局分支局部重试耗尽",
        )

        self.assertTrue(result["omitted"])
        self.assertEqual(["ending_original"], topology[0]["next"])
        self.assertTrue(topology[2]["omitted_branch"])

    def test_original_chapter_cannot_be_omitted_as_optional_branch(self):
        topology = [
            {
                "node_id": "ch1",
                "type": "chapter",
                "is_original": True,
                "next": [],
            }
        ]

        result = runtime.omit_optional_topology_branch(topology, "ch1", reason="x")

        self.assertFalse(result["omitted"])
        self.assertFalse(topology[0].get("omitted_branch", False))

    def test_omitting_whatif_cascades_to_unreachable_optional_descendant(self):
        topology = [
            {
                "node_id": "ch1",
                "is_original": True,
                "type": "chapter",
                "next": ["ch2", "ch1_alt"],
                "parent_ids": [],
            },
            {
                "node_id": "ch2",
                "is_original": True,
                "type": "chapter",
                "next": ["ch3"],
                "parent_ids": ["ch1"],
            },
            {
                "node_id": "ch1_alt",
                "is_original": False,
                "type": "chapter",
                "next": ["ch2_alt"],
                "parent_ids": ["ch1"],
            },
            {
                "node_id": "ch2_alt",
                "is_original": False,
                "type": "chapter",
                "next": ["ch3"],
                "parent_ids": ["ch1_alt"],
            },
            {
                "node_id": "ch3",
                "is_original": True,
                "type": "chapter",
                "next": [],
                "parent_ids": ["ch2", "ch2_alt"],
            },
        ]

        result = runtime.omit_optional_topology_branch(
            topology,
            "ch1_alt",
            reason="硬规则失败",
        )

        self.assertEqual(["ch1_alt", "ch2_alt"], result["omitted_node_ids"])
        self.assertTrue(topology[3]["omitted_branch"])
        self.assertEqual(["ch2"], topology[4]["parent_ids"])

    def test_checkpoint_accepts_omitted_status_for_optional_node(self):
        plan = {
            "nodes": {
                "ch1_alt": {
                    "status": "running",
                    "real_id": "node_alt",
                }
            }
        }

        runtime.update_plan_node(
            plan,
            "ch1_alt",
            status="omitted",
            error="重试耗尽后省略可选分支",
        )

        self.assertEqual("omitted", plan["nodes"]["ch1_alt"]["status"])
        self.assertIn("省略", plan["nodes"]["ch1_alt"]["error"])

    def test_omitted_whatif_is_not_counted_in_soft_branch_budget(self):
        counts = runtime.estimate_soft_branch_counts(
            [
                {
                    "node_id": "ch1_alt",
                    "type": "chapter",
                    "is_original": False,
                    "omitted_branch": True,
                }
            ],
            ["正文" * 1000],
        )

        self.assertEqual([0], counts)


class OriginalChapterSoftBranchBudgetTests(unittest.TestCase):
    def test_adaptive_budget_bumps_node_generation_version(self):
        self.assertEqual(
            "node-generation-v3",
            runtime.CHAPTER_BRANCH_STEP_VERSIONS["node_generation"],
        )

    def test_short_normal_original_chapter_gets_one_soft_branch(self):
        counts = runtime.estimate_soft_branch_counts(
            [
                {
                    "node_id": "ch2",
                    "type": "chapter",
                    "is_original": True,
                    "source_chapter_idx": 1,
                }
            ],
            ["正文" * 750, "\n\n".join(["正文" * 750] * 2), "正文" * 750],
        )

        self.assertEqual([1], counts)

    def test_short_original_chapter_reduces_soft_branches_by_length(self):
        short_chapter = "\n\n".join(["段落" * 1000, "段落" * 1000, "段落" * 1000])

        scene_count = runtime.plan_original_chapter_scene_count(
            "chapter",
            chapter_text=short_chapter,
        )

        self.assertEqual(3, scene_count)
        self.assertEqual(2, scene_count - 1)

    def test_single_effective_scene_does_not_create_a_fake_soft_branch(self):
        chapter = "一个没有自然分段的短章节。" * 300

        scene_count = runtime.plan_original_chapter_scene_count(
            "chapter",
            chapter_text=chapter,
        )

        self.assertEqual(1, scene_count)

    def test_long_original_chapter_stays_at_the_normal_three_branch_cap(self):
        chapter = "\n\n".join(["段落" * 2500] * 12)

        scene_count = runtime.plan_original_chapter_scene_count(
            "chapter",
            chapter_text=chapter,
        )

        self.assertEqual(4, scene_count)
        self.assertLessEqual(scene_count - 1, 4)

    def test_branch_point_can_use_four_branches_but_never_more(self):
        chapter = "\n\n".join(["段落" * 2500] * 12)

        scene_count = runtime.plan_original_chapter_scene_count(
            "branch_point",
            chapter_text=chapter,
        )

        self.assertEqual(5, scene_count)
        self.assertEqual(4, scene_count - 1)

    def test_short_branch_point_also_reduces_its_soft_branch_budget(self):
        chapter = "\n\n".join(["段落" * 150] * 10)

        scene_count = runtime.plan_original_chapter_scene_count(
            "branch_point",
            chapter_text=chapter,
        )

        self.assertEqual(3, scene_count)
        self.assertEqual(2, scene_count - 1)

    def test_fantasy_estimate_reports_zero_soft_branches(self):
        counts = runtime.estimate_soft_branch_counts(
            [
                {
                    "node_id": "ch1",
                    "type": "chapter",
                    "is_original": True,
                    "source_chapter_idx": 0,
                },
                {"node_id": "ch1_alt", "type": "chapter", "is_original": False},
            ],
            ["正文" * 3000],
            branch_type="fantasy",
        )

        self.assertEqual([0, 0], counts)


class WhatIfSceneCheckpointTests(unittest.TestCase):
    def test_scene_checkpoint_can_resume_completed_scenes(self):
        state = runtime.initialize_whatif_scene_checkpoint(
            {"completed_scenes": [{"scene_index": 0, "content": "第一场正文。"}]},
            scene_count=3,
        )

        self.assertEqual(1, runtime.next_whatif_scene_index(state))
        runtime.record_whatif_scene_checkpoint(
            state,
            scene_index=1,
            content="第二场正文。",
            snapshot="人物仍在现场",
            accumulated_summary="第一、二场已经完成",
        )

        self.assertEqual(2, runtime.next_whatif_scene_index(state))
        self.assertEqual("第一场正文。\n\n第二场正文。", runtime.join_whatif_scene_content(state))


class WhatIfTerminalRoutingTests(unittest.TestCase):
    def test_incompatible_terminal_branch_is_never_force_converged(self):
        node = {
            "node_id": "whatif_1",
            "type": "chapter",
            "is_original": False,
            "terminal_branch": True,
            "convergence_status": "terminal",
            "next": [],
        }

        self.assertFalse(runtime.should_force_whatif_convergence(node, []))

    def test_nonterminal_whatif_without_children_can_use_safety_convergence(self):
        node = {
            "node_id": "whatif_2",
            "type": "chapter",
            "is_original": False,
            "next": [],
        }

        self.assertTrue(runtime.should_force_whatif_convergence(node, []))

    def test_whatif_budget_uses_eight_thousand_floor_for_normal_novels(self):
        budget = runtime.plan_whatif_budget(average_chapter_chars=6000)

        self.assertGreaterEqual(budget["target_chars"], 8000)
        self.assertLessEqual(budget["target_chars"], 12000)
        self.assertLessEqual(budget["target_chars"], 6000 * 2)
        self.assertIn(budget["soft_branch_count"], (3, 4))

    def test_estimator_never_assigns_more_than_four_soft_branches_to_whatif(self):
        counts = runtime.estimate_soft_branch_counts(
            [
                {"node_id": "ch1_alt", "type": "chapter", "is_original": False},
            ],
            ["正文" * 20000, "正文" * 20000],
        )

        self.assertLessEqual(counts[0], 4)

    def test_prebudgeted_target_is_not_scaled_a_second_time(self):
        from services.story_planner.core import branch_designer

        budget = branch_designer.resolve_whatif_budget(
            9000,
            already_budgeted=True,
        )

        self.assertEqual(9000, budget["target_chars"])
        self.assertEqual(4, budget["scene_count"])
        self.assertEqual(3, budget["soft_branch_count"])


class ChapterBranchPlanTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(runtime, "chapter_branch_runtime 尚未实现")
        self.topology = {
            "topology": [
                {"node_id": "ch1", "type": "chapter"},
                {"node_id": "ending_a", "type": "ending"},
            ],
            "variable_defs": [],
        }

    def test_initialize_plan_creates_pending_node_states(self):
        plan = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
        )

        self.assertEqual(plan["version"], runtime.PLAN_VERSION)
        self.assertEqual(plan["branch_type"], "default")
        self.assertEqual(plan["nodes"]["ch1"]["status"], "pending")
        self.assertEqual(plan["nodes"]["ending_a"]["status"], "pending")
        self.assertTrue(plan["topology_fingerprint"])

    def test_plan_initializes_versioned_pipeline_steps(self):
        plan = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
        )

        self.assertEqual(list(runtime.CHAPTER_BRANCH_STEP_ORDER), list(plan["steps"]))
        self.assertTrue(
            all(step["status"] == "pending" for step in plan["steps"].values())
        )

    def test_step_is_reused_only_when_input_and_producer_match(self):
        plan = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
        )
        step_inputs = {
            step: {"source": "v1", "step": step}
            for step in runtime.CHAPTER_BRANCH_STEP_ORDER
        }
        runtime.reconcile_plan_steps(plan, step_inputs)
        runtime.update_plan_step(
            plan,
            "novel_analysis",
            status="completed",
            output_digest="analysis-v1",
        )

        self.assertTrue(
            runtime.can_reuse_plan_step(
                plan,
                "novel_analysis",
                input_payload=step_inputs["novel_analysis"],
            )
        )
        self.assertFalse(
            runtime.can_reuse_plan_step(
                plan,
                "novel_analysis",
                input_payload={"source": "v2"},
            )
        )

    def test_changed_step_version_invalidates_that_step_and_downstream_only(self):
        plan = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
        )
        step_inputs = {
            step: {"source": "v1", "step": step}
            for step in runtime.CHAPTER_BRANCH_STEP_ORDER
        }
        runtime.reconcile_plan_steps(plan, step_inputs)
        for step in runtime.CHAPTER_BRANCH_STEP_ORDER:
            runtime.update_plan_step(
                plan,
                step,
                status="completed",
                output_digest=f"{step}-output",
            )

        versions = dict(runtime.CHAPTER_BRANCH_STEP_VERSIONS)
        versions["terminal_states"] = "terminal-v-next"
        invalidated = runtime.reconcile_plan_steps(
            plan,
            step_inputs,
            producer_versions=versions,
        )

        self.assertEqual(
            ["terminal_states", "node_generation", "release_gate", "story_publish", "summary_publish", "stage_complete"],
            invalidated,
        )
        self.assertEqual("completed", plan["steps"]["topology"]["status"])
        self.assertTrue(
            all(plan["steps"][step]["status"] == "pending" for step in invalidated)
        )

    def test_unrelated_earlier_steps_remain_completed_when_later_input_changes(self):
        plan = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
        )
        step_inputs = {
            step: {"source": "v1", "step": step}
            for step in runtime.CHAPTER_BRANCH_STEP_ORDER
        }
        runtime.reconcile_plan_steps(plan, step_inputs)
        for step in runtime.CHAPTER_BRANCH_STEP_ORDER:
            runtime.update_plan_step(
                plan,
                step,
                status="completed",
                output_digest=f"{step}-output",
            )
        changed_inputs = dict(step_inputs)
        changed_inputs["summary_publish"] = {"summary_schema": "v2"}

        invalidated = runtime.reconcile_plan_steps(plan, changed_inputs)

        self.assertEqual(["summary_publish", "stage_complete"], invalidated)
        self.assertEqual("completed", plan["steps"]["story_publish"]["status"])

    def test_node_generation_change_reopens_nodes_but_summary_change_does_not(self):
        plan = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
        )
        for node_id in plan["nodes"]:
            runtime.update_plan_node(
                plan,
                node_id,
                status="completed",
                artifact_sha256="digest",
            )
            plan["nodes"][node_id]["whatif_scene_checkpoint"] = {
                "status": "completed"
            }

        reopened = runtime.invalidate_checkpoint_outputs_for_steps(
            plan,
            ["node_generation", "release_gate", "story_publish"],
        )

        self.assertEqual(set(plan["nodes"]), set(reopened))
        self.assertTrue(
            all(entry["status"] == "pending" for entry in plan["nodes"].values())
        )
        self.assertTrue(
            all(not entry["artifact_sha256"] for entry in plan["nodes"].values())
        )
        self.assertTrue(
            all(not entry["whatif_scene_checkpoint"] for entry in plan["nodes"].values())
        )

        for node_id in plan["nodes"]:
            runtime.update_plan_node(plan, node_id, status="completed")
        self.assertEqual(
            [],
            runtime.invalidate_checkpoint_outputs_for_steps(
                plan,
                ["summary_publish", "stage_complete"],
            ),
        )
        self.assertTrue(
            all(entry["status"] == "completed" for entry in plan["nodes"].values())
        )

    def test_node_generation_identity_change_reopens_only_target_hint(self):
        plan = runtime.initialize_chapter_branch_plan(
            "task-1",
            {
                "topology": [
                    {"node_id": "ch1", "type": "chapter", "is_original": True},
                    {"node_id": "ch2", "type": "chapter", "is_original": True},
                    {"node_id": "ending_a", "type": "ending", "is_original": False},
                ],
                "variable_defs": [],
            },
            branch_type="default",
        )
        for node_id, entry in plan["nodes"].items():
            runtime.update_plan_node(
                plan,
                node_id,
                status="completed",
                artifact_sha256=f"digest-{node_id}",
                summary=f"summary-{node_id}",
            )

        reopened = runtime.invalidate_checkpoint_outputs_for_steps(
            plan,
            ["node_generation", "release_gate", "story_publish"],
            target_node_ids=["ending_a"],
        )

        self.assertEqual({"ending_a"}, set(reopened))
        self.assertEqual("completed", plan["nodes"]["ch1"]["status"])
        self.assertEqual("completed", plan["nodes"]["ch2"]["status"])
        self.assertEqual("pending", plan["nodes"]["ending_a"]["status"])
        self.assertEqual("digest-ch1", plan["nodes"]["ch1"]["artifact_sha256"])

    def test_node_generation_identity_change_with_self_heal_target_reopens_only_target(self):
        plan = runtime.initialize_chapter_branch_plan(
            "task-1",
            {
                "topology": [
                    {"node_id": "ch1", "type": "chapter", "is_original": True},
                    {"node_id": "ch2", "type": "chapter", "is_original": True},
                    {"node_id": "ending_a", "type": "ending", "is_original": False},
                ],
                "variable_defs": [],
            },
            branch_type="default",
        )
        for node_id, entry in plan["nodes"].items():
            runtime.update_plan_node(
                plan,
                node_id,
                status="completed",
                artifact_sha256=f"digest-{node_id}",
                summary=f"summary-{node_id}",
            )
        plan["node_self_heal_request"] = {
            "node_id": "ending_a",
            "attempt": 1,
            "status": "pending",
        }

        reopened = runtime.invalidate_checkpoint_outputs_for_steps(
            plan,
            ["node_generation", "release_gate"],
            target_node_ids=["ending_a"],
        )

        self.assertEqual(["ending_a"], reopened)
        self.assertEqual("completed", plan["nodes"]["ch1"]["status"])
        self.assertEqual("completed", plan["nodes"]["ch2"]["status"])
        self.assertEqual("pending", plan["nodes"]["ending_a"]["status"])
        self.assertEqual("digest-ch1", plan["nodes"]["ch1"]["artifact_sha256"])

    def test_initialize_plan_preserves_active_node_self_heal_request(self):
        original = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
        )
        original["node_self_heal_request"] = {
            "node_id": "ending_a",
            "attempt": 1,
            "status": "pending",
        }

        resumed = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
            existing_plan=original,
        )

        self.assertEqual(
            "ending_a",
            resumed["node_self_heal_request"]["node_id"],
        )

    def test_initialize_plan_keeps_completed_upstream_for_self_heal_target_topology_refresh(self):
        topology = {
            "topology": [
                {"node_id": "ch1", "type": "chapter", "is_original": True, "next": ["ending_a"]},
                {"node_id": "ending_a", "type": "ending", "is_original": False, "next": [], "title": "old"},
            ],
            "variable_defs": [],
        }
        plan = runtime.initialize_chapter_branch_plan(
            "task-1", topology, branch_type="default", source_fingerprint="source"
        )
        for step_name in ("novel_analysis", "topology", "terminal_states"):
            runtime.update_plan_step(plan, step_name, status="completed", output_digest=f"{step_name}-digest")
        for node_id in plan["nodes"]:
            runtime.update_plan_node(plan, node_id, status="completed", artifact_sha256=f"digest-{node_id}")
        plan["node_self_heal_request"] = {"node_id": "ending_a", "attempt": 1, "status": "pending"}

        refreshed = {
            "topology": [
                {"node_id": "ch1", "type": "chapter", "is_original": True, "next": ["ending_a"]},
                {"node_id": "ending_a", "type": "ending", "is_original": False, "next": [], "title": "repaired title"},
            ],
            "variable_defs": [],
        }
        resumed = runtime.initialize_chapter_branch_plan(
            "task-1", refreshed, branch_type="default", existing_plan=plan, source_fingerprint="source"
        )

        self.assertEqual("completed", resumed["nodes"]["ch1"]["status"])
        self.assertEqual("completed", resumed["steps"]["novel_analysis"]["status"])
        self.assertEqual("ending_a", resumed["node_self_heal_request"]["node_id"])

    def test_node_self_heal_request_can_be_completed_without_affecting_other_nodes(self):
        plan = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
        )
        plan["node_self_heal_request"] = {
            "node_id": "ending_a",
            "attempt": 1,
            "status": "pending",
        }

        self.assertTrue(runtime.mark_node_self_heal_completed(plan, "ending_a"))
        self.assertEqual("completed", plan["node_self_heal_request"]["status"])
        self.assertEqual([], runtime.get_active_node_self_heal_targets(plan))

    def test_reopening_target_clears_only_that_nodes_semantic_review_cache(self):
        plan = runtime.initialize_chapter_branch_plan(
            "task-1",
            {
                "topology": [
                    {"node_id": "ch1", "type": "chapter", "is_original": True},
                    {"node_id": "ending_a", "type": "ending", "is_original": False},
                ],
                "variable_defs": [],
            },
            branch_type="default",
        )
        plan["nodes"]["ch1"]["ending_semantic_review"] = {"passed": True}
        plan["nodes"]["ending_a"]["ending_semantic_review"] = {"passed": False}

        runtime.reopen_plan_nodes(
            plan,
            ["ending_a"],
            include_non_original_descendants=False,
            reason="发布级定向修复",
        )

        self.assertEqual({"passed": True}, plan["nodes"]["ch1"]["ending_semantic_review"])
        self.assertEqual({}, plan["nodes"]["ending_a"].get("ending_semantic_review"))
        self.assertEqual({}, plan["nodes"]["ending_a"].get("semantic_reviews"))

    def test_node_generation_invalidation_clears_all_semantic_review_caches(self):
        plan = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
        )
        for entry in plan["nodes"].values():
            entry["ending_semantic_review"] = {"passed": True}

        reopened = runtime.invalidate_checkpoint_outputs_for_steps(
            plan,
            ["node_generation", "release_gate"],
        )

        self.assertEqual(set(plan["nodes"]), set(reopened))
        self.assertTrue(
            all(not entry.get("ending_semantic_review") for entry in plan["nodes"].values())
        )
        self.assertTrue(
            all(not entry.get("semantic_reviews") for entry in plan["nodes"].values())
        )

    def test_ending_semantic_review_cache_requires_exact_identity(self):
        plan = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
        )
        fingerprint = runtime.ending_semantic_review_fingerprint(
            variable={"key": "courage", "high_desc": "主动面对真相"},
            content="她推开门，让所有人看见真相。",
            provider="openai",
            model="gpt-5.6-sol",
        )
        review = {"passed": True, "confidence": 0.96}

        runtime.record_ending_semantic_review(
            plan,
            "ending_a",
            fingerprint=fingerprint,
            review=review,
        )

        self.assertEqual(
            review,
            runtime.get_reusable_ending_semantic_review(
                plan,
                "ending_a",
                fingerprint=fingerprint,
            ),
        )
        changed_content_fingerprint = runtime.ending_semantic_review_fingerprint(
            variable={"key": "courage", "high_desc": "主动面对真相"},
            content="她关上门，独自离开。",
            provider="openai",
            model="gpt-5.6-sol",
        )
        self.assertIsNone(
            runtime.get_reusable_ending_semantic_review(
                plan,
                "ending_a",
                fingerprint=changed_content_fingerprint,
            )
        )

    def test_ending_semantic_review_fingerprint_is_node_specific(self):
        common = {
            "variable": {"key": "courage", "high_desc": "主动面对真相"},
            "content": "她推开门，让所有人看见真相。",
            "provider": "openai",
            "model": "gpt-5.6-sol",
        }

        ending_a = runtime.ending_semantic_review_fingerprint(
            **common,
            context={"topology_node_id": "ending_a", "ending_id": "node-a"},
        )
        ending_a_with_gate_context = runtime.ending_semantic_review_fingerprint(
            **common,
            context={
                "topology_node_id": "ending_a",
                "ending_id": "node-a",
                "title": "结局·勇气",
                "route": {"variable": "courage", "threshold": 70},
            },
        )
        ending_b = runtime.ending_semantic_review_fingerprint(
            **common,
            context={"topology_node_id": "ending_b", "ending_id": "node-b"},
        )

        self.assertEqual(ending_a, ending_a_with_gate_context)
        self.assertNotEqual(ending_a, ending_b)

    def test_reconciling_normalized_topology_does_not_reopen_completed_nodes(self):
        raw = {
            "topology": [
                {"node_id": "ch1", "type": "chapter", "is_original": True,
                 "source_chapter_idx": 0, "next": ["ending_a"]},
                {"node_id": "ending_a", "type": "ending", "is_original": False,
                 "source_chapter_idx": 1, "next": [], "choice_text": "继续原著路线"},
            ],
            "variable_defs": [],
        }
        normalized = {
            "topology": [
                {"node_id": "ch1", "type": "chapter", "is_original": True,
                 "source_chapter_idx": 0, "next": ["ending_a"]},
                {"node_id": "ending_a", "type": "ending", "is_original": False,
                 "source_chapter_idx": 1, "next": [], "choice_text": ""},
            ],
            "variable_defs": [],
        }
        plan = runtime.initialize_chapter_branch_plan(
            "task-1", raw, branch_type="default"
        )
        runtime.update_plan_node(
            plan, "ch1", status="completed", artifact_sha256="source-hash"
        )

        resumed = runtime.initialize_chapter_branch_plan(
            "task-1", normalized, branch_type="default", existing_plan=plan
        )
        self.assertEqual("completed", resumed["nodes"]["ch1"]["status"])

    def test_legacy_raw_topology_fingerprint_is_canonicalized_before_comparison(self):
        raw = {
            "topology": [
                {"node_id": "ch1", "type": "chapter", "is_original": True,
                 "source_chapter_idx": 0, "next": ["ending_a"]},
                {"node_id": "ending_a", "type": "ending", "is_original": False,
                 "source_chapter_idx": 1, "next": [], "choice_text": "继续原著路线"},
            ],
            "variable_defs": [],
        }
        normalized = {
            "topology": [
                {"node_id": "ch1", "type": "chapter", "is_original": True,
                 "source_chapter_idx": 0, "next": ["ending_a"]},
                {"node_id": "ending_a", "type": "ending", "is_original": False,
                 "source_chapter_idx": 1, "next": [], "choice_text": ""},
            ],
            "variable_defs": [],
        }
        plan = runtime.initialize_chapter_branch_plan(
            "task-1", raw, branch_type="default"
        )
        runtime.update_plan_node(plan, "ch1", status="completed")
        # Simulate a plan written before ending choice normalization existed.
        plan["topology_fingerprint"] = runtime.content_sha256(
            json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )

        resumed = runtime.initialize_chapter_branch_plan(
            "task-1", normalized, branch_type="default", existing_plan=plan
        )

        self.assertEqual("completed", resumed["nodes"]["ch1"]["status"])

    def test_topology_fingerprint_is_stable_across_ending_choice_normalization(self):
        raw = {
            "topology": [
                {"node_id": "ch1", "type": "chapter", "is_original": True,
                 "source_chapter_idx": 0, "next": ["ending_a"]},
                {"node_id": "ending_a", "type": "ending", "is_original": False,
                 "source_chapter_idx": 1, "next": [], "choice_text": "继续原著路线"},
            ],
            "variable_defs": [],
        }
        normalized = {
            "topology": [
                {"node_id": "ch1", "type": "chapter", "is_original": True,
                 "source_chapter_idx": 0, "next": ["ending_a"]},
                {"node_id": "ending_a", "type": "ending", "is_original": False,
                 "source_chapter_idx": 1, "next": [], "choice_text": ""},
            ],
            "variable_defs": [],
        }

        self.assertEqual(
            runtime.topology_fingerprint(raw), runtime.topology_fingerprint(normalized)
        )

    def test_runtime_derived_topology_fields_do_not_reset_targeted_repair(self):
        """Summaries/parent ids added during assembly must not reopen the novel."""
        base = {
            "topology": [
                {
                    "node_id": "ch1", "type": "chapter", "is_original": True,
                    "source_chapter_idx": 0, "next": ["ending_1", "ending_2"],
                },
                {
                    "node_id": "ending_1", "type": "ending", "is_original": False,
                    "source_chapter_idx": 1, "next": [],
                },
                {
                    "node_id": "ending_2", "type": "ending", "is_original": False,
                    "source_chapter_idx": 1, "next": [],
                },
            ],
            "variable_defs": [],
        }
        enriched = {
            "topology": [
                {**base["topology"][0], "summary": "真实章节摘要", "parent_ids": []},
                {
                    **base["topology"][1], "summary": "修复目标一摘要",
                    "parent_ids": ["ch1"], "plan_direction": "目标一方向",
                },
                {
                    **base["topology"][2], "summary": "修复目标二摘要",
                    "parent_ids": ["ch1"], "plan_direction": "目标二方向",
                },
            ],
            "variable_defs": [],
        }
        plan = runtime.initialize_chapter_branch_plan("task-1", base, branch_type="default")
        for node_id in plan["nodes"]:
            runtime.update_plan_node(plan, node_id, status="completed")
        runtime.reopen_plan_nodes(
            plan, ["ending_1", "ending_2"],
            include_non_original_descendants=False,
            reason="发布级定向修复",
        )

        resumed = runtime.initialize_chapter_branch_plan(
            "task-1", enriched, branch_type="default", existing_plan=plan
        )

        self.assertEqual("completed", resumed["nodes"]["ch1"]["status"])
        self.assertEqual("pending", resumed["nodes"]["ending_1"]["status"])
        self.assertEqual("pending", resumed["nodes"]["ending_2"]["status"])
        self.assertEqual(
            ["ending_1", "ending_2"], resumed["regeneration_request"]["node_ids"]
        )

    def test_active_targeted_repair_preserves_scope_when_topology_checkpoint_changes(self):
        """A targeted repair must not fall back to a full node reset."""
        base = {
            "topology": [
                {"node_id": "ch1", "type": "chapter", "is_original": True,
                 "source_chapter_idx": 0, "next": ["ending_1"]},
                {"node_id": "ending_1", "type": "ending", "is_original": False,
                 "source_chapter_idx": 1, "next": []},
            ],
            "variable_defs": [],
        }
        changed = {
            "topology": [
                {"node_id": "ch1", "type": "chapter", "is_original": True,
                 "source_chapter_idx": 0, "next": ["ending_1"]},
                {"node_id": "ending_1", "type": "ending", "is_original": False,
                 "source_chapter_idx": 1, "next": [], "branch_threshold": 71},
            ],
            "variable_defs": [],
        }
        plan = runtime.initialize_chapter_branch_plan("task-1", base, branch_type="default")
        for step_name in ("novel_analysis", "topology", "terminal_states"):
            runtime.update_plan_step(plan, step_name, status="completed")
        for node_id in plan["nodes"]:
            runtime.update_plan_node(plan, node_id, status="completed")
        runtime.reopen_plan_nodes(
            plan, ["ending_1"], include_non_original_descendants=False,
            reason="发布级定向修复",
        )

        resumed = runtime.initialize_chapter_branch_plan(
            "task-1", changed, branch_type="default", existing_plan=plan
        )

        self.assertEqual("completed", resumed["nodes"]["ch1"]["status"])
        self.assertEqual("pending", resumed["nodes"]["ending_1"]["status"])
        self.assertEqual(["ending_1"], resumed["regeneration_request"]["node_ids"])

    def test_targeted_repair_does_not_hide_a_real_target_edge_change(self):
        base = {
            "topology": [
                {"node_id": "ch1", "type": "chapter", "is_original": True,
                 "source_chapter_idx": 0, "next": ["ending_1"]},
                {"node_id": "ending_1", "type": "ending", "is_original": False,
                 "source_chapter_idx": 1, "next": []},
            ],
            "variable_defs": [],
        }
        changed = {
            "topology": [
                {"node_id": "ch1", "type": "chapter", "is_original": True,
                 "source_chapter_idx": 0, "next": ["ending_1"]},
                {"node_id": "ending_1", "type": "ending", "is_original": False,
                 "source_chapter_idx": 1, "next": ["ch1"]},
            ],
            "variable_defs": [],
        }
        plan = runtime.initialize_chapter_branch_plan("task-1", base, branch_type="default")
        for step_name in ("novel_analysis", "topology", "terminal_states"):
            runtime.update_plan_step(plan, step_name, status="completed")
        for node_id in plan["nodes"]:
            runtime.update_plan_node(plan, node_id, status="completed")
        runtime.reopen_plan_nodes(
            plan, ["ending_1"], include_non_original_descendants=False,
            reason="发布级定向修复",
        )

        resumed = runtime.initialize_chapter_branch_plan(
            "task-1", changed, branch_type="default", existing_plan=plan
        )

        self.assertEqual("pending", resumed["nodes"]["ch1"]["status"])
        self.assertEqual({}, resumed["regeneration_request"])

    def test_targeted_regeneration_request_survives_plan_reconciliation(self):
        plan = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
        )
        runtime.reopen_plan_nodes(
            plan,
            ["ending_a"],
            include_non_original_descendants=False,
            reason="发布级定向修复",
        )

        resumed = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
            existing_plan=plan,
        )

        self.assertEqual(
            ["ending_a"],
            resumed["regeneration_request"]["node_ids"],
        )

    def test_node_generation_version_change_reopens_only_target_with_stale_request(self):
        plan = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
        )
        step_inputs = {
            step: {"source": "v1", "step": step}
            for step in runtime.CHAPTER_BRANCH_STEP_ORDER
        }
        runtime.reconcile_plan_steps(plan, step_inputs)
        for step_name in runtime.CHAPTER_BRANCH_STEP_ORDER:
            runtime.update_plan_step(plan, step_name, status="completed")
        for node_id in plan["nodes"]:
            runtime.update_plan_node(plan, node_id, status="completed")
        runtime.reopen_plan_nodes(
            plan,
            ["ending_a"],
            include_non_original_descendants=False,
            reason="发布级定向修复",
        )

        resumed = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
            existing_plan=plan,
        )
        changed_inputs = dict(step_inputs)
        changed_inputs["node_generation"] = {"source": "v2", "step": "node_generation"}
        invalidated = runtime.reconcile_plan_steps(resumed, changed_inputs)
        reopened = runtime.invalidate_checkpoint_outputs_for_steps(
            resumed,
            invalidated,
            target_node_ids=runtime.get_active_regeneration_targets(resumed),
        )

        self.assertEqual(
            ["node_generation", "release_gate", "story_publish", "summary_publish", "stage_complete"],
            invalidated,
        )
        self.assertEqual({"ending_a"}, set(reopened))
        self.assertEqual("completed", resumed["nodes"]["ch1"]["status"])
        self.assertEqual("pending", resumed["nodes"]["ending_a"]["status"])

    def test_upstream_input_change_overrides_targeted_scope_and_reopens_all_nodes(self):
        plan = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
        )
        step_inputs = {
            step: {"source": "v1", "step": step}
            for step in runtime.CHAPTER_BRANCH_STEP_ORDER
        }
        runtime.reconcile_plan_steps(plan, step_inputs)
        for step_name in runtime.CHAPTER_BRANCH_STEP_ORDER:
            runtime.update_plan_step(plan, step_name, status="completed")
        for node_id in plan["nodes"]:
            runtime.update_plan_node(
                plan,
                node_id,
                status="completed",
                artifact_sha256=f"digest-{node_id}",
            )
        runtime.reopen_plan_nodes(
            plan,
            ["ending_a"],
            include_non_original_descendants=False,
            reason="发布级定向修复",
        )

        changed_inputs = dict(step_inputs)
        changed_inputs["terminal_states"] = {
            "source": "v2",
            "step": "terminal_states",
        }
        invalidated = runtime.reconcile_plan_steps(plan, changed_inputs)
        reopened = runtime.invalidate_checkpoint_outputs_for_steps(
            plan,
            invalidated,
            target_node_ids=runtime.get_active_regeneration_targets(plan),
        )

        self.assertEqual(
            ["terminal_states", "node_generation", "release_gate", "story_publish", "summary_publish", "stage_complete"],
            invalidated,
        )
        self.assertEqual(set(plan["nodes"]), set(reopened))
        self.assertTrue(
            all(entry["status"] == "pending" for entry in plan["nodes"].values())
        )

    def test_completed_targeted_regeneration_request_is_not_active(self):
        plan = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
        )
        runtime.reopen_plan_nodes(
            plan,
            ["ending_a"],
            include_non_original_descendants=False,
            reason="发布级定向修复",
        )
        plan["regeneration_request"]["status"] = "completed"

        self.assertEqual(
            [],
            runtime.get_active_regeneration_targets(plan),
        )

    def test_empty_regeneration_request_is_not_marked_completed(self):
        plan = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
        )

        self.assertFalse(runtime.mark_regeneration_request_completed(plan))

    def test_already_completed_regeneration_request_is_not_reprocessed(self):
        plan = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
        )
        runtime.reopen_plan_nodes(
            plan,
            ["ending_a"],
            include_non_original_descendants=False,
            reason="发布级定向修复",
        )
        plan["nodes"]["ending_a"]["status"] = "completed"
        self.assertTrue(runtime.mark_regeneration_request_completed(plan))
        self.assertFalse(runtime.mark_regeneration_request_completed(plan))

    def test_chapter_substep_reuses_only_complete_parseable_matching_output(self):
        plan = {}
        payload = {"chapter_sha256": "chapter-v1", "inherited": "state-v0"}
        states = {"主角": {"life_status": "alive_confirmed"}}
        runtime.record_chapter_substep(
            plan,
            "terminal_states",
            0,
            input_payload=payload,
            output=states,
            producer_version="terminal-v1",
        )

        self.assertEqual(
            states,
            runtime.get_reusable_chapter_substep(
                plan,
                "terminal_states",
                0,
                input_payload=payload,
                producer_version="terminal-v1",
            ),
        )
        plan["chapter_substeps"]["terminal_states"]["0"]["output_digest"] = "bad"
        self.assertIsNone(
            runtime.get_reusable_chapter_substep(
                plan,
                "terminal_states",
                0,
                input_payload=payload,
                producer_version="terminal-v1",
            )
        )

    def test_changed_chapter_substep_input_invalidates_that_chapter_and_later_chain(self):
        plan = {}
        for index in range(3):
            runtime.record_chapter_substep(
                plan,
                "terminal_states",
                index,
                input_payload={"chapter": index, "version": "v1"},
                output={"chapter": index},
                producer_version="terminal-v1",
            )

        removed = runtime.invalidate_chapter_substeps_from(
            plan,
            "terminal_states",
            1,
        )

        self.assertEqual([1, 2], removed)
        self.assertIn("0", plan["chapter_substeps"]["terminal_states"])
        self.assertNotIn("1", plan["chapter_substeps"]["terminal_states"])

    def test_soft_branch_gap_checkpoint_keeps_only_contiguous_verified_gaps(self):
        state = runtime.initialize_soft_branch_gap_checkpoint(
            {
                "input_digest": runtime.stable_payload_digest({"chapter": "v1"}),
                "completed_gaps": [
                    {
                        "gap_index": 0,
                        "marker": "marker-0",
                        "marker_sha256": runtime.content_sha256("marker-0"),
                        "omitted": False,
                    },
                    {
                        "gap_index": 2,
                        "marker": "marker-2",
                        "marker_sha256": runtime.content_sha256("marker-2"),
                        "omitted": False,
                    },
                ],
            },
            gap_count=3,
            input_payload={"chapter": "v1"},
        )

        self.assertEqual([0], [item["gap_index"] for item in state["completed_gaps"]])
        self.assertEqual("marker-0", runtime.get_soft_branch_gap(state, 0)["marker"])
        self.assertIsNone(runtime.get_soft_branch_gap(state, 1))

    def test_soft_branch_gap_checkpoint_resets_when_input_changes(self):
        state = runtime.initialize_soft_branch_gap_checkpoint(
            {
                "input_digest": runtime.stable_payload_digest({"chapter": "v1"}),
                "completed_gaps": [
                    {
                        "gap_index": 0,
                        "marker": "marker-0",
                        "marker_sha256": runtime.content_sha256("marker-0"),
                        "omitted": False,
                    },
                ],
            },
            gap_count=2,
            input_payload={"chapter": "v2"},
        )

        self.assertEqual([], state["completed_gaps"])

    def test_soft_branch_gap_checkpoint_records_omitted_gap_as_complete(self):
        state = runtime.initialize_soft_branch_gap_checkpoint(
            None,
            gap_count=2,
            input_payload={"chapter": "v1"},
        )
        runtime.record_soft_branch_gap(
            state,
            gap_index=0,
            marker="",
            omitted=True,
        )

        self.assertTrue(runtime.get_soft_branch_gap(state, 0)["omitted"])
        self.assertEqual("running", state["status"])

    def test_soft_branch_gap_checkpoint_discards_tampered_or_unhashed_marker(self):
        input_payload = {"chapter": "v1"}
        state = runtime.initialize_soft_branch_gap_checkpoint(
            {
                "input_digest": runtime.stable_payload_digest(input_payload),
                "completed_gaps": [
                    {
                        "gap_index": 0,
                        "marker": "marker-tampered",
                        "marker_sha256": runtime.content_sha256("marker-original"),
                        "omitted": False,
                    },
                    {
                        "gap_index": 1,
                        "marker": "legacy-unhashed",
                        "omitted": False,
                    },
                ],
            },
            gap_count=2,
            input_payload=input_payload,
        )

        self.assertEqual([], state["completed_gaps"])

    def test_initialize_plan_preserves_completed_checkpoint(self):
        existing = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
        )
        runtime.update_plan_node(
            existing,
            "ch1",
            status="completed",
            real_id="chapter_1",
            terminal_states={"女主": {"alive": True}},
            summary="真实摘要",
        )

        resumed = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
            existing_plan=existing,
        )

        self.assertEqual(resumed["nodes"]["ch1"]["status"], "completed")
        self.assertEqual(resumed["nodes"]["ch1"]["real_id"], "chapter_1")
        self.assertEqual(resumed["nodes"]["ch1"]["summary"], "真实摘要")

    def test_initialize_plan_invalidates_checkpoint_from_older_state_schema(self):
        existing = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
        )
        runtime.update_plan_node(
            existing,
            "ch1",
            status="completed",
            real_id="chapter_1",
            terminal_states={"女主": {"alive": True}},
        )
        existing["version"] = 3

        resumed = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
            existing_plan=existing,
        )

        self.assertEqual("pending", resumed["nodes"]["ch1"]["status"])
        self.assertEqual("plan_version_changed", resumed["checkpoint_invalidation_reason"])

    def test_version_five_plan_is_migrated_without_discarding_verified_nodes(self):
        existing = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
        )
        runtime.update_plan_node(
            existing,
            "ch1",
            status="completed",
            artifact_sha256="verified-node",
        )
        existing["version"] = 5
        existing.pop("steps", None)

        resumed = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
            existing_plan=existing,
        )

        self.assertEqual(runtime.PLAN_VERSION, resumed["version"])
        self.assertEqual("completed", resumed["nodes"]["ch1"]["status"])
        self.assertEqual("verified-node", resumed["nodes"]["ch1"]["artifact_sha256"])
        self.assertEqual("plan_schema_upgraded", resumed["checkpoint_invalidation_reason"])

    def test_initialize_plan_preserves_verified_chapter_substeps_for_same_inputs(self):
        existing = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
            source_fingerprint="source-v1",
        )
        runtime.record_chapter_substep(
            existing,
            "terminal_states",
            0,
            input_payload={"chapter_sha256": "chapter-v1"},
            output={"主角": {"alive": True}},
            producer_version="terminal-v1",
        )

        resumed = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
            existing_plan=existing,
            source_fingerprint="source-v1",
        )

        self.assertEqual(
            existing["chapter_substeps"],
            resumed["chapter_substeps"],
        )

    def test_initialize_plan_discards_chapter_substeps_when_source_changes(self):
        existing = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
            source_fingerprint="source-v1",
        )
        runtime.record_chapter_substep(
            existing,
            "terminal_states",
            0,
            input_payload={"chapter_sha256": "chapter-v1"},
            output={"主角": {"alive": True}},
            producer_version="terminal-v1",
        )

        resumed = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
            existing_plan=existing,
            source_fingerprint="source-v2",
        )

        self.assertEqual({}, resumed["chapter_substeps"])

    def test_step_output_is_reused_only_when_payload_is_complete_and_unchanged(self):
        plan = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
            source_fingerprint="source-v1",
        )
        input_payload = {"source_fingerprint": "source-v1", "model": "gpt-5.6-sol"}
        output = {"worldview": {"genre": "mystery"}, "chapter_summaries": ["摘要"]}
        runtime.record_plan_step_output(
            plan,
            "novel_analysis",
            input_payload=input_payload,
            output=output,
            producer_version="analysis-v1",
        )

        self.assertEqual(
            output,
            runtime.get_reusable_plan_step_output(
                plan,
                "novel_analysis",
                input_payload=input_payload,
                producer_version="analysis-v1",
            ),
        )
        plan["step_outputs"]["novel_analysis"]["output_digest"] = "corrupt"
        self.assertIsNone(
            runtime.get_reusable_plan_step_output(
                plan,
                "novel_analysis",
                input_payload=input_payload,
                producer_version="analysis-v1",
            )
        )

    def test_initialize_plan_preserves_step_outputs_but_source_change_discards_them(self):
        existing = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
            source_fingerprint="source-v1",
        )
        runtime.record_plan_step_output(
            existing,
            "novel_analysis",
            input_payload={"source_fingerprint": "source-v1"},
            output={"chapter_summaries": ["摘要"]},
            producer_version="analysis-v1",
        )

        same_source = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
            existing_plan=existing,
            source_fingerprint="source-v1",
        )
        changed_source = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
            existing_plan=existing,
            source_fingerprint="source-v2",
        )

        self.assertIn("novel_analysis", same_source["step_outputs"])
        self.assertEqual({}, changed_source["step_outputs"])

    def test_initialize_plan_preserves_global_style_checkpoint_for_same_source_only(self):
        existing = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
            source_fingerprint="source-v1",
        )
        existing["global_style_checkpoint"] = {
            "schema_version": 1,
            "style": "persisted style",
        }

        same_source = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
            existing_plan=existing,
            source_fingerprint="source-v1",
        )
        changed_source = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
            existing_plan=existing,
            source_fingerprint="source-v2",
        )

        self.assertEqual(
            existing["global_style_checkpoint"],
            same_source["global_style_checkpoint"],
        )
        self.assertEqual({}, changed_source["global_style_checkpoint"])

    def test_empty_bootstrap_topology_is_never_reused_as_a_completed_topology(self):
        bootstrap = runtime.initialize_chapter_branch_plan(
            "task-1",
            {"topology": [], "variable_defs": []},
            branch_type="default",
            source_fingerprint="source-v1",
        )

        self.assertFalse(
            runtime.has_reusable_topology_checkpoint(
                bootstrap,
                branch_type="default",
                analysis_step_reusable=True,
                topology_step_reusable=True,
            )
        )

    def test_nonempty_compatible_topology_can_be_reused(self):
        plan = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
            source_fingerprint="source-v1",
        )

        self.assertTrue(
            runtime.has_reusable_topology_checkpoint(
                plan,
                branch_type="default",
                analysis_step_reusable=True,
                topology_step_reusable=True,
            )
        )

    def test_first_real_topology_preserves_preanalysis_output_from_bootstrap_plan(self):
        bootstrap = runtime.initialize_chapter_branch_plan(
            "task-1",
            {"topology": [], "variable_defs": []},
            branch_type="default",
            source_fingerprint="source-v1",
        )
        runtime.record_plan_step_output(
            bootstrap,
            "novel_analysis",
            input_payload={"source_fingerprint": "source-v1"},
            output={"chapter_summaries": ["摘要"]},
            producer_version="analysis-v1",
        )

        planned = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
            existing_plan=bootstrap,
            source_fingerprint="source-v1",
        )

        self.assertIn("novel_analysis", planned["step_outputs"])
        self.assertEqual("", planned["checkpoint_invalidation_reason"])

    def test_failed_running_node_is_reopened_for_retry(self):
        existing = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
        )
        runtime.update_plan_node(
            existing,
            "ending_a",
            status="running",
            error="worker interrupted",
        )

        resumed = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
            existing_plan=existing,
        )

        self.assertEqual(resumed["nodes"]["ending_a"]["status"], "pending")
        self.assertIn("worker interrupted", resumed["nodes"]["ending_a"]["error"])

    def test_running_nodes_are_marked_failed_when_pipeline_aborts(self):
        plan = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
        )
        runtime.update_plan_node(plan, "ch1", status="completed")
        runtime.update_plan_node(plan, "ending_a", status="running")

        changed = runtime.mark_running_nodes_failed(plan, "gateway timeout")

        self.assertTrue(changed)
        self.assertEqual(plan["nodes"]["ch1"]["status"], "completed")
        self.assertEqual(plan["nodes"]["ending_a"]["status"], "failed")
        self.assertEqual(plan["nodes"]["ending_a"]["error"], "gateway timeout")

    def test_changed_source_fingerprint_invalidates_completed_checkpoints(self):
        self.assertIn(
            "source_fingerprint",
            inspect.signature(runtime.initialize_chapter_branch_plan).parameters,
            "计划初始化尚未支持原文指纹",
        )
        existing = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
            source_fingerprint="source-v1",
        )
        runtime.update_plan_node(existing, "ch1", status="completed")

        resumed = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
            existing_plan=existing,
            source_fingerprint="source-v2",
        )

        self.assertEqual(resumed["nodes"]["ch1"]["status"], "pending")
        self.assertEqual(resumed["source_fingerprint"], "source-v2")

    def test_changed_topology_invalidates_completed_checkpoints(self):
        existing = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
        )
        runtime.update_plan_node(existing, "ch1", status="completed")
        changed_topology = {
            "topology": self.topology["topology"]
            + [{"node_id": "ending_b", "type": "ending"}],
            "variable_defs": [],
        }

        resumed = runtime.initialize_chapter_branch_plan(
            "task-1",
            changed_topology,
            branch_type="default",
            existing_plan=existing,
        )

        self.assertEqual(resumed["nodes"]["ch1"]["status"], "pending")
        self.assertIn("ending_b", resumed["nodes"])

    def test_refreshing_fingerprint_after_runtime_topology_enrichment_preserves_checkpoints(self):
        existing = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
        )
        runtime.update_plan_node(existing, "ch1", status="completed")
        existing["topology_result"]["topology"][0]["summary"] = "运行后生成的章节摘要"

        runtime.refresh_plan_topology_fingerprint(existing)
        resumed = runtime.initialize_chapter_branch_plan(
            "task-1",
            existing["topology_result"],
            branch_type="default",
            existing_plan=existing,
        )

        self.assertEqual(resumed["nodes"]["ch1"]["status"], "completed")

    def test_persisting_checkpoint_resumes_only_when_artifact_hash_matches(self):
        self.assertTrue(
            hasattr(runtime, "content_sha256"),
            "尚未实现节点正文哈希",
        )
        self.assertTrue(
            hasattr(runtime, "can_resume_node_checkpoint"),
            "尚未实现两阶段检查点恢复校验",
        )
        self.assertIn(
            "artifact_sha256",
            inspect.signature(runtime.update_plan_node).parameters,
            "节点状态尚未保存正文哈希",
        )
        plan = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
        )
        content = "# 第一章\n\n已经落库的正文"
        runtime.update_plan_node(
            plan,
            "ch1",
            status="persisting",
            artifact_sha256=runtime.content_sha256(content),
        )

        self.assertTrue(
            runtime.can_resume_node_checkpoint(plan["nodes"]["ch1"], content)
        )
        self.assertFalse(
            runtime.can_resume_node_checkpoint(
                plan["nodes"]["ch1"],
                content + "已被篡改",
            )
        )

    def test_legacy_completed_checkpoint_without_hash_must_regenerate(self):
        self.assertTrue(
            hasattr(runtime, "can_resume_node_checkpoint"),
            "尚未实现检查点恢复校验",
        )
        entry = {"status": "completed", "artifact_sha256": ""}

        self.assertFalse(runtime.can_resume_node_checkpoint(entry, "旧版完整正文"))

    def test_targeted_regeneration_reopens_non_original_descendants_only(self):
        self.assertTrue(
            hasattr(runtime, "reopen_plan_nodes"),
            "尚未实现失败节点定向重开",
        )
        self.assertIn(
            "artifact_sha256",
            inspect.signature(runtime.update_plan_node).parameters,
            "节点状态尚未保存正文哈希",
        )
        topology = {
            "topology": [
                {
                    "node_id": "pivot",
                    "type": "branch_point",
                    "is_original": True,
                    "next": ["whatif", "original_sibling"],
                },
                {
                    "node_id": "whatif",
                    "type": "chapter",
                    "is_original": False,
                    "next": ["whatif_ending", "convergence"],
                },
                {
                    "node_id": "whatif_ending",
                    "type": "ending",
                    "is_original": False,
                    "next": [],
                },
                {
                    "node_id": "original_sibling",
                    "type": "chapter",
                    "is_original": True,
                    "next": ["convergence"],
                },
                {
                    "node_id": "convergence",
                    "type": "chapter",
                    "is_original": True,
                    "next": [],
                },
            ],
            "variable_defs": [],
        }
        plan = runtime.initialize_chapter_branch_plan(
            "task-1",
            topology,
            branch_type="default",
        )
        for node_id in plan["nodes"]:
            runtime.update_plan_node(
                plan,
                node_id,
                status="completed",
                artifact_sha256="old-hash",
                terminal_states={"主角": {"alive": True}},
                summary="旧摘要",
            )

        reopened = runtime.reopen_plan_nodes(
            plan,
            ["whatif"],
            include_non_original_descendants=True,
            reason="人工定向重生成",
        )

        self.assertEqual(reopened, ["whatif", "whatif_ending"])
        self.assertEqual(plan["nodes"]["whatif"]["status"], "pending")
        self.assertEqual(plan["nodes"]["whatif_ending"]["status"], "pending")
        self.assertEqual(plan["nodes"]["convergence"]["status"], "completed")
        self.assertEqual(plan["nodes"]["original_sibling"]["status"], "completed")
        self.assertEqual(plan["nodes"]["whatif"]["artifact_sha256"], "")
        self.assertEqual(plan["nodes"]["whatif"]["terminal_states"], {})
        self.assertEqual(plan["nodes"]["whatif"]["summary"], "")
        self.assertEqual(plan["nodes"]["whatif"]["regeneration_count"], 1)

    def test_targeted_regeneration_rejects_unknown_node(self):
        self.assertTrue(
            hasattr(runtime, "reopen_plan_nodes"),
            "尚未实现失败节点定向重开",
        )
        plan = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
        )

        with self.assertRaisesRegex(ValueError, "不存在"):
            runtime.reopen_plan_nodes(plan, ["missing"])

    def test_node_self_heal_reopens_only_failed_node_and_preserves_first_checkpoint(self):
        plan = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
        )
        plan["nodes"]["ch1"]["status"] = "running"
        plan["nodes"]["ch1"]["artifact_sha256"] = "bad-artifact"
        plan["nodes"]["ch1"]["whatif_scene_checkpoint"] = {"completed_scenes": [1]}

        result = runtime.schedule_node_self_heal(
            plan,
            "ch1",
            reason="正文末句截断",
            max_attempts=3,
        )

        self.assertTrue(result["scheduled"])
        self.assertEqual(1, result["attempt"])
        self.assertEqual("pending", plan["nodes"]["ch1"]["status"])
        self.assertEqual("", plan["nodes"]["ch1"]["artifact_sha256"])
        self.assertEqual(
            {"completed_scenes": [1]},
            plan["nodes"]["ch1"]["whatif_scene_checkpoint"],
        )
        self.assertEqual("pending", plan["nodes"]["ending_a"]["status"])

    def test_node_self_heal_clears_scene_checkpoint_from_second_retry(self):
        plan = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
        )
        plan["nodes"]["ch1"]["node_self_heal_attempts"] = 1
        plan["nodes"]["ch1"]["whatif_scene_checkpoint"] = {"completed_scenes": [1]}

        result = runtime.schedule_node_self_heal(
            plan,
            "ch1",
            reason="正文仍不完整",
            max_attempts=3,
        )

        self.assertTrue(result["scheduled"])
        self.assertTrue(result["cleared_scene_checkpoint"])
        self.assertEqual({}, plan["nodes"]["ch1"]["whatif_scene_checkpoint"])

    def test_node_self_heal_exhaustion_marks_node_failed(self):
        plan = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
        )
        plan["nodes"]["ch1"]["node_self_heal_attempts"] = 2

        result = runtime.schedule_node_self_heal(
            plan,
            "ch1",
            reason="正文仍不完整",
            max_attempts=2,
        )

        self.assertFalse(result["scheduled"])
        self.assertTrue(result["exhausted"])
        self.assertEqual("failed", plan["nodes"]["ch1"]["status"])

    def test_release_quality_repair_reopens_target_nodes_and_stops_after_limit(self):
        plan = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
        )
        for node_id in plan["nodes"]:
            plan["nodes"][node_id]["status"] = "completed"
        directive = {
            "mode": "targeted",
            "target_node_ids": ["ending_a"],
            "reason": "结局正文质量失败",
        }

        first = runtime.schedule_release_quality_repair(
            plan,
            directive,
            max_rounds=1,
        )
        second = runtime.schedule_release_quality_repair(
            plan,
            directive,
            max_rounds=1,
        )

        self.assertTrue(first["scheduled"])
        self.assertEqual("pending", plan["nodes"]["ending_a"]["status"])
        self.assertFalse(second["scheduled"])
        self.assertTrue(second["exhausted"])

    def test_targeted_release_repair_preserves_completed_upstream_steps(self):
        plan = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
        )
        for step_name in runtime.CHAPTER_BRANCH_STEP_ORDER:
            runtime.update_plan_step(
                plan,
                step_name,
                status="completed",
                output_digest=f"{step_name}-digest",
            )
        plan["step_outputs"] = {
            step_name: {"status": "completed"}
            for step_name in runtime.CHAPTER_BRANCH_STEP_ORDER
        }
        for node_id in plan["nodes"]:
            runtime.update_plan_node(plan, node_id, status="completed")
        plan["nodes"]["ending_a"]["whatif_scene_checkpoint"] = {
            "completed_scenes": [1]
        }

        result = runtime.schedule_release_quality_repair(
            plan,
            {
                "mode": "targeted",
                "target_node_ids": ["ending_a"],
                "reason": "结局正文语义校验未通过",
            },
            max_rounds=1,
        )

        self.assertTrue(result["scheduled"])
        self.assertEqual("completed", plan["steps"]["novel_analysis"]["status"])
        self.assertEqual("completed", plan["steps"]["topology"]["status"])
        self.assertEqual("pending", plan["steps"]["node_generation"]["status"])
        self.assertEqual("pending", plan["steps"]["release_gate"]["status"])
        self.assertNotIn("node_generation", plan["step_outputs"])
        self.assertIn("topology", plan["step_outputs"])
        self.assertEqual({}, plan["nodes"]["ending_a"]["whatif_scene_checkpoint"])
        self.assertEqual("completed", plan["nodes"]["ch1"]["status"])

    def test_targeted_release_repair_deactivates_stale_node_self_heal_request(self):
        plan = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
        )
        plan["node_self_heal_request"] = {
            "node_id": "ch1",
            "attempt": 1,
            "status": "pending",
        }

        result = runtime.schedule_release_quality_repair(
            plan,
            {
                "mode": "targeted",
                "target_node_ids": ["ending_a"],
                "reason": "结局正文待定向修复",
            },
            max_rounds=1,
        )

        self.assertTrue(result["scheduled"])
        self.assertEqual("inactive", plan["node_self_heal_request"]["status"])
        self.assertEqual([], runtime.get_active_node_self_heal_targets(plan))

    def test_manual_targeted_reopen_deactivates_stale_node_self_heal_request(self):
        plan = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
        )
        plan["node_self_heal_request"] = {
            "node_id": "ch1",
            "attempt": 1,
            "status": "pending",
        }

        reopened = runtime.reopen_plan_nodes(
            plan,
            ["ending_a"],
            include_non_original_descendants=False,
            reason="人工指定结局重生成",
        )

        self.assertEqual(["ending_a"], reopened)
        self.assertEqual("inactive", plan["node_self_heal_request"]["status"])
        self.assertEqual([], runtime.get_active_node_self_heal_targets(plan))

    def test_targeted_release_repair_does_not_reopen_unrequested_descendants(self):
        topology = {
            "topology": [
                {"node_id": "ch1", "type": "chapter", "is_original": True, "next": ["whatif"]},
                {"node_id": "whatif", "type": "chapter", "is_original": False, "next": ["whatif_ending"]},
                {"node_id": "whatif_ending", "type": "ending", "is_original": False, "next": []},
            ],
            "variable_defs": [],
        }
        plan = runtime.initialize_chapter_branch_plan(
            "task-1",
            topology,
            branch_type="default",
        )
        for node_id in plan["nodes"]:
            runtime.update_plan_node(plan, node_id, status="completed")

        result = runtime.schedule_release_quality_repair(
            plan,
            {
                "mode": "targeted",
                "target_node_ids": ["whatif"],
                "reason": "What-If 正文质量失败",
            },
            max_rounds=1,
        )

        self.assertTrue(result["scheduled"])
        self.assertEqual(["whatif"], result["reopened_node_ids"])
        self.assertEqual("pending", plan["nodes"]["whatif"]["status"])
        self.assertEqual("completed", plan["nodes"]["whatif_ending"]["status"])

    def test_targeted_release_repair_without_targets_does_not_schedule_a_noop(self):
        plan = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
        )
        for node_id in plan["nodes"]:
            runtime.update_plan_node(plan, node_id, status="completed")

        result = runtime.schedule_release_quality_repair(
            plan,
            {
                "mode": "targeted",
                "target_node_ids": [],
                "reason": "发布报告缺少可定位节点",
            },
            max_rounds=1,
        )

        self.assertTrue(result["scheduled"])
        self.assertEqual("full", result["mode"])
        self.assertEqual(set(plan["nodes"]), set(result["reopened_node_ids"]))

    def test_release_quality_repair_stops_repeating_the_same_blind_rewrite(self):
        plan = runtime.initialize_chapter_branch_plan(
            "task-1",
            self.topology,
            branch_type="default",
        )
        for node_id in plan["nodes"]:
            plan["nodes"][node_id]["status"] = "completed"
        directive = {
            "mode": "targeted",
            "target_node_ids": ["ending_a"],
            "reason": "结局 ending_a 的勇气路线缺少可验证遗产",
        }

        first = runtime.schedule_release_quality_repair(
            plan,
            directive,
            max_rounds=2,
        )
        plan["nodes"]["ending_a"]["status"] = "completed"
        second = runtime.schedule_release_quality_repair(
            plan,
            directive,
            max_rounds=2,
        )

        self.assertTrue(first["scheduled"])
        self.assertFalse(second["scheduled"])
        self.assertTrue(second["exhausted"])
        self.assertTrue(second["repeated"])

    def test_release_repair_reason_is_exposed_to_the_regenerated_node_prompt(self):
        entry = {
            "regeneration_count": 1,
            "error": "勇气路线缺少可验证遗产",
        }

        reason = runtime.get_node_regeneration_reason(entry)

        self.assertEqual("勇气路线缺少可验证遗产", reason)

    def test_latest_release_repair_reason_overrides_stale_node_self_heal_reason(self):
        entry = {
            "node_self_heal_reason": "旧的末态抽取失败",
            "release_repair_reason": "新的勇气路线缺少可验证遗产",
            "regeneration_count": 2,
        }

        reason = runtime.get_node_regeneration_reason(entry)

        self.assertEqual("新的勇气路线缺少可验证遗产", reason)

    def test_release_gate_error_exposes_candidate_and_quality_state(self):
        report = {
            "passed": False,
            "score": 76,
            "errors": ["勇气路线缺少可验证遗产"],
        }

        error = runtime.ReleaseGateError(report)

        self.assertEqual("release_gate_failed", error.status)
        self.assertTrue(error.candidate_generated)
        self.assertFalse(error.published)
        self.assertEqual(report, error.quality_report)
        self.assertIn("勇气路线缺少可验证遗产", str(error))


class GeneratedContentQualityTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(runtime, "chapter_branch_runtime 尚未实现")

    def test_rejects_empty_or_short_generated_content(self):
        ok, reason = runtime.validate_generated_content("很短", min_chars=100)

        self.assertFalse(ok)
        self.assertIn("长度不足", reason)

    def test_rejects_placeholder_or_internal_instruction_text(self):
        for text in (
            "故事在此延伸。" * 20,
            "（核心冲突：主角必须完成任务）" * 20,
            "REPLACE_ME" * 20,
        ):
            with self.subTest(text=text[:20]):
                ok, reason = runtime.validate_generated_content(text, min_chars=20)
                self.assertFalse(ok)
                self.assertIn("占位", reason)

    def test_accepts_substantive_content(self):
        content = "夜雨落在窗棂上，沈清舒握紧剑柄，终于说出了隐瞒多年的真相。" * 10

        ok, reason = runtime.validate_generated_content(content, min_chars=100)

        self.assertTrue(ok, reason)

    def test_rejects_title_with_dangling_connector_or_unclosed_bracket(self):
        for title in (
            "第6章（What-If：接受劝说，提前押注极限武馆自）",
            "第6章（What-If：接受劝说",
        ):
            with self.subTest(title=title):
                ok, reason = runtime.validate_title_completeness(title)
                self.assertFalse(ok)
                self.assertIn("标题", reason)

    def test_accepts_complete_title_ending_with_aspect_particle(self):
        ok, reason = runtime.validate_title_completeness("第12章 天终于亮了")

        self.assertTrue(ok, reason)

    def test_rejects_semantically_clipped_whatif_titles(self):
        for title in (
            "第7章（What-If：坦白这些照顾习惯来自另一段共同生）",
            "第9章（What-If：承认自己怕他再次走到无）",
        ):
            with self.subTest(title=title):
                ok, reason = runtime.validate_title_completeness(title)
                self.assertFalse(ok)
                self.assertIn("残句", reason)

    def test_rejects_generated_prose_with_unclosed_quote(self):
        content = (
            "夜雨落在窗棂上，沈清舒握紧剑柄，终于说出了真相。" * 10
            + "\n\n“小峰啊。”罗洪国说道，“还有几天就高考了，心态放平。"
        )

        ok, reason = runtime.validate_generated_content(content, min_chars=100)

        self.assertFalse(ok)
        self.assertIn("引号", reason)

    def test_repairs_single_unclosed_chinese_quote_in_final_paragraph(self):
        content = (
            "夜雨落在窗棂上，沈清舒握紧剑柄，终于说出了真相。\n\n"
            "罗洪国低声说：“还有几天就高考了，心态放平。"
        )

        repaired, repaired_ok, reason = runtime.repair_unbalanced_prose_delimiters(content)

        self.assertTrue(repaired_ok, reason)
        self.assertEqual(content + "”", repaired)

    def test_does_not_guess_when_unclosed_quote_is_before_final_paragraph(self):
        content = (
            "罗洪国低声说：“还有几天就高考了，心态放平。\n\n"
            "夜雨落在窗棂上，沈清舒握紧剑柄，终于说出了真相。"
        )

        repaired, repaired_ok, reason = runtime.repair_unbalanced_prose_delimiters(content)

        self.assertFalse(repaired_ok)
        self.assertEqual(content, repaired)
        self.assertIn("末段", reason)

    def test_does_not_remove_an_extra_chinese_closing_quote(self):
        content = "夜雨落在窗棂上，沈清舒终于说出了真相。”"

        repaired, repaired_ok, reason = runtime.repair_unbalanced_prose_delimiters(content)

        self.assertFalse(repaired_ok)
        self.assertEqual(content, repaired)
        self.assertIn("无法安全修复", reason)

    def test_release_preparation_revalidates_after_safe_quote_repair(self):
        content = (
            "夜雨落在窗棂上，沈清舒握紧剑柄，终于说出了真相。" * 10
            + "\n\n罗洪国低声说：“还有几天就高考了，心态放平。"
        )

        prepared, ok, reason, changed = runtime.prepare_generated_content_for_release(
            content,
            min_chars=100,
        )

        self.assertTrue(ok, reason)
        self.assertTrue(changed)
        self.assertEqual(content + "”", prepared)

    def test_release_preparation_preserves_unsafe_candidate_for_diagnostics(self):
        content = (
            "罗洪国低声说：“还有几天就高考了，心态放平。\n\n"
            + "夜雨落在窗棂上，沈清舒终于说出了真相。" * 10
        )

        prepared, ok, reason, changed = runtime.prepare_generated_content_for_release(
            content,
            min_chars=100,
        )

        self.assertFalse(ok)
        self.assertFalse(changed)
        self.assertEqual(content, prepared)
        self.assertIn("引号", reason)

    def test_rejects_generated_prose_ending_with_incomplete_sentence(self):
        content = "夜雨落在窗棂上，沈清舒反复思量。" * 10 + "她最终决定继续向"

        ok, reason = runtime.validate_generated_content(content, min_chars=100)

        self.assertFalse(ok)
        self.assertIn("末句", reason)


class TerminalStateValidationTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(runtime, "chapter_branch_runtime 尚未实现")

    def test_dead_character_cannot_be_resurrected_and_missing_character_is_inherited(self):
        inherited = {
            "女主": {"alive": False, "condition": "已死亡", "location": "书房"},
            "侍女": {"alive": True, "condition": "轻伤", "location": "庭院"},
        }
        candidate = {
            "女主": {"alive": "true", "condition": "安然无恙", "location": "城外"},
        }

        normalized, issues = runtime.normalize_terminal_states(candidate, inherited)

        self.assertFalse(normalized["女主"]["alive"])
        self.assertEqual(normalized["女主"]["condition"], "已死亡")
        self.assertIn("侍女", normalized)
        self.assertTrue(any("复活" in issue for issue in issues))

    def test_alive_value_is_always_boolean(self):
        normalized, _issues = runtime.normalize_terminal_states(
            {"主角": {"alive": "false", "condition": 123, "location": None}},
            {},
        )

        self.assertIs(normalized["主角"]["alive"], False)
        self.assertIsInstance(normalized["主角"]["condition"], str)
        self.assertIsInstance(normalized["主角"]["location"], str)

    def test_terminal_state_keeps_narrative_layer_fields(self):
        normalized, issues = runtime.normalize_terminal_states(
            {
                "江渡": {
                    "alive": False,
                    "presence_type": "hallucination",
                    "reality_layer": "therapy_reconstruction",
                    "timeline": "治疗中的重构记忆",
                    "location": "咨询室投射空间",
                    "identity_status": "被重构的逝者",
                }
            },
            {},
        )

        self.assertEqual([], issues)
        self.assertFalse(normalized["江渡"]["alive"])
        self.assertEqual("hallucination", normalized["江渡"]["presence_type"])
        self.assertEqual(
            "therapy_reconstruction", normalized["江渡"]["reality_layer"]
        )
        self.assertEqual("治疗中的重构记忆", normalized["江渡"]["timeline"])
        self.assertEqual("被重构的逝者", normalized["江渡"]["identity_status"])

    def test_dead_character_can_appear_as_hallucination_without_becoming_alive(self):
        inherited = {
            "江渡": {
                "alive": False,
                "presence_type": "absent",
                "reality_layer": "reality",
                "timeline": "十六岁病逝后",
                "location": "墓园",
                "identity_status": "已故",
            }
        }
        candidate = {
            "江渡": {
                "alive": True,
                "presence_type": "hallucination",
                "reality_layer": "therapy_reconstruction",
                "timeline": "治疗中的重构记忆",
                "location": "咨询室投射空间",
                "identity_status": "被重构的逝者",
            }
        }

        normalized, issues = runtime.normalize_terminal_states(candidate, inherited)

        self.assertFalse(normalized["江渡"]["alive"])
        self.assertEqual("hallucination", normalized["江渡"]["presence_type"])
        self.assertEqual(
            "therapy_reconstruction", normalized["江渡"]["reality_layer"]
        )
        self.assertTrue(any("复活" in issue for issue in issues))

    def test_dual_life_state_keeps_outer_death_and_current_layer_activity(self):
        normalized, issues = runtime.normalize_terminal_states(
            {
                "江渡": {
                    "outer_alive": False,
                    "current_layer_alive": True,
                    "alive": True,
                    "presence_type": "hallucination",
                    "reality_layer": "therapy_reconstruction",
                    "participation": "active",
                }
            },
            {
                "江渡": {
                    "outer_alive": False,
                    "current_layer_alive": False,
                    "alive": False,
                    "presence_type": "absent",
                    "reality_layer": "reality",
                }
            },
        )

        self.assertFalse(normalized["江渡"]["outer_alive"])
        self.assertTrue(normalized["江渡"]["current_layer_alive"])
        self.assertFalse(normalized["江渡"]["alive"])
        self.assertFalse(any("复活" in issue for issue in issues))

    def test_convergence_requires_same_life_layer_timeline_place_and_identity(self):
        whatif_states = {
            "江渡": {
                "alive": False,
                "presence_type": "hallucination",
                "reality_layer": "therapy_reconstruction",
                "timeline": "治疗中的重构记忆",
                "location": "咨询室",
                "identity_status": "被重构的逝者",
            }
        }
        sibling_states = {
            "江渡": {
                "alive": True,
                "presence_type": "physical",
                "reality_layer": "reality",
                "timeline": "高三下学期",
                "location": "学校",
                "identity_status": "在校学生",
            }
        }

        compatible, issues = runtime.validate_convergence_states(
            whatif_states, sibling_states
        )

        self.assertFalse(compatible)
        joined = "；".join(issues)
        for field in (
            "alive",
            "presence_type",
            "reality_layer",
            "timeline",
            "location",
            "identity_status",
        ):
            self.assertIn(field, joined)

    def test_missing_mentions_are_unknown_and_do_not_block_convergence(self):
        branch = {
            "罗峰": {
                "alive": True,
                "presence_type": "physical",
                "reality_layer": "reality",
                "timeline": "六月八日考场内",
                "location": "第一高中考场",
                "identity_status": "高中生",
            }
        }
        sibling = {
            "罗峰": {
                "alive": True,
                "presence_type": "unknown",
                "reality_layer": "unknown",
                "timeline": "本章未提及",
                "location": "本章未提及",
                "identity_status": "本章未提及",
                "note": "本章未提及，沿用上一节点状态",
            }
        }

        compatible, issues = runtime.validate_convergence_states(branch, sibling)

        self.assertTrue(compatible, issues)
        self.assertEqual([], issues)

    def test_missing_character_state_does_not_block_convergence(self):
        compatible, issues = runtime.validate_convergence_states(
            {"江渡": {"alive": True, "reality_layer": "reality"}},
            {},
        )

        self.assertTrue(compatible, issues)
        self.assertIn("state 缺失", "；".join(issues))

    def test_missing_character_state_is_inheritable_warning_not_blocking(self):
        """A character absent from one terminal scan is not itself a contradiction."""
        compatible, issues = runtime.validate_convergence_states(
            {
                "江渡": {
                    "alive": True,
                    "life_status": "alive_confirmed",
                    "presence_type": "physical",
                    "reality_layer": "reality",
                }
            },
            {},
        )

        self.assertTrue(compatible, issues)
        self.assertEqual(1, len(issues))
        self.assertIn("state 缺失", issues[0])

    def test_missing_character_states_on_both_sides_are_non_blocking(self):
        """Characters not present in either extracted map should not fail convergence."""
        compatible, issues = runtime.validate_convergence_states(
            {"江渡": {}},
            {"江渡": {}},
        )

        self.assertTrue(compatible, issues)
        self.assertEqual([], issues)

    def test_field_level_unknown_values_are_inheritable_warnings_not_conflicts(self):
        branch = {
            "姜岁岁": {
                "alive": True,
                "life_status": "alive_confirmed",
                "presence_type": "physical",
                "participation": "active",
                "timeline": "现实时间线；周一清早",
                "location": "寝室",
                "identity_status": "班上女生",
            }
        }
        sibling = {
            "姜岁岁": {
                "alive": True,
                "life_status": "alive_confirmed",
                "presence_type": "unknown",
                "participation": "offscreen",
                "timeline": "",
                "location": "本章未提及",
                "identity_status": "未出场",
            }
        }

        compatible, issues = runtime.validate_convergence_states(branch, sibling)

        self.assertTrue(compatible, issues)
        self.assertEqual([], issues)

    def test_absent_presence_cannot_remain_active(self):
        normalized, issues = runtime.normalize_terminal_states(
            {
                "周司扬": {
                    "alive": True,
                    "life_status": "alive_confirmed",
                    "presence_type": "absent",
                    "participation": "active",
                    "location": "本章未提及",
                }
            },
            {},
        )

        self.assertEqual([], issues)
        self.assertIn(
            normalized["周司扬"]["participation"],
            {"offscreen", "absent", "mentioned"},
        )

    def test_unknown_terminal_fields_inherit_known_previous_values(self):
        normalized, issues = runtime.normalize_terminal_states(
            {
                "江渡": {
                    "alive": True,
                    "presence_type": "unknown",
                    "reality_layer": "未提及",
                    "timeline": "",
                    "location": "本章未提及",
                    "identity_status": "未说明",
                }
            },
            {
                "江渡": {
                    "alive": True,
                    "presence_type": "physical",
                    "reality_layer": "reality",
                    "timeline": "周一下午",
                    "location": "学校操场",
                    "identity_status": "在校学生",
                }
            },
        )

        self.assertEqual([], issues)
        state = normalized["江渡"]
        self.assertEqual("physical", state["presence_type"])
        self.assertEqual("reality", state["reality_layer"])
        self.assertEqual("周一下午", state["timeline"])
        self.assertEqual("学校操场", state["location"])
        self.assertEqual("在校学生", state["identity_status"])
        self.assertEqual("active", state["participation"])

    def test_convergence_returns_structured_hard_warnings_and_bridge_issues(self):
        result = runtime.evaluate_convergence_states(
            {
                "江渡": {
                    "alive": True,
                    "life_status": "alive_confirmed",
                    "presence_type": "physical",
                    "reality_layer": "reality",
                    "timeline": "周一",
                    "location": "学校",
                    "identity_status": "在校学生",
                }
            },
            {
                "江渡": {
                    "alive": True,
                    "life_status": "alive_confirmed",
                    "presence_type": "physical",
                    "reality_layer": "reality",
                    "timeline": "周二",
                    "location": "寝室",
                    "identity_status": "在校学生",
                }
            },
        )

        self.assertTrue(result["compatible"])
        self.assertEqual([], result["hard_issues"])
        self.assertTrue(result["bridge_requirements"])
        self.assertEqual(
            result["issues"],
            result["warnings"] + result["bridge_requirements"],
        )

    def test_convergence_allows_transitionable_time_place_and_presence_changes(self):
        branch = {
            "江渡": {
                "alive": True,
                "presence_type": "memory",
                "reality_layer": "memory",
                "timeline": "2019年9月1日晚",
                "location": "小区外便利店",
                "identity_status": "魏清越恋人",
            }
        }
        sibling = {
            "江渡": {
                "alive": True,
                "presence_type": "absent",
                "reality_layer": "memory",
                "timeline": "2019年9月1日深夜",
                "location": "江渡家中",
                "identity_status": "魏清越初恋女友",
            }
        }

        compatible, issues = runtime.validate_convergence_states(branch, sibling)

        self.assertTrue(compatible, issues)
        self.assertTrue(any("timeline" in issue for issue in issues))
        self.assertTrue(any("location" in issue for issue in issues))

    def test_convergence_rejects_reality_and_core_identity_conflicts(self):
        branch = {
            "江渡": {
                "alive": True,
                "presence_type": "physical",
                "reality_layer": "reality",
                "timeline": "高三",
                "location": "学校",
                "identity_status": "在校学生",
            }
        }
        sibling = {
            "江渡": {
                "alive": True,
                "presence_type": "hallucination",
                "reality_layer": "therapy_reconstruction",
                "timeline": "治疗中",
                "location": "咨询室",
                "identity_status": "被重构的逝者",
            }
        }

        compatible, issues = runtime.validate_convergence_states(branch, sibling)

        self.assertFalse(compatible)
        joined = "；".join(issues)
        self.assertIn("reality_layer", joined)
        self.assertIn("identity_status", joined)

    def test_normalization_adds_layered_reality_fields_from_global_contract(self):
        normalized, issues = runtime.normalize_terminal_states(
            {
                "李迟舒": {
                    "alive": False,
                    "presence_type": "dream",
                    "reality_layer": "therapy_reconstruction",
                    "timeline": "高三体育课后",
                    "location": "学校",
                    "identity_status": "高中生形象",
                }
            },
            {},
            narrative_contract={
                "primary_reality_domain": "reconstructed_world",
                "primary_reconstruction_type": "dream",
            },
        )

        self.assertEqual([], issues)
        state = normalized["李迟舒"]
        self.assertEqual("reconstructed_world", state["reality_domain"])
        self.assertEqual("dream", state["reconstruction_type"])
        self.assertEqual("present", state["local_frame"])
        self.assertEqual("active", state["participation"])

    def test_invalid_state_confidence_falls_back_without_breaking_scan(self):
        normalized, issues = runtime.normalize_terminal_states(
            {
                "李迟舒": {
                    "alive": False,
                    "presence_type": "memory",
                    "reality_layer": "memory",
                    "state_confidence": "high",
                }
            },
            {},
        )

        self.assertEqual([], issues)
        self.assertEqual(1.0, normalized["李迟舒"]["state_confidence"])

    def test_dream_and_therapy_reconstruction_are_compatible_in_same_domain(self):
        branch, _ = runtime.normalize_terminal_states(
            {
                "李迟舒": {
                    "alive": False,
                    "presence_type": "dream",
                    "reality_layer": "therapy_reconstruction",
                    "timeline": "高三体育课后下午重构",
                    "location": "食堂三楼",
                    "identity_status": "高中生形象",
                }
            },
            {},
            narrative_contract={
                "primary_reality_domain": "reconstructed_world",
                "primary_reconstruction_type": "dream",
            },
        )
        sibling, _ = runtime.normalize_terminal_states(
            {
                "李迟舒": {
                    "alive": False,
                    "presence_type": "dream",
                    "reality_layer": "dream",
                    "timeline": "高三体育课后至回班前",
                    "location": "食堂三楼",
                    "identity_status": "高中生形象",
                }
            },
            {},
            narrative_contract={
                "primary_reality_domain": "reconstructed_world",
                "primary_reconstruction_type": "dream",
            },
        )

        compatible, issues = runtime.validate_convergence_states(branch, sibling)

        self.assertTrue(compatible, issues)
        self.assertTrue(any("reality_layer" in issue for issue in issues))

    def test_nested_memory_mention_does_not_block_dream_convergence(self):
        branch = {
            "洛可": {
                "alive": True,
                "presence_type": "memory",
                "reality_layer": "memory",
                "reality_domain": "reconstructed_world",
                "reconstruction_type": "dream",
                "local_frame": "memory",
                "participation": "mentioned",
                "timeline": "前世电话及十月二日夜",
                "location": "日记中",
                "identity_status": "李迟舒同学",
            }
        }
        sibling = {
            "洛可": {
                "alive": True,
                "presence_type": "memory",
                "reality_layer": "dream",
                "reality_domain": "reconstructed_world",
                "reconstruction_type": "dream",
                "local_frame": "memory",
                "participation": "mentioned",
                "timeline": "十月二日日记",
                "location": "班级回忆",
                "identity_status": "未回家的女生",
            }
        }

        compatible, issues = runtime.validate_convergence_states(branch, sibling)

        self.assertTrue(compatible, issues)
        self.assertTrue(any("timeline" in issue for issue in issues))

    def test_low_confidence_mentioned_character_life_difference_is_not_hard(self):
        branch = {
            "洛可": {
                "alive": False,
                "presence_type": "memory",
                "reality_layer": "memory",
                "reality_domain": "reconstructed_world",
                "participation": "mentioned",
                "state_confidence": 0.35,
                "identity_status": "日记中被提及的同学",
            }
        }
        sibling = {
            "洛可": {
                "alive": True,
                "presence_type": "memory",
                "reality_layer": "dream",
                "reality_domain": "reconstructed_world",
                "participation": "mentioned",
                "state_confidence": 0.4,
                "identity_status": "未回家的女生",
            }
        }

        compatible, issues = runtime.validate_convergence_states(branch, sibling)

        self.assertTrue(compatible, issues)
        self.assertTrue(any("alive" in issue for issue in issues))

    def test_structural_transition_gap_requires_bridge_scene_repair(self):
        strategy = runtime.classify_convergence_repair(
            [],
            transition_issues=[
                "结尾仍在学校楼梯转角，下游已经位于乡下租房",
                "缺少接受邀请、出发、到达和住宿等关键动作链",
                "时间从九月三十日下午跳到第二天清晨，需要读者脑补转场",
            ],
        )

        self.assertEqual("bridge_scene", strategy)

    def test_irreversible_life_conflict_requires_terminal_branch(self):
        strategy = runtime.classify_convergence_repair(
            ["李迟舒.alive 不一致：What-If=False，主线=True"],
            transition_issues=[],
        )

        self.assertEqual("terminal", strategy)

    def test_terminal_state_conflict_gets_two_full_scene_repairs_before_terminal(self):
        self.assertEqual(
            "regenerate_last_scene",
            runtime.escalate_state_repair_strategy("terminal", attempt=0),
        )
        self.assertEqual(
            "regenerate_last_scene",
            runtime.escalate_state_repair_strategy("terminal", attempt=1),
        )
        self.assertEqual(
            "terminal",
            runtime.escalate_state_repair_strategy("terminal", attempt=2),
        )

    def test_physical_and_reconstructed_world_conflict_requires_terminal_branch(self):
        strategy = runtime.classify_convergence_repair(
            [
                "沈抱山.reality_layer 不一致：What-If='reality'，"
                "主线='therapy_reconstruction'"
            ],
            transition_issues=[],
        )

        self.assertEqual("terminal", strategy)

    def test_incompatible_whatif_is_marked_terminal_instead_of_forced_to_mainline(self):
        node = {"node_id": "ch36_alt", "type": "chapter", "next": ["ch37"]}

        runtime.mark_incompatible_whatif_terminal(node, ["江渡.alive 不一致"])

        self.assertEqual([], node["next"])
        self.assertTrue(node["terminal_branch"])
        self.assertEqual("terminal", node["convergence_status"])
        self.assertEqual("terminal", node["merge_policy"])
        self.assertIn("江渡.alive", node["convergence_issues"][0])


class RouteMetadataTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(runtime, "chapter_branch_runtime 尚未实现")

    def test_multiple_endings_on_same_variable_are_preserved_by_child_id(self):
        topology = [
            {"node_id": "ending_low", "type": "ending", "branch_variable": "courage", "branch_threshold": 60},
            {"node_id": "ending_high", "type": "ending", "branch_variable": "courage", "branch_threshold": 80},
        ]
        node_id_map = {"ending_low": "node_low", "ending_high": "node_high"}
        variable_defs = [{"key": "courage", "init": 50}]

        player_variables, routes = runtime.build_route_metadata(
            topology,
            node_id_map,
            variable_defs,
        )

        self.assertEqual(player_variables, {"courage": 50})
        self.assertEqual(routes["node_low"], {"variable": "courage", "threshold": 60})
        self.assertEqual(routes["node_high"], {"variable": "courage", "threshold": 80})

    def test_route_metadata_exports_normalized_tendency_and_margin_rules(self):
        topology = [
            {
                "node_id": "ending_courage",
                "type": "ending",
                "branch_variable": "courage",
                "branch_threshold": 85,
                "branch_ratio_threshold": 0.65,
                "branch_margin_ratio": 0.10,
                "effective_choice_count": 20,
                "minimum_effective_choices": 3,
                "route_enabled": True,
            }
        ]

        _variables, routes = runtime.build_route_metadata(
            topology,
            {"ending_courage": "node_courage"},
            [{"key": "courage", "init": 50}],
        )

        route = routes["node_courage"]
        self.assertEqual(0.65, route["ratio_threshold"])
        self.assertEqual(0.10, route["margin_ratio"])
        self.assertEqual(20, route["effective_choice_count"])
        self.assertEqual(3, route["minimum_effective_choices"])
        self.assertTrue(route["route_enabled"])

    def test_route_metadata_exports_required_legacy_flags_for_ending(self):
        topology = [
            {
                "node_id": "ending_self_worth",
                "type": "ending",
                "branch_variable": "self_worth",
                "branch_threshold": 70,
                "legacy_requirements": [
                    "signed_clarification",
                    "school_record_preserved",
                    "signed_clarification",
                    "",
                ],
            }
        ]

        _variables, routes = runtime.build_route_metadata(
            topology,
            {"ending_self_worth": "node_self_worth"},
            [{"key": "self_worth", "init": 50}],
        )

        self.assertEqual(
            ["signed_clarification", "school_record_preserved"],
            routes["node_self_worth"]["required_legacy_flags"],
        )


class SoftBranchBudgetTests(unittest.TestCase):
    def test_original_ending_does_not_subtract_from_total_branch_budget(self):
        chapters = [
            "\n\n".join(["第一章" * 500] * 2),
            "\n\n".join(["第二章" * 600] * 5),
            "第三章" * 1000,
        ]
        topology = [
            {"type": "chapter", "is_original": True, "source_chapter_idx": 0},
            {"type": "branch_point", "is_original": True, "source_chapter_idx": 1},
            {"type": "ending", "is_original": True, "source_chapter_idx": 2},
        ]

        total = runtime.estimate_total_soft_branches(topology, chapters)
        counts = runtime.estimate_soft_branch_counts(topology, chapters)

        self.assertEqual(5, total)
        self.assertEqual([1, 4, 0], counts)


class RunMetricsTests(unittest.TestCase):
    def test_build_run_metrics_separates_attempt_and_cumulative_values(self):
        previous = {
            "nodes_generated": 8,
            "nodes_resumed": 2,
            "optional_branches_omitted": 1,
            "cache_hits": 3,
            "cache_misses": 1,
            "cache_hit_rate": 0.75,
            "phase_seconds": {"analysis": 12.5},
            "soft_branches": {"total": 10, "generated_branch_count": 9},
        }
        attempt = {
            "nodes_generated": 1,
            "nodes_resumed": 7,
            "optional_branches_omitted": 0,
            "cache_hits": 1,
            "cache_misses": 1,
            "cache_hit_rate": 0.5,
            "phase_seconds": {"analysis": 3.5, "topology": 4.0},
            "soft_branches": {"total": 2, "generated_branch_count": 2},
        }

        result = runtime.build_run_metrics(previous, attempt)

        self.assertEqual(attempt, result["attempt_metrics"])
        self.assertEqual(9, result["cumulative_metrics"]["nodes_generated"])
        self.assertEqual(9, result["cumulative_metrics"]["nodes_resumed"])
        self.assertEqual(16.0, result["cumulative_metrics"]["phase_seconds"]["analysis"])
        self.assertEqual(4.0, result["cumulative_metrics"]["phase_seconds"]["topology"])
        self.assertEqual(12, result["cumulative_metrics"]["soft_branches"]["total"])
        self.assertEqual(4, result["cumulative_metrics"]["cache_hits"])
        self.assertEqual(2, result["cumulative_metrics"]["cache_misses"])
        self.assertAlmostEqual(4 / 6, result["cumulative_metrics"]["cache_hit_rate"])


class ReleaseIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(runtime, "chapter_branch_runtime 尚未实现")
        self.payload = {
            "root_id": "chapter_1",
            "nodes": {
                "chapter_1": {"children": [{"child_id": "ending_a"}]},
                "ending_a": {"children": []},
            },
        }

    def test_valid_release_requires_md_for_every_node(self):
        ok, errors = runtime.validate_release_integrity(
            self.payload,
            saved_md_ids={"chapter_1", "ending_a"},
        )

        self.assertTrue(ok, errors)

    def test_rejects_missing_md_and_dangling_child(self):
        self.payload["nodes"]["chapter_1"]["children"].append(
            {"child_id": "missing"}
        )

        ok, errors = runtime.validate_release_integrity(
            self.payload,
            saved_md_ids={"chapter_1"},
        )

        self.assertFalse(ok)
        self.assertTrue(any("MD" in error for error in errors))
        self.assertTrue(any("child_id" in error for error in errors))

    def test_rejects_missing_root(self):
        self.payload["root_id"] = "unknown"

        ok, errors = runtime.validate_release_integrity(
            self.payload,
            saved_md_ids={"chapter_1", "ending_a"},
        )

        self.assertFalse(ok)
        self.assertTrue(any("root_id" in error for error in errors))

    def test_release_integrity_validates_optional_content_warning_schema(self):
        self.payload["content_warnings"] = {
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
        ok, errors = runtime.validate_release_integrity(
            self.payload,
            saved_md_ids={"chapter_1", "ending_a"},
        )

        self.assertTrue(ok, errors)

        self.payload["content_warnings"]["work"]["localized"] = {}
        ok, errors = runtime.validate_release_integrity(
            self.payload,
            saved_md_ids={"chapter_1", "ending_a"},
        )
        self.assertFalse(ok)
        self.assertTrue(any("content_warnings" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
