"""game-platform 入口:FastAPI(注册中心 + 网关) 。

启动时建表 + 声明 MQ 拓扑 + 种子默认管线模板。
回调消费者单独进程跑: python -m orchestrator.consumer
"""
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import (
    OperationalError,
    SQLAlchemyError,
    TimeoutError as SQLAlchemyTimeoutError,
)
from sqlmodel import Session, select

from common.models import (
    CreationModeConfig, Pipeline, PipelineTemplate, ServiceImpl, create_all_tables, engine,
)
from common.mq import declare_topology
from auth.api import router as auth_router
from gateway.api import router as gateway_router
from gateway.assets import router as assets_router
from gateway.cms_publish import router as cms_publish_router
from gateway.creation import router as creation_router
from gateway.games import router as games_router
from gateway.pipelines import CREATION_MODES
from gateway.pipelines import router as pipelines_router
from orchestrator.dag import DEFAULT_DAG, VISUAL_PARALLEL_DAG
from orchestrator.outbox import start_outbox_runtime, stop_outbox_runtime
from registry.api import router as registry_router

logger = logging.getLogger(__name__)


def seed_default_template():
    with Session(engine) as session:
        if session.exec(
            select(PipelineTemplate).where(PipelineTemplate.name == "default")
        ).first():
            return
        session.add(PipelineTemplate(
            name="default",
            dag_json=json.dumps(DEFAULT_DAG, ensure_ascii=False),
        ))
        session.commit()
        print("🌱 seeded default pipeline template")


def seed_visual_parallel_template():
    """Seed a versioned template without changing DAGs of already running tasks."""
    with Session(engine) as session:
        if session.exec(
            select(PipelineTemplate).where(
                PipelineTemplate.name == "visual-parallel-v1"
            )
        ).first():
            return
        session.add(PipelineTemplate(
            name="visual-parallel-v1",
            dag_json=json.dumps(VISUAL_PARALLEL_DAG, ensure_ascii=False),
        ))
        session.commit()
        print("🌱 seeded visual-parallel-v1 pipeline template")


def _group_impl_map(session: Session, group_name: str) -> dict:
    """按 group_name 每个 capability 取一个 impl_id(组内多版本取最新注册的)。"""
    impls = session.exec(
        select(ServiceImpl)
        .where(ServiceImpl.group_name == group_name, ServiceImpl.enabled == True)  # noqa: E712
        .order_by(ServiceImpl.created_at)
    ).all()
    return {impl.capability: impl.impl_id for impl in impls}


def seed_pipelines():
    """预置4条管线(幂等,按 name 判存在):默认/推荐先空占位(自动选择),组A/组B按注册表生成。"""
    presets = [
        ("default", "默认", lambda s: {}, "默认备选管线,各步自动选择健康实现"),
        ("recommended", "推荐", lambda s: {}, "效果最好管线,实现组合待效果对比后配置"),
        ("group-a", "组A", lambda s: _group_impl_map(s, "group-ref"), "一组(group-ref)全套实现"),
        ("group-b", "组B", lambda s: _group_impl_map(s, "group-two"), "二组(group-two)全套实现"),
    ]
    with Session(engine) as session:
        for name, display_name, impl_map_fn, description in presets:
            if session.exec(select(Pipeline).where(Pipeline.name == name)).first():
                continue
            session.add(Pipeline(
                name=name, display_name=display_name,
                impl_map_json=json.dumps(impl_map_fn(session), ensure_ascii=False),
                description=description, is_preset=True,
            ))
            print(f"🌱 seeded pipeline: {name}")
        session.commit()


def seed_creation_modes():
    """三档创作模式(幂等,按 mode 判存在),初始都指向 default 管线,由管理员后续调整。"""
    with Session(engine) as session:
        for mode, _label in CREATION_MODES:
            if session.exec(
                select(CreationModeConfig).where(CreationModeConfig.mode == mode)
            ).first():
                continue
            session.add(CreationModeConfig(mode=mode, pipeline_name="default"))
            print(f"🌱 seeded creation mode: {mode}")
        session.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_all_tables()
    declare_topology()
    seed_default_template()
    seed_visual_parallel_template()
    seed_pipelines()
    seed_creation_modes()
    start_outbox_runtime()
    try:
        yield
    finally:
        stop_outbox_runtime()


app = FastAPI(title="Game Platform 中台", version="0.1.0", lifespan=lifespan)


@app.exception_handler(SQLAlchemyTimeoutError)
async def database_pool_timeout(_request: Request, exc: SQLAlchemyTimeoutError):
    """连接池紧张时明确返回 503，并记录现场，避免无信息的 Internal Server Error。"""
    logger.error("数据库连接池等待超时: %s; pool=%s", exc, engine.pool.status())
    return JSONResponse(
        status_code=503,
        content={"detail": "数据库连接繁忙，请稍后重试"},
        headers={"Retry-After": "3"},
    )


@app.exception_handler(OperationalError)
async def database_operational_error(_request: Request, exc: OperationalError):
    """数据库断连、锁/语句超时统一按可重试故障返回，不伪装成业务 500。"""
    logger.error("数据库操作失败: %s; pool=%s", exc, engine.pool.status())
    return JSONResponse(
        status_code=503,
        content={"detail": "数据库暂时不可用，请稍后重试"},
        headers={"Retry-After": "3"},
    )


app.include_router(registry_router)
app.include_router(gateway_router)
app.include_router(auth_router)
app.include_router(cms_publish_router)
app.include_router(games_router)
app.include_router(pipelines_router)
app.include_router(assets_router)
app.include_router(creation_router)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/health/ready")
def ready():
    """业务就绪检查：连接池或数据库不可用时从 Service endpoints 摘除。"""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        logger.error("readiness 数据库检查失败: %s; pool=%s", exc, engine.pool.status())
        raise HTTPException(503, "database unavailable") from exc
    return {"status": "ready"}
