"""
全局 API 用量追踪器。

设计目标：
- LLM：每次成功响应后立即 upsert，尽量避免进程崩溃造成 token 丢失。
- image / tts / music：线程安全地批量累积，并按数量/时间阈值周期落库。
- 计数器按 (task_id, service, run_id) 隔离，切换上下文不会串任务。
- 各供应商 usage 先归一化，再写入统一字段。
"""
import json
import logging
import os
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, Iterator, Mapping, Optional

from sqlmodel import SQLModel, Field
from sqlalchemy import UniqueConstraint, text

# 添加项目根目录到路径（兼容各服务的不同工作目录）
_this_dir = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.dirname(_this_dir)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# api_usage 是跨模块共用表,读写走平台库(agent_platform)
from common.platform_db import platform_engine
from common.config import GROUP_NAME, now_local

logger = logging.getLogger(__name__)
_engine = platform_engine
_table_init_lock = threading.Lock()
_table_initialized = False


def _short_error(exc: Any, limit: int = 300) -> str:
    """将数据库异常压成单行，业务日志无需打印 SQLAlchemy 的整段说明。"""
    text_value = " ".join(str(exc).split())
    return text_value if len(text_value) <= limit else text_value[:limit - 3] + "..."


class UsageBudgetExceeded(RuntimeError):
    """A task crossed a configured provider-usage safety budget."""


def _budget_limit(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default))))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


# 默认值刻意留得较宽，只负责兜住死循环/异常重复，不替代业务侧精确计费。
_USAGE_BUDGETS = {
    "total_tokens": _budget_limit("TASK_MAX_TOTAL_TOKENS", 20_000_000),
    "call_count": _budget_limit("TASK_MAX_API_CALLS", 5_000),
    "image_count": _budget_limit("TASK_MAX_IMAGES", 2_000),
    "tts_chars": _budget_limit("TASK_MAX_TTS_CHARS", 20_000_000),
    "music_count": _budget_limit("TASK_MAX_MUSIC", 500),
}


def enforce_task_usage_budget(task_id: str) -> None:
    """Fail closed after recorded usage crosses a task-level safety ceiling.

    A zero limit disables that dimension. The check runs after usage is persisted,
    so one in-flight request may overshoot slightly, but the next pipeline action
    stops instead of looping indefinitely and continuing to spend provider quota.
    """
    enabled = {key: value for key, value in _USAGE_BUDGETS.items() if value > 0}
    if not task_id or not enabled:
        return
    try:
        with _engine.connect() as conn:
            row = conn.execute(text("""
                SELECT
                    COALESCE(SUM(total_tokens), 0),
                    COALESCE(SUM(call_count), 0),
                    COALESCE(SUM(image_count), 0),
                    COALESCE(SUM(tts_chars), 0),
                    COALESCE(SUM(music_count), 0)
                FROM public.api_usage
                WHERE task_id = :task_id
            """), {"task_id": task_id}).first()
    except Exception as exc:
        logger.warning(
            "[UsageTracker] 用量预算查询暂不可用，跳过本次检查: task=%s error=%s: %s",
            task_id, type(exc).__name__, _short_error(exc),
        )
        return

    names = ("total_tokens", "call_count", "image_count", "tts_chars", "music_count")
    totals = dict(zip(names, [int(value or 0) for value in row]))
    exceeded = [
        f"{name}={totals[name]}>{enabled[name]}"
        for name in enabled
        if totals[name] > enabled[name]
    ]
    if exceeded:
        raise UsageBudgetExceeded(
            f"usage budget exceeded for task {task_id}: {', '.join(exceeded)}"
        )


# ==================== 数据库表模型 ====================

