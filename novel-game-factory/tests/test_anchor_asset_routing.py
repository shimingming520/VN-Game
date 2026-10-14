from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from common.visual_style import (
    ASSET_CHARACTER,
    ASSET_SCENE,
    AssetStylePlan,
    STYLE_MODE_CUSTOM,
    default_visual_style_config,
    file_sha256,
    save_visual_style_config,
    style_anchor_path,
)
from services.char_designer.core import regenerate as char_regenerate
from services.scene_painter.core.cg_generator import CGProcessor
from services.scene_painter.core.scene_generator import ImageData, SceneProcessor


def _png_bytes() -> bytes:
    from io import BytesIO

    buffer = BytesIO()
    Image.new("RGB", (8, 8), (80, 60, 40)).save(buffer, format="PNG")
    return buffer.getvalue()


def _write_png(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_png_bytes())
    return path


class _FakeSceneGenerator:
    model = "gpt-image-2"
    supports_edit = False
    supports_anchor_edit = True

    def __init__(self):
        self.edit_calls = []
        self.text_calls = []

    def edit_image_with_retry(self, **kwargs):
        self.edit_calls.append(kwargs)
        return ImageData(data=_png_bytes())

    def retry_on_failure(self, **kwargs):
        self.text_calls.append(kwargs)
        return ImageData(data=_png_bytes())


def _scene_config(tmp_path):
    return SimpleNamespace(
        scene=SimpleNamespace(
            output_directory=str(tmp_path),
            width=1792,
            height=1008,
            num_inference_steps=30,
        ),
        api_max_retries=1,
        api_model="gpt-image-2",
    )


def test_primary_scene_uses_anchor_and_keeps_semantics_first(tmp_path):
    anchor = tmp_path / "scene_anchor.png"
    _write_png(anchor)
    plan = AssetStylePlan(
        task_id="task-1",
        asset_kind=ASSET_SCENE,
        mode=STYLE_MODE_CUSTOM,
        style="soft gouache, muted palette",
        anchor_path=anchor,
    )
    generator = _FakeSceneGenerator()
    processor = SceneProcessor(_scene_config(tmp_path), generator, plan)

    result = processor._process_single(
        "scene_poor_lane",
        {"image_prompt": "a narrow poor lane with patched wooden doors"},
    )

    assert result.success is True
    assert not generator.text_calls
    call = generator.edit_calls[0]
    assert call["base_image_path"] == anchor
    assert call["prompt"].index("patched wooden doors") < call["prompt"].index(
        "Optional user style notes"
    )


def test_secondary_scene_uses_parent_not_style_anchor(tmp_path):
    anchor = tmp_path / "scene_anchor.png"
    _write_png(anchor)
    parent = tmp_path / "scene_lane.png"
    _write_png(parent)
    plan = AssetStylePlan(
        task_id="task-1",
        asset_kind=ASSET_SCENE,
        mode=STYLE_MODE_CUSTOM,
        style="soft gouache",
        anchor_path=anchor,
    )
    generator = _FakeSceneGenerator()
    processor = SceneProcessor(_scene_config(tmp_path), generator, plan)

    result = processor._process_single(
        "scene_lane_1",
        {"image_prompt": "the same lane seen from inside a doorway"},
    )

    assert result.success is True
    call = generator.edit_calls[0]
    assert call["base_image_path"] == parent
    assert "same place identity and art style" in call["prompt"]
    assert "Do not copy its location" not in call["prompt"]


class _FakeCGGenerator:
    model = "gpt-image-2"

    def __init__(self):
        self.calls = []

    def generate_with_reference(self, prompt, reference_image, output_path):
        self.calls.append((prompt, reference_image, output_path))
        _write_png(output_path)
        return True

    def generate_with_references(self, prompt, reference_images, output_path):
        raise AssertionError("unexpected multi-reference call")

    def supports_text_only(self):
        return True

    def generate_text_only(self, prompt, output_path):
        raise AssertionError("custom mode must not silently fall back to text-to-image")


def test_cg_without_business_references_uses_scene_anchor(tmp_path):
    anchor = tmp_path / "scene_anchor.png"
    _write_png(anchor)
    plan = AssetStylePlan(
        task_id="task-1",
        asset_kind=ASSET_SCENE,
        mode=STYLE_MODE_CUSTOM,
        style="misty ink wash",
        anchor_path=anchor,
    )
    image_generator = _FakeCGGenerator()
    processor = CGProcessor(
        image_generator,
        tmp_path / "cg",
        tmp_path / "characters",
        tmp_path / "scenes",
        "task-1",
        plan,
    )
    processor.output_dir.mkdir()

    result = processor._process_single(
        "cg_1",
        {"cg_prompt": "a child holding a broken lantern in a poor courtyard"},
    )

    assert result.success is True
    prompt, reference, _ = image_generator.calls[0]
    assert reference == anchor
    assert prompt.index("broken lantern") < prompt.index("Optional user style notes")


def test_base_calm_regeneration_uses_character_anchor(monkeypatch, tmp_path):
    monkeypatch.setattr("common.visual_style.OUTPUTS_PATH", str(tmp_path))
    monkeypatch.setattr("common.visual_style.fetch_output_file", lambda *args, **kwargs: None)
    monkeypatch.setattr("common.visual_style.mirror_outputs", lambda *args, **kwargs: 0)
    task_id = "character-anchor-regen"
    config = default_visual_style_config()
    config["mode"] = STYLE_MODE_CUSTOM
    config[ASSET_CHARACTER].update(
        global_style="rough ink linework",
        source="uploaded",
        confirmed=True,
    )
    anchor = style_anchor_path(task_id, ASSET_CHARACTER, outputs_path=tmp_path)
    _write_png(anchor)
    config[ASSET_CHARACTER]["sha256"] = file_sha256(anchor)
    save_visual_style_config(task_id, config, outputs_path=tmp_path, mirror=False)

    captured = {}

    def fake_edit(base_image, prompt, final_path, options):
        captured.update(base_image=base_image, prompt=prompt, final_path=final_path)
        return True, ""

    monkeypatch.setattr(char_regenerate, "_generate_edit_image", fake_edit)
    monkeypatch.setattr(
        char_regenerate,
        "_generate_text_image",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("custom base calm must use image-to-image")
        ),
    )
    target = tmp_path / "calm.png"

    success, error = char_regenerate._generate_base_calm_image(
        task_id,
        "a poor child with a very large mouth wearing patched clothes",
        target,
        char_regenerate.ImageProviderOptions(image_provider="gpt"),
    )

    assert success is True
    assert error == ""
    assert captured["base_image"] == anchor
    assert captured["prompt"].index("very large mouth") < captured["prompt"].index(
        "Optional user style notes"
    )
