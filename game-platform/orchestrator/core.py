"""Orchestrator 核心:submit / advance(推进 DAG) / handle_callback(状态机)。

设计要点(对应文档七章、十四章):
- 中台是唯一写 task 状态的人
- advance() 从 DB 纯推导,无内存状态(A3)
- callback 按 idempotency_key 落 callback_log 去重(B3)
- failed.retryable → attempt+1 重发,耗尽 → task failed
- cancel_requested → 不再下发下游(A2)
"""
import json
import hashlib
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import inspect, text, update
from sqlmodel import Session, select

from common.config import OSS_BUCKET, OSS_OUTPUT_PREFIX
from common.models import (
    CallbackLog,
    JobOutbox,
    OutboxStatus,
    Pipeline,
    PipelineTemplate,
    StepStatus,
    Task,
    TaskStatus,
    TaskStep,
    TokenUsage,
    engine,
)
from orchestrator.dag import all_succeeded, parse_dag, ready_capabilities
from orchestrator.outbox import wake_outbox_publisher
from protocol.envelope import (
    CallbackFailed,
    CallbackProgress,
    CallbackSucceeded,
    JobInput,
    JobOutput,
    JobRequest,
    parse_callback,
)
from registry.api import ImplUnavailable, pick_impl


# novel_tasks 与默认 DAG 使用相同的能力名。这里保留一份显式白名单，既避免
# 动态列名来自外部输入，也让任务级重启能在同一事务内同步中台状态机和看板。
_NOVEL_STAGE_CAPABILITIES = (
    "story_planner",
    "info_extractor",
    "sound_arranger",
    "char_designer",
    "audio_producer",
    "scene_painter",
    "game_builder",
)
_NOVEL_PENDING = 0
_NOVEL_PROCESSING = 1
_NOVEL_SUCCEEDED = 2
_NOVEL_FAILED = 3

# 一次人工重启重新获得一整轮正常重试预算。TaskStep.max_attempts 是绝对
# attempt 上限，因此不能只把 FAILED 改回 PENDING，否则已经跑到 attempt=3
# 的步骤虽然会手动重跑一次，却再也没有自动重试机会。
_MANUAL_RESTART_ATTEMPTS = 3
_MANUAL_RESTART_MARKER = "MANUAL_RESTART"
_MANUAL_STAGE_RUN_MARKER = "MANUAL_STAGE_RUN"
RESTART_FAILED_ONLY = "failed_only"
RESTART_RUNNING_AND_FAILED = "running_and_failed"
_RESTART_MODES = frozenset({RESTART_FAILED_ONLY, RESTART_RUNNING_AND_FAILED})
_ACTIVE_STEP_STATUSES = frozenset({
    StepStatus.PUBLISHING,
    StepStatus.DISPATCHED,
    StepStatus.RUNNING,
})


def submit(task_id: str, template_name: str = "default",
           impl_map: Optional[dict] = None, params: Optional[dict] = None,
           user_id: Optional[int] = None, pipeline_name: Optional[str] = None) -> Task:
    """创建平台任务:落 task + 全量 task_step(pending),然后推进一轮。

    impl_map 解析优先级:显式传入 > 按 pipeline_name 查 pipeline 表的 impl_map > 空(各步自动选)。
    """
    with Session(engine) as session:
        template = session.exec(
            select(PipelineTemplate).where(PipelineTemplate.name == template_name)
        ).first()
        if template is None:
            raise ValueError(f"pipeline template not found: {template_name}")
        dag = parse_dag(template.dag_json)

        if session.exec(select(Task).where(Task.task_id == task_id)).first():
            raise ValueError(f"task already exists: {task_id}")

        if not impl_map and pipeline_name:
            pipeline = session.exec(
                select(Pipeline).where(Pipeline.name == pipeline_name)
            ).first()
            if pipeline is None:
                raise ValueError(f"pipeline not found: {pipeline_name}")
            impl_map = json.loads(pipeline.impl_map_json or "{}")
            print(f"📋 task {task_id} use pipeline '{pipeline_name}' impl_map={impl_map}")

        task = Task(
            task_id=task_id, user_id=user_id,
            pipeline_template_id=template.id,
            pipeline_name=pipeline_name,
            impl_map_json=json.dumps(impl_map or {}, ensure_ascii=False),
            params_json=json.dumps(params or {}, ensure_ascii=False),
            status=TaskStatus.RUNNING,
        )
        session.add(task)
        for capability in dag["nodes"]:
            session.add(TaskStep(
                step_id=str(uuid.uuid4()), task_id=task_id, capability=capability,
            ))
        session.commit()
        session.refresh(task)

    advance(task_id)
    return task


