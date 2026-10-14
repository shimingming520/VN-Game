#!/usr/bin/env python3
"""
故事树数据入库模块 (story_tree_to_db.py)

负责将故事树的 MD 文件和 JSON 数据保存到数据库。
- MD 文件: 每生成一个节点的 MD 就存入 story_tree_md 表
- JSON 文件: 所有 MD 生成完后，将 story_tree.json 存入 story_tree_json 表
"""

import os
import sys
import json
import hashlib
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Any, Optional, List, Iterable
from datetime import datetime

# 添加项目根目录到路径，确保能找到相关模块
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from sqlalchemy import text
from sqlmodel import Session, select
from .models import StoryTreeMd, StoryTreeJson
from common.platform_db import platform_engine as engine  # story_tree_md/json 已迁平台库


class StaleStoryTreePublicationError(RuntimeError):
    """Raised when an older worker attempts to replace a newer publication."""


@dataclass(frozen=True)
class StoryTreePublishResult:
    md_count: int
    orphan_md_deleted: int
    publication_fingerprint: str


def calculate_publication_fingerprint(
    md_contents: Dict[str, str],
    json_data: Dict[str, Any],
) -> str:
    """Return the canonical fingerprint for one public story-tree publication."""
    payload = {
        "md": {key: md_contents[key] for key in sorted(md_contents)},
        "json": json_data,
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


# Backward-compatible private alias for callers/tests written before the
# completed-artifact validator needed the canonical calculation.
_publication_fingerprint = calculate_publication_fingerprint


def _lock_story_tree_publication(session: Session, task_id: str) -> None:
    """Serialize final publication per task on PostgreSQL within this transaction."""
    if session.connection().dialect.name != "postgresql":
        return
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
        {"lock_key": f"story-tree-publication:{task_id}"},
    )


def publish_story_tree_transactionally(
    task_id: str,
    md_contents: Dict[str, str],
    json_data: Dict[str, Any],
    expected_node_ids: Iterable[str],
    *,
    run_id: str,
    run_started_at: str,
    db_engine=engine,
) -> StoryTreePublishResult:
    """Atomically upsert the final story tree and remove this task's orphan MD rows.

    A PostgreSQL transaction advisory lock serializes concurrent publications for the
    same task. ``run_started_at`` is a fencing token: a delayed older worker cannot
    overwrite a publication produced by a newer run.
    """
    expected = set(expected_node_ids or [])
    provided = set(md_contents or {})
    if not expected:
        raise ValueError("expected_node_ids 不能为空")
    if provided != expected:
        missing = sorted(expected - provided)
        extra = sorted(provided - expected)
        raise ValueError(f"MD 集合与预期节点不一致: missing={missing}, extra={extra}")
    if not run_id or not run_started_at:
        raise ValueError("run_id 和 run_started_at 不能为空")

    published_json = deepcopy(json_data)
    fingerprint = calculate_publication_fingerprint(md_contents, published_json)
    published_json["publication"] = {
        "run_id": run_id,
        "run_started_at": run_started_at,
        "fingerprint": fingerprint,
    }

    with Session(db_engine) as session:
        try:
            _lock_story_tree_publication(session, task_id)

            json_rows = list(
                session.exec(
                    select(StoryTreeJson).where(StoryTreeJson.task_id == task_id)
                ).all()
            )
            existing_json = json_rows[0] if json_rows else None
            if existing_json and isinstance(existing_json.data, dict):
                previous = existing_json.data.get("publication", {})
                previous_started_at = str(previous.get("run_started_at", ""))
                if previous_started_at and previous_started_at > run_started_at:
                    raise StaleStoryTreePublicationError(
                        f"拒绝旧运行发布: task_id={task_id}, run_id={run_id}, "
                        f"newer_run_id={previous.get('run_id', '')}"
                    )

            existing_md_rows = list(
                session.exec(
                    select(StoryTreeMd).where(StoryTreeMd.task_id == task_id)
                ).all()
            )
            md_by_name: Dict[str, StoryTreeMd] = {}
            orphan_md_deleted = 0
            for row in existing_md_rows:
                if row.name not in expected:
                    session.delete(row)
                    orphan_md_deleted += 1
                    continue
                if row.name in md_by_name:
                    # Clean historical duplicates while the per-task lock is held.
                    session.delete(row)
                    orphan_md_deleted += 1
                    continue
                md_by_name[row.name] = row

            now = datetime.now()
            for name, content in md_contents.items():
                row = md_by_name.get(name)
                if row is None:
                    session.add(StoryTreeMd(task_id=task_id, name=name, data=content))
                else:
                    row.data = content
                    row.update_time = now
                    session.add(row)

            if existing_json is None:
                session.add(StoryTreeJson(task_id=task_id, data=published_json))
            else:
                existing_json.data = published_json
                existing_json.update_time = now
                session.add(existing_json)
                for duplicate in json_rows[1:]:
                    session.delete(duplicate)

            session.commit()
        except Exception:
            session.rollback()
            raise

    return StoryTreePublishResult(
        md_count=len(md_contents),
        orphan_md_deleted=orphan_md_deleted,
        publication_fingerprint=fingerprint,
    )


