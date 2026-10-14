"""Transactional outbox publisher and crash recovery.

The orchestrator transaction only records a stable job envelope.  This module
leases pending rows, publishes them with RabbitMQ confirms, and *then* moves the
step from ``publishing`` to ``dispatched``.  A process death anywhere between
publish and the final DB commit can produce a duplicate, but can no longer lose
the job; the same message/idempotency id is reused on every attempt.
"""
from __future__ import annotations

import json
import hashlib
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import and_, or_
from sqlmodel import Session, select

from common.config import (
    OUTBOX_DRAIN_LIMIT,
    OUTBOX_LEGACY_DISPATCH_GRACE_SECONDS,
    OUTBOX_LEASE_SECONDS,
    OUTBOX_POLL_INTERVAL_SECONDS,
    OUTBOX_RECOVERY_INTERVAL_SECONDS,
)
from common.models import (
    JobOutbox,
    OutboxStatus,
    StepStatus,
    Task,
    TaskStatus,
    TaskStep,
    engine,
)
from common.mq import publish_job

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClaimedJob:
    outbox_id: int
    claim_token: str
    message_id: str
    task_id: str
    step_id: str
    attempt: int
    capability: str
    impl_id: str
    routing_key: str
    payload_json: str
    payload_sha256: str


class OutboxPayloadError(ValueError):
    """The persisted envelope no longer matches its immutable payload hash."""


