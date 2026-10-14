"""Structured runtime telemetry and heartbeats for long chapter_branch jobs."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
import threading
import time
from typing import Any, Callable, Iterator, Optional


logger = logging.getLogger(__name__)
_SENSITIVE_FIELD_MARKERS = (
    "api_key",
    "authorization",
    "prompt",
    "content",
    "novel_text",
    "md_content",
    "secret",
    "token",
)
_heartbeat_guard = threading.Lock()
_active_heartbeats: dict[str, "RuntimeHeartbeat"] = {}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _safe_fields(fields: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in fields.items():
        normalized_key = str(key).lower()
        if any(marker in normalized_key for marker in _SENSITIVE_FIELD_MARKERS):
            continue
        if isinstance(value, dict):
            safe[key] = _safe_fields(value)
        elif isinstance(value, (list, tuple)):
            safe[key] = [
                _safe_fields(item) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            safe[key] = value
    return safe


def structured_log(
    target_logger: logging.Logger,
    event: str,
    *,
    level: str = "info",
    **fields: Any,
) -> dict[str, Any]:
    """Write one JSON log record without prompts, prose, credentials or tokens."""
    payload = {
        "timestamp": _utcnow().isoformat(),
        "event": str(event),
        **_safe_fields(fields),
    }
    log_method = getattr(target_logger, level.lower(), target_logger.info)
    log_method(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
    return payload


@contextmanager
def phase_timer(
    metrics: dict[str, Any],
    phase: str,
    *,
    logger: logging.Logger = logger,
    log_fields: Optional[dict[str, Any]] = None,
    clock: Callable[[], float] = time.perf_counter,
) -> Iterator[None]:
    """Record phase duration and emit started/completed/failed events."""
    fields = dict(log_fields or {})
    started = clock()
    structured_log(logger, "phase_started", phase=phase, **fields)
    try:
        yield
    except Exception as exc:
        duration = round(clock() - started, 3)
        metrics.setdefault("phase_seconds", {})[phase] = duration
        structured_log(
            logger,
            "phase_failed",
            level="error",
            phase=phase,
            duration_seconds=duration,
            error_category=classify_runtime_error(exc),
            error_type=type(exc).__name__,
            **fields,
        )
        raise
    else:
        duration = round(clock() - started, 3)
        metrics.setdefault("phase_seconds", {})[phase] = duration
        structured_log(
            logger,
            "phase_completed",
            phase=phase,
            duration_seconds=duration,
            **fields,
        )


def classify_runtime_error(exc: Exception) -> str:
    """Return a stable operational category without serializing exception details."""
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    if "stale" in name or "旧运行" in message:
        return "stale_publication"
    if "timeout" in name or "超时" in message:
        return "timeout"
    if (
        "质量门禁" in message
        or "完整性校验" in message
        or "一致性门禁" in message
    ):
        return "release_gate"
    if "401" in message or "api key" in message or "authentication" in message:
        return "authentication"
    if "429" in message or "rate limit" in message or "限流" in message:
        return "rate_limit"
    if isinstance(exc, (ConnectionError, OSError)):
        return "dependency"
    return "unexpected"


class RuntimeHeartbeat:
    """Periodically persist node progress and renew the story_planner lease."""

    def __init__(
        self,
        *,
        task_id: str,
        run_id: str,
        branch_type: str,
        topo_id: str,
        real_id: str,
        interval_seconds: int,
        plan_updater: Callable[[dict[str, Any]], Any],
        lease_renewer: Callable[[str, str], Any],
        logger: logging.Logger = logger,
        clock: Callable[[], float] = time.perf_counter,
        utcnow: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.task_id = task_id
        self.run_id = run_id
        self.branch_type = branch_type
        self.topo_id = topo_id
        self.real_id = real_id
        self.interval_seconds = max(1, int(interval_seconds))
        self.plan_updater = plan_updater
        self.lease_renewer = lease_renewer
        self.logger = logger
        self.clock = clock
        self.utcnow = utcnow
        self._started_clock = clock()
        self._started_at = utcnow().isoformat()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def beat(self, *, status: str = "running") -> dict[str, Any]:
        payload = {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "branch_type": self.branch_type,
            "topo_id": self.topo_id,
            "real_id": self.real_id,
            "stage": "story_planner",
            "status": status,
            "started_at": self._started_at,
            "heartbeat_at": self.utcnow().isoformat(),
            "elapsed_seconds": round(self.clock() - self._started_clock, 3),
        }
        try:
            self.lease_renewer(self.task_id, "story_planner")
            self.plan_updater(payload)
            structured_log(self.logger, "node_heartbeat", **payload)
        except Exception as exc:
            structured_log(
                self.logger,
                "node_heartbeat_failed",
                level="warning",
                task_id=self.task_id,
                run_id=self.run_id,
                branch_type=self.branch_type,
                topo_id=self.topo_id,
                real_id=self.real_id,
                error_category=classify_runtime_error(exc),
                error_type=type(exc).__name__,
            )
        return payload

    def start(self) -> "RuntimeHeartbeat":
        self.beat(status="running")

        def _run() -> None:
            while not self._stop.wait(self.interval_seconds):
                self.beat(status="running")

        self._thread = threading.Thread(
            target=_run,
            name=f"chapter-branch-heartbeat-{self.task_id[:24]}",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self, *, final_status: str = "completed") -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=min(2.0, float(self.interval_seconds)))
        self.beat(status=final_status)


def start_runtime_heartbeat(**kwargs: Any) -> RuntimeHeartbeat:
    heartbeat = RuntimeHeartbeat(**kwargs)
    with _heartbeat_guard:
        previous = _active_heartbeats.pop(heartbeat.task_id, None)
        _active_heartbeats[heartbeat.task_id] = heartbeat
    if previous:
        previous.stop(final_status="replaced")
    return heartbeat.start()


def stop_runtime_heartbeat(task_id: str, *, final_status: str = "completed") -> None:
    with _heartbeat_guard:
        heartbeat = _active_heartbeats.pop(task_id, None)
    if heartbeat:
        heartbeat.stop(final_status=final_status)


def stop_all_runtime_heartbeats() -> None:
    with _heartbeat_guard:
        items = list(_active_heartbeats.values())
        _active_heartbeats.clear()
    for heartbeat in items:
        heartbeat.stop(final_status="stopped")


def detect_stalled_node(
    topo_id: str,
    node_state: dict[str, Any],
    *,
    now: Optional[datetime] = None,
    timeout_seconds: int,
) -> Optional[dict[str, Any]]:
    """Return a structured stall issue for active nodes whose heartbeat is too old."""
    if not isinstance(node_state, dict) or node_state.get("status") not in {
        "running",
        "persisting",
    }:
        return None
    raw_heartbeat = (
        node_state.get("heartbeat_at")
        or node_state.get("started_at")
        or node_state.get("updated_at")
    )
    if not raw_heartbeat:
        return None
    try:
        heartbeat_at = datetime.fromisoformat(str(raw_heartbeat).replace("Z", "+00:00"))
    except ValueError:
        return {
            "error_category": "invalid_heartbeat",
            "topo_id": topo_id,
            "heartbeat_at": str(raw_heartbeat),
        }
    if heartbeat_at.tzinfo is None:
        heartbeat_at = heartbeat_at.replace(tzinfo=timezone.utc)
    current = now or _utcnow()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    stalled_seconds = max(0, int((current - heartbeat_at).total_seconds()))
    if stalled_seconds <= max(1, int(timeout_seconds)):
        return None
    return {
        "error_category": "node_stalled",
        "topo_id": topo_id,
        "real_id": node_state.get("real_id", ""),
        "heartbeat_at": heartbeat_at.isoformat(),
        "stalled_seconds": stalled_seconds,
        "timeout_seconds": max(1, int(timeout_seconds)),
    }
