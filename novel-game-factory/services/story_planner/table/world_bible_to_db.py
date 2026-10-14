#!/usr/bin/env python3
"""
世界观数据入库模块 (world_bible_to_db.py)

负责将 world_bible 数据保存到数据库，统一处理 task_id 生成、艺术风格映射、状态管理等。
支持软删除（status=WorldviewStatus.ARCHIVED）。
"""

import os
import sys
import json
import re
import uuid
import random
import string
from pathlib import Path
from typing import Dict, Any, Optional, Union
from datetime import datetime

# 添加项目根目录到路径，确保能找到相关模块
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from sqlmodel import Session
from .models import Worldview, WorldviewStatus, engine
try:
    from pypinyin import lazy_pinyin
    PYPINYIN_AVAILABLE = True
except ImportError:
    PYPINYIN_AVAILABLE = False


# ==================== 工具函数（从 world_analyst 复制/适配） ====================

def generate_task_id() -> str:
    """生成任务ID: YYYYMMDD_HHMMSS_xxxx"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"{timestamp}_{suffix}"


def generate_task_id_from_project_name(project_name: str) -> str:
    """
    生成task_id: 12位uuid

    Args:
        project_name: 项目/小说名称（保留参数以兼容调用方，不再参与生成）

    Returns:
        str: task_id 格式: 12位uuid
    """
    return uuid.uuid4().hex[:12]


def map_art_style_to_code(art_style_str: str) -> int:
    """
    将艺术风格文字映射为数字代码

    Args:
        art_style_str: 艺术风格文字

    Returns:
        int: 1-仙侠古风, 2-科幻, 3-现代都市, 0-未知
    """
    if not art_style_str:
        return 0

    art_style_str = art_style_str.lower().strip()

    # 中文匹配
    if "仙侠" in art_style_str or "古风" in art_style_str or "xianxia" in art_style_str:
        return 1
    elif "科幻" in art_style_str or "sci-fi" in art_style_str or "science fiction" in art_style_str:
        return 2
    elif "现代" in art_style_str or "都市" in art_style_str or "modern" in art_style_str or "urban" in art_style_str:
        return 3
    else:
        return 0  # 未知类型


def validate_world_bible_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    验证 world_bible 数据结构，补充缺失字段

    Args:
        data: world_bible 数据字典

    Returns:
        验证并补充后的数据
    """
    # 必需字段列表（v3.6 增强版）
    required_fields = [
        'project_name', 'art_style', 'worldview', 'characters',
        'scenes', 'relationship_network', 'scene_graph', 'narrative_structure'
    ]

    result = data.copy()

    # 检查缺失字段并补充默认值
    missing = [f for f in required_fields if f not in result]
    if missing:
        print(f"⚠️  缺失字段: {missing}")
        # 为缺失字段添加空值
        for field in missing:
            if field in ['project_name', 'art_style']:
                result[field] = ""
            else:
                result[field] = {}

    return result


def create_storytree_folder(task_id: str) -> Path:
    """
    在storage/story_tree目录下创建以task_id为名的文件夹

    Args:
        task_id: 世界观任务ID

    Returns:
        Path: 创建的文件夹路径
    """
    # 项目根目录（table目录的父目录的父目录）
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    story_tree_dir = Path(project_root) / "storage" / "story_tree" / task_id
    story_tree_dir.mkdir(parents=True, exist_ok=True)
    print(f"📁 创建storytree目录: {story_tree_dir}")
    return story_tree_dir


# ==================== 核心入库函数 ====================