def advance(task_id: str):
    """推进 DAG：在数据库内创建 job outbox，不在请求线程访问 RabbitMQ。

    Task 行锁串行化同一任务的 advance/cancel；_dispatch 还会对 step 做条件
    UPDATE，作为第二道并发保护。事务提交后发布线程才有资格发送消息。
    """
    dispatched = 0
    with Session(engine) as session:
        task = session.exec(
            select(Task)
            .where(Task.task_id == task_id)
            .with_for_update()
        ).first()
        if task is None or task.status not in (
            TaskStatus.RUNNING, TaskStatus.CREATED, TaskStatus.MANUAL_STAGE
        ):
            return
        if task.cancel_requested:
            _cancel_pending_steps(session, task)
            session.commit()
            return

        template = session.get(PipelineTemplate, task.pipeline_template_id)
        dag = parse_dag(template.dag_json)
        steps = {
            s.capability: s
            for s in session.exec(
                select(TaskStep).where(TaskStep.task_id == task_id)
            ).all()
        }
        status_map = {cap: s.status for cap, s in steps.items()}

        if all_succeeded(dag, status_map):
            task.status = TaskStatus.SUCCEEDED
            task.progress = 100
            task.completed_at = datetime.now()
            session.add(task)
            session.commit()
            print(f"🎉 task {task_id} all steps succeeded")
            return

        impl_map = json.loads(task.impl_map_json)
        params = json.loads(task.params_json)

        # 单模块运行的 marker 是持久化的。若 API 在重置提交后、创建 outbox
        # 前崩溃，recovery 仍能只补发这个目标模块，而不会顺带启动其它 ready
        # 节点。
        forced_capabilities = [
            capability for capability, step in steps.items()
            if step.status == StepStatus.PENDING
            and str(step.error_code or "").startswith(
                f"{_MANUAL_STAGE_RUN_MARKER}:"
            )
        ]
        dispatch_capabilities = (
            forced_capabilities
            if task.status == TaskStatus.MANUAL_STAGE
            else ready_capabilities(dag, status_map)
        )
        for capability in dispatch_capabilities:
            step = steps[capability]
            try:
                impl = pick_impl(session, capability, impl_map.get(capability))
            except ImplUnavailable as e:
                # 任务已判失败,本轮不再下发其它就绪步骤,免得白烧 token
                _fail_step(session, task, step, "IMPL_UNAVAILABLE", str(e))
                # 同一事务里本轮较早创建的 outbox 尚未发布，直接取消；已在
                # RUNNING/DISPATCHED 的历史步骤不在此范围，仍可正常回调收口。
                _cancel_pending_steps(session, task)
                session.commit()
                print(
                    f"❌ task {task.task_id} step {step.capability} "
                    f"IMPL_UNAVAILABLE: {e}"
                )
                return
            if impl is None:
                print(f"⚠️  no available impl for {capability}, step stays pending")
                continue
            if _dispatch(session, task, step, impl, params) is not None:
                dispatched += 1

        done = sum(1 for s in steps.values() if s.status == StepStatus.SUCCEEDED)
        task.progress = int(done * 100 / max(len(dag["nodes"]), 1))
        session.add(task)
        session.commit()
    if dispatched:
        wake_outbox_publisher()


def _fail_step(session: Session, task: Task, step: TaskStep,
               error_code: str, error_msg: str):
    """下发前就判定跑不了(如管线指定的实现不可用):step 与 task 一起置失败。

    错误落在 step.error_msg 上,中台任务详情能直接看到是哪个能力、哪个实现的问题。
    """
    step.status = StepStatus.FAILED
    step.finished_at = datetime.now()
    step.error_code = error_code
    step.error_msg = error_msg
    task.status = TaskStatus.FAILED
    session.add(step)
    session.add(task)


