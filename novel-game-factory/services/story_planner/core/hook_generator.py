"""
序章（Hook）生成器
在第一章正文前自动生成 1-2 分钟的沉浸式序章剧情，
使用多模板轮换策略，绝不直接操控小说主角。

生成结果以纯文本形式追加到第一章内容头部，
并用醒目分隔标记与正文隔开：

    ════════════════════════════════════════
    ◆◆◆  序幕结束 · 正文开始  ◆◆◆
    ════════════════════════════════════════

后续 UI 渲染层可通过该标记切换宽银幕/全彩模式。
"""
import os
import random
from typing import Optional

# 分隔标记（UI 渲染层通过此标记识别序章/正文边界）
HOOK_SEPARATOR = (
    "\n\n"
    "════════════════════════════════════════\n"
    "◆◆◆  序幕结束 · 正文开始  ◆◆◆\n"
    "════════════════════════════════════════\n\n"
)

# 模板 ID 常量
TEMPLATE_RED_SHIRT = "red_shirt"       # 模板A：无名氏视角
TEMPLATE_CRYPTIC   = "cryptic"         # 模板B：谜语人博弈
TEMPLATE_LORE_DROP = "lore_drop"       # 模板C：史诗残卷
TEMPLATE_OMEN      = "omen"            # 模板D：不祥预兆（新增）
TEMPLATE_ECHO      = "echo"            # 模板E：回声碎片（新增）

ALL_TEMPLATES = [
    TEMPLATE_RED_SHIRT,
    TEMPLATE_CRYPTIC,
    TEMPLATE_LORE_DROP,
    TEMPLATE_OMEN,
    TEMPLATE_ECHO,
]

# ──────────────────────────────────────────────────────────────
# System prompt（所有模板共用）
# ──────────────────────────────────────────────────────────────
_SYSTEM_PROMPT = """你是一位顶级视觉小说编剧，专门负责为小说改编的视觉小说创作沉浸式序章（Hook）。

【核心约束 - 必须严格遵守】
1. 绝对不操控、不改写小说主角的行为、台词或命运。
2. 序章内容必须是"旁路视角"——通过配角、路人、幕后势力或旁白来烘托世界观。
3. 序章时长约 1-2 分钟（300-500 字，10-20 句对话/旁白）。
4. 结尾必须以悬念最高潮收尾，让玩家迫切想点击继续。
5. 输出格式：纯文本剧本，每行一句台词或旁白，无需 JSON 包装。
6. 台词格式：【角色名/旁白】：内容。无名角色用【甲】【乙】【旁白】等。
7. 语言风格与小说题材保持一致（古风/赛博朋克/现代/奇幻等）。
8. 不要在输出中包含任何模板说明、注释或元信息。"""


# ──────────────────────────────────────────────────────────────
# 各模板的 User Prompt 构造函数
# ──────────────────────────────────────────────────────────────

def _prompt_red_shirt(chapter_summary: str, genre: str) -> str:
    """模板A：无名氏视角 —— 底层小兵/路人甲遭遇危险，烘托世界残酷"""
    return f"""【题材】{genre}
【第一章核心背景摘要】
{chapter_summary}

【任务】请使用"无名氏视角"模板创作序章：
- 虚构一个底层小兵、路人甲或无名角色（不是主角）
- 让他/她在第一章的危险环境或敌对势力中遭遇险境
- 通过这个小人物的视角，侧面展示世界观的残酷与压迫感
- 结尾：这个无名角色的命运戛然而止，画面定格在最紧张的瞬间
- 不要出现主角姓名，不要剧透主角的行动

请直接输出剧本内容，每行一句。"""


def _prompt_cryptic(chapter_summary: str, genre: str) -> str:
    """模板B：谜语人博弈 —— 两个高维实体在黑暗中评估即将发生的事"""
    return f"""【题材】{genre}
【第一章核心背景摘要】
{chapter_summary}

【任务】请使用"谜语人博弈"模板创作序章：
- 场景：纯黑背景，两个没有名字的神秘存在（可以是命运之神、幕后黑手、观测者、古老意志等）
- 他们用隐晦、宿命论的语气，讨论第一章即将发生的事件
- 语气：冷静、高深、带有一丝玩味或悲悯
- 他们可以打赌、争论、或各自陈述预言
- 结尾：其中一个说出一句让人不寒而栗的预言，然是沉默
- 绝对不直接说出主角姓名，用"那个人""棋子""变数"等代称

请直接输出剧本内容，每行一句。"""


