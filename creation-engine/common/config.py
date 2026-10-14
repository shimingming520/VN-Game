"""创作引擎配置 - 全部走环境变量"""
import os
from urllib.parse import quote_plus

# 平台库(与中台共用 agent_platform)
_DB_HOST = os.getenv("PLATFORM_DB_HOST", "172.16.50.17")
_DB_PORT = os.getenv("PLATFORM_DB_PORT", "5432")
_DB_NAME = os.getenv("PLATFORM_DB_NAME", "agent_platform")
_DB_USER = os.getenv("PLATFORM_DB_USER", "test")
_DB_PASSWORD = os.getenv("PLATFORM_DB_PASSWORD", "change_me")

PLATFORM_DATABASE_URL = os.getenv(
    "PLATFORM_DATABASE_URL",
    f"postgresql://{_DB_USER}:{quote_plus(_DB_PASSWORD)}@{_DB_HOST}:{_DB_PORT}/{_DB_NAME}?client_encoding=utf8",
)

# ── CMS OAuth2 对接 ──
MAINSITE_CLIENT_ID = os.getenv("MAINSITE_CLIENT_ID", "game-platform")
MAINSITE_CLIENT_SECRET = os.getenv("MAINSITE_CLIENT_SECRET", "")
MAINSITE_MOCK = os.getenv("MAINSITE_MOCK", "1") == "1"

# OAuth2 回调地址(创作引擎域名)
OAUTH_REDIRECT_URI = os.getenv("OAUTH_REDIRECT_URI", "https://chilang.farmmx.com/auth/callback")

# 平台自签 JWT
PLATFORM_JWT_SECRET = os.getenv("PLATFORM_JWT_SECRET", "dev-jwt-secret-change-me")
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "168"))

# 内部服务调用令牌(为空则不接受 service token 调用;也用于向中台/创作服务发起内部调用)
PLATFORM_SERVICE_TOKEN = os.getenv("PLATFORM_SERVICE_TOKEN", "")

# ── 上游服务地址 ──
# 中台(game-platform)API:创作代理的目标,浏览器 → 创作引擎 → 中台 /api/v1/creation
PLATFORM_API_URL = os.getenv("PLATFORM_API_URL", "http://localhost:8000").rstrip("/")

# novel-game-factory api_gateway:创作服务本体。创作引擎默认不直连(经中台代理),
# 这里保留配置用于排障与后续需要绕过中台的内部调用。
NOVEL_API_URL = os.getenv("NOVEL_API_URL", "http://localhost:8100").rstrip("/")

# 游戏站点根地址(game_builder 产物的访问入口,拼作品预览/播放链接用)
GAME_BASE_URL = os.getenv("GAME_BASE_URL", "https://testgame.farmmx.com").rstrip("/")

# oauth_token 落库加密密钥(Fernet key,base64 44字符;为空则明文存)
TOKEN_ENCRYPT_KEY = os.getenv("TOKEN_ENCRYPT_KEY", "")

# ── 本地素材库 ──
# 保留旧变量名以兼容导入方，但本地模式下不会初始化或访问 OSS。
OSS_ENDPOINT = ""
ASSET_OSS_BUCKET = ""
OSS_ACCESS_KEY = os.getenv("OSS_ACCESS_KEY", "")
OSS_SECRET_KEY = os.getenv("OSS_SECRET_KEY", "")

# 素材统一由本地 API 路由提供，避免生成或保存远端 URL。
CDN_BASE_URL = ""
ASSET_BASE_URL = ""

# CMS 管理后台 API(OAuth2 授权用)
CMS_API_BASE = os.getenv("CMS_API_BASE", "https://testapi.farmmx.com/admin").rstrip("/")

# ── 素材名翻译(阿里云百炼 DashScope,OpenAI 兼容模式) ──
# 为空则不调用 LLM,前端回落本地词典
DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE_URL = os.getenv(
    "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
).rstrip("/")
TRANSLATE_MODEL = os.getenv("TRANSLATE_MODEL", "qwen3.6-plus")
