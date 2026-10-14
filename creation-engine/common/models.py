"""创作引擎数据模型(复用中台 PlatformUser + 独立 OAuth token + 只读 NovelTask)。"""
from datetime import datetime
from typing import Optional

from sqlalchemy import text
from sqlmodel import Field, SQLModel, create_engine

from common.config import PLATFORM_DATABASE_URL


class PlatformUser(SQLModel, table=True):
    __tablename__ = "platform_user"
    id: Optional[int] = Field(default=None, primary_key=True)
    main_site_uid: str = Field(index=True, unique=True)
    username: str
    email: Optional[str] = None
    role: str = "creator"
    created_at: datetime = Field(default_factory=datetime.now)


class OauthToken(SQLModel, table=True):
    # 不能与中台共用 oauth_token：两套服务使用不同 TOKEN_ENCRYPT_KEY，任一端登录
    # 都会覆盖另一端密文，随后另一端必然报“密钥变更”。独立表后两边互不影响。
    __tablename__ = "creation_engine_oauth_token"
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, unique=True)
    access_token: str  # Fernet 加密存储,"enc:" 前缀;无前缀=明文
    refresh_token: Optional[str] = None
    expires_at: Optional[datetime] = None


class NovelTask(SQLModel, table=True):
    """只读镜像:查询 agent_platform.novel_tasks 表(仅本服务端点需要的字段)。

    建表与写入由 novel-game-factory 负责(权威定义见 novel-game-factory/common/
    platform_task_models.py),这里只镜像读取用到的列。新增列一律声明 Optional:
    真实表里这些列有 server_default,但历史行可能为 NULL,声明成非空会让 SQLModel
    在取到 NULL 时报校验错,整个列表接口 500。
    """
    __tablename__ = "novel_tasks"
    __table_args__ = {'schema': 'public'}

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: str = Field(index=True, unique=True)
    novel_filename: str
    project_name: str
    user_id: Optional[str] = Field(default=None, index=True)
    generated_project_name: Optional[str] = Field(default=None)
    # 进度与状态(0-待处理 1-处理中 2-已完成 3-失败;progress 0-100)
    progress_percentage: Optional[int] = Field(default=None)
    overall_status: Optional[int] = Field(default=None)
    # 前端四步流程阶段标记 (upload / config / adaptation / confirmed)
    info_confirm_stage: Optional[str] = Field(default=None)
    # 上传类型 (single=单文件流程, batch=批量流程)
    upload_type: Optional[str] = Field(default=None)
    # 发布标记:CMS 发布成功后由中台置位
    is_published: Optional[bool] = Field(default=None)
    is_deleted: bool = Field(default=False)
    create_time: datetime = Field(default_factory=datetime.now)
    update_time: Optional[datetime] = Field(default=None)
    completed_at: Optional[datetime] = Field(default=None)

    @property
    def created_at(self):
        """兼容属性:my_assets.py 使用 created_at,实际字段名为 create_time"""
        return self.create_time


class PipelineTask(SQLModel, table=True):
    """中台 orchestrator.task 的只读进度镜像。"""
    __tablename__ = "task"

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: str = Field(index=True, unique=True)
    status: Optional[str] = Field(default=None)
    progress: Optional[int] = Field(default=None)


class PromoGame(SQLModel, table=True):
    """只读镜像:中台创作推广游戏(建表由中台负责)"""
    __tablename__ = "promo_game"
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    url: str = ""
    cover: str = ""
    description: str = ""
    created_at: datetime = Field(default_factory=datetime.now)


class PromoGameDistribution(SQLModel, table=True):
    """只读镜像:分发记录(建表由中台负责),有记录=该游戏已分发到本平台游戏库"""
    __tablename__ = "promo_game_distribution"
    id: Optional[int] = Field(default=None, primary_key=True)
    promo_game_id: int = Field(index=True, unique=True)
    distributed_by: Optional[str] = None
    distributed_at: datetime = Field(default_factory=datetime.now)


class UserMaterialFlag(SQLModel, table=True):
    """用户对素材的个人操作:hidden=在“我的素材”里软删除(仅对该用户隐藏,不动 OSS);
    saved=已保存到我的资产。按 (user_id, asset_key) 唯一。"""
    __tablename__ = "user_material_flag"
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: str = Field(index=True)  # CMS uid
    asset_key: str = Field(index=True)  # OSS 对象 key
    hidden: bool = Field(default=False)
    saved: bool = Field(default=False)
    updated_at: datetime = Field(default_factory=datetime.now)


class UserFolder(SQLModel, table=True):
    """用户在“资产”里新建的文件夹(持久化,刷新不丢)"""
    __tablename__ = "user_folder"
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: str = Field(index=True)  # CMS uid
    name: str
    created_at: datetime = Field(default_factory=datetime.now)


class SavedMaterial(SQLModel, table=True):
    """用户保存到“我的资产”的素材。一条记录 = 某素材放在某文件夹里;
    同一素材复制到多个文件夹 = 多条记录。folder_id 为空表示放在“未分类”。"""
    __tablename__ = "saved_material"
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: str = Field(index=True)  # CMS uid
    folder_id: Optional[int] = Field(default=None, index=True)  # 关联 user_folder.id
    asset_key: str = Field(index=True)  # OSS 对象 key
    name: str = ""
    url: str = ""
    created_at: datetime = Field(default_factory=datetime.now)


class AssetNameTranslation(SQLModel, table=True):
    """素材文件名 → 中文展示名的翻译缓存(LLM 翻译一次后长期复用,避免重复调用)。
    source 为原始文件名(不含路径,含扩展名),全局共享不分用户。"""
    __tablename__ = "asset_name_translation"
    id: Optional[int] = Field(default=None, primary_key=True)
    source: str = Field(index=True, unique=True)
    translated: str = ""
    created_at: datetime = Field(default_factory=datetime.now)


# 创作页会并发请求项目、游戏库、模式、模型和用量；每个同步端点都在线程池中
# 独占一条数据库连接。SQLAlchemy 默认只有 5 条常驻连接 + 10 条临时连接，
# 页面轮询或多人同时访问时很容易耗尽，随后鉴权和用量接口会一起等待 30 秒并报 500。
# 与中台保持同一连接池口径，并缩短池等待时间，避免连接紧张时拖成前端长时间卡死。
engine = create_engine(
    PLATFORM_DATABASE_URL,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=10,
    pool_timeout=3,
    pool_recycle=1800,
    pool_use_lifo=True,
    pool_reset_on_return="rollback",
    # 数据库网络或路由异常时尽快失败；否则每个新连接可能卡几十秒，
    # Uvicorn 会一直停在 Waiting for application startup。
    connect_args={
        "connect_timeout": 5,
        "options": (
            "-c statement_timeout=15000 "
            "-c lock_timeout=5000 "
            "-c idle_in_transaction_session_timeout=30000 "
            "-c application_name=creation-engine-api"
        ),
    },
)


def create_all_tables():
    # create_all 也会执行 DDL。启动期间不允许无限等待别的事务释放表锁，
    # 否则 Pod 永远无法进入 Ready。超时由 main.on_startup 记录并降级启动。
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL lock_timeout = '3s'"))
        connection.execute(text("SET LOCAL statement_timeout = '15s'"))
        SQLModel.metadata.create_all(connection)
