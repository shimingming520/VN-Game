"""平台配置 - 全部走环境变量,本地默认对齐 docker-compose.dev.yml"""
import os
from urllib.parse import quote_plus

# 平台库(独立于业务库 extreme,同实例不同库)
# 优先用分段变量拼 URL,密码自动做 URL 转义(避免 secret 里手写 %40 出错);
# 也可以直接设 PLATFORM_DATABASE_URL 整串覆盖(需自行保证转义正确)。
_DB_HOST = os.getenv("PLATFORM_DB_HOST", "172.16.50.17")
_DB_PORT = os.getenv("PLATFORM_DB_PORT", "5432")
_DB_NAME = os.getenv("PLATFORM_DB_NAME", "agent_platform")
_DB_USER = os.getenv("PLATFORM_DB_USER", "test")
_DB_PASSWORD = os.getenv("PLATFORM_DB_PASSWORD", "change_me")

PLATFORM_DATABASE_URL = os.getenv(
    "PLATFORM_DATABASE_URL",
    f"postgresql://{_DB_USER}:{quote_plus(_DB_PASSWORD)}@{_DB_HOST}:{_DB_PORT}/{_DB_NAME}?client_encoding=utf8",
)

# RabbitMQ 消息总线
PLATFORM_MQ_URL = os.getenv("PLATFORM_MQ_URL", "amqp://guest:guest@localhost:5672/%2F")

# Exchange 命名(见设计文档第六章)
JOBS_EXCHANGE = "game.jobs"
CALLBACKS_EXCHANGE = "game.callbacks"

# 中台自己消费回调的队列
CALLBACK_QUEUE = "q.platform.callbacks"

# 回调失败重试不修改已存在 q.platform.callbacks 的声明参数，避免线上已有队列
# 因 x-arguments 不一致触发 PRECONDITION_FAILED。失败消息先进入独立延迟队列，
# 到期后由 RabbitMQ dead-letter 回原 callbacks exchange；耗尽后进入 durable DLQ。
CALLBACK_RETRY_EXCHANGE = "game.callbacks.retry"
CALLBACK_RETRY_QUEUE = "q.platform.callbacks.retry.v1"
CALLBACK_RETRY_ROUTING_KEY = "retry"
CALLBACK_RETRY_DELAY_MS = int(os.getenv("CALLBACK_RETRY_DELAY_MS", "5000"))
CALLBACK_MAX_RETRIES = int(os.getenv("CALLBACK_MAX_RETRIES", "3"))
CALLBACK_DLQ_EXCHANGE = "game.callbacks.dead"
CALLBACK_DLQ_QUEUE = "q.platform.callbacks.dead.v1"
CALLBACK_DLQ_ROUTING_KEY = "dead"

# Transactional outbox。lease 必须长于一次 AMQP 连接+confirm 的最坏超时，
# 崩溃后超时 claim 会被其它发布器重新接管；稳定 message_id 让下游可以去重。
OUTBOX_LEASE_SECONDS = int(os.getenv("OUTBOX_LEASE_SECONDS", "60"))
OUTBOX_POLL_INTERVAL_SECONDS = float(
    os.getenv("OUTBOX_POLL_INTERVAL_SECONDS", "2")
)
OUTBOX_DRAIN_LIMIT = int(os.getenv("OUTBOX_DRAIN_LIMIT", "100"))
OUTBOX_RECOVERY_INTERVAL_SECONDS = float(
    os.getenv("OUTBOX_RECOVERY_INTERVAL_SECONDS", "30")
)
# 旧版本没有 outbox。仅对长期停在 dispatched 且没有任何 outbox 的步骤
# 做保守补偿，避免滚动升级时立刻重发刚刚正常派出的长任务。
OUTBOX_LEGACY_DISPATCH_GRACE_SECONDS = int(
    # 旧任务可能合法运行 48 小时；默认等 49 小时，避免首次滚动升级把所有
    # 正常在途任务重放。确认部署前无在途任务时可临时设为 0 做立即迁移。
    os.getenv("OUTBOX_LEGACY_DISPATCH_GRACE_SECONDS", "176400")
)

# 心跳超时(秒):超过则 health_status=unhealthy,调度时跳过
HEARTBEAT_TIMEOUT_SEC = int(os.getenv("HEARTBEAT_TIMEOUT_SEC", "60"))

