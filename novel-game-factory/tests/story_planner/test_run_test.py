from types import SimpleNamespace

import pytest

from common.task_models import InfoConfirmStage, TaskStatus
from services.story_planner.run_test import (
    build_manual_task_values,
    _reset_existing_task,
    validate_manual_planner_handoff,
)


def test_manual_task_values_include_downstream_contract_and_planner_processing_state():
    values = build_manual_task_values(
        novel_filename="demo.txt",
        project_name="demo",
        output_language="zh-CN",
        art_style="ink_chinese",
        story_mode="chapter_branch",
    )

    assert values["story_planner_status"] == TaskStatus.PROCESSING
    assert values["overall_status"] == TaskStatus.PROCESSING
    assert values["progress_percentage"] == 18
    assert values["image_model"] == "gpt-image-2"
    assert values["text_model"] == "gpt-5.6-sol"
    assert values["audio_model"] == "speech-2.8-hd"
    assert values["resolution"] == "1080p"
    assert values["gameplay"] == "interactive_story"
    assert values["upload_type"] == "single"
    assert values["info_confirm_stage"] == InfoConfirmStage.CONFIRMED


def test_validate_manual_planner_handoff_rejects_incomplete_database_outputs():
    errors = validate_manual_planner_handoff(
        story_mode="chapter_branch",
        task=SimpleNamespace(
            story_planner_status=TaskStatus.PENDING,
            overall_status=TaskStatus.PENDING,
            image_model=None,
        ),
        story_tree_data={"nodes": {}},
        md_names=[],
        summary=SimpleNamespace(status=0, data={}),
        chapter_branch_plan=None,
        worldview=None,
    )

    assert any("story_planner_status" in error for error in errors)
    assert any("image_model" in error for error in errors)
    assert any("story_tree_json" in error for error in errors)
    assert any("story_tree_md" in error for error in errors)
    assert any("story_summary" in error for error in errors)
    assert any("chapter_branch_plan" in error for error in errors)
    assert any("world_bible" in error for error in errors)


def test_validate_manual_planner_handoff_accepts_complete_chapter_branch_outputs():
    story_tree = {
        "en-US": {
            "nodes": {"ch1": {}, "ending": {}},
            "quality_gate": {"passed": True, "score": 90},
        },
        "ja-JP": {
            "nodes": {"ch1": {}, "ending": {}},
            "quality_gate": {"passed": True, "score": 90},
        },
        "publication": {"fingerprint": "abc"},
    }
    errors = validate_manual_planner_handoff(
        story_mode="chapter_branch",
        task=SimpleNamespace(
            story_planner_status=TaskStatus.COMPLETED,
            overall_status=TaskStatus.PROCESSING,
            image_model="gpt-image-2",
        ),
        story_tree_data=story_tree,
        md_names={"ch1", "ending"},
        summary=SimpleNamespace(
            status=1,
            data={"outline": "o", "background": "b", "one_liner": "l"},
        ),
        chapter_branch_plan={"nodes": {"ch1": {}, "ending": {}}},
        worldview=SimpleNamespace(
            status=1,
            data={
                "worldview": {},
                "char_cards": "cards",
                "relationship_map": "map",
                "world_rules": "rules",
                "narrative_arc": "arc",
            },
        ),
    )

    assert errors == []


def test_reset_existing_task_rejects_published_or_active_rows():
    values = build_manual_task_values(
        novel_filename="demo.txt",
        project_name="demo",
        output_language="zh-CN",
        art_style="ink_chinese",
        story_mode="chapter_branch",
    )

    with pytest.raises(RuntimeError, match="已发布"):
        _reset_existing_task(
            SimpleNamespace(task_id="published", is_published=True), values
        )

    with pytest.raises(RuntimeError, match="阶段运行中"):
        _reset_existing_task(
            SimpleNamespace(
                task_id="active",
                is_published=False,
                is_deleted=False,
                is_purged=False,
                story_planner_status=TaskStatus.PENDING,
                info_extractor_status=TaskStatus.PROCESSING,
                sound_arranger_status=TaskStatus.PENDING,
                char_designer_status=TaskStatus.PENDING,
                audio_producer_status=TaskStatus.PENDING,
                scene_painter_status=TaskStatus.PENDING,
                game_builder_status=TaskStatus.PENDING,
            ),
            values,
        )
