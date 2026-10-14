import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from common.celery_app import celery_app
import common.progress_utils as progress_utils
from services.story_planner.core import llm_client as llm_client_module
from services.story_planner.core.llm_client import LLMClient


class _FakeCompletions:
    def __init__(self, content):
        self.content = content
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))],
            usage=None,
        )


class _AsyncFakeCompletions(_FakeCompletions):
    async def create(self, **kwargs):
        return super().create(**kwargs)


class StoryPlannerLongTaskTimeoutTests(unittest.TestCase):
    def test_runtime_config_has_versioned_cache_defaults(self):
        from services.story_planner.core.runtime_config import resolve_runtime_settings

        settings = resolve_runtime_settings({})

        self.assertTrue(hasattr(settings, "cache_dir"), "运行时配置尚未暴露缓存目录")
        self.assertTrue(hasattr(settings, "cache_version"), "运行时配置尚未暴露缓存版本")
        self.assertTrue(
            hasattr(settings, "cache_max_age_days"),
            "运行时配置尚未暴露缓存最大保留天数",
        )
        self.assertEqual(
            Path(settings.cache_dir).parts[-3:],
            ("story_planner", ".cache", "chapter_branch"),
        )
        self.assertEqual(settings.cache_version, "chapter-branch-v1")
        self.assertEqual(settings.cache_max_age_days, 30)

    def test_runtime_config_accepts_shared_cache_overrides(self):
        from services.story_planner.core.runtime_config import resolve_runtime_settings

        settings = resolve_runtime_settings(
            {
                "STORY_PLANNER_CACHE_DIR": "/storage/story_planner/cache/chapter_branch",
                "STORY_PLANNER_CACHE_VERSION": "chapter-branch-v9",
                "STORY_PLANNER_CACHE_MAX_AGE_DAYS": "45",
            }
        )

        self.assertTrue(hasattr(settings, "cache_dir"), "运行时配置尚未支持共享缓存")
        self.assertEqual(
            settings.cache_dir,
            "/storage/story_planner/cache/chapter_branch",
        )
        self.assertEqual(settings.cache_version, "chapter-branch-v9")
        self.assertEqual(settings.cache_max_age_days, 45)

    def test_runtime_config_exposes_heartbeat_and_stall_thresholds(self):
        from services.story_planner.core.runtime_config import resolve_runtime_settings

        defaults = resolve_runtime_settings({})
        overridden = resolve_runtime_settings(
            {
                "STORY_PLANNER_HEARTBEAT_INTERVAL_SECONDS": "45",
                "STORY_PLANNER_NODE_STALL_TIMEOUT_SECONDS": "2400",
            }
        )

        self.assertEqual(60, defaults.heartbeat_interval_seconds)
        self.assertEqual(1800, defaults.node_stall_timeout_seconds)
        self.assertEqual(45, overridden.heartbeat_interval_seconds)
        self.assertEqual(2400, overridden.node_stall_timeout_seconds)

    def test_runtime_config_exposes_safe_defaults_for_large_novels(self):
        module_name = "services.story_planner.core.runtime_config"
        self.assertIsNotNone(
            importlib.util.find_spec(module_name),
            "缺少 story_planner 长任务运行时配置模块",
        )
        from services.story_planner.core.runtime_config import resolve_runtime_settings

        settings = resolve_runtime_settings({})

        self.assertEqual(settings.task_soft_time_limit, 38 * 60 * 60)
        self.assertEqual(settings.task_time_limit, 48 * 60 * 60)
        self.assertEqual(settings.confirmation_timeout, 6 * 60 * 60)

    def test_runtime_config_accepts_positive_environment_overrides(self):
        module_name = "services.story_planner.core.runtime_config"
        self.assertIsNotNone(importlib.util.find_spec(module_name))
        from services.story_planner.core.runtime_config import resolve_runtime_settings

        settings = resolve_runtime_settings(
            {
                "STORY_PLANNER_TASK_SOFT_TIME_LIMIT": "90000",
                "STORY_PLANNER_TASK_TIME_LIMIT": "93600",
                "STORY_PLANNER_CONFIRMATION_TIMEOUT": "28800",
            }
        )

        self.assertEqual(settings.task_soft_time_limit, 90000)
        self.assertEqual(settings.task_time_limit, 93600)
        self.assertEqual(settings.confirmation_timeout, 28800)

    def test_runtime_config_keeps_soft_limit_below_hard_limit(self):
        from services.story_planner.core.runtime_config import resolve_runtime_settings

        settings = resolve_runtime_settings(
            {
                "STORY_PLANNER_TASK_SOFT_TIME_LIMIT": "90000",
                "STORY_PLANNER_TASK_TIME_LIMIT": "86400",
            }
        )

        self.assertEqual(settings.task_soft_time_limit, 82800)
        self.assertLess(settings.task_soft_time_limit, settings.task_time_limit)

    def test_celery_visibility_and_stage_wait_cover_story_planner_hard_limit(self):
        expected_minimum = 48 * 60 * 60 + 600

        self.assertGreaterEqual(
            celery_app.conf.broker_transport_options["visibility_timeout"],
            expected_minimum,
        )
        self.assertGreaterEqual(
            progress_utils._DEFAULT_STAGE_WAIT_TIMEOUT,
            expected_minimum,
        )
        self.assertTrue(celery_app.conf.broker_connection_retry_on_startup)

    def test_sync_text_call_uses_configured_timeout_when_omitted(self):
        completions = _FakeCompletions("ok")
        client = LLMClient()
        client._sync_client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )

        with patch.object(
            llm_client_module,
            "_LLM_SETTINGS",
            SimpleNamespace(timeout=1800),
        ):
            self.assertEqual(client.chat_completion("system", "user"), "ok")

        self.assertEqual(completions.last_kwargs["timeout"], 1800)

    def test_sync_json_call_uses_configured_timeout_when_omitted(self):
        completions = _FakeCompletions('{"ok": true}')
        client = LLMClient()
        client._sync_client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )

        with patch.object(
            llm_client_module,
            "_LLM_SETTINGS",
            SimpleNamespace(timeout=1800),
        ):
            self.assertEqual(
                client.chat_completion_json("system", "user"),
                {"ok": True},
            )

        self.assertEqual(completions.last_kwargs["timeout"], 1800)

    def test_async_text_call_uses_configured_timeout_when_omitted(self):
        completions = _AsyncFakeCompletions("ok")
        client = LLMClient()
        client._async_client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )

        async def run_call():
            with patch.object(
                llm_client_module,
                "_LLM_SETTINGS",
                SimpleNamespace(timeout=1800),
            ):
                return await client.async_chat_completion("system", "user")

        self.assertEqual(asyncio.run(run_call()), "ok")
        self.assertEqual(completions.last_kwargs["timeout"], 1800)


if __name__ == "__main__":
    unittest.main()
