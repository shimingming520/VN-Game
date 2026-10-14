"""OAuth2 授权流程的服务端暂存:state → code_verifier,一次性消费 + TTL。

流程(Authorization Code + PKCE,平台后端扮演"第三方后端"):
  1. 前端点登录 → GET /auth/authorize-url → 这里生成 state+verifier+challenge 并暂存
  2. CMS 登录页授权完成 → 浏览器带 code&state 回前端 /auth/callback
  3. 前端 POST /auth/login {code, state} → consume(state) 取回 verifier 换 token

单副本内存暂存即可;pod 重启丢失未完成的登录流程,用户重新点登录就行。
"""
import base64
import hashlib
import secrets
import time

_TTL_SECONDS = 600
_store: dict[str, tuple[str, float]] = {}  # state -> (code_verifier, created_at)


def new_flow() -> tuple[str, str]:
    """生成并暂存一次授权流程,返回 (state, code_challenge)。"""
    _prune()
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    _store[state] = (verifier, time.time())
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return state, challenge


def consume(state: str) -> str | None:
    """原子消费 state,返回对应 code_verifier;不存在/已用过/过期返回 None。"""
    _prune()
    item = _store.pop(state, None)
    return item[0] if item else None


def _prune() -> None:
    now = time.time()
    for key in [k for k, (_, t) in _store.items() if now - t > _TTL_SECONDS]:
        _store.pop(key, None)
