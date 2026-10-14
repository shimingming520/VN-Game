#!/usr/bin/env python3
"""
角色立绘生成器 - 集成版
基于数据库人物提示词，批量生成完整人物立绘（空白背景）。
美术风格在 Provider 边界由任务级 global_style 与对应 Anchor 统一控制。

输出命名: 例如 Harlow_1-1-1_calm.png / 萧无尘_1-1-1_calm.png
"""

import os
import sys
import json
import time
import logging
import argparse
import re
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime

import dashscope
from dashscope import MultiModalConversation
import requests

# 尝试导入数据库相关模块
try:
    from sqlalchemy import text
    HAS_SQLALCHEMY = True
except ImportError:
    HAS_SQLALCHEMY = False

# 尝试导入pypinyin
try:
    from pypinyin import pinyin, Style
    HAS_PYPINYIN = True
except ImportError:
    HAS_PYPINYIN = False

# 添加项目根目录到路径（core -> char_designer -> services -> novel-game-factory）
current_dir = Path(__file__).resolve().parent
char_designer_path = current_dir.parent
workspace_root = char_designer_path.parent
project_root_path = workspace_root.parent
for path in (str(workspace_root), str(char_designer_path), str(current_dir), str(project_root_path)):
    if path not in sys.path:
        sys.path.insert(0, path)


def add_common_config_search_paths() -> None:
    # 本仓库自带 common/config.py 时，直接用它并返回，绝不去扫描其它仓库副本。
    # 否则 glob 命中的外部仓库 config.py 会被 insert(0) 抢占 sys.path，
    # 而部分旧副本的 DATABASE_URL 未对密码里的 '@' 做 %40 编码，导致连库失败。
    if (project_root_path / "common" / "config.py").exists():
        candidate_str = str(project_root_path)
        if candidate_str not in sys.path:
            sys.path.insert(0, candidate_str)
        return

    candidates = [
        workspace_root,
        workspace_root.parent,
        workspace_root.parent / "novel-game-factory",
        workspace_root.parent / "sj_novel" / "novel-game-factory",
    ]
    search_roots = [workspace_root.parent, Path.home() / "Documents" / "code", Path.home() / "Documents" / "gitlab"]
    for search_root in search_roots:
        if search_root.exists():
            candidates.extend(path.parent.parent for path in search_root.glob("*/novel-game-factory/common/config.py"))
            candidates.extend(path.parent.parent for path in search_root.glob("*/*/novel-game-factory/common/config.py"))

    for candidate in candidates:
        if (candidate / "common" / "config.py").exists():
            candidate_str = str(candidate)
            if candidate_str not in sys.path:
                sys.path.insert(0, candidate_str)


add_common_config_search_paths()

os.environ.setdefault("DB_HOST", "172.16.50.17")
os.environ.setdefault("DB_PORT", "5432")
os.environ.setdefault("DB_NAME", "extreme")
os.environ.setdefault("DB_USER", "test")
os.environ.setdefault("DB_PASSWORD", "change_me")

# 尝试加载环境变量（从项目根目录）
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=project_root_path / "novel-game-factory" / ".env")
    load_dotenv(dotenv_path=workspace_root / ".env")
except ImportError:
    pass  # 如果没有 dotenv，继续

from common.config import OUTPUTS_PATH
from common.image_concurrency import (
    ThreadLocalResource,
    resolve_image_concurrency,
    run_dependency_layer,
)
from common.platform_db import platform_engine
from common.name_mapping_utils import get_change_conversion, get_original_conversion
from common.visual_checkpoint import (
    assert_retry_budget,
    canonical_digest,
    mark_failed as checkpoint_mark_failed,
    mark_running as checkpoint_mark_running,
    mark_success as checkpoint_mark_success,
    reusable_or_adopt_existing,
)
from common.visual_style import (
    ASSET_CHARACTER,
    apply_asset_style,
    compose_anchor_guided_prompt,
    resolve_asset_style_plan,
)
from common.visual_debug import record_visual_generation
from services.char_designer.core.artifact_contract import (
    CharacterImageArtifactError,
    atomic_image_output,
    character_resume_source_path,
    publish_character_resume_source,
    restore_character_working_copy,
)
from services.char_designer.utils.native_transparency import (
    inspect_native_transparency,
    resolve_character_output_policy,
    validator_for_policy,
)

try:
    from char_designer.image_providers.gpt_image import GPTImageEditProvider, GPTImageTextProvider
    from char_designer.image_providers.qwen_image import QwenImageEditProvider, QwenTextImageProvider
    from char_designer.image_providers.wan_image_25 import Wan25TextImageProvider
    from char_designer.utils.prompt_repair import (
        PromptPersistenceContext,
        execute_with_prompt_repair,
    )
except ImportError:
    from image_providers.gpt_image import GPTImageEditProvider, GPTImageTextProvider  # type: ignore
    from image_providers.qwen_image import QwenImageEditProvider, QwenTextImageProvider  # type: ignore
    from image_providers.wan_image_25 import Wan25TextImageProvider  # type: ignore
    from utils.prompt_repair import PromptPersistenceContext, execute_with_prompt_repair  # type: ignore

try:
    from common.usage_tracker import tracker as _usage_tracker
except Exception:
    _usage_tracker = None

from services.char_designer.utils.character_identity import (
    apply_authoritative_speaker_id,
    authoritative_speaker_id,
    combination_filename,
    normalize_resource_id,
)

# 配置日志
import logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

_conversion_cache: Dict[str, str] = {}
CALM_GENERATOR_VERSION = "character-calm-v3-native-rgba"


def _normalise_conversion_mapping(data: Any) -> Dict[str, str]:
    """Load the upstream identity IDs without rewriting their Unicode text."""
    mapping = get_original_conversion(data)
    if not mapping:
        mapping = get_change_conversion(data)
    result: Dict[str, str] = {}
    for name, value in mapping.items():
        try:
            result[str(name)] = normalize_resource_id(
                value, field=f"name_mappings[{name}]"
            )
        except ValueError as exc:
            logger.warning(
                "忽略不安全的人物 ID 映射，回退本地名称转换: name=%r error=%s",
                name,
                exc,
            )
    return result


def resolve_authoritative_speaker_id(name: object, fallback: object = "") -> str:
    """Resolve a prompt/name to the task's exact upstream resource ID."""
    return authoritative_speaker_id(name, fallback, _conversion_cache)


def _load_conversion_mapping(task_id: str) -> Dict[str, str]:
    """
    加载人名 conversion 映射（DB 优先，本地文件降级）。
    """
    try:
        with platform_engine.connect() as conn:
            row = conn.execute(
                text("SELECT data FROM name_mappings WHERE task_id = :tid"),
                {"tid": task_id},
            ).fetchone()
        if row:
            data = row[0]
            if isinstance(data, str):
                data = json.loads(data)
            # New name_mappings keeps the pre-anonymization IDs under
            # original_name.conversion.  Character generation must use those
            # exact IDs; scene_painter applies the later old->new rename only
            # after all visual assets have consumed them.  Legacy flat data is
            # still accepted by the shared helpers.
            return _normalise_conversion_mapping(data)
    except Exception as e:
        logger.warning(f"从数据库加载 name_mappings 失败: {e}")
    local_path = Path(OUTPUTS_PATH) / task_id / "name_mappings.json"
    if local_path.exists():
        with open(local_path, encoding="utf-8") as f:
            return _normalise_conversion_mapping(json.load(f))
    return {}


def _init_conversion_cache(task_id: str) -> None:
    global _conversion_cache
    try:
        _conversion_cache = _load_conversion_mapping(task_id)
        if _conversion_cache:
            logger.info(f"已加载人名映射表: {len(_conversion_cache)} 条")
        else:
            logger.warning(f"人名映射表为空（task_id={task_id}），将使用拼音转换")
    except Exception as e:
        logger.warning(f"人名映射表加载失败，将使用拼音转换: {e}")

# 尝试导入 PIL（用于按身高扩展顶部画布）
try:
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    logger.warning("Pillow 未安装，画布扩展功能将不可用。请安装: pip install Pillow")

# ==================== 数据类定义 ====================

@dataclass
class CharacterInfo:
    """角色信息数据类"""
    name: str  # 原始键名，如 Tang_San
    speaker_id: str  # 驼峰格式，如 TangSan

    # 基础信息（默认值）
    gender: str = "unknown"
    age: int = 18
    age_category: str = ""  # 年龄分类，如 "middle-aged"、"young adult"、"adolescent"等
    race: str = "human"
    occupation: str = "cultivator"
    combination: str = ""  # active combination string, e.g. "TangSan-1-2-3"

    # 外观信息（从 appearance 字段映射）
    appearance: Dict[str, Any] = field(default_factory=dict)

    # 服装信息（从 clothing 字段映射）
    clothing: Dict[str, Any] = field(default_factory=dict)

    # 表情/性格信息（从 expression 字段映射）
    expression: Dict[str, Any] = field(default_factory=dict)

    # 扩展信息（用于提示词生成）
    personality: Dict[str, Any] = field(default_factory=lambda: {"main_traits": []})
    abilities: List[str] = field(default_factory=list)
    background: str = ""



