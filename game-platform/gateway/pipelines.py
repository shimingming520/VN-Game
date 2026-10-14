"""管线管理 API:管线 = name(即任务/用量记录里的管线名) + impl_map(每能力选一个实现)。

预置4条(默认/推荐/组A/组B,main.py seed)不可删;自定义管线可增删改。
任务创建带管线名 → orchestrator 查此表解析 impl_map(见 orchestrator/core.py)。
另提供创作模式(效率/均衡/高质量)→ 管线的映射配置,写入仅超级管理员。
"""
import json
import re
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from auth.deps import get_current_user_optional, require_admin
from common.models import (
    CreationModeConfig,
    Pipeline,
    PlatformUser,
    ServiceImpl,
    engine,
)

router = APIRouter(prefix="/api/v1/pipelines", tags=["pipelines"])

# 对外三档创作模式,顺序即展示顺序
CREATION_MODES = [
    ("efficiency", "效率"),
    ("balanced", "均衡"),
    ("quality", "高质量"),
]
CREATION_MODE_LABELS = dict(CREATION_MODES)


class PipelineIn(BaseModel):
    name: str
    display_name: str = ""
    impl_map: dict = {}   # {"char_designer": "group-two.char_designer.v1"} 缺省能力自动选
    description: str = ""


class PipelineUpdateIn(BaseModel):
    display_name: Optional[str] = None
    impl_map: Optional[dict] = None
    description: Optional[str] = None


class PipelineOut(BaseModel):
    name: str
    display_name: str
    impl_map: dict
    description: str
    is_preset: bool
    created_at: datetime


class CreationModeOut(BaseModel):
    mode: str
    label: str
    pipeline_name: str
    pipeline_exists: bool
    updated_at: datetime
    updated_by: str


class CreationModeUpdateIn(BaseModel):
    pipeline_name: str


_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


def _validate_name(name: str):
    """管线名会进 URL,也会写进 novel_tasks.group_name / api_usage.group_name,
    含空格或中文会让 URL 和用量归账都出问题,建时就挡住。"""
    if not _NAME_RE.match(name):
        raise HTTPException(
            400, f"管线名只能用字母/数字/点/下划线/连字符,且以字母或数字开头: {name}"
        )


def _validate_impl_map(session: Session, impl_map: dict):
    """校验每个 capability 指向的实现真实存在且 capability 对得上。

    调度时指定实现不可用会直接让任务失败(不切备用),所以配置阶段就得挡住写错的 impl_id。
    实现的启用状态与心跳是会变的运行时状态,这里不校验。
    """
    for capability, impl_id in (impl_map or {}).items():
        if not impl_id:
            continue
        impl = session.exec(
            select(ServiceImpl).where(ServiceImpl.impl_id == impl_id)
        ).first()
        if impl is None:
            raise HTTPException(400, f"{capability}: 实现未注册: {impl_id}")
        if impl.capability != capability:
            raise HTTPException(
                400,
                f"{capability}: 实现 {impl_id} 属于 {impl.capability},不能用在此能力上",
            )


def _to_out(p: Pipeline) -> PipelineOut:
    return PipelineOut(
        name=p.name, display_name=p.display_name,
        impl_map=json.loads(p.impl_map_json or "{}"),
        description=p.description, is_preset=p.is_preset,
        created_at=p.created_at,
    )


@router.get("", response_model=List[PipelineOut])
def list_pipelines():
    with Session(engine) as session:
        rows = session.exec(select(Pipeline).order_by(Pipeline.id)).all()
        return [_to_out(p) for p in rows]


@router.post("", response_model=PipelineOut)
def create_pipeline(body: PipelineIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "管线名不能为空")
    _validate_name(name)
    with Session(engine) as session:
        if session.exec(select(Pipeline).where(Pipeline.name == name)).first():
            raise HTTPException(400, f"管线名已存在: {name}")
        _validate_impl_map(session, body.impl_map)
        pipeline = Pipeline(
            name=name, display_name=body.display_name or name,
            impl_map_json=json.dumps(body.impl_map, ensure_ascii=False),
            description=body.description,
        )
        session.add(pipeline)
        session.commit()
        session.refresh(pipeline)
        return _to_out(pipeline)


