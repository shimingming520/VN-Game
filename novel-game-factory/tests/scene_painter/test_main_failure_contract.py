from __future__ import annotations

import ast
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import dotenv
import pytest

from common.platform_db import platform_engine
from common.progress_utils import StageLeaseLostError
from common.task_models import engine as business_engine
from services.scene_painter import main_to_db
from services.scene_painter.core import (
    artifact_contract,
    cover_generator,
    retry_failed_images,
    scene_generator,
)


def _configure_two_step_pipeline(monkeypatch, return_codes):
    monkeypatch.setitem(
        main_to_db.STEP_GROUPS,
        "all",
        [
            ("core/first.py", "first", []),
            ("core/recovery.py", "recovery", []),
        ],
    )
    codes = iter(return_codes)
    monkeypatch.setattr(
        main_to_db,
        "run_command",
        lambda *args, **kwargs: (next(codes), "", ""),
    )


class _FakeProcess:
    pid = 5151

    def __init__(self, return_code=0, output=""):
        self.return_code = return_code
        self.stdout = StringIO(output)

    def wait(self, timeout=None):
        return self.return_code


def test_scene_nonzero_step_and_incomplete_outputs_returns_failure(monkeypatch):
    _configure_two_step_pipeline(monkeypatch, [1, 0])
    monkeypatch.setattr(
        artifact_contract,
        "validate_scene_output_contract",
        lambda task_id: (_ for _ in ()).throw(
            artifact_contract.SceneImageArtifactError("missing scene")
        ),
    )

    assert main_to_db.main(task_id="task-scene", mode="all") == 1


def test_scene_stage_failure_does_not_become_partial_success(monkeypatch):
    _configure_two_step_pipeline(monkeypatch, [1, 0])
    monkeypatch.setattr(
        artifact_contract,
        "validate_scene_output_contract",
        lambda task_id: None,
    )

    assert main_to_db.main(task_id="task-scene", mode="all") == 1


def test_scene_asset_level_partial_exit_is_recovered_when_contract_is_complete(monkeypatch, tmp_path):
    _configure_two_step_pipeline(monkeypatch, [2, 0])
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path))
    (tmp_path / "outputs" / "task-scene").mkdir(parents=True)
    monkeypatch.setattr(
        artifact_contract,
        "validate_scene_output_contract",
        lambda *args, **kwargs: [],
    )
    assert main_to_db.main(task_id="task-scene", mode="all") == 0


def test_scene_subprocess_starts_new_session_and_limits_log_tail(monkeypatch):
    captured = {}

    def _popen(*args, **kwargs):
        captured.update(kwargs)
        return _FakeProcess(output="one\ntwo\nthree\n")

    monkeypatch.setattr(main_to_db.subprocess, "Popen", _popen)
    monkeypatch.setenv("SCENE_SUBPROCESS_LOG_TAIL_LINES", "2")

    return_code, output, error = main_to_db.run_command(
        ["fake"],
        "fake scene step",
        task_id="task-scene",
        timeout_seconds=30,
    )

    assert return_code == 0
    assert output == "two\nthree"
    assert error == ""
    assert captured["start_new_session"] is True


def test_scene_lease_loss_terminates_subprocess_group(monkeypatch):
    terminated = []
    monkeypatch.setattr(
        main_to_db.subprocess,
        "Popen",
        lambda *args, **kwargs: _FakeProcess(),
    )
    monkeypatch.setattr(
        main_to_db,
        "ensure_stage_lease_owned",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            StageLeaseLostError("lost lease")
        ),
    )
    monkeypatch.setattr(
        main_to_db,
        "_terminate_process_group",
        lambda process, description: terminated.append(description),
    )

    with pytest.raises(StageLeaseLostError, match="lost lease"):
        main_to_db.run_command(
            ["fake"],
            "fake scene step",
            task_id="task-scene",
            timeout_seconds=30,
        )

    assert terminated == ["fake scene step"]


def test_scene_subprocess_cleanup_uses_term_kill_and_reap(monkeypatch):
    signals = []
    waits = []

    class _StubbornProcess:
        pid = 6262

        def poll(self):
            return None

        def wait(self, timeout=None):
            waits.append(timeout)
            if timeout is None:
                return -9
            raise main_to_db.subprocess.TimeoutExpired("fake", timeout)

    monkeypatch.setattr(main_to_db.os, "name", "posix")
    monkeypatch.setattr(
        main_to_db.os,
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )
    monkeypatch.setenv("SCENE_SUBPROCESS_TERM_GRACE_SECONDS", "0.01")

    main_to_db._terminate_process_group(_StubbornProcess(), "fake scene step")

    assert signals == [
        (6262, main_to_db.signal.SIGTERM),
        (6262, main_to_db.signal.SIGKILL),
    ]
    assert waits == [0.01, None]


def test_scene_main_path_reuses_shared_database_engines():
    assert retry_failed_images._engine() is business_engine
    assert retry_failed_images._platform_engine() is platform_engine

    repository_root = Path(__file__).resolve().parents[2]
    relative_paths = [
        "services/scene_painter/core/scene_generator.py",
        "services/scene_painter/core/cg_generator.py",
        "services/scene_painter/core/retry_failed_images.py",
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


def test_cover_main_path_uses_single_outer_request_attempt():
    captured = {}

    class _Generator:
        api_max_retries = 99

        def retry_on_failure(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(data=b"image", format="png", width=10, height=10)

    cover_generator._wrap_generate_image(
        _Generator(),
        prompt="scene prompt",
        negative_prompt="",
        width=10,
        height=10,
    )

    assert scene_generator.SCENE_OUTER_REQUEST_ATTEMPTS == 1
    assert captured["max_retries"] == 1


def test_scene_prompt_rewrite_client_has_finite_timeout_and_retries(monkeypatch):
    captured = {}

    class _Completions:
        def create(self, **kwargs):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content='{"image_prompt": "safe rewritten scene"}'
                        )
                    )
                ]
            )

    class _Client:
        chat = SimpleNamespace(completions=_Completions())

    def _fake_openai(**kwargs):
        captured.update(kwargs)
        return _Client()

    monkeypatch.setenv("DASHSCOPE_API_KEY", "offline-test-key")
    monkeypatch.delenv("SCENE_LLM_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("SCENE_LLM_MAX_RETRIES", raising=False)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setattr(retry_failed_images, "OpenAI", _fake_openai)

    result = retry_failed_images.rewrite_prompt_for_moderation("original scene")

    assert result == "safe rewritten scene"
    assert captured["timeout"] == 120.0
    assert captured["max_retries"] == 2
