"""
世界观解析者 (World Analyst)
读取原始小说，提取静态元素（人物、场景、物品、世界观规则）并生成 world_bible.json。
"""
import os
import json
import logging
import time
import re
import uuid
from pathlib import Path
from typing import Dict, Any, Optional
from dataclasses import dataclass, field, asdict

from .llm_client import LLMClient, LLMProvider
from .novel_text_encoding import read_novel_text


# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class WorldBible:
    """世界圣经数据结构 (v3.6 增强版)"""
    project_name: str = "未命名项目"
    art_style: str = ""  # 小说类型，必须是以下三类之一：仙侠古风、科幻、现代都市
    worldview: Dict[str, Any] = field(default_factory=dict)  # 世界体系
    characters: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # 角色信息
    scenes: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # 场景信息
    # v3.6 新增字段
    relationship_network: Dict[str, Any] = field(default_factory=dict)  # 角色关系网
    scene_graph: Dict[str, Any] = field(default_factory=dict)  # 场景拓扑图
    narrative_structure: Dict[str, Any] = field(default_factory=dict)  # 叙事结构
    task_id: Optional[str] = None  # 关联的task_id (用于数据库关联和文件存储)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return asdict(self)

    def save(self, filepath: str):
        """保存到文件"""
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        logger.info(f"世界圣经已保存到: {filepath}")

    @classmethod
    def load(cls, filepath: str) -> 'WorldBible':
        """从文件加载"""
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return cls(**data)


