"""创作引擎 API 入口"""
import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import (
    OperationalError,
    SQLAlchemyError,
    TimeoutError as SQLAlchemyTimeoutError,
)

from auth.api import router as auth_router
from gateway.games import router as games_router
from gateway.assets import router as assets_router
from gateway.my_assets import router as my_assets_router
from gateway.usage import router as usage_router
from gateway.translate import router as translate_router
from gateway.creation import router as creation_router
from gateway.projects import router as projects_router
from common.models import create_all_tables, engine
from common.schema import ensure_shared_schema

app = FastAPI(title="Creation Engine API", version="1.0.0")
logger = logging.getLogger(__name__)


@app.exception_handler(SQLAlchemyTimeoutError)
async def database_pool_timeout(_request: Request, exc: SQLAlchemyTimeoutError):
    """连接池拿不到连接时快速返回可重试的 503，并把池状态写进服务日志。"""
    logger.error("数据库连接池等待超时: %s; pool=%s", exc, engine.pool.status())
    return JSONResponse(
        status_code=503,
        content={"detail": "数据库连接繁忙，请稍后重试"},
        headers={"Retry-After": "3"},
    )


@app.exception_handler(OperationalError)
async def database_operational_error(_request: Request, exc: OperationalError):
    """数据库断连、锁/语句超时统一返回 503，让前端可以重试。"""
    logger.error("数据库操作失败: %s; pool=%s", exc, engine.pool.status())
    return JSONResponse(
        status_code=503,
        content={"detail": "数据库暂时不可用，请稍后重试"},
        headers={"Retry-After": "3"},
    )

# CORS(开发期全开,生产按需收窄)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    # 建表/补字段只是兼容旧环境的自修复，不应成为 API 启动的无限阻塞点。
    # 权威共享表由 novel-game / 中台维护；这里失败后先提供服务，readiness
    # 会继续检查数据库连通性，具体业务接口也会返回明确的 503。
    try:
        create_all_tables()
    except SQLAlchemyError:
        logger.exception("启动建表失败，已降级继续启动")
        return
    try:
        ensure_shared_schema()
    except SQLAlchemyError:
        logger.exception("共享表结构检查失败，已降级继续启动")


@app.get("/")
def root():
    return {"service": "creation-engine", "status": "running"}


@app.get("/health/ready")
def readiness():
    """readiness 必须验证平台数据库；仅检查 HTTP 根路径会把不可用 Pod 放进 Service。"""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        logger.error("readiness 数据库检查失败: %s; pool=%s", exc, engine.pool.status())
        raise HTTPException(503, "平台数据库暂不可用") from exc
    return {"status": "ready"}


app.include_router(auth_router)
app.include_router(games_router)
app.include_router(assets_router)
app.include_router(my_assets_router)
app.include_router(usage_router)
app.include_router(translate_router)
# projects 必须在 creation 之前:creation 末尾有 /api/v1/creation/{subpath:path} 通配,
# 前缀不同本不冲突,但保持「具体路由先注册」的习惯,避免以后加通配路由时踩到。
app.include_router(projects_router)
app.include_router(creation_router)
