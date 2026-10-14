"""RabbitMQ 薄封装：声明持久拓扑并执行 publisher-confirm 发布。"""
import json
from typing import Optional

import pika

from common.config import (
    CALLBACK_DLQ_EXCHANGE,
    CALLBACK_DLQ_QUEUE,
    CALLBACK_DLQ_ROUTING_KEY,
    CALLBACK_RETRY_DELAY_MS,
    CALLBACK_RETRY_EXCHANGE,
    CALLBACK_RETRY_QUEUE,
    CALLBACK_RETRY_ROUTING_KEY,
    CALLBACK_QUEUE,
    CALLBACKS_EXCHANGE,
    JOBS_EXCHANGE,
    PLATFORM_MQ_URL,
)


def connect() -> pika.BlockingConnection:
    """创建带有限超时的连接，供发布器和 consumer 共用。"""
    params = pika.URLParameters(PLATFORM_MQ_URL)
    # MQ 不可达时快速失败；否则调度线程可能在 TCP/AMQP 握手上挂很久。
    params.socket_timeout = 5
    params.stack_timeout = 10
    params.blocked_connection_timeout = 10
    params.connection_attempts = 2
    params.retry_delay = 1
    return pika.BlockingConnection(params)


def declare_topology():
    """幂等声明主拓扑以及独立 retry/DLQ。

    特意不向既有 CALLBACK_QUEUE 添加任何 arguments：RabbitMQ 不允许以不同
    x-arguments 重声明同名队列，线上滚动升级时会直接关闭 channel。
    """
    conn = connect()
    try:
        ch = conn.channel()
        ch.exchange_declare(JOBS_EXCHANGE, "topic", durable=True)
        ch.exchange_declare(CALLBACKS_EXCHANGE, "topic", durable=True)
        ch.exchange_declare(CALLBACK_RETRY_EXCHANGE, "direct", durable=True)
        ch.exchange_declare(CALLBACK_DLQ_EXCHANGE, "direct", durable=True)

        # 保持历史声明完全兼容：durable=True，无 TTL/DLX arguments。
        ch.queue_declare(CALLBACK_QUEUE, durable=True)
        ch.queue_bind(CALLBACK_QUEUE, CALLBACKS_EXCHANGE, routing_key="cb.#")

        ch.queue_declare(
            CALLBACK_RETRY_QUEUE,
            durable=True,
            arguments={
                "x-message-ttl": CALLBACK_RETRY_DELAY_MS,
                "x-dead-letter-exchange": CALLBACKS_EXCHANGE,
                "x-dead-letter-routing-key": "cb.retry",
            },
        )
        ch.queue_bind(
            CALLBACK_RETRY_QUEUE,
            CALLBACK_RETRY_EXCHANGE,
            routing_key=CALLBACK_RETRY_ROUTING_KEY,
        )
        ch.queue_declare(CALLBACK_DLQ_QUEUE, durable=True)
        ch.queue_bind(
            CALLBACK_DLQ_QUEUE,
            CALLBACK_DLQ_EXCHANGE,
            routing_key=CALLBACK_DLQ_ROUTING_KEY,
        )
    finally:
        conn.close()


def declare_impl_queue(queue: str, capability: str, impl_id: str):
    """注册实现时建队列并绑定 job.<capability>.<impl_id>;绑定即热插拔开"""
    conn = connect()
    try:
        ch = conn.channel()
        ch.queue_declare(queue, durable=True)
        ch.queue_bind(queue, JOBS_EXCHANGE, routing_key=f"job.{capability}.{impl_id}")
    finally:
        conn.close()


def unbind_impl_queue(queue: str, capability: str, impl_id: str):
    """enabled=false 时解绑(队列保留,消息不再进) = 热插拔关"""
    conn = connect()
    try:
        ch = conn.channel()
        ch.queue_unbind(queue, JOBS_EXCHANGE, routing_key=f"job.{capability}.{impl_id}")
    finally:
        conn.close()


def publish_job(
    capability: str,
    impl_id: str,
    envelope: dict,
    *,
    message_id: Optional[str] = None,
    routing_key: Optional[str] = None,
):
    """持久化发布一条 job，并等待 broker confirm/return。

    mandatory=True 会把没有任何绑定队列的消息作为失败返回；confirm_delivery
    则确保 broker 已接管消息。函数正常返回才可把 outbox 标成 published。
    """
    conn = connect()
    try:
        ch = conn.channel()
        ch.confirm_delivery()
        published = ch.basic_publish(
            exchange=JOBS_EXCHANGE,
            routing_key=routing_key or f"job.{capability}.{impl_id}",
            body=json.dumps(envelope, ensure_ascii=False),
            mandatory=True,
            properties=pika.BasicProperties(
                content_type="application/json",
                delivery_mode=2,
                message_id=message_id or envelope.get("msg_id"),
            ),
        )
        if published is False:
            raise RuntimeError("RabbitMQ negatively acknowledged job publish")
    finally:
        conn.close()


def publish_callback(task_id: str, capability: str, envelope: dict):
    """给 mock worker / SDK 用:服务侧发回调"""
    conn = connect()
    try:
        ch = conn.channel()
        ch.basic_publish(
            exchange=CALLBACKS_EXCHANGE,
            routing_key=f"cb.{task_id}.{capability}",
            body=json.dumps(envelope, ensure_ascii=False),
            properties=pika.BasicProperties(
                content_type="application/json", delivery_mode=2
            ),
        )
    finally:
        conn.close()