def _dispatch(session: Session, task: Task, step: TaskStep, impl, params: dict):
    """原子地把 step 代次和稳定 job envelope 写入 outbox。

    本函数不 commit；调用者把 TaskStep.PUBLISHING、attempt 和 JobOutbox 放在
    同一事务。RabbitMQ confirm 发生在另一个线程，成功后才置 DISPATCHED。
    """
    if task.cancel_requested or step.status != StepStatus.PENDING:
        return None

    task_id = task.task_id
    step_id = step.step_id
    capability = step.capability
    impl_id = impl.impl_id
    group_name = impl.group_name
    expected_attempt = step.attempt
    attempt = expected_attempt + 1
    now = datetime.now()
    marker = getattr(step, "error_code", None)
    manual_restart = bool(
        marker
        and marker.startswith(f"{_MANUAL_RESTART_MARKER}:")
    )
    dispatch_context = {}
    manual_stage_run = bool(
        marker
        and marker.startswith(f"{_MANUAL_STAGE_RUN_MARKER}:")
    )
    if manual_restart:
        dispatch_context = {
            "dispatch_reason": "manual_restart",
            "restart_id": marker.split(":", 1)[1],
            "supersedes_attempt": expected_attempt,
        }
    elif manual_stage_run:
        dispatch_context = {
            "dispatch_reason": "manual_stage_run",
            "stage_run_id": marker.split(":", 1)[1],
            "target_capability": capability,
            "supersedes_attempt": expected_attempt,
        }
    elif expected_attempt > 0:
        dispatch_context = {
            "dispatch_reason": "automatic_retry",
            "supersedes_attempt": expected_attempt,
        }

    # 即使某个调用路径忘了拿 Task 行锁，这个 compare-and-set 也只能让一个
    # 并发 advance 赢得该代次，避免双派发。
    claimed = session.exec(
        update(TaskStep)
        .where(
            TaskStep.id == step.id,
            TaskStep.status == StepStatus.PENDING,
            TaskStep.attempt == expected_attempt,
        )
        .values(
            impl_id=impl_id,
            group_name=group_name,
            attempt=attempt,
            status=StepStatus.PUBLISHING,
            progress=0,
            started_at=None,
            finished_at=None,
            error_code=None,
            error_msg=None,
        )
        .execution_options(synchronize_session="fetch")
    )
    if not claimed.rowcount:
        return None

    message_id = f"job:{step_id}:{attempt}"

    # 业务参数保持和普通任务完全一致，服务可以继续按 task_id 读取自己的
    # 断点；人工运行控制信息只放在 envelope.context 中。
    job = JobRequest(
        msg_id=message_id,
        task_id=task_id,
        step_id=step_id,
        attempt=attempt,
        idempotency_key=f"{task_id}:{step_id}:{attempt}",
        capability=capability,
        impl_id=impl_id,
        group_name=group_name,
        reply_routing_key=f"cb.{task_id}.{capability}",
        input=JobInput(params=params, context=dispatch_context),
        output=JobOutput(
            oss_bucket=OSS_BUCKET,
            oss_prefix=OSS_OUTPUT_PREFIX.format(task_id=task_id),
        ),
    )
    payload_json = job.model_dump_json()
    session.add(JobOutbox(
        message_id=message_id,
        task_id=task_id,
        step_id=step_id,
        attempt=attempt,
        capability=capability,
        impl_id=impl_id,
        routing_key=f"job.{capability}.{impl_id}",
        payload_json=payload_json,
        payload_sha256=hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
        status=OutboxStatus.PENDING,
        available_at=now,
        created_at=now,
        updated_at=now,
    ))
    print(
        f"📥 queued {capability} → {impl_id} "
        f"(attempt {attempt}) for task {task_id}"
    )
    return message_id


