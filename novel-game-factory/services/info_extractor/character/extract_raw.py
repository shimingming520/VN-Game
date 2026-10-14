"""
故事树人物特征提取器02 (Story Tree Character Features Extractor 02)
专门用于从故事树（Story Tree）中提取人物信息，按照故事树节点顺序（深度优先）提取
生成类似01_world_bible_raw格式的数据

核心改进：
1. 严格按照世界树树状结构，一个一个文件进行人物分析和提取
2. 一个文件提取完所有的人物信息内容，才进入下一个文件
3. 通读全文进行提取，而不是仅提取### DirectorBlock JSON部分
4. 最终生成多个人物json文件
5. 不要走回头路，读取完一份markdown就下一份文件进行提取

新增功能：支持从数据库获取数据

================================================================================
使用说明 (Usage)
================================================================================

【方式一】通过 main.py 管道运行（推荐）
  python main.py --task-id your_task_id
  python main.py --task-id your_task_id --no-generate-images  # 不生成图片

【方式二】直接运行 extract_raw.py

  # 从数据库提取（推荐）
  python core/extract_raw.py --task-id your_task_id

  # 从本地文件提取
  python core/extract_raw.py path/to/story_tree.json

  # 指定输出目录
  python core/extract_raw.py --task-id your_task_id --output-dir outputs/my_chars

  # 使用不同的 LLM 提供商
  python core/extract_raw.py --task-id your_task_id --provider claude

================================================================================
命令行参数说明
================================================================================
  story_tree_file     故事树文件路径（story_tree.json 或 extracted_story_tree.json）
                      如果未提供，自动查找最新的文件
  --output-dir        输出目录 (默认: outputs/characters_json/03_story_tree_raw)
  --project-name      项目名称
  --provider          LLM提供商 (openai/claude/qwen，默认: qwen)
  --model             模型名称（默认根据提供商选择）
  --task-id           任务ID（如果提供则从数据库获取数据，忽略 story_tree_file）

================================================================================
输出说明
================================================================================
  输出目录结构:
    outputs/characters_json/03_story_tree_raw/YYYYMMDD_HHMMSS/
      TangSan.json          # 角色1
      XiaoWu.json           # 角色2
      ...
      character_mapping.json # 角色映射文件

================================================================================
"""
import os
import sys
import json
import logging
import time
import re
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field
from sqlalchemy import create_engine, text
import yaml

# 解决直接运行时的相对导入问题
import sys
from pathlib import Path

# 将项目根目录添加到 sys.path
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent.parent.parent  # char_designer/core -> char_designer -> services -> novel-game-factory
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))
# 确保当前目录在 sys.path 中，以便导入同级模块
if str(current_dir) not in sys.path:
    sys.path.insert(0, str(current_dir))

# 尝试多种导入方式
# 从 utils 导入 llm_client（相对导入）
from ..utils.llm_client import LLMClient, LLMProvider
from .template_renderer import render_prompt_template

try:
    from info_extractor.utils.story_tree_json_parse import load_story_tree_for_player
except ImportError:
    from ..utils.story_tree_json_parse import load_story_tree_for_player  # type: ignore

# 配置日志
import logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 合法的人形-兽化等级取值（英语名），见《人物人形与兽化程度分类规范》。
# 非法或缺失时回退空字符串，由生图端按人类保守处理。
_VALID_HUMAN_ANIMAL_FORMS = {
    "pure_human",
    "humanoid",
    "humanoid_local_animal_feature",
    "human_face_animal_body",
    "animal_form_humanoid_activity",
    "animal_head_humanoid_body",
    "pure_animal_form",
    "other",
}

# 年龄段区间（含下界，末段无上界），须与提示词及 char_prompt_builder 的 age_map 保持一致。
# 唯一强关系 apparent_age → age_category 由此表确定性派生。
_AGE_CATEGORY_BANDS = (
    (0, 5, "infant"),
    (6, 11, "child"),
    (12, 18, "adolescent"),
    (19, 35, "young adult"),
    (36, 59, "middle-aged"),
)
_AGE_CATEGORY_ELDERLY = "elderly"  # 60 岁及以上


def _age_number_to_category(age_value: Any) -> str:
    """把纯数字年龄映射到年龄段；非数字返回空串。仅用于 apparent_age 派生。"""
    s = str(age_value or "").strip()
    if not s.isdigit():
        return ""
    n = int(s)
    for low, high, category in _AGE_CATEGORY_BANDS:
        if low <= n <= high:
            return category
    return _AGE_CATEGORY_ELDERLY


_PERSONALITY_LEGACY_FIELDS = ("main_traits", "secondary_traits", "flaws", "virtues")
_TRANSIENT_PERSONALITY_STATES = {
    "愤怒", "生气", "悲伤", "难过", "惊讶", "震惊", "恐惧", "害怕",
    "厌恶", "恶心", "开心", "高兴", "紧张",
    "angry", "anger", "sad", "sadness", "surprised", "surprise",
    "shocked", "fearful", "fear", "afraid", "disgusted", "disgust",
    "happy", "happiness", "nervous", "nervousness", "shock",
}


def _normalize_personality_traits(value: Any, max_traits: int = 8) -> List[str]:
    """规范化单一 traits 字段；不兼容旧四字段，旧字段直接丢弃。"""
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

# 导入数据库配置
from common.config import DATABASE_URL, PLATFORM_DATABASE_URL

# 导入拼音转换库
try:
    from pypinyin import pinyin, Style
    HAS_PINYIN = True
except ImportError:
    HAS_PINYIN = False
    logger.warning("未安装pypinyin库，中文名将无法转换为拼音驼峰格式。请安装: pip install pypinyin")


def sanitize_filename(name: str) -> str:
    """
    清理文件名，移除不需要的字符
    1. 移除所有句点（.）
    2. 移除 's（所有格）

    Args:
        name: 原始名称

    Returns:
        str: 清理后的名称
    """
    # 移除所有句点
    name = name.replace('.', '')
    # 移除 's（所有格，处理不同的撇号字符）
    name = name.replace("'s", '')
    name = name.replace("'s", '')
    name = name.replace("'S", '')
    name = name.replace("'S", '')
    return name


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
            result = ''.join(camel_parts)
            # 清理文件名
            return sanitize_filename(result)
        except Exception as e:
            logger.warning(f"转换中文名 '{chinese_name}' 为拼音失败: {e}")
            # 回退到安全文件名
            safe_name = re.sub(r'[<>:"/\\|?*]', '_', chinese_name)
            return sanitize_filename(safe_name.capitalize())
    else:
        # 英文或其他语言：移除非法字符，移除空格，首字母大写
        safe_name = re.sub(r'[<>:"/\\|?*\s]', '', chinese_name)
        if safe_name:
            result = safe_name[0].upper() + safe_name[1:] if safe_name[0].islower() else safe_name
            # 清理文件名
            return sanitize_filename(result)
        else:
            # 如果移除非法字符后为空，使用回退方案
            fallback = re.sub(r'[<>:"/\\|?*]', '_', chinese_name)
            return sanitize_filename(fallback.capitalize())


# ================= 数据库配置 =================
# 数据库连接（从 common.config 导入）
DB_URL = PLATFORM_DATABASE_URL  # 跨模块共用表已迁平台库(agent_platform)


def fetch_source_data_from_db(target_task_id=None):
    """从数据库获取 story_tree_json 和 story_tree_md"""
    engine = create_engine(DB_URL)
    with engine.connect() as conn:
        # 1. 确定 Task ID
        task_id = target_task_id
        if not task_id:
            logger.info("🔍 未指定 task_id，查找最新任务...")
            sql = text("SELECT task_id FROM story_tree_json ORDER BY id DESC LIMIT 1")
            res = conn.execute(sql).fetchone()
            if not res: return None, None, None
            task_id = res[0]

        logger.info(f"🚀 锁定任务 Task ID: {task_id}")

        # 2. 获取树结构
        sql_tree = text("SELECT data FROM story_tree_json WHERE task_id = :tid")
        row_tree = conn.execute(sql_tree, {"tid": task_id}).fetchone()
        if not row_tree: return None, None, None
        tree_data = load_story_tree_for_player(
            row_tree[0], task_id=task_id, db_engine=engine, logger=logger
        )
        if not isinstance(tree_data.get("nodes"), dict):
            logger.error(
                f"❌ task_id={task_id} 的 story_tree_json 没有可用的语言版本"
            )
            return None, None, None

        # 3. 获取 MD 内容
        sql_md = text("SELECT name, data FROM story_tree_md WHERE task_id = :tid")
        rows_md = conn.execute(sql_md, {"tid": task_id}).fetchall()

        # 转字典: { "node_id": "content..." }
        md_contents = {}
        for row in rows_md:
            # 去除可能的 .md 后缀以匹配 node_id
            key = row[0].replace(".md", "")
            md_contents[key] = row[1]

        logger.info(f"📦 从数据库加载源数据: 树结构 + {len(md_contents)} 个文档节点")
        return task_id, tree_data, md_contents


