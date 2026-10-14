"""
进度更新工具 - 用于各服务更新任务进度
"""
import os
import threading
import time
import uuid
from datetime import datetime
import redis
from sqlalchemy import func, text
from sqlmodel import Session, select
from common.task_models import NovelTask, TaskStatus
from common.platform_db import platform_engine as engine  # novel_tasks 已迁平台库
from common.config import REDIS_URL
from common.local_runtime import local_pipeline_enabled
from common.stage_fencing import (
    StageFenceClaimError,
    StageFenceIdentity,
    StageFenceOwnershipError,
    VALID_STAGE_NAMES,
    claim_stage_fence,
    ensure_stage_fencing_schema,
    lock_and_validate_stage_fence,
    read_stage_fence,
)


_LEASE_MISSING = 0
_LEASE_RENEWED = 1
_LEASE_FOREIGN_OWNER = -1


_STAGE_LEASE_TTL = int(os.environ.get("STAGE_LEASE_TTL", "1800"))
_STAGE_LEASE_RENEW_INTERVAL = int(
    os.environ.get("STAGE_LEASE_RENEW_INTERVAL", "60")
)
_STAGE_LEASE_STARTUP_GRACE = max(
    0, int(os.environ.get("STAGE_LEASE_STARTUP_GRACE", "5"))
)
_stage_redis = redis.Redis.from_url(
    REDIS_URL,
    decode_responses=True,
    socket_connect_timeout=5,
    socket_timeout=5,
)
_lease_guard = threading.Lock()
_active_leases = {}
_active_fence_tokens = {}
# ``key`` entries remain for diagnostics/backward-compatible tests.  The
# owner-specific tuple is the permanent fence: a later claim for the same
# task/stage never makes an old execution context valid again.
_lost_stage_leases = set()
_execution_context = threading.local()


class StageLeaseLostError(RuntimeError):
    """当前执行上下文已不再拥有阶段租约。"""


class StageCompletionError(RuntimeError):
    """阶段主体已结束，但终态未能可靠写入数据库。"""


class StageTakeoverValidationError(RuntimeError):
    """Redis 抢占成功后，PostgreSQL 拒绝该执行权。"""


_COMPARE_AND_EXPIRE_SCRIPT = """
local current = redis.call('get', KEYS[1])
if not current then
    return 0
end
if current == ARGV[1] then
    return redis.call('expire', KEYS[1], tonumber(ARGV[2]))
end
return -1
"""

_COMPARE_AND_DELETE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


def _stage_lease_key(task_id: str, stage: str) -> str:
    return f"novel-game:stage-lease:{task_id}:{stage}"


def _new_lease_owner() -> str:
    celery_task_id = None
    try:
        from celery import current_task
        celery_task_id = getattr(
            getattr(current_task, "request", None), "id", None
        )
    except Exception:
        pass
    # Infrastructure recovery deliberately reuses the Celery task id.  The
    # random suffix prevents old and recovery processes from appearing equal.
    prefix = str(celery_task_id) if celery_task_id else "local"
    return f"{prefix}:{os.getpid()}:{uuid.uuid4().hex}"


def _context_claims():
    claims = getattr(_execution_context, "claims", None)
    if claims is None:
        claims = {}
        _execution_context.claims = claims
    return claims


def _context_non_owners():
    keys = getattr(_execution_context, "non_owners", None)
    if keys is None:
        keys = set()
        _execution_context.non_owners = keys
    return keys


def _local_stage_identity(task_id: str, stage: str):
    return _context_claims().get(_stage_lease_key(task_id, stage))


def _compare_and_expire_state(key: str, owner: str) -> int:
    return int(_stage_redis.eval(
        _COMPARE_AND_EXPIRE_SCRIPT,
        1,
        key,
        owner,
        _STAGE_LEASE_TTL,
    ))


def _compare_and_delete(key: str, owner: str) -> bool:
    return bool(_stage_redis.eval(
        _COMPARE_AND_DELETE_SCRIPT,
        1,
        key,
        owner,
    ))


