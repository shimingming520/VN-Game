import asyncio
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import main as platform_main
from gateway import api as gateway_api
from common.models import (
    JobOutbox,
    OutboxStatus,
    PipelineTemplate,
    PlatformUser,
    ServiceImpl,
    StepStatus,
    Task,
    TaskStatus,
    TaskStep,
)
from orchestrator import core, outbox


@pytest.fixture
def platform_db(monkeypatch):
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(test_engine)
    monkeypatch.setattr(core, "engine", test_engine)
    monkeypatch.setattr(outbox, "engine", test_engine)

    with Session(test_engine) as session:
        session.add(PipelineTemplate(
            name="default",
            dag_json=json.dumps({"nodes": ["story_planner"], "edges": []}),
        ))
        session.add(ServiceImpl(
            capability="story_planner",
            group_name="group-ref",
            impl_id="group-ref.story_planner.v1",
            queue="q.story-planner",
            enabled=True,
            health_status="healthy",
            last_heartbeat=datetime.now(),
        ))
        session.commit()
    return test_engine


def _rows(test_engine):
    with Session(test_engine) as session:
        task = session.exec(select(Task)).one()
        step = session.exec(select(TaskStep)).one()
        job = session.exec(select(JobOutbox)).one()
        return task, step, job


def _success_payload(task_id, step, *, attempt=None, key_suffix="success"):
    return {
        "msg_type": "callback.succeeded",
        "task_id": task_id,
        "step_id": step.step_id,
        "attempt": step.attempt if attempt is None else attempt,
        "capability": step.capability,
        "impl_id": step.impl_id or f"group-ref.{step.capability}.v1",
        "group_name": step.group_name or "group-ref",
        "idempotency_key": (
            f"{task_id}:{step.step_id}:{key_suffix}:"
            f"{step.attempt if attempt is None else attempt}"
        ),
        "status": "succeeded",
        "output": {"artifacts": [], "summary": {}},
        "usage": [],
    }


def test_dispatch_atomically_creates_stable_hashed_outbox(platform_db):
    core.submit("task-atomic", params={"novel": "book.txt"})

    task, step, job = _rows(platform_db)
    payload = json.loads(job.payload_json)
    assert task.status == TaskStatus.RUNNING
    assert step.status == StepStatus.PUBLISHING
    assert step.attempt == 1
    assert job.status == OutboxStatus.PENDING
    assert job.message_id == f"job:{step.step_id}:1"
    assert payload["msg_id"] == job.message_id
    assert payload["idempotency_key"] == f"task-atomic:{step.step_id}:1"
    assert job.payload_sha256 == hashlib.sha256(
        job.payload_json.encode("utf-8")
    ).hexdigest()


def test_publish_failure_stays_queued_then_confirm_marks_dispatched(
    platform_db, monkeypatch
):
    core.submit("task-retry")
    sent = []

    def fail_publish(*_args, **_kwargs):
        raise OSError("mq unavailable")

    monkeypatch.setattr(outbox, "publish_job", fail_publish)
    assert outbox.publish_available(limit=1) == 0

    _, step, job = _rows(platform_db)
    stable_message_id = job.message_id
    assert step.status == StepStatus.PUBLISHING
    assert job.status == OutboxStatus.PENDING
    assert job.publish_attempts == 1
    assert "mq unavailable" in job.last_error

    with Session(platform_db) as session:
        retry = session.get(JobOutbox, job.id)
        retry.available_at = datetime.now() - timedelta(seconds=1)
        session.add(retry)
        session.commit()

    def confirm_publish(capability, impl_id, payload, **kwargs):
        sent.append((capability, impl_id, payload, kwargs))

    monkeypatch.setattr(outbox, "publish_job", confirm_publish)
    assert outbox.publish_available(limit=1) == 1

    _, step, job = _rows(platform_db)
    assert job.status == OutboxStatus.PUBLISHED
    assert step.status == StepStatus.DISPATCHED
    assert job.message_id == stable_message_id
    assert sent[0][3] == {
        "message_id": stable_message_id,
        "routing_key": job.routing_key,
    }


