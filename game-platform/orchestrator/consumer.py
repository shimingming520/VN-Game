"""Durable callback consumer with bounded retry and a dead-letter queue."""
import json
import logging
import sys
from datetime import datetime, timezone

import pika

from common.config import (
    CALLBACK_DLQ_EXCHANGE,
    CALLBACK_DLQ_ROUTING_KEY,
    CALLBACK_MAX_RETRIES,
    CALLBACK_QUEUE,
    CALLBACK_RETRY_EXCHANGE,
    CALLBACK_RETRY_ROUTING_KEY,
)
from common.models import create_all_tables
from common.mq import connect, declare_topology
from orchestrator.core import handle_callback

logger = logging.getLogger(__name__)

_RETRY_HEADER = "x-platform-retry-count"
_LAST_ERROR_HEADER = "x-platform-last-error"
_ORIGINAL_ROUTE_HEADER = "x-platform-original-routing-key"
_FAILED_AT_HEADER = "x-platform-failed-at"


def _retry_count(properties) -> int:
    headers = getattr(properties, "headers", None) or {}
    raw = headers.get(_RETRY_HEADER, 0)
    if isinstance(raw, bytes):
        raw = raw.decode("ascii", errors="ignore")
    try:
        return max(int(raw), 0)
    except (TypeError, ValueError):
        return 0


def _republish_properties(properties, headers: dict) -> pika.BasicProperties:
    """Preserve useful identity fields while forcing durable delivery."""
    return pika.BasicProperties(
        content_type=getattr(properties, "content_type", None) or "application/json",
        content_encoding=getattr(properties, "content_encoding", None),
        headers=headers,
        delivery_mode=2,
        priority=getattr(properties, "priority", None),
        correlation_id=getattr(properties, "correlation_id", None),
        reply_to=getattr(properties, "reply_to", None),
        message_id=getattr(properties, "message_id", None),
        timestamp=getattr(properties, "timestamp", None),
        type=getattr(properties, "type", None),
        app_id=getattr(properties, "app_id", None),
    )


def _publish_failure(
    publisher_channel,
    *,
    method,
    properties,
    body: bytes,
    error: BaseException,
) -> None:
    retry_count = _retry_count(properties)
    original_headers = dict(getattr(properties, "headers", None) or {})
    original_headers.setdefault(
        _ORIGINAL_ROUTE_HEADER,
        getattr(method, "routing_key", "cb.unknown"),
    )
    original_headers[_LAST_ERROR_HEADER] = (
        f"{type(error).__name__}: {error}"[:1000]
    )

    if retry_count < max(CALLBACK_MAX_RETRIES, 0):
        original_headers[_RETRY_HEADER] = retry_count + 1
        exchange = CALLBACK_RETRY_EXCHANGE
        routing_key = CALLBACK_RETRY_ROUTING_KEY
    else:
        original_headers[_RETRY_HEADER] = retry_count
        original_headers[_FAILED_AT_HEADER] = datetime.now(timezone.utc).isoformat()
        exchange = CALLBACK_DLQ_EXCHANGE
        routing_key = CALLBACK_DLQ_ROUTING_KEY

    published = publisher_channel.basic_publish(
        exchange=exchange,
        routing_key=routing_key,
        body=body,
        mandatory=True,
        properties=_republish_properties(properties, original_headers),
    )
    if published is False:
        raise RuntimeError("RabbitMQ negatively acknowledged callback retry/DLQ")


def process_delivery(channel, publisher_channel, method, properties, body: bytes):
    """Process exactly one delivery; ACK only after success or confirmed handoff."""
    try:
        handle_callback(json.loads(body))
    except Exception as callback_error:
        logger.exception(
            "callback processing failed delivery_tag=%s retry=%s",
            method.delivery_tag,
            _retry_count(properties),
        )
        try:
            _publish_failure(
                publisher_channel,
                method=method,
                properties=properties,
                body=body,
                error=callback_error,
            )
        except Exception:
            # Retry/DLQ publish is not confirmed: keep the original delivery.
            logger.exception("failed to hand callback to retry/DLQ; requeue original")
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
            return

        channel.basic_ack(delivery_tag=method.delivery_tag)
        return

    channel.basic_ack(delivery_tag=method.delivery_tag)


def main():
    logging.basicConfig(level=logging.INFO)
    # Consumer may roll before API.  Ensure the outbox table exists before a
    # successful callback calls advance(); DDL failure exits and lets K8s retry.
    create_all_tables()
    declare_topology()
    conn = connect()
    consume_channel = conn.channel()
    publisher_channel = conn.channel()
    publisher_channel.confirm_delivery()
    consume_channel.basic_qos(prefetch_count=8)

    def on_message(channel, method, properties, body):
        process_delivery(channel, publisher_channel, method, properties, body)

    consume_channel.basic_consume(CALLBACK_QUEUE, on_message)
    print(f"🎧 orchestrator consuming {CALLBACK_QUEUE} ...")
    try:
        consume_channel.start_consuming()
    except KeyboardInterrupt:
        sys.exit(0)
    finally:
        if conn.is_open:
            conn.close()


if __name__ == "__main__":
    main()
