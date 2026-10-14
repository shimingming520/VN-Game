"""
故事树角色合并器 (Story Tree Character Merger)
专门用于合并和去重03_story_tree_raw中的角色数据，生成04_story_tree_merged格式的数据
完全使用大模型进行操作，不包含规则逻辑
"""
import os
import sys
import json
import logging
import re
import time
import yaml
from pathlib import Path
from typing import Dict, Any, List, Optional
from collections import defaultdict

# 解决直接运行时的相对导入问题
import sys
from pathlib import Path

# 将项目根目录添加到 sys.path（已禁用，char_designer 独立）
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent.parent  # char_designer/core -> char_designer -> project root
# if str(project_root) not in sys.path:
#     sys.path.insert(0, str(project_root))
# 确保当前目录在 sys.path 中，以便导入同级模块
if str(current_dir) not in sys.path:
    sys.path.insert(0, str(current_dir))

# 尝试多种导入方式
# 从 utils 导入 llm_client（相对导入）
from ..utils.llm_client import LLMClient, LLMProvider
from .template_renderer import render_prompt_template

# 配置日志
import logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

_PERSONALITY_LEGACY_FIELDS = ("main_traits", "secondary_traits", "flaws", "virtues")
_TRANSIENT_PERSONALITY_STATES = {
    "愤怒", "生气", "悲伤", "难过", "惊讶", "震惊", "恐惧", "害怕",
    "厌恶", "恶心", "开心", "高兴", "紧张",
    "angry", "anger", "sad", "sadness", "surprised", "surprise",
    "shocked", "fearful", "fear", "afraid", "disgusted", "disgust",
    "happy", "happiness", "nervous", "nervousness", "shock",
}


def _normalize_personality_traits(value: Any, max_traits: int = 8) -> List[str]:
    values = value if isinstance(value, list) else re.split(r"[，,、；;\n]", value) if isinstance(value, str) else []
    result: List[str] = []
    seen = set()
    for item in values:
        if not isinstance(item, str):
            continue
        trait = item.strip()
        key = trait.casefold()
        if not trait or key in _TRANSIENT_PERSONALITY_STATES or key in seen:
            continue
        seen.add(key)
        result.append(trait)
        if len(result) >= max_traits:
            break
    return result


def _normalize_personality_variant(variant: Dict[str, Any], default_id: int) -> Dict[str, Any]:
    normalized = dict(variant)
    try:
        normalized["personality_id"] = int(normalized.get("personality_id", default_id))
    except (TypeError, ValueError):
        normalized["personality_id"] = default_id
    normalized["traits"] = _normalize_personality_traits(normalized.get("traits", []))
    for field in _PERSONALITY_LEGACY_FIELDS:
        normalized.pop(field, None)
    return normalized

def load_prompt_from_yaml(filename: str) -> str:
    """
    从 char_prompts 目录加载 YAML 文件并返回 content 字段。
    文件名相对于当前文件的 ../prompts/char_prompts/ 目录。
    """
    prompt_dir = Path(__file__).parent.parent / "prompts" / "char_prompts"
    filepath = prompt_dir / filename
    if not filepath.exists():
        raise FileNotFoundError(f"Prompt file not found: {filepath}")
    with open(filepath, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f)
    content = data.get('content', '')
    if not content:
        raise ValueError(f"Prompt file {filename} has empty content field")
    return content.strip()