def _mark_stage_lease_lost(key: str, owner: str) -> None:
    with _lease_guard:
        _lost_stage_leases.add(key)
        _lost_stage_leases.add((key, owner))
        active = _active_leases.get(key)
        if active and active[0] == owner:
            active[1].set()


def _fence_stage_non_owner(task_id: str, stage: str) -> None:
    _context_non_owners().add(_stage_lease_key(task_id, stage))


def _renew_stage_lease_once(task_id: str, stage: str, owner: str) -> bool:
    key = _stage_lease_key(task_id, stage)
    state = _compare_and_expire_state(key, owner)
    if state == _LEASE_RENEWED:
        return True

    # Missing and foreign-owner results are both definitive loss of the Redis
    # lease.  Keep an owner-specific permanent local fence so a later claim in
    # this process cannot accidentally lend its identity to this old task.
    _mark_stage_lease_lost(key, owner)
    if state == _LEASE_FOREIGN_OWNER:
        print(
            f"❌ [{stage}] stage lease 已被恢复 Worker 接管；旧执行已永久 fence"
        )
    else:
        print(f"⚠️  [{stage}] stage lease 已过期；当前执行已永久 fence")
    return False


def _start_stage_lease(
    task_id: str,
    stage: str,
    identity: StageFenceIdentity,
) -> None:
    key = _stage_lease_key(task_id, stage)
    owner = identity.owner
    stop = threading.Event()
    with _lease_guard:
        previous = _active_leases.pop(key, None)
        if previous:
            previous[1].set()
        _active_leases[key] = (owner, stop)
        _active_fence_tokens[key] = identity.token
        _lost_stage_leases.discard(key)
    _context_claims()[key] = identity
    _context_non_owners().discard(key)

    def _renew():
        while not stop.wait(_STAGE_LEASE_RENEW_INTERVAL):
            try:
                if not _renew_stage_lease_once(task_id, stage, owner):
                    break
            except Exception as exc:
                print(f"⚠️  [{stage}] stage lease renew failed: {exc}")

    threading.Thread(
        target=_renew,
        name=f"stage-lease-{stage}",
        daemon=True,
    ).start()


def _claim_stage_execution(
    task_id: str,
    stage: str,
    *,
    allowed_statuses,
):
    """Acquire Redis liveness first, then linearize ownership in PostgreSQL."""

    if stage not in VALID_STAGE_NAMES:
        raise StageTakeoverValidationError(f"[{stage}] 无效的主流水线阶段")
    ensure_stage_fencing_schema(engine)
    key = _stage_lease_key(task_id, stage)
    owner = _new_lease_owner()
    try:
        claimed = bool(_stage_redis.set(
            key,
            owner,
            nx=True,
            ex=_STAGE_LEASE_TTL,
        ))
    except Exception as exc:
        raise RuntimeError(f"[{stage}] stage lease unavailable: {exc}") from exc
    if not claimed:
        return None

    try:
        result = claim_stage_fence(
            engine,
            task_id=task_id,
            stage=stage,
            owner=owner,
            allowed_statuses=frozenset(allowed_statuses),
            processing_status=TaskStatus.PROCESSING,
        )
    except Exception:
        try:
            _compare_and_delete(key, owner)
        except Exception as release_exc:
            print(f"⚠️  [{stage}] failed claim lease cleanup: {release_exc}")
        raise

    if not result.claimed:
        try:
            _compare_and_delete(key, owner)
        except Exception as release_exc:
            print(f"⚠️  [{stage}] rejected claim lease cleanup: {release_exc}")
        return result

    _start_stage_lease(task_id, stage, result.identity)
    return result


def _claim_stage_lease(
    task_id: str,
    stage: str,
    *,
    overwrite: bool = False,
) -> bool:
    """Backward-compatible boolean claim helper; overwrite is never unsafe."""

    if overwrite:
        # Retain the old keyword for callers/tests, but never replace a live
        # owner.  NX plus the PostgreSQL token is the only safe protocol.
        pass
    result = _claim_stage_execution(
        task_id,
        stage,
        allowed_statuses=(TaskStatus.PENDING, TaskStatus.PROCESSING),
    )
    return bool(result and result.claimed)