# creation-modes 两条必须声明在 /{name} 之前,否则会被 {name} 匹配掉
@router.get("/creation-modes", response_model=List[CreationModeOut])
def list_creation_modes():
    with Session(engine) as session:
        rows = {c.mode: c for c in session.exec(select(CreationModeConfig)).all()}
        names = {p.name for p in session.exec(select(Pipeline)).all()}
        out = []
        for mode, label in CREATION_MODES:
            cfg = rows.get(mode)
            pipeline_name = cfg.pipeline_name if cfg else ""
            out.append(CreationModeOut(
                mode=mode, label=label, pipeline_name=pipeline_name,
                pipeline_exists=pipeline_name in names,
                updated_at=cfg.updated_at if cfg else datetime.now(),
                updated_by=cfg.updated_by if cfg else "",
            ))
        return out


@router.put("/creation-modes/{mode}", response_model=CreationModeOut)
def update_creation_mode(
    mode: str,
    body: CreationModeUpdateIn,
    user: PlatformUser = Depends(require_admin),
):
    if mode not in CREATION_MODE_LABELS:
        raise HTTPException(404, f"创作模式不存在: {mode}")
    pipeline_name = body.pipeline_name.strip()
    with Session(engine) as session:
        if not session.exec(select(Pipeline).where(Pipeline.name == pipeline_name)).first():
            raise HTTPException(400, f"管线不存在: {pipeline_name}")
        cfg = session.exec(
            select(CreationModeConfig).where(CreationModeConfig.mode == mode)
        ).first()
        if cfg is None:
            cfg = CreationModeConfig(mode=mode, pipeline_name=pipeline_name)
        cfg.pipeline_name = pipeline_name
        cfg.updated_at = datetime.now()
        cfg.updated_by = user.username
        session.add(cfg)
        session.commit()
        session.refresh(cfg)
        return CreationModeOut(
            mode=cfg.mode, label=CREATION_MODE_LABELS[cfg.mode],
            pipeline_name=cfg.pipeline_name, pipeline_exists=True,
            updated_at=cfg.updated_at, updated_by=cfg.updated_by,
        )


@router.patch("/{name}", response_model=PipelineOut)
def update_pipeline(
    name: str,
    body: PipelineUpdateIn,
    user: Optional[PlatformUser] = Depends(get_current_user_optional),
):
    with Session(engine) as session:
        pipeline = session.exec(select(Pipeline).where(Pipeline.name == name)).first()
        if pipeline is None:
            raise HTTPException(404, f"管线不存在: {name}")
        # 预置管线的内容编辑(名称/实现组合/描述)仅超级管理员可改
        edits_content = (
            body.display_name is not None or body.impl_map is not None
            or body.description is not None
        )
        if pipeline.is_preset and edits_content and (user is None or user.role != "admin"):
            raise HTTPException(403, f"预置管线仅超级管理员可编辑: {name}")
        if body.display_name is not None:
            pipeline.display_name = body.display_name
        if body.impl_map is not None:
            _validate_impl_map(session, body.impl_map)
            pipeline.impl_map_json = json.dumps(body.impl_map, ensure_ascii=False)
        if body.description is not None:
            pipeline.description = body.description
        session.add(pipeline)
        session.commit()
        session.refresh(pipeline)
        return _to_out(pipeline)


@router.delete("/{name}")
def delete_pipeline(name: str):
    with Session(engine) as session:
        pipeline = session.exec(select(Pipeline).where(Pipeline.name == name)).first()
        if pipeline is None:
            raise HTTPException(404, f"管线不存在: {name}")
        if pipeline.is_preset:
            raise HTTPException(400, f"预置管线不可删除: {name}")
        ref = session.exec(
            select(CreationModeConfig).where(CreationModeConfig.pipeline_name == name)
        ).first()
        if ref is not None:
            label = CREATION_MODE_LABELS.get(ref.mode, ref.mode)
            raise HTTPException(400, f"管线正被「{label}」创作模式使用,请先改该模式的管线")
        session.delete(pipeline)
        session.commit()
        return {"ok": True}
