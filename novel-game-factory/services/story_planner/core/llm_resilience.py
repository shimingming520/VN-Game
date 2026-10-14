"""Retry, error classification, and client-side rate limiting for LLM calls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import logging
import math
import os
import threading
import time
from typing import Callable, Mapping, Optional
import urllib.error
import uuid


DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BACKOFF_BASE_SECONDS = 2.0
DEFAULT_BACKOFF_MAX_SECONDS = 60.0
DEFAULT_BACKOFF_JITTER_RATIO = 0.2
DEFAULT_REQUESTS_PER_MINUTE = 60
DEFAULT_GLOBAL_REQUESTS_PER_MINUTE = 60
DEFAULT_GLOBAL_MAX_CONCURRENCY = 4
DEFAULT_BACKPRESSURE_MAX_WAIT_SECONDS = 900.0
DEFAULT_BACKPRESSURE_POLL_SECONDS = 0.5
DEFAULT_CONCURRENCY_LEASE_SECONDS = 1800
DEFAULT_GLOBAL_KEY_PREFIX = "story_planner:llm:gpt-5.6-sol"


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResilienceSettings:
    max_attempts: int
    backoff_base_seconds: float
    backoff_max_seconds: float
    backoff_jitter_ratio: float
    requests_per_minute: int
    global_limit_enabled: bool = False
    global_requests_per_minute: int = DEFAULT_GLOBAL_REQUESTS_PER_MINUTE
    global_max_concurrency: int = DEFAULT_GLOBAL_MAX_CONCURRENCY
    backpressure_max_wait_seconds: float = DEFAULT_BACKPRESSURE_MAX_WAIT_SECONDS
    backpressure_poll_seconds: float = DEFAULT_BACKPRESSURE_POLL_SECONDS
    concurrency_lease_seconds: int = DEFAULT_CONCURRENCY_LEASE_SECONDS
    global_key_prefix: str = DEFAULT_GLOBAL_KEY_PREFIX


@dataclass(frozen=True)
class LLMErrorClassification:
    category: str
    retryable: bool
    status_code: Optional[int] = None
    retry_after_seconds: Optional[float] = None


class LLMBackpressureTimeout(RuntimeError):
    """Raised when a request cannot enter the global LLM budget in time."""


class LLMInvalidResponseError(RuntimeError):
    """Raised when a successful HTTP response has no usable chat content."""

    def __init__(
        self,
        message: str,
        *,
        diagnostics: Optional[Mapping[str, object]] = None,
    ):
        super().__init__(message)
        self.diagnostics = dict(diagnostics or {})


@dataclass(frozen=True)
class AdmissionPermit:
    token: str
    slot_key: str
    wait_seconds: float


def _int_setting(
    source: Mapping[str, str],
    key: str,
    default: int,
    minimum: int,
) -> int:
    try:
        value = int(source.get(key, ""))
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def _float_setting(
    source: Mapping[str, str],
    key: str,
    default: float,
    minimum: float,
    maximum: Optional[float] = None,
) -> float:
    try:
        value = float(source.get(key, ""))
    except (TypeError, ValueError):
        return default
    if value < minimum or (maximum is not None and value > maximum):
        return default
    return value


def _bool_setting(source: Mapping[str, str], key: str, default: bool) -> bool:
    value = str(source.get(key, "") or "").strip().lower()
    if not value:
        return default
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def resolve_resilience_settings(
    env: Optional[Mapping[str, str]] = None,
) -> ResilienceSettings:
    source = os.environ if env is None else env
    base_delay = _float_setting(
        source,
        "STORY_PLANNER_LLM_BACKOFF_BASE_SECONDS",
        DEFAULT_BACKOFF_BASE_SECONDS,
        minimum=0.0,
    )
    max_delay = max(
        base_delay,
        _float_setting(
            source,
            "STORY_PLANNER_LLM_BACKOFF_MAX_SECONDS",
            DEFAULT_BACKOFF_MAX_SECONDS,
            minimum=0.0,
        ),
    )
    return ResilienceSettings(
        max_attempts=_int_setting(
            source,
            "STORY_PLANNER_LLM_MAX_ATTEMPTS",
            DEFAULT_MAX_ATTEMPTS,
            minimum=1,
        ),
        backoff_base_seconds=base_delay,
        backoff_max_seconds=max_delay,
        backoff_jitter_ratio=_float_setting(
            source,
            "STORY_PLANNER_LLM_BACKOFF_JITTER_RATIO",
            DEFAULT_BACKOFF_JITTER_RATIO,
            minimum=0.0,
            maximum=1.0,
        ),
        requests_per_minute=_int_setting(
            source,
            "STORY_PLANNER_LLM_REQUESTS_PER_MINUTE",
            DEFAULT_REQUESTS_PER_MINUTE,
            # 0 明确表示关闭进程内请求节流；负数和非法值仍回退安全默认值。
            minimum=0,
        ),
        global_limit_enabled=_bool_setting(
            source,
            "STORY_PLANNER_LLM_GLOBAL_LIMIT_ENABLED",
            False,
        ),
        global_requests_per_minute=_int_setting(
            source,
            "STORY_PLANNER_LLM_GLOBAL_REQUESTS_PER_MINUTE",
            DEFAULT_GLOBAL_REQUESTS_PER_MINUTE,
            minimum=1,
        ),
        global_max_concurrency=_int_setting(
            source,
            "STORY_PLANNER_LLM_GLOBAL_MAX_CONCURRENCY",
            DEFAULT_GLOBAL_MAX_CONCURRENCY,
            minimum=1,
        ),
        backpressure_max_wait_seconds=_float_setting(
            source,
            "STORY_PLANNER_LLM_BACKPRESSURE_MAX_WAIT_SECONDS",
            DEFAULT_BACKPRESSURE_MAX_WAIT_SECONDS,
            minimum=0.1,
        ),
        backpressure_poll_seconds=_float_setting(
            source,
            "STORY_PLANNER_LLM_BACKPRESSURE_POLL_SECONDS",
            DEFAULT_BACKPRESSURE_POLL_SECONDS,
            minimum=0.01,
        ),
        concurrency_lease_seconds=_int_setting(
            source,
            "STORY_PLANNER_LLM_CONCURRENCY_LEASE_SECONDS",
            DEFAULT_CONCURRENCY_LEASE_SECONDS,
            minimum=1,
        ),
        global_key_prefix=(
            str(
                source.get(
                    "STORY_PLANNER_LLM_GLOBAL_LIMIT_KEY_PREFIX",
                    DEFAULT_GLOBAL_KEY_PREFIX,
                )
                or ""
            ).strip()
            or DEFAULT_GLOBAL_KEY_PREFIX
        ),
    )


RESILIENCE_SETTINGS = resolve_resilience_settings()


def _status_code_from_exception(exc: Exception) -> Optional[int]:
    for candidate in (
        getattr(exc, "status_code", None),
        getattr(exc, "code", None),
        getattr(getattr(exc, "response", None), "status_code", None),
    ):
        if candidate is not None:
            try:
                return int(candidate)
            except (TypeError, ValueError):
                continue
    return None


def _header_value(exc: Exception, header_name: str) -> Optional[str]:
    header_sources = (
        getattr(exc, "headers", None),
        getattr(getattr(exc, "response", None), "headers", None),
    )
    wanted = header_name.lower()
    for headers in header_sources:
        if not headers:
            continue
        try:
            for key, value in headers.items():
                if str(key).lower() == wanted:
                    return str(value)
        except AttributeError:
            continue
    return None


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except (TypeError, ValueError):
        pass
    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def classify_llm_error(exc: Exception) -> LLMErrorClassification:
    if isinstance(exc, LLMBackpressureTimeout):
        return LLMErrorClassification("backpressure", False)
    if isinstance(exc, LLMInvalidResponseError):
        return LLMErrorClassification("invalid_response", True)
    status_code = _status_code_from_exception(exc)
    retry_after = _parse_retry_after(_header_value(exc, "Retry-After"))
    if status_code == 401:
        return LLMErrorClassification("authentication", False, status_code)
    if status_code == 403:
        return LLMErrorClassification("permission", False, status_code)
    if status_code == 404:
        return LLMErrorClassification("not_found", False, status_code)
    if status_code in (408, 409):
        return LLMErrorClassification("timeout", True, status_code, retry_after)
    if status_code == 429:
        return LLMErrorClassification("rate_limit", True, status_code, retry_after)
    if status_code is not None and 500 <= status_code < 600:
        return LLMErrorClassification("server", True, status_code, retry_after)
    if status_code is not None and 400 <= status_code < 500:
        return LLMErrorClassification("invalid_request", False, status_code)

    if isinstance(exc, (TimeoutError,)):
        return LLMErrorClassification("timeout", True)
    if isinstance(exc, (ConnectionError, urllib.error.URLError)):
        return LLMErrorClassification("connection", True)

    message = str(exc).lower()
    if any(
        marker in message
        for marker in (
            "invalid api key",
            "invalid_api_key",
            "authentication",
            "unauthorized",
        )
    ):
        return LLMErrorClassification("authentication", False)
    if any(
        marker in message
        for marker in (
            "maximum context length",
            "context length exceeded",
            "context_length_exceeded",
            "too many tokens",
        )
    ):
        return LLMErrorClassification("context_length", False)
    if any(marker in message for marker in ("permission", "forbidden")):
        return LLMErrorClassification("permission", False)
    if any(
        marker in message
        for marker in ("rate limit", "too many requests", "temporarily throttled")
    ):
        return LLMErrorClassification("rate_limit", True, retry_after_seconds=retry_after)
    if any(marker in message for marker in ("timeout", "timed out")):
        return LLMErrorClassification("timeout", True)
    if any(
        marker in message
        for marker in ("connection", "network", "dns", "proxy", "ssl")
    ):
        return LLMErrorClassification("connection", True)
    if any(
        marker in message
        for marker in (
            "temporarily unavailable",
            "service unavailable",
            "server error",
            "internal error",
            "overloaded",
            "server busy",
        )
    ):
        return LLMErrorClassification("server", True)
    return LLMErrorClassification("unknown", False, status_code, retry_after)


def calculate_retry_delay(
    retry_index: int,
    settings: ResilienceSettings = RESILIENCE_SETTINGS,
    retry_after_seconds: Optional[float] = None,
    jitter_sample: float = 0.0,
) -> float:
    exponential = min(
        settings.backoff_base_seconds * (2 ** max(0, retry_index)),
        settings.backoff_max_seconds,
    )
    jitter = exponential * settings.backoff_jitter_ratio * min(
        1.0,
        max(0.0, jitter_sample),
    )
    delay = min(exponential + jitter, settings.backoff_max_seconds)
    if retry_after_seconds is not None:
        delay = max(delay, max(0.0, retry_after_seconds))
    return round(delay, 3)


class RequestRateLimiter:
    """Thread-safe leaky-bucket limiter shared by one worker; 0 disables it."""

    def __init__(
        self,
        requests_per_minute: int,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.requests_per_minute = max(0, int(requests_per_minute))
        self._interval_seconds = (
            0.0
            if self.requests_per_minute == 0
            else 60.0 / self.requests_per_minute
        )
        self._clock = clock
        self._next_allowed_at = 0.0
        self._lock = threading.Lock()

    def reserve_delay(self) -> float:
        if self._interval_seconds == 0:
            return 0.0
        with self._lock:
            now = self._clock()
            scheduled_at = max(now, self._next_allowed_at)
            self._next_allowed_at = scheduled_at + self._interval_seconds
            return max(0.0, scheduled_at - now)

    def wait_sync(self, sleep: Callable[[float], None] = time.sleep) -> float:
        delay = self.reserve_delay()
        if delay > 0:
            sleep(delay)
        return delay

    async def wait_async(self) -> float:
        import asyncio

        delay = self.reserve_delay()
        if delay > 0:
            await asyncio.sleep(delay)
        return delay


_RELEASE_SLOT_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


class RedisAdmissionController:
    """Redis-backed global rate and in-flight concurrency admission control."""

    def __init__(
        self,
        redis_client,
        *,
        requests_per_minute: int,
        max_concurrency: int,
        max_wait_seconds: float,
        poll_seconds: float,
        lease_seconds: int,
        key_prefix: str,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.redis_client = redis_client
        self.requests_per_minute = max(1, int(requests_per_minute))
        self.max_concurrency = max(1, int(max_concurrency))
        self.max_wait_seconds = max(0.1, float(max_wait_seconds))
        self.poll_seconds = max(0.01, float(poll_seconds))
        self.lease_seconds = max(1, int(lease_seconds))
        self.key_prefix = str(key_prefix).rstrip(":")
        self._clock = clock
        self._sleep = sleep
        self._rate_interval_ms = max(
            1,
            int(math.ceil(60000 / self.requests_per_minute)),
        )

    def _wait_or_timeout(self, deadline: float, redis_ttl_ms: int) -> None:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise LLMBackpressureTimeout(
                f"等待全局 LLM 配额超过 {self.max_wait_seconds:.1f} 秒"
            )
        ttl_seconds = redis_ttl_ms / 1000.0 if redis_ttl_ms > 0 else self.poll_seconds
        self._sleep(min(remaining, self.poll_seconds, max(0.001, ttl_seconds)))

    def _acquire_concurrency_slot(self, token: str, deadline: float) -> str:
        lease_ms = self.lease_seconds * 1000
        while True:
            smallest_ttl = -1
            for slot_index in range(self.max_concurrency):
                slot_key = f"{self.key_prefix}:concurrency:{slot_index}"
                if self.redis_client.set(slot_key, token, nx=True, px=lease_ms):
                    return slot_key
                ttl = int(self.redis_client.pttl(slot_key))
                if ttl > 0 and (smallest_ttl < 0 or ttl < smallest_ttl):
                    smallest_ttl = ttl
            self._wait_or_timeout(deadline, smallest_ttl)

    def _acquire_rate_slot(self, token: str, deadline: float) -> None:
        rate_key = f"{self.key_prefix}:rate"
        while not self.redis_client.set(
            rate_key,
            token,
            nx=True,
            px=self._rate_interval_ms,
        ):
            self._wait_or_timeout(deadline, int(self.redis_client.pttl(rate_key)))

    def acquire_sync(self) -> AdmissionPermit:
        started_at = self._clock()
        deadline = started_at + self.max_wait_seconds
        token = uuid.uuid4().hex
        slot_key = self._acquire_concurrency_slot(token, deadline)
        try:
            self._acquire_rate_slot(token, deadline)
        except Exception:
            self._release_slot(slot_key, token)
            raise
        wait_seconds = max(0.0, self._clock() - started_at)
        if wait_seconds > 0:
            logger.info(
                "LLM 全局背压等待完成: wait_seconds=%.3f, max_concurrency=%s, rpm=%s",
                wait_seconds,
                self.max_concurrency,
                self.requests_per_minute,
            )
        return AdmissionPermit(token, slot_key, wait_seconds)

    async def acquire_async(self) -> AdmissionPermit:
        import asyncio

        return await asyncio.to_thread(self.acquire_sync)

    def _release_slot(self, slot_key: str, token: str) -> None:
        try:
            self.redis_client.eval(_RELEASE_SLOT_SCRIPT, 1, slot_key, token)
        except Exception as exc:
            logger.warning(
                "LLM 全局并发租约释放失败，将等待 TTL 自动回收: slot=%s, error=%s",
                slot_key,
                exc,
            )

    def release_sync(self, permit: Optional[AdmissionPermit]) -> None:
        if permit is not None:
            self._release_slot(permit.slot_key, permit.token)

    async def release_async(self, permit: Optional[AdmissionPermit]) -> None:
        import asyncio

        await asyncio.to_thread(self.release_sync, permit)


LLM_RATE_LIMITER = RequestRateLimiter(RESILIENCE_SETTINGS.requests_per_minute)


def _create_global_admission_controller(
    settings: ResilienceSettings,
) -> Optional[RedisAdmissionController]:
    if not settings.global_limit_enabled:
        return None

    import redis
    from common.config import GROUP_NAME, REDIS_URL

    redis_client = redis.Redis.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    )
    key_prefix = f"{settings.global_key_prefix}:{GROUP_NAME}"
    return RedisAdmissionController(
        redis_client,
        requests_per_minute=settings.global_requests_per_minute,
        max_concurrency=settings.global_max_concurrency,
        max_wait_seconds=settings.backpressure_max_wait_seconds,
        poll_seconds=settings.backpressure_poll_seconds,
        lease_seconds=settings.concurrency_lease_seconds,
        key_prefix=key_prefix,
    )


GLOBAL_LLM_ADMISSION = _create_global_admission_controller(RESILIENCE_SETTINGS)


def acquire_llm_slot_sync() -> Optional[AdmissionPermit]:
    """Apply the worker-local pacing and then the optional cluster-wide gate."""
    LLM_RATE_LIMITER.wait_sync()
    if GLOBAL_LLM_ADMISSION is None:
        return None
    return GLOBAL_LLM_ADMISSION.acquire_sync()


async def acquire_llm_slot_async() -> Optional[AdmissionPermit]:
    """Async counterpart that never sleeps on the event-loop thread."""
    await LLM_RATE_LIMITER.wait_async()
    if GLOBAL_LLM_ADMISSION is None:
        return None
    return await GLOBAL_LLM_ADMISSION.acquire_async()


def release_llm_slot_sync(permit: Optional[AdmissionPermit]) -> None:
    if GLOBAL_LLM_ADMISSION is not None:
        GLOBAL_LLM_ADMISSION.release_sync(permit)


async def release_llm_slot_async(permit: Optional[AdmissionPermit]) -> None:
    if GLOBAL_LLM_ADMISSION is not None:
        await GLOBAL_LLM_ADMISSION.release_async(permit)
