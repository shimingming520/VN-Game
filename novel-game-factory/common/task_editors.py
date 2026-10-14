"""作品编辑者名单。

只记录某个作品曾被哪些用户成功编辑过，不记录具体编辑动作。编辑者按
最近一次成功编辑排序，同一用户在数组中只保留一条记录。
"""

import json
import logging
import threading
from typing import Dict, Iterable, List, Optional

from sqlalchemy import bindparam, text

from common.platform_db import platform_engine
from common.task_models import engine as business_engine


logger = logging.getLogger(__name__)

_schema_lock = threading.Lock()
_schema_ready = False


def _ensure_schema_for_engine(engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("""
            ALTER TABLE public.novel_tasks
            ADD COLUMN IF NOT EXISTS editors JSONB NOT NULL DEFAULT '[]'::jsonb
        """))
        connection.execute(text("""
            UPDATE public.novel_tasks
            SET editors = '[]'::jsonb
            WHERE editors IS NULL
        """))
        connection.execute(text("""
            ALTER TABLE public.novel_tasks
            ALTER COLUMN editors SET DEFAULT '[]'::jsonb,
            ALTER COLUMN editors SET NOT NULL
        """))
        connection.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_novel_tasks_editors_gin
            ON public.novel_tasks USING GIN (editors)
        """))


def ensure_task_editors_schema() -> None:
    """幂等创建 agent_platform/extreme 两个库的 editors 字段与索引。"""
    global _schema_ready
    if _schema_ready:
        return

    with _schema_lock:
        if _schema_ready:
            return
        _ensure_schema_for_engine(platform_engine)
        _ensure_schema_for_engine(business_engine)
        _schema_ready = True


def record_task_editor(task_id: str, user_id: str, user_name: Optional[str]) -> bool:
    """把成功操作的用户写入作品编辑者名单。

    写入使用单条 UPDATE 完成：先移除该 user_id 的旧记录，再追加到数组
    末尾。因此并发更新不会发生 Python 侧的“读-改-写”覆盖，同一用户也
    不会重复出现。

    Returns:
        True 表示找到作品并完成更新；False 表示参数不完整或作品不存在。
    """
    normalized_task_id = (task_id or "").strip()
    normalized_user_id = (user_id or "").strip()
    normalized_user_name = (user_name or "").strip()
    if not normalized_task_id or not normalized_user_id:
        logger.warning(
            "跳过编辑者记录：task_id 或 user_id 为空 task_id=%r user_id=%r",
            task_id,
            user_id,
        )
        return False

    ensure_task_editors_schema()
    editor_json = json.dumps(
        [{"user_id": normalized_user_id, "user_name": normalized_user_name}],
        ensure_ascii=False,
    )
    # 编辑者名单的实际读写只走 agent_platform。extreme 仅保持相同表结构，
    # 不同步编辑者数据。
    with platform_engine.begin() as platform_connection:
        platform_row = platform_connection.execute(
            text("""
                UPDATE public.novel_tasks
                SET editors = COALESCE(
                    (
                        SELECT jsonb_agg(item ORDER BY position)
                        FROM jsonb_array_elements(COALESCE(editors, '[]'::jsonb))
                             WITH ORDINALITY AS current_editor(item, position)
                        WHERE item ->> 'user_id' IS DISTINCT FROM :user_id
                    ),
                    '[]'::jsonb
                ) || CAST(:editor AS jsonb)
                WHERE task_id = :task_id
                RETURNING task_id
            """),
            {
                "task_id": normalized_task_id,
                "user_id": normalized_user_id,
                "editor": editor_json,
            },
        ).fetchone()

        if not platform_row:
            logger.warning("编辑者记录失败：平台库作品不存在 task_id=%s", normalized_task_id)
            return False

    return True


def record_task_editor_if_success(
    result: object,
    task_id: str,
    user_id: str,
    user_name: Optional[str],
) -> bool:
    """仅在异步编辑结果至少成功一项时记录编辑者。"""
    if not isinstance(result, dict) or int(result.get("success", 0) or 0) <= 0:
        return False
    return record_task_editor(task_id, user_id, user_name)


def _normalize_editors(value: object) -> List[dict]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return []
    if not isinstance(value, list):
        return []

    editors = []
    for item in value:
        if not isinstance(item, dict) or not item.get("user_id"):
            continue
        editors.append({
            "user_id": str(item["user_id"]),
            "user_name": str(item.get("user_name") or ""),
        })
    return editors


def get_task_editors(task_ids: Iterable[str]) -> Dict[str, List[dict]]:
    """批量读取作品编辑者名单。"""
    normalized_ids = list(dict.fromkeys(
        task_id.strip() for task_id in task_ids if task_id and task_id.strip()
    ))
    if not normalized_ids:
        return {}

    ensure_task_editors_schema()
    statement = text("""
        SELECT task_id, editors
        FROM public.novel_tasks
        WHERE task_id IN :task_ids
    """).bindparams(bindparam("task_ids", expanding=True))
    with platform_engine.connect() as connection:
        rows = connection.execute(statement, {"task_ids": normalized_ids}).fetchall()
    return {row[0]: _normalize_editors(row[1]) for row in rows}


def edited_task_ids_for_user_name(user_name: str) -> List[str]:
    """查询某用户名曾成功编辑过的作品，保持现有接口的用户名口径。"""
    normalized_user_name = (user_name or "").strip()
    if not normalized_user_name:
        return []

    ensure_task_editors_schema()
    probe = json.dumps([{"user_name": normalized_user_name}], ensure_ascii=False)
    with platform_engine.connect() as connection:
        rows = connection.execute(
            text("""
                SELECT task_id
                FROM public.novel_tasks
                WHERE editors @> CAST(:probe AS jsonb)
            """),
            {"probe": probe},
        ).fetchall()
    return [row[0] for row in rows]

