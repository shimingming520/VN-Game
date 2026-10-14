"""Narrative point-of-view contract and drift regression tests.

These tests intentionally exercise the small, deterministic POV gate used by
generated branch prose.  Dialogue may use first-person speech regardless of
the narrator's POV; narration itself must follow the inferred contract.
"""

import unittest

from services.story_planner.core import novel_reader
from services.story_planner.core import branch_designer, ending_writer, soft_branch


class NarrativePerspectiveContractTests(unittest.TestCase):
    def test_infers_third_person_limited_contract_from_worldview(self):
        contract = novel_reader.infer_narrative_perspective_contract(
            {
                "style_notes": "全文采用第三人称限知叙述，跟随沈砚的所见所感。",
                "characters": [
                    {"name": "沈砚", "role": "主角"},
                    {"name": "顾宁", "role": "重要配角"},
                ],
            }
        )

        self.assertEqual("third_person_limited", contract["perspective"])
        self.assertEqual("沈砚", contract["protagonist_name"])
        self.assertTrue(contract.get("narrator_pronouns"))
        rule_text = str(contract.get("rule_text") or "")
        self.assertIn("保持", rule_text)
        self.assertIn("第三人称", rule_text)

    def test_first_person_narration_with_third_person_dialogue_note_stays_first_person(self):
        """A dialogue-person qualifier must not override the narrator POV."""
        contract = novel_reader.infer_narrative_perspective_contract(
            {
                "style_notes": "全文采用第一人称叙述，角色对白偶尔使用第三人称转述。",
                "characters": [{"name": "沈砚", "role": "主角"}],
            }
        )

        self.assertEqual("first_person", contract["perspective"])

    def test_detects_first_person_narration_drift_in_third_person_text(self):
        contract = {
            "perspective": "third_person_limited",
            "protagonist_name": "沈砚",
            "narrator_pronouns": ["他", "她"],
            "rule_text": "保持第三人称限知叙事，不得切换为第一人称。",
        }
        prose = (
            "沈砚推开旧门，雨水顺着屋檐落下。\n"
            "我不知道这场等待还要持续多久。\n"
            "他把信纸收进衣袋。"
        )

        issues = novel_reader.detect_narrative_pov_drift(prose, contract)

        self.assertTrue(issues, "第三人称正文中的第一人称叙述应被检测出来")

    def test_ignores_first_person_inside_dialogue_for_third_person_contract(self):
        contract = {
            "perspective": "third_person_limited",
            "protagonist_name": "沈砚",
            "narrator_pronouns": ["他", "她"],
            "rule_text": "保持第三人称限知叙事，不得切换为第一人称。",
        }
        prose = (
            "沈砚抬头看向顾宁。\n"
            "顾宁说：“我今晚会回来。”\n"
            "沈砚点了点头，把伞递给她。"
        )

        issues = novel_reader.detect_narrative_pov_drift(prose, contract)

        self.assertEqual([], issues)


