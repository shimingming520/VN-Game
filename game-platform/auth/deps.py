"""FastAPI 鉴权依赖。

get_current_user          强制鉴权(/auth/me、/publish 等面向前端用户的端点)
get_current_user_optional 可选鉴权(查询类端点:带了 JWT 就解析用户,没带不打断)
get_user_or_service       强制鉴权二选一(/api/v1/tasks 创建):
                          内部服务带 X-Service-Token(novel-game-factory api_gateway),
                          前端带平台 JWT;两者都没有 → 401。
get_task_owner            强制鉴权二选一 + 确定任务归属(创作代理):
                          可信内部服务带 service token 并显式声明归属用户,
                          前端带平台 JWT 则归属取自 JWT;两者都没有 → 401。
"""
import logging
import time
from collections import OrderedDict
from threading import RLock
from typing import NamedTuple, Optional
from urllib.parse import unquote

from fastapi import Depends, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from common.config import (
    PLATFORM_AUTH_DISABLED,
    PLATFORM_LOCAL_USER_NAME,
    PLATFORM_LOCAL_USER_UID,
    PLATFORM_SERVICE_TOKEN,
)
from common.models import PlatformUser, engine
from auth.jwt_utils import decode_token, TokenError

_bearer = HTTPBearer(auto_error=False)
logger = logging.getLogger(__name__)

# 一个页面会同时请求 games/options/modes/projects/usage 等多个接口；旧实现每个请求
# 都重复查一次 platform_user，等于在真正的业务 SQL 之前先把数据库并发放大一倍。
# JWT 已经完成签名校验，因此这里只需短时间缓存“该用户仍存在”的数据库快照。
# 30 秒后必定重新核验，删除用户或修改权限不会被长期缓存。
_USER_CACHE_TTL_SECONDS = 30.0
_USER_CACHE_MAX_ITEMS = 2048
_user_cache_lock = RLock()
_user_cache: OrderedDict[tuple, tuple[float, Optional[PlatformUser]]] = OrderedDict()
_LOCAL_USER = PlatformUser(
    id=0,
    main_site_uid=PLATFORM_LOCAL_USER_UID,
    username=PLATFORM_LOCAL_USER_NAME,
    email=None,
    role="admin",
)


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


class TaskOwner(NamedTuple):
    """任务归属人。user_id 一律是 CMS 主站 uid,不是平台库自增主键。"""
    user_id: str
    user_name: str


def _resolve_user(credentials: Optional[HTTPAuthorizationCredentials]) -> Optional[PlatformUser]:
    if credentials is None:
        return None
    payload = decode_token(credentials.credentials)  # TokenError 由调用方处理
    cache_key = _user_cache_key(payload)
    now = time.monotonic()

    # 未命中时也在锁内查询：数据库变慢时，同一批并发鉴权只允许一条查询进入连接池，
    # 其余线程等待这一个结果，避免缓存击穿再次把池打满。
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
    if PLATFORM_AUTH_DISABLED:
        return _copy_user(_LOCAL_USER)
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
    if PLATFORM_AUTH_DISABLED:
        return _copy_user(_LOCAL_USER)
    try:
        return _resolve_user(credentials)
    except TokenError:
        return None


def require_admin(user: PlatformUser = Depends(get_current_user)) -> PlatformUser:
    """强制超级管理员(role=admin),用于预置管线编辑等敏感操作。"""
    if user.role != "admin":
        raise HTTPException(403, "需要超级管理员权限")
    return user


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
    if PLATFORM_AUTH_DISABLED:
        return _copy_user(_LOCAL_USER)
    if credentials is None:
        raise HTTPException(401, "缺少凭证:请带 Authorization: Bearer <JWT> 或 X-Service-Token")
    try:
        user = _resolve_user(credentials)
    except TokenError as e:
        raise HTTPException(401, f"token 无效或已过期: {e}")
    if user is None:
        raise HTTPException(401, "用户不存在,请重新登录")
    return user


def get_task_owner(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    x_service_token: Optional[str] = Header(default=None),
    x_platform_user_uid: Optional[str] = Header(default=None),
    x_platform_user_name: Optional[str] = Header(default=None),
) -> TaskOwner:
    """确定创作任务归属人:可信内部服务 + 显式归属,或前端平台 JWT。

    为什么要这个模式:creation-engine 有自己的一套用户体系和 JWT 密钥,它的 token
    中台解不开(而且也不该解 —— 密钥同值等于让引擎签发的 token 直接拿到中台权限)。
    所以引擎作为可信内部服务出示 service token,并把它自己校验过的用户 uid
    显式声明出来,由中台按这个 uid 归属任务。

    归属头只在 service token 校验通过后才被采信:不带 service token 时(浏览器直连)
    这两个头一律忽略,不然任何人加个头就能冒用他人身份。
    """
    if x_service_token:
        if not (PLATFORM_SERVICE_TOKEN and x_service_token == PLATFORM_SERVICE_TOKEN):
            raise HTTPException(401, "X-Service-Token 无效")
        uid = (x_platform_user_uid or "").strip()
        if not uid:
            # service token 本身不含用户信息,归属缺失就只能拒绝 ——
            # 落成匿名任务会让素材库、用量、CMS 这些按 uid 认人的地方全都对不上
            raise HTTPException(400, "service token 调用必须带 X-Platform-User-Uid 声明任务归属")
        encoded_name = (x_platform_user_name or "").strip()
        user_name = unquote(encoded_name) if encoded_name else uid
        return TaskOwner(user_id=uid, user_name=user_name)

    if PLATFORM_AUTH_DISABLED:
        return TaskOwner(
            user_id=PLATFORM_LOCAL_USER_UID,
            user_name=PLATFORM_LOCAL_USER_NAME,
        )

    user = get_current_user(credentials)
    # 任务归属用主站 uid,不是平台库自增主键 —— 自增 id 只在本库有意义,
    # novel_tasks 在平台库里跨系统被查(素材库、用量、CMS 都按主站 uid 认人),
    # 写自增 id 会让这些地方对不上人。
    if not user.main_site_uid:
        raise HTTPException(400, "当前账号缺少主站 uid,无法归属创作任务,请重新登录")
    return TaskOwner(user_id=str(user.main_site_uid), user_name=user.username)
