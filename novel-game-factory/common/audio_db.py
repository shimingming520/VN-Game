"""audio_producer 使用的进程级延迟数据库连接池。

延迟创建使纯文件续跑、单元测试和离线诊断无需安装 PostgreSQL 驱动；一旦实际
访问数据库，同一进程始终复用同一个 Engine/Pool，避免函数内反复创建连接池。
"""
from functools import lru_cache

from sqlalchemy import create_engine

from common.config import DATABASE_URL, PLATFORM_DATABASE_URL
from common.db_pool import worker_pool_kwargs


def _engine(url, application_name):
    return create_engine(
        url,
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
                f"-c application_name={application_name}"
            ),
        },
        **worker_pool_kwargs(default_pool_size=5, default_max_overflow=5),
    )


@lru_cache(maxsize=1)
def get_business_engine():
    return _engine(DATABASE_URL, "audio-producer-business")


@lru_cache(maxsize=1)
def get_platform_engine():
    return _engine(PLATFORM_DATABASE_URL, "audio-producer-platform")

