"""Token 用量 API:按当前登录用户口径查询(全部作品累计)。

数据源:agent_platform 库跨模块只读表 novel_tasks / api_usage,用原生 SQL。
关联链:平台登录 uid → novel_tasks.user_id → task_id → api_usage.task_id。
口径:只统计 token/调用数量,不折算金额。
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session, text as sql_text

from common.models import PlatformUser, engine
from auth.deps import get_current_user

router = APIRouter(prefix="/api/v1", tags=["usage"])

# api_type 固定枚举(前端下拉写死,这里仅作校验参考):llm/image/tts/music


def _user_task_ids(session: Session, uid: str) -> list[str]:
    """当前用户全部未删除作品的 task_id 列表。"""
    rows = session.exec(
        sql_text(
            "SELECT task_id FROM public.novel_tasks "
            "WHERE user_id = :uid AND is_deleted = false"
        ).bindparams(uid=uid)
    ).all()
    return [r[0] for r in rows]


class UsageSummaryOut(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    call_count: int
    image_count: int
    tts_chars: int
    music_count: int
    task_count: int


@router.get("/usage/mine/summary", response_model=UsageSummaryOut)
def my_usage_summary(user: PlatformUser = Depends(get_current_user)):
    """当前用户全部作品的 token 用量累计(账户面板 Token 账单总量)。"""
    with Session(engine) as session:
        task_ids = _user_task_ids(session, user.main_site_uid)
        if not task_ids:
            return UsageSummaryOut(
                prompt_tokens=0, completion_tokens=0, total_tokens=0,
                call_count=0, image_count=0, tts_chars=0,
                music_count=0, task_count=0,
            )
        row = session.exec(
            sql_text("""
                SELECT COALESCE(SUM(prompt_tokens), 0),
                       COALESCE(SUM(completion_tokens), 0),
                       COALESCE(SUM(total_tokens), 0),
                       COALESCE(SUM(call_count), 0),
                       COALESCE(SUM(image_count), 0),
                       COALESCE(SUM(tts_chars), 0),
                       COALESCE(SUM(music_count), 0),
                       COUNT(DISTINCT task_id)
                FROM public.api_usage
                WHERE task_id = ANY(:task_ids)
            """).bindparams(task_ids=task_ids)
        ).first()
    return UsageSummaryOut(
        prompt_tokens=int(row[0]), completion_tokens=int(row[1]),
        total_tokens=int(row[2]), call_count=int(row[3]),
        image_count=int(row[4]), tts_chars=int(row[5]),
        music_count=int(row[6]), task_count=int(row[7]),
    )


@router.get("/usage/mine/models")
def my_usage_models(user: PlatformUser = Depends(get_current_user)):
    """当前用户用量涉及的 model 去重列表(账单详情模型下拉)。"""
    with Session(engine) as session:
        task_ids = _user_task_ids(session, user.main_site_uid)
        if not task_ids:
            return []
        rows = session.exec(
            sql_text("""
                SELECT DISTINCT model FROM public.api_usage
                WHERE task_id = ANY(:task_ids) AND model IS NOT NULL AND model <> ''
                ORDER BY model
            """).bindparams(task_ids=task_ids)
        ).all()
    return [r[0] for r in rows]


class UsageDetailTotals(BaseModel):
    total_tokens: int
    tts_chars: int
    image_count: int
    music_count: int


class UsageDetailPageOut(BaseModel):
    total: int
    page: int
    page_size: int
    items: list
    # 当前筛选条件下的全量合计(非本页合计),供明细底部展示
    totals: UsageDetailTotals


@router.get("/usage/mine/detail", response_model=UsageDetailPageOut)
def my_usage_detail(
    api_type: Optional[str] = None,          # llm/image/tts/music,精确匹配
    model: Optional[str] = None,             # 精确匹配
    start_time: Optional[datetime] = None,   # created_at 范围
    end_time: Optional[datetime] = None,
    page: int = 1,
    page_size: int = 20,
    user: PlatformUser = Depends(get_current_user),
):
    """当前用户全部作品的用量明细(账单详情页),按 api_type/model/日期筛选。"""
    page = max(page, 1)
    page_size = min(max(page_size, 1), 200)
    offset = (page - 1) * page_size

    with Session(engine) as session:
        task_ids = _user_task_ids(session, user.main_site_uid)
        if not task_ids:
            return UsageDetailPageOut(
                total=0, page=page, page_size=page_size, items=[],
                totals=UsageDetailTotals(
                    total_tokens=0, tts_chars=0, image_count=0, music_count=0
                ),
            )

        where = ["u.task_id = ANY(:task_ids)"]
        params: dict = {"task_ids": task_ids}
        if api_type:
            where.append("u.api_type = :api_type")
            params["api_type"] = api_type
        if model:
            where.append("u.model = :model")
            params["model"] = model
        if start_time:
            where.append("u.created_at >= :start_time")
            params["start_time"] = start_time
        if end_time:
            where.append("u.created_at <= :end_time")
            params["end_time"] = end_time
        where_sql = " AND ".join(where)

        agg = session.exec(
            sql_text(f"""
                SELECT count(*),
                       COALESCE(SUM(u.total_tokens), 0),
                       COALESCE(SUM(u.tts_chars), 0),
                       COALESCE(SUM(u.image_count), 0),
                       COALESCE(SUM(u.music_count), 0)
                FROM public.api_usage u WHERE {where_sql}
            """).bindparams(**params)
        ).first()
        total = agg[0]
        totals = UsageDetailTotals(
            total_tokens=int(agg[1]), tts_chars=int(agg[2]),
            image_count=int(agg[3]), music_count=int(agg[4]),
        )

        rows = session.exec(
            sql_text(f"""
                SELECT u.task_id, t.project_name, u.service, u.api_type, u.model,
                       u.prompt_tokens, u.completion_tokens, u.total_tokens,
                       u.image_count, u.tts_chars, u.music_count, u.call_count,
                       u.created_at
                FROM public.api_usage u
                LEFT JOIN public.novel_tasks t ON t.task_id = u.task_id
                WHERE {where_sql}
                ORDER BY u.created_at DESC
                LIMIT :limit OFFSET :offset
            """).bindparams(**params, limit=page_size, offset=offset)
        ).all()

    columns = [
        "task_id", "project_name", "service", "api_type", "model",
        "prompt_tokens", "completion_tokens", "total_tokens",
        "image_count", "tts_chars", "music_count", "call_count",
        "created_at",
    ]
    items = []
    for row in rows:
        item = dict(zip(columns, row))
        if item["created_at"] is not None:
            item["created_at"] = item["created_at"].isoformat()
        items.append(item)

    return UsageDetailPageOut(
        total=total, page=page, page_size=page_size, items=items, totals=totals
    )
