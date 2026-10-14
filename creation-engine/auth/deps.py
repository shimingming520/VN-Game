"""FastAPI 鉴权依赖。

get_current_user          强制鉴权(/auth/me、/publish 等面向前端用户的端点)
get_current_user_optional 可选鉴权(查询类端点:带了 JWT 就解析用户,没带不打断)
get_user_or_service       强制鉴权二选一(/api/v1/tasks 创建):
                          内部服务带 X-Service-Token(novel-game-factory api_gateway),
                          前端带平台 JWT;两者都没有 → 401。
"""
import logging
import time
from collections import OrderedDict
from threading import RLock
from typing import Optional

from fastapi import Depends, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from common.config import PLATFORM_SERVICE_TOKEN
from common.models import PlatformUser, engine
from auth.jwt_utils import decode_token, TokenError

_bearer = HTTPBearer(auto_error=False)
logger = logging.getLogger(__name__)

# 创作首页一次会并发拉取多个接口。JWT 已完成签名校验，这里短缓存一次用户存在性
# 查询，避免每个接口都重复占一条数据库连接；30 秒后重新核验，不改变封禁/删号语义。
_USER_CACHE_TTL_SECONDS = 30.0
_USER_CACHE_MAX_ITEMS = 2048
_user_cache_lock = RLock()
_user_cache: OrderedDict[tuple, tuple[float, Optional[PlatformUser]]] = OrderedDict()


def _copy_user(user: Optional[PlatformUser]) -> Optional[PlatformUser]:
    if user is None:
        return None
    return PlatformUser(
        id=user.id,
        main_site_uid=user.main_site_uid,
        username=user.username,
        email=user.email,
        role=user.role,
        created_at=user.created_at,
    )


def _user_cache_key(payload: dict) -> tuple:
    try:
        user_id = int(payload["sub"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TokenError("token 缺少有效用户标识") from exc
    return (
        user_id,
        str(payload.get("uid") or ""),
        str(payload.get("username") or ""),
        str(payload.get("role") or ""),
    )


def _resolve_user(credentials: Optional[HTTPAuthorizationCredentials]) -> Optional[PlatformUser]:
    if credentials is None:
        return None
    payload = decode_token(credentials.credentials)  # TokenError 由调用方处理
    cache_key = _user_cache_key(payload)
    now = time.monotonic()

    # 锁内完成未命中的单次数据库核验，防止页面并发请求在缓存为空时一起击穿连接池。
    with _user_cache_lock:
        cached = _user_cache.get(cache_key)
        if cached and cached[0] > now:
            _user_cache.move_to_end(cache_key)
            return _copy_user(cached[1])
        if cached:
            _user_cache.pop(cache_key, None)

        try:
            with Session(engine) as session:
                user = session.exec(
                    select(PlatformUser).where(PlatformUser.id == cache_key[0])
                ).first()
                snapshot = _copy_user(user)
        except SQLAlchemyError as exc:
            # 平台库不可达时不要变成无信息的 500,否则前端只能看到「服务器内部错误」
            logger.exception("读取 platform_user 失败，连接池状态: %s", engine.pool.status())
            raise HTTPException(503, "平台用户库暂不可用，请稍后重试") from exc

        _user_cache[cache_key] = (now + _USER_CACHE_TTL_SECONDS, snapshot)
        _user_cache.move_to_end(cache_key)
        while len(_user_cache) > _USER_CACHE_MAX_ITEMS:
            _user_cache.popitem(last=False)
        return _copy_user(snapshot)


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> PlatformUser:
    if credentials is None:
        raise HTTPException(401, "缺少 Authorization: Bearer <token>")
    try:
        user = _resolve_user(credentials)
    except TokenError as e:
        raise HTTPException(401, f"token 无效或已过期: {e}")
    if user is None:
        raise HTTPException(401, "用户不存在,请重新登录")
    return user


def get_current_user_optional(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> Optional[PlatformUser]:
    try:
        return _resolve_user(credentials)
    except TokenError:
        return None


def get_user_or_service(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    x_service_token: Optional[str] = Header(default=None),
) -> Optional[PlatformUser]:
    """内部 service token 或前端 JWT,二选一强制鉴权。

    返回 PlatformUser(JWT 调用)或 None(service token 内部调用,无用户上下文)。
    """
    if x_service_token:
        if PLATFORM_SERVICE_TOKEN and x_service_token == PLATFORM_SERVICE_TOKEN:
            return None
        raise HTTPException(401, "X-Service-Token 无效")
    if credentials is None:
        raise HTTPException(401, "缺少凭证:请带 Authorization: Bearer <JWT> 或 X-Service-Token")
    try:
        user = _resolve_user(credentials)
    except TokenError as e:
        raise HTTPException(401, f"token 无效或已过期: {e}")
    if user is None:
        raise HTTPException(401, "用户不存在,请重新登录")
    return user