def _prompt_lore_drop(chapter_summary: str, genre: str) -> str:
    """模板C：史诗残卷 —— 第三方旁白讲述引发第一章事件的历史渊源"""
    return f"""【题材】{genre}
【第一章核心背景摘要】
{chapter_summary}

【任务】请使用"史诗残卷"模板创作序章：
- 以第三方史书/古籍/数据档案/传说的口吻进行旁白叙述
- 讲述引发第一章核心冲突的历史渊源、古老诅咒或世界背景
- 语气：庄重、史诗感、带有沧桑与宿命
- 可以穿插简短的历史场景片段（用【旁白】标注）
- 结尾：旁白话锋一转，指向"而今日，一切将再度开始……"类似的收尾
- 不要直接描述主角，只描述世界和历史

请直接输出剧本内容，每行一句。"""


def _prompt_omen(chapter_summary: str, genre: str) -> str:
    """模板D：不祥预兆 —— 通过自然/超自然异象暗示即将到来的风暴"""
    return f"""【题材】{genre}
【第一章核心背景摘要】
{chapter_summary}

【任务】请使用"不祥预兆"模板创作序章：
- 场景：第一章故事发生地的某个角落，在主角到来之前
- 通过环境描写、动物异动、超自然现象、普通人的对话来营造山雨欲来的压迫感
- 可以有1-2个普通路人的简短对话，他们感受到了某种不对劲但说不清楚
- 语气：压抑、诡异、充满不安
- 结尾：一个最终的异象出现，然后画面骤然黑暗
- 不要出现主角，只描写环境和普通人的反应

请直接输出剧本内容，每行一句。"""


def _prompt_echo(chapter_summary: str, genre: str) -> str:
    """模板E：回声碎片 —— 来自未来/平行时间线的破碎记忆闪回"""
    return f"""【题材】{genre}
【第一章核心背景摘要】
{chapter_summary}

【任务】请使用"回声碎片"模板创作序章：
- 呈现来自"未来某个时间点"或"另一条时间线"的破碎画面
- 内容：3-4个极短的场景碎片（每个2-4句），像记忆闪回一样跳跃
- 这些碎片暗示了故事可能走向的某种结局或关键转折，但语焉不详
- 每个碎片之间用【……】或【画面切换】分隔
- 语气：迷离、破碎、带有强烈的情感残留
- 结尾：所有碎片消散，只剩一句话悬在空中
- 碎片中的角色可以用模糊的称呼，不要直接点名主角

请直接输出剧本内容，每行一句。"""


# ──────────────────────────────────────────────────────────────
# 模板注册表
# ──────────────────────────────────────────────────────────────
_TEMPLATE_PROMPTS = {
    TEMPLATE_RED_SHIRT: _prompt_red_shirt,
    TEMPLATE_CRYPTIC:   _prompt_cryptic,
    TEMPLATE_LORE_DROP: _prompt_lore_drop,
    TEMPLATE_OMEN:      _prompt_omen,
    TEMPLATE_ECHO:      _prompt_echo,
}

_TEMPLATE_NAMES = {
    TEMPLATE_RED_SHIRT: "无名氏视角",
    TEMPLATE_CRYPTIC:   "谜语人博弈",
    TEMPLATE_LORE_DROP: "史诗残卷",
    TEMPLATE_OMEN:      "不祥预兆",
    TEMPLATE_ECHO:      "回声碎片",
}


# ──────────────────────────────────────────────────────────────
# 核心生成函数
# ──────────────────────────────────────────────────────────────

