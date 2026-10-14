from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

from common.artifact_io import ArtifactValidationError
from services.scene_painter.core import artifact_contract


def _png_bytes(color: tuple[int, int, int] = (40, 60, 80)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (10, 7), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _write_png(path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_png_bytes())


@pytest.mark.parametrize(
    "payload, expected",
    [
        (_png_bytes(), True),
        (b"", False),
        (b"fake-png", False),
        (_png_bytes()[:24], False),
    ],
)
def test_scene_image_validation_contract(tmp_path, payload, expected):
    image_path = tmp_path / "scene.png"
    image_path.write_bytes(payload)

    assert artifact_contract.validate_image_file(image_path) is expected


def test_scene_atomic_failure_preserves_previous_valid_image(tmp_path):
    final_path = tmp_path / "scene.png"
    previous = _png_bytes((1, 2, 3))
    final_path.write_bytes(previous)

    with pytest.raises(ArtifactValidationError):
        with artifact_contract.atomic_image_output(final_path) as temporary_path:
            temporary_path.write_bytes(b"partial")

    assert final_path.read_bytes() == previous
    assert artifact_contract.validate_image_file(final_path) is True


def test_scene_atomic_publish_rejects_lost_inherited_execution(tmp_path, monkeypatch):
    from common import progress_utils

    final_path = tmp_path / "fenced-scene.png"
    monkeypatch.setattr(
        progress_utils,
        "ensure_inherited_stage_lease_owned",
        lambda: (_ for _ in ()).throw(RuntimeError("stale execution")),
    )

    with pytest.raises(RuntimeError, match="stale execution"):
        with artifact_contract.atomic_image_output(final_path) as temporary_path:
            temporary_path.write_bytes(_png_bytes())

    assert not final_path.exists()


def test_scene_output_oracle_validates_scenes_instances_covers_and_cg(
    tmp_path,
    monkeypatch,
):
    task_id = "task-scene-contract"
    task_dir = tmp_path / task_id
    expected_data = {
        "scene_prompts": {
            "scene-room": {"image_prompt": "room", "copy_count": 1},
        },
        "backgrounds": {
            "generated_prompts": {"chapter_1": {"prompt": "cover"}},
            "novel_cover_metadata": {"prompt": "novel cover"},
        },
        "cg": {
            "cg_1": {"cg_prompt": "dramatic moment"},
        },
    }
    monkeypatch.setattr(
        artifact_contract,
        "_load_platform_json",
        lambda received_task_id, table: expected_data[table],
    )

    _write_png(task_dir / "images" / "scenes" / "scene-room.png")
    _write_png(task_dir / "images" / "scenes" / "scene-room_1.png")
    _write_png(task_dir / "images" / "backgrounds" / "chapter_1.png")
    _write_png(task_dir / "images" / "backgrounds" / "novel_cover.png")
    _write_png(task_dir / "images" / "cg" / "cg_1.png")

    artifact_contract.validate_scene_output_contract(
        task_id,
        outputs_root=tmp_path,
    )

    (task_dir / "images" / "cg" / "cg_1.png").write_bytes(b"damaged")
    with pytest.raises(artifact_contract.SceneImageArtifactError, match="CG cg_1"):
        artifact_contract.validate_scene_output_contract(
            task_id,
            outputs_root=tmp_path,
        )


def test_scene_asset_oracle_can_validate_before_cg_join(tmp_path, monkeypatch):
    task_id = "task-scene-assets"
    task_dir = tmp_path / task_id
    expected_data = {
        "scene_prompts": {"scene-room": {"image_prompt": "room"}},
        "backgrounds": {
            "generated_prompts": {"chapter_1": {"prompt": "cover"}},
            "novel_cover_metadata": {},
        },
        "cg": {"cg_1": {"cg_prompt": "later"}},
    }
    monkeypatch.setattr(
        artifact_contract,
        "_load_platform_json",
        lambda received_task_id, table: expected_data[table],
    )
    _write_png(task_dir / "images" / "scenes" / "scene-room.png")
    _write_png(task_dir / "images" / "backgrounds" / "chapter_1.png")

    artifact_contract.validate_scene_output_contract(
        task_id,
        outputs_root=tmp_path,
        include_cg=False,
    )
