"""平台协议适配层(阶段2:内部适配)。

职责:把现有 7 个 Celery 服务包装成"平台实现"——
  消费 Job envelope → send_task 调现有 Celery task → 等结果
  → 从 api_usage 表汇总本步 token 用量 → 发 callback envelope。

现有算法代码零改动;本文件也不 import game-platform 的任何代码,
只遵循 envelope JSON 契约(证明协议跨代码库/跨语言成立)。

用法:
    python -m services.platform_adapter.adapter                 # 全 7 能力
    python -m services.platform_adapter.adapter char_designer   # 单能力
"""
import functools
import json
import os
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pika
import redis
import requests
from celery.exceptions import TimeoutError as CeleryTimeoutError

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from common.celery_app import celery_app  # noqa: E402
from common.config import REDIS_URL  # noqa: E402

# ── 配置(环境变量,默认对齐本地联调) ──
PLATFORM_MQ_URL = os.getenv("PLATFORM_MQ_URL", "amqp://guest:guest@localhost:5672/%2F")
PLATFORM_API = os.getenv("PLATFORM_API", "http://localhost:8000")
GROUP_NAME = os.getenv("PLATFORM_GROUP_NAME", "group-ref")
HEARTBEAT_INTERVAL = 20
DEFAULT_ADAPTER_CONCURRENCY = max(1, int(os.getenv("ADAPTER_CONCURRENCY", "10")))
DISPATCH_MARKER_TTL = int(os.getenv("ADAPTER_DISPATCH_MARKER_TTL", "1209600"))
DISPATCH_RECOVERY_GRACE = int(os.getenv("ADAPTER_DISPATCH_RECOVERY_GRACE", "300"))
WORKER_RECOVERY_GRACE = int(os.getenv("ADAPTER_WORKER_RECOVERY_GRACE", "2100"))
RESULT_POLL_INTERVAL = int(os.getenv("ADAPTER_RESULT_POLL_INTERVAL", "60"))
# 平台侧 job 交换机(名字来自平台 common/config.py 的 JOBS_EXCHANGE,
# 这里按契约写死常量,不 import 平台代码)
JOBS_EXCHANGE = "game.jobs"

# capability → (现有 Celery task 名, 内部队列)
#
# 主生成任务允许运行十几个小时甚至更久。Adapter 不设置业务执行截止时间，
# 始终等待 Celery 返回真实终态；超时治理由外部模型调用自身的请求超时、任务
# 心跳和人工取消负责，不能在桥接层把仍在正常生成的游戏误判为失败。
CAPABILITY_MAP = {
    "story_planner":  ("services.story_planner.tasks.parse_novel_to_draft", "story_queue"),
    "info_extractor": ("services.info_extractor.tasks.extract_info", "extract_queue"),
    "char_designer":  ("services.char_designer.tasks.generate_characters", "char_queue"),
    "scene_painter":  ("services.scene_painter.tasks.generate_scenes", "scene_queue"),
    "audio_producer": ("services.audio_producer.tasks.generate_audio", "audio_queue"),
    "sound_arranger": ("services.sound_arranger.tasks.generate_sound", "sound_queue"),
    "game_builder":   ("services.game_builder.tasks.build_game_package", "build_queue"),
}

_dispatch_redis = redis.Redis.from_url(
    REDIS_URL,
    decode_responses=True,
    socket_connect_timeout=5,
    socket_timeout=5,
)

