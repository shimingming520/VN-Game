from pathlib import Path
from tempfile import TemporaryDirectory
import os
import stat
import unittest
from unittest.mock import patch

from services.story_planner.core import novel_text_encoding as encoding


class NovelTextEncodingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[2]

    def test_utf8_file_is_read_without_rewrite(self):
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "novel.txt"
            original = "第一章\r\n这是正文。"
            path.write_bytes(original.encode("utf-8"))

            result = encoding.read_novel_text(path, convert_to_utf8=True)

            self.assertEqual(original, result.text)
            self.assertEqual("utf-8", result.source_encoding)
            self.assertFalse(result.converted)
            self.assertEqual(original.encode("utf-8"), path.read_bytes())

    def test_gbk_file_is_detected_and_atomically_converted_to_utf8(self):
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "novel.txt"
            original = "（穿书）黑莲花攻略手册\r\n第1章 替嫁（一）\r\n红衣少女坐在妆台前。"
            path.write_bytes(original.encode("gbk"))

            result = encoding.read_novel_text(path, convert_to_utf8=True)

            self.assertEqual(original, result.text)
            self.assertEqual("gb18030", result.source_encoding)
            self.assertTrue(result.converted)
            self.assertEqual(original, path.read_bytes().decode("utf-8"))
            self.assertEqual([], list(path.parent.glob(f".{path.name}.*.tmp")))

    def test_utf8_bom_is_removed_during_normalization(self):
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "novel.txt"
            path.write_bytes(b"\xef\xbb\xbf" + "第一章\n正文".encode("utf-8"))

            result = encoding.read_novel_text(path, convert_to_utf8=True)

            self.assertEqual("第一章\n正文", result.text)
            self.assertEqual("utf-8-sig", result.source_encoding)
            self.assertTrue(result.converted)
            self.assertFalse(path.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_read_only_detection_does_not_modify_gbk_source(self):
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "novel.txt"
            raw = "第1章\n中文正文".encode("gbk")
            path.write_bytes(raw)

            result = encoding.read_novel_text(path, convert_to_utf8=False)

            self.assertEqual("第1章\n中文正文", result.text)
            self.assertEqual("gb18030", result.source_encoding)
            self.assertFalse(result.converted)
            self.assertEqual(raw, path.read_bytes())

    @unittest.skipUnless(os.name == "nt", "只读替换行为仅用于 Windows")
    def test_read_only_gbk_file_is_converted_and_remains_read_only(self):
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "novel.txt"
            original = "第一章\n只读的中文小说正文。"
            path.write_bytes(original.encode("gbk"))
            path.chmod(stat.S_IREAD)
            try:
                result = encoding.read_novel_text(path, convert_to_utf8=True)

                self.assertTrue(result.converted)
                self.assertEqual(original, path.read_bytes().decode("utf-8"))
                self.assertFalse(path.stat().st_mode & stat.S_IWRITE)
            finally:
                path.chmod(stat.S_IWRITE | stat.S_IREAD)

    def test_binary_or_unsupported_input_is_rejected_without_rewrite(self):
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "novel.txt"
            raw = b"\x00\x01\x02\x03\x04\x05\xff\xfe" * 100
            path.write_bytes(raw)

            with self.assertRaises(encoding.NovelEncodingError):
                encoding.read_novel_text(path, convert_to_utf8=True)

            self.assertEqual(raw, path.read_bytes())

    def test_atomic_replace_failure_preserves_original_and_cleans_temp_file(self):
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "novel.txt"
            raw = "第一章\n转换失败时保留原文。".encode("gbk")
            path.write_bytes(raw)

            with patch.object(encoding.os, "replace", side_effect=OSError("disk error")):
                with self.assertRaises(encoding.NovelEncodingError):
                    encoding.read_novel_text(path, convert_to_utf8=True)

            self.assertEqual(raw, path.read_bytes())
            self.assertEqual([], list(path.parent.glob(f".{path.name}.*.tmp")))

    @unittest.skipUnless(os.name == "nt", "只读替换行为仅用于 Windows")
    def test_read_only_replace_failure_restores_original_permissions(self):
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "novel.txt"
            raw = "第一章\n只读转换失败。".encode("gbk")
            path.write_bytes(raw)
            path.chmod(stat.S_IREAD)
            try:
                with patch.object(encoding.os, "replace", side_effect=OSError("disk error")):
                    with self.assertRaises(encoding.NovelEncodingError):
                        encoding.read_novel_text(path, convert_to_utf8=True)

                self.assertEqual(raw, path.read_bytes())
                self.assertFalse(path.stat().st_mode & stat.S_IWRITE)
            finally:
                path.chmod(stat.S_IWRITE | stat.S_IREAD)

    def test_all_story_planner_novel_entrypoints_use_strict_normalizer(self):
        tasks_source = (
            self.repo_root / "services" / "story_planner" / "tasks.py"
        ).read_text(encoding="utf-8")
        batch_source = (
            self.repo_root / "services" / "story_planner" / "batch_translate.py"
        ).read_text(encoding="utf-8")
        smoke_source = (
            self.repo_root
            / "services"
            / "story_planner"
            / "run_chapter_branch_smoke.py"
        ).read_text(encoding="utf-8")
        analyst_source = (
            self.repo_root
            / "services"
            / "story_planner"
            / "core"
            / "world_analyst.py"
        ).read_text(encoding="utf-8")
        main_source = (
            self.repo_root / "services" / "story_planner" / "main.py"
        ).read_text(encoding="utf-8")

        self.assertNotIn('errors="replace"', tasks_source)
        self.assertNotIn("errors='replace'", tasks_source)
        self.assertIn("read_novel_text", batch_source)
        self.assertNotIn('errors="replace"', batch_source)
        self.assertIn("read_novel_text(input_path, convert_to_utf8=True)", smoke_source)
        self.assertIn("read_novel_text(filepath, convert_to_utf8=True)", analyst_source)
        self.assertNotIn("'latin1'", analyst_source)
        self.assertIn("read_novel_text(novel_file, convert_to_utf8=True)", main_source)


if __name__ == "__main__":
    unittest.main()