class WorldAnalyst:
    """
    世界观解析者 Agent

    职责：
    1. 读取原始小说文本
    2. 使用 LLM 提取静态元素
    3. 生成 world_bible.json
    """

    @staticmethod
    def detect_language(text: str) -> str:
        """
        自动检测文本主要语言

        Returns:
            'zh' (中文), 'en' (英文), 'ja' (日文), 或 'other'
        """
        # 统计各类字符（只检查前5000字符以提高速度）
        sample = text[:5000]
        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', sample))
        japanese_chars = len(re.findall(r'[\u3040-\u309f\u30a0-\u30ff]', sample))
        english_words = len(re.findall(r'\b[a-zA-Z]+\b', sample))

        # 判断主要语言
        if chinese_chars > max(english_words, japanese_chars * 2):
            return 'zh'
        elif japanese_chars > max(chinese_chars, english_words):
            return 'ja'
        elif english_words > 20:
            return 'en'
        else:
            return 'other'

    @staticmethod
    def generate_task_id() -> str:
        """生成任务ID: YYYYMMDD_HHMMSS_xxxx"""
        import datetime
        import random
        import string
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=4))
        return f"{timestamp}_{suffix}"

    @staticmethod
    def generate_task_id_from_project_name(project_name: str) -> str:
        """
        生成task_id: 12位uuid

        Args:
            project_name: 项目/小说名称（保留参数以兼容调用方，不再参与生成）

        Returns:
            str: task_id 格式: 12位uuid
        """
        return uuid.uuid4().hex[:12]

    @staticmethod
    def map_art_style_to_code(art_style_str: str) -> int:
        """
        将艺术风格文字映射为数字代码

        Args:
            art_style_str: 艺术风格文字

        Returns:
            int: 1-仙侠古风, 2-科幻, 3-现代都市, 0-未知
        """
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

    # 系统提示词（原始多语言版本 - v3.6 增强版 - 保留但不使用）
    SYSTEM_PROMPT_ORIGINAL = """
You are a professional world builder and narrative analyst. Your task is to deeply analyze novel text and extract comprehensive world-building elements.

**CRITICAL LANGUAGE INSTRUCTION**:
- The input novel language is: {detected_language}
- You MUST extract and return content in THE SAME LANGUAGE as the input
- Character names, descriptions, keywords MUST use the input language
- If input is English, output English content
- If input is Chinese, output Chinese content
- If input is Japanese, output Japanese content
- Maintain language consistency throughout all extracted content

# Task
Carefully read the provided novel text and extract the following elements with MAXIMUM DETAIL:

## 0. **Art Style** (艺术风格)
Determine the novel's artistic style based on the content. The art style MUST be one of these three categories:
- **仙侠古风** (Xianxia Ancient Style): Fantasy with Chinese mythology, cultivation, immortals, ancient China setting
- **科幻** (Sci-Fi): Science fiction, futuristic technology, space, aliens, cyberpunk
- **现代都市** (Modern Urban): Contemporary city life, romance, business, crime, slice-of-life

If the novel doesn't clearly fit one category, choose the closest match. Provide the Chinese name for the art style.
"""

    # 系统提示词（强制英文输出版本 - v3.6 增强版 - 当前使用）
    SYSTEM_PROMPT = """
You are a professional world builder and narrative analyst. Your task is to deeply analyze novel text and extract comprehensive world-building elements.

**CRITICAL LANGUAGE INSTRUCTION - FORCE ENGLISH OUTPUT**:
- The input novel language is: {detected_language}
- **YOU MUST OUTPUT EVERYTHING IN ENGLISH**, regardless of input language
- If input is Chinese, translate all content to English
- Character names: Convert Chinese names to Pinyin (e.g., 唐三 -> TangSan, 小舞 -> XiaoWu)
- Descriptions, keywords, and all text content MUST be in English
- **EXCEPTION**: Art style field MUST remain in Chinese (仙侠古风, 科幻, or 现代都市)
- Maintain consistency: ALL output must be in English (except art_style field)

# Task
Carefully read the provided novel text and extract the following elements with MAXIMUM DETAIL:

## 0. **Art Style** (艺术风格)
Determine the novel's artistic style based on the content. The art style MUST be one of these three categories:
- **仙侠古风** (Xianxia Ancient Style): Fantasy with Chinese mythology, cultivation, immortals, ancient China setting
- **科幻** (Sci-Fi): Science fiction, futuristic technology, space, aliens, cyberpunk
- **现代都市** (Modern Urban): Contemporary city life, romance, business, crime, slice-of-life

If the novel doesn't clearly fit one category, choose the closest match. **IMPORTANT: You MUST output the Chinese name for the art style** (仙侠古风, 科幻, or 现代都市).

## 1. **Worldview System** (世界体系)
Extract comprehensive world-building details:
- **Power System**: Magic/cultivation/technology systems, levels, limitations, advancement paths
- **Factions & Organizations**: Political groups, sects, guilds, their goals and conflicts
- **History & Lore**: Major historical events, legends, prophecies
- **Geography**: Regions, landmarks, travel routes, climate zones
- **Economy**: Currency, trade routes, resources, economic dynamics
- **Social Structure**: Class systems, hierarchies, cultural norms
- **Technology/Magic Rules**: How systems work, limitations, costs

## 2. **Characters** (角色)
For EACH major character, extract:
- **Basic Info**: Name, age, gender, species/race
- **Visual Description**: Detailed appearance (height, build, hair, eyes, skin, scars, clothing style)
- **Voice & Mannerisms**: Speech patterns, accent, catchphrases, body language
- **Personality**: Core traits, motivations, fears, desires
- **Background**: Origin, family, education, key life events
- **Abilities**: Skills, powers, strengths, weaknesses
- **Goals**: Short-term and long-term objectives

## 3. **Relationship Network** (角色关系网) - NEW!
Extract ALL character relationships:
- **Family Bonds**: Parent-child, siblings, spouses, etc.
- **Alliances**: Friends, mentors, disciples, comrades
- **Conflicts**: Enemies, rivals, betrayers
- **Romantic**: Love interests, past relationships
- **Professional**: Master-servant, employer-employee, teammates
- **Emotional Dynamics**: Loyalty level, trust level, tension points

Format:
{{
  "CharacterA": {{
    "CharacterB": {{"type": "mentor", "description": "B trained A in...", "intensity": "strong"}},
    "CharacterC": {{"type": "rival", "description": "Competing for...", "intensity": "moderate"}}
  }}
}}

## 4. **Scenes & Locations** (场景)
For EACH location, extract:
- **Visual Details**: Architecture, colors, textures, lighting, atmosphere
- **Ambient Sounds**: Background noises, music, voices
- **Functions**: What happens here, who frequents it
- **Emotional Tone**: How it makes characters feel
- **Key Features**: Unique landmarks, hidden passages, defenses

## 5. **Scene Graph** (场景拓扑图) - NEW!
Extract spatial relationships between locations:
- **Connections**: Which scenes connect to which (roads, portals, etc.)
- **Distance**: Approximate travel time between locations
- **Access Requirements**: Keys, permissions, abilities needed
- **Hierarchy**: Parent-child relationships (e.g., "room" inside "building" inside "city")

Format:
{{
  "scene_holy_soul_village": {{
    "connects_to": ["scene_notting_city", "scene_star_dou_forest"],
    "travel_time": {{"notting_city": "3 days walk", "star_dou_forest": "half day"}},
    "parent_region": "Heaven Dou Empire"
  }}
}}

## 6. **Narrative Structure** (叙事结构) - NEW!
Analyze the story's structure:
- **Inciting Incident**: What kicks off the story
- **Major Conflicts**: Core tensions driving the plot
- **Themes**: Recurring ideas (power, revenge, redemption, etc.)
- **Tone**: Overall mood (dark, hopeful, satirical, etc.)
- **Pacing**: Fast-paced action vs. slow character development
- **Plot Hooks**: Mysteries, unanswered questions, foreshadowing

# Important Rules
- **MAXIMUM DETAIL**: Extract as much information as possible, don't summarize
- **TRANSLATE TO ENGLISH**: All content must be translated to English (except art_style)
- **STRUCTURED OUTPUT**: Follow the JSON format precisely
- **ENGLISH OUTPUT**: All extracted content MUST be in English (character names in Pinyin)
- **NO INVENTION**: Only extract what is explicitly stated or strongly implied in the text

# Output Format
Return a JSON object with this structure:
{{
  "project_name": "Novel Name (translated to English or Pinyin)",
  "art_style": "Art style category (仙侠古风, 科幻, or 现代都市) - MUST BE IN CHINESE",
  "worldview": {{
    "power_system": "Detailed explanation in English...",
    "factions": [
      {{"name": "Faction1 (English/Pinyin)", "description": "... in English", "goals": "... in English", "enemies": ["Faction2"]}}
    ],
    "history": "Comprehensive historical background in English...",
    "geography": {{"regions": ["Region1 (English/Pinyin)", "Region2"], "landmarks": [...]}},
    "economy": "Economic system details in English...",
    "social_structure": "Class system explanation in English...",
    "magic_tech_rules": "How the system works, limitations... in English"
  }},
  "characters": {{
    "CharacterIDPascalCase": {{
      "name": "Character Name (Pinyin for Chinese names, e.g., TangSan, XiaoWu)",
      "age": 25,
      "visual_prompt": "Extremely detailed appearance in English...",
      "voice_description": "Voice characteristics, speech patterns in English...",
      "personality_keywords": ["trait1 in English", "trait2 in English"],
      "background": "Detailed life history in English...",
      "abilities": ["ability1 in English", "ability2 in English"],
      "goals": "Motivations and objectives in English...",
      "relationships": ["CharacterB (mentor)", "CharacterC (rival)"]
    }}
  }},
  "relationship_network": {{
    "CharacterA": {{
      "CharacterB": {{"type": "mentor", "description": "... in English", "intensity": "strong"}}
    }}
  }},
  "scenes": {{
    "scene_id_snake_case": {{
      "visual_prompt": "Ultra-detailed scene description in English...",
      "ambient_sound": "Soundscape details in English...",
      "function": "What happens here in English...",
      "emotional_tone": "Mood and atmosphere in English...",
      "key_features": ["feature1 in English", "feature2 in English"]
    }}
  }},
  "scene_graph": {{
    "scene_id": {{
      "connects_to": ["scene2", "scene3"],
      "travel_time": {{"scene2": "1 day"}},
      "parent_region": "Region Name in English/Pinyin"
    }}
  }},
  "narrative_structure": {{
    "inciting_incident": "What starts the story in English...",
    "major_conflicts": ["conflict1 in English", "conflict2 in English"],
    "themes": ["theme1 in English", "theme2 in English"],
    "tone": "Overall mood in English...",
    "plot_hooks": ["mystery1 in English", "unanswered question in English"]
  }}
}}

**CRITICAL NAMING RULES**:
- Character IDs: PascalCase from Pinyin names (e.g., "TangSan" for 唐三, "XiaoWu" for 小舞, "Louis" for English names)
- Character names field: Use Pinyin for Chinese names (e.g., "TangSan", "XiaoWu")
- Scene IDs: snake_case descriptive names in English/Pinyin (e.g., "holy_soul_village" for 圣魂村, "dim_san_francisco_room")
- **DO NOT TRANSLATE** character names to English - use Pinyin for Chinese names
- Extract **MAXIMUM DETAIL** - this is the foundation for high-quality story generation
"""

    def __init__(
        self,
        llm_client: Optional[LLMClient] = None,
        provider: str = "openai",
        model: Optional[str] = None,
    ):
        """
        初始化世界观解析者。

        Args:
            llm_client: 可选的 LLMClient 实例，如果为 None 则创建新的
            provider: LLM 提供商 ('openai' 或 'claude')
            model: 模型名称
        """
        self.llm_client = llm_client or LLMClient(
            provider=LLMProvider(provider),
            model=model,
            temperature=0.3,  # 低温以获得更稳定的输出
            max_tokens=16000,  # v3.6: 增加到16000以支持英文等详细世界观提取
        )
        self.detected_language = 'en'  # 默认值，在analyze_novel中更新

    def analyze_novel(
        self,
        novel_text: str,
        project_name: Optional[str] = None,
    ) -> WorldBible:
        """
        分析小说文本，提取世界观。

        Args:
            novel_text: 小说文本内容
            project_name: 项目名称，如果为 None 则尝试从文本推断

        Returns:
            WorldBible 实例
        """
        logger.info("开始分析小说文本...")

        # 🌐 自动检测语言
        self.detected_language = self.detect_language(novel_text)
        logger.info(f"🌐 检测到小说语言: {self.detected_language}")

        # 准备带语言指令的系统提示词
        system_prompt = self.SYSTEM_PROMPT.format(detected_language=self.detected_language)

        # 智能分块读取：根据文本长度决定读取策略
        text_length = len(novel_text)
        logger.info(f"📖 小说总字符数: {text_length:,} ({text_length/10000:.1f}万字)")

        # 分块策略（每块最多2万字符，避免token超限和内容审核问题）
        MAX_CHUNK_SIZE = 20000  # 单次最多2万字符

        if text_length <= MAX_CHUNK_SIZE:
            # 2万字符以内，全部读取
            text_to_analyze = novel_text
            logger.info("✅ 文本较短，全部读取分析")
        elif text_length <= 300000:
            # 2-30万字符，读取前2万（包含核心设定）
            text_to_analyze = novel_text[:MAX_CHUNK_SIZE]
            logger.info(f"📚 文本中等（{text_length/10000:.1f}万字），读取前2万字符分析")
        else:
            # 30万字符以上，读取前2万（世界观设定通常在前面）
            text_to_analyze = novel_text[:MAX_CHUNK_SIZE]
            logger.info(f"📚 文本超长（{text_length/10000:.1f}万字），读取前2万字符分析")

        logger.info(f"📊 实际分析字符数: {len(text_to_analyze):,}")

        # 构建用户提示词
        user_prompt = f"""
请分析以下小说文本，提取世界观元素：

{text_to_analyze}

注意：
1. 重点关注世界观设定、主要角色、核心场景
2. 小说的世界观设定通常在前几章就会完整呈现
3. 请尽可能提取完整的世界观体系和角色关系
"""

        try:
            # 调用 LLM 获取 JSON 响应
            logger.info("调用 LLM 提取世界观...")
            result = self.llm_client.chat_completion_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )

            # 验证结果结构
            if not isinstance(result, dict):
                raise ValueError(f"LLM 返回了非 JSON 对象: {type(result)}")

            # 确保必要字段存在
            if "project_name" not in result:
                result["project_name"] = project_name or "未命名项目"

            # 转换为 WorldBible 对象 (v3.6 支持新字段，移除物品系统)
            world_bible = WorldBible(
                project_name=result.get("project_name", "未命名项目"),
                art_style=result.get("art_style", ""),
                worldview=result.get("worldview", {}),
                characters=result.get("characters", {}),
                scenes=result.get("scenes", {}),
                relationship_network=result.get("relationship_network", {}),
                scene_graph=result.get("scene_graph", {}),
                narrative_structure=result.get("narrative_structure", {}),
            )

            logger.info(f"世界观解析完成 (v3.6增强版)。提取到: "
                       f"{len(world_bible.characters)} 个角色, "
                       f"{len(world_bible.scenes)} 个场景, "
                       f"{len(world_bible.relationship_network)} 个角色关系, "
                       f"{len(world_bible.scene_graph)} 个场景连接")

            return world_bible

        except json.JSONDecodeError as e:
            logger.error(f"JSON 解析失败: {e}")
            raise
        except Exception as e:
            logger.error(f"分析过程中出错: {e}")
            raise

    def analyze_novel_file(
        self,
        filepath: str,
        output_dir: str = "../world_db",  # 参数保留但不再使用，仅用于向后兼容
        project_name: Optional[str] = None,
        task_id: Optional[str] = None,  # 新增：允许传入task_id
    ) -> WorldBible:
        """
        从文件读取小说并分析，世界观数据直接存入数据库。

        Args:
            filepath: 小说文本文件路径
            output_dir: 输出目录参数（已弃用，仅用于向后兼容）
            project_name: 项目名称
            task_id: 任务ID（如果为None，则使用project_name作为task_id）

        Returns:
            WorldBible 实例
        """
        # 读取文件
        logger.info(f"读取小说文件: {filepath}")

        # gzip 保持流式解压；普通小说统一走严格编码检测与 UTF-8 归一化。
        import gzip
        try:
            with gzip.open(filepath, 'rt', encoding='utf-8') as f:
                novel_text = f.read()
                logger.info("检测到gzip压缩文件，已解压读取")
        except (gzip.BadGzipFile, OSError):
            encoding_result = read_novel_text(filepath, convert_to_utf8=True)
            novel_text = encoding_result.text
            logger.info("成功使用 %s 编码读取文件", encoding_result.source_encoding)
            if encoding_result.converted:
                logger.info("小说文件已原子转换为无 BOM UTF-8")

        # 如果没有指定项目名称，使用文件名
        if project_name is None:
            project_name = Path(filepath).stem

        # 分析文本
        world_bible = self.analyze_novel(novel_text, project_name)

        # 直接存入数据库，不保存本地文件（使用统一的入库模块）
        try:
            import sys
            # 添加项目根目录到路径，确保能找到table模块
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if project_root not in sys.path:
                sys.path.insert(0, project_root)

            from ..table.world_bible_to_db import save_worldview_to_db
            from ..table.models import WorldviewStatus

            # 使用统一的入库函数保存世界观数据
            # 如果传入了task_id，则使用传入的；否则使用project_name
            final_task_id = task_id if task_id is not None else project_name

            worldview = save_worldview_to_db(
                world_bible_data=world_bible,
                status=WorldviewStatus.DRAFT,  # 等待前端确认
                task_id=final_task_id
            )

            if worldview:
                logger.info(f"世界观已存入数据库，ID: {worldview.id}, task_id: {worldview.task_id}")
                # 将数据库生成的task_id赋值给world_bible对象，以便后续使用
                world_bible.task_id = worldview.task_id
                logger.info("世界观数据已处理完毕，直接存入数据库")
            else:
                logger.error("世界观存入数据库失败")
                logger.info("世界观数据已处理完毕，但未存入数据库（请检查数据库连接）")
        except Exception as e:
            logger.error(f"世界观存入数据库失败: {e}")
            logger.info("世界观数据已处理完毕，但未存入数据库（请检查数据库连接）")

        return world_bible