@dataclass
class StoryTreeCharacterFeaturesData:
    """故事树人物特征数据结构"""
    name: str
    info: str = ""
    refs: List[str] = field(default_factory=list)
    other_names: List[str] = field(default_factory=list)
    personality_variants: List[Dict[str, Any]] = field(default_factory=list)
    whole_body_variants: List[Dict[str, Any]] = field(default_factory=list)
    clothing_variants: List[Dict[str, Any]] = field(default_factory=list)
    valid_status_combinations: List[Dict[str, Any]] = field(default_factory=list)
    important_rank: int = 1  # 重要性等级，数字越大重要性越高

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

class StoryTreeCharacterExtractor02:
    """
    故事树人物特征提取器 Agent (02版本)

    职责：
    1. 加载故事树结构（从story_tree.json或extracted_story_tree.json）
    2. 深度优先遍历故事树节点
    3. 从每个节点的markdown文件中通读全文提取人物信息
    4. 按照时间顺序（节点遍历顺序）整理人物特征
    5. 生成符合01_world_bible_raw格式的人物JSON文件

    核心原则：
    - 严格按照世界树树状结构，一个一个文件进行人物分析和提取
    - 一个文件提取完所有的人物信息内容，才进入下一个文件
    - 通读全文进行提取，而不是仅提取### DirectorBlock JSON部分
    - 最终生成多个人物json文件
    - 不要走回头路，读取完一份markdown就下一份文件进行提取
    """


    def __init__(
        self,
        llm_client: Optional[LLMClient] = None,
        provider: str = "qwen",
        model: Optional[str] = None,
    ):
        """
        初始化故事树人物特征提取器。

        Args:
            llm_client: 可选的 LLMClient 实例，如果为 None 则创建新的
            provider: LLM 提供商 ('openai' 或 'claude' 或 'qwen')
            model: 模型名称
        """
        self.llm_client = llm_client or LLMClient(
            provider=LLMProvider(provider),
            model=model,
            temperature=0.3,  # 低温以获得更稳定的输出
            max_tokens=16000,  # 增加token限制以处理详细角色数据
        )

        # 加载提示词
        self.SYSTEM_PROMPT = load_prompt_from_yaml("extract_raw_extract_sys_en.yaml")
        self.SEGMENT_USER_PROMPT = load_prompt_from_yaml("extract_raw_segment_user_en.yaml")
        self.DEDUPLICATE_SYSTEM_PROMPT = load_prompt_from_yaml("extract_raw_deduplicate_sys_en.yaml")
        self.DEDUPLICATE_USER_PROMPT = load_prompt_from_yaml("extract_raw_deduplicate_user_en.yaml")
        self.MERGE_USER_PROMPT = load_prompt_from_yaml("extract_raw_merge_user_en.yaml")

    def load_story_tree(self, story_tree_path: str) -> Dict[str, Any]:
        """加载故事树文件"""
        try:
            with open(story_tree_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            logger.info(f"加载故事树文件: {story_tree_path}")
            selected = load_story_tree_for_player(data, logger=logger)
            logger.info(f"节点数量: {len(selected.get('nodes', {}))}")

            return selected
        except Exception as e:
            logger.error(f"加载故事树文件失败: {e}")
            raise

    def depth_first_traversal(self, tree_data: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
        """
        深度优先遍历故事树，返回按遍历顺序排列的节点列表

        Args:
            tree_data: 故事树数据，包含root_id和nodes

        Returns:
            列表：(node_id, node_data)
        """
        nodes = tree_data.get("nodes", {})
        root_id = tree_data.get("root_id")

        if not root_id or root_id not in nodes:
            logger.warning("没有有效的根节点，按字典顺序处理所有节点")
            return [(node_id, nodes[node_id]) for node_id in sorted(nodes.keys())]

        traversal_order = []
        visited = set()

        def dfs(current_id: str):
            if current_id in visited or current_id not in nodes:
                return

            visited.add(current_id)
            traversal_order.append((current_id, nodes[current_id]))

            # 获取子节点
            children = []
            for child_choice in nodes[current_id].get("children", []):
                child_id = child_choice.get("child_id")
                if child_id and child_id in nodes:
                    children.append(child_id)

            # 递归处理子节点
            for child_id in children:
                dfs(child_id)

        dfs(root_id)

        # 处理可能未连接到的节点
        for node_id in nodes:
            if node_id not in visited:
                traversal_order.append((node_id, nodes[node_id]))

        logger.info(f"深度优先遍历完成，节点数量: {len(traversal_order)}")
        return traversal_order

    @staticmethod
    def _number_text_lines(text: str, start_line: int) -> str:
        """为正文添加绝对行号，格式与 Scene 的 refs 坐标系一致。"""
        return "\n".join(
            f"{start_line + offset}│{line}"
            for offset, line in enumerate(text.split("\n"))
        )

    @staticmethod
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

    @classmethod
    def _merge_info_values(cls, *values: Any) -> str:
        """合并来自多个文本块/节点的情景摘要，不丢失独立描述。"""
        flattened: List[Any] = []
        for value in values:
            if isinstance(value, list):
                flattened.extend(value)
            else:
                flattened.append(value)
        return "\n".join(cls._ordered_unique_strings(flattened))

    @staticmethod
    def _character_ref(
        record: Dict[str, Any],
        node_id: str,
        chunk_start_line: int,
        chunk_end_line: int,
        fallback_ref: Optional[str] = None,
    ) -> str:
        """将 LLM 返回的起止行钳制到当前 chunk，并生成标准 refs。

        与 Scene 一致，LLM 只负责返回绝对 start_line/end_line；最终 ref 由代码生成。
        缺失、非法或越界时，人物顶层回退到整块，子变体回退到人物顶层 ref。
        """
        try:
            start_line = int(record.get("start_line"))
            end_line = int(record.get("end_line"))
        except (TypeError, ValueError):
            return fallback_ref or f"{node_id}_{chunk_start_line}_{chunk_end_line}"

        if start_line > end_line:
            start_line, end_line = end_line, start_line
        start_line = max(chunk_start_line, min(start_line, chunk_end_line))
        end_line = max(chunk_start_line, min(end_line, chunk_end_line))
        return f"{node_id}_{start_line}_{end_line}"

    @classmethod
    def _normalize_record_evidence(
        cls,
        record: Dict[str, Any],
        node_id: str,
        chunk_start_line: int,
        chunk_end_line: int,
        fallback_ref: Optional[str] = None,
    ) -> str:
        """规范单条人物/变体/组合的 info 与 refs，并移除临时行号字段。"""
        record["info"] = str(record.get("info") or "").strip()
        ref = cls._character_ref(
            record,
            node_id,
            chunk_start_line,
            chunk_end_line,
            fallback_ref=fallback_ref,
        )
        record["refs"] = [ref]
        record.pop("start_line", None)
        record.pop("end_line", None)
        return ref

    @classmethod
    def _split_text_into_chunks(
        cls,
        text: str,
        chunk_size: int = 4000,
        overlap: int = 500,
        base_line: int = 1,
    ) -> List[Dict[str, Any]]:
        """
        将文本按段落边界切分为带重叠的块。

        Args:
            text: 原始文本
            chunk_size: 每块目标字符数
            overlap: 相邻块的重叠字符数

        Returns:
            文本块列表；每块包含 text、numbered_text、start_line、end_line
        """
        def build_chunk(start: int, end: int) -> Dict[str, Any]:
            chunk_text = text[start:end]
            start_line = base_line + text.count("\n", 0, start)
            end_line = start_line + chunk_text.count("\n")
            return {
                "text": chunk_text,
                "numbered_text": cls._number_text_lines(chunk_text, start_line),
                "start_line": start_line,
                "end_line": end_line,
            }

        if len(text) <= chunk_size:
            return [build_chunk(0, len(text))]

        chunks = []
        start = 0
        while start < len(text):
            end = start + chunk_size
            if end >= len(text):
                chunks.append(build_chunk(start, len(text)))
                break
            # 在 end 附近向后找最近的换行符，避免截断句子（最多多找200字）
            newline_pos = text.find('\n', end, end + 200)
            if newline_pos != -1:
                end = newline_pos + 1
            chunks.append(build_chunk(start, end))
            # 下一块从 (end - overlap) 处开始，同样对齐到换行
            next_start = end - overlap
            if next_start > start:
                # 向后找换行对齐
                nl = text.find('\n', next_start, next_start + 200)
                if nl != -1:
                    next_start = nl + 1
            start = max(next_start, start + 1)  # 防止死循环

        return chunks

    @staticmethod
    def _merge_chunk_results(
        accumulated: Dict[str, List[Dict[str, Any]]],
        chunk_result: Dict[str, List[Dict[str, Any]]]
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        将单块提取结果合并到累积结果中。
        同名角色：合并 other_names / personality_variants / whole_body_variants /
                  clothing_variants / valid_status_combinations，去重。
        新角色：直接加入。
        """
        for char_name, states in chunk_result.items():
            if not states:
                continue
            chunk_state = states[0]  # 每块每个角色只有一条 node_state

            if char_name not in accumulated:
                accumulated[char_name] = [chunk_state]
            else:
                existing = accumulated[char_name][0]

                # 合并 other_names（去重）
                existing_names = set(existing.get("other_names", []))
                for n in chunk_state.get("other_names", []):
                    existing_names.add(n)
                existing["other_names"] = list(existing_names)

                # 合并 variants 列表（直接追加，后续 analyze_character_with_llm 会去重整合）
                for field in ("personality_variants", "whole_body_variants",
                              "clothing_variants", "valid_status_combinations"):
                    existing.setdefault(field, [])
                    chunk_state.setdefault(field, [])
                    existing[field] = existing[field] + chunk_state[field]

                # 合并情景摘要和原文引用。refs 始终由代码生成，按出现顺序去重。
                existing["info"] = StoryTreeCharacterExtractor02._merge_info_values(
                    existing.get("info"), chunk_state.get("info")
                )
                existing["refs"] = StoryTreeCharacterExtractor02._ordered_unique_strings(
                    list(existing.get("refs") or []) + list(chunk_state.get("refs") or [])
                )

                # raw_data 原本只保留第一块，这里同步为节点级累计结果，供身份去重摘要使用。
                raw_data = existing.setdefault("raw_data", {})
                if isinstance(raw_data, dict):
                    raw_data["name"] = existing.get("character_id", char_name)
                    raw_data["other_names"] = existing.get("other_names", [])
                    raw_data["info"] = existing.get("info", "")
                    raw_data["refs"] = existing.get("refs", [])
                    for field in ("personality_variants", "whole_body_variants",
                                  "clothing_variants", "valid_status_combinations"):
                        raw_data[field] = existing.get(field, [])

        return accumulated

    def extract_characters_from_markdown_full_text(
        self,
        markdown_content: str,
        node_id: str,
        node_title: str = "",
        base_line: int = 1,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        从markdown正文中分块提取角色数据。
        每块约4000字，相邻块重叠约500字，逐块调用LLM并累积合并结果。

        Args:
            markdown_content: markdown文件内容
            node_id: 节点ID
            node_title: 节点标题

        Returns:
            字典：角色ID -> 该角色在文本中的状态列表
        """
        chunks = self._split_text_into_chunks(
            markdown_content,
            chunk_size=4000,
            overlap=500,
            base_line=base_line,
        )
        total = len(chunks)
        logger.info(f"节点 {node_id} 共 {len(markdown_content)} 字，拆分为 {total} 块进行提取")

        accumulated: Dict[str, List[Dict[str, Any]]] = {}
        for idx, chunk in enumerate(chunks):
            logger.info(f"  处理块 {idx + 1}/{total}（{len(chunk['text'])} 字）")
            chunk_result = self._extract_characters_from_full_text_with_llm(
                chunk, node_id, node_title, chunk_index=idx + 1, total_chunks=total
            )
            accumulated = self._merge_chunk_results(accumulated, chunk_result)
            logger.info(f"  块 {idx + 1} 完成，当前累积角色数: {len(accumulated)}")

        return accumulated

    def _extract_characters_from_full_text_with_llm(
        self,
        chunk: Dict[str, Any],
        node_id: str,
        node_title: str,
        chunk_index: int = 1,
        total_chunks: int = 1,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        使用LLM从文本块中提取角色特征。

        Args:
            chunk: 文本块及其绝对行号信息
            node_id: 节点ID
            node_title: 节点标题
            chunk_index: 当前块序号（1-based）
            total_chunks: 总块数

        Returns:
            字典：角色ID -> 该角色在文本中的状态列表
        """
        logger.info(f"使用LLM提取角色特征: {node_id} 块 {chunk_index}/{total_chunks}")

        text_content = chunk["numbered_text"]
        chunk_start_line = int(chunk["start_line"])
        chunk_end_line = int(chunk["end_line"])

        system_prompt = self.SYSTEM_PROMPT

        chunk_hint = (
            f"(Chunk {chunk_index} of {total_chunks} — this is a partial segment of the full chapter)"
            if total_chunks > 1 else ""
        )

        user_prompt = render_prompt_template(
            self.SEGMENT_USER_PROMPT,
            {
                "chunk_hint": chunk_hint,
                "node_title": node_title,
                "text_content": text_content,
            },
            source="extract_raw_segment_user_en.yaml",
        )

        try:
            # 调用LLM
            logger.info(f"调用LLM提取角色特征: {node_id} 块 {chunk_index}/{total_chunks}")
            response = self.llm_client.chat_completion_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.3,
                max_tokens=4000
            )

            # 转换响应格式为内部数据结构
            # logger.debug(f"LLM响应类型: {type(response)}")  # 注释掉，避免打印LLM响应
            # logger.debug(f"LLM响应内容: {response}")       # 注释掉，避免打印LLM响应

            # 检查响应格式
            if response is None:
                logger.error("LLM返回None响应")
                return {}

            # 兼容 LLM 偶发返回扁平单角色结构而不是 {"角色名": {...}} 形式：
            # 例如本应返回 {"凌妙妙": {"name": "...", ...}, "慕声": {...}}，
            # 但 LLM 直接返回 {"name": "凌妙妙", "personality_variants": [...], ...}。
            # 这里把它重新包成以 name 为 key 的 dict，保持下游解析逻辑不变。
            character_data_fields = {
                "personality_variants", "whole_body_variants", "clothing_variants",
                "valid_status_combinations",
            }
            if (
                isinstance(response, dict)
                and (character_data_fields & set(response.keys()))
            ):
                inferred_name = response.get("name") or "Unknown"
                logger.warning(
                    f"检测到 LLM 返回扁平的单角色结构（顶层含 {sorted(character_data_fields & set(response.keys()))}），"
                    f"自动包装为 {{'{inferred_name}': {{...}}}}"
                )
                response = {inferred_name: response}

            character_node_data = {}
            if isinstance(response, dict):
                for char_id, char_data in response.items():
                    if not isinstance(char_data, dict):
                        continue

                    # 构建节点状态信息
                    # 提取other_names，支持两种字段名
                    other_names_raw = char_data.get("other_names", char_data.get("other_name", []))
                    if not isinstance(other_names_raw, list):
                        # 如果不是列表，尝试转换为列表或使用空列表
                        if isinstance(other_names_raw, str):
                            other_names = [other_names_raw]
                        else:
                            other_names = []
                    else:
                        other_names = other_names_raw

                    # 使用分析阶段的最终模板格式。人物及其各类状态的 refs 均由代码根据
                    # LLM 返回的绝对行号生成，后续合并只做并集，避免模型改写引用。
                    char_data = char_data.copy()
                    character_ref = self._normalize_record_evidence(
                        char_data,
                        node_id,
                        chunk_start_line,
                        chunk_end_line,
                    )
                    for evidence_field in (
                        "personality_variants",
                        "whole_body_variants",
                        "clothing_variants",
                        "valid_status_combinations",
                    ):
                        evidence_items = char_data.get(evidence_field, [])
                        if not isinstance(evidence_items, list):
                            continue
                        for item in evidence_items:
                            if isinstance(item, dict):
                                self._normalize_record_evidence(
                                    item,
                                    node_id,
                                    chunk_start_line,
                                    chunk_end_line,
                                    fallback_ref=character_ref,
                                )

                    personality_variants = char_data.get("personality_variants", [])
                    if isinstance(personality_variants, list):
                        char_data["personality_variants"] = [
                            _normalize_personality_variant(item, index)
                            for index, item in enumerate(personality_variants, 1)
                            if isinstance(item, dict)
                        ]

                    # 使用分析阶段的最终模板格式
                    # 角色名称：优先使用char_data中的name字段，否则使用char_id
                    character_name = char_data.get("name", char_id)

                    # 完全使用新格式，不兼容旧字段
                    node_state = {
                        "node_id": node_id,
                        "depth": 0,  # 未知深度
                        "node_title": node_title,
                        "character_id": character_name,
                        "other_names": other_names,
                        "info": char_data.get("info", ""),
                        "refs": char_data.get("refs", []),
                        # 分析阶段的最终模板字段
                        "personality_variants": char_data.get("personality_variants", []),
                        "whole_body_variants": char_data.get("whole_body_variants", []),
                        "clothing_variants": char_data.get("clothing_variants", []),
                        "valid_status_combinations": char_data.get("valid_status_combinations", []),
                        "raw_data": char_data,
                        "source": "full_text_analyzed"  # 标记数据来源（使用分析阶段提示词）
                    }

                    character_node_data[character_name] = [node_state]

                character_names = list(character_node_data.keys())
                logger.info(f"块 {chunk_index}/{total_chunks} 提取到 {len(character_node_data)} 个角色: {', '.join(character_names)}")
                return character_node_data
            else:
                logger.warning(f"LLM返回非字典格式: {type(response)}，无法提取角色信息")
                # 尝试从字符串中提取信息（如果response是字符串）
                if isinstance(response, str):
                    # logger.debug(f"响应字符串内容: {response[:500]}...")  # 注释掉，避免打印LLM响应
                    pass
                return {}

        except Exception as e:
            logger.error(f"LLM提取角色特征失败 {node_id} 块 {chunk_index}/{total_chunks}: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return {}

    @staticmethod
    def _unwrap_if_wrapped(
        response: Dict[str, Any],
        expected_fields: set,
        context: str = "",
    ) -> Dict[str, Any]:
        """兼容 LLM 偶发把目标结构包了一层外层 key 的情况。

        例如期望顶层是 {"name": ..., "personality_variants": ...}，
        但 LLM 返回 {"凌妙妙": {"name": ..., ...}}，或 {"result": {...}}、
        {"character": {...}}、{"data": {...}} 等。

        判定条件：
        - response 是 dict 且只有 1 个顶层 key
        - 这个 key 不在 expected_fields 里
        - 它的 value 是 dict，且与 expected_fields 有交集
        命中即解包并打 warning。

        Args:
            response: LLM 原始响应字典
            expected_fields: 当前调用方期望出现的顶层字段集合
            context: 用于日志的上下文标识（例如角色名 / 流程名）

        Returns:
            解包后的 dict，未命中则原样返回。
        """
        if not isinstance(response, dict) or len(response) != 1:
            return response
        only_key = next(iter(response))
        if only_key in expected_fields:
            return response
        inner = response[only_key]
        if not isinstance(inner, dict):
            return response
        if not (expected_fields & set(inner.keys())):
            return response
        logger.warning(
            f"检测到 LLM 返回被包了一层外层 key '{only_key}'，自动解包"
            + (f"（{context}）" if context else "")
        )
        return inner

    def _load_source_data(
        self,
        story_tree_path: str,
        task_id: Optional[str],
        log_prefix: str = "",
    ) -> Tuple[Dict[str, Any], Optional[Dict[str, str]], Optional[str], bool]:
        """
        加载数据源（数据库或文件系统）。

        Returns:
            (tree_data, md_contents, story_tree_dir, use_database)
        """
        if task_id is not None:
            logger.info(f"开始从数据库提取角色特征{log_prefix}，任务ID: {task_id}")
            task_id, tree_data, md_contents = fetch_source_data_from_db(task_id)
            if not task_id:
                logger.error("无法从数据库获取数据")
                raise ValueError("无法从数据库获取数据")
            logger.info(f"从数据库加载故事树数据，节点数量: {len(tree_data.get('nodes', {}))}")
            logger.info(f"从数据库加载markdown内容，文档数量: {len(md_contents)}")
            return tree_data, md_contents, None, True
        else:
            logger.info(f"开始按故事树顺序逐个文件提取角色特征{log_prefix}: {story_tree_path}")
            logger.info("核心原则：严格按照世界树结构，一个文件提取完所有人物信息后才进入下一个文件")
            logger.info("不要走回头路，读取完一份markdown就下一份文件进行提取")
            tree_data = self.load_story_tree(story_tree_path)
            story_tree_dir = os.path.dirname(story_tree_path)
            return tree_data, None, story_tree_dir, False

    def _resolve_markdown_content(
        self,
        node_id: str,
        node_data: Dict[str, Any],
        use_database: bool,
        md_contents: Optional[Dict[str, str]],
        story_tree_dir: Optional[str],
    ) -> Optional[str]:
        """
        获取节点对应的 markdown 内容，返回 None 表示跳过该节点。
        """
        if use_database:
            content = md_contents.get(node_id)
            if content is None:
                logger.warning(f"数据库中没有节点 {node_id} 的markdown内容，跳过")
                return None
            logger.info(f"从数据库获取节点 {node_id} 的内容，长度: {len(content)} 字符")
            return content

        # 文件系统模式
        content_file = node_data.get("content_file")
        md_path = None

        if content_file:
            md_path = content_file if os.path.isabs(content_file) else os.path.join(story_tree_dir, content_file)
            if os.path.exists(md_path):
                logger.info(f"使用content_file路径: {md_path}")
            else:
                logger.warning(f"content_file路径不存在: {md_path}")
                md_path = None

        if not md_path:
            default_path = os.path.join(story_tree_dir, "generated_stories", f"{node_id}.md")
            if os.path.exists(default_path):
                md_path = default_path
                logger.info(f"使用默认路径: {md_path}")
            else:
                generated_dir = os.path.join(story_tree_dir, "generated_stories")
                if os.path.exists(generated_dir):
                    import glob
                    possible_files = glob.glob(os.path.join(generated_dir, f"{node_id}*.md"))
                    if possible_files:
                        md_path = possible_files[0]
                        logger.info(f"找到匹配文件: {md_path}")
                    elif node_id == "chapter_1":
                        chapter_path = os.path.join(generated_dir, "chapter_1.md")
                        if os.path.exists(chapter_path):
                            md_path = chapter_path
                            logger.info(f"使用根节点文件: {md_path}")

        if not md_path:
            logger.warning(f"无法找到节点 {node_id} 的markdown文件，跳过")
            return None

        logger.info(f"读取文件: {md_path}")
        try:
            with open(md_path, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception as e:
            logger.error(f"读取markdown文件失败 {md_path}: {e}")
            return None

    def _collect_character_states(
        self,
        story_tree_path: str,
        task_id: Optional[str] = None,
        log_prefix: str = "",
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        遍历故事树，逐节点提取并合并角色状态，最后去重。

        Returns:
            字典：角色ID -> 状态列表
        """
        tree_data, md_contents, story_tree_dir, use_database = self._load_source_data(
            story_tree_path, task_id, log_prefix
        )
        traversal_order = self.depth_first_traversal(tree_data)
        character_states: Dict[str, List[Dict[str, Any]]] = {}

        for node_idx, (node_id, node_data) in enumerate(traversal_order):
            logger.info(f"=== 开始处理文件 {node_idx+1}/{len(traversal_order)} ===")
            logger.info(f"文件ID: {node_id} - 标题: {node_data.get('title', '')}")
            logger.info("原则：一个文件提取完所有的人物信息内容，才进入下一个文件，不要走回头路")

            markdown_content = self._resolve_markdown_content(
                node_id, node_data, use_database, md_contents, story_tree_dir
            )
            if markdown_content is None:
                continue

            # chapter_1 中序幕部分不提取人物，跳过分隔符之前的内容
            base_line = 1
            if node_id == "chapter_1":
                separator = "◆◆◆  序幕结束 · 正文开始  ◆◆◆"
                sep_pos = markdown_content.find(separator)
                if sep_pos != -1:
                    after_sep = markdown_content.find("\n", sep_pos)
                    if after_sep != -1:
                        base_line = markdown_content.count("\n", 0, after_sep + 1) + 1
                        markdown_content = markdown_content[after_sep + 1:]
                    logger.info("chapter_1 检测到序幕分隔符，跳过序幕内容，从正文开始提取")

            logger.info(f"通读全文提取角色数据: {node_id}")
            node_character_data = self.extract_characters_from_markdown_full_text(
                markdown_content, node_id, node_data.get("title", ""), base_line=base_line
            )

            if node_character_data:
                character_names = list(node_character_data.keys())
                logger.info(f"提取结果: 成功提取到 {len(character_names)} 个角色: {', '.join(character_names)}")
                for char_id, states in node_character_data.items():
                    if char_id not in character_states:
                        character_states[char_id] = []
                    character_states[char_id].extend(states)
            else:
                logger.info("提取结果: 未提取到角色数据")

            logger.info(f"✅ 文件 {node_id} 处理完成，已提取 {len(node_character_data)} 个角色")
            logger.info(f"=== 文件 {node_id} 所有人物信息提取完毕，准备进入下一个文件 ===\n")

        logger.info(f"所有文件处理完成，共收集到 {len(character_states)} 个角色的状态数据")

        logger.info("=" * 80)
        logger.info("开始角色去重阶段：使用LLM判断哪些角色实际上是同一个角色")
        logger.info("=" * 80)
        character_states = self.deduplicate_characters_with_llm(character_states)
        logger.info(f"去重完成，最终角色数量: {len(character_states)}")
        logger.info("=" * 80)

        return character_states

    def _build_character_features(
        self,
        character_states: Dict[str, List[Dict[str, Any]]],
    ) -> Dict[str, "StoryTreeCharacterFeaturesData"]:
        """
        对每个角色调用 LLM 分析，构建 StoryTreeCharacterFeaturesData 字典。
        """
        character_data: Dict[str, StoryTreeCharacterFeaturesData] = {}

        for char_id, states in character_states.items():
            logger.info(f"分析角色: {char_id} ({len(states)} 个节点状态)")
            try:
                analyzed_data = self.analyze_character_with_llm(char_id, states)
                features_data = StoryTreeCharacterFeaturesData(
                    name=analyzed_data.get("name", char_id),
                    info=analyzed_data.get("info", ""),
                    refs=analyzed_data.get("refs", []),
                    other_names=analyzed_data.get("other_names", []),
                    personality_variants=analyzed_data.get("personality_variants", []),
                    whole_body_variants=analyzed_data.get("whole_body_variants", []),
                    clothing_variants=analyzed_data.get("clothing_variants", []),
                    valid_status_combinations=analyzed_data.get("valid_status_combinations", [])
                )
            except Exception as e:
                logger.error(f"分析角色 {char_id} 失败: {e}")
                try:
                    fallback_data = self.create_fallback_character_data(char_id, states)
                    features_data = StoryTreeCharacterFeaturesData(
                        name=fallback_data.get("name", char_id),
                        info=fallback_data.get("info", ""),
                        refs=fallback_data.get("refs", []),
                        other_names=fallback_data.get("other_names", []),
                        personality_variants=fallback_data.get("personality_variants", []),
                        whole_body_variants=fallback_data.get("whole_body_variants", []),
                        clothing_variants=fallback_data.get("clothing_variants", []),
                        valid_status_combinations=fallback_data.get("valid_status_combinations", [])
                    )
                    logger.warning(f"角色 {features_data.name} 使用回退数据")
                except Exception as inner_e:
                    logger.error(f"创建角色 {char_id} 回退数据失败: {inner_e}")
                    continue

            # valid_status_combinations 应与 states 一一对应（LLM 每个节点生成一个）。
            # raw 阶段只保留组合及其 refs，不再生成或持久化 node_appearances。
            combos = features_data.valid_status_combinations
            default_combo = f"{features_data.name}-1-1-1"
            if combos:
                # 补齐到与 states 等长
                while len(combos) < len(states):
                    last = combos[-1]
                    combos.append(
                        last.copy()
                        if isinstance(last, dict)
                        else {"combination": last, "description": "", "is_active": False}
                    )
            else:
                combos = [
                    {
                        "combination": default_combo,
                        "description": "",
                        "info": state.get("info", ""),
                        "refs": list(state.get("refs") or []),
                        "is_active": False,
                    }
                    for state in states
                ]

            # 组合的 info/refs 描述“什么剧情情景下使用这套人物状态”。
            # LLM 未返回时，至少继承对应 node_state 的证据范围。
            for i, combo_entry in enumerate(combos):
                if not isinstance(combo_entry, dict):
                    continue
                state = states[min(i, len(states) - 1)] if states else {}
                combo_entry["info"] = str(
                    combo_entry.get("info") or state.get("info") or ""
                ).strip()
                combo_refs = combo_entry.get("refs") or state.get("refs") or []
                if not isinstance(combo_refs, list):
                    combo_refs = [combo_refs]
                combo_entry["refs"] = self._ordered_unique_strings(combo_refs)
            features_data.valid_status_combinations = combos
            character_data[features_data.name] = features_data
            logger.info(f"角色 {features_data.name} 分析完成")

        return character_data

    def process_story_tree_file_by_file(
        self,
        story_tree_path: str,
        output_dir: str = "outputs/characters_json/03_story_tree_raw",
        project_name: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> str:
        """
        按照故事树顺序提取角色特征，一个文件一个文件处理，通读全文提取人物信息。

        Args:
            story_tree_path: 故事树文件路径（如果 task_id 为 None 则必需）
            output_dir: 输出目录
            project_name: 项目名称
            task_id: 任务ID，如果提供则从数据库获取数据，忽略 story_tree_path

        Returns:
            保存目录路径
        """
        character_states = self._collect_character_states(story_tree_path, task_id)
        character_data = self._build_character_features(character_states)
        output_path = self.save_character_features_to_files(character_data, output_dir)
        logger.info(f"按顺序提取完成，已保存到: {output_path}")
        return output_path

    def create_llm_prompt_for_character(
        self,
        character_id: str,
        character_states: List[Dict[str, Any]]
    ) -> str:
        """创建LLM提示词，用于分析角色特征"""
        # 构建角色状态数据字符串
        states_str = json.dumps(character_states, ensure_ascii=False, indent=2)

        states_count = len(character_states)
        prompt = render_prompt_template(
            self.MERGE_USER_PROMPT,
            {
                "character_id": character_id,
                "states_count": states_count,
                "states_str": states_str,
            },
            source="extract_raw_merge_user_en.yaml",
        )

        return prompt

    def validate_and_fix_variant_fields(
        self,
        response: Dict[str, Any],
        character_states: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        """
        验证并修复变体数组中的字段完整性

        确保每个变体字典包含所有必需字段，缺失的字段使用默认值填充。
        对于other_names字段，如果为空或缺失，尝试从character_states中恢复数据。

        Args:
            response: LLM返回的响应字典
            character_states: 可选的节点状态列表，用于恢复other_names数据

        Returns:
            修复后的响应字典
        """
        # 确保响应是字典
        if not isinstance(response, dict):
            return response

        state_infos = []
        state_refs: List[str] = []
        for state in character_states or []:
            state_infos.append(state.get("info"))
            state_refs.extend(state.get("refs") or [])

        response_info = str(response.get("info") or "").strip()
        response["info"] = response_info or self._merge_info_values(state_infos)
        response_refs = response.get("refs", [])
        if not isinstance(response_refs, list):
            response_refs = [response_refs]
        response["refs"] = self._ordered_unique_strings(
            response_refs + state_refs
        )

        # 1. 验证并修复 personality_variants
        if "personality_variants" in response:
            personality_variants = response["personality_variants"]
            if not isinstance(personality_variants, list):
                # 如果不是列表，设置为空列表
                response["personality_variants"] = []
                personality_variants = []

            if isinstance(personality_variants, list):
                fixed_variants = []
                for idx, variant in enumerate(personality_variants):
                    if not isinstance(variant, dict):
                        # 如果不是字典，跳过或创建默认字典
                        continue

                    variant = _normalize_personality_variant(variant, idx + 1)

                    required_fields = {
                        "personality_id": idx + 1,
                        "traits": [],
                        "info": "",
                        "refs": [],
                    }

                    for field, default_value in required_fields.items():
                        if field not in variant:
                            variant[field] = default_value
                        # 确保字段类型正确
                        elif field == "personality_id":
                            # 确保是整数
                            try:
                                variant[field] = int(variant[field])
                            except (ValueError, TypeError):
                                variant[field] = idx + 1
                        elif field == "traits":
                            if not isinstance(variant[field], list):
                                variant[field] = _normalize_personality_traits(variant[field])
                        elif field == "info":
                            variant[field] = str(variant[field] or "").strip()
                        elif field == "refs":
                            refs = variant[field] if isinstance(variant[field], list) else [variant[field]]
                            variant[field] = self._ordered_unique_strings(refs)

                    fixed_variants.append(variant)

                response["personality_variants"] = fixed_variants

        # 2. 验证并修复 whole_body_variants
        if "whole_body_variants" in response:
            whole_body_variants = response["whole_body_variants"]
            if isinstance(whole_body_variants, list):
                fixed_variants = []
                for idx, variant in enumerate(whole_body_variants):
                    if not isinstance(variant, dict):
                        continue

                    variant = variant.copy()

                    # 必需字段及其默认值
                    required_fields = {
                        "whole_body_id": idx + 1,
                        "age": "",
                        "apparent_age": "",
                        "age_category": "",
                        "height": "",
                        "height_ai_guess": True,
                        "gender": "",
                        "race": "人类",
                        "human_animal_form": "",
                        "info": "",
                        "refs": [],
                        "appearance": {
                            "head": {
                                "hair_color": "",
                                "hair_style": "",
                                "eye_color": "",
                                "skin_color": "",
                                "facial_description": ""
                            },
                            "body": {
                                "body_type": "",
                                "body_description": ""
                            }
                        }
                    }

                    # 确保顶层字段存在
                    for field, default_value in required_fields.items():
                        if field not in variant:
                            variant[field] = default_value
                        elif field == "whole_body_id":
                            try:
                                variant[field] = int(variant[field])
                            except (ValueError, TypeError):
                                variant[field] = idx + 1
                        elif field == "info":
                            variant[field] = str(variant[field] or "").strip()
                        elif field == "height_ai_guess":
                            value = variant[field]
                            if isinstance(value, str):
                                variant[field] = value.strip().lower() in ("true", "1", "yes")
                            else:
                                variant[field] = bool(value)
                        elif field == "refs":
                            refs = variant[field] if isinstance(variant[field], list) else [variant[field]]
                            variant[field] = self._ordered_unique_strings(refs)
                        elif field == "apparent_age":
                            # 表面年龄仅保留数字字符串，非法值清空
                            av = str(variant[field] or "").strip()
                            variant[field] = av if av.isdigit() else ""
                        elif field == "human_animal_form":
                            # 非法或未知等级回退空字符串，交由生图端按人类保守处理
                            fv = str(variant[field] or "").strip()
                            variant[field] = fv if fv in _VALID_HUMAN_ANIMAL_FORMS else ""

                    # 唯一强关系 apparent_age → age_category：外表年龄有数字时，
                    # age_category 必须按其区间派生，不采纳基于真实 age 的判断
                    # （百岁看似18 → adolescent，而非 elderly）。apparent_age 为空时，
                    # 保留 LLM 依外表/上下文给出的 age_category，绝不由真实 age 反推。
                    derived_category = _age_number_to_category(variant.get("apparent_age"))
                    if derived_category:
                        variant["age_category"] = derived_category

                    # 确保appearance字段是字典且包含必要结构
                    if not isinstance(variant.get("appearance"), dict):
                        variant["appearance"] = required_fields["appearance"]
                    else:
                        # 确保head和body存在
                        appearance = variant["appearance"]
                        if not isinstance(appearance.get("head"), dict):
                            appearance["head"] = required_fields["appearance"]["head"]
                        else:
                            # 确保head中的字段存在
                            head = appearance["head"]
                            for head_field in ["hair_color", "hair_style", "eye_color", "skin_color", "facial_description"]:
                                if head_field not in head:
                                    head[head_field] = ""

                        if not isinstance(appearance.get("body"), dict):
                            appearance["body"] = required_fields["appearance"]["body"]
                        else:
                            # 确保body中的字段存在
                            body = appearance["body"]
                            for body_field in ["body_type", "body_description"]:
                                if body_field not in body:
                                    body[body_field] = ""

                    fixed_variants.append(variant)

                response["whole_body_variants"] = fixed_variants

        # 3. 验证并修复 clothing_variants
        if "clothing_variants" in response:
            clothing_variants = response["clothing_variants"]
            if isinstance(clothing_variants, list):
                fixed_variants = []
                for idx, variant in enumerate(clothing_variants):
                    if not isinstance(variant, dict):
                        continue

                    variant = variant.copy()

                    # 必需字段及其默认值
                    required_fields = {
                        "clothing_id": idx + 1,
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

                    for field, default_value in required_fields.items():
                        if field not in variant:
                            variant[field] = default_value
                        elif field == "clothing_id":
                            try:
                                variant[field] = int(variant[field])
                            except (ValueError, TypeError):
                                variant[field] = idx + 1
                        elif field in ["accessories", "tmp_accessories", "weapons", "armor"]:
                            values = variant[field] if isinstance(variant[field], list) else [variant[field]]
                            variant[field] = self._ordered_unique_strings(values)
                        elif field == "info":
                            variant[field] = str(variant[field] or "").strip()
                        elif field == "refs":
                            refs = variant[field] if isinstance(variant[field], list) else [variant[field]]
                            variant[field] = self._ordered_unique_strings(refs)

                    fixed_variants.append(variant)

                response["clothing_variants"] = fixed_variants

        # 4. 验证并修复 other_names 字段
        if "other_names" not in response:
            response["other_names"] = []
        elif not isinstance(response["other_names"], list):
            # 如果不是列表，尝试转换或使用空列表
            if isinstance(response["other_names"], str):
                response["other_names"] = [response["other_names"]]
            else:
                response["other_names"] = []

        # 如果other_names为空列表或缺失，且提供了character_states，尝试从节点状态中恢复数据
        if (not response.get("other_names") or response["other_names"] == []) and character_states:
            other_names_set = set()
            for state in character_states:
                state_other_names = state.get("other_names", [])
                if isinstance(state_other_names, list):
                    other_names_set.update(state_other_names)
                elif isinstance(state_other_names, str):
                    other_names_set.add(state_other_names)

            if other_names_set:
                response["other_names"] = list(other_names_set)

        # 5. 验证并修复状态组合的证据字段
        combinations = response.get("valid_status_combinations", [])
        if not isinstance(combinations, list):
            combinations = []
        fixed_combinations = []
        for combination in combinations:
            if isinstance(combination, str):
                combination = {
                    "combination": combination,
                    "description": "",
                    "is_active": False,
                }
            if not isinstance(combination, dict):
                continue
            combination = combination.copy()
            combination["info"] = str(combination.get("info") or "").strip()
            refs = combination.get("refs", [])
            combination["refs"] = self._ordered_unique_strings(
                refs if isinstance(refs, list) else [refs]
            )
            fixed_combinations.append(combination)
        response["valid_status_combinations"] = fixed_combinations

        # 6. 清理字段内容
        # 清理 whole_body_variants 中的字段
        if "whole_body_variants" in response and isinstance(response["whole_body_variants"], list):
            for variant in response["whole_body_variants"]:
                if isinstance(variant, dict):
                    # 清理 skin_color
                    appearance = variant.get("appearance")
                    if isinstance(appearance, dict):
                        head = appearance.get("head")
                        if isinstance(head, dict) and head.get("skin_color") == "普通":
                            head["skin_color"] = ""

                        # 清理 body_description
                        body = appearance.get("body")
                        if isinstance(body, dict):
                            body_desc = body.get("body_description", "")
                            if isinstance(body_desc, str):
                                # 简单的非身体描述关键词检测（不完善，但可处理明显错误）
                                non_body_keywords = ["被折叠刀抵喉", "被刀抵", "被枪指", "被绑架"]
                                for keyword in non_body_keywords:
                                    if keyword in body_desc:
                                        # 如果描述主要是非身体内容，清空字段
                                        body["body_description"] = ""
                                        break
                            # 清理 body_type
                            body_type = body.get("body_type", "")
                            if isinstance(body_type, str) and body_type == "普通":
                                body["body_type"] = ""

        # 清理 clothing_variants 中的 outfit_discription 字段
        if "clothing_variants" in response and isinstance(response["clothing_variants"], list):
            for variant in response["clothing_variants"]:
                if isinstance(variant, dict):
                    outfit = variant.get("outfit_discription", "")
                    if isinstance(outfit, str):
                        # 处理 "未具体描述，但注重外表" -> "注重外表"
                        if "未具体描述，但注重外表" in outfit:
                            variant["outfit_discription"] = "注重外表"
                        # 处理 "未具体描述" -> ""
                        elif outfit == "未具体描述":
                            variant["outfit_discription"] = ""
                        # 处理 "未详述" -> ""
                        elif outfit == "未详述":
                            variant["outfit_discription"] = ""
                        # 处理 "未描述" -> ""
                        elif outfit == "未描述":
                            variant["outfit_discription"] = ""
                        # 处理 "未提及" -> ""
                        elif outfit == "未提及":
                            variant["outfit_discription"] = ""
                        # 处理 "未详细描述，但为..." 模式
                        elif "未详细描述，但为" in outfit:
                            # 提取"但为"后面的部分
                            import re
                            match = re.search(r"未详细描述，但为(.+)", outfit)
                            if match:
                                variant["outfit_discription"] = match.group(1).strip()
                        # 处理时间序列描述（如"早期→中期→晚期"）
                        elif "→" in outfit:
                            # 取第一个"→"之前的部分，并去除括号内容
                            import re
                            # 分割字符串，取第一部分
                            first_part = outfit.split("→")[0].strip()
                            # 去除括号内容，如"（早期）"
                            first_part = re.sub(r"（[^）]*）", "", first_part).strip()
                            variant["outfit_discription"] = first_part
                        # 处理过于笼统的描述如"普通衣服"
                        elif outfit in ["普通衣服", "普通服装", "普通着装"]:
                            variant["outfit_discription"] = ""

                    # 清理 clothing_style 字段中的"普通便装"等笼统描述
                    clothing_style = variant.get("clothing_style", "")
                    if isinstance(clothing_style, str):
                        if clothing_style in ["普通便装", "普通衣服", "普通服装", "普通着装"]:
                            variant["clothing_style"] = ""

        return response

    def analyze_character_with_llm(
        self,
        character_id: str,
        character_states: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """使用LLM分析角色特征"""
        logger.info(f"使用LLM分析角色: {character_id} ({len(character_states)} 个节点状态)")

        try:
            # 创建提示词
            user_prompt = self.create_llm_prompt_for_character(character_id, character_states)

            # 调用LLM
            logger.debug(f"调用LLM分析角色: {character_id}")
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

            # 记录LLM返回的字段，用于诊断
            logger.debug(f"LLM返回的字段: {list(response.keys())}")

            # 兼容 LLM 偶发把整个角色 JSON 包了一层外层 key 的情况，如
            # {"凌妙妙": {"name": "...", "other_names": [...], ...}}
            # 或 {"character": {...}}, {"result": {...}}, {"data": {...}}
            response = self._unwrap_if_wrapped(
                response,
                expected_fields={
                    "name", "info", "refs", "other_names",
                    "personality_variants", "whole_body_variants", "clothing_variants",
                    "valid_status_combinations",
                },
                context=f"single-character analysis: {character_id}",
            )

            # 保留原始响应快照，用于诊断（在默认值填充之前）
            raw_keys_snapshot = list(response.keys())
            try:
                raw_preview = json.dumps(response, ensure_ascii=False)[:1000]
            except Exception:
                raw_preview = repr(response)[:1000]

            # 确保包含必要字段，使用默认值而不是直接回退
            required_fields = {
                "name": character_id,  # 如果缺少name，使用character_id作为默认值
                "other_names": [],
                "personality_variants": [],
                "whole_body_variants": [],
                "clothing_variants": []
            }

            # 新增证据字段兼容旧响应：缺失时稍后从 character_states 恢复，
            # 不计入“关键字段缺失过多”的回退阈值。
            response.setdefault("info", "")
            response.setdefault("refs", [])

            missing_fields = []
            for field, default_value in required_fields.items():
                if field not in response:
                    logger.warning(f"LLM响应缺少字段: {field}，使用默认值")
                    response[field] = default_value
                    missing_fields.append(field)

            # 如果缺少太多关键字段（超过3个），说明LLM响应质量很差，使用回退方案
            if len(missing_fields) > 3:
                logger.error(
                    f"LLM响应缺少过多必要字段: {missing_fields}，使用回退方案。"
                    f"LLM原始顶层字段: {raw_keys_snapshot}；原始内容预览: {raw_preview}"
                )
                response = self.create_fallback_character_data(character_id, character_states)
            elif missing_fields:
                logger.info(f"已自动补充缺失字段: {missing_fields}")

            # 验证并修复变体数组中的字段完整性
            response = self.validate_and_fix_variant_fields(response, character_states)

            # 添加valid_status_combinations如果缺失
            if "valid_status_combinations" not in response:
                response["valid_status_combinations"] = []

            character_name = response.get("name", character_id)
            logger.info(f"角色 {character_name} 分析成功")

            return response

        except Exception as e:
            logger.error(f"LLM分析失败 {character_id}: {e}")
            # 回退到简单分析
            return self.create_fallback_character_data(character_id, character_states)

    def deduplicate_characters_with_llm(
        self,
        character_states: Dict[str, List[Dict[str, Any]]]
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        使用LLM判断哪些角色实际上是同一个角色，并合并它们的状态数据

        Args:
            character_states: 角色ID -> 状态列表的字典

        Returns:
            去重后的角色状态字典
        """
        if len(character_states) <= 1:
            logger.info("只有1个或0个角色，无需去重")
            return character_states

        logger.info(f"开始分析 {len(character_states)} 个角色，判断是否存在重复...")

        # 构建角色摘要信息，用于LLM判断
        character_summaries = {}
        for char_id, states in character_states.items():
            # 收集该角色的所有名称
            all_names = set([char_id])
            for state in states:
                other_names = state.get("other_names", [])
                if isinstance(other_names, list):
                    all_names.update(other_names)
                elif isinstance(other_names, str):
                    all_names.add(other_names)

            # 收集该角色出现的节点
            nodes = [state.get("node_id", "unknown") for state in states]

            # 收集一些关键特征（用于辅助判断）
            sample_state = states[0] if states else {}
            raw_data = sample_state.get("raw_data", {})

            character_summaries[char_id] = {
                "primary_name": char_id,
                "all_names": list(all_names),
                "node_count": len(states),
                "nodes": nodes,
                "info": self._merge_info_values(
                    [state.get("info") for state in states]
                ),
                "refs": self._ordered_unique_strings(
                    [ref for state in states for ref in (state.get("refs") or [])]
                ),
                "sample_description": {
                    "personality": _normalize_personality_traits(
                        raw_data.get("personality_variants", [{}])[0].get("traits", [])
                    ) if raw_data.get("personality_variants") else [],
                    "gender": raw_data.get("whole_body_variants", [{}])[0].get("gender", "") if raw_data.get("whole_body_variants") else "",
                    "occupation": raw_data.get("clothing_variants", [{}])[0].get("occupation", "") if raw_data.get("clothing_variants") else ""
                }
            }

        # 构建LLM提示词
        system_prompt = self.DEDUPLICATE_SYSTEM_PROMPT

        # 构建角色列表描述
        characters_description = []
        for char_id, summary in character_summaries.items():
            desc = f"""Character ID: {char_id}
- Primary Name: {summary['primary_name']}
- All Names: {', '.join(summary['all_names'])}
- Appears in {summary['node_count']} nodes: {', '.join(summary['nodes'][:5])}{'...' if len(summary['nodes']) > 5 else ''}
- Context Info: {summary['info'] or 'unknown'}
- Source Refs: {', '.join(summary['refs'][:8]) if summary['refs'] else 'unknown'}
- Sample Info:
  * Gender: {summary['sample_description']['gender'] or 'unknown'}
  * Occupation: {summary['sample_description']['occupation'] or 'unknown'}
  * Personality Traits: {', '.join(summary['sample_description']['personality'][:3]) if summary['sample_description']['personality'] else 'unknown'}
"""
            characters_description.append(desc)

        user_prompt = render_prompt_template(
            self.DEDUPLICATE_USER_PROMPT,
            {
                "character_count": len(character_states),
                "characters_description": "\n".join(characters_description),
            },
            source="extract_raw_deduplicate_user_en.yaml",
        )

        try:
            logger.info("调用LLM进行角色去重分析...")
            response = self.llm_client.chat_completion_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.1,  # 使用较低温度以获得更一致的结果
                max_tokens=2000
            )

            if not isinstance(response, dict):
                logger.error(f"LLM返回的不是字典: {type(response)}")
                return character_states

            # 兼容 LLM 偶发把去重结果包了一层外层 key，如
            # {"result": {"merge_groups": [...], "unique_characters": [...]}}
            response = self._unwrap_if_wrapped(
                response,
                expected_fields={"merge_groups", "unique_characters", "total_unique_count"},
                context="character deduplication",
            )

            merge_groups = response.get("merge_groups", [])
            unique_characters = response.get("unique_characters", [])
            total_unique_count = response.get("total_unique_count", len(character_states))

            logger.info(f"LLM分析结果: 发现 {len(merge_groups)} 个合并组，{len(unique_characters)} 个独立角色")
            logger.info(f"预计去重后角色数量: {total_unique_count}")

            # 执行合并：按 LLM 给出的合并组把各角色状态并到 canonical_name 下。
            deduplicated_states: Dict[str, List[Dict[str, Any]]] = {}
            merged_char_ids = set()

            for group in merge_groups:
                canonical_name = group.get("canonical_name")
                merged_characters = group.get("merged_characters", []) or group.get("characters", [])
                reason = group.get("reason", "")
                if not canonical_name and merged_characters:
                    canonical_name = merged_characters[0]
                    logger.warning(f"合并组缺少 canonical_name，自动使用第一个角色名: {canonical_name}")

                if not canonical_name or not merged_characters:
                    logger.warning(f"跳过无效的合并组: {group}")
                    continue

                logger.info(f"合并角色组: {canonical_name}")
                logger.info(f"  - 合并的角色: {', '.join(merged_characters)}")
                logger.info(f"  - 原因: {reason}")

                merged_states: List[Dict[str, Any]] = []
                for char_id in merged_characters:
                    if char_id in character_states:
                        merged_states.extend(character_states[char_id])
                        merged_char_ids.add(char_id)
                    else:
                        logger.warning(f"  - 警告: 角色 {char_id} 不在原始数据中")

                if merged_states:
                    deduplicated_states[canonical_name] = merged_states
                    logger.info(f"  - 合并后状态数量: {len(merged_states)}")

            # 保留未被任何合并组吸收的独立角色。
            for char_id in character_states.keys():
                if char_id not in merged_char_ids:
                    deduplicated_states[char_id] = character_states[char_id]

            logger.info(f"去重完成: {len(character_states)} -> {len(deduplicated_states)} 个角色")

            # 显示去重前后的对比
            logger.info("去重前角色列表:")
            for char_id in sorted(character_states.keys()):
                logger.info(f"  - {char_id}")
            logger.info("去重后角色列表:")
            for char_id in sorted(deduplicated_states.keys()):
                logger.info(f"  - {char_id}")

            return deduplicated_states

        except Exception as e:
            logger.error(f"LLM去重失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
            logger.warning("LLM去重失败，返回原始角色数据")
            return character_states

    def create_fallback_character_data(
        self,
        character_id: str,
        character_states: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """创建回退角色数据（当LLM失败时使用）"""
        logger.warning(f"为角色 {character_id} 使用回退分析方案")

        # 简单的分析策略
        personality_variants = []
        whole_body_variants = []
        clothing_variants = []
        valid_status_combinations = []

        # 提取基本信息
        name = character_id

        # 收集所有节点中的 other_names
        other_names_set = set()
        state_infos = []
        state_refs: List[str] = []
        for state in character_states:
            state_other_names = state.get("other_names", [])
            if isinstance(state_other_names, list):
                other_names_set.update(state_other_names)
            elif isinstance(state_other_names, str):
                other_names_set.add(state_other_names)
            state_infos.append(state.get("info"))
            state_refs.extend(state.get("refs") or [])

        other_names = list(other_names_set)
        info = self._merge_info_values(state_infos)
        refs = self._ordered_unique_strings(state_refs)

        # 为每个节点创建简单的状态组合
        for i, state in enumerate(character_states):
            node_id = state.get("node_id", f"node_{i}")

            # 创建默认变体
            personality_id = 1
            whole_body_id = 1
            clothing_id = 1

            # 如果有多个不同状态，可以创建更多变体
            if i > 0:
                # 简单检测变化
                prev_state = character_states[i-1]
                if state.get("appearance") != prev_state.get("appearance"):
                    whole_body_id = 2
                if state.get("clothing") != prev_state.get("clothing"):
                    clothing_id = 2

            # 创建状态组合
            combination = f"{name}-{personality_id}-{whole_body_id}-{clothing_id}"
            valid_status_combinations.append({
                "combination": combination,
                "description": "",
                "info": str(state.get("info") or "").strip(),
                "refs": self._ordered_unique_strings(list(state.get("refs") or [])),
                "is_active": False,
            })

        # 创建基本变体
        if character_states:
            first_state = character_states[0]

            # 性格变体
            personality_variants.append({
                "personality_id": 1,
                "traits": [],
                "info": str(first_state.get("info") or "").strip(),
                "refs": self._ordered_unique_strings(list(first_state.get("refs") or [])),
            })

            # 身体变体
            appearance = first_state.get("appearance", {})
            whole_body_variants.append({
                "whole_body_id": 1,
                "age": "",
                "apparent_age": "",
                "age_category": "",
                "height": "",
                "height_ai_guess": True,
                "gender": "",
                "race": "人类",
                "human_animal_form": "",
                "info": str(first_state.get("info") or "").strip(),
                "refs": self._ordered_unique_strings(list(first_state.get("refs") or [])),
                "appearance": {
                    "head": {
                        "hair_color": "",
                        "hair_style": "",
                        "eye_color": "",
                        "skin_color": "",
                        "facial_description": str(appearance.get("facial_features", []))
                    },
                    "body": {
                        "body_type": "",
                        "body_description": str(appearance.get("body_details", ""))
                    }
                }
            })

            # 服装变体
            clothing = first_state.get("clothing", {})
            clothing_variants.append({
                "clothing_id": 1,
                "occupation": "",
                "clothing_style": "",
                "outfit_discription": str(clothing.get("texture", "")),
                "accessories": clothing.get("accessories", []),
                "tmp_accessories": clothing.get("tmp_accessories", []),
                "weapons": [],
                "armor": [],
                "info": str(first_state.get("info") or "").strip(),
                "refs": self._ordered_unique_strings(list(first_state.get("refs") or [])),
            })

        return {
            "name": name,
            "info": info,
            "refs": refs,
            "other_names": other_names,
            "personality_variants": personality_variants,
            "whole_body_variants": whole_body_variants,
            "clothing_variants": clothing_variants,
            "valid_status_combinations": valid_status_combinations
        }

    def save_character_features_to_files(
        self,
        character_data: Dict[str, StoryTreeCharacterFeaturesData],
        output_dir: str = "outputs/characters_json/03_story_tree_raw",
    ) -> str:
        """
        保存角色特征数据到JSON文件。

        Args:
            character_data: 角色特征数据字典
            output_dir: 输出目录

        Returns:
            保存目录路径
        """
        import re

        # 创建时间戳子目录
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        output_path = Path(output_dir) / timestamp
        output_path.mkdir(parents=True, exist_ok=True)

        # 保存每个角色的JSON文件
        for char_name, data in character_data.items():
            # 转换为拼音驼峰格式文件名
            camel_name = chinese_to_camel_case(char_name)
            filepath = output_path / f"{camel_name}.json"

            # 转换为字典并保存
            data_dict = {
                "name": data.name,
                "info": data.info,
                "refs": data.refs,
                "other_names": data.other_names,
                "personality_variants": data.personality_variants,
                "whole_body_variants": data.whole_body_variants,
                "clothing_variants": data.clothing_variants,
                "valid_status_combinations": data.valid_status_combinations,
                "important_rank": data.important_rank
            }

            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(data_dict, f, ensure_ascii=False, indent=2)

            logger.info(f"角色特征已保存到: {filepath}")

        # 创建映射文件
        mapping_file = output_path / "character_mapping.json"
        mapping_data = {}
        for char_name, data in character_data.items():
            camel_name = chinese_to_camel_case(char_name)
            mapping_data[char_name] = {
                "name": data.name,
                "info": data.info,
                "refs": data.refs,
                "personality_variants": [v.get("personality_id") for v in data.personality_variants],
                "whole_body_variants": [v.get("whole_body_id") for v in data.whole_body_variants],
                "clothing_variants": [v.get("clothing_id") for v in data.clothing_variants],
                "valid_status_combinations": data.valid_status_combinations,
                "important_rank": data.important_rank,
                "filepath": str(output_path / f"{camel_name}.json")
            }

        with open(mapping_file, 'w', encoding='utf-8') as f:
            json.dump(mapping_data, f, ensure_ascii=False, indent=2)

        logger.info(f"角色映射已保存到: {mapping_file}")

        return str(output_path)

    def extract_to_memory(
        self,
        story_tree_path: str,
        task_id: Optional[str] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        按照故事树顺序提取角色特征，返回内存中的数据，不保存到文件。

        Args:
            story_tree_path: 故事树文件路径（如果 task_id 为 None 则必需）
            task_id: 任务ID，如果提供则从数据库获取数据，忽略 story_tree_path

        Returns:
            字典：角色名 -> 该角色的数据列表（单个元素列表，格式与文件读取一致）
        """
        character_states = self._collect_character_states(story_tree_path, task_id, log_prefix="到内存")
        character_data = self._build_character_features(character_states)

        result = {}
        for char_name, features_data in character_data.items():
            result[char_name] = [{
                "name": features_data.name,
                "info": features_data.info,
                "refs": features_data.refs,
                "other_names": features_data.other_names,
                "personality_variants": features_data.personality_variants,
                "whole_body_variants": features_data.whole_body_variants,
                "clothing_variants": features_data.clothing_variants,
                "valid_status_combinations": features_data.valid_status_combinations,
                "important_rank": features_data.important_rank,
            }]

        logger.info(f"内存提取完成，共 {len(result)} 个角色")
        return result


def find_latest_story_tree_file():
    """查找最新的 story_tree.json 文件"""
    story_tree_dir = Path("story_tree")
    if not story_tree_dir.exists():
        return None

    # 查找所有子目录中的 story_tree.json 文件
    story_tree_files = []
    for subdir in story_tree_dir.iterdir():
        if subdir.is_dir():
            json_file = subdir / "story_tree.json"
            if json_file.exists():
                # 按目录修改时间排序
                story_tree_files.append((subdir.stat().st_mtime, json_file))

    if not story_tree_files:
        return None

    # 按修改时间排序，返回最新的文件
    story_tree_files.sort(key=lambda x: x[0], reverse=True)
    return str(story_tree_files[0][1])


def main():
    """命令行入口"""
    import argparse

    # 加载环境变量（从项目根目录）
    try:
        from dotenv import load_dotenv
        from pathlib import Path
        # 加载主 .env 文件（novel-game-factory/.env）
        dotenv_path = Path(__file__).resolve().parent.parent.parent.parent / ".env"
        load_dotenv(dotenv_path=dotenv_path)
        print(f"已加载环境变量文件: {dotenv_path}")
    except ImportError:
        print("⚠️  警告: 未安装 python-dotenv，无法加载 .env 文件")

    # 清理代理环境变量
    os.environ.pop("http_proxy", None)
    os.environ.pop("https_proxy", None)
    os.environ.pop("all_proxy", None)

    parser = argparse.ArgumentParser(description="故事树人物特征提取器（02版本）")
    parser.add_argument(
        "story_tree_file",
        nargs='?',  # 改为可选参数
        help="故事树文件路径（story_tree.json 或 extracted_story_tree.json）。如果未提供，自动查找最新的文件。"
    )
    parser.add_argument(
        "--output-dir",
        default="outputs/characters_json/03_story_tree_raw",
        help="输出目录 (默认: outputs/characters_json/03_story_tree_raw)"
    )
    parser.add_argument(
        "--project-name",
        help="项目名称 (默认使用文件名)"
    )
    parser.add_argument(
        "--provider",
        choices=["openai", "claude", "qwen"],
        default="qwen",
        help="LLM 提供商 (默认: qwen)"
    )
    parser.add_argument(
        "--model",
        help="模型名称 (默认根据提供商选择)"
    )
    parser.add_argument(
        "--task-id",
        help="任务ID，如果提供则从数据库获取数据，忽略 story_tree_file 参数"
    )

    args = parser.parse_args()

    # 创建提取器
    extractor = StoryTreeCharacterExtractor02(provider=args.provider, model=args.model)

    try:
        # 判断数据来源：数据库还是文件系统
        if args.task_id:
            print(f"使用数据库模式，任务ID: {args.task_id}")
            # 从数据库提取，不需要 story_tree_file
            output_path = extractor.process_story_tree_file_by_file(
                story_tree_path="",  # 传递空字符串，因为 task_id 已提供
                output_dir=args.output_dir,
                project_name=args.project_name,
                task_id=args.task_id,
            )
        else:
            # 文件系统模式
            # 如果没有提供 story_tree_file，自动查找最新的文件
            if args.story_tree_file is None:
                print("未提供故事树文件，自动查找最新的 story_tree.json...")
                args.story_tree_file = find_latest_story_tree_file()
                if args.story_tree_file is None:
                    print("错误: 未找到任何 story_tree.json 文件。请手动指定文件路径。")
                    return
                print(f"找到最新文件: {args.story_tree_file}")

            # 检查文件是否存在
            if not os.path.exists(args.story_tree_file):
                print(f"错误: 文件不存在: {args.story_tree_file}")
                return

            # 按顺序提取并保存
            output_path = extractor.process_story_tree_file_by_file(
                story_tree_path=args.story_tree_file,
                output_dir=args.output_dir,
                project_name=args.project_name,
                task_id=None,  # 明确传递 None
            )

        mode_str = "数据库" if args.task_id else "文件系统"
        print(f"\n✅ 故事树人物特征提取完成! (模式: {mode_str})")
        print(f"   输出目录: {output_path}")
        print(f"   处理模式: 逐个文件处理，通读全文提取，不要走回头路")

    except Exception as e:
        print(f"❌ 提取失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
