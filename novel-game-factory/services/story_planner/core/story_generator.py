# -*- coding: utf-8 -*-
"""
无限流作家 (Infinite Writer)
基于世界圣经生成剧情树,编写细节丰富的章节节点.
"""
import os
import json
import logging
import uuid
import re
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, TypedDict, Annotated, Literal
from dataclasses import dataclass, field, asdict
from datetime import datetime

# LangGraph imports
try:
    from langgraph.graph import StateGraph, END
    LANGGRAPH_AVAILABLE = True
except ImportError:
    LANGGRAPH_AVAILABLE = False
    logger = logging.getLogger(__name__)
    logger.warning("LangGraph未安装,将使用传统生成模式.安装: pip install langgraph")

from .llm_client import LLMClient, LLMProvider
from .world_analyst import WorldBible


# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _strip_stage_directions(content: str, node_id: str = "") -> str:
    """
    删除主线正文里泄漏的舞台指令/创作注释（给 AI 的过程说明），
    如“（场景转换：从诀别切换到杀戮，进入暗网/夺权线）”。
    仅删除括号内出现导演/机制词的整块括号，不动正常的括号补充。
    保留软分支标记等其它内容。
    """
    if not content:
        return content

    def _strip(m):
        inner = m.group(1)
        if re.search(
            r"(场景转换|镜头|切换到|进入.{0,8}线|画外|旁白提示|此处|下一(场|章|幕)|"
            r"数值|维度|好感度|变量|玩家|选项分支|伏笔|承接上文|过渡段)",
            inner,
        ):
            return ""
        return m.group(0)

    # 中文括号 + 英文括号各处理一次
    cleaned = re.sub(r"（([^）]*)）", _strip, content)
    cleaned = re.sub(r"\(([^)]*)\)", _strip, cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)

    if cleaned != content:
        removed = len(content) - len(cleaned)
        logger.info(f"[清洗舞台指令] 节点 {node_id} 删除约 {removed} 字")
    return cleaned


@dataclass
class StoryChoice:
    """剧情选择项"""
    choice_text: str                        # 显示给用户的选项文本；结局节点填空字符串
    child_id: str                           # 子节点ID
    branch_variable: Optional[str] = None  # 仅结局节点：触发该结局的变量key
    branch_threshold: Optional[int] = None # 仅结局节点：触发阈值（>= 此值跳转）


@dataclass
class StoryNode:
    """剧情树节点"""
    id: str
    depth: int
    title: str
    parent_id: Optional[str] = None   # 保留兼容旧代码；DAG 场景请用 parent_ids
    parent_ids: List[str] = field(default_factory=list)  # DAG：支持多父节点（收束点）
    content_file: str = ""  # 对应的Markdown文件路径
    children: List[StoryChoice] = field(default_factory=list)
    summary: str = ""  # 节点正文摘要（章节写完后由 LLM 抽取，用于下游 prompt 锚定）
    plan_direction: str = ""  # 规划期 LLM 给出的"走向方向"（区别于内容实摘要）
    terminal_states: Dict[str, Any] = field(default_factory=dict)  # 节点末态：核心角色生死/伤情/位置，供下游章节防穿帮
    legacy_flags: Dict[str, bool] = field(default_factory=dict)  # 结局路线的可验证遗产/证据标记
    legacy_requirements: List[str] = field(default_factory=list)  # 本结局路线要求的遗产类型
    legacy_evidence: List[Dict[str, Any]] = field(default_factory=list)  # 题材无关的结构化证据链
    legacy_review: Dict[str, Any] = field(default_factory=dict)  # 歧义时的一次语义复核结果
    legacy_validation: Dict[str, Any] = field(default_factory=dict)  # 最终采用的 OR 通道及核心链检查
    is_original: bool = True  # 是否沿用原著内容
    director_metadata: Dict[str, Any] = field(default_factory=dict)  # 保留：状态追踪
    generated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        data = asdict(self)
        data["children"] = [asdict(c) for c in self.children]
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'StoryNode':
        """从字典创建"""
        children_data = data.pop("children", [])
        # 兼容旧数据：若无 parent_ids 字段则初始化为空列表
        data.setdefault("parent_ids", [])
        data.setdefault("is_original", True)
        data.setdefault("plan_direction", "")
        data.setdefault("terminal_states", {})
        data.setdefault("legacy_flags", {})
        data.setdefault("legacy_requirements", [])
        data.setdefault("legacy_evidence", [])
        data.setdefault("legacy_review", {})
        data.setdefault("legacy_validation", {})
        node = cls(**data)
        node.children = [StoryChoice(**c) for c in children_data]
        return node


# LangGraph 状态定义 (Graph State)
class StoryGraphState(TypedDict, total=False):
    """
    LangGraph 工作流状态
    用于动态大纲与自修正机制
    """
    # 输入/当前状态
    node_id: str
    depth: int
    parent_node: Optional[StoryNode]
    choice_text: Optional[str]
    carried_items: List[str]
    chapter_objective: str
    rolling_summary: str
    state_snapshot: str

    # 生成的内容
    full_content: str
    full_content_parts: List[str]  # 5个segment的内容
    director_metadata: Dict[str, Any]  # 保留：用于状态追踪和一致性
    choices: List[str]
    summary: str

    # 审查结果
    critic_score: float  # 0.0 - 1.0 (1.0表示完全符合大纲)
    deviation_analysis: str  # 偏离原因分析

    # 循环控制
    retry_count: int
    max_retries: int


@dataclass
class StoryTree:
    """剧情树"""
    root_id: str
    nodes: Dict[str, StoryNode] = field(default_factory=dict)

    def add_node(self, node: StoryNode):
        """添加节点"""
        self.nodes[node.id] = node

    def get_node(self, node_id: str) -> Optional[StoryNode]:
        """获取节点"""
        return self.nodes.get(node_id)

    def get_children(self, node_id: str) -> List[StoryNode]:
        """获取子节点"""
        node = self.get_node(node_id)
        if not node:
            return []
        return [self.get_node(c.child_id) for c in node.children if self.get_node(c.child_id)]

    def save(self, filepath: str):
        """保存到文件"""
        data = {
            "root_id": self.root_id,
            "nodes": {node_id: node.to_dict() for node_id, node in self.nodes.items()}
        }
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"剧情树已保存到: {filepath}")

    @classmethod
    def load(cls, filepath: str) -> 'StoryTree':
        """从文件加载"""
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)

        nodes = {}
        for node_id, node_data in data.get("nodes", {}).items():
            nodes[node_id] = StoryNode.from_dict(node_data)

        return cls(
            root_id=data.get("root_id", ""),
            nodes=nodes
        )

    def prune_invalid_links(self, max_depth: Optional[int] = None) -> None:
        """
        剪枝无效链接:删除指向不存在节点的选项.

        Args:
            max_depth: 最大深度(可选),用于警告非叶子节点无子节点的情况
        """
        logger.info("开始剪枝无效链接...")
        valid_ids = set(self.nodes.keys())
        total_pruned = 0
        nodes_without_children = []

        for node_id, node in self.nodes.items():
            original_count = len(node.children)
            # 过滤掉child_id不在valid_ids中的选项
            node.children = [
                choice for choice in node.children
                if choice.child_id in valid_ids
            ]
            pruned_count = original_count - len(node.children)
            total_pruned += pruned_count

            # 检查剪枝后是否没有子节点
            if len(node.children) == 0:
                nodes_without_children.append((node_id, node.depth))

        logger.info(f"剪枝完成,共移除 {total_pruned} 个无效选项")

        # 可选:打印没有子节点的节点的警告
        if nodes_without_children:
            for node_id, depth in nodes_without_children:
                # 如果提供了max_depth,且当前深度小于最大深度,说明不是正常叶子节点
                if max_depth is not None and depth < max_depth:
                    logger.warning(
                        f"节点 {node_id} (深度 {depth}) 没有子节点,但未达到最大深度 {max_depth}."
                        "这可能导致剧情线提前结束."
                    )
                else:
                    logger.info(
                        f"节点 {node_id} (深度 {depth}) 没有子节点."
                    )