def _extract_chapter_summary(chapter_text: str, max_chars: int = 2000) -> str:
    """
    从第一章原文中提取摘要用于 prompt。
    取前 max_chars 字，避免 prompt 过长。
    """
    text = chapter_text.strip()
    if len(text) <= max_chars:
        return text
    # 截取前段，在句号/换行处就近截断
    snippet = text[:max_chars]
    for sep in ('。', '\n', '！', '？', '.', '!', '?'):
        pos = snippet.rfind(sep)
        if pos > max_chars // 2:
            return snippet[:pos + 1] + "……（后略）"
    return snippet + "……（后略）"


def _detect_genre_from_art_style(art_style: str) -> str:
    """将 art_style 映射为中文题材描述，供 prompt 使用"""
    mapping = {
        "ink_chinese":      "仙侠古风",
        "cyberpunk":        "赛博朋克科幻",
        "japanese":         "日式奇幻",
        "dark_european":    "黑暗欧式奇幻",
        "modern_realism":   "现代都市现实",
    }
    return mapping.get(art_style, "奇幻冒险")


_LANGUAGE_INSTRUCTION = {
    "en-US": "9. Output language: English. All dialogue and narration must be in English.",
    "ja-JP": "9. Output language: Japanese. All dialogue and narration must be in Japanese.",
    "zh-CN": "",
    "zh-TW": "9. Output language: Traditional Chinese. All dialogue and narration must be in Traditional Chinese.",
}


def generate_hook(
    chapter_text: str,
    art_style: str = "ink_chinese",
    provider: Optional[str] = None,
    template: Optional[str] = None,
    output_language: Optional[str] = None,
) -> str:
    """
    生成序章文本。

    Args:
        chapter_text: 第一章原文（用于提取背景摘要）
        art_style:    画面风格（ink_chinese/cyberpunk/japanese/dark_european/modern_realism）
        provider:     LLM 提供商（qwen/openai/claude），默认读环境变量
        template:     指定模板 ID，None 则随机选择

    Returns:
        序章纯文本剧本（不含分隔标记）
    """
    from services.story_planner.core.llm_client import LLMClient

    if provider is None:
        provider = os.getenv("LLM_PROVIDER", "openai")

    # 随机或指定模板
    if template is None or template not in _TEMPLATE_PROMPTS:
        template = random.choice(ALL_TEMPLATES)

    template_name = _TEMPLATE_NAMES[template]
    print(f"🎬 序章模板: [{template_name}]")

    genre = _detect_genre_from_art_style(art_style)
    summary = _extract_chapter_summary(chapter_text)

    user_prompt = _TEMPLATE_PROMPTS[template](summary, genre)

    lang_instruction = _LANGUAGE_INSTRUCTION.get(output_language or "", "")
    system_prompt = _SYSTEM_PROMPT + ("\n" + lang_instruction if lang_instruction else "")

    client = LLMClient(provider=provider, temperature=0.85, max_tokens=1200)
    hook_text = client.chat_completion(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        timeout=180,
    )

    return hook_text.strip()


def inject_hook_into_chapter(
    chapter_text: str,
    art_style: str = "ink_chinese",
    provider: Optional[str] = None,
    template: Optional[str] = None,
    output_language: Optional[str] = None,
) -> str:
    """
    生成序章并注入到第一章文本头部，中间插入分隔标记。

    返回格式：
        <序章剧本>
        ════════════════════════════════════════
        ◆◆◆  序幕结束 · 正文开始  ◆◆◆
        ════════════════════════════════════════
        <第一章原文>

    Args:
        chapter_text: 第一章原文
        art_style:    画面风格
        provider:     LLM 提供商
        template:     指定模板 ID，None 则随机

    Returns:
        注入序章后的完整文本
    """
    try:
        hook_text = generate_hook(
            chapter_text=chapter_text,
            art_style=art_style,
            provider=provider,
            template=template,
            output_language=output_language,
        )
        print(f"✅ 序章生成完成，字数: {len(hook_text)}")
        return hook_text + HOOK_SEPARATOR + chapter_text
    except Exception as e:
        print(f"⚠️  序章生成失败，跳过（原因: {e}）")
        # 生成失败不阻断主流程，直接返回原文
        return chapter_text
