# platform_models.py - 迁入平台库(agent_platform)的跨模块共用表
# plots / story_tree_md / story_tree_json 三张表读写走 platform_engine
from typing import Optional, Dict, Any
from datetime import datetime, timezone
from sqlmodel import SQLModel, Field, Column
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import DateTime, Text
from sqlalchemy.sql import func
from common.platform_db import platform_engine


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# 1. 情节表 (只存数据)
class Plot(SQLModel, table=True):
    __tablename__ = "plots"
    id: Optional[int] = Field(default=None, primary_key=True)
    # 线上库 plots 表实际含 task_id/update_time 列(此前模型缺失),建新表时补齐
    task_id: Optional[str] = Field(default=None, index=True)
    data: Dict[str, Any] = Field(default={}, sa_column=Column(JSONB))
    create_time: datetime = Field(default_factory=now_utc)
    update_time: datetime = Field(
        default_factory=datetime.now,
        sa_column=Column(DateTime, server_default=func.now(), onupdate=func.now())
    )


# 6. 故事树Markdown表 (存储超长文章文字)
class StoryTreeMd(SQLModel, table=True):
    __tablename__ = "story_tree_md"
    __table_args__ = ({'schema': 'public'})

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: str = Field(index=True, nullable=False)
    name: str = Field(index=True, nullable=False)
    data: str = Field(default="", sa_column=Column(Text))
    create_time: datetime = Field(default_factory=now_utc)
    update_time: datetime = Field(
        default_factory=datetime.now,
        sa_column=Column(DateTime, server_default=func.now(), onupdate=func.now())
    )


# 7. 故事树JSON表 (存储JSON格式数据)
class StoryTreeJson(SQLModel, table=True):
    __tablename__ = "story_tree_json"
    __table_args__ = {'schema': 'public'}

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: str = Field(index=True, unique=True, nullable=False)
    data: Dict[str, Any] = Field(default={}, sa_column=Column(JSONB))
    create_time: datetime = Field(default_factory=now_utc)
    update_time: datetime = Field(
        default_factory=datetime.now,
        sa_column=Column(DateTime, server_default=func.now(), onupdate=func.now())
    )
