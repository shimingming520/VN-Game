"""登录/用户接口(Auth/BFF)。

流程(Authorization Code + PKCE,对接 CMS admin_backend):
  前端 GET /api/v1/auth/authorize-url → 后端生成 state+verifier 暂存,调 CMS authorize
  拿登录页地址 → 前端跳转 → 管理员在 CMS 登录页授权 → 浏览器带 code&state 回前端
  /auth/callback → 前端 POST /api/v1/auth/login {code, state} → 校验 state 取 verifier
  → 换 CMS token → 拉用户信息 → upsert platform_user + oauth_token → 签发平台 JWT。
  之后前端所有请求带平台 JWT,不再接触 CMS token。
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from common.models import PlatformUser, OauthToken, engine
from auth import mainsite_client, oauth_store
from auth.mainsite_client import MainSiteError
from auth.crypto import encrypt_token
from auth.jwt_utils import issue_token
from auth.deps import get_current_user

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginIn(BaseModel):
    code: str
    state: str


class AuthorizeUrlOut(BaseModel):
    authorize_url: str


class UserOut(BaseModel):
    id: int
    main_site_uid: str
    username: str
    email: str | None = None
    role: str


class LoginOut(BaseModel):
    token: str
    user: UserOut


def _to_user_out(user: PlatformUser) -> UserOut:
    return UserOut(
        id=user.id, main_site_uid=user.main_site_uid,
        username=user.username, email=user.email, role=user.role,
    )


@router.get("/authorize-url", response_model=AuthorizeUrlOut)
def authorize_url():
    """发起授权:生成 state+PKCE 暂存,返回 CMS OAuth2 登录页地址(前端自行跳转)。"""
    state, code_challenge = oauth_store.new_flow()
    try:
        url = mainsite_client.get_authorize_url(state, code_challenge)
    except MainSiteError as e:
        raise HTTPException(502, str(e))
    return AuthorizeUrlOut(authorize_url=url)


@router.post("/login", response_model=LoginOut)
def login(body: LoginIn):
    code_verifier = oauth_store.consume(body.state)
    if code_verifier is None:
        raise HTTPException(400, "state 无效或已过期,请重新发起登录")
    try:
        token_data = mainsite_client.exchange_code(body.code, code_verifier)
        user_info = mainsite_client.fetch_user_info(token_data["access_token"])
    except MainSiteError as e:
        raise HTTPException(502, str(e))

    with Session(engine) as session:
        user = session.exec(
            select(PlatformUser).where(PlatformUser.main_site_uid == user_info["uid"])
        ).first()
        if user is None:
            user = PlatformUser(main_site_uid=user_info["uid"])
        user.username = user_info["username"]
        user.email = user_info.get("email")
        session.add(user)
        session.commit()
        session.refresh(user)

        oauth = session.exec(
            select(OauthToken).where(OauthToken.user_id == user.id)
        ).first()
        if oauth is None:
            oauth = OauthToken(user_id=user.id, access_token="")
        oauth.access_token = encrypt_token(token_data["access_token"])
        oauth.refresh_token = encrypt_token(token_data.get("refresh_token"))
        expires_in = token_data.get("expires_in")
        oauth.expires_at = (
            datetime.now() + timedelta(seconds=expires_in) if expires_in else None
        )
        session.add(oauth)
        session.commit()

        return LoginOut(token=issue_token(user), user=_to_user_out(user))


@router.get("/me", response_model=UserOut)
def me(user: PlatformUser = Depends(get_current_user)):
    return _to_user_out(user)
