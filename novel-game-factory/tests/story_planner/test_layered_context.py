import unittest

from services.story_planner.core import branch_designer, chapter_context, ending_writer


class LayeredChapterContextTests(unittest.TestCase):
    def test_local_window_and_relevant_remote_decisions_are_selected(self):
        summaries = [
            "序幕，人物相识。",
            "江渡决定隐瞒病情。",
            "魏清越发现异常。",
            "两人在学校争执。",
            "江渡拒绝治疗并离开医院。",
            "朋友组织聚会。",
            "魏清越坦白自己知道病情。",
            "故事收束。",
        ]

        selected = chapter_context.select_relevant_chapter_summaries(
            summaries,
            current_index=3,
            query="江渡病情治疗隐瞒",
            neighbor_radius=2,
            remote_limit=2,
        )

        indexes = [item[0] for item in selected]
        self.assertEqual([1, 2, 3, 4, 5], indexes[:5])
        self.assertIn(6, indexes)
        self.assertLessEqual(len(indexes), 7)

    def test_context_does_not_include_unrelated_full_book_summaries(self):
        summaries = [f"第{i + 1}章普通日常" for i in range(48)]
        summaries[40] = "主角决定公开身世并承担代价"

        block = chapter_context.build_layered_summary_block(
            summaries,
            current_index=10,
            query="公开身世",
        )

        self.assertIn("第41章", block)
        self.assertIn("第9章", block)
        self.assertNotIn("第1章普通日常", block)
        self.assertLess(len(block), 3000)

    def test_ending_context_keeps_last_chapters_and_retrieves_related_history(self):
        summaries = [f"第{i + 1}章普通日常" for i in range(48)]
        summaries[5] = "江渡留下署名信和治疗录音"

        block = ending_writer.build_ending_summary_context(
            summaries,
            query="江渡署名信录音",
        )

        self.assertIn("第48章", block)
        self.assertIn("第47章", block)
        self.assertIn("第6章", block)
        self.assertNotIn("第1章普通日常", block)

    def test_fixed_fact_context_uses_pivot_window_instead_of_all_summaries(self):
        summaries = [f"第{i + 1}章普通日常" for i in range(48)]
        summaries[30] = "江渡拒绝治疗并离开医院"

        block = branch_designer.build_fixed_fact_summary_context(
            summaries,
            pivot_summary=summaries[30],
        )

        self.assertIn("第31章", block)
        self.assertIn("第29章", block)
        self.assertNotIn("第1章普通日常", block)
