#!/usr/bin/env python3
"""
数据库迁移脚本：为 outlines 表添加 source 和 version 字段

此迁移脚本会：
1. 添加 source 和 version 两个新字段（可为空）
2. 从现有的 data 字段中提取 source 和 version 值（如果存在）
3. 更新 data 字段，移除 source/version/metadata，只保留 chapters 内容
4. 将 data 字段格式统一为直接存储章节内容（与本地 JSON 格式一致）

运行前请备份数据库！
"""

import sys
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy import text
from sqlmodel import Session
from .models import engine, Outline


def migrate():
    """执行迁移"""
    print("🚀 开始数据库迁移：为 outlines 表添加 source 和 version 字段")
    print("=" * 60)

    try:
        with Session(engine) as session:
            # 1. 添加 source 和 version 字段
            print("\n步骤 1: 添加新字段...")
            try:
                session.exec(text("ALTER TABLE public.outlines ADD COLUMN IF NOT EXISTS source VARCHAR"))
                session.exec(text("ALTER TABLE public.outlines ADD COLUMN IF NOT EXISTS version VARCHAR"))
                session.commit()
                print("✅ 字段添加成功")
            except Exception as e:
                print(f"⚠️  字段可能已存在: {e}")
                session.rollback()

            # 2. 从现有记录中提取和更新数据
            print("\n步骤 2: 更新现有记录...")
            outlines = session.query(Outline).all()

            if not outlines:
                print("📝 没有需要迁移的记录")
                return

            updated_count = 0
            for outline in outlines:
                data = outline.data

                # 检查 data 是否是旧格式（包含 chapters 字段）
                if isinstance(data, dict) and 'chapters' in data:
                    # 旧格式：提取 source, version, chapters
                    outline.source = data.get('source')
                    outline.version = data.get('version')
                    outline.data = data.get('chapters', {})  # 只保留章节内容
                    updated_count += 1
                    print(f"   更新记录 ID={outline.id}, task_id={outline.task_id}")
                elif isinstance(data, dict) and not outline.source:
                    # 新格式但没有 source/version：保持 data 不变
                    # 可以根据需要设置默认值
                    outline.source = outline.source or "unknown"
                    outline.version = outline.version or "v1.0"
                    print(f"   设置默认值 ID={outline.id}, task_id={outline.task_id}")
                    updated_count += 1

            session.commit()
            print(f"✅ 更新了 {updated_count} 条记录")

            # 3. 验证迁移结果
            print("\n步骤 3: 验证迁移结果...")
            outlines_after = session.query(Outline).all()

            print(f"\n📊 迁移统计:")
            print(f"   总记录数: {len(outlines_after)}")

            for outline in outlines_after[:3]:  # 显示前3条
                chapter_count = len(outline.data) if isinstance(outline.data, dict) else 0
                print(f"\n   记录 ID={outline.id}")
                print(f"   - task_id: {outline.task_id}")
                print(f"   - source: {outline.source}")
                print(f"   - version: {outline.version}")
                print(f"   - 章节数: {chapter_count}")
                if chapter_count > 0:
                    sample_keys = list(outline.data.keys())[:3]
                    print(f"   - 章节示例: {sample_keys}")

            if len(outlines_after) > 3:
                print(f"\n   ... 还有 {len(outlines_after) - 3} 条记录")

            print("\n" + "=" * 60)
            print("✅ 迁移完成！")
            print("\n重要提示：")
            print("  - data 字段现在直接存储章节内容（与本地 JSON 格式一致）")
            print("  - source 和 version 作为独立字段存储")
            print("  - 请测试应用程序以确保一切正常工作")

    except Exception as e:
        print(f"\n❌ 迁移失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


def rollback():
    """回滚迁移（删除添加的字段）"""
    print("⚠️  开始回滚：删除 source 和 version 字段")

    try:
        with Session(engine) as session:
            session.exec(text("ALTER TABLE public.outlines DROP COLUMN IF EXISTS source"))
            session.exec(text("ALTER TABLE public.outlines DROP COLUMN IF EXISTS version"))
            session.commit()
            print("✅ 回滚完成")
    except Exception as e:
        print(f"❌ 回滚失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="数据库迁移：为 outlines 表添加 source 和 version 字段")
    parser.add_argument("--rollback", action="store_true", help="回滚迁移（删除新字段）")

    args = parser.parse_args()

    if args.rollback:
        confirm = input("确认要回滚迁移吗？这将删除 source 和 version 字段。(yes/no): ")
        if confirm.lower() == 'yes':
            rollback()
        else:
            print("❌ 取消回滚")
    else:
        confirm = input("确认要执行迁移吗？建议先备份数据库。(yes/no): ")
        if confirm.lower() == 'yes':
            migrate()
        else:
            print("❌ 取消迁移")
