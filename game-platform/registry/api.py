"""注册中心 API(manifest_portal 升级版):能力→实现的活地图 + 热插拔开关"""
import json
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from common.config import HEARTBEAT_TIMEOUT_SEC
from common.models import ServiceImpl, ServiceRegistry, engine, heartbeat_engine
from common.mq import declare_impl_queue, unbind_impl_queue

router = APIRouter(prefix="/api", tags=["registry"])


class ImplRegister(BaseModel):
    group_name: str
    capability: str
    impl_id: str
    queue: str
    manifest: dict = {}
    endpoint: Optional[str] = None
    enabled: bool = True


class ImplOut(BaseModel):
    capability: str
    group_name: str
    impl_id: str
    queue: str
    enabled: bool
    health_status: str
    last_heartbeat: Optional[datetime] = None


class ImplPatch(BaseModel):
    enabled: Optional[bool] = None
    queue: Optional[str] = None
    manifest: Optional[dict] = None


@router.post("/impls", response_model=ImplOut)
def register_impl(body: ImplRegister):
    """【1】注册:登记实现 + 建队列/绑定。重复注册 = upsert(幂等)"""
    with Session(engine) as session:
        impl = session.exec(
            select(ServiceImpl).where(ServiceImpl.impl_id == body.impl_id)
        ).first()
        if impl is None:
            impl = ServiceImpl(
                capability=body.capability, group_name=body.group_name,
                impl_id=body.impl_id, queue=body.queue,
            )
        impl.queue = body.queue
        impl.manifest_json = json.dumps(body.manifest, ensure_ascii=False)
        impl.endpoint = body.endpoint
        impl.enabled = body.enabled
        impl.health_status = "healthy"
        impl.last_heartbeat = datetime.now()
        session.add(impl)

        # 能力目录自动补录(正式版应由平台方预先定义并带输出契约)
        if not session.exec(
            select(ServiceRegistry).where(ServiceRegistry.capability == body.capability)
        ).first():
            session.add(ServiceRegistry(capability=body.capability))
        session.commit()
        session.refresh(impl)

    if body.enabled:
        declare_impl_queue(body.queue, body.capability, body.impl_id)
    return _to_out(impl)


@router.get("/impls", response_model=List[ImplOut])
def list_impls(capability: Optional[str] = None, enabled_only: bool = False):
    """【2】选实现:前端"该环节有哪些组可选"的数据源"""
    with Session(engine) as session:
        stmt = select(ServiceImpl)
        if capability:
            stmt = stmt.where(ServiceImpl.capability == capability)
        if enabled_only:
            stmt = stmt.where(ServiceImpl.enabled == True)  # noqa: E712
        return [_to_out(i) for i in session.exec(stmt).all()]


@router.patch("/impls/{impl_id}", response_model=ImplOut)
def patch_impl(impl_id: str, body: ImplPatch):
    """【4】热插拔:enabled 开关,改绑定不改代码不重启"""
    mq_action = None
    with Session(engine) as session:
        impl = session.exec(
            select(ServiceImpl).where(ServiceImpl.impl_id == impl_id)
        ).first()
        if impl is None:
            raise HTTPException(404, f"impl not found: {impl_id}")
        if body.queue is not None:
            impl.queue = body.queue
        if body.manifest is not None:
            impl.manifest_json = json.dumps(body.manifest, ensure_ascii=False)
        if body.enabled is not None and body.enabled != impl.enabled:
            impl.enabled = body.enabled
            mq_action = "bind" if body.enabled else "unbind"
        session.add(impl)
        session.commit()
        session.refresh(impl)
        result = _to_out(impl)
        queue = impl.queue
        capability = impl.capability

    # RabbitMQ 是外部网络操作，必须在 Session 关闭、连接归还池后执行。
    if mq_action == "bind":
        declare_impl_queue(queue, capability, impl_id)
    elif mq_action == "unbind":
        unbind_impl_queue(queue, capability, impl_id)
    return result


@router.post("/impls/{impl_id}/heartbeat")
def heartbeat(impl_id: str):
    # 单条原子 UPDATE，避免旧实现的 SELECT + ORM UPDATE 长时间占用主业务连接池。
    # 心跳使用独立小池；池忙或 DB 短暂变慢时快速失败，让 adapter 下周期重试。
    try:
        with heartbeat_engine.begin() as conn:
            result = conn.execute(
                text(
                    "UPDATE service_impl "
                    "SET last_heartbeat = :now, health_status = 'healthy' "
                    "WHERE impl_id = :impl_id"
                ),
                {"now": datetime.now(), "impl_id": impl_id},
            )
    except SQLAlchemyError as exc:
        raise HTTPException(503, "心跳数据库繁忙，请稍后重试") from exc
    if not result.rowcount:
        raise HTTPException(404, f"impl not found: {impl_id}")
    return {"ok": True}


class ImplUnavailable(Exception):
    """管线指定的实现不可用。不静默切备用,让任务显式失败。"""


def pick_impl(session: Session, capability: str,
              preferred_impl_id: Optional[str] = None) -> Optional[ServiceImpl]:
    """【3】找队列:调度器选实现。

    管线指定了实现就必须用它,不可用直接抛 ImplUnavailable —— 悄悄切到别的组会让
    「跑的是哪条管线」和用量归属都对不上账。只有管线没指定(自动选)时才挑健康实现。
    """
    if preferred_impl_id:
        impl = session.exec(
            select(ServiceImpl).where(ServiceImpl.impl_id == preferred_impl_id)
        ).first()
        if impl is None:
            raise ImplUnavailable(f"{capability}: 实现 {preferred_impl_id} 未注册")
        if impl.capability != capability:
            raise ImplUnavailable(
                f"{capability}: 实现 {preferred_impl_id} 属于 {impl.capability}"
            )
        if not impl.enabled:
            raise ImplUnavailable(f"{capability}: 实现 {preferred_impl_id} 已禁用")
        if not _is_alive(impl):
            raise ImplUnavailable(
                f"{capability}: 实现 {preferred_impl_id} 心跳超时"
                f"(last_heartbeat={impl.last_heartbeat})"
            )
        return impl
    for impl in session.exec(
        select(ServiceImpl)
        .where(ServiceImpl.capability == capability)
        .where(ServiceImpl.enabled == True)  # noqa: E712
    ).all():
        if _is_alive(impl):
            return impl
    return None


def _is_alive(impl: ServiceImpl) -> bool:
    if impl.last_heartbeat is None:
        return False
    return (datetime.now() - impl.last_heartbeat).total_seconds() < HEARTBEAT_TIMEOUT_SEC


def _to_out(impl: ServiceImpl) -> ImplOut:
    return ImplOut(
        capability=impl.capability, group_name=impl.group_name,
        impl_id=impl.impl_id, queue=impl.queue, enabled=impl.enabled,
        health_status=impl.health_status, last_heartbeat=impl.last_heartbeat,
    )