def test_confirm_then_db_crash_releases_by_lease_with_same_message_id(
    platform_db, monkeypatch
):
    core.submit("task-confirm-crash")
    sent_ids = []
    monkeypatch.setattr(
        outbox,
        "publish_job",
        lambda *_args, **kwargs: sent_ids.append(kwargs["message_id"]),
    )
    real_complete = outbox._complete_claim

    def crash_after_confirm(_job):
        raise RuntimeError("process died after broker confirm")

    monkeypatch.setattr(outbox, "_complete_claim", crash_after_confirm)
    with pytest.raises(RuntimeError, match="after broker confirm"):
        outbox.publish_available(limit=1)

    _, step, job = _rows(platform_db)
    assert step.status == StepStatus.PUBLISHING
    assert job.status == OutboxStatus.PUBLISHING
    with Session(platform_db) as session:
        expired = session.get(JobOutbox, job.id)
        expired.claimed_at = datetime.now() - timedelta(
            seconds=outbox.OUTBOX_LEASE_SECONDS + 1
        )
        session.add(expired)
        session.commit()

    monkeypatch.setattr(outbox, "_complete_claim", real_complete)
    assert outbox.publish_available(limit=1) == 1
    assert sent_ids == [job.message_id, job.message_id]
    _, step, job = _rows(platform_db)
    assert step.status == StepStatus.DISPATCHED
    assert job.status == OutboxStatus.PUBLISHED
    assert job.publish_attempts == 2


def test_submit_commit_before_advance_is_recovered(platform_db, monkeypatch):
    real_advance = core.advance
    monkeypatch.setattr(
        core,
        "advance",
        lambda _task_id: (_ for _ in ()).throw(RuntimeError("submit crash")),
    )
    with pytest.raises(RuntimeError, match="submit crash"):
        core.submit("task-submit-crash")

    with Session(platform_db) as session:
        assert session.exec(select(Task)).one().status == TaskStatus.RUNNING
        assert session.exec(select(TaskStep)).one().status == StepStatus.PENDING
        assert session.exec(select(JobOutbox)).first() is None

    monkeypatch.setattr(core, "advance", real_advance)
    assert outbox.recover_pending_tasks() == 1
    _, step, job = _rows(platform_db)
    assert step.status == StepStatus.PUBLISHING
    assert job.status == OutboxStatus.PENDING


def test_stale_concurrent_dispatch_cannot_create_second_outbox(platform_db):
    core.submit("task-concurrent")
    with Session(platform_db) as session:
        task = session.exec(select(Task)).one()
        current = session.exec(select(TaskStep)).one()
        impl = session.exec(select(ServiceImpl)).one()
        stale = SimpleNamespace(
            id=current.id,
            step_id=current.step_id,
            task_id=current.task_id,
            capability=current.capability,
            status=StepStatus.PENDING,
            attempt=0,
        )
        assert core._dispatch(session, task, stale, impl, {}) is None
        session.commit()

    with Session(platform_db) as session:
        assert len(session.exec(select(JobOutbox)).all()) == 1
        assert session.exec(select(TaskStep)).one().attempt == 1


def test_cancel_cancels_claimed_but_unconfirmed_outbox(platform_db):
    core.submit("task-cancel")
    claimed = outbox._claim_one()
    assert claimed is not None

    assert core.request_cancel("task-cancel") is True
    assert outbox._complete_claim(claimed) is False

    task, step, job = _rows(platform_db)
    assert task.status == TaskStatus.CANCELLED
    assert step.status == StepStatus.CANCELLED
    assert job.status == OutboxStatus.CANCELLED