def find_world_bible_file(search_dirs: List[str] = None) -> Optional[str]:
    """
    查找最新的world_bible.json文件

    Args:
        search_dirs: 要搜索的目录列表，如果为None则使用默认目录

    Returns:
        Optional[str]: 找到的文件路径，如果未找到则返回None
    """
    import os
    import glob
    from pathlib import Path

    if search_dirs is None:
        # 默认搜索目录（仅限 char_designer 内部）
        char_designer_dir = Path(__file__).resolve().parent.parent
        search_dirs = [
            str(char_designer_dir / "world_db"),
            "."
        ]

    latest_file = None
    latest_mtime = 0

    for base_dir in search_dirs:
        if not os.path.exists(base_dir):
            continue

        # 查找所有world_bible.json文件
        pattern = os.path.join(base_dir, "**", "world_bible.json")
        for filepath in glob.glob(pattern, recursive=True):
            mtime = os.path.getmtime(filepath)
            if mtime > latest_mtime:
                latest_mtime = mtime
                latest_file = filepath

    return latest_file


# ==================== 字符转换工具 ====================

def sanitize_filename(name: str) -> str:
    """
    清理文件名，移除不需要的字符
    1. 移除 's 和 's（所有格）
    2. 移除所有单引号（英文 ' 和中文 ' '）
    3. 移除句点（.）
    4. 移除所有特殊符号（保留字母、数字、空格、下划线、连字符）

    Args:
        name: 原始名称

    Returns:
        str: 清理后的名称
    """
    import re

    # 1. 先移除 's（所有格，处理不同的撇号字符）
    name = name.replace("'s", '')  # 英文单引号
    name = name.replace("'s", '')  # 中文右单引号
    name = name.replace("'s", '')  # 中文左单引号
    name = name.replace("'S", '')
    name = name.replace("'S", '')
    name = name.replace("'S", '')

    # 2. 移除所有剩余的单引号（英文和中文）
    name = name.replace("'", '')   # 英文单引号
    name = name.replace("'", '')   # 中文左单引号
    name = name.replace("'", '')   # 中文右单引号

    # 3. 移除句点（英文和中文）
    name = name.replace('.', '')   # 英文句点
    name = name.replace('。', '')  # 中文句号

    # 4. 移除所有其他特殊符号（保留字母、数字、空格、下划线、连字符）
    name = re.sub(r'[^a-zA-Z0-9\s_-]', '', name)

    return name


def to_camel_case(name: str) -> str:
    """
    将下划线或空格分隔的名称转换为大驼峰格式（无空格无下划线）
    例如: Tang_San -> TangSan, Wang zong -> WangZong, Louis de pointe du lac -> LouisDePointeDuLac
    Moira's Daugther -> MoiraDaugther, Su Xi'er -> SuXier

    Args:
        name: 下划线或空格分隔的名称

    Returns:
        str: 大驼峰格式名称
    """
    import re
    # 先清理特殊符号和所有格
    name = sanitize_filename(name)
    # 分割（支持下划线、空格、连字符等分隔符）
    parts = re.split(r'[_\s-]+', name)
    camel_parts = [part.capitalize() for part in parts if part]
    result = ''.join(camel_parts)
    return result


def chinese_to_camel_case(chinese_name: str) -> str:
    if chinese_name in _conversion_cache:
        return _conversion_cache[chinese_name]
    import re
    logger.debug(f"chinese_to_camel_case 输入: '{chinese_name}'")
    has_chinese = re.search(r'[\u4e00-\u9fff]', chinese_name)
    logger.debug(f"是否包含中文字符: {has_chinese}")
    if not has_chinese:
        # 如果不包含中文，使用to_camel_case处理（支持空格和下划线）
        result = to_camel_case(chinese_name)
        logger.debug(f"非中文名称，使用 to_camel_case: '{result}'")
        return result

    # 尝试导入pypinyin
    try:
        from pypinyin import pinyin, Style
    except ImportError:
        logger.warning("未安装pypinyin库，中文名将无法转换为拼音驼峰格式。请安装: pip install pypinyin")
        # 回退到安全文件名：先清理特殊符号，保留空格，然后按空格分割并首字母大写
        safe_name = sanitize_filename(chinese_name)
        # 按空格分割并首字母大写
        parts = re.split(r'[_\s-]+', safe_name)
        result = ''.join([part.capitalize() for part in parts if part])
        logger.debug(f"pypinyin未安装，回退到安全文件名: '{result}'")
        return result

    try:
        # 获取拼音列表，每个字返回一个列表，取第一个拼音
        pinyin_list = pinyin(chinese_name, style=Style.NORMAL)
        logger.debug(f"拼音列表: {pinyin_list}")
        # 将每个拼音转换为首字母大写，其余小写
        camel_parts = [py[0].capitalize() for py in pinyin_list if py]
        result = ''.join(camel_parts)
        # 清理文件名
        result = sanitize_filename(result)
        logger.debug(f"转换结果: '{result}'")
        return result
    except Exception as e:
        logger.warning(f"转换中文名 '{chinese_name}' 为拼音失败: {e}")
        # 回退到安全文件名：先清理特殊符号，保留空格，然后按空格分割并首字母大写
        safe_name = sanitize_filename(chinese_name)
        # 按空格分割并首字母大写
        parts = re.split(r'[_\s-]+', safe_name)
        result = ''.join([part.capitalize() for part in parts if part])
        logger.debug(f"转换失败，回退到安全文件名: '{result}'")
        return result




# ==================== 角色数据转换 ====================



# ==================== World Bible 数据加载 ====================

def _parse_age_from_variant(age_str: str, age_mapping: Dict[str, int]) -> int:
    """
    从年龄字符串解析年龄数值

    Args:
        age_str: 年龄字符串，可能是数字、范围或年龄分类
        age_mapping: 年龄分类到数值的映射

    Returns:
        int: 解析出的年龄数值
    """
    age = 0

    # 处理范围字符串，如"16-17"
    if '-' in age_str:
        parts = age_str.split('-')
        if len(parts) == 2:
            try:
                # 取第一个数字
                age = int(parts[0].strip())
            except ValueError:
                # 如果第一个部分不是数字，尝试第二个
                try:
                    age = int(parts[1].strip())
                except ValueError:
                    age = 0
    else:
        # 尝试直接转换为整数
        try:
            age = int(age_str)
        except ValueError:
            # 使用映射
            for key, value in age_mapping.items():
                if key in age_str:
                    age = value
                    break
            # 如果仍未找到映射，保持为0

    return age


def _infer_gender_from_variants(all_body_variants: List[Dict[str, Any]]) -> Optional[str]:
    """
    从所有身体变体中推断性别

    Args:
        all_body_variants: 所有身体变体列表

    Returns:
        Optional[str]: 推断出的性别 ('male' 或 'female')，如果无法推断则返回 None
    """
    gender_map = {'男': 'male', '女': 'female'}

    # 遍历所有变体，寻找有效的性别信息
    for variant in all_body_variants:
        gender_str = variant.get('gender', '')
        if gender_str in gender_map:
            return gender_map[gender_str]

    # 如果所有变体都没有有效的性别信息
    return None


