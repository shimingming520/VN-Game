import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class StoryTreePersistenceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[2]
        cls.persistence_source = (
            cls.repo_root
            / "services"
            / "story_planner"
            / "table"
            / "story_tree_to_db.py"
        ).read_text(encoding="utf-8")
        cls.tasks_source = (
            cls.repo_root
            / "services"
            / "story_planner"
            / "tasks.py"
        ).read_text(encoding="utf-8")
        cls.ending_writer_source = (
            cls.repo_root
            / "services"
            / "story_planner"
            / "core"
            / "ending_writer.py"
        ).read_text(encoding="utf-8")

    def test_normal_writes_do_not_reset_database_sequences(self):
        self.assertNotIn("setval(", self.persistence_source)

    def test_chapter_branch_checks_each_md_save_result(self):
        chapter_branch_source = self.tasks_source.split(
            "def _parse_chapter_branch_mode", 1
        )[1].split("@celery_app.task", 1)[0]

        self.assertIn("md_record = _save_owned_chapter_branch_md", chapter_branch_source)
        self.assertIn("if md_record is None", chapter_branch_source)

    def test_optional_whatif_exhaustion_is_omitted_instead_of_failing_book(self):
        chapter_branch_source = self.tasks_source.split(
            "def _parse_chapter_branch_mode", 1
        )[1].split("@celery_app.task", 1)[0]

        self.assertIn("omit_optional_topology_branch", chapter_branch_source)
        self.assertIn("def _omit_current_optional_branch", chapter_branch_source)
        self.assertIn('status="omitted"', chapter_branch_source)
        self.assertIn("WhatIfContentBudgetError", chapter_branch_source)
        self.assertIn("tree.nodes.pop(omitted_real_id, None)", chapter_branch_source)
        budget_except_pos = chapter_branch_source.find(
            "except WhatIfContentBudgetError as exc:"
        )
        retry_pos = chapter_branch_source.find(
            "_schedule_optional_branch_self_heal(",
            budget_except_pos,
        )
        omit_pos = chapter_branch_source.find(
            "_omit_current_optional_branch(",
            retry_pos,
        )
        self.assertGreaterEqual(budget_except_pos, 0)
        self.assertGreater(retry_pos, budget_except_pos)
        self.assertGreater(omit_pos, retry_pos)

    def test_generated_content_gate_saves_failed_candidate_as_warning_without_raising(self):
        chapter_branch_source = self.tasks_source.split(
            "def _parse_chapter_branch_mode", 1
        )[1].split("@celery_app.task", 1)[0]

        prepare_pos = chapter_branch_source.find("prepare_generated_content_for_release(")
        candidate_pos = chapter_branch_source.find(
            'name=f"{real_id}.candidate"',
            prepare_pos,
        )
        warning_pos = chapter_branch_source.find("正文质量告警", prepare_pos)

        self.assertGreaterEqual(prepare_pos, 0)
        self.assertGreater(candidate_pos, prepare_pos)
        self.assertGreater(warning_pos, candidate_pos)

    def test_chapter_branch_wrapper_restarts_for_node_and_release_repairs(self):
        self.assertIn("except NodeSelfHealRequired", self.tasks_source)
        self.assertIn("except ReleaseQualityRepairRequired", self.tasks_source)
        self.assertIn("return _parse_chapter_branch_mode_once(", self.tasks_source)

    def test_node_quality_failure_does_not_schedule_outer_self_heal(self):
        chapter_branch_source = self.tasks_source.split(
            "def _parse_chapter_branch_mode", 1
        )[1].split("@celery_app.task", 1)[0]
        gate_pos = chapter_branch_source.find("prepare_generated_content_for_release(")
        warning_pos = chapter_branch_source.find("正文质量告警", gate_pos)
        fatal_pos = chapter_branch_source.find("正文质量门禁失败", gate_pos)

        self.assertGreater(warning_pos, gate_pos)
        self.assertEqual(-1, fatal_pos)

    def test_optional_generated_content_gate_does_not_schedule_outer_node_self_heal(self):
        chapter_branch_source = self.tasks_source.split(
            "def _parse_chapter_branch_mode", 1
        )[1].split("@celery_app.task", 1)[0]
        helper_pos = chapter_branch_source.find(
            "def _schedule_optional_branch_self_heal("
        )
        self.assertGreaterEqual(helper_pos, 0)
        helper_end = chapter_branch_source.find(
            "        def _schedule_consistency_self_heal(", helper_pos
        )
        helper = chapter_branch_source[helper_pos:helper_end]
        self.assertIn("record_optional_branch_failure(", helper)
        self.assertNotIn("raise NodeSelfHealRequired", helper)

    def test_whatif_transition_acceptance_helper_is_imported_by_runtime(self):
        """The full What-If path must resolve its transition gate at runtime."""
        chapter_branch_source = self.tasks_source.split(
            "def _parse_chapter_branch_mode", 1
        )[1].split("@celery_app.task", 1)[0]
        import_block = chapter_branch_source[chapter_branch_source.find(
            "from services.story_planner.core.branch_designer import"
        ):]
        self.assertIn("is_acceptable_whatif_transition_review", import_block)

    def test_optional_generated_content_quality_failure_is_warning_before_omission(self):
        chapter_branch_source = self.tasks_source.split(
            "def _parse_chapter_branch_mode", 1
        )[1].split("@celery_app.task", 1)[0]
        gate_pos = chapter_branch_source.find("prepare_generated_content_for_release(")
        optional_condition_pos = chapter_branch_source.find(
            'if not is_original and node_type == "chapter":', gate_pos
        )
        optional_omit_pos = chapter_branch_source.find(
            "_omit_current_optional_branch(_optional_reason)", optional_condition_pos
        )
        self.assertGreaterEqual(gate_pos, 0)
        self.assertGreaterEqual(optional_condition_pos, 0)
        self.assertIn("_record_optional_quality_warning(", chapter_branch_source[optional_condition_pos:optional_omit_pos])
        self.assertGreater(optional_omit_pos, optional_condition_pos)

    def test_normal_whatif_initializes_omit_reason_before_generation(self):
        """A successful What-If path uses the bounded local generator wrapper."""
        chapter_branch_source = self.tasks_source.split(
            "def _parse_chapter_branch_mode", 1
        )[1].split("@celery_app.task", 1)[0]
        normal_branch_pos = chapter_branch_source.find(
            "# What-If 章节：LLM 全新生成"
        )
        self.assertGreaterEqual(normal_branch_pos, 0)
        normal_branch = chapter_branch_source[normal_branch_pos:]
        generation_pos = normal_branch.find("def _generate_whatif_candidate(")
        wrapper_pos = normal_branch.find("_run_optional_generator(", generation_pos)
        self.assertGreaterEqual(generation_pos, 0)
        self.assertGreater(wrapper_pos, generation_pos)

    def test_ending_consistency_failure_uses_local_optional_retry(self):
        chapter_branch_source = self.tasks_source.split(
            "def _parse_chapter_branch_mode", 1
        )[1].split("@celery_app.task", 1)[0]
        helper_pos = chapter_branch_source.find(
            "def _schedule_consistency_self_heal("
        )
        ending_pos = chapter_branch_source.find("generate_ending_content(")
        self.assertGreaterEqual(helper_pos, 0)
        self.assertGreaterEqual(ending_pos, 0)
        self.assertIn("allow_optional_omission=True", chapter_branch_source)
        wrapper_pos = chapter_branch_source.find("_run_optional_generator(", ending_pos)
        self.assertGreater(wrapper_pos, ending_pos)

    def test_ending_quality_repairs_are_local_warnings_not_outer_restarts(self):
        chapter_branch_source = self.tasks_source.split(
            "def _parse_chapter_branch_mode", 1
        )[1].split("@celery_app.task", 1)[0]
        ending_pos = chapter_branch_source.find("# 平行结局：LLM 生成")
        ending_block = chapter_branch_source[ending_pos:]
        self.assertGreaterEqual(ending_pos, 0)
        self.assertIn("_ending_omit_reason", ending_block)
        self.assertIn("结局正文叙事视角不一致", ending_block)
        self.assertIn("结局节点", ending_block)
        self.assertNotIn(
            "raise NodeSelfHealRequired(",
            ending_block[: ending_block.find("# What-If 章节：LLM 全新生成")],
        )

    def test_whatif_and_bad_end_consistency_failures_use_local_retry_wrapper(self):
        chapter_branch_source = self.tasks_source.split(
            "def _parse_chapter_branch_mode", 1
        )[1].split("@celery_app.task", 1)[0]
        generation_positions = []
        offset = 0
        while True:
            position = chapter_branch_source.find("generate_whatif_chapter(", offset)
            if position < 0:
                break
            generation_positions.append(position)
            offset = position + 1

        self.assertEqual(2, len(generation_positions))
        wrapper_pos = chapter_branch_source.find("def _run_optional_generator(")
        consistency_pos = chapter_branch_source.find(
            "allow_optional_omission=True", wrapper_pos
        )
        self.assertGreaterEqual(wrapper_pos, 0)
        self.assertGreater(consistency_pos, wrapper_pos)

    def test_ending_consistency_anchors_are_filtered_by_pivot(self):
        self.assertIn(
            "filter_behavior_anchors_for_ending(",
            self.ending_writer_source,
        )
        self.assertIn("pivot_chapter_index", self.ending_writer_source)

        chapter_branch_source = self.tasks_source.split(
            "def _parse_chapter_branch_mode", 1
        )[1].split("@celery_app.task", 1)[0]
        ending_pos = chapter_branch_source.find("generate_ending_content(")
        pivot_arg_pos = chapter_branch_source.find(
            "pivot_chapter_index=_ending_pivot_index",
            ending_pos,
        )
        self.assertGreater(pivot_arg_pos, ending_pos)

    def test_release_quality_failure_does_not_schedule_full_repair(self):
        chapter_branch_source = self.tasks_source.split(
            "def _parse_chapter_branch_mode", 1
        )[1].split("@celery_app.task", 1)[0]
        gate_pos = chapter_branch_source.find('if not quality_report["passed"]')
        gate_block = chapter_branch_source[gate_pos:]
        self.assertNotIn("build_release_repair_directive(", gate_block)
        self.assertNotIn("schedule_release_quality_repair(", gate_block)
        self.assertIn("raise ReleaseGateError(quality_report)", gate_block)

    def test_ending_causality_log_uses_direct_semantic_review_wording(self):
        self.assertIn("执行大模型语义因果校验", self.tasks_source)
        self.assertNotIn("结局变量关键词匹配已停用", self.tasks_source)

    def test_chapter_branch_validates_release_before_transactional_publish(self):
        chapter_branch_source = self.tasks_source.split(
            "def _parse_chapter_branch_mode", 1
        )[1].split("@celery_app.task", 1)[0]

        validation_pos = chapter_branch_source.find("validate_release_integrity")
        publish_pos = chapter_branch_source.find(
            "publish_result = _publish_chapter_branch_story_tree"
        )
        self.assertGreaterEqual(validation_pos, 0)
        self.assertGreater(publish_pos, validation_pos)
        self.assertIn("md_contents=node_md_contents", chapter_branch_source)
        self.assertIn("expected_node_ids=saved_md_ids", chapter_branch_source)

    def test_chapter_branch_runs_quality_gate_before_transactional_publish(self):
        chapter_branch_source = self.tasks_source.split(
            "def _parse_chapter_branch_mode", 1
        )[1].split("@celery_app.task", 1)[0]

        gate_pos = chapter_branch_source.find(
            "quality_report = validate_chapter_branch_release"
        )
        publish_pos = chapter_branch_source.find(
            "publish_result = _publish_chapter_branch_story_tree"
        )
        self.assertGreaterEqual(gate_pos, 0)
        self.assertGreater(publish_pos, gate_pos)
        self.assertIn('json_data["quality_gate"] = quality_report', chapter_branch_source)
        self.assertIn('if not quality_report["passed"]', chapter_branch_source)

    def test_post_gate_language_warnings_recompute_score_before_publish(self):
        chapter_branch_source = self.tasks_source.split(
            "def _parse_chapter_branch_mode", 1
        )[1].split("@celery_app.task", 1)[0]
        warning_pos = chapter_branch_source.find("append_quality_warnings(")
        publish_pos = chapter_branch_source.find(
            "publish_result = _publish_chapter_branch_story_tree"
        )
        self.assertGreaterEqual(warning_pos, 0)
        self.assertGreater(publish_pos, warning_pos)
        post_gate = chapter_branch_source[warning_pos:publish_pos]
        self.assertIn('if not quality_report["passed"]', post_gate)

    def test_threshold_reachability_is_advisory_and_does_not_abort_pipeline(self):
        chapter_branch_source = self.tasks_source.split(
            "def _parse_chapter_branch_mode", 1
        )[1].split("@celery_app.task", 1)[0]

        self.assertNotIn(
            'raise ValueError("玩家变量阈值可达性校验失败：',
            chapter_branch_source,
        )
        self.assertIn(
            "_pre_release_quality_warnings.append(",
            chapter_branch_source,
        )

    def test_release_integrity_failure_still_saves_diagnostic_artifacts(self):
        chapter_branch_source = self.tasks_source.split(
            "def _parse_chapter_branch_mode", 1
        )[1].split("@celery_app.task", 1)[0]

        integrity_pos = chapter_branch_source.find("validate_release_integrity")
        diagnostic_pos = chapter_branch_source.find(
            "_save_chapter_branch_diagnostics",
            integrity_pos,
        )
        self.assertGreaterEqual(integrity_pos, 0)
        self.assertGreater(diagnostic_pos, integrity_pos)
        self.assertNotIn('raise ValueError("发布完整性校验失败', chapter_branch_source)

    def test_chapter_branch_exports_each_node_md_filename(self):
        chapter_branch_source = self.tasks_source.split(
            "def _parse_chapter_branch_mode", 1
        )[1].split("@celery_app.task", 1)[0]

        self.assertIn('content_file=f"{real_id}.md"', chapter_branch_source)

    def test_chapter_branch_emits_structured_runtime_events_and_node_heartbeats(self):
        chapter_branch_source = self.tasks_source.split(
            "def _parse_chapter_branch_mode", 1
        )[1].split("@celery_app.task", 1)[0]

        self.assertIn("structured_log(", chapter_branch_source)
        self.assertIn("start_runtime_heartbeat(", chapter_branch_source)
        self.assertIn("stop_runtime_heartbeat(", chapter_branch_source)
        self.assertIn("detect_stalled_node(", chapter_branch_source)
        self.assertIn("RUNTIME_SETTINGS.heartbeat_interval_seconds", chapter_branch_source)
        self.assertIn("RUNTIME_SETTINGS.node_stall_timeout_seconds", chapter_branch_source)

    def test_chapter_branch_reads_novel_through_encoding_normalizer(self):
        chapter_branch_source = self.tasks_source.split(
            "def _parse_chapter_branch_mode", 1
        )[1].split("@celery_app.task", 1)[0]

        self.assertIn("read_novel_text(", chapter_branch_source)
        self.assertIn("convert_to_utf8=True", chapter_branch_source)
        self.assertNotIn("errors='replace'", chapter_branch_source)

    def test_chapter_branch_uses_deterministic_ids_and_fails_invalid_sort(self):
        chapter_branch_source = self.tasks_source.split(
            "def _parse_chapter_branch_mode", 1
        )[1].split("@celery_app.task", 1)[0]

        self.assertIn("deterministic_node_id", chapter_branch_source)
        self.assertNotIn("_uuid.uuid4", chapter_branch_source)
        self.assertIn("raise ValueError(\"拓扑排序失败", chapter_branch_source)

    def test_route_metadata_is_built_by_child_id(self):
        chapter_branch_source = self.tasks_source.split(
            "def _parse_chapter_branch_mode", 1
        )[1].split("@celery_app.task", 1)[0]

        self.assertIn("build_route_metadata", chapter_branch_source)

    def test_ending_choice_text_stays_empty_while_other_empty_choices_continue(self):
        chapter_branch_source = self.tasks_source.split(
            "def _parse_chapter_branch_mode", 1
        )[1].split("@celery_app.task", 1)[0]

        self.assertIn('if dst_topo.get("type") == "ending":', chapter_branch_source)
        self.assertIn('choice_text = ""', chapter_branch_source)
        self.assertIn('choice_text = dst_topo.get("choice_text") or "继续"', chapter_branch_source)

    def test_reused_checkpoint_topology_is_normalized_before_generation(self):
        chapter_branch_source = self.tasks_source.split(
            "def _parse_chapter_branch_mode", 1
        )[1].split("@celery_app.task", 1)[0]

        reuse_pos = chapter_branch_source.find("topo_result = _saved_topology_result")
        normalize_pos = chapter_branch_source.find(
            "normalize_topology_choice_texts(topo_result)",
            reuse_pos,
        )
        topology_pos = chapter_branch_source.find(
            'topology = topo_result.get("topology", [])',
            reuse_pos,
        )
        self.assertGreaterEqual(reuse_pos, 0)
        self.assertGreater(normalize_pos, reuse_pos)
        self.assertGreater(topology_pos, normalize_pos)

    def test_real_effect_schedule_and_threshold_calibration_run_before_route_export(self):
        chapter_branch_source = self.tasks_source.split(
            "def _parse_chapter_branch_mode", 1
        )[1].split("@celery_app.task", 1)[0]

        schedule_pos = chapter_branch_source.find("build_effect_key_schedule")
        calibration_pos = chapter_branch_source.find("calibrate_ending_rules")
        route_export_pos = chapter_branch_source.find("build_route_metadata")
        self.assertGreaterEqual(schedule_pos, 0)
        self.assertGreater(calibration_pos, schedule_pos)
        self.assertGreater(route_export_pos, calibration_pos)

        self.assertNotIn("apply_normalized_ending_rules(", chapter_branch_source)

    def test_every_story_mode_exports_normalized_ui_mode(self):
        single_source = self.tasks_source.split(
            "def _parse_single_mode", 1
        )[1].split("SOFT_BRANCH_OPEN", 1)[0]
        chapter_branch_source = self.tasks_source.split(
            "def _parse_chapter_branch_mode", 1
        )[1].split("@celery_app.task", 1)[0]
        multi_tree_source = self.tasks_source.split(
            "def generate_story_tree", 1
        )[1]

        self.assertIn('"UI_mode": infer_ui_mode(', single_source)
        self.assertIn('"UI_mode": infer_ui_mode(', chapter_branch_source)
        self.assertIn("json_data['UI_mode'] = infer_ui_mode(", multi_tree_source)

    def test_chapter_branch_persists_and_resumes_node_checkpoints(self):
        chapter_branch_source = self.tasks_source.split(
            "def _parse_chapter_branch_mode", 1
        )[1].split("@celery_app.task", 1)[0]

        self.assertIn("_load_chapter_branch_plan", chapter_branch_source)
        self.assertIn("initialize_chapter_branch_plan", chapter_branch_source)
        self.assertIn("update_plan_node", chapter_branch_source)
        self.assertIn('status="running"', chapter_branch_source)
        self.assertIn('status="completed"', chapter_branch_source)
        self.assertIn("get_story_md_by_name", chapter_branch_source)
        self.assertIn("from services.story_planner.table.story_tree_to_db import (", chapter_branch_source)
        self.assertIn("恢复已完成节点", chapter_branch_source)

    def test_chapter_branch_checkpoints_preprocessing_before_llm_analysis(self):
        chapter_branch_source = self.tasks_source.split(
            "def _parse_chapter_branch_mode", 1
        )[1].split("@celery_app.task", 1)[0]
        checkpoint_pos = chapter_branch_source.find("build_chapter_processing_checkpoint")
        reuse_pos = chapter_branch_source.find("get_reusable_chapter_processing_checkpoint")
        analysis_pos = chapter_branch_source.find("analyze_novel(")
        self.assertGreaterEqual(checkpoint_pos, 0)
        self.assertGreaterEqual(reuse_pos, 0)
        self.assertGreater(analysis_pos, reuse_pos)
        self.assertIn('"preprocessing_checkpoint"', chapter_branch_source)

    def test_plan_save_refreshes_runtime_enriched_topology_fingerprint(self):
        save_source = self.tasks_source.split(
            "def _save_chapter_branch_plan", 1
        )[1].split("def _record_chapter_branch_heartbeat", 1)[0]

        self.assertIn("refresh_plan_topology_fingerprint(plan)", save_source)

    def test_resumed_artifact_recovers_missing_terminal_states(self):
        chapter_branch_source = self.tasks_source.split(
            "def _parse_chapter_branch_mode", 1
        )[1].split("@celery_app.task", 1)[0]

        resume_pos = chapter_branch_source.find("if _candidate_ok:")
        strip_pos = chapter_branch_source.find(
            "strip_soft_branch_blocks(_candidate_content)",
            resume_pos,
        )
        extract_pos = chapter_branch_source.find(
            "extract_terminal_states(",
            strip_pos,
        )

        self.assertGreaterEqual(resume_pos, 0)
        self.assertGreater(strip_pos, resume_pos)
        self.assertGreater(extract_pos, strip_pos)
        self.assertIn("chapter_idx_to_states[ch_idx]", chapter_branch_source[resume_pos:strip_pos])

    def test_whatif_generation_persists_scene_level_checkpoints(self):
        chapter_branch_source = self.tasks_source.split(
            "def _parse_chapter_branch_mode", 1
        )[1].split("@celery_app.task", 1)[0]

        self.assertIn("checkpoint_callback=_persist_whatif_scene", chapter_branch_source)
        self.assertIn("resume_state=_plan_entry.get(\"whatif_scene_checkpoint\")", chapter_branch_source)
        self.assertIn('"whatif_scene_checkpoint"', chapter_branch_source)

    def test_whatif_body_budget_is_enforced_before_soft_branches(self):
        chapter_branch_source = self.tasks_source.split(
            "def _parse_chapter_branch_mode", 1
        )[1].split("@celery_app.task", 1)[0]

        budget_pos = chapter_branch_source.find("enforce_whatif_content_budget(")
        soft_branch_pos = chapter_branch_source.find("inject_soft_branches(", budget_pos)
        self.assertGreaterEqual(budget_pos, 0)
        self.assertGreater(soft_branch_pos, budget_pos)

    def test_terminal_state_scan_reuses_one_narrative_reality_contract(self):
        chapter_branch_source = self.tasks_source.split(
            "def _parse_chapter_branch_mode", 1
        )[1].split("@celery_app.task", 1)[0]

        self.assertIn(
            "_narrative_reality_contract = infer_narrative_reality_contract(",
            chapter_branch_source,
        )
        self.assertIn(
            "narrative_contract=_narrative_reality_contract",
            chapter_branch_source,
        )
        terminal_stage_pos = chapter_branch_source.find('"stage": "terminal_states"')
        terminal_cache_v4_pos = chapter_branch_source.find(
            '"cache_version": 5', terminal_stage_pos
        )
        novel_context_stage_pos = chapter_branch_source.find('"stage": "novel_context"')
        novel_context_cache_v4_pos = chapter_branch_source.find(
            '"cache_version": 5', novel_context_stage_pos
        )
        self.assertGreaterEqual(terminal_stage_pos, 0)
        self.assertGreater(terminal_cache_v4_pos, terminal_stage_pos)
        self.assertGreater(novel_context_cache_v4_pos, novel_context_stage_pos)
        self.assertNotIn("_legacy_novel_context_key", chapter_branch_source)

    def test_whatif_runtime_uses_classified_segment_repair(self):
        chapter_branch_source = self.tasks_source.split(
            "def _parse_chapter_branch_mode", 1
        )[1].split("@celery_app.task", 1)[0]

        self.assertIn("classify_convergence_repair", chapter_branch_source)
        self.assertIn("repair_whatif_convergence_segment", chapter_branch_source)
        self.assertIn("normalize_whatif_merge_policy", chapter_branch_source)

    def test_chapter_branch_invalidates_stale_plan_when_source_changes(self):
        chapter_branch_source = self.tasks_source.split(
            "def _parse_chapter_branch_mode", 1
        )[1].split("@celery_app.task", 1)[0]

        fingerprint_pos = chapter_branch_source.find("_source_fingerprint = content_sha256(text)")
        load_pos = chapter_branch_source.find("_existing_plan = _load_chapter_branch_plan")
        topology_reuse_pos = chapter_branch_source.find("_saved_topology_result")
        self.assertGreaterEqual(fingerprint_pos, 0)
        self.assertGreater(load_pos, fingerprint_pos)
        self.assertGreater(topology_reuse_pos, load_pos)
        self.assertIn("source_fingerprint=_source_fingerprint", chapter_branch_source)

    def test_chapter_branch_uses_two_phase_artifact_checkpoint(self):
        chapter_branch_source = self.tasks_source.split(
            "def _parse_chapter_branch_mode", 1
        )[1].split("@celery_app.task", 1)[0]

        persisting_pos = chapter_branch_source.find('status="persisting"')
        md_save_pos = chapter_branch_source.find(
            "md_record = _save_owned_chapter_branch_md"
        )
        completed_pos = chapter_branch_source.find('status="completed"', md_save_pos)
        self.assertGreaterEqual(persisting_pos, 0)
        self.assertGreater(md_save_pos, persisting_pos)
        self.assertGreater(completed_pos, md_save_pos)
        self.assertIn("artifact_sha256=", chapter_branch_source)
        self.assertIn("can_resume_node_checkpoint", chapter_branch_source)

    def test_targeted_node_regeneration_has_celery_entrypoint(self):
        self.assertIn(
            'name="services.story_planner.tasks.regenerate_chapter_branch_nodes"',
            self.tasks_source,
        )
        self.assertIn("def regenerate_chapter_branch_nodes", self.tasks_source)
        self.assertIn("reopen_plan_nodes", self.tasks_source)

    def test_quality_failure_diagnostics_are_written_by_production_boundary(self):
        from services.story_planner import tasks

        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {"STORY_PLANNER_DIAGNOSTIC_DIR": temp_dir},
        ):
            saved = tasks._save_chapter_branch_diagnostics(
                "task/unsafe:id",
                {"root_id": "node_1", "nodes": {}},
                {"passed": False, "errors": ["重复选项"]},
            )

            output_dir = Path(temp_dir) / "task_unsafe_id"
            self.assertTrue(saved)
            self.assertEqual(
                "node_1",
                json.loads(
                    (output_dir / "candidate_story_tree.json").read_text(encoding="utf-8")
                )["root_id"],
            )
            self.assertFalse(
                json.loads(
                    (output_dir / "quality_report.json").read_text(encoding="utf-8")
                )["passed"]
            )


if __name__ == "__main__":
    unittest.main()
