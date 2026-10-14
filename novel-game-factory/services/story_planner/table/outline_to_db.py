#!/usr/bin/env python3
"""
故事大纲数据入库模块 (outline_to_db.py)

负责将 global_outline.json 格式的大纲数据保存到数据库。
支持自动识别章节类型、编号、结局变体等。
"""

import os
import sys
import json
import re
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime

# 添加项目根目录到路径，确保能找到相关模块
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from sqlmodel import Session, select, func
from .models import Outline, Worldview, OutlineStatus, engine


def parse_chapter_key(chapter_key: str) -> tuple:
    """
    解析章节键，返回 (chapter_number, chapter_type)

    Args:
        chapter_key: 章节键，如 "1", "2", "8-ending-happy"

    Returns:
        tuple: (章节编号, 章节类型)
    """
    chapter_key = chapter_key.strip()

    # 检查是否为结局变体
    if "-ending-" in chapter_key:
        # 如 "8-ending-happy"
        match = re.match(r'(\d+)-ending-(\w+)', chapter_key)
        if match:
            chapter_num = int(match.group(1))
            ending_type = match.group(2)
            return chapter_num, "ending"
        else:
            # 其他格式的结局
            match = re.match(r'(\d+)', chapter_key)
            if match:
                return int(match.group(1)), "ending"
            else:
                return 0, "ending"

    # 检查是否为纯数字
    if chapter_key.isdigit():
        return int(chapter_key), "chapter"

    # 其他情况
    match = re.match(r'(\d+)', chapter_key)
    if match:
        return int(match.group(1)), "chapter"

    return 0, "chapter"


def extract_chapter_title(content: str) -> Optional[str]:
    """
    从内容中提取章节标题（第一句话或前50个字符）

    Args:
        content: 章节内容

    Returns:
        提取的标题或None
    """
    if not content:
        return None

    # 取第一句话（以句号、问号、感叹号分割）
    sentences = re.split(r'[。！？]', content.strip())
    if sentences and sentences[0]:
        first_sentence = sentences[0].strip()
        # 限制长度
        if len(first_sentence) > 100:
            return first_sentence[:97] + "..."
        return first_sentence

    # 如果没有句子分隔符，取前50个字符
    return content[:50].strip() + "..." if len(content) > 50 else content.strip()


