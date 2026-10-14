from types import SimpleNamespace

from common import oss_utils
from common.local_runtime import (
    local_pipeline_enabled,
    oss_disabled,
    submit_local_visual_pipeline,
)
from common.progress_utils import _all_required_stages_completed
from common.task_models import TaskStatus


class _FakeWorkflow:
    def __init__(self, steps):
        self.steps = steps
        self.applied = False

    def apply_async(self):
        self.applied = True
        return self


class _FakeCelery:
    def signature(self, name, **options):
        return name, options


def test_local_storage_is_always_enabled(monkeypatch):
    monkeypatch.delenv("OSS_DISABLED", raising=False)
    monkeypatch.delenv("LOCAL_PIPELINE_MODE", raising=False)
    assert oss_disabled() is True
    assert local_pipeline_enabled() is False

    monkeypatch.setenv("OSS_DISABLED", "false")
    monkeypatch.setenv("LOCAL_PIPELINE_MODE", "1")
    assert oss_disabled() is True
    assert local_pipeline_enabled() is True


def test_storage_never_initializes_remote_client(monkeypatch, tmp_path):
    monkeypatch.delenv("OSS_DISABLED", raising=False)
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path))
    monkeypatch.setattr(
        oss_utils,
        "_bucket",
        lambda: (_ for _ in ()).throw(AssertionError("OSS client must not be used")),
    )

    key = "storage/raw_novels/test.txt"
    assert oss_utils.oss_write(key, b"novel") is True
    assert oss_utils.oss_read(key) == b"novel"
    assert oss_utils.oss_exists(key) is True
    assert oss_utils.oss_list_keys("storage/raw_novels") == [key]
    assert oss_utils.oss_get_size(key) == len(b"novel")
    assert oss_utils.oss_delete(key) is True


def test_local_pipeline_contains_visual_services_only():
    captured = {}

    def fake_chain(*steps):
        workflow = _FakeWorkflow(steps)
        captured["workflow"] = workflow
        return workflow

    def fake_group(*steps):
        return ("group", steps)

    def fake_chord(header, callback):
        return ("chord", header, callback)

    result = submit_local_visual_pipeline(
        _FakeCelery(),
        "local-task",
        chain_factory=fake_chain,
        group_factory=fake_group,
        chord_factory=fake_chord,
    )

    prefix_names = [step[0] for step in captured["workflow"].steps[:2]]
    assert prefix_names == [
        "services.story_planner.tasks.parse_novel_to_draft",
        "services.info_extractor.tasks.extract_info",
    ]
    visual_join = captured["workflow"].steps[2]
    assert visual_join[0] == "chord"
    visual_names = [step[0] for step in visual_join[1][1]]
    assert visual_names == [
        "services.char_designer.tasks.generate_characters",
        "services.scene_painter.tasks.generate_scenes",
    ]
    assert visual_join[2][0] == "services.game_builder.tasks.build_game_package"
    names = prefix_names + visual_names + [visual_join[2][0]]
    assert all("audio" not in name and "sound" not in name for name in names)
    assert result.applied is True


def test_local_visual_completion_does_not_wait_for_audio(monkeypatch):
    task = SimpleNamespace(
        story_planner_status=TaskStatus.COMPLETED,
        info_extractor_status=TaskStatus.COMPLETED,
        sound_arranger_status=TaskStatus.PENDING,
        char_designer_status=TaskStatus.COMPLETED,
        audio_producer_status=TaskStatus.PENDING,
        scene_painter_status=TaskStatus.COMPLETED,
        game_builder_status=TaskStatus.COMPLETED,
    )

    monkeypatch.setenv("LOCAL_PIPELINE_MODE", "1")
    assert _all_required_stages_completed(task) is True

    monkeypatch.delenv("LOCAL_PIPELINE_MODE", raising=False)
    assert _all_required_stages_completed(task) is False
