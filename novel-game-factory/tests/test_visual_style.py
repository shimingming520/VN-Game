import json
import io
from pathlib import Path

import pytest
from PIL import Image

from common import visual_style
from common.visual_style import (
    ANCHOR_STATUS_CONFIRMED,
    ANCHOR_STATUS_MISSING,
    ANCHOR_STATUS_READY,
    ASSET_CHARACTER,
    ASSET_SCENE,
    REMOVE_CHARACTER_BACKGROUND_KEY,
    STYLE_MODE_CUSTOM,
    VisualStyleConfigError,
    apply_asset_style,
    compose_anchor_guided_prompt,
    default_visual_style_config,
    file_sha256,
    load_visual_style_config,
    normalize_visual_style_config,
    persist_visual_style_bundle_for_run,
    resolve_asset_style_plan,
    save_visual_style_config,
    should_remove_character_background,
    style_anchor_path,
    validate_visual_style_for_run,
)


LLM_STYLE = "watercolor illustration, muted palette"


def test_llm_route_uses_automatic_anchor_with_existing_global_style(tmp_path):
    config = default_visual_style_config()
    anchor = style_anchor_path("task-1", ASSET_CHARACTER, outputs_path=tmp_path)
    anchor.parent.mkdir(parents=True, exist_ok=True)
    image_buffer = io.BytesIO()
    image = Image.new("RGBA", (8, 8), (20, 30, 40, 0))
    for x in range(2, 6):
        for y in range(2, 6):
            image.putpixel((x, y), (20, 30, 40, 255))
    image.save(image_buffer, format="PNG")
    anchor.write_bytes(image_buffer.getvalue())
    config[ASSET_CHARACTER].update(
        source="llm_generated",
        status=ANCHOR_STATUS_READY,
        requested_model="gpt-image-2",
        sha256=file_sha256(anchor),
    )
    save_visual_style_config("task-1", config, outputs_path=tmp_path, mirror=False)

    plan = resolve_asset_style_plan(
        "task-1",
        ASSET_CHARACTER,
        outputs_path=tmp_path,
        fetch_remote=False,
        global_style_loader=lambda _task_id: LLM_STYLE,
    )

    assert plan.mode == "llm"
    assert plan.anchor_path == anchor
    assert apply_asset_style(plan, "a full-body character") == (
        f"{LLM_STYLE}, a full-body character"
    )


def test_llm_mode_rejects_missing_automatic_anchor(tmp_path):
    with pytest.raises(VisualStyleConfigError, match="尚未生成自动 character Anchor"):
        resolve_asset_style_plan(
            "task-1",
            ASSET_CHARACTER,
            outputs_path=tmp_path,
            fetch_remote=False,
            global_style_loader=lambda _task_id: LLM_STYLE,
        )


def test_llm_mode_rejects_unparseable_automatic_anchor(tmp_path):
    config = default_visual_style_config()
    anchor = style_anchor_path("task-1", ASSET_CHARACTER, outputs_path=tmp_path)
    anchor.parent.mkdir(parents=True, exist_ok=True)
    anchor.write_bytes(b"not-an-image")
    config[ASSET_CHARACTER].update(
        source="llm_generated",
        status=ANCHOR_STATUS_READY,
        sha256=file_sha256(anchor),
    )
    save_visual_style_config("task-1", config, outputs_path=tmp_path, mirror=False)

    with pytest.raises(VisualStyleConfigError, match="不可解析"):
        resolve_asset_style_plan(
            "task-1",
            ASSET_CHARACTER,
            outputs_path=tmp_path,
            fetch_remote=False,
            global_style_loader=lambda _task_id: LLM_STYLE,
        )


def test_k8s_visual_route_has_fixed_automatic_compatibility_pin():
    project_root = Path(__file__).resolve().parents[1]
    for filename in ("configmap.yaml", "configmap-test.yaml"):
        content = (project_root / "k8s" / filename).read_text(encoding="utf-8")
        assert 'LLM_LAZY_STYLE_ANCHOR_ENABLED: "1"' in content
        assert 'LLM_LAZY_STYLE_ANCHOR_ENABLED: "0"' not in content