# ==================== MD 文件操作 ====================

def save_story_md_to_db(
    task_id: str,
    name: str,
    content: str,
    overwrite: bool = True
) -> Optional[StoryTreeMd]:
    """
    保存单个节点的 MD 内容到数据库

    Args:
        task_id: 任务ID (关联世界观/大纲的task_id)
        name: 节点名称 (文件名，不含 .md 后缀)
        content: MD 文件内容
        overwrite: 如果已存在，是否覆盖 (默认 True)

    Returns:
        StoryTreeMd: 保存成功的记录，失败返回 None
    """
    try:
        with Session(engine) as session:
            # 检查是否已存在相同 task_id + name 的记录
            existing = session.exec(
                select(StoryTreeMd).where(
                    StoryTreeMd.task_id == task_id,
                    StoryTreeMd.name == name
                )
            ).first()

            if existing:
                if not overwrite:
                    print(f"⚠️  MD记录已存在: task_id={task_id}, name={name} (overwrite=False)")
                    return existing
                else:
                    # 覆盖：更新现有记录
                    existing.data = content
                    existing.update_time = datetime.now()
                    session.commit()
                    session.refresh(existing)
                    print(f"🔄 MD记录已更新: task_id={task_id}, name={name}")
                    return existing
            else:
                # 创建新记录
                record = StoryTreeMd(
                    task_id=task_id,
                    name=name,
                    data=content
                )
                session.add(record)
                session.commit()
                session.refresh(record)
                print(f"✅ MD记录已保存: task_id={task_id}, name={name}")
                return record

    except Exception as e:
        print(f"❌ 保存MD记录失败: {e}")
        import traceback
        traceback.print_exc()
        return None


def get_story_md_by_task_id(task_id: str) -> List[StoryTreeMd]:
    """
    根据 task_id 获取所有 MD 记录

    Args:
        task_id: 任务ID

    Returns:
        List[StoryTreeMd]: MD 记录列表
    """
    try:
        with Session(engine) as session:
            records = session.exec(
                select(StoryTreeMd).where(StoryTreeMd.task_id == task_id)
            ).all()
            return list(records)

    except Exception as e:
        print(f"❌ 获取MD记录失败: {e}")
        return []


def get_story_md_by_name(task_id: str, name: str) -> Optional[StoryTreeMd]:
    """
    根据 task_id 和 name 获取单个 MD 记录

    Args:
        task_id: 任务ID
        name: 节点名称

    Returns:
        StoryTreeMd: MD 记录，不存在返回 None
    """
    try:
        with Session(engine) as session:
            record = session.exec(
                select(StoryTreeMd).where(
                    StoryTreeMd.task_id == task_id,
                    StoryTreeMd.name == name
                )
            ).first()
            return record

    except Exception as e:
        print(f"❌ 获取MD记录失败: {e}")
        return None


def get_story_md_by_name_strict(
    task_id: str,
    name: str,
    *,
    db_engine=engine,
) -> Optional[StoryTreeMd]:
    """Read one MD for checkpoint recovery without hiding database failures."""

    with Session(db_engine) as session:
        return session.exec(
            select(StoryTreeMd).where(
                StoryTreeMd.task_id == task_id,
                StoryTreeMd.name == name,
            )
        ).first()