# 本地存储产物约定：key=本地相对路径含 storage/outputs 前缀。
# 保留旧变量名以兼容现有 outbox payload 字段，但不再用于远端上传。
OSS_BUCKET = ""
OSS_OUTPUT_PREFIX = "storage/outputs/{task_id}/"

# ── CMS OAuth2 对接 ──
# MAINSITE_MOCK=1 时 auth/mainsite_client.py 返回假数据(离线跑通全链路);
# 凭证到手后置 0 走真实 CMS(接口基地址复用下面的 CMS_API_BASE)
MAINSITE_CLIENT_ID = os.getenv("MAINSITE_CLIENT_ID", "game-platform")
MAINSITE_CLIENT_SECRET = os.getenv("MAINSITE_CLIENT_SECRET", "")
MAINSITE_MOCK = os.getenv("MAINSITE_MOCK", "1") == "1"

# OAuth2 回调地址(前端回调页,需与 CMS 侧 redirect_uris 白名单精确匹配)
OAUTH_REDIRECT_URI = os.getenv("OAUTH_REDIRECT_URI", "https://console.farmmx.com/auth/callback")

# 平台自签 JWT(登录后发给前端,后续请求鉴权用)
PLATFORM_JWT_SECRET = os.getenv("PLATFORM_JWT_SECRET", "dev-jwt-secret-change-me")
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "168"))

# oauth_token 落库加密密钥(Fernet key,base64 44字符;为空则明文存,仅限本地 dev)
# 生成: python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
TOKEN_ENCRYPT_KEY = os.getenv("TOKEN_ENCRYPT_KEY", "")

# 内部服务调用令牌(novel-game-factory api_gateway → 平台 POST /api/v1/tasks 用,
# 与前端 JWT 二选一;为空则不接受 service token 调用)
PLATFORM_SERVICE_TOKEN = os.getenv("PLATFORM_SERVICE_TOKEN", "")

# 本地开发默认关闭平台登录校验,让浏览器直接进入视觉小说生产页面。
# 需要恢复 JWT/OAuth 鉴权时设置 PLATFORM_AUTH_DISABLED=0。
PLATFORM_AUTH_DISABLED = os.getenv("PLATFORM_AUTH_DISABLED", "1").strip().lower() in {
    "1", "true", "yes", "on",
}
PLATFORM_LOCAL_USER_UID = os.getenv("PLATFORM_LOCAL_USER_UID", "local-anonymous")
PLATFORM_LOCAL_USER_NAME = os.getenv("PLATFORM_LOCAL_USER_NAME", "本地用户")

# novel-game-factory api_gateway 地址(中台代理创作请求过去;api_gateway 自身无用户鉴权,
# 所以由中台校验 JWT 后带 service token 转发,不把它直接暴露给浏览器)
NOVEL_API_URL = os.getenv("NOVEL_API_URL", "http://localhost:8100")

# 超级管理员(按 CMS uid 认定,可编辑预置管线等)。逗号分隔,登录时命中则 role=admin。
PLATFORM_ADMIN_UIDS = {
    u.strip() for u in os.getenv(
        "PLATFORM_ADMIN_UIDS", "019ddc6f-d22f-7d13-975d-9c5b26ba7002"
    ).split(",") if u.strip()
}

# ── CMS 发布对接 ──
# 游戏站点根地址(game_builder 构建后 nginx 托管的游戏访问入口)。
# 默认用测试域名:它是 novel-game 里 game-server 的 ingress。
# game.farmmx.com 的 ingress 在 default namespace、指向另一套服务,配过去链接打不开。
GAME_BASE_URL = os.getenv("GAME_BASE_URL", "https://testgame.farmmx.com").rstrip("/")
# 素材统一由本地 API 路由提供，避免生成或保存远端 URL。
CDN_BASE_URL = ""
OSS_ENDPOINT = ""
ASSET_OSS_BUCKET = ""
ASSET_BASE_URL = ""
# 保留旧变量名以兼容导入方，但本地模式下不会初始化或访问 OSS。
OSS_ACCESS_KEY = os.getenv("OSS_ACCESS_KEY", "")
OSS_SECRET_KEY = os.getenv("OSS_SECRET_KEY", "")
# CMS 管理后台 API(发布游戏用;token 用当前登录用户的 OAuth2 access_token)
CMS_API_BASE = os.getenv("CMS_API_BASE", "https://testapi.farmmx.com/admin").rstrip("/")