def save_outline_directly(
    outline_data: Dict[str, str],
    worldview_id: Optional[int] = None,
    worldview_task_id: Optional[str] = None,
    overwrite: bool = False
) -> Optional[Outline]:
    """
    直接将大纲字典数据保存到数据库（不经过文件）
    现在将整个大纲存储为单个记录，data字段包含所有章节

    Args:
        outline_data: 大纲字典 {章节键: 内容}
        worldview_id: 关联的世界观ID
        worldview_task_id: 关联的世界观task_id（如果未提供worldview_id，则通过task_id查找）
        overwrite: 是否覆盖同版本已存在的大纲

    Returns:
        Outline: 成功保存的大纲记录，失败返回None
    """
    try:
        print(f"💾 直接保存大纲到数据库（单个记录）")
        print(f"   章节数量: {len(outline_data)}")

        # 如果提供了worldview_task_id，查找对应的worldview_id
        if worldview_task_id and not worldview_id:
            with Session(engine) as session:
                worldview = session.query(Worldview).filter(
                    Worldview.task_id == worldview_task_id
                ).first()
                if worldview:
                    worldview_id = worldview.id
                    print(f"   关联世界观: {worldview.task_id} (ID: {worldview_id})")
                else:
                    print(f"⚠️  未找到世界观 task_id={worldview_task_id}")

        # 优先使用世界观的task_id作为大纲的task_id，确保它们相同
        # 如果提供了worldview_task_id，直接使用
        # 如果没有提供worldview_task_id但提供了worldview_id，通过worldview_id查找
        # 如果都没有提供，则报错（大纲必须关联到世界观）
        task_id = None
        if worldview_task_id:
            task_id = worldview_task_id
            print(f"   使用提供的世界观task_id作为大纲task_id: {task_id}")
        elif worldview_id:
            # 通过worldview_id查找worldview记录，获取其task_id
            with Session(engine) as session:
                worldview = session.get(Worldview, worldview_id)
                if worldview:
                    task_id = worldview.task_id
                    print(f"   通过worldview_id找到世界观task_id作为大纲task_id: {task_id}")
                else:
                    raise ValueError(f"未找到ID为{worldview_id}的世界观记录")
        else:
            raise ValueError("大纲必须关联到世界观，请提供worldview_task_id或worldview_id参数。task_id应该是全局的，世界观和大纲使用相同的task_id。")

        # 注意：不再生成新的大纲task_id，始终使用世界观的task_id

        # 准备data字段：直接存储章节数据（与本地JSON文件格式一致）
        data = outline_data
        # 不再需要单独存储worldview_task_id在data中，因为task_id字段已经是世界观task_id

        # 保存到数据库
        with Session(engine) as session:
            # 检查是否已存在相同task_id的大纲记录
            existing = session.query(Outline).filter(
                Outline.task_id == task_id
            ).first()

            if existing:
                if not overwrite:
                    print(f"⚠️  大纲已存在: task_id={task_id}")
                    print(f"   返回现有记录 (overwrite=False)")
                    return existing
                else:
                    # 覆盖：更新现有记录的数据
                    print(f"🔄  覆盖现有大纲记录: task_id={task_id}")
                    existing.data = data
                    # 状态保持原样（不重置为DRAFT）
                    session.commit()
                    session.refresh(existing)
                    outline = existing
            else:
                # 创建新的大纲记录
                outline = Outline(
                    task_id=task_id,
                    data=data,
                    status=0  # DRAFT，等待前端确认
                )
                session.add(outline)
                session.commit()
                session.refresh(outline)

        print(f"✅ 大纲直接保存成功!")
        print(f"   task_id: {task_id}")
        print(f"   章节数: {len(outline_data)}")
        print(f"   世界观ID: {worldview_id}")

        return outline

    except Exception as e:
        print(f"❌ 大纲直接保存失败: {e}")
        import traceback
        traceback.print_exc()
        return None


def import_outline_json_to_db(
    json_path: Path,
    worldview_id: Optional[int] = None,
    worldview_task_id: Optional[str] = None,
    overwrite: bool = False
) -> Optional[Outline]:
    """
    从 JSON 文件导入故事大纲数据到数据库
    现在将整个大纲存储为单个记录

    Args:
        json_path: JSON 文件路径
        worldview_id: 关联的世界观ID
        worldview_task_id: 关联的世界观task_id（如果未提供worldview_id，则通过task_id查找）
        overwrite: 是否覆盖同版本已存在的大纲

    Returns:
        Outline: 成功导入的大纲记录，失败返回None
    """
    try:
        # 读取 JSON 文件
        with open(json_path, 'r', encoding='utf-8') as f:
            outline_data = json.load(f)

        print(f"📁 读取大纲文件: {json_path}")
        print(f"   章节数量: {len(outline_data)}")

        # 调用save_outline_directly进行保存
        outline = save_outline_directly(
            outline_data=outline_data,
            worldview_id=worldview_id,
            worldview_task_id=worldview_task_id,
            overwrite=overwrite
        )

        if outline:
            print(f"✅ 大纲导入成功!")
            print(f"   task_id: {outline.task_id}")
            print(f"   章节数: {len(outline_data)}")
            return outline
        else:
            print(f"❌ 大纲导入失败")
            return None

    except Exception as e:
        print(f"❌ 大纲导入失败: {e}")
        import traceback
        traceback.print_exc()
        return None


