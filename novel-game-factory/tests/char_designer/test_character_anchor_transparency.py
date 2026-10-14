from pathlib import Path

from common import style_anchor_generation
from common.visual_style import ASSET_CHARACTER, ASSET_SCENE


def test_character_anchor_prompt_requires_real_transparent_canvas():
    prompt = style_anchor_generation.build_anchor_prompt(
        ASSET_CHARACTER,
        "painterly fantasy",
    )

    assert "genuinely transparent canvas" in prompt
    assert "real alpha channel" in prompt
    assert "pure white background" not in prompt.lower()


def test_character_cleanup_prompt_preserves_alpha():
    prompt = style_anchor_generation.build_anchor_cleanup_prompt(
        ASSET_CHARACTER,
        "painterly fantasy",
    )

    assert "Preserve the real alpha channel" in prompt


def test_character_anchor_prompt_version_invalidates_old_opaque_anchor(monkeypatch):
    monkeypatch.setattr(
        style_anchor_generation,
        "_two_pass_anchor_enabled",
        lambda _model: True,
    )

    character_version = style_anchor_generation._anchor_prompt_version(
        ASSET_CHARACTER,
        "gpt-image-2",
    )
    scene_version = style_anchor_generation._anchor_prompt_version(
        ASSET_SCENE,
        "gpt-image-2",
    )

    assert character_version.endswith("native-rgba-v1")
    assert not scene_version.endswith("native-rgba-v1")


def test_normalize_generated_character_anchor_preserves_rgba(tmp_path):
    from PIL import Image

    source = tmp_path / "source.png"
    target = tmp_path / "target.png"
    Image.new("RGBA", (32, 48), (20, 30, 40, 0)).save(source, format="PNG")

    style_anchor_generation._normalize_image(
        source,
        target,
        preserve_transparency=True,
    )

    with Image.open(target) as image:
        assert image.mode == "RGBA"
