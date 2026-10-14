"""Opt-in PostgreSQL + RabbitMQ reliability smoke tests.

These tests deliberately pause/restart containers.  They only run when all
``PLATFORM_INTEGRATION_*`` guards are present and the target containers carry
the ``codex.integration=platform-outbox`` label, so a normal pytest run cannot
touch a developer's shared PostgreSQL or RabbitMQ.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pika
import pytest
from sqlalchemy import update
from sqlmodel import SQLModel, Session, select


if os.getenv("RUN_PLATFORM_INTEGRATION") != "1":
    pytest.skip(
        "set RUN_PLATFORM_INTEGRATION=1 for destructive isolated-container smoke",
        allow_module_level=True,
    )

from common.config import (  # noqa: E402
    CALLBACK_DLQ_QUEUE,
    CALLBACK_MAX_RETRIES,
    CALLBACK_QUEUE,
    CALLBACK_RETRY_QUEUE,
    CALLBACKS_EXCHANGE,
)
from common.models import (  # noqa: E402
    CallbackLog,
    JobOutbox,
    OutboxStatus,
    PipelineTemplate,
    ServiceImpl,
    StepStatus,
    TaskStep,
    create_all_tables,
    engine,
)
from common.mq import (  # noqa: E402
    connect,
    declare_impl_queue,
    declare_topology,
    publish_job,
)
from orchestrator import core, outbox  # noqa: E402


PG_CONTAINER = os.environ.get("PLATFORM_INTEGRATION_PG_CONTAINER", "")
RABBIT_CONTAINER = os.environ.get("PLATFORM_INTEGRATION_RABBIT_CONTAINER", "")
EXPECTED_LABEL = "codex.integration=platform-outbox"
PROJECT_DIR = Path(__file__).resolve().parents[1]


def _run(*args: str, timeout: float = 30, check: bool = True):
    return subprocess.run(
        args,
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _docker(*args: str, timeout: float = 30, check: bool = True):
    return _run("docker", *args, timeout=timeout, check=check)


def _assert_isolated_container(name: str) -> None:
    assert name.startswith("codex-outbox-"), (
        "integration tests only accept codex-outbox-* containers"
    )
    label = _docker(
        "inspect",
        "--format",
        "{{ index .Config.Labels \"codex.integration\" }}",
        name,
    ).stdout.strip()
    assert label == "platform-outbox", (
        f"{name} is missing safety label {EXPECTED_LABEL}"
    )


def _container_status(name: str) -> str:
    return _docker("inspect", "--format", "{{.State.Status}}", name).stdout.strip()


def _ensure_running(name: str) -> None:
    status = _container_status(name)
    if status == "paused":
        _docker("unpause", name)
    elif status != "running":
        _docker("start", name)


def _wait_exec(container: str, command: list[str], timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        result = _docker("exec", container, *command, check=False, timeout=10)
        last = (result.stdout + result.stderr).strip()
        if result.returncode == 0:
            return
        time.sleep(0.25)
    raise AssertionError(f"{container} did not become ready: {last}")


def _wait_postgres(timeout: float = 60) -> None:
    _wait_exec(
        PG_CONTAINER,
        ["pg_isready", "-U", "platform_it", "-d", "agent_platform_it"],
        timeout,
    )


def _wait_rabbit(timeout: float = 90) -> None:
    _wait_exec(
        RABBIT_CONTAINER,
        ["rabbitmq-diagnostics", "-q", "ping"],
        timeout,
    )
    # rabbitmq-diagnostics can succeed a moment before the AMQP listener and
    # default user are ready after a real container restart.
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            conn = connect()
            conn.close()
            return
        except pika.exceptions.AMQPError as exc:
            last_error = exc
            time.sleep(0.25)
    raise AssertionError(f"RabbitMQ AMQP listener did not become ready: {last_error}")


def _queue_state(queue: str) -> tuple[int, int]:
    conn = connect()
    try:
        result = conn.channel().queue_declare(queue=queue, passive=True)
        return result.method.message_count, result.method.consumer_count
    finally:
        conn.close()


def _wait_queue_count(queue: str, minimum: int, timeout: float = 30) -> int:
    deadline = time.monotonic() + timeout
    last = -1
    while time.monotonic() < deadline:
        try:
            last, _ = _queue_state(queue)
        except pika.exceptions.AMQPError:
            time.sleep(0.1)
            continue
        if last >= minimum:
            return last
        time.sleep(0.05)
    raise AssertionError(f"queue {queue} count stayed at {last}, wanted {minimum}")


def _wait_consumer(queue: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    last = -1
    while time.monotonic() < deadline:
        try:
            _, last = _queue_state(queue)
        except pika.exceptions.AMQPError:
            time.sleep(0.1)
            continue
        if last >= 1:
            return
        time.sleep(0.1)
    raise AssertionError(f"queue {queue} consumer_count stayed at {last}")


def _purge(queue: str) -> None:
    conn = connect()
    try:
        conn.channel().queue_purge(queue=queue)
    finally:
        conn.close()


def _delete_queue(queue: str) -> None:
    conn = connect()
    try:
        conn.channel().queue_delete(queue=queue)
    finally:
        conn.close()


def _get(queue: str, timeout: float = 15):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            conn = connect()
        except pika.exceptions.AMQPError as exc:
            last_error = exc
            time.sleep(0.1)
            continue
        try:
            channel = conn.channel()
            method, properties, body = channel.basic_get(queue, auto_ack=False)
            if method is not None:
                channel.basic_ack(method.delivery_tag)
                return properties, body
        finally:
            conn.close()
        time.sleep(0.05)
    raise AssertionError(f"no message arrived in {queue}; last AMQP error={last_error}")


def _publish_callback(payload: dict, *, message_id: str) -> None:
    conn = connect()
    try:
        channel = conn.channel()
        channel.confirm_delivery()
        published = channel.basic_publish(
            exchange=CALLBACKS_EXCHANGE,
            routing_key=f"cb.{payload['task_id']}.{payload['capability']}",
            body=json.dumps(payload).encode(),
            mandatory=True,
            properties=pika.BasicProperties(
                content_type="application/json",
                delivery_mode=2,
                message_id=message_id,
            ),
        )
        assert published is not False
    finally:
        conn.close()


def _seed_one_step(template: str, capability: str, impl_id: str, queue: str) -> None:
    with Session(engine) as session:
        session.add(PipelineTemplate(
            name=template,
            dag_json=json.dumps({"nodes": [capability], "edges": []}),
        ))
        session.add(ServiceImpl(
            capability=capability,
            group_name="integration",
            impl_id=impl_id,
            queue=queue,
            enabled=True,
            health_status="healthy",
            last_heartbeat=datetime.now(),
        ))
        session.commit()


def _make_available() -> None:
    with Session(engine) as session:
        session.exec(
            update(JobOutbox).where(
                JobOutbox.status == OutboxStatus.PENDING
            ).values(available_at=datetime.now() - timedelta(seconds=1))
        )
        session.commit()


def _reset_database() -> None:
    engine.dispose()
    SQLModel.metadata.drop_all(engine)
    create_all_tables()


@pytest.fixture(scope="session", autouse=True)
def isolated_services():
    assert os.getenv("PLATFORM_INTEGRATION_ALLOW_RESTART") == "1", (
        "set PLATFORM_INTEGRATION_ALLOW_RESTART=1 to acknowledge pause/restart"
    )
    assert PG_CONTAINER and RABBIT_CONTAINER
    _assert_isolated_container(PG_CONTAINER)
    _assert_isolated_container(RABBIT_CONTAINER)
    _ensure_running(PG_CONTAINER)
    _ensure_running(RABBIT_CONTAINER)
    _wait_postgres()
    _wait_rabbit()
    declare_topology()
    yield
    _ensure_running(PG_CONTAINER)
    _ensure_running(RABBIT_CONTAINER)
    _wait_postgres()
    _wait_rabbit()


@pytest.fixture(autouse=True)
def clean_database():
    _reset_database()
    declare_topology()
    yield


def test_thirty_tasks_remain_queued_while_rabbit_paused_then_drain():
    capability = "it_queue_30"
    impl_id = "integration.queue30.v1"
    queue = "q.integration.queue30"
    _seed_one_step("it-queue-30", capability, impl_id, queue)
    declare_impl_queue(queue, capability, impl_id)
    _purge(queue)

    _docker("pause", RABBIT_CONTAINER)
    try:
        for number in range(30):
            core.submit(f"it-queued-{number:02d}", template_name="it-queue-30")

        with Session(engine) as session:
            jobs = session.exec(select(JobOutbox)).all()
            steps = session.exec(select(TaskStep)).all()
            assert len(jobs) == 30
            assert len(steps) == 30
            assert {job.status for job in jobs} == {OutboxStatus.PENDING}
            assert {step.status for step in steps} == {StepStatus.PUBLISHING}

        # One real TCP/AMQP attempt must fail without losing the leased row.
        assert outbox.publish_available(limit=1) == 0
        with Session(engine) as session:
            jobs = session.exec(select(JobOutbox)).all()
            assert sum(job.publish_attempts for job in jobs) == 1
            assert {job.status for job in jobs} == {OutboxStatus.PENDING}
    finally:
        if _container_status(RABBIT_CONTAINER) == "paused":
            _docker("unpause", RABBIT_CONTAINER)

    _wait_rabbit()
    _make_available()
    assert outbox.publish_available(limit=30) == 30
    assert _wait_queue_count(queue, 30) == 30

    received_ids = set()
    for _ in range(30):
        properties, body = _get(queue)
        payload = json.loads(body)
        assert properties.delivery_mode == 2
        assert properties.message_id == payload["msg_id"]
        received_ids.add(properties.message_id)
    assert len(received_ids) == 30

    with Session(engine) as session:
        assert {
            job.status for job in session.exec(select(JobOutbox)).all()
        } == {OutboxStatus.PUBLISHED}
        assert {
            step.status for step in session.exec(select(TaskStep)).all()
        } == {StepStatus.DISPATCHED}


def test_unroutable_mandatory_publish_stays_pending_until_binding_exists():
    capability = "it_unroutable"
    impl_id = "integration.unroutable.v1"
    queue = "q.integration.unroutable"
    # A previous opt-in run may have left this durable queue/binding behind.
    # Deleting it is what makes this test repeatably exercise mandatory return.
    _delete_queue(queue)
    _seed_one_step("it-unroutable", capability, impl_id, queue)
    core.submit("it-unroutable-task", template_name="it-unroutable")

    assert outbox.publish_available(limit=1) == 0
    with Session(engine) as session:
        job = session.exec(select(JobOutbox)).one()
        step = session.exec(select(TaskStep)).one()
        assert job.status == OutboxStatus.PENDING
        assert job.publish_attempts == 1
        assert "Unroutable" in (job.last_error or "")
        assert step.status == StepStatus.PUBLISHING

    declare_impl_queue(queue, capability, impl_id)
    _purge(queue)
    _make_available()
    assert outbox.publish_available(limit=1) == 1
    properties, _ = _get(queue)

    with Session(engine) as session:
        job = session.exec(select(JobOutbox)).one()
        step = session.exec(select(TaskStep)).one()
        assert properties.message_id == job.message_id
        assert job.status == OutboxStatus.PUBLISHED
        assert step.status == StepStatus.DISPATCHED


def test_confirm_then_db_crash_republishes_exact_message_id(monkeypatch):
    capability = "it_confirm_crash"
    impl_id = "integration.confirm-crash.v1"
    queue = "q.integration.confirm-crash"
    _seed_one_step("it-confirm-crash", capability, impl_id, queue)
    declare_impl_queue(queue, capability, impl_id)
    _purge(queue)
    core.submit("it-confirm-crash-task", template_name="it-confirm-crash")

    real_complete = outbox._complete_claim

    def crash_after_confirm(_job):
        raise RuntimeError("simulated process death after RabbitMQ confirm")

    monkeypatch.setattr(outbox, "_complete_claim", crash_after_confirm)
    with pytest.raises(RuntimeError, match="after RabbitMQ confirm"):
        outbox.publish_available(limit=1)
    first_properties, first_body = _get(queue)

    with Session(engine) as session:
        job = session.exec(select(JobOutbox)).one()
        stable_id = job.message_id
        assert job.status == OutboxStatus.PUBLISHING
        job.claimed_at = datetime.now() - timedelta(
            seconds=outbox.OUTBOX_LEASE_SECONDS + 1
        )
        session.add(job)
        session.commit()

    monkeypatch.setattr(outbox, "_complete_claim", real_complete)
    assert outbox.publish_available(limit=1) == 1
    second_properties, second_body = _get(queue)

    assert first_properties.message_id == stable_id
    assert second_properties.message_id == stable_id
    assert first_body == second_body
    with Session(engine) as session:
        job = session.exec(select(JobOutbox)).one()
        step = session.exec(select(TaskStep)).one()
        assert job.publish_attempts == 2
        assert job.status == OutboxStatus.PUBLISHED
        assert step.status == StepStatus.DISPATCHED


def test_persistent_job_survives_real_rabbit_restart():
    capability = "it_persistent"
    impl_id = "integration.persistent.v1"
    queue = "q.integration.persistent"
    declare_impl_queue(queue, capability, impl_id)
    _purge(queue)
    message_id = f"persistent-{uuid.uuid4()}"
    payload = {"msg_id": message_id, "probe": "rabbit-restart"}

    publish_job(
        capability,
        impl_id,
        payload,
        message_id=message_id,
        routing_key=f"job.{capability}.{impl_id}",
    )
    assert _wait_queue_count(queue, 1) == 1

    _docker("restart", RABBIT_CONTAINER, timeout=90)
    _wait_rabbit()
    properties, body = _get(queue, timeout=30)
    assert properties.delivery_mode == 2
    assert properties.message_id == message_id
    assert json.loads(body) == payload


def test_callback_database_outage_uses_retry_then_durable_dlq():
    for queue in (CALLBACK_QUEUE, CALLBACK_RETRY_QUEUE, CALLBACK_DLQ_QUEUE):
        _purge(queue)

    consumer_env = os.environ.copy()
    consumer_env["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        [sys.executable, "-m", "orchestrator.consumer"],
        cwd=PROJECT_DIR,
        env=consumer_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    logs = ""
    try:
        _wait_consumer(CALLBACK_QUEUE)

        transient_key = f"transient-{uuid.uuid4()}"
        transient = {
            "msg_type": "callback.progress",
            "task_id": "it-callback-transient",
            "step_id": "unknown-transient-step",
            "attempt": 1,
            "capability": "it_callback",
            "impl_id": "integration.callback.v1",
            "group_name": "integration",
            "idempotency_key": transient_key,
            "progress": 10,
        }

        _docker("stop", "-t", "1", PG_CONTAINER, timeout=15)
        _publish_callback(transient, message_id="it-callback-transient")
        # Seeing the message in the TTL queue proves the original was ACKed only
        # after a confirmed durable retry handoff.
        assert _wait_queue_count(CALLBACK_RETRY_QUEUE, 1, timeout=15) >= 1
        _docker("start", PG_CONTAINER)
        _wait_postgres()

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            engine.dispose()
            with Session(engine) as session:
                if session.exec(
                    select(CallbackLog).where(
                        CallbackLog.idempotency_key == transient_key
                    )
                ).first():
                    break
            time.sleep(0.2)
        else:
            raise AssertionError("transient callback never succeeded after PG recovery")

        permanent = dict(transient)
        permanent["task_id"] = "it-callback-permanent"
        permanent["step_id"] = "unknown-permanent-step"
        permanent["idempotency_key"] = f"permanent-{uuid.uuid4()}"

        _docker("stop", "-t", "1", PG_CONTAINER, timeout=15)
        _publish_callback(permanent, message_id="it-callback-permanent")
        properties, body = _get(CALLBACK_DLQ_QUEUE, timeout=30)
        assert json.loads(body)["idempotency_key"] == permanent["idempotency_key"]
        assert properties.delivery_mode == 2
        assert properties.headers["x-platform-retry-count"] == CALLBACK_MAX_RETRIES
        assert properties.headers["x-platform-failed-at"]
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            captured, _ = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            captured, _ = process.communicate(timeout=5)
        logs += captured or ""
        _ensure_running(PG_CONTAINER)
        _wait_postgres()

    assert any(marker in logs for marker in (
        "OperationalError",
        "connection refused",
        "server closed the connection unexpectedly",
        "could not connect to server",
    )), logs
