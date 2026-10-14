"""主站 access_token 的取用入口:解密 + 过期自动 refresh。

所有需要拿用户主站 token 调主站的地方统一走
get_valid_access_token,不要直接读 OauthToken.access_token(库里是密文)。

OauthToken 对应创作引擎独立表 creation_engine_oauth_token；不能与中台共表，
否则不同 TOKEN_ENCRYPT_KEY 会互相覆盖密文。
"""
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlmodel import Session, select

from common.models import OauthToken, PlatformUser
from auth import mainsite_client
from auth.mainsite_client import MainSiteError
from auth.crypto import encrypt_token, decrypt_token, TokenCryptoError

# 过期判断余量:到期前 60s 内也当过期,避免拿着临期 token 调主站失败
_EXPIRY_MARGIN = timedelta(seconds=60)


def get_valid_access_token(session: Session, user: PlatformUser) -> str:
    """返回该用户可用的主站 access_token(明文);过期则用 refresh_token 刷新并回写。

    抛 HTTPException 401: 无授权记录 / 解密失败 / 刷新失败 → 前端引导重新登录。
    """
    oauth = session.exec(
        select(OauthToken).where(OauthToken.user_id == user.id)
    ).first()
    if oauth is None:
        raise HTTPException(401, "该用户没有主站授权令牌,请重新登录")

    try:
        access_token = decrypt_token(oauth.access_token)
        refresh_token = decrypt_token(oauth.refresh_token)
    except TokenCryptoError:
        raise HTTPException(401, "主站授权令牌已失效(密钥变更),请重新登录")

    # 无过期时间视为长期有效(主站没回 expires_in 的情况)
    if oauth.expires_at is None or oauth.expires_at - _EXPIRY_MARGIN > datetime.now():
        return access_token

    if not refresh_token:
        raise HTTPException(401, "主站授权已过期且无 refresh_token,请重新登录")

    try:
        token_data = mainsite_client.refresh_access_token(refresh_token)
    except MainSiteError:
        raise HTTPException(401, "主站授权刷新失败,请重新登录")

    new_access = token_data["access_token"]
    oauth.access_token = encrypt_token(new_access)
    # 主站可能轮换 refresh_token,没回就沿用旧的
    new_refresh = token_data.get("refresh_token") or refresh_token
    oauth.refresh_token = encrypt_token(new_refresh)
    expires_in = token_data.get("expires_in")
    oauth.expires_at = (
        datetime.now() + timedelta(seconds=expires_in) if expires_in else None
    )
    session.add(oauth)
    session.commit()
    return new_access