def delete_story_md_by_task_id(task_id: str) -> int:
    """
    删除指定 task_id 的所有 MD 记录

    Args:
        task_id: 任务ID

    Returns:
        int: 删除的记录数
    """
    try:
        with Session(engine) as session:
            records = session.exec(
                select(StoryTreeMd).where(StoryTreeMd.task_id == task_id)
            ).all()
            count = len(records)
            for record in records:
                session.delete(record)
            session.commit()
            print(f"🗑️  已删除 {count} 条MD记录 (task_id={task_id})")
            return count

    except Exception as e:
        print(f"❌ 删除MD记录失败: {e}")
        return 0


# ==================== JSON 文件操作 ====================

def save_story_json_to_db(
    task_id: str,
    json_data: Dict[str, Any],
    overwrite: bool = True
) -> Optional[StoryTreeJson]:
    """
    保存故事树 JSON 数据到数据库

    Args:
        task_id: 任务ID (关联世界观/大纲的task_id)
        json_data: JSON 数据 (story_tree.json 的内容)
        overwrite: 如果已存在，是否覆盖 (默认 True)

    Returns:
        StoryTreeJson: 保存成功的记录，失败返回 None
    """
    try:
        with Session(engine) as session:
            # 检查是否已存在相同 task_id 的记录
            existing = session.exec(
                select(StoryTreeJson).where(StoryTreeJson.task_id == task_id)
            ).first()

            if existing:
                if not overwrite:
                    print(f"⚠️  JSON记录已存在: task_id={task_id} (overwrite=False)")
                    return existing
                else:
                    # 覆盖：更新现有记录
                    existing.data = json_data
                    existing.update_time = datetime.now()
                    session.commit()
                    session.refresh(existing)
                    print(f"🔄 JSON记录已更新: task_id={task_id}")
                    return existing
            else:
                # 创建新记录
                record = StoryTreeJson(
                    task_id=task_id,
                    data=json_data
                )
                session.add(record)
                session.commit()
                session.refresh(record)
                print(f"✅ JSON记录已保存: task_id={task_id}")
                return record

    except Exception as e:
        print(f"❌ 保存JSON记录失败: {e}")
        import traceback
        traceback.print_exc()
        return None


def get_story_json_by_task_id(task_id: str) -> Optional[StoryTreeJson]:
    """
    根据 task_id 获取 JSON 记录

    Args:
        task_id: 任务ID

    Returns:
        StoryTreeJson: JSON 记录，不存在返回 None
    """
    try:
        with Session(engine) as session:
            record = session.exec(
                select(StoryTreeJson).where(StoryTreeJson.task_id == task_id)
            ).first()
            return record

    except Exception as e:
        print(f"❌ 获取JSON记录失败: {e}")
        return None


def delete_story_json_by_task_id(task_id: str) -> bool:
    """
    删除指定 task_id 的 JSON 记录

    Args:
        task_id: 任务ID

    Returns:
        bool: 是否删除成功
    """
    try:
        with Session(engine) as session:
            record = session.exec(
                select(StoryTreeJson).where(StoryTreeJson.task_id == task_id)
            ).first()
            if record:
                session.delete(record)
                session.commit()
                print(f"🗑️  已删除JSON记录 (task_id={task_id})")
                return True
            else:
                print(f"⚠️  未找到JSON记录 (task_id={task_id})")
                return False

    except Exception as e:
        print(f"❌ 删除JSON记录失败: {e}")
        return False


# ==================== 批量导入 ====================

