"""平台库表模型(独立库 agent_platform,见设计文档第九章)"""
import os
from datetime import datetime
from typing import Optional

from sqlalchemy import UniqueConstraint, text
from sqlmodel import Field, SQLModel, create_engine

from common.config import PLATFORM_DATABASE_URL


class StepStatus:
    PENDING = "pending"
    PUBLISHING = "publishing"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class TaskStatus:
    CREATED = "created"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    MANUAL_STAGE = "manual_stage"


class OutboxStatus:
    PENDING = "pending"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    CANCELLED = "cancelled"
    FAILED = "failed"


class PlatformUser(SQLModel, table=True):
    __tablename__ = "platform_user"
    id: Optional[int] = Field(default=None, primary_key=True)
    main_site_uid: str = Field(index=True, unique=True)
    username: str
    email: Optional[str] = None
    role: str = "creator"
    created_at: datetime = Field(default_factory=datetime.now)


class OauthToken(SQLModel, table=True):
    __tablename__ = "oauth_token"
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    access_token: str  # Fernet 加密存储,"enc:" 前缀(见 auth/crypto.py);无前缀=存量明文
    refresh_token: Optional[str] = None  # 同上加密存储
    expires_at: Optional[datetime] = None


class Task(SQLModel, table=True):
    __tablename__ = "task"
    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: str = Field(index=True, unique=True)
    user_id: Optional[int] = None
    pipeline_template_id: int
    # 任务用的管线名。用量按它归账(见 orchestrator._record_usage):一个任务对应一条管线,
    # 混搭管线的各步会落在不同组的 worker 上,所以「执行方组名」不能作为任务级口径。
    pipeline_name: Optional[str] = Field(default=None, index=True)
    impl_map_json: str = "{}"  # {"char_designer": "groupA.char.v2", ...}
    params_json: str = "{}"    # 创作配置快照(art_style/output_language/...)
    status: str = Field(default=TaskStatus.CREATED, index=True)
    progress: int = 0
    cancel_requested: bool = False
    created_at: datetime = Field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None


class TaskStep(SQLModel, table=True):
    __tablename__ = "task_step"
    id: Optional[int] = Field(default=None, primary_key=True)
    step_id: str = Field(index=True, unique=True)
    task_id: str = Field(index=True)
    capability: str
    impl_id: Optional[str] = None
    group_name: Optional[str] = None
    status: str = Field(default=StepStatus.PENDING, index=True)
    progress: int = 0
    attempt: int = 0
    max_attempts: int = 3
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error_code: Optional[str] = None
    error_msg: Optional[str] = None
    output_json: Optional[str] = None  # callback.succeeded 的 output(产物清单)


class JobOutbox(SQLModel, table=True):
    """Job 发布意图；与 TaskStep.PUBLISHING 在同一事务落库。"""

    __tablename__ = "job_outbox"
    __table_args__ = (
        UniqueConstraint("step_id", "attempt", name="uq_job_outbox_step_attempt"),
    )
    id: Optional[int] = Field(default=None, primary_key=True)
    # step_id + attempt，Rabbit properties.message_id 与 envelope.msg_id 共用。
    message_id: str = Field(index=True, unique=True)
    task_id: str = Field(index=True)
    step_id: str = Field(index=True)
    attempt: int
    capability: str
    impl_id: str
    routing_key: str
    payload_json: str
    payload_sha256: str
    status: str = Field(default=OutboxStatus.PENDING, index=True)
    publish_attempts: int = 0
    available_at: datetime = Field(default_factory=datetime.now, index=True)
    claimed_at: Optional[datetime] = None
    claim_token: Optional[str] = Field(default=None, index=True)
    published_at: Optional[datetime] = None
    last_error: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class ServiceRegistry(SQLModel, table=True):
    """能力目录:平台有哪些 capability 及其输入/输出契约引用"""
    __tablename__ = "service_registry"
    id: Optional[int] = Field(default=None, primary_key=True)
    capability: str = Field(index=True, unique=True)
    description: Optional[str] = None
    input_schema_ref: Optional[str] = None
    output_schema_ref: Optional[str] = None  # A1: 输出契约,任意搭配的前提


class ServiceImpl(SQLModel, table=True):
    """实现实例 = 热插拔登记簿(manifest_portal 升级)"""
    __tablename__ = "service_impl"
    id: Optional[int] = Field(default=None, primary_key=True)
    capability: str = Field(index=True)
    group_name: str
    impl_id: str = Field(index=True, unique=True)
    queue: str
    manifest_json: str = "{}"
    endpoint: Optional[str] = None
    enabled: bool = True           # 热插拔开关
    health_status: str = "unknown"  # healthy / unhealthy / unknown
    last_heartbeat: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.now)


class PipelineTemplate(SQLModel, table=True):
    __tablename__ = "pipeline_template"
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    dag_json: str  # {"nodes":[...], "edges":[["a","b"],...]}
    default_impl_map_json: str = "{}"
    is_active: bool = True


class Pipeline(SQLModel, table=True):
    """管线 = 名字(group_name 语义) + impl_map(每能力选一个实现)。

    任务创建带 group_name=管线名 → orchestrator 按此表解析 impl_map 调度;
    novel_tasks/api_usage 的 group_name 记录也用这个名字,任务与用量按管线区分。
    """
    __tablename__ = "pipeline"
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)  # 管线标识,即 group_name
    display_name: str = ""
    impl_map_json: str = "{}"  # {"char_designer": "group-two.char_designer.v1", ...} 空=各步自动选
    description: str = ""
    is_preset: bool = False    # 预置管线不可删
    created_at: datetime = Field(default_factory=datetime.now)