def load_character_data_from_dir(data_dir: str) -> List[CharacterInfo]:
    """
    从角色数据目录（如 world_bible_merged 或 story_tree_merged）加载角色数据并转换为 CharacterInfo 列表
    每个角色取最靠近18岁的age，以及该age对应的第一组服装变体和第一组性格变体

    Args:
        data_dir: 角色数据目录路径（包含时间戳子目录）

    Returns:
        List[CharacterInfo]: 角色信息列表，每个角色生成一个实例
    """
    characters = []

    # 查找最新的时间戳目录
    data_path = Path(data_dir)
    if not data_path.exists():
        logger.error(f"角色数据目录不存在: {data_dir}")
        return characters

    # 查找所有时间戳目录
    timestamp_dirs = [d for d in data_path.iterdir() if d.is_dir() and d.name[0].isdigit()]
    if not timestamp_dirs:
        logger.error(f"在 {data_dir} 中未找到时间戳目录")
        return characters

    # 使用最新的目录
    latest_dir = max(timestamp_dirs, key=lambda d: d.name)
    logger.info(f"使用角色数据目录: {latest_dir}")

    # 加载 character_mapping.json
    mapping_file = latest_dir / "character_mapping.json"
    if not mapping_file.exists():
        logger.error(f"character_mapping.json 不存在: {mapping_file}")
        return characters

    with open(mapping_file, 'r', encoding='utf-8') as f:
        mapping_data = json.load(f)

    # 处理每个角色
    for char_name, char_info in mapping_data.items():
        # 加载角色详细JSON文件
        char_file = Path(char_info['filepath'])
        if not char_file.exists():
            char_file = latest_dir / f"{char_name}.json"

        if not char_file.exists():
            logger.warning(f"角色文件不存在: {char_name}.json")
            continue

        with open(char_file, 'r', encoding='utf-8') as f:
            all_characters_data = json.load(f)

        # 获取当前角色的数据
        if char_name not in all_characters_data:
            logger.warning(f"角色 {char_name} 不在字符数据文件中")
            continue
        char_data = all_characters_data[char_name]

        # 解析变体数据
        personality_variants = char_data.get('personality_variants', [])
        whole_body_variants = char_data.get('whole_body_variants', [])
        clothing_variants = char_data.get('clothing_variants', [])

        # 如果没有变体数据，跳过
        if not personality_variants or not whole_body_variants or not clothing_variants:
            logger.warning(f"角色 {char_name} 缺少变体数据")
            continue

        # 1. 选择第一个性格变体
        personality_variant = personality_variants[0]

        # 2. 选择第一个服装变体
        clothing_variant = clothing_variants[0]

        # 3. 从 whole_body_variants 中选择变体（优先选择 age_category="adolescent" 的变体）
        target_age = 18
        selected_variant = None

        # 年龄映射（英文版本）
        age_mapping = {
            "infant": 3,
            "child": 9,
            "adolescent": 15,
            "young adult": 23,
            "middle-aged": 40,
            "elderly": 60
        }

        # 第一阶段：优先寻找 age_category 为 "adolescent" 的变体
        teenage_variants = []
        for variant in whole_body_variants:
            age_category = variant.get('age_category', '')
            if age_category == "adolescent":
                teenage_variants.append(variant)

        if teenage_variants:
            # 如果有多个"adolescent"变体，选择年龄最接近18岁的那个
            if len(teenage_variants) > 1:
                closest_variant = None
                min_age_diff = float('inf')

                for variant in teenage_variants:
                    age_str = variant.get('age', '0')
                    age = _parse_age_from_variant(age_str, age_mapping)

                    age_diff = abs(age - target_age)
                    if age_diff < min_age_diff:
                        min_age_diff = age_diff
                        closest_variant = variant
                    elif age_diff == min_age_diff:
                        # 如果年龄差相同，选择年龄较大的（更接近成年）
                        closest_age_str = closest_variant.get('age', '0')
                        closest_age = _parse_age_from_variant(closest_age_str, age_mapping)
                        if age > closest_age:
                            closest_variant = variant

                selected_variant = closest_variant
            else:
                # 只有一个"adolescent"变体
                selected_variant = teenage_variants[0]

            logger.info(f"角色 {char_name} 选择了 age_category='adolescent' 的变体")

        # 第二阶段：如果没有"adolescent"变体，优先选择年龄最接近15岁（adolescent年龄）的变体
        if not selected_variant:
            teenage_age = 15  # adolescent年龄
            closest_variant = None
            min_age_diff = float('inf')

            for variant in whole_body_variants:
                age_str = variant.get('age', '0')
                age = _parse_age_from_variant(age_str, age_mapping)

                # 跳过年龄为0的情况（无法解析）
                if age == 0:
                    continue

                age_diff = abs(age - teenage_age)
                if age_diff < min_age_diff:
                    min_age_diff = age_diff
                    closest_variant = variant
                elif age_diff == min_age_diff:
                    # 如果年龄差相同，选择年龄较大的（更接近成年）
                    closest_age_str = closest_variant.get('age', '0')
                    closest_age = _parse_age_from_variant(closest_age_str, age_mapping)
                    if age > closest_age:
                        closest_variant = variant

            if closest_variant:
                selected_variant = closest_variant
                logger.info(f"角色 {char_name} 选择了年龄最接近{teenage_age}岁（adolescent年龄）的变体")

        # 第三阶段：如果还是没有选择变体，选择最接近18岁的变体
        if not selected_variant:
            closest_variant = None
            min_age_diff = float('inf')

            for variant in whole_body_variants:
                age_str = variant.get('age', '0')
                age = _parse_age_from_variant(age_str, age_mapping)

                age_diff = abs(age - target_age)
                if age_diff < min_age_diff:
                    min_age_diff = age_diff
                    closest_variant = variant
                elif age_diff == min_age_diff:
                    # 如果年龄差相同，选择年龄较大的（更接近成年）
                    closest_age_str = closest_variant.get('age', '0')
                    closest_age = _parse_age_from_variant(closest_age_str, age_mapping)
                    if age > closest_age:
                        closest_variant = variant

            selected_variant = closest_variant
            if selected_variant:
                logger.info(f"角色 {char_name} 选择了最接近{target_age}岁的变体")

        if not selected_variant:
            selected_variant = whole_body_variants[0]
            logger.warning(f"角色 {char_name} 无法选择变体，使用第一个变体")

        # 转换为 CharacterInfo
        character = convert_character_variants(
            char_name, char_data, personality_variant,
            selected_variant, clothing_variant, whole_body_variants
        )
        if character:
            explicit_id = (
                char_data.get("speaker_id")
                or char_data.get("logical_character_id")
                or char_data.get("character_id")
                or character.speaker_id
            )
            character.speaker_id = resolve_authoritative_speaker_id(
                char_name,
                explicit_id,
            ) or character.speaker_id
            characters.append(character)

    logger.info(f"从角色数据目录加载了 {len(characters)} 个角色")
    return characters


def convert_character_variants(
    char_name: str,
    char_data: Dict[str, Any],
    personality_variant: Dict[str, Any],
    whole_body_variant: Dict[str, Any],
    clothing_variant: Dict[str, Any],
    all_body_variants: List[Dict[str, Any]] = None
) -> Optional[CharacterInfo]:
    """
    将角色变体数据转换为 CharacterInfo

    Args:
        char_name: 角色名称
        char_data: 完整角色数据
        personality_variant: 性格变体数据
        whole_body_variant: 全身变体数据
        clothing_variant: 服装变体数据
        all_body_variants: 所有身体变体列表（用于性别推断）

    Returns:
        Optional[CharacterInfo]: 转换后的角色信息，失败返回None
    """
    try:
        # 基础信息
        # ``name_mappings.original_name.conversion`` is authoritative for
        # visual resource identity.  Fall back to the historical transliterator
        # only when the upstream mapping is unavailable (legacy/local runs).
        speaker_id = resolve_authoritative_speaker_id(
            char_name,
            chinese_to_camel_case(char_name),
        )

        # 从 whole_body_variant 提取性别、年龄、种族
        gender_map = {'男': 'male', '女': 'female', 'other': 'other'}
        gender_str = whole_body_variant.get('gender', '未知')
        gender = gender_map.get(gender_str, 'unknown')

        # 如果性别为 other 或 unknown，尝试从其他身体变体推断
        if gender in ['other', 'unknown'] and all_body_variants:
            logger.info(f"角色 {char_name} 的性别为 {gender}，尝试从其他身体变体推断")
            inferred_gender = _infer_gender_from_variants(all_body_variants)
            if inferred_gender:
                gender = inferred_gender
                logger.info(f"角色 {char_name} 从其他变体推断性别为: {gender}")
            else:
                # 如果所有变体都无法判断，默认为 male
                gender = 'male'
                logger.info(f"角色 {char_name} 无法从变体推断性别，默认为: male")

        age_str = whole_body_variant.get('age', '0')
        # 年龄映射（英文版本）
        age_mapping = {
            "infant": 3,
            "child": 9,
            "adolescent": 15,
            "young adult": 23,
            "middle-aged": 40,
            "elderly": 60
        }
        # 解析年龄
        age = 0
        # 处理范围字符串，如"16-17"
        if '-' in age_str:
            parts = age_str.split('-')
            if len(parts) == 2:
                try:
                    # 取第一个数字
                    age = int(parts[0].strip())
                except ValueError:
                    # 如果第一个部分不是数字，尝试第二个
                    try:
                        age = int(parts[1].strip())
                    except ValueError:
                        age = 0
        else:
            # 尝试直接转换为整数
            try:
                age = int(age_str)
            except ValueError:
                # 使用映射
                for key, value in age_mapping.items():
                    if key in age_str:
                        age = value
                        break
                # 如果仍未找到映射，保持为0

        race = whole_body_variant.get('race', '人类')
        age_category = whole_body_variant.get('age_category', '')  # 年龄分类，如"middle-aged"、"young adult"等

        # 从 clothing_variant 提取职业
        occupation = clothing_variant.get('occupation', '未知')

        # 外观信息（从 whole_body_variant.appearance 映射）
        appearance_data = whole_body_variant.get('appearance', {})
        head_data = appearance_data.get('head', {})
        body_data = appearance_data.get('body', {})

        # 构建符合 douluoV2.5 结构的appearance字典
        appearance = {
            # douluoV2.5 风格的字段
            "hair_color": head_data.get('hair_color', ''),
            "eye_color": head_data.get('eye_color', ''),
            "skin_color": head_data.get('skin_color', ''),
            "facial_features": [
                head_data.get('facial_description', '')
            ] if head_data.get('facial_description') else [],
            "body_type": body_data.get('body_type', ''),
            "body_details": body_data.get('body_description', ''),
            # 原字段保持
            "skin_texture": head_data.get('skin_color', ''),
        }

        # 服装信息
        clothing = {
            # douluoV2.5 风格的字段
            "clothing_style": clothing_variant.get('clothing_style', ''),
            "main_outfit": clothing_variant.get('outfit_discription', ''),
            "accessories": clothing_variant.get('accessories', []),
            "weapons": clothing_variant.get('weapons', []),
            "armor": clothing_variant.get('armor', []),
            # 原字段保持
            "texture": clothing_variant.get('outfit_discription', ''),
        }

        # 性格信息
        personality = {
            "main_traits": personality_variant.get('main_traits', []),
            "secondary_traits": personality_variant.get('secondary_traits', []),
            "flaws": personality_variant.get('flaws', []),
            "virtues": personality_variant.get('virtues', [])
        }

        # 创建 CharacterInfo
        character = CharacterInfo(
            name=char_name,
            speaker_id=speaker_id,
            gender=gender,
            age=age,
            age_category=age_category,  # 添加年龄分类
            race=race,
            occupation=occupation,
            appearance=appearance,
            clothing=clothing,
            expression={},  # world_bible 中没有expression字段
            personality=personality,
            abilities=[],  # world_bible 中没有abilities字段
            background=""  # world_bible 中没有background字段
        )

        return character

    except Exception as e:
        logger.error(f"转换角色 {char_name} 数据失败: {e}")
        return None