def import_story_tree_from_directory(
    task_id: str,
    story_dir: str,
    overwrite: bool = True,
    delete_source_after_import: bool = True
) -> Dict[str, Any]:
    """
    从本地目录导入故事树数据到数据库

    Args:
        task_id: 任务ID
        story_dir: 故事树目录路径 (包含 story_tree.json 和 generated_stories/)
        overwrite: 如果已存在，是否覆盖 (默认 True)
        delete_source_after_import: 导入成功后是否删除源目录 (默认 True)

    Returns:
        Dict: {"md_count": int, "json_saved": bool, "errors": List[str]}
    """
    result = {
        "md_count": 0,
        "json_saved": False,
        "errors": []
    }

    story_path = Path(story_dir)

    # 1. 导入所有 MD 文件
    md_dir = story_path / "generated_stories"
    if md_dir.exists():
        for md_file in md_dir.glob("*.md"):
            try:
                name = md_file.stem  # 文件名（不含 .md 后缀）作为 name
                content = md_file.read_text(encoding='utf-8')
                record = save_story_md_to_db(task_id, name, content, overwrite)
                if record:
                    result["md_count"] += 1
            except Exception as e:
                result["errors"].append(f"导入 {md_file.name} 失败: {e}")

    # 2. 导入 JSON 文件
    json_file = story_path / "story_tree.json"
    if json_file.exists():
        try:
            with open(json_file, 'r', encoding='utf-8') as f:
                json_data = json.load(f)
            record = save_story_json_to_db(task_id, json_data, overwrite)
            if record:
                result["json_saved"] = True
        except Exception as e:
            result["errors"].append(f"导入 story_tree.json 失败: {e}")

    print(f"📦 导入完成: {result['md_count']} 个MD文件, JSON {'成功' if result['json_saved'] else '失败'}")
    if result["errors"]:
        print(f"   ⚠️  错误: {result['errors']}")

    # 导入成功后删除源目录（如果启用）
    if delete_source_after_import and story_path.exists():
        # 只有JSON文件成功导入时才删除目录（JSON是核心数据）
        if result["json_saved"]:
            try:
                import shutil
                shutil.rmtree(story_path)
                print(f"🗑️  已删除源目录: {story_path}")
            except Exception as e:
                error_msg = f"删除源目录失败: {e}"
                print(f"⚠️  {error_msg}")
                result["errors"].append(error_msg)
        elif result["md_count"] > 0:
            print(f"⚠️  JSON文件未导入，保留源目录: {story_path}")
        else:
            print(f"⚠️  未导入任何数据，保留源目录: {story_path}")

    return result


# ==================== 命令行接口 ====================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="故事树数据入库工具")
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # import 命令
    import_parser = subparsers.add_parser("import", help="从本地目录导入故事树")
    import_parser.add_argument("task_id", help="任务ID")
    import_parser.add_argument("story_dir", help="故事树目录路径")
    import_parser.add_argument("--no-overwrite", action="store_true", help="不覆盖已存在的记录")
    import_parser.add_argument("--keep-files", action="store_true", help="导入后保留本地文件（默认会删除）")

    # list 命令
    list_parser = subparsers.add_parser("list", help="列出指定task_id的所有记录")
    list_parser.add_argument("task_id", help="任务ID")

    # delete 命令
    delete_parser = subparsers.add_parser("delete", help="删除指定task_id的所有记录")
    delete_parser.add_argument("task_id", help="任务ID")
    delete_parser.add_argument("--confirm", action="store_true", help="确认删除")

    args = parser.parse_args()

    if args.command == "import":
        result = import_story_tree_from_directory(
            task_id=args.task_id,
            story_dir=args.story_dir,
            overwrite=not args.no_overwrite,
            delete_source_after_import=not args.keep_files
        )
        print(f"\n导入结果: {result}")

    elif args.command == "list":
        print(f"\n📋 task_id={args.task_id} 的记录:")
        md_records = get_story_md_by_task_id(args.task_id)
        print(f"   MD记录数: {len(md_records)}")
        for r in md_records[:5]:
            print(f"     - {r.name}: {len(r.data)} 字符")
        if len(md_records) > 5:
            print(f"     ... 还有 {len(md_records) - 5} 条")

        json_record = get_story_json_by_task_id(args.task_id)
        if json_record:
            nodes_count = len(json_record.data.get("nodes", {}))
            print(f"   JSON记录: 存在 ({nodes_count} 个节点)")
        else:
            print(f"   JSON记录: 不存在")

    elif args.command == "delete":
        if not args.confirm:
            print("⚠️  请添加 --confirm 参数确认删除操作")
        else:
            md_count = delete_story_md_by_task_id(args.task_id)
            json_deleted = delete_story_json_by_task_id(args.task_id)
            print(f"已删除: {md_count} 条MD记录, JSON {'已删除' if json_deleted else '未找到'}")

    else:
        parser.print_help()