class CreationModeConfig(SQLModel, table=True):
    """创作模式 → 管线映射。对外只暴露三档模式,内部才是具体管线。

    pipeline_name 存字符串而非外键:管线可增删改名,悬空时接口返回
    pipeline_exists=false 让前端提示,而不是让配置写不进去。
    """
    __tablename__ = "creation_mode_config"
    id: Optional[int] = Field(default=None, primary_key=True)
    mode: str = Field(index=True, unique=True)  # efficiency / balanced / quality
    pipeline_name: str
    updated_at: datetime = Field(default_factory=datetime.now)
    updated_by: str = ""


class TokenUsage(SQLModel, table=True):
    __tablename__ = "token_usage"
    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: str = Field(index=True)
    step_id: str = Field(index=True)
    capability: str
    group_name: str = Field(index=True)
    impl_id: str
    api_type: str  # llm / image / tts / music
    model: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    image_count: int = 0
    tts_chars: int = 0
    requests: int = 0
    cost: float = 0.0
    created_at: datetime = Field(default_factory=datetime.now)


class PublishRecord(SQLModel, table=True):
    __tablename__ = "publish_record"
    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: str = Field(index=True)
    main_site_pub_id: Optional[str] = None
    status: str = "submitted"
    artifact_oss_key: Optional[str] = None
    submitted_at: datetime = Field(default_factory=datetime.now)
    published_at: Optional[datetime] = None
    error: Optional[str] = None


class CallbackLog(SQLModel, table=True):
    """幂等 + 审计:idempotency_key 唯一,重复回调直接丢弃"""
    __tablename__ = "callback_log"
    id: Optional[int] = Field(default=None, primary_key=True)
    idempotency_key: str = Field(index=True, unique=True)
    msg_type: str
    task_id: str = Field(index=True)
    step_id: str
    payload_json: str
    received_at: datetime = Field(default_factory=datetime.now)


class PromoGame(SQLModel, table=True):
    """创作推广游戏:平台自维护(不来自 CMS),用户上传封面/名/url/描述。"""
    __tablename__ = "promo_game"
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    url: str = ""
    cover: str = ""            # OSS 公网 URL
    description: str = ""
    created_at: datetime = Field(default_factory=datetime.now)


class PromoGameDistribution(SQLModel, table=True):
    """创作推广游戏分发记录:一条=一款游戏已分发到创作引擎游戏库,撤销即删记录。"""
    __tablename__ = "promo_game_distribution"
    id: Optional[int] = Field(default=None, primary_key=True)
    promo_game_id: int = Field(index=True, unique=True)
    distributed_by: Optional[str] = None   # 分发操作人 main_site_uid
    distributed_at: datetime = Field(default_factory=datetime.now)


def _int_env(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


# 端点都是同步 def,FastAPI 放线程池跑,每个请求各占一条连接。默认 5+10 太小:
# 页面轮询一轮的并发就能占满,多出来的请求在池里干等到前端超时。
engine = create_engine(
    PLATFORM_DATABASE_URL,
    pool_pre_ping=True,
    pool_size=_int_env("PLATFORM_DB_POOL_SIZE", 10, 1),
    max_overflow=_int_env("PLATFORM_DB_MAX_OVERFLOW", 10),
    pool_timeout=_int_env("PLATFORM_DB_POOL_TIMEOUT", 3, 1),
    pool_recycle=1800,    # 避免连接被数据库侧静默掐断后拿到坏连接
    pool_use_lifo=True,   # 优先复用热连接,让多余连接自然在数据库侧回收
    pool_reset_on_return="rollback",
    connect_args={
        "connect_timeout": 5,
        # 防止慢 SQL、锁等待或遗留事务无限占住连接。这里的端点都是短查询/短事务，
        # 超过 15 秒继续等待只会把整个池拖死，快速失败后客户端可以安全重试。
        "options": (
            "-c statement_timeout=15000 "
            "-c lock_timeout=5000 "
            "-c idle_in_transaction_session_timeout=30000 "
            "-c application_name=agent-platform-api"
        ),
    },
)

# adapter 每 20 秒会按 capability 上报心跳。数据库瞬时变慢时，这类高频请求
# 不能把任务列表、管线、登录等业务接口的连接全部占满，所以单独限制在 2 条连接内。
# 心跳允许短暂失败；下一个周期会自动恢复，业务接口则必须保留连接可用。
heartbeat_engine = create_engine(
    PLATFORM_DATABASE_URL,
    pool_pre_ping=True,
    pool_size=_int_env("PLATFORM_HEARTBEAT_DB_POOL_SIZE", 2, 1),
    max_overflow=_int_env("PLATFORM_HEARTBEAT_DB_MAX_OVERFLOW", 0),
    pool_timeout=_int_env("PLATFORM_HEARTBEAT_DB_POOL_TIMEOUT", 1, 1),
    pool_recycle=1800,
    pool_use_lifo=True,
    pool_reset_on_return="rollback",
    connect_args={
        "connect_timeout": 3,
        "options": (
            "-c statement_timeout=3000 "
            "-c lock_timeout=1000 "
            "-c idle_in_transaction_session_timeout=5000 "
            "-c application_name=agent-platform-heartbeat"
        ),
    },
)


def create_all_tables():
    # 启动自修复不能无限等待 DDL 锁，否则 Pod 会一直卡在 application startup。
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL lock_timeout = '3s'"))
        connection.execute(text("SET LOCAL statement_timeout = '15s'"))
        SQLModel.metadata.create_all(connection)