def handle_callback(payload: dict):
    """消费一条回调:幂等去重 → 更新 step → token 落库 → 推进 DAG"""
    cb = parse_callback(payload)

    with Session(engine) as session:
        # B3 幂等:idempotency_key 唯一,重复直接丢
        if session.exec(
            select(CallbackLog).where(
                CallbackLog.idempotency_key == cb.idempotency_key
            )
        ).first():
            print(f"🔁 duplicate callback dropped: {cb.idempotency_key}")
            return
        session.add(CallbackLog(
            idempotency_key=cb.idempotency_key, msg_type=cb.msg_type,
            task_id=cb.task_id, step_id=cb.step_id,
            payload_json=json.dumps(payload, ensure_ascii=False),
        ))

        # 与 advance/cancel/outbox confirm 使用同一锁顺序，避免回调与发布确认
        # 同时落库时形成 step/outbox 反向锁。
        task = session.exec(
            select(Task)
            .where(Task.task_id == cb.task_id)
            .with_for_update()
        ).first()
        step = session.exec(
            select(TaskStep)
            .where(TaskStep.step_id == cb.step_id)
            .with_for_update()
        ).first()
        if step is None:
            print(f"⚠️  callback for unknown step {cb.step_id}, ignored")
            session.commit()
            return

        # A2: 已取消的任务,回调只记日志不推进
        if task is not None and (
            task.cancel_requested or task.status == TaskStatus.CANCELLED
        ):
            session.commit()
            return

        manual_stage_context = None
        if isinstance(cb, (CallbackSucceeded, CallbackFailed)):
            manual_stage_context = _manual_stage_context(
                session, cb.step_id, cb.attempt
            )

        # 回调能够到达就是比 publisher confirm 更强的发布证据。若进程在
        # broker confirm 后、DB complete 前停顿/崩溃，这里直接收口 outbox，
        # 防止 lease 到期后用同一 message_id 再发一遍。
        _mark_outbox_observed(session, cb.step_id, cb.attempt)

        if isinstance(cb, (CallbackSucceeded, CallbackFailed)):
            # Adapter/Celery/RabbitMQ 都可能在故障恢复时送来旧 attempt 的终态。
            # 旧失败回调若继续走下面的 retry 分支，会在当前 attempt 之外再派发
            # 一轮；旧成功回调也可能覆盖正在执行的新 attempt。用 step.attempt
            # 作为唯一业务代次，只记录用量，不允许陈旧回调改变状态机。
            if cb.attempt != step.attempt:
                _record_usage(session, cb)
                session.commit()
                print(
                    f"🗑️ stale terminal callback dropped: step={step.step_id} "
                    f"callback_attempt={cb.attempt} current_attempt={step.attempt}"
                )
                return
            # 合法终态只能从一个已经下发/执行中的 attempt 进入。人工重启会
            # 先把旧步骤原子地改回 PENDING，再创建新 attempt；这两个事务间
            # 即使撞进旧 Worker 的迟到终态，也不能把刚重置的步骤重新覆盖。
            if step.status not in (
                StepStatus.PUBLISHING,
                StepStatus.DISPATCHED,
                StepStatus.RUNNING,
            ):
                _record_usage(session, cb)
                session.commit()
                print(
                    f"🗑️ inactive step callback ignored: step={step.step_id} "
                    f"status={step.status} attempt={step.attempt}"
                )
                return

        if isinstance(cb, CallbackProgress):
            if cb.attempt != step.attempt:
                session.commit()
                print(
                    f"🗑️ stale progress dropped: step={step.step_id} "
                    f"callback_attempt={cb.attempt} current_attempt={step.attempt}"
                )
                return
            # B2 乱序:终态后到达的 progress 直接忽略
            # callback 能到达本身就证明 broker 已发布；它可能抢在 outbox
            # confirm 落库前到达，因此 PUBLISHING 也可直接进入 RUNNING。
            if step.status in (
                StepStatus.PUBLISHING,
                StepStatus.DISPATCHED,
                StepStatus.RUNNING,
            ):
                step.status = StepStatus.RUNNING
                step.progress = cb.progress
                session.add(step)
            session.commit()
            return

        if isinstance(cb, CallbackSucceeded):
            step.status = StepStatus.SUCCEEDED
            step.progress = 100
            step.finished_at = datetime.now()
            step.output_json = cb.output.model_dump_json()
            session.add(step)
            _record_usage(session, cb)
            if manual_stage_context and task is not None:
                all_steps = session.exec(
                    select(TaskStep).where(TaskStep.task_id == cb.task_id)
                ).all()
                task.progress = int(
                    sum(s.status == StepStatus.SUCCEEDED for s in all_steps)
                    * 100 / max(len(all_steps), 1)
                )
                if all(s.status == StepStatus.SUCCEEDED for s in all_steps):
                    task.status = TaskStatus.SUCCEEDED
                    task.progress = 100
                    task.completed_at = datetime.now()
                else:
                    task.status = TaskStatus.MANUAL_STAGE
                    task.completed_at = None
                session.add(task)
            session.commit()
            # 单模块调试成功后不自动推进下游，避免一次“只跑 sound”意外
            # 把 game_builder 等其它等待阶段也启动。普通任务仍按原 DAG 推进。
            if not manual_stage_context:
                advance(cb.task_id)
            return

        if isinstance(cb, CallbackFailed):
            _record_usage(session, cb)  # 失败也可能烧了 token,照记
            if cb.error.retryable and step.attempt < step.max_attempts:
                step.status = StepStatus.PENDING  # advance 会重发(attempt+1)
                if manual_stage_context:
                    step.error_code = (
                        f"{_MANUAL_STAGE_RUN_MARKER}:"
                        f"{manual_stage_context['stage_run_id']}"
                    )
                else:
                    step.error_code = cb.error.code
                step.error_msg = cb.error.message
                session.add(step)
                session.commit()
                print(f"🔁 step {step.capability} retryable failure, will re-dispatch")
                if manual_stage_context:
                    _dispatch_single_stage_attempt(
                        cb.task_id, step.capability
                    )
                else:
                    advance(cb.task_id)
            else:
                step.status = StepStatus.FAILED
                step.finished_at = datetime.now()
                step.error_code = cb.error.code
                step.error_msg = cb.error.message
                session.add(step)
                if task is not None:
                    task.status = TaskStatus.FAILED
                    session.add(task)
                session.commit()
                print(f"❌ step {step.capability} failed permanently, task failed")
            return


def _manual_stage_context(
    session: Session, step_id: str, attempt: int
) -> Optional[dict]:
    """从稳定 outbox envelope 取出人工单模块运行标记。"""
    row = session.exec(
        select(JobOutbox.payload_json)
        .where(
            JobOutbox.step_id == step_id,
            JobOutbox.attempt == attempt,
        )
    ).first()
    if not row:
        return None
    payload_json = row[0] if isinstance(row, tuple) else row
    try:
        context = (json.loads(payload_json).get("input") or {}).get("context") or {}
    except (TypeError, ValueError, AttributeError):
        return None
    if context.get("dispatch_reason") != "manual_stage_run":
        return None
    stage_run_id = str(context.get("stage_run_id") or "").strip()
    return {"stage_run_id": stage_run_id} if stage_run_id else None