def test_manual_restart_reuses_task_and_step_with_next_attempt(platform_db):
    core.submit("task-manual-restart")
    with Session(platform_db) as session:
        original = session.exec(select(TaskStep)).one()
        original_step_id = original.step_id

    result = core.request_restart("task-manual-restart")

    assert result["preserved_steps"] == []
    assert result["restarted_steps"] == ["story_planner"]
    with Session(platform_db) as session:
        task = session.exec(select(Task)).one()
        step = session.exec(select(TaskStep)).one()
        jobs = session.exec(select(JobOutbox).order_by(JobOutbox.attempt)).all()
        assert task.status == TaskStatus.RUNNING
        assert step.step_id == original_step_id
        assert step.status == StepStatus.PUBLISHING
        # 人工重启不创建新 step，也不跳过 attempt 2。
        assert step.attempt == 2
        assert [job.attempt for job in jobs] == [1, 2]
        assert jobs[0].status == OutboxStatus.CANCELLED
        assert jobs[1].status == OutboxStatus.PENDING
        context = json.loads(jobs[1].payload_json)["input"]["context"]
        assert context == {
            "dispatch_reason": "manual_restart",
            "restart_id": result["restart_id"],
            "supersedes_attempt": 1,
        }


def _configure_three_stage_restart_task(platform_db):
    """Build three independent steps so restart scope can be asserted directly."""
    with Session(platform_db) as session:
        template = session.exec(select(PipelineTemplate)).one()
        template.dag_json = json.dumps({
            "nodes": ["story_planner", "info_extractor", "audio_producer"],
            "edges": [],
        })
        session.add(template)
        for capability in ("info_extractor", "audio_producer"):
            session.add(ServiceImpl(
                capability=capability,
                group_name="group-ref",
                impl_id=f"group-ref.{capability}.v1",
                queue=f"q.{capability}",
                enabled=True,
                health_status="healthy",
                last_heartbeat=datetime.now(),
            ))
        session.commit()


def _set_restart_statuses(platform_db):
    with Session(platform_db) as session:
        steps = {
            step.capability: step
            for step in session.exec(select(TaskStep)).all()
        }
        steps["story_planner"].status = StepStatus.SUCCEEDED
        steps["story_planner"].progress = 100
        steps["info_extractor"].status = StepStatus.FAILED
        steps["info_extractor"].error_msg = "old failure"
        steps["audio_producer"].status = StepStatus.RUNNING
        steps["audio_producer"].progress = 42
        for step in steps.values():
            session.add(step)
        session.commit()


def test_restart_failed_only_preserves_running_and_pending_steps(platform_db):
    _configure_three_stage_restart_task(platform_db)
    core.submit("task-restart-failed-only")
    _set_restart_statuses(platform_db)

    result = core.request_restart(
        "task-restart-failed-only", mode=core.RESTART_FAILED_ONLY
    )

    assert result["restarted_steps"] == ["info_extractor"]
    assert result["untouched_steps"] == ["audio_producer"]
    with Session(platform_db) as session:
        steps = {
            step.capability: step
            for step in session.exec(select(TaskStep)).all()
        }
        jobs = session.exec(
            select(JobOutbox).order_by(JobOutbox.capability, JobOutbox.attempt)
        ).all()
        assert steps["story_planner"].status == StepStatus.SUCCEEDED
        assert steps["audio_producer"].status == StepStatus.RUNNING
        assert steps["audio_producer"].attempt == 1
        assert steps["info_extractor"].status == StepStatus.PUBLISHING
        assert steps["info_extractor"].attempt == 2
        info_jobs = [job for job in jobs if job.capability == "info_extractor"]
        audio_jobs = [job for job in jobs if job.capability == "audio_producer"]
        assert info_jobs[0].status == OutboxStatus.CANCELLED
        assert info_jobs[1].status == OutboxStatus.PENDING
        assert audio_jobs[0].status == OutboxStatus.PENDING


