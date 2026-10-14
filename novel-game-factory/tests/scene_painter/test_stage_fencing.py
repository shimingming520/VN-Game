from __future__ import annotations

import sys
from types import ModuleType

import pytest

from common.progress_utils import StageLeaseLostError
from services.scene_painter import tasks


def _install_scene_pipeline_modules(monkeypatch, events):
    fake_main = ModuleType("main_to_db")
    fake_main.main = (
        lambda **kwargs: events.append(f"pipeline:{kwargs.get('mode')}") or 0
    )

    fake_core = ModuleType("core")
    fake_core.__path__ = []
    fake_contract = ModuleType("core.artifact_contract")
    fake_contract.validate_scene_output_contract = (
        lambda task_id: events.append("contract")
    )

    monkeypatch.setitem(sys.modules, "main_to_db", fake_main)
    monkeypatch.setitem(sys.modules, "core", fake_core)
    monkeypatch.setitem(sys.modules, "core.artifact_contract", fake_contract)


def _patch_scene_task_dependencies(monkeypatch, events):
    from common import output_mirror

    # These tests focus on stage fencing and pipeline ordering.  Use the
    # preserved custom route so they do not need to create real LLM style
    # Anchors; the fixed LLM route is covered by visual-style tests.
    monkeypatch.setattr(
        tasks,
        "load_visual_style_config",
        lambda *args, **kwargs: {"mode": "custom"},
    )
    monkeypatch.setattr(
        tasks, "claim_stage_for_validated_resume", lambda *args, **kwargs: "execute"
    )
    monkeypatch.setattr(tasks, "publish_scene_stage_checkpoint", lambda *args: None)
    monkeypatch.setattr(
        tasks, "synchronize_replayed_character_assets", lambda *args: {}
    )
    monkeypatch.setattr(
        tasks, "validate_character_asset_name_finalization", lambda *args, **kwargs: {}
    )
    monkeypatch.setattr(
        tasks, "_wait_for_character_assets", lambda *args: events.append("join")
    )
    monkeypatch.setattr(tasks, "update_task_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr(tasks, "_task_image_model", lambda task_id: None)
    monkeypatch.setattr(
        output_mirror,
        "mirror_outputs_with_policy",
        lambda *args, **kwargs: events.append("mirror"),
    )
    monkeypatch.setattr(
        tasks,
        "complete_stage",
        lambda *args, **kwargs: events.append("complete"),
    )


def test_scene_task_checks_lease_before_mirror_and_completion(monkeypatch):
    events = []
    _install_scene_pipeline_modules(monkeypatch, events)
    _patch_scene_task_dependencies(monkeypatch, events)
    monkeypatch.setattr(
        tasks,
        "ensure_stage_lease_owned",
        lambda *args, **kwargs: events.append("lease"),
    )
    monkeypatch.setattr(
        tasks,
        "fail_stage",
        lambda *args, **kwargs: pytest.fail("成功路径不应标记失败"),
    )

    assert tasks.generate_scenes.run("task-scene") == "task-scene"

    assert events == [
        "pipeline:scene_assets",
        "join",
        "pipeline:cg",
        "contract",
        "lease",
        "mirror",
        "complete",
    ]


def test_scene_task_does_not_fail_stage_after_lease_loss(monkeypatch):
    events = []
    _install_scene_pipeline_modules(monkeypatch, events)
    _patch_scene_task_dependencies(monkeypatch, events)
    fail_calls = []
    monkeypatch.setattr(
        tasks,
        "ensure_stage_lease_owned",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            StageLeaseLostError("lost lease")
        ),
    )
    monkeypatch.setattr(
        tasks,
        "fail_stage",
        lambda *args, **kwargs: fail_calls.append(args),
    )

    with pytest.raises(StageLeaseLostError, match="lost lease"):
        tasks.generate_scenes.run("task-scene")

    assert fail_calls == []
    assert "mirror" not in events
    assert "complete" not in events


def test_scene_completed_stage_skips_when_resume_contract_is_valid(monkeypatch):
    monkeypatch.setattr(
        tasks,
        "claim_stage_for_validated_resume",
        lambda *args, **kwargs: "complete",
    )
    monkeypatch.setattr(
        tasks,
        "validate_scene_stage_checkpoint",
        lambda task_id: True,
    )
    monkeypatch.setattr(
        tasks,
        "update_task_progress",
        lambda *args, **kwargs: pytest.fail("完整断点不应进入任务主体"),
    )

    assert tasks.generate_scenes.run("task-scene") == "task-scene"


def test_scene_reuses_current_global_style_without_generating(monkeypatch):
    monkeypatch.setattr(tasks, "ensure_stage_lease_owned", lambda *args: True)
    monkeypatch.setattr(tasks, "get_current_global_style", lambda task_id: "style")
    monkeypatch.setattr(
        tasks,
        "ensure_global_style_generated",
        lambda task_id: pytest.fail("并行正常路径不应由 scene_painter 生成新风格"),
    )

    assert tasks._wait_for_character_owned_global_style("task-scene") == "style"


def test_scene_direct_replay_can_use_explicit_global_style_fallback(monkeypatch):
    clock = iter([0.0, 0.0, 2.0, 2.0])
    monkeypatch.setenv("SCENE_ALLOW_GLOBAL_STYLE_FALLBACK", "1")
    monkeypatch.setenv("SCENE_GLOBAL_STYLE_PENDING_GRACE_SECONDS", "1")
    monkeypatch.setattr(tasks.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(tasks.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(tasks, "ensure_stage_lease_owned", lambda *args: True)
    monkeypatch.setattr(tasks, "get_current_global_style", lambda task_id: "")
    monkeypatch.setattr(
        tasks,
        "get_task_progress",
        lambda task_id: {"stages": {"char_designer": 0}},
    )
    monkeypatch.setattr(
        tasks, "ensure_global_style_generated", lambda task_id: "fallback-style"
    )

    assert (
        tasks._wait_for_character_owned_global_style("task-scene")
        == "fallback-style"
    )
