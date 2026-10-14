"""我的项目 API:当前用户的作品列表 / 重命名 / 移入回收站。

数据源是 agent_platform.novel_tasks —— 建表和生成流程的写入都归 novel-game-factory,
这里只碰用户自己能改的那几列(作品名、软删除标记),不动任何生成状态字段。

归属口径:novel_tasks.user_id 存的是 CMS uid,对应 PlatformUser.main_site_uid
(与 my_assets.py / usage.py 一致)。所有端点都按当前登录用户过滤,
单条操作额外做一次归属校验,避免换个 task_id 就能改别人的作品。
"""
from datetime import datetime
from typing import Optional

import requests
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Session, select

from auth.deps import get_current_user
from auth.token_service import get_valid_access_token
from common.config import ASSET_BASE_URL, PLATFORM_API_URL, PLATFORM_SERVICE_TOKEN
from common.models import NovelTask, PipelineTask, PlatformUser, engine

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])

# 作品名长度上限(前端输入框同步限制,这里是最终防线)
_NAME_MAX_LEN = 20


def _cover_url(task_id: str) -> str:
    """封面 OSS key 约定: storage/outputs/{task_id}/images/backgrounds/novel_cover.png

    与中台 gateway/cms_publish.py 同一套规则(生成侧按本地 storage/outputs 目录结构上传)。
    对象可能还没生成好,此时链接 404,由前端回落占位图 —— 为了列表接口不被 OSS 拖慢,
    这里不逐条探测存在性。
    """
    if not ASSET_BASE_URL:
        return ""
    return f"{ASSET_BASE_URL}/storage/outputs/{task_id}/images/backgrounds/novel_cover.png"


def _display_name(task: NovelTask) -> str:
    """展示名优先用户改过的 project_name,回落 AI 生成名,再回落原始文件名。"""
    return task.project_name or task.generated_project_name or task.novel_filename or ""


class ProjectOut(BaseModel):
    task_id: str
    name: str
    cover: str
    update_time: Optional[str] = None
    progress: int = 0
    status: Optional[int] = None
    is_published: bool = False
    upload_type: Optional[str] = None
    info_confirm_stage: Optional[str] = None


class ProjectListOut(BaseModel):
    items: list[ProjectOut]
    total: int
    limit: int
    offset: int


def _effective_progress(task: NovelTask, pipeline_task: Optional[PipelineTask] = None) -> int:
    """合并生成侧与中台进度，避免异步回调把项目卡片从 5% 写回 0%。"""
    progress = max(
        int(task.progress_percentage or 0),
        int(pipeline_task.progress or 0) if pipeline_task else 0,
    )
    if (
        bool(task.is_published)
        or task.overall_status == 2
        or (pipeline_task and pipeline_task.status == "succeeded")
    ):
        return 100
    if task.overall_status == 1 or (pipeline_task and pipeline_task.status == "running"):
        progress = max(progress, 5)
    return max(0, min(100, progress))


def _to_out(task: NovelTask, pipeline_task: Optional[PipelineTask] = None) -> ProjectOut:
    # update_time 是可空列(历史行可能没写),回落 create_time 保证前端总有时间可显示
    updated = task.update_time or task.create_time
    return ProjectOut(
        task_id=task.task_id,
        name=_display_name(task),
        cover=_cover_url(task.task_id),
        update_time=updated.isoformat() if updated else None,
        progress=_effective_progress(task, pipeline_task),
        status=task.overall_status,
        is_published=bool(task.is_published),
        upload_type=task.upload_type,
        info_confirm_stage=task.info_confirm_stage,
    )


def _owned(session: Session, task_id: str, uid: str) -> NovelTask:
    """取当前用户名下的作品;不存在或不属于本人都按 404 处理。

    越权访问不单独回 403:那会告诉调用方「这个 task_id 确实存在,只是不是你的」,
    等于白送一个作品 id 探测接口。
    """
    task = session.exec(select(NovelTask).where(NovelTask.task_id == task_id)).first()
    if task is None or task.user_id != uid or task.is_deleted:
        raise HTTPException(404, "作品不存在")
    return task