def _release_stage_lease(task_id: str, stage: str) -> None:
    key = _stage_lease_key(task_id, stage)
    identity = _context_claims().pop(key, None)
    if identity is None:
        # A duplicate waiter in the same process must never pop the actual
        # owner's global renewer merely because both share task_id/stage.
        return
    owner = identity.owner
    with _lease_guard:
        active = _active_leases.get(key)
        if active and active[0] == owner:
            _active_leases.pop(key, None)
            _active_fence_tokens.pop(key, None)
            active[1].set()
    try:
        _compare_and_delete(key, owner)
    except Exception as exc:
        print(f"⚠️  [{stage}] stage lease release failed: {exc}")


def renew_stage_lease(task_id: str, stage: str) -> bool:
    """Extend the active local worker's lease without changing its owner."""
    key = _stage_lease_key(task_id, stage)
    with _lease_guard:
        active = _active_leases.get(key)
    if not active:
        return False
    owner, _stop = active
    with _lease_guard:
        if (key, owner) in _lost_stage_leases:
            return False
    try:
        return _renew_stage_lease_once(task_id, stage, owner)
    except Exception as exc:
        print(f"⚠️  [{stage}] stage lease heartbeat failed: {exc}")
        return False


def ensure_stage_lease_owned(task_id: str, stage: str) -> bool:
    """Atomically prove the current execution still owns its Redis lease."""

    key = _stage_lease_key(task_id, stage)
    identity = _local_stage_identity(task_id, stage)
    if identity is None:
        if key in _context_non_owners():
            raise StageLeaseLostError(
                f"[{stage}] task_id={task_id} 当前 delivery 没有执行权"
            )
        # Auxiliary/non-main stages historically have no lease.  PostgreSQL's
        # trigger remains the final guard if a fenced main-stage row exists.
        return True

    with _lease_guard:
        if (key, identity.owner) in _lost_stage_leases:
            raise StageLeaseLostError(
                f"[{stage}] task_id={task_id} stage lease ownership lost"
            )
    try:
        state = _compare_and_expire_state(key, identity.owner)
    except Exception as exc:
        _mark_stage_lease_lost(key, identity.owner)
        raise RuntimeError(
            f"[{stage}] task_id={task_id} cannot verify stage lease: {exc}"
        ) from exc
    if state != _LEASE_RENEWED:
        _mark_stage_lease_lost(key, identity.owner)
        raise StageLeaseLostError(
            f"[{stage}] task_id={task_id} stage lease ownership lost"
        )
    return True


_FENCE_ENV_TASK_ID = "NOVEL_GAME_STAGE_FENCE_TASK_ID"
_FENCE_ENV_STAGE = "NOVEL_GAME_STAGE_FENCE_STAGE"
_FENCE_ENV_OWNER = "NOVEL_GAME_STAGE_FENCE_OWNER"
_FENCE_ENV_TOKEN = "NOVEL_GAME_STAGE_FENCE_TOKEN"


def current_stage_identity(task_id: str, stage: str):
    """Return the current process' claimed identity for diagnostics/propagation."""
    return _local_stage_identity(task_id, stage)


def inherited_stage_identity():
    task_id = os.getenv(_FENCE_ENV_TASK_ID, "").strip()
    stage = os.getenv(_FENCE_ENV_STAGE, "").strip()
    owner = os.getenv(_FENCE_ENV_OWNER, "").strip()
    token_raw = os.getenv(_FENCE_ENV_TOKEN, "").strip()
    if not all((task_id, stage, owner, token_raw)):
        return None
    try:
        token = int(token_raw)
    except ValueError as exc:
        raise StageLeaseLostError("inherited stage fencing token is invalid") from exc
    return StageFenceIdentity(task_id=task_id, stage=stage, owner=owner, token=token)


def effective_stage_identity(task_id: str, stage: str):
    local = _local_stage_identity(task_id, stage)
    if local is not None:
        return local
    inherited = inherited_stage_identity()
    if inherited is None:
        return None
    if inherited.task_id != task_id or inherited.stage != stage:
        raise StageLeaseLostError(
            f"[{stage}] inherited execution belongs to "
            f"{inherited.task_id}/{inherited.stage}, not {task_id}/{stage}"
        )
    return inherited