def import_directory_outlines(
    directory_path: Path,
    recursive: bool = False,
    worldview_task_id: Optional[str] = None,
    overwrite: bool = False
) -> Dict[str, Optional[Outline]]:
    """
    导入目录中的所有大纲JSON文件

    Args:
        directory_path: 目录路径
        recursive: 是否递归搜索
        worldview_task_id: 关联的世界观task_id
        overwrite: 是否覆盖

    Returns:
        导入结果字典 {文件路径: 大纲记录}
    """
    results = {}

    print(f"📂 扫描目录: {directory_path}")

    # 查找JSON文件
    json_files = []
    if recursive:
        for json_file in directory_path.rglob("global_outline.json"):
            json_files.append(json_file)
    else:
        for json_file in directory_path.glob("global_outline.json"):
            json_files.append(json_file)
        # 检查一级子目录
        for subdir in directory_path.iterdir():
            if subdir.is_dir():
                for json_file in subdir.glob("global_outline.json"):
                    json_files.append(json_file)

    print(f"   找到 {len(json_files)} 个大纲文件")

    for json_file in json_files:
        print(f"\n📄 处理文件: {json_file}")

        outline = import_outline_json_to_db(
            json_path=json_file,
            worldview_task_id=worldview_task_id,
            overwrite=overwrite
        )

        results[str(json_file)] = outline

    return results


def list_outlines(
    worldview_id: Optional[int] = None
) -> List[Outline]:
    """
    查询大纲列表

    Args:
        worldview_id: 世界观ID（暂不支持，已移除该字段）

    Returns:
        大纲记录列表
    """
    try:
        with Session(engine) as session:
            query = session.query(Outline)

            outlines = query.order_by(Outline.create_time.desc()).all()
            return outlines

    except Exception as e:
        print(f"❌ 查询失败: {e}")
        return []


def get_outline_by_id(outline_id: int, include_archived: bool = False) -> Optional[Outline]:
    """
    根据ID获取大纲记录

    Args:
        outline_id: 大纲ID
        include_archived: 是否包含已归档的大纲（默认 False）

    Returns:
        Outline 或 None
    """
    try:
        with Session(engine) as session:
            outline = session.get(Outline, outline_id)
            if not outline:
                return None

            # 如果不包含已归档的，且状态是 ARCHIVED，则返回 None
            if not include_archived and outline.status == OutlineStatus.ARCHIVED:
                return None

            return outline

    except Exception as e:
        print(f"❌ 获取大纲失败: {e}")
        return None


# ==================== 增删改查功能扩展 ====================

# ---------- 改（更新） ----------
def update_outline_data(
    outline_id: int,
    new_data: Dict[str, Any],
    allow_update_archived: bool = False
) -> Optional[Outline]:
    """
    更新大纲数据

    Args:
        outline_id: 大纲数据库 ID
        new_data: 新的大纲数据（章节字典）
        allow_update_archived: 是否允许更新已归档的大纲（默认 False）

    Returns:
        更新后的 Outline 对象或 None
    """
    try:
        with Session(engine) as session:
            outline = session.get(Outline, outline_id)
            if not outline:
                print(f"❌ 未找到大纲 ID={outline_id}")
                return None

            # 检查是否允许更新已归档的大纲
            if outline.status == OutlineStatus.ARCHIVED and not allow_update_archived:
                print(f"❌ 大纲 ID={outline_id} 已归档，不允许更新")
                return None

            # 更新数据
            outline.data = new_data

            session.commit()
            session.refresh(outline)

            print(f"✅ 大纲数据更新成功 ID={outline_id}")
            print(f"   章节数: {len(new_data)}")
            return outline

    except Exception as e:
        print(f"❌ 更新大纲数据失败: {e}")
        import traceback
        traceback.print_exc()
        return None