@router.get("", response_model=ProjectListOut)
def list_projects(
    limit: int = 20,
    offset: int = 0,
    user: PlatformUser = Depends(get_current_user),
):
    """我的项目列表:按最近更新倒序分页。

    排序用 coalesce(update_time, create_time):update_time 是可空列,直接按它排会把
    历史未回填的行甩到最后(NULLS LAST),看起来像作品凭空消失了。
    """
    limit = min(max(limit, 1), 100)
    offset = max(offset, 0)
    uid = user.main_site_uid
    sort_key = func.coalesce(NovelTask.update_time, NovelTask.create_time)

    with Session(engine) as session:
        total = session.exec(
            select(func.count()).select_from(NovelTask).where(
                NovelTask.user_id == uid,
                NovelTask.is_deleted == False,  # noqa: E712
            )
        ).one()
        rows = session.exec(
            select(NovelTask)
            .where(
                NovelTask.user_id == uid,
                NovelTask.is_deleted == False,  # noqa: E712
            )
            .order_by(sort_key.desc())
            .limit(limit)
            .offset(offset)
        ).all()

        task_ids = [task.task_id for task in rows]
        pipeline_rows = session.exec(
            select(PipelineTask).where(PipelineTask.task_id.in_(task_ids))
        ).all() if task_ids else []
        pipeline_by_task_id = {task.task_id: task for task in pipeline_rows}

    return ProjectListOut(
        items=[_to_out(t, pipeline_by_task_id.get(t.task_id)) for t in rows],
        total=int(total),
        limit=limit,
        offset=offset,
    )


class ProjectRenameIn(BaseModel):
    project_name: str


@router.patch("/{task_id}", response_model=ProjectOut)
def rename_project(
    task_id: str,
    body: ProjectRenameIn,
    user: PlatformUser = Depends(get_current_user),
):
    """重命名作品(1-20 字,同一用户内不允许重名)。

    project_name 与 generated_project_name 一起改:列表和详情两边取名的优先级不同,
    只改一个会出现「列表已改、详情还是旧名」。
    """
    name = (body.project_name or "").strip()
    if not name:
        raise HTTPException(400, "作品名不能为空")
    if len(name) > _NAME_MAX_LEN:
        raise HTTPException(400, "超出字数上限")

    uid = user.main_site_uid
    with Session(engine) as session:
        task = _owned(session, task_id, uid)
        # 重名只在本人未删除的作品之间判,别人的作品叫什么与这里无关
        duplicate = session.exec(
            select(NovelTask).where(
                NovelTask.user_id == uid,
                NovelTask.is_deleted == False,  # noqa: E712
                NovelTask.task_id != task_id,
                NovelTask.project_name == name,
            )
        ).first()
        if duplicate is not None:
            raise HTTPException(400, "已有该命名")

        task.project_name = name
        task.generated_project_name = name
        task.update_time = datetime.now()
        session.add(task)
        session.commit()
        session.refresh(task)
        return _to_out(task)


# ── 发布(单个 / 批量) ──
#
# 都转给中台的 CMS 发布端点,payload 组装(章节结构、封面、游戏链接)在中台那一份,
# 不在这里重写一套 —— 两套字段集合一旦走偏,CMS 会以参数校验失败(code=3)告终。
#
# CMS token 由本服务解密后带过去:用户的 OAuth token 是用创作引擎自己的
# TOKEN_ENCRYPT_KEY 加密的,中台的密钥解不开(两边 secret 里就是不同的值)。

_PUBLISH_TIMEOUT_SEC = 180
_BATCH_PUBLISH_TIMEOUT_SEC = 300
# 与中台 cms_publish._MAX_BATCH_ITEMS 保持一致
_MAX_BATCH_ITEMS = 50


def _publish_succeeded(data: dict) -> bool:
    """兼容中台/CMS 各版本的成功响应。"""
    return bool(
        data.get("ok") is True
        or data.get("success") is True
        or data.get("code") in (0, 1, 200, "0", "1", "200")
    )


class ChapterPriceIn(BaseModel):
    """章节定价。number 是章序,免费章的 coins 一律按 0 提交。"""
    number: int
    is_free: bool = False
    coins: int = 0


class PublishIn(BaseModel):
    name: str = ""
    description: str = ""
    class_id: int = 0
    tags: list[int] = []
    cover: Optional[str] = None
    site_id: int = 0
    is_recommend: Optional[bool] = None
    chapters: Optional[list[ChapterPriceIn]] = None


class BatchPublishItemIn(PublishIn):
    """批量发布的单项。字段与中台 cms_publish.BatchPublishItemIn 对齐 ——
    批量走 CMS 原生 batch_set,不是循环单发布,per-item 字段必须完整送到。
    """
    task_id: str