def ensure_inherited_stage_lease_owned() -> bool:
    """Validate a parent task identity inherited by a generator subprocess."""
    identity = inherited_stage_identity()
    if identity is None:
        return True
    key = _stage_lease_key(identity.task_id, identity.stage)
    try:
        redis_owner = _stage_redis.get(key)
    except Exception as exc:
        raise RuntimeError(
            f"[{identity.stage}] cannot verify inherited Redis lease: {exc}"
        ) from exc
    if str(redis_owner or "") != identity.owner:
        raise StageLeaseLostError(
            f"[{identity.stage}] inherited Redis lease ownership lost"
        )
    database_identity = read_stage_fence(engine, identity.task_id, identity.stage)
    if database_identity != (identity.owner, identity.token):
        raise StageLeaseLostError(
            f"[{identity.stage}] inherited PostgreSQL fence ownership lost"
        )
    return True


def ensure_visual_stage_ownership(task_id: str) -> bool:
    """Validate whichever visual main stage owns the current execution."""
    inherited = inherited_stage_identity()
    if inherited is not None:
        if inherited.task_id != task_id:
            raise StageLeaseLostError(
                f"inherited visual execution belongs to {inherited.task_id}, not {task_id}"
            )
        return ensure_inherited_stage_lease_owned()
    for stage in ("char_designer", "scene_painter"):
        if _local_stage_identity(task_id, stage) is not None:
            return ensure_stage_lease_owned(task_id, stage)
    return True


def stage_execution_environment(task_id: str, stage: str) -> dict:
    """Environment propagated to subprocesses for fenced atomic publication."""
    identity = _local_stage_identity(task_id, stage)
    if identity is None:
        return {}
    execution_id = ""
    platform_attempt = "0"
    try:
        from celery import current_task

        request = getattr(current_task, "request", None)
        execution_id = str(getattr(request, "id", "") or "")
        headers = getattr(request, "headers", None) or {}
        platform_attempt = str(headers.get("platform_attempt") or "0")
    except Exception:
        pass
    return {
        _FENCE_ENV_TASK_ID: identity.task_id,
        _FENCE_ENV_STAGE: identity.stage,
        _FENCE_ENV_OWNER: identity.owner,
        _FENCE_ENV_TOKEN: str(identity.token),
        "NOVEL_GAME_EXECUTION_ID": execution_id,
        "NOVEL_GAME_PLATFORM_ATTEMPT": platform_attempt,
    }


def claim_stage_for_validated_resume(task_id: str, stage: str, completion_validator) -> str:
    """Claim a main stage while auditing a previously completed delivery.

    Returns ``execute``, ``wait`` or ``complete``.  FAILED remains platform-owned:
    only a new platform attempt may reset it to PENDING.
    """
    if stage not in VALID_STAGE_NAMES:
        raise RuntimeError(f"[{stage}] 无效的主流水线阶段")
    stage_field = f"{stage}_status"
    with Session(engine) as session:
        task = session.exec(
            select(NovelTask).where(NovelTask.task_id == task_id)
        ).first()
        if task is None:
            raise RuntimeError(f"[{stage}] task_id={task_id} 记录不存在，无法执行")
        current = getattr(task, stage_field, None)

    if current == TaskStatus.COMPLETED:
        try:
            valid = bool(completion_validator())
        except Exception as exc:
            print(f"⚠️  [{stage}] 已完成产物复核失败，将进入补跑: {exc}")
            valid = False
        if valid:
            _fence_stage_non_owner(task_id, stage)
            return "complete"
        result = _claim_stage_execution(
            task_id, stage, allowed_statuses=(TaskStatus.COMPLETED,)
        )
        if result and result.claimed:
            from sqlalchemy import update as sa_update

            with Session(engine) as session:
                session.exec(
                    sa_update(NovelTask)
                    .where(NovelTask.task_id == task_id)
                    .values(
                        overall_status=TaskStatus.PROCESSING,
                        error_message=None,
                    )
                    .execution_options(synchronize_session=False)
                )
                session.commit()
            print(f"🔁 [{stage}] 已完成状态的产物不完整，按原 task_id 补跑")
            return "execute"
        _fence_stage_non_owner(task_id, stage)
        return "wait"

    if current == TaskStatus.PENDING:
        result = _claim_stage_execution(
            task_id, stage, allowed_statuses=(TaskStatus.PENDING,)
        )
        if result and result.claimed:
            return "execute"
        _fence_stage_non_owner(task_id, stage)
        return "wait"
    if current == TaskStatus.FAILED:
        _fence_stage_non_owner(task_id, stage)
        raise RuntimeError(
            f"[{stage}] task_id={task_id} 当前为失败态，等待中台创建下一次业务重试"
        )
    if current != TaskStatus.PROCESSING:
        _fence_stage_non_owner(task_id, stage)
        raise RuntimeError(f"[{stage}] 非法阶段状态: {current!r}")

    lease_key = _stage_lease_key(task_id, stage)
    try:
        lease_owner = _stage_redis.get(lease_key)
        if not lease_owner:
            time.sleep(_STAGE_LEASE_STARTUP_GRACE)
            lease_owner = _stage_redis.get(lease_key)
        if not lease_owner:
            result = _claim_stage_execution(
                task_id, stage, allowed_statuses=(TaskStatus.PROCESSING,)
            )
            if result and result.claimed:
                print(f"🔁 [{stage}] task_id={task_id} 已由本 Worker 接管")
                return "execute"
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"[{stage}] stage lease check failed: {exc}") from exc
    _fence_stage_non_owner(task_id, stage)
    return "wait"