def test_restart_running_and_failed_resets_both_selected_steps(platform_db):
    _configure_three_stage_restart_task(platform_db)
    core.submit("task-restart-running-and-failed")
    _set_restart_statuses(platform_db)

    result = core.request_restart(
        "task-restart-running-and-failed",
        mode=core.RESTART_RUNNING_AND_FAILED,
    )

    assert result["restarted_steps"] == ["info_extractor", "audio_producer"]
    assert result["untouched_steps"] == []
    with Session(platform_db) as session:
        steps = {
            step.capability: step
            for step in session.exec(select(TaskStep)).all()
        }
        jobs = session.exec(
            select(JobOutbox).order_by(JobOutbox.capability, JobOutbox.attempt)
        ).all()
        assert steps["story_planner"].status == StepStatus.SUCCEEDED
        assert steps["info_extractor"].status == StepStatus.PUBLISHING
        assert steps["audio_producer"].status == StepStatus.PUBLISHING
        assert steps["info_extractor"].attempt == 2
        assert steps["audio_producer"].attempt == 2
        for capability in ("info_extractor", "audio_producer"):
            stage_jobs = [job for job in jobs if job.capability == capability]
            assert stage_jobs[0].status == OutboxStatus.CANCELLED
            assert stage_jobs[1].status == OutboxStatus.PENDING


def _configure_two_stage_task(platform_db):
    with Session(platform_db) as session:
        template = session.exec(select(PipelineTemplate)).one()
        template.dag_json = json.dumps({
            "nodes": ["story_planner", "info_extractor"],
            "edges": [["story_planner", "info_extractor"]],
        })
        session.add(template)
        session.add(ServiceImpl(
            capability="info_extractor",
            group_name="group-ref",
            impl_id="group-ref.info_extractor.v1",
            queue="q.info-extractor",
            enabled=True,
            health_status="healthy",
            last_heartbeat=datetime.now(),
        ))
        session.commit()


def test_manual_stage_run_forces_pending_stage_without_dispatching_others(
    platform_db,
):
    _configure_two_stage_task(platform_db)
    core.submit("task-single-stage")

    with Session(platform_db) as session:
        before = {
            step.capability: (step.status, step.attempt)
            for step in session.exec(select(TaskStep)).all()
        }

    result = core.request_stage_run("task-single-stage", "info_extractor")

    assert result["capability"] == "info_extractor"
    assert result["attempt"] == 1
    with Session(platform_db) as session:
        steps = {
            step.capability: step
            for step in session.exec(select(TaskStep)).all()
        }
        jobs = session.exec(
            select(JobOutbox).order_by(JobOutbox.capability, JobOutbox.attempt)
        ).all()
        assert (steps["story_planner"].status, steps["story_planner"].attempt) == before["story_planner"]
        assert steps["info_extractor"].status == StepStatus.PUBLISHING
        assert steps["info_extractor"].attempt == 1
        assert [job.capability for job in jobs].count("info_extractor") == 1
        context = next(
            json.loads(job.payload_json)["input"]["context"]
            for job in jobs if job.capability == "info_extractor"
        )
        assert context["dispatch_reason"] == "manual_stage_run"
        assert context["target_capability"] == "info_extractor"


def test_manual_stage_success_does_not_advance_downstream(platform_db):
    _configure_two_stage_task(platform_db)
    core.submit("task-single-stage-callback")
    result = core.request_stage_run("task-single-stage-callback", "info_extractor")
    with Session(platform_db) as session:
        step = session.exec(
            select(TaskStep).where(
                TaskStep.task_id == "task-single-stage-callback",
                TaskStep.capability == "info_extractor",
            )
        ).one()
        payload = _success_payload(
            "task-single-stage-callback", step, key_suffix="manual-stage"
        )

    core.handle_callback(payload)

    with Session(platform_db) as session:
        steps = {
            step.capability: step
            for step in session.exec(select(TaskStep)).all()
        }
        task = session.exec(select(Task)).one()
        assert steps["info_extractor"].status == StepStatus.SUCCEEDED
        # story_planner 是原本已经下发的步骤；单模块成功不能触发其它 pending
        # 节点或改变其 attempt。
        assert steps["story_planner"].status == StepStatus.PUBLISHING
        assert steps["story_planner"].attempt == 1
        assert task.status == TaskStatus.MANUAL_STAGE
        assert result["stage_run_id"]