def save_worldview_to_db(
    world_bible_data: Union[Dict[str, Any], 'WorldBible'],
    status: int = WorldviewStatus.CONFIRMED,
    task_id: Optional[str] = None,
    art_style_code: Optional[int] = None,
    skip_duplicate_check: bool = False
) -> Optional[Worldview]:
    """
    将 world_bible 数据保存到数据库

    Args:
        world_bible_data: world_bible 数据，可以是字典或 WorldBible 对象
        status: 世界观状态（默认 CONFIRMED）
        task_id: 指定 task_id，如果为 None 则自动生成
        art_style_code: 指定艺术风格代码，如果为 None 则从数据中提取并映射
        skip_duplicate_check: 是否跳过重复 task_id 检查（默认 False）

    Returns:
        Worldview 对象（成功）或 None（失败）
    """
    try:
        # 转换数据为字典
        if hasattr(world_bible_data, 'to_dict'):
            # WorldBible 对象
            data_dict = world_bible_data.to_dict()
        else:
            # 字典类型
            data_dict = world_bible_data

        # 验证数据结构
        data_dict = validate_world_bible_data(data_dict)

        # 获取项目名称
        project_name = data_dict.get('project_name', '')
        if not project_name:
            # 如果数据中没有项目名称，使用默认值
            project_name = "未命名项目"
            data_dict['project_name'] = project_name

        # 生成 task_id（如果没有提供）
        if task_id is None:
            task_id = generate_task_id_from_project_name(project_name)

        # 确定艺术风格代码
        if art_style_code is None:
            art_style_str = data_dict.get('art_style', '')
            art_style_code = map_art_style_to_code(art_style_str)

        # 插入数据库
        with Session(engine) as session:
            # 检查是否已存在相同的 task_id（除非跳过检查）
            if not skip_duplicate_check:
                existing = session.query(Worldview).filter(Worldview.task_id == task_id).first()
                if existing:
                    print(f"⚠️  task_id已存在: {task_id}")
                    # 生成新的 task_id（使用相同项目名称但新的UUID）
                    task_id = generate_task_id_from_project_name(project_name)
                    print(f"   使用新的 task_id: {task_id}")

            # 创建世界观记录
            worldview = Worldview(
                task_id=task_id,
                data=data_dict,
                art_style=art_style_code,
                status=status,
            )

            session.add(worldview)
            session.commit()
            session.refresh(worldview)

            print(f"✅ 世界观入库成功!")
            print(f"   数据库ID: {worldview.id}")
            print(f"   task_id: {task_id}")
            print(f"   项目名称: {project_name}")
            print(f"   艺术风格代码: {art_style_code}")
            print(f"   状态: {status} ({WorldviewStatus.get_description(status)})")

            # 创建storytree文件夹
            try:
                storytree_path = create_storytree_folder(task_id)
                print(f"   storytree目录: {storytree_path}")
            except Exception as e:
                print(f"⚠️  创建storytree目录失败: {e}")
                # 不阻止返回，文件夹创建不是关键操作

            return worldview

    except Exception as e:
        print(f"❌ 世界观入库失败: {e}")
        import traceback
        traceback.print_exc()
        return None


def import_json_file_to_db(
    json_path: Union[str, Path],
    status: int = WorldviewStatus.CONFIRMED,
    task_id: Optional[str] = None,
    art_style_code: Optional[int] = None
) -> Optional[Worldview]:
    """
    从 JSON 文件导入 world_bible 数据到数据库

    Args:
        json_path: JSON 文件路径
        status: 世界观状态（默认 CONFIRMED）
        task_id: 指定 task_id
        art_style_code: 指定艺术风格代码

    Returns:
        Worldview 对象（成功）或 None（失败）
    """
    try:
        # 读取 JSON 文件
        with open(json_path, 'r', encoding='utf-8') as f:
            json_data = json.load(f)

        print(f"📁 读取文件: {json_path}")
        print(f"   项目名称: {json_data.get('project_name', '未知')}")
        print(f"   艺术风格: {json_data.get('art_style', '未知')}")

        # 使用主函数保存到数据库
        return save_worldview_to_db(
            world_bible_data=json_data,
            status=status,
            task_id=task_id,
            art_style_code=art_style_code
        )

    except Exception as e:
        print(f"❌ JSON 文件导入失败: {e}")
        import traceback
        traceback.print_exc()
        return None


# ==================== 辅助函数（增删改查） ====================

# ---------- 增 ----------
# save_worldview_to_db() 已在上面定义

# ---------- 查 ----------
def get_worldview_by_id(worldview_id: int, include_archived: bool = False) -> Optional[Worldview]:
    """
    根据 ID 获取世界观

    Args:
        worldview_id: 世界观数据库 ID
        include_archived: 是否包含已归档的世界观（默认 False）

    Returns:
        Worldview 对象或 None
    """
    try:
        with Session(engine) as session:
            worldview = session.get(Worldview, worldview_id)
            if not worldview:
                return None

            # 如果不包含已归档的，且状态是 ARCHIVED，则返回 None
            if not include_archived and worldview.status == WorldviewStatus.ARCHIVED:
                return None

            return worldview

    except Exception as e:
        print(f"❌ 获取世界观失败: {e}")
        return None