def _ordered_unique_strings(values: List[Any]) -> List[str]:
    """保持首次出现顺序，对字符串列表去空、去重。"""
    result: List[str] = []
    seen = set()
    for value in values:
        if not isinstance(value, str):
            continue
        normalized = value.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _merge_info_values(*values: Any) -> str:
    flattened: List[Any] = []
    def append_value(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                append_value(item)
        else:
            flattened.append(value)
    for value in values:
        append_value(value)
    return "\n".join(_ordered_unique_strings(flattened))


def _merge_refs(*values: Any) -> List[str]:
    flattened: List[Any] = []
    def append_value(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                append_value(item)
        elif value:
            flattened.append(value)
    for value in values:
        append_value(value)
    return _ordered_unique_strings(flattened)


def _normalize_evidence_fields(
    data: Dict[str, Any],
    source_versions: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """保证人物顶层、变体和组合的 info/refs 在二次合并后仍然存在。"""
    sources = source_versions or []
    source_infos = [source.get("info") for source in sources if isinstance(source, dict)]
    source_refs = [source.get("refs", []) for source in sources if isinstance(source, dict)]

    data["info"] = str(data.get("info") or "").strip() or _merge_info_values(source_infos)
    data["refs"] = _merge_refs(data.get("refs", []), source_refs)

    personality_variants = data.get("personality_variants", [])
    if isinstance(personality_variants, list):
        data["personality_variants"] = [
            _normalize_personality_variant(item, index)
            for index, item in enumerate(personality_variants, 1)
            if isinstance(item, dict)
        ]

    clothing_variants = data.get("clothing_variants", [])
    if isinstance(clothing_variants, list):
        for item in clothing_variants:
            if not isinstance(item, dict):
                continue
            for field in ("accessories", "tmp_accessories", "weapons", "armor"):
                values = item.get(field, [])
                if not isinstance(values, list):
                    values = [values]
                item[field] = _ordered_unique_strings(values)

    for field in (
        "personality_variants",
        "whole_body_variants",
        "clothing_variants",
        "valid_status_combinations",
    ):
        items = data.get(field, [])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            item["info"] = str(item.get("info") or "").strip()
            item["refs"] = _merge_refs(item.get("refs", []))

    return data


def _simplify_character_entry(char_val: Any) -> Optional[Dict[str, Any]]:
    """characters 值可能是 dict 或 [dict]，取出角色档案本体。"""
    if isinstance(char_val, list):
        char_val = char_val[0] if char_val else None
    return char_val if isinstance(char_val, dict) else None


def _build_simplify_user_prompt(merged: Dict[str, Dict[str, Any]]) -> str:
    """把角色清单（名称 + info 摘要）序列化为人名精简的 user prompt。"""
    roster: List[Dict[str, str]] = []
    for char_key, char_val in merged.items():
        entry = _simplify_character_entry(char_val)
        info = str(entry.get("info") or "").strip() if entry is not None else ""
        roster.append({"name": char_key, "info": info})
    return "角色清单如下（按顺序）：\n" + json.dumps(roster, ensure_ascii=False, indent=2)


def _validate_simplify_rename_map(
    rename_map: Any, original_keys: List[str]
) -> Optional[Dict[str, str]]:
    """校验 LLM 返回的映射：类型正确、覆盖全部原 key、新名无重复。

    通过返回规范化后的 {原key: 新名}；不通过返回 None（调用方回退不改名）。
    """
    if not isinstance(rename_map, dict):
        logger.warning("人名精简：LLM 未返回 rename_map 字典，回退为不改名")
        return None

    normalized: Dict[str, str] = {}
    for old_key in original_keys:
        new_name = rename_map.get(old_key)
        if not isinstance(new_name, str) or not new_name.strip():
            logger.warning(f"人名精简：角色「{old_key}」缺少有效新名，回退为不改名")
            return None
        normalized[old_key] = new_name.strip()

    new_names = list(normalized.values())
    if len(set(new_names)) != len(new_names):
        dupes = sorted({n for n in new_names if new_names.count(n) > 1})
        logger.warning(f"人名精简：规范名称存在重复 {dupes}，回退为不改名")
        return None

    return normalized


def _rewrite_combination_prefix(combination: str, new_name: str) -> str:
    """把 combination 的名字前缀替换为新名。

    格式为 "{name}-{p}-{b}-{c}"，与 extract_combination_description._parse_combination 的
    rsplit("-", 3) 解析对称；名字中可能含连字符，故只替换最左侧的名字部分。
    """
    parts = combination.rsplit("-", 3)
    if len(parts) != 4:
        return combination
    _, p_id, b_id, c_id = parts
    return f"{new_name}-{p_id}-{b_id}-{c_id}"


def _apply_simplify_rename(
    merged: Dict[str, Dict[str, Any]], rename_map: Dict[str, str]
) -> Dict[str, Dict[str, Any]]:
    """按映射改写每个角色的 key、name、other_names 与 combination 前缀。"""
    result: Dict[str, Dict[str, Any]] = {}
    for old_key, char_val in merged.items():
        new_name = rename_map.get(old_key, old_key)
        entry = _simplify_character_entry(char_val)

        if entry is None or new_name == old_key:
            result[new_name] = char_val
            continue

        entry["name"] = new_name

        # 旧名 prepend 进 other_names 留档（去重、保序）。
        other_names = entry.get("other_names")
        if not isinstance(other_names, list):
            other_names = [other_names] if other_names else []
        deduped: List[str] = []
        seen = set()
        for candidate in [old_key] + other_names:
            if isinstance(candidate, str) and candidate.strip() and candidate not in seen:
                seen.add(candidate)
                deduped.append(candidate)
        entry["other_names"] = deduped

        # 同步重写 valid_status_combinations 的名字前缀。
        combos = entry.get("valid_status_combinations")
        if isinstance(combos, list):
            for combo in combos:
                if isinstance(combo, dict) and isinstance(combo.get("combination"), str):
                    combo["combination"] = _rewrite_combination_prefix(
                        combo["combination"], new_name
                    )

        result[new_name] = char_val

    return result


def _deduplicate_variants_for_fallback(
    variants: List[Dict[str, Any]],
    id_field: str,
) -> List[Dict[str, Any]]:
    """Fallback deduplication; tmp_accessories is mergeable metadata, not identity."""
    seen: Dict[str, Dict[str, Any]] = {}
    deduplicated: List[Dict[str, Any]] = []

    for variant in variants:
        if not isinstance(variant, dict):
            continue
        comparable = {
            key: value
            for key, value in variant.items()
            if key not in {id_field, "info", "refs", "tmp_accessories", "height_ai_guess"}
        }
        variant_key = json.dumps(comparable, sort_keys=True, ensure_ascii=False)
        if variant_key not in seen:
            merged_variant = variant.copy()
            merged_variant[id_field] = len(deduplicated) + 1
            merged_variant["info"] = str(merged_variant.get("info") or "").strip()
            merged_variant["refs"] = _merge_refs(merged_variant.get("refs", []))
            if id_field == "clothing_id":
                tmp_accessories = merged_variant.get("tmp_accessories", [])
                if not isinstance(tmp_accessories, list):
                    tmp_accessories = [tmp_accessories]
                merged_variant["tmp_accessories"] = _ordered_unique_strings(tmp_accessories)
            seen[variant_key] = merged_variant
            deduplicated.append(merged_variant)
            continue

        existing = seen[variant_key]
        existing["info"] = _merge_info_values(existing.get("info"), variant.get("info"))
        existing["refs"] = _merge_refs(existing.get("refs", []), variant.get("refs", []))
        if id_field == "clothing_id":
            incoming = variant.get("tmp_accessories", [])
            if not isinstance(incoming, list):
                incoming = [incoming]
            existing["tmp_accessories"] = _ordered_unique_strings(
                list(existing.get("tmp_accessories", [])) + incoming
            )

    return deduplicated


# 非实体角色（系统/旁白等）：没有真实身高，跳过身高校验。
# 与 plot/extract_plots_to_db.py 的 protected_names 保持一致。
NON_ENTITY_CHARACTER_NAMES = {"旁白", "Narrator", "系统", "System", "Story", "XiTong", "PangBai"}


def _coerce_height_ai_guess(value: Any) -> bool:
    """height_ai_guess: True 表示身高由 AI 推测，False 表示原文给出或明确长高/变矮。默认 True。"""
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def validate_character_heights(character_name: str, data: Dict[str, Any]) -> None:
    """Validate that every body variant has a non-negative integer cm height."""
    if character_name in NON_ENTITY_CHARACTER_NAMES:
        logger.info(f"角色 {character_name} 为非实体角色，跳过身高校验")
        return

    variants = data.get("whole_body_variants", [])
    if not isinstance(variants, list):
        raise ValueError(f"角色 {character_name} 的 whole_body_variants 不是数组")

    invalid = []
    for index, variant in enumerate(variants, 1):
        if not isinstance(variant, dict):
            invalid.append(f"第 {index} 项不是对象")
            continue
        body_id = variant.get("whole_body_id", index)
        height = str(variant.get("height", "")).strip()
        if not re.fullmatch(r"\d+", height):
            invalid.append(f"whole_body_id={body_id}, height={height!r} -> 已降级为 0")
            height = "0"
        variant["height"] = str(int(height))
        variant["height_ai_guess"] = _coerce_height_ai_guess(variant.get("height_ai_guess", True))

    if invalid:
        logger.warning(
            f"角色 {character_name} 身高数据缺失或格式错误，必须是非负整数厘米字符串"
            f"（0 表示无实体或无需身高）: "
            + "; ".join(invalid)
        )


# 导入拼音转换库
try:
    from pypinyin import pinyin, Style
    HAS_PINYIN = True
except ImportError:
    HAS_PINYIN = False
    logger.warning("未安装pypinyin库，中文名将无法转换为拼音驼峰格式。请安装: pip install pypinyin")


def chinese_to_camel_case(chinese_name: str) -> str:
    """
    将角色名转换为安全的文件名格式

    对于中文名：转换为拼音驼峰格式，例如: "唐三" -> "TangSan", "小舞" -> "XiaoWu"
    对于英文名：移除非法字符，保持原样，例如: "Sherlock Holmes" -> "SherlockHolmes"

    Args:
        chinese_name: 角色名字符串

    Returns:
        安全的文件名格式字符串
    """
    if not chinese_name:
        return "Unknown"

    import re

    # 检测是否包含中文字符
    has_chinese = re.search(r'[\u4e00-\u9fff]', chinese_name)

    # 如果是中文且有拼音库，使用拼音转换
    if has_chinese and HAS_PINYIN:
        try:
            # 获取拼音列表，每个字返回一个列表，取第一个拼音
            pinyin_list = pinyin(chinese_name, style=Style.NORMAL)
            # 每个拼音首字母大写
            camel_parts = [py[0].capitalize() for py in pinyin_list if py]
            return ''.join(camel_parts)
        except Exception as e:
            logger.warning(f"转换中文名 '{chinese_name}' 为拼音失败: {e}")
            # 回退到安全文件名
            safe_name = re.sub(r'[<>:"/\\|?*]', '_', chinese_name)
            return safe_name.capitalize()
    else:
        # 英文或其他语言：移除非法字符，移除空格，首字母大写
        safe_name = re.sub(r'[<>:"/\\|?*\s]', '', chinese_name)
        if safe_name:
            return safe_name[0].upper() + safe_name[1:] if safe_name[0].islower() else safe_name
        else:
            # 如果移除非法字符后为空，使用回退方案
            fallback = re.sub(r'[<>:"/\\|?*]', '_', chinese_name)
            return fallback.capitalize()


class StoryTreeLLMDeduplicationAgent:
    """
    LLM驱动故事树去重代理 (LLM-Powered Story Tree Deduplication Agent)
    完全使用大模型合并和去重故事树角色数据
    """

    # System Prompt loaded from yaml
    SYSTEM_PROMPT = load_prompt_from_yaml("extract_deduplicate_sys.yaml")
    SIMPLIFY_NAMES_SYSTEM_PROMPT = load_prompt_from_yaml(
        "extract_deduplicate_simplify_names_sys.yaml"
    )


    def __init__(
        self,
        characters_json_dir: str = "outputs/characters_json",
        llm_client: Optional[LLMClient] = None,
        provider: str = "qwen",
        model: Optional[str] = None,
        cleanup_raw: bool = True,
    ):
        """
        初始化LLM故事树角色合并器

        Args:
            characters_json_dir: characters_json目录路径
            llm_client: 可选的LLMClient实例，如果为None则创建新的
            provider: LLM提供商 ('openai' 或 'claude' 或 'qwen')
            model: 模型名称
            cleanup_raw: 合并完成后是否清理03_story_tree_raw中的原始数据（默认：True）
        """
        self.characters_json_dir = Path(characters_json_dir)

        # 初始化LLM客户端
        if llm_client is None:
            try:
                self.llm_client = LLMClient(
                    provider=LLMProvider(provider),
                    model=model,
                    temperature=0.3,  # 低温以获得更稳定的输出
                    max_tokens=16000,  # 需要较多tokens来处理复杂合并
                )
                logger.info(f"LLM客户端已初始化（使用{provider}）")
            except Exception as e:
                logger.error(f"LLM客户端初始化失败: {e}")
                raise
        else:
            self.llm_client = llm_client
            logger.info(f"使用提供的LLM客户端: {llm_client.provider}")

        # 设置清理标志
        self.cleanup_raw = cleanup_raw
        if cleanup_raw:
            logger.info("✅ 启用原始数据清理功能：合并完成后将删除03_story_tree_raw中的原始数据")
        else:
            logger.info("ℹ️  原始数据清理功能未启用：03_story_tree_raw中的原始数据将保留")

    def find_latest_raw_timestamp(self) -> str:
        """查找03_story_tree_raw中最新的时间戳目录"""
        raw_base_dir = self.characters_json_dir / "03_story_tree_raw"
        latest_timestamp = None

        if raw_base_dir.exists():
            timestamp_dirs = []
            for item in raw_base_dir.iterdir():
                if item.is_dir() and re.match(r'\d{8}_\d{6}', item.name):
                    timestamp_dirs.append(item.name)

            if timestamp_dirs:
                # 按名称排序（时间戳），取最新的
                timestamp_dirs.sort(reverse=True)
                latest_timestamp = timestamp_dirs[0]
                logger.info(f"使用最新的时间戳目录: {latest_timestamp}")
            else:
                # 如果没有找到时间戳目录，使用当前时间戳
                import datetime
                latest_timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                logger.info(f"03_story_tree_raw中没有时间戳目录，使用当前时间戳: {latest_timestamp}")
        else:
            # 03_story_tree_raw目录不存在，使用当前时间戳
            import datetime
            latest_timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            logger.warning(f"03_story_tree_raw目录不存在，使用当前时间戳: {latest_timestamp}")

        return latest_timestamp

    def load_character_data(self, timestamp: str) -> Dict[str, List[Dict[str, Any]]]:
        """
        加载03_story_tree_raw中指定时间戳目录的角色数据

        Returns:
            字典：角色名 -> 该角色的数据列表（可能多个版本）
        """
        character_data = defaultdict(list)

        # 构建数据目录路径：03_story_tree_raw/时间戳
        data_dir = self.characters_json_dir / "03_story_tree_raw" / timestamp

        if not data_dir.exists():
            logger.warning(f"数据目录不存在: {data_dir}")
            return dict(character_data)

        logger.info(f"处理目录: {data_dir}")

        # 加载该目录中的所有角色JSON文件
        for json_file in data_dir.glob("*.json"):
            if json_file.name == "character_mapping.json":
                continue

            try:
                with open(json_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)

                character_name = data.get("name", "")
                if not character_name:
                    # 尝试从文件名提取角色名
                    character_name = json_file.stem

                character_data[character_name].append(data)
                logger.debug(f"加载角色: {character_name} from {json_file.name}")

            except Exception as e:
                logger.error(f"加载文件失败 {json_file}: {e}")

        logger.info(f"共加载 {len(character_data)} 个角色的数据")
        return dict(character_data)

    def create_llm_prompt(self, character_name: str, data_list: List[Dict[str, Any]]) -> str:
        """Create LLM prompt for merging story tree character data"""

        # Convert data list to readable JSON string
        data_str = json.dumps(data_list, ensure_ascii=False, indent=2)

        template = load_prompt_from_yaml("extract_deduplicate_user.yaml")
        prompt = render_prompt_template(
            template,
            {
                "character_name": character_name,
                "count": len(data_list),
                "data_str": data_str,
            },
            source="extract_deduplicate_user.yaml",
        )
        return prompt

    def merge_character_with_llm(self, character_name: str, data_list: List[Dict[str, Any]]) -> Dict[str, Any]:
        """使用LLM合并同一角色的多个数据文件"""
        logger.info(f"使用LLM合并角色: {character_name} ({len(data_list)} 个版本)")

        try:
            # 创建提示词
            user_prompt = self.create_llm_prompt(character_name, data_list)

            # 调用LLM
            logger.debug(f"调用LLM合并角色: {character_name}")
            response = self.llm_client.chat_completion_json(
                system_prompt=self.SYSTEM_PROMPT,
                user_prompt=user_prompt,
                temperature=0.3,
                max_tokens=16000
            )

            # 验证响应结构
            if not isinstance(response, dict):
                logger.error(f"LLM返回的不是字典: {type(response)}")
                raise ValueError(f"LLM返回无效格式: {type(response)}")

            # 确保包含必要字段
            required_fields = [
                "name",
                "personality_variants",
                "whole_body_variants",
                "clothing_variants",
                "valid_status_combinations",
            ]
            for field in required_fields:
                if field not in response:
                    logger.error(f"LLM响应缺少必要字段: {field}")
                    # 尝试使用第一个数据作为基础
                    response = self.create_fallback_merge(character_name, data_list)
                    break

            # 检查ID连续性，如果发现不连续则记录警告
            self.check_id_continuity(response)

            # refs 由第一步根据绝对行号生成；二次 LLM 合并后再与全部输入版本做并集，
            # 防止模型漏回人物顶层证据。变体/组合中的证据字段同时做类型规范化。
            response = _normalize_evidence_fields(response, data_list)

            logger.info(f"角色 {character_name} 合并成功")
            # logger.debug(f"合并后数据: {json.dumps(response, ensure_ascii=False, indent=2)[:500]}...")  # 注释掉，避免打印详细数据

            return response

        except Exception as e:
            logger.error(f"LLM合并失败 {character_name}: {e}")
            # 回退到简单合并
            return self.create_fallback_merge(character_name, data_list)

    def check_id_continuity(self, data: Dict[str, Any]) -> None:
        """检查变体ID的连续性，只记录警告，不进行修改"""
        # 检查personality_variants ID连续性
        if "personality_variants" in data and isinstance(data["personality_variants"], list):
            p_ids = [v.get("personality_id") for v in data["personality_variants"]]
            if p_ids:
                expected = list(range(1, len(p_ids) + 1))
                if p_ids != expected:
                    logger.warning(f"性格变体ID不连续: {p_ids}，期望: {expected}，但信任大模型的ID分配")

        # 检查whole_body_variants ID连续性
        if "whole_body_variants" in data and isinstance(data["whole_body_variants"], list):
            w_ids = [v.get("whole_body_id") for v in data["whole_body_variants"]]
            if w_ids:
                expected = list(range(1, len(w_ids) + 1))
                if w_ids != expected:
                    logger.warning(f"身体变体ID不连续: {w_ids}，期望: {expected}，但信任大模型的ID分配")

        # 检查clothing_variants ID连续性
        if "clothing_variants" in data and isinstance(data["clothing_variants"], list):
            c_ids = [v.get("clothing_id") for v in data["clothing_variants"]]
            if c_ids:
                expected = list(range(1, len(c_ids) + 1))
                if c_ids != expected:
                    logger.warning(f"服装变体ID不连续: {c_ids}，期望: {expected}，但信任大模型的ID分配")

    def create_fallback_merge(self, character_name: str, data_list: List[Dict[str, Any]]) -> Dict[str, Any]:
        """创建回退合并方案（当LLM失败时使用）"""
        logger.warning(f"为角色 {character_name} 使用回退合并方案")

        # 简单的合并策略：取第一个数据作为基础，补充其他数据中的独特信息
        base_data = data_list[0].copy()

        # 收集所有独特信息
        all_other_names = set()
        all_personality_variants = []
        all_whole_body_variants = []
        all_clothing_variants = []
        all_valid_combinations = {}  # combination string -> dict, for dedup
        all_infos = []
        all_refs: List[str] = []

        for data in data_list:
            all_other_names.update(data.get("other_names", []))
            all_infos.append(data.get("info"))
            all_refs.extend(_merge_refs(data.get("refs", [])))
            all_personality_variants.extend(data.get("personality_variants", []))
            all_whole_body_variants.extend(data.get("whole_body_variants", []))
            all_clothing_variants.extend(data.get("clothing_variants", []))
            for entry in data.get("valid_status_combinations", []):
                if isinstance(entry, dict):
                    key = entry.get("combination", "")
                    if key and key not in all_valid_combinations:
                        all_valid_combinations[key] = entry.copy()
                    elif key:
                        existing = all_valid_combinations[key]
                        existing["info"] = _merge_info_values(
                            existing.get("info"), entry.get("info")
                        )
                        existing["refs"] = _merge_refs(
                            existing.get("refs", []), entry.get("refs", [])
                        )
                elif isinstance(entry, str) and entry not in all_valid_combinations:
                    all_valid_combinations[entry] = {"combination": entry, "description": "", "is_active": False}

        # 处理变体去重
        personality_variants = _deduplicate_variants_for_fallback(all_personality_variants, "personality_id")
        whole_body_variants = _deduplicate_variants_for_fallback(all_whole_body_variants, "whole_body_id")
        clothing_variants = _deduplicate_variants_for_fallback(all_clothing_variants, "clothing_id")

        merged_data = {
            "name": character_name,
            "info": _merge_info_values(all_infos),
            "refs": _merge_refs(all_refs),
            "other_names": list(all_other_names),
            "personality_variants": personality_variants,
            "whole_body_variants": whole_body_variants,
            "clothing_variants": clothing_variants,
            "valid_status_combinations": list(all_valid_combinations.values()),
        }

        return _normalize_evidence_fields(merged_data, data_list)

    @staticmethod
    def _collect_variant_infos(merged_data: Dict[str, Any]) -> str:
        """把人物 info 与各已存在变体/组合的 info 汇总，作为兜底还原的线索来源。

        这类角色没有独立外貌/服装段落，但视觉与身份线索通常散落在这些 info 里
        （例如「政府军队长」「坐轮椅、面容沧桑、头发白了大半」），足够 LLM 推断出
        一套合理保守的基础变体。
        """
        infos: List[str] = [str(merged_data.get("info") or "")]
        for field in (
            "personality_variants",
            "whole_body_variants",
            "clothing_variants",
            "valid_status_combinations",
        ):
            for item in merged_data.get(field) or []:
                if isinstance(item, dict) and item.get("info"):
                    infos.append(str(item.get("info")))
        return _merge_info_values(infos)

    def _synthesize_missing_variants(
        self, character_name: str, merged_data: Dict[str, Any], missing: List[str]
    ) -> Optional[Dict[str, Any]]:
        """用一次 LLM 调用，按已知线索还原出缺失那几类的一套基础变体。

        只请求 missing 中列出的类别，已有的类别原样保留。任何异常返回 None，
        由调用方走规则兜底，绝不中断合并流程。
        """
        clues = self._collect_variant_infos(merged_data)
        gender_hint = ""
        for body in merged_data.get("whole_body_variants") or []:
            if isinstance(body, dict) and body.get("gender"):
                gender_hint = str(body.get("gender"))
                break

        label = {
            "personality_variants": "性格变体 personality_variants",
            "whole_body_variants": "身体变体 whole_body_variants",
            "clothing_variants": "服装变体 clothing_variants",
        }
        wanted = "、".join(label[m] for m in missing)

        system = (
            "你是视觉小说人物立绘设定专家。给定一个缺少独立外貌/服装描写的角色，"
            "你要依据其姓名、身份、职业、剧情作用等已知线索，为【指定缺失的变体类别】"
            "各还原出恰好一套【合理且保守】的基础设定，使其能被文生图。严格规则：\n"
            "1. 只输出被要求补全的类别，每类恰好一个变体，id 从 1 开始。\n"
            "2. 只依据线索合理推断，可补出与身份相符的常识性外观（如队长穿制服、中年人"
            "的大致身形），但不得编造线索里没有、也无法从身份合理推断的独特特征"
            "（具体伤疤、纹身、特殊发色等），未知字段一律留空字符串。\n"
            "3. gender 依据线索填 male/female/other，无法判断留空；race 默认 human；"
            "human_animal_form 默认 pure_human。\n"
            "4. clothing_style / outfit_discription 依据职业或身份推断（如「政府军队长」→"
            "军用制服），不要用「普通衣服」这类无效描述，无法推断则留空。\n"
            "5. traits 给 2-4 个稳定性格词，不要情绪词。\n"
            "6. 只返回 JSON，不要解释。"
        )
        user = (
            f"角色名：{character_name}\n"
            f"已知线索（人物及各片段 info 汇总）：{clues or '（无）'}\n"
            f"已知性别线索：{gender_hint or '（未知）'}\n"
            f"需要补全的类别：{wanted}\n\n"
            "返回 JSON，仅包含被要求的键，结构如下（只放需要补全的那几类）：\n"
            "{\n"
            '  "personality_variants": [{"personality_id":1,"traits":[...],"info":"一句话"}],\n'
            '  "whole_body_variants": [{"whole_body_id":1,"age":"","apparent_age":"",'
            '"age_category":"","height":"","height_ai_guess":true,"gender":"","race":"human",'
            '"human_animal_form":"pure_human","info":"一句话","appearance":{"head":{"hair_color":"",'
            '"hair_style":"","eye_color":"","skin_color":"","facial_description":""},'
            '"body":{"body_type":"","body_description":""}}}],\n'
            '  "clothing_variants": [{"clothing_id":1,"occupation":"","clothing_style":"",'
            '"outfit_discription":"","accessories":[],"tmp_accessories":[],"weapons":[],"armor":[]}]\n'
            "}"
        )
        try:
            response = self.llm_client.chat_completion_json(
                system_prompt=system,
                user_prompt=user,
                temperature=0.3,
                max_tokens=2000,
            )
        except Exception as exc:
            logger.warning(f"缺失变体兜底 LLM 调用失败 {character_name}: {exc}")
            return None
        if not isinstance(response, dict):
            logger.warning(
                f"缺失变体兜底 LLM 返回非字典 {character_name}: {type(response)}"
            )
            return None
        return response

    @staticmethod
    def _default_variant(field: str) -> Dict[str, Any]:
        """LLM 兜底也失败时的最小规则变体，保证该类不为空、结构完整。"""
        if field == "personality_variants":
            return {"personality_id": 1, "traits": [], "info": "", "refs": []}
        if field == "whole_body_variants":
            return {
                "whole_body_id": 1,
                "age": "",
                "apparent_age": "",
                "age_category": "",
                "height": "",
                "height_ai_guess": True,
                "gender": "",
                "race": "human",
                "human_animal_form": "pure_human",
                "info": "",
                "refs": [],
                "appearance": {
                    "head": {
                        "hair_color": "",
                        "hair_style": "",
                        "eye_color": "",
                        "skin_color": "",
                        "facial_description": "",
                    },
                    "body": {"body_type": "", "body_description": ""},
                },
            }
        # clothing_variants
        return {
            "clothing_id": 1,
            "occupation": "",
            "clothing_style": "",
            "outfit_discription": "",
            "accessories": [],
            "tmp_accessories": [],
            "weapons": [],
            "armor": [],
            "info": "",
            "refs": [],
        }

    def _ensure_minimal_variants(
        self, character_name: str, merged_data: Dict[str, Any]
    ) -> None:
        """三类立绘变体中任意一类为空时，原地补出该类的基础变体（每类至少 1 个）。

        缺哪类补哪类：先尝试 LLM 依线索还原，失败或仍缺则用规则默认变体兜底。
        补出的变体 refs 继承人物顶层 refs，并复用 _normalize_evidence_fields 做
        id 连续化与字段规范，保证与正常变体结构一致。
        """
        id_field = {
            "personality_variants": "personality_id",
            "whole_body_variants": "whole_body_id",
            "clothing_variants": "clothing_id",
        }
        missing = [
            field
            for field in id_field
            if not (isinstance(merged_data.get(field), list) and merged_data.get(field))
        ]
        if not missing:
            return

        logger.warning(
            f"角色 {character_name} 缺少变体类别 {missing}，触发基础变体还原"
        )
        char_refs = _merge_refs(merged_data.get("refs", []))

        llm_result = self._synthesize_missing_variants(character_name, merged_data, missing)
        for field in missing:
            variants = None
            if isinstance(llm_result, dict):
                candidate = llm_result.get(field)
                if isinstance(candidate, list) and candidate:
                    variants = [v for v in candidate if isinstance(v, dict)]
            if not variants:
                logger.warning(
                    f"角色 {character_name} 的 {field} LLM 未补出，使用规则默认变体"
                )
                variants = [self._default_variant(field)]

            # 只保留第一个，缺失类别每类保底 1 个即可；补齐 id、info、refs。
            variant = variants[0]
            variant[id_field[field]] = 1
            variant.setdefault("info", "")
            variant["info"] = str(variant.get("info") or "").strip()
            variant["refs"] = _merge_refs(variant.get("refs", []) or char_refs)
            merged_data[field] = [variant]

        if merged_data.get("personality_variants"):
            merged_data["personality_variants"] = [
                _normalize_personality_variant(item, index)
                for index, item in enumerate(merged_data["personality_variants"], 1)
                if isinstance(item, dict)
            ]
        # 复用既有规范化：id 连续、证据字段类型统一。
        _normalize_evidence_fields(merged_data)

    def merge_all_characters(self, character_data: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
        """合并所有角色数据"""
        merged_characters = {}

        for character_name, data_list in character_data.items():
            if not data_list:
                continue

            logger.info(f"正在合并角色: {character_name} ({len(data_list)} 个版本)")

            # 使用LLM合并
            merged_data = self.merge_character_with_llm(character_name, data_list)
            # 合并后若三类立绘变体（性格/身体/服装）中任意一类为空，该角色就无法
            # 生成完整提示词：char_prompt_builder 要求 body 和 clothing 变体同时非空，
            # 缺任意一类都会被 skip，进而 char_designer 预检因 character_prompts 缺人失败。
            # 这里在变体定型后、身高校验前，按 name+info 等已知线索用 LLM 还原出缺失的
            # 那几类基础变体（缺哪类补哪类，每类至少 1 个），让角色数据自洽。
            self._ensure_minimal_variants(character_name, merged_data)
            validate_character_heights(character_name, merged_data)
            # node_appearances 已退出人物数据契约；兼容旧输入或模型回显时也不再入库。
            merged_data.pop("node_appearances", None)
            merged_characters[character_name] = merged_data

            # 记录统计信息
            original_personality = sum(len(data.get("personality_variants", [])) for data in data_list)
            original_whole_body = sum(len(data.get("whole_body_variants", [])) for data in data_list)
            original_clothing = sum(len(data.get("clothing_variants", [])) for data in data_list)

            merged_personality = len(merged_data.get("personality_variants", []))
            merged_whole_body = len(merged_data.get("whole_body_variants", []))
            merged_clothing = len(merged_data.get("clothing_variants", []))

            logger.info(f"  性格变体: {original_personality} -> {merged_personality}")
            logger.info(f"  身体变体: {original_whole_body} -> {merged_whole_body}")
            logger.info(f"  服装变体: {original_clothing} -> {merged_clothing}")

        return merged_characters

    def merge_in_memory(self, character_data: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
        """
        在内存中合并角色数据，不涉及文件系统。

        Args:
            character_data: 角色数据字典，格式为 {角色名: [数据字典]}

        Returns:
            合并后的角色数据字典 {角色名: 合并后的数据}
        """
        logger.info("开始在内存中合并角色数据")
        return self.merge_all_characters(character_data)

    def simplify_names(
        self, merged: Dict[str, Dict[str, Any]]
    ) -> Dict[str, Dict[str, Any]]:
        """把合并后的描述型角色名精简为职业/身份名（入库前调用）。

        规则见 extract_deduplicate_simplify_names_sys.yaml：真名/关系型名称保留原样，
        动作描述型简化为身份名，同类多个用 A/B/C 编号，旧名存入 other_names 留档，
        并同步重写 valid_status_combinations 的名字前缀。

        任何异常或硬校验失败时原样返回 merged，保证流水线不中断。
        """
        if not merged:
            return merged

        original_keys = list(merged.keys())
        try:
            response = self.llm_client.chat_completion_json(
                system_prompt=self.SIMPLIFY_NAMES_SYSTEM_PROMPT,
                user_prompt=_build_simplify_user_prompt(merged),
                temperature=0.2,
                max_tokens=4000,
            )
            rename_map = _validate_simplify_rename_map(
                response.get("rename_map"), original_keys
            )
            if rename_map is None:
                return merged

            result = _apply_simplify_rename(merged, rename_map)
            changed = [f"{old} -> {new}" for old, new in rename_map.items() if old != new]
            logger.info(f"人名精简完成，改名 {len(changed)} 个：{changed}")
            return result
        except Exception as e:
            logger.error(f"人名精简失败，回退为不改名: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return merged

    def save_merged_characters(self, merged_characters: Dict[str, Dict[str, Any]], timestamp: str) -> str:
        """保存合并后的角色数据到04_story_tree_merged目录

        将所有角色数据保存到单个JSON文件（characters.json），
        并创建角色映射文件（character_mapping.json）引用该文件。
        """
        # 设置输出目录：04_story_tree_merged/时间戳
        merged_dir = self.characters_json_dir / "04_story_tree_merged" / timestamp
        merged_dir.mkdir(parents=True, exist_ok=True)

        # 保存所有角色数据到一个JSON文件（文件名：characters.json）
        characters_file = merged_dir / "characters.json"
        try:
            with open(characters_file, 'w', encoding='utf-8') as f:
                json.dump(merged_characters, f, ensure_ascii=False, indent=2)
            logger.info(f"保存所有角色数据到单个文件: {characters_file}")
        except Exception as e:
            logger.error(f"保存所有角色数据文件失败 {characters_file}: {e}")

        # 创建character_mapping.json（更新格式以引用单个文件）
        mapping_file = merged_dir / "character_mapping.json"
        mapping_data = {}

        for character_name, data in merged_characters.items():
            personality_ids = [p.get("personality_id") for p in data.get("personality_variants", [])]
            whole_body_ids = [w.get("whole_body_id") for w in data.get("whole_body_variants", [])]
            clothing_ids = [c.get("clothing_id") for c in data.get("clothing_variants", [])]

            mapping_data[character_name] = {
                "name": character_name,
                "personality_variants": personality_ids,
                "whole_body_variants": whole_body_ids,
                "clothing_variants": clothing_ids,
                "valid_status_combinations": data.get("valid_status_combinations", []),
                "filepath": str(characters_file),  # 指向单个文件
                "character_key": character_name  # 在单个文件中的键名
            }

        try:
            with open(mapping_file, 'w', encoding='utf-8') as f:
                json.dump(mapping_data, f, ensure_ascii=False, indent=2)
            logger.info(f"保存角色映射: {mapping_file}")
        except Exception as e:
            logger.error(f"保存映射文件失败 {mapping_file}: {e}")

        return str(merged_dir)

    def cleanup_raw_data(self, timestamp: str) -> bool:
        """
        清理03_story_tree_raw中的原始数据目录

        Args:
            timestamp: 要清理的时间戳目录名

        Returns:
            成功返回True，失败返回False
        """
        import shutil
        import os

        raw_dir = self.characters_json_dir / "03_story_tree_raw" / timestamp

        if not raw_dir.exists():
            logger.warning(f"原始数据目录不存在，无需清理: {raw_dir}")
            return False

        try:
            # 检查目录是否为空（安全确认）
            if any(raw_dir.iterdir()):
                logger.info(f"清理原始数据目录: {raw_dir} (包含 {len(list(raw_dir.iterdir()))} 个文件/子目录)")
            else:
                logger.info(f"清理空目录: {raw_dir}")

            # 删除整个时间戳目录
            shutil.rmtree(raw_dir)
            logger.info(f"✅ 原始数据清理完成: {timestamp}")
            return True

        except Exception as e:
            logger.error(f"清理原始数据失败 {raw_dir}: {e}")
            return False

    def run(self, timestamp: Optional[str] = None, cleanup_raw: Optional[bool] = None) -> str:
        """运行LLM故事树角色合并器

        Args:
            timestamp: 指定时间戳目录（默认使用最新的）
            cleanup_raw: 是否清理原始数据（默认使用实例设置）
        """
        logger.info("=" * 50)
        logger.info("🧠 开始运行LLM故事树角色合并器")
        logger.info("=" * 50)

        # 确定是否清理原始数据
        use_cleanup = cleanup_raw if cleanup_raw is not None else self.cleanup_raw
        if use_cleanup:
            logger.info("🧹 启用原始数据清理：合并完成后将删除03_story_tree_raw中的原始数据")
        else:
            logger.info("📁 原始数据清理未启用：03_story_tree_raw中的原始数据将保留")

        # 1. 查找最新时间戳（如果未指定）
        if timestamp is None:
            timestamp = self.find_latest_raw_timestamp()

        logger.info(f"使用时间戳目录: {timestamp}")

        # 2. 加载所有角色数据
        logger.info("📂 步骤1: 加载所有角色数据...")
        character_data = self.load_character_data(timestamp)

        if not character_data:
            logger.warning("❌ 未找到角色数据，退出")
            return ""

        # 3. 使用LLM合并角色数据
        logger.info("🤖 步骤2: 使用LLM合并角色数据...")
        merged_characters = self.merge_all_characters(character_data)

        if not merged_characters:
            logger.warning("❌ 合并失败，退出")
            return ""

        # 4. 保存合并后的数据
        logger.info("💾 步骤3: 保存合并数据...")
        import datetime
        output_timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        logger.info(f"生成输出时间戳: {output_timestamp}")
        output_dir = self.save_merged_characters(merged_characters, output_timestamp)

        # 5. 清理原始数据（如果启用）
        if use_cleanup:
            logger.info("🧹 步骤4: 清理原始数据...")
            cleanup_success = self.cleanup_raw_data(timestamp)
            if cleanup_success:
                logger.info("✅ 原始数据清理完成")
            else:
                logger.warning("⚠️  原始数据清理失败或无需清理")

        logger.info("=" * 50)
        logger.info("✅ LLM故事树角色合并器运行完成")
        logger.info(f"   合并角色数量: {len(merged_characters)}")
        logger.info(f"   输出目录: {output_dir}")
        logger.info("=" * 50)

        return output_dir


def main():
    """命令行入口"""
    import os
    import sys
    import argparse

    # --- 环境配置 (参考 run_project.py) ---
    try:
        from dotenv import load_dotenv
        from pathlib import Path
        # 加载主 .env 文件（novel-game-factory/.env）
        dotenv_path = Path(__file__).resolve().parent.parent.parent.parent / ".env"
        load_dotenv(dotenv_path=dotenv_path)
        print(f"已加载环境变量文件: {dotenv_path}")
    except ImportError:
        print("⚠️  警告: 未安装 python-dotenv，无法加载 .env 文件")

    # 清理代理，防止 API 连接问题
    os.environ.pop("http_proxy", None)
    os.environ.pop("https_proxy", None)
    os.environ.pop("all_proxy", None)

    parser = argparse.ArgumentParser(description="LLM故事树角色合并器 - 完全使用大模型合并故事树角色数据")
    parser.add_argument(
        "--input-dir",
        default="outputs/characters_json",
        help="characters_json目录路径 (默认: outputs/characters_json)"
    )
    parser.add_argument(
        "--timestamp",
        help="指定时间戳目录（默认使用最新的）"
    )
    parser.add_argument(
        "--provider",
        choices=["openai", "claude", "qwen"],
        default="qwen",
        help="LLM提供商 (默认: qwen)"
    )
    parser.add_argument(
        "--model",
        help="模型名称（默认根据提供商选择）"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="显示详细日志"
    )
    parser.add_argument(
        "--no-cleanup",
        action="store_false",
        dest="cleanup_raw",
        default=True,
        help="禁用原始数据清理（默认启用清理）"
    )

    args = parser.parse_args()

    # 设置日志级别
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    # 运行LLM故事树角色合并器
    try:
        merger = StoryTreeLLMDeduplicationAgent(
            characters_json_dir=args.input_dir,
            provider=args.provider,
            model=args.model,
            cleanup_raw=args.cleanup_raw
        )
        output_dir = merger.run(args.timestamp, cleanup_raw=args.cleanup_raw)

        if output_dir:
            print(f"\n✅ LLM故事树角色合并完成!")
            print(f"   输出目录: {output_dir}")
            print(f"   建议在生成故事线之前使用此目录的角色数据")
        else:
            print("\n❌ LLM故事树角色合并失败")

    except Exception as e:
        print(f"❌ 运行失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
