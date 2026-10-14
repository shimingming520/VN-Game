#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
节点 ID 转换工具

OSS node_id 格式：chapter_{depth}_{suffix}
DB node_id 格式：chapter_1 或 node_{suffix}

转换规则：
- chapter_1_chapter_1 → chapter_1 (DB) / chapter_1_chapter_1 (OSS)
- chapter_2_7a80a1c2 → node_7a80a1c2 (DB) / chapter_2_7a80a1c2 (OSS)
- chapter_3_14beaa8b → node_14beaa8b (DB) / chapter_3_14beaa8b (OSS)
"""

import json
import os
import sys


def parse_oss_node_id(oss_node_id: str) -> tuple:
    """
    解析 OSS node_id，返回 (db_node_id, oss_node_id)

    Args:
        oss_node_id: OSS 格式的 node_id（如 chapter_1_chapter_1、chapter_2_7a80a1c2）

    Returns:
        (db_node_id, oss_node_id) 元组
        - db_node_id: 用于查询数据库（chapter_1 或 node_xxx）
        - oss_node_id: 用于 OSS 路径（原样返回）
    """
    # 格式：chapter_{depth}_{suffix}
    if not oss_node_id.startswith("chapter_"):
        # 兼容旧格式：如果传入的就是 chapter_1 或 node_xxx，直接返回
        return oss_node_id, oss_node_id

    parts = oss_node_id.split("_", 2)  # 最多分3段
    if len(parts) < 3:
        # 格式不对，原样返回
        return oss_node_id, oss_node_id

    # parts = ["chapter", "1", "chapter_1"] 或 ["chapter", "2", "7a80a1c2"]
    suffix = parts[2]

    if suffix == "chapter_1":
        db_node_id = "chapter_1"
    else:
        db_node_id = f"node_{suffix}"

    return db_node_id, oss_node_id


def db_to_oss_node_id(db_node_id: str, task_id: str = None) -> str:
    """
    将 DB 格式 node_id 转换为 OSS 格式。

    - chapter_1          → chapter_1_chapter_1（直接推导，无需查 DB）
    - node_{suffix}      → chapter_{depth}_{suffix}（查 story_tree_json 取 depth）
    - 已是 OSS 格式时原样返回

    Args:
        db_node_id: DB 格式或 OSS 格式的 node_id
        task_id:    任务 ID，node_xxx 时必须提供（用于查 story_tree_json）

    Returns:
        OSS 格式的 node_id
    """
    # 已是 OSS 格式（chapter_{depth}_{suffix}，三段）
    if db_node_id.startswith("chapter_"):
        parts = db_node_id.split("_", 2)
        if len(parts) == 3:
            return db_node_id  # 已是 OSS 格式，原样返回

    # DB 格式：chapter_1 → chapter_1_chapter_1
    if db_node_id == "chapter_1":
        return "chapter_1_chapter_1"

    # DB 格式：node_{suffix} → chapter_{depth}_{suffix}
    if db_node_id.startswith("node_") and task_id:
        suffix = db_node_id[5:]
        depth = _lookup_node_depth(task_id, db_node_id)
        if depth:
            return f"chapter_{depth}_{suffix}"

    return db_node_id  # 兜底：原样返回


def _lookup_node_depth(task_id: str, db_node_id: str):
    """从 story_tree_json 表查询节点 depth，找不到返回 None。"""
    try:
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
        from common.config import DATABASE_URL, PLATFORM_DATABASE_URL
        from common.story_tree_locale import select_story_tree_for_consumer
        from sqlalchemy import create_engine, text
        engine = create_engine(PLATFORM_DATABASE_URL)
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT data FROM story_tree_json WHERE task_id = :tid LIMIT 1"),
                {"tid": task_id}
            ).fetchone()
        if row:
            data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
            data = select_story_tree_for_consumer(data)
            node_data = data.get("nodes", {}).get(db_node_id, {})
            return node_data.get("depth")
    except Exception:
        pass
    return None