def list_worldviews(
    include_archived: bool = False,
    status: Optional[int] = None,
    art_style: Optional[int] = None,
    limit: int = 100,
    offset: int = 0,
    order_by: str = "create_time_desc"  # 可选: create_time_desc, create_time_asc, update_time_desc, update_time_asc
) -> list[Worldview]:
    """
    列出世界观，支持过滤和分页

    Args:
        include_archived: 是否包含已归档的世界观（默认 False）
        status: 按状态筛选（如果提供，include_archived 参数无效）
        art_style: 按艺术风格代码筛选
        limit: 返回记录数限制
        offset: 偏移量（用于分页）
        order_by: 排序方式

    Returns:
        世界观列表
    """
    try:
        from sqlmodel import select

        with Session(engine) as session:
            statement = select(Worldview)

            # 状态筛选
            if status is not None:
                # 如果指定了状态，按状态筛选
                statement = statement.where(Worldview.status == status)
            elif not include_archived:
                # 默认排除已归档的
                statement = statement.where(Worldview.status != WorldviewStatus.ARCHIVED)

            # 艺术风格筛选
            if art_style is not None:
                statement = statement.where(Worldview.art_style == art_style)

            # 排序
            if order_by == "create_time_desc":
                statement = statement.order_by(Worldview.create_time.desc())
            elif order_by == "create_time_asc":
                statement = statement.order_by(Worldview.create_time.asc())
            elif order_by == "update_time_desc":
                statement = statement.order_by(Worldview.update_time.desc())
            elif order_by == "update_time_asc":
                statement = statement.order_by(Worldview.update_time.asc())
            else:
                statement = statement.order_by(Worldview.create_time.desc())

            # 分页
            statement = statement.offset(offset).limit(limit)

            results = session.exec(statement)
            return results.all()

    except Exception as e:
        print(f"❌ 列出世界观失败: {e}")
        return []


def search_worldviews(
    project_name: Optional[str] = None,
    art_style: Optional[int] = None,
    status: Optional[int] = None,
    include_archived: bool = False,
    limit: int = 50
) -> list[Worldview]:
    """
    搜索世界观（支持按项目名称模糊搜索）

    Args:
        project_name: 项目名称（模糊匹配）
        art_style: 艺术风格代码
        status: 状态筛选
        include_archived: 是否包含已归档的世界观
        limit: 返回记录数限制

    Returns:
        世界观列表
    """
    try:
        from sqlmodel import select

        with Session(engine) as session:
            statement = select(Worldview)

            # 状态筛选
            if status is not None:
                statement = statement.where(Worldview.status == status)
            elif not include_archived:
                statement = statement.where(Worldview.status != WorldviewStatus.ARCHIVED)

            # 艺术风格筛选
            if art_style is not None:
                statement = statement.where(Worldview.art_style == art_style)

            # 项目名称模糊搜索
            if project_name:
                statement = statement.where(
                    Worldview.data["project_name"].astext.ilike(f"%{project_name}%")
                )

            # 排序和分页
            statement = statement.order_by(Worldview.create_time.desc()).limit(limit)

            results = session.exec(statement)
            return results.all()

    except Exception as e:
        print(f"❌ 搜索世界观失败: {e}")
        return []


def get_latest_worldview(
    include_archived: bool = False,
    status: Optional[int] = None
) -> Optional[Worldview]:
    """
    获取最新的世界观记录（按create_time降序）

    Args:
        include_archived: 是否包含已归档的世界观（默认 False）
        status: 按状态筛选（如果提供，include_archived 参数无效）

    Returns:
        Worldview 对象或 None（如果没有记录）
    """
    try:
        from sqlmodel import select

        with Session(engine) as session:
            statement = select(Worldview)

            # 状态筛选
            if status is not None:
                statement = statement.where(Worldview.status == status)
            elif not include_archived:
                statement = statement.where(Worldview.status != WorldviewStatus.ARCHIVED)

            # 按创建时间降序排序，取第一条
            statement = statement.order_by(Worldview.create_time.desc()).limit(1)

            results = session.exec(statement)
            worldview = results.first()

            return worldview

    except Exception as e:
        print(f"❌ 获取最新世界观失败: {e}")
        return None


