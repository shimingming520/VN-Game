from pathlib import Path

import pytest
from PIL import Image

from services.char_designer.utils import native_transparency


def _save_rgba(path: Path, *, alpha: int = 255, transparent_canvas: bool = False):
    image = Image.new("RGBA", (100, 120), (80, 100, 120, alpha))
    if transparent_canvas:
        image = Image.new("RGBA", (100, 120), (0, 0, 0, 0))
        for x in range(25, 75):
            for y in range(15, 110):
                image.putpixel((x, y), (80, 100, 120, 220))
    image.save(path, format="PNG")


def test_transparent_prompt_removes_white_background_conflict_idempotently():
    original = (
        "The background is completely pure white, pure white background, "
        "and the main subject is one full-body character, clear silhouette, "
        "plain pure white background, generous margin."
    )

    prepared = native_transparency.prepare_transparent_character_prompt(original)

    assert "pure white background" not in prepared.lower()
    assert "genuinely transparent canvas" in prepared
    assert native_transparency.prepare_transparent_character_prompt(prepared) == prepared


def test_gpt_image_2_policy_forces_transparent_png(monkeypatch):
    monkeypatch.setattr(
        native_transparency,
        "should_remove_character_background",
        lambda _task_id: True,
    )

    policy = native_transparency.resolve_character_output_policy(
        "task",
        image_provider="gpt",
        model="gpt-image-2",
        requested_background="opaque",
        requested_output_format="jpeg",
    )

    assert policy.native_transparency is True
    assert policy.background == "transparent"
    assert policy.output_format == "png"


def test_gpt_image_2_ignores_legacy_opaque_debug_flag(monkeypatch):
    monkeypatch.setattr(
        native_transparency,
        "should_remove_character_background",
        lambda _task_id: False,
    )

    policy = native_transparency.resolve_character_output_policy(
        "task",
        image_provider="gpt",
        model="gpt-image-2",
        requested_background="opaque",
        requested_output_format="jpeg",
    )

    assert policy.require_transparency is True
    assert policy.native_transparency is True
    assert policy.background == "transparent"
    assert policy.output_format == "png"


def test_non_gpt_provider_cannot_silently_publish_required_transparency(monkeypatch):
    monkeypatch.setattr(
        native_transparency,
        "should_remove_character_background",
        lambda _task_id: True,
    )

    with pytest.raises(native_transparency.CharacterTransparencyError):
        native_transparency.resolve_character_output_policy(
            "task",
            image_provider="qwen",
            model="qwen-image-plus",
        )


def test_explicit_debug_mode_keeps_opaque_provider_route(monkeypatch):
    monkeypatch.setattr(
        native_transparency,
        "should_remove_character_background",
        lambda _task_id: False,
    )

    policy = native_transparency.resolve_character_output_policy(
        "task",
        image_provider="qwen",
        model="qwen-image-plus",
    )

    assert policy.require_transparency is False
    assert policy.mode == native_transparency.OPAQUE_DEBUG_MODE


def test_native_transparency_validator_requires_real_clear_canvas(tmp_path):
    valid = tmp_path / "valid.png"
    opaque_rgba = tmp_path / "opaque-rgba.png"
    rgb = tmp_path / "rgb.png"
    _save_rgba(valid, transparent_canvas=True)
    _save_rgba(opaque_rgba)
    Image.new("RGB", (100, 120), (255, 255, 255)).save(rgb, format="PNG")

    assert native_transparency.validate_native_transparent_character_file(valid)
    assert not native_transparency.validate_native_transparent_character_file(
        opaque_rgba
    )
    assert not native_transparency.validate_native_transparent_character_file(rgb)

    metrics = native_transparency.inspect_native_transparency(valid)
    assert metrics["format"] == "PNG"
    assert metrics["mode"] == "RGBA"
    assert metrics["alpha_min"] == 0
    assert metrics["alpha_max"] == 220
    assert metrics["fully_transparent_ratio"] > 0.5
    assert metrics["corner_alpha"] == [0, 0, 0, 0]