class ApiUsage(SQLModel, table=True):
    __tablename__ = "api_usage"
    __table_args__ = (
        UniqueConstraint(
            "task_id", "service", "api_type", "model", "run_id", "group_name",
            name="uq_api_usage_scope",
        ),
        {"schema": "public"},
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: str = Field(index=True, nullable=False)
    service: str = Field(nullable=False)
    api_type: str = Field(nullable=False)  # llm / image / tts / music
    provider: str = Field(default="", nullable=False)
    model: str = Field(default="", nullable=False)
    prompt_tokens: int = Field(default=0)
    completion_tokens: int = Field(default=0)
    cached_input_tokens: int = Field(default=0)
    cache_write_tokens: int = Field(default=0)
    reasoning_tokens: int = Field(default=0)
    total_tokens: int = Field(default=0)
    call_count: int = Field(default=0)
    image_count: int = Field(default=0)
    tts_chars: int = Field(default=0)
    music_count: int = Field(default=0)
    music_seconds: int = Field(default=0)
    # 北京时间，理由同 platform_task_models：容器漏配 TZ 时 datetime.now() 会写 UTC。
    # 注意本文件的裸 SQL 用的是 DB 的 now()，库的 TimeZone 已是 Asia/Shanghai，无需改。
    created_at: datetime = Field(default_factory=now_local)
    run_id: str = Field(default="", nullable=False)
    # 执行方组名：这一步是谁的 worker 干的，排障维度
    group_name: str = Field(default="group-ref", nullable=False, index=True)
    # 任务用的管线名：用量的业务口径。各组只知道自己是谁，不知道任务选了哪条管线，
    # 所以这一列由中台在回调时统一回填（见 game-platform orchestrator._stamp_pipeline_name）
    pipeline_name: Optional[str] = Field(default=None, index=True)


def init_usage_table() -> bool:
    """创建新表；瞬时连库失败只告警，后续写入会继续重试初始化。"""
    global _table_initialized
    if _table_initialized:
        return True
    with _table_init_lock:
        if _table_initialized:
            return True
        try:
            SQLModel.metadata.create_all(_engine, tables=[ApiUsage.__table__])
            _table_initialized = True
            return True
        except Exception as exc:
            logger.warning(
                "[UsageTracker] api_usage 表初始化暂不可用，稍后重试: %s: %s",
                type(exc).__name__, _short_error(exc),
            )
            return False


# ==================== usage 归一化 ====================

@dataclass(frozen=True)
class NormalizedLLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    raw_usage: Optional[Dict[str, Any]] = None


def _as_non_negative_int(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(parsed, 0)


def _usage_to_dict(usage: Any) -> Dict[str, Any]:
    if usage is None:
        return {}
    if isinstance(usage, Mapping):
        return dict(usage)
    for method_name in ("model_dump", "to_dict", "dict"):
        method = getattr(usage, method_name, None)
        if callable(method):
            try:
                value = method()
                if isinstance(value, Mapping):
                    return dict(value)
            except Exception:
                pass
    try:
        return {
            key: value
            for key, value in vars(usage).items()
            if not key.startswith("_")
        }
    except (TypeError, AttributeError):
        return {}


def _nested_get(data: Mapping[str, Any], *paths: str) -> Any:
    for path in paths:
        current: Any = data
        found = True
        for part in path.split("."):
            if isinstance(current, Mapping) and part in current:
                current = current[part]
            else:
                found = False
                break
        if found and current is not None:
            return current
    return 0


def normalize_llm_usage(usage: Any, provider: Optional[str] = None) -> NormalizedLLMUsage:
    """
    将 OpenAI compatible、DashScope、Gemini、Anthropic usage 转成统一口径。

    output_tokens 表示供应商返回的全部输出 token。reasoning_tokens 是其中的
    明细，不再无条件与 output_tokens 相加，以避免推理 token 重复计算。
    """
    raw = _usage_to_dict(usage)
    provider_key = (provider or "").lower()

    input_tokens = _as_non_negative_int(_nested_get(
        raw, "prompt_tokens", "input_tokens", "promptTokenCount",
    ))
    output_tokens = _as_non_negative_int(_nested_get(
        raw, "completion_tokens", "output_tokens", "candidatesTokenCount",
    ))
    cached_input_tokens = _as_non_negative_int(_nested_get(
        raw,
        "prompt_tokens_details.cached_tokens",
        "input_tokens_details.cached_tokens",
        "cache_read_input_tokens",
        "cachedContentTokenCount",
    ))
    cache_write_tokens = _as_non_negative_int(_nested_get(
        raw, "cache_creation_input_tokens",
    ))
    reasoning_tokens = _as_non_negative_int(_nested_get(
        raw,
        "completion_tokens_details.reasoning_tokens",
        "output_tokens_details.reasoning_tokens",
        "reasoning_tokens",
        "thoughtsTokenCount",
    ))
    total_tokens = _as_non_negative_int(_nested_get(
        raw, "total_tokens", "totalTokenCount",
    ))

    # Gemini 的 candidatesTokenCount 通常不含 thoughtsTokenCount；totalTokenCount
    # 是更可靠的总量。DashScope/OpenAI 的 total 也可用于防止字段版本差异。
    derived_output = max(total_tokens - input_tokens, 0) if total_tokens else 0
    if derived_output and (
        provider_key in {"gemini", "google", "dashscope", "qwen", "openai", "openai_compatible"}
        or output_tokens == 0
    ):
        output_tokens = max(output_tokens, derived_output)

    # Anthropic 的缓存读写 token 是独立计费输入，input_tokens 不一定包含它们。
    if not total_tokens:
        total_tokens = (
            input_tokens + cached_input_tokens + cache_write_tokens + output_tokens
        )

    return NormalizedLLMUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
        cache_write_tokens=cache_write_tokens,
        reasoning_tokens=reasoning_tokens,
        total_tokens=total_tokens,
        raw_usage=raw or None,
    )


# ==================== 追踪器 ====================

@dataclass(frozen=True)
class UsageContext:
    task_id: str
    service: str
    run_id: str


class UsageTracker:
    """进程级用量追踪器；当前生产入口按“一进程同一时刻一个 task”使用。"""

    def __init__(
        self,
        auto_flush_count: Optional[int] = None,
        auto_flush_interval: Optional[float] = None,
    ):
        self._lock = threading.RLock()
        self._db_write_lock = threading.Lock()
        self._context: Optional[UsageContext] = None
        # key: (task_id, service, run_id, api_type, provider, model)
        self._counters: Dict[tuple, Dict[str, int]] = {}
        self._pending_calls = 0
        self._last_flush_at = time.monotonic()
        self._db_retry_after = 0.0
        self._db_failure_cooldown = max(
            1.0,
            float(os.getenv("USAGE_DB_FAILURE_COOLDOWN_SECONDS", "30")),
        )
        self._auto_flush_count = (
            int(os.getenv("USAGE_AUTO_FLUSH_COUNT", "50"))
            if auto_flush_count is None else auto_flush_count
        )
        self._auto_flush_interval = (
            float(os.getenv("USAGE_AUTO_FLUSH_SECONDS", "10"))
            if auto_flush_interval is None else auto_flush_interval
        )

    def set_context(
        self,
        task_id: str,
        service: str,
        *,
        force_new_run: bool = False,
        run_id: Optional[str] = None,
    ) -> str:
        """设置任务上下文；同一任务/服务的嵌套调用默认复用 run_id。"""
        if not task_id or not service:
            raise ValueError("task_id and service are required for usage tracking")
        # 平台发起下一次业务重试时，先检查之前 attempts 已经产生的累计用量，
        # 避免超过预算的任务又进入新一轮模型调用。
        enforce_task_usage_budget(task_id)
        with self._lock:
            if (
                self._context
                and self._context.task_id == task_id
                and self._context.service == service
                and not force_new_run
                and not run_id
            ):
                return self._context.run_id
            resolved_run_id = run_id or uuid.uuid4().hex
            self._context = UsageContext(task_id, service, resolved_run_id)
            return resolved_run_id

    def clear_context(self, run_id: Optional[str] = None) -> None:
        """清除当前上下文；传 run_id 时只清理匹配的运行。"""
        with self._lock:
            if run_id and self._context and self._context.run_id != run_id:
                return
            self._context = None

    @contextmanager
    def context(
        self,
        task_id: str,
        service: str,
        *,
        run_id: Optional[str] = None,
    ) -> Iterator["UsageTracker"]:
        """任务入口推荐用法：自动创建新 run，退出时 flush 并清理上下文。"""
        resolved_run_id = self.set_context(
            task_id, service, force_new_run=True, run_id=run_id,
        )
        try:
            yield self
        finally:
            self.flush(run_id=resolved_run_id)
            self.clear_context(resolved_run_id)

    def _current_context(self) -> Optional[UsageContext]:
        with self._lock:
            return self._context

    def _upsert_llm(
        self,
        context: UsageContext,
        model: str,
        provider: str,
        usage: NormalizedLLMUsage,
    ) -> bool:
        with self._lock:
            cooling_down = time.monotonic() < self._db_retry_after
        if cooling_down:
            self._accumulate_llm(context, model, provider, usage)
            return False

        # 多线程翻译可能同时返回。只让一个线程探测/写入数据库，其余先缓存，
        # 避免平台库断开时多个连接同时等待 connect_timeout。
        if not self._db_write_lock.acquire(blocking=False):
            self._accumulate_llm(context, model, provider, usage)
            return False

        try:
            if not init_usage_table():
                self._accumulate_llm(context, model, provider, usage)
                self._mark_db_failure("api_usage 表暂不可用")
                return False
            with _engine.begin() as conn:
                conn.execute(text("""
                    INSERT INTO public.api_usage AS current_usage (
                        task_id, service, api_type, provider, model,
                        prompt_tokens, completion_tokens, cached_input_tokens,
                        cache_write_tokens, reasoning_tokens, total_tokens, call_count,
                        image_count, tts_chars, music_count, music_seconds,
                        run_id, group_name, created_at
                    ) VALUES (
                        :task_id, :service, 'llm', :provider, :model,
                        :prompt, :completion, :cached_input, :cache_write,
                        :reasoning, :total, 1, 0, 0, 0, :music_seconds,
                        :run_id, :group_name, now()
                    )
                    ON CONFLICT (task_id, service, api_type, model, run_id, group_name)
                    DO UPDATE SET
                        provider = CASE
                            WHEN current_usage.provider = '' THEN EXCLUDED.provider
                            ELSE current_usage.provider
                        END,
                        prompt_tokens = current_usage.prompt_tokens + EXCLUDED.prompt_tokens,
                        completion_tokens = current_usage.completion_tokens + EXCLUDED.completion_tokens,
                        cached_input_tokens = current_usage.cached_input_tokens + EXCLUDED.cached_input_tokens,
                        cache_write_tokens = current_usage.cache_write_tokens + EXCLUDED.cache_write_tokens,
                        reasoning_tokens = current_usage.reasoning_tokens + EXCLUDED.reasoning_tokens,
                        total_tokens = current_usage.total_tokens + EXCLUDED.total_tokens,
                        music_seconds = current_usage.music_seconds + EXCLUDED.music_seconds,
                        call_count = current_usage.call_count + 1
                """), {
                    "task_id": context.task_id,
                    "service": context.service,
                    "provider": provider or "",
                    "model": model or "",
                    "prompt": usage.input_tokens,
                    "completion": usage.output_tokens,
                    "cached_input": usage.cached_input_tokens,
                    "cache_write": usage.cache_write_tokens,
                    "reasoning": usage.reasoning_tokens,
                    "total": usage.total_tokens,
                    "music_seconds": 0,
                    "run_id": context.run_id,
                    "group_name": GROUP_NAME,
                })
        except Exception as exc:
            self._accumulate_llm(context, model, provider, usage)
            self._mark_db_failure(exc)
            return False
        finally:
            self._db_write_lock.release()
        enforce_task_usage_budget(context.task_id)
        return True

    @staticmethod
    def _empty_counter() -> Dict[str, int]:
        return {
            "prompt": 0,
            "completion": 0,
            "cached_input": 0,
            "cache_write": 0,
            "reasoning": 0,
            "total": 0,
            "image": 0,
            "tts": 0,
            "music": 0,
            "music_seconds": 0,
            "calls": 0,
        }

    def _accumulate_llm(
        self,
        context: UsageContext,
        model: str,
        provider: str,
        usage: NormalizedLLMUsage,
    ) -> None:
        key = (
            context.task_id,
            context.service,
            context.run_id,
            "llm",
            provider or "",
            model or "",
        )
        with self._lock:
            counter = self._counters.setdefault(key, self._empty_counter())
            counter["prompt"] += usage.input_tokens
            counter["completion"] += usage.output_tokens
            counter["cached_input"] += usage.cached_input_tokens
            counter["cache_write"] += usage.cache_write_tokens
            counter["reasoning"] += usage.reasoning_tokens
            counter["total"] += usage.total_tokens
            counter["calls"] += 1
            self._pending_calls += 1

    def _mark_db_failure(self, exc: Any) -> None:
        with self._lock:
            already_cooling_down = time.monotonic() < self._db_retry_after
            self._db_retry_after = time.monotonic() + self._db_failure_cooldown
            pending_calls = self._pending_calls
        if not already_cooling_down:
            logger.warning(
                "[UsageTracker] 用量库暂不可用，已缓存 %s 次调用；未来 %ss "
                "不再阻塞业务请求: %s: %s",
                pending_calls,
                int(self._db_failure_cooldown),
                type(exc).__name__,
                _short_error(exc),
            )

    def add_llm(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        model: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> bool:
        context = self._current_context()
        if not context:
            logger.warning(
                "[UsageTracker] dropped LLM usage because context is missing: model=%s",
                model or "",
            )
            return False
        normalized = NormalizedLLMUsage(
            input_tokens=_as_non_negative_int(prompt_tokens),
            output_tokens=_as_non_negative_int(completion_tokens),
            total_tokens=(
                _as_non_negative_int(prompt_tokens)
                + _as_non_negative_int(completion_tokens)
            ),
        )
        return self._upsert_llm(context, model or "", provider or "", normalized)

    def add_llm_usage(
        self,
        usage: Any,
        model: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> bool:
        context = self._current_context()
        if not context:
            logger.warning(
                "[UsageTracker] dropped LLM usage because context is missing: model=%s provider=%s",
                model or "", provider or "",
            )
            return False
        normalized = normalize_llm_usage(usage, provider=provider)
        return self._upsert_llm(context, model or "", provider or "", normalized)

    def _accumulate(
        self,
        api_type: str,
        model: Optional[str],
        provider: Optional[str],
        *,
        image_count: int = 0,
        tts_chars: int = 0,
        music_count: int = 0,
        music_seconds: int = 0,
    ) -> bool:
        should_flush = False
        with self._lock:
            context = self._context
            if not context:
                logger.warning(
                    "[UsageTracker] dropped %s usage because context is missing: model=%s",
                    api_type, model or "",
                )
                return False
            key = (
                context.task_id,
                context.service,
                context.run_id,
                api_type,
                provider or "",
                model or "",
            )
            counter = self._counters.setdefault(key, self._empty_counter())
            counter["image"] += _as_non_negative_int(image_count)
            counter["tts"] += _as_non_negative_int(tts_chars)
            counter["music"] += _as_non_negative_int(music_count)
            counter["music_seconds"] += _as_non_negative_int(music_seconds)
            counter["calls"] += 1
            self._pending_calls += 1

            elapsed = time.monotonic() - self._last_flush_at
            should_flush = (
                (self._auto_flush_count > 0 and self._pending_calls >= self._auto_flush_count)
                or (self._auto_flush_interval > 0 and elapsed >= self._auto_flush_interval)
            )
        if should_flush:
            self.flush()
        return True

    def add_image(
        self,
        count: int = 1,
        model: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> bool:
        return self._accumulate(
            "image", model, provider, image_count=count,
        )

    def add_tts(
        self,
        char_count: int,
        model: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> bool:
        return self._accumulate(
            "tts", model, provider, tts_chars=char_count,
        )

    def add_music(
        self,
        count: int = 1,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        duration_seconds: int = 0,
    ) -> bool:
        return self._accumulate(
            "music", model, provider,
            music_count=count,
            music_seconds=duration_seconds,
        )

    def _merge_failed_batch(self, batch: Dict[tuple, Dict[str, int]]) -> None:
        with self._lock:
            for key, values in batch.items():
                counter = self._counters.setdefault(key, self._empty_counter())
                for field in self._empty_counter():
                    counter[field] += values[field]
                self._pending_calls += values["calls"]

    def flush(self, run_id: Optional[str] = None) -> bool:
        """原子取出待写批次；写入失败时重新合并，避免无声丢数据。"""
        with self._lock:
            if run_id:
                batch = {
                    key: value
                    for key, value in self._counters.items()
                    if key[2] == run_id
                }
                for key in batch:
                    del self._counters[key]
            else:
                batch = self._counters
                self._counters = {}
            if not batch:
                self._last_flush_at = time.monotonic()
                return True
            batch_calls = sum(item["calls"] for item in batch.values())
            self._pending_calls = max(self._pending_calls - batch_calls, 0)

        try:
            if not init_usage_table():
                self._merge_failed_batch(batch)
                self._mark_db_failure("api_usage 表暂不可用")
                return False
            with _engine.begin() as conn:
                for key, counter in batch.items():
                    task_id, service, item_run_id, api_type, provider, model = key
                    conn.execute(text("""
                        INSERT INTO public.api_usage AS current_usage (
                            task_id, service, api_type, provider, model,
                            prompt_tokens, completion_tokens, cached_input_tokens,
                            cache_write_tokens, reasoning_tokens, total_tokens, call_count,
                            image_count, tts_chars, music_count, music_seconds,
                            run_id, group_name, created_at
                        ) VALUES (
                            :task_id, :service, :api_type, :provider, :model,
                            :prompt, :completion, :cached_input, :cache_write,
                            :reasoning, :total, :calls,
                            :image, :tts, :music, :music_seconds, :run_id, :group_name, now()
                        )
                        ON CONFLICT (task_id, service, api_type, model, run_id, group_name)
                        DO UPDATE SET
                            provider = CASE
                                WHEN current_usage.provider = '' THEN EXCLUDED.provider
                            ELSE current_usage.provider
                        END,
                            prompt_tokens = current_usage.prompt_tokens + EXCLUDED.prompt_tokens,
                            completion_tokens = current_usage.completion_tokens + EXCLUDED.completion_tokens,
                            cached_input_tokens = current_usage.cached_input_tokens + EXCLUDED.cached_input_tokens,
                            cache_write_tokens = current_usage.cache_write_tokens + EXCLUDED.cache_write_tokens,
                            reasoning_tokens = current_usage.reasoning_tokens + EXCLUDED.reasoning_tokens,
                            total_tokens = current_usage.total_tokens + EXCLUDED.total_tokens,
                            image_count = current_usage.image_count + EXCLUDED.image_count,
                            tts_chars = current_usage.tts_chars + EXCLUDED.tts_chars,
                            music_count = current_usage.music_count + EXCLUDED.music_count,
                            music_seconds = current_usage.music_seconds + EXCLUDED.music_seconds,
                            call_count = current_usage.call_count + EXCLUDED.call_count
                    """), {
                        "task_id": task_id,
                        "service": service,
                        "api_type": api_type,
                        "provider": provider,
                        "model": model,
                        "prompt": counter["prompt"],
                        "completion": counter["completion"],
                        "cached_input": counter["cached_input"],
                        "cache_write": counter["cache_write"],
                        "reasoning": counter["reasoning"],
                        "total": counter["total"],
                        "calls": counter["calls"],
                        "image": counter["image"],
                        "tts": counter["tts"],
                        "music": counter["music"],
                        "music_seconds": counter["music_seconds"],
                        "run_id": item_run_id,
                        "group_name": GROUP_NAME,
                    })
            with self._lock:
                self._last_flush_at = time.monotonic()
            logger.info("[UsageTracker] flushed %s aggregate records", len(batch))
        except Exception as exc:
            self._merge_failed_batch(batch)
            self._mark_db_failure(exc)
            return False

        with self._lock:
            self._db_retry_after = 0.0
        # flush 的职责只是尽力落库，不能在业务步骤已经完成后再以预算异常
        # 反向判定该步骤失败。预算超限仍会在下一次 set_context 时检查；这里
        # 只记录告警并正常返回，保证用量统计永远不是业务流水线的中断点。
        for task_id in {key[0] for key in batch}:
            try:
                enforce_task_usage_budget(task_id)
            except Exception as exc:
                logger.warning(
                    "[UsageTracker] flush 后预算检查失败，仅记录不影响业务: "
                    "task=%s error=%s: %s",
                    task_id, type(exc).__name__, _short_error(exc),
                )
        return True

    def debug_snapshot(self) -> Dict[str, Any]:
        """仅用于诊断和测试，不包含 API 密钥或提示词。"""
        with self._lock:
            return {
                "context": asdict(self._context) if self._context else None,
                "pending_calls": self._pending_calls,
                "db_cooldown_remaining": max(
                    self._db_retry_after - time.monotonic(), 0.0
                ),
                "counters": {
                    json.dumps(key, ensure_ascii=False): dict(value)
                    for key, value in self._counters.items()
                },
            }


# 全局单例
tracker = UsageTracker()
