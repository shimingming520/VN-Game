import os
import unittest
from unittest.mock import MagicMock, patch

os.environ["STORY_PLANNER_SMOKE_MODE"] = "1"

from services.story_planner.tasks import (
    _rebalance_last_two_chapters,
    _should_rebalance_chapters,
    _split_chapters,
    _strip_front_matter,
    _validated_explicit_heading_matches,
)


class ChapterSplitterTests(unittest.TestCase):
    def test_explicit_chapter_headings_are_preserved(self):
        text = (
            "第1章 替嫁（一）\n" + "第一章正文。" * 1800 + "\n"
            "第2章 替嫁（二）\n" + "第二章正文。" * 1800 + "\n"
            "第3章 替嫁（三）\n" + "第三章正文。" * 1800
        )

        chapters = _split_chapters(text)

        self.assertEqual(len(chapters), 3)
        self.assertTrue(chapters[0].startswith("第1章"))
        self.assertTrue(chapters[2].startswith("第3章"))

    def test_headingless_long_text_still_uses_size_based_split(self):
        text = "无标题正文。" * 4000

        chapters = _split_chapters(text)

        self.assertGreater(len(chapters), 1)
        self.assertEqual("".join(chapters), text)

    def test_headingless_size_split_targets_ten_to_fifteen_thousand_characters(self):
        text = "无标题正文。" * 7000

        chapters = _split_chapters(text)

        self.assertEqual("".join(chapters), text)
        self.assertTrue(all(10000 <= len(chapter) <= 15000 for chapter in chapters))

    def test_headingless_split_prefers_complete_sentence_over_hard_cut(self):
        sentence = "失手把师祖从平步崖上推了下去。"
        text = "甲" * 9995 + sentence + "乙" * 500 + "\n\n" + "丙" * 9500

        chapters = _split_chapters(text)

        self.assertEqual("".join(chapters), text)
        self.assertGreater(len(chapters), 1)
        self.assertTrue(chapters[0].endswith("。"), chapters[0][-40:])
        self.assertTrue(chapters[1].startswith("乙"), chapters[1][:40])

    def test_section_heading_moves_with_its_body(self):
        section = "四 起死回生\n关河梦和公羊无门赶到时，屋里仍点着一盏灯。"
        text = "甲" * 9995 + "\n" + section + "乙" * 9000

        chapters = _split_chapters(text)

        self.assertEqual("".join(chapters), text)
        self.assertGreater(len(chapters), 1)
        self.assertTrue(chapters[1].startswith("四 起死回生\n关河梦"), chapters[1][:50])

    def test_section_heading_near_minimum_is_never_split(self):
        section = "四 起死回生\n关河梦和公羊无门赶到时，屋里仍点着一盏灯。"
        text = "甲" * 9995 + "\n" + section + "乙" * 12000

        chapters = _split_chapters(text)

        self.assertEqual("".join(chapters), text)
        self.assertTrue(chapters[1].startswith("四 起死回生\n关河梦"), chapters[1][:50])

    @patch("services.story_planner.core.llm_client.LLMClient")
    def test_semantic_rebalance_fallback_respects_minimum_ending_size(self, client_cls):
        client = MagicMock()
        client.chat_completion_json.return_value = {"split_marker": "不存在的标记"}
        client_cls.return_value = client
        combined = "甲" * 7000 + "\n\n" + "乙" * 2798 + "\n\n" + "丙" * 2200
        chapters = [combined[:6000], combined[6000:]]

        rebalanced = _rebalance_last_two_chapters(chapters, provider="openai")

        self.assertGreaterEqual(len(rebalanced[-1]), 4500)
        self.assertGreaterEqual(len(rebalanced[-2]), 4500)
        self.assertEqual(combined, "".join(rebalanced))

    def test_explicit_original_chapters_are_not_semantically_rebalanced(self):
        chapters = ["第1章 开端\n正文", "第2章 结局\n正文"]

        self.assertFalse(_should_rebalance_chapters(chapters))
        self.assertTrue(_should_rebalance_chapters(["无标题上半", "无标题下半"]))

    def test_normalized_explicit_stream_is_not_rebalanced_when_last_chunk_has_no_title(self):
        chapters = ["Chapter 1\n正文", "正文续段\n", "Chapter 2\n正文"]

        self.assertFalse(_should_rebalance_chapters(chapters))

    @patch("services.story_planner.core.llm_client.LLMClient")
    def test_front_matter_cleaning_never_deletes_first_explicit_heading(self, client_cls):
        client = MagicMock()
        client.chat_completion.return_value = "简介：测试"
        client_cls.return_value = client
        text = "书名\n作者：某人\n简介：测试\n\n第1章 开端\n正文"

        cleaned = _strip_front_matter(text)

        self.assertTrue(cleaned.startswith("第1章 开端"))

    @patch("services.story_planner.core.llm_client.LLMClient")
    def test_front_matter_cleaning_removes_translation_credit_after_heading(self, client_cls):
        client = MagicMock()
        client.chat_completion.return_value = "作者：某人"
        client_cls.return_value = client
        text = (
            "书名\n作者：某人\n\n第一卷 序章 明明只是想和好\n"
            "网译版 转自 轻之国度\n翻译：沐梓\n校对：小明\n\n这是正文首句。"
        )

        cleaned = _strip_front_matter(text)

        self.assertTrue(cleaned.startswith("第一卷 序章 明明只是想和好"))
        self.assertNotIn("网译版", cleaned)
        self.assertNotIn("翻译：沐梓", cleaned)
        self.assertNotIn("校对：小明", cleaned)
        self.assertIn("这是正文首句。", cleaned)

    @patch("services.story_planner.core.llm_client.LLMClient")
    def test_front_matter_with_explicit_heading_still_uses_llm(self, client_cls):
        """Front matter detection must not bypass the LLM just because a heading exists."""
        client = MagicMock()
        client.chat_completion.return_value = "作者：测试作者"
        client_cls.return_value = client
        text = "广告：下载 APP\n作者：测试作者\n\n第一章 开端\n正文首句。\n第二章 发展\n正文。"

        cleaned = _strip_front_matter(text, provider="openai")

        client.chat_completion.assert_called_once()
        prompt = client.chat_completion.call_args.kwargs["user_prompt"]
        self.assertLessEqual(len(prompt), 10000)
        self.assertTrue(cleaned.startswith("第一章 开端"))
        self.assertNotIn("广告：下载 APP", cleaned)
        self.assertIn("正文首句。", cleaned)

    @patch("services.story_planner.core.llm_client.LLMClient")
    def test_front_matter_llm_returning_first_heading_keeps_heading(self, client_cls):
        """A model mistake must not delete the first chapter title."""
        client = MagicMock()
        client.chat_completion.return_value = "第一章 开端"
        client_cls.return_value = client
        text = "广告：下载 APP\n\n第一章 开端\n正文首句。\n第二章 发展\n正文。"

        cleaned = _strip_front_matter(text, provider="openai")

        self.assertTrue(cleaned.startswith("第一章 开端"))
        self.assertIn("正文首句。", cleaned)

    @patch("services.story_planner.core.llm_client.LLMClient")
    def test_front_matter_llm_boundary_inside_body_keeps_complete_story(self, client_cls):
        """An over-eager model boundary must never remove chapter正文."""
        client = MagicMock()
        client.chat_completion.return_value = "正文第二句。"
        client_cls.return_value = client
        text = "广告：下载 APP\n\n第一章 开端\n正文首句。\n正文第二句。\n第二章 发展\n正文。"

        cleaned = _strip_front_matter(text, provider="openai")

        self.assertEqual(cleaned, text)

    def test_mixed_no_and_chinese_headings_are_preserved_when_contiguous(self):
        text = (
            "NO.1 初遇\n" + "第一章正文。" * 1800 + "\n"
            "NO.2 雨天\n" + "第二章正文。" * 1800 + "\n"
            "第三章 转折\n" + "第三章正文。" * 1800 + "\n"
            "第四章 结局\n" + "第四章正文。" * 1800
        )

        chapters = _split_chapters(text)

        self.assertEqual(len(chapters), 4)
        self.assertEqual(
            [chapter.splitlines()[0] for chapter in chapters],
            ["NO.1 初遇", "NO.2 雨天", "第三章 转折", "第四章 结局"],
        )
        self.assertEqual("".join(chapters), text)

    def test_fullwidth_no_headings_are_supported(self):
        text = (
            "ＮＯ．１ 初遇\n甲。\n"
            "ＮＯ．２ 雨天\n乙。\n"
            "第3章 转折\n丙。"
        )

        chapters = _split_chapters(text)

        self.assertEqual(len(chapters), 1)
        self.assertTrue(chapters[0].startswith("ＮＯ．１"))
        self.assertIn("第3章", chapters[0])

    def test_no_headings_with_fullwidth_indentation_are_supported(self):
        text = (
            "　　No.1 初遇\n甲。\n"
            "　　No.2 雨天\n乙。\n"
            "　　第3章 转折\n丙。"
        )

        chapters = _split_chapters(text)

        self.assertEqual(len(chapters), 1)
        self.assertTrue(chapters[0].lstrip().startswith("No.1"))
        self.assertIn("第3章", chapters[0])

    def test_three_hundred_no_chapters_followed_by_chinese_chapters_are_complete(self):
        parts = []
        for number in range(1, 401):
            title = f"NO.{number}" if number <= 300 else f"第{number}章"
            parts.append(f"{title}\n正文{number}。")
        text = "\n".join(parts)

        chapters = _split_chapters(text)

        self.assertEqual(len(chapters), 1)
        self.assertTrue(chapters[0].startswith("NO.1"))
        self.assertIn("NO.300", chapters[0])
        self.assertIn("第301章", chapters[0])
        self.assertTrue(chapters[0].endswith("正文400。"))
        self.assertEqual("".join(chapters), text)

    def test_short_explicit_chapters_are_merged_to_game_chapter_size(self):
        parts = [
            f"Chapter {number}\n" + (f"正文{number}。" * 650)
            for number in range(1, 5)
        ]
        text = "\n".join(parts)

        chapters = _split_chapters(text)

        self.assertEqual("".join(chapters), text)
        self.assertEqual(len(chapters), 1)
        self.assertGreaterEqual(len(chapters[0]), 10000)
        self.assertLessEqual(len(chapters[0]), 15000)

    def test_short_explicit_chapters_fall_back_to_lossless_size_split_when_needed(self):
        parts = [
            f"Chapter {number}\n" + (f"正文{number}。" * 1250)
            for number in range(1, 5)
        ]
        text = "\n".join(parts)

        chapters = _split_chapters(text)

        self.assertEqual("".join(chapters), text)
        self.assertEqual(len(chapters), 2)
        self.assertTrue(all(10000 <= len(chapter) <= 15000 for chapter in chapters))

    def test_tiny_explicit_novel_is_kept_as_one_short_game_chapter(self):
        text = "\n".join(
            f"Chapter {number}\n短正文{number}。" for number in range(1, 4)
        )

        chapters = _split_chapters(text)

        self.assertEqual("".join(chapters), text)
        self.assertEqual(len(chapters), 1)

    def test_explicit_chapters_already_in_target_range_are_not_merged(self):
        parts = [
            f"Chapter {number}\n" + (f"正文{number}。" * 2600)
            for number in range(1, 4)
        ]
        text = "\n".join(parts)

        chapters = _split_chapters(text)

        self.assertEqual("".join(chapters), text)
        self.assertEqual(len(chapters), 3)

    def test_oversized_explicit_chapter_is_split_to_game_chapter_size(self):
        text = "Chapter 1\n" + ("长正文。" * 4000) + "\nChapter 2\n" + ("短正文。" * 1000)

        chapters = _split_chapters(text)

        self.assertEqual("".join(chapters), text)
        self.assertTrue(all(10000 <= len(chapter) <= 15000 for chapter in chapters))

    def test_non_contiguous_explicit_headings_fall_back_to_size_split(self):
        text = "第2章 开始\n" + "甲" * 9000 + "\n第4章 结束\n" + "乙" * 9000

        chapters = _split_chapters(text)

        self.assertGreater(len(chapters), 1)
        self.assertEqual("".join(chapters), text)
        self.assertEqual(_validated_explicit_heading_matches(text), [])


if __name__ == "__main__":
    unittest.main()