def get_latest_worldview_data_and_task_id(
    include_archived: bool = False,
    status: Optional[int] = None
) -> Optional[Dict[str, Any]]:
    """
    获取最新世界观记录的data和task_id

    Args:
        include_archived: 是否包含已归档的世界观（默认 False）
        status: 按状态筛选（如果提供，include_archived 参数无效）

    Returns:
        包含data和task_id的字典，格式为:
        {
            "data": world_bible_data,
            "task_id": task_id
        }
        或 None（如果没有记录）
    """
    try:
        worldview = get_latest_worldview(
            include_archived=include_archived,
            status=status
        )

        if worldview is None:
            return None

        return {
            "data": worldview.data,
            "task_id": worldview.task_id
        }

    except Exception as e:
        print(f"❌ 获取最新世界观data和task_id失败: {e}")
        return None


def count_worldviews(
    include_archived: bool = False,
    status: Optional[int] = None,
    art_style: Optional[int] = None
) -> int:
    """
    统计世界观数量

    Args:
        include_archived: 是否包含已归档的世界观
        status: 按状态筛选
        art_style: 按艺术风格代码筛选

    Returns:
        世界观数量
    """
    try:
        from sqlmodel import select, func

        with Session(engine) as session:
            statement = select(func.count(Worldview.id))

            # 状态筛选
            if status is not None:
                statement = statement.where(Worldview.status == status)
            elif not include_archived:
                statement = statement.where(Worldview.status != WorldviewStatus.ARCHIVED)

            # 艺术风格筛选
            if art_style is not None:
                statement = statement.where(Worldview.art_style == art_style)

            result = session.exec(statement).one()
            return result

    except Exception as e:
        print(f"❌ 统计世界观数量失败: {e}")
        return 0


# ---------- 改 ----------
def update_worldview_data(
    worldview_id: int,
    new_data: Dict[str, Any],
    allow_update_archived: bool = False
) -> Optional[Worldview]:
    """
    更新世界观数据

    Args:
        worldview_id: 世界观数据库 ID
        new_data: 新的 world_bible 数据
        allow_update_archived: 是否允许更新已归档的世界观（默认 False）

    Returns:
        更新后的 Worldview 对象或 None
    """
    try:
        with Session(engine) as session:
            worldview = session.get(Worldview, worldview_id)
            if not worldview:
                print(f"❌ 未找到世界观 ID={worldview_id}")
                return None

            # 检查是否允许更新已归档的世界观
            if worldview.status == WorldviewStatus.ARCHIVED and not allow_update_archived:
                print(f"❌ 世界观 ID={worldview_id} 已归档，不允许更新")
                return None

            # 验证新数据
            validated_data = validate_world_bible_data(new_data)

            # 更新数据
            worldview.data = validated_data

            # 更新艺术风格代码（如果新数据中有艺术风格信息）
            if "art_style" in validated_data:
                art_style_str = validated_data.get("art_style", "")
                worldview.art_style = map_art_style_to_code(art_style_str)

            session.commit()
            session.refresh(worldview)

            print(f"✅ 世界观数据更新成功 ID={worldview_id}")
            return worldview

    except Exception as e:
        print(f"❌ 更新世界观数据失败: {e}")
        import traceback
        traceback.print_exc()
        return None