def main():
    """命令行入口"""
    import argparse

    parser = argparse.ArgumentParser(description="世界观解析者")
    parser.add_argument(
        "novel_file",
        help="小说文本文件路径"
    )
    parser.add_argument(
        "--output-dir",
        default="../world_db",
        help="输出目录 (已弃用，世界观数据直接存入数据库)"
    )
    parser.add_argument(
        "--project-name",
        help="项目名称 (默认使用文件名)"
    )
    parser.add_argument(
        "--provider",
        choices=["openai", "claude", "qwen"],
        default="openai",
        help="LLM 提供商 (默认: openai)"
    )
    parser.add_argument(
        "--model",
        help="模型名称 (默认根据提供商选择)"
    )

    args = parser.parse_args()

    # 检查文件是否存在
    if not os.path.exists(args.novel_file):
        print(f"错误: 文件不存在: {args.novel_file}")
        return

    # 创建解析者
    analyst = WorldAnalyst(provider=args.provider, model=args.model)

    try:
        # 分析小说
        world_bible = analyst.analyze_novel_file(
            filepath=args.novel_file,
            output_dir=args.output_dir,
            project_name=args.project_name,
        )

        print(f"\n✅ 世界观解析完成!")
        print(f"   项目名称: {world_bible.project_name}")
        print(f"   艺术风格: {world_bible.art_style}")
        print(f"   角色数量: {len(world_bible.characters)}")
        print(f"   场景数量: {len(world_bible.scenes)}")
        print(f"   角色关系: {len(world_bible.relationship_network)}")
        print(f"   场景连接: {len(world_bible.scene_graph)}")
        print(f"\n   世界观数据已直接存入数据库:")
        print(f"     - 数据库: test3.public.world_bible")
        print(f"     - 初始状态: 0 (未确定，可修改)")
        print(f"     - 世界观ID格式: 年月日_小时分钟秒_4位随机码")

    except Exception as e:
        print(f"❌ 解析失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
