import unittest

from services.story_planner.core import ending_writer


class EndingLegacyTests(unittest.TestCase):
    def test_courage_and_self_worth_create_distinct_verifiable_legacies(self):
        courage = ending_writer.build_variable_legacy_requirement(
            "courage >= 70",
            {"characters": [{"name": "江渡", "role": "主角"}]},
        )
        self_worth = ending_writer.build_variable_legacy_requirement(
            "self_worth >= 70",
            {"characters": [{"name": "江渡", "role": "主角"}]},
        )

        self.assertIn("署名信", courage)
        self.assertIn("录音", courage)
        self.assertIn("主动", courage)
        self.assertIn("身世", self_worth)
        self.assertIn("校方证词", self_worth)
        self.assertNotEqual(courage, self_worth)

    def test_unknown_axis_still_requires_a_unique_material_legacy(self):
        requirement = ending_writer.build_variable_legacy_requirement(
            "trust >= 70",
            {"characters": [{"name": "主角", "role": "主角"}]},
        )

        self.assertIn("可验证", requirement)
        self.assertIn("遗产", requirement)

    def test_playfulness_requires_observable_behavior_instead_of_material_legacy(self):
        requirement = ending_writer.build_variable_legacy_requirement(
            "playfulness >= 64（玩心维度：用幽默打破拘谨）",
            {"characters": [{"name": "主角", "role": "主角"}]},
        )

        self.assertIn("玩笑", requirement)
        self.assertIn("可观察", requirement)
        self.assertNotIn("物质遗产", requirement)

    def test_ancient_routes_use_period_appropriate_evidence_examples(self):
        courage = ending_writer.build_variable_legacy_requirement(
            "courage >= 70",
            {"characters": [{"name": "小柳", "role": "主角"}]},
            ui_mode="Ancient",
        )
        self_worth = ending_writer.build_variable_legacy_requirement(
            "self_worth >= 70",
            {"characters": [{"name": "小柳", "role": "主角"}]},
            ui_mode="Ancient",
        )

        self.assertIn("血书", courage)
        self.assertIn("县衙", courage)
        self.assertNotIn("录音", courage)
        self.assertIn("契书", self_worth)
        self.assertIn("账本", self_worth)
        self.assertNotIn("校方", self_worth)
