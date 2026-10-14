"""Dependency-layer image concurrency helpers.

The production default is deliberately serial.  Local validation or a future
deployment may opt in by setting ``IMAGE_GENERATION_CONCURRENCY`` (or a
stage-specific override) to a value greater than one.  Callers are responsible
for invoking this helper only after their input dependencies are complete.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import threading
import time
import uuid
from typing import Callable, Generic, Iterable, List, Optional, TypeVar


T = TypeVar("T")
R = TypeVar("R")

_AUDIT_WRITE_LOCK = threading.Lock()

_ACQUIRE_PROVIDER_SLOT = """
redis.call('zremrangebyscore', KEYS[1], '-inf', ARGV[1])
if redis.call('zcard', KEYS[1]) < tonumber(ARGV[3]) then
    redis.call('zadd', KEYS[1], ARGV[2], ARGV[4])
    redis.call('pexpire', KEYS[1], tonumber(ARGV[5]))
    return 1
end
return 0
"""
_REFRESH_PROVIDER_SLOT = """
if redis.call('zscore', KEYS[1], ARGV[2]) then
    redis.call('zadd', KEYS[1], ARGV[1], ARGV[2])
    redis.call('pexpire', KEYS[1], tonumber(ARGV[3]))
    return 1
end
return 0
"""
_RELEASE_PROVIDER_SLOT = "return redis.call('zrem', KEYS[1], ARGV[1])"


def _positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def resolve_image_concurrency(
    stage_env: str,
    explicit: Optional[int] = None,
    *,
    default: int = 1,
) -> int:
    """Resolve a bounded concurrency value without changing production defaults."""
    requested = explicit
    if requested is None:
        requested = _positive_int(
            os.getenv(stage_env, os.getenv("IMAGE_GENERATION_CONCURRENCY", str(default))),
            default,
        )
    else:
        requested = _positive_int(requested, default)
    maximum = _positive_int(os.getenv("IMAGE_GENERATION_MAX_CONCURRENCY", "64"), 64)
    return max(1, min(int(requested), maximum))


@contextmanager
def image_provider_slot(*, stage: str, task_id: str = ""):
    """Optionally enforce one Redis-backed image-provider budget across Pods.

    ``IMAGE_PROVIDER_GLOBAL_CONCURRENCY`` is opt-in so existing deployments are
    unchanged.  When enabled, char_designer and scene_painter share the same
    expiring semaphore instead of each independently opening the configured
    number of requests.
    """
    limit = _positive_int(
        os.getenv("IMAGE_PROVIDER_GLOBAL_CONCURRENCY", "0"), 0
    )
    if limit <= 0:
        yield
        return

    import redis
    from common.config import REDIS_URL

    key = os.getenv(
        "IMAGE_PROVIDER_GLOBAL_CONCURRENCY_KEY",
        "novel-game:image-provider:global-slots",
    ).strip() or "novel-game:image-provider:global-slots"
    wait_timeout = _positive_int(
        os.getenv("IMAGE_PROVIDER_SLOT_WAIT_SECONDS", "7200"), 7200
    )
    lease_seconds = _positive_int(
        os.getenv("IMAGE_PROVIDER_SLOT_LEASE_SECONDS", "1800"), 1800
    )
    poll_seconds = max(
        0.05,
        float(os.getenv("IMAGE_PROVIDER_SLOT_POLL_SECONDS", "0.2") or "0.2"),
    )
    token = f"{task_id or '-'}:{stage}:{os.getpid()}:{threading.get_ident()}:{uuid.uuid4().hex}"
    client = redis.Redis.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    )
    deadline = time.monotonic() + wait_timeout
    acquired = False
    while time.monotonic() < deadline:
        now_ms = int(time.time() * 1000)
        expiry_ms = now_ms + lease_seconds * 1000
        acquired = bool(
            client.eval(
                _ACQUIRE_PROVIDER_SLOT,
                1,
                key,
                now_ms,
                expiry_ms,
                limit,
                token,
                (lease_seconds + 60) * 1000,
            )
        )
        if acquired:
            break
        time.sleep(poll_seconds)
    if not acquired:
        raise TimeoutError(
            f"等待全局生图并发槽位超时: stage={stage} "
            f"task_id={task_id} limit={limit} timeout={wait_timeout}s"
        )
    logging.getLogger(__name__).info(
        "[全局生图并发] ACQUIRED stage=%s task_id=%s limit=%s",
        stage,
        task_id or "-",
        limit,
    )

    stop = threading.Event()

    def _heartbeat() -> None:
        interval = max(1.0, lease_seconds / 3)
        while not stop.wait(interval):
            expiry_ms = int(time.time() * 1000) + lease_seconds * 1000
            try:
                if not client.eval(
                    _REFRESH_PROVIDER_SLOT,
                    1,
                    key,
                    expiry_ms,
                    token,
                    (lease_seconds + 60) * 1000,
                ):
                    logging.getLogger(__name__).error(
                        "全局生图并发槽位已丢失: stage=%s task_id=%s",
                        stage,
                        task_id,
                    )
                    return
            except Exception:
                logging.getLogger(__name__).exception(
                    "全局生图并发槽位续租失败: stage=%s task_id=%s",
                    stage,
                    task_id,
                )

    heartbeat = threading.Thread(
        target=_heartbeat,
        name=f"image-slot-{stage[:24]}",
        daemon=True,
    )
    heartbeat.start()
    try:
        yield
    finally:
        stop.set()
        heartbeat.join(timeout=2)
        try:
            client.eval(_RELEASE_PROVIDER_SLOT, 1, key, token)
            logging.getLogger(__name__).info(
                "[全局生图并发] RELEASED stage=%s task_id=%s limit=%s",
                stage,
                task_id or "-",
                limit,
            )
        except Exception:
            logging.getLogger(__name__).exception(
                "释放全局生图并发槽位失败: stage=%s task_id=%s",
                stage,
                task_id,
            )


def _audit_event(payload: dict) -> None:
    """Append one optional local JSONL audit event.

    No file is created unless ``IMAGE_CONCURRENCY_AUDIT_LOG`` is explicitly
    configured by the runtime.  This keeps K8s behaviour unchanged.
    """
    raw_path = str(os.getenv("IMAGE_CONCURRENCY_AUDIT_LOG", "") or "").strip()
    if not raw_path:
        return
    path = Path(raw_path).expanduser()
    payload = {
        "timestamp": datetime.now().astimezone().isoformat(),
        "pid": os.getpid(),
        "thread_id": threading.get_ident(),
        **payload,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
        with _AUDIT_WRITE_LOCK:
            with path.open("a", encoding="utf-8") as file_obj:
                file_obj.write(line)
                file_obj.flush()
    except OSError:
        logging.getLogger(__name__).warning(
            "并发生图审计日志写入失败: %s", path, exc_info=True
        )


def run_dependency_layer(
    items: Iterable[T],
    worker: Callable[[T], R],
    *,
    concurrency: int,
    stage: str,
    task_id: str = "",
    label: Optional[Callable[[T], str]] = None,
    logger: Optional[logging.Logger] = None,
) -> List[R]:
    """Run one dependency-complete image layer and preserve input result order."""
    work_items = list(items)
    if not work_items:
        return []

    resolved = max(1, min(int(concurrency), len(work_items)))
    log = logger or logging.getLogger(__name__)
    log.info(
        "[并发生图] stage=%s task_id=%s jobs=%s concurrency=%s",
        stage,
        task_id or "-",
        len(work_items),
        resolved,
    )
    _audit_event(
        {
            "event": "layer_started",
            "stage": stage,
            "task_id": task_id,
            "jobs": len(work_items),
            "concurrency": resolved,
        }
    )

    def _run(index: int, item: T) -> tuple[int, R]:
        item_label = label(item) if label else str(index + 1)
        started = time.monotonic()
        log.info(
            "[并发生图] START stage=%s job=%s index=%s/%s",
            stage,
            item_label,
            index + 1,
            len(work_items),
        )
        _audit_event(
            {
                "event": "job_started",
                "stage": stage,
                "task_id": task_id,
                "job": item_label,
                "index": index + 1,
                "total": len(work_items),
            }
        )
        try:
            with image_provider_slot(stage=stage, task_id=task_id):
                result = worker(item)
        except BaseException as exc:
            elapsed = time.monotonic() - started
            log.exception(
                "[并发生图] ERROR stage=%s job=%s elapsed=%.3fs",
                stage,
                item_label,
                elapsed,
            )
            _audit_event(
                {
                    "event": "job_failed",
                    "stage": stage,
                    "task_id": task_id,
                    "job": item_label,
                    "elapsed_seconds": round(elapsed, 6),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
            raise
        elapsed = time.monotonic() - started
        log.info(
            "[并发生图] DONE stage=%s job=%s elapsed=%.3fs",
            stage,
            item_label,
            elapsed,
        )
        _audit_event(
            {
                "event": "job_completed",
                "stage": stage,
                "task_id": task_id,
                "job": item_label,
                "elapsed_seconds": round(elapsed, 6),
            }
        )
        return index, result

    started = time.monotonic()
    ordered: List[Optional[R]] = [None] * len(work_items)
    if resolved == 1:
        for index, item in enumerate(work_items):
            result_index, result = _run(index, item)
            ordered[result_index] = result
    else:
        with ThreadPoolExecutor(
            max_workers=resolved,
            thread_name_prefix=f"image-{stage[:24]}",
        ) as executor:
            futures = {
                executor.submit(_run, index, item): index
                for index, item in enumerate(work_items)
            }
            for future in as_completed(futures):
                result_index, result = future.result()
                ordered[result_index] = result

    elapsed = time.monotonic() - started
    log.info(
        "[并发生图] layer complete stage=%s task_id=%s jobs=%s "
        "concurrency=%s elapsed=%.3fs",
        stage,
        task_id or "-",
        len(work_items),
        resolved,
        elapsed,
    )
    _audit_event(
        {
            "event": "layer_completed",
            "stage": stage,
            "task_id": task_id,
            "jobs": len(work_items),
            "concurrency": resolved,
            "elapsed_seconds": round(elapsed, 6),
        }
    )
    return [item for item in ordered]  # type: ignore[misc]


class ThreadLocalResource(Generic[R]):
    """Create one mutable Provider/resource instance per worker thread."""

    def __init__(self, factory: Callable[[], R], initial: Optional[R] = None):
        self._factory = factory
        self._local = threading.local()
        if initial is not None:
            self._local.value = initial

    def get(self) -> R:
        value = getattr(self._local, "value", None)
        if value is None:
            value = self._factory()
            self._local.value = value
        return value


__all__ = [
    "ThreadLocalResource",
    "image_provider_slot",
    "resolve_image_concurrency",
    "run_dependency_layer",
]
