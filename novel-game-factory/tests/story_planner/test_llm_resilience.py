import asyncio
import importlib
import importlib.util
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from services.story_planner.core import llm_client as llm_client_module
from services.story_planner.core.llm_client import LLMClient


class _HTTPFailure(Exception):
    def __init__(self, status_code, message, headers=None):
        super().__init__(message)
        self.status_code = status_code
        self.response = SimpleNamespace(headers=headers or {})


class _FakeRedis:
    def __init__(self, now):
        self.now = now
        self.values = {}

    def _purge(self, key):
        entry = self.values.get(key)
        if entry and entry[1] <= self.now[0]:
            self.values.pop(key, None)

    def set(self, key, value, *, nx=False, px=None):
        self._purge(key)
        if nx and key in self.values:
            return False
        expires_at = self.now[0] + ((px or 0) / 1000.0)
        self.values[key] = (str(value), expires_at)
        return True

    def pttl(self, key):
        self._purge(key)
        if key not in self.values:
            return -2
        return max(0, int(round((self.values[key][1] - self.now[0]) * 1000)))

    def eval(self, script, numkeys, key, token):
        self._purge(key)
        entry = self.values.get(key)
        if entry and entry[0] == str(token):
            self.values.pop(key, None)
            return 1
        return 0


class LLMResilienceUnitTests(unittest.TestCase):
    def _module(self):
        module_name = "services.story_planner.core.llm_resilience"
        self.assertIsNotNone(
            importlib.util.find_spec(module_name),
            "缺少 story_planner LLM 网络韧性模块",
        )
        return importlib.import_module(module_name)

    def test_default_retry_and_rate_limit_settings(self):
        module = self._module()

        settings = module.resolve_resilience_settings({})

        self.assertEqual(settings.max_attempts, 5)
        self.assertEqual(settings.backoff_base_seconds, 2.0)
        self.assertEqual(settings.backoff_max_seconds, 60.0)
        self.assertEqual(settings.backoff_jitter_ratio, 0.2)
        self.assertEqual(settings.requests_per_minute, 60)
        self.assertTrue(
            hasattr(settings, "global_limit_enabled"),
            "尚未提供 Redis 集群级 LLM 限流配置",
        )
        self.assertFalse(settings.global_limit_enabled)
        self.assertEqual(settings.global_requests_per_minute, 60)
        self.assertEqual(settings.global_max_concurrency, 4)
        self.assertEqual(settings.backpressure_max_wait_seconds, 900.0)
        self.assertEqual(settings.concurrency_lease_seconds, 1800)

    def test_resilience_settings_support_environment_overrides(self):
        module = self._module()

        settings = module.resolve_resilience_settings(
            {
                "STORY_PLANNER_LLM_MAX_ATTEMPTS": "4",
                "STORY_PLANNER_LLM_BACKOFF_BASE_SECONDS": "1.5",
                "STORY_PLANNER_LLM_BACKOFF_MAX_SECONDS": "30",
                "STORY_PLANNER_LLM_BACKOFF_JITTER_RATIO": "0.1",
                "STORY_PLANNER_LLM_REQUESTS_PER_MINUTE": "90",
                "STORY_PLANNER_LLM_GLOBAL_LIMIT_ENABLED": "true",
                "STORY_PLANNER_LLM_GLOBAL_REQUESTS_PER_MINUTE": "45",
                "STORY_PLANNER_LLM_GLOBAL_MAX_CONCURRENCY": "3",
                "STORY_PLANNER_LLM_BACKPRESSURE_MAX_WAIT_SECONDS": "120",
                "STORY_PLANNER_LLM_BACKPRESSURE_POLL_SECONDS": "0.25",
                "STORY_PLANNER_LLM_CONCURRENCY_LEASE_SECONDS": "900",
            }
        )

        self.assertEqual(settings.max_attempts, 4)
        self.assertEqual(settings.backoff_base_seconds, 1.5)
        self.assertEqual(settings.backoff_max_seconds, 30.0)
        self.assertEqual(settings.backoff_jitter_ratio, 0.1)
        self.assertEqual(settings.requests_per_minute, 90)
        self.assertTrue(
            hasattr(settings, "global_limit_enabled"),
            "尚未支持集群级限流环境变量",
        )
        self.assertTrue(settings.global_limit_enabled)
        self.assertEqual(settings.global_requests_per_minute, 45)
        self.assertEqual(settings.global_max_concurrency, 3)
        self.assertEqual(settings.backpressure_max_wait_seconds, 120.0)
        self.assertEqual(settings.backpressure_poll_seconds, 0.25)
        self.assertEqual(settings.concurrency_lease_seconds, 900)

    def test_backoff_maximum_cannot_be_lower_than_base_delay(self):
        module = self._module()

        settings = module.resolve_resilience_settings(
            {
                "STORY_PLANNER_LLM_BACKOFF_BASE_SECONDS": "100",
                "STORY_PLANNER_LLM_BACKOFF_MAX_SECONDS": "60",
            }
        )

        self.assertGreaterEqual(
            settings.backoff_max_seconds,
            settings.backoff_base_seconds,
        )

    def test_http_errors_are_classified_by_retryability(self):
        module = self._module()
        cases = (
            (401, "authentication", False),
            (403, "permission", False),
            (429, "rate_limit", True),
            (500, "server", True),
            (503, "server", True),
        )

        for status_code, category, retryable in cases:
            with self.subTest(status_code=status_code):
                result = module.classify_llm_error(
                    _HTTPFailure(status_code, "request failed")
                )
                self.assertEqual(result.category, category)
                self.assertEqual(result.retryable, retryable)
                self.assertEqual(result.status_code, status_code)

    def test_retry_after_header_is_extracted_from_rate_limit_error(self):
        module = self._module()

        result = module.classify_llm_error(
            _HTTPFailure(429, "too many requests", {"Retry-After": "7"})
        )

        self.assertEqual(result.retry_after_seconds, 7.0)

    def test_error_messages_classify_auth_context_and_timeout(self):
        module = self._module()
        cases = (
            (RuntimeError("Invalid API key provided"), "authentication", False),
            (RuntimeError("maximum context length exceeded"), "context_length", False),
            (TimeoutError("request timeout"), "timeout", True),
            (ConnectionError("connection reset"), "connection", True),
        )

        for exc, category, retryable in cases:
            with self.subTest(category=category):
                result = module.classify_llm_error(exc)
                self.assertEqual(result.category, category)
                self.assertEqual(result.retryable, retryable)

    def test_backoff_is_exponential_with_jitter_and_retry_after(self):
        module = self._module()
        settings = module.ResilienceSettings(
            max_attempts=5,
            backoff_base_seconds=2.0,
            backoff_max_seconds=60.0,
            backoff_jitter_ratio=0.2,
            requests_per_minute=60,
        )

        delays = [
            module.calculate_retry_delay(index, settings, jitter_sample=0.5)
            for index in range(3)
        ]
        retry_after_delay = module.calculate_retry_delay(
            0,
            settings,
            retry_after_seconds=7.0,
            jitter_sample=0.0,
        )

        self.assertEqual(delays, [2.2, 4.4, 8.8])
        self.assertEqual(retry_after_delay, 7.0)

    def test_rate_limiter_reserves_evenly_spaced_request_slots(self):
        module = self._module()
        now = [100.0]
        limiter = module.RequestRateLimiter(
            requests_per_minute=60,
            clock=lambda: now[0],
        )

        first = limiter.reserve_delay()
        second = limiter.reserve_delay()
        now[0] += 0.25
        third = limiter.reserve_delay()

        self.assertEqual(first, 0.0)
        self.assertEqual(second, 1.0)
        self.assertEqual(third, 1.75)

    def test_zero_requests_per_minute_disables_local_pacing(self):
        module = self._module()
        settings = module.resolve_resilience_settings(
            {"STORY_PLANNER_LLM_REQUESTS_PER_MINUTE": "0"}
        )
        limiter = module.RequestRateLimiter(
            requests_per_minute=settings.requests_per_minute,
            clock=lambda: 100.0,
        )

        self.assertEqual(settings.requests_per_minute, 0)
        self.assertEqual(limiter.reserve_delay(), 0.0)
        self.assertEqual(limiter.reserve_delay(), 0.0)

    def test_global_admission_spaces_requests_across_workers(self):
        module = self._module()
        self.assertTrue(
            hasattr(module, "RedisAdmissionController"),
            "尚未实现 Redis 集群级准入控制器",
        )
        now = [100.0]
        redis_client = _FakeRedis(now)
        sleeps = []

        def sleep(delay):
            sleeps.append(delay)
            now[0] += delay

        controller = module.RedisAdmissionController(
            redis_client,
            requests_per_minute=60,
            max_concurrency=2,
            max_wait_seconds=10,
            poll_seconds=0.1,
            lease_seconds=30,
            key_prefix="test:llm",
            clock=lambda: now[0],
            sleep=sleep,
        )

        first = controller.acquire_sync()
        controller.release_sync(first)
        second = controller.acquire_sync()

        self.assertGreaterEqual(sum(sleeps), 1.0)
        self.assertLess(sum(sleeps), 1.2)
        self.assertGreaterEqual(second.wait_seconds, 1.0)
        controller.release_sync(second)

    def test_global_admission_applies_backpressure_when_concurrency_is_full(self):
        module = self._module()
        self.assertTrue(hasattr(module, "RedisAdmissionController"))
        self.assertTrue(hasattr(module, "LLMBackpressureTimeout"))
        now = [200.0]
        redis_client = _FakeRedis(now)

        def sleep(delay):
            now[0] += delay

        controller = module.RedisAdmissionController(
            redis_client,
            requests_per_minute=600,
            max_concurrency=1,
            max_wait_seconds=1,
            poll_seconds=0.25,
            lease_seconds=30,
            key_prefix="test:llm",
            clock=lambda: now[0],
            sleep=sleep,
        )
        permit = controller.acquire_sync()

        with self.assertRaises(module.LLMBackpressureTimeout):
            controller.acquire_sync()

        controller.release_sync(permit)
        recovered = controller.acquire_sync()
        controller.release_sync(recovered)

    def test_expired_concurrency_lease_recovers_after_worker_loss(self):
        module = self._module()
        self.assertTrue(hasattr(module, "RedisAdmissionController"))
        now = [300.0]
        redis_client = _FakeRedis(now)

        controller = module.RedisAdmissionController(
            redis_client,
            requests_per_minute=600,
            max_concurrency=1,
            max_wait_seconds=2,
            poll_seconds=0.1,
            lease_seconds=1,
            key_prefix="test:llm",
            clock=lambda: now[0],
            sleep=lambda delay: now.__setitem__(0, now[0] + delay),
        )
        abandoned = controller.acquire_sync()
        now[0] += 1.1

        recovered = controller.acquire_sync()

        self.assertNotEqual(abandoned.token, recovered.token)
        controller.release_sync(recovered)