def fail_pending_stage_claim(
    task_id: str,
    stage: str,
    progress: int,
    error_msg: str,
) -> bool:
    """Safely turn an unclaimed PENDING stage into FAILED without Redis.

    This is only for failures that occur while acquiring the normal Redis/DB
    execution claim.  PostgreSQL serializes this emergency claim against a real
    worker: whichever locks the task row first wins, so an active PROCESSING
    owner can never be overwritten.
    """
    owner = f"claim-failure:{os.getpid()}:{uuid.uuid4().hex}"
    result = claim_stage_fence(
        engine,
        task_id=task_id,
        stage=stage,
        owner=owner,
        allowed_statuses=frozenset({TaskStatus.PENDING}),
        processing_status=TaskStatus.PROCESSING,
    )
    if not result.claimed:
        return False
    identity = result.identity
    from common.stage_fencing import lock_and_validate_stage_fence_connection

    task_table = (
        "novel_tasks" if engine.dialect.name == "sqlite" else "public.novel_tasks"
    )
    with engine.begin() as connection:
        lock_and_validate_stage_fence_connection(connection, identity)
        update_result = connection.execute(
            text(f"""
                UPDATE {task_table}
                   SET {stage}_status = :failed,
                       overall_status = :failed,
                       progress_percentage = :progress,
                       error_message = :error
                 WHERE task_id = :task_id
                   AND {stage}_status = :processing
            """),
            {
                "failed": TaskStatus.FAILED,
                "processing": TaskStatus.PROCESSING,
                "progress": max(0, min(100, progress)),
                "error": str(error_msg or "")[:4000],
                "task_id": task_id,
            },
        )
    return update_result.rowcount == 1


def _all_required_stages_completed(task: NovelTask) -> bool:
    """Check completion using the active runtime's stage contract."""
    checks = [
        task.story_planner_status == TaskStatus.COMPLETED,
        task.info_extractor_status == TaskStatus.COMPLETED,
        task.char_designer_status == TaskStatus.COMPLETED,
        task.scene_painter_status == TaskStatus.COMPLETED,
        task.game_builder_status == TaskStatus.COMPLETED,
    ]
    if not local_pipeline_enabled():
        checks.extend([
            task.sound_arranger_status == TaskStatus.COMPLETED,
            task.audio_producer_status == TaskStatus.COMPLETED,
        ])
    return all(checks)


def _load_fresh_task(session, task_id: str):
    """Reload a task after a targeted UPDATE bypassed the identity map."""
    session.expire_all()
    return session.exec(
        select(NovelTask).where(NovelTask.task_id == task_id)
    ).first()


