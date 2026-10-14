"""Read-side schema repair for creation-engine shared tables."""

from sqlalchemy import text

from common.models import engine


_COLUMNS = {
    "generated_project_name": "TEXT",
    "info_confirm_stage": "TEXT",
    "upload_type": "TEXT",
    "progress_percentage": "INTEGER DEFAULT 0",
    "overall_status": "INTEGER DEFAULT 0",
    "is_published": "BOOLEAN DEFAULT FALSE",
    "is_deleted": "BOOLEAN NOT NULL DEFAULT FALSE",
    "completed_at": "TIMESTAMP",
    "update_time": "TIMESTAMP",
}


def ensure_shared_schema() -> None:
    """Keep the engine's shared-table queries compatible with older deployments."""
    with engine.begin() as connection:
        # novel_tasks 是生成链路高频表，ALTER TABLE 需要排他锁。Pod 启动不能
        # 因为等这把锁无限挂起；拿不到锁就快速失败，由启动钩子降级处理。
        connection.execute(text("SET LOCAL lock_timeout = '3s'"))
        connection.execute(text("SET LOCAL statement_timeout = '15s'"))
        for name, definition in _COLUMNS.items():
            connection.execute(text(
                f"ALTER TABLE public.novel_tasks ADD COLUMN IF NOT EXISTS {name} {definition}"
            ))
        connection.execute(text(
            "UPDATE public.novel_tasks SET progress_percentage = 0 "
            "WHERE progress_percentage IS NULL"
        ))
        connection.execute(text(
            "UPDATE public.novel_tasks SET overall_status = 0 "
            "WHERE overall_status IS NULL"
        ))
        connection.execute(text(
            "UPDATE public.novel_tasks SET is_published = FALSE "
            "WHERE is_published IS NULL"
        ))
        connection.execute(text(
            "UPDATE public.novel_tasks SET is_deleted = FALSE "
            "WHERE is_deleted IS NULL"
        ))
