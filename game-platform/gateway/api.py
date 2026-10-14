"""平台网关 API:任务创建/查询/取消 + token 用量查询 + 发布"""
from datetime import datetime
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import inspect, text as sql_text
from sqlmodel import Session, select

from common.models import (
    Task, TaskStep,
    PlatformUser, engine,
)
from orchestrator import core as orchestrator
from auth.deps import get_user_or_service, get_current_user

router = APIRouter(prefix="/api/v1", tags=["gateway"])


class TaskCreateIn(BaseModel):
    task_id: str
    template_name: str = "default"
    pipeline_name: Optional[str] = None  # 按 pipeline 表解析 impl_map(显式 impl_map 优先)
    impl_map: dict = {}   # {"char_designer": "groupB.char.v1"} 缺省能力走自动选择
    params: dict = {}     # art_style / output_language / story_mode / 模型选择...


class RestartTaskIn(BaseModel):
    """人工重启范围；空 body 仍按旧行为重启进行中和失败模块。"""
    mode: Literal["failed_only", "running_and_failed"] = "running_and_failed"


class StepOut(BaseModel):
    capability: str
    impl_id: Optional[str]
    group_name: Optional[str]
    status: str
    progress: int
    attempt: int
    error_code: Optional[str] = None
    error_msg: Optional[str] = None


class TaskOut(BaseModel):
    task_id: str
    status: str
    progress: int
    steps: List[StepOut]


@router.post("/tasks", response_model=TaskOut)
def create_task(
    body: TaskCreateIn,
    user: Optional[PlatformUser] = Depends(get_user_or_service),
):
    # 强制鉴权二选一:novel-game-factory api_gateway 带 X-Service-Token(user=None),
    # 前端带平台 JWT(user_id 记到 task 上)。
    try:
        orchestrator.submit(
            task_id=body.task_id, template_name=body.template_name,
            impl_map=body.impl_map, params=body.params,
            user_id=user.id if user else None,
            pipeline_name=body.pipeline_name,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return get_task(body.task_id)


@router.get("/tasks/{task_id}", response_model=TaskOut)
def get_task(task_id: str):
    with Session(engine) as session:
        task = session.exec(select(Task).where(Task.task_id == task_id)).first()
        if task is None:
            raise HTTPException(404, f"task not found: {task_id}")
        steps = session.exec(
            select(TaskStep).where(TaskStep.task_id == task_id)
        ).all()
        return TaskOut(
            task_id=task.task_id, status=task.status, progress=task.progress,
            steps=[StepOut(
                capability=s.capability, impl_id=s.impl_id,
                group_name=s.group_name, status=s.status, progress=s.progress,
                attempt=s.attempt, error_code=s.error_code, error_msg=s.error_msg,
            ) for s in steps],
        )


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str):
    if not orchestrator.request_cancel(task_id):
        raise HTTPException(404, f"task not found: {task_id}")
    return {"ok": True}


# ── token 用量(收口后统一直查 api_usage 表,novel-game-factory 各服务直写、带 group_name;
#    平台自己的 token_usage 表保留双写,阶段5后退役) ──

_API_USAGE_COLUMNS = [
    "id", "task_id", "service", "api_type", "provider", "model",
    "prompt_tokens", "completion_tokens", "cached_input_tokens",
    "cache_write_tokens", "reasoning_tokens", "total_tokens",
    "call_count", "image_count", "tts_chars", "music_count",
    "music_seconds", "run_id", "group_name", "pipeline_name", "created_at",
]

# 用量的业务口径是管线名;老数据没有 pipeline_name(那时只按执行方组名记),
# 回退到 group_name 让历史用量仍归在原名字下,不会在页面上凭空消失。
_PIPELINE_EXPR = "COALESCE(NULLIF(pipeline_name, ''), group_name)"


@router.get("/tasks/{task_id}/usage")
def task_usage(task_id: str):
    """单任务 token 用量明细,直查 api_usage(精确匹配 task_id)。"""
    with Session(engine) as session:
        rows = session.exec(
            sql_text(f"""
                SELECT {", ".join(_API_USAGE_COLUMNS)}
                FROM public.api_usage
                WHERE task_id = :tid
                ORDER BY created_at
            """).bindparams(tid=task_id)
        ).all()
    items = [dict(zip(_API_USAGE_COLUMNS, row)) for row in rows]
    for item in items:
        if item["created_at"] is not None:
            item["created_at"] = item["created_at"].isoformat()
    return items


