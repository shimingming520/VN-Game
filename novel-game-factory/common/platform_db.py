"""平台库(agent_platform)共享 engine。

跨模块共用的12张表读写都走这个 engine:
  name_mappings / plots / sound / backgrounds / cg / character_prompts /
  characters / novel_tasks / scene_prompts / story_tree_json / story_tree_md / api_usage
模块内部私有表(world_bible/outlines/scenes/story_summary/chapter_branch_plan/
material_regen_sessions 等)继续用业务库 DATABASE_URL。
"""
from sqlmodel import create_engine

from common.config import PLATFORM_DATABASE_URL
from common.db_pool import worker_pool_kwargs

platform_engine = create_engine(
    PLATFORM_DATABASE_URL,
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
            "-c application_name=novel-game-platform"
        ),
    },
    **worker_pool_kwargs(default_pool_size=5, default_max_overflow=5),
)