class _SequenceCompletions:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, SimpleNamespace):
            return outcome
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=outcome))],
            usage=None,
        )


class _AsyncSequenceCompletions(_SequenceCompletions):
    async def create(self, **kwargs):
        return super().create(**kwargs)


class _RecordingLimiter:
    def __init__(self):
        self.sync_calls = 0
        self.async_calls = 0

    def wait_sync(self):
        self.sync_calls += 1
        return 0.0

    async def wait_async(self):
        self.async_calls += 1
        return 0.0


class _RecordingAdmission:
    def __init__(self):
        self.sync_acquires = 0
        self.async_acquires = 0
        self.released = []

    def acquire_sync(self):
        self.sync_acquires += 1
        return f"sync-{self.sync_acquires}"

    async def acquire_async(self):
        self.async_acquires += 1
        return f"async-{self.async_acquires}"

    def release_sync(self, permit):
        self.released.append(permit)

    async def release_async(self, permit):
        self.released.append(permit)


class LLMClientResilienceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module(
            "services.story_planner.core.llm_resilience"
        )
        self.settings = self.module.ResilienceSettings(
            max_attempts=3,
            backoff_base_seconds=2.0,
            backoff_max_seconds=60.0,
            backoff_jitter_ratio=0.0,
            requests_per_minute=60,
        )
        self.limiter = _RecordingLimiter()

    def _sync_client(self, completions):
        client = LLMClient()
        client._sync_client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )
        return client

    def _patch_resilience(self):
        return (
            patch.object(self.module, "RESILIENCE_SETTINGS", self.settings),
            patch.object(self.module, "LLM_RATE_LIMITER", self.limiter),
        )

    def test_sync_text_retries_503_with_exponential_backoff(self):
        completions = _SequenceCompletions(
            [
                _HTTPFailure(503, "upstream failure"),
                _HTTPFailure(503, "upstream failure"),
                "ok",
            ]
        )
        client = self._sync_client(completions)
        sleeps = []
        settings_patch, limiter_patch = self._patch_resilience()

        with settings_patch, limiter_patch, patch.object(
            llm_client_module.time,
            "sleep",
            side_effect=sleeps.append,
        ):
            try:
                result = client.chat_completion("system", "user")
            except Exception as exc:
                self.fail(f"同步文本调用未重试 503: {exc}")

        self.assertEqual(result, "ok")
        self.assertEqual(completions.calls, 3)
        self.assertEqual(sleeps, [2.0, 4.0])
        self.assertEqual(self.limiter.sync_calls, 3)

    def test_sync_text_retries_http_200_response_without_choices(self):
        completions = _SequenceCompletions(
            [
                SimpleNamespace(choices=None, usage=None),
                "ok",
            ]
        )
        client = self._sync_client(completions)
        sleeps = []
        settings_patch, limiter_patch = self._patch_resilience()

        with settings_patch, limiter_patch, patch.object(
            llm_client_module.time,
            "sleep",
            side_effect=sleeps.append,
        ):
            result = client.chat_completion("system", "user")

        self.assertEqual(result, "ok")
        self.assertEqual(completions.calls, 2)
        self.assertEqual(sleeps, [2.0])

    def test_http_200_error_without_choices_logs_safe_gateway_diagnostics(self):
        completions = _SequenceCompletions(
            [
                SimpleNamespace(
                    choices=None,
                    usage=None,
                    error={
                        "message": (
                            "stream error: stream disconnected before completion: "
                            "stream closed before response.completed"
                        ),
                        "type": "invalid_request_error",
                        "code": None,
                    },
                    model_fields_set={"error"},
                    _request_id="req-k8s-123",
                ),
                '{"ok": true}',
            ]
        )
        client = self._sync_client(completions)
        settings_patch, limiter_patch = self._patch_resilience()

        with (
            settings_patch,
            limiter_patch,
            patch.object(llm_client_module.time, "sleep"),
            self.assertLogs(llm_client_module.logger, level="WARNING") as captured,
        ):
            result = client.chat_completion_json("system-secret", "novel-secret")

        output = "\n".join(captured.output)
        self.assertEqual(result, {"ok": True})
        self.assertIn("LLM响应协议异常", output)
        self.assertIn("http_status=200", output)
        self.assertIn("invalid_reason=http_200_error_without_choices", output)
        self.assertIn("body_keys=['error']", output)
        self.assertIn("request_id=req-k8s-123", output)
        self.assertIn("error_type=invalid_request_error", output)
        self.assertIn("stream disconnected before completion", output)
        self.assertIn("attempt=1/3", output)
        self.assertNotIn("system-secret", output)
        self.assertNotIn("novel-secret", output)

    def test_http_200_empty_choices_logs_shape_without_gateway_error(self):
        completions = _SequenceCompletions(
            [
                SimpleNamespace(
                    choices=[],
                    usage=None,
                    model_fields_set={"choices"},
                ),
                "ok",
            ]
        )
        client = self._sync_client(completions)
        settings_patch, limiter_patch = self._patch_resilience()

        with (
            settings_patch,
            limiter_patch,
            patch.object(llm_client_module.time, "sleep"),
            self.assertLogs(llm_client_module.logger, level="WARNING") as captured,
        ):
            result = client.chat_completion("system", "user")

        output = "\n".join(captured.output)
        self.assertEqual(result, "ok")
        self.assertIn("invalid_reason=choices_empty", output)
        self.assertIn("choices_type=list", output)
        self.assertIn("choices_count=0", output)
        self.assertIn("error_type=none", output)

    def test_authentication_error_fails_without_retry(self):
        completions = _SequenceCompletions(
            [_HTTPFailure(401, "Invalid API key provided")]
        )
        client = self._sync_client(completions)
        sleeps = []
        settings_patch, limiter_patch = self._patch_resilience()

        with settings_patch, limiter_patch, patch.object(
            llm_client_module.time,
            "sleep",
            side_effect=sleeps.append,
        ):
            with self.assertRaises(_HTTPFailure):
                client.chat_completion("system", "user")

        self.assertEqual(completions.calls, 1)
        self.assertEqual(sleeps, [])
        self.assertEqual(self.limiter.sync_calls, 1)

    def test_rate_limit_retry_respects_retry_after(self):
        completions = _SequenceCompletions(
            [
                _HTTPFailure(429, "too many requests", {"Retry-After": "7"}),
                "ok",
            ]
        )
        client = self._sync_client(completions)
        sleeps = []
        settings_patch, limiter_patch = self._patch_resilience()

        with settings_patch, limiter_patch, patch.object(
            llm_client_module.time,
            "sleep",
            side_effect=sleeps.append,
        ):
            result = client.chat_completion("system", "user")

        self.assertEqual(result, "ok")
        self.assertEqual(sleeps, [7.0])

    def test_async_text_retries_transient_server_error(self):
        completions = _AsyncSequenceCompletions(
            [_HTTPFailure(503, "upstream failure"), "ok"]
        )
        client = LLMClient()
        client._async_client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )
        sleeps = []
        settings_patch, limiter_patch = self._patch_resilience()

        async def fake_sleep(delay):
            sleeps.append(delay)

        async def run_call():
            with settings_patch, limiter_patch, patch.object(
                asyncio,
                "sleep",
                side_effect=fake_sleep,
            ):
                try:
                    return await client.async_chat_completion("system", "user")
                except Exception as exc:
                    self.fail(f"异步文本调用未重试 503: {exc}")

        self.assertEqual(asyncio.run(run_call()), "ok")
        self.assertEqual(completions.calls, 2)
        self.assertEqual(sleeps, [2.0])
        self.assertEqual(self.limiter.async_calls, 2)

    def test_async_text_retries_http_200_response_without_content(self):
        completions = _AsyncSequenceCompletions(
            [
                SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=None))],
                    usage=None,
                ),
                "ok",
            ]
        )
        client = LLMClient()
        client._async_client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )
        sleeps = []
        settings_patch, limiter_patch = self._patch_resilience()

        async def fake_sleep(delay):
            sleeps.append(delay)

        async def run_call():
            with settings_patch, limiter_patch, patch.object(
                asyncio,
                "sleep",
                side_effect=fake_sleep,
            ):
                return await client.async_chat_completion("system", "user")

        self.assertEqual(asyncio.run(run_call()), "ok")
        self.assertEqual(completions.calls, 2)
        self.assertEqual(sleeps, [2.0])

    def test_sync_json_retries_transient_server_error(self):
        completions = _SequenceCompletions(
            [_HTTPFailure(503, "upstream failure"), '{"ok": true}']
        )
        client = self._sync_client(completions)
        sleeps = []
        settings_patch, limiter_patch = self._patch_resilience()

        with settings_patch, limiter_patch, patch.object(
            llm_client_module.time,
            "sleep",
            side_effect=sleeps.append,
        ):
            try:
                result = client.chat_completion_json("system", "user")
            except Exception as exc:
                self.fail(f"同步 JSON 调用未重试 503: {exc}")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(completions.calls, 2)
        self.assertEqual(sleeps, [2.0])
        self.assertEqual(self.limiter.sync_calls, 2)

    def test_sync_json_retries_http_200_response_with_empty_choices(self):
        completions = _SequenceCompletions(
            [
                SimpleNamespace(choices=[], usage=None),
                '{"ok": true}',
            ]
        )
        client = self._sync_client(completions)
        sleeps = []
        settings_patch, limiter_patch = self._patch_resilience()

        with settings_patch, limiter_patch, patch.object(
            llm_client_module.time,
            "sleep",
            side_effect=sleeps.append,
        ):
            result = client.chat_completion_json("system", "user")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(completions.calls, 2)
        self.assertEqual(sleeps, [2.0])

    def test_sync_retry_reacquires_and_releases_global_permit_each_attempt(self):
        self.assertTrue(
            hasattr(self.module, "GLOBAL_LLM_ADMISSION"),
            "LLM 客户端尚未接入全局准入控制器",
        )
        completions = _SequenceCompletions(
            [_HTTPFailure(503, "upstream failure"), "ok"]
        )
        client = self._sync_client(completions)
        admission = _RecordingAdmission()

        with (
            patch.object(self.module, "RESILIENCE_SETTINGS", self.settings),
            patch.object(self.module, "LLM_RATE_LIMITER", self.limiter),
            patch.object(self.module, "GLOBAL_LLM_ADMISSION", admission),
            patch.object(llm_client_module.time, "sleep"),
        ):
            result = client.chat_completion("system", "user")

        self.assertEqual(result, "ok")
        self.assertEqual(admission.sync_acquires, 2)
        self.assertEqual(admission.released, ["sync-1", "sync-2"])

    def test_async_call_releases_global_permit(self):
        self.assertTrue(
            hasattr(self.module, "GLOBAL_LLM_ADMISSION"),
            "异步 LLM 客户端尚未接入全局准入控制器",
        )
        completions = _AsyncSequenceCompletions(["ok"])
        client = LLMClient()
        client._async_client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )
        admission = _RecordingAdmission()

        async def run_call():
            with (
                patch.object(self.module, "RESILIENCE_SETTINGS", self.settings),
                patch.object(self.module, "LLM_RATE_LIMITER", self.limiter),
                patch.object(self.module, "GLOBAL_LLM_ADMISSION", admission),
            ):
                return await client.async_chat_completion("system", "user")

        self.assertEqual(asyncio.run(run_call()), "ok")
        self.assertEqual(admission.async_acquires, 1)
        self.assertEqual(admission.released, ["async-1"])


if __name__ == "__main__":
    unittest.main()
