"""平台自签 JWT —— 登录成功后发给前端,后续请求凭此鉴权(不再透传主站 token)。"""
from datetime import datetime, timedelta, timezone

import jwt

from common.config import PLATFORM_JWT_SECRET, JWT_EXPIRE_HOURS

_ALGORITHM = "HS256"


class TokenError(Exception):
    """JWT 无效或已过期"""


def issue_token(user) -> str:
    """按 PlatformUser 签发平台 JWT。"""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "uid": user.main_site_uid,
        "username": user.username,
        "role": user.role,
        "iat": now,
        "exp": now + timedelta(hours=JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, PLATFORM_JWT_SECRET, algorithm=_ALGORITHM)


def decode_token(token: str) -> dict:
    """校验并解出 payload;无效/过期抛 TokenError(由 deps 转 401)。"""
    try:
        return jwt.decode(token, PLATFORM_JWT_SECRET, algorithms=[_ALGORITHM])
    except jwt.PyJWTError as e:
        raise TokenError(str(e)) from e
