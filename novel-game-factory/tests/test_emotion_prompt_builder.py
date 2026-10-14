import unittest

from services.char_designer.core.emotion_prompt_builder import (
    EMOTIONS,
    EMOTION_PROMPT_CONTRACT_VERSION,
    build_emotion_prompt,
    select_personality_variant,
)


class EmotionPromptBuilderTest(unittest.TestCase):
    def test_selects_personality_from_combination_id(self):
        character_data = [{
            "personality_variants": [
                {
                    "personality_id": 1,
                    "main_traits": ["外向", "直率"],
                    "secondary_traits": [],
                    "virtues": [],
                    "flaws": [],
                },
                {
                    "personality_id": 2,
                    "main_traits": ["冷静", "克制"],
                    "secondary_traits": ["眉宇间有淡淡忧郁"],
                    "virtues": ["体贴"],
                    "flaws": ["自负"],
                },
            ]
        }]

        personality = select_personality_variant(character_data, "柳拂衣-2-1-1")

        self.assertEqual(personality["personality_id"], 2)
        self.assertEqual(personality["main_traits"], ["冷静", "克制"])
        self.assertEqual(personality["secondary_traits"], ["眉宇间有淡淡忧郁"])

    def test_happy_prompt_uses_personality_and_allows_restrained_body_language(self):
        prompt = build_emotion_prompt(
            "happy",
            {
                "personality_id": 1,
                "main_traits": ["温润和蔼", "冷静", "沉稳"],
                "secondary_traits": ["眉宇间有淡淡忧郁"],
                "virtues": ["体贴"],
                "flaws": ["自负"],
            },
        )

        self.assertIn("Target emotion: happy", prompt)
        self.assertIn("温润和蔼", prompt)
        self.assertIn("眉宇间有淡淡忧郁", prompt)
        self.assertIn("personality-consistent micro-expressions and body language", prompt)
        self.assertIn("whether a gesture appears at all", prompt)
        self.assertIn("Body-language changes are optional", prompt)
        self.assertIn("at most one coherent gesture", prompt)
        self.assertIn("hips, legs, feet, ground anchor", prompt)
        self.assertIn("full-body scale, overall x/y placement", prompt)
        self.assertIn("Do not add, remove, transfer, multiply, or redesign accessories", prompt)
        self.assertIn("papers, talismans", prompt)
        self.assertIn("manga reaction symbols", prompt)
        self.assertNotIn("body proportions, pose, hand placement, limb positions", prompt)
        self.assertNotIn("cheerful and bright smile", prompt)
        self.assertNotIn("all other gestures are allowed", prompt)

    def test_all_supported_emotions_share_the_same_bounded_body_language_contract(self):
        for emotion in EMOTIONS:
            with self.subTest(emotion=emotion):
                prompt = build_emotion_prompt(emotion)
                self.assertIn(f"Target emotion: {emotion}", prompt)
                self.assertIn("one subtle and natural upper-body gesture", prompt)
                self.assertIn("Body-language changes are optional", prompt)
                self.assertIn("ground anchor", prompt)
                self.assertIn("overall x/y placement", prompt)
                self.assertIn("Do not add, remove, transfer", prompt)
                self.assertIn("do not crop or zoom", prompt)
                self.assertIn("Do not add text", prompt)
                self.assertNotIn("You may also adjust the body language", prompt)
                self.assertNotIn("thinking_chin", prompt)
                self.assertNotIn("pose_id", prompt)

    def test_intensity_and_hint_remain_available_without_a_pose_taxonomy(self):
        prompt = build_emotion_prompt(
            "angry",
            {"main_traits": ["冷静", "沉稳"]},
            intensity="low and restrained",
            expression_hint="Show controlled displeasure rather than rage.",
        )

        self.assertIn("Requested expression intensity: low and restrained", prompt)
        self.assertIn(
            "Additional expression direction: Show controlled displeasure rather than rage.",
            prompt,
        )
        self.assertNotIn("pose_name", prompt)

    def test_contract_version_marks_body_language_semantics(self):
        self.assertEqual(
            EMOTION_PROMPT_CONTRACT_VERSION,
            "personality-expression-body-language-v2",
        )


if __name__ == "__main__":
    unittest.main()
