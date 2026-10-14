"""协议 SDK:算法组接入用的最小 worker 封装。

用法(mock worker 或真实实现都一样):

    from protocol.sdk import ImplWorker

    def handle(job: dict, report_progress) -> dict:
        report_progress(50, "干到一半")
        return {"artifacts": [...], "summary": {...}, "usage": [...]}

    ImplWorker(queue="q.groupA.char", impl_id="groupA.char.v2",
               group_name="group-a", capability="char_designer",
               handler=handle).run()
"""
import json
import os
import traceback

import pika

try:
    from protocol.envelope import (
        CallbackError,
        CallbackFailed,
        CallbackOutput,
        CallbackProgress,
        CallbackSucceeded,
    )
except ImportError:  # 两文件平铺拷走(不放 protocol 包里)时走这里
    from envelope import (
        CallbackError,
        CallbackFailed,
        CallbackOutput,
        CallbackProgress,
        CallbackSucceeded,
    )


class ImplWorker:
    def __init__(self, queue: str, impl_id: str, group_name: str,
                 capability: str, handler):
        self.queue = queue
        self.impl_id = impl_id
        self.group_name = group_name
        self.capability = capability
        self.handler = handler
        self._seq = 0

    def _identity(self, job: dict) -> dict:
        return {
            "task_id": job["task_id"],
            "step_id": job["step_id"],
            "trace_id": job.get("trace_id"),
            "capability": self.capability,
            "impl_id": self.impl_id,
            "group_name": self.group_name,
        }

    def _publish(self, ch, job: dict, envelope):
        ch.basic_publish(
            exchange=job.get("reply_to", "game.callbacks"),
            routing_key=job["reply_routing_key"],
            body=envelope.model_dump_json(),
            properties=pika.BasicProperties(
                content_type="application/json", delivery_mode=2
            ),
        )

    def _on_message(self, ch, method, properties, body):
        job = json.loads(body)
        base = self._identity(job)
        attempt = int(job.get("attempt") or 1)

        def report_progress(progress: int, detail: str = None, metrics: dict = None):
            self._seq += 1
            self._publish(ch, job, CallbackProgress(
                **base,
                attempt=attempt,
                idempotency_key=(
                    f"{job['task_id']}:{job['step_id']}:"
                    f"progress:attempt{attempt}:{self._seq}"
                ),
                progress=progress, stage_detail=detail, metrics=metrics or {},
            ))

        try:
            result = self.handler(job, report_progress) or {}
            self._publish(ch, job, CallbackSucceeded(
                **base,
                attempt=attempt,
                idempotency_key=(
                    f"{job['task_id']}:{job['step_id']}:result:attempt{attempt}"
                ),
                output=CallbackOutput(
                    artifacts=result.get("artifacts", []),
                    summary=result.get("summary", {}),
                ),
                usage=result.get("usage", []),
            ))
        except Exception as e:
            traceback.print_exc()
            self._publish(ch, job, CallbackFailed(
                **base,
                attempt=attempt,
                idempotency_key=(
                    f"{job['task_id']}:{job['step_id']}:error:"
                    f"attempt{attempt}"
                ),
                error=CallbackError(code="INTERNAL", retryable=True,
                                    message=str(e)),
            ))
        finally:
            ch.basic_ack(delivery_tag=method.delivery_tag)

    def run(self):
        # 环境变量直读,不依赖平台 common 包 —— envelope.py + sdk.py 两文件即可拷走接入
        mq_url = os.getenv("PLATFORM_MQ_URL", "amqp://guest:guest@localhost:5672/%2F")
        conn = pika.BlockingConnection(pika.URLParameters(mq_url))
        ch = conn.channel()
        ch.queue_declare(self.queue, durable=True)
        ch.basic_qos(prefetch_count=1)
        ch.basic_consume(self.queue, self._on_message)
        print(f"[{self.impl_id}] consuming {self.queue} ...")
        ch.start_consuming()