class NarrativePerspectivePromptBlockTests(unittest.TestCase):
    """Every prose-producing module must expose the same POV guard block.

    The helpers are intentionally private: they are implementation seams for
    prompt composition, while the public generation APIs remain backwards
    compatible.  These tests are RED until each module wires the contract into
    its prompts.
    """

    THIRD_PERSON_CONTRACT = {
        "perspective": "third_person_limited",
        "protagonist_name": "沈砚",
        "narrator_pronouns": ["他", "她"],
        "allow_switch": False,
        "rule_text": "保持第三人称限知叙事，不得切换为第一人称。",
    }
    FIRST_PERSON_CONTRACT = {
        "perspective": "first_person",
        "protagonist_name": "沈砚",
        "narrator_pronouns": ["我", "我们"],
        "allow_switch": False,
        "rule_text": "保持主角沈砚的第一人称叙事，我只能指向主角。",
    }

    def test_soft_branch_prompt_block_preserves_third_person_narration(self):
        block = soft_branch._narrative_pov_prompt_block(self.THIRD_PERSON_CONTRACT)

        self.assertIn("third_person_limited", block)
        self.assertIn("沈砚", block)
        self.assertIn("第三人称", block)
        self.assertIn("不得", block)
        self.assertIn("我", block)

    def test_whatif_prompt_block_preserves_third_person_narration(self):
        block = branch_designer._narrative_pov_prompt_block(self.THIRD_PERSON_CONTRACT)

        self.assertIn("third_person_limited", block)
        self.assertIn("沈砚", block)
        self.assertIn("第三人称", block)
        self.assertIn("不得", block)

    def test_ending_prompt_block_preserves_third_person_narration(self):
        block = ending_writer._narrative_pov_prompt_block(self.THIRD_PERSON_CONTRACT)

        self.assertIn("third_person_limited", block)
        self.assertIn("沈砚", block)
        self.assertIn("第三人称", block)
        self.assertIn("不得", block)

    def test_prompt_blocks_keep_first_person_bound_to_protagonist(self):
        for module in (soft_branch, branch_designer, ending_writer):
            with self.subTest(module=module.__name__):
                block = module._narrative_pov_prompt_block(self.FIRST_PERSON_CONTRACT)
                self.assertIn("first_person", block)
                self.assertIn("沈砚", block)
                self.assertIn("第一人称", block)
                self.assertIn("只能", block)

    def test_prompt_blocks_are_empty_for_missing_contract(self):
        for module in (soft_branch, branch_designer, ending_writer):
            with self.subTest(module=module.__name__):
                self.assertEqual("", module._narrative_pov_prompt_block(None))

    def test_accepts_first_person_narration_when_contract_is_first_person(self):
        contract = {
            "perspective": "first_person",
            "protagonist_name": "沈砚",
            "narrator_pronouns": ["我", "我们"],
            "rule_text": "保持主角沈砚的第一人称叙事，不得改用第三人称描述主角。",
        }
        prose = "我推开旧门，雨水顺着屋檐落下。我把信纸收进衣袋。"

        issues = novel_reader.detect_narrative_pov_drift(prose, contract)

        self.assertEqual([], issues)

    def test_unknown_cached_contract_does_not_override_explicit_style(self):
        contract = novel_reader.infer_narrative_perspective_contract(
            {
                "narrative_pov_contract": {"perspective": "unknown"},
                "style_notes": "全文采用第三人称限知叙述，跟随沈砚的所见所感。",
                "characters": [{"name": "沈砚", "role": "主角"}],
            }
        )
        self.assertEqual("third_person_limited", contract["perspective"])

    def test_first_person_contract_ignores_supporting_character_third_person(self):
        contract = {
            "perspective": "first_person",
            "protagonist_name": "沈砚",
            "narrator_pronouns": ["我", "我们"],
        }
        prose = "我推开旧门。顾宁看见雨水落下。他转身关上窗。"
        self.assertEqual([], novel_reader.detect_narrative_pov_drift(prose, contract))

    def test_third_person_contract_ignores_soft_branch_choice_labels(self):
        contract = {
            "perspective": "third_person_limited",
            "protagonist_name": "沈砚",
        }
        prose = (
            "沈砚抬头看向顾宁。\n"
            "▶ 选项A：我推开那扇门\n"
            "▶ 选项B：我留下继续观察\n"
            "沈砚把信纸收进衣袋。"
        )
        self.assertEqual([], novel_reader.detect_narrative_pov_drift(prose, contract))

    def test_unknown_checkpoint_does_not_mask_explicit_style_metadata(self):
        contract = novel_reader.infer_narrative_perspective_contract(
            {
                "narrative_pov_contract": {
                    "contract_version": novel_reader.NARRATIVE_POV_CONTRACT_VERSION - 1,
                    "perspective": "unknown",
                },
                "style_notes": "全文采用第三人称限知叙述，跟随沈砚的所见所感。",
                "characters": [{"name": "沈砚", "role": "主角"}],
            }
        )

        self.assertEqual("third_person_limited", contract["perspective"])

    def test_unknown_cached_rule_text_does_not_mask_explicit_style_metadata(self):
        """An unknown checkpoint's old rule text is not authoritative evidence."""
        contract = novel_reader.infer_narrative_perspective_contract(
            {
                "narrative_pov_contract": {
                    "contract_version": 0,
                    "perspective": "unknown",
                    "rule_text": "保持第三人称叙述，不得使用第一人称。",
                },
                "style_notes": "全文采用第一人称叙述，跟随沈砚的内心。",
                "characters": [{"name": "沈砚", "role": "主角"}],
            }
        )

        self.assertEqual("first_person", contract["perspective"])

    def test_previous_pov_contract_version_is_recomputed_after_rule_change(self):
        """A previously persisted valid contract must not preserve an old POV guess."""
        contract = novel_reader.infer_narrative_perspective_contract(
            {
                "narrative_pov_contract": {
                    "contract_version": novel_reader.NARRATIVE_POV_CONTRACT_VERSION - 1,
                    "perspective": "third_person_limited",
                    "protagonist_name": "沈砚",
                    "rule_text": "保持第三人称叙述。",
                },
                "style_notes": "全文采用第一人称叙述，跟随沈砚的内心。",
                "characters": [{"name": "沈砚", "role": "主角"}],
            }
        )

        self.assertEqual("first_person", contract["perspective"])

    def test_reusing_a_valid_pov_contract_is_idempotent(self):
        worldview = {
            "style_notes": "全文采用第三人称限知叙述，跟随沈砚的所见所感。",
            "characters": [{"name": "沈砚", "role": "主角"}],
        }

        first = novel_reader.infer_narrative_perspective_contract(worldview)
        worldview["narrative_pov_contract"] = first
        second = novel_reader.infer_narrative_perspective_contract(worldview)
        worldview["narrative_pov_contract"] = second
        third = novel_reader.infer_narrative_perspective_contract(worldview)

        self.assertEqual(first, second)
        self.assertEqual(second, third)

    def test_first_person_contract_ignores_third_person_supporting_character(self):
        contract = {
            "perspective": "first_person",
            "protagonist_name": "沈砚",
            "pov_character_aliases": [],
        }
        prose = (
            "我推开门，看见顾宁站在门外。"
            "他转身走到窗边。他低头检查手里的信。"
        )

        self.assertEqual([], novel_reader.detect_narrative_pov_drift(prose, contract))

    def test_strips_choice_control_lines_before_pov_detection(self):
        contract = {
            "perspective": "third_person_limited",
            "protagonist_name": "沈砚",
        }
        prose = (
            "沈砚抬头看向顾宁。\n"
            "▶ 选项A：我留下来继续调查\n"
            "▶ 选项B：我转身离开现场\n"
            "沈砚把信收进衣袋。"
        )

        self.assertEqual([], novel_reader.detect_narrative_pov_drift(prose, contract))

    def test_ignores_unquoted_letter_or_diary_first_person_block(self):
        """Unquoted written text is not the surrounding narrator's POV."""
        contract = {
            "perspective": "third_person_limited",
            "protagonist_name": "沈砚",
        }
        prose = (
            "沈砚拆开信封，信中写着：\n"
            "我终于明白那天为什么要离开。\n"
            "我会在春天回来。\n\n"
            "沈砚把信纸折好，放回抽屉。"
        )

        issues = novel_reader.detect_narrative_pov_drift(prose, contract)

        self.assertEqual([], issues)

    def test_real_first_person_after_written_block_is_still_detected(self):
        """Written-text masking must not hide a later real POV switch."""
        contract = {
            "perspective": "third_person_limited",
            "protagonist_name": "沈砚",
        }
        prose = (
            "信纸上写着：\n"
            "我会等你回来。\n\n"
            "我推开门，走进雨里。"
        )

        issues = novel_reader.detect_narrative_pov_drift(prose, contract)

        self.assertTrue(issues)


if __name__ == "__main__":
    unittest.main()
