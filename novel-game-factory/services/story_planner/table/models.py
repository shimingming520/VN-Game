# models.py
from typing import Optional, Dict, Any
from datetime import datetime
from sqlmodel import SQLModel, Field, Column
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import DateTime, Text
from sqlalchemy.sql import func
from common.task_models import NovelTask, TaskStatus, engine  # 从 common 导入并复用业务库连接池
from common.platform_db import platform_engine
# 跨模块共用表(平台库):保持旧 import 路径兼容
from services.story_planner.table.platform_models import Plot, StoryTreeJson, StoryTreeMd  # noqa: F401

# 世界观状态常量
class WorldviewStatus:
    """世界观状态常量"""
    DRAFT = 0      # 草稿/编辑中
    CONFIRMED = 1  # 已确定/使用中
    ARCHIVED = 2   # 软删除/已归档

    @classmethod
    def get_description(cls, status_code: int) -> str:
        descriptions = {
            cls.DRAFT: "草稿（可编辑）",
            cls.CONFIRMED: "已确定",
            cls.ARCHIVED: "已归档（软删除）"
        }
        return descriptions.get(status_code, "未知状态")


# 大纲状态常量
class OutlineStatus:
    """大纲状态常量"""
    DRAFT = 0      # 草稿/编辑中
    CONFIRMED = 1  # 已确定/使用中
    ARCHIVED = 2   # 软删除/已归档

    @classmethod
    def get_description(cls, status_code: int) -> str:
        descriptions = {
            cls.DRAFT: "草稿（可编辑）",
            cls.CONFIRMED: "已确定",
            cls.ARCHIVED: "已归档（软删除）"
        }
        return descriptions.get(status_code, "未知状态")


# 1. 情节表 → 已迁至平台库,见 platform_models.Plot


# 2. 场景表 (只存数据)
class Scene(SQLModel, table=True):
    __tablename__ = "scenes"
    id: Optional[int] = Field(default=None, primary_key=True)
    data: Dict[str, Any] = Field(default={}, sa_column=Column(JSONB))
    create_time: datetime = Field(default_factory=datetime.now)


# 3. 关联表 (存对应关系)
class StoryRelease(SQLModel, table=True):
    __tablename__ = "story_releases"
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: str = Field(index=True, unique=True, nullable=False)
    plot_id: int = Field(index=True, nullable=False)
    scene_id: int = Field(index=True, nullable=False)
    create_time: datetime = Field(default_factory=datetime.now)


# 4. 世界观表 (存世界观数据)
class Worldview(SQLModel, table=True):
    __tablename__ = "world_bible"
    __table_args__ = {'schema': 'public'}

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: str = Field(index=True, unique=True, nullable=False)
    data: Dict[str, Any] = Field(default={}, sa_column=Column(JSONB))
    art_style: int = Field(default=0, nullable=False)
    # 状态: 0-草稿(DRAFT), 1-已确定(CONFIRMED), 2-已归档(ARCHIVED)
    status: int = Field(default=WorldviewStatus.CONFIRMED, nullable=False)
    create_time: datetime = Field(default_factory=datetime.now)
    update_time: datetime = Field(
        default_factory=datetime.now,
        sa_column=Column(DateTime, server_default=func.now(), onupdate=func.now())
    )


# 5. 故事大纲表 (存完整大纲数据)
class Outline(SQLModel, table=True):
    __tablename__ = "outlines"
    __table_args__ = {'schema': 'public'}

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: str = Field(index=True, unique=True, nullable=False)
    # 格式: {"1": "第1章...", "2": "第2章...", "8-ending-happy": "结局..."}
    data: Dict[str, Any] = Field(default={}, sa_column=Column(JSONB))
    # 状态: 0-草稿(DRAFT), 1-已确定(CONFIRMED), 2-已归档(ARCHIVED)
    status: int = Field(default=OutlineStatus.CONFIRMED, nullable=False)
    create_time: datetime = Field(default_factory=datetime.now)
    update_time: datetime = Field(
        default_factory=datetime.now,
        sa_column=Column(DateTime, server_default=func.now(), onupdate=func.now())
    )


# 6. 故事树Markdown表 → 已迁至平台库,见 platform_models.StoryTreeMd


# 7. 故事树JSON表 → 已迁至平台库,见 platform_models.StoryTreeJson


# 8. 故事摘要表 (存储剧本大纲、背景、一句话故事)
class StorySummary(SQLModel, table=True):
    __tablename__ = "story_summary"
    __table_args__ = {'schema': 'public'}

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: str = Field(index=True, unique=True, nullable=False)
    # 格式: {"outline": "剧本大纲...", "background": "剧本背景...", "one_liner": "一句话故事..."}
    data: Dict[str, Any] = Field(default={}, sa_column=Column(JSONB))
    # 状态: 0=pending(生成中), 1=ready(已完成), 2=failed(生成失败)
    status: int = Field(default=0, nullable=False)
    create_time: datetime = Field(default_factory=datetime.now)
    update_time: datetime = Field(
        default_factory=datetime.now,
        sa_column=Column(DateTime, server_default=func.now(), onupdate=func.now())
    )


# 9. chapter_branch 拓扑规划中间表
class ChapterBranchPlan(SQLModel, table=True):
    __tablename__ = "chapter_branch_plan"
    __table_args__ = {'schema': 'public'}

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: str = Field(index=True, unique=True, nullable=False)
    topology_json: Dict[str, Any] = Field(default={}, sa_column=Column(JSONB))
    create_time: datetime = Field(default_factory=datetime.now)
    update_time: datetime = Field(
        default_factory=datetime.now,
        sa_column=Column(DateTime, server_default=func.now(), onupdate=func.now())
    )


def init_db():
    # 如果数据库报错表结构不对，解开下面这行重置一次
    # SQLModel.metadata.drop_all(engine)
    # 全部模型共用一份 metadata,必须按表拆分建到各自的库
    platform_tables = [Plot.__table__, StoryTreeMd.__table__, StoryTreeJson.__table__]
    from common.platform_task_models import NovelTask as _PlatformNovelTask  # noqa: F401
    platform_tables.append(_PlatformNovelTask.__table__)
    business_tables = [t for t in SQLModel.metadata.tables.values() if t not in platform_tables]
    SQLModel.metadata.create_all(engine, tables=business_tables)
    SQLModel.metadata.create_all(platform_engine, tables=platform_tables)
    print("✅ 数据库结构已就绪 (业务库: Scenes/StoryReleases/WorldBible/Outlines/StorySummary/ChapterBranchPlan...; 平台库: Plots/StoryTreeMd/StoryTreeJson/NovelTasks)")


if __name__ == "__main__":
    init_db()
