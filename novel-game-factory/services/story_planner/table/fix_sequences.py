#!/usr/bin/env python3
"""
数据库序列修复工具

用途：修复PostgreSQL自增序列与表数据不同步的问题
使用场景：
1. 手动插入数据后序列未更新
2. 数据库恢复/迁移后序列不正确
3. 出现主键冲突错误时

运行方式：
    python fix_sequences.py
"""

from sqlmodel import Session, text
from models import engine
from common.platform_db import platform_engine  # story_tree_json/md 已迁平台库


def fix_sequence(session: Session, table_name: str, seq_name: str) -> dict:
    """
    修复单个表的序列

    Args:
        session: 数据库会话
        table_name: 表名
        seq_name: 序列名

    Returns:
        dict: 修复结果 {"table": str, "max_id": int, "old_seq": int, "new_seq": int, "fixed": bool}
    """
    # 获取表中最大ID
    result = session.exec(text(f'SELECT MAX(id) FROM {table_name}')).first()
    max_id = result[0] if result[0] else 0

    # 获取序列当前值
    result = session.exec(text(f'SELECT last_value FROM {seq_name}')).first()
    old_seq_value = result[0] if result else 0

    # 判断是否需要修复
    needs_fix = old_seq_value < max_id

    if needs_fix:
        # 修复序列：设置为 MAX(id)
        session.exec(text(f"SELECT setval('{seq_name}', (SELECT MAX(id) FROM {table_name}))"))
        session.commit()

        # 获取新的序列值
        result = session.exec(text(f'SELECT last_value FROM {seq_name}')).first()
        new_seq_value = result[0] if result else 0
    else:
        new_seq_value = old_seq_value

    return {
        "table": table_name,
        "max_id": max_id,
        "old_seq": old_seq_value,
        "new_seq": new_seq_value,
        "fixed": needs_fix
    }


def check_and_fix_all_sequences():
    """检查并修复所有表的序列"""

    # 定义需要检查的表和序列（分两组：平台库 / 业务库）
    platform_tables = [
        ('story_tree_json', 'story_tree_json_id_seq'),
        ('story_tree_md', 'story_tree_md_id_seq'),
    ]
    business_tables = [
        ('world_bible', 'world_bible_id_seq'),
        ('outlines', 'outlines_id_seq'),
    ]

    print("=" * 70)
    print("🔧 数据库序列检查与修复工具")
    print("=" * 70)
    print()

    results = []

    for eng, tables in ((platform_engine, platform_tables), (engine, business_tables)):
        with Session(eng) as session:
            for table_name, seq_name in tables:
                try:
                    result = fix_sequence(session, table_name, seq_name)
                    results.append(result)

                    status = "❌ 已修复" if result["fixed"] else "✅ 正常"
                    print(f"{status} {table_name}:")
                    print(f"   最大ID: {result['max_id']}")
                    print(f"   序列值: {result['old_seq']} -> {result['new_seq']}")
                    print()

                except Exception as e:
                    print(f"❌ 修复 {table_name} 失败: {e}")
                    print()

    # 统计
    fixed_count = sum(1 for r in results if r["fixed"])
    total_count = len(results)

    print("=" * 70)
    print(f"📊 修复完成: {fixed_count}/{total_count} 个表需要修复")
    print("=" * 70)

    return results


if __name__ == "__main__":
    check_and_fix_all_sequences()