def test_manual_stage_run_resets_retry_budget(platform_db):
    core.submit("task-single-stage-budget")
    with Session(platform_db) as session:
        step = session.exec(select(TaskStep)).one()
        step.status = StepStatus.FAILED
        step.attempt = 3
        step.max_attempts = 3
        step.error_msg = "old failure"
        session.add(step)
        task = session.exec(select(Task)).one()
        task.status = TaskStatus.FAILED
        session.add(task)
        session.commit()

    result = core.request_stage_run("task-single-stage-budget", "story_planner")

    assert result["attempt"] == 4
    with Session(platform_db) as session:
        step = session.exec(select(TaskStep)).one()
        assert step.status == StepStatus.PUBLISHING
        assert step.attempt == 4
        assert step.max_attempts >= 6


def test_restart_window_ignores_old_same_attempt_terminal_callback(
    platform_db, monkeypatch
):
    core.submit("task-restart-window")
    with Session(platform_db) as session:
        old_step = session.exec(select(TaskStep)).one()
        old_attempt = old_step.attempt
        payload = _success_payload(
            "task-restart-window",
            old_step,
            attempt=old_attempt,
            key_suffix="late-window",
        )

    real_advance = core.advance
    monkeypatch.setattr(core, "advance", lambda _task_id: None)
    core.request_restart("task-restart-window")

    # reset 已提交而新 outbox 尚未建立：旧 attempt 号暂时仍相同，但 PENDING
    # 不是合法回调源，因此不能被迟到 success 抢回终态。
    real_handle_callback = core.handle_callback
    real_handle_callback(payload)
    with Session(platform_db) as session:
        step = session.exec(select(TaskStep)).one()
        assert step.status == StepStatus.PENDING
        assert step.attempt == old_attempt

    monkeypatch.setattr(core, "advance", real_advance)
    core.advance("task-restart-window")
    with Session(platform_db) as session:
        step = session.exec(select(TaskStep)).one()
        assert step.status == StepStatus.PUBLISHING
        assert step.attempt == old_attempt + 1


def test_restart_all_succeeded_is_safe_noop(platform_db):
    core.submit("task-already-complete")
    with Session(platform_db) as session:
        step = session.exec(select(TaskStep)).one()
        payload = _success_payload("task-already-complete", step)
    core.handle_callback(payload)

    result = core.request_restart("task-already-complete")

    assert result["preserved_steps"] == ["story_planner"]
    assert result["restarted_steps"] == []
    with Session(platform_db) as session:
        task = session.exec(select(Task)).one()
        step = session.exec(select(TaskStep)).one()
        jobs = session.exec(select(JobOutbox)).all()
        assert task.status == TaskStatus.SUCCEEDED
        assert task.progress == 100
        assert task.completed_at is not None
        assert step.status == StepStatus.SUCCEEDED
        assert step.attempt == 1
        assert len(jobs) == 1


def test_restart_preserves_completed_module_and_resets_novel_task_dashboard(
    platform_db
):
    with Session(platform_db) as session:
        template = session.exec(select(PipelineTemplate)).one()
        template.dag_json = json.dumps({
            "nodes": ["story_planner", "info_extractor"],
            "edges": [["story_planner", "info_extractor"]],
        })
        session.add(template)
        session.add(ServiceImpl(
            capability="info_extractor",
            group_name="group-ref",
            impl_id="group-ref.info_extractor.v1",
            queue="q.info-extractor",
            enabled=True,
            health_status="healthy",
            last_heartbeat=datetime.now(),
        ))
        session.commit()

    stage_columns = ",\n".join(
        f"{capability}_status INTEGER NOT NULL DEFAULT 0"
        for capability in core._NOVEL_STAGE_CAPABILITIES
    )
    with platform_db.begin() as connection:
        connection.exec_driver_sql(f"""
            CREATE TABLE novel_tasks (
                task_id TEXT PRIMARY KEY,
                user_id TEXT,
                is_deleted BOOLEAN NOT NULL DEFAULT 0,
                {stage_columns},
                overall_status INTEGER NOT NULL DEFAULT 0,
                progress_percentage INTEGER NOT NULL DEFAULT 0,
                error_message TEXT,
                completed_at TIMESTAMP,
                update_time TIMESTAMP
            )
        """)
        connection.exec_driver_sql(
            """
            INSERT INTO novel_tasks (
                task_id, user_id, story_planner_status,
                info_extractor_status, overall_status,
                progress_percentage, error_message
            ) VALUES (?, ?, 2, 1, 1, 42, ?)
            """,
            ("task-dashboard-restart", "owner-1", "old error"),
        )

    core.submit("task-dashboard-restart")
    result = core.request_restart("task-dashboard-restart")

    assert result["preserved_steps"] == ["story_planner"]
    assert result["restarted_steps"] == ["info_extractor"]
    with Session(platform_db) as session:
        steps = {
            step.capability: step
            for step in session.exec(select(TaskStep)).all()
        }
        assert steps["story_planner"].status == StepStatus.SUCCEEDED
        assert steps["story_planner"].attempt == 1
        assert steps["info_extractor"].status == StepStatus.PUBLISHING
        # 原步骤从未下发，因此人工重启仍从 attempt 1 开始。
        assert steps["info_extractor"].attempt == 1
        dashboard = session.exec(text(
            "SELECT story_planner_status, info_extractor_status, "
            "overall_status, progress_percentage, error_message, completed_at "
            "FROM novel_tasks WHERE task_id = 'task-dashboard-restart'"
        )).one()
        assert tuple(dashboard) == (2, 0, 1, 50, None, None)


