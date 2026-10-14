from pathlib import Path

import pytest

from common.visual_style import (
    ASSET_CHARACTER,
    AssetStylePlan,
    apply_asset_style,
    compose_anchor_guided_prompt,
)
from experiments.style_anchor_ui.reuse_pipeline import (
    ReusePipelineError,
    cleanse_backgrounds,
    cleanse_cg_prompts,
    cleanse_character_prompts,
    require_reuse_feature,
)


OLD_CHARACTER_STYLE = "Old character ink style, muted palette."
OLD_SCENE_STYLE = "Old scene gouache style, cold light."


def test_cleanse_character_prompts_removes_old_style_without_losing_content():
    data = [
        {
            "speaker_id": "XiaoLiu",
            "positive_prompt": (
                f"{OLD_CHARACTER_STYLE}, {OLD_CHARACTER_STYLE} "
                "The background is pure white. A poor child with a very large mouth."
            ),
        }
    ]

    cleaned, count = cleanse_character_prompts(data, [OLD_CHARACTER_STYLE])

    assert count == 1
    prompt = cleaned[0]["positive_prompt"]
    assert OLD_CHARACTER_STYLE.rstrip(".") not in prompt
    assert "poor child" in prompt
    assert "very large mouth" in prompt
    assert data[0]["positive_prompt"].startswith(OLD_CHARACTER_STYLE)


def test_cleanse_backgrounds_only_removes_known_style_prefixes():
    data = {
        "generated_prompts": {
            "chapter_1": {
                "prompt": f"{OLD_SCENE_STYLE}, oppressive fermentation courtyard"
            }
        },
        "novel_cover_metadata": {
            "prompt": f"{OLD_SCENE_STYLE} comprehensive novel cover"
        },
        "cover_metadata": {"theme": "keep semantic metadata"},
    }

    cleaned, count = cleanse_backgrounds(data, [OLD_SCENE_STYLE])

    assert count == 2
    assert cleaned["generated_prompts"]["chapter_1"]["prompt"].startswith(
        "oppressive fermentation courtyard"
    )
    assert cleaned["novel_cover_metadata"]["prompt"] == "comprehensive novel cover"
    assert cleaned["cover_metadata"] == data["cover_metadata"]


def test_cleanse_cg_prefers_init_description_and_resets_old_character_attachment():
    data = {
        "cg_1": {
            "cg_prompt": f"{OLD_SCENE_STYLE}, old rendered prompt",
            "init_description": "child looking back at a monster between sauce jars",
            "char_img_att": ["source_task_character_sheet.png"],
            "scene_img_att": ["scene_yard.png"],
        }
    }

    cleaned, count = cleanse_cg_prompts(data, [OLD_SCENE_STYLE])

    assert count == 1
    assert cleaned["cg_1"]["cg_prompt"] == data["cg_1"]["init_description"]
    assert cleaned["cg_1"]["char_img_att"] == []
    assert cleaned["cg_1"]["scene_img_att"] == ["scene_yard.png"]


def test_cleaned_prompt_receives_new_style_exactly_once_at_provider_boundary():
    cleaned, _ = cleanse_character_prompts(
        [{"positive_prompt": f"{OLD_CHARACTER_STYLE}, a poor child with a large mouth"}],
        [OLD_CHARACTER_STYLE],
    )
    new_style = "New rough woodblock style"
    plan = AssetStylePlan(
        task_id="derived",
        asset_kind=ASSET_CHARACTER,
        mode="custom",
        style=new_style,
        anchor_path=Path("/tmp/character_anchor.png"),
    )

    styled = apply_asset_style(plan, cleaned[0]["positive_prompt"])
    provider_prompt = compose_anchor_guided_prompt(plan, styled)

    assert provider_prompt.count(new_style) == 1
    assert OLD_CHARACTER_STYLE.rstrip(".") not in provider_prompt
    assert "poor child with a large mouth" in provider_prompt


def test_reuse_feature_requires_all_three_local_flags(monkeypatch):
    for name in (
        "STYLE_ANCHOR_REUSE_ENABLED",
        "LOCAL_PIPELINE_MODE",
        "OSS_DISABLED",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ReusePipelineError, match="STYLE_ANCHOR_REUSE_ENABLED"):
        require_reuse_feature()

    monkeypatch.setenv("STYLE_ANCHOR_REUSE_ENABLED", "1")
    monkeypatch.setenv("LOCAL_PIPELINE_MODE", "1")
    monkeypatch.setenv("OSS_DISABLED", "1")
    require_reuse_feature()