def update_task_progress(
    task_id: str,
    stage: str = None,
    stage_status: int = None,
    progress: int = None,
    error_msg: str = None
):
    """
    更新任务进度

    Args:
        task_id: 任务ID
        stage: 阶段名称（story_planner, info_extractor, sound_arranger, char_designer, audio_producer, scene_painter, game_builder）
        stage_status: 阶段状态（0-待处理, 1-处理中, 2-已完成, 3-失败）
        progress: 进度百分比（0-100）
        error_msg: 错误信息

    Example:
        # 开始处理
        update_task_progress('douluodalu_722eed3e', 'char_designer', TaskStatus.PROCESSING, 40)

        # 完成处理
        update_task_progress('douluodalu_722eed3e', 'char_designer', TaskStatus.COMPLETED, 60)

        # 失败
        update_task_progress('douluodalu_722eed3e', 'char_designer', TaskStatus.FAILED, 40, '生成失败')
    """
    from sqlalchemy import update as sa_update
    try:
        # 并发安全：char/audio/sound 三个分支会同时调用本函数更新同一行。
        # 必须用针对性 UPDATE（只写本次涉及的列），不能"读整行对象 -> 改 -> 全列写回"，
        # 否则 Read Committed 下会发生 lost update（后提交者用旧快照覆盖他人刚写的列）。
        values = {}
        if stage and stage_status is not None:
            stage_field = f"{stage}_status"
            if not hasattr(NovelTask, stage_field):
                print(f"⚠️  无效的阶段名称: {stage}")
                return False
            values[stage_field] = stage_status
        if progress is not None:
            bounded_progress = max(0, min(100, progress))
            values["progress_percentage"] = func.greatest(
                func.coalesce(NovelTask.progress_percentage, 0),
                bounded_progress,
            )
        if error_msg:
            values["error_message"] = error_msg
        if stage_status == TaskStatus.FAILED:
            values["overall_status"] = TaskStatus.FAILED

        with Session(engine) as session:
            identity = None
            if stage and stage_status in (
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
            ):
                identity = _local_stage_identity(task_id, stage)
            if identity is not None:
                # Lock order matches claim_stage_fence: novel_tasks first,
                # fence second.  The trigger GUC is transaction-local.
                locked_task = session.exec(
                    select(NovelTask)
                    .where(NovelTask.task_id == task_id)
                    .with_for_update()
                ).first()
                if locked_task is None:
                    print(f"⚠️  任务不存在: {task_id}")
                    return False
                lock_and_validate_stage_fence(session, identity)

            if values:
                statement = sa_update(NovelTask).where(
                    NovelTask.task_id == task_id
                )
                if stage and stage_status in (
                    TaskStatus.COMPLETED,
                    TaskStatus.FAILED,
                ):
                    # Terminal states may only be entered from PROCESSING.
                    # This also prevents an old failure from replacing a new
                    # completion even on legacy rows without a fence.
                    statement = statement.where(
                        getattr(NovelTask, f"{stage}_status")
                        == TaskStatus.PROCESSING
                    )
                result = session.exec(
                    statement.values(**values).execution_options(
                        synchronize_session=False
                    )
                )
                if result.rowcount == 0:
                    print(
                        f"⚠️  [{stage}] task_id={task_id} 终态 CAS 未命中"
                    )
                    return False

            # 重新读取最新状态判断是否全部完成（基于已提交的最新值，而非进入函数时的旧快照）
            # 针对性 UPDATE 使用 synchronize_session=False 避免并发分支的
            # 全列回写；_load_fresh_task 会先清理 identity map，确保这里
            # 判断的是刚写入的阶段状态，而不是旧缓存对象。
            task = _load_fresh_task(session, task_id)
            if task and _all_required_stages_completed(task):
                session.exec(
                    sa_update(NovelTask)
                    .where(NovelTask.task_id == task_id)
                    .values(
                        overall_status=TaskStatus.COMPLETED,
                        progress_percentage=100,
                        completed_at=datetime.now(),
                    )
                    .execution_options(synchronize_session=False)
                )
            session.commit()
            return True

    except Exception as e:
        print(f"❌ 更新进度失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def get_task_progress(task_id: str):
    """
    查询任务进度

    Args:
        task_id: 任务ID

    Returns:
        dict: 任务信息，如果不存在返回 None
    """
    try:
        with Session(engine) as session:
            task = session.exec(
                select(NovelTask).where(NovelTask.task_id == task_id)
            ).first()

            if not task:
                return None

            return {
                'task_id': task.task_id,
                'progress_percentage': task.progress_percentage,
                'overall_status': task.overall_status,
                'stages': {
                    'story_planner': task.story_planner_status,
                    'info_extractor': task.info_extractor_status,
                    'sound_arranger': task.sound_arranger_status,
                    'char_designer': task.char_designer_status,
                    'audio_producer': task.audio_producer_status,
                    'scene_painter': task.scene_painter_status,
                    'game_builder': task.game_builder_status,
                },
                'error_message': task.error_message
            }
    except Exception as e:
        print(f"❌ 查询进度失败: {e}")
        return None


def is_stage_already_running(task_id: str, stage: str) -> bool:
    """
    原子性地将阶段从 PENDING 改为 PROCESSING，返回 False 表示抢占成功可以继续执行。
    返回 True 表示已被其他 worker 抢占，调用方应等待该阶段完成后再继续 chain。
    """
    try:
        if stage not in VALID_STAGE_NAMES:
            raise RuntimeError(f"[{stage}] 无效的主流水线阶段")
        stage_field = f"{stage}_status"
        with Session(engine) as session:
            task = session.exec(
                select(NovelTask).where(NovelTask.task_id == task_id)
            ).first()
            if task is None:
                raise RuntimeError(f"[{stage}] task_id={task_id} 记录不存在，无法执行")
            current = getattr(task, stage_field, None)

        if current == TaskStatus.PENDING:
            result = _claim_stage_execution(
                task_id, stage, allowed_statuses=(TaskStatus.PENDING,)
            )
            if result and result.claimed:
                return False
            _fence_stage_non_owner(task_id, stage)
            return True
        if current == TaskStatus.COMPLETED:
            _fence_stage_non_owner(task_id, stage)
            return True
        if current == TaskStatus.FAILED:
            _fence_stage_non_owner(task_id, stage)
            raise RuntimeError(
                f"[{stage}] task_id={task_id} 当前为失败态，等待中台创建下一次业务重试"
            )
        if current != TaskStatus.PROCESSING:
            _fence_stage_non_owner(task_id, stage)
            raise RuntimeError(f"[{stage}] 非法阶段状态: {current!r}")

        lease_key = _stage_lease_key(task_id, stage)
        try:
            lease_owner = _stage_redis.get(lease_key)
            if not lease_owner:
                time.sleep(_STAGE_LEASE_STARTUP_GRACE)
                lease_owner = _stage_redis.get(lease_key)
            if not lease_owner:
                result = _claim_stage_execution(
                    task_id,
                    stage,
                    allowed_statuses=(TaskStatus.PROCESSING,),
                )
                if result and result.claimed:
                    print(f"🔁 [{stage}] task_id={task_id} 已由本 Worker 接管")
                    return False
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"[{stage}] stage lease check failed: {exc}"
            ) from exc

        _fence_stage_non_owner(task_id, stage)
        return True
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"[{stage}] task_id={task_id} 原子抢占失败，中断执行: {e}") from e