@router.get("/usage/summary")
def usage_summary(
    group: Optional[str] = None,            # 管线名精确筛选,可选
    start_time: Optional[datetime] = None,  # created_at 范围,可选
    end_time: Optional[datetime] = None,
):
    """按管线聚合 token 用量,SQL group by 直查 api_usage。"""
    where = ["1=1"]
    params: dict = {}
    if group:
        where.append(f"{_PIPELINE_EXPR} = :group")
        params["group"] = group
    if start_time:
        where.append("created_at >= :start_time")
        params["start_time"] = start_time
    if end_time:
        where.append("created_at <= :end_time")
        params["end_time"] = end_time
    where_sql = " AND ".join(where)

    with Session(engine) as session:
        rows = session.exec(
            sql_text(f"""
                SELECT {_PIPELINE_EXPR} AS pipeline,
                       COALESCE(SUM(prompt_tokens), 0),
                       COALESCE(SUM(completion_tokens), 0),
                       COALESCE(SUM(total_tokens), 0),
                       COALESCE(SUM(call_count), 0),
                       COALESCE(SUM(image_count), 0),
                       COALESCE(SUM(tts_chars), 0),
                       COALESCE(SUM(music_count), 0),
                       COUNT(DISTINCT task_id)
                FROM public.api_usage
                WHERE {where_sql}
                GROUP BY pipeline
                ORDER BY pipeline
            """).bindparams(**params)
        ).all()
    return [
        {
            "group_name": r[0],
            "prompt_tokens": int(r[1]),
            "completion_tokens": int(r[2]),
            "total_tokens": int(r[3]),
            "call_count": int(r[4]),
            "image_count": int(r[5]),
            "tts_chars": int(r[6]),
            "music_count": int(r[7]),
            "task_count": int(r[8]),
        }
        for r in rows
    ]


@router.get("/usage/summary/{group_name}")
def usage_group_detail(
    group_name: str,
    start_time: Optional[datetime] = None,  # created_at 范围,可选
    end_time: Optional[datetime] = None,
):
    """单管线用量分布:按 service+api_type 聚合,给前端画该管线详情图表。"""
    where = [f"{_PIPELINE_EXPR} = :group_name"]
    params: dict = {"group_name": group_name}
    if start_time:
        where.append("created_at >= :start_time")
        params["start_time"] = start_time
    if end_time:
        where.append("created_at <= :end_time")
        params["end_time"] = end_time
    where_sql = " AND ".join(where)

    with Session(engine) as session:
        rows = session.exec(
            sql_text(f"""
                SELECT service, api_type,
                       COALESCE(SUM(prompt_tokens), 0),
                       COALESCE(SUM(completion_tokens), 0),
                       COALESCE(SUM(total_tokens), 0),
                       COALESCE(SUM(call_count), 0),
                       COALESCE(SUM(image_count), 0),
                       COALESCE(SUM(tts_chars), 0),
                       COALESCE(SUM(music_count), 0)
                FROM public.api_usage
                WHERE {where_sql}
                GROUP BY service, api_type
                ORDER BY service, api_type
            """).bindparams(**params)
        ).all()
    return [
        {
            "service": r[0],
            "api_type": r[1],
            "prompt_tokens": int(r[2]),
            "completion_tokens": int(r[3]),
            "total_tokens": int(r[4]),
            "call_count": int(r[5]),
            "image_count": int(r[6]),
            "tts_chars": int(r[7]),
            "music_count": int(r[8]),
        }
        for r in rows
    ]


# ── 后台看板:跨组任务列表 / token 明细列表(直读 novel_tasks / api_usage) ──
# 这两张表是 12 张跨模块共用表的一部分(novel-game-factory 侧读写,结构见
# novel-game-factory/common/platform_task_models.py 与 common/usage_tracker.py),
# 本服务只读查询,不建模、不建表,用原生 SQL 避免和 12 表的所有权混淆。

class PageOut(BaseModel):
    total: int
    page: int
    page_size: int
    items: list


