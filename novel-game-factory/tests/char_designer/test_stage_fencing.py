from __future__ import annotations

import sys
from types import ModuleType

import pytest

from common.progress_utils import StageLeaseLostError
from services.char_designer import tasks


def _install_character_pipeline_modules(monkeypatch, events):
    fake_main = ModuleType("main")
    fake_main.run_pipeline = lambda **kwargs: events.append("pipeline") or kwargs["task_id"]
    fake_main._is_cg_identity_only_task = (
        lambda task_id: events.append("identity_check") or False
    )

    fake_core = ModuleType("core")
    fake_core.__path__ = []
    fake_identity = ModuleType("core.generate_cg_identity_references")
    fake_identity.validate_required_identity_references = (
        lambda task_id: events.append("validate")
    )

    monkeypatch.setitem(sys.modules, "main", fake_main)
    monkeypatch.setitem(sys.modules, "core", fake_core)
    monkeypatch.setitem(
        sys.modules,
        "core.generate_cg_identity_references",
        fake_identity,
    )


def _patch_character_task_dependencies(monkeypatch, events):
    from common import output_mirror

    # These tests exercise stage fencing/order only.  Preserve the custom route
    # so no database-backed automatic global-style preflight is needed here.
    monkeypatch.setattr(
        tasks,
        "load_visual_style_config",
        lambda *args, **kwargs: {"mode": "custom"},
    )
    monkeypatch.setattr(
        tasks, "claim_stage_for_validated_resume", lambda *args, **kwargs: "execute"
    )
    monkeypatch.setattr(
        tasks, "publish_character_stage_checkpoint", lambda *args: None
    )
    monkeypatch.setattr(tasks, "update_task_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr(tasks, "_task_image_model", lambda task_id: None)
    monkeypatch.setattr(
        tasks,
        "write_character_image_manifest",
        lambda task_id: events.append("manifest") or "/tmp/manifest.json",
    )
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


def test_character_task_checks_lease_before_manifest_and_mirror(monkeypatch):
    events = []
    _install_character_pipeline_modules(monkeypatch, events)
    _patch_character_task_dependencies(monkeypatch, events)
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

    assert tasks.generate_characters.run("task-character") == "task-character"

    assert events == [
        "pipeline",
        "validate",
        "lease",
        "identity_check",
        "manifest",
        "lease",
        "mirror",
        "complete",
    ]


def test_character_task_does_not_fail_stage_after_lease_loss(monkeypatch):
    events = []
    _install_character_pipeline_modules(monkeypatch, events)
    _patch_character_task_dependencies(monkeypatch, events)
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
        tasks.generate_characters.run("task-character")

    assert fail_calls == []
    assert "manifest" not in events
    assert "mirror" not in events
    assert "complete" not in events


def test_character_completed_stage_skips_when_resume_contract_is_valid(monkeypatch):
    monkeypatch.setattr(
        tasks,
        "claim_stage_for_validated_resume",
        lambda *args, **kwargs: "complete",
    )
    monkeypatch.setattr(
        tasks,
        "validate_character_stage_checkpoint",
        lambda task_id: True,
    )
    monkeypatch.setattr(
        tasks,
        "update_task_progress",
        lambda *args, **kwargs: pytest.fail("完整断点不应进入任务主体"),
    )

    assert tasks.generate_characters.run("task-character") == "task-character"