class StoryGenerator:
    """
    无限流作家 Agent

    职责:
    1. 基于世界圣经生成剧情节点
    2. 维护剧情树结构
    3. 生成格式化的Markdown内容
    """

    @staticmethod
    def detect_language(text: str) -> str:
        """
        自动检测文本主要语言

        Returns:
            'zh' (中文), 'en' (英文), 'ja' (日文), 或 'other'
        """
        # 统计各类字符
        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text[:2000]))
        japanese_chars = len(re.findall(r'[\u3040-\u309f\u30a0-\u30ff]', text[:2000]))
        english_words = len(re.findall(r'\b[a-zA-Z]+\b', text[:2000]))

        # 判断主要语言
        if chinese_chars > max(english_words, japanese_chars * 2):
            return 'zh'
        elif japanese_chars > max(chinese_chars, english_words):
            return 'ja'
        elif english_words > 20:  # 降低阈值到20个英文单词
            return 'en'
        else:
            return 'other'

    def _extract_sample_text_from_world_bible(self) -> str:
        """
        从world_bible中提取样本文本用于语言检测

        Returns:
            样本文本字符串
        """
        sample_parts = []

        # 从worldview提取文本
        if self.world_bible.worldview:
            for key, value in self.world_bible.worldview.items():
                if isinstance(value, str):
                    sample_parts.append(value)
                elif isinstance(value, list):
                    sample_parts.extend([str(v) for v in value[:3]])

        # 从characters提取文本
        if self.world_bible.characters:
            for char_id, char_data in list(self.world_bible.characters.items())[:3]:
                if isinstance(char_data, dict):
                    sample_parts.append(char_data.get('name', ''))
                    sample_parts.append(str(char_data.get('personality', '')))

        # 从scenes提取文本
        if self.world_bible.scenes:
            for scene_id, scene_data in list(self.world_bible.scenes.items())[:2]:
                if isinstance(scene_data, dict):
                    sample_parts.append(scene_data.get('visual_prompt', ''))

        # 合并样本文本
        sample_text = ' '.join(sample_parts)

        # 如果样本太少,返回默认值(假设英文)
        if len(sample_text) < 100:
            logger.warning("world_bible样本文本过少,假设语言为英文")
            return "This is a default English sample text for language detection."

        return sample_text

    # 系统提示词(原始多语言版)- 无限流引擎版 - 保留但不使用
    SYSTEM_PROMPT_ORIGINAL = """
You are a professional novelist and asset director, skilled at creating richly detailed scenes and dialogues while generating high-fidelity asset metadata.

**CRITICAL LANGUAGE INSTRUCTION**:
- The input text language is: {detected_language}
- You MUST write the story output in THE SAME LANGUAGE as the input text
- If input is English, output English story
- If input is Chinese, output Chinese story
- If input is Japanese, output Japanese story
- Maintain consistency throughout all generated content

# Task
Based on the provided World Bible, rolling summary, and current state, write the next story scene with detailed asset metadata.

# World Setting Summary
{world_bible_summary}

# Rolling Summary (Previous Events)
{rolling_summary}

# Current State Snapshot
{state_snapshot}

# Writing Rules
1. **Vivid Details**: Describe lighting, textures, sounds, smells, atmosphere, muscle tension, material reflections, etc.
2. **Context Consistency**: Strictly follow the rolling summary and current state
3. **Format Requirements**:
   - Start new scenes with: ## Scene: [Scene Name]
   - Use character names directly without special markers
   - Use quotation marks for dialogue
   - Scene descriptions should be extremely detailed for AI image generation
   - NO markdown bold markers like **[]**

4. **Content Requirements**:
   - Each scene should be 800-1500 words (in the target language)
   - Rich dialogue, action descriptions, psychological activities
   - At least 2-3 character interactions
   - Clear environmental descriptions and camera language
   - Leave foreshadowing for future developments

5. **Item References**: If characters carry these items, explicitly show them in the narrative:
{carried_items}

# Output Format
You must output in this exact order:

## Story Content
## Scene: [Scene Name in target language]

[Extremely detailed environment description with lighting, sound, atmosphere, material details...]

[Character A] walks to the window, looking at the rain outside.
"It's raining heavily," they say.

[Character B] remains silent for a moment.
"Yes, we should go."

[Story development...]

## Then provide 2-3 options for future plot development (1-2 sentences each in target language):
1. [Option one description]
2. [Option two description]
3. [Option three description]

**IMPORTANT RULES**:
1. Start new scenes with: ## Scene: [Scene Name]
2. Use quotation marks for dialogue
3. NO markdown bold markers like **[]** in story text
"""

    # 系统提示词(强制英文输出版)- 无限流引擎版 - 当前使用
    SYSTEM_PROMPT = """
You are a professional novelist and asset director, skilled at creating richly detailed scenes and dialogues while generating high-fidelity asset metadata.

**CRITICAL LANGUAGE INSTRUCTION - FORCE ENGLISH OUTPUT**:
- The input text language is: {detected_language}
- **YOU MUST WRITE ALL STORY CONTENT IN ENGLISH**, regardless of input language
- If input is Chinese, write the story in English
- Character names: Use Pinyin for Chinese names (e.g., TangSan, XiaoWu)
- All descriptions, dialogues, and narrative MUST be in English
- Maintain consistency: ALL story output must be in English

# Task
Based on the provided World Bible, rolling summary, and current state, write the next story scene with detailed asset metadata.

# World Setting Summary
{world_bible_summary}

# Rolling Summary (Previous Events)
{rolling_summary}

# Current State Snapshot
{state_snapshot}

# Writing Rules
1. **Vivid Details**: Describe lighting, textures, sounds, smells, atmosphere, muscle tension, material reflections, etc.
2. **Context Consistency**: Strictly follow the rolling summary and current state
3. **Format Requirements**:
   - Start new scenes with: ## Scene: [Scene Name]
   - Use character names directly without special markers
   - Use quotation marks for dialogue
   - Scene descriptions should be extremely detailed for AI image generation
   - NO markdown bold markers like **[]**

4. **Content Requirements**:
   - Each scene should be 800-1500 words in English
   - Rich dialogue, action descriptions, psychological activities
   - At least 2-3 character interactions
   - Clear environmental descriptions and camera language
   - Leave foreshadowing for future developments

5. **Item References**: If characters carry these items, explicitly show them in the narrative:
{carried_items}

# Output Format
You must output in this exact order:

## Story Content
## Scene: [Scene Name in English]

[Extremely detailed environment description with lighting, sound, atmosphere, material details... ALL IN ENGLISH]

[Character A] walks to the window, looking at the rain outside.
"It's raining heavily," they say.

[Character B] remains silent for a moment.
"Yes, we should go."

[Story development... ALL IN ENGLISH]

## Then provide 2-3 options for future plot development (1-2 sentences each in English):
1. [Option one description in English]
2. [Option two description in English]
3. [Option three description in English]

**IMPORTANT RULES**:
1. Start new scenes with: ## Scene: [Scene Name in English]
2. Use quotation marks for dialogue
3. NO markdown bold markers like **[]** in story text
4. ALL CONTENT MUST BE IN ENGLISH
"""

    # 分段生成提示词模板(原始中文版 - 保留但不使用)
    SEGMENT_PROMPT_ORIGINAL = """
你正在写一部超长篇小说的一章.本章将分为5个部分(Segment 1-5),总计约15,000-20,000字.
当前进度是 [ 第 {current_segment}/5 部分 ].

# 本章核心目标 (Chapter Objective)
{chapter_objective}

**重要**: 你的写作必须围绕上述目标展开,确保剧情朝着这个方向发展.不要偏离核心目标.
"""

    # 分段生成提示词模板(强制英文输出版 - 当前使用)
    SEGMENT_PROMPT = """
You are writing a chapter of an epic novel. This chapter will be divided into 5 parts (Segment 1-5), totaling approximately 15,000-20,000 words in English.
Current progress: [ Part {current_segment}/5 ].

# Chapter Core Objective
{chapter_objective}

**CRITICAL**: Your writing must revolve around the above objective, ensuring the plot develops in this direction. Do not deviate from the core objective.

**LANGUAGE REQUIREMENT**: ALL content must be written in English, regardless of the input language. Use Pinyin for Chinese character names.

{first_chapter_hook}

# 上下文信息
## 世界设定摘要
{world_bible_summary}

## 前情摘要 (Rolling Summary)
{rolling_summary}

## 当前状态 (State Snapshot)
{state_snapshot}

## 本章前文摘要
{chapter_context}

## 上一部分结尾 (最后1000字)
{previous_segment_end}

## 携带物品
{carried_items}

# **剧情一致性强制检查 (Coherence Check)**:
**在开始写作前,请务必执行以下检查**:
1. **回顾前情摘要**:列出最近3个关键事件(人物做了什么、获得了什么、说了什么)
2. **承接检查**:本章开头必须明确回应上述事件的后果
   - 如果上章有人受伤,本章必须描写伤痛或治疗
   - 如果上章获得物品,本章必须尝试使用或提及
   - 如果上章有人物承诺,本章必须兑现或解释
3. **目标对齐**:检查你的写作是否朝着"本章核心目标"推进
4. **禁止突发事件**:不要创造与前文无因果关系的突发剧情

# **章节边界强制约束 (防止剧情压缩 - 最高优先级!)**:
**你正在写的是本章的Segment {current_segment}/5,不是整部小说!**

1. **本章核心目标解析**:
   - 仔细阅读上面的"本章核心目标"
   - 识别其中的**动作边界**:如果目标说"面临选择",就不要写"做出行动"
   - 识别**场景边界**:如果目标提到"龙门客栈",就不要写"到达静寂区"

2. **禁止跨章节内容**:
   - 如果本章目标是"接受委托,决定是否前往静寂区",不要写"前往静寂区并遭遇战斗"
   - 如果本章目标是"遭遇苏琉璃,选择信任或背叛",不要写"已经进入古庙核心"
   - 只写本章核心目标中**明确提到的事件**,停在**选择点**,不要继续

3. **Segment进度边界**:
   - Segment 1-2: 场景建立 + 冲突引入(只写目标的前半部分)
   - Segment 3-4: 冲突发展 + 准备选择(写到面临选择的时刻,但不要解决)
   - Segment 5: 总结本章 + 给出选项(停在这里,不要写下一章的行动!)

4. **强制检查清单**(每个Segment写完后自查):
   - 我是否只写了本章目标范围内的事件?
   - 我是否跳跃到了后续章节的场景?
   - 如果本章目标是"决定",我是否只写到决定点而非行动后果?
   - 我写的内容是否会让下一章无事可写?

**示例对比**:

场景:本章目标是"在龙门客栈接受委托,发现晶片线索,面临是否前往静寂区的选择"

**错误写法**(Segment 3):
"李长夜决定接受委托.第二天清晨,他穿越电子荒原,抵达静寂区外围..."
→ 问题:已经跳到下一章的内容!

**正确写法**(Segment 3):
"李长夜凝视晶片上的坐标,心中权衡利弊.静寂区凶险异常,但师父的数字签名..."
→ 正确:只写内心挣扎,停在决策点

# **上下文一致性检查表(P0-5强化)**:
**在开始写作前,请务必回顾以下内容确保一致性**:
1. **角色性格一致性**:角色的言行举止是否与前文描写的性格特征一致?是否出现性格突变?
2. **场景描写连贯性**:场景的环境、天气、时间是否与之前矛盾?是否突然从白天变成黑夜?
3. **时间线合理性**:事件发生的时间顺序是否合理?是否出现时间倒流或跳跃?
4. **物品状态连贯性**:物品的状态是否连贯?是否出现物品突然消失或重复出现?
5. **人物关系一致性**:角色之间的关系是否与前文一致?是否出现陌生人突然变熟人?
6. **能力体系一致性**:角色的能力和魂力等级是否与设定一致?是否突然变强或变弱?

**如果发现不一致,请停下来重新思考,确保剧情自然流畅.**

# 分段写作要求

## Segment 1-4(非结局部分)要求:
1. **纯正文输出**:只输出2000-2500字左右的纯正文,不要输出JSON元数据,不要输出选项
2. **严禁结束章节**:不要在本部分结束本章,剧情必须继续发展
3. **节奏控制**:
   - Segment 1(开篇):**根据玩家选择创造独特开场**,不要重复使用通用场景描写
     * 如果选择"激进路线",开场应展现紧张对峙
     * 如果选择"隐忍路线",开场应展现低调潜伏
   - Segment 2(发展):铺垫剧情,增加细节和人物互动,丰富对话和动作描写
   - Segment 3(转折):剧情爆发,矛盾激化,制造紧张感,详细描写冲突过程
   - Segment 4(高潮):核心冲突的解决或高潮描写,展现细节和情感
4. **细节描写**:生动地描写光影、材质、声音、气味、氛围、肌肉纹理、材质反射等感官细节
5. **上下文一致性**:严格遵循前文设定,保持角色性格和行为一致
6. **格式规范**:
   - 角色名直接使用,禁止使用**[]**等markdown加粗标记
   - 对话用引号标记
   - 详细描写每个动作和表情
7. **视觉小说质量要求**:
   - 每个句子必须完整,以标点符号结尾
   - 不要跳过短对话(如"好.""走!")
   - 避免重复前文内容,每个分支应有独特叙事
8. **避免重复描写 (P0级强制要求)**:
   - **严禁重复使用相同的意象或比喻**:每个segment必须使用全新的描写角度
   - **禁止重复的场景描写**:
     * 如果前文已用"雨水冲刷霓虹残影",本段改用"电弧在积水中折射"、"全息广告在雾气中扭曲"等全新表达
     * 如果前文已用"油污街角",本段改用"锈蚀管道交汇处"、"废弃冷却塔下"等不同地标
   - **环境描写多样化**:每个segment至少引入2-3个新的环境元素(新的建筑、新的光源、新的天气变化)
   - **感官描写轮换**:不要连续3次都用视觉描写,轮流使用听觉、嗅觉、触觉、味觉
   - **CRITICAL: 避免章节开头重复**:
     * **严禁每章开头都描写"酸雨"** - 如果前一章已用酸雨开场,本章必须用完全不同的开场(如晨雾、电子尘暴、寂静、突发事件等)
     * **严禁重复使用"新洛阳城的酸雨从黄昏就开始下了"等相同句式**
     * **每章开头必须独特** - 根据剧情节点选择合适的开场方式(动作开场、对话开场、环境开场、回忆开场等)
   - **检查清单**(写完每段后自查):
     * 我是否使用了与前文完全不同的比喻?
     * 我是否引入了新的场景细节?
     * 我是否避免了"雨水"、"霓虹"、"油污"、"酸雨"等高频词的重复出现?
     * 本章开头是否与前一章完全不同?

9. **🔴 单章内严禁剧情重复 (P0级最高优先级 - CRITICAL FOR PREVENTING REDUNDANCY) 🔴**:

   **⚠️ MOST IMPORTANT RULE: Each segment MUST advance NEW plot points, NOT repeat previous segments!**

   - **每个segment必须推进全新的剧情**:
     * ❌ **严禁重复前面segment已经发生的事件**
     * ❌ **严禁重复前面segment已经说过的对话**
     * ❌ **严禁重复前面segment已经描写过的场景**
     * ✅ **必须推进时间线**:每个segment应该发生在不同的时间点或场景
     * ✅ **必须引入新信息**:每个segment应该揭示新的情节、新的角色动作、新的冲突

   - **Segment连贯性强制要求**:
     * **Segment 1**: 开场+引入核心冲突(WHO/WHERE/WHAT) - 建立场景,介绍主要角色,提出本章核心问题
     * **Segment 2**: 冲突发展+新信息揭示(WHY/HOW) - 深入探讨冲突原因,角色开始行动,揭示关键信息
     * **Segment 3**: 转折点+意外事件(TWIST) - 情况发生意外变化,出现新的障碍或机会,剧情转向
     * **Segment 4**: 高潮+核心冲突爆发(CLIMAX) - 矛盾激化到顶点,角色做出关键决策,展现情感冲击
     * **Segment 5**: 结局+后续选择(RESOLUTION) - 解决本章冲突,收束剧情,提供后续分支选项

   - **严禁的重复模式**(必须避免):
     * ❌ 每个segment都重新介绍角色身份
     * ❌ 每个segment都重新描述同一个场景(如"楚家豪宅破败不堪")
     * ❌ 每个segment都重复相同的对话内容(如"你为什么要帮我?")
     * ❌ 每个segment都重复相同的情节(如"厉寒霆阻止楚瑶吞药")
     * ❌ 每个segment都重复相同的冲突(如"白冰和王德发来催债")

   - **正确的推进方式**(必须遵循):
     * ✅ Segment 1建立场景 → Segment 2角色对话深入 → Segment 3出现新角色/新事件 → Segment 4冲突爆发 → Segment 5解决并分支
     * ✅ 每个segment引入新的场景元素(新房间、新地点、新时间)
     * ✅ 每个segment推进剧情时间线(从早晨→中午→下午→傍晚→夜晚)
     * ✅ 每个segment揭示新的信息(新的秘密、新的线索、新的矛盾)

   - **写作前强制自查清单**(MANDATORY CHECKLIST BEFORE WRITING):
     * ❓ 我是否在重复前面segment的剧情?(如果是,立即停止,改写新剧情)
     * ❓ 我是否在重复前面segment的对话?(如果是,立即停止,写新对话)
     * ❓ 我是否在重复前面segment的场景描写?(如果是,立即停止,转移场景或时间)
     * ❓ 本segment是否推进了新的剧情发展?(如果没有,立即停止,添加新事件)
     * ❓ 本segment是否引入了新的信息或冲突?(如果没有,立即停止,添加新元素)

   - **示例对比**:

     **❌ 错误示例(重复剧情)**:
     ```
     Segment 1: 厉寒霆醒来,发现自己重生了,阻止楚瑶吞药
     Segment 2: 厉寒霆再次醒来,发现自己重生了,再次阻止楚瑶吞药  ← 重复!
     Segment 3: 厉寒霆又一次醒来,又一次阻止楚瑶吞药  ← 重复!
     ```

     **✅ 正确示例(推进剧情)**:
     ```
     Segment 1: 厉寒霆醒来,阻止楚瑶吞药,揭示重生真相
     Segment 2: 白冰和王德发来催债,厉寒霆用未来信息震慑他们
     Segment 3: 秦洛冰派车来接,厉寒霆和楚瑶前往新发地产
     Segment 4: 在新发地产总部,厉寒霆展示预知能力,与秦洛冰谈判
     Segment 5: 谈判成功,获得合作机会,面临新的选择分支
     ```

10. **对话密度强制要求 (P0级)**:
11. **对话密度强制要求 (P0级)**:
   - **每个segment必须包含至少3段有意义的对话交流**(不包括独白)
   - **对话占比**:对话内容应占本段总字数的25%-40%
   - **对话质量标准**:
     * 避免纯功能性对话(如"我们走吧""好的"),每句对话必须推动剧情或揭示角色性格
     * 每段对话后必须有角色的微表情、肢体语言或心理活动描写
     * 优先使用"对话-动作-对话"交替结构,避免连续5句纯对话
   - **语言交锋**:当两个角色对话时,必须体现立场差异、信息不对等或情感冲突
     * 正确示例:"你为什么要帮我?"李长夜眯起眼,握紧了腰间的刀柄."我没有帮你."苏琉璃冷笑,"我只是在阻止你犯下和我一样的错误."
     * 错误示例:"你好.""你好.""我们走吧.""好的."
   - **对话节奏**:
     * Segment 1-2: 至少包含1次深度对话(10轮以上对话往返)
     * Segment 3-4: 至少包含1次激烈争论或质问(角色情绪爆发)
   - **检查清单**(写完每段后自查):
     * 我是否写了至少3段对话?
     * 对话是否推动了剧情或揭示了角色?
     * 是否避免了功能性废话?
     * 角色是否有语言交锋?

## Segment 5(结局部分)要求:
1. **收束本章**:给本章一个完整的结局,解决主要冲突
2. **菱形收束策略**:
   {convergence_instruction}
3. **输出选项**:
   {options_instruction}

# 写作格式规范
1. 使用 ## Scene: [场景名称] 开始一个新场景
2. 角色名直接使用,禁止使用**[]**等markdown加粗标记
3. 对话使用引号标记
4. 场景描写要极其详细,包含环境、光线、声音、气味等感官细节
5. 角色对话要自然,符合人物性格
6. 心理活动用斜体或括号标注

# 输出格式(根据当前Segment不同)

## Segment 1-4 输出格式:
[3000-4000字左右的纯正文,专注于环境描写、心理活动、对话和详细叙事]

## Segment 5 输出格式:
[本章结局的正文内容,约3000-4000字]

### 后续选项
{options_output}
"""

    def __init__(
        self,
        world_bible: WorldBible,
        llm_client: Optional[LLMClient] = None,
        provider: str = "openai",
        model: Optional[str] = None,
        story_dir: str = "../story_tree",
        max_depth: Optional[int] = None,
        save_to_db: bool = True,
        task_id: Optional[str] = None,
    ):
        """
        初始化故事生成器.

        Args:
            world_bible: 世界圣经
            llm_client: 可选的 LLMClient 实例
            provider: LLM 提供商
            model: 模型名称
            story_dir: 故事文件存储目录
            max_depth: 最大深度,当节点深度达到此值时强制进入大结局模式(可选)
            save_to_db: 是否将大纲保存到数据库 (默认 True)
            task_id: 关联的task_id (用于数据库关联)
        """
        self.world_bible = world_bible
        self.llm_client = llm_client or LLMClient(
            provider=LLMProvider(provider),
            model=model,
            temperature=0.8,  # 较高温度以获得更多创造性
            max_tokens=10000,  # 增加到10000以支持更长的详细内容
        )
        self.story_dir = Path(story_dir)
        self.story_dir.mkdir(exist_ok=True)
        self.max_depth = max_depth
        self.save_to_db = save_to_db
        self.task_id = task_id

        # 自动检测语言(从world_bible或第一次生成时检测)
        sample_text = self._extract_sample_text_from_world_bible()
        self.detected_language = self.detect_language(sample_text)
        logger.info(f"检测到输入语言: {self.detected_language}")

        # 剧情树
        self.tree: Optional[StoryTree] = None

        # 无限流引擎状态
        self.rolling_summary: str = "故事开始." if self.detected_language == 'zh' else "Story begins."
        self.state_snapshot: Dict[str, Any] = {
            "time": "未知" if self.detected_language == 'zh' else "Unknown",
            "location": "未知" if self.detected_language == 'zh' else "Unknown",
            "character_states": {},
            "foreshadowing": []
        }
        self.node_counter: int = 0
        self.rolling_window_size: int = 5  # 每5个节点触发一次摘要更新
        self.recent_nodes: List[Tuple[str, str]] = []  # (node_id, summary) 用于摘要生成

        # 全局大纲锚点系统 (Global Outline Anchors)
        self.global_outline: Dict[int, str] = {}  # {depth: objective} 每个章节的目标
        self.outline_generated: bool = False  # 标记大纲是否已生成

        # LangGraph 工作流系统
        self.app = None  # LangGraph workflow app
        if LANGGRAPH_AVAILABLE:
            self._build_langgraph_workflow()
        else:
            logger.warning("LangGraph未安装,将使用传统生成模式(无自修正机制)")

    def _build_langgraph_workflow(self):
        """
        构建 LangGraph 工作流

        流程:
        1. Writer 生成内容
        2. Critic 审查内容
        3. 根据审查结果:
           - 如果通过(score >= 0.7): 结束
           - 如果未通过且重试次数 < 2: 返回Writer重写
           - 如果重试次数 >= 2: 进入Replanner调整大纲并接受
        """
        if not LANGGRAPH_AVAILABLE:
            return

        logger.info("构建 LangGraph 工作流...")

        # 创建状态图
        workflow = StateGraph(StoryGraphState)

        # 添加节点
        workflow.add_node("writer", self._node_writer)
        workflow.add_node("critic", self._node_critic)
        workflow.add_node("replanner", self._node_replanner)

        # 设置入口点
        workflow.set_entry_point("writer")

        # 定义路由函数
        def router(state: StoryGraphState) -> str:
            """
            根据Critic审查结果决定下一步

            Returns:
                - "writer": 重写(retry_count < 2 且 score < 0.7)
                - "replanner": 重规划大纲并接受(retry_count >= 2)
                - "END": 通过审查(score >= 0.7)
            """
            # 如果重试超过2次,强制进入重规划
            if state['retry_count'] >= 2:
                logger.info(f"  路由决策: 重试次数达到{state['retry_count']},进入 Replanner")
                return "replanner"

            # 如果分数及格,结束
            if state['critic_score'] >= 0.7:
                logger.info(f"  路由决策: 得分{state['critic_score']:.2f}及格,结束生成")
                return "END"

            # 否则,重写
            logger.info(f"  路由决策: 得分{state['critic_score']:.2f}不及格,返回 Writer 重写")
            # 注意: retry_count的增量由Critic节点负责,不在router中修改
            return "writer"

        # 添加条件边 (从Critic出发)
        workflow.add_conditional_edges(
            "critic",
            router,
            {
                "writer": "writer",
                "replanner": "replanner",
                "END": END
            }
        )

        # 添加固定边
        workflow.add_edge("writer", "critic")  # Writer -> Critic
        workflow.add_edge("replanner", END)    # Replanner -> END

        # 编译工作流
        self.app = workflow.compile()

        logger.info("LangGraph 工作流构建完成")

    def _prepare_world_summary(self) -> str:
        """准备世界设定摘要 (v3.6 增强版 - 支持复杂结构)"""
        summary_parts = []

        def format_value(value, indent=0):
            """递归格式化复杂数据结构"""
            prefix = "  " * indent
            if isinstance(value, dict):
                lines = []
                for k, v in list(value.items())[:5]:  # 限制显示数量
                    if isinstance(v, (dict, list)):
                        lines.append(f"{prefix}- {k}:")
                        lines.append(format_value(v, indent + 1))
                    else:
                        lines.append(f"{prefix}- {k}: {str(v)[:100]}")
                return "\n".join(lines)
            elif isinstance(value, list):
                if not value:
                    return f"{prefix}(空)"
                # 如果列表元素是字符串，直接join
                if all(isinstance(item, str) for item in value):
                    return f"{prefix}{', '.join(value[:5])}"
                # 如果列表元素是字典，逐个格式化
                lines = []
                for item in value[:3]:  # 限制显示数量
                    if isinstance(item, dict):
                        lines.append(format_value(item, indent))
                    else:
                        lines.append(f"{prefix}- {str(item)[:100]}")
                return "\n".join(lines)
            else:
                return f"{prefix}{str(value)[:200]}"

        # 世界观
        if self.world_bible.worldview:
            summary_parts.append("## 世界观")
            for key, value in list(self.world_bible.worldview.items())[:10]:  # 限制数量
                summary_parts.append(f"### {key}")
                summary_parts.append(format_value(value, 0))

        # 角色
        if self.world_bible.characters:
            summary_parts.append("\n## 主要角色")
            for char_id, char_data in list(self.world_bible.characters.items())[:5]:  # 限制数量
                name = char_data.get("name", char_id) if isinstance(char_data, dict) else char_id
                if isinstance(char_data, dict):
                    personality = char_data.get("personality_keywords", [])
                    if isinstance(personality, list):
                        personality_str = ', '.join(personality) if personality else "未知"
                    else:
                        personality_str = str(personality)
                    background = char_data.get("background", "")
                    if background:
                        summary_parts.append(f"- {name} ({char_id}): {personality_str} | {str(background)[:100]}")
                    else:
                        summary_parts.append(f"- {name} ({char_id}): {personality_str}")
                else:
                    summary_parts.append(f"- {name} ({char_id})")

        # 角色关系网 (v3.6 新增)
        if self.world_bible.relationship_network:
            summary_parts.append("\n## 角色关系网")
            for char_id, relationships in list(self.world_bible.relationship_network.items())[:5]:
                if isinstance(relationships, dict):
                    rel_list = []
                    for target, rel_data in list(relationships.items())[:3]:
                        if isinstance(rel_data, dict):
                            rel_type = rel_data.get("type", "未知")
                            rel_list.append(f"{target}({rel_type})")
                        else:
                            rel_list.append(f"{target}")
                    if rel_list:
                        summary_parts.append(f"- {char_id}: {', '.join(rel_list)}")

        # 场景
        if self.world_bible.scenes:
            summary_parts.append("\n## 主要场景")
            for scene_id, scene_data in list(self.world_bible.scenes.items())[:3]:
                if isinstance(scene_data, dict):
                    visual = scene_data.get('visual_prompt', '')
                    summary_parts.append(f"- {scene_id}: {str(visual)[:100]}...")
                else:
                    summary_parts.append(f"- {scene_id}")

        # 场景拓扑 (v3.6 新增)
        if self.world_bible.scene_graph:
            summary_parts.append("\n## 场景连接")
            for scene_id, connections in list(self.world_bible.scene_graph.items())[:5]:
                if isinstance(connections, dict):
                    connects = connections.get("connects_to", [])
                    if isinstance(connects, list) and connects:
                        summary_parts.append(f"- {scene_id} -> {', '.join(connects[:3])}")

        # 叙事结构 (v3.6 新增)
        if self.world_bible.narrative_structure:
            summary_parts.append("\n## 叙事结构")
            for key, value in list(self.world_bible.narrative_structure.items())[:5]:
                if isinstance(value, list):
                    summary_parts.append(f"- {key}: {', '.join([str(v)[:50] for v in value[:3]])}")
                else:
                    summary_parts.append(f"- {key}: {str(value)[:150]}")

        return "\n".join(summary_parts)

    def _generate_global_outline(self) -> Dict[int, str]:
        """
        生成全局大纲锚点

        根据世界观和预设深度,为每个章节生成目标性描述.
        这是解决"剧情分裂"的核心方法:给每个节点明确的"航向".

        Returns:
            {depth: objective} 字典,例如:
            {
                1: "主角觉醒,发现自己的独特能力",
                2: "初次冲突,与村中恶霸产生矛盾",
                3: "离开家乡,踏上修炼之路"
            }
        """
        if self.outline_generated and self.global_outline:
            return self.global_outline

        logger.info("开始生成全局剧情大纲...")

        # 准备世界设定摘要
        world_summary = self._prepare_world_summary()

        # 根据语言选择提示词 - 强制使用英文版本（保留中文版本但不使用）
        # 原中文版本保留在下方注释中
        if False:  # 不再使用中文版本
            system_prompt = """你是一位资深互动小说编剧,擅长规划多分支剧情结构和角色深度发展.

# 任务要求 (v3.6 优化版 - 8层固定深度)
基于世界观设定,生成一个**8章精确控制的多分支剧情大纲**,支持玩家选择影响剧情走向.

## 8层结构说明 (强制遵守)
1. **第1-2章**: 发散节点,提供2个重大选择
2. **第3章**: 收束节点,不同分支汇聚到关键事件
3. **第4-5章**: 发散节点,提供2个重大选择
4. **第6章**: 收束节点,再次汇聚
5. **第7章**: 关键分流节点,提供3个选择分别通向3种结局(团圆/悲剧/开放)
6. **第8章**: 最终结局章节,根据第7章选择呈现对应结局,无后续选项

## 核心原则
1. **精确的分支控制**: 严格按照上述8层结构设计
2. **深度的角色发展**: 每章推进主角成长,揭示角色背景,建立人物关系
3. **多层次的冲突**: 包含个人冲突、势力冲突、价值观冲突、世界观冲突
4. **密集的剧情元素**: 每章至少包含3个核心事件/转折点
5. **伏笔与回收**: 前期埋设伏笔,后期逐步揭示真相
6. **3种结局设计**: 第7章必须明确铺垫3种不同情感基调的结局

## 大纲结构
为8个章节设计超详细目标,每章描述**5-8句话**,格式如下:

**章节类型标记**:
- **发散节点** (第1,2,4,5章): 提供2个重大选择,影响后续剧情走向
  - 选择应涉及:道德两难、策略选择、势力站队、角色关系等
  - 示例: "选择帮助A派系 vs 暗中调查B派系真相"

- **收束节点** (第3,6章): 不同分支汇聚到关键剧情点
  - 无论之前选择如何,都会经历的核心事件
  - 但角色状态、势力关系、获得物品会有差异

- **结局分流节点** (第7章): 提供3个选择分别通向不同结局类型
  - 选项1: 团圆结局路线 - 积极解决,和解共赢
  - 选项2: 悲剧结局路线 - 悲壮牺牲,付出代价
  - 选项3: 开放结局路线 - 离开远行,留有悬念

- **最终结局** (第8章): 根据第7章选择呈现对应结局,回收所有伏笔

## 超详细章节目标模板 (每章5-8句话)
格式:
```
[核心事件描述,2句话] + [角色发展与互动,2句话] + [关键选择/冲突,2句话] + [伏笔埋设/揭示,1-2句话] + [本章结束状态,1句话]
```

示例 (优秀的超详细大纲):
```
第1章: "李长夜接到神秘委托,在龙门客栈收到一块加密晶片,坐标指向静寂区废墟.同时察觉天道公司密探铁屠在暗中监视,怀疑这是陷阱.晶片激活时显示一段警告:'归墟未死,勿信天道',署名是已故师父的数字签名.李长夜陷入矛盾:师父生前反复警告远离静寂区,但这个签名无法伪造.客栈老板娘苏婆婆透露,最近有三批雇佣兵接了类似任务后失踪,劝他放弃.本章埋下伏笔:晶片背后的真实委托人身份,铁屠监视的真实目的,苏婆婆与师父的隐秘关系.本章结束时,李长夜面临选择:(A)接受委托,冒险前往静寂区寻找真相 vs (B)拒绝委托,转而调查铁屠和天道公司的阴谋."
```

**避免的错误示例** (太简略):
```
"主角继续探索,遇到了一些困难,最后做出了选择." ❌ 太模糊,缺乏细节
"主角战斗,获得胜利." ❌ 没有剧情深度
```

# 世界观设定
{world_summary}

# 大纲设计要点 (v3.6 增强)
1. **深度挖掘世界观元素**: 充分利用世界观中的势力、物品、场景、人物关系、角色关系网、场景拓扑
2. **制造冲突层次**:
   - 外部冲突: 主角 vs 天道公司/其他势力
   - 内部冲突: 价值观抉择,信任与背叛
   - 系统冲突: 旧世界 vs 新秩序,人性 vs 机械
3. **角色弧光** (8章完整弧光):
   - 第1-2章: 建立角色初始状态和动机,首次冲突
   - 第3章: 第一次重大挑战,角色信念受到考验
   - 第4-5章: 角色成长,获得新能力/盟友/信息
   - 第6章: 第二次重大挑战,揭示更深层真相
   - 第7章: 终极选择,角色面临人生转折
   - 第8章: 角色完成转变或坚守初心,达成结局
4. **物品与能力**: 每章可安排获得新物品/技能/信息,影响后续剧情
5. **NPC深度**: 重要NPC要有立场、目标、秘密,不只是功能性角色
6. **利用关系网**: 参考world_bible中的relationship_network,设计角色互动
7. **利用场景图**: 参考scene_graph设计场景转换和探索路线

# 第7章结局分流设计 (重点!)
第7章必须设计3个明确的选择,分别通向:
1. **团圆结局**(第8章-路线1): 主角选择与盟友合作,通过谈判/牺牲自我利益/宽恕敌人等方式化解冲突,最终达成和解,多方共赢.情感基调:温暖、希望、圆满
2. **悲剧结局**(第8章-路线2): 主角选择决裂/复仇/孤注一掷,虽然可能达成部分目标但付出惨重代价(重要角色牺牲、自身陷入绝境、世界陷入混乱).情感基调:悲壮、沉重、反思
3. **开放结局**(第8章-路线3): 主角选择离开/放弃/走向未知,核心冲突未完全解决,留下悬念和思考空间.情感基调:迷茫、未竟、哲学思考

示例(第7章):
```
"李长夜终于抵达归墟服务器核心,发现师父并未死亡而是数字化存在,天道公司CEO竟是师父的克隆体.师父揭示真相:旧世界毁灭是为了避免人类自我毁灭,但代价是剥夺了自由意志.李长夜必须在三条路中选择:(A)选择信任师父,帮助完成世界重启,牺牲自我换取人类新生(团圆路线) vs (B)选择摧毁归墟,即使这会导致世界崩溃也要捍卫自由意志(悲剧路线) vs (C)选择离开,拒绝做出选择,带着真相远走他乡(开放路线)."
```

# 输出格式
严格按照以下JSON格式输出(不要有额外文字):
```json
{{
  "1": "第1章超详细大纲(5-8句话,包含: 核心事件+角色发展+关键选择+伏笔+结束状态)",
  "2": "第2章超详细大纲(5-8句话)",
  "3": "第3章超详细大纲(5-8句话,收束节点,汇聚分支)",
  "4": "第4章超详细大纲(5-8句话)",
  "5": "第5章超详细大纲(5-8句话)",
  "6": "第6章超详细大纲(5-8句话,收束节点,汇聚分支)",
  "7": "第7章超详细大纲(6-9句话,结局分流节点,必须包含3个明确选择,分别指向团圆/悲剧/开放结局)",
  "8-ending-happy": "第8章团圆结局(10-15句话,对应第7章选择A,必须包含: 开场冲击+核心事件展开+角色命运+伏笔回收+情感升华+结尾画面,温暖圆满)",
  "8-ending-tragic": "第8章悲剧结局(10-15句话,对应第7章选择B,必须包含: 开场冲击+核心事件展开+角色命运+伏笔回收+情感升华+结尾画面,悲壮沉重)",
  "8-ending-open": "第8章开放结局(10-15句话,对应第7章选择C,必须包含: 开场冲击+核心事件展开+角色命运+伏笔回收+情感升华+结尾画面,留有悬念)"
}}
```

**强制要求**:
1. 每章描述至少5句话,第7章至少6句话
2. **第8章的3个结局各至少10-15句话** (这是最重要的章节,必须写得足够详细和震撼!)
3. 发散节点必须明确标注2个选择方向 (用"(A)...vs(B)..."格式)
4. 第7章必须标注3个选择方向 (用"(A)...(团圆路线) vs (B)...(悲剧路线) vs (C)...(开放路线)")
5. 第8章必须生成3个独立的结局版本:
   - "8-ending-happy": 对应第7章选择A的团圆结局
   - "8-ending-tragic": 对应第7章选择B的悲剧结局
   - "8-ending-open": 对应第7章选择C的开放结局
6. 收束节点要说明"无论之前选择如何,本章都会..."
7. 伏笔要明确(如: "埋下伏笔: XXX的真实身份")
8. 角色发展要具体(如: "苏琉璃透露XXX,但拒绝XXX")
9. 每个结局必须回收所有前面章节埋下的伏笔

**第8章结局质量标准 (最高优先级!)**:
每个结局必须包含以下元素,确保震撼力和吸引力:

1. **开场冲击** (2-3句话): 用强烈的画面或情感冲击开场
   - 团圆结局: 温暖的重逢场景,细腻的情感描写
   - 悲剧结局: 悲壮的牺牲时刻,震撼的视觉冲击
   - 开放结局: 神秘的离别场景,意境深远的画面

2. **核心事件展开** (4-5句话): 详细描写结局的关键事件
   - 具体的对话内容(不要只说"达成协议",要写出对话)
   - 细腻的动作描写(如何交出U盘、如何告别、如何离开)
   - 环境氛围渲染(天气、光线、声音、气味)

3. **角色命运** (3-4句话): 交代每个主要角色的最终归宿
   - 厉寒霆、楚瑶、秦洛冰、江明坤、韩慈、白冰、王总
   - 不要只说"XXX做了XXX",要写出他们的内心状态和生活细节
   - 用具体的场景描写(如"楚瑶在乡村工作室画图,窗外是金色麦田")

4. **伏笔回收** (2-3句话): 明确回收所有前面章节的伏笔
   - 用"回收第X章伏笔: XXX"的格式标注
   - 至少回收3-5个关键伏笔

5. **情感升华** (2-3句话): 结局的情感高潮和哲学思考
   - 团圆结局: 温暖治愈,强调"守护"与"陪伴"的力量
   - 悲剧结局: 悲壮崇高,强调"牺牲"与"正义"的价值
   - 开放结局: 意境深远,强调"选择"与"未知"的哲学

6. **结尾画面** (1-2句话): 用一个令人难忘的画面或意象结束
   - 团圆结局: 温馨的日常生活场景
   - 悲剧结局: 墓前的白菊,或纪念碑上的名字
   - 开放结局: 远方的风铃,或未读的信件

**结局示例对比**:

❌ 差的结局 (太简略):
"厉寒霆选择团圆,大家都很幸福,过上了平凡的生活."

✅ 好的结局 (详细震撼):
"厉寒霆在凤山村小院的桂花树下,将U盘交给楚父,换来楚母的自由.楚母归来那天,楚瑶跪在母亲面前痛哭,厉寒霆默默转身,给她们留下团聚的空间.三年后,厉寒霆按韩慈药方调养,身体完全康复,能背起楚瑶在田埂上奔跑.他们的乡村设计工作室墙上挂满了贫困村落改造前后的对比照,每一张都是希望的种子.秦洛冰苏醒后解散新发地产,成立公益基金会,第一笔捐款就是给凤山村小学.江明坤资助韩慈建立罕见病研究中心,白冰在狱中写信忏悔,王总改行做义工赎罪.所有伏笔回收: 楚母未死真相(第7章)、韩慈药方(第7章)、U盘证据(第6章)、白冰背叛(第2章)、王总身份(第1章).结局温暖圆满,厉寒霆终于弥补前世遗憾.黄昏时分,楚瑶在厨房做饭,厉寒霆在院子里浇花,远处传来孩子们的欢笑声,这就是他用一生守护的平凡幸福."

**质量标准**:
1. **因果连贯**: 每章的核心事件必须是前一章选择的合理后果
2. **目标明确**: 用动词+宾语格式描述具体事件,避免"继续发展"等模糊表述
3. **冲突具体**: 明确说明对立双方和争夺对象
4. **选择有意义**: 选择必须导致完全不同的后果和体验
5. **伏笔可追溯**: 伏笔在后续章节必须有明确回收,注明"回收第X章伏笔:XXX"
6. **状态追踪**: 每章结尾说明角色获得/失去的物品、能力、盟友、信息

**章节边界规则 (防止剧情压缩)**:
1. **单章单目标**: 每章只完成大纲中明确的目标,不要跨越到下一章
2. **禁止任务完成**: 如果某章目标是"接受任务",不要写"完成任务"
3. **使用"面临"而非"完成"**: 描述选择点,而非行动结果
4. **明确本章结束点**: 每章描述最后必须说明"本章结束时,主角处于XXX状态"
"""
        else:
            system_prompt = """You are an experienced screenwriter skilled at planning long-form narrative structures.
You need to generate a clear chapter outline based on the given world settings.

# Task Requirements
1. Design {max_depth} chapters based on the worldview and character settings
2. Each chapter must have a clear **core objective**, described in 1-2 sentences
3. Chapters should have causal relationships, no jumps
4. Follow classic narrative rhythm: Beginning -> Development -> Conflict -> Climax -> Ending
5. Each objective should be specific and guide subsequent writing, avoid vagueness

# World Settings
{world_summary}

# Output Format
Please output strictly in the following JSON format (no extra text):
```json
{{
  "1": "Chapter 1 objective (1-2 sentences)",
  "2": "Chapter 2 objective (1-2 sentences)",
  "3": "Chapter 3 objective (1-2 sentences)",
  ...
  "{max_depth}": "Chapter {max_depth} objective (1-2 sentences, grand finale)"
}}
```

**Important**:
- Chapters 3, 6, 9... are "convergence nodes", should be designed as key plot points where different branches merge
- Other chapters are "divergence nodes", can provide different choice routes
- The last chapter must be a shocking grand finale, resolving all foreshadowing"""

        # 格式化提示词
        formatted_prompt = system_prompt.format(
            max_depth=self.max_depth or 10,
            world_summary=world_summary
        )

        user_prompt = "请生成剧情大纲JSON." if self.detected_language == 'zh' else "Please generate the plot outline JSON."

        try:
            # 调用LLM生成大纲(增加token数以支持超详细大纲,特别是第8章的3个结局)
            response = self.llm_client.chat_completion(
                system_prompt=formatted_prompt,
                user_prompt=user_prompt,
                max_tokens=10000,  # v3.6: 增加到10000,确保第8章3个结局有足够空间(每个结局约2000字)
                temperature=0.7,
            )

            # 解析JSON
            json_str = response.strip()
            # 提取JSON部分
            if '```json' in json_str:
                json_str = json_str.split('```json')[1].split('```')[0].strip()
            elif '```' in json_str:
                json_str = json_str.split('```')[1].split('```')[0].strip()

            outline_data = json.loads(json_str)

            # 转换key为int，处理第8章的3个结局
            self.global_outline = {}
            for k, v in outline_data.items():
                # 处理第8章的3个结局
                if k.startswith('8-ending-'):
                    # 将3个结局都存储为深度8，但用不同的后缀区分
                    self.global_outline[k] = v
                else:
                    # 普通章节
                    self.global_outline[int(k)] = v

            self.outline_generated = True

            logger.info(f"全局大纲生成成功,共{len([k for k in self.global_outline.keys() if isinstance(k, int)])}个章节 + 3个结局")
            logger.info("📖 大纲详情:")
            for depth_or_key in sorted(self.global_outline.keys(), key=lambda x: (int(str(x).split('-')[0]) if isinstance(x, str) else x, str(x))):
                objective = self.global_outline[depth_or_key]
                if isinstance(depth_or_key, str):
                    # 结局
                    ending_type = depth_or_key.split('-')[-1]
                    ending_name = {"happy": "团圆", "tragic": "悲剧", "open": "开放"}[ending_type]
                    logger.info(f"   第8章({ending_name}结局): {objective[:100]}...")
                else:
                    # 普通章节
                    logger.info(f"   章节{depth_or_key}: {objective[:100]}...")

            return self.global_outline

        except Exception as e:
            logger.error(f"全局大纲生成失败: {e}")
            logger.warning("将使用默认大纲策略（占位符大纲，非基于世界观生成）")
            # 使用默认大纲
            default_depth = self.max_depth or 10
            if self.detected_language == 'zh':
                self.global_outline = {
                    i: f"第{i}章: 剧情发展阶段{i}（占位符大纲，需设置API key生成基于世界观的大纲）"
                    for i in range(1, default_depth + 1)
                }
            else:
                self.global_outline = {
                    i: f"Chapter {i}: Plot development stage {i} (placeholder outline, set API key for worldview-based outline)"
                    for i in range(1, default_depth + 1)
                }
            # 标记为占位符大纲并设置生成标志
            self.outline_generated = True
            self.is_placeholder_outline = True
            return self.global_outline

    def _save_outline_to_db(self) -> bool:
        """
        将当前大纲保存到数据库

        Returns:
            bool: 保存成功返回True，失败返回False
        """
        if not self.save_to_db:
            logger.info("数据库保存已禁用，跳过大纲保存")
            return False

        if not self.global_outline:
            logger.warning("没有可保存的大纲数据")
            return False

        # 检查是否为占位符大纲
        if getattr(self, 'is_placeholder_outline', False):
            logger.warning("⚠️  检测到占位符大纲，不保存到数据库。请设置API key以生成基于世界观的大纲。")
            return False

        try:
            # 动态导入以避免循环依赖
            import sys
            import os
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if project_root not in sys.path:
                sys.path.insert(0, project_root)

            from ..table.outline_to_db import save_outline_directly

            # 转换global_outline为字符串键字典
            # 注意：self.global_outline可能是混合类型键（int和str）
            outline_data = {}
            for key, value in self.global_outline.items():
                if isinstance(key, int):
                    outline_data[str(key)] = value
                else:
                    outline_data[key] = value

            # 保存到数据库
            outlines = save_outline_directly(
                outline_data=outline_data,
                worldview_task_id=self.task_id,
                overwrite=True  # 覆盖同版本旧记录
            )

            if outlines:
                chapter_count = len(outlines.data)  # data字段直接存储章节内容
                logger.info(f"✅ 大纲已保存到数据库，共 {chapter_count} 个章节")
                logger.info(f"   task_id: {self.task_id or '未关联'}")
                return True
            else:
                logger.warning("大纲保存到数据库失败")
                return False

        except Exception as e:
            logger.error(f"保存大纲到数据库时出错: {e}")
            import traceback
            traceback.print_exc()
            return False

    def _get_chapter_objective(self, current_depth: int, parent_choice_index: Optional[int] = None) -> str:
        """
        获取当前章节的目标

        Args:
            current_depth: 当前节点深度
            parent_choice_index: 父节点的选择索引（用于第8章判断结局类型）

        Returns:
            该章节的目标描述字符串
        """
        # 如果大纲未生成,先生成
        if not self.outline_generated:
            self._generate_global_outline()

        # 特殊处理第8章：根据第7章的选择返回对应结局
        if current_depth == 8:
            # 如果有父节点选择索引，根据索引选择结局
            if parent_choice_index is not None:
                ending_map = {
                    0: '8-ending-happy',   # 第7章选择0 -> 团圆结局
                    1: '8-ending-tragic',  # 第7章选择1 -> 悲剧结局
                    2: '8-ending-open',    # 第7章选择2 -> 开放结局
                }
                ending_key = ending_map.get(parent_choice_index, '8-ending-happy')
                objective = self.global_outline.get(
                    ending_key,
                    "最终结局章节" if self.detected_language == 'zh' else "Final ending chapter"
                )
                logger.info(f"第8章使用结局: {ending_key} (基于第7章选择{parent_choice_index})")
                return objective
            else:
                # 如果没有父节点信息，默认使用团圆结局
                logger.warning("第8章未提供父节点选择信息，默认使用团圆结局")
                return self.global_outline.get(
                    '8-ending-happy',
                    "最终结局章节" if self.detected_language == 'zh' else "Final ending chapter"
                )

        # 返回对应深度的目标
        objective = self.global_outline.get(
            current_depth,
            "继续推进剧情" if self.detected_language == 'zh' else "Continue the plot"
        )

        return objective

    def _format_state_snapshot(self) -> str:
        """格式化状态快照为字符串"""
        state = self.state_snapshot
        lines = [
            "## 当前状态",
            f"- 时间: {state.get('time', '未知')}",
            f"- 地点: {state.get('location', '未知')}",
        ]

        # 角色状态
        char_states = state.get("character_states", {})
        if char_states:
            lines.append("- 角色状态:")
            for char_id, char_state in char_states.items():
                lines.append(f"  * {char_id}: {char_state}")

        # 伏笔
        foreshadowing = state.get("foreshadowing", [])
        if foreshadowing:
            lines.append("- 活跃伏笔:")
            for f in foreshadowing:
                lines.append(f"  * {f}")

        return "\n".join(lines)

    def _parse_llm_output(self, content: str) -> Tuple[Dict[str, Any], str]:
        """
        解析LLM输出，返回空元数据和纯正文内容。
        (v3.6: 移除DirectorBlock JSON生成，保持接口兼容)

        Args:
            content: LLM完整输出

        Returns:
            (空字典, story_content)
        """
        story_content = content.strip()

        # 移除可能的markdown代码块标记
        if story_content.startswith('```markdown'):
            story_content = story_content[11:].strip()
        if story_content.startswith('```'):
            story_content = story_content[3:].strip()
        if story_content.endswith('```'):
            story_content = story_content[:-3].strip()

        # 返回空元数据，保持接口兼容
        return {}, story_content

    def _extract_summary(self, content: str, max_length: int = 200) -> str:
        """从内容中提取摘要"""
        # 取前几行,去除格式标记
        lines = []
        for line in content.split('\n'):
            line = line.strip()
            if line.startswith(('#', '*', '`', '1.', '2.', '3.', '选项')):
                continue
            if line and len(line) > 10:
                lines.append(line)
            if len('\n'.join(lines)) > max_length:
                break

        summary = ' '.join(lines)
        if len(summary) > max_length:
            summary = summary[:max_length] + '...'

        return summary

    def _build_chapter_context(self, previous_segments: List[str]) -> str:
        """构建本章前文摘要(用于Segment 2-5)"""
        if not previous_segments:
            return "本章开始." if self.detected_language == 'zh' else "Chapter begins."

        # 提取每个segment的前200字作为摘要
        summaries = []
        for i, segment in enumerate(previous_segments):
            # 取前200字
            preview = segment[:200].strip()
            if len(segment) > 200:
                preview += "..."
            summaries.append(f"Segment {i+1}: {preview}")

        prefix = "本章前文摘要:\n" if self.detected_language == 'zh' else "Chapter context:\n"
        return prefix + "\n".join(summaries)

    def _get_previous_segment_end(self, previous_segments: List[str]) -> str:
        """
        获取前文详细上下文(增强版 - 方案2)

        返回:
        1. 所有已完成segment的事件摘要(防止重复)
        2. 上一个segment的最后1000字(用于衔接)
        """
        if not previous_segments:
            return "无(本章开始)" if self.detected_language == 'zh' else "None (chapter begins)"

        # 构建详细的前文上下文
        context_parts = []

        # 1. 添加所有segment的事件摘要(防止重复)
        if self.detected_language == 'zh':
            context_parts.append("# 本章已完成的剧情段落(严禁重复以下内容):")
        else:
            context_parts.append("# Completed segments in this chapter (DO NOT REPEAT the following):")

        for i, segment in enumerate(previous_segments):
            # 提取每个segment的关键事件(前300字+中间200字+后300字)
            segment_length = len(segment)
            if segment_length <= 800:
                summary = segment
            else:
                # 提取开头、中间、结尾片段
                start = segment[:300]
                middle_pos = segment_length // 2
                middle = segment[middle_pos-100:middle_pos+100]
                end = segment[-300:]
                summary = f"{start}...[中间省略]...{middle}...[中间省略]...{end}"

            if self.detected_language == 'zh':
                context_parts.append(f"\n## Segment {i+1} 已发生的事件:")
            else:
                context_parts.append(f"\n## Segment {i+1} events that already happened:")
            context_parts.append(summary)

        # 2. 添加上一个segment的完整结尾(用于衔接)
        last_segment = previous_segments[-1]
        if self.detected_language == 'zh':
            context_parts.append(f"\n# 上一段落的结尾(请从此处自然衔接,不要重复):")
        else:
            context_parts.append(f"\n# End of previous segment (continue naturally from here, do NOT repeat):")

        if len(last_segment) > 1000:
            context_parts.append(last_segment[-1000:].strip())
        else:
            context_parts.append(last_segment.strip())

        # 3. 添加强制提醒
        if self.detected_language == 'zh':
            context_parts.append("\n⚠️ 重要提醒: 不要重复上述任何事件、对话或场景描写! 必须推进新的剧情!")
        else:
            context_parts.append("\n⚠️ CRITICAL REMINDER: Do NOT repeat any events, dialogues, or scene descriptions above! You MUST advance to NEW plot points!")

        return "\n".join(context_parts)

    def _get_convergence_instruction(self, current_depth: int) -> str:
        """
        获取菱形收束指令(根据节点深度)

        v3.6 新版8层策略:
        - 第3章: 收束节点
        - 第6章: 收束节点
        - 第7章: 分流到3种结局(团圆/悲剧/开放)
        - 第8章: 最终结局
        """
        is_chinese = self.detected_language == 'zh'

        if current_depth == 3 or current_depth == 6:
            # 第3章和第6章: 收束节点
            if is_chinese:
                return "这是收束节点(第3章或第6章).无论之前做出了什么选择,本章结局必须收束到核心主线事件,为下一阶段统一铺垫.提供1个统一的后续选项(如:'继续推进主线')."
            else:
                return "This is a convergence node (Chapter 3 or 6). Regardless of previous choices, this chapter must converge to the core storyline, setting up the next phase. Provide 1 unified follow-up option (e.g., 'Continue main storyline')."
        elif current_depth == 7:
            # 第7章: 分流到3种结局类型
            if is_chinese:
                return """这是关键分流节点(第7章).本章结束时必须分流到3种不同的结局走向:
1. **团圆结局路线** - 主角获得成功,解决冲突,达成和解
2. **悲剧结局路线** - 主角遭遇重大失败,付出巨大代价,悲壮收场
3. **开放结局路线** - 结局留有悬念,部分问题未解决,引发思考

请在Segment 5生成3个明确指向不同结局类型的选项."""
            else:
                return """This is a critical branching node (Chapter 7). At the end of this chapter, branch into 3 different ending paths:
1. **Happy Ending Route** - Protagonist succeeds, resolves conflicts, achieves reconciliation
2. **Tragic Ending Route** - Protagonist faces major failure, pays heavy price, tragic conclusion
3. **Open Ending Route** - Ending leaves mysteries, some issues unresolved, thought-provoking

Generate 3 options in Segment 5 that clearly point to different ending types."""
        elif current_depth == 8:
            # 第8章: 最终结局(不应该生成选项)
            if is_chinese:
                return "这是最终结局章节(第8章).根据第7章的选择,呈现对应的结局类型(团圆/悲剧/开放).回收所有伏笔,给出震撼或深刻的结局.不要生成后续选项."
            else:
                return "This is the final ending chapter (Chapter 8). Based on Chapter 7's choice, present the corresponding ending type (happy/tragic/open). Resolve all plot threads, deliver a powerful or profound conclusion. Do not generate follow-up options."
        else:
            # 第1,2,4,5章: 发散节点
            if is_chinese:
                return "这是发散节点.请提供2个截然不同的大方向选项(如:潜入 vs 强攻,谈判 vs 战斗),让玩家体验不同的剧情分支."
            else:
                return "This is a divergence node. Provide 2 distinctly different directional options (e.g., infiltrate vs assault, negotiate vs fight), allowing players to experience different story branches."

    def _get_options_instruction(self, current_depth: int) -> str:
        """
        获取选项生成指令

        v3.6 新版8层策略:
        - 第3,6章: 1个选项(收束)
        - 第7章: 3个选项(分流到3种结局)
        - 第8章: 0个选项(最终结局)
        - 其他: 2个选项(发散)
        """
        is_chinese = self.detected_language == 'zh'

        if current_depth == 3 or current_depth == 6:
            # 收束节点:只提供1个选项
            if is_chinese:
                return "只提供1个选项:'剧情归一,[描述统一的后果]'.不要提供多个选项."
            else:
                return "Provide only 1 option: 'Plot converges, [describe unified outcome]'. Do not provide multiple options."
        elif current_depth == 7:
            # 第7章: 分流到3种结局
            if is_chinese:
                return """提供3个选项,每个选项明确指向不同的结局类型:
1. 团圆结局选项 - 描述主角选择积极解决冲突、寻求和解的道路
2. 悲剧结局选项 - 描述主角选择悲壮的牺牲、决裂或失败的道路
3. 开放结局选项 - 描述主角选择离开、放弃或走向未知的道路

每个选项2-3句话,清晰展现不同的情感基调和后果方向."""
            else:
                return """Provide 3 options, each clearly pointing to a different ending type:
1. Happy ending option - Describe protagonist choosing to actively resolve conflicts and seek reconciliation
2. Tragic ending option - Describe protagonist choosing tragic sacrifice, rupture, or failure
3. Open ending option - Describe protagonist choosing to leave, give up, or venture into the unknown

Each option 2-3 sentences, clearly showing different emotional tones and consequence directions."""
        elif current_depth == 8:
            # 第8章: 最终结局,不生成选项
            if is_chinese:
                return "不要提供任何后续选项.这是故事的最终结局."
            else:
                return "Do not provide any follow-up options. This is the story's final ending."
        else:
            # 发散节点:提供2个选项
            if is_chinese:
                return "提供2个截然不同的大方向选项(每个选项1-2句话)."
            else:
                return "Provide 2 distinctly different directional options (each option 1-2 sentences)."

    def _generate_segment(
        self,
        current_segment: int,
        previous_segments: List[str],
        current_depth: int,
        carried_items: List[str] = None,
        parent_context: str = "",
    ) -> str:
        """
        生成单个segment的内容.

        Args:
            current_segment: 当前segment编号(1-5)
            previous_segments: 之前生成的segment内容列表
            current_depth: 当前节点深度(用于菱形收束策略)
            carried_items: 携带的物品列表
            parent_context: 父节点上下文(摘要和选择文本)

        Returns:
            生成的segment内容
        """
        # 获取当前章节目标(核心改进点)
        chapter_objective = self._get_chapter_objective(current_depth)

        # 准备上下文信息
        chapter_context = self._build_chapter_context(previous_segments)
        previous_segment_end = self._get_previous_segment_end(previous_segments)

        # 准备携带物品文本
        items_text = "无"
        if carried_items:
            items_text = "\n".join([f"- {item}" for item in carried_items])

        # 菱形收束指令(仅Segment 5需要)
        convergence_instruction = ""
        options_instruction = ""
        options_output = ""

        if current_segment == 5:
            # 检查是否达到最大深度(强制大结局)
            if self.max_depth is not None and current_depth >= self.max_depth:
                convergence_instruction = "这是整个故事的大结局.请回收所有伏笔,给出一个震撼人心或发人深省的最终结局."
                options_instruction = "不要提供任何后续选项.故事到此结束."
                options_output = ""  # 无选项
            else:
                convergence_instruction = self._get_convergence_instruction(current_depth)
                options_instruction = self._get_options_instruction(current_depth)

                # 设置options_output占位符
                if current_depth % 3 == 0:
                    options_output = "1. 剧情归一,[描述统一的后果]" if self.detected_language == 'zh' else "1. Plot converges, [describe unified outcome]"
                else:
                    options_output = "1. [选项一描述]\n2. [选项二描述]" if self.detected_language == 'zh' else "1. [Option one description]\n2. [Option two description]"

        # 第一章特殊吸引力指令
        first_chapter_hook = ""
        if current_depth == 1:
            if self.detected_language == 'zh':
                first_chapter_hook = """
# **🔥 第一章特殊要求 - 吸引读者的黄金开场 🔥**

**这是整个故事的第一章,必须在开篇就牢牢抓住读者的注意力!**

## 开场策略 (选择其一,根据剧情选择最合适的):
1. **冲突开场**: 直接从紧张的冲突场景开始(追逐、对峙、危机)
2. **悬念开场**: 从神秘事件或令人困惑的场景开始,引发好奇
3. **震撼开场**: 从视觉冲击力强的场景开始(爆炸、奇观、惊人发现)
4. **对话开场**: 从一段引人入胜的对话开始,揭示关键信息

## 第一章写作强制要求:
1. **前500字必须有钩子**: 在开篇500字内必须出现以下至少一个元素:
   - 令人震惊的事件或发现
   - 紧张的冲突或危机
   - 引人好奇的谜团或秘密
   - 强烈的情感冲击(恐惧、愤怒、惊讶)

2. **立即建立代入感**:
   - 第一段就要让读者感受到主角的处境和情绪
   - 用感官细节(视觉、听觉、触觉、嗅觉)让场景立体化
   - 避免冗长的背景介绍,通过行动和对话自然展现

3. **节奏要快**:
   - 避免大段环境描写,用简洁有力的笔触勾勒场景
   - 多用短句制造紧张感
   - 每1000字至少要有一个小转折或新信息

4. **角色要鲜活**:
   - 主角的第一次出场必须展现其核心特质(勇敢/谨慎/聪明/冲动等)
   - 通过具体行动而非抽象描述展现性格
   - 给主角一个立即可见的目标或困境

5. **埋设钩子**:
   - 在第一章中至少埋设2-3个未解之谜,让读者想继续阅读
   - 暗示更大的危机或秘密
   - 引入一个神秘角色或物品

## 避免的第一章错误:
❌ 从平淡的日常生活开始
❌ 大段世界观背景介绍
❌ 主角独自思考或回忆过去
❌ 缓慢的场景描写没有冲突
❌ 功能性对话("你好""早上好")

## 优秀第一章示例结构:
1. **开场画面**(200字): 强烈的视觉冲击或紧张场景
2. **主角登场**(300字): 通过行动展现性格,立即面临问题
3. **冲突升级**(500字): 情况变得更糟,出现意外
4. **关键信息**(400字): 揭示部分真相,但留下更多疑问
5. **钩子结尾**(600字): 以悬念或转折结束,让读者想看下一章

**记住: 第一章的每一句话都要为"让读者继续阅读"服务!**
"""
            else:
                first_chapter_hook = """
# **🔥 Chapter 1 Special Requirements - Golden Opening Hook 🔥**

**This is the first chapter of the entire story - it MUST grab the reader's attention from the very beginning!**

## Opening Strategies (choose the most suitable one based on the plot):
1. **Conflict Opening**: Start directly with a tense conflict scene (chase, confrontation, crisis)
2. **Mystery Opening**: Begin with a mysterious event or puzzling scene that sparks curiosity
3. **Shocking Opening**: Start with a visually impactful scene (explosion, spectacle, stunning discovery)
4. **Dialogue Opening**: Begin with a captivating dialogue that reveals key information

## Chapter 1 Mandatory Writing Requirements:
1. **Hook within first 500 words**: The opening 500 words MUST include at least one of these elements:
   - A shocking event or discovery
   - Tense conflict or crisis
   - An intriguing mystery or secret
   - Strong emotional impact (fear, anger, surprise)

2. **Establish immediate immersion**:
   - The first paragraph should make readers feel the protagonist's situation and emotions
   - Use sensory details (visual, auditory, tactile, olfactory) to make the scene vivid
   - Avoid lengthy background exposition; reveal naturally through action and dialogue

3. **Fast pacing**:
   - Avoid long environmental descriptions; sketch scenes with concise, powerful strokes
   - Use short sentences to create tension
   - Include at least one small twist or new information every 1000 words

4. **Vivid characters**:
   - The protagonist's first appearance must showcase their core traits (brave/cautious/smart/impulsive, etc.)
   - Show personality through specific actions, not abstract descriptions
   - Give the protagonist an immediately visible goal or predicament

5. **Plant hooks**:
   - Plant at least 2-3 unresolved mysteries in Chapter 1 to make readers want to continue
   - Hint at larger crises or secrets
   - Introduce a mysterious character or object

## First Chapter Mistakes to Avoid:
❌ Starting with mundane daily life
❌ Long worldbuilding background exposition
❌ Protagonist alone thinking or reminiscing
❌ Slow scene descriptions without conflict
❌ Functional dialogue ("Hello" "Good morning")

## Excellent Chapter 1 Structure Example:
1. **Opening Scene** (200 words): Strong visual impact or tense scenario
2. **Protagonist Entrance** (300 words): Show personality through action, immediately face a problem
3. **Escalating Conflict** (500 words): Situation worsens, unexpected developments
4. **Key Information** (400 words): Reveal partial truth but leave more questions
5. **Hook Ending** (600 words): End with suspense or twist to make readers want the next chapter

**Remember: Every sentence in Chapter 1 should serve the purpose of "making readers continue reading"!**
"""
        else:
            first_chapter_hook = ""

        # 构建系统提示词
        system_prompt = self.SEGMENT_PROMPT.format(
            current_segment=current_segment,
            chapter_objective=f"**{chapter_objective}**",  # 注入章节目标
            first_chapter_hook=first_chapter_hook,  # 第一章特殊指令
            world_bible_summary=self._prepare_world_summary(),
            rolling_summary=self.rolling_summary,
            state_snapshot=self._format_state_snapshot(),
            chapter_context=chapter_context,
            previous_segment_end=previous_segment_end,
            carried_items=items_text,
            convergence_instruction=convergence_instruction,
            options_instruction=options_instruction,
            options_output=options_output,
        )

        # 添加语言指令到系统提示词开头
        language_instruction = f"""
**CRITICAL LANGUAGE INSTRUCTION**:
- Input text language: {self.detected_language}
- You MUST write the story in THE SAME LANGUAGE as the input
- If input is English, write in English
- If input is Chinese, write in Chinese
- If input is Japanese, write in Japanese
- Maintain language consistency throughout all content

"""
        system_prompt = language_instruction + system_prompt

        # 用户提示词(根据segment不同) - 通用多语言版本
        if current_segment == 1:
            user_prompt = f"""
{parent_context}
Begin writing the first part of this chapter (Segment 1/5).

Requirements:
1. Continue from previous events, introduce this chapter's core conflict
2. Output approximately 2000-2500 words of pure narrative (no JSON, no options)
3. Focus on environment description, character interactions, and plot setup
4. Do NOT use markdown bold markers like **[]**
5. Use quotation marks for dialogue
6. **Write in {self.detected_language} language** (zh=Chinese, en=English, ja=Japanese)
7. [TARGET] **Keep chapter objective in mind**: {chapter_objective}
8. **CRITICAL: Avoid repetitive descriptions**:
   - Use fresh imagery different from previous segments
   - Introduce new environmental details (2-3 new elements)
   - Rotate sensory descriptions (visual → auditory → olfactory)
   - **NEVER repeat the same opening scene/weather/atmosphere from previous chapter**
   - Use varied chapter openings: action, dialogue, silence, memory, internal monologue, etc.
   - Check: Does this opening feel fresh and different from the last chapter?
9. **CRITICAL: High dialogue density (25%-40% of content)**:
   - Include at least 3 meaningful dialogue exchanges
   - Each dialogue must advance plot or reveal character
   - Add micro-expressions/body language after each line
   - Include 1 deep conversation (10+ dialogue turns)
"""
        elif current_segment == 5:
            user_prompt = f"""
Complete the final part of this chapter (Segment 5/5).

Requirements:
1. Conclude this chapter's plot, resolve main conflicts
2. Output approximately 2000-2500 words of ending narrative
3. Then output follow-up options
4. {convergence_instruction}
5. Do NOT use markdown bold markers like **[]**, use quotation marks for dialogue
6. **Write all narrative in {self.detected_language} language** (zh=Chinese, en=English, ja=Japanese)
7. **Ensure chapter objective is achieved**: {chapter_objective}
8. **🔴 CRITICAL: Review all previous segments and DO NOT REPEAT any events/dialogues/scenes! 🔴**
"""
        else:
            user_prompt = f"""
Continue writing part {current_segment} of this chapter (Segment {current_segment}/5).

**🔴 CRITICAL ANTI-REDUNDANCY CHECK (INTERNAL THINKING ONLY - DO NOT OUTPUT): 🔴**

Before you start writing, you MUST internally review the previous segments and think about:
1. What events have ALREADY happened in previous segments?
2. What dialogues have ALREADY been said?
3. What scenes have ALREADY been described?
4. What NEW events will I introduce in THIS segment? (Must be different from above)
5. What NEW location/time will THIS segment take place? (Must advance timeline)

**⚠️ IMPORTANT: These questions are for your internal thinking ONLY. DO NOT write them in your output!**
**⚠️ Your output should ONLY contain the story narrative, starting directly with "## Scene: [场景名]"**

Requirements:
1. **🔴 DO NOT REPEAT any events, dialogues, or scenes from previous segments! 🔴**
2. **MUST advance to NEW plot points**: Introduce new events, new conflicts, new information
3. **MUST change location or time**: Move to a different scene or advance the timeline
4. Output approximately 2000-2500 words of pure narrative (no JSON, no options)
5. Focus on: {['plot development + new information', 'plot twist + unexpected events', 'climax + core conflict explosion'][current_segment-2]}
6. Do NOT use markdown bold markers like **[]**
7. Use quotation marks for dialogue
8. **Write in {self.detected_language} language** (zh=Chinese, en=English, ja=Japanese)
9. **Progress towards chapter objective**: {chapter_objective}
10. **CRITICAL: Avoid repetitive descriptions**:
   - Do NOT reuse imagery from previous segments
   - Introduce 2-3 completely new environmental elements
   - Rotate sensory focus: if previous segment was visual, focus on sound/smell
   - Check: Have I avoided repeating "rain", "neon", "oil" metaphors?
11. **CRITICAL: High dialogue density (25%-40% of content)**:
   - Include at least 3 meaningful dialogue exchanges
   - Each line must reveal character personality or advance plot
   - Add body language/facial expressions after dialogue
   - Segment {current_segment} must include: {['a deep conversation', 'an intense argument'][min(current_segment-3, 1)]}

**REMEMBER: Your job is to ADVANCE the story, NOT to repeat what already happened!**
"""

        logger.info(f"生成Segment {current_segment}/5 (深度: {current_depth})")
        logger.info(f"章节目标: {chapter_objective}")
        logger.debug(f"前文摘要: {chapter_context[:100]}...")

        try:
            # 调用LLM生成内容(降低token数以减少超时风险)
            max_tokens = 4000 if current_segment == 5 else 3000  # 从4000/6000降到3000/4000
            content = self.llm_client.chat_completion(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=max_tokens,
            )

            # 后处理：移除LLM可能输出的检查清单内容
            content = self._clean_redundancy_check_output(content)

            logger.info(f"Segment {current_segment} 生成完成,长度: {len(content)}字符")
            return content

        except Exception as e:
            logger.error(f"生成Segment {current_segment}失败: {e}")
            raise

    def _clean_redundancy_check_output(self, content: str) -> str:
        """
        清理LLM输出中可能包含的反重复检查清单内容

        移除类似以下格式的内容：
        - **🔴 CRITICAL ANTI-REDUNDANCY CHECK ANSWERS:**
        - 1. **已发生事件**：...
        - 等等
        """
        import re

        # 定义需要移除的模式
        patterns_to_remove = [
            # 匹配整个检查清单块（中文）
            r'\*\*🔴 CRITICAL ANTI-REDUNDANCY CHECK ANSWERS:\*\*.*?(?=\n##|\n###|\Z)',
            # 匹配整个检查清单块（英文）
            r'\*\*🔴 CRITICAL ANTI-REDUNDANCY CHECK.*?(?=\n##|\n###|\Z)',
            # 匹配已发生事件列表
            r'\d+\.\s*\*\*已发生事件\*\*[：:].+?(?=\n\d+\.|\n##|\n###|\Z)',
            # 匹配已说对话列表
            r'\d+\.\s*\*\*已说对话\*\*[：:].+?(?=\n\d+\.|\n##|\n###|\Z)',
            # 匹配已描写场景列表
            r'\d+\.\s*\*\*已描写场景\*\*[：:].+?(?=\n\d+\.|\n##|\n###|\Z)',
            # 匹配本段新事件列表
            r'\d+\.\s*\*\*本段新事件\*\*[：:].+?(?=\n\d+\.|\n##|\n###|\Z)',
            # 匹配新地点/时间列表
            r'\d+\.\s*\*\*新地点/时间\*\*[：:].+?(?=\n\d+\.|\n##|\n###|\Z)',
        ]

        cleaned_content = content
        for pattern in patterns_to_remove:
            cleaned_content = re.sub(pattern, '', cleaned_content, flags=re.DOTALL | re.MULTILINE)

        # 移除多余的空行（超过2个连续换行）
        cleaned_content = re.sub(r'\n{3,}', '\n\n', cleaned_content)

        # 移除开头和结尾的空白
        cleaned_content = cleaned_content.strip()

        # 如果清理后内容明显变短，记录日志
        if len(cleaned_content) < len(content) * 0.9:
            logger.info(f"已清理反重复检查清单内容，原长度: {len(content)}, 清理后: {len(cleaned_content)}")

        return cleaned_content

    def _validate_asset_completeness(self, full_content: str, director_metadata: Dict[str, Any]) -> None:
        """
        检查DirectorBlock是否包含正文中提到的所有角色。

        验证Segment 5生成的DirectorBlock是否遗漏了Segment 1-4中提到的资产。

        Args:
            full_content: 完整的故事内容(所有segment)
            director_metadata: Segment 5生成的DirectorBlock元数据
        """
        import re

        # 从world_bible获取所有已知角色名称
        known_characters = set()
        if self.world_bible.characters:
            for char_id, char_data in self.world_bible.characters.items():
                # 添加角色ID (如 TangSan, XiaoWu)
                known_characters.add(char_id)
                # 添加角色中文名 (如 唐三, 小舞)
                if isinstance(char_data, dict) and 'name' in char_data:
                    known_characters.add(char_data['name'])

        # 检查正文中实际出现的已知角色
        mentioned_names = set()
        for char_name in known_characters:
            # 简单检查: 角色名是否在正文中出现
            if char_name in full_content:
                mentioned_names.add(char_name)

        # 从metadata中提取已定义角色
        defined_chars = set(director_metadata.get("characters", {}).keys())

        # 检查缺失的角色(仅限world_bible中的已知角色)
        missing_chars = mentioned_names - defined_chars
        if missing_chars:
            logger.warning(f"正文提到但未在DirectorBlock中定义的角色: {missing_chars}")
            logger.warning(f"   提示:Segment 5可能遗漏了这些角色的定义.建议在SEGMENT_PROMPT中强化要求.")
        else:
            logger.info("资产完整性检查通过,所有提到的角色都已定义.")

    def _update_rolling_summary(self):
        """更新滚动摘要"""
        if len(self.recent_nodes) < self.rolling_window_size:
            return

        # 提取最近N个节点的摘要
        recent_summaries = [summary for _, summary in self.recent_nodes[-self.rolling_window_size:]]
        combined_text = "\n".join(recent_summaries)

        # 调用LLM生成摘要
        summary_prompt = f"""
请将以下最近{self.rolling_window_size}个剧情节点的内容压缩成一段简洁的「前情提要」,约200-300字.

最近剧情:
{combined_text}

要求:
1. 保持关键情节转折和角色发展
2. 忽略细节,保留主线
3. 用连贯的叙事语言
4. 为后续剧情提供上下文

前情提要:
"""
        try:
            new_summary = self.llm_client.chat_completion(
                system_prompt="你是一位专业的剧情摘要师,擅长压缩剧情内容.",
                user_prompt=summary_prompt,
                max_tokens=500,
                temperature=0.3,
            ).strip()

            self.rolling_summary = new_summary
            logger.info(f"滚动摘要已更新,长度: {len(new_summary)}字符")

            # 清空近期节点列表(摘要已包含它们)
            self.recent_nodes = []

        except Exception as e:
            logger.error(f"更新滚动摘要失败: {e}")

    def generate_node(
            self,
            parent_node: Optional[StoryNode] = None,
            choice_index: int = 0,
            choice_text: Optional[str] = None,
            carried_items: List[str] = None,
    ) -> Tuple[StoryNode, str]:
        """
        生成一个剧情节点

        架构升级:
        - 如果安装了LangGraph: 使用 Writer->Critic->Replanner 工作流(带自修正)
        - 否则: 使用传统的直接生成模式

        Args:
            parent_node: 父节点(None表示根节点)
            choice_index: 父节点中的选择索引
            choice_text: 选择的文本描述
            carried_items: 携带的物品列表

        Returns:
            (StoryNode, str): 生成的节点和完整内容
        """
        # 1. 计算新的 ID 和深度
        if parent_node:
            current_depth = parent_node.depth + 1
            node_id = f"node_{uuid.uuid4().hex[:8]}"
        else:
            current_depth = 1
            node_id = "chapter_1"

        title = f"剧情节点 {node_id}"

        # 2. 获取当前章节目标（第8章需要父节点的选择索引）
        parent_choice_index = choice_index if parent_node and parent_node.depth == 7 else None
        chapter_objective = self._get_chapter_objective(current_depth, parent_choice_index)

        logger.info(f"生成剧情节点: {node_id} (深度: {current_depth})")
        logger.info(f"章节目标: {chapter_objective}")

        # 3. 根据是否安装LangGraph选择生成方式
        if LANGGRAPH_AVAILABLE and self.app is not None:
            # 使用 LangGraph 工作流(带自修正)
            return self._generate_node_with_langgraph(
                node_id=node_id,
                current_depth=current_depth,
                title=title,
                parent_node=parent_node,
                choice_text=choice_text,
                carried_items=carried_items or [],
                chapter_objective=chapter_objective
            )
        else:
            # 传统生成模式(无自修正)
            logger.warning("使用传统生成模式(LangGraph未安装)")
            return self._generate_node_legacy(
                node_id=node_id,
                current_depth=current_depth,
                title=title,
                parent_node=parent_node,
                choice_text=choice_text,
                carried_items=carried_items or [],
                chapter_objective=chapter_objective
            )

    def _extract_summary(self, content: str, max_length: int = 200) -> str:
        """从内容中提取摘要"""
        # 取前几行,去除格式标记
        lines = []
        for line in content.split('\n'):
            line = line.strip()
            if line.startswith(('#', '*', '`', '1.', '2.', '3.', '选项')):
                continue
            if line and len(line) > 10:
                lines.append(line)
            if len('\n'.join(lines)) > max_length:
                break

        summary = ' '.join(lines)
        if len(summary) > max_length:
            summary = summary[:max_length] + '...'

        return summary

    # ========================================
    # LangGraph Agent 节点方法
    # ========================================

    def _node_writer(self, state: StoryGraphState) -> StoryGraphState:
        """
        Writer Agent: 生成章节内容(5个Segment)

        功能:
        - 读取chapter_objective
        - 执行5个Segment生成循环
        - 解析director_metadata
        - 如果retry_count > 0, 在Prompt中追加修正指令

        Returns:
            更新后的state (包含full_content和director_metadata)
        """
        logger.info(f"Writer Agent 开始生成 (深度: {state['depth']}, 重试: {state['retry_count']})")

        # 准备上下文
        chapter_objective = state['chapter_objective']
        current_depth = state['depth']
        carried_items = state.get('carried_items', [])

        # 如果是重试,准备修正指令
        correction_hint = ""
        if state['retry_count'] > 0:
            correction_hint = f"""
# 重要修正指令
上一轮生成因为偏离大纲被驳回.
偏离原因: {state.get('deviation_analysis', '未指明')}
请在本轮生成中严格遵循本章目标: {chapter_objective}
不要再次偏离.
"""
            logger.warning(f"重试生成 (第{state['retry_count']}次), 原因: {state.get('deviation_analysis', '未指明')[:50]}...")

        # 准备父节点上下文
        parent_context = ""
        parent_node = state.get('parent_node')
        choice_text = state.get('choice_text')
        if parent_node:
            parent_context = f"上一个场景的摘要:\n{parent_node.summary}\n\n"
            if choice_text:
                parent_context += f"用户选择: {choice_text}\n\n"

        if correction_hint:
            parent_context = correction_hint + "\n" + parent_context

        try:
            # 执行5个Segment生成循环
            full_content_parts = []
            for segment_num in range(1, 6):
                logger.info(f"  生成Segment {segment_num}/5...")

                segment_content = self._generate_segment(
                    current_segment=segment_num,
                    previous_segments=full_content_parts,
                    current_depth=current_depth,
                    carried_items=carried_items,
                    parent_context=parent_context,
                )
                full_content_parts.append(segment_content)

            # 拼接完整内容
            full_content = "\n\n".join(full_content_parts)

            # 解析最后一个segment获取元数据
            last_segment = full_content_parts[-1]
            director_metadata, segment_5_story = self._parse_llm_output(last_segment)

            # 构建完整故事内容
            story_content = "\n\n".join(full_content_parts[:4] + [segment_5_story])

            # 提取选项
            def extract_choices_from_segment(segment_text: str, depth: int) -> List[str]:
                """从segment文本中提取选项"""
                lines = segment_text.strip().split('\n')
                choices = []
                for line in reversed(lines):
                    line = line.strip()
                    if (line.startswith(("1.", "2.", "3.", "选项一", "选项二", "选项三"))
                            and len(line) > 5):
                        if line.startswith("选项一"):
                            line = "1." + line[3:]
                        elif line.startswith("选项二"):
                            line = "2." + line[3:]
                        elif line.startswith("选项三"):
                            line = "3." + line[3:]
                        choices.append(line)
                    if len(choices) >= 3:
                        break

                # v3.6 新版8层策略: 根据章节强制选项数量
                is_chinese = self.detected_language == 'zh'

                if depth == 3 or depth == 6:
                    # 第3,6章收束节点: 1个选项
                    if choices:
                        choices = [choices[0]]
                    else:
                        choices = ["1. 剧情归一,继续推进主线"] if is_chinese else ["1. Plot converges, continue main storyline"]
                elif depth == 7:
                    # 第7章分流节点: 强制3个选项
                    if len(choices) >= 3:
                        choices = choices[:3]
                    elif len(choices) == 2:
                        choices.append("3. 选择第三条道路" if is_chinese else "3. Choose the third path")
                    elif len(choices) == 1:
                        if is_chinese:
                            choices.extend(["2. 选择另一条道路", "3. 选择第三条道路"])
                        else:
                            choices.extend(["2. Choose another path", "3. Choose the third path"])
                    else:
                        if is_chinese:
                            choices = [
                                "1. 团圆路线,寻求和解与共赢",
                                "2. 悲剧路线,孤注一掷付出代价",
                                "3. 开放路线,离开远行留有悬念"
                            ]
                        else:
                            choices = [
                                "1. Happy ending route, seek reconciliation and win-win",
                                "2. Tragic ending route, go all-in and pay the price",
                                "3. Open ending route, leave with mysteries unresolved"
                            ]
                elif depth == 8:
                    # 第8章最终结局: 0个选项
                    choices = []
                else:
                    # 第1,2,4,5章发散节点: 2个选项
                    if len(choices) >= 2:
                        choices = choices[:2]
                    elif len(choices) == 1:
                        choices = [choices[0], "2. 选择另一条道路" if is_chinese else "2. Choose another path"]
                    else:
                        if is_chinese:
                            choices = ["1. 激进路线,强行突破", "2. 稳健路线,暗中调查"]
                        else:
                            choices = ["1. Aggressive route, force breakthrough", "2. Cautious route, investigate secretly"]

                # 清理选项文本
                cleaned_choices = []
                for choice in choices:
                    cleaned = re.sub(r'^\d+\.\s*', '', choice)
                    cleaned = cleaned.replace('**', '').replace('"', '').replace('"', '').replace('"', '')
                    cleaned = re.sub(r'^[::\.\\s]+', '', cleaned)
                    cleaned = cleaned.strip()
                    cleaned_choices.append(cleaned)

                return cleaned_choices

            # 检查是否达到最大深度
            if self.max_depth is not None and current_depth >= self.max_depth:
                choices = []
            else:
                choices = extract_choices_from_segment(segment_5_story, current_depth)

            # 提取摘要
            summary = self._extract_summary(story_content)

            # 验证资产完整性 (v3.6: 已移除DirectorBlock JSON生成，注释掉验证)
            # self._validate_asset_completeness(full_content, director_metadata)


            # 更新状态
            state['full_content'] = full_content
            state['full_content_parts'] = full_content_parts
            state['director_metadata'] = director_metadata
            state['choices'] = choices
            state['summary'] = summary

            logger.info(f"Writer Agent 完成生成 (总字数: {len(full_content)})")
            return state

        except Exception as e:
            logger.error(f"Writer Agent 失败: {e}")
            raise

    def _node_critic(self, state: StoryGraphState) -> StoryGraphState:
        """
        Critic Agent: 审查内容是否偏离大纲

        功能:
        - 评估生成的内容是否完成了chapter_objective
        - 检测剧情是否跑偏
        - 给出评分(0-1.0)和偏离分析

        Returns:
            更新后的state (包含critic_score和deviation_analysis)
        """
        logger.info(f"Critic Agent 开始审查 (深度: {state['depth']})")

        chapter_objective = state['chapter_objective']
        full_content = state['full_content']
        parent_node = state.get('parent_node')
        choice_text = state.get('choice_text')

        # 生成内容摘要(前1000字,增加审查深度)
        content_summary = full_content[:1000] + "..." if len(full_content) > 1000 else full_content

        # 准备前文上下文(用于检查逻辑一致性)
        context_info = ""
        if parent_node:
            context_info = f"\n\n# 前文摘要\n{parent_node.summary}\n"
            if choice_text:
                context_info += f"\n# 玩家选择\n{choice_text}\n"

        # 构建Critic Prompt
        if self.detected_language == 'zh':
            critic_prompt = f"""你是一位严格的互动小说质量审查专家,负责确保多分支剧情的逻辑一致性和高质量.

# 本章目标
{chapter_objective}
{context_info}
# 📖 生成的内容摘要 (前1000字)
{content_summary}

# 审查维度 (总分12分)

## 1. 目标完成度 (0-3分)
- 内容是否完成了本章目标中的核心事件?
- 关键选择/冲突是否体现?
- 伏笔是否埋下?

## 2. 逻辑一致性 (0-3分) - **最重要**
- 角色行为是否符合性格设定和前文表现?
- 世界观规则是否保持一致(魂力体系、势力关系等)?
- 时间线是否合理(不突然跳跃,事件发生顺序正确)?
- 物品/能力使用是否符合设定(不突然消失或凭空出现)?
- 如果是分支后的剧情,是否合理延续了玩家的选择?

## 3. 剧情连贯性 (0-2分)
- 是否与前文自然衔接,没有突兀转折?
- 角色关系发展是否合理渐进?

## 4. 分支合理性 (0-2分) - **多线路关键**
- 提供的选择是否有意义,能产生不同后果?
- 选择是否符合当前剧情情境,不牵强?
- 两个选择方向是否真正对立或差异化?

## 5. 章节边界合规性 (0-2分) - **防止剧情压缩的关键!**
- **内容是否局限在本章大纲范围内?**
- **是否出现了后续章节才应有的事件/场景?**
- **结尾是否正确停在本章目标完成点,而非继续推进到下一章?**

**检查要点**:
- 如果本章目标是"在龙门客栈接受委托,面临是否前往静寂区的选择"
  * 应该停在: 角色思考是否接受委托
  * 不应有: 角色已经到达静寂区,已经遭遇战斗
- 如果本章目标是"遭遇苏琉璃,选择信任或背叛"
  * 应该停在: 两人初次见面,面临信任抉择
  * 不应有: 已经进入古庙,已经获得灵核

# 严重问题 (出现以下任一情况,总分不得超过5分)
1. **角色突变**: 角色性格突然改变,行为完全不符合设定
2. **逻辑矛盾**: 明显的因果倒置或事件冲突
3. **世界观崩塌**: 违背已确立的世界规则
4. **时空错乱**: 时间线混乱或场景莫名切换
5. **分支无效**: 选择与之前玩家决策完全无关,失去互动性
6. **章节越界**: 写出了大纲中明确标注为后续章节的内容/场景 (如第1章写到第3章的事件)

# 输出格式
严格按照以下JSON格式输出(不要有额外文字):
```json
{{
  "score": 8.5,
  "analysis": "详细分析: [目标完成情况] + [逻辑问题诊断] + [分支评价] + [改进建议]"
}}
```

# 评分标准 (总分12分)
- **10-12分**: 完美,逻辑严密,分支合理,高度符合目标,章节边界清晰
- **8-9分**: 良好,基本符合目标,有小瑕疵但可接受
- **6-7分**: 及格线,有明显问题需修正,但主线尚可
- **0-5分**: 不及格,严重偏离目标或存在逻辑崩塌/章节越界,必须重写

**当前重点**:
1. 确保不同选择路线的逻辑自洽性,避免"无论选什么结果都一样"的假分支
2. **严格检查章节边界,防止剧情压缩(一章写完多章内容)**
"""
        else:
            critic_prompt = f"""You are a strict plot reviewer responsible for checking if the generated content meets the outline objective.

# Chapter Objective
{chapter_objective}

# Generated Content Summary
{content_summary}

# Review Requirements
Please evaluate from the following dimensions (total 10 points):
1. **Objective Completion** (0-4 points): Does the content complete the chapter objective?
2. **Plot Coherence** (0-3 points): Does it connect naturally with the previous content?
3. **Logical Consistency** (0-3 points): Are there obvious deviations or contradictions?

# Output Format
Please output strictly in the following JSON format (no extra text):
```json
{{
  "score": 8.5,
  "analysis": "The content basically completes the objective, but slightly deviates in XXX, suggest XXX."
}}
```

**Scoring Standard**:
- 9-10: Perfect completion, no deviation
- 7-8: Basically complete, minor deviation but acceptable
- 5-6: Partially complete, obvious deviation needs correction
- 0-4: Serious deviation, needs rewrite
"""

        try:
            # 调用LLM进行审查
            response = self.llm_client.chat_completion(
                system_prompt="你是一位专业的剧情审查专家." if self.detected_language == 'zh' else "You are a professional plot reviewer.",
                user_prompt=critic_prompt,
                max_tokens=500,
                temperature=0.3,
            )

            # 解析JSON
            json_str = response.strip()
            if '```json' in json_str:
                json_str = json_str.split('```json')[1].split('```')[0].strip()
            elif '```' in json_str:
                json_str = json_str.split('```')[1].split('```')[0].strip()

            result = json.loads(json_str)

            # 归一化分数到0-1 (总分12分)
            score = float(result.get('score', 0)) / 12.0
            analysis = result.get('analysis', 'No analysis provided')

            state['critic_score'] = score
            state['deviation_analysis'] = analysis

            if score >= 0.7:
                logger.info(f"Critic Agent: 通过审查 (得分: {score:.2f})")
            else:
                logger.warning(f"Critic Agent: 未通过审查 (得分: {score:.2f})")
                logger.warning(f"   偏离分析: {analysis}")
                # 修复: 在Critic节点中增加重试计数
                state['retry_count'] += 1
                logger.info(f"   当前重试次数: {state['retry_count']}/{state['max_retries']}")

            return state

        except Exception as e:
            logger.error(f"Critic Agent 失败: {e}, 默认通过")
            # 如果审查失败,默认通过
            state['critic_score'] = 0.8
            state['deviation_analysis'] = f"审查失败: {str(e)}, 默认通过"
            return state

    def _node_replanner(self, state: StoryGraphState) -> StoryGraphState:
        """
        Replanner Agent: 动态修改后续大纲以适配当前剧情

        功能:
        - 读取deviation_analysis
        - 读取global_outline
        - 调用LLM修改后续章节大纲
        - 更新self.global_outline
        - 强制将critic_score置为1.0以跳出循环

        Returns:
            更新后的state (critic_score=1.0, 表示接受当前内容)
        """
        logger.info(f"Replanner Agent 开始重规划 (深度: {state['depth']})")

        current_depth = state['depth']
        deviation_analysis = state.get('deviation_analysis', '未指明')

        # 准备当前大纲
        remaining_outline = {
            k: v for k, v in self.global_outline.items()
            if k > current_depth
        }

        if not remaining_outline:
            logger.info("  已到达最后一章,无需重规划")
            state['critic_score'] = 1.0
            return state

        # 构建Replanner Prompt
        if self.detected_language == 'zh':
            replanner_prompt = f"""你是一位资深剧情编剧,需要根据实际生成的剧情调整后续大纲.

# 当前情况
- 当前章节深度: {current_depth}
- 原定目标: {self.global_outline.get(current_depth, '未知')}
- 实际生成的剧情偏离: {deviation_analysis}

# 原定后续大纲
{json.dumps(remaining_outline, ensure_ascii=False, indent=2)}

# 任务
请基于当前实际生成的剧情,调整后续章节的大纲,确保剧情能够自然衔接.

# 输出格式
请严格按照以下JSON格式输出(不要有任何额外文字):
```json
{{
  "{current_depth + 1}": "调整后的第{current_depth + 1}章目标",
  "{current_depth + 2}": "调整后的第{current_depth + 2}章目标",
  ...
}}
```
"""
        else:
            replanner_prompt = f"""You are an experienced plot writer who needs to adjust the subsequent outline based on the actually generated plot.

# Current Situation
- Current chapter depth: {current_depth}
- Original objective: {self.global_outline.get(current_depth, 'Unknown')}
- Actual plot deviation: {deviation_analysis}

# Original Subsequent Outline
{json.dumps(remaining_outline, ensure_ascii=False, indent=2)}

# Task
Please adjust the subsequent chapters' outline based on the actual generated plot to ensure a natural transition.

# Output Format
Please output strictly in the following JSON format (no extra text):
```json
{{
  "{current_depth + 1}": "Adjusted chapter {current_depth + 1} objective",
  "{current_depth + 2}": "Adjusted chapter {current_depth + 2} objective",
  ...
}}
```
"""

        try:
            # 调用LLM重规划
            response = self.llm_client.chat_completion(
                system_prompt="你是一位专业的剧情编剧." if self.detected_language == 'zh' else "You are a professional plot writer.",
                user_prompt=replanner_prompt,
                max_tokens=1500,
                temperature=0.7,
            )

            # 解析JSON
            json_str = response.strip()
            if '```json' in json_str:
                json_str = json_str.split('```json')[1].split('```')[0].strip()
            elif '```' in json_str:
                json_str = json_str.split('```')[1].split('```')[0].strip()

            new_outline = json.loads(json_str)

            # 更新global_outline
            for key, value in new_outline.items():
                self.global_outline[int(key)] = value

            logger.info(f"Replanner Agent: 已更新后续{len(new_outline)}个章节的大纲")
            for depth, objective in list(new_outline.items())[:2]:
                logger.info(f"   章节{depth}: {objective[:50]}...")

            # 保存更新后的大纲
            # 如果task_id不存在，保存到本地文件便于调试
            if not self.task_id:
                outline_file = self.story_dir / "global_outline.json"
                try:
                    with open(outline_file, 'w', encoding='utf-8') as f:
                        json.dump(self.global_outline, f, ensure_ascii=False, indent=2)
                    logger.info(f"  已更新大纲文件: {outline_file}")
                except Exception as e:
                    logger.warning(f"  保存大纲失败: {e}")
            else:
                logger.info(f"  大纲已更新（关联task_id: {self.task_id}）")
                # 注意：数据库中的大纲可能需要更新，但为了简化，当前不自动更新
                # 可以考虑在故事生成完成后统一更新数据库

            # 强制通过(接受当前内容)
            state['critic_score'] = 1.0
            state['deviation_analysis'] = f"已接受偏离并调整后续大纲: {deviation_analysis}"

            return state

        except Exception as e:
            logger.error(f"Replanner Agent 失败: {e}, 强制接受当前内容")
            # 如果重规划失败,强制接受
            state['critic_score'] = 1.0
            state['deviation_analysis'] = f"重规划失败: {str(e)}, 强制接受"
            return state

    # ========================================
    # 节点生成方法(LangGraph模式和传统模式)
    # ========================================

    def _generate_node_with_langgraph(
        self,
        node_id: str,
        current_depth: int,
        title: str,
        parent_node: Optional[StoryNode],
        choice_text: Optional[str],
        carried_items: List[str],
        chapter_objective: str,
    ) -> Tuple[StoryNode, str]:
        """
        使用LangGraph工作流生成节点(带自修正)

        Args:
            node_id: 节点ID
            current_depth: 当前深度
            title: 节点标题
            parent_node: 父节点
            choice_text: 选择文本
            carried_items: 携带物品
            chapter_objective: 章节目标

        Returns:
            (StoryNode, str): 生成的节点和完整内容
        """
        logger.info(f"启动 LangGraph 工作流生成节点...")

        # 准备初始状态
        initial_state: StoryGraphState = {
            'node_id': node_id,
            'depth': current_depth,
            'parent_node': parent_node,
            'choice_text': choice_text,
            'carried_items': carried_items,
            'chapter_objective': chapter_objective,
            'rolling_summary': self.rolling_summary,
            'state_snapshot': self._format_state_snapshot(),

            # 初始化生成内容字段
            'full_content': '',
            'full_content_parts': [],
            'director_metadata': {},
            'choices': [],
            'summary': '',

            # 初始化审查字段
            'critic_score': 0.0,
            'deviation_analysis': '',

            # 初始化循环控制
            'retry_count': 0,
            'max_retries': 2,
        }

        try:
            # 执行LangGraph工作流
            logger.info("  执行 Writer -> Critic -> [Replanner] 工作流...")
            final_state = self.app.invoke(initial_state)

            # 从最终状态中提取结果
            full_content = final_state['full_content']
            director_metadata = final_state['director_metadata']
            choices = final_state['choices']
            summary = final_state['summary']

            logger.info(f"LangGraph 工作流完成 (最终得分: {final_state['critic_score']:.2f})")

            # 构建StoryNode
            node = StoryNode(
                id=node_id,
                depth=current_depth,
                title=title,
                parent_id=parent_node.id if parent_node else None,
                summary=summary,
                director_metadata=director_metadata,
            )

            # 添加子节点选项
            for choice_text in choices:
                child_id = f"node_{uuid.uuid4().hex[:8]}"
                node.children.append(StoryChoice(
                    choice_text=choice_text,
                    child_id=child_id
                ))

            # 更新滚动摘要和状态
            self.recent_nodes.append((node_id, summary))
            if len(self.recent_nodes) >= self.rolling_window_size:
                self._update_rolling_summary()

            # 从director_metadata更新状态快照
            if 'state_updates' in director_metadata:
                state_updates = director_metadata['state_updates']
                if 'time' in state_updates:
                    self.state_snapshot['time'] = state_updates['time']
                if 'location' in state_updates:
                    self.state_snapshot['location'] = state_updates['location']
                if 'character_states' in state_updates:
                    self.state_snapshot['character_states'].update(state_updates['character_states'])
                if 'foreshadowing' in state_updates:
                    self.state_snapshot['foreshadowing'] = state_updates['foreshadowing']

            return node, full_content

        except Exception as e:
            logger.error(f"LangGraph 工作流失败: {e}")
            logger.warning("  回退到传统生成模式...")
            # 回退到传统模式
            return self._generate_node_legacy(
                node_id=node_id,
                current_depth=current_depth,
                title=title,
                parent_node=parent_node,
                choice_text=choice_text,
                carried_items=carried_items,
                chapter_objective=chapter_objective
            )

    def _generate_node_legacy(
        self,
        node_id: str,
        current_depth: int,
        title: str,
        parent_node: Optional[StoryNode],
        choice_text: Optional[str],
        carried_items: List[str],
        chapter_objective: str,
    ) -> Tuple[StoryNode, str]:
        """
        传统节点生成模式(无自修正)

        直接调用5次_generate_segment,不经过Critic审查.

        Args:
            node_id: 节点ID
            current_depth: 当前深度
            title: 节点标题
            parent_node: 父节点
            choice_text: 选择文本
            carried_items: 携带物品
            chapter_objective: 章节目标

        Returns:
            (StoryNode, str): 生成的节点和完整内容
        """
        logger.info(f"传统模式生成节点...")

        # 准备父节点上下文
        parent_context = ""
        if parent_node:
            parent_context = f"上一个场景的摘要:\n{parent_node.summary}\n\n"
            if choice_text:
                parent_context += f"用户选择: {choice_text}\n\n"

        try:
            # 执行5个Segment生成循环
            full_content_parts = []
            for segment_num in range(1, 6):
                logger.info(f"  生成Segment {segment_num}/5...")

                segment_content = self._generate_segment(
                    current_segment=segment_num,
                    previous_segments=full_content_parts,
                    current_depth=current_depth,
                    carried_items=carried_items,
                    parent_context=parent_context,
                )
                full_content_parts.append(segment_content)

            # 拼接完整内容
            full_content = "\n\n".join(full_content_parts)

            # 解析最后一个segment获取元数据
            last_segment = full_content_parts[-1]
            director_metadata, segment_5_story = self._parse_llm_output(last_segment)

            # 构建完整故事内容
            story_content = "\n\n".join(full_content_parts[:4] + [segment_5_story])

            # 提取选项
            def extract_choices_from_segment(segment_text: str, depth: int) -> List[str]:
                """从segment文本中提取选项"""
                lines = segment_text.strip().split('\n')
                choices = []
                for line in reversed(lines):
                    line = line.strip()
                    if (line.startswith(("1.", "2.", "3.", "选项一", "选项二", "选项三"))
                            and len(line) > 5):
                        if line.startswith("选项一"):
                            line = "1." + line[3:]
                        elif line.startswith("选项二"):
                            line = "2." + line[3:]
                        elif line.startswith("选项三"):
                            line = "3." + line[3:]
                        choices.append(line)
                    if len(choices) >= 3:
                        break

                # v3.6 新版8层策略: 根据章节强制选项数量
                is_chinese = self.detected_language == 'zh'

                if depth == 3 or depth == 6:
                    # 第3,6章收束节点: 1个选项
                    if choices:
                        choices = [choices[0]]
                    else:
                        choices = ["1. 剧情归一,继续推进主线"] if is_chinese else ["1. Plot converges, continue main storyline"]
                elif depth == 7:
                    # 第7章分流节点: 强制3个选项
                    if len(choices) >= 3:
                        choices = choices[:3]
                    elif len(choices) == 2:
                        choices.append("3. 选择第三条道路" if is_chinese else "3. Choose the third path")
                    elif len(choices) == 1:
                        if is_chinese:
                            choices.extend(["2. 选择另一条道路", "3. 选择第三条道路"])
                        else:
                            choices.extend(["2. Choose another path", "3. Choose the third path"])
                    else:
                        if is_chinese:
                            choices = [
                                "1. 团圆路线,寻求和解与共赢",
                                "2. 悲剧路线,孤注一掷付出代价",
                                "3. 开放路线,离开远行留有悬念"
                            ]
                        else:
                            choices = [
                                "1. Happy ending route, seek reconciliation and win-win",
                                "2. Tragic ending route, go all-in and pay the price",
                                "3. Open ending route, leave with mysteries unresolved"
                            ]
                elif depth == 8:
                    # 第8章最终结局: 0个选项
                    choices = []
                else:
                    # 第1,2,4,5章发散节点: 2个选项
                    if len(choices) >= 2:
                        choices = choices[:2]
                    elif len(choices) == 1:
                        choices = [choices[0], "2. 选择另一条道路" if is_chinese else "2. Choose another path"]
                    else:
                        if is_chinese:
                            choices = ["1. 激进路线,强行突破", "2. 稳健路线,暗中调查"]
                        else:
                            choices = ["1. Aggressive route, force breakthrough", "2. Cautious route, investigate secretly"]

                # 清理选项文本
                cleaned_choices = []
                for choice in choices:
                    cleaned = re.sub(r'^\d+\.\s*', '', choice)
                    cleaned = cleaned.replace('**', '').replace('"', '').replace('"', '').replace('"', '')
                    cleaned = re.sub(r'^[::\.\\s]+', '', cleaned)
                    cleaned = cleaned.strip()
                    cleaned_choices.append(cleaned)

                return cleaned_choices

            # 检查是否达到最大深度
            if self.max_depth is not None and current_depth >= self.max_depth:
                choices = []
            else:
                choices = extract_choices_from_segment(segment_5_story, current_depth)

            # 提取摘要
            summary = self._extract_summary(story_content)

            # 验证资产完整性 (v3.6: 已移除DirectorBlock JSON生成，注释掉验证)
            # self._validate_asset_completeness(full_content, director_metadata)


            # 构建StoryNode
            node = StoryNode(
                id=node_id,
                depth=current_depth,
                title=title,
                parent_id=parent_node.id if parent_node else None,
                summary=summary,
                director_metadata=director_metadata,
            )

            # 添加子节点选项
            for choice_text in choices:
                child_id = f"node_{uuid.uuid4().hex[:8]}"
                node.children.append(StoryChoice(
                    choice_text=choice_text,
                    child_id=child_id
                ))

            # 更新滚动摘要和状态
            self.recent_nodes.append((node_id, summary))
            if len(self.recent_nodes) >= self.rolling_window_size:
                self._update_rolling_summary()

            # 从director_metadata更新状态快照
            if 'state_updates' in director_metadata:
                state_updates = director_metadata['state_updates']
                if 'time' in state_updates:
                    self.state_snapshot['time'] = state_updates['time']
                if 'location' in state_updates:
                    self.state_snapshot['location'] = state_updates['location']
                if 'character_states' in state_updates:
                    self.state_snapshot['character_states'].update(state_updates['character_states'])
                if 'foreshadowing' in state_updates:
                    self.state_snapshot['foreshadowing'] = state_updates['foreshadowing']

            logger.info(f"传统模式生成完成 (总字数: {len(full_content)})")

            return node, full_content

        except Exception as e:
            logger.error(f"传统模式生成失败: {e}")
            raise

    def save_node_content(self, node: StoryNode, content: str):
        """保存节点内容到Markdown文件，同时存入数据库"""
        content_dir = self.story_dir / "generated_stories"
        content_dir.mkdir(exist_ok=True)

        # 清洗正文里泄漏的舞台指令/创作注释（如“（场景转换：进入夺权线）”）
        content = _strip_stage_directions(content, node_id=node.id)

        filepath = content_dir / f"{node.id}.md"
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)

        logger.info(f"节点内容已保存到本地: {filepath}")

        # 保存到数据库（如果启用）
        if self.save_to_db and self.task_id:
            try:
                from ..table.story_tree_to_db import save_story_md_to_db
                record = save_story_md_to_db(
                    task_id=self.task_id,
                    name=node.id,
                    content=content,
                    overwrite=True
                )
                if record:
                    logger.info(f"节点内容已保存到数据库: task_id={self.task_id}, name={node.id}")
                else:
                    logger.warning(f"节点内容保存到数据库失败: name={node.id}")
            except Exception as e:
                logger.warning(f"保存节点内容到数据库失败: {e}")
                import traceback
                traceback.print_exc()

    def initialize_story(self) -> StoryTree:
        """初始化故事,创建根节点"""
        logger.info("初始化故事树...")

        # 先获取/生成全局大纲(在生成任何节点前)
        if self.max_depth and self.max_depth > 0:
            # 优先从数据库获取大纲
            outline_loaded = False
            if self.task_id:
                logger.info(f"尝试从数据库获取大纲 (task_id={self.task_id})...")
                outline_loaded = self._load_outline_from_db()

            if outline_loaded:
                logger.info("成功从数据库加载大纲，跳过生成")
            else:
                # 数据库中没有大纲，生成新的
                logger.info("数据库中未找到大纲，开始生成全局大纲...")
                self._generate_global_outline()

                # 保存大纲到数据库（如果启用）
                if self.save_to_db and self.task_id:
                    try:
                        logger.info("正在保存大纲到数据库...")
                        self._save_outline_to_db()
                        logger.info("大纲已保存到数据库，跳过本地文件保存")
                    except Exception as e:
                        logger.warning(f"保存大纲到数据库失败: {e}")
                        import traceback
                        traceback.print_exc()
                else:
                    # 只有在不保存到数据库时，才保存到本地文件
                    outline_file = self.story_dir / "global_outline.json"
                    try:
                        with open(outline_file, 'w', encoding='utf-8') as f:
                            json.dump(self.global_outline, f, ensure_ascii=False, indent=2)
                        logger.info(f"全局大纲已保存到本地文件: {outline_file}")
                        if self.task_id:
                            logger.info(f"   关联task_id: {self.task_id}")
                    except Exception as e:
                        logger.warning(f"保存大纲到本地文件失败: {e}")

        # 创建根节点
        root_node, content = self.generate_node(
            parent_node=None,
            carried_items=[]
        )

        # 创建剧情树
        self.tree = StoryTree(root_id=root_node.id)
        self.tree.add_node(root_node)

        # 保存内容
        self.save_node_content(root_node, content)

        # 保存剧情树
        tree_file = self.story_dir / "story_tree.json"
        self.tree.save(str(tree_file))

        logger.info(f"故事树初始化完成,根节点: {root_node.id}")
        return self.tree

    def _load_outline_from_db(self) -> bool:
        """
        从数据库加载大纲

        Returns:
            bool: 是否成功加载
        """
        if not self.task_id:
            return False

        try:
            from ..table.outline_to_db import get_outline_by_task_id

            outline = get_outline_by_task_id(self.task_id)
            if outline and outline.data:
                # 将数据库中的大纲数据转换为 global_outline 格式
                # 数据库中的 key 是字符串 "1", "2", ..., "8-ending-happy" 等
                # global_outline 的 key 是整数或特殊字符串
                self.global_outline = {}
                for key, value in outline.data.items():
                    # 处理数字键 (1-7)
                    if key.isdigit():
                        self.global_outline[int(key)] = value
                    else:
                        # 处理结局键 (8-ending-happy 等)
                        self.global_outline[key] = value

                self.outline_generated = True
                logger.info(f"从数据库加载大纲成功: {len(self.global_outline)} 个章节")
                return True
            else:
                logger.info(f"数据库中未找到大纲 (task_id={self.task_id})")
                return False

        except ImportError as e:
            logger.warning(f"无法导入 outline_to_db 模块: {e}")
            return False
        except Exception as e:
            logger.warning(f"从数据库加载大纲失败: {e}")
            import traceback
            traceback.print_exc()
            return False

    def continue_story(
        self,
        current_node_id: str,
        choice_index: int,
        carried_items: List[str] = None,
    ) -> StoryNode:
        """
        基于选择继续故事.

        Args:
            current_node_id: 当前节点ID
            choice_index: 选择索引(0-based)
            carried_items: 携带的物品列表

        Returns:
            新的故事节点
        """
        if not self.tree:
            raise ValueError("故事树未初始化,请先调用 initialize_story()")

        current_node = self.tree.get_node(current_node_id)
        if not current_node:
            raise ValueError(f"节点不存在: {current_node_id}")

        if choice_index < 0 or choice_index >= len(current_node.children):
            raise ValueError(f"无效的选择索引: {choice_index}")

        choice = current_node.children[choice_index]

        # 生成新节点
        new_node, content = self.generate_node(
            parent_node=current_node,
            choice_text=choice.choice_text,
            carried_items=carried_items or [],
        )

        # 添加到树中
        self.tree.add_node(new_node)

        # 更新父节点的子节点引用(确保child_id正确)
        current_node.children[choice_index].child_id = new_node.id

        # 保存内容
        self.save_node_content(new_node, content)

        # 保存更新的剧情树
        tree_file = self.story_dir / "story_tree.json"
        self.tree.save(str(tree_file))

        logger.info(f"故事继续,新节点: {new_node.id}")
        return new_node

    def load_existing_tree(self, tree_file: Optional[str] = None):
        """加载现有的剧情树"""
        if tree_file is None:
            tree_file = self.story_dir / "story_tree.json"
        else:
            tree_file = Path(tree_file)

        if not tree_file.exists():
            raise FileNotFoundError(f"剧情树文件不存在: {tree_file}")

        self.tree = StoryTree.load(str(tree_file))
        logger.info(f"已加载剧情树,根节点: {self.tree.root_id}")

        # 尝试加载已有的大纲
        outline_file = self.story_dir / "global_outline.json"
        if outline_file.exists():
            try:
                with open(outline_file, 'r', encoding='utf-8') as f:
                    outline_data = json.load(f)
                # 转换key，处理第8章的3个结局
                self.global_outline = {}
                for k, v in outline_data.items():
                    if k.startswith('8-ending-'):
                        # 结局保持字符串key
                        self.global_outline[k] = v
                    else:
                        # 普通章节转为int
                        self.global_outline[int(k)] = v
                self.outline_generated = True
                chapter_count = len([k for k in self.global_outline.keys() if isinstance(k, int)])
                ending_count = len([k for k in self.global_outline.keys() if isinstance(k, str) and k.startswith('8-ending-')])
                logger.info(f"已加载全局大纲,共{chapter_count}个章节 + {ending_count}个结局")
            except Exception as e:
                logger.warning(f"加载大纲失败: {e},将重新生成")

        return self.tree