# 等待超时必须大于任何阶段自身的执行上限，否则持有者仍在正常工作时，
# 等待方就会抛异常并中断 chain——白白丢掉一个已经跑了几小时的任务。
#
# 主生成任务默认最多 48 小时。重复投递等待原持有者时多留 30 分钟收尾；
# 该等待上限本身不会终止真正执行任务的 Worker。
_MAIN_TASK_TIME_LIMIT = int(os.environ.get("MAIN_TASK_TIME_LIMIT", "172800"))
_DEFAULT_STAGE_WAIT_TIMEOUT = int(
    os.environ.get("STAGE_WAIT_TIMEOUT", str(_MAIN_TASK_TIME_LIMIT + 1800))
)
# 轮询间隔的上限（退避后不再增长）。
_MAX_POLL_INTERVAL = 60


def wait_for_stage_completion(
    task_id: str,
    stage: str,
    poll_interval: int = 5,
    timeout: int = _DEFAULT_STAGE_WAIT_TIMEOUT,
):
    """
    等待某阶段完成（COMPLETED 或 FAILED）。
    被抢占的 worker 调用此函数阻塞等待，确保 chain 不会提前推进。
    超时后抛出异常中断 chain。

    注意：新增阶段若要设置 time_limit，必须确保它小于本超时，
    否则该阶段会在正常执行期间被等待方误判为卡死。
    """
    import time
    stage_field = f"{stage}_status"
    elapsed = 0
    interval = poll_interval
    while elapsed < timeout:
        try:
            with Session(engine) as session:
                task = session.exec(
                    select(NovelTask).where(NovelTask.task_id == task_id)
                ).first()
                if task:
                    status = getattr(task, stage_field, None)
                    if status == TaskStatus.COMPLETED:
                        print(f"✅ [{stage}] task_id={task_id} 已由其他 worker 完成，继续 chain")
                        return
                    if status == TaskStatus.FAILED:
                        raise Exception(f"[{stage}] task_id={task_id} 由其他 worker 执行失败")
        except Exception as e:
            if "由其他 worker 执行失败" in str(e):
                raise
            print(f"⚠️  wait_for_stage_completion 查询异常: {e}")
        time.sleep(interval)
        elapsed += interval
        # 退避：每次轮询都新开一个 Session，固定 5s 间隔在小时级等待下会
        # 累积上千次无谓查询。前期保持灵敏（刚被抢占时对方可能很快结束），
        # 之后逐步拉长到 60s——阶段耗时是小时级，晚一分钟发现无影响。
        interval = min(interval * 2, _MAX_POLL_INTERVAL)
    raise Exception(f"[{stage}] task_id={task_id} 等待超时 ({timeout}s)")