@router.get("/admin/tasks", response_model=PageOut)
def list_novel_tasks(
    group_name: Optional[str] = None,   # 模糊匹配
    project_name: Optional[str] = None,  # 模糊匹配
    task_id: Optional[str] = None,       # 模糊匹配
    user_id: Optional[str] = None,       # 精确匹配:创作页只看自己提交的
    overall_status: Optional[int] = None,
    start_time: Optional[datetime] = None,  # 按 update_time 范围模糊筛选
    end_time: Optional[datetime] = None,
    page: int = 1,
    page_size: int = 20,
):
    """任务列表看板:按管线名/项目名/任务ID模糊查询 + 时间范围,直查 novel_tasks。"""
    page = max(page, 1)
    page_size = min(max(page_size, 1), 200)
    offset = (page - 1) * page_size

    where = ["is_deleted = false"]
    params: dict = {}
    if group_name:
        # 按管线筛。老任务没有 pipeline_name,回退到 group_name(见 _PIPELINE_EXPR)
        where.append(f"{_PIPELINE_EXPR} ILIKE :group_name")
        params["group_name"] = f"%{group_name}%"
    if project_name:
        where.append("project_name ILIKE :project_name")
        params["project_name"] = f"%{project_name}%"
    if task_id:
        where.append("task_id ILIKE :task_id")
        params["task_id"] = f"%{task_id}%"
    if user_id:
        where.append("user_id = :user_id")
        params["user_id"] = user_id
    if overall_status is not None:
        where.append("overall_status = :overall_status")
        params["overall_status"] = overall_status
    if start_time:
        where.append("update_time >= :start_time")
        params["start_time"] = start_time
    if end_time:
        where.append("update_time <= :end_time")
        params["end_time"] = end_time
    where_sql = " AND ".join(where)

    with Session(engine) as session:
        total = session.exec(
            sql_text(f"SELECT count(*) FROM public.novel_tasks WHERE {where_sql}")
            .bindparams(**params)
        ).first()[0]

        rows = session.exec(
            sql_text(f"""
                SELECT task_id, novel_filename, project_name, generated_project_name,
                       user_id, user_name,
                       {_PIPELINE_EXPR} AS group_name, output_language, art_style,
                       story_mode, gameplay, overall_status, progress_percentage,
                       story_planner_status, info_extractor_status, sound_arranger_status,
                       char_designer_status, audio_producer_status, scene_painter_status,
                       game_builder_status, error_message, create_time, update_time,
                       completed_at
                FROM public.novel_tasks
                WHERE {where_sql}
                ORDER BY update_time DESC
                LIMIT :limit OFFSET :offset
            """).bindparams(**params, limit=page_size, offset=offset)
        ).all()

        columns = [
            "task_id", "novel_filename", "project_name", "generated_project_name",
            "user_id", "user_name", "group_name", "output_language", "art_style",
            "story_mode", "gameplay", "overall_status", "progress_percentage",
            "story_planner_status", "info_extractor_status", "sound_arranger_status",
            "char_designer_status", "audio_producer_status", "scene_painter_status",
            "game_builder_status", "error_message", "create_time", "update_time",
            "completed_at",
        ]
        items = [dict(zip(columns, row)) for row in rows]
        for item in items:
            for key in ("create_time", "update_time", "completed_at"):
                if item[key] is not None:
                    item[key] = item[key].isoformat()

    return PageOut(total=total, page=page, page_size=page_size, items=items)


@router.get("/admin/tasks/mine", response_model=PageOut)
def list_my_novel_tasks(
    page: int = 1,
    page_size: int = 20,
    user: PlatformUser = Depends(get_current_user),
):
    """创作测试的当前用户任务列表,归属由 JWT 的 CMS uid 决定。"""
    if not user.main_site_uid:
        raise HTTPException(400, "当前账号缺少主站 uid,无法查询创作任务")
    return list_novel_tasks(
        user_id=str(user.main_site_uid),
        page=page,
        page_size=page_size,
    )


@router.delete("/admin/tasks/{task_id}")
def delete_novel_task(
    task_id: str,
    user: Optional[PlatformUser] = Depends(get_user_or_service),
):
    """取消任务并从列表移除:中台标记取消 + novel_tasks 软删除。

    不杀 worker 副本:队列是按 impl_id 共享的,一个副本轮着处理所有任务的消息,
    杀掉它等于打断该实现上所有人正在跑的任务。这里只保证「不再往下派发」,
    正在跑的那一步会自然跑完。
    """
    # 中台侧:老链路直发 Celery 的任务在 task 表里没有,取消不到属正常
    cancelled = orchestrator.request_cancel(task_id)

    with Session(engine) as session:
        result = session.exec(
            sql_text(
                "UPDATE public.novel_tasks SET is_deleted = true, update_time = now() "
                "WHERE task_id = :tid AND is_deleted = false"
            ).bindparams(tid=task_id)
        )
        session.commit()
        removed = result.rowcount or 0

    if not cancelled and not removed:
        raise HTTPException(404, f"task not found: {task_id}")
    return {"ok": True, "cancelled": cancelled, "removed": removed}


@router.post("/admin/tasks/{task_id}/restart")
def restart_novel_task(
    task_id: str,
    user: PlatformUser = Depends(get_current_user),
    body: Optional[RestartTaskIn] = None,
):
    """重新启动同一任务：已完成/等待模块保留，按 mode 重跑指定阶段。"""
    if user.role != "admin":
        owner_uid = _novel_task_owner_uid(task_id)
        if owner_uid is None:
            raise HTTPException(404, f"task not found: {task_id}")
        if str(owner_uid or "") != str(user.main_site_uid or ""):
            # 不向普通用户泄露另一账号的 task_id 是否存在。
            raise HTTPException(404, f"task not found: {task_id}")
    try:
        if body is None:
            # 保留旧客户端/内部调用的单参数兼容性；默认语义仍是
            # running_and_failed。
            result = orchestrator.request_restart(task_id)
        else:
            result = orchestrator.request_restart(task_id, mode=body.mode)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if result is None:
        raise HTTPException(404, f"task not found: {task_id}")
    return {"ok": True, **result}


