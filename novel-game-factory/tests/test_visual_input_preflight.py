from common.visual_input_preflight import _validate_cg_contracts


def test_gpt_cg_preflight_accepts_more_than_two_character_references():
    errors = []
    requirements = [
        {
            "role": "scene",
            "required": True,
            "entity_id": "scene_1",
            "file_candidates": ["images/scenes/scene_1.png"],
        }
    ]
    for index in range(5):
        requirements.append(
            {
                "role": "character",
                "required": True,
                "entity_id": f"Char{index}",
                "entity_name": f"人物{index}",
                "file_candidates": [f"images/characters/Char{index}.png"],
                "identity_prompt": "identity",
            }
        )

    count, needs_character = _validate_cg_contracts(
        {
            "cg_1": {
                "cg_prompt": "five people",
                "reference_contract_schema": "cg-reference-v1",
                "reference_requirements": requirements,
            }
        },
        errors,
        image_model="gpt-image-2",
    )

    assert count == 1
    assert needs_character is True
    assert errors == []


def test_qwen_cg_preflight_blocks_required_references_over_model_limit():
    errors = []
    _validate_cg_contracts(
        {
            "cg_1": {
                "cg_prompt": "two people",
                "reference_contract_schema": "cg-reference-v1",
                "reference_requirements": [
                    {
                        "role": "scene",
                        "required": True,
                        "entity_id": "scene_1",
                        "file_candidates": ["images/scenes/scene_1.png"],
                    },
                    *[
                        {
                            "role": "character",
                            "required": True,
                            "entity_id": f"Char{index}",
                            "entity_name": f"人物{index}",
                            "file_candidates": [f"images/characters/Char{index}.png"],
                            "identity_prompt": "identity",
                        }
                        for index in range(2)
                    ],
                ],
            }
        },
        errors,
        image_model="qwen-image-plus",
    )

    assert any("不会静默丢弃 required 人物" in error for error in errors)