# 快捷函数
def start_stage(task_id: str, stage: str, progress: int):
    """开始某个阶段"""
    if stage in VALID_STAGE_NAMES and _local_stage_identity(task_id, stage) is None:
        # Manual regeneration paths historically call start_stage directly.
        # Give them the same fenced ownership protocol without changing their
        # public call signature.
        result = _claim_stage_execution(
            task_id,
            stage,
            allowed_statuses=(
                TaskStatus.PENDING,
                TaskStatus.PROCESSING,
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
            ),
        )
        if not result or not result.claimed:
            _fence_stage_non_owner(task_id, stage)
            return False
    return update_task_progress(task_id, stage, TaskStatus.PROCESSING, progress)


def complete_stage(task_id: str, stage: str, progress: int):
    """完成阶段；Redis 与 PostgreSQL owner/token 均通过才可提交。"""
    if stage not in VALID_STAGE_NAMES:
        return update_task_progress(
            task_id, stage, TaskStatus.COMPLETED, progress
        )
    try:
        ensure_stage_lease_owned(task_id, stage)
        updated = update_task_progress(
            task_id, stage, TaskStatus.COMPLETED, progress
        )
        if not updated:
            identity = _local_stage_identity(task_id, stage)
            if identity is not None:
                _mark_stage_lease_lost(
                    _stage_lease_key(task_id, stage), identity.owner
                )
            _fence_stage_non_owner(task_id, stage)
            raise StageCompletionError(
                f"[{stage}] task_id={task_id} failed to persist completion"
            )
        return True
    finally:
        _release_stage_lease(task_id, stage)


def fail_stage(task_id: str, stage: str, progress: int, error_msg: str):
    """仅当前 owner 可提交失败；duplicate/stale wrapper 直接跳过。"""
    if stage not in VALID_STAGE_NAMES:
        return update_task_progress(
            task_id, stage, TaskStatus.FAILED, progress, error_msg
        )
    try:
        try:
            ensure_stage_lease_owned(task_id, stage)
        except (StageLeaseLostError, RuntimeError) as exc:
            print(f"⚠️  [{stage}] skip non-owner failure update: {exc}")
            return False
        return update_task_progress(
            task_id, stage, TaskStatus.FAILED, progress, error_msg
        )
    finally:
        _release_stage_lease(task_id, stage)