def test_restart_endpoint_allows_owner_but_hides_foreign_task(
    platform_db, monkeypatch
):
    with platform_db.begin() as connection:
        connection.exec_driver_sql("""
            CREATE TABLE novel_tasks (
                task_id TEXT PRIMARY KEY,
                user_id TEXT,
                is_deleted BOOLEAN NOT NULL DEFAULT 0
            )
        """)
        connection.exec_driver_sql(
            "INSERT INTO novel_tasks (task_id, user_id) VALUES (?, ?)",
            ("task-owned", "cms-owner"),
        )
    monkeypatch.setattr(gateway_api, "engine", platform_db)
    monkeypatch.setattr(
        gateway_api.orchestrator,
        "request_restart",
        lambda task_id: {
            "task_id": task_id,
            "restart_id": "restart-1",
            "preserved_steps": ["story_planner"],
            "restarted_steps": ["info_extractor"],
        },
    )
    owner = PlatformUser(
        id=1,
        main_site_uid="cms-owner",
        username="owner",
        role="creator",
    )
    foreign = PlatformUser(
        id=2,
        main_site_uid="cms-foreign",
        username="foreign",
        role="creator",
    )

    result = gateway_api.restart_novel_task("task-owned", owner)
    assert result["ok"] is True
    assert result["restart_id"] == "restart-1"

    with pytest.raises(HTTPException) as exc:
        gateway_api.restart_novel_task("task-owned", foreign)
    assert exc.value.status_code == 404


def test_restart_endpoint_passes_selected_mode(platform_db, monkeypatch):
    with platform_db.begin() as connection:
        connection.exec_driver_sql("""
            CREATE TABLE novel_tasks (
                task_id TEXT PRIMARY KEY,
                user_id TEXT,
                is_deleted BOOLEAN NOT NULL DEFAULT 0
            )
        """)
        connection.exec_driver_sql(
            "INSERT INTO novel_tasks (task_id, user_id) VALUES (?, ?)",
            ("task-mode", "cms-owner"),
        )
    monkeypatch.setattr(gateway_api, "engine", platform_db)
    calls = []

    def fake_restart(task_id, *, mode):
        calls.append((task_id, mode))
        return {
            "task_id": task_id,
            "restart_id": "restart-mode",
            "mode": mode,
            "preserved_steps": [],
            "restarted_steps": ["story_planner"],
            "untouched_steps": [],
        }

    monkeypatch.setattr(gateway_api.orchestrator, "request_restart", fake_restart)
    owner = PlatformUser(
        id=1,
        main_site_uid="cms-owner",
        username="owner",
        role="creator",
    )

    result = gateway_api.restart_novel_task(
        "task-mode",
        owner,
        gateway_api.RestartTaskIn(mode="failed_only"),
    )
    assert result["mode"] == "failed_only"
    assert calls == [("task-mode", "failed_only")]


