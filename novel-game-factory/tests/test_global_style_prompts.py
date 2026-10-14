from services.info_extractor.cg.cg_prompt import _compose_cg_prompt
from services.info_extractor.character.char_prompt_builder import (
    CharacterInfo,
    generate_prompt,
)
from services.info_extractor.scene.cover_extractor import (
    _build_chapter_prompt,
    _build_novel_cover_prompt,
)
from services.scene_painter.core.scene_generator import compose_img2img_prompt


STYLE = "ink-wash and opaque mineral pigment, restrained jade palette, soft lantern light"


def _character():
    return CharacterInfo(
        name="测试角色",
        speaker_id="TestCharacter",
        gender="female",
        age=24,
        age_category="young adult",
        race="human",
        occupation="researcher",
        appearance={"hair_color": "black", "eye_color": "brown"},
        clothing={"main_outfit": "a practical long coat"},
        expression={},
        personality={},
    )


def _assert_style_is_single_prefix(prompt):
    assert prompt.startswith(STYLE)
    assert prompt.count(STYLE) == 1


def test_character_prompt_uses_global_style_without_legacy_style_mapping():
    prompt = generate_prompt(_character(), STYLE)

    assert prompt.startswith(STYLE)
    for legacy in (
        "Japanese 2D Anime",
        "Ancient Chinese style clothing",
        "Cyberpunk style clothing",
        "European style clothing",
        "Modern realistic clothing",
        "no photorealism",
        "must be anime style",
    ):
        assert legacy not in prompt


def test_character_prompt_allows_uploaded_anchor_without_text_style():
    prompt = generate_prompt(_character(), "")

    assert not prompt.startswith(".")
    assert "The background is completely pure white" in prompt


def test_scene_cg_and_cover_prompts_use_global_style():
    cg_prompt = _compose_cg_prompt(STYLE, "two rivals facing each other in rain")
    chapter_prompt, _ = _build_chapter_prompt(
        "chapter_1",
        {"depth": 1, "order": 1},
        STYLE,
        {
            "scene_setting": "an empty riverside pavilion",
            "atmosphere": "quiet tension",
            "key_objects": ["an extinguished lantern"],
            "colors": [],
            "visual_symbols": [],
            "lighting": "moonlight through clouds",
        },
    )
    novel_prompt, _ = _build_novel_cover_prompt(
        {
            "chapter_1": {
                "visual_elements": {
                    "scene_setting": "an empty riverside pavilion",
                    "atmosphere": "quiet tension",
                    "colors": [],
                }
            }
        },
        STYLE,
    )

    for prompt in (cg_prompt, chapter_prompt, novel_prompt):
        _assert_style_is_single_prefix(prompt)

    combined = "\n".join((cg_prompt, chapter_prompt, novel_prompt))
    for legacy in (
        "anime cartoon style",
        "2D animation style",
        "photorealistic",
        "Realistic fantasy style",
        "semi-realistic illustration",
    ):
        assert legacy not in combined


def test_scene_img2img_guidance_keeps_global_style_as_prefix():
    prompt = compose_img2img_prompt(f"{STYLE}, an empty riverside pavilion")

    _assert_style_is_single_prefix(prompt)
    assert "preceding text description" in prompt