class BatchPublishIn(BaseModel):
    items: list[BatchPublishItemIn]


def _platform_headers() -> dict:
    if not PLATFORM_SERVICE_TOKEN:
        raise HTTPException(503, "未配置 PLATFORM_SERVICE_TOKEN,无法调用中台发布接口")
    return {"X-Service-Token": PLATFORM_SERVICE_TOKEN}


def _cms_token_of(user: PlatformUser) -> str:
    with Session(engine) as session:
        return get_valid_access_token(session, user)


def _post_platform(path: str, payload: dict, timeout: int) -> dict:
    try:
        resp = requests.post(
            f"{PLATFORM_API_URL}{path}",
            json=payload,
            headers=_platform_headers(),
            timeout=timeout,
        )
    except requests.RequestException as e:
        raise HTTPException(502, f"中台不可达: {e}")
    if resp.status_code >= 400:
        detail = resp.text[:500]
        try:
            body = resp.json()
            if isinstance(body, dict):
                detail = str(body.get("detail") or body)
        except ValueError:
            pass
        raise HTTPException(resp.status_code, detail)
    try:
        return resp.json()
    except ValueError:
        raise HTTPException(502, "中台返回了非 JSON 响应")


@router.post("/{task_id}/publish")
def publish_project(
    task_id: str,
    body: PublishIn,
    user: PlatformUser = Depends(get_current_user),
):
    """发布单个作品到 CMS(复用中台 /admin/tasks/{task_id}/publish-cms)。"""
    with Session(engine) as session:
        task = _owned(session, task_id, user.main_site_uid)
        if bool(task.is_published):
            raise HTTPException(400, "作品已发布，请勿重复发布")
        default_name = _display_name(task)

    payload = body.model_dump()
    payload["name"] = body.name or default_name
    payload["cms_token"] = _cms_token_of(user)
    data = _post_platform(
        f"/api/v1/admin/tasks/{task_id}/publish-cms", payload, _PUBLISH_TIMEOUT_SEC,
    )
    if not _publish_succeeded(data):
        raise HTTPException(502, f"CMS 发布失败: {data.get('msg') or data.get('code')}")
    return {"ok": True, "task_id": task_id, "msg": data.get("msg", "")}


@router.post("/publish-batch")
def publish_projects_batch(
    body: BatchPublishIn,
    user: PlatformUser = Depends(get_current_user),
):
    """批量发布(复用中台 /admin/tasks/publish-cms-batch → CMS /business/game/batch_set)。

    每个 task_id 都先做归属校验:批量入口不能成为「传别人的 id 就能发布」的口子。
    """
    incoming = [it for it in body.items if it.task_id and it.task_id.strip()]
    if not incoming:
        raise HTTPException(400, "请至少选择 1 个作品")
    if len(incoming) > _MAX_BATCH_ITEMS:
        raise HTTPException(400, f"单次最多发布 {_MAX_BATCH_ITEMS} 个作品,请分批操作")

    uid = user.main_site_uid
    items = []
    seen: set[str] = set()
    with Session(engine) as session:
        for entry in incoming:
            task_id = entry.task_id.strip()
            if task_id in seen:
                continue
            seen.add(task_id)
            task = _owned(session, task_id, uid)
            if bool(task.is_published):
                raise HTTPException(400, f"作品已发布，请取消勾选：{_display_name(task)}")
            payload = entry.model_dump(exclude_none=True)
            payload["task_id"] = task_id
            # 前端漏填名字时回落到作品名,CMS 侧 name 是必填
            payload["name"] = entry.name or _display_name(task)
            items.append(payload)

    return _post_platform(
        "/api/v1/admin/tasks/publish-cms-batch",
        {"items": items, "cms_token": _cms_token_of(user)},
        _BATCH_PUBLISH_TIMEOUT_SEC,
    )


@router.delete("/{task_id}")
def delete_project(task_id: str, user: PlatformUser = Depends(get_current_user)):
    """删除作品:只置 is_deleted=true 进回收站。

    不动 OSS 素材也不删数据行 —— 生成产物和用量记录都挂在这个 task_id 上,
    真删了用量统计会对不上账。彻底清理由后台任务按 is_purged 处理。
    """
    with Session(engine) as session:
        task = _owned(session, task_id, user.main_site_uid)
        task.is_deleted = True
        task.update_time = datetime.now()
        session.add(task)
        session.commit()
    return {"ok": True}