def test_progress_arriving_before_confirm_moves_publishing_step_to_running(
    platform_db
):
    core.submit("task-fast-progress")
    _, step, _ = _rows(platform_db)
    core.handle_callback({
        "msg_type": "callback.progress",
        "task_id": "task-fast-progress",
        "step_id": step.step_id,
        "attempt": 1,
        "capability": "story_planner",
        "impl_id": "group-ref.story_planner.v1",
        "group_name": "group-ref",
        "idempotency_key": "fast-progress-1",
        "progress": 3,
    })

    _, step, job = _rows(platform_db)
    assert step.status == StepStatus.RUNNING
    assert step.progress == 3
    assert job.status == OutboxStatus.PUBLISHED


def test_payload_hash_mismatch_is_never_published(platform_db, monkeypatch):
    core.submit("task-corrupt")
    with Session(platform_db) as session:
        job = session.exec(select(JobOutbox)).one()
        job.payload_json += " "
        session.add(job)
        session.commit()

    monkeypatch.setattr(
        outbox,
        "publish_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("corrupt payload must not be published")
        ),
    )
    assert outbox.publish_available(limit=1) == 0
    task, step, job = _rows(platform_db)
    assert job.status == OutboxStatus.FAILED
    assert step.status == StepStatus.FAILED
    assert step.error_code == "OUTBOX_PAYLOAD_CORRUPT"
    assert task.status == TaskStatus.FAILED


def test_legacy_dispatched_without_outbox_is_adopted(platform_db, monkeypatch):
    monkeypatch.setattr(outbox, "OUTBOX_LEGACY_DISPATCH_GRACE_SECONDS", 0)
    with Session(platform_db) as session:
        template = session.exec(select(PipelineTemplate)).one()
        session.add(Task(
            task_id="task-legacy",
            pipeline_template_id=template.id,
            params_json=json.dumps({"legacy": True}),
            status=TaskStatus.RUNNING,
        ))
        session.add(TaskStep(
            step_id="legacy-step",
            task_id="task-legacy",
            capability="story_planner",
            impl_id="group-ref.story_planner.v1",
            group_name="group-ref",
            status=StepStatus.DISPATCHED,
            attempt=1,
            started_at=datetime.now() - timedelta(seconds=1),
        ))
        session.commit()

    assert outbox.recover_legacy_dispatches() == 1
    _, step, job = _rows(platform_db)
    assert step.status == StepStatus.PUBLISHING
    assert step.attempt == 1
    assert job.message_id == "job:legacy-step:1"
    assert json.loads(job.payload_json)["idempotency_key"] == (
        "task-legacy:legacy-step:1"
    )


def test_main_lifespan_starts_and_stops_outbox(monkeypatch):
    events = []
    for name in (
        "create_all_tables",
        "declare_topology",
        "seed_default_template",
        "seed_pipelines",
        "seed_creation_modes",
    ):
        monkeypatch.setattr(platform_main, name, lambda name=name: events.append(name))
    monkeypatch.setattr(
        platform_main,
        "start_outbox_runtime",
        lambda: events.append("start_outbox"),
    )
    monkeypatch.setattr(
        platform_main,
        "stop_outbox_runtime",
        lambda: events.append("stop_outbox"),
    )

    async def exercise():
        async with platform_main.lifespan(SimpleNamespace()):
            events.append("serving")

    asyncio.run(exercise())
    assert events[-3:] == ["start_outbox", "serving", "stop_outbox"]


def test_k8s_database_pool_caps_are_process_specific():
    platform_root = Path(__file__).resolve().parents[1]
    api = (platform_root / "k8s/04-platform-api.yaml").read_text()
    consumer = (platform_root / "k8s/05-orchestrator-consumer.yaml").read_text()
    assert 'name: PLATFORM_DB_POOL_SIZE\n              value: "15"' in api
    assert 'name: PLATFORM_DB_MAX_OVERFLOW\n              value: "2"' in api
    assert 'name: PLATFORM_DB_POOL_SIZE\n              value: "3"' in consumer
    assert 'name: PLATFORM_DB_MAX_OVERFLOW\n              value: "1"' in consumer