def update_outline_status(
    outline_id: int,
    new_status: int,
    validate_transition: bool = True
) -> Optional[Outline]:
    """
    更新大纲状态

    Args:
        outline_id: 大纲数据库 ID
        new_status: 新的状态
        validate_transition: 是否验证状态转换合法性（默认 True）

    Returns:
        更新后的 Outline 对象或 None
    """
    try:
        with Session(engine) as session:
            outline = session.get(Outline, outline_id)
            if not outline:
                print(f"❌ 未找到大纲 ID={outline_id}")
                return None

            current_status = outline.status

            # 验证状态转换（如果需要）
            if validate_transition:
                valid_transitions = {
                    OutlineStatus.DRAFT: [OutlineStatus.CONFIRMED, OutlineStatus.ARCHIVED],
                    OutlineStatus.CONFIRMED: [OutlineStatus.DRAFT, OutlineStatus.ARCHIVED],
                    OutlineStatus.ARCHIVED: [OutlineStatus.DRAFT, OutlineStatus.CONFIRMED]
                }

                if new_status not in valid_transitions.get(current_status, []):
                    print(f"❌ 不允许从状态 {current_status} 转换到 {new_status}")
                    print(f"   允许的转换: {valid_transitions.get(current_status, [])}")
                    return None

            # 更新状态
            outline.status = new_status
            session.commit()
            session.refresh(outline)

            status_names = {
                OutlineStatus.DRAFT: "DRAFT",
                OutlineStatus.CONFIRMED: "CONFIRMED",
                OutlineStatus.ARCHIVED: "ARCHIVED"
            }
            print(f"✅ 大纲状态更新成功 ID={outline_id}: "
                  f"{status_names.get(current_status, current_status)} -> "
                  f"{status_names.get(new_status, new_status)}")
            return outline

    except Exception as e:
        print(f"❌ 更新大纲状态失败: {e}")
        import traceback
        traceback.print_exc()
        return None


def patch_outline_chapter(
    outline_id: int,
    chapter_key: str,
    new_content: str,
    allow_update_archived: bool = False
) -> Optional[Outline]:
    """
    更新大纲中的单个章节内容

    Args:
        outline_id: 大纲数据库 ID
        chapter_key: 章节键（如 "1", "2", "8-ending-happy"）
        new_content: 新的章节内容
        allow_update_archived: 是否允许更新已归档的大纲（默认 False）

    Returns:
        更新后的 Outline 对象或 None
    """
    try:
        with Session(engine) as session:
            outline = session.get(Outline, outline_id)
            if not outline:
                print(f"❌ 未找到大纲 ID={outline_id}")
                return None

            # 检查是否允许更新已归档的大纲
            if outline.status == OutlineStatus.ARCHIVED and not allow_update_archived:
                print(f"❌ 大纲 ID={outline_id} 已归档，不允许更新")
                return None

            # 复制当前数据
            data = outline.data.copy()

            # 检查章节是否存在
            if chapter_key not in data:
                print(f"⚠️  章节 '{chapter_key}' 不存在，将创建新章节")

            # 更新章节
            data[chapter_key] = new_content
            outline.data = data

            session.commit()
            session.refresh(outline)

            print(f"✅ 章节更新成功 ID={outline_id}, 章节: {chapter_key}")
            print(f"   内容预览: {new_content[:100]}...")
            return outline

    except Exception as e:
        print(f"❌ 更新章节失败: {e}")
        import traceback
        traceback.print_exc()
        return None


def delete_outline_chapter(
    outline_id: int,
    chapter_key: str,
    allow_update_archived: bool = False
) -> Optional[Outline]:
    """
    删除大纲中的单个章节

    Args:
        outline_id: 大纲数据库 ID
        chapter_key: 章节键（如 "1", "2", "8-ending-happy"）
        allow_update_archived: 是否允许更新已归档的大纲（默认 False）

    Returns:
        更新后的 Outline 对象或 None
    """
    try:
        with Session(engine) as session:
            outline = session.get(Outline, outline_id)
            if not outline:
                print(f"❌ 未找到大纲 ID={outline_id}")
                return None

            # 检查是否允许更新已归档的大纲
            if outline.status == OutlineStatus.ARCHIVED and not allow_update_archived:
                print(f"❌ 大纲 ID={outline_id} 已归档，不允许更新")
                return None

            # 复制当前数据
            data = outline.data.copy()

            # 检查章节是否存在
            if chapter_key not in data:
                print(f"❌ 章节 '{chapter_key}' 不存在")
                return None

            # 删除章节
            del data[chapter_key]
            outline.data = data

            session.commit()
            session.refresh(outline)

            print(f"✅ 章节删除成功 ID={outline_id}, 章节: {chapter_key}")
            print(f"   剩余章节数: {len(data)}")
            return outline

    except Exception as e:
        print(f"❌ 删除章节失败: {e}")
        import traceback
        traceback.print_exc()
        return None


