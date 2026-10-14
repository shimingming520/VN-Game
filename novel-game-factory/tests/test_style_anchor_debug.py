from experiments.style_anchor_ui import visual_debug
from experiments.style_anchor_ui.reuse_pipeline import (
    _expected_visual_asset_counts,
    _visual_progress,
)


LOCAL_FLAGS = (
    "STYLE_ANCHOR_REUSE_ENABLED",
    "LOCAL_PIPELINE_MODE",
    "OSS_DISABLED",
)


def test_generation_debug_record_round_trip_is_local_and_relative(monkeypatch, tmp_path):
    for name in LOCAL_FLAGS:
        monkeypatch.setenv(name, "1")
    monkeypatch.setattr(visual_debug, "OUTPUTS_PATH", str(tmp_path))

    task_root = tmp_path / "task-1"
    anchor = task_root / "style_anchors" / "character_anchor.png"
    output = task_root / "images" / "characters" / "XiaoLiu_1-1-1_calm_LS.png"
    anchor.parent.mkdir(parents=True)
    output.parent.mkdir(parents=True)
    anchor.write_bytes(b"anchor")
    output.write_bytes(b"image")

    target = visual_debug.record_visual_generation(
        "task-1",
        "characters",
        output,
        prompt="final provider prompt",
        generation_mode="image_to_image",
        reference_images=[{"path": anchor, "role": "人物风格 Anchor"}],
        logic=["content first", "anchor controls style"],
        model="gpt-image-2",
        provider="gpt",
    )

    assert target is not None and target.exists()
    stored = visual_debug.load_visual_generation_record(
        "task-1", "characters", output.name
    )
    assert stored["prompt"] == "final provider prompt"
    assert stored["reference_images"] == [
        {
            "name": "character_anchor.png",
            "role": "人物风格 Anchor",
            "relative_path": "style_anchors/character_anchor.png",
            "exists": True,
        }
    ]
    assert str(tmp_path) not in target.read_text(encoding="utf-8")


def test_generation_debug_is_disabled_without_all_local_guards(monkeypatch, tmp_path):
    for name in LOCAL_FLAGS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(visual_debug, "OUTPUTS_PATH", str(tmp_path))

    output = tmp_path / "task-1" / "images" / "cg" / "cg_1.png"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"image")

    assert visual_debug.record_visual_generation(
        "task-1",
        "cg",
        output,
        prompt="prompt",
        generation_mode="text_to_image",
    ) is None


def test_visual_progress_uses_expected_local_asset_counts():
    expected = _expected_visual_asset_counts(
        {
            "character_prompts": [
                {
                    "combination": "A-1-1-1",
                    "positive_prompt": "A",
                    "emotions": ["calm", "happy"],
                },
                {
                    "combination": "B-1-1-1",
                    "positive_prompt": "B",
                    "emotions": ["calm"],
                },
            ],
            "scene_prompts": {
                "scene_room": {"image_prompt": "room", "copy_count": 2}
            },
            "cg": {"cg_1": {"cg_prompt": "event"}},
            "backgrounds": {
                "generated_prompts": {"chapter_1": {"prompt": "cover"}},
                "novel_cover_metadata": {"prompt": "novel cover"},
            },
        }
    )

    assert expected == {
        "characters": 9,
        "scenes": 3,
        "cg": 1,
        "backgrounds": 2,
    }
    percentage, breakdown = _visual_progress(
        "processing",
        expected,
        {"characters": 3, "scenes": 1, "cg": 0, "backgrounds": 0},
    )
    assert percentage == 26
    assert breakdown["total"] == {"expected": 15, "generated": 4, "completed": 4}
    assert _visual_progress("completed", expected, {})[0] == 100
