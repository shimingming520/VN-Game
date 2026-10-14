"""CMS(主站 admin 后端) OAuth2 客户端 —— 全平台唯一跟 CMS 授权服务打交道的地方。

对接规格见 example/oauth2_authorization_flow.md(Authorization Code + PKCE):
  GET  /business/oauth2/authorize   校验参数,返回 CMS OAuth2 登录页地址
  POST /business/oauth2/token       Basic(client_id:secret) + code + code_verifier 换 token
  GET  /business/oauth2/userinfo    Bearer token 拉管理员信息

MAINSITE_MOCK=1 时返回确定性假数据,全链路(含前端回调页)可离线跑通;
凭证(client_id/secret)到手后置 0 即切真实 CMS。
注意:CMS 不签发 refresh_token,access_token 过期(86400s)后引导重新登录。
"""
import time

import requests

from common.config import (
    CMS_API_BASE, MAINSITE_CLIENT_ID, MAINSITE_CLIENT_SECRET, MAINSITE_MOCK,
    OAUTH_REDIRECT_URI,
)


class MainSiteError(Exception):
    """CMS 调用失败(网络错误/非2xx/返回格式不对)"""


def get_authorize_url(state: str, code_challenge: str) -> str:
    """发起授权:调 CMS authorize 端点,返回 CMS OAuth2 登录页地址(前端跳转用)。"""
    if MAINSITE_MOCK:
        # mock:直接指回平台自己的回调页,code 用占位值,离线走通全流程
        return f"{OAUTH_REDIRECT_URI}?code=mock-code-{int(time.time())}&state={state}"
    try:
        resp = requests.get(
            f"{CMS_API_BASE}/business/oauth2/authorize",
            params={
                "response_type": "code",
                "client_id": MAINSITE_CLIENT_ID,
                "redirect_uri": OAUTH_REDIRECT_URI,
                "scope": "game.publish userinfo.read",
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("redirect_uri"):
            raise MainSiteError(f"authorize 响应缺少 redirect_uri: {data}")
        return data["redirect_uri"]
    except requests.RequestException as e:
        raise MainSiteError(f"get_authorize_url 调 CMS 失败: {e}") from e


def exchange_code(code: str, code_verifier: str) -> dict:
    """授权码换 CMS access_token。

    返回: {"access_token": str, "refresh_token": None, "expires_in": int(秒)}
    (CMS 不签发 refresh_token,字段恒为 None,保持与 token_service 的约定结构)
    """
    if MAINSITE_MOCK:
        return {
            "access_token": f"mock-at-{code}",
            "refresh_token": None,
            "expires_in": 86400,
        }
    try:
        resp = requests.post(
            f"{CMS_API_BASE}/business/oauth2/token",
            auth=(MAINSITE_CLIENT_ID, MAINSITE_CLIENT_SECRET),
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": OAUTH_REDIRECT_URI,
                "code_verifier": code_verifier,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token"),
            "expires_in": data.get("expires_in"),
        }
    except requests.RequestException as e:
        raise MainSiteError(f"exchange_code 调 CMS 失败: {e}") from e
    except KeyError as e:
        raise MainSiteError(f"token 响应缺少字段: {e}") from e


def refresh_access_token(refresh_token: str) -> dict:
    """CMS 不支持 refresh_token;仅 mock 保留,真实分支直接报错让用户重新登录。"""
    if MAINSITE_MOCK:
        return {
            "access_token": f"mock-at-refreshed-{int(time.time())}",
            "refresh_token": refresh_token,
            "expires_in": 86400,
        }
    raise MainSiteError("CMS 不签发 refresh_token,请重新登录")


def fetch_user_info(access_token: str) -> dict:
    """用 CMS access_token 拉管理员信息。

    返回: {"uid": str, "username": str, "email": str|None}
    (CMS userinfo 回 user_id/username/nickname,无 email)
    mock 从 token 派生确定性 uid: 同一 code 重复登录 → 同一用户,可验证 upsert 逻辑。
    """
    if MAINSITE_MOCK:
        suffix = access_token[-8:]
        return {
            "uid": f"mock-uid-{suffix}",
            "username": f"mock_user_{suffix}",
            "email": f"{suffix}@mock.local",
        }
    try:
        resp = requests.get(
            f"{CMS_API_BASE}/business/oauth2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "uid": str(data["user_id"]),
            "username": data.get("nickname") or data.get("username") or "",
            "email": None,
        }
    except requests.RequestException as e:
        raise MainSiteError(f"fetch_user_info 调 CMS 失败: {e}") from e
    except KeyError as e:
        raise MainSiteError(f"userinfo 响应缺少字段: {e}") from e