def _dispatch_single_stage_attempt(task_id: str, capability: str) -> bool:
    """只为单模块调试轮次创建下一次 attempt，不推进其它 DAG 节点。"""
    dispatched = False
    with Session(engine) as session:
        task = session.exec(
            select(Task).where(Task.task_id == task_id).with_for_update()
        ).first()
        if task is None or task.status not in (
            TaskStatus.RUNNING, TaskStatus.MANUAL_STAGE
        ):
            return False
        step = session.exec(
            select(TaskStep)
            .where(TaskStep.task_id == task_id, TaskStep.capability == capability)
            .with_for_update()
        ).first()
        if step is None or step.status != StepStatus.PENDING:
            return False
        impl_map = json.loads(task.impl_map_json or "{}")
        params = json.loads(task.params_json or "{}")
        try:
            impl = pick_impl(session, capability, impl_map.get(capability))
        except ImplUnavailable as exc:
            step.status = StepStatus.FAILED
            step.finished_at = datetime.now()
            step.error_code = "IMPL_UNAVAILABLE"
            step.error_msg = str(exc)
            task.status = TaskStatus.FAILED
            session.add(step)
            session.add(task)
            session.commit()
            return False
        if impl is not None:
            dispatched = _dispatch(session, task, step, impl, params) is not None
        session.commit()
    if dispatched:
        wake_outbox_publisher()
    return dispatched


def _mark_outbox_observed(session: Session, step_id: str, attempt: int) -> None:
    row = session.exec(
        select(JobOutbox)
        .where(
            JobOutbox.step_id == step_id,
            JobOutbox.attempt == attempt,
            JobOutbox.status.in_((OutboxStatus.PENDING, OutboxStatus.PUBLISHING)),
        )
        .with_for_update()
    ).first()
    if row is None:
        return
    now = datetime.now()
    row.status = OutboxStatus.PUBLISHED
    row.published_at = row.published_at or now
    row.claimed_at = None
    row.claim_token = None
    row.last_error = None
    row.updated_at = now
    session.add(row)


def request_cancel(task_id: str) -> bool:
    with Session(engine) as session:
        task = session.exec(
            select(Task)
            .where(Task.task_id == task_id)
            .with_for_update()
        ).first()
        if task is None:
            return False
        task.cancel_requested = True
        task.status = TaskStatus.CANCELLED
        session.add(task)
        _cancel_pending_steps(session, task)
        session.commit()
        return True