_PREPARE_MANUAL_RESTART_SCRIPT = """
if redis.call('exists', KEYS[1]) == 1 then
    return 0
end
redis.call('set', KEYS[1], ARGV[1], 'EX', tonumber(ARGV[2]))
redis.call('del', KEYS[2])
return 1
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _usage_snapshot(task_id: str, service: str) -> int:
    """记录 api_usage 当前最大 id,任务完成后取增量 = 本步用量"""
    from sqlmodel import Session, text
    from common.usage_tracker import _engine
    with Session(_engine) as s:
        row = s.exec(text(
            "select coalesce(max(id),0) from api_usage "
            "where task_id=:t and service=:svc"
        ).bindparams(t=task_id, svc=service)).first()
        return row[0] if row else 0


def _collect_usage(task_id: str, service: str, since_id: int) -> list:
    """把 api_usage 增量行转成 envelope 的 usage 数组"""
    from sqlmodel import Session, text
    from common.usage_tracker import _engine
    with Session(_engine) as s:
        rows = s.exec(text(
            "select api_type, model, prompt_tokens, completion_tokens, "
            "image_count, tts_chars, music_count from api_usage "
            "where task_id=:t and service=:svc and id>:i"
        ).bindparams(t=task_id, svc=service, i=since_id)).all()
    usage = []
    for api_type, model, pt, ct, img, tts, music in rows:
        usage.append({
            "api_type": "music" if api_type == "music" else api_type,
            "model": model,
            "prompt_tokens": pt or 0,
            "completion_tokens": ct or 0,
            "image_count": img or 0,
            "tts_chars": tts or 0,
            "requests": 1,
        })
    return usage


class CapabilityAdapter:
    def __init__(self, capability: str):
        self.capability = capability
        self.celery_task, self.celery_queue = CAPABILITY_MAP[capability]
        self.impl_id = f"{GROUP_NAME}.{capability}.celery"
        self.queue = f"q.{GROUP_NAME}.{capability}"
        self.consuming = False  # 心跳只在真的挂上队列时上报
        # 每个能力独立限流。RabbitMQ prefetch 与线程池大小保持一致，保证
        # 同一能力最多只有 concurrency 条未确认消息/长任务，不会因为瞬时
        # 堆积而无限创建线程。可用 ADAPTER_CONCURRENCY_CHAR_DESIGNER 等覆盖。
        env_name = f"ADAPTER_CONCURRENCY_{capability.upper()}"
        self.concurrency = max(
            1, int(os.getenv(env_name, str(DEFAULT_ADAPTER_CONCURRENCY)))
        )
        self.executor = ThreadPoolExecutor(
            max_workers=self.concurrency,
            thread_name_prefix=f"adapter-{capability}",
        )

    # ── 注册 + 心跳 ──
    def register(self):
        r = requests.post(f"{PLATFORM_API}/api/impls", json={
            "group_name": GROUP_NAME,
            "capability": self.capability,
            "impl_id": self.impl_id,
            "queue": self.queue,
            "manifest": {
                "adapter": "celery",
                "celery_task": self.celery_task,
                "concurrency": self.concurrency,
            },
            "enabled": True,
        }, timeout=10)
        r.raise_for_status()
        print(f"✅ registered {self.impl_id}")

    def heartbeat_loop(self):
        while True:
            # 只在真的挂上队列时才上报。7 个能力跑在同一进程的 7 条线程里,
            # 某条线程死掉时进程照活 —— 无条件上报会让平台看到一路绿灯的心跳,
            # 而消息其实没人消费(踩过:char_designer 卡了三天心跳一直是 healthy)。
            if self.consuming:
                try:
                    requests.post(
                        f"{PLATFORM_API}/api/impls/{self.impl_id}/heartbeat", timeout=5
                    )
                except Exception as e:
                    print(f"⚠️  [{self.capability}] heartbeat failed: {e}")
            time.sleep(HEARTBEAT_INTERVAL)

    # ── 前置检查:平台是否还需要这个 job ──
    def _task_is_stale(self, job: dict) -> bool:
        """平台已给该任务/步骤下了终态 → 这条消息是陈旧重投,不该再跑。

        MQ 的重投不经过平台,attempt 恒为 1,平台的 max_attempts 拦不住它;
        没有这道检查,一条永远 ack 不掉的消息会被无限重投(踩过:一个已取消的
        任务被重投了三天,还因为 prefetch=1 把后面所有任务的 job 全堵死)。
        查不到状态时返回 False —— 宁可多跑一次,也不要漏跑。
        """
        try:
            r = requests.get(
                f"{PLATFORM_API}/api/v1/tasks/{job['task_id']}", timeout=5
            )
            if r.status_code == 404:
                return True
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"⚠️  [{self.capability}] 状态预检失败,按需要执行处理: {e}")
            return False
        context = ((job.get("input") or {}).get("context") or {})
        manual_stage_run = (
            context.get("dispatch_reason") == "manual_stage_run"
            and context.get("target_capability") == self.capability
        )
        if data.get("status") in ("succeeded", "failed", "cancelled"):
            return True
        for s in data.get("steps") or []:
            if s.get("capability") == self.capability:
                current_attempt = int(s.get("attempt") or 0)
                message_attempt = int(job.get("attempt") or 1)
                if current_attempt > message_attempt:
                    return True
                # 合法 outbox 在发布前已把步骤原子置为 PUBLISHING。若同 attempt
                # 的旧消息在人工重启 reset→下一次 dispatch 的短窗口到达，平台
                # 会显示 PENDING；它不属于当前状态机，必须丢弃，避免旧模块再烧
                # 一轮额度。正常新消息不会在 PENDING 状态被 broker 收到。
                # 单模块调试会故意把目标阶段设为 pending 再发新 attempt；
                # 这是唯一允许“pending 状态收到合法 job”的场景。
                if manual_stage_run and current_attempt == message_attempt:
                    return s.get("status") in ("succeeded", "failed", "cancelled", "skipped")
                return s.get("status") in (
                    "pending",
                    "succeeded",
                    "failed",
                    "cancelled",
                    "skipped",
                )
        return False

    def _execution_id(self, job: dict) -> str:
        """同一个中台 step attempt 永远映射到同一个 Celery task id。"""
        return f"platform-{job['step_id']}-{int(job.get('attempt') or 1)}"

    def _prepare_platform_retry(self, job: dict) -> None:
        """只有中台的新 attempt 才有权把算法阶段从 FAILED 重置为 PENDING。"""
        attempt = int(job.get("attempt") or 1)
        if attempt <= 1:
            return
        from sqlalchemy import update as sa_update
        from sqlmodel import Session
        from common.platform_db import platform_engine
        from common.task_models import NovelTask, TaskStatus

        stage_field = f"{self.capability}_status"
        with Session(platform_engine) as session:
            result = session.exec(
                sa_update(NovelTask)
                .where(NovelTask.task_id == job["task_id"])
                .where(getattr(NovelTask, stage_field) == TaskStatus.FAILED)
                .values({
                    stage_field: TaskStatus.PENDING,
                    "overall_status": TaskStatus.PROCESSING,
                    "error_message": None,
                })
                .execution_options(synchronize_session=False)
            )
            session.commit()
        if result.rowcount:
            print(
                f"🔁 [{self.capability}] task {job['task_id']} "
                f"prepare platform attempt {attempt}"
            )

    def _prepare_manual_restart(self, job: dict) -> bool:
        """人工重启时只释放一次旧阶段租约，让新 attempt 立即接管。

        中台已先把未完成阶段重置为 PENDING，并用新的 platform attempt 隔离
        旧回调。这里以 restart_id 做 Redis 幂等标记，再原子删除旧 Worker 的
        stage lease。新 Worker 随后会走现有 PostgreSQL fencing claim；旧 Worker
        即使晚到，也不能再提交阶段终态。
        """
        context = ((job.get("input") or {}).get("context") or {})
        if context.get("dispatch_reason") != "manual_restart":
            return False

        restart_id = str(context.get("restart_id") or "").strip()
        if not restart_id:
            raise RuntimeError("manual restart job missing restart_id")

        task_id = job["task_id"]
        marker_key = (
            f"platform-adapter:restart-prepared:{restart_id}:"
            f"{task_id}:{self.capability}"
        )
        lease_key = f"novel-game:stage-lease:{task_id}:{self.capability}"
        prepared = bool(_dispatch_redis.eval(
            _PREPARE_MANUAL_RESTART_SCRIPT,
            2,
            marker_key,
            lease_key,
            str(time.time()),
            DISPATCH_MARKER_TTL,
        ))
        if prepared:
            print(
                f"🔁 [{self.capability}] task {task_id} manual restart "
                f"prepared restart_id={restart_id}"
            )
        return prepared

    def _prepare_manual_stage_run(self, job: dict) -> bool:
        """单模块强制运行时释放该模块旧 Worker 的 Redis 租约。"""
        context = ((job.get("input") or {}).get("context") or {})
        if context.get("dispatch_reason") != "manual_stage_run":
            return False
        run_id = str(context.get("stage_run_id") or "").strip()
        if not run_id:
            raise RuntimeError("manual stage run job missing stage_run_id")
        task_id = job["task_id"]
        marker_key = (
            f"platform-adapter:stage-run-prepared:{run_id}:"
            f"{task_id}:{self.capability}"
        )
        lease_key = f"novel-game:stage-lease:{task_id}:{self.capability}"
        prepared = bool(_dispatch_redis.eval(
            _PREPARE_MANUAL_RESTART_SCRIPT,
            2,
            marker_key,
            lease_key,
            str(time.time()),
            DISPATCH_MARKER_TTL,
        ))
        if prepared:
            print(
                f"🔁 [{self.capability}] task {task_id} manual stage run "
                f"prepared stage_run_id={run_id}"
            )
        return prepared

    def _send_celery_once(self, job: dict):
        """把一次平台 attempt 尽量收敛成一次 Celery 投递。

        Redis marker 解决 Adapter/RabbitMQ 重投造成的重复 send_task；即使极端
        崩溃窗口产生重复消息，NovelTask 的 PENDING→PROCESSING 原子抢占仍会
        保证只有一个 Worker 真正执行烧 token 的主体。
        """
        execution_id = self._execution_id(job)
        marker_key = f"platform-adapter:submitted:{execution_id}"
        marker_value = str(time.time())

        try:
            claimed = bool(_dispatch_redis.set(
                marker_key,
                marker_value,
                nx=True,
                ex=DISPATCH_MARKER_TTL,
            ))
        except Exception as exc:
            # Redis marker 不可用时仍允许业务继续；数据库阶段原子抢占是最后防线。
            print(f"⚠️  [{self.capability}] dispatch marker unavailable: {exc}")
            claimed = True

        result = celery_app.AsyncResult(execution_id)
        if not claimed:
            try:
                if result.state != "PENDING":
                    print(
                        f"🔗 [{self.capability}] reuse Celery {execution_id} "
                        f"state={result.state}"
                    )
                    return result
            except Exception as exc:
                print(f"⚠️  [{self.capability}] result state lookup failed: {exc}")

            # marker 可能写入后进程恰好死在 send_task 前。给首次投递留出启动
            # 时间；仍然 PENDING 时发送同 task_id 的恢复消息。即使原消息只是
            # 排队较久，阶段原子锁也会让重复消息只等待、不重复调用模型。
            deadline = time.monotonic() + DISPATCH_RECOVERY_GRACE
            while time.monotonic() < deadline:
                time.sleep(5)
                try:
                    if result.state != "PENDING":
                        return result
                except Exception:
                    pass
            print(f"⚠️  [{self.capability}] recovering pending Celery {execution_id}")

        try:
            self._prepare_platform_retry(job)
            self._prepare_manual_restart(job)
            self._prepare_manual_stage_run(job)
            return celery_app.send_task(
                self.celery_task,
                args=[job["task_id"]],
                queue=self.celery_queue,
                task_id=execution_id,
                headers={
                    "platform_step_id": job["step_id"],
                    "platform_attempt": int(job.get("attempt") or 1),
                },
            )
        except Exception:
            if claimed:
                try:
                    _dispatch_redis.delete(marker_key)
                except Exception:
                    pass
            raise

    def _wait_for_celery_result(self, async_result, job: dict) -> None:
        """等待终态，并在整个 Worker Pod 丢失时恢复同一个业务 attempt。

        Redis broker 的 visibility timeout 为了支持 48h 长任务设得很长，不能
        靠它及时发现 Pod 死亡。Worker 执行期间会续租 stage lease；lease 消失
        且结果仍未完成超过宽限期时，Adapter 用相同 task id 补发基础设施恢复
        消息。阶段原子抢占保证这不是一次新的业务重试。
        """
        execution_id = self._execution_id(job)
        lease_key = f"novel-game:stage-lease:{job['task_id']}:{self.capability}"
        last_recovery = time.monotonic()

        while True:
            try:
                async_result.get(
                    timeout=RESULT_POLL_INTERVAL,
                    propagate=True,
                    disable_sync_subtasks=False,
                )
                return
            except CeleryTimeoutError:
                pass

            now = time.monotonic()
            if now - last_recovery < WORKER_RECOVERY_GRACE:
                continue
            try:
                lease_exists = bool(_dispatch_redis.exists(lease_key))
            except Exception as exc:
                print(f"⚠️  [{self.capability}] lease lookup failed: {exc}")
                continue
            if lease_exists:
                last_recovery = now
                continue

            print(
                f"🔁 [{self.capability}] no stage lease for task {job['task_id']}; "
                f"recover Celery {execution_id} without increasing platform attempt"
            )
            celery_app.send_task(
                self.celery_task,
                args=[job["task_id"]],
                queue=self.celery_queue,
                task_id=execution_id,
                headers={
                    "platform_step_id": job["step_id"],
                    "platform_attempt": int(job.get("attempt") or 1),
                    "infrastructure_recovery": True,
                },
            )
            last_recovery = now

    @staticmethod
    def _is_retryable_error(exc: Exception) -> bool:
        """死循环/预算/参数错误不再让中台重复跑三轮。"""
        name = type(exc).__name__.lower()
        message = str(exc).lower()
        non_retryable_markers = (
            "usagebudgetexceeded",
            "softtimelimitexceeded",
            "timelimitexceeded",
            "soft time limit",
            "time limit exceeded",
            "usage budget exceeded",
            "invalid task_id",
            "等待中台创建下一次业务重试",
        )
        return not any(
            marker in name or marker in message
            for marker in non_retryable_markers
        )

    # ── 回调 ──
    def _publish_cb(self, ch, job: dict, payload: dict) -> bool:
        """发回调。ch 属于 pika 的 I/O 线程,只能由它自己碰,所以工作线程
        通过 add_callback_threadsafe 把发送动作排回去(见 _on_job_worker)。"""
        published = ch.basic_publish(
            exchange=job.get("reply_to", "game.callbacks"),
            routing_key=job["reply_routing_key"],
            body=json.dumps(payload, ensure_ascii=False),
            properties=pika.BasicProperties(content_type="application/json", delivery_mode=2),
            mandatory=True,
        )
        return published is not False

    def _publish_threadsafe(
        self,
        conn,
        ch,
        job: dict,
        payload: dict,
        *,
        wait_for_confirm: bool = False,
    ) -> bool:
        """从工作线程发回调；终态可等待 RabbitMQ publisher confirm。"""
        completed = threading.Event()
        result = {"ok": False}

        def _do():
            try:
                result["ok"] = self._publish_cb(ch, job, payload)
            except Exception as e:
                print(f"⚠️  [{self.capability}] 回调发送失败({payload['msg_type']}): {e}")
            finally:
                completed.set()
        try:
            conn.add_callback_threadsafe(_do)
        except Exception as e:
            print(f"⚠️  [{self.capability}] 回调无法入队({payload['msg_type']}): {e}")
            return False
        if not wait_for_confirm:
            return True
        if not completed.wait(timeout=30):
            print(f"⚠️  [{self.capability}] 回调确认超时({payload['msg_type']})")
            return False
        return result["ok"]

    def _base(self, job: dict) -> dict:
        return {
            "schema_version": "1.0",
            "msg_id": str(uuid.uuid4()),
            "emitted_at": _now(),
            "task_id": job["task_id"],
            "step_id": job["step_id"],
            "trace_id": job.get("trace_id"),
            "capability": self.capability,
            "impl_id": self.impl_id,
            "group_name": GROUP_NAME,
        }

    # ── 消费 Job ──
    def on_job(self, conn, ch, method, properties, body):
        """I/O 线程回调:只做派发,绝不阻塞。

        算法要跑几十分钟到几小时,在这里等就会占死 pika 的 I/O 循环、答不上
        broker 心跳 → 连接被单方面掐 → 干完活发不出回调也 ack 不掉 → 消息重投,
        无限循环(踩过:一个已取消的任务被每小时重投,连跑三天)。
        """
        job = json.loads(body)
        task_id = job["task_id"]
        print(f"📥 [{self.capability}] job for task {task_id} "
              f"(attempt {job.get('attempt', 1)})")
        self.executor.submit(self._on_job_worker, conn, ch, method, job)

    def _on_job_worker(self, conn, ch, method, job: dict):
        task_id = job["task_id"]
        attempt = job.get("attempt", 1)
        ack = functools.partial(self._ack_threadsafe, conn, ch, method.delivery_tag)
        nack = functools.partial(self._nack_threadsafe, conn, ch, method.delivery_tag)
        terminal_published = False

        # 陈旧重投直接丢掉:平台已给终态,再跑一遍是纯浪费(还会烧 token)
        if self._task_is_stale(job):
            print(f"🗑️  [{self.capability}] task {task_id} 已是终态,丢弃陈旧消息")
            ack()
            return

        started_at = _now()
        since_id = 0
        try:
            since_id = _usage_snapshot(task_id, self.capability)
        except Exception:
            traceback.print_exc()

        self._publish_threadsafe(conn, ch, job, {
            **self._base(job),
            "msg_type": "callback.progress",
            "attempt": attempt,
            "idempotency_key": f"{task_id}:{job['step_id']}:progress:start{attempt}",
            "progress": 5,
            "stage_detail": f"{self.capability} 已开始(celery: {self.celery_task})",
            "metrics": {},
        })

        try:
            # 调现有 Celery task(签名统一为 task_id → task_id)
            async_result = self._send_celery_once(job)
            self._wait_for_celery_result(async_result, job)

            usage = []
            try:
                usage = _collect_usage(task_id, self.capability, since_id)
            except Exception:
                traceback.print_exc()

            terminal_published = self._publish_threadsafe(conn, ch, job, {
                **self._base(job),
                "msg_type": "callback.succeeded",
                "attempt": attempt,
                # 不同业务 attempt 必须使用不同幂等键。否则 attempt 1 的迟到
                # success 先落 callback_log 后，会把 attempt 2 的真实 success
                # 当成重复消息丢弃，步骤永久停在运行中。
                "idempotency_key": (
                    f"{task_id}:{job['step_id']}:result:attempt{attempt}"
                ),
                "status": "succeeded",
                "started_at": started_at,
                "finished_at": _now(),
                "output": {
                    # oss_prefix 现为任务级根 storage/outputs/{task_id}/(对齐《OSS文件规则》)。
                    # 各服务在自己流水线里已"谁产出谁推"到该根下的真实子路径;
                    # 此处回填任务根 + 能力名,平台仅存档不解析(见 orchestrator/core.py)。
                    "artifacts": [{
                        "role": f"{self.capability}_output",
                        "oss_key": job["output"]["oss_prefix"],
                    }],
                    "summary": {"adapter": "celery"},
                },
                "usage": usage,
            }, wait_for_confirm=True)
            print(f"✅ [{self.capability}] task {task_id} succeeded")

        except Exception as e:
            traceback.print_exc()
            usage = []
            try:
                usage = _collect_usage(task_id, self.capability, since_id)
            except Exception:
                pass
            terminal_published = self._publish_threadsafe(conn, ch, job, {
                **self._base(job),
                "msg_type": "callback.failed",
                "attempt": attempt,
                "idempotency_key": f"{task_id}:{job['step_id']}:error:attempt{attempt}",
                "status": "failed",
                "error": {
                    "code": "INTERNAL",
                    "retryable": self._is_retryable_error(e),
                    "message": str(e)[:500],
                    "detail": traceback.format_exc()[-2000:],
                },
                "usage": usage,  # 失败也可能已烧 token,照报
            }, wait_for_confirm=True)
            print(f"❌ [{self.capability}] task {task_id} failed: {e}")
        finally:
            if terminal_published:
                ack()
            else:
                # 终态回调没有得到 publisher confirm 时不能 ACK Job。把这一条
                # 原地 requeue；重投会复用相同 Celery task id/result，只补回调，
                # 不会重新执行烧 token 的主体。
                nack()

    def _ack_threadsafe(self, conn, ch, delivery_tag):
        """从工作线程 ack:同样要排回 I/O 线程。

        ack 失败(通常是连接已断)只能记日志 —— 此时 broker 迟早会重投,
        靠 _task_is_stale 的前置检查兜住,不会再变成无限重跑。
        """
        def _do():
            try:
                ch.basic_ack(delivery_tag=delivery_tag)
            except Exception as e:
                print(f"⚠️  [{self.capability}] ack 失败(消息会被重投): {e}")
        try:
            conn.add_callback_threadsafe(_do)
        except Exception as e:
            print(f"⚠️  [{self.capability}] ack 无法入队: {e}")

    def _nack_threadsafe(self, conn, ch, delivery_tag):
        """终态回调未确认时，只重投当前 Job，不影响同 channel 的其它任务。"""
        def _do():
            try:
                ch.basic_nack(delivery_tag=delivery_tag, requeue=True)
            except Exception as e:
                print(f"⚠️  [{self.capability}] nack 失败: {e}")
        try:
            conn.add_callback_threadsafe(_do)
        except Exception as e:
            print(f"⚠️  [{self.capability}] nack 无法入队: {e}")

    def run(self):
        self.register()
        threading.Thread(target=self.heartbeat_loop, daemon=True).start()
        while True:
            try:
                conn = pika.BlockingConnection(pika.URLParameters(PLATFORM_MQ_URL))
                ch = conn.channel()
                ch.confirm_delivery()
                ch.queue_declare(self.queue, durable=True)
                # 每次(重)连都自己补绑定。绑定原本只在 register() 里由平台侧建一次,
                # 而 broker 换实例/丢元数据后队列会被重新声明、绑定却回不来 ——
                # 结果是队列有消费者、消息却匹配不到任何绑定被静默丢弃,
                # 任务永远停在「已下发」0%。踩过一次,别再依赖启动时那一次绑定。
                ch.exchange_declare(JOBS_EXCHANGE, "topic", durable=True)
                ch.queue_bind(
                    self.queue, JOBS_EXCHANGE,
                    routing_key=f"job.{self.capability}.{self.impl_id}",
                )
                # 未 ack 的 Job 会一直保留到对应 Celery 任务完成。prefetch 必须
                # 与有界线程池一致：既能并行处理指定数量，也不会把更多长任务
                # 提前占在当前 Adapter 上。
                ch.basic_qos(prefetch_count=self.concurrency)
                ch.basic_consume(
                    self.queue, functools.partial(self.on_job, conn),
                )
                print(
                    f"🎧 [{self.capability}] consuming {self.queue} "
                    f"(concurrency={self.concurrency})"
                )
                self.consuming = True
                ch.start_consuming()
            # 捕获全部异常:这个方法跑在 daemon 线程里,漏出去线程就静默死掉,
            # 而进程和其他 6 个能力照活,外部完全看不出这一路已经不消费了。
            except Exception as e:
                print(f"⚠️  [{self.capability}] 消费循环异常,5s 后重连: "
                      f"{type(e).__name__}: {e}")
                traceback.print_exc()
                time.sleep(5)
            finally:
                self.consuming = False


def main():
    caps = sys.argv[1:] if len(sys.argv) > 1 else list(CAPABILITY_MAP.keys())
    for c in caps:
        if c not in CAPABILITY_MAP:
            print(f"unknown capability: {c}"); sys.exit(1)
    for c in caps[:-1]:
        threading.Thread(target=CapabilityAdapter(c).run, daemon=True).start()
        time.sleep(0.3)
    CapabilityAdapter(caps[-1]).run()  # 主线程占最后一个


if __name__ == "__main__":
    main()
