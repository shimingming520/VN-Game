import unittest

from common.character_personality_contract import (
    CHARACTER_PROMPT_CONTRACT_VERSION,
    build_character_prompt_record,
    normalize_personality,
    select_personality_variant,
)


class CharacterPromptPersonalityContractTest(unittest.TestCase):
    def test_full_personality_variant_is_preserved_in_prompt_record(self):
        personality = {
            "personality_id": "2",
            "main_traits": ["冷静", "克制", "冷静"],
            "secondary_traits": ["眉宇间有淡淡忧郁"],
            "virtues": ["体贴"],
            "flaws": ["自负"],
        }
        record = build_character_prompt_record(
            name="柳拂衣",
            speaker_id="LiuFuYi",
            positive_prompt="base calm prompt",
            negative_prompt="negative prompt",
            height="178",
            personality=personality,
            combination="柳拂衣-2-1-1",
        )

        self.assertEqual(
            record["prompt_contract_version"],
            CHARACTER_PROMPT_CONTRACT_VERSION,
        )
        self.assertEqual(record["personality"]["personality_id"], 2)
        self.assertEqual(record["personality"]["main_traits"], ["冷静", "克制"])
        self.assertEqual(
            record["personality"]["secondary_traits"],
            ["眉宇间有淡淡忧郁"],
        )
        self.assertEqual(record["personality"]["virtues"], ["体贴"])
        self.assertEqual(record["personality"]["flaws"], ["自负"])

    def test_combination_personality_id_is_contract_source_of_truth(self):
        record = build_character_prompt_record(
            name="凌妙妙",
            speaker_id="LingMiaoMiao",
            positive_prompt="base calm prompt",
            negative_prompt="negative prompt",
            height="163",
            personality={"personality_id": 9, "main_traits": ["冷静"]},
            combination="凌妙妙-1-1-1",
        )

        self.assertEqual(record["personality"]["personality_id"], 1)

    def test_string_traits_are_normalized_and_deduplicated(self):
        personality = normalize_personality({
            "personality_id": "3",
            "main_traits": "冷静，克制，冷静",
            "secondary_traits": "警觉;理性",
        })

        self.assertEqual(personality["personality_id"], 3)
        self.assertEqual(personality["main_traits"], ["冷静", "克制"])
        self.assertEqual(personality["secondary_traits"], ["警觉", "理性"])

    def test_missing_exact_personality_does_not_fall_back_to_another_variant(self):
        personality = select_personality_variant(
            {
                "personality_variants": [
                    {"personality_id": 1, "main_traits": ["外向"]},
                ],
            },
            "凌妙妙-2-1-1",
        )

        self.assertEqual(personality["personality_id"], 2)
        self.assertEqual(personality["main_traits"], [])


if __name__ == "__main__":
    unittest.main()