def update_worldview_status(
    worldview_id: int,
    new_status: int,
    validate_transition: bool = True
) -> Optional[Worldview]:
    """
    更新世界观状态

    Args:
        worldview_id: 世界观数据库 ID
        new_status: 新的状态
        validate_transition: 是否验证状态转换合法性（默认 True）

    Returns:
        更新后的 Worldview 对象或 None
    """
    try:
        with Session(engine) as session:
            worldview = session.get(Worldview, worldview_id)
            if not worldview:
                print(f"❌ 未找到世界观 ID={worldview_id}")
                return None

            current_status = worldview.status

            # 验证状态转换（如果需要）
            if validate_transition:
                valid_transitions = {
                    WorldviewStatus.DRAFT: [WorldviewStatus.CONFIRMED, WorldviewStatus.ARCHIVED],
                    WorldviewStatus.CONFIRMED: [WorldviewStatus.ARCHIVED],
                    WorldviewStatus.ARCHIVED: [WorldviewStatus.DRAFT, WorldviewStatus.CONFIRMED]
                }

                if new_status not in valid_transitions.get(current_status, []):
                    print(f"❌ 不允许从状态 {current_status} 转换到 {new_status}")
                    print(f"   允许的转换: {valid_transitions.get(current_status, [])}")
                    return None

            # 更新状态
            worldview.status = new_status
            session.commit()
            session.refresh(worldview)

            status_names = {
                WorldviewStatus.DRAFT: "DRAFT",
                WorldviewStatus.CONFIRMED: "CONFIRMED",
                WorldviewStatus.ARCHIVED: "ARCHIVED"
            }
            print(f"✅ 世界观状态更新成功 ID={worldview_id}: "
                  f"{status_names.get(current_status, current_status)} -> "
                  f"{status_names.get(new_status, new_status)}")
            return worldview

    except Exception as e:
        print(f"❌ 更新世界观状态失败: {e}")
        import traceback
        traceback.print_exc()
        return None