@router.post("/admin/tasks/{task_id}/stages/{capability}/run")
def run_novel_task_stage(
    task_id: str,
    capability: str,
    user: PlatformUser = Depends(get_current_user),
):
    """只运行指定模块，沿用原 task_id；其它模块状态和产物不变。"""
    if user.role != "admin":
        owner_uid = _novel_task_owner_uid(task_id)
        if owner_uid is None:
            raise HTTPException(404, f"task not found: {task_id}")
        if str(owner_uid or "") != str(user.main_site_uid or ""):
            raise HTTPException(404, f"task not found: {task_id}")
    try:
        result = orchestrator.request_stage_run(task_id, capability)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if result is None:
        raise HTTPException(404, f"task not found: {task_id}")
    return {"ok": True, **result}


def _novel_task_owner_uid(task_id: str) -> Optional[str]:
    bind = engine
    sqlite = bind.dialect.name == "sqlite"
    if sqlite and not inspect(bind).has_table("novel_tasks"):
        return None
    table = "novel_tasks" if sqlite else "public.novel_tasks"
    with Session(bind) as session:
        row = session.exec(
            sql_text(
                f"SELECT user_id FROM {table} "
                "WHERE task_id = :tid AND is_deleted = false"
            ).bindparams(tid=task_id)
        ).first()
    return None if row is None else row[0]


@router.get("/admin/usage", response_model=PageOut)
def list_api_usage(
    group_name: Optional[str] = None,  # 模糊匹配
    task_id: Optional[str] = None,     # 模糊匹配
    service: Optional[str] = None,     # 模糊匹配
    model: Optional[str] = None,       # 模糊匹配
    api_type: Optional[str] = None,    # 精确匹配:llm/image/tts/music
    start_time: Optional[datetime] = None,  # 按 created_at 范围模糊筛选
    end_time: Optional[datetime] = None,
    page: int = 1,
    page_size: int = 20,
):
    """token 用量明细看板:按管线名/任务ID/服务/模型模糊查询 + 时间范围,直查 api_usage。"""
    page = max(page, 1)
    page_size = min(max(page_size, 1), 200)
    offset = (page - 1) * page_size

    where = ["1=1"]
    params: dict = {}
    if group_name:
        where.append("group_name ILIKE :group_name")
        params["group_name"] = f"%{group_name}%"
    if task_id:
        where.append("task_id ILIKE :task_id")
        params["task_id"] = f"%{task_id}%"
    if service:
        where.append("service ILIKE :service")
        params["service"] = f"%{service}%"
    if model:
        where.append("model ILIKE :model")
        params["model"] = f"%{model}%"
    if api_type:
        where.append("api_type = :api_type")
        params["api_type"] = api_type
    if start_time:
        where.append("created_at >= :start_time")
        params["start_time"] = start_time
    if end_time:
        where.append("created_at <= :end_time")
        params["end_time"] = end_time
    where_sql = " AND ".join(where)

    with Session(engine) as session:
        total = session.exec(
            sql_text(f"SELECT count(*) FROM public.api_usage WHERE {where_sql}")
            .bindparams(**params)
        ).first()[0]

        rows = session.exec(
            sql_text(f"""
                SELECT id, task_id, service, api_type, provider, model,
                       prompt_tokens, completion_tokens, cached_input_tokens,
                       cache_write_tokens, reasoning_tokens, total_tokens,
                       call_count, image_count, tts_chars, music_count,
                       music_seconds, run_id, group_name, created_at
                FROM public.api_usage
                WHERE {where_sql}
                ORDER BY created_at DESC
                LIMIT :limit OFFSET :offset
            """).bindparams(**params, limit=page_size, offset=offset)
        ).all()

        columns = [
            "id", "task_id", "service", "api_type", "provider", "model",
            "prompt_tokens", "completion_tokens", "cached_input_tokens",
            "cache_write_tokens", "reasoning_tokens", "total_tokens",
            "call_count", "image_count", "tts_chars", "music_count",
            "music_seconds", "run_id", "group_name", "created_at",
        ]
        items = [dict(zip(columns, row)) for row in rows]
        for item in items:
            if item["created_at"] is not None:
                item["created_at"] = item["created_at"].isoformat()

    return PageOut(total=total, page=page, page_size=page_size, items=items)
