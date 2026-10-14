from types import SimpleNamespace

import pika
import pytest

from common import mq
from common.config import (
    CALLBACK_DLQ_EXCHANGE,
    CALLBACK_MAX_RETRIES,
    CALLBACK_QUEUE,
    CALLBACK_RETRY_EXCHANGE,
    JOBS_EXCHANGE,
)
from orchestrator import consumer


class ConsumeChannel:
    def __init__(self):
        self.acks = []
        self.nacks = []

    def basic_ack(self, **kwargs):
        self.acks.append(kwargs)

    def basic_nack(self, **kwargs):
        self.nacks.append(kwargs)


class PublisherChannel:
    def __init__(self, result=True, error=None):
        self.result = result
        self.error = error
        self.publishes = []

    def basic_publish(self, **kwargs):
        self.publishes.append(kwargs)
        if self.error:
            raise self.error
        return self.result


def _delivery(headers=None):
    method = SimpleNamespace(delivery_tag=7, routing_key="cb.task.story_planner")
    properties = pika.BasicProperties(
        content_type="application/json",
        headers=headers or {},
        message_id="callback-1",
    )
    return method, properties


def test_successful_callback_is_acked(monkeypatch):
    consume = ConsumeChannel()
    publisher = PublisherChannel()
    method, properties = _delivery()
    monkeypatch.setattr(consumer, "handle_callback", lambda payload: None)

    consumer.process_delivery(consume, publisher, method, properties, b"{}")

    assert consume.acks == [{"delivery_tag": 7}]
    assert consume.nacks == []
    assert publisher.publishes == []


def test_failure_is_confirmed_to_durable_retry_before_ack(monkeypatch):
    consume = ConsumeChannel()
    publisher = PublisherChannel()
    method, properties = _delivery()

    def fail(_payload):
        raise RuntimeError("temporary database failure")

    monkeypatch.setattr(consumer, "handle_callback", fail)
    consumer.process_delivery(consume, publisher, method, properties, b"{}")

    publish = publisher.publishes[0]
    assert publish["exchange"] == CALLBACK_RETRY_EXCHANGE
    assert publish["mandatory"] is True
    assert publish["properties"].delivery_mode == 2
    assert publish["properties"].headers["x-platform-retry-count"] == 1
    assert consume.acks == [{"delivery_tag": 7}]
    assert consume.nacks == []


def test_exhausted_callback_goes_to_durable_dlq(monkeypatch):
    consume = ConsumeChannel()
    publisher = PublisherChannel()
    method, properties = _delivery(
        {"x-platform-retry-count": CALLBACK_MAX_RETRIES}
    )
    monkeypatch.setattr(
        consumer,
        "handle_callback",
        lambda _payload: (_ for _ in ()).throw(ValueError("poison message")),
    )

    consumer.process_delivery(consume, publisher, method, properties, b"{}")

    publish = publisher.publishes[0]
    assert publish["exchange"] == CALLBACK_DLQ_EXCHANGE
    assert publish["properties"].delivery_mode == 2
    assert "x-platform-failed-at" in publish["properties"].headers
    assert consume.acks == [{"delivery_tag": 7}]


def test_retry_publish_failure_nacks_original(monkeypatch):
    consume = ConsumeChannel()
    publisher = PublisherChannel(error=OSError("mq publish failed"))
    method, properties = _delivery()
    monkeypatch.setattr(
        consumer,
        "handle_callback",
        lambda _payload: (_ for _ in ()).throw(RuntimeError("db failed")),
    )

    consumer.process_delivery(consume, publisher, method, properties, b"{}")

    assert consume.acks == []
    assert consume.nacks == [{"delivery_tag": 7, "requeue": True}]


def test_existing_callback_queue_declaration_remains_argument_compatible(monkeypatch):
    class Channel:
        def __init__(self):
            self.queue_declarations = []

        def exchange_declare(self, *_args, **_kwargs):
            pass

        def queue_declare(self, queue, **kwargs):
            self.queue_declarations.append((queue, kwargs))

        def queue_bind(self, *_args, **_kwargs):
            pass

    class Connection:
        def __init__(self):
            self.channel_obj = Channel()
            self.closed = False

        def channel(self):
            return self.channel_obj

        def close(self):
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(mq, "connect", lambda: connection)

    mq.declare_topology()

    callback = next(
        kwargs for queue, kwargs in connection.channel_obj.queue_declarations
        if queue == CALLBACK_QUEUE
    )
    assert callback == {"durable": True}
    assert any(
        kwargs.get("arguments", {}).get("x-message-ttl")
        for queue, kwargs in connection.channel_obj.queue_declarations
        if queue != CALLBACK_QUEUE
    )


def test_job_publish_uses_confirm_mandatory_persistent_and_exact_route(monkeypatch):
    class Channel:
        def __init__(self):
            self.confirmed = False
            self.publish = None

        def confirm_delivery(self):
            self.confirmed = True

        def basic_publish(self, **kwargs):
            self.publish = kwargs
            return True

    class Connection:
        def __init__(self):
            self.channel_obj = Channel()

        def channel(self):
            return self.channel_obj

        def close(self):
            pass

    connection = Connection()
    monkeypatch.setattr(mq, "connect", lambda: connection)

    mq.publish_job(
        "story_planner",
        "group-ref.story_planner.v1",
        {"msg_id": "job-1"},
        message_id="job-1",
        routing_key="job.story_planner.group-ref.story_planner.v1",
    )

    published = connection.channel_obj.publish
    assert connection.channel_obj.confirmed is True
    assert published["exchange"] == JOBS_EXCHANGE
    assert published["routing_key"] == (
        "job.story_planner.group-ref.story_planner.v1"
    )
    assert published["mandatory"] is True
    assert published["properties"].delivery_mode == 2
    assert published["properties"].message_id == "job-1"


def test_consumer_initializes_schema_before_mq_topology(monkeypatch):
    calls = []

    class Consume:
        def basic_qos(self, **_kwargs):
            pass

        def basic_consume(self, *_args, **_kwargs):
            pass

        def start_consuming(self):
            raise KeyboardInterrupt

    class Publisher:
        def confirm_delivery(self):
            pass

    class Connection:
        is_open = True

        def __init__(self):
            self.channels = [Consume(), Publisher()]

        def channel(self):
            return self.channels.pop(0)

        def close(self):
            calls.append("close")

    monkeypatch.setattr(
        consumer, "create_all_tables", lambda: calls.append("create_tables")
    )
    monkeypatch.setattr(
        consumer, "declare_topology", lambda: calls.append("declare_topology")
    )
    monkeypatch.setattr(consumer, "connect", Connection)

    with pytest.raises(SystemExit):
        consumer.main()

    assert calls[:2] == ["create_tables", "declare_topology"]
