"""作品复刻关系。

源作品在 ``novel_tasks.remake_task_ids`` 中保存所有成功启动的衍生
``task_id``。字段结构在 agent_platform/extreme 两个库保持一致，实际
业务数据只写 agent_platform。
"""

import threading
from typing import List

from sqlalchemy import text

from common.platform_db import platform_engine
from common.task_models import engine as business_engine

_schema_lock = threading.Lock()
_schema_ready = False


def _ensure_schema_for_engine(engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("""
            ALTER TABLE public.novel_tasks
            ADD COLUMN IF NOT EXISTS remake_task_ids JSONB NOT NULL DEFAULT '[]'::jsonb
        """))
        connection.execute(text("""
            UPDATE public.novel_tasks
            SET remake_task_ids = '[]'::jsonb
            WHERE remake_task_ids IS NULL
        """))
        connection.execute(text("""
            ALTER TABLE public.novel_tasks
            ALTER COLUMN remake_task_ids SET DEFAULT '[]'::jsonb,
            ALTER COLUMN remake_task_ids SET NOT NULL
        """))


def ensure_task_remakes_schema() -> None:
    """幂等创建 agent_platform/extreme 两个库的复刻任务列表字段。"""
    global _schema_ready
    if _schema_ready:
        return

    with _schema_lock:
        if _schema_ready:
            return
        _ensure_schema_for_engine(platform_engine)
        _ensure_schema_for_engine(business_engine)
        _schema_ready = True


def normalize_remake_task_ids(value: object) -> List[str]:
    """把数据库 JSONB 值规范化为去重后的 task_id 列表。"""
    if not isinstance(value, list):
        return []

    normalized = []
    seen = set()
    for item in value:
        task_id = str(item or "").strip()
        if not task_id or task_id in seen:
            continue
        normalized.append(task_id)
        seen.add(task_id)
    return normalized


def append_remake_task_id(session, source_task_id: str, new_task_id: str) -> bool:
    """把成功启动的新任务原子追加到源作品，重复调用不会重复写入。

    调用方提供当前 agent_platform Session，使源作品数组更新可与新任务状态
    在同一事务提交。返回 False 表示参数无效或源作品不存在。
    """
    normalized_source_id = (source_task_id or "").strip()
    normalized_new_id = (new_task_id or "").strip()
    if (
        not normalized_source_id
        or not normalized_new_id
        or normalized_source_id == normalized_new_id
    ):
        return False

    row = session.execute(
        text("""
            UPDATE public.novel_tasks
            SET remake_task_ids = CASE
                WHEN COALESCE(remake_task_ids, '[]'::jsonb)
                     @> jsonb_build_array(CAST(:new_task_id AS text))
                THEN COALESCE(remake_task_ids, '[]'::jsonb)
                ELSE COALESCE(remake_task_ids, '[]'::jsonb)
                     || jsonb_build_array(CAST(:new_task_id AS text))
            END
            WHERE task_id = :source_task_id
            RETURNING task_id
        """),
        {
            "source_task_id": normalized_source_id,
            "new_task_id": normalized_new_id,
        },
    ).fetchone()
    return row is not None