def request_restart(
    task_id: str,
    mode: str = RESTART_RUNNING_AND_FAILED,
) -> Optional[dict]:
    """按原 task_id 重新启动指定范围的步骤，成功步骤与产物保持不动。

    ``failed_only`` 只重置失败步骤；``running_and_failed`` 还会 fence 并
    重置已经下发/执行中的步骤。两种模式都会保留 pending 步骤，让它们
    继续等待 DAG 上游完成，避免一次重启把尚未轮到的步骤提前重复派发。

    运行中任务无法从 RabbitMQ 撤回，旧 Celery 进程也可能稍后才结束。因此
    重启必须同时做到：

    * Task 行锁串行化 restart / advance / cancel；
    * 已成功步骤保持 SUCCEEDED；novel_tasks 已完成但成功回调丢失时，提升
      对应 TaskStep 为 SUCCEEDED，避免重复烧模型额度；
    * 被选中的步骤回到 PENDING，由 advance 创建新的 attempt 来 fence 旧消息；
    * 仅取消被选中步骤未确认发布的旧 outbox，未选中步骤继续原队列流程；
    * 同事务同步 novel_tasks，看板不会继续显示旧的处理中/失败状态。
    """
    if mode not in _RESTART_MODES:
        raise ValueError(
            f"unsupported restart mode: {mode}; "
            f"allowed: {', '.join(sorted(_RESTART_MODES))}"
        )

    restarted = []
    preserved = []
    untouched = []
    now = datetime.now()
    restart_id = str(uuid.uuid4())

    with Session(engine) as session:
        task = session.exec(
            select(Task)
            .where(Task.task_id == task_id)
            .with_for_update()
        ).first()
        if task is None:
            return None

        template = session.get(PipelineTemplate, task.pipeline_template_id)
        if template is None:
            raise ValueError(
                f"pipeline template not found for task {task_id}: "
                f"{task.pipeline_template_id}"
            )
        dag = parse_dag(template.dag_json)
        steps = session.exec(
            select(TaskStep)
            .where(TaskStep.task_id == task_id)
            .with_for_update()
        ).all()
        by_capability = {step.capability: step for step in steps}

        missing = [node for node in dag["nodes"] if node not in by_capability]
        if missing:
            raise ValueError(
                f"task {task_id} is missing DAG steps: {', '.join(missing)}"
            )

        # novel_tasks 是算法 Worker 实际提交阶段终态的地方。它若已经是 2，
        # 说明算法产物完成、只是 Adapter/中台成功回调可能丢了，必须按完成保留。
        novel_statuses = _lock_novel_stage_statuses(session, task_id)

        reset_step_ids = set()
        all_succeeded = True
        for capability in dag["nodes"]:
            step = by_capability[capability]
            # novel_tasks 是 Worker 带 PostgreSQL fence 写下的业务终态；存在
            # 时以它为准。仅对没有 novel_tasks 行的纯中台任务回退 TaskStep。
            actually_succeeded = (
                novel_statuses.get(capability) == _NOVEL_SUCCEEDED
                if novel_statuses is not None
                else step.status == StepStatus.SUCCEEDED
            )
            if actually_succeeded:
                preserved.append(capability)
                if step.status != StepStatus.SUCCEEDED:
                    step.status = StepStatus.SUCCEEDED
                    step.progress = 100
                    step.finished_at = step.finished_at or now
                    step.error_code = None
                    step.error_msg = None
                    session.add(step)
                continue

            all_succeeded = False
            novel_status = (
                novel_statuses.get(capability)
                if novel_statuses is not None
                else None
            )
            failed = step.status in (
                StepStatus.FAILED,
                StepStatus.CANCELLED,
            ) or novel_status == _NOVEL_FAILED
            running = (
                step.status in _ACTIVE_STEP_STATUSES
                or novel_status == _NOVEL_PROCESSING
            )
            should_restart = failed or (
                mode == RESTART_RUNNING_AND_FAILED and running
            )
            if not should_restart:
                untouched.append(capability)
                continue

            restarted.append(capability)
            reset_step_ids.add(step.step_id)

            step.max_attempts = max(
                step.max_attempts,
                step.attempt + _MANUAL_RESTART_ATTEMPTS,
            )
            step.status = StepStatus.PENDING
            step.progress = 0
            step.started_at = None
            step.finished_at = None
            # 持久 marker 让稍后才满足 DAG 上游条件的步骤也能在 Job context
            # 中携带人工重启信号；_dispatch 写新 attempt 时会原子清掉它。
            step.error_code = f"{_MANUAL_RESTART_MARKER}:{restart_id}"
            step.error_msg = None
            step.output_json = None
            session.add(step)

        _cancel_unconfirmed_outboxes(
            session,
            task_id=task_id,
            # 只收口本轮要重启的步骤。未选中的 running/pending 步骤保留其
            # outbox，避免 failed_only 意外打断 sound/audio 等并行任务。
            step_ids=reset_step_ids,
            now=now,
        )

        total = max(len(dag["nodes"]), 1)
        progress = int(len(preserved) * 100 / total)
        task.cancel_requested = False
        task.progress = progress
        task.completed_at = None
        task.status = TaskStatus.SUCCEEDED if all_succeeded else TaskStatus.RUNNING
        if all_succeeded:
            task.progress = 100
            task.completed_at = now
        session.add(task)

        _sync_novel_task_restart(
            session,
            task_id=task_id,
            capabilities=set(dag["nodes"]),
            succeeded=set(preserved),
            reset_capabilities=set(restarted),
            progress=task.progress,
            completed=all_succeeded,
            now=now,
        )
        session.commit()

    # 不在持锁事务中访问实现注册表/创建下一批 outbox。即使进程恰好在这里
    # 崩溃，outbox recovery 也会从 RUNNING + PENDING 状态继续推进。
    if restarted:
        advance(task_id)
    return {
        "task_id": task_id,
        "restart_id": restart_id,
        "mode": mode,
        "preserved_steps": preserved,
        "restarted_steps": restarted,
        "untouched_steps": untouched,
    }


