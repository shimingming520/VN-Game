"""Configuration module - reads from .env file."""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

# 全站统一北京时间。不靠容器的 TZ 环境变量 —— 漏配的容器里 datetime.now() 会返回
# UTC，同一条记录就会出现 update_time 比 create_time 早 8 小时这种事（踩过）。
APP_TZ = ZoneInfo(os.getenv("APP_TIMEZONE", "Asia/Shanghai"))


def now_local() -> datetime:
    """当前北京时间。返回 naive datetime，配合库里的 timestamp(无时区) 列存取一致。"""
    return datetime.now(APP_TZ).replace(tzinfo=None)

# Database configuration (External PostgreSQL Server)
DB_HOST = os.getenv("DB_HOST", "172.16.50.17")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "extreme")
DB_USER = os.getenv("DB_USER", "test")
DB_PASSWORD = os.getenv("DB_PASSWORD", "change_me")

DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD.replace('@', '%40')}@{DB_HOST}:{DB_PORT}/{DB_NAME}?client_encoding=utf8"

# Redis configuration
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = os.getenv("REDIS_PORT", "6379")
REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}/0"

# Path configuration
STORAGE_PATH = os.getenv("STORAGE_PATH", "/storage")
RAW_NOVELS_PATH = os.path.join(STORAGE_PATH, "raw_novels")
OUTPUTS_PATH = os.path.join(STORAGE_PATH, "outputs")
BUILT_GAMES_PATH = os.path.join(STORAGE_PATH, "built_games")

# Story generation configuration
STORY_DEPTH = int(os.getenv("STORY_DEPTH", "8"))  # 故事深度（层数）
STORY_WIDTH = int(os.getenv("STORY_WIDTH", "3"))  # 故事宽度（分支数）

# game-platform 编排 API（替代本地 Celery chain 编排任务提交）
PLATFORM_API_URL = os.getenv("PLATFORM_API_URL", "http://localhost:8000")
# 平台内部服务令牌（POST /api/v1/tasks 强制鉴权后必带,值与平台侧 PLATFORM_SERVICE_TOKEN 一致）
PLATFORM_SERVICE_TOKEN = os.getenv("PLATFORM_SERVICE_TOKEN", "")

# 组名标识（用于多组共用平台库时区分数据来源）
GROUP_NAME = os.getenv("GROUP_NAME", "group-ref")

# 平台库(agent_platform):跨模块共用表所在库,与业务库 extreme 同实例不同库
# 分段配置对齐 game-platform/common/config.py,密码写原文即可自动转义
PLATFORM_DB_HOST = os.getenv("PLATFORM_DB_HOST", DB_HOST)
PLATFORM_DB_PORT = os.getenv("PLATFORM_DB_PORT", DB_PORT)
PLATFORM_DB_NAME = os.getenv("PLATFORM_DB_NAME", "agent_platform")
PLATFORM_DB_USER = os.getenv("PLATFORM_DB_USER", DB_USER)
PLATFORM_DB_PASSWORD = os.getenv("PLATFORM_DB_PASSWORD", DB_PASSWORD)

from urllib.parse import quote_plus as _q  # noqa: E402

PLATFORM_DATABASE_URL = (
    f"postgresql://{_q(PLATFORM_DB_USER)}:{_q(PLATFORM_DB_PASSWORD)}"
    f"@{PLATFORM_DB_HOST}:{PLATFORM_DB_PORT}/{PLATFORM_DB_NAME}?client_encoding=utf8"
)