# ==================== 数据库相关函数 ====================

def fetch_generation_context(task_id: str = None):
    """
    从数据库获取角色数据

    Args:
        task_id: 任务ID，如果为None则查找最新的任务

    Returns:
        tuple: (task_id, char_data) 或 (None, None)
    """
    if not HAS_SQLALCHEMY:
        logger.error("sqlalchemy 模块未安装，无法从数据库加载数据")
        return None, None

    try:
        with platform_engine.connect() as conn:
            target_task_id = task_id
            if not target_task_id:
                logger.info("未指定 Task ID，查找最新人物数据...")
                sql_latest = text("SELECT task_id FROM characters ORDER BY id DESC LIMIT 1")
                result = conn.execute(sql_latest).fetchone()
                if not result:
                    logger.error("数据库中未找到角色数据")
                    return None, None
                target_task_id = result[0]
                logger.info(f"自动锁定最新任务: {target_task_id}")
            else:
                logger.info(f"锁定指定任务: {target_task_id}")

            sql_char = text("SELECT data FROM characters WHERE task_id = :tid")
            res_char = conn.execute(sql_char, {"tid": target_task_id}).fetchone()
            if not res_char:
                logger.error(f"未找到任务 {target_task_id} 的角色数据")
                return None, None
            char_data = res_char[0]

            return target_task_id, char_data
    except Exception as e:
        logger.error(f"数据库查询失败: {e}")
        return None, None


def _parse_age_db(age_val: Any) -> int:
    """解析年龄值（适配数据库JSON结构）"""
    age_mapping = {"infant": 3, "child": 9, "adolescent": 15, "young adult": 23, "middle-aged": 40, "elderly": 60}
    if isinstance(age_val, int):
        return age_val
    age_str = str(age_val).strip()
    if age_str.isdigit():
        return int(age_str)
    # 处理范围如 "16-18"
    if '-' in age_str:
        try:
            return int(age_str.split('-')[0])
        except:
            pass
    for k, v in age_mapping.items():
        if k in age_str:
            return v
    return 18


def _variant_visual_age(variant: Dict[str, Any]) -> int:
    """选变体时用的“看起来的年龄”：优先 apparent_age（外表年龄），
    否则回落真实 age。百岁看似18的角色应按18匹配画像变体，而非按100。"""
    apparent = variant.get('apparent_age')
    if apparent is not None and str(apparent).strip().isdigit():
        return int(str(apparent).strip())
    return _parse_age_db(variant.get('age', 0))


def extract_traits_db(traits_input):
    """提取性格特征（适配数据库JSON结构）"""
    if isinstance(traits_input, list):
        return traits_input
    if isinstance(traits_input, str):
        return re.split(r'[，,、；;]', traits_input)
    return []


def parse_characters_from_db_json(json_data: Dict) -> List[CharacterInfo]:
    """
    从数据库JSON数据解析角色信息

    Args:
        json_data: 数据库characters表中的data字段（JSON）

    Returns:
        List[CharacterInfo]: 角色信息列表
    """
    characters = []
    # 遍历 JSON 字典
    for char_key, char_data in json_data.items():
        char_name = char_data.get('name', char_key)

        # 获取变体列表
        p_vars = char_data.get('personality_variants', [])
        b_vars = char_data.get('whole_body_variants', [])
        c_vars = char_data.get('clothing_variants', [])

        if not (b_vars and c_vars):
            logger.warning(f"跳过角色 {char_name}: 缺少变体数据")
            continue

        # 默认选择第一个
        selected_body = b_vars[0]
        selected_clothing = c_vars[0]
        selected_personality = p_vars[0] if p_vars else {}

        # 选角逻辑：优先选择最接近 18 岁或 "young adult/adolescent" 的变体
        target_age = 18
        best_diff = float('inf')

        for variant in b_vars:
            # 优先检查 age_category
            age_cat = variant.get('age_category', '')
            # 用“看起来的年龄”匹配画像变体：apparent_age 优先，回落 age。
            age_val = _variant_visual_age(variant)

            diff = abs(age_val - target_age)

            # 简单的加权逻辑：如果是特定类别，优先级更高
            if age_cat in ["adolescent", "young adult"] and diff < best_diff:
                best_diff = diff
                selected_body = variant
            elif diff < best_diff:
                best_diff = diff
                selected_body = variant

        # 转换数据
        char_obj = convert_db_json_to_character_info(
            char_name, selected_personality, selected_body, selected_clothing, b_vars
        )
        if char_obj:
            explicit_id = (
                char_data.get("speaker_id")
                or char_data.get("logical_character_id")
                or char_data.get("character_id")
                or char_obj.speaker_id
            )
            char_obj.speaker_id = resolve_authoritative_speaker_id(
                char_name,
                explicit_id,
            ) or char_obj.speaker_id
            characters.append(char_obj)

    return characters


def _parse_combination_str(combination: str):
    """解析 combination 字符串，返回 (name, personality_id, whole_body_id, clothing_id)。"""
    parts = combination.rsplit("-", 3)
    if len(parts) != 4:
        raise ValueError(f"无效的 combination 格式: {combination}")
    name, p_id, b_id, c_id = parts
    return name, int(p_id), int(b_id), int(c_id)


def parse_characters_by_active_combinations(json_data: Dict) -> List[CharacterInfo]:
    """
    为每个 is_active=True 的 combination 生成一个 CharacterInfo。
    若角色无激活组合，回退到原始逻辑（最接近18岁的 body + 第一个 clothing）。
    """
    characters = []
    for char_key, char_data in json_data.items():
        if isinstance(char_data, list):
            char_data = char_data[0] if char_data else {}
        if not isinstance(char_data, dict):
            continue

        char_name = char_data.get('name', char_key)
        p_vars = char_data.get('personality_variants', [])
        b_vars = char_data.get('whole_body_variants', [])
        c_vars = char_data.get('clothing_variants', [])
        combinations = char_data.get('valid_status_combinations', [])

        if not (b_vars and c_vars):
            logger.warning(f"跳过角色 {char_name}: 缺少变体数据")
            continue

        body_map = {v["whole_body_id"]: v for v in b_vars if isinstance(v, dict) and "whole_body_id" in v}
        clothing_map = {v["clothing_id"]: v for v in c_vars if isinstance(v, dict) and "clothing_id" in v}
        active_combos = [c for c in combinations if isinstance(c, dict) and c.get("is_active")]

        if not active_combos:
            logger.debug(f"跳过角色 {char_name}: 无激活组合（未在剧情中说话）")
            continue

        for combo in active_combos:
            combination_str = combo.get("combination", "")
            if not combination_str:
                continue
            try:
                _, _, b_id, c_id = _parse_combination_str(combination_str)
            except ValueError:
                logger.warning(f"跳过无效 combination: {combination_str}")
                continue

            body = body_map.get(b_id, b_vars[0])
            clothing = clothing_map.get(c_id, c_vars[0])
            char_obj = convert_db_json_to_character_info(
                char_name, p_vars[0] if p_vars else {}, body, clothing, b_vars
            )
            if char_obj:
                explicit_id = (
                    char_data.get("speaker_id")
                    or char_data.get("logical_character_id")
                    or char_data.get("character_id")
                    or char_obj.speaker_id
                )
                char_obj.speaker_id = resolve_authoritative_speaker_id(
                    char_name,
                    explicit_id,
                ) or char_obj.speaker_id
                char_obj.combination = combination_str
                characters.append(char_obj)

    return characters


DEFAULT_SHOT = "LS"


def _combination_to_filename(
    combination: str,
    emotion: str = "calm",
    shot: str = DEFAULT_SHOT,
    *,
    prompt_record: Optional[Dict[str, Any]] = None,
    output_dir: Optional[Path] = None,
    prefer_existing: bool = False,
) -> str:
    """'萧无尘-1-1-1' -> 'XiaoWuChen_1-1-1_calm_LS.png'."""
    return combination_filename(
        combination,
        emotion,
        shot,
        prompt_record=prompt_record,
        output_dir=output_dir,
        fallback_converter=chinese_to_camel_case,
        prefer_existing=prefer_existing,
    )


