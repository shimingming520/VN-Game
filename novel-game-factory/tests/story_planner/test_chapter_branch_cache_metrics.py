import inspect
import pickle
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from services.story_planner.utils import result_cache as result_cache_module
from services.story_planner.utils.result_cache import ResultCache


class ResultCacheTests(unittest.TestCase):
    def _make_versioned_cache(self, cache_dir, **kwargs):
        parameters = inspect.signature(ResultCache).parameters
        self.assertIn("namespace", parameters, "缓存尚未支持全局版本命名空间")
        if "max_age_seconds" in kwargs:
            self.assertIn("max_age_seconds", parameters, "缓存尚未支持 TTL 失效")
        if "clock" in kwargs:
            self.assertIn("clock", parameters, "缓存尚未提供可验证的时间源")
        return ResultCache(cache_dir, **kwargs)

    def test_nested_cache_directory_is_created(self):
        with TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir) / "nested" / "chapter_branch"
            cache = ResultCache(str(cache_dir))
            key = cache.get_cache_key("text", {"stage": "analysis"})
            cache.set(key, {"ok": True})

            self.assertEqual(cache.get(key), {"ok": True})

    def test_cache_namespace_version_changes_cache_key(self):
        with TemporaryDirectory() as temp_dir:
            cache_v1 = self._make_versioned_cache(
                temp_dir, namespace="chapter-branch-v1"
            )
            cache_v2 = self._make_versioned_cache(
                temp_dir, namespace="chapter-branch-v2"
            )

            key_v1 = cache_v1.get_cache_key("text", {"stage": "analysis"})
            key_v2 = cache_v2.get_cache_key("text", {"stage": "analysis"})

            self.assertNotEqual(key_v1, key_v2)
            self.assertEqual(len(key_v1), 64)

    def test_expired_cache_entry_is_invalidated_and_counted(self):
        with TemporaryDirectory() as temp_dir:
            now = [1000.0]
            cache = self._make_versioned_cache(
                temp_dir,
                namespace="chapter-branch-v1",
                max_age_seconds=60,
                clock=lambda: now[0],
            )
            key = cache.get_cache_key("text", {"stage": "analysis"})
            cache.set(key, {"ok": True})
            now[0] = 1061.0

            self.assertIsNone(cache.get(key))
            self.assertFalse((Path(temp_dir) / f"{key}.pkl").exists())
            metrics = cache.get_metrics()
            self.assertEqual(metrics["misses"], 1)
            self.assertEqual(metrics["invalidations"], 1)
            self.assertEqual(metrics["miss_reasons"]["expired"], 1)

    def test_unversioned_pickle_is_invalidated_instead_of_reused(self):
        with TemporaryDirectory() as temp_dir:
            cache = self._make_versioned_cache(
                temp_dir, namespace="chapter-branch-v1"
            )
            key = cache.get_cache_key("text", {"stage": "analysis"})
            cache_file = Path(temp_dir) / f"{key}.pkl"
            with cache_file.open("wb") as handle:
                pickle.dump({"legacy": True}, handle)

            self.assertIsNone(cache.get(key))
            self.assertFalse(cache_file.exists())
            self.assertEqual(
                cache.get_metrics()["miss_reasons"]["format_mismatch"],
                1,
            )

    def test_failed_write_preserves_previous_complete_entry(self):
        with TemporaryDirectory() as temp_dir:
            cache = self._make_versioned_cache(
                temp_dir, namespace="chapter-branch-v1"
            )
            key = cache.get_cache_key("text", {"stage": "analysis"})
            cache.set(key, {"version": "old"})

            def fail_after_partial_write(value, handle):
                handle.write(b"partial")
                raise OSError("shared storage interrupted")

            with patch.object(
                result_cache_module.pickle,
                "dump",
                side_effect=fail_after_partial_write,
            ):
                cache.set(key, {"version": "new"})

            self.assertEqual(cache.get(key), {"version": "old"})
            self.assertEqual(cache.get_metrics()["write_errors"], 1)
            self.assertEqual(list(Path(temp_dir).glob("*.tmp")), [])

    def test_hit_miss_write_metrics_have_stable_hit_rate(self):
        with TemporaryDirectory() as temp_dir:
            cache = self._make_versioned_cache(
                temp_dir, namespace="chapter-branch-v1"
            )
            key = cache.get_cache_key("text", {"stage": "analysis"})

            self.assertIsNone(cache.get(key))
            cache.set(key, {"ok": True})
            self.assertEqual(cache.get(key), {"ok": True})

            metrics = cache.get_metrics()
            self.assertEqual(metrics["hits"], 1)
            self.assertEqual(metrics["misses"], 1)
            self.assertEqual(metrics["writes"], 1)
            self.assertEqual(metrics["hit_rate"], 0.5)

    def test_clear_keeps_unrelated_shared_storage_files(self):
        with TemporaryDirectory() as temp_dir:
            cache = self._make_versioned_cache(
                temp_dir, namespace="chapter-branch-v1"
            )
            marker = Path(temp_dir) / "README.keep"
            marker.write_text("shared volume marker", encoding="utf-8")
            key = cache.get_cache_key("text", {"stage": "analysis"})
            cache.set(key, {"ok": True})

            cache.clear()

            self.assertTrue(marker.exists())
            self.assertFalse((Path(temp_dir) / f"{key}.pkl").exists())


