import importlib
import importlib.util
import json
import unittest
from unittest.mock import patch

from services.story_planner.core import novel_reader


class HierarchicalProcessingUnitTests(unittest.TestCase):
    def _module(self):
        module_name = "services.story_planner.core.hierarchical_processing"
        self.assertIsNotNone(
            importlib.util.find_spec(module_name),
            "缺少超长文本切分与分层合并模块",
        )
        return importlib.import_module(module_name)

    def test_split_preserves_text_and_caps_every_segment(self):
        module = self._module()
        text = ("第一段。\n" + "甲" * 47) * 20

        segments = module.split_text_at_boundaries(text, max_chars=120)

        self.assertEqual("".join(segments), text)
        self.assertGreater(len(segments), 1)
        self.assertTrue(all(0 < len(segment) <= 120 for segment in segments))
        self.assertTrue(any(segment.endswith(("。", "\n")) for segment in segments[:-1]))

    def test_hierarchical_reduce_never_exceeds_batch_size(self):
        module = self._module()
        group_sizes = []

        def merge_group(items, level, batch_index):
            group_sizes.append(len(items))
            return "[" + "|".join(items) + "]"

        result = module.hierarchical_reduce(
            [str(index) for index in range(10)],
            merge_batch_size=4,
            merge_group=merge_group,
        )

        self.assertEqual(group_sizes, [4, 4, 2, 3])
        self.assertLessEqual(max(group_sizes), 4)
        for index in range(10):
            self.assertIn(str(index), result)

    def test_hierarchical_reduce_compacts_failed_merge(self):
        module = self._module()

        def failing_merge(items, level, batch_index):
            raise RuntimeError("merge failed")

        result = module.hierarchical_reduce(
            ["内容" * 100 for _ in range(9)],
            merge_batch_size=3,
            merge_group=failing_merge,
            fallback_max_chars=90,
        )

        self.assertTrue(result)
        self.assertLessEqual(len(result), 90)

    def test_chunking_settings_support_environment_overrides(self):
        module = self._module()

        settings = module.resolve_hierarchical_settings(
            {
                "STORY_PLANNER_ANALYSIS_SEGMENT_CHARS": "16000",
                "STORY_PLANNER_SUMMARY_MERGE_BATCH_SIZE": "6",
                "STORY_PLANNER_WORLDVIEW_SEGMENT_CHARS": "60000",
                "STORY_PLANNER_WORLDVIEW_MERGE_BATCH_SIZE": "4",
            }
        )

        self.assertEqual(settings.analysis_segment_chars, 16000)
        self.assertEqual(settings.summary_merge_batch_size, 6)
        self.assertEqual(settings.worldview_segment_chars, 60000)
        self.assertEqual(settings.worldview_merge_batch_size, 4)


class _RecordingSummaryClient:
    def __init__(self):
        self.merge_prompt_lengths = []

    def chat_completion(self, system_prompt, user_prompt, **kwargs):
        if system_prompt.startswith("你是专业摘要师"):
            self.merge_prompt_lengths.append(len(user_prompt))
            return "合并摘要" * 200
        return "分段摘要" * 250


class _RecordingWorldviewClient:
    def __init__(self):
        self.merge_prompt_lengths = []

    def chat_completion_json(self, system_prompt, user_prompt, **kwargs):
        if user_prompt.startswith("以下是同一部小说"):
            self.merge_prompt_lengths.append(len(user_prompt))
            return {"title": "合并", "payload": "乙" * 1000}
        return {"title": "分段", "payload": "甲" * 5000}


class HierarchicalNovelReaderIntegrationTests(unittest.TestCase):
    def test_worldview_merge_prompt_uses_field_aware_compaction(self):
        payload = {
            "source_segment": "seg-a",
            "characters": [
                {
                    "name": f"角色-{index}",
                    "aliases": [f"别名-{index}"],
                    "background": "背景事实" * 300,
                    "secrets": ["秘密" * 100 for _ in range(4)],
                }
                for index in range(30)
            ],
            "key_events": [f"事件-{index}-" + "事实" * 300 for index in range(40)],
            "narrative_arc": "叙事弧线" * 2000,
        }

        prompt = novel_reader._build_worldview_merge_prompt(
            [payload, {**payload, "source_segment": "seg-b"}],
            level=0,
            batch_index=0,
            max_prompt_tokens=9000,
        )

        from services.story_planner.core.prompt_budget import estimate_prompt_tokens

        self.assertLessEqual(estimate_prompt_tokens(prompt), 9000)
        self.assertIn("seg-a", prompt)
        self.assertIn("seg-b", prompt)
        self.assertIn('"characters"', prompt)
        self.assertNotIn("上下文已按预算裁剪", prompt)

    def test_worldview_merge_groups_split_by_prompt_budget(self):
        items = [
            '{"source_segment":"seg-%s","characters":[%s]}'
            % (index, ",".join('"角色%s"' % item for item in range(30)))
            for index in range(8)
        ]

        groups = novel_reader._group_worldview_merge_items(
            items,
            max_batch_size=5,
            max_prompt_tokens=1200,
        )

        self.assertGreater(len(groups), 2)
        self.assertTrue(all(group for group in groups))
        self.assertTrue(all(len(group) <= 5 for group in groups))

    def test_worldview_fallback_merge_keeps_valid_structured_payload(self):
        merged = novel_reader._fallback_merge_worldview_payloads(
            [
                {"source_segment": "seg-a", "characters": [{"name": "甲"}], "key_events": ["开端"]},
                {"source_segment": "seg-b", "characters": [{"name": "乙"}], "key_events": ["转折"]},
            ]
        )

        self.assertEqual(merged["source_segments"], ["seg-a", "seg-b"])
        self.assertEqual([item["name"] for item in merged["characters"]], ["甲", "乙"])
        self.assertEqual(merged["key_events"], ["开端", "转折"])

    def test_worldview_payload_compaction_is_valid_and_bounded_at_small_budget(self):
        payload = {
            "source_segment": "seg-a",
            "title": "很长的标题" * 30,
            "characters": [{"name": "甲", "background": "背景" * 100} for _ in range(10)],
            "key_events": ["事件" * 100 for _ in range(10)],
        }

        compacted = novel_reader._compact_worldview_payload(payload, max_chars=60)
        encoded = json.dumps(compacted, ensure_ascii=False, separators=(",", ":"))

        self.assertLessEqual(len(encoded), 60)
        self.assertEqual(json.loads(encoded)["source_segment"], "seg-a")

    def test_oversized_chapter_summary_uses_multiple_bounded_merge_requests(self):
        client = _RecordingSummaryClient()
        chapter = "甲" * (13000 * 20)

        result = novel_reader._summarize_one_chapter(
            chapter,
            0,
            {},
            client,
        )

        self.assertTrue(result)
        self.assertGreater(len(client.merge_prompt_lengths), 1)
        self.assertLessEqual(max(client.merge_prompt_lengths), 12000)

    def test_large_worldview_uses_multiple_bounded_merge_requests(self):
        client = _RecordingWorldviewClient()

        with patch.object(novel_reader, "LLMClient", return_value=client):
            result = novel_reader._analyze_worldview(
                "文" * (50000 * 12),
                provider="openai",
            )

        self.assertEqual(result.get("title"), "合并")
        self.assertGreater(len(client.merge_prompt_lengths), 1)
        self.assertLessEqual(max(client.merge_prompt_lengths), 40000)


if __name__ == "__main__":
    unittest.main()