def parse_characters_first_combination(json_data: Dict) -> List[CharacterInfo]:
    """
    每个角色只取数字最小的那个激活组合（按 p_id, b_id, c_id 升序排序取第一个）。
    无激活组合的角色跳过。
    """
    characters = []
    for char_key, char_data in json_data.items():
        if isinstance(char_data, list):
            char_data = char_data[0] if char_data else {}
        if not isinstance(char_data, dict):
            continue

        char_name = char_data.get('name', char_key)
        p_vars = char_data.get('personality_variants', [])
        b_vars = char_data.get('whole_body_variants', [])
        c_vars = char_data.get('clothing_variants', [])
        combinations = char_data.get('valid_status_combinations', [])

        if not (b_vars and c_vars):
            logger.warning(f"跳过角色 {char_name}: 缺少变体数据")
            continue

        active_combos = [c for c in combinations if isinstance(c, dict) and c.get("is_active")]
        if not active_combos:
            logger.debug(f"跳过角色 {char_name}: 无激活组合")
            continue

        # 按 (p_id, b_id, c_id) 升序，取最小的那个
        def _sort_key(combo):
            try:
                _, p, b, c = _parse_combination_str(combo.get("combination", ""))
                return (p, b, c)
            except Exception:
                return (999, 999, 999)

        first_combo = min(active_combos, key=_sort_key)
        combination_str = first_combo.get("combination", "")
        if not combination_str:
            continue

        try:
            _, _, b_id, c_id = _parse_combination_str(combination_str)
        except ValueError:
            logger.warning(f"跳过无效 combination: {combination_str}")
            continue

        body_map = {v["whole_body_id"]: v for v in b_vars if isinstance(v, dict) and "whole_body_id" in v}
        clothing_map = {v["clothing_id"]: v for v in c_vars if isinstance(v, dict) and "clothing_id" in v}
        body = body_map.get(b_id, b_vars[0])
        clothing = clothing_map.get(c_id, c_vars[0])

        char_obj = convert_db_json_to_character_info(
            char_name, p_vars[0] if p_vars else {}, body, clothing, b_vars
        )
        if char_obj:
            char_obj.combination = combination_str
            characters.append(char_obj)

    return characters
    """回退：选最接近18岁的 body + 第一个 clothing。"""
    selected_body = b_vars[0]
    best_diff = float('inf')
    for variant in b_vars:
        age_cat = variant.get('age_category', '')
        age_val = _parse_age_db(variant.get('age', 0))
        diff = abs(age_val - 18)
        if age_cat in ("adolescent", "young adult") and diff < best_diff:
            best_diff = diff
            selected_body = variant
        elif diff < best_diff:
            best_diff = diff
            selected_body = variant
    return convert_db_json_to_character_info(
        char_name, p_vars[0] if p_vars else {}, selected_body, c_vars[0], b_vars
    )


def convert_db_json_to_character_info(name, personality, body, clothing, all_body_variants: List[Dict[str, Any]] = None) -> Optional[CharacterInfo]:
    """
    将数据库JSON片段转换为 CharacterInfo 对象
    适配新的嵌套 appearance 结构 (head/body)

    Args:
        name: 角色名称
        personality: 性格变体数据
        body: 身体变体数据
        clothing: 服装变体数据
        all_body_variants: 所有身体变体列表（用于性别推断）

    Returns:
        Optional[CharacterInfo]: 转换后的角色信息，失败返回None
    """
    try:
        # 1. 基础信息
        gender = body.get('gender', 'unknown')
        if gender == '女':
            gender = 'female'
        elif gender == '男':
            gender = 'male'
        elif gender == 'other':
            gender = 'other'

        # 如果性别为 other 或 unknown，尝试从其他身体变体推断
        if gender in ['other', 'unknown'] and all_body_variants:
            logger.info(f"角色 {name} 的性别为 {gender}，尝试从其他身体变体推断")
            inferred_gender = _infer_gender_from_variants(all_body_variants)
            if inferred_gender:
                gender = inferred_gender
                logger.info(f"角色 {name} 从其他变体推断性别为: {gender}")
            else:
                # 如果所有变体都无法判断，默认为 male
                gender = 'male'
                logger.info(f"角色 {name} 无法从变体推断性别，默认为: male")

        age_category = body.get('age_category', 'young adult')

        # 如果 age 字段为空，根据 age_category 推断年龄
        age_raw = body.get('age')
        if not age_raw or str(age_raw).strip() == '':
            # age 字段为空，根据 age_category 推断
            age_mapping = {
                "infant": 3,
                "child": 9,
                "adolescent": 15,
                "young adult": 23,
                "middle-aged": 40,
                "elderly": 60
            }
            age = age_mapping.get(age_category, 18)
            logger.debug(f"角色 {name} age 字段为空，根据 age_category={age_category} 推断年龄为 {age}")
        else:
            age = _parse_age_db(age_raw)

        # 2. 外观信息 (Appearance) - 适配新结构
        raw_app = body.get('appearance', {})
        head_info = raw_app.get('head', {})
        body_info = raw_app.get('body', {})

        # 提取扁平化的外观数据
        appearance = {
            "hair_color": head_info.get('hair_color', ''),
            "eye_color": head_info.get('eye_color', ''),
            "skin_color": head_info.get('skin_color', ''),
            "hair_style": head_info.get('hair_style', ''),
            "facial_features": [head_info.get('facial_description', '')] if head_info.get('facial_description') else [],
            "body_type": body_info.get('body_type', ''),
            "body_details": body_info.get('body_description', '')
        }

        # 3. 表情信息 (Expression) - 数据库JSON可能没有expression字段
        raw_expr = body.get('expression', {})
        expression = {
            "eyes": raw_expr.get('eyes', '')
        }

        # 4. 服装信息 (Clothing)
        clothing_info = {
            "clothing_style": clothing.get('clothing_style', ''),
            "main_outfit": clothing.get('outfit_discription', ''),  # 注意 JSON 中的 typo: discription
            "accessories": clothing.get('accessories', []),
            "occupation": clothing.get('occupation', '')
        }

        # 5. 性格信息 (Personality)
        personality_info = {
            "main_traits": extract_traits_db(personality.get('main_traits', []))
        }

        # 6. 种族信息 (从body获取)
        race = body.get('race', '人类')

        # 创建 CharacterInfo（适配现有结构）
        character = CharacterInfo(
            name=name,
            speaker_id=resolve_authoritative_speaker_id(
                name,
                chinese_to_camel_case(name),
            ),
            gender=gender,
            age=age,
            age_category=age_category,
            race=race,
            occupation=clothing_info['occupation'],
            appearance=appearance,
            clothing=clothing_info,
            expression=expression,
            personality=personality_info,
            abilities=[],  # 数据库JSON中可能没有abilities字段
            background=""   # 数据库JSON中可能没有background字段
        )

        return character
    except Exception as e:
        logger.error(f"转换数据库角色数据失败 {name}: {e}")
        return None


def load_character_data_from_db(task_id: str = None) -> List[CharacterInfo]:
    """
    从数据库加载角色数据

    Args:
        task_id: 任务ID，如果为None则使用最新的任务

    Returns:
        List[CharacterInfo]: 角色信息列表
    """
    if not HAS_SQLALCHEMY:
        logger.error("sqlalchemy 模块未安装，无法从数据库加载数据")
        return []

    task_id, char_data = fetch_generation_context(task_id)
    if not char_data:
        logger.error("无法从数据库获取角色数据")
        return []

    _init_conversion_cache(task_id)
    characters = parse_characters_first_combination(char_data)
    logger.info(f"从数据库加载了 {len(characters)} 个角色（每角色取最小组合）")
    return characters


def load_prompt_metadata_from_db(
    task_id: str,
) -> Tuple[Dict[str, str], Dict[str, str], Dict[str, Dict[str, Any]]]:
    """
    从 character_prompts 表读取指定 task_id 的提示词与身高。

    返回提示词、身高，以及用于自适应解析文件角色 ID 的原始提示词记录。
    """
    try:
        with platform_engine.connect() as conn:
            row = conn.execute(
                text("SELECT data FROM character_prompts WHERE task_id = :tid"),
                {"tid": task_id}
            ).fetchone()
            if not row:
                logger.error(f"character_prompts 表中未找到 task_id={task_id} 的提示词")
                return {}, {}, {}
            prompts = {}
            heights = {}
            prompt_records = {}
            authoritative_updates = 0
            for raw_item in row[0]:
                if not isinstance(raw_item, dict):
                    continue
                item = apply_authoritative_speaker_id(raw_item, _conversion_cache)
                if item.get("speaker_id") != raw_item.get("speaker_id"):
                    authoritative_updates += 1
                key = item.get("combination") or item.get("speaker_id", "")
                if key:
                    prompts[key] = item["positive_prompt"]
                    heights[key] = str(item.get("height", "")).strip()
                    prompt_records[key] = item
            logger.info(f"从 character_prompts 表加载了 {len(prompts)} 个提示词")
            if authoritative_updates:
                logger.info(
                    "已按 name_mappings 原始 conversion 对齐 %s 条人物提示词 ID",
                    authoritative_updates,
                )
            return prompts, heights, prompt_records
    except Exception as e:
        logger.error(f"读取 character_prompts 失败: {e}")
        return {}, {}, {}