def test_custom_mode_resolves_confirmed_asset_specific_anchors(tmp_path):
    config = default_visual_style_config()
    config["mode"] = STYLE_MODE_CUSTOM
    config[ASSET_CHARACTER].update(
        global_style="flat cel shading, restrained colors",
        source="uploaded",
        confirmed=True,
        sha256="character-sha",
    )
    config[ASSET_SCENE].update(
        global_style="painterly backgrounds, misty light",
        source="generated",
        confirmed=True,
        sha256="scene-sha",
    )
    for kind in (ASSET_CHARACTER, ASSET_SCENE):
        anchor = style_anchor_path("task-1", kind, outputs_path=tmp_path)
        anchor.parent.mkdir(parents=True, exist_ok=True)
        anchor.write_bytes(f"{kind}-image".encode())
        config[kind]["sha256"] = file_sha256(anchor)
    save_visual_style_config("task-1", config, outputs_path=tmp_path, mirror=False)

    character = resolve_asset_style_plan(
        "task-1", ASSET_CHARACTER, outputs_path=tmp_path, fetch_remote=False
    )
    scene = resolve_asset_style_plan(
        "task-1", ASSET_SCENE, outputs_path=tmp_path, fetch_remote=False
    )

    assert character.mode == STYLE_MODE_CUSTOM
    assert character.anchor_path.name == "character_anchor.png"
    assert character.style == "flat cel shading, restrained colors"
    assert scene.anchor_path.name == "scene_anchor.png"
    assert scene.style == "painterly backgrounds, misty light"


def test_custom_mode_is_fail_closed_when_anchor_unconfirmed(tmp_path):
    config = default_visual_style_config()
    config["mode"] = STYLE_MODE_CUSTOM
    config[ASSET_CHARACTER]["global_style"] = "ink linework"
    save_visual_style_config("task-1", config, outputs_path=tmp_path, mirror=False)

    with pytest.raises(VisualStyleConfigError, match="尚未确认"):
        resolve_asset_style_plan(
            "task-1", ASSET_CHARACTER, outputs_path=tmp_path, fetch_remote=False
        )


def test_uploaded_anchor_may_use_no_text_style(tmp_path):
    config = default_visual_style_config()
    config["mode"] = STYLE_MODE_CUSTOM
    config[ASSET_CHARACTER].update(source="uploaded", confirmed=True)
    anchor = style_anchor_path("task-1", ASSET_CHARACTER, outputs_path=tmp_path)
    anchor.parent.mkdir(parents=True, exist_ok=True)
    anchor.write_bytes(b"image")
    config[ASSET_CHARACTER]["sha256"] = file_sha256(anchor)
    save_visual_style_config("task-1", config, outputs_path=tmp_path, mirror=False)

    plan = resolve_asset_style_plan(
        "task-1", ASSET_CHARACTER, outputs_path=tmp_path, fetch_remote=False
    )

    assert plan.style == ""
    assert apply_asset_style(plan, "a child with a very large mouth") == (
        "a child with a very large mouth"
    )


def test_anchor_prompt_places_semantics_before_style_and_rejects_content_copy(tmp_path):
    style = "soft gouache, muted mineral pigments"
    config = default_visual_style_config()
    config["mode"] = STYLE_MODE_CUSTOM
    config[ASSET_CHARACTER].update(
        global_style=style,
        source="generated",
        confirmed=True,
    )
    anchor = style_anchor_path("task-1", ASSET_CHARACTER, outputs_path=tmp_path)
    anchor.parent.mkdir(parents=True, exist_ok=True)
    anchor.write_bytes(b"image")
    config[ASSET_CHARACTER]["sha256"] = file_sha256(anchor)
    save_visual_style_config("task-1", config, outputs_path=tmp_path, mirror=False)
    plan = resolve_asset_style_plan(
        "task-1", ASSET_CHARACTER, outputs_path=tmp_path, fetch_remote=False
    )

    prompt = compose_anchor_guided_prompt(
        plan,
        f"{style}. A poor child with a very large mouth wearing plain patched clothing.",
    )

    assert prompt.index("very large mouth") < prompt.index("Optional user style notes")
    assert prompt.count(style) == 1
    assert "Do not copy its identity" in prompt
    assert "clothing" in prompt


