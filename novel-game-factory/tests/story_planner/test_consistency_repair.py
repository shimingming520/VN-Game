import unittest
from unittest.mock import MagicMock, patch

from services.story_planner.core import novel_reader


class ConsistencyRepairTests(unittest.TestCase):
    def test_whatif_behavior_anchors_exclude_replaced_sibling_decisions(self):
        anchors = (
            "【各章已定型的关键决策】\n"
            "  第4章：江渡决定暂时隐瞒秘密\n"
            "  第5章：江渡决定继续观察魏清越\n"
            "  第6章：江渡借书城提问并主动搭话\n"
            "使用方式：写 What-If 时只能改变分叉点之后的决定。"
        )

        filtered = novel_reader.filter_behavior_anchors_for_whatif(
            anchors,
            pivot_chapter_index=4,
        )

        self.assertIn("第4章", filtered)
        self.assertIn("第5章", filtered)
        self.assertNotIn("第6章：江渡借书城提问", filtered)

    def test_ending_anchors_mark_open_mystery_and_drop_post_pivot_decisions(self):
        anchors = (
            "【各章已定型的关键决策】\n"
            "  第2章：张婆婆背走菜瓮\n"
            "  第3章：张婆婆随后失踪，真实去向未明\n"
            "  第4章：众人最终放弃寻找张婆婆\n"
            "使用方式：结局只能改变分叉点之后的决定。"
        )

        filtered = novel_reader.filter_behavior_anchors_for_ending(
            anchors,
            pivot_chapter_index=2,
        )

        self.assertIn("第2章：张婆婆背走菜瓮", filtered)
        self.assertIn("【开放悬念】第3章：张婆婆随后失踪，真实去向未明", filtered)
        self.assertNotIn("第4章：众人最终放弃寻找张婆婆", filtered)
        self.assertIn("允许在结局中通过合理过程揭示", filtered)

    @patch.object(novel_reader, "check_content_consistency")
    @patch.object(novel_reader, "LLMClient")
    def test_inconsistent_content_is_repaired_and_rechecked(self, client_cls, check):
        check.side_effect = [(False, "已死角色复活"), (True, "")]
        client = MagicMock()
        client.chat_completion_json.return_value = {
            "replacements": [{"old": "复活", "new": "只在回忆中出现"}]
        }
        client_cls.return_value = client

        repaired = novel_reader.repair_content_consistency_or_raise(
            content="已死角色复活",
            title="平行章",
            char_cards="角色卡",
            fixed_facts="既定事实",
            provider="openai",
        )

        self.assertEqual(repaired, "已死角色只在回忆中出现")
        self.assertEqual(check.call_count, 2)

    @patch.object(novel_reader, "check_content_consistency")
    @patch.object(novel_reader, "LLMClient")
    def test_failed_recheck_blocks_release(self, client_cls, check):
        check.side_effect = [
            (False, "已死角色复活"),
            (False, "仍然复活"),
            (False, "场景重写后仍然复活"),
        ]
        client = MagicMock()
        client.chat_completion_json.side_effect = [
            {"replacements": [{"old": "复活", "new": "重新出现"}]},
            {"replacements": [{"old": "重新出现", "new": "以实体再次出现"}]},
        ]
        client_cls.return_value = client

        with self.assertRaisesRegex(
            novel_reader.ContentConsistencyRepairRequired,
            "场景级修复后仍未通过",
        ):
            novel_reader.repair_content_consistency_or_raise(
                content="已死角色复活",
                title="平行章",
                char_cards="角色卡",
                fixed_facts="既定事实",
                provider="openai",
            )

    @patch.object(novel_reader, "check_content_consistency")
    @patch.object(novel_reader, "LLMClient")
    def test_failed_patch_uses_one_segment_rewrite_before_escalating(
        self,
        client_cls,
        check,
    ):
        check.side_effect = [
            (False, "张婆婆被找回但缺少寻找过程"),
            (False, "补丁后仍缺少寻找过程"),
            (True, ""),
        ]
        client = MagicMock()
        client.chat_completion_json.side_effect = [
            {
                "replacements": [
                    {"old": "被找回", "new": "后来现身"},
                ]
            },
            {
                "replacements": [
                    {
                        "old": "张婆婆后来现身并完整交代旧事。",
                        "new": "众人循着菜瓮留下的痕迹找到别院，才见到张婆婆。",
                    }
                ]
            },
        ]
        client_cls.return_value = client

        repaired = novel_reader.repair_content_consistency_or_raise(
            content="张婆婆被找回并完整交代旧事。",
            title="结局·胆魄",
            char_cards="角色卡",
            fixed_facts="【开放悬念】张婆婆失踪，真实去向未明",
            provider="openai",
        )

        self.assertEqual(
            "众人循着菜瓮留下的痕迹找到别院，才见到张婆婆。",
            repaired,
        )
        self.assertEqual(3, check.call_count)
        self.assertEqual(2, client.chat_completion_json.call_count)


if __name__ == "__main__":
    unittest.main()