def request_stage_run(task_id: str, capability: str) -> Optional[dict]:
    """只重新运行一个模块，沿用原 task_id 并保留其它模块状态/产物。

    这是给调试用的强制入口：目标步骤无论 pending、running、failed 还是
    succeeded 都会被重置为 pending，并创建新的平台 attempt。它不会修改
    其它 TaskStep，也不会按 DAG 重置下游。目标模块收到 job 后继续使用各自
    的断点和产物复用逻辑；旧 attempt 的回调由现有 fence/attempt 检查丢弃。
    """
    if capability not in _NOVEL_STAGE_CAPABILITIES:
        raise ValueError(
            f"unsupported stage capability: {capability}; "
            f"allowed: {', '.join(_NOVEL_STAGE_CAPABILITIES)}"
        )

    stage_run_id = str(uuid.uuid4())
    now = datetime.now()
    with Session(engine) as session:
        task = session.exec(
            select(Task)
            .where(Task.task_id == task_id)
            .with_for_update()
        ).first()
        if task is None:
            return None
        if task.status == TaskStatus.CANCELLED or task.cancel_requested:
            raise ValueError("cancelled task cannot run a stage")

        template = session.get(PipelineTemplate, task.pipeline_template_id)
        if template is None:
            raise ValueError(
                f"pipeline template not found for task {task_id}: "
                f"{task.pipeline_template_id}"
            )
        dag = parse_dag(template.dag_json)
        if capability not in dag["nodes"]:
            raise ValueError(
                f"stage {capability} is not part of task {task_id} pipeline"
            )
        step = session.exec(
            select(TaskStep)
            .where(
                TaskStep.task_id == task_id,
                TaskStep.capability == capability,
            )
            .with_for_update()
        ).first()
        if step is None:
            raise ValueError(f"task {task_id} has no step {capability}")

        # 与算法 Worker 的 novel_tasks 写入串行化，避免旧 Worker 恰好在人工
        # 重置时把目标阶段又写回 processing/completed。
        _lock_novel_stage_statuses(session, task_id)

        # 人工运行开启新的业务轮次，旧轮次的失败次数不限制本轮的三次自动
        # 重试；但这不是无限预算，仍然由 TaskStep.max_attempts 约束本轮。
        step.max_attempts = max(
            step.max_attempts,
            step.attempt + _MANUAL_RESTART_ATTEMPTS,
        )
        step.status = StepStatus.PENDING
        step.progress = 0
        step.started_at = None
        step.finished_at = None
        step.error_code = f"{_MANUAL_STAGE_RUN_MARKER}:{stage_run_id}"
        step.error_msg = None
        step.output_json = None
        session.add(step)

        _cancel_unconfirmed_outboxes(
            session,
            task_id=task_id,
            step_ids={step.step_id},
            now=now,
        )

        impl_map = json.loads(task.impl_map_json or "{}")
        params = json.loads(task.params_json or "{}")
        try:
            impl = pick_impl(session, capability, impl_map.get(capability))
        except ImplUnavailable as exc:
            raise ValueError(str(exc)) from exc
        if impl is None:
            raise ValueError(f"no healthy implementation available for {capability}")

        # 直接在这一个事务里重置并写入该模块的 outbox。这样不会出现“重置已
        # 提交、但 advance 尚未来得及执行”的窗口，也不会顺带派发其它 ready
        # 模块；若进程在提交后退出，outbox publisher 会可靠地继续发送。
        message_id = _dispatch(session, task, step, impl, params)
        if message_id is None:
            raise ValueError(f"stage {capability} is no longer pending")

        steps = session.exec(
            select(TaskStep).where(TaskStep.task_id == task_id)
        ).all()
        task.cancel_requested = False
        task.status = TaskStatus.MANUAL_STAGE
        task.completed_at = None
        task.progress = int(
            sum(s.status == StepStatus.SUCCEEDED for s in steps)
            * 100 / max(len(dag["nodes"]), 1)
        )
        session.add(task)
        _sync_novel_task_stage_run(
            session,
            task_id=task_id,
            capability=capability,
            progress=task.progress,
            now=now,
        )
        run_attempt = step.attempt
        session.commit()

    wake_outbox_publisher()
    return {
        "task_id": task_id,
        "stage_run_id": stage_run_id,
        "capability": capability,
        "attempt": run_attempt,
    }


def _lock_novel_stage_statuses(
    session: Session, task_id: str
) -> Optional[dict]:
    """锁住 novel_tasks 阶段快照；纯中台单测/独立任务允许该表不存在。"""
    bind = session.get_bind()
    sqlite = bind.dialect.name == "sqlite"
    if sqlite and not inspect(session.connection()).has_table("novel_tasks"):
        return None
    table = "novel_tasks" if sqlite else "public.novel_tasks"
    suffix = "" if sqlite else " FOR UPDATE"
    columns = [f"{capability}_status" for capability in _NOVEL_STAGE_CAPABILITIES]
    row = session.exec(
        text(
            f"SELECT {', '.join(columns)} FROM {table} "
            f"WHERE task_id = :task_id{suffix}"
        ).bindparams(task_id=task_id)
    ).first()
    if row is None:
        return None
    return {
        capability: int(row[index])
        for index, capability in enumerate(_NOVEL_STAGE_CAPABILITIES)
    }