class ChapterBranchCacheContractTests(unittest.TestCase):
    def test_main_pipeline_caches_analysis_and_terminal_states_and_exports_metrics(self):
        source = (
            Path(__file__).resolve().parents[2]
            / "services"
            / "story_planner"
            / "tasks.py"
        ).read_text(encoding="utf-8")
        chapter_branch_source = source.split("def _parse_chapter_branch_mode", 1)[1].split(
            "@celery_app.task", 1
        )[0]

        self.assertIn("ResultCache", chapter_branch_source)
        self.assertIn('"novel_context"', chapter_branch_source)
        self.assertIn('"terminal_states"', chapter_branch_source)
        self.assertIn('"cache_hits"', chapter_branch_source)
        self.assertIn('"phase_seconds"', chapter_branch_source)
        self.assertIn('"metrics": _run_metrics', chapter_branch_source)
        self.assertIn("build_run_metrics", chapter_branch_source)
        self.assertIn('"attempt_metrics"', chapter_branch_source)
        self.assertIn('"convergence_warnings"', chapter_branch_source)
        self.assertIn('"convergence_transition_issues"', chapter_branch_source)
        self.assertIn("director_metadata=", chapter_branch_source)

    def test_main_pipeline_uses_runtime_cache_policy_and_exports_detailed_metrics(self):
        source = (
            Path(__file__).resolve().parents[2]
            / "services"
            / "story_planner"
            / "tasks.py"
        ).read_text(encoding="utf-8")
        chapter_branch_source = source.split("def _parse_chapter_branch_mode", 1)[1].split(
            "@celery_app.task", 1
        )[0]

        self.assertIn("RUNTIME_SETTINGS.cache_dir", chapter_branch_source)
        self.assertIn("namespace=RUNTIME_SETTINGS.cache_version", chapter_branch_source)
        self.assertIn("RUNTIME_SETTINGS.cache_max_age_days * 86400", chapter_branch_source)
        self.assertIn("_result_cache.get_metrics()", chapter_branch_source)
        self.assertIn('"cache_hit_rate"', chapter_branch_source)
        self.assertIn('"cache_invalidations"', chapter_branch_source)
        self.assertIn('"cache_miss_reasons"', chapter_branch_source)

    def test_terminal_state_cache_only_keeps_high_confidence_llm_results(self):
        source = (
            Path(__file__).resolve().parents[2]
            / "services"
            / "story_planner"
            / "tasks.py"
        ).read_text(encoding="utf-8")
        chapter_branch_source = source.split("def _parse_chapter_branch_mode", 1)[1].split(
            "@celery_app.task", 1
        )[0]

        self.assertIn("terminal_states_are_cacheable", chapter_branch_source)
        self.assertIn("if terminal_states_are_cacheable(_states):", chapter_branch_source)
        self.assertIn('"cache_version": 5', chapter_branch_source)

    def test_analysis_and_terminal_cache_keys_use_current_pov_aware_version(self):
        source = (
            Path(__file__).resolve().parents[2]
            / "services"
            / "story_planner"
            / "tasks.py"
        ).read_text(encoding="utf-8")
        chapter_branch_source = source.split("def _parse_chapter_branch_mode", 1)[1].split(
            "@celery_app.task", 1
        )[0]

        self.assertIn('"cache_version": 5', chapter_branch_source)
        self.assertIn('"narrative_pov_contract": _narrative_pov_contract', chapter_branch_source)


if __name__ == "__main__":
    unittest.main()