def test_config_is_saved_as_normalized_json(tmp_path):
    config = default_visual_style_config()
    config["mode"] = "CUSTOM"
    target = save_visual_style_config(
        "task-1", config, outputs_path=tmp_path, mirror=False
    )

    stored = json.loads(target.read_text(encoding="utf-8"))
    loaded = load_visual_style_config(
        "task-1", outputs_path=tmp_path, fetch_remote=False
    )
    assert stored["mode"] == STYLE_MODE_CUSTOM
    assert loaded == stored


def test_custom_mode_rejects_anchor_changed_after_confirmation(tmp_path):
    config = default_visual_style_config()
    config["mode"] = STYLE_MODE_CUSTOM
    config[ASSET_CHARACTER].update(source="uploaded", confirmed=True)
    anchor = style_anchor_path("task-1", ASSET_CHARACTER, outputs_path=tmp_path)
    anchor.parent.mkdir(parents=True, exist_ok=True)
    anchor.write_bytes(b"confirmed-image")
    config[ASSET_CHARACTER]["sha256"] = file_sha256(anchor)
    save_visual_style_config("task-1", config, outputs_path=tmp_path, mirror=False)

    anchor.write_bytes(b"changed-image")

    with pytest.raises(VisualStyleConfigError, match="确认版本不一致"):
        resolve_asset_style_plan(
            "task-1", ASSET_CHARACTER, outputs_path=tmp_path, fetch_remote=False
        )


def test_character_background_removal_switch_is_backward_compatible(tmp_path):
    assert should_remove_character_background(
        "task-1", outputs_path=tmp_path, fetch_remote=False
    ) is True

    config = default_visual_style_config()
    config[REMOVE_CHARACTER_BACKGROUND_KEY] = False
    save_visual_style_config("task-1", config, outputs_path=tmp_path, mirror=False)

    assert should_remove_character_background(
        "task-1", outputs_path=tmp_path, fetch_remote=False
    ) is False


def test_anchor_status_is_derived_from_persisted_confirmation_state():
    confirmed = default_visual_style_config()
    confirmed[ASSET_CHARACTER].update(
        source="uploaded",
        sha256="abc",
        confirmed=True,
        status=ANCHOR_STATUS_MISSING,
    )
    assert normalize_visual_style_config(confirmed)[ASSET_CHARACTER][
        "status"
    ] == ANCHOR_STATUS_CONFIRMED

    ready = default_visual_style_config()
    ready[ASSET_CHARACTER].update(source="uploaded", sha256="abc")
    assert normalize_visual_style_config(ready)[ASSET_CHARACTER][
        "status"
    ] == ANCHOR_STATUS_READY


def test_llm_run_preflight_does_not_require_story_planner_output(monkeypatch, tmp_path):
    monkeypatch.setattr(
        visual_style,
        "get_global_style",
        lambda _task_id: (_ for _ in ()).throw(
            AssertionError("story planner global_style does not exist before run")
        ),
    )

    config = validate_visual_style_for_run(
        "task-1", outputs_path=tmp_path, fetch_remote=False
    )

    assert config["mode"] == "llm"


def test_custom_bundle_uploads_anchors_before_config_commit_marker(monkeypatch, tmp_path):
    monkeypatch.setattr(visual_style, "OUTPUTS_PATH", str(tmp_path))
    monkeypatch.setattr(visual_style, "oss_disabled", lambda: False)

    task_id = "task-1"
    config = default_visual_style_config()
    config["mode"] = STYLE_MODE_CUSTOM
    for kind in (ASSET_CHARACTER, ASSET_SCENE):
        anchor = style_anchor_path(task_id, kind)
        anchor.parent.mkdir(parents=True, exist_ok=True)
        anchor.write_bytes(f"{kind}-anchor".encode())
        config[kind].update(
            source="uploaded",
            confirmed=True,
            sha256=file_sha256(anchor),
        )

    remote = {}
    writes = []

    def fake_write(key, content, content_type=None):
        writes.append((key, content_type))
        remote[key] = bytes(content)
        return True

    monkeypatch.setattr(visual_style, "oss_write", fake_write)
    monkeypatch.setattr(visual_style, "oss_read", lambda key: remote.get(key))

    persist_visual_style_bundle_for_run(task_id, config, strict_remote=True)

    assert [key for key, _ in writes] == [
        f"storage/outputs/{task_id}/style_anchors/character_anchor.png",
        f"storage/outputs/{task_id}/style_anchors/scene_anchor.png",
        f"storage/outputs/{task_id}/style_anchors/config.json",
    ]
