import importlib.util
from pathlib import Path
import unittest


def _load_module():
    path = (
        Path(__file__).resolve().parents[2]
        / "services"
        / "story_planner"
        / "core"
        / "effects_validator.py"
    )
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location("effects_validator_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


effects = _load_module()


class EffectCapTests(unittest.TestCase):
    def test_caps_are_dynamic_and_bounded(self):
        self.assertIsNotNone(effects, "effects_validator 尚未实现")
        variable_defs = [
            {"key": "courage", "init": 50, "threshold": 65},
            {"key": "trust", "init": 50, "threshold": 95},
        ]

        caps = effects.calculate_effect_caps(variable_defs, total_branches=10)

        self.assertGreaterEqual(caps["courage"], 3)
        self.assertLessEqual(caps["trust"], 15)
        self.assertGreaterEqual(caps["trust"], caps["courage"])

    def test_unreachable_threshold_is_reported(self):
        variable_defs = [{"key": "trust", "init": 50, "threshold": 100}]

        ok, issues = effects.validate_threshold_reachability(
            variable_defs,
            total_branches=2,
            caps={"trust": 15},
        )

        self.assertFalse(ok)
        self.assertTrue(any("不可达" in issue for issue in issues))

    def test_calculated_cap_never_rounds_a_reachable_threshold_down(self):
        variable_defs = [{"key": "affection", "init": 50, "threshold": 66}]

        caps = effects.calculate_effect_caps(variable_defs, total_branches=6)
        ok, issues = effects.validate_threshold_reachability(
            variable_defs,
            total_branches=6,
            caps=caps,
        )

        self.assertEqual(caps["affection"], 5)
        self.assertTrue(ok, issues)

    def test_effect_key_schedule_covers_every_route_axis(self):
        variable_defs = [
            {"key": "affection_a"},
            {"key": "affection_b"},
            {"key": "affection_c"},
        ]

        schedule = effects.build_effect_key_schedule(variable_defs, total_branches=7)

        self.assertEqual(7, len(schedule))
        self.assertEqual(
            {"affection_a", "affection_b", "affection_c"},
            set(schedule),
        )
        counts = [schedule.count(item["key"]) for item in variable_defs]
        self.assertLessEqual(max(counts) - min(counts), 1)

    def test_actual_reachability_uses_saved_effects_not_theoretical_caps(self):
        variable_defs = [
            {"key": "affection_a", "init": 50, "threshold": 55},
            {"key": "affection_b", "init": 50, "threshold": 54},
        ]
        effect_pairs = [
            ({"affection_a": 3}, {"affection_a": -3}),
            ({"affection_a": 3}, {"affection_a": -3}),
        ]

        reachability = effects.calculate_actual_effect_reachability(
            variable_defs,
            effect_pairs,
        )

        self.assertTrue(reachability["affection_a"]["reachable"])
        self.assertEqual(56, reachability["affection_a"]["max_value"])
        self.assertFalse(reachability["affection_b"]["reachable"])
        self.assertEqual(0, reachability["affection_b"]["positive_choices"])

    def test_actual_reachability_clamps_runtime_value_to_one_hundred(self):
        variable_defs = [{"key": "courage", "init": 50, "threshold": 85}]
        effect_pairs = [
            ({"courage": 8}, {"courage": -8})
            for _ in range(20)
        ]

        reachability = effects.calculate_actual_effect_reachability(
            variable_defs,
            effect_pairs,
        )

        self.assertEqual(100, reachability["courage"]["max_value"])

    def test_effect_pairs_are_extracted_from_saved_markdown(self):
        markdown = (
            "正文\n<!-- effects_a={'courage': 3} effects_b={'courage': -3} -->\n"
            "后文\n<!-- effects_a={'trust': -2} effects_b={'trust': 2} -->"
        )

        pairs, issues = effects.extract_effect_pairs_from_markdown(markdown)

        self.assertEqual(
            [
                ({"courage": 3}, {"courage": -3}),
                ({"trust": -2}, {"trust": 2}),
            ],
            pairs,
        )
        self.assertEqual([], issues)

    def test_observed_effects_preserve_generation_cap_during_resume(self):
        merged = effects.merge_effect_caps_with_observed(
            {"affection_a": 3, "affection_b": 4},
            [
                ({"affection_a": 4}, {"affection_a": -4}),
                ({"affection_b": 2}, {"affection_b": -2}),
            ],
        )

        self.assertEqual({"affection_a": 4, "affection_b": 4}, merged)

    def test_threshold_calibration_never_unlocks_an_uncovered_axis_for_free(self):
        variable_defs = [
            {"key": "affection_a", "init": 50, "threshold": 65},
            {"key": "affection_b", "init": 50, "threshold": 65},
        ]
        topology = [
            {"node_id": "ending_a", "branch_variable": "affection_a", "branch_threshold": 65},
            {"node_id": "ending_b", "branch_variable": "affection_b", "branch_threshold": 65},
        ]
        effect_pairs = [
            ({"affection_a": 4}, {"affection_a": -4}),
            ({"affection_a": 4}, {"affection_a": -4}),
        ]

        issues = effects.calibrate_thresholds_to_actual_effects(
            variable_defs,
            topology,
            effect_pairs,
        )

        self.assertEqual(58, variable_defs[0]["threshold"])
        self.assertEqual(58, topology[0]["branch_threshold"])
        self.assertEqual(65, variable_defs[1]["threshold"])
        self.assertTrue(any("affection_b" in issue for issue in issues))

    def test_unified_ending_rules_keep_short_story_routes_reachable(self):
        variable_defs = [
            {"key": "courage", "init": 50, "threshold": 65},
            {"key": "self_worth", "init": 50, "threshold": 65},
        ]
        topology = [
            {"type": "ending", "branch_variable": "courage"},
            {"type": "ending", "branch_variable": "self_worth"},
        ]
        pairs = [({"courage": 2}, {"courage": -2}) for _ in range(7)]
        pairs.extend(
            [({"self_worth": 3}, {"self_worth": -3}) for _ in range(3)]
        )

        issues = effects.calibrate_ending_rules(variable_defs, topology, pairs)

        self.assertEqual([], [issue for issue in issues if "禁用" in issue])
        self.assertEqual(60, variable_defs[0]["threshold"])
        self.assertEqual(57, variable_defs[1]["threshold"])
        for variable in variable_defs:
            self.assertEqual(0.65, variable["ratio_threshold"])
            self.assertEqual(0.10, variable["margin_ratio"])
            self.assertEqual(3, variable["minimum_effective_choices"])
            self.assertTrue(variable["route_enabled"])
        self.assertEqual(60, topology[0]["branch_threshold"])
        self.assertEqual(57, topology[1]["branch_threshold"])

    def test_unified_ending_rules_make_long_story_threshold_scale_to_eighty_five(self):
        variable_defs = [{"key": "courage", "init": 50, "threshold": 65}]
        topology = [{"type": "ending", "branch_variable": "courage"}]
        pairs = [({"courage": 3}, {"courage": -3}) for _ in range(20)]

        effects.calibrate_ending_rules(variable_defs, topology, pairs)

        self.assertEqual(85, variable_defs[0]["threshold"])
        self.assertEqual(100, variable_defs[0]["max_reachable_value"])

    def test_route_is_disabled_when_effective_choice_coverage_is_too_low(self):
        variable_defs = [{"key": "trust", "init": 50, "threshold": 60}]
        topology = [{"type": "ending", "branch_variable": "trust"}]
        pairs = [({"trust": 4}, {"trust": -4}) for _ in range(2)]

        issues = effects.calibrate_ending_rules(variable_defs, topology, pairs)

        self.assertFalse(variable_defs[0]["route_enabled"])
        self.assertFalse(topology[0]["route_enabled"])
        self.assertTrue(any("有效选择不足" in issue for issue in issues))

    def test_real_path_simulation_detects_jointly_unreachable_route(self):
        variable_defs = [
            {
                "key": "courage",
                "init": 50,
                "threshold": 58,
                "ratio_threshold": 0.65,
                "margin_ratio": 0.10,
                "minimum_effective_choices": 3,
                "route_enabled": True,
            },
            {"key": "trust", "init": 50, "threshold": 58},
        ]
        pairs = [
            ({"courage": 3, "trust": 3}, {"courage": -3, "trust": -3})
            for _ in range(4)
        ]

        simulation = effects.simulate_ending_route_reachability(
            variable_defs,
            pairs,
        )

        self.assertFalse(simulation["courage"]["reachable"])
        self.assertEqual("normalized_route_rules", simulation["courage"]["reason"])


class EffectPairSanitizationTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(effects, "effects_validator 尚未实现")
        self.variable_defs = [
            {"key": "courage", "init": 50, "threshold": 65},
            {"key": "trust", "init": 50, "threshold": 60},
        ]
        self.caps = {"courage": 6, "trust": 4}

    def test_filters_unknown_keys_converts_numbers_and_clamps(self):
        effect_a, effect_b, issues = effects.sanitize_effect_pair(
            {"courage": "99", "invented": 10},
            {"courage": "-7", "invented": -10},
            self.variable_defs,
            self.caps,
        )

        self.assertEqual(effect_a, {"courage": 6})
        self.assertEqual(effect_b, {"courage": -6})
        self.assertTrue(any("非法 key" in issue for issue in issues))

    def test_fake_identical_choices_fall_back_to_opposite_pair(self):
        effect_a, effect_b, issues = effects.sanitize_effect_pair(
            {"trust": 4},
            {"trust": 4},
            self.variable_defs,
            self.caps,
        )

        self.assertEqual(effect_a, {"courage": 6})
        self.assertEqual(effect_b, {"courage": -6})
        self.assertTrue(any("假选择" in issue for issue in issues))

    def test_zero_or_non_numeric_values_fall_back(self):
        effect_a, effect_b, _issues = effects.sanitize_effect_pair(
            {"courage": 0},
            {"courage": "not-a-number"},
            self.variable_defs,
            self.caps,
        )

        self.assertEqual(effect_a, {"courage": 6})
        self.assertEqual(effect_b, {"courage": -6})

    def test_different_dominant_variables_fall_back_to_one_axis(self):
        effect_a, effect_b, issues = effects.sanitize_effect_pair(
            {"courage": 6, "trust": 1},
            {"trust": -4, "courage": -1},
            self.variable_defs,
            self.caps,
        )

        self.assertEqual(effect_a, {"courage": 6})
        self.assertEqual(effect_b, {"courage": -6})
        self.assertTrue(any("主导变量" in issue for issue in issues))

    def test_same_dominant_variable_must_have_opposite_directions(self):
        effect_a, effect_b, issues = effects.sanitize_effect_pair(
            {"trust": 4},
            {"trust": 3},
            self.variable_defs,
            self.caps,
            fallback_key="trust",
        )

        self.assertEqual(effect_a, {"trust": 4})
        self.assertEqual(effect_b, {"trust": -4})
        self.assertTrue(any("相反方向" in issue for issue in issues))

    def test_valid_secondary_effects_are_preserved(self):
        effect_a, effect_b, issues = effects.sanitize_effect_pair(
            {"courage": 5, "trust": -2},
            {"courage": -6, "trust": 2},
            self.variable_defs,
            self.caps,
        )

        self.assertEqual(effect_a, {"courage": 5, "trust": -2})
        self.assertEqual(effect_b, {"courage": -6, "trust": 2})
        self.assertEqual(issues, [])

    def test_romance_contract_accepts_two_different_affection_axes(self):
        effect_a, effect_b, issues = effects.sanitize_romance_effect_pair(
            {"affection_a": 4, "affection_b": -3},
            {"affection_a": -4, "affection_b": 3},
            [
                {"key": "affection_a"},
                {"key": "affection_b"},
            ],
            {"affection_a": 6, "affection_b": 6},
        )

        self.assertEqual({"affection_a": 4, "affection_b": -3}, effect_a)
        self.assertEqual({"affection_a": -4, "affection_b": 3}, effect_b)
        self.assertEqual([], issues)

    def test_required_positive_axis_is_added_as_a_semantic_secondary_effect(self):
        effect_a, effect_b, issues = effects.sanitize_effect_pair(
            {"courage": 6},
            {"courage": -6},
            self.variable_defs,
            self.caps,
            required_positive_key="trust",
        )

        self.assertEqual(effect_a, {"courage": 6, "trust": -4})
        self.assertEqual(effect_b, {"courage": -6, "trust": 4})
        self.assertTrue(any("目标变量" in issue for issue in issues))


if __name__ == "__main__":
    unittest.main()