def main():
    """命令行入口"""
    import argparse

    parser = argparse.ArgumentParser(description="无限流作家")
    parser.add_argument(
        "--world-bible",
        default="../world_db/world_bible.json",
        help="世界圣经文件路径 (默认: ../world_db/world_bible.json)"
    )
    parser.add_argument(
        "--action",
        choices=["init", "continue"],
        default="init",
        help="操作类型: init=初始化故事, continue=继续故事"
    )
    parser.add_argument(
        "--node-id",
        help="继续故事时的当前节点ID"
    )
    parser.add_argument(
        "--choice-index",
        type=int,
        default=0,
        help="选择索引 (0-based)"
    )
    parser.add_argument(
        "--provider",
        choices=["openai", "claude", "qwen"],
        default="openai",
        help="LLM 提供商 (默认: openai)"
    )
    parser.add_argument(
        "--model",
        help="模型名称"
    )

    args = parser.parse_args()

    # 加载世界圣经
    if not os.path.exists(args.world_bible):
        print(f"错误: 世界圣经文件不存在: {args.world_bible}")
        return

    world_bible = WorldBible.load(args.world_bible)

    # 创建生成器
    generator = StoryGenerator(
        world_bible=world_bible,
        provider=args.provider,
        model=args.model,
    )

    try:
        if args.action == "init":
            # 初始化故事
            tree = generator.initialize_story()
            root = tree.get_node(tree.root_id)

            print(f"\n故事初始化完成!")
            print(f"   根节点ID: {root.id}")
            print(f"   标题: {root.title}")
            print(f"   选项数量: {len(root.children)}")
            for i, choice in enumerate(root.children):
                print(f"   {i+1}. {choice.choice_text}")

            # 显示内容预览
            content_file = generator.story_dir / "generated_stories" / f"{root.id}.md"
            if content_file.exists():
                with open(content_file, 'r', encoding='utf-8') as f:
                    preview = f.read(200)
                print(f"\n   内容预览: {preview}...")

        elif args.action == "continue":
            if not args.node_id:
                print("错误: --node-id 参数是必须的")
                return

            # 加载现有树或初始化
            tree_file = generator.story_dir / "story_tree.json"
            if tree_file.exists():
                generator.load_existing_tree()
            else:
                print(f"错误: 剧情树文件不存在,请先初始化故事")
                return

            # 继续故事
            new_node = generator.continue_story(
                current_node_id=args.node_id,
                choice_index=args.choice_index,
                carried_items=[],  # 实际使用时应从游戏状态获取
            )

            print(f"\n故事继续完成!")
            print(f"   新节点ID: {new_node.id}")
            print(f"   标题: {new_node.title}")
            print(f"   选项数量: {len(new_node.children)}")
            for i, choice in enumerate(new_node.children):
                print(f"   {i+1}. {choice.choice_text}")

    except Exception as e:
        print(f"操作失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
