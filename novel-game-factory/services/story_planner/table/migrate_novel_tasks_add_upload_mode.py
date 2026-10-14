#!/usr/bin/env python3
"""为 public.novel_tasks 添加 upload_mode 字段（single | batch）。

存量数据回填策略：所有历史记录视为单部，置为 'single'。
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy import text
from sqlmodel import Session

from common.task_models import UploadMode
from common.platform_db import platform_engine as engine  # novel_tasks 已迁平台库


COL = "upload_mode"
TBL = "public.novel_tasks"


def migrate():
    print(f"开始迁移：{TBL} 添加 {COL}")
    with Session(engine) as session:
        session.exec(text(
            f"ALTER TABLE {TBL} ADD COLUMN IF NOT EXISTS {COL} VARCHAR "
            f"NOT NULL DEFAULT '{UploadMode.SINGLE}'"
        ))
        session.commit()
        affected = session.exec(text(
            f"UPDATE {TBL} SET {COL} = '{UploadMode.SINGLE}' WHERE {COL} IS NULL"
        ))
        session.commit()
        print(f"完成。回填行数: {getattr(affected, 'rowcount', 'n/a')}")


def rollback():
    print(f"回滚：{TBL} 删除 {COL}")
    with Session(engine) as session:
        session.exec(text(f"ALTER TABLE {TBL} DROP COLUMN IF EXISTS {COL}"))
        session.commit()
        print("已删除")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    if args.rollback:
        rollback()
    else:
        migrate()
