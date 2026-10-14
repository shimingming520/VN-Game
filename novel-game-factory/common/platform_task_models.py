# platform_task_models.py - 迁入平台库(agent_platform)的任务表
# novel_tasks 是跨模块共用表,读写走 platform_engine
from typing import List, Optional
from datetime import datetime
from sqlmodel import SQLModel, Field, Column
from sqlalchemy import DateTime, Text, Boolean, text
from sqlalchemy.dialects.postgresql import JSONB
from common.config import now_local
from common.platform_db import platform_engine

# 默认值与 common/task_models.py 的 ArtStyle/StoryMode/TaskStatus 常量一致
# (不 import 以避免与 task_models 的 re-export 形成循环导入)
_DEFAULT_ART_STYLE = "ink_chinese"
_DEFAULT_STORY_MODE = "original"
_STATUS_PENDING = 0


# 小说任务表 (批量处理任务管理)
class NovelTask(SQLModel, table=True):
    __tablename__ = "novel_tasks"
    __table_args__ = {'schema': 'public'}

    id: Optional[int] = Field(default=None, primary_key=True)

    # 任务ID (格式: 小说名拼音_8位uuid)
    task_id: str = Field(index=True, unique=True, nullable=False)

    # 小说文件名
    novel_filename: str = Field(nullable=False)

    # 项目名称
    project_name: str = Field(nullable=False)

    # 用户信息
    user_id: Optional[str] = Field(default=None, index=True)
    user_name: Optional[str] = Field(default=None)

    # 执行方组名（多组共用平台库时区分数据来源，各组写自己的组名）
    group_name: str = Field(default="group-ref", nullable=False, index=True)

    # 任务跑的管线名：任务日志与用量的业务口径。
    # 混搭管线的各步会落在不同组的 worker 上，所以「执行方组名」不能当任务级口径。
    # 提交时由 api_gateway 写入；中台在回调时也会兜底回填（见 game-platform
    # orchestrator._stamp_pipeline_name），两边都写同一个值，幂等。
    pipeline_name: Optional[str] = Field(default=None, index=True)

    # 输出语言 (zh-CN, en-US, ja-JP, etc.)
    output_language: str = Field(default="zh-CN", nullable=False)

    # 画面风格 (ink_chinese, cyberpunk, japanese, dark_european, modern_realism)
    art_style: str = Field(default=_DEFAULT_ART_STYLE, nullable=False)

    # 剧情模式 (original=原文适应, multi=多剧情改写)
    story_mode: str = Field(default=_DEFAULT_STORY_MODE, nullable=False)

    # 游戏玩法 (interactive_story=互动故事, free_growth=自由成长)
    gameplay: Optional[str] = Field(default=None)

    # 生成所用的具体模型名称
    text_model: Optional[str] = Field(default=None)   # 文本模型，如 gpt-5.6-sol
    image_model: Optional[str] = Field(default=None)  # 生图模型，如 gpt-image-2 / wan2.5-t2i-preview
    audio_model: Optional[str] = Field(default=None)  # 音频模型

    # 输出分辨率 (1080p / 2k / 4k)
    resolution: Optional[str] = Field(default=None)

    # 上传类型 (single=单文件流程, batch=批量流程)
    upload_type: Optional[str] = Field(default=None)

    # 改编约束（仅 MULTI 模式）
    target_audience: Optional[str] = Field(default=None)          # male | female
    narrative_perspective: Optional[str] = Field(default=None)    # first_person | third_person
    hot_elements: Optional[str] = Field(default=None, sa_column=Column(Text))  # JSON 字符串

    # AI 生成的作品名（用户可在信息确认页修改）
    generated_project_name: Optional[str] = Field(default=None)

    # 成功启动的衍生作品 task_id 列表；复刻次数由数组长度动态计算。
    remake_task_ids: List[str] = Field(
        default_factory=list,
        sa_column=Column(
            JSONB,
            nullable=False,
            server_default=text("'[]'::jsonb"),
        ),
    )

    # 前端四步流程阶段标记 (upload / config / adaptation / confirmed)
    info_confirm_stage: Optional[str] = Field(default=None)

    # 各阶段状态 (0-待处理, 1-处理中, 2-已完成, 3-失败)
    story_planner_status: int = Field(default=_STATUS_PENDING)
    info_extractor_status: int = Field(default=_STATUS_PENDING)
    sound_arranger_status: int = Field(default=_STATUS_PENDING)
    char_designer_status: int = Field(default=_STATUS_PENDING)
    audio_producer_status: int = Field(default=_STATUS_PENDING)
    scene_painter_status: int = Field(default=_STATUS_PENDING)
    game_builder_status: int = Field(default=_STATUS_PENDING)

    # 整体状态
    overall_status: int = Field(default=_STATUS_PENDING)

    # 进度百分比 (0-100)，用于前端进度条
    progress_percentage: int = Field(default=0)

    # 错误信息
    error_message: Optional[str] = Field(default=None, sa_column=Column(Text))

    # Celery任务链ID
    celery_chain_id: Optional[str] = Field(default=None)

    # 完成时间（任务进入 COMPLETED 状态时写入）
    completed_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime, nullable=True))

    # 软删除标记 (false-正常, true-已删除)，进入回收站
    is_deleted: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default="false")
    )

    # 彻底删除标记 (false-正常, true-已彻底删除)。
    # 二级软删除：置位后正常列表和回收站列表均不再返回该作品，前端彻底查不到。
    # 数据行与 OSS 素材仍保留，仅对前端隐藏，后续如需回收存储由后台清理任务处理。
    is_purged: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default="false")
    )

    # 发布标记 (false-未发布, true-已发布)，build/export 触发时置位。
    # 这是判断作品是否已发布的唯一依据（分类列表/统计/entry 均读本列）。
    is_published: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default="false")
    )

    # 时间统一走 now_local()（北京时间），不用 datetime.now：后者取容器本地时区，
    # 漏配 TZ 的容器会写入 UTC，同一条记录出现 update_time 早于 create_time 8 小时。
    create_time: datetime = Field(default_factory=now_local)
    update_time: datetime = Field(
        default_factory=now_local,
        sa_column=Column(DateTime, onupdate=now_local),
    )
