"""creation_engine_oauth_token 落库加解密(Fernet 对称加密)。

约定:
  - 密文带 "enc:" 前缀存库,便于区分存量明文(向后兼容:无前缀直接原样返回)
  - TOKEN_ENCRYPT_KEY 为空时明文透传,仅限本地 dev;生产必须配置
  - 换 key 后旧密文解不开 → 用户重新登录即可(本服务令牌会被覆盖)
"""
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from common.config import TOKEN_ENCRYPT_KEY

_ENC_PREFIX = "enc:"

_fernet: Optional[Fernet] = None
if TOKEN_ENCRYPT_KEY:
    _fernet = Fernet(TOKEN_ENCRYPT_KEY.encode())


class TokenCryptoError(Exception):
    """解密失败(key 变更/密文损坏),调用方应引导用户重新登录。"""


def encrypt_token(plain: Optional[str]) -> Optional[str]:
    if not plain or _fernet is None:
        return plain
    return _ENC_PREFIX + _fernet.encrypt(plain.encode()).decode()


def decrypt_token(stored: Optional[str]) -> Optional[str]:
    if not stored or not stored.startswith(_ENC_PREFIX):
        return stored  # 存量明文或空值
    if _fernet is None:
        raise TokenCryptoError("库里是加密 token 但未配置 TOKEN_ENCRYPT_KEY")
    try:
        return _fernet.decrypt(stored[len(_ENC_PREFIX):].encode()).decode()
    except InvalidToken as e:
        raise TokenCryptoError(f"token 解密失败(密钥可能已更换): {e}") from e
