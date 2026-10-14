from __future__ import annotations

import ast
from io import StringIO
from pathlib import Path

import pytest

from common.progress_utils import StageLeaseLostError
from services.char_designer import main as character_main


class _FakeProcess:
    def __init__(self, return_code: int):
        self.return_code = return_code
        self.stdout = StringIO("")

    def wait(self, timeout=None):
        return self.return_code


def _run_one_fake_step(monkeypatch, return_code: int) -> bool:
    monkeypatch.setattr(
        character_main,
        "_build_character_generation_steps",
        lambda *args, **kwargs: [("fake_step.py", "fake step", 30)],
    )
    monkeypatch.setattr(
        character_main.subprocess,
        "Popen",
        lambda *args, **kwargs: _FakeProcess(return_code),
    )
    return character_main.generate_images_from_db_fallback("task-character")


def test_character_subprocess_nonzero_is_pipeline_failure(monkeypatch):
    assert _run_one_fake_step(monkeypatch, return_code=7) is False


def test_character_subprocess_zero_is_pipeline_success(monkeypatch):
    assert _run_one_fake_step(monkeypatch, return_code=0) is True


def test_character_asset_level_partial_exit_is_recoverable(monkeypatch):
    monkeypatch.setattr(
        character_main,
        "write_failure_report",
        lambda *args, **kwargs: "/tmp/char-report.json",
    )
    assert _run_one_fake_step(monkeypatch, return_code=2) is True


def test_character_subprocess_starts_new_session(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        character_main,
        "_build_character_generation_steps",
        lambda *args, **kwargs: [("fake_step.py", "fake step", 30)],
    )

    def _popen(*args, **kwargs):
        captured.update(kwargs)
        return _FakeProcess(0)

    monkeypatch.setattr(character_main.subprocess, "Popen", _popen)

    assert character_main.generate_images_from_db_fallback("task-character") is True
    assert captured["start_new_session"] is True


def test_character_lease_loss_terminates_subprocess_group(monkeypatch):
    terminated = []

    monkeypatch.setattr(
        character_main,
        "_build_character_generation_steps",
        lambda *args, **kwargs: [("fake_step.py", "fake step", 30)],
    )
    monkeypatch.setattr(
        character_main.subprocess,
        "Popen",
        lambda *args, **kwargs: _FakeProcess(0),
    )
    monkeypatch.setattr(
        character_main,
        "ensure_stage_lease_owned",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            StageLeaseLostError("lost lease")
        ),
    )
    monkeypatch.setattr(
        character_main,
        "_terminate_process_group",
        lambda process, script: terminated.append(script),
    )

    with pytest.raises(StageLeaseLostError, match="lost lease"):
        character_main.generate_images_from_db_fallback("task-character")

    assert terminated == ["fake_step.py"]


def test_character_subprocess_cleanup_uses_term_kill_and_reap(monkeypatch):
    signals = []
    waits = []

    class _StubbornProcess:
        pid = 4242

        def poll(self):
            return None

        def wait(self, timeout=None):
            waits.append(timeout)
            if timeout is None:
                return -9
            raise character_main.subprocess.TimeoutExpired("fake", timeout)

    monkeypatch.setattr(character_main.os, "name", "posix")
    monkeypatch.setattr(
        character_main.os,
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )
    monkeypatch.setenv("CHAR_SUBPROCESS_TERM_GRACE_SECONDS", "0.01")

    character_main._terminate_process_group(_StubbornProcess(), "fake_step.py")

    assert signals == [
        (4242, character_main.signal.SIGTERM),
        (4242, character_main.signal.SIGKILL),
    ]
    assert waits == [0.01, None]


def test_character_main_path_does_not_create_function_scoped_engines():
    repository_root = Path(__file__).resolve().parents[2]
    relative_paths = [
        "services/char_designer/main.py",
        "services/char_designer/core/generate_calm.py",
        "services/char_designer/core/generate_variant_calm.py",
        "services/char_designer/core/generate_emotion.py",
        "services/char_designer/core/expand_canvas.py",
        "services/char_designer/utils/prompt_repair.py",
    ]

    offenders = []
    for relative_path in relative_paths:
        path = repository_root / relative_path
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "create_engine"
            ):
                offenders.append(f"{relative_path}:{node.lineno}")

    assert offenders == []
