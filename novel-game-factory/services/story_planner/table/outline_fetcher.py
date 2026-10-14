#!/usr/bin/env python3
"""
大纲获取模块 (outline_fetcher.py)

负责根据世界观task_id从数据库获取大纲。
如果大纲不存在，可基于world_bible生成大纲并保存到数据库。

输入：task_id (世界观的task_id), world_bible (可选)
输出：大纲task_id, 大纲数据 (chapters字典)
"""

import sys
import os
from typing import Dict, Any, Optional, Tuple
from pathlib import Path

# 添加项目根目录到路径，确保能找到相关模块
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from sqlmodel import Session, select
from .models import Outline, engine
from .outline_to_db import save_outline_directly


def get_outline_by_world_bible_task_id(world_bible_task_id: str) -> Optional[Outline]:
    """
    根据世界观的task_id查找关联的大纲记录

    现在大纲的task_id字段直接使用世界观的task_id，所以可以直接通过task_id查找。

    Args:
        world_bible_task_id: 世界观的task_id

    Returns:
        Outline对象（如果找到），否则返回None
    """
    with Session(engine) as session:
        # 直接通过task_id字段查找
        outline = session.query(Outline).filter(
            Outline.task_id == world_bible_task_id
        ).first()
        return outline


def fetch_outline(
    worldview_task_id: str,
    world_bible: Optional[Any] = None,
    max_depth: int = 8
) -> Tuple[str, Dict[str, Any]]:
    """
    获取大纲的主要函数

    流程：
    1. 根据worldview_task_id查找现有大纲
    2. 如果找到，返回大纲的task_id和chapters数据
    3. 如果未找到且提供了world_bible，生成新大纲并保存
    4. 如果未找到且未提供world_bible，抛出异常

    Args:
        worldview_task_id: 世界观的task_id
        world_bible: WorldBible对象或字典（可选，用于生成新大纲）
        max_depth: 最大章节深度（默认8）

    Returns:
        tuple: (大纲task_id, chapters字典)

    Raises:
        ValueError: 未找到大纲且未提供world_bible
        Exception: 大纲生成或保存失败
    """
    # 1. 尝试查找现有大纲
    outline = get_outline_by_world_bible_task_id(worldview_task_id)
    if outline:
        chapters = outline.data  # data字段直接存储章节内容（与本地JSON格式一致）
        print(f"✅ 找到现有大纲，task_id: {outline.task_id}")
        print(f"   章节数: {len(chapters)}")
        # outline.task_id 现在就是 worldview_task_id
        return outline.task_id, chapters

    # 2. 未找到大纲，需要生成
    if world_bible is None:
        raise ValueError(f"未找到与世界观task_id '{worldview_task_id}' 关联的大纲，且未提供world_bible用于生成")

    print(f"⚠️  未找到与世界观task_id '{worldview_task_id}' 关联的大纲")
    print(f"   正在基于world_bible生成新大纲...")

    # 3. 导入必要的模块（动态导入以避免循环依赖）
    try:
        from services.story_planner.core.story_generator import StoryGenerator
        from services.story_planner.core.world_analyst import WorldBible as WB

        # 确保world_bible是WorldBible对象
        if isinstance(world_bible, dict):
            # 如果是字典，转换为WorldBible对象
            world_bible_obj = WB(**world_bible)
        elif hasattr(world_bible, 'to_dict'):
            # 已经是WorldBible对象
            world_bible_obj = world_bible
        else:
            raise TypeError("world_bible必须是WorldBible对象或字典")

        # 创建StoryGenerator实例
        generator = StoryGenerator(
            world_bible=world_bible_obj,
            task_id=worldview_task_id,
            max_depth=max_depth,
            save_to_db=True
        )

        # 生成大纲（调用initialize_story会生成大纲并保存到数据库）
        # 注意：initialize_story会生成根节点，但我们只需要大纲
        # 所以直接调用_generate_global_outline
        generator._generate_global_outline()

        # 检查是否为占位符大纲
        if getattr(generator, 'is_placeholder_outline', False):
            print("❌ 生成的是占位符大纲，而非基于世界观生成的大纲")
            print("   请设置 STORY_PLANNER_LLM_API_KEY 以通过 farmmx GPT-5.6-SOL 中转生成大纲")
            raise RuntimeError("无法生成基于世界观的大纲：LLM API key未设置")

        # 准备大纲数据（global_outline字典）
        outline_data = {}
        for key, value in generator.global_outline.items():
            if isinstance(key, int):
                outline_data[str(key)] = value
            else:
                outline_data[key] = value

        # 保存大纲到数据库（通过outline_to_db模块）
        outline_record = save_outline_directly(
            outline_data=outline_data,
            worldview_task_id=worldview_task_id,
            overwrite=False
        )

        if not outline_record:
            raise RuntimeError("大纲保存到数据库失败")

        # 返回大纲task_id和chapters数据（data字段直接存储章节内容）
        chapters = outline_record.data
        print(f"✅ 新大纲生成并保存成功，task_id: {outline_record.task_id}")
        print(f"   章节数: {len(chapters)}")
        # outline_record.task_id 现在就是 worldview_task_id
        return outline_record.task_id, chapters

    except ImportError as e:
        print(f"❌ 导入模块失败: {e}")
        print(f"   请确保项目结构正确，src和table模块可访问")
        raise
    except Exception as e:
        print(f"❌ 大纲生成失败: {e}")
        import traceback
        traceback.print_exc()
        raise


def main():
    """命令行接口（测试用）"""
    import argparse
    import json

    parser = argparse.ArgumentParser(description="大纲获取工具")
    parser.add_argument("worldview_task_id", help="世界观的task_id")
    parser.add_argument("--world-bible-file", help="world_bible.json文件路径（用于生成新大纲）")
    parser.add_argument("--output", help="输出大纲JSON文件路径（可选）")

    args = parser.parse_args()

    world_bible = None
    if args.world_bible_file:
        # 加载world_bible文件
        try:
            with open(args.world_bible_file, 'r', encoding='utf-8') as f:
                world_bible_data = json.load(f)
            world_bible = world_bible_data
            print(f"📁 加载world_bible文件: {args.world_bible_file}")
        except Exception as e:
            print(f"❌ 加载world_bible文件失败: {e}")
            sys.exit(1)

    try:
        outline_task_id, chapters = fetch_outline(
            worldview_task_id=args.worldview_task_id,
            world_bible=world_bible
        )

        print(f"\n✅ 大纲获取成功!")
        print(f"   大纲task_id: {outline_task_id}")
        print(f"   章节数: {len(chapters)}")

        # 输出大纲内容预览
        print("\n📝 大纲预览:")
        for key in sorted(chapters.keys(), key=lambda x: (int(x.split('-')[0]) if '-' in x else int(x), x)):
            content = chapters[key]
            preview = content[:100] + "..." if len(content) > 100 else content
            print(f"   {key}: {preview}")

        # 如果指定了输出文件，保存大纲
        if args.output:
            with open(args.output, 'w', encoding='utf-8') as f:
                json.dump(chapters, f, ensure_ascii=False, indent=2)
            print(f"\n💾 大纲已保存到: {args.output}")

    except Exception as e:
        print(f"❌ 大纲获取失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