# ---------- 删（软删除） ----------
def soft_delete_outline(outline_id: int) -> bool:
    """
    软删除大纲（将状态改为 ARCHIVED）

    Args:
        outline_id: 大纲数据库 ID

    Returns:
        bool: 成功 True，失败 False
    """
    try:
        with Session(engine) as session:
            outline = session.get(Outline, outline_id)
            if not outline:
                print(f"❌ 未找到大纲 ID={outline_id}")
                return False

            if outline.status == OutlineStatus.ARCHIVED:
                print(f"⚠️  大纲 ID={outline_id} 已经是 ARCHIVED 状态")
                return True

            outline.status = OutlineStatus.ARCHIVED
            session.commit()

            print(f"✅ 大纲 ID={outline_id} 已软删除（状态改为 ARCHIVED）")
            return True

    except Exception as e:
        print(f"❌ 软删除失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def restore_outline(outline_id: int) -> bool:
    """
    恢复软删除的大纲（将状态改为 DRAFT）

    Args:
        outline_id: 大纲数据库 ID

    Returns:
        bool: 成功 True，失败 False
    """
    try:
        with Session(engine) as session:
            outline = session.get(Outline, outline_id)
            if not outline:
                print(f"❌ 未找到大纲 ID={outline_id}")
                return False

            if outline.status != OutlineStatus.ARCHIVED:
                print(f"⚠️  大纲 ID={outline_id} 状态不是 ARCHIVED，无法恢复")
                return False

            outline.status = OutlineStatus.DRAFT
            session.commit()

            print(f"✅ 大纲 ID={outline_id} 已恢复（状态改为 DRAFT）")
            return True

    except Exception as e:
        print(f"❌ 恢复失败: {e}")
        import traceback
        traceback.print_exc()
        return False


# ---------- 查（搜索和统计） ----------
def search_outlines(
    worldview_task_id: Optional[str] = None,
    status: Optional[int] = None,
    include_archived: bool = False,
    limit: int = 50
) -> List[Outline]:
    """
    搜索大纲

    Args:
        worldview_task_id: 关联的世界观task_id
        status: 状态筛选
        include_archived: 是否包含已归档的大纲
        limit: 返回记录数限制

    Returns:
        大纲列表
    """
    try:
        with Session(engine) as session:
            statement = select(Outline)

            # 状态筛选
            if status is not None:
                statement = statement.where(Outline.status == status)
            elif not include_archived:
                statement = statement.where(Outline.status != OutlineStatus.ARCHIVED)

            # 世界观task_id精确匹配
            if worldview_task_id:
                statement = statement.where(Outline.task_id == worldview_task_id)

            # 排序和分页
            statement = statement.order_by(Outline.create_time.desc()).limit(limit)

            results = session.exec(statement)
            return list(results.all())

    except Exception as e:
        print(f"❌ 搜索大纲失败: {e}")
        import traceback
        traceback.print_exc()
        return []


def count_outlines(
    include_archived: bool = False,
    status: Optional[int] = None
) -> int:
    """
    统计大纲数量

    Args:
        include_archived: 是否包含已归档的大纲
        status: 按状态筛选

    Returns:
        大纲数量
    """
    try:
        with Session(engine) as session:
            statement = select(func.count(Outline.id))

            # 状态筛选
            if status is not None:
                statement = statement.where(Outline.status == status)
            elif not include_archived:
                statement = statement.where(Outline.status != OutlineStatus.ARCHIVED)

            result = session.exec(statement).one()
            return result

    except Exception as e:
        print(f"❌ 统计大纲数量失败: {e}")
        return 0


def get_outline_by_task_id(task_id: str, include_archived: bool = False) -> Optional[Outline]:
    """
    根据 task_id 获取大纲

    Args:
        task_id: 大纲的 task_id
        include_archived: 是否包含已归档的大纲（默认 False）

    Returns:
        Outline 对象或 None
    """
    try:
        with Session(engine) as session:
            statement = select(Outline).where(Outline.task_id == task_id)

            if not include_archived:
                statement = statement.where(Outline.status != OutlineStatus.ARCHIVED)

            outline = session.exec(statement).first()
            return outline

    except Exception as e:
        print(f"❌ 获取大纲失败: {e}")
        return None


# ==================== 命令行接口 ====================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="故事大纲数据入库工具")
    subparsers = parser.add_subparsers(dest="command", help="命令")

    # 导入JSON文件命令
    import_parser = subparsers.add_parser("import", help="导入JSON文件到数据库")
    import_parser.add_argument("json_file", help="global_outline.json 文件路径")
    import_parser.add_argument("--worldview-id", type=int, help="关联的世界观ID")
    import_parser.add_argument("--worldview-task-id", help="关联的世界观task_id")
    import_parser.add_argument("--overwrite", action="store_true", help="覆盖已存在的版本")

    # 导入目录命令
    import_dir_parser = subparsers.add_parser("import-dir", help="导入目录中的所有大纲文件")
    import_dir_parser.add_argument("directory", help="包含global_outline.json文件的目录")
    import_dir_parser.add_argument("--recursive", "-r", action="store_true", help="递归搜索")
    import_dir_parser.add_argument("--worldview-task-id", help="关联的世界观task_id")
    import_dir_parser.add_argument("--overwrite", action="store_true", help="覆盖已存在的版本")

    # 列表命令
    list_parser = subparsers.add_parser("list", help="查询大纲列表")
    list_parser.add_argument("--worldview-id", type=int, help="世界观ID")

    # 查看命令
    get_parser = subparsers.add_parser("get", help="查看大纲详情")
    get_parser.add_argument("id", type=int, help="大纲记录ID")

    # 更新数据命令
    update_parser = subparsers.add_parser("update", help="更新大纲数据")
    update_parser.add_argument("id", type=int, help="大纲数据库 ID")
    update_parser.add_argument("--json-file", required=True, help="包含新数据的JSON文件路径")
    update_parser.add_argument("--allow-update-archived", action="store_true",
                              help="允许更新已归档的大纲")

    # 更新状态命令
    status_parser = subparsers.add_parser("status", help="更新大纲状态")
    status_parser.add_argument("id", type=int, help="大纲数据库 ID")
    status_parser.add_argument("new_status", type=int, choices=[0, 1, 2],
                              help="新状态: 0-DRAFT, 1-CONFIRMED, 2-ARCHIVED")
    status_parser.add_argument("--no-validate", action="store_true",
                              help="不验证状态转换合法性")

    # 更新章节命令
    patch_parser = subparsers.add_parser("patch-chapter", help="更新或添加单个章节")
    patch_parser.add_argument("id", type=int, help="大纲数据库 ID")
    patch_parser.add_argument("chapter_key", help="章节键（如 1, 2, 8-ending-happy）")
    patch_parser.add_argument("content", help="新的章节内容")
    patch_parser.add_argument("--allow-update-archived", action="store_true",
                            help="允许更新已归档的大纲")

    # 删除章节命令
    delete_chapter_parser = subparsers.add_parser("delete-chapter", help="删除单个章节")
    delete_chapter_parser.add_argument("id", type=int, help="大纲数据库 ID")
    delete_chapter_parser.add_argument("chapter_key", help="章节键（如 1, 2, 8-ending-happy）")
    delete_chapter_parser.add_argument("--allow-update-archived", action="store_true",
                                      help="允许更新已归档的大纲")

    # 软删除命令
    soft_delete_parser = subparsers.add_parser("soft-delete", help="软删除大纲")
    soft_delete_parser.add_argument("id", type=int, help="大纲数据库 ID")

    # 恢复命令
    restore_parser = subparsers.add_parser("restore", help="恢复软删除的大纲")
    restore_parser.add_argument("id", type=int, help="大纲数据库 ID")

    # 搜索命令
    search_parser = subparsers.add_parser("search", help="搜索大纲")
    search_parser.add_argument("--worldview-task-id", help="世界观task_id")
    search_parser.add_argument("--status", type=int, choices=[0, 1, 2],
                              help="按状态筛选: 0-DRAFT, 1-CONFIRMED, 2-ARCHIVED")
    search_parser.add_argument("--include-archived", action="store_true",
                              help="包含已归档的大纲")
    search_parser.add_argument("--limit", type=int, default=20,
                              help="返回记录数限制 (默认: 20)")

    # 统计命令
    count_parser = subparsers.add_parser("count", help="统计大纲数量")
    count_parser.add_argument("--include-archived", action="store_true",
                            help="包含已归档的大纲")
    count_parser.add_argument("--status", type=int, choices=[0, 1, 2],
                            help="按状态筛选: 0-DRAFT, 1-CONFIRMED, 2-ARCHIVED")

    args = parser.parse_args()

    if args.command == "import":
        json_path = Path(args.json_file)
        if not json_path.exists():
            print(f"❌ 文件不存在: {json_path}")
            sys.exit(1)

        outlines = import_outline_json_to_db(
            json_path=json_path,
            worldview_id=args.worldview_id,
            worldview_task_id=args.worldview_task_id,
            overwrite=args.overwrite
        )

        if outlines:
            data = outlines.data
            chapters = data.get('chapters', {})
            print(f"\n✅ 导入成功！")
            print(f"   Task ID: {outlines.task_id}")
            print(f"   章节数: {len(chapters)}")
        else:
            print("\n❌ 导入失败")
            sys.exit(1)

    elif args.command == "import-dir":
        directory = Path(args.directory)
        if not directory.exists():
            print(f"❌ 目录不存在: {directory}")
            sys.exit(1)

        results = import_directory_outlines(
            directory_path=directory,
            recursive=args.recursive,
            worldview_task_id=args.worldview_task_id,
            overwrite=args.overwrite
        )

        successful_imports = [outline for outline in results.values() if outline is not None]
        total_chapters = sum(len(outline.data) for outline in successful_imports)  # data字段直接存储章节内容
        print(f"\n✅ 目录导入完成!")
        print(f"   处理文件数: {len(results)}")
        print(f"   成功导入: {len(successful_imports)} 个大纲")
        print(f"   总章节数: {total_chapters}")

    elif args.command == "list":
        outlines = list_outlines(
            worldview_id=args.worldview_id
        )

        if outlines:
            print(f"\n📋 大纲列表 ({len(outlines)} 条记录):")
            print("=" * 80)

            for outline in outlines:
                # data字段直接存储章节内容
                chapters = outline.data
                chapter_count = len(chapters)

                # 获取第一个章节的预览
                first_chapter_preview = ""
                if chapters:
                    first_key = next(iter(chapters))
                    first_content = chapters[first_key]
                    first_chapter_preview = first_content[:50] + "..." if len(first_content) > 50 else first_content

                print(f"\n📚 ID: {outline.id} | Task ID: {outline.task_id}")
                print(f"   章节数: {chapter_count}")
                print(f"   状态: {outline.status} | 创建时间: {outline.create_time}")
                if first_chapter_preview:
                    print(f"   示例章节: {first_chapter_preview}")
                print("-" * 80)
        else:
            print("❌ 未找到大纲记录")

    elif args.command == "get":
        outline = get_outline_by_id(args.id)
        if outline:
            # data字段直接存储章节内容
            chapters = outline.data

            print(f"\n📋 大纲详情 (ID: {outline.id}):")
            print("=" * 60)
            print(f"  Task ID: {outline.task_id}")
            print(f"  状态: {outline.status} ({OutlineStatus.get_description(outline.status)})")
            print(f"  创建时间: {outline.create_time}")
            print(f"  更新时间: {outline.update_time}")
            print(f"  章节数量: {len(chapters)}")
            print(f"\n📝 章节列表:")
            print("-" * 60)

            for chapter_key, content in chapters.items():
                # 解析章节信息
                chapter_number, chapter_type = parse_chapter_key(chapter_key)
                chapter_title = extract_chapter_title(content)
                print(f"\n  【{chapter_key}】类型: {chapter_type}, 编号: {chapter_number}")
                print(f"  标题: {chapter_title or '(无)'}")
                print(f"  内容预览: {content[:100]}..." if len(content) > 100 else f"  内容: {content}")
                print("-" * 40)
        else:
            print(f"❌ 未找到大纲 ID={args.id}")
            sys.exit(1)

    elif args.command == "update":
        try:
            with open(args.json_file, 'r', encoding='utf-8') as f:
                new_data = json.load(f)
        except Exception as e:
            print(f"❌ 读取JSON文件失败: {e}")
            sys.exit(1)

        outline = update_outline_data(
            outline_id=args.id,
            new_data=new_data,
            allow_update_archived=args.allow_update_archived
        )

        if outline:
            print(f"\n✅ 大纲数据更新成功!")
            print(f"   大纲ID: {outline.id}")
            print(f"   task_id: {outline.task_id}")
            print(f"   章节数: {len(outline.data)}")
        else:
            print("\n❌ 大纲数据更新失败")
            sys.exit(1)

    elif args.command == "status":
        outline = update_outline_status(
            outline_id=args.id,
            new_status=args.new_status,
            validate_transition=not args.no_validate
        )

        if outline:
            print(f"\n✅ 大纲状态更新成功!")
            print(f"   大纲ID: {outline.id}")
            print(f"   task_id: {outline.task_id}")
        else:
            print("\n❌ 大纲状态更新失败")
            sys.exit(1)

    elif args.command == "patch-chapter":
        outline = patch_outline_chapter(
            outline_id=args.id,
            chapter_key=args.chapter_key,
            new_content=args.content,
            allow_update_archived=args.allow_update_archived
        )

        if outline:
            print(f"\n✅ 章节更新成功!")
        else:
            print("\n❌ 章节更新失败")
            sys.exit(1)

    elif args.command == "delete-chapter":
        outline = delete_outline_chapter(
            outline_id=args.id,
            chapter_key=args.chapter_key,
            allow_update_archived=args.allow_update_archived
        )

        if outline:
            print(f"\n✅ 章节删除成功!")
        else:
            print("\n❌ 章节删除失败")
            sys.exit(1)

    elif args.command == "soft-delete":
        success = soft_delete_outline(args.id)
        if not success:
            sys.exit(1)

    elif args.command == "restore":
        success = restore_outline(args.id)
        if not success:
            sys.exit(1)

    elif args.command == "search":
        outlines = search_outlines(
            worldview_task_id=args.worldview_task_id,
            status=args.status,
            include_archived=args.include_archived,
            limit=args.limit
        )

        print(f"\n🔍 搜索到 {len(outlines)} 个大纲:")
        if args.worldview_task_id:
            print(f"   世界观task_id: {args.worldview_task_id}")
        if args.status is not None:
            status_names = {0: "DRAFT", 1: "CONFIRMED", 2: "ARCHIVED"}
            print(f"   状态: {status_names.get(args.status, '未知')}")

        print("-" * 80)
        for i, outline in enumerate(outlines):
            status_desc = OutlineStatus.get_description(outline.status)
            print(f"{i+1:3d}. ID: {outline.id:4d} | task_id: {outline.task_id:30s}")
            print(f"     状态: {status_desc:10s} | 章节数: {len(outline.data):3d} | "
                  f"创建: {outline.create_time.strftime('%Y-%m-%d %H:%M')}")
            print("-" * 80)

    elif args.command == "count":
        count = count_outlines(
            include_archived=args.include_archived,
            status=args.status
        )

        print(f"\n📊 大纲统计:")
        print(f"   总数: {count}")

        if args.status is not None:
            status_names = {0: "DRAFT", 1: "CONFIRMED", 2: "ARCHIVED"}
            print(f"   状态 '{status_names.get(args.status, '未知')}': {count}")
        elif not args.include_archived:
            print(f"   (不包含已归档的大纲)")

    else:
        parser.print_help()