def _claim_one(*, outbox_id: Optional[int] = None) -> Optional[ClaimedJob]:
    """Atomically lease one publishable row.

    ``SKIP LOCKED`` lets multiple API replicas safely share the table.  An
    expired ``publishing`` claim is eligible again after a publisher crash.
    """
    now = datetime.now()
    lease_cutoff = now - timedelta(seconds=max(OUTBOX_LEASE_SECONDS, 1))
    eligible = or_(
        and_(
            JobOutbox.status == OutboxStatus.PENDING,
            JobOutbox.available_at <= now,
        ),
        and_(
            JobOutbox.status == OutboxStatus.PUBLISHING,
            or_(
                JobOutbox.claimed_at.is_(None),
                JobOutbox.claimed_at <= lease_cutoff,
            ),
        ),
    )
    with Session(engine) as session:
        stmt = select(JobOutbox).where(eligible)
        if outbox_id is not None:
            stmt = stmt.where(JobOutbox.id == outbox_id)
        row = session.exec(
            stmt.order_by(JobOutbox.available_at, JobOutbox.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        ).first()
        if row is None:
            return None

        claim_token = str(uuid.uuid4())
        row.status = OutboxStatus.PUBLISHING
        row.claim_token = claim_token
        row.claimed_at = now
        row.publish_attempts += 1
        row.updated_at = now
        session.add(row)
        # Snapshot before commit: SQLAlchemy expires ORM state at commit.
        claimed = ClaimedJob(
            outbox_id=row.id,
            claim_token=claim_token,
            message_id=row.message_id,
            task_id=row.task_id,
            step_id=row.step_id,
            attempt=row.attempt,
            capability=row.capability,
            impl_id=row.impl_id,
            routing_key=row.routing_key,
            payload_json=row.payload_json,
            payload_sha256=row.payload_sha256,
        )
        session.commit()
        return claimed


def _release_claim(job: ClaimedJob, exc: BaseException) -> None:
    """Return a failed publish to pending with bounded exponential backoff."""
    now = datetime.now()
    with Session(engine) as session:
        row = session.exec(
            select(JobOutbox)
            .where(JobOutbox.id == job.outbox_id)
            .with_for_update()
        ).first()
        if (
            row is None
            or row.status != OutboxStatus.PUBLISHING
            or row.claim_token != job.claim_token
        ):
            return
        delay_seconds = min(60, 2 ** min(max(row.publish_attempts, 1), 6))
        row.status = OutboxStatus.PENDING
        row.available_at = now + timedelta(seconds=delay_seconds)
        row.claimed_at = None
        row.claim_token = None
        row.last_error = f"{type(exc).__name__}: {exc}"[:2000]
        row.updated_at = now
        session.add(row)
        session.commit()


def _complete_claim(job: ClaimedJob) -> bool:
    """Record broker confirm and make the matching step dispatch-visible."""
    now = datetime.now()
    with Session(engine) as session:
        # Lock order matches advance/cancel (task -> step -> outbox), avoiding a
        # cancel-vs-confirm deadlock.  It also gives cancellation a clean winner:
        # whoever locks Task first decides whether this in-flight claim completes.
        task = session.exec(
            select(Task)
            .where(Task.task_id == job.task_id)
            .with_for_update()
        ).first()
        step = session.exec(
            select(TaskStep)
            .where(TaskStep.step_id == job.step_id)
            .with_for_update()
        ).first()
        row = session.exec(
            select(JobOutbox)
            .where(JobOutbox.id == job.outbox_id)
            .with_for_update()
        ).first()
        if (
            row is None
            or row.status != OutboxStatus.PUBLISHING
            or row.claim_token != job.claim_token
        ):
            # Cancellation or a newer lease won the race.  Never resurrect it.
            return False

        row.status = OutboxStatus.PUBLISHED
        row.published_at = now
        row.claimed_at = None
        row.claim_token = None
        row.last_error = None
        row.updated_at = now
        session.add(row)

        if (
            step is not None
            and step.attempt == job.attempt
            and step.status == StepStatus.PUBLISHING
            and task is not None
            and not task.cancel_requested
            and task.status != TaskStatus.CANCELLED
        ):
            step.status = StepStatus.DISPATCHED
            if step.started_at is None:
                step.started_at = now
            session.add(step)
        session.commit()
        logger.info(
            "published job message_id=%s task=%s step=%s attempt=%s",
            job.message_id,
            job.task_id,
            job.step_id,
            job.attempt,
        )
        return True


def _fail_corrupt_claim(job: ClaimedJob, exc: BaseException) -> None:
    """Quarantine a corrupt envelope instead of retrying it forever."""
    now = datetime.now()
    with Session(engine) as session:
        task = session.exec(
            select(Task)
            .where(Task.task_id == job.task_id)
            .with_for_update()
        ).first()
        step = session.exec(
            select(TaskStep)
            .where(TaskStep.step_id == job.step_id)
            .with_for_update()
        ).first()
        row = session.exec(
            select(JobOutbox)
            .where(JobOutbox.id == job.outbox_id)
            .with_for_update()
        ).first()
        if (
            row is None
            or row.status != OutboxStatus.PUBLISHING
            or row.claim_token != job.claim_token
        ):
            return
        row.status = OutboxStatus.FAILED
        row.claimed_at = None
        row.claim_token = None
        row.last_error = f"{type(exc).__name__}: {exc}"[:2000]
        row.updated_at = now
        session.add(row)
        if (
            step is not None
            and step.attempt == job.attempt
            and step.status == StepStatus.PUBLISHING
        ):
            step.status = StepStatus.FAILED
            step.finished_at = now
            step.error_code = "OUTBOX_PAYLOAD_CORRUPT"
            step.error_msg = str(exc)[:2000]
            session.add(step)
        if task is not None and task.status in (
            TaskStatus.CREATED, TaskStatus.RUNNING, TaskStatus.MANUAL_STAGE
        ):
            task.status = TaskStatus.FAILED
            session.add(task)
        session.commit()


def publish_available(*, limit: int = OUTBOX_DRAIN_LIMIT) -> int:
    """Publish up to ``limit`` jobs; network failures remain queued."""
    completed = 0
    for _ in range(max(limit, 0)):
        job = _claim_one()
        if job is None:
            break
        try:
            actual_hash = hashlib.sha256(
                job.payload_json.encode("utf-8")
            ).hexdigest()
            if actual_hash != job.payload_sha256:
                raise OutboxPayloadError(
                    f"payload sha256 mismatch for {job.message_id}"
                )
            payload = json.loads(job.payload_json)
        except (OutboxPayloadError, json.JSONDecodeError) as exc:
            logger.error("quarantining corrupt outbox %s: %s", job.message_id, exc)
            _fail_corrupt_claim(job, exc)
            continue

        try:
            publish_job(
                job.capability,
                job.impl_id,
                payload,
                message_id=job.message_id,
                routing_key=job.routing_key,
            )
        except Exception as exc:
            logger.warning(
                "job publish failed; retained in outbox message_id=%s: %s",
                job.message_id,
                exc,
            )
            _release_claim(job, exc)
            continue

        # Deliberately outside the publish try/except.  If this DB write fails,
        # the claim expires and the exact same message is published again.
        # Treating it as a publish failure could overwrite an uncertain confirm.
        if _complete_claim(job):
            completed += 1
    return completed


def recover_legacy_dispatches(*, limit: int = 100) -> int:
    """Adopt old ``DISPATCHED`` rows that predate the outbox deployment.

    There is no perfect way to distinguish an old publish-confirmed row from
    the legacy commit-before-publish crash window.  A generous grace period plus
    the original business idempotency key gives a conservative at-least-once
    migration path without immediately replaying freshly dispatched work.
    """
    cutoff = datetime.now() - timedelta(
        seconds=max(OUTBOX_LEGACY_DISPATCH_GRACE_SECONDS, 0)
    )
    with Session(engine) as session:
        candidates = session.exec(
            select(TaskStep.task_id, TaskStep.step_id, TaskStep.attempt)
            .join(Task, Task.task_id == TaskStep.task_id)
            .outerjoin(
                JobOutbox,
                and_(
                    JobOutbox.step_id == TaskStep.step_id,
                    JobOutbox.attempt == TaskStep.attempt,
                ),
            )
            .where(
                Task.status.in_(
                    (TaskStatus.CREATED, TaskStatus.RUNNING, TaskStatus.MANUAL_STAGE)
                ),
                Task.cancel_requested == False,  # noqa: E712
                TaskStep.status == StepStatus.DISPATCHED,
                TaskStep.attempt > 0,
                TaskStep.impl_id.is_not(None),
                TaskStep.started_at.is_not(None),
                TaskStep.started_at <= cutoff,
                JobOutbox.id.is_(None),
            )
            .limit(max(limit, 0))
        ).all()

    adopted = 0
    for task_id, step_id, attempt in candidates:
        if _adopt_legacy_dispatch(task_id, step_id, attempt):
            adopted += 1
    if adopted:
        wake_outbox_publisher()
        logger.warning("adopted %s legacy dispatched jobs into outbox", adopted)
    return adopted


def _adopt_legacy_dispatch(task_id: str, step_id: str, attempt: int) -> bool:
    now = datetime.now()
    with Session(engine) as session:
        task = session.exec(
            select(Task).where(Task.task_id == task_id).with_for_update()
        ).first()
        if (
            task is None
            or task.cancel_requested
            or task.status not in (
                TaskStatus.CREATED, TaskStatus.RUNNING, TaskStatus.MANUAL_STAGE
            )
        ):
            return False
        step = session.exec(
            select(TaskStep).where(TaskStep.step_id == step_id).with_for_update()
        ).first()
        if (
            step is None
            or step.status != StepStatus.DISPATCHED
            or step.attempt != attempt
            or not step.impl_id
            or not step.group_name
        ):
            return False
        if session.exec(
            select(JobOutbox).where(
                JobOutbox.step_id == step_id,
                JobOutbox.attempt == attempt,
            )
        ).first():
            return False

        params = json.loads(task.params_json or "{}")
        message_id = f"job:{step_id}:{attempt}"
        # Reuse the protocol model so migration envelopes match new submissions.
        from common.config import OSS_BUCKET, OSS_OUTPUT_PREFIX
        from protocol.envelope import JobInput, JobOutput, JobRequest

        envelope = JobRequest(
            msg_id=message_id,
            task_id=task_id,
            step_id=step_id,
            attempt=attempt,
            idempotency_key=f"{task_id}:{step_id}:{attempt}",
            capability=step.capability,
            impl_id=step.impl_id,
            group_name=step.group_name,
            reply_routing_key=f"cb.{task_id}.{step.capability}",
            input=JobInput(params=params),
            output=JobOutput(
                oss_bucket=OSS_BUCKET,
                oss_prefix=OSS_OUTPUT_PREFIX.format(task_id=task_id),
            ),
        )
        payload_json = envelope.model_dump_json()
        step.status = StepStatus.PUBLISHING
        session.add(step)
        session.add(JobOutbox(
            message_id=message_id,
            task_id=task_id,
            step_id=step_id,
            attempt=attempt,
            capability=step.capability,
            impl_id=step.impl_id,
            routing_key=f"job.{step.capability}.{step.impl_id}",
            payload_json=payload_json,
            payload_sha256=hashlib.sha256(
                payload_json.encode("utf-8")
            ).hexdigest(),
            status=OutboxStatus.PENDING,
            available_at=now,
            created_at=now,
            updated_at=now,
            last_error="adopted from legacy dispatched row without outbox",
        ))
        session.commit()
        return True


def recover_pending_tasks(*, limit: int = 200) -> int:
    """Advance tasks left between submit's commit and its advance call."""
    with Session(engine) as session:
        task_ids = list(
            session.exec(
                select(Task.task_id)
                .join(TaskStep, TaskStep.task_id == Task.task_id)
                .where(
                    Task.status.in_(
                        (TaskStatus.CREATED, TaskStatus.RUNNING, TaskStatus.MANUAL_STAGE)
                    ),
                    Task.cancel_requested == False,  # noqa: E712
                    TaskStep.status == StepStatus.PENDING,
                )
                .distinct()
                .limit(max(limit, 0))
            ).all()
        )
    if not task_ids:
        return 0

    # Local import avoids core -> outbox -> core import cycle.
    from orchestrator.core import advance

    for task_id in task_ids:
        try:
            advance(task_id)
        except Exception:
            logger.exception("failed to recover pending task %s", task_id)
    return len(task_ids)


class OutboxRuntime:
    """One lightweight publisher/recovery thread per process."""

    def __init__(self):
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="platform-outbox-publisher",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def stop(self, timeout: float = 15.0) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            logger.warning("outbox publisher did not stop within %.1fs", timeout)

    def _run(self) -> None:
        next_recovery_at = 0.0
        while not self._stop.is_set():
            try:
                now = time.monotonic()
                # Immediate startup recovery closes submit-commit/advance crash
                # windows.  Later scans are deliberately much less frequent than
                # outbox polling so long-running tasks do not create DB churn.
                if now >= next_recovery_at:
                    recover_pending_tasks()
                    recover_legacy_dispatches()
                    next_recovery_at = now + max(
                        OUTBOX_RECOVERY_INTERVAL_SECONDS, 1.0
                    )
                published = publish_available()
                if OUTBOX_DRAIN_LIMIT > 0 and published >= OUTBOX_DRAIN_LIMIT:
                    continue
            except Exception:
                logger.exception("outbox recovery cycle failed")
            self._wake.wait(timeout=max(OUTBOX_POLL_INTERVAL_SECONDS, 0.1))
            self._wake.clear()


_runtime_lock = threading.Lock()
_runtime: Optional[OutboxRuntime] = None


def start_outbox_runtime() -> OutboxRuntime:
    global _runtime
    with _runtime_lock:
        if _runtime is not None and _runtime._thread.is_alive():
            return _runtime
        _runtime = OutboxRuntime()
        _runtime.start()
        return _runtime


def wake_outbox_publisher() -> None:
    runtime = _runtime
    if runtime is not None:
        runtime.wake()


def stop_outbox_runtime() -> None:
    global _runtime
    with _runtime_lock:
        runtime = _runtime
        _runtime = None
    if runtime is not None:
        runtime.stop()
