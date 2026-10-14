import io

import pytest
from PIL import Image

from common.style_anchor_generation import (
    AnchorGenerationError,
    build_anchor_prompt,
    normalize_uploaded_anchor,
)
from common.visual_style import ASSET_CHARACTER, ASSET_SCENE


def _image_bytes(fmt="JPEG"):
    buffer = io.BytesIO()
    Image.new("RGB", (48, 32), "navy").save(buffer, format=fmt)
    return buffer.getvalue()


def test_anchor_prompts_keep_style_and_neutral_content_separate():
    character = build_anchor_prompt(ASSET_CHARACTER, "rough charcoal linework")
    scene = build_anchor_prompt(ASSET_SCENE, "soft gouache atmosphere")

    assert character.startswith("Requested visual style:\nrough charcoal linework")
    assert "anonymous adult human" in character
    assert "rank symbols" in character
    assert scene.startswith("Requested visual style:\nsoft gouache atmosphere")
    assert "uninhabited environment" in scene
    assert "No people" in scene
    assert "balanced neutral exposure" in scene
    assert "uniform gray wash" in scene


def test_anchor_prompt_requires_nonempty_style_for_generation():
    with pytest.raises(AnchorGenerationError, match="global style"):
        build_anchor_prompt(ASSET_CHARACTER, "  ")


def test_uploaded_anchor_is_normalized_to_png(tmp_path):
    target = tmp_path / "style_anchors" / "character_anchor.png"

    metadata = normalize_uploaded_anchor(_image_bytes(), target)

    assert target.exists()
    assert metadata["width"] == 48
    assert metadata["height"] == 32
    with Image.open(target) as image:
        assert image.format == "PNG"
        assert image.mode == "RGB"
