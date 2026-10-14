from __future__ import annotations

from pathlib import Path

from PIL import Image
from sqlalchemy import create_engine

from common.visual_checkpoint import (
    canonical_digest,
    checkpoint_is_reusable,
    load_checkpoint,
    mark_failed,
    mark_running,
    mark_success,
    reset_visual_checkpoint_schema_cache_for_tests,
)


def _png(path: Path, color=(10, 20, 30)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 12), color).save(path, format="PNG")
    return path


def _valid(path) -> bool:
    try:
        with Image.open(path) as image:
            image.verify()
        return True
    except Exception:
        return False


def test_canonical_digest_is_order_independent():
    assert canonical_digest({"b": 2, "a": [1, {"z": True}]}) == canonical_digest(
        {"a": [1, {"z": True}], "b": 2}
    )


def test_checkpoint_requires_matching_input_and_intact_output(tmp_path):
    engine = create_engine("sqlite://")
    reset_visual_checkpoint_schema_cache_for_tests()
    task_id = "task-one"
    output = _png(tmp_path / task_id / "images" / "scene.png")
    digest = canonical_digest({"prompt": "one"})

    mark_success(
        task_id,
        "scene_painter",
        "generate_scene",
        "scene_1",
        input_digest=digest,
        generator_version="v1",
        output_paths=[output],
        validator=_valid,
        outputs_root=tmp_path,
        engine=engine,
    )
    record = load_checkpoint(
        task_id,
        "scene_painter",
        "generate_scene",
        "scene_1",
        engine=engine,
    )
    assert checkpoint_is_reusable(
        record,
        task_id=task_id,
        input_digest=digest,
        generator_version="v1",
        output_paths=[output],
        validator=_valid,
        outputs_root=tmp_path,
    ).reusable
    assert not checkpoint_is_reusable(
        record,
        task_id=task_id,
        input_digest=canonical_digest({"prompt": "changed"}),
        generator_version="v1",
        output_paths=[output],
        validator=_valid,
        outputs_root=tmp_path,
    ).reusable

    output.write_bytes(b"broken")
    decision = checkpoint_is_reusable(
        record,
        task_id=task_id,
        input_digest=digest,
        generator_version="v1",
        output_paths=[output],
        validator=_valid,
        outputs_root=tmp_path,
    )
    assert not decision.reusable
    assert decision.reason.startswith("output_invalid:")


def test_failed_retry_count_resets_when_input_changes():
    engine = create_engine("sqlite://")
    reset_visual_checkpoint_schema_cache_for_tests()
    kwargs = {
        "task_id": "task-two",
        "stage": "char_designer",
        "step_name": "generate_emotion",
        "asset_id": "char:happy",
        "generator_version": "v1",
        "engine": engine,
    }
    mark_running(input_digest="old", **kwargs)
    mark_failed(input_digest="old", error="first", **kwargs)
    mark_running(input_digest="old", **kwargs)
    mark_failed(input_digest="old", error="second", **kwargs)
    assert load_checkpoint(
        "task-two", "char_designer", "generate_emotion", "char:happy", engine=engine
    ).retry_count == 2

    mark_running(input_digest="new", **kwargs)
    mark_failed(input_digest="new", error="new input", **kwargs)
    record = load_checkpoint(
        "task-two", "char_designer", "generate_emotion", "char:happy", engine=engine
    )
    assert record.retry_count == 1
    assert record.input_digest == "new"

    version_two = {**kwargs, "generator_version": "v2"}
    mark_running(input_digest="new", **version_two)
    mark_failed(input_digest="new", error="new generator", **version_two)
    record = load_checkpoint(
        "task-two", "char_designer", "generate_emotion", "char:happy", engine=engine
    )
    assert record.retry_count == 1
    assert record.generator_version == "v2"
