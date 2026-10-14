# task_models.py - 任务进度相关的模型和常量
from typing import Optional
from datetime import datetime
from sqlmodel import SQLModel, Field, create_engine, Column
from sqlalchemy import DateTime, Text, Boolean, Integer, UniqueConstraint
from sqlalchemy.sql import func
from common.config import DATABASE_URL
from common.db_pool import worker_pool_kwargs
from common.platform_db import platform_engine


# 风格常量
class ArtStyle:
    """画面风格枚举"""
    INK_CHINESE = "ink_chinese"       # 新中式水墨风
    CYBERPUNK = "cyberpunk"           # 赛博朋克/霓虹
    JAPANESE = "japanese"             # 唯美日系
    DARK_EUROPEAN = "dark_european"   # 欧式暗黑
    MODERN_REALISM = "modern_realism" # 现代写实

    VALID_STYLES = [INK_CHINESE, CYBERPUNK, JAPANESE, DARK_EUROPEAN, MODERN_REALISM]


# 剧情模式常量
class StoryMode:
    """剧情模式枚举"""
    ORIGINAL = "original"              # 原文适应（直接切分，默认）
    MULTI = "multi"                    # 多剧情改写（世界观大纲LLM改写）
    CHAPTER_BRANCH = "chapter_branch"  # 章节分支模式（软硬分支+多结局）

    VALID_MODES = [ORIGINAL, MULTI, CHAPTER_BRANCH]


# 文本模型常量
class TextModel:
    """文本（LLM）模型枚举"""
    GPT_5_6_SOL = "gpt-5.6-sol"

    DEFAULT = GPT_5_6_SOL
    VALID_MODELS = [GPT_5_6_SOL]


# 生图模型常量
class ImageModel:
    """文生图模型枚举"""
    GPT_IMAGE_2 = "gpt-image-2"
    QWEN_IMAGE_PLUS = "qwen-image-plus"
    WAN25_T2I = "wan2.5-t2i-preview"

    DEFAULT = GPT_IMAGE_2
    VALID_MODELS = [GPT_IMAGE_2, QWEN_IMAGE_PLUS, WAN25_T2I]


# 音频模型常量
class AudioModel:
    """音频（配音 TTS）模型枚举"""
    SPEECH_2_8_HD = "speech-2.8-hd"

    DEFAULT = SPEECH_2_8_HD
    VALID_MODELS = [SPEECH_2_8_HD]


# 分辨率常量
class Resolution:
    """输出分辨率枚举"""
    P1080 = "1080p"
    K2 = "2k"
    K4 = "4k"

    DEFAULT = P1080
    VALID_RESOLUTIONS = [P1080, K2, K4]

engine = create_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    pool_timeout=3,
    pool_recycle=1800,
    pool_use_lifo=True,
    pool_reset_on_return="rollback",
    connect_args={
        "connect_timeout": 5,
        "options": (
            "-c statement_timeout=30000 "
            "-c lock_timeout=5000 "
            "-c idle_in_transaction_session_timeout=60000 "
            "-c application_name=novel-game-business"
        ),
    },
    **worker_pool_kwargs(default_pool_size=5, default_max_overflow=5),
)


# 作品全流程阶段常量（单字段 info_confirm_stage 承载：创建期 + 完成后工作台）
class InfoConfirmStage:
    """作品从创建到发布的当前阶段标记，单调推进，不回退。

    前段（创建期，overall_status=0）：upload → config → confirmed
    后段（完成后工作台，任务链已启动）：material → performance → publish

    config 必须保留为独立断点：摘要等异步任务执行期间用户退出后，
    进入接口才能把用户精确送回输出约束或信息确认页。
    """
    # 创建期
    UPLOAD = "upload"           # 已完成上传剧本
    CONFIG = "config"           # 已完成输出约束
    CONFIRMED = "confirmed"     # 已确认提交，资产生成已启动（进入③人物场景工作台）
    # 完成后工作台
    MATERIAL = "material"       # ③ 人物场景（重生成/修改）
    PERFORMANCE = "performance" # ④ 演绎微调
    PUBLISH = "publish"         # ⑤ 打包发布

    # 单调推进次序（advance 接口据此判断“只前进不后退”）
    ORDER = [UPLOAD, CONFIG, CONFIRMED, MATERIAL, PERFORMANCE, PUBLISH]
    # 完成后工作台可推进的目标（advance 接口的合法入参）
    WORKSPACE_STAGES = [MATERIAL, PERFORMANCE, PUBLISH]


# 任务状态常量
class TaskStatus:
    """任务状态常量"""
    PENDING = 0      # 待处理
    PROCESSING = 1   # 处理中
    COMPLETED = 2    # 已完成
    FAILED = 3       # 失败

    @classmethod
    def get_description(cls, status_code: int) -> str:
        descriptions = {
            cls.PENDING: "待处理",
            cls.PROCESSING: "处理中",
            cls.COMPLETED: "已完成",
            cls.FAILED: "失败"
        }
        return descriptions.get(status_code, "未知状态")


# 小说任务表 → 已迁至平台库(agent_platform),见 common/platform_task_models.py
# 这里 re-export 保持旧 import 路径兼容;放在类常量定义之后避免循环导入
from common.platform_task_models import NovelTask  # noqa: E402, F401


# 素材重生成版本记录表
class MaterialRegenSession(SQLModel, table=True):
    __tablename__ = "material_regen_sessions"
    __table_args__ = (
        UniqueConstraint("task_id", "session_no", "node_id", "material_id",
                         name="uq_mrs_task_session_material"),
        {'schema': 'public'},
    )

    id: Optional[int] = Field(default=None, primary_key=True)

    # 关联任务
    task_id: str = Field(index=True, nullable=False)

    # 操作序号：0=初始备份，1/2/3...=用户每次点击重生成
    session_no: int = Field(nullable=False)

    # 服务类型
    service_type: str = Field(nullable=False)  # char_designer / scene_painter / audio_producer / sound_arranger

    # 素材定位
    node_id: str = Field(nullable=False)       # OSS 格式，如 chapter_1_chapter_1
    material_id: str = Field(nullable=False)   # 文件名，如 LiYu_1_calm.png / scene_alley_01.png / scene_alley-1.mp3

    # 存储路径（完整 OSS key）
    # 格式：{task_id}-regenerate/{session_no}/{node_id}/assets/{subdir}/{material_id}
    oss_key: str = Field(sa_column=Column(Text, nullable=False))

    # 生成时使用的 prompt（音频类可为空）
    prompt: Optional[str] = Field(default=None, sa_column=Column(Text))

    # 是否已被确认激活（copy 到主路径）
    is_active: bool = Field(default=False, sa_column=Column(Boolean, nullable=False, server_default="false"))

    created_at: datetime = Field(
        default_factory=datetime.now,
        sa_column=Column(DateTime, nullable=False, server_default=func.now())
    )
