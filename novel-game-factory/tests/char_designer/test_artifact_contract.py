from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

from common.artifact_io import ArtifactValidationError
from services.char_designer.core.artifact_contract import (
    atomic_image_output,
    validate_image_file,
    validate_transparent_character_file,
)
from services.char_designer.core.character_manifest import (
    CharacterManifestError,
    build_character_image_manifest,
    write_character_image_manifest,
)


def _png_bytes(color: tuple[int, int, int, int] = (20, 40, 60, 255)) -> bytes:
    buffer = BytesIO()
    Image.new("RGBA", (8, 6), color).save(buffer, format="PNG")
    return buffer.getvalue()


def test_validate_image_file_accepts_complete_png(tmp_path):
    image_path = tmp_path / "valid.png"
    image_path.write_bytes(_png_bytes())

    assert validate_image_file(image_path) is True


def test_transparent_character_contract_rejects_opaque_png(tmp_path):
    opaque = tmp_path / "opaque.png"
    transparent = tmp_path / "transparent.png"
    opaque.write_bytes(_png_bytes((20, 40, 60, 255)))
    transparent_image = Image.new("RGBA", (8, 6), (0, 0, 0, 0))
    for x in range(2, 6):
        for y in range(1, 5):
            transparent_image.putpixel((x, y), (20, 40, 60, 100))
    transparent_image.save(transparent, format="PNG")

    assert validate_transparent_character_file(opaque) is False
    assert validate_transparent_character_file(transparent) is True


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"not-an-image",
        _png_bytes()[:20],
    ],
)
def test_validate_image_file_rejects_empty_fake_and_truncated_png(tmp_path, payload):
    image_path = tmp_path / "invalid.png"
    image_path.write_bytes(payload)

    assert validate_image_file(image_path) is False


def test_atomic_image_output_publishes_only_complete_image(tmp_path):
    final_path = tmp_path / "character.png"

    with atomic_image_output(final_path) as temporary_path:
        temporary_path.write_bytes(_png_bytes())

    assert validate_image_file(final_path) is True


def test_atomic_image_output_applies_asset_specific_validator(tmp_path):
    final_path = tmp_path / "character.png"

    with pytest.raises(ArtifactValidationError):
        with atomic_image_output(
            final_path,
            artifact_validator=validate_transparent_character_file,
            ownership_check=lambda: True,
        ) as temporary_path:
            temporary_path.write_bytes(_png_bytes((20, 40, 60, 255)))

    assert not final_path.exists()


def test_atomic_image_output_failure_preserves_previous_valid_image(tmp_path):
    final_path = tmp_path / "character.png"
    previous = _png_bytes((10, 20, 30, 255))
    final_path.write_bytes(previous)

    with pytest.raises(ArtifactValidationError):
        with atomic_image_output(final_path) as temporary_path:
            temporary_path.write_bytes(b"truncated-provider-response")

    assert final_path.read_bytes() == previous
    assert validate_image_file(final_path) is True


def test_atomic_image_output_rejects_lost_inherited_execution(tmp_path, monkeypatch):
    from common import progress_utils

    final_path = tmp_path / "fenced-character.png"
    monkeypatch.setattr(
        progress_utils,
        "ensure_inherited_stage_lease_owned",
        lambda: (_ for _ in ()).throw(RuntimeError("stale execution")),
    )

    with pytest.raises(RuntimeError, match="stale execution"):
        with atomic_image_output(final_path) as temporary_path:
            temporary_path.write_bytes(_png_bytes())

    assert not final_path.exists()


def test_character_manifest_rejects_damaged_referenced_image(tmp_path):
    task_id = "task-character-contract"
    character_dir = tmp_path / task_id / "images" / "characters"
    character_dir.mkdir(parents=True)
    (character_dir / "Hero_1-1-1_calm_LS.png").write_bytes(b"fake-png")

    with pytest.raises(CharacterManifestError, match="damaged"):
        build_character_image_manifest(
            task_id,
            outputs_root=tmp_path,
            prompt_records=[
                {
                    "speaker_id": "Hero",
                    "combination": "Hero-1-1-1",
                }
            ],
        )


def test_character_manifest_checks_lease_before_atomic_publish(tmp_path, monkeypatch):
    task_id = "task-character-fence"
    character_dir = tmp_path / task_id / "images" / "characters"
    character_dir.mkdir(parents=True)
    (character_dir / "Hero_1-1-1_calm_LS.png").write_bytes(_png_bytes())

    from services.char_designer.core import character_manifest

    monkeypatch.setattr(
        character_manifest,
        "ensure_stage_lease_owned",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("lost owner")),
    )

    with pytest.raises(RuntimeError, match="lost owner"):
        write_character_image_manifest(
            task_id,
            outputs_root=tmp_path,
            prompt_records=[
                {
                    "speaker_id": "Hero",
                    "combination": "Hero-1-1-1",
                }
            ],
        )

    assert not (
        tmp_path / task_id / "manifests" / "character_image_manifest.json"
    ).exists()