def load_prompts_from_db(task_id: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    """兼容旧调用方，仅返回提示词和身高。"""
    prompts, heights, _ = load_prompt_metadata_from_db(task_id)
    return prompts, heights


# ==================== Keyword Replacement Library ====================

KEYWORD_REPLACEMENTS = {
    "蝎子辫": "scorpion braid (no scorpions)",
    "蝎尾辫": "scorpion tail braid (no scorpions)",
    "沾有血迹": "略有战损痕迹",
    "沾有血污": "略有战损痕迹",
    "沾满黑血的": "战损的",
    "沾满血迹的": "战损的",
    # Blood-related vocabulary cleanup (clean from database descriptions)
    "血迹": "",
    "血污": "",
    "blood": "",
    "bloodstain": "",
    "blood stain": "",
    # Mud and stain-related vocabulary cleanup (clean from database descriptions)
    "泥污": "",
    "污垢": "",
    "mud": "",
    "dirt": "",
    "stain": "",
    "mud stain": "",
    "dirt stain": "",
    # Add more keyword replacements here
}

def apply_keyword_replacements(prompt: str) -> str:
    """
    Apply keyword replacements to prompt

    Args:
        prompt: Original prompt

    Returns:
        str: Prompt after replacement
    """
    result = prompt
    for old, new in KEYWORD_REPLACEMENTS.items():
        result = result.replace(old, new)
    result = re.sub(r'，\s*，+', '，', result)
    result = re.sub(r'，\s*\.', '.', result)
    result = re.sub(r'\s+\.', '.', result)
    result = re.sub(r'沾有(?=[，。,.]|$)', '', result)
    result = re.sub(r'\s{2,}', ' ', result)
    return result.strip()


# ==================== Qwen API 集成 ====================

class QwenImageGenerator:
    """Qwen 图像生成器（简化版）"""

    def __init__(self, api_key: Optional[str] = None):
        """
        初始化生成器

        Args:
            api_key: Qwen API 密钥，如果为None则从环境变量读取
        """
        # 加载配置
        self.config = self._load_config(api_key)

        # 配置 dashscope
        dashscope.api_key = self.config['qwen_api_key']
        dashscope.base_http_api_url = os.getenv(
            "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/api/v1"
        )

        logger.info(f"Qwen 图像生成器初始化完成，模型: {self.config['qwen_model']}")

    def _load_config(self, api_key: Optional[str] = None) -> Dict[str, Any]:
        """加载配置"""
        # 从参数或环境变量获取API密钥
        qwen_api_key = api_key or os.environ.get("DASHSCOPE_API_KEY", "")
        if not qwen_api_key:
            logger.error("API Key 未设置，请设置 DASHSCOPE_API_KEY 环境变量或通过参数传递")
            raise ValueError("API Key 未设置")

        # 默认配置
        config = {
            # Qwen API 配置
            "qwen_api_key": qwen_api_key,
            "qwen_model": os.environ.get("QWEN_MODEL", "qwen-image-plus"),

            # 输出配置（由外部参数指定）
            "output_dir": "",

            # Image generation configuration
            "image_style": "Polished full-body character asset on a pure white background",
            "aspect_ratio": os.environ.get("QWEN_ASPECT_RATIO", "3:4"),
            "image_quality": os.environ.get("QWEN_IMAGE_QUALITY", "standard"),
            "image_count": int(os.environ.get("QWEN_IMAGE_COUNT", "1")),

            # 处理配置
            "max_retries": int(os.environ.get("QWEN_MAX_RETRIES", "3")),
            "retry_delay": int(os.environ.get("QWEN_RETRY_DELAY", "5")),
            "timeout_seconds": int(os.environ.get("QWEN_TIMEOUT_SECONDS", "120")),

            # 批量处理配置
            "batch_size": int(os.environ.get("QWEN_BATCH_SIZE", "1")),
            "delay_between_batches": int(os.environ.get("QWEN_DELAY_BETWEEN_BATCHES", "10"))
        }

        return config

    def _parse_aspect_ratio(self, aspect_ratio: str) -> Tuple[int, int]:
        """解析宽高比字符串为宽高元组"""
        try:
            width, height = map(int, aspect_ratio.split(':'))
            return width, height
        except:
            # 默认返回 3:4
            return 3, 4

    def _get_image_size(self, aspect_ratio: str) -> str:
        """
        根据宽高比获取 Qwen 支持的图像尺寸

        qwen-image-plus 支持的尺寸映射：
        - 1664*928 (约 16:9)
        - 1472*1140 (约 4:3)
        - 1328*1328 (1:1)
        - 1140*1472 (约 3:4)
        - 928*1664 (约 9:16)
        """
        width, height = self._parse_aspect_ratio(aspect_ratio)

        if width == 1 and height == 1:
            return "1328*1328"  # 1:1
        elif width == 3 and height == 4:
            return "1140*1472"  # 约 3:4
        elif width == 4 and height == 3:
            return "1472*1140"  # 约 4:3
        elif width == 9 and height == 16:
            return "928*1664"   # 约 9:16
        elif width == 16 and height == 9:
            return "1664*928"   # 约 16:9
        else:
            # 默认使用 3:4
            return "1140*1472"

    def submit_qwen_task(self, prompt: str) -> Optional[str]:
        """
        提交 Qwen 图像生成任务

        Args:
            prompt: 提示词

        Returns:
            Optional[str]: 图片URL，失败返回None
        """
        # 根据宽高比确定图像尺寸
        image_size = self._get_image_size(self.config['aspect_ratio'])

        # 构建消息
        messages = [
            {
                "role": "user",
                "content": [
                    {"text": prompt}
                ]
            }
        ]

        for attempt in range(self.config['max_retries']):
            try:
                qwen_timeout = int(self.config.get('qwen_timeout', 180))
                logger.info(f"提交 Qwen 图像生成任务 (尝试 {attempt + 1}/{self.config['max_retries']}, 超时 {qwen_timeout}s)")

                response = MultiModalConversation.call(
                    model=self.config['qwen_model'],
                    messages=messages,
                    result_format='message',
                    stream=False,
                    watermark=False,
                    prompt_extend=True,
                    negative_prompt='',
                    size=image_size,
                    request_timeout=qwen_timeout,  # DashScope SDK 用 request_timeout
                )

                if response.status_code == 200:
                    # 从响应中提取图片URL
                    image_url = None

                    # 尝试从不同路径提取（支持字典和对象格式）
                    try:
                        # 获取output部分
                        output = None
                        if hasattr(response, 'output'):
                            output = response.output
                        elif hasattr(response, 'get'):
                            output = response.get('output')

                        if output:
                            # 获取choices列表
                            choices = None
                            if hasattr(output, 'choices'):
                                choices = output.choices
                            elif isinstance(output, dict) and 'choices' in output:
                                choices = output['choices']

                            if choices and len(choices) > 0:
                                # 获取第一个choice
                                choice = choices[0]
                                # 获取message
                                message = None
                                if hasattr(choice, 'message'):
                                    message = choice.message
                                elif isinstance(choice, dict) and 'message' in choice:
                                    message = choice['message']

                                if message:
                                    # 获取content列表
                                    content_list = None
                                    if hasattr(message, 'content'):
                                        content_list = message.content
                                    elif isinstance(message, dict) and 'content' in message:
                                        content_list = message['content']

                                    if content_list:
                                        for content in content_list:
                                            # 获取image URL
                                            if hasattr(content, 'image'):
                                                image_url = content.image
                                            elif isinstance(content, dict) and 'image' in content:
                                                image_url = content['image']
                                            if image_url:
                                                break
                    except Exception as e:
                        logger.error(f"提取图片URL时发生异常: {e}")

                    if image_url:
                        logger.info(f"任务提交成功，图片URL: {image_url[:50]}...")
                        return image_url
                    else:
                        logger.error(f"未获取到图片URL，返回数据: {response}")

                else:
                    logger.error(f"提交任务失败 (HTTP {response.status_code}): {response.message}")

            except Exception as e:
                logger.error(f"提交任务异常 (尝试 {attempt + 1}): {e}")

            if attempt < self.config['max_retries'] - 1:
                time.sleep(self.config['retry_delay'])

        return None

    def download_image(self, image_url: str, output_path: str) -> bool:
        """
        下载图片

        Args:
            image_url: 图片URL
            output_path: 输出路径

        Returns:
            bool: 是否成功
        """
        try:
            response = requests.get(image_url, timeout=30, stream=True)
            if response.status_code == 200:
                with open(output_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        f.write(chunk)
                logger.info(f"图片下载成功: {output_path}")
                return True
            else:
                logger.error(f"下载图片失败 (HTTP {response.status_code})")
                return False
        except Exception as e:
            logger.error(f"下载图片异常: {e}")
            return False

    def generate_for_character(self, character: CharacterInfo, prompt: str,
                              output_dir: str, state_index: int = 1,
                              filename_override: str = None) -> Tuple[bool, Optional[str]]:
        """
        为单个角色生成图片

        Args:
            character: 角色信息
            prompt: 文生图正向提示词（从 character_prompts 表读取）
            output_dir: 输出目录
            state_index: 状态索引（filename_override 优先）
            filename_override: 直接指定文件名（含扩展名）

        Returns:
            Tuple[bool, Optional[str]]: (是否成功, 图片路径)
        """
        logger.info(f"开始处理角色: {character.name} -> {character.speaker_id}")

        # 提交 Qwen 任务
        image_url = self.submit_qwen_task(prompt)
        if not image_url:
            logger.error(f"提交任务失败: {character.name}")
            return False, None

        # 生成图片文件名
        filename = filename_override if filename_override else f"{character.speaker_id}_{state_index}_calm_{DEFAULT_SHOT}.png"
        image_path = os.path.join(output_dir, filename)

        # 下载图片
        if not self.download_image(image_url, image_path):
            logger.error(f"下载图片失败: {character.name}")
            return False, None

        # 不再保存提示词（根据用户要求）

        logger.info(f"角色 {character.name} 图片已保存: {filename}")
        return True, image_path


# ==================== 主函数 ====================

def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description="角色立绘生成器 - 从数据库生成角色图片",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  # 从数据库加载指定任务的角色数据并生成图片
  python generate_clam.py --task-id specific_task_001

  # 测试模式（只生成提示词，不调用API）
  python generate_clam.py --task-id test_task --test

环境变量:
  - DASHSCOPE_API_KEY: Qwen API Key（必需）
  - DATABASE_URL: 数据库连接URL（默认: postgresql://appuser:change_me@localhost:5432/test3）
  - QWEN_MODEL: 模型名称（默认: qwen-image-plus）
  - QWEN_ASPECT_RATIO: 宽高比（默认: 3:4）
  - QWEN_IMAGE_QUALITY: 图像质量（默认: standard）
        """
    )

    parser.add_argument("--task-id", type=str, required=True,
                       help="任务ID，用于构建输出路径: char_designer/outputs/{task_id}/images/characters，并从数据库加载角色数据")
    parser.add_argument("--output-dir", type=str,
                       help="输出目录（已弃用，使用 --task-id 替代）")
    parser.add_argument("--test", action="store_true",
                       help="测试模式：只生成提示词，不调用API")
    parser.add_argument("--api-key", type=str,
                       help="当前 image provider 使用的 API Key（Qwen 使用 DASHSCOPE_API_KEY，GPT 使用 GPT_IMAGE_API_KEY / OPENAI_API_KEY）")
    parser.add_argument("--image-provider", choices=["qwen", "wan", "gpt"], default="gpt",
                       help="图片模型提供方，默认 gpt")
    parser.add_argument("--base-url",
                       default=os.getenv("GPT_IMAGE_BASE_URL", "https://ai.farmmx.com"),
                       help="GPT Image 兼容接口 Base URL（默认读环境变量 GPT_IMAGE_BASE_URL）")
    parser.add_argument("--model", default=None,
                       help="图片模型名；qwen 默认 QWEN_MODEL/qwen-image-plus，gpt 默认 gpt-image-2")
    parser.add_argument("--size", default=None,
                       help="图片尺寸；qwen 格式如 1140*1472，gpt 格式如 1024x1536")
    parser.add_argument("--quality", default="high",
                       help="GPT Image 质量：low / medium / high / auto")
    parser.add_argument("--response-format", default="",
                       help="GPT Image 返回格式：url / b64_json，留空则不传")
    parser.add_argument("--output-format", default="",
                       help="GPT Image 输出格式：png / jpeg / webp，留空则不传")
    parser.add_argument("--background", default="",
                       help="GPT Image 背景；正式人物透明合同会强制使用 transparent")
    parser.add_argument("--moderation", default="",
                       help="GPT Image 审核：auto / low，留空则不传")
    parser.add_argument("--timeout", type=int, default=300,
                       help="GPT Image 请求超时时间（秒）")
    parser.add_argument("--verbose", "-v", action="store_true",
                       help="详细输出模式")

    args = parser.parse_args()

    # 警告已弃用的参数
    if args.output_dir:
        logger.warning("--output-dir 参数已弃用，使用 --task-id 替代")

    # 设置日志级别
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # 检查sqlalchemy是否安装
    if not HAS_SQLALCHEMY:
        logger.error("sqlalchemy 模块未安装，无法从数据库加载数据。请安装: pip install sqlalchemy")
        sys.exit(1)

    # 从数据库加载角色数据
    characters = load_character_data_from_db(args.task_id)
    if not characters:
        logger.error("从数据库加载角色数据失败")
        sys.exit(1)

    if _usage_tracker and args.task_id:
        _usage_tracker.set_context(
            args.task_id, "char_designer", force_new_run=True
        )

    logger.info(f"从数据库加载了 {len(characters)} 个角色")
    for character in characters:
        logger.info(f"  角色: {character.name} -> {character.speaker_id} ({character.gender}, {character.age}岁, {character.occupation})")

    # 从 character_prompts 表读取提示词
    prompts, heights, prompt_records = load_prompt_metadata_from_db(args.task_id)
    if not prompts:
        logger.error("从 character_prompts 表加载提示词失败，请先运行 char_prompt_builder.py")
        sys.exit(1)

    style_plan = resolve_asset_style_plan(args.task_id, ASSET_CHARACTER)
    resolved_model = args.model or {
        "gpt": "gpt-image-2",
        "qwen": "qwen-image-plus",
        "wan": "wan2.5-t2i-preview",
    }[args.image_provider]
    output_policy = resolve_character_output_policy(
        args.task_id,
        image_provider=args.image_provider,
        model=resolved_model,
        requested_background=args.background,
        requested_output_format=args.output_format,
    )
    asset_validator = validator_for_policy(output_policy)
    logger.info(
        "人物美术风格路由: mode=%s style=%s anchor=%s",
        style_plan.mode,
        style_plan.style,
        style_plan.anchor_path or "(none)",
    )

    logger.info("人物输出合同: %s", output_policy.digest_payload())

    # 复用 common.config 在进程启动时解析出的统一输出目录。
    # 子进程会继承 Worker 的 STORAGE_PATH；这里不能再用 override=True 加载
    # 仓库 .env，否则 Docker 中的 /storage 会被开发机绝对路径覆盖。
    output_dir = Path(OUTPUTS_PATH) / args.task_id / "images" / "characters"
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"图片输出目录: {output_dir}")

    # 测试模式：只打印提示词预览
    if args.test:
        logger.info("测试模式：打印提示词预览，不调用API")

        for character in characters:
            key = character.combination or character.speaker_id
            prompt = prompts.get(key, "")
            if not prompt:
                logger.warning(f"未找到角色 {character.speaker_id} (combination={character.combination}) 的提示词")
                continue
            prompt_record = prompt_records.get(key)
            filename = _combination_to_filename(
                character.combination,
                prompt_record=prompt_record,
            ) if character.combination else f"{character.speaker_id}_calm_{DEFAULT_SHOT}.png"
            print(f"\n{'='*60}")
            print(f"角色: {character.name} -> {character.speaker_id} combination={character.combination}")
            print(f"文件名: {filename}")
            prompt = apply_asset_style(style_plan, prompt)
            provider_prompt = compose_anchor_guided_prompt(style_plan, prompt)
            final_provider_prompt = provider_prompt
            print(f"提示词预览: {provider_prompt[:500]}...")
            print(f"{'='*60}")

        logger.info(f"测试模式完成，共 {len(characters)} 个角色组合")
        return

    # 保留上一份有效图片。每张新图会经 staging 校验后原子替换同名文件；
    # 若本轮 Provider/透明合同校验失败，旧文件仍可供重投与人工审查使用。
    logger.info(f"保留已有角色图片并按文件原子更新: {output_dir}")

    # 创建图像 provider。并发开启时每个 worker thread 独立持有实例，避免
    # last_prompt / prompt_profile 等可变审计状态串图。
    def build_generator():
        if args.image_provider in {"qwen", "wan"}:
            if style_plan.uses_anchor:
                # qwen / wan 的自定义风格都经由项目现有 Qwen Edit
                # 把用户已确认 Anchor 转换为角色基础立绘。
                return QwenImageEditProvider(
                    api_key=args.api_key,
                    base_url=args.base_url,
                    model=os.environ.get("QWEN_VARIANT_MODEL", "qwen-image-edit-plus"),
                    size=os.environ.get("QWEN_EDIT_CHARACTER_SIZE", "1140*1472"),
                    max_retries=int(os.environ.get("QWEN_MAX_RETRIES", "3")),
                    retry_delay=int(os.environ.get("QWEN_RETRY_DELAY", "5")),
                )
            else:
                provider_cls = QwenTextImageProvider if args.image_provider == "qwen" else Wan25TextImageProvider
                return provider_cls(
                    api_key=args.api_key,
                    model=args.model,
                    size=args.size,
                    max_retries=int(os.environ.get("QWEN_MAX_RETRIES", "3")),
                    retry_delay=int(os.environ.get("QWEN_RETRY_DELAY", "5")),
                )
        api_key = args.api_key or os.getenv("GPT_IMAGE_API_KEY") or os.getenv("OPENAI_API_KEY")
        provider_cls = GPTImageEditProvider if style_plan.uses_anchor else GPTImageTextProvider
        return provider_cls(
            api_key=api_key,
            base_url=args.base_url,
            model=args.model or "gpt-image-2",
            size=args.size or "1024x1536",
            quality=args.quality,
            n=1,
            response_format=args.response_format,
            output_format=output_policy.output_format,
            background=output_policy.background,
            moderation=args.moderation,
            timeout_seconds=args.timeout,
            max_retries=3,
            retry_delay=5,
            transparent_character_output=output_policy.native_transparency,
        )

    try:
        generator = build_generator()
    except ValueError as e:
        logger.error(f"初始化图像 provider 失败: {e}")
        sys.exit(1)
    generators = ThreadLocalResource(build_generator, initial=generator)

    # 批量生成图片
    logger.info(f"开始批量生成图片，输出目录: {output_dir}")

    results = {
        "total": len(characters),
        "success": 0,
        "failed": 0,
        "failed_characters": [],
        "generated_files": []
    }

    def process_character(character):
        checkpoint_digest = ""
        asset_id = str(character.combination or character.speaker_id)
        try:
            key = character.combination or character.speaker_id
            prompt = prompts.get(key, "")
            if not prompt:
                logger.warning(f"跳过角色 {character.speaker_id} (combination={character.combination})：未找到对应提示词")
                return {
                    "success": False,
                    "character": character.name,
                    "generated_file": None,
                }
            # Provider 边界统一执行资产级风格合同。LLM 自动路线和
            # custom 路线均使用内容优先提示词 + 对应人物 Anchor 图生图。
            prompt = apply_asset_style(style_plan, prompt)
            provider_prompt = compose_anchor_guided_prompt(style_plan, prompt)

            # 文件名：人物名_p-b-c_calm.png
            prompt_record = prompt_records.get(key)
            resolved_speaker_id = str(
                (prompt_record or {}).get("speaker_id") or character.speaker_id
            )
            if character.combination:
                filename_base = _combination_to_filename(
                    character.combination,
                    "calm",
                    prompt_record=prompt_record,
                )
            else:
                filename_base = f"{resolved_speaker_id}_calm_{DEFAULT_SHOT}.png"

            image_path = output_dir / filename_base
            final_provider_prompt = provider_prompt
            checkpoint_digest = canonical_digest(
                {
                    "prompt": provider_prompt,
                    "prompt_record": prompt_record or {},
                    "character": {
                        "name": character.name,
                        "speaker_id": resolved_speaker_id,
                        "combination": character.combination,
                        "gender": character.gender,
                        "age": character.age,
                        "race": character.race,
                        "occupation": character.occupation,
                    },
                    "style": {
                        "mode": style_plan.mode,
                        "style": style_plan.style,
                        "anchor_sha256": style_plan.anchor_sha256,
                    },
                    "provider": args.image_provider,
                    "model": args.model or "gpt-image-2",
                    "size": args.size or "1024x1536",
                    "quality": args.quality,
                    "output_contract": output_policy.digest_payload(),
                }
            )
            decision = reusable_or_adopt_existing(
                args.task_id,
                "char_designer",
                "generate_base_calm",
                asset_id,
                input_digest=checkpoint_digest,
                generator_version=CALM_GENERATOR_VERSION,
                output_paths=[
                    character_resume_source_path(
                        args.task_id, image_path, fetch_remote=True
                    )
                ],
                validator=asset_validator,
            )
            if decision.reusable:
                restore_character_working_copy(args.task_id, image_path)
                logger.info(
                    "复用有效角色基础立绘: %s (%s)",
                    image_path,
                    decision.reason,
                )
                return {
                    "success": True,
                    "character": character.name,
                    "generated_file": {
                        "character": character.name,
                        "speaker_id": resolved_speaker_id,
                        "combination": character.combination,
                        "filename": filename_base,
                        "path": str(image_path),
                    },
                }
            assert_retry_budget(
                args.task_id,
                "char_designer",
                "generate_base_calm",
                asset_id,
                input_digest=checkpoint_digest,
                generator_version=CALM_GENERATOR_VERSION,
            )
            checkpoint_mark_running(
                args.task_id,
                "char_designer",
                "generate_base_calm",
                asset_id,
                input_digest=checkpoint_digest,
                generator_version=CALM_GENERATOR_VERSION,
            )
            generator = generators.get()
            with atomic_image_output(
                image_path,
                artifact_validator=asset_validator,
            ) as temporary_path:
                if args.image_provider in {"qwen", "wan"}:
                    if style_plan.uses_anchor:
                        image_url = generator.generate(style_plan.anchor_path, provider_prompt)
                        usage_provider = "qwen"
                    else:
                        image_url = generator.generate(provider_prompt)
                        usage_provider = args.image_provider
                    if image_url and _usage_tracker:
                        _usage_tracker.add_image(
                            1, generator.model, provider=usage_provider
                        )
                    success = bool(
                        image_url and generator.download(image_url, temporary_path)
                    )
                else:
                    persistence = None
                    if character.combination and not style_plan.uses_anchor:
                        persistence = PromptPersistenceContext(
                            task_id=args.task_id,
                            speaker_id=resolved_speaker_id,
                            combination=character.combination,
                        )
                    operation = (
                        (lambda repaired_prompt: generator.edit(style_plan.anchor_path, repaired_prompt))
                        if style_plan.uses_anchor
                        else generator.generate
                    )
                    data_items, prompt = execute_with_prompt_repair(
                        operation,
                        provider_prompt,
                        generation_mode="image_to_image" if style_plan.uses_anchor else "text_to_image",
                        stage=f"generate_calm:{character.combination or character.speaker_id}",
                        persistence=persistence,
                    )
                    # 默认文生图保持原有审核修复入库逻辑；Anchor 包装语只是
                    # Provider 请求指令，不反写 character_prompts 语义提示词。
                    if not style_plan.uses_anchor:
                        prompts[key] = prompt
                    final_provider_prompt = prompt
                    if data_items and _usage_tracker:
                        _usage_tracker.add_image(
                            len(data_items), generator.model, provider=args.image_provider
                        )
                    success = bool(
                        data_items
                        and generator.save_image_item(data_items[0], temporary_path)
                    )
                    final_provider_prompt = str(
                        getattr(generator, "last_prompt", prompt)
                    )

                if not success:
                    raise CharacterImageArtifactError(
                        f"Provider 未生成有效角色图片: {image_path}"
                    )

            references = []
            if style_plan.uses_anchor:
                references.append(
                    {"path": style_plan.anchor_path, "role": "人物风格 Anchor"}
                )
            transparency_metrics = (
                inspect_native_transparency(image_path)
                if output_policy.require_transparency
                else {}
            )
            if transparency_metrics:
                logger.info(
                    "原生透明人物立绘校验通过: path=%s mode=%s alpha=%s-%s transparent_ratio=%.4f",
                    image_path,
                    transparency_metrics.get("mode"),
                    transparency_metrics.get("alpha_min"),
                    transparency_metrics.get("alpha_max"),
                    transparency_metrics.get("fully_transparent_ratio", 0.0),
                )
            record_visual_generation(
                args.task_id,
                "characters",
                image_path,
                prompt=final_provider_prompt,
                generation_mode=(
                    "image_to_image" if style_plan.uses_anchor else "text_to_image"
                ),
                reference_images=references,
                logic=(
                    [
                        "人物基础 calm 立绘：上游人物语义要求优先。",
                        "人物 Anchor 仅控制媒介、线条、色彩、光影与材质。",
                    ]
                    if style_plan.uses_anchor
                    else ["人物基础 calm 立绘：使用上游人物提示词直接文生图。"]
                ),
                model=str(getattr(generator, "model", args.model or "")),
                provider=args.image_provider,
                extra={
                    "combination": character.combination,
                    "character_output_policy": output_policy.digest_payload(),
                    "transparency_metrics": transparency_metrics,
                },
            )
            resume_source = publish_character_resume_source(args.task_id, image_path)
            checkpoint_mark_success(
                args.task_id,
                "char_designer",
                "generate_base_calm",
                asset_id,
                input_digest=checkpoint_digest,
                generator_version=CALM_GENERATOR_VERSION,
                output_paths=[resume_source],
                validator=asset_validator,
            )
            logger.info(f"✓ 角色 {character.name} ({filename_base}) 图片生成成功")
            return {
                "success": True,
                "character": character.name,
                "generated_file": {
                    "character": character.name,
                    "speaker_id": resolved_speaker_id,
                    "combination": character.combination,
                    "filename": filename_base,
                    "path": str(image_path),
                },
            }

        except Exception as e:
            logger.error(f"处理角色 {character.name} 时发生异常: {e}")
            if checkpoint_digest:
                try:
                    checkpoint_mark_failed(
                        args.task_id,
                        "char_designer",
                        "generate_base_calm",
                        asset_id,
                        input_digest=checkpoint_digest,
                        generator_version=CALM_GENERATOR_VERSION,
                        error=str(e),
                    )
                except Exception:
                    logger.exception("记录角色基础立绘失败 checkpoint 时发生异常")
            return {
                "success": False,
                "character": character.name,
                "generated_file": None,
            }

    character_results = run_dependency_layer(
        characters,
        process_character,
        concurrency=resolve_image_concurrency("CHAR_CALM_IMAGE_CONCURRENCY"),
        stage="character_calm",
        task_id=args.task_id,
        label=lambda character: str(character.combination or character.speaker_id),
        logger=logger,
    )
    for item in character_results:
        if item["success"]:
            results["success"] += 1
            results["generated_files"].append(item["generated_file"])
        else:
            results["failed"] += 1
            results["failed_characters"].append(item["character"])

    # 顶部透明画布扩展已移到独立步骤 expand_canvas.py，
    # 在 generate_variant_calm.py 之后、generate_emotion.py 之前统一执行，
    # 避免 calm 图被提前扩展导致变体图生图时把空白画布吃进 base 影响构图。

    # 输出总结
    print("\n" + "="*60)
    print("处理完成!")
    print("="*60)
    print(f"总角色数: {results['total']}")
    print(f"成功: {results['success']}")
    print(f"失败: {results['failed']}")
    print(f"成功率: {results['success']/results['total']:.2%}" if results['total'] > 0 else "成功率: 0%")
    print(f"输出目录: {output_dir}")

    if results['failed_characters']:
        print(f"失败的角色: {', '.join(results['failed_characters'])}")

    if results['success'] > 0:
        print("\n生成的文件:")
        for file_info in results['generated_files']:
            print(f"  ✓ {file_info['filename']}")

    print("="*60)

    if _usage_tracker:
        _usage_tracker.flush()

    # Exit code 2 means that individual assets failed but the batch completed;
    # the parent char_designer pipeline records those assets and continues.
    # Exit code 1 remains reserved for stage/process-level exceptions.
    return 2 if results["failed"] else 0



if __name__ == "__main__":
    raise SystemExit(main())