# ---------- 软删除相关 ----------
def soft_delete_worldview(worldview_id: int) -> bool:
    """
    软删除世界观（将状态改为 ARCHIVED）

    Args:
        worldview_id: 世界观数据库 ID

    Returns:
        bool: 成功 True，失败 False
    """
    try:
        with Session(engine) as session:
            worldview = session.get(Worldview, worldview_id)
            if not worldview:
                print(f"❌ 未找到世界观 ID={worldview_id}")
                return False

            if worldview.status == WorldviewStatus.ARCHIVED:
                print(f"⚠️  世界观 ID={worldview_id} 已经是 ARCHIVED 状态")
                return True

            worldview.status = WorldviewStatus.ARCHIVED
            session.commit()

            print(f"✅ 世界观 ID={worldview_id} 已软删除（状态改为 ARCHIVED）")
            return True

    except Exception as e:
        print(f"❌ 软删除失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def restore_worldview(worldview_id: int) -> bool:
    """
    恢复软删除的世界观（将状态改为 DRAFT）

    Args:
        worldview_id: 世界观数据库 ID

    Returns:
        bool: 成功 True，失败 False
    """
    try:
        with Session(engine) as session:
            worldview = session.get(Worldview, worldview_id)
            if not worldview:
                print(f"❌ 未找到世界观 ID={worldview_id}")
                return False

            if worldview.status != WorldviewStatus.ARCHIVED:
                print(f"⚠️  世界观 ID={worldview_id} 状态不是 ARCHIVED，无法恢复")
                return False

            worldview.status = WorldviewStatus.DRAFT
            session.commit()

            print(f"✅ 世界观 ID={worldview_id} 已恢复（状态改为 DRAFT）")
            return True

    except Exception as e:
        print(f"❌ 恢复失败: {e}")
        import traceback
        traceback.print_exc()
        return False


# ==================== 命令行接口 ====================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="世界观数据入库工具")
    subparsers = parser.add_subparsers(dest="command", help="命令")

    # 导入 JSON 文件命令
    import_parser = subparsers.add_parser("import", help="导入 JSON 文件到数据库")
    import_parser.add_argument("json_file", help="world_bible.json 文件路径")
    import_parser.add_argument("--task-id", help="指定 task_id")
    import_parser.add_argument("--art-style-code", type=int, choices=[0, 1, 2, 3],
                              help="艺术风格代码：0-未知,1-仙侠古风,2-科幻,3-现代都市")
    import_parser.add_argument("--status", type=int, default=WorldviewStatus.CONFIRMED,
                              choices=[WorldviewStatus.DRAFT, WorldviewStatus.CONFIRMED, WorldviewStatus.ARCHIVED],
                              help=f"状态：{WorldviewStatus.DRAFT}-DRAFT, {WorldviewStatus.CONFIRMED}-CONFIRMED, {WorldviewStatus.ARCHIVED}-ARCHIVED")

    # 软删除命令
    delete_parser = subparsers.add_parser("delete", help="软删除世界观")
    delete_parser.add_argument("id", type=int, help="世界观数据库 ID")

    # 恢复命令
    restore_parser = subparsers.add_parser("restore", help="恢复软删除的世界观")
    restore_parser.add_argument("id", type=int, help="世界观数据库 ID")

    # 查询命令
    get_parser = subparsers.add_parser("get", help="查询世界观详情")
    get_parser.add_argument("id", type=int, help="世界观数据库 ID")
    get_parser.add_argument("--include-archived", action="store_true",
                           help="包含已归档的世界观")

    # 列出命令
    list_parser = subparsers.add_parser("list", help="列出世界观")
    list_parser.add_argument("--include-archived", action="store_true",
                           help="包含已归档的世界观")
    list_parser.add_argument("--status", type=int, choices=[0, 1, 2],
                           help="按状态筛选: 0-DRAFT, 1-CONFIRMED, 2-ARCHIVED")
    list_parser.add_argument("--art-style", type=int, choices=[0, 1, 2, 3],
                           help="艺术风格代码: 0-未知,1-仙侠古风,2-科幻,3-现代都市")
    list_parser.add_argument("--limit", type=int, default=20,
                           help="返回记录数限制 (默认: 20)")
    list_parser.add_argument("--offset", type=int, default=0,
                           help="偏移量 (默认: 0)")
    list_parser.add_argument("--order-by", default="create_time_desc",
                           choices=["create_time_desc", "create_time_asc", "update_time_desc", "update_time_asc"],
                           help="排序方式 (默认: create_time_desc)")

    # 搜索命令
    search_parser = subparsers.add_parser("search", help="搜索世界观")
    search_parser.add_argument("--project-name", help="项目名称（模糊匹配）")
    search_parser.add_argument("--art-style", type=int, choices=[0, 1, 2, 3],
                             help="艺术风格代码: 0-未知,1-仙侠古风,2-科幻,3-现代都市")
    search_parser.add_argument("--status", type=int, choices=[0, 1, 2],
                             help="按状态筛选: 0-DRAFT, 1-CONFIRMED, 2-ARCHIVED")
    search_parser.add_argument("--include-archived", action="store_true",
                             help="包含已归档的世界观")
    search_parser.add_argument("--limit", type=int, default=20,
                             help="返回记录数限制 (默认: 20)")

    # 更新数据命令
    update_parser = subparsers.add_parser("update", help="更新世界观数据")
    update_parser.add_argument("id", type=int, help="世界观数据库 ID")
    update_parser.add_argument("--json-file", help="包含新数据的JSON文件路径")
    update_parser.add_argument("--allow-update-archived", action="store_true",
                             help="允许更新已归档的世界观")

    # 更新状态命令
    status_parser = subparsers.add_parser("status", help="更新世界观状态")
    status_parser.add_argument("id", type=int, help="世界观数据库 ID")
    status_parser.add_argument("new_status", type=int, choices=[0, 1, 2],
                             help="新状态: 0-DRAFT, 1-CONFIRMED, 2-ARCHIVED")
    status_parser.add_argument("--no-validate", action="store_true",
                             help="不验证状态转换合法性")

    # 统计命令
    count_parser = subparsers.add_parser("count", help="统计世界观数量")
    count_parser.add_argument("--include-archived", action="store_true",
                            help="包含已归档的世界观")
    count_parser.add_argument("--status", type=int, choices=[0, 1, 2],
                            help="按状态筛选: 0-DRAFT, 1-CONFIRMED, 2-ARCHIVED")
    count_parser.add_argument("--art-style", type=int, choices=[0, 1, 2, 3],
                            help="艺术风格代码: 0-未知,1-仙侠古风,2-科幻,3-现代都市")

    # 最新记录命令
    latest_parser = subparsers.add_parser("latest", help="获取最新世界观记录")
    latest_parser.add_argument("--include-archived", action="store_true",
                             help="包含已归档的世界观")
    latest_parser.add_argument("--status", type=int, choices=[0, 1, 2],
                             help="按状态筛选: 0-DRAFT, 1-CONFIRMED, 2-ARCHIVED")
    latest_parser.add_argument("--output-format", default="detailed",
                             choices=["detailed", "simple", "json"],
                             help="输出格式: detailed-详细, simple-简单, json-JSON格式")

    args = parser.parse_args()

    if args.command == "import":
        worldview = import_json_file_to_db(
            json_path=args.json_file,
            status=args.status,
            task_id=args.task_id,
            art_style_code=args.art_style_code
        )
        if worldview:
            print(f"\n✅ 导入成功！世界观 ID: {worldview.id}")
        else:
            print("\n❌ 导入失败")
            sys.exit(1)

    elif args.command == "delete":
        success = soft_delete_worldview(args.id)
        if not success:
            sys.exit(1)

    elif args.command == "restore":
        success = restore_worldview(args.id)
        if not success:
            sys.exit(1)

    elif args.command == "get":
        worldview = get_worldview_by_id(args.id, args.include_archived)
        if worldview:
            print(f"\n📋 世界观详情:")
            print(f"   数据库ID: {worldview.id}")
            print(f"   task_id: {worldview.task_id}")
            print(f"   项目名称: {worldview.data.get('project_name', '未知')}")
            print(f"   艺术风格代码: {worldview.art_style}")
            print(f"   状态: {worldview.status} ({WorldviewStatus.get_description(worldview.status)})")
            print(f"   创建时间: {worldview.create_time}")
            print(f"   更新时间: {worldview.update_time}")
            print(f"   角色数量: {len(worldview.data.get('characters', {}))}")
            print(f"   场景数量: {len(worldview.data.get('scenes', {}))}")
        else:
            print(f"❌ 未找到世界观 ID={args.id}")
            sys.exit(1)

    elif args.command == "list":
        worldviews = list_worldviews(
            include_archived=args.include_archived,
            status=args.status,
            art_style=args.art_style,
            limit=args.limit,
            offset=args.offset,
            order_by=args.order_by
        )

        print(f"\n📊 找到 {len(worldviews)} 个世界观:")
        print("-" * 80)
        for i, wv in enumerate(worldviews):
            status_desc = WorldviewStatus.get_description(wv.status)
            art_style_names = {0: "未知", 1: "仙侠古风", 2: "科幻", 3: "现代都市"}
            art_style_name = art_style_names.get(wv.art_style, "未知")

            print(f"{i+1:3d}. ID: {wv.id:4d} | task_id: {wv.task_id:20s} | "
                  f"项目: {wv.data.get('project_name', '未知')[:20]:20s} | "
                  f"风格: {art_style_name:8s} | 状态: {status_desc:10s} | "
                  f"创建: {wv.create_time.strftime('%Y-%m-%d %H:%M')}")

    elif args.command == "search":
        worldviews = search_worldviews(
            project_name=args.project_name,
            art_style=args.art_style,
            status=args.status,
            include_archived=args.include_archived,
            limit=args.limit
        )

        print(f"\n🔍 搜索到 {len(worldviews)} 个世界观:")
        if args.project_name:
            print(f"   搜索条件: 项目名称包含 '{args.project_name}'")
        if args.art_style is not None:
            art_style_names = {0: "未知", 1: "仙侠古风", 2: "科幻", 3: "现代都市"}
            print(f"   艺术风格: {art_style_names.get(args.art_style, '未知')}")
        if args.status is not None:
            status_names = {0: "DRAFT", 1: "CONFIRMED", 2: "ARCHIVED"}
            print(f"   状态: {status_names.get(args.status, '未知')}")

        print("-" * 80)
        for i, wv in enumerate(worldviews):
            status_desc = WorldviewStatus.get_description(wv.status)
            art_style_names = {0: "未知", 1: "仙侠古风", 2: "科幻", 3: "现代都市"}
            art_style_name = art_style_names.get(wv.art_style, "未知")

            print(f"{i+1:3d}. ID: {wv.id:4d} | task_id: {wv.task_id:20s} | "
                  f"项目: {wv.data.get('project_name', '未知')[:20]:20s} | "
                  f"风格: {art_style_name:8s} | 状态: {status_desc:10s}")

    elif args.command == "update":
        if not args.json_file:
            print("❌ 请提供 --json-file 参数")
            sys.exit(1)

        try:
            with open(args.json_file, 'r', encoding='utf-8') as f:
                new_data = json.load(f)
        except Exception as e:
            print(f"❌ 读取JSON文件失败: {e}")
            sys.exit(1)

        worldview = update_worldview_data(
            worldview_id=args.id,
            new_data=new_data,
            allow_update_archived=args.allow_update_archived
        )

        if worldview:
            print(f"\n✅ 世界观数据更新成功!")
            print(f"   世界观ID: {worldview.id}")
            print(f"   task_id: {worldview.task_id}")
            print(f"   项目名称: {worldview.data.get('project_name', '未知')}")
        else:
            print("\n❌ 世界观数据更新失败")
            sys.exit(1)

    elif args.command == "status":
        worldview = update_worldview_status(
            worldview_id=args.id,
            new_status=args.new_status,
            validate_transition=not args.no_validate
        )

        if worldview:
            print(f"\n✅ 世界观状态更新成功!")
            print(f"   世界观ID: {worldview.id}")
            print(f"   task_id: {worldview.task_id}")
            old_status_name = {0: "DRAFT", 1: "CONFIRMED", 2: "ARCHIVED"}.get(worldview.status, "未知")
            new_status_name = {0: "DRAFT", 1: "CONFIRMED", 2: "ARCHIVED"}.get(args.new_status, "未知")
            print(f"   状态: {old_status_name} -> {new_status_name}")
        else:
            print("\n❌ 世界观状态更新失败")
            sys.exit(1)

    elif args.command == "count":
        count = count_worldviews(
            include_archived=args.include_archived,
            status=args.status,
            art_style=args.art_style
        )

        print(f"\n📊 世界观统计:")
        print(f"   总数: {count}")

        if args.status is not None:
            status_names = {0: "DRAFT", 1: "CONFIRMED", 2: "ARCHIVED"}
            print(f"   状态 '{status_names.get(args.status, '未知')}': {count}")
        elif args.art_style is not None:
            art_style_names = {0: "未知", 1: "仙侠古风", 2: "科幻", 3: "现代都市"}
            print(f"   艺术风格 '{art_style_names.get(args.art_style, '未知')}': {count}")
        elif not args.include_archived:
            print(f"   (不包含已归档的世界观)")

    elif args.command == "latest":
        result = get_latest_worldview_data_and_task_id(
            include_archived=args.include_archived,
            status=args.status
        )

        if not result:
            print("❌ 未找到世界观记录")
            sys.exit(1)

        data = result["data"]
        task_id = result["task_id"]

        if args.output_format == "json":
            # JSON格式输出
            import json
            output_data = {
                "task_id": task_id,
                "data": data
            }
            print(json.dumps(output_data, ensure_ascii=False, indent=2))
        elif args.output_format == "simple":
            # 简单格式输出
            print(f"task_id: {task_id}")
            print(f"project_name: {data.get('project_name', '未知')}")
            print(f"art_style: {data.get('art_style', '未知')}")
            print(f"characters_count: {len(data.get('characters', {}))}")
            print(f"scenes_count: {len(data.get('scenes', {}))}")
        else:
            # 详细格式输出（默认）
            art_style_names = {0: "未知", 1: "仙侠古风", 2: "科幻", 3: "现代都市"}
            art_style_code = map_art_style_to_code(data.get('art_style', ''))
            art_style_name = art_style_names.get(art_style_code, "未知")

            print(f"\n📋 最新世界观详情:")
            print(f"   task_id: {task_id}")
            print(f"   项目名称: {data.get('project_name', '未知')}")
            print(f"   艺术风格: {data.get('art_style', '未知')} ({art_style_name})")
            print(f"   世界观概述: {data.get('worldview', {}).get('summary', '无')[:100]}...")
            print(f"   角色数量: {len(data.get('characters', {}))}")
            print(f"   场景数量: {len(data.get('scenes', {}))}")
            print(f"   关系网络: {len(data.get('relationship_network', {}))} 个关系")
            print(f"   场景图: {len(data.get('scene_graph', {}))} 个节点")
            print(f"   叙事结构: {len(data.get('narrative_structure', {}))} 个部分")

            # 显示前几个角色和场景
            characters = data.get('characters', {})
            if characters:
                print(f"\n   主要角色:")
                for i, (char_id, char_info) in enumerate(list(characters.items())[:3]):
                    print(f"     {i+1}. {char_info.get('name', '未知')} - {char_info.get('role', '未知')}")

            scenes = data.get('scenes', {})
            if scenes:
                print(f"\n   主要场景:")
                for i, (scene_id, scene_info) in enumerate(list(scenes.items())[:3]):
                    print(f"     {i+1}. {scene_info.get('name', '未知')} - {scene_info.get('type', '未知')}")

    else:
        parser.print_help()