def _sync_novel_task_restart(
    session: Session,
    *,
    task_id: str,
    capabilities: set,
    succeeded: set,
    reset_capabilities: set,
    progress: int,
    completed: bool,
    now: datetime,
) -> None:
    """把任务级重启结果同步到 novel_tasks（与 TaskStep 同一事务）。"""
    bind = session.get_bind()
    sqlite = bind.dialect.name == "sqlite"
    if sqlite and not inspect(session.connection()).has_table("novel_tasks"):
        return
    table = "novel_tasks" if sqlite else "public.novel_tasks"
    # 已完成列保持原值，不由中台伪造 Worker 的 fenced terminal write；只把
    # 确认需要重跑的列退回待处理。生产库 trigger 会拒绝没有 owner/token 的
    # 0/1→2 写入，这正是防止旧 Worker 覆盖新 attempt 的最后一道保护。
    values = {
        f"{capability}_status": _NOVEL_PENDING
        for capability in _NOVEL_STAGE_CAPABILITIES
        if capability in capabilities and capability in reset_capabilities
    }
    values.update({
        "overall_status": (
            _NOVEL_SUCCEEDED if completed else _NOVEL_PROCESSING
        ),
        "progress_percentage": 100 if completed else progress,
        "error_message": None,
        "completed_at": now if completed else None,
        "update_time": now,
    })
    assignments = ", ".join(f"{column} = :{column}" for column in values)
    session.exec(
        text(
            f"UPDATE {table} SET {assignments} WHERE task_id = :task_id"
        ).bindparams(task_id=task_id, **values)
    )


def _sync_novel_task_stage_run(
    session: Session,
    *,
    task_id: str,
    capability: str,
    progress: int,
    now: datetime,
) -> None:
    """只把目标阶段退回待处理，保留 novel_tasks 其它阶段和产物状态。"""
    bind = session.get_bind()
    sqlite = bind.dialect.name == "sqlite"
    if sqlite and not inspect(session.connection()).has_table("novel_tasks"):
        return
    table = "novel_tasks" if sqlite else "public.novel_tasks"
    stage_column = f"{capability}_status"
    session.exec(
        text(
            f"UPDATE {table} SET {stage_column} = :pending, "
            "overall_status = :processing, progress_percentage = :progress, "
            "error_message = NULL, completed_at = NULL, update_time = :now "
            "WHERE task_id = :task_id"
        ).bindparams(
            pending=_NOVEL_PENDING,
            processing=_NOVEL_PROCESSING,
            progress=progress,
            now=now,
            task_id=task_id,
        )
    )


def _cancel_unconfirmed_outboxes(
    session: Session,
    *,
    task_id: str,
    step_ids: set,
    now: datetime,
) -> None:
    if not step_ids:
        return
    for outbox in session.exec(
        select(JobOutbox)
        .where(JobOutbox.task_id == task_id)
        .where(JobOutbox.step_id.in_(step_ids))
        .where(
            JobOutbox.status.in_((OutboxStatus.PENDING, OutboxStatus.PUBLISHING))
        )
        .with_for_update()
    ).all():
        outbox.status = OutboxStatus.CANCELLED
        outbox.claim_token = None
        outbox.claimed_at = None
        outbox.updated_at = now
        session.add(outbox)


def _cancel_pending_steps(session: Session, task: Task):
    for step in session.exec(
        select(TaskStep)
        .where(TaskStep.task_id == task.task_id)
        .where(TaskStep.status.in_((StepStatus.PENDING, StepStatus.PUBLISHING)))
    ).all():
        step.status = StepStatus.CANCELLED
        step.finished_at = datetime.now()
        session.add(step)
    for outbox in session.exec(
        select(JobOutbox)
        .where(JobOutbox.task_id == task.task_id)
        .where(
            JobOutbox.status.in_((OutboxStatus.PENDING, OutboxStatus.PUBLISHING))
        )
    ).all():
        outbox.status = OutboxStatus.CANCELLED
        outbox.claim_token = None
        outbox.claimed_at = None
        outbox.updated_at = datetime.now()
        session.add(outbox)


def _record_usage(session: Session, cb):
    for u in cb.usage:
        session.add(TokenUsage(
            task_id=cb.task_id, step_id=cb.step_id, capability=cb.capability,
            group_name=cb.group_name, impl_id=cb.impl_id,
            api_type=u.api_type, model=u.model,
            prompt_tokens=u.prompt_tokens, completion_tokens=u.completion_tokens,
            image_count=u.image_count, tts_chars=u.tts_chars, requests=u.requests,
        ))
    _stamp_pipeline_name(session, cb.task_id)


def _stamp_pipeline_name(session: Session, task_id: str):
    """给 api_usage 和 novel_tasks 补任务的管线名。

    两张表的 group_name 都由各组 worker 写自己的组名(执行方),契约如此、不会改。
    中台知道任务用的是哪条管线,所以在回调时统一回填 pipeline_name ——
    用量汇总和任务日志要的都是「跑的哪条管线」,不是「谁执行的」。
    """
    task = session.exec(select(Task).where(Task.task_id == task_id)).first()
    if task is None or not task.pipeline_name:
        return
    for table in ("api_usage", "novel_tasks"):
        session.exec(
            text(
                f"UPDATE public.{table} SET pipeline_name = :p "
                "WHERE task_id = :t AND (pipeline_name IS NULL OR pipeline_name = '')"
            ).bindparams(p=task.pipeline_name, t=task_id)
        )
