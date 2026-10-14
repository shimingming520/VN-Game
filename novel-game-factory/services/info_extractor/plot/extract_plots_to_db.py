import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
services_dir = os.path.join(project_root, "services")
utils_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "utils")
for p in (project_root, services_dir, os.path.normpath(utils_dir)):
    if p not in sys.path:
        sys.path.insert(0, p)

import json
import re
import asyncio
from collections import deque
from sqlalchemy import create_engine
from sqlalchemy import text as sa_text
from openai import AsyncOpenAI
from tqdm.asyncio import tqdm
from tenacity import retry, stop_after_attempt, wait_random_exponential
from logger_config import setup_logger
from pypinyin import lazy_pinyin

logger = setup_logger("Step1_Plots", "step1_plots.log")

from common.config import DATABASE_URL, OUTPUTS_PATH, PLATFORM_DATABASE_URL
from common.db_pool import worker_pool_kwargs
from common.llm_fallback import ModelFallback
from common.generic_appellations import INTERPUNCT_RE
from common.model_json import parse_model_json
from common.name_mapping_utils import get_original_conversion
from common.plot_llm_config import resolve_plot_llm_config
from info_extractor.cg.cg_build import (
    build_cg_annotation_draft,
    strip_cg_details_from_plots,
    upsert_cg_data,
)
from info_extractor.utils.story_tree_json_parse import update_story_tree_field

try:
    from common.usage_tracker import tracker as _usage_tracker
    from common.usage_tracker import init_usage_table

    init_usage_table()
except Exception:
    _usage_tracker = None

DB_URL = PLATFORM_DATABASE_URL  # 跨模块共用表已迁平台库(agent_platform)
global_engine = create_engine(
    DB_URL,
    pool_pre_ping=True,
    **worker_pool_kwargs(default_pool_size=10, default_max_overflow=20),
)

# 需求3: full_name → speaker_id 的转换映射表
# (剧情脱敏前使用 name_mappings.json 的 original_name.conversion)
# {中文主名: speaker_id},如 {"李玉": "LiYu"}。在 main 开头加载一次,全程复用。
# 命中则直接用映射值,未命中走 to_pascal_case_speaker_id 的原有拼音/驼峰规则。
_NAME_CONVERSION_MAP = {}

# 已知角色主名集合。用于 _is_anchored_relational 判断"<角色>的<关系词>"里
# 的锚点(如"凌妙妙的丫鬟"中的"凌妙妙")是否真实存在于角色库。
# 在 main 开头由 load_known_character_names(char_data) 填充,全程复用。
_KNOWN_CHARACTER_NAMES = set()


def load_known_character_names(char_data):
    """从角色库收集"可信角色主名"集合,写入全局 _KNOWN_CHARACTER_NAMES。

    用途:_is_anchored_relational 靠它判断"<锚点>的<关系词>"里的锚点是不是
    真实存在的具名角色。只有锚点命中本集合,才把该称呼认定为"锚定型关系称呼"
    (如"凌妙妙的丫鬟"),从而与"漂浮描述短语"(如"御剑飞天的女子")区分开。

    收集口径:char_data 的 key 与每个角色的 name 字段(不含 other_names,
    别名本身不可信,不作为锚点依据)。
    """
    global _KNOWN_CHARACTER_NAMES
    names = set()
    if isinstance(char_data, dict):
        for key, info in char_data.items():
            if isinstance(key, str) and key.strip():
                names.add(key.strip())
            if isinstance(info, dict):
                nm = info.get("name")
                if isinstance(nm, str) and nm.strip():
                    names.add(nm.strip())
    _KNOWN_CHARACTER_NAMES = names
    logger.info(f"✅ 已加载已知角色名集合: {len(names)} 个 (用于锚定型关系称呼识别)")
    return names


def load_name_conversion_map(task_id):
    """加载 full_name→speaker_id 转换表,三级回退并写入全局 _NAME_CONVERSION_MAP。

    优先级:
      1. name_mappings 表 (data.original_name.conversion)
      2. 本地文件 {project_root}/storage/outputs/{task_id}/name_mappings.json
      3. 都没有 → 空表(全部走原有拼音/驼峰规则)
    """
    global _NAME_CONVERSION_MAP
    conversion = None
    source_data = None

    # ---- 来源1: name_mappings 表 ----
    try:
        with global_engine.connect() as conn:
            sql = sa_text("SELECT data FROM name_mappings WHERE task_id = :tid")
            row = conn.execute(sql, {"tid": task_id}).fetchone()
        if row and row[0]:
            data = row[0]
            if isinstance(data, str):
                data = json.loads(data)
            if isinstance(data, dict) and (
                isinstance(data.get("original_name"), dict)
                or isinstance(data.get("conversion"), dict)
            ):
                conversion = get_original_conversion(data)
                source_data = data
                logger.info(
                    f"✅ 从 name_mappings 表加载 original_name.conversion: "
                    f"{len(conversion)} 条"
                )
    except Exception as e:
        logger.warning(f"⚠️ 查询 name_mappings 表失败(将尝试本地文件): {e}")

    # ---- 来源2: 本地文件 ----
    if conversion is None:
        local_path = os.path.join(project_root, "storage", "outputs", str(task_id), "name_mappings.json")
        if os.path.exists(local_path):
            try:
                with open(local_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and (
                    isinstance(data.get("original_name"), dict)
                    or isinstance(data.get("conversion"), dict)
                ):
                    conversion = get_original_conversion(data)
                    source_data = data
                    logger.info(
                        f"✅ 从本地文件加载 original_name.conversion: "
                        f"{local_path} ({len(conversion)} 条)"
                    )
            except Exception as e:
                logger.warning(f"⚠️ 读取本地 name_mappings.json 失败: {e}")
        else:
            logger.info(f"ℹ️ 本地 name_mappings.json 不存在: {local_path}")

    # ---- 来源3: 兜底空表 ----
    if conversion is None:
        logger.info("ℹ️ 未找到 name_mappings,full_name→speaker_id 全部采用内置拼音/驼峰规则")
        conversion = {}

    _NAME_CONVERSION_MAP = _reject_anonymized_conversion(conversion, source_data)
    return _NAME_CONVERSION_MAP


def _reject_anonymized_conversion(conversion, mappings):
    """剔除值是脱敏后 ID 的条目,保证本步骤绝不提前换名。

    旧扁平结构的 name_mappings 没有 original_name 段,get_original_conversion 会用
    new-name 反查 conversion,返回"原名 -> 脱敏 ID"。若直接采用,本步骤就会给原名
    打上脱敏 speaker_id,与 full_name 仍是原名的状态自相矛盾,也违反"换名只在
    第 16 步进行"的约定。命中的条目退回内置拼音规则。
    """
    if not conversion:
        return {}

    changed_ids = set()
    changed_names = set()
    if isinstance(mappings, dict):
        change_section = mappings.get("change_name")
        if not isinstance(change_section, dict):
            change_section = mappings
        changed_conversion = change_section.get("conversion")
        if isinstance(changed_conversion, dict):
            changed_ids = {
                str(value) for value in changed_conversion.values() if value
            }
            changed_names = {str(key) for key in changed_conversion if key}
        new_names = change_section.get("new-name")
        if isinstance(new_names, dict):
            changed_names.update(str(value) for value in new_names.values() if value)

    cleaned = {}
    rejected = {}
    for original_name, speaker_id in conversion.items():
        name_text, id_text = str(original_name), str(speaker_id)
        # 原名自身就是脱敏后的名字时,说明这份映射已被换过名,同样不可信。
        is_anonymized = id_text in changed_ids and id_text != _pinyin_speaker_id(name_text)
        if is_anonymized or name_text in changed_names:
            rejected[name_text] = id_text
            continue
        cleaned[original_name] = speaker_id

    if rejected:
        logger.warning(
            f"⚠️ name_mappings 疑似已脱敏,已忽略 {len(rejected)} 条映射并退回拼音规则"
            f"(本步骤不做换名): {rejected}"
        )
    return cleaned

_PLOT_LLM_CONFIG = resolve_plot_llm_config()
API_KEY = _PLOT_LLM_CONFIG.api_key
BASE_URL = _PLOT_LLM_CONFIG.base_url
MODEL_NAME = _PLOT_LLM_CONFIG.model
# 主模型偶发不稳定时自动改用备选模型重发同一请求（同中转站、同调用参数）。
_model_fallback = ModelFallback(MODEL_NAME, name="Plots", logger=logger)

MAX_CONCURRENT_REQUESTS = 5  # 正文提取并发数
MAX_POV_CONCURRENT = 10  # POV 嗅探并发数(轻量调用,可以放更高)

ALLOWED_EMOTIONS = [
    "calm", "fearful", "angry", "sad",
    "surprised", "happy", "disgusted"
]

EMOTION_KEYWORDS = {
    "happy": [
        "微笑", "大笑", "欢笑", "欢喜", "开心", "高兴", "激动", "温柔", "得意", "轻快", "好耶", "打趣", "释然", "轻笑",
        "噗嗤",
        "smile", "laugh", "joy", "grin", "chuckle", "happy", "glad", "cheerful", "relieved", "excited"
    ],
    "sad": [
        "哭泣", "大哭", "落泪", "眼泪", "泪水", "抽泣", "悲伤", "哀求", "哽咽", "苦涩", "伤心", "难过", "绝望", "叹息",
        "垂下头", "心如刀绞", "痛苦", "红了眼眶",
        "cry", "tear", "sob", "weep", "sad", "grief", "despair", "sigh", "pain", "heartbroken", "mourn"
    ],
    "angry": [
        "愤怒", "发怒", "怒吼", "大吼", "气愤", "愤恨", "怒骂", "责骂", "怒瞪", "瞪眼", "喝斥", "大喝", "冷笑", "讥笑",
        "嘲笑", "质问", "咆哮", "滚", "闭嘴", "呵斥", "咬牙切齿",
        "angry", "furious", "rage", "shout", "yell", "glare", "roar", "growl", "scowl", "shut up", "sneer", "mock"
    ],
    "fearful": [
        "惊恐", "害怕", "苍白", "退缩", "哆嗦", "冷汗", "别过来", "惊慌", "发抖", "颤抖", "恐惧", "畏惧", "慌乱",
        "慌张",
        "fear", "scared", "terrified", "tremble", "shiver", "pale", "panic", "shudder", "horrified"
    ],
    "surprised": [
        "惊诧", "愣住", "错愕", "发呆", "傻眼", "意外", "不可置信", "瞪大眼", "倒吸", "怎么可能", "难道",
        "shock", "surprise", "stunned", "gasp", "unbelievable", "startle", "taken aback"
    ],
    "disgusted": [
        "厌恶", "鄙视", "嫌弃", "呸", "讨厌", "恶心", "皱眉", "冷漠", "白眼", "冷傲", "嘲弄", "令人作呕",
        "disgust", "hate", "revulsion", "frown", "scoff", "disdain", "roll eyes"
    ],
}

ROLE_MAPPING = {}

# ===================== 情绪信号分类体系 (语言/题材无关) =====================
# 通用原则: prompt 描述"如何思考",不描述"思考什么"。
# 不绑定任何具体小说的角色、台词、场景。

EMOTION_SIGNAL_TAXONOMY_CN = """
## 类别 1: 悲伤信号 (sadness)
- 体征类: 泪液/哽塞/视线模糊/呼吸不稳/眼眶红/嘴唇咬出血
- 姿态类: 蜷缩/低头/掩面/瘫坐/跪地/缩成一团
- 声学类: 颤抖/沙哑/断续/近乎气音/带哭腔/几不可闻
- 言语类: 自责/追忆/诀别/留恋/反复呢喃

## 类别 2: 愤怒信号 (anger)
- 体征类: 充血/绷紧/拳握/咬合/青筋暴起/脸色铁青
- 姿态类: 逼近/俯压/砸击/拍打/起身怒指/掀桌
- 声学类: 高亢/爆破/低吼/咆哮/嘶吼
- 言语类: 命令式/贬损/威胁/咒骂/质问

## 类别 3: 震惊信号 (surprise)
- 体征类: 瞳孔放大/呼吸停滞/僵在原地/血色褪去
- 姿态类: 后退一步/手中物坠落/猛地回头/起身
- 声学类: 倒吸气/失声/突然拔高/语无伦次
- 言语类: 反问/不可置信/"怎么会"/"什么"

## 类别 4: 恐惧信号 (fear)
- 体征类: 发抖/苍白/冷汗/瞳孔收缩/双腿发软
- 姿态类: 退缩/缩头/护住要害/连退数步/跌坐
- 声学类: 颤抖/破音/嗓音变细/语速急促
- 言语类: 求饶/否认/"别过来"/"求你了"

## 类别 5: 喜悦信号 (happy)
- 体征类: 眼睛发亮/嘴角上扬/脸颊泛红/松了一口气
- 姿态类: 雀跃/拥抱/拍手/小跑/扑过去
- 声学类: 轻快/上扬/明亮/带笑意
- 言语类: 赞叹/感谢/邀约/期待

## 类别 6: 厌恶信号 (disgust)
- 体征类: 皱眉/撇嘴/移开视线/嫌恶地擦手
- 姿态类: 后退/隔开距离/转身/挥手驱赶
- 声学类: 冷笑/嗤笑/拖长音/夹杂"啧"
- 言语类: 鄙夷/嘲讽/讽刺/价值贬低

## 类别 7: 平静信号 (calm)
- 默认基线: 缺乏上述任何信号时的状态
- 主动信号: 平稳/缓慢/松弛/呼吸均匀/语调平和

## 类别 8: 掩饰/反讽信号 (mask) - 关键!
触发"字面情绪反转"判断的复合信号:
- 矛盾共现: 负面言语 + 关怀姿态(凑过去/递东西/挡在身前)
- 矛盾共现: 负面言语 + 羞窘体征(耳根红/脸颊红/不敢直视)
- 矛盾共现: 凶恶言语 + 哭泣体征(眼泪/红眼眶/带哭腔)
- 收敛动作: 移开视线/转头/快速结束话题/低头/装作没事
- 言语自洽矛盾: 嘴上拒绝 + 行为接受(嘟囔着收下/边骂边塞)
- 强调词: 故作/强行/掩饰/装作/嘴硬
"""

EMOTION_SIGNAL_TAXONOMY_EN = """
## Category 1: Sadness signals
- Physiological: tears / choking / blurred vision / unsteady breath / red eyes / bitten lips
- Posture: curled up / head down / covering face / slumped / kneeling
- Acoustic: trembling / hoarse / broken / near-whisper / tearful tone
- Verbal: self-blame / reminiscence / farewell / longing / repeated murmuring

## Category 2: Anger signals
- Physiological: flushed / tensed / fists clenched / jaw locked / veins bulging
- Posture: stepping forward / looming / slamming / pointing accusingly / overturning
- Acoustic: raised / explosive / growling / roaring / snarling
- Verbal: commanding / belittling / threatening / cursing / interrogating

## Category 3: Surprise signals
- Physiological: pupils dilated / breath caught / frozen in place / blood drained
- Posture: stepping back / dropping objects / whipping head around
- Acoustic: gasping / voice cracking / sudden pitch jump / incoherent
- Verbal: rhetorical questions / disbelief / "how could" / "what"

## Category 4: Fear signals
- Physiological: shivering / pale / cold sweat / knees weak / pupils constricted
- Posture: shrinking / protecting vitals / retreating / collapsing
- Acoustic: trembling / cracking voice / thin / rapid
- Verbal: pleading / denial / "don't come closer" / "please"

## Category 5: Happiness signals
- Physiological: eyes brightening / smile / cheeks flushed / sigh of relief
- Posture: bouncing / hugging / clapping / running over / leaping
- Acoustic: light / rising / bright / with laughter
- Verbal: praise / thanks / invitation / anticipation

## Category 6: Disgust signals
- Physiological: frowning / curling lip / looking away / wiping hands in revulsion
- Posture: backing off / keeping distance / turning away / waving off
- Acoustic: sneering / scoffing / drawn-out tone / tsk
- Verbal: scorn / mockery / sarcasm / devaluation

## Category 7: Calm signals
- Default baseline when none of the above signals are present
- Active signals: steady / slow / relaxed / even breathing / level tone

## Category 8: Mask / Irony signals (CRITICAL!)
Triggers "literal-emotion reversal" inference:
- Contradiction: negative words + caring action (leaning in / handing over / shielding)
- Contradiction: negative words + embarrassment cues (red ears / flushed cheeks / avoiding gaze)
- Contradiction: harsh words + crying cues (tears / red eyes / tearful voice)
- Concealment: looking away / turning head / cutting conversation short / pretending nothing
- Verbal self-contradiction: refusing in words + accepting in action
- Markers: "pretending to" / "forcing oneself to" / "covering up" / "tough on the surface"
"""

EMOTION_TYPE_PLAYBOOK_CN = """
[角色情绪类型 → 判读策略]
- direct_expressive (情绪外露型): 字面情绪 ≈ 真实情绪,直接采信字面,无须额外推理
- tsundere_masked (嘴硬心软型): 字面 angry/disgusted + 类别8掩饰信号 → 真实情绪反转
  (关怀场景下反转为 sad,亲昵场景下反转为 happy)
- stoic_restrained (沉默克制型): 字面 calm 但伴随负面体征(攥拳/咬牙/低沉) → 真实通常是 sad
  (真情绪基本不外露,看动作不看语言)
- gentle_calm (温柔平和型): 以 calm 为基线,负面情绪的真实强度通常打折一档
- melodramatic (夸张戏剧型): 字面情绪强度 > 真实强度,适度降级一档
- cold_aloof (冷漠疏离型): 真实情绪基本不外露,disgusted/calm 为常态;
  伴随强烈体征信号才上升为其他情绪
- volatile_erratic (情绪不稳型): 切换快且无明显铺垫,以最近一个 narration 信号为准
"""

EMOTION_TYPE_PLAYBOOK_EN = """
[Character Emotion Type → Interpretation Strategy]
- direct_expressive: literal emotion ≈ real emotion, trust the surface
- tsundere_masked: literal angry/disgusted + Category 8 mask signals → real emotion reversed
  (sad in caring contexts, happy in intimate contexts)
- stoic_restrained: literal calm + negative physiological cues (fist clench / jaw lock / low voice)
  → real emotion usually sad. True feelings shown in action, not words.
- gentle_calm: calm is baseline; negative emotion intensity discounted by one level
- melodramatic: literal intensity > real intensity; downgrade by one level
- cold_aloof: emotions almost never surface. disgusted/calm is default;
  upgrade only on strong physiological signals
- volatile_erratic: rapid shifts without buildup; trust the most recent narration signal
"""

EMOTION_DECISION_TREE_CN = """
[情绪决策树 - 执行顺序: 从上到下,命中即停]
仅对 dialogue 和 nvl_thought 应用。narration/nvl_narration 强制 calm。

(1) 周边 narration 含"类别 1 悲伤信号"任一子类:
    无论字面如何,判 sad

(2) 周边 narration 含"类别 8 掩饰/反讽信号" 且 当前角色情绪类型 ∈
    {tsundere_masked, stoic_restrained, cold_aloof}:
    - 字面 angry/disgusted,关怀/亲昵场景下 → 反转为 sad 或 happy
    - 字面 calm + 负面体征 → 反转为 sad
    - 字面 calm + 羞窘体征 → 反转为 happy

(3) 周边 narration 含"类别 3 震惊信号": 判 surprised

(4) 周边 narration 含"类别 4 恐惧信号" 且 不在悲伤场景下: 判 fearful
    (注意: "颤抖"+寒冷场景=体感不计;+对峙场景=fearful;+哀伤场景=sad)

(5) 周边 narration 含"类别 2 愤怒信号" 且 不在关怀场景下: 判 angry
    (关怀场景下的"凶骂"通常是 sad 的反面表达)

(6) 周边 narration 含"类别 5 喜悦信号": 判 happy

(7) 周边 narration 含"类别 6 厌恶信号" 且 不在亲昵场景下: 判 disgusted

(8) 都不命中: 判 calm

[关键原则]
- "字面词义" 永远不是单独依据,必须有 narration 信号印证或反驳
- 同一信号在不同场景性质下含义不同
  场景性质分类: 冲突 / 关怀 / 亲昵 / 独处 / 对峙 / 离别 / 重逢
- 角色情绪类型 (emotion_type) 是先验,决定"字面"与"真实"的转换关系
"""

EMOTION_DECISION_TREE_EN = """
[Emotion Decision Tree - top to bottom, first match wins]
Applies ONLY to dialogue and nvl_thought. narration/nvl_narration forced to calm.

(1) Surrounding narration contains any Category 1 (sadness) signal:
    Regardless of literal words, judge sad

(2) Surrounding narration contains Category 8 (mask) signals AND
    speaker emotion_type ∈ {tsundere_masked, stoic_restrained, cold_aloof}:
    - Literal angry/disgusted in caring/intimate scene → reverse to sad or happy
    - Literal calm + negative physiological cues → reverse to sad
    - Literal calm + embarrassment cues → reverse to happy

(3) Surrounding narration contains Category 3 (surprise) signal: judge surprised

(4) Surrounding narration contains Category 4 (fear) signal AND not in sad scene: judge fearful
    (note: "trembling" + cold scene = somatic, not emotion;
     + confrontation = fearful; + grief scene = sad)

(5) Surrounding narration contains Category 2 (anger) signal AND not in caring scene: judge angry
    (harsh words in caring contexts usually mask sad)

(6) Surrounding narration contains Category 5 (happiness) signal: judge happy

(7) Surrounding narration contains Category 6 (disgust) signal AND not in intimate scene: judge disgusted

(8) None match: judge calm

[Key Principles]
- Literal word meaning is NEVER sufficient alone; narration must confirm or refute
- The same signal carries different meaning depending on scene type
  Scene types: conflict / caring / intimate / solitary / standoff / parting / reunion
- emotion_type is a prior that determines literal-to-real conversion rules
"""

# ===================== 系统文面板识别 (system_panel) =====================
# 系统文常见的多行键值面板,例如:
#   "宿主：徐缺
#    境界：凡人（手无缚鸡之力）
#    经验值：0/100
#    装逼值：1点
#    ……"
# 这种块结构稳定,直接用正则在 LLM 介入前抠出来,避免被拆碎成 narration。

PANEL_KV_LINE_RE = re.compile(
    r'^["\u201c\u201d\u2018\u2019\'「]?\s*([^：:\n]{1,15})\s*[：:]\s*(.+?)\s*["\u201c\u201d\u2018\u2019\'」]?\s*$')

# 触发关键词:面板的"键"中包含这些词之一,才认定该 KV 序列是面板。
# 数值/状态/资源型词条优先,避免普通对话里"老板：来碗面"这种被误判。
PANEL_TRIGGER_KEYWORDS = {
    "宿主", "境界", "等级", "经验值", "经验", "属性", "血量", "气血", "法力", "灵力",
    "装逼值", "功法", "职业", "地位", "称号", "天赋", "灵根", "修为",
    "生命", "生命力", "状态", "状態", "积分", "声望", "贡献", "魅力",
    "力量", "敏捷", "智力", "体质", "精神",
    # 通用游戏化属性常见键
    "level", "lv", "hp", "mp", "exp",
}

# 面板的省略号/分隔符尾巴,允许出现在面板内部
_PANEL_ELLIPSIS_RE = re.compile(
    r'^["\u201c\u201d\u2018\u2019\'「」]*(\.\.\.|……|…|—+|-+)["\u201c\u201d\u2018\u2019\'「」]*$')


def _split_text_block_by_panels(text_content):
    """扫描一段 text 块,把里面的系统面板抠出来。

    返回 [{type, content}, ...]:
      - 没有面板时:[{"type": "text", "content": <原文>}]
      - 有面板时:按顺序产出 [text...][system_panel][text...] 多个块

    检测口径(保守):
      1. 连续 >=3 行匹配 "键：值" 形态
      2. 其中至少 1 个键命中 PANEL_TRIGGER_KEYWORDS
      3. 允许首尾出现引号/省略号
    """
    if not text_content or not text_content.strip():
        return [{"type": "text", "content": text_content}]

    lines = text_content.split('\n')
    panels = []  # 收集 (start_idx, end_idx_inclusive)

    i = 0
    n = len(lines)
    while i < n:
        # 跳过空行,但记住可能的面板起点
        if not lines[i].strip():
            i += 1
            continue

        consecutive = []  # 当前累积的面板候选行(原始 line index)
        has_trigger = False
        j = i

        while j < n:
            raw = lines[j].strip()
            if not raw:
                # 空行直接结束面板尝试
                break

            # 检查是否是省略号尾巴(必须已经有累积内容才接受)
            if _PANEL_ELLIPSIS_RE.match(raw):
                if consecutive:
                    consecutive.append(j)
                    j += 1
                    continue
                else:
                    break

            m = PANEL_KV_LINE_RE.match(raw)
            if not m:
                break

            key = m.group(1).strip().lower()
            # 命中触发词(允许键里包含触发词,如"当前生命力状态")
            if any(t in key or t == key for t in PANEL_TRIGGER_KEYWORDS):
                has_trigger = True

            consecutive.append(j)
            j += 1

        # 面板成立条件:>=3 行 KV + 至少 1 个触发词
        if len(consecutive) >= 3 and has_trigger:
            panels.append((consecutive[0], consecutive[-1]))
            i = consecutive[-1] + 1
        else:
            i += 1

    if not panels:
        return [{"type": "text", "content": text_content}]

    # 切片重组
    result = []
    cursor = 0
    for start, end in panels:
        if start > cursor:
            pre = '\n'.join(lines[cursor:start]).strip()
            if pre:
                result.append({"type": "text", "content": pre})
        panel_lines = lines[start:end + 1]
        # 清掉首尾的孤立引号(纯引号行 / 引号被单独留下)
        panel_text = '\n'.join(panel_lines)
        # 去掉首尾引号包裹(保留行内)
        panel_text = panel_text.strip()
        panel_text = re.sub(r'^["\u201c\u201d\u2018\u2019\'「]+', '', panel_text)
        panel_text = re.sub(r'["\u201c\u201d\u2018\u2019\'」]+$', '', panel_text)
        panel_text = panel_text.strip()
        if panel_text:
            result.append({"type": "system_panel", "content": panel_text})
        cursor = end + 1

    if cursor < len(lines):
        rest = '\n'.join(lines[cursor:]).strip()
        if rest:
            result.append({"type": "text", "content": rest})

    return result


def _flush_text_buffer(blocks, current_text, current_line_indices=None):
    """把累积的 current_text 行展平到 blocks,顺带把系统面板切出去。原地清空 current_text。

    current_line_indices:与 current_text 一一对应的源行号(0-based)。
    用于给每个产出 block 标注 src_start_line / src_end_line,供下游 _src_lines 对齐。
    面板切分会把一个 text 块拆成多个子块,这些子块统一继承父块的行号区间
    (块级粒度对齐,面板少见,足够下游 scenes 阶段使用)。
    """
    if not current_text:
        return
    if current_line_indices:
        src_start = current_line_indices[0]
        src_end = current_line_indices[-1]
    else:
        src_start, src_end = -1, -1
    joined = "\n".join(current_text)
    for sub in _split_text_block_by_panels(joined):
        sub["src_start_line"] = src_start
        sub["src_end_line"] = src_end
        blocks.append(sub)
    current_text.clear()
    if current_line_indices is not None:
        current_line_indices.clear()


def _natural_key(s):
    parts = re.split(r'(\d+)', s)
    return [int(p) if p.isdigit() else p for p in parts]


def fetch_source_context(target_task_id=None):
    """获取提取剧情所需的全部上下文"""

    with global_engine.connect() as conn:
        task_id = target_task_id
        if not task_id:
            logger.info("🔍 未指定 task_id，查找最新任务...")
            sql = sa_text("SELECT task_id FROM story_tree_md ORDER BY id DESC LIMIT 1")
            res = conn.execute(sql).fetchone()
            if not res:
                logger.error("❌ story_tree_md 表为空")
                return None, None, None
            task_id = res[0]

        logger.info(f"🚀 锁定任务 Task ID: {task_id}")

        sql_char = sa_text("SELECT data FROM characters WHERE task_id = :tid")
        row_char = conn.execute(sql_char, {"tid": task_id}).fetchone()

        char_data = {}
        if row_char:
            char_data = row_char[0]
            logger.info(f"✅ 成功加载角色库: 包含 {len(char_data)} 个角色")
        else:
            logger.warning("⚠️ 警告: 未找到对应的角色数据。")

        sql_md = sa_text("SELECT name, data FROM story_tree_md WHERE task_id = :tid")
        rows_md = conn.execute(sql_md, {"tid": task_id}).fetchall()

        if not rows_md:
            logger.error(f"❌ 未找到 MD 内容 (TaskID: {task_id})")
            return None, None, None

        md_contents = {row[0]: row[1] for row in rows_md}
        logger.info(f"📦 加载剧情文本: {len(md_contents)} 个章节")

        return task_id, char_data, md_contents


class PlotsExtractionError(RuntimeError):
    """plots 未产出或未落地。

    plots 是后续 14 步的根数据，缺了它整条流水线只能空转，因此这是唯一
    需要硬中断的步骤：宁可早失败，也不要在库里留一个缺 plots 的半成品任务。
    """


def save_to_plots_table(task_id, plots_data, cg_annotations=None):
    """原子写入 plots + CG 草稿，返回 (db_saved, local_saved)。"""
    if not plots_data:
        return False, False

    try:
        with global_engine.begin() as conn:
            sql = sa_text("""
                          INSERT INTO plots (task_id, data, create_time, update_time)
                          VALUES (:tid, :data, NOW(), NOW()) ON CONFLICT (task_id) DO
                          UPDATE SET
                              data = EXCLUDED.data,
                              update_time = NOW()
                              RETURNING id
                          """)
            result = conn.execute(sql, {
                "tid": task_id,
                "data": json.dumps(plots_data, ensure_ascii=False)
            })

            if cg_annotations is not None:
                upsert_cg_data(conn, task_id, cg_annotations)

            row = result.fetchone()
            if row is None:
                raise RuntimeError("[Plots] RETURNING id 未返回任何行")
            logger.info(f"💾 [Plots] 入库成功! DB_ID: {row[0]}")
            if cg_annotations is not None:
                logger.info(
                    "💾 [CG] 标注草稿与 plots 原子入库成功，共 %s 条",
                    len(cg_annotations),
                )
            return True, False
    except Exception as e:
        logger.error(f"❌ [Plots] 入库失败: {e}")

    # 入库没成功：plots 和 CG 草稿必须作为一组共享存储
    # 合同落盘。不能只保存已删除 cg_visual_entities 的 plots.json。
    try:
        fallback_dir = os.path.join(OUTPUTS_PATH, str(task_id))
        os.makedirs(fallback_dir, exist_ok=True)
        payloads = {"plots.json": plots_data}
        if cg_annotations is not None:
            payloads["cg.json"] = cg_annotations
        written_paths = []
        for filename, payload in payloads.items():
            fallback_path = os.path.join(fallback_dir, filename)
            temporary_path = f"{fallback_path}.{os.getpid()}.tmp"
            try:
                with open(temporary_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temporary_path, fallback_path)
            finally:
                if os.path.exists(temporary_path):
                    os.unlink(temporary_path)
            written_paths.append(fallback_path)
        logger.info(
            "📁 [Plots/CG] 已降级保存到共享存储: %s",
            written_paths,
        )
        return False, True
    except Exception as fe:
        logger.error(f"❌ [Plots] 本地降级保存也失败: {fe}")
        return False, False


# ================= 序幕处理与数据库更新 =================
def process_hook_section(raw_text):
    """解析序幕部分，清除【】标签，强制转为旁白"""
    lines = raw_text.split('\n')
    hook_beats = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith('#'):
            continue

        clean_line = re.sub(r'【.*?】[：:]?\s*', '', line)
        clean_line = clean_line.strip('""\'\'""''')

        if not clean_line:
            continue

        hook_beats.append({
            "is_hook": True,
            "beat_type": "narration",
            "speaker_name": "旁白",
            "speaker_id": "PangBai",
            "content": clean_line,
            "emotion": "calm",
            "_src_lines": [-1, -1],  # hook beats 不参与场景对齐,占位
        })
    return hook_beats


def update_story_tree_hook_id(task_id, hook_id):
    """将提取到的 hook_id 更新到 story_tree_json 表中（只 UPDATE）"""
    try:
        with global_engine.begin() as conn:
            row = conn.execute(
                sa_text(
                    "SELECT data FROM story_tree_json "
                    "WHERE task_id = :task_id LIMIT 1"
                ),
                {"task_id": task_id},
            ).fetchone()
            if not row:
                logger.warning(f"⚠️ 未找到 task_id: {task_id}，hook_id 更新可能被忽略！")
                return

            data = row[0]
            if isinstance(data, (str, bytes, bytearray)):
                data = json.loads(data)
            if not isinstance(data, dict):
                logger.warning(f"⚠️ task_id={task_id} 的 story_tree_json 不是对象，跳过 hook_id 更新")
                return

            # 新格式把内部字段放在每个完整 locale tree 内；历史扁平格式
            # 继续写在树顶层。publication 只保存发布校验信息，绝不写入其中。
            updated = update_story_tree_field(data, "hook_id", hook_id)
            locale_count = sum(
                1
                for locale, locale_tree in updated.items()
                if (
                    locale != "publication"
                    and isinstance(locale_tree, dict)
                    and isinstance(locale_tree.get("nodes"), dict)
                )
            )

            result = conn.execute(
                sa_text(
                    "UPDATE story_tree_json SET data = CAST(:data AS jsonb), "
                    "update_time = now() WHERE task_id = :task_id"
                ),
                {
                    "data": json.dumps(updated, ensure_ascii=False),
                    "task_id": task_id,
                },
            )
            if result.rowcount > 0:
                logger.info(
                    f"✅ 成功将 hook_id ({hook_id}) 更新至 story_tree_json, "
                    f"task_id: {task_id}, locale_count={locale_count or 1}"
                )
            else:
                logger.warning(f"⚠️ 未找到 task_id: {task_id}，hook_id 更新可能被忽略！")
    except Exception as e:
        logger.error(f"❌ 更新 hook_id 失败: {e}")


# ============ 泛称黑名单 ============
# 这些词在上游角色提取阶段可能被误标为 other_names,
# 但实际上是泛指(可指代多个角色),不应作为任何具体角色的别名。
# 命中黑名单的 alias 会被静默跳过,不进入 prompt,避免无意义的消歧义警告污染模型注意力。
GENERIC_TITLE_BLACKLIST_CN = {
    # 小孩泛称
    "小羊", "小崽子", "小子", "小娃", "小娃子", "孩子", "娃子", "黄毛小儿", "黄口小儿",
    "丫头", "丫头片子", "小丫头", "姑娘", "小姑娘",
    # 成人泛称
    "男人", "女人", "男子", "女子", "汉子", "壮汉",
    # 礼节性敬称(必须结合上下文才能确定指谁)
    "小姐", "公子", "先生", "夫人", "大人", "老爷", "少爷", "大爷", "客官",
    "老婆婆", "老婆子", "老头", "老头子", "老人", "老人家", "老大爷", "老大娘",
    # 代词类
    "他", "她", "它", "你", "我", "咱", "俺",
    "那人", "此人", "那个人", "这个人", "某人", "他人",
    "对方", "众人", "大家", "诸位",
    # 其他常见泛指
    "路人", "陌生人", "来人", "身影", "影子",
    "未知", "无名氏", "无名",
}

GENERIC_TITLE_BLACKLIST_EN = {
    # children
    "boy", "girl", "kid", "child", "lad", "lass", "youngster",
    # adults
    "man", "woman", "guy", "gal", "fellow", "person",
    # honorifics
    "mister", "mr", "mrs", "miss", "madam", "sir", "lady", "lord",
    "master", "young master",
    # pronouns
    "he", "she", "it", "they", "you", "i", "me",
    "him", "her", "them", "us",
    # generic
    "stranger", "someone", "anyone", "everyone", "everybody",
    "the man", "the woman", "the boy", "the girl",
    "passerby", "figure", "silhouette", "shadow",
    "unknown", "no name", "noname",
}


# ============ 锚定型关系称呼识别 ============
# "锚定型关系称呼" = <具名角色> + 的 + <关系/身份词>,如"凌妙妙的丫鬟""张三的妈妈"。
# 与"漂浮描述短语"(如"御剑飞天的女子""穿白衣的男子")的本质区别:
#   - 锚定型:左侧是一个真实存在的具名角色,整个称呼稳定指向"某具体人的某个下属/亲属",
#     跨章节不漂移,是小说家给无名角色做区分、**避免**混淆的正规手段 → 应作为独立身份保留。
#   - 漂浮型:左侧是外貌/动作描述,同一说法在不同章节可能指不同人 → 必须过滤。
# 判据:靠 _KNOWN_CHARACTER_NAMES(角色库主名集合)判断锚点是否真实存在。
#
# 关系/身份词表(family + 下人随从 + 师门 + 亲属),补齐 GENERIC_TITLE_SUFFIX_CN
# 缺失的"妈妈/爸爸/父亲/母亲"及仆从类词汇。
_RELATIONAL_NOUNS_CN = {
    # 直系/旁系亲属
    "爸爸", "妈妈", "父亲", "母亲", "爹", "娘", "爹爹", "娘亲",
    "爷爷", "奶奶", "外公", "外婆", "姥姥", "姥爷",
    "哥哥", "姐姐", "弟弟", "妹妹", "兄长", "大哥", "大姐",
    "叔叔", "阿姨", "伯伯", "舅舅", "婶婶", "姑姑", "姑妈", "姨妈", "舅妈",
    "表哥", "表姐", "表弟", "表妹", "堂哥", "堂姐",
    "儿子", "女儿", "孙子", "孙女", "侄子", "侄女", "外甥",
    "丈夫", "妻子", "老公", "老婆", "夫君", "娘子", "未婚妻", "未婚夫",
    # 下人/随从/部属
    "丫鬟", "丫头", "侍女", "侍从", "书童", "家丁", "管家", "护卫", "保镖",
    "随从", "手下", "部下", "下属", "仆人", "佣人", "婢女", "小厮", "车夫",
    "门客", "亲信", "心腹", "跟班", "副手", "属下",
    # 师门
    "师父", "师傅", "师兄", "师姐", "师弟", "师妹", "徒弟", "弟子", "门徒",
    "师叔", "师伯", "师祖",
}

_RELATIONAL_NOUNS_EN = {
    "father", "mother", "dad", "mom", "papa", "mama",
    "grandfather", "grandmother", "grandpa", "grandma",
    "brother", "sister", "uncle", "aunt", "cousin", "nephew", "niece",
    "son", "daughter", "grandson", "granddaughter",
    "husband", "wife", "fiance", "fiancee",
    "maid", "servant", "butler", "guard", "bodyguard", "attendant",
    "subordinate", "follower", "page", "valet", "coachman", "driver",
    "master", "mentor", "teacher", "disciple", "apprentice", "student",
}


def _is_anchored_relational(alias):
    """判断称呼是否为"锚定型关系称呼"(<具名角色>的<关系/身份词>)。

    命中条件(中文):含"的",按最右侧"的"切分,左段精确命中 _KNOWN_CHARACTER_NAMES,
    右段以 _RELATIONAL_NOUNS_CN 中任一词结尾。
    命中条件(英文):含 "'s",左段命中已知角色名,右段命中 _RELATIONAL_NOUNS_EN。

    返回 True 表示这是"某具体角色的下属/亲属",是稳定低歧义的身份,应作为独立
    身份保留(_has_descriptive_pattern 对其豁免)。但它仍不可作为某 VIP 的别名
    (_is_generic_title 仍会拦截),否则会把下属/亲属的台词错并到锚点角色头上。
    """
    if not alias:
        return False
    a = alias.strip()
    if not a:
        return False
    if not _KNOWN_CHARACTER_NAMES:
        # 未加载已知角色名(如单元测试或上游缺数据)→ 无法确认锚点真实性,
        # 保守返回 False,交回原有描述性/泛指规则处理。
        return False

    # ---- 中文:<角色>的<关系词> ----
    if "的" in a:
        idx = a.rfind("的")
        left = a[:idx].strip()
        right = a[idx + 1:].strip()
        if left in _KNOWN_CHARACTER_NAMES and right:
            for noun in _RELATIONAL_NOUNS_CN:
                if right.endswith(noun):
                    return True

    # ---- 英文:<Name>'s <relational> ----
    m = re.match(r"^(.*?)['’]s\s+(.+)$", a, flags=re.IGNORECASE)
    if m:
        left = m.group(1).strip()
        right = m.group(2).strip().lower()
        if left in _KNOWN_CHARACTER_NAMES:
            last_word = re.findall(r"[a-z]+", right)
            if last_word and last_word[-1] in _RELATIONAL_NOUNS_EN:
                return True

    return False


def _is_generic_title(alias):
    """判断别名是否不可靠(应被丢弃,不进 role_mapping,不进 prompt)。

    设计哲学(通用小说提取,不依赖具体小说词汇):
    =========================================
    1. 上游角色提取经常把"描述性别名"(如"御剑飞天的女子"、"白衣少年")错绑给某个角色。
       这类别名在不同章节、不同场景下可能指向不同人,作为映射喂给 LLM 会造成
       系统性的台词错配(典型案例:章节 A 的"仙子"指 X,章节 B 的"仙子"指 Y)。
    2. 解决之道**不是**穷举黑名单,而是**识别描述性模式**:
       含"的"字结构、含动作/外貌修饰、以泛指身份词结尾的别名,一律视为不可靠。
    3. 真昵称(如"小明"、"老张"、"赵姐姐")在严格规则下可能被误伤,
       但**让模型从原文判断归属**,比模型被错误别名误导**更安全**。
       宁可让模型多想一秒,也不要让它一秒钟做出错误归属。

    返回 True 表示别名不可靠,应被丢弃。
    """
    if not alias:
        return True
    a = alias.strip()
    if not a:
        return True

    a_lower = a.lower()

    # 1. 精确黑名单(代词、独立的通用身份词)
    if a_lower in GENERIC_TITLE_BLACKLIST_CN or a_lower in GENERIC_TITLE_BLACKLIST_EN:
        return True

    # 1.5 锚定型关系称呼("凌妙妙的丫鬟"):作为**别名**不可靠 —— 下人/亲属不是
    #     锚点角色本人,若把它当作某 VIP 的别名映射,会把下人的台词错并到锚点头上。
    #     所以这里仍返回 True(逐出别名表)。注意:_has_descriptive_pattern 对它豁免,
    #     使它能作为**独立 NPC 身份**在主名/出口闸门处存活;两处口径分工不同,不矛盾。
    if _is_anchored_relational(a):
        return True

    # 1.6 群体称呼("同事们""众弟子""三名老者"):指向多人,不是任何个体的身份
    if _is_collective_reference(a):
        return True

    # 2. 描述性短语模式("X的Y"、含比喻/动作/服饰/指示词)
    if _has_descriptive_pattern(a):
        return True

    # 3. 以泛指身份词为尾(无论前缀是什么)
    #    例:"小仙女"、"美丽姑娘"、"老婆婆"、"年轻男子"、"那名少女"
    if _ends_with_generic_title(a):
        return True

    return False


# ============ 描述性别名识别(语言/题材无关) ============
# 中文描述性短语的语言学特征
_DESCRIPTIVE_MARKERS_CN = (
    "的",  # X的Y 结构(穿白衣的男人)
    "般", "似", "如",  # 比喻(仙女般的女子)
    "穿", "戴", "披", "持", "握", "抱", "佩", "挂",  # 服饰/动作修饰
    "身着", "身穿", "手持", "腰佩",  # 复合修饰
    "那名", "那位", "那个", "这名", "这位", "这个", "某个", "某名",  # 指示词
    "看上去", "看起来", "貌似", "似乎",  # 推测词
    "约莫",  # 描述年龄/外貌
)

# 泛指身份词尾(中文) - 别名以这些词结尾时,大概率是描述性别名
# 不需要罗列所有可能性,覆盖最常见的题材身份即可。
GENERIC_TITLE_SUFFIX_CN = {
    # 女性身份
    "仙女", "仙子", "神女", "妖女", "魔女", "女神", "女仙",
    "美人", "佳人", "丽人", "尤物", "美女",
    "姑娘", "女子", "女人", "少女", "丫头", "丫鬟", "侍女",
    "小姐", "夫人", "太太", "娘子", "公主", "王妃", "贵妃",
    # 男性身份
    "男子", "男人", "汉子", "壮汉", "男孩", "少年", "青年",
    "公子", "少爷", "相公", "郎君", "书生", "秀才",
    # 老幼
    "老者", "老人", "老头", "老妇", "老婆婆", "老婆子", "老太婆", "老太太",
    "孩子", "娃子", "崽子", "小子", "小儿", "婴孩",
    # 通用称谓
    "大人", "老爷", "客官", "客人",
    # 修行/宗门类(常见于仙侠/玄幻)
    "师父", "师傅", "师兄", "师姐", "师弟", "师妹",
    "师叔", "师伯", "师爷", "师祖",
    "长老", "长辈", "前辈", "晚辈", "弟子", "门人", "宗主",
    # 亲属(相对称谓,稳定性差)
    "哥哥", "姐姐", "弟弟", "妹妹",
    "叔叔", "阿姨", "伯伯", "舅舅", "婶婶",
    "爷爷", "奶奶", "外公", "外婆", "姑姑",
    # 通用泛指
    "路人", "陌生人", "外人", "众人", "旁人",
    # 贬称(泛指,可能指多人)
    "贼人", "鼠辈", "宵小", "废物", "蝼蚁",
}

# 英文泛指身份词尾
GENERIC_TITLE_SUFFIX_EN = {
    "man", "woman", "boy", "girl", "kid", "child", "lad", "lass",
    "lady", "gentleman", "fellow", "guy", "gal",
    "stranger", "person", "people", "figure", "shadow", "silhouette",
    "master", "mistress", "lord", "sir", "madam",
    "elder", "senior", "junior", "disciple",
    "brother", "sister", "uncle", "aunt",
    "warrior", "soldier", "guard", "knight",
}


# 名字里的"结构标记":它们把字数撑大,但本身不是描述性修饰。
#   - 间隔号:音译全名的名/姓分隔符("林雷·巴鲁克""艾丽斯·达夫")
#   - 尾部单个大写字母:上游给同身份群演的序号("高年级学员A""同事B")
# 间隔号字符集与 common.generic_appellations 共用:那里按同一判据切出家族名段供
# 换名使用,两处若漂移,同一个名字会在一处被当人名、在另一处被当描述短语。
_INTERPUNCT_RE = INTERPUNCT_RE
_SERIAL_SUFFIX_RE = re.compile(r"(?<=[一-龥])[A-Z]$")


def _descriptive_length_core(alias):
    """返回参与"长度兜底"判断的名字主体,剥掉结构标记。

    长度兜底的本意是拦"御剑飞天的女子"这类漂浮描述短语。但音译全名与序号型
    群演名天然超过 6 字,被误判后该角色会被逐出 role_mapping 与
    canonical_names —— 它的台词只能退到短别名上另立身份,全名的姓氏段就此丢失,
    并在 name_mappings 里与真主名分裂成两条记录。
    """
    core = _SERIAL_SUFFIX_RE.sub("", alias)
    parts = [p for p in _INTERPUNCT_RE.split(core) if p]
    if 2 <= len(parts) <= 3 and all(len(p) <= 4 for p in parts):
        # 音译全名:按最长的一段判长度,整体字数不参与。
        return max(parts, key=len)
    return core


def _has_descriptive_pattern(alias):
    """检测别名是否含描述性短语模式。

    判定逻辑:
    - 中文:含上方任一描述性标志词,或长度 >= 6 字(过长大概率是描述短语)
    - 英文:多于 2 个单词,或以冠词开头,或含介词修饰(with/in/wearing)
    """
    a = alias.strip()
    if not a:
        return False

    # 锚定型关系称呼("凌妙妙的丫鬟""张三的妈妈")豁免:虽含"的",但锚定在
    # 具名角色上,指向稳定,是合法独立身份,不算漂浮描述短语。
    if _is_anchored_relational(a):
        return False

    has_chinese = bool(re.search(r'[\u4e00-\u9fa5]', a))
    if has_chinese:
        # 任意标志词命中即视为描述性
        for marker in _DESCRIPTIVE_MARKERS_CN:
            if marker in a:
                return True
        # 长度兜底:中文 ≥6 字且不含真名结构标记(间隔号/序号字母)的,大概率是描述。
        # 这一兜底会误伤一些 6 字以上的复合姓名(如"欧阳无极"),
        # 但通用提取的安全口径优先,真长名通常会在 name 字段而非 other_names
        if len(_descriptive_length_core(a)) >= 6:
            return True
        return False

    # 英文判定
    a_lower = a.lower()
    words = a_lower.split()
    if not words:
        return False
    if len(words) > 2:
        return True
    if words[0] in {"the", "a", "an"}:
        return True
    if any(p in words for p in ("with", "in", "wearing", "of", "from")):
        return True
    return False


def _ends_with_generic_title(alias):
    """检测别名是否以泛指身份词结尾。

    例:"小仙女"(尾"仙女")、"美丽姑娘"(尾"姑娘")、"那个老婆婆"(尾"老婆婆")
    """
    a = alias.strip()
    if not a:
        return False

    has_chinese = bool(re.search(r'[\u4e00-\u9fa5]', a))
    if has_chinese:
        # 任意尾词命中即视为以泛指身份词结尾
        for suffix in GENERIC_TITLE_SUFFIX_CN:
            if a.endswith(suffix) and len(a) > len(suffix):
                # 长度严格大于尾词长度,避免与黑名单精确匹配段重复
                return True
            if a == suffix:
                # 别名本身就是泛指词(如别名 == "仙女"),已被前置黑名单覆盖,
                # 这里作为兜底也返回 True
                return True
        return False

    # 英文:按词切,看最后一个 token
    parts = re.findall(r'[A-Za-z]+', a)
    if parts:
        last_word = parts[-1].lower()
        if last_word in GENERIC_TITLE_SUFFIX_EN:
            return True
    return False


# ============ 群体称呼识别(语言/题材无关) ============
# "一个提示语带 N 条连续台词,这 N 条其实出自群体中 N 个不同的人" 是跨语种、跨题材的
# 通用叙事手法("同事们猜测道:""众人纷纷道""七嘴八舌""voices chimed in")。
# 模型忠于原文表层称呼时会产出"同事们"这类群体标签,而它不是任何**个体**的身份:
#   - 作为 full_name 会让 N 个人共用一个 speaker_id → 下游按 id 分配音色时 N 人共一档,
#     且输出里会出现"某句台词 + 对该句的附和"同属一个 speaker 的逻辑矛盾。
#   - 角色库里也不会有这条记录 → 音色映射缺档。
# 因此群体称呼必须在别名表与 full_name 两道闸门同时拦下,与 reconcile prompt 里
# "群体名词永远 keep,不可合并到任何具体个体" 的口径保持一致。
_COLLECTIVE_WORDS_EN = {
    "crowd", "crowds", "everyone", "everybody", "all", "group", "groups",
    "others", "rest", "voices", "villagers", "soldiers", "disciples",
    "students", "colleagues", "guests", "customers", "onlookers",
    "bystanders", "audience", "mob", "throng", "chorus", "several", "many",
    "both", "few", "some", "couple", "pair", "trio", "team", "party",
}

_COLLECTIVE_WORDS_CN = {
    "大家", "众人", "诸位", "各位", "人们", "旁人", "旁人们", "其余人", "其他人",
}

# 明确的集体前缀:"众弟子""诸同门""一群村民""一帮混混"
_COLLECTIVE_PREFIX_RE_CN = re.compile(r'^(?:[众诸群]|一群|一帮|一伙|一众|一干)')

# 单字集体前缀(众/诸/群)也可能是复姓或真名的首字("诸葛亮"),这类必须豁免。
_COLLECTIVE_PREFIX_EXEMPT_CN = ("诸葛",)

# 数量词前缀:必须"数词 + 量词"成对出现才算群体。
# 只看数词会误伤真名("三生""七夜""九歌"),量词是关键判据。
_COLLECTIVE_QUANT_RE_CN = re.compile(
    r'^(?:[几数两二三四五六七八九十百千万多]|十几|几十|好几)+[名个位群批帮伙人]'
)


def _is_collective_reference(name):
    """判断称呼是否指向"一群人"而非某个个体。

    中文判据:
      1. 以"们"结尾,且去掉"们"后不是已知具名角色(避免误伤本身叫"我们"之类的怪名,
         也避免把具名角色的合法称呼当群体)。
      2. 命中数量前缀(众/诸/一群/三名/几个…)且前缀之后仍有内容。
    英文判据:含 _COLLECTIVE_WORDS_EN 中的整词,或以 "s" 结尾的复数身份词组合。

    返回 True 表示这是群体称呼,不可作为个体的 full_name 或别名。
    """
    if not name:
        return False
    a = name.strip()
    if not a:
        return False

    has_chinese = bool(re.search(r'[一-龥]', a))
    if has_chinese:
        if a in _COLLECTIVE_WORDS_CN:
            return True
        if a.endswith("们") and len(a) > 1:
            # "他们/她们/同事们/业主们" 一律群体。原文用"们"就是在指多人。
            return True
        if a.startswith(_COLLECTIVE_PREFIX_EXEMPT_CN):
            return False
        for pattern in (_COLLECTIVE_PREFIX_RE_CN, _COLLECTIVE_QUANT_RE_CN):
            if pattern.match(a):
                # 前缀之后必须还有身份词,"三名"这种光杆前缀不算。
                # 且剩余部分至少 2 字:"众生""群山"这类是词的一部分而非"众+身份词"。
                rest = pattern.sub("", a, count=1).strip()
                # 剩余部分需 ≥2 字才算"前缀 + 身份词";"众生""群山"这类单字尾
                # 是词本身的一部分,不是群体。例外:"人"("好几个人""三个人")。
                if len(rest) >= 2 or rest == "人":
                    return True
        return False

    # 英文/罗马字:PascalCase 也要能切开("TheColleagues" → the, colleagues)
    words = [w.lower() for w in re.findall(r'[A-Z][a-z]*|[a-z]+', a)]
    if not words:
        return False
    if any(w in _COLLECTIVE_WORDS_EN for w in words):
        return True
    # 复数身份词尾:去掉尾 s 后命中泛指身份词("Disciples""Guards")
    last = words[-1]
    if last.endswith("s") and last[:-1] in GENERIC_TITLE_SUFFIX_EN:
        return True
    return False


# ============ 序号型群演系列识别 ============
# 上游 extract_deduplicate 的命名规范(prompts/char_prompts/
# extract_deduplicate_simplify_names_sys.yaml):同一身份的匿名群演若有多个,
# 按输入顺序用大写字母 A/B/C… 区分("同事A""业主B""顾客C")。
# 字母只表示输入顺序,不携带语义 —— 能区分它们的只有各自的 〔简介〕。
# 所以当同一批群演在**新场景**里说新台词时,任何一条简介都不会吻合,
# 铁律 11 的证据 (D) 必然不成立,模型只能退回 NPC 路径。
# 这不是模型犯错,而是提示词缺了"续编号"这条出路:新出现的同类匿名角色应从
# 该系列下一个可用字母继续排(同事A~D 已占用 → 新的是 同事E/F/G),
# 这样既延续上游规范,下游也能拿到 N 个独立音色档位。
_SERIAL_NAME_RE = re.compile(r'^(.{1,8}?)([A-Z])$')


def collect_serial_series(canonical_names):
    """从角色库主名集合中识别"身份名 + 单个大写字母"的序号型群演系列。

    返回 {身份名: 已占用字母有序列表},如 {"同事": ["A","B","C","D"], "业主": ["A","B","C"]}。
    仅收集真正成系列的(至少 2 个成员);单个"角色A"更可能是巧合而非系列。
    """
    series = {}
    for nm in canonical_names or ():
        if not nm:
            continue
        m = _SERIAL_NAME_RE.match(nm.strip())
        if not m:
            continue
        base, letter = m.group(1), m.group(2)
        if not base:
            continue
        series.setdefault(base, set()).add(letter)

    return {
        base: sorted(letters)
        for base, letters in series.items()
        if len(letters) >= 2
    }


def _next_serial_letter(used_letters):
    """给出该系列下一个可用大写字母;A-Z 用尽返回 None。"""
    used = set(used_letters or ())
    for code in range(ord("A"), ord("Z") + 1):
        ch = chr(code)
        if ch not in used:
            return ch
    return None


_CHARACTER_INFO_MAX_LEN = 80


def _clean_character_info(raw_info):
    """把角色简介压成可安全拼进 VIP 列表的一行。

    角色列表用 " | " 分隔、别名用 "," 分隔,简介里的分隔符和换行会打乱结构,
    统一替换掉。过长的简介会挤占正文注意力,按句截断到 80 字以内。
    """
    if not raw_info or not isinstance(raw_info, str):
        return ""

    text = re.sub(r"\s+", " ", raw_info).strip()
    text = text.replace("|", "／").replace("〔", "(").replace("〕", ")")
    if not text:
        return ""

    if len(text) <= _CHARACTER_INFO_MAX_LEN:
        return text

    # 优先在句末标点处截断,读起来完整;找不到就硬截。
    window = text[:_CHARACTER_INFO_MAX_LEN]
    cut = max(window.rfind(ch) for ch in "。；;！!？?")
    if cut >= _CHARACTER_INFO_MAX_LEN // 2:
        return window[: cut + 1]
    return window.rstrip("，,、") + "…"


def build_local_context(node_id, char_db_json):
    """构建单节点的局部角色映射，并进行命名碰撞预警"""
    local_role_mapping = {}
    prompt_lines = []

    if not char_db_json:
        return local_role_mapping, "", "", {}

    present_chars = {}
    for char_key, info in char_db_json.items():
        appearances = info.get("node_appearances", {})
        if node_id in appearances or not appearances:
            present_chars[char_key] = info

    if not present_chars:
        present_chars = char_db_json

    alias_to_owner = {}
    conflicts = set()
    skipped_generics = []  # 记录被跳过的泛称,便于排查上游数据问题
    skipped_descriptive_names = []  # 记录被跳过的描述性主名 (上游数据问题)
    speaker_emotion_types = {}  # B-EMO: 收集 speaker_name -> emotion_type 映射,供复审 pass 使用

    for key, info in present_chars.items():
        real_name = info.get("name") or key

        # 源头防御:角色库的 name 字段不能是描述短语 (如错把"御剑飞天的女子"当主名)。
        # 这种角色不能进 role_mapping,否则:
        #  - 出口闸门的 canonical_names 会把描述短语认作合法主名,放行
        #  - LLM 收到的 VIP 列表里会含这个描述短语,加深混淆
        # 这类角色直接跳过,等同于该角色在本节点不存在,描述短语 speaker 留给 reconcile 走 NPC 路径处理。
        if real_name and _has_descriptive_pattern(real_name):
            skipped_descriptive_names.append(real_name)
            continue

        local_role_mapping[real_name] = real_name

        # B-EMO: 提取角色情绪类型,默认 direct_expressive (向后兼容上游未打标的情况)
        emotion_type = info.get("emotion_type", "direct_expressive")
        if emotion_type not in {
            "direct_expressive", "tsundere_masked", "stoic_restrained",
            "gentle_calm", "melodramatic", "cold_aloof", "volatile_erratic"
        }:
            emotion_type = "direct_expressive"
        speaker_emotion_types[real_name] = emotion_type

        if real_name in alias_to_owner and alias_to_owner[real_name] != real_name:
            conflicts.add(real_name)
        alias_to_owner[real_name] = real_name

        valid_aliases = []
        aliases = info.get("other_names", [])
        if isinstance(aliases, list):
            for alias in aliases:
                if alias and isinstance(alias, str):
                    clean = alias.strip()
                    if not clean or clean == real_name:
                        continue

                    # B3: 泛称黑名单拦截 — 不进入 role_mapping,不进入 prompt
                    if _is_generic_title(clean):
                        skipped_generics.append((real_name, clean))
                        continue

                    local_role_mapping[clean] = real_name
                    valid_aliases.append(clean)

                    if clean in alias_to_owner and alias_to_owner[clean] != real_name:
                        conflicts.add(clean)
                    alias_to_owner[clean] = real_name

        # 角色简介(与 other_names 同级的 info 字段)。同名度高的群演
        # (同事A/B/C、业主A/B/C)靠简介里的职业、立场、亲属关系才能区分归属,
        # 而这些角色的区分性别名往往是描述性短语,已被泛称黑名单丢弃。
        char_info = _clean_character_info(info.get("info"))

        # B-EMO: 在 prompt 中同时暴露真名、情绪类型、别名、简介
        line = f"{real_name} [情绪类型: {emotion_type}]"
        if valid_aliases:
            line += f" (别名/代称: {', '.join(valid_aliases)})"
        if char_info:
            line += f" 〔简介: {char_info}〕"
        prompt_lines.append(line)

    # 不可靠别名跳过情况汇总告警(包括精确黑名单 + 描述性模式 + 泛指身份词尾)
    if skipped_generics:
        sample = ", ".join([f"{n} ← '{a}'" for n, a in skipped_generics[:8]])
        more = f" 等共 {len(skipped_generics)} 条" if len(skipped_generics) > 8 else ""
        logger.warning(
            f"⚠️ [节点 {node_id}] 已丢弃不可靠别名(泛称/描述性短语/泛指身份词尾): {sample}{more}。"
            f"模型将依据原文上下文自行判断归属,避免被上游误标的别名映射误导。"
        )

    # 角色库中 name 字段本身是描述短语的情况(上游数据错误)
    if skipped_descriptive_names:
        logger.warning(
            f"⚠️ [节点 {node_id}] 角色库中 {len(skipped_descriptive_names)} 个角色的 name 字段"
            f"是描述短语,已从 role_mapping 中剔除: {skipped_descriptive_names}。"
            f"上游应把描述短语放入 other_names 而非 name。"
            f"这些角色的台词将作为未具名 NPC 进入 reconcile 处理流程。"
        )

    local_vip_prompt_str = " | ".join(prompt_lines)

    # 序号型群演系列:告诉模型每个系列已占用到哪个字母,新出现的同类匿名角色
    # 从下一个字母续排。不做这件事,模型只能退回群体称呼("同事们")或
    # 硬塞给某个已有编号(把不同人的台词并到一个人头上)。
    serial_series = collect_serial_series(local_role_mapping.values())
    serial_hint = ""
    if serial_series:
        hint_is_cn = is_chinese_content("".join(serial_series.keys()))
        series_lines = []
        for base, letters in sorted(serial_series.items()):
            next_letter = _next_serial_letter(letters)
            if not next_letter:
                continue
            taken = ", ".join(base + l for l in letters)
            if hint_is_cn:
                series_lines.append(
                    f"  - 「{base}」系列已占用: {taken}"
                    f" → 本系列新出现的匿名角色从 {base}{next_letter} 开始依次续排"
                )
            else:
                series_lines.append(
                    f"  - Series \"{base}\" already taken: {taken}"
                    f" → number new anonymous members of this series from {base}{next_letter} onward"
                )
        if series_lines and hint_is_cn:
            serial_hint = (
                "\n### 序号型群演系列(续编号依据)\n"
                "上方名单中,以下身份存在「身份名 + 大写字母」的同类匿名群演系列。"
                "字母仅代表上游录入顺序,**不携带任何语义**,能区分它们的只有各自的〔简介〕。\n"
                + "\n".join(series_lines)
                + "\n"
            )
        elif series_lines:
            serial_hint = (
                "\n### Serial extra series (basis for continuing the numbering)\n"
                "The list above contains series of same-identity anonymous extras named "
                "\"identity + capital letter\". The letter only reflects upstream insertion order and "
                "**carries no semantics**; the only thing distinguishing them is each one's 〔profile〕.\n"
                + "\n".join(series_lines)
                + "\n"
            )
        logger.info(
            f"ℹ️ [节点 {node_id}] 识别到序号型群演系列: "
            + ", ".join(f"{b}({''.join(ls)})" for b, ls in sorted(serial_series.items()))
        )

    if serial_hint:
        local_vip_prompt_str = f"{local_vip_prompt_str}\n{serial_hint}"

    conflict_warning = ""
    if conflicts:
        conflict_str = ", ".join(conflicts)
        conflict_warning = f"""
⚠️【极高优先级防混淆警告】:
在本段剧情中，以下称呼存在严重指代歧义（多角色共用或主别名重叠）：[{conflict_str}]。
提取时，绝不可进行无脑的字面替换！你**必须**严密结合上下文语境、动作描写及人物立场，推理出该称呼在本句中具体属于哪个角色的真名。
"""
        logger.warning(f"⚠️ [节点 {node_id}] 检测到名称冲突风险: {conflict_str}，已注入消歧义 Prompt。")

    return local_role_mapping, local_vip_prompt_str, conflict_warning, speaker_emotion_types


def clean_markdown_content(content):
    """切除无关元数据"""
    stop_markers = [r'\n###\s*后续选项', r'\n###\s*Next Options', r'\n###\s*Options']
    for marker in stop_markers:
        match = re.search(marker, content, re.IGNORECASE)
        if match:
            return content[:match.start()]
    return content


def _sanitize_content_strings(json_text):
    """
    B2 代码兜底: 修复 LLM 输出 JSON 时,content 字符串内部出现的
    裸英文双引号/单引号,这些会破坏 JSON 解析。

    策略: 用状态机扫描,识别"字段值字符串"区域,把区域内的裸 " 或 ' 替换为中文引号。
    保守起见,只处理 "content": "..." 这一个最常出问题的字段。
    """
    if not json_text:
        return json_text

    # 匹配 "content": "..." 这种结构,允许跨行
    # 关键:用非贪婪 + 后视确保抓到的是值字符串
    # 实际上 LLM 输出的 content 不会跨行(我们提示词约束了),所以简单处理即可
    def fix_one(match):
        prefix = match.group(1)  # "content":\s*"
        body = match.group(2)  # 中间内容
        suffix = match.group(3)  # 结尾的 "

        # 把 body 中的裸双引号替换为中文双引号
        # 注意保留 JSON 转义序列 \"
        # 做法: 先把 \" 替换成占位符,处理完再换回来
        placeholder = "\x00ESCQUOTE\x00"
        safe_body = body.replace('\\"', placeholder)
        # 现在剩下的 " 都是裸的
        safe_body = safe_body.replace('"', '\u201d')  # 中文右双引号
        # 英文裸单引号:只替换"孤立的引用引号",跳过词内撇号。
        # it's / don't / y'all 这类撇号两侧是字母,对 JSON 解析无害,替换反而毁词形。
        safe_body = re.sub(r"(?<![A-Za-z])'(?![A-Za-z])", "\u201d", safe_body)

        # 中文引号 \u201c \u201d \u2018 \u2019 \u300c \u300d 一律**原样保留**:它们对 JSON 解析毫无威胁
        # (JSON 只怕英文双引号)。且作者常用"外层双引号=对话 / 内层单引号=强调"的层级,
        # 如 \u201c\u2018凌小姐\u2019三个字,拗口。\u201d。若把 \u2018\u2019 转成 \u201c\u201d,下游 clean_universal_content
        # 剥外层对话引号时会连内层左引号一起剥掉,产生"凌小姐\u201d三个字"这种单边残留。
        safe_body = safe_body.replace(placeholder, '\\"')
        return prefix + safe_body + suffix

    # 这个正则匹配: "content": " <body> "
    # body 可以包含任何字符,但不能跨越下一个键值对
    # 用 [^\n] + 非贪婪 + lookahead 来界定结束边界
    pattern = re.compile(
        r'("content"\s*:\s*")'  # group 1: prefix
        r'(.*?)'  # group 2: body (greedy 关闭)
        r'(")(?=\s*[,}])',  # group 3: closing quote, lookahead 确保下一个有意义字符是 , 或 }
        re.DOTALL
    )

    try:
        fixed = pattern.sub(fix_one, json_text)
        return fixed
    except Exception:
        # 任何意外都回退原文,避免兜底逻辑反而成为新的故障源
        return json_text


def robust_json_extract(raw_text):
    match = re.search(r'```json\s*(.*?)\s*```', raw_text, re.DOTALL)
    if match:
        candidate = match.group(1).strip()
    else:
        start = raw_text.find('[')
        end = raw_text.rfind(']')
        if start != -1 and end != -1:
            candidate = raw_text[start:end + 1]
        else:
            candidate = raw_text.strip()

    # B2 兜底: 修复 content 字符串内的裸引号
    candidate = _sanitize_content_strings(candidate)
    return candidate


def robust_json_parse(raw_text, *, context="", expect=None):
    """robust_json_extract + json_repair 兜底的完整解析。

    robust_json_extract 只剥围栏并修裸引号，修不了 ``"bpm": fifty`` 这类非法
    token。Step 3 无产出会硬中断整条流水线，所以这里多兜一层：单个字段写坏
    不该让整章剧情作废。两层都失败仍抛 JSONDecodeError，交给 tenacity 重试。
    """
    candidate = robust_json_extract(raw_text)
    if expect is None:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            # 顶层类型未知时先试对象再试数组，避免误判修复结果。
            for expected_type in (dict, list):
                try:
                    return parse_model_json(
                        candidate, context=context, logger=logger,
                        expect=expected_type,
                    )
                except json.JSONDecodeError:
                    continue
            raise
    return parse_model_json(
        candidate, context=context, logger=logger, expect=expect
    )


def is_chinese_content(raw_text):
    return bool(re.search(r'[\u4e00-\u9fa5]', raw_text))


def _pinyin_speaker_id(raw_text):
    """纯拼音/驼峰规则生成 speaker_id,不查任何映射表。"""
    if not raw_text:
        return None

    if re.search(r'[\u4e00-\u9fa5]', raw_text):
        return "".join([p.capitalize() for p in lazy_pinyin(raw_text)])

    raw_text = re.sub(r"['']s\b", "", raw_text, flags=re.IGNORECASE)
    raw_text = re.sub(r"['']", "", raw_text)
    raw_text = re.sub(r"[^a-zA-Z0-9]", " ", raw_text)

    parts = raw_text.split()
    return "".join(part.capitalize() for part in parts)


def to_pascal_case_speaker_id(raw_text):
    """生成合规的 speaker_id。

    需求3: 优先查 original_name.conversion 映射(_NAME_CONVERSION_MAP),
    命中(通常是中文主名,如"李玉"→"LiYu")直接返回;未命中再走原有拼音/驼峰规则。
    """
    if not raw_text:
        return None

    # 优先用 conversion 映射表(原文主名直接命中)
    mapped = _NAME_CONVERSION_MAP.get(raw_text)
    if mapped:
        return mapped

    return _pinyin_speaker_id(raw_text)


def universal_merge_speech_tags(raw_text):
    """
    通用小说换行提示语缝合器：
    解决作者喜欢把"XXX说。"单独换行导致大模型角色张冠李戴的问题。
    """
    speech_verbs = r"(?:说|道|问|答|开口|叹|喊|笑|冷哼|冷笑|补充|解释|接话)"
    pattern = r'([”」\'])\s*\n+\s*([^“”「」\n]{1,20}' + speech_verbs + r'[^“”「」\n]{0,5}[。！？])'
    merged_text = re.sub(pattern, r'\1\2', raw_text)
    return merged_text


def split_text_with_global_context(full_text, global_buffer, is_main_line=True, chunk_size=5000, overlap_size=600,
                                   base_line_offset=0):
    """带记忆隔离机制的切块器 (消除分支带来的失忆)

    新增 base_line_offset:full_text 在原始 markdown 中的起始行号(0-based)。
    返回的每个 chunk 带 src_lines = [start_line, end_line](inclusive),
    用于下游 scenes 阶段与 plots beat 的行号对齐。
    """
    lines = full_text.split('\n')
    chunks_data = []
    current_chunk = []
    current_chunk_line_indices = []  # 当前 chunk 包含的行号(相对 full_text)
    current_length = 0

    working_buffer = global_buffer  # str

    for line_idx, line in enumerate(lines):
        line_len = len(line) + 1
        if current_length + line_len > chunk_size:
            clean_line = line.strip()
            prev_clean_line = current_chunk[-1].strip() if current_chunk else ""
            is_dialogue_active = clean_line.startswith(('"', '\u201c', '\u300c')) or \
                                 prev_clean_line.startswith(('"', '\u201c', '\u300c'))

            if not is_dialogue_active or current_length > chunk_size + 1000:
                chunk_text = "\n".join(current_chunk)
                if chunk_text.strip():
                    context_str = working_buffer[-overlap_size:] if working_buffer else ""
                    src_start = base_line_offset + current_chunk_line_indices[0]
                    src_end = base_line_offset + current_chunk_line_indices[-1]
                    chunks_data.append({
                        "text": chunk_text,
                        "context": context_str,
                        "src_lines": [src_start, src_end],
                    })

                    working_buffer = (working_buffer + "\n" + chunk_text)[-(overlap_size * 4):]

                current_chunk = []
                current_chunk_line_indices = []
                current_length = 0

        current_chunk.append(line)
        current_chunk_line_indices.append(line_idx)
        current_length += line_len

    if current_chunk:
        chunk_text = "\n".join(current_chunk)
        if chunk_text.strip():
            context_str = working_buffer[-overlap_size:] if working_buffer else ""
            src_start = base_line_offset + current_chunk_line_indices[0]
            src_end = base_line_offset + current_chunk_line_indices[-1]
            chunks_data.append({
                "text": chunk_text,
                "context": context_str,
                "src_lines": [src_start, src_end],
            })
            working_buffer = (working_buffer + "\n" + chunk_text)[-(overlap_size * 4):]

    final_buffer_to_return = working_buffer if is_main_line else global_buffer

    return chunks_data, final_buffer_to_return


def refine_emotion_by_keywords(raw_text, original_emotion, beat_type):
    """轻量兜底情绪逻辑(向后兼容保留)。

    主情绪推理已由 LLM 复审 pass (re_label_emotions_async) 负责。
    本函数仅做最基础的硬规则校验:
      1. narration / nvl_narration 强制 calm
      2. 合法值白名单过滤
      3. 当 LLM 第一次输出 calm 时,用一个保守的强信号词典做兜底
         (仅在出现强烈情绪词且无掩饰矛盾时才改写)

    复杂的上下文窗口、反讽、角色类型推理留给复审 pass 处理。
    """
    if beat_type in ('narration', 'nvl_narration'):
        return "calm"

    if original_emotion in ALLOWED_EMOTIONS and original_emotion != "calm":
        return original_emotion

    # 仅在 calm/空/非法值时做保守兜底
    if not raw_text:
        return original_emotion if original_emotion in ALLOWED_EMOTIONS else "calm"

    text_lower = raw_text.strip().lower()

    emotion_scores = {emo: 0 for emo in EMOTION_KEYWORDS}
    for emotion, keywords in EMOTION_KEYWORDS.items():
        for kw in keywords:
            if re.match(r'^[a-zA-Z\s]+$', kw):
                if re.search(rf'\b{kw}[a-z]*\b', text_lower):
                    if re.search(rf'\b(not|didn\'t|wasn\'t|isn\'t|never|no)\s+{kw}[a-z]*\b', text_lower):
                        continue
                    emotion_scores[emotion] += 1
            else:
                if kw in text_lower:
                    if f"不{kw}" in text_lower or f"没{kw}" in text_lower:
                        continue
                    emotion_scores[emotion] += 1

    best_emotion = max(emotion_scores, key=emotion_scores.get)

    # 保守策略: 至少 2 个信号词命中才覆写,降低误改风险
    if emotion_scores[best_emotion] >= 2:
        return best_emotion

    return original_emotion if original_emotion in ALLOWED_EMOTIONS else "calm"


def strip_dialogue_for_sniffing(raw_text):
    """物理剥离台词：用于 POV 嗅探前，将所有引号内的对话删除，只保留纯叙事文本。"""
    pattern = r'[“"「].*?[”"」]'
    cleaned_text = re.sub(pattern, ' ', raw_text)
    return re.sub(r'\n+', '\n', cleaned_text).strip()


@retry(stop=stop_after_attempt(3), wait=wait_random_exponential(min=1, max=10))
async def detect_pov_async(client, text_block, vip_prompt_str, pov_semaphore, vip_real_names=None):
    """
    极速视角嗅探器:截取头部 600 字,调用小模型分析当前块的视点人物。

    B7 增强:
    1. 中英双语自动切换 — 根据 text_block 语言选择对应的 system_prompt
    2. POV 嗅探误判加固 — 提示词加强红线 + 代码侧 VIP 白名单兜底:
       LLM 返回的 anchor_id 必须在 VIP 真名集合或 {PangBai, INHERIT} 中,
       否则强制降级为 INHERIT(避免把"狗子""翠儿"等路人误锁为视角主人)。

    参数 vip_real_names:VIP 真名集合(set),用于代码侧白名单校验。
    传 None 时跳过白名单校验(向后兼容,但不推荐)。
    """
    head_text = text_block[:600]
    pure_narrative = strip_dialogue_for_sniffing(head_text)

    if len(pure_narrative) < 20:
        return {"pov_type": "INHERIT", "pov_anchor_id": "INHERIT"}

    is_cn = is_chinese_content(pure_narrative)

    if is_cn:
        system_prompt = """
你是一个极其精准的小说视角分析器。
你的任务是阅读给定的【纯叙事文本】(已去除对话),并输出严格的 JSON 格式。

输出格式:{"pov_type": "1st" | "3rd_limited" | "3rd_omniscient" | "INHERIT", "pov_anchor_id": "RealName" | "PangBai" | "INHERIT"}
只返回 JSON 对象,禁止任何其他内容。
"""
        user_prompt = f"""
【核心剧组/主角团名单】:[{vip_prompt_str}]

<text>
{pure_narrative}
</text>

# 视角嗅探分析规则 (多视角群像通用法则):

1. **【第一人称推断】**:
   - **唯一线索**:文本中"我"作为**主语**的句子(如"我走过去"、"我心想")。
   - 仅当"我"作为主语高频出现时,才判断为 "1st"。你必须结合文本和【核心剧组名单】推断"我"的真名。
   - **【防误判红线 1】**:某个角色名在文本中频繁出现 **≠** 该角色是视角主人。
     例如文本是"翠儿哭了,翠儿向我求救,翠儿说她害怕"——翠儿被频繁提到,但视角主人是"我",不是翠儿!
   - **【防误判红线 2】**:`pov_anchor_id` **只能从【核心剧组名单】中选**。
     严禁仅仅因为文本提到某个配角或路人(如"狗子"、"小二"、"大夫"、"翠儿"),就把"我"归属于他们!
     如果你倾向于的人名不在名单中,请改为输出 INHERIT。

2. **【状态继承机制 (INHERIT) - 极其重要】**:
   如果文本中大量使用"我",但你**无法严谨地从名单中**确定"我"是谁(文本被截断 / 上下文不足 / 名单中多人都可能),【绝对不要瞎猜】!
   此时,强制输出:{{"pov_type": "1st", "pov_anchor_id": "INHERIT"}},表示将判定权交由系统的历史上下文。

3. **【第三人称分类】**:
   如果纯叙事文本中几乎没有"我"作为主语(对话中的"我"已被剔除,不计入):
   - 若极度聚焦于某个【名单内核心角色】的私人心理、主观视觉,判断为 "3rd_limited",`pov_anchor_id` 输出该角色真名。
   - 若为纯粹的客观环境白描或无偏向动作,判断为 "3rd_omniscient",`pov_anchor_id` 填 "PangBai"。
   - 同样适用红线 2:`pov_anchor_id` 必须在名单内,否则填 "PangBai"。
"""
    else:
        system_prompt = """
You are an extremely precise novel POV analyzer.
Your task: read the given [pure narrative text] (dialogue removed) and output strict JSON format.

Output format: {"pov_type": "1st" | "3rd_limited" | "3rd_omniscient" | "INHERIT", "pov_anchor_id": "RealName" | "PangBai" | "INHERIT"}
Return ONLY the JSON object. No other content allowed.
"""
        user_prompt = f"""
[CORE CAST / MAIN PROTAGONIST LIST]: [{vip_prompt_str}]

<text>
{pure_narrative}
</text>

# POV Detection Rules (universal for multi-POV ensemble novels):

1. **[1st Person Inference]**:
   - **Sole signal**: sentences with "I" as the **subject** (e.g. "I walked over", "I thought").
   - Only when "I" appears frequently as the subject, judge as "1st". You must combine the text and [Core Cast List] to infer who "I" is.
   - **[Anti-misjudgment red line 1]**: A character name appearing frequently in the text **≠** that character is the POV holder.
     E.g. if the text says "Mary cried, Mary begged me, Mary said she was scared" — Mary is mentioned often, but the POV holder is "I", NOT Mary!
   - **[Anti-misjudgment red line 2]**: `pov_anchor_id` **MUST come from the [Core Cast List]**.
     You are FORBIDDEN to assign "I" to a side character or extra (e.g. "Driver", "Waiter", "Mary") just because they appear in the text!
     If the name you lean toward is NOT in the list, output INHERIT instead.

2. **[State Inheritance Mechanism (INHERIT) - Critical]**:
   If "I" appears frequently but you **cannot rigorously confirm** which person in the list "I" refers to (text truncated / context insufficient / multiple candidates), DO NOT GUESS!
   Force output: {{"pov_type": "1st", "pov_anchor_id": "INHERIT"}}, delegating decision to the system's historical context.

3. **[3rd Person Classification]**:
   If the pure narrative text rarely uses "I" as the subject (dialogue "I" has been removed and does not count):
   - If extremely focused on one [core cast member]'s private psychology or subjective vision, judge as "3rd_limited", `pov_anchor_id` outputs that character's real name.
   - If purely objective environmental description or unbiased action, judge as "3rd_omniscient", `pov_anchor_id` is "PangBai".
   - Red line 2 applies: `pov_anchor_id` must be in the list, otherwise fill "PangBai".
"""

    try:
        async with pov_semaphore:
            response, used_model = await _model_fallback.achat(
                client,
                messages=[
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': user_prompt}
                ],
                temperature=0.1
            )

        if _usage_tracker and response.usage:
            _usage_tracker.add_llm_usage(
                response.usage, used_model, provider="openai_compatible"
            )
        pov_result = robust_json_parse(
            response.choices[0].message.content, context="pov", expect=dict
        )
    except Exception as e:
        logger.warning(f"⚠️ POV 嗅探失败,启用降级兜底: {e}")
        return {"pov_type": "INHERIT", "pov_anchor_id": "INHERIT"}

    # ===== 代码侧白名单兜底:防止 LLM 把名单外的角色锁为视角主人 =====
    if vip_real_names is not None:
        anchor = pov_result.get("pov_anchor_id")
        # 接受的值: VIP 名单内真名、PangBai、INHERIT
        accepted = {"PangBai", "INHERIT"} | set(vip_real_names)
        if anchor and anchor not in accepted:
            logger.warning(
                f"⚠️ POV 嗅探返回的 anchor_id '{anchor}' 不在 VIP 名单中,"
                f"强制降级为 INHERIT。VIP 名单: {sorted(vip_real_names)[:8]}{'...' if len(vip_real_names) > 8 else ''}"
            )
            # 第三人称下兜底为 PangBai;第一人称兜底为 INHERIT
            if pov_result.get("pov_type", "").startswith("3rd"):
                pov_result["pov_anchor_id"] = "PangBai"
            else:
                pov_result["pov_anchor_id"] = "INHERIT"

    return pov_result


def build_dynamic_pov_rules(pov_data, is_cn=True):
    """根据嗅探结果，动态生成红线规则 + 专属 One-Shot Example"""
    pov_type = pov_data.get("pov_type", "3rd_omniscient")
    anchor_id = pov_data.get("pov_anchor_id", "PangBai")

    if pov_type == "1st" and anchor_id and anchor_id != "PangBai":
        rules = f"""
#第一人称(最高优先级)
1. 经系统嗅探，当前文本块为【第一人称】视角，视点人物锁定为：【{anchor_id}】。
2. 文本中所有的"我"，100% 绝对指代【{anchor_id}】！严禁将"我"的台词或动作分发给旁白或其他任何人。
3. 文本中任何没有明确主语的主观感受、心理活动、幻觉或回忆闪回，必须判定为 `nvl_thought`，且 `speaker_id` 必须强制指定为【{anchor_id}】！
"""
        if is_cn:
            example = f"""
# ONE-SHOT EXAMPLE (第一人称示范 — 演示"字面情绪 ≠ 真实情绪"的判读)
注意:示例中出现的人名均为格式占位符,与你正在处理的 <current_text> 无关。
你必须以本提示词上方【GROUP A: VIPs 名单】中的真实角色为准,严禁将示例中的角色名带入实际提取!

## Input:
角色档案: [CharacterA - tsundere_masked], [CharacterB - direct_expressive]
<current_text>
我抹了一把眼睛，撇过头去。"笨蛋！谁让你这么做的！"
CharacterB笑了笑："别太凶了。"
我猛地转过头，声音都在抖："我哪有凶人！"
</current_text>

## Output JSON (注意 emotion 字段的反字面判读):
[
  {{ "beat_type": "narration", "speaker_name": "旁白", "content": "{anchor_id}抹了眼睛，撇过头去。", "emotion": "calm" }},
  {{ "beat_type": "dialogue", "speaker_name": "{anchor_id}", "content": "笨蛋！谁让你这么做的！", "emotion": "sad" }},
  {{ "beat_type": "dialogue", "speaker_name": "CharacterB", "content": "别太凶了。", "emotion": "calm" }},
  {{ "beat_type": "dialogue", "speaker_name": "{anchor_id}", "content": "我哪有凶人！", "emotion": "sad" }}
]

# 教学说明:
- {anchor_id} 是 tsundere_masked 类型, 字面 angry 台词 "笨蛋！" 在
  "抹眼睛 + 撇过头 + 声音发抖" 的悲伤+掩饰信号下, 真实情绪是 sad, 不是 angry
- CharacterB 是 direct_expressive 类型, 平静微笑且无强烈体征 → 保持 calm
"""
        else:
            example = f"""
# ONE-SHOT EXAMPLE (1st-person demo — shows "literal emotion ≠ real emotion" inference)
NOTE: Character names below are format placeholders only, unrelated to your actual <current_text>.
You MUST use real characters from the [GROUP A: VIPs list] above. Do NOT carry example names into your extraction!

## Input:
Character profiles: [CharacterA - tsundere_masked], [CharacterB - direct_expressive]
<current_text>
I wiped at my eyes and turned my face away. "Idiot! Who told you to do that!"
CharacterB smiled faintly. "Don't be too hard on him."
I whipped my head around, my voice shaking. "I'm not being hard on anyone!"
</current_text>

## Output JSON (note the anti-literal emotion judgments):
[
  {{ "beat_type": "narration", "speaker_name": "Narrator", "content": "{anchor_id} wiped at the eyes and turned away.", "emotion": "calm" }},
  {{ "beat_type": "dialogue", "speaker_name": "{anchor_id}", "content": "Idiot! Who told you to do that!", "emotion": "sad" }},
  {{ "beat_type": "dialogue", "speaker_name": "CharacterB", "content": "Don't be too hard on him.", "emotion": "calm" }},
  {{ "beat_type": "dialogue", "speaker_name": "{anchor_id}", "content": "I'm not being hard on anyone!", "emotion": "sad" }}
]

# Teaching notes:
- {anchor_id} is tsundere_masked. The literal "angry" line "Idiot!" under
  "wiping eyes + turning away + trembling voice" mask signals reflects sad, NOT angry.
- CharacterB is direct_expressive. Calm smile, no strong physiological cues → calm.
"""

        return rules + "\n" + example

    else:
        rules = f"""
# 第三人称视角(最高优先级)
1. 当前文本为主观性较弱的第三人称。旁白 (PangBai) 仅作为摄像机存在，绝对不具备私人感情。
2. 遇到极其宏大的设定科普或历史背景介绍，必须判定为 `nvl_narration`，`speaker_id` 为 `PangBai`。
3. 文本中出现的角色内心独白（带有"心想"或括号），必须判定为 `nvl_thought`，并寻找确切的思考者真名。
"""
        if is_cn:
            example = """
# ONE-SHOT EXAMPLE (第三人称示范 — 演示信号识别与角色类型应用)
注意:示例中角色名仅为占位符,不要带入实际提取。

## Input:
角色档案: [CharacterA - tsundere_masked], [CharacterB - stoic_restrained]
<current_text>
CharacterA手指在袖口里死死攥紧，眼眶突然就红了。
"你是不是傻？！"CharacterA几乎吼出来，"这有什么值得高兴的！"
"嗯。"CharacterB低头看着掌心的东西，声音很轻。
难道他从来没有过这样的东西吗？CharacterA心里一紧。
</current_text>

## Output JSON (注意 emotion 字段的反字面判读):
[
  {{ "beat_type": "narration", "speaker_name": "旁白", "content": "CharacterA手指攥紧，眼眶红了。", "emotion": "calm" }},
  {{ "beat_type": "dialogue", "speaker_name": "CharacterA", "content": "你是不是傻？！这有什么值得高兴的！", "emotion": "sad" }},
  {{ "beat_type": "dialogue", "speaker_name": "CharacterB", "content": "嗯。", "emotion": "sad" }},
  {{ "beat_type": "nvl_thought", "speaker_name": "CharacterA", "content": "难道他从来没有过这样的东西吗？", "emotion": "sad" }}
]

# 教学说明:
- CharacterA 是 tsundere_masked, 字面 angry "吼出来" + 类别1悲伤信号 "眼眶红了" →
  反转为 sad, 不是 angry
- CharacterB 是 stoic_restrained, 字面 calm "嗯" + 类别1信号 "低头 + 声音很轻" →
  反转为 sad, 不是 calm
"""
        else:
            example = """
# ONE-SHOT EXAMPLE (3rd-person demo — shows signal-based and type-aware judgment)
NOTE: Names below are format placeholders only — do NOT carry them into your actual extraction.

## Input:
Character profiles: [CharacterA - tsundere_masked], [CharacterB - stoic_restrained]
<current_text>
CharacterA's fingers clenched tightly in her sleeves, and her eyes suddenly went red.
"Are you stupid?!" CharacterA almost shouted. "What is there to be happy about!"
"Mm." CharacterB looked down at the thing in his palm, voice very soft.
Had he never had something like this before? CharacterA thought, her chest tightening.
</current_text>

## Output JSON (note the anti-literal emotion judgments):
[
  {{ "beat_type": "narration", "speaker_name": "Narrator", "content": "CharacterA's fingers clenched and her eyes went red.", "emotion": "calm" }},
  {{ "beat_type": "dialogue", "speaker_name": "CharacterA", "content": "Are you stupid?! What is there to be happy about!", "emotion": "sad" }},
  {{ "beat_type": "dialogue", "speaker_name": "CharacterB", "content": "Mm.", "emotion": "sad" }},
  {{ "beat_type": "nvl_thought", "speaker_name": "CharacterA", "content": "Had he never had something like this before?", "emotion": "sad" }}
]

# Teaching notes:
- CharacterA is tsundere_masked. Literal "angry" "almost shouted" + Category 1 sadness signal
  "eyes went red" → reverse to sad, not angry.
- CharacterB is stoic_restrained. Literal "calm" "Mm" + Category 1 signal "looked down + soft voice"
  → reverse to sad, not calm.
"""

        return rules + "\n" + example


def _quotes_wrap_whole(t, open_q, close_q):
    """判断 t 的首尾引号是否真的包裹整个字符串(而非"两段并列引用")。

    做法:从左到右走一遍深度计数,首字符入栈(depth=1),
    若在**到达末尾之前**深度归零,说明开头那个引号在句中就已闭合,
    首尾并非同一对 → 不可剥。

    例:
      “你好。”            → 深度只在末尾归零        → True(可剥)
      “好。”他说。         → 深度在"好。"后就归零     → False
      “他说“不行”。”      → 嵌套,深度末尾才归零      → True(可剥)
      “凌小姐”三个字，拗口。→ 深度在"凌小姐"后归零     → False
    """
    # 左右同形的直引号无法做深度计数,退化为"整串仅出现两次"判断
    if open_q == close_q:
        return t.count(open_q) == 2

    depth = 0
    for i, ch in enumerate(t):
        if ch == open_q:
            depth += 1
        elif ch == close_q:
            depth -= 1
            if depth == 0:
                # 只有恰好在最后一个字符处闭合,才算包裹整句
                return i == len(t) - 1
    return False


def _strip_wrapping_quotes(text):
    """剥掉最外层的对话引号,但**只在首尾真正配对包裹整句时**才动手。

    为什么不能无条件剥首尾引号(历史 bug):
    台词有时以一个被引用的词开头,如 “‘凌小姐’三个字,拗口。”。
    旧实现用 ^["“«'] 和 ["”»']$ 各剥一刀,遇到
    “凌小姐”三个字,拗口。 这种(右引号在句中而非句尾)就只剥掉左引号,
    留下 凌小姐”三个字,拗口。 的单边残留。

    现在的口径:
      1. 首尾必须是**同一组**引号的左右两半(或同为直引号)才剥,剥完递归处理嵌套包裹。
      2. 不配对则**原样保留**,宁可留个引号让人看懂,也不丢字/破坏语义。
    """
    if not text:
        return text
    t = text.strip()

    # 成对的引号组:左 -> 右
    PAIRS = {
        '“': '”',   # 中文双引号
        '‘': '’',   # 中文单引号
        '「': '」',   # 日式括号
        '『': '』',
        '«': '»',   # 法式书名号
        '"': '"',         # 直双引号(左右同形)
        "'": "'",         # 直单引号(左右同形)
    }

    # 只剥**最外面一层**。对话引号只有一层,内层引号是作者的强调/引用语义,
    # 必须保留(如 “‘凌小姐’” 应得到 ‘凌小姐’,而不是裸的 凌小姐)。
    if len(t) >= 2:
        head, tail = t[0], t[-1]
        if head in PAIRS and PAIRS[head] == tail and _quotes_wrap_whole(t, head, PAIRS[head]):
            t = t[1:-1].strip()

    return t


def clean_universal_content(raw_text):
    if not raw_text:
        return ""
    raw_text = raw_text.strip()

    if "effects_" in raw_text or "◆◆◆" in raw_text or "选项" in raw_text:
        return raw_text

    raw_text = _strip_wrapping_quotes(raw_text)

    raw_text = re.sub(r'[\*_]+', '', raw_text)
    raw_text = re.sub(r'^>\s*', '', raw_text)
    raw_text = re.sub(r'~~.*?~~', '', raw_text)

    if re.match(r'^[-*_]{3,}$', raw_text):
        return ""

    if raw_text.endswith(','):
        raw_text = raw_text[:-1] + '.'
    return raw_text.strip()


def parse_markdown_with_branches(content):
    """将 Markdown 文本切分为纯剧情块和结构标记块。

    系统文增强:在每次 flush text 时,会自动用 _split_text_block_by_panels
    把系统面板(多行键值块)抠成独立的 system_panel block,绕过 LLM 抽取。

    行号增强:每个 block 额外带 src_start_line / src_end_line(0-based,inclusive),
    指向原文 markdown 中该 block 内容的行号区间,供下游 scenes 阶段做 _src_lines 对齐。
    """
    blocks = []
    current_text = []
    current_line_indices = []  # 与 current_text 对应的源行号
    in_side_story = False

    for line_idx, raw_line in enumerate(content.split('\n')):
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith('## 番外'):
            _flush_text_buffer(blocks, current_text, current_line_indices)

            if in_side_story:
                blocks.append({"type": "side_story_end", "content": "番外结束，回到主线",
                               "src_start_line": line_idx, "src_end_line": line_idx})

            clean_side = line.replace('#', '').strip()
            blocks.append({"type": "side_story", "content": clean_side,
                           "src_start_line": line_idx, "src_end_line": line_idx})
            in_side_story = True
            continue

        elif line.startswith('#'):
            _flush_text_buffer(blocks, current_text, current_line_indices)

            if in_side_story:
                blocks.append({"type": "side_story_end", "content": "番外结束，回到主线",
                               "src_start_line": line_idx, "src_end_line": line_idx})
                in_side_story = False

            continue

        elif re.match(r'^番外[一二三四五六七八九十百0-9]+', line) and len(line) < 20:
            continue

        elif '软分支选择' in line:
            _flush_text_buffer(blocks, current_text, current_line_indices)
            blocks.append({"type": "branch_start", "content": line,
                           "src_start_line": line_idx, "src_end_line": line_idx})

        elif '软分支结束' in line:
            _flush_text_buffer(blocks, current_text, current_line_indices)
            blocks.append({"type": "branch_end", "content": line,
                           "src_start_line": line_idx, "src_end_line": line_idx})

        elif re.search(r'^.*?\[?选项\s*[A-Z]\]?', line):
            _flush_text_buffer(blocks, current_text, current_line_indices)
            clean_opt = re.sub(r'^.*?选项\s*[A-Z]\]?[:：\s]*', '', line).strip()
            clean_opt = re.sub(r'^[^\w\u4e00-\u9fa5]+', '', clean_opt).strip()
            blocks.append({"type": "option", "content": clean_opt, "raw": line,
                           "src_start_line": line_idx, "src_end_line": line_idx})

        elif line.startswith('<!--') and 'effects' in line:
            _flush_text_buffer(blocks, current_text, current_line_indices)
            clean_effect = re.sub(r'<!--\s*(.*?)\s*-->', r'\1', line).strip()
            blocks.append({"type": "variable_update", "content": clean_effect,
                           "src_start_line": line_idx, "src_end_line": line_idx})

        else:
            current_text.append(line)
            current_line_indices.append(line_idx)

    if in_side_story:
        last_line = current_line_indices[-1] if current_line_indices else -1
        _flush_text_buffer(blocks, current_text, current_line_indices)
        blocks.append({"type": "side_story_end", "content": "番外结束，回到主线",
                       "src_start_line": last_line, "src_end_line": last_line})

    _flush_text_buffer(blocks, current_text, current_line_indices)

    return blocks


@retry(stop=stop_after_attempt(5), wait=wait_random_exponential(min=1, max=60))
async def extract_beats_async(client, chunk_data, semaphore, vip_prompt_str, conflict_warning, dynamic_pov_rules):
    chunk_text = chunk_data['text']
    prev_context = chunk_data['context']

    vip_list_str = vip_prompt_str if vip_prompt_str else "None provided"

    is_cn = is_chinese_content(chunk_text)

    if is_cn:
        npc_guidelines = """
- 如果确定该角色不在 VIP 名单中，请直接提取原文中的中文称呼或名字，**绝对不要翻译成英文！**
- 例如：原文是"车夫"，就输出"车夫"；原文是"翠儿"，就输出"翠儿"；原文是"老和尚"，就输出"老和尚"。
- **严禁使用群体标签**("同事们""众人""大家""村民们")作为说话人 —— 详见铁律 12。
- 规则：避免极其冗长的描述性名称。
  - 错误示例："卖花的老奶奶"
  - 正确示例："老奶奶" 或 "卖花女"
    """
    else:
        npc_guidelines = """
- If the character is definitely NOT in the VIP list, use a generic Role Name in PascalCase.
- Examples: "Driver", "Waiter", "PoliceOfficer", "Stranger".
- NEVER use a collective label ("Crowd", "Everyone", "Villagers", "Colleagues") as a speaker — see Rule 12.
- Rule: Avoid long descriptive names. 
  - Bad: "The old lady selling flowers"
  - Good: "FlowerSeller"
    """

    user_content_str = f"""
<previous_context>
{prev_context}
</previous_context>

<current_text>
{chunk_text}
</current_text>
"""

    if is_cn:
        system_prompt = f"""
# Role
视觉小说脚本专家 (Director Mode).

# Task
将 <current_text> 中的小说内容提取为视觉小说脚本格式（JSON）。
**注意：<previous_context> 仅供你"阅读"以理解人物关系（比如识别"她"是谁），严禁提取 Context 中的内容！**

{conflict_warning}

{dynamic_pov_rules}
# 角色管理系统

## GROUP A: VIPs (主角名单)
**你必须将文中的描述性称呼映射为以下真名:**
[{vip_list_str}]

* **规则**: 如果角色的外貌/行为符合 VIP 特征，**必须使用真名**。
* *例子*: 如果名单里有 "Lestat"，文中 "那个穿天鹅绒外套的男人" -> `Lestat`。
* **〔简介〕是台词归属的关键依据**: 名单里每个角色可能带 `〔简介: ...〕`，写明其身份、职业、
  所属场所、立场倾向、亲属关系及已知行为。当多个角色称呼相近，或原文只用泛称/描述性称呼指代时，
  **必须比对简介再定归属**：
  - 台词内容或所处场景与某角色简介中的身份、职业、所属场所吻合 → 归给该角色。
  - 台词表达的立场、主张与某角色简介中记录的立场一致 → 归给该角色。
  - 亲属类称呼（"<某人>的母亲"、"<某人>的女儿"之类）借助简介里的关系链定位到具体真名。
  - 同一批仅以编号或序号区分的同类角色（同职业、同场合的群演），简介往往是唯一区分线索。
* 若简介与当前段落的原文描写矛盾，**以原文为准**：简介是先验参考，不能覆盖原文事实。

## GROUP B: NPCs (路人规范)
如果确定不是 VIP，请使用标准化的职业/身份词 (PascalCase):
{npc_guidelines}

# 核心铁律

1. **角色定位 (角色对齐 - 非常重要)**:
   - 严格执行上述 GROUP A 和 GROUP B 的规则。
   - 遇到 "他/她" 或 "那个男人"，必须结合上下文判断是 VIP 还是 NPC，填入准确名字。

2. **旁白瘦身 (重要)**:
   - **视觉排重**: 假设画面已经展示了环境和人物立绘。**删掉**对衣着、家具、天气、五官、室内陈设、地理环境的静态描写,无论该描写有多长。
   - **关键边界**: "环境静态描写"(可视化的空间/人物外观) 必须删除;"超时间维度的设定信息"(世界观、历史、法则、规则体系) 才走 nvl_narration(见规则4B)。判断口径:能用一张背景图替代的内容 -> 删;无法被画面表达的抽象信息 -> 走 nvl_narration。
   - **保留核心**: 仅保留 **心理活动**、**嗅觉/触觉**、**抽象氛围**、**关键动作**。
   - *案例1*: 原文"她穿着一件白色的连衣裙,裙摆随风飘扬,站在树下。" -> **删除**(立绘和背景已有)
   - *案例2*: 原文"空气中弥漫着消毒水的味道,让他想起三年前的那个夜晚。" -> 保留为"空气中弥漫着刺鼻的消毒水味,勾起了冰冷的回忆。"
   - *案例3*: 原文"城堡矗立在悬崖边,黑色的塔楼直插云霄,周围环绕着浓雾。" -> **删除**(背景图可表达,即使有300字也要删)

3. **角色情绪 (基于信号识别 - 极其重要)**:
   - **合法值白名单**: `calm`, `happy`, `sad`, `angry`, `fearful`, `surprised`, `disgusted`
   - **作用对象**: 仅对 `dialogue` 和 `nvl_thought` 标注情绪。
     `narration` / `nvl_narration` 强制 `calm`,不参与本节判断。

   **情绪信号分类体系 (8 类 × 4 维度 - 语言与题材无关)**:
{EMOTION_SIGNAL_TAXONOMY_CN}

   **角色情绪类型判读策略** (基于上方 GROUP A 名单里每个角色的 `[情绪类型: xxx]` 标签):
{EMOTION_TYPE_PLAYBOOK_CN}

   **决策树 (执行顺序自上而下,命中即停)**:
{EMOTION_DECISION_TREE_CN}

   **绝对红线**:
   - 永远不要只看台词字面。必须结合台词前后 1-2 句 narration 的体征/姿态/声学/言语信号
     作为情绪判读的主要依据。
   - "动作描写中频繁出现的情绪词" ≠ "该角色的真实情绪"。例如"凶恶地说话"未必是 angry,
     需结合掩饰信号判断是否反转为 sad/happy。
   - 角色情绪类型 (emotion_type) 是先验,它定义了该角色的"字面"和"真实"之间的转换关系,
     必须严格遵守对应类型的判读策略。
   - 同一信号在不同场景性质下含义不同,需先识别本段的场景性质
     (冲突/关怀/亲昵/独处/对峙/离别/重逢),再应用决策树。

4. **特殊文本模式 (NVL Mode - 非常重要)**:
   除了普通的 `dialogue` 和 `narration`,当遇到以下两种情况时,你必须使用特定的 `beat_type`:

   - **情形 A [内心独白 → `nvl_thought`]**:
     当文本明确是角色的心理活动(带有"心想"、"暗忖",或被括号、特殊引号包裹的无声思考)时,`beat_type` 必须输出为 `nvl_thought`。`speaker_name` 必须是思考者的真名,根据内容赋予明确的 `emotion`。

   - **情形 B [设定科普/历史背景 → `nvl_narration`]**:
     `nvl_narration` 是"全屏字幕式"呈现,适合开场科普、世界观铺陈。**三个充要条件必须同时满足**:
     1. **时间维度脱离**:讲的不是"此刻发生"的事(历史背景、典故、传说、跨时间叙事)
     2. **空间维度脱离**:讲的不是"当前镜头能看到"的画面内容
     3. **长文本**:必须是较长的成段叙述(中文 ≥80 字 / 英文 ≥150 字符)。过短的零散叙述不算。

     **只要有一条不满足,就不是 `nvl_narration`**(应改判 `narration`)。`speaker_name` 必须是"旁白",`emotion` 强制 `calm`。

     正例:"天玄大陆,以武为尊。三百年前的神魔大战,改变了法则……"(历史背景 + 抽象 + 成段长文)
     反例:"这客栈其实是尹三的产业。"(讲的是当前场景,空间未脱离 → 应是 `narration`)
     反例:"众人沿着规划路线向东走。"(讲的是当下行动,时间未脱离 → 应是 `narration`,并按规则 2 处理)
     反例:"三百年前曾有一战。"(虽是历史背景,但太短不成段 → 应是 `narration`)

   - **情形 C [系统提示音/弹窗通知 → `narration` (speaker = 系统)]** (系统文专用):
     系统文中常有"系统"用文字向主角发送的提示音/通知,**即便被引号包裹**也**不是 dialogue**(没有真正的"角色"在说),必须判为 `narration`,且 `speaker_name` 必须是 **"系统"** (而非"旁白"),`emotion` 强制 `calm`。

     **触发特征(满足任一条即视为系统提示音)**:
     1. 开头带 "叮，"、"叮!"、"恭喜宿主"、"检测到宿主"、"系统检测到"、"奖励" 等
     2. 内容含被书名号或特殊括号包裹的宿主名,如 `宿主『XX』`、`宿主「XX」`
     3. 内容明显在描述系统行为:"获得 N 点XX值"、"激活XX系统"、"开启XX功能"、"扣除X点"

     正例:`"叮，恭喜宿主『徐缺』装逼成功，奖励装逼值1点。"` →
       `{{"beat_type": "narration", "speaker_name": "系统", "content": "叮，恭喜宿主『徐缺』装逼成功，奖励装逼值1点。", "emotion": "calm", ...}}`

     反例(不要套用):`徐缺脑海里响起了系统提示音。` ← 这是"描述系统行为"的旁白,**不带系统语音的文字内容**,按普通 `narration`(speaker=旁白)处理。

     **不要把系统提示音拼进相邻角色台词中**:即便它紧贴在某个角色台词后,也应单独成一个 `narration` beat。

5. **代词消解 (重要)**:
   - 必须结合 <previous_context> 将文中出现的"她"、"他"、"那个男人"、"说话者"替换为**角色的真名**（如"楚瑶"、"厉寒霆"）。
   - **严禁输出代词**：`speaker_name` 字段绝对不允许出现 "她", "他", "男人", "女人", "未知", "说话者"。

7. **对话清洗**:
   - 去除对话标签（如"他冷笑道"、"厉寒霆问"），只保留引号内的台词。

7B. **对话长度与自然拆分(策划硬性要求 - 极其重要)**:
   - **作用对象**: 本规则**仅约束 `dialogue` 类型**。`nvl_thought` / `narration` / `nvl_narration` **不受此长度规则约束**,按各自规则正常输出即可。
   - **长度目标**: 单条 `dialogue` 的 content **应控制在 100 个汉字以内**(包括标点)。
   - **硬天花板**: 单条 `dialogue` 的 content **绝对不允许超过 150 个汉字**。超过即视为严重违规。
   - **当原文台词较长时,必须按"自然拆分铁律"处理,不允许删信息只允许拆 beat。**

   ### 自然拆分铁律

   **核心原则**: 把同一角色的连续长台词,按自然语义切成 N 个连续的 `dialogue` beat,信息完整保留,在 JSON 数组中按原文顺序连续排列,中间不要插入旁白。所有拆出的 beat 共享相同的 `speaker_name`/`speaker_id`;`emotion` 可根据每个片段的语气微调。

   **切点优先级(从高到低,优先用高优先级的切点)**:
   1. **语义段落切点**: 话题转换处、举例引出处、新主张提出处。
   2. **强标点切点**: 句号"。"、感叹号"！"、问号"？"后。
   3. **转折/递进切点**: 含"不过"、"但是"、"而且"、"再说"、"还有"等连接词的逗号处。
   4. **省略号"…"前后**: 是天然的语气停顿,可切。

   **拆分尺度建议**: 单个拆出的 beat **尽量接近但不超过 100 字**。避免出现 "1 字 + 99 字" 这种极端不均衡的拆分。如果一句台词只有 105 字,可以**保留整句不拆**(在硬天花板内);只有明显超过 100 字才需要拆。

   ### 反例 vs 正例

   **错误做法(整段 130 字塞进一个 beat)**:
   ```json
   {{ "beat_type": "dialogue", "speaker_name": "角色A", "content": "当然是善事啊!你听我说...先听我说!好友,这几年各地大旱,很多人都被饿死,所以啊,有快饿死的人家把孩子卖出去换钱粮。正巧,北边有一只大肥羊!他有的是钱,但就是没有孩子,便想从穷地方收几个来养!", "emotion": "happy" }}
   ```

   **正确做法(按语义切成 3 个连续 beat)**:
   ```json
   [
     {{ "beat_type": "dialogue", "speaker_name": "角色A", "content": "当然是善事啊!你听我说...先听我说!", "emotion": "happy" }},
     {{ "beat_type": "dialogue", "speaker_name": "角色A", "content": "好友,这几年各地大旱,很多人都被饿死,有快饿死的人家把孩子卖出去换钱粮。", "emotion": "calm" }},
     {{ "beat_type": "dialogue", "speaker_name": "角色A", "content": "正巧,北边有一只大肥羊!他有的是钱,但没有孩子,便想从穷地方收几个来养!", "emotion": "happy" }}
   ]
   ```

8. 相对称谓消歧义 (重要):
   - **现象**：小说中极常出现"娘"、"爹"、"师父"、"老大"、"少爷"等【相对关系称谓】。
   - **指令**：相对称谓指代谁，完全取决于【当前说话人】或【当前主视角】是谁。你【严禁】将某个通用称谓全局死绑在某一个角色上！
   - **操作步骤**：遇到相对称谓时，必须先在逻辑上补全"这是【谁的】娘/师父/老大？"，然后再根据上下文关系，将其映射为 VIP 名单中的具体真名。

9. 倒装提示语防错位 (重要):
   - **现象**：许多作者喜欢"台词在前，提示语在后且单独换行"的排版（例如：\\n"你好。"\\n男人说道。\\n"你也好。"）。
   - **指令**：当你遇到单独成行且没有台词的短句（如"某某说道。"、"某某叹了口气。"）时，你必须判定它是对【上一句】台词的补充说明，它代表了上一句台词的 Speaker。绝不可将其作为【下一句】台词的提示语，严防角色发生向下错位平移！

10. 无提示语交锋的强制锁（重要）：
   - **现象**：文中常出现连续的、没有"某某说"的引号对话（如：A一句，B一句，A一句，B一句），且中间可能穿插了大段的心理描写或旁白。
   - **核心红线**：一旦进入这种"双人对谈"模式，你【必须】严格追踪当前交锋的【唯二】两个角色是谁！严禁在没有明确提示词的情况下，凭空让第三个角色突然插嘴！
   - **防雷动作**：当看到台词中出现相对称谓时，绝对不可无脑匹配字面关系（看到"娘"就填小孩）。你必须向前后寻找【谁在执行当前动作】(例如:上文是 A 在喂汤,那么问"还要不要喝"的人**必定**是 A)。
   - **与规则9的仲裁**:如果"独立成行的提示语"中【明确写出了第三人的真名】,那么以规则9为准,该真名为上一句台词的真实说话人,**当前双人对话锁解除**,后续重新追踪新的对话组合。规则9只有在"提示语角色明确出现在 VIP 名单或上下文已建立的角色集合中"时才能突破规则10的对话锁。

11. **陌生描述性称呼的归属判断 (最高优先级 — 通用小说必备)**:
   - **背景**:GROUP A 列出的别名是**经过严格过滤的可靠映射**,所有可能产生歧义的描述性别名(含"的"字短语、以"姑娘/仙女/少年/老婆婆"等泛指身份词结尾的称呼、过长的描述短语等)**已经被系统主动剔除**。这意味着:
     - **原文里出现的、不在任何 VIP 别名列表中的描述性称呼,必须由你根据上下文重新判断归属,绝不可凭"语义相近"硬塞给某个 VIP**。
     - 例如,VIP 名单中没有列出"仙子"这个别名时,你看到原文写"那位仙子",**不能**因为某 VIP 是"修仙者"就把这句台词归给他/她。

   - **归属判断决策流程(必须严格按顺序执行)**:
     **第 1 步**:扫描该称呼**所在段落 ±3 句**,查找以下任一**强证据**:
       (A) 同段内有某 VIP 的**真名**出现,且该 VIP 与"这个称呼"的动作描述对得上(例:"唐雪茹挥剑刺去,这仙子的剑法极快" — 真名同段且承接同一动作)。
       (B) 该称呼的动作/能力/形貌描写与某 VIP 的**独有特征**完全吻合(例:已知 VIP X 拥有"踩剑飞行"能力,此处描述"踩剑飞行的女子",且段落内无其他 VIP 在场)。
       (C) 该称呼在对话中被另一名 VIP **直接喊出真名**(例:某长老说"雪茹,以剑封他",而前后描述都是"那女子如何如何")。
       (D) 该称呼的身份、职业、所属场所或所持立场,与某 VIP `〔简介〕`中记载的内容**具体且唯一**地吻合,
           且本段内没有其他 VIP 的简介同样吻合(仅"同属一类人"不算吻合,必须是能把该 VIP 与同类角色区分开的细节)。
     **第 2 步**:四个强证据都没有时,**绝对禁止**归给任何 VIP。改为以下两种处理之一:
       - 该称呼是泛指身份词(如"老婆婆"、"少年"):直接使用该称呼作为 NPC 名(`speaker_name = "老婆婆"`,`speaker_id = LaoPoPo`)。
       - 该称呼是描述性短语(如"白衣女子"):用 PascalCase 缩成 NPC 名(`speaker_name = "白衣女子"`,`speaker_id = BaiYiNvZi`)。
     **第 3 步**:即便某 VIP 名单中的角色与这个称呼"看起来很像",**只要不满足第 1 步任一强证据,就当作 NPC 处理**。

   - **铁律口号**:**"宁可归为 NPC,不可误绑 VIP"**。这条规则的优先级**高于**"必须使用 VIP 真名"。一个被错绑的 VIP 会污染整章台词,一个被漏掉的 NPC 仅仅是少了个声优档位。

   - **反例(必须避免)**:
     原文片段:"那位仙子御剑飞来,落在徐缺面前,冷哼一声。'淫贼,纳命来!'"
     VIP 名单:`徐缺` / `小柔`(无别名);徐缺动作是"草丛里抬头",小柔在本段无任何动作/真名出现。
     **错误做法**:把"仙子"归给小柔,理由"小柔是个女角色"。
     **正确做法**:第 1 步 — 段落 ±3 句内,小柔真名未出现,无独有特征匹配,无被他人喊名 → 第 2 步:"仙子"是泛指身份词 → `speaker_name = "仙子"`,`speaker_id = XianZi`,按 NPC 处理。

12. **群体提示语的逐句拆分 (通用叙事手法 — 极其重要)**:
   - **现象**:作者极常用**一个群体提示语带出连续多句台词**,这些台词其实出自群体中**不同的人**,每人一句。
     常见写法:"同事们猜测道:""众人纷纷开口:""几个弟子七嘴八舌:""下面有人喊道…又有人接道…"。
   - **核心红线**:`speaker_name` **绝对不允许**填群体称呼。禁止输出"同事们""众人""大家""村民们""几个弟子""一群人"这类指向多人的标签。
     理由:`speaker_name` 是**单个说话者**的身份。填群体标签会让 N 个不同的人共用一个音色档位,
     下游配音无法区分,并会产出"某句台词 + 对该句的附和"同属一个说话人的逻辑矛盾。

   - **两种群体发声必须先区分**:
     (a) **轮流型**(绝大多数):群体提示语后跟着 N 句语气/立场各异的台词,常见彼此**问答、反驳或附和**
         ("对啊""那可不一样""我也刷到了")。这是 **N 个人各说一句**,必须拆成 N 个不同的说话人。
     (b) **齐声型**(少数):原文明确写了"齐声""异口同声""众人合唱""整齐地喊道"。这是一次真正的集体发声,
         可作为**一条** beat,`speaker_name` 用该群体的**单数身份名**(如"众弟子"合唱 → 仍属集体,按下方第 3 步兜底处理)。

   - **轮流型的说话人命名决策流程(严格按顺序)**:
     **第 1 步 — 先找具名个体**:若某句台词能凭规则 11 的强证据锁定到某个 VIP(真名同段出现、独有特征吻合、
       被他人喊名、或〔简介〕具体且唯一吻合),照常填该 VIP 真名。群体提示语**不妨碍**其中某句归属具名角色。
       注意:群体中往往混有已具名的角色(如原文"老宋摇摇头"就在群体对话里),这些句子必须归给他本人。
     **第 2 步 — 续编号(优先)**:若上方 GROUP A 的【序号型群演系列】里存在与该群体**同一身份**的系列
       (如群体是"同事们",而名单里有 同事A/同事B/同事C/同事D),则**从该系列下一个可用字母继续排**:
       第 1 个未归属的说话人 = 同事E,第 2 个 = 同事F,第 3 个 = 同事G,依此类推。
       - 名单已给出每个系列的下一个起始字母,直接沿用,不要跳号也不要重复已占用的字母。
       - **严禁**把新台词硬塞给已有编号(同事A~D)。已有编号各自绑定了〔简介〕里记载的**特定台词**;
         新场景的新台词不属于他们,硬塞等于把不同人的台词并到一个人头上。
       - 同一段群体对话内,**同一个字母只能代表同一个人**:若某人在这段里说了两句(如先提问、后补充),
         两句用同一字母;不同人必须用不同字母。
     **第 3 步 — 无系列可续时**:GROUP A 里没有同身份的序号系列(如群体是"村民们",名单里无"村民A/B"),
       则用**该身份的单数形式 + 大写字母**自建编号:"村民A""村民B""村民C"。
       - 身份名取群体称呼去掉复数标记后的**单数身份词**("同事们"→"同事","众弟子"→"弟子","一群村民"→"村民")。
       - 从 A 开始排,同一段内一人一字母。
     **第 4 步 — 实在无法区分人数时**:若原文确实只有一句集体台词、无法判断出自几人,
       宁可**只输出这一句**并用单数身份名 + A(如"村民A"),也不要使用群体标签。

   - **示例(轮流型 + 续编号)**:
     原文:
     ```
     "小陈刚进公司这就又走了？忙什么呐。"
     同事们猜测道：
     "看着样子像是找到下家了？"
     "这行业哪还有下家？"老宋摇摇头。
     "前一阵送外卖的时候，他不是跑了半个月？这次不会摆摊去了吧。"
     "摆摊可不好干。"
     "对啊，那能赚到钱吗？"
     ```
     GROUP A 序号系列:「同事」已占用 同事A/B/C/D → 新角色从 同事E 起排。名单另有 VIP `宋立伟`(别名: 老宋)。
     **错误做法**:把后 4 句全填 `speaker_name = "同事们"`(群体标签,4 个人挤进 1 个音色档)。
     **同样错误**:把后 4 句塞回 同事A~D(他们的〔简介〕记的是另一场戏的台词)。
     **正确做法**:
     ```json
     [
       {{ "beat_type": "dialogue", "speaker_name": "宋立伟", "content": "小陈刚进公司这就又走了？忙什么呐。", "emotion": "surprised" }},
       {{ "beat_type": "dialogue", "speaker_name": "同事E", "content": "看着样子像是找到下家了？", "emotion": "calm" }},
       {{ "beat_type": "dialogue", "speaker_name": "宋立伟", "content": "这行业哪还有下家？", "emotion": "calm" }},
       {{ "beat_type": "dialogue", "speaker_name": "同事F", "content": "前一阵送外卖的时候，他不是跑了半个月？这次不会摆摊去了吧。", "emotion": "surprised" }},
       {{ "beat_type": "dialogue", "speaker_name": "同事G", "content": "摆摊可不好干。", "emotion": "calm" }},
       {{ "beat_type": "dialogue", "speaker_name": "同事H", "content": "对啊，那能赚到钱吗？", "emotion": "calm" }}
     ]
     ```
     要点:"这行业哪还有下家？"带独立提示语"老宋摇摇头",按规则 9 归 `宋立伟`(第 1 步优先);
     其余 4 句是 4 个未具名同事,从 E 起续排;"对啊"是在附和上一句,说明它与上一句**必定不是同一个人**。

   - **群体提示语本身的处理**:"同事们猜测道:""众人纷纷开口:"这类只起引出作用、不含台词内容的提示语,
     按规则 7 的对话清洗**删掉**,不单独成 beat,也不要塞进任何角色的台词里。

   - **铁律口号**:**"群体标签零容忍 —— 有几句话就有几个人,先找具名个体,再按系列续编号。"**

# Output Format

严格要求,任何违反都会导致下游解析失败:

1. **只返回 JSON 数组**,以 `[` 开头,以 `]` 结尾。
2. **JSON 必须完整闭合**: 生成最后一个 beat 后,务必输出 `}}` 闭合该对象,再输出 `]` 闭合数组。**宁可少几个 beat,也要保证 JSON 结构完整**。严禁生成到一半就结束。
3. **严禁** markdown 代码围栏(如 ```json ... ```)。
4. **严禁** 在数组前后追加任何标题、说明、注释(如 `## Output JSON:`、`下面是结果:`)。
5. **严禁** 在 JSON 后追加"(注意:...)"这类解读。

# 字符串内引号铁律(防 JSON 损坏 - 极其重要)

JSON 字符串由英文双引号 `"..."` 包裹。content 字段内部如果出现英文引号会破坏 JSON 解析。规则:

- content 内部的任何英文双引号 `"` → 必须替换为中文双引号 `"` 或 `"`
- content 内部的任何英文单引号 `'` → 必须替换为中文双引号 `"` 或 `"`
- content 内部的任何中文引号 `"` `"` `「` `」` `'` `'` → 直接保留,不需转义
- 严禁 content 字符串内部出现裸的英文双引号或英文单引号
- 严禁 content 字符串内部出现换行符 `\\n` 或制表符 `\\t`,改为空格

你的整段输出第一个字符必须是 `[`,最后一个字符必须是 `]`。
"""
    else:
        system_prompt = f"""
# Role
Visual Novel Script Expert(Director Mode).

# Task
Extract structured script data from <current_text> ONLY.
**Use <previous_context> as READ-ONLY reference to identify characters (e.g., resolving 'he'/'she'). DO NOT extract text from context.**

{conflict_warning}
{dynamic_pov_rules}
# CAST SYSTEM (CRITICAL)

## GROUP A: THE VIPs (Strict Mapping)
**You must map any descriptions of these characters to their Real Names:**
[{vip_list_str}]

* **Rule**: If a character matches the description of a VIP, YOU MUST USE THE VIP NAME.
* *Example*: If "Daniel" is in the list, "The boy" -> `Daniel`.
* *Example*: If "Lestat" is in the list, "The man in velvet" -> `Lestat`.
* **The 〔profile〕 is key evidence for line attribution**: each VIP may carry a
  `〔profile: ...〕` stating their role, occupation, affiliation, stance and known behaviour.
  When several characters have similar appellations, or the text refers to someone only by a
  generic or descriptive title, **compare the profiles before assigning the line**:
  - The line's content or setting matches a character's stated role, occupation or affiliation
    → attribute it to that character.
  - The stance or claim expressed in the line matches a character's recorded stance
    → attribute it to that character.
  - Kinship appellations ("<someone>'s mother", "<someone>'s daughter") resolve to a real name
    through the relationships recorded in the profiles.
  - For groups of extras distinguished only by a letter or number (same occupation, same scene),
    the profile is often the only discriminating clue.
* If a profile contradicts the current passage, **the text wins**: profiles are prior context and
  must not override what the passage actually states.

## GROUP B: THE NPCs (Standardized Roles)
If a character is definitely NOT a VIP, use a **Standardized Role Name**:
{npc_guidelines}

# CRITICAL RULES

1.  **Cast Alignment (High Priority)**:
    - Strictly enforce GROUP A and GROUP B logic.
    - Resolve "He"/"She"/"The Man" to a specific VIP Name or NPC Role.
    - **Forbidden**: Do not output "The Man", "The Boy", "Stranger" if they are VIPs.

2. **Narration Reduction (CRITICAL)**:
   - **Visual Filtering**: Assume the Background (BG) and Sprites are visible. **REMOVE** descriptions of clothes, furniture, weather, and facial features if static.
   - **Keep**: Internal thoughts, Smells, Touch, Abstract Atmosphere, Critical Actions.
   - *Example*: "She stood there in a red dress." -> **DELETE** (Sprite shows it).
   - *Example*: "The smell of blood made him sick." -> **KEEP** (Can't see smell).

3. **Character Emotion (Signal-Based — Critical)**:
   - **Allowed values**: `calm`, `happy`, `sad`, `angry`, `fearful`, `surprised`, `disgusted`
   - **Scope**: emotion applies ONLY to `dialogue` and `nvl_thought`.
     `narration` / `nvl_narration` are forced to `calm` and do not participate.

   **Emotion Signal Taxonomy (8 categories × 4 dimensions, language-agnostic)**:
{EMOTION_SIGNAL_TAXONOMY_EN}

   **Character Type Playbook** (each VIP carries an `[emotion_type: xxx]` tag in GROUP A):
{EMOTION_TYPE_PLAYBOOK_EN}

   **Decision Tree (top to bottom, first match wins)**:
{EMOTION_DECISION_TREE_EN}

   **Absolute Red Lines**:
   - NEVER judge by literal words alone. You MUST consult the 1-2 surrounding narration
     sentences for physiological / posture / acoustic / verbal signals as primary evidence.
   - "Emotion-loaded words appearing in action description" ≠ "the character's real emotion".
     A line spoken "harshly" may not be angry if mask signals indicate it should reverse to sad/happy.
   - The character's `emotion_type` is a prior. It defines the conversion rule between
     "literal" and "real" emotion for that character. Adhere strictly to its playbook entry.
   - The same signal carries different meaning in different scene types
     (conflict / caring / intimate / solitary / standoff / parting / reunion).
     Identify scene type before applying the decision tree.

4. **Special Text Mode (NVL Mode - Extremely Important)**:
   Besides `dialogue` and `narration`, when one of the following two scenarios occurs, you must use a specific `beat_type`:

   - **Scenario A [Inner Monologue → `nvl_thought`]**:
     When the text is clearly the silent mental activity of a character (marked by "thought", "wondered", or wrapped in parentheses/special quotes), `beat_type` must be `nvl_thought`. `speaker_name` must be the real name of the thinker, with a clear `emotion` based on content.

   - **Scenario B [Setting Exposition / Historical Background → `nvl_narration`]**:
     `nvl_narration` is rendered as full-screen subtitles, suitable for opening lore and world-building. **All THREE conditions MUST be satisfied simultaneously**:
     1. **Temporal detachment**: Not describing "what is happening now" (historical background, legend, myth, cross-time narration)
     2. **Spatial detachment**: Not describing "what the current camera can see"
     3. **Long text**: Must be a substantial paragraph (Chinese ≥80 chars / English ≥150 chars). Short scattered statements do not qualify.

     **If even one condition is unmet, it is NOT `nvl_narration`** (use `narration` instead). `speaker_name` must be "Narrator", `emotion` forced to `calm`.

     Correct: "On Tian-Xuan continent, martial arts ruled. Three centuries ago, the war of gods and demons changed the laws. ..." (historical + abstract + substantial paragraph)
     Wrong: "This inn is actually Yin San's business." (describes current scene, spatial not detached → should be `narration`)
     Wrong: "The party walked east along the planned route." (current action, temporal not detached → should be `narration`, handle per rule 2)
     Wrong: "There was a great war three centuries ago." (historical but too short → should be `narration`)

   - **Scenario C [System notification / pop-up → `narration` (speaker = System)]** (System-novel specific):
     In "system novels" (系统文), the in-world System sends text notifications to the protagonist. Even when wrapped in quotes, these are **NOT dialogue** (no real "character" is speaking) — they must be `narration` with `speaker_name = "System"` (not "Narrator"), and `emotion` forced to `calm`.

     **Triggers (any one satisfies)**:
     1. Starts with "Ding," / "Congratulations, host" / "Detected: host" / "Reward:" / "System detected"
     2. Contains the host's name in special brackets like `host [XX]`, `host 『XX』`
     3. Describes system actions verbatim: "gained N points", "activated XX system", "deducted N points"

     Example: `"Ding! Congratulations, host 『Xu Que』, you successfully showed off. Reward: 1 BragPoint."` →
       `{{"beat_type": "narration", "speaker_name": "System", "content": "...", "emotion": "calm", ...}}`

     Counter-example (do NOT apply): `Xu Que heard the system's notification chime in his mind.` ← This is NARRATION *describing* a system action; it carries no system-voice content. Treat as a normal `narration` with speaker = Narrator.

     **Do not merge system notifications into adjacent character dialogue** — even if it appears right after a character line, emit it as its own `narration` beat.

5. **Pronoun Resolution (Identity Persistence)**:
   - You MUST resolve pronouns ("he", "she", "the man") to **Real Names** (e.g., "Louis", "Lestat") using the context.
   - **FORBIDDEN**: Do not output "She", "He", "Man", "Woman", "Unknown", "The Speaker" as `speaker_name`.

7. **Dialogue Cleaning**:
   - Remove speech tags (e.g., "he said", "cried Louis"). Keep only the spoken dialogue.

7B. **Dialogue Length and Natural Splitting (CRITICAL - PRODUCT REQUIREMENT)**:
   - **Scope**: This rule applies **ONLY to `dialogue` beats**. `nvl_thought` / `narration` / `nvl_narration` are **NOT subject to this length constraint** — emit them naturally per their own rules.
   - **Length target**: A single `dialogue` content should be **kept within 150 characters** (including punctuation).
   - **Hard ceiling**: A single `dialogue` content **absolutely must not exceed 220 characters**. Exceeding this is a serious violation.
   - **When the original speech is long, you MUST apply the Natural Splitting Rules below — never delete information, only split into more beats.**

   ### Natural Splitting Rules

   **Core principle**: Split one character's long continuous speech into N consecutive `dialogue` beats, preserving all information. Place them in consecutive array order without inserting narration between them. All split beats share the same `speaker_name`/`speaker_id`; `emotion` may vary per fragment based on local tone.

   **Split point priority (high to low — prefer higher priority cuts)**:
   1. **Semantic paragraph break**: topic shift, introducing an example, presenting a new claim.
   2. **Strong punctuation**: after period `.`, exclamation `!`, or question mark `?`.
   3. **Transition/contrast comma**: at a comma followed by "however", "but", "moreover", "besides", "also", "still".
   4. **Ellipsis "..."**: a natural breath/pause point.

   **Splitting granularity**: Each split beat should approach but **not exceed 150 characters**. Avoid extremely uneven splits like "5 chars + 145 chars". If a line is 160 characters, **keep it whole** (within the hard ceiling); only split when clearly exceeding the soft target.

   ### Counter-example vs Correct example

   **WRONG (cramming 200+ chars into one beat)**:
   ```json
   {{ "beat_type": "dialogue", "speaker_name": "CharacterA", "content": "Of course it's a good deed! Listen to me — listen first! These years droughts have hit everywhere, many people are starving, so families on the verge of starvation sell their children for grain. As it happens, there is a rich man in the north — plenty of money but no children, who wants to take a few from poor regions to raise!", "emotion": "happy" }}
   ```

   **CORRECT (split by semantic cuts into 3 consecutive beats)**:
   ```json
   [
     {{ "beat_type": "dialogue", "speaker_name": "CharacterA", "content": "Of course it's a good deed! Listen to me — listen first!", "emotion": "happy" }},
     {{ "beat_type": "dialogue", "speaker_name": "CharacterA", "content": "These years droughts have hit everywhere, many people are starving, so families sell their children for grain.", "emotion": "calm" }},
     {{ "beat_type": "dialogue", "speaker_name": "CharacterA", "content": "As it happens, there's a rich man in the north — plenty of money but no children, who wants to take a few from poor regions to raise!", "emotion": "happy" }}
   ]
   ```

8. Relational Title Disambiguation (Important):
   - **Phenomenon**: Novels frequently use [relational titles] such as "Mother", "Father", "Master", "Boss", or "Young Master".
   - **Directive**: Who a relational title refers to depends ENTIRELY on the [current speaker] or [current POV]. You are STRICTLY FORBIDDEN from permanently binding a generic title to one specific character globally!
   - **Operation**: When encountering a relational title, you must first logically complete the thought: "Whose Mother/Master/Boss is this?", and then map it to the specific True Name in the VIP list based on the contextual relationship.

9. Trailing Speech Tag Prevention (Important):
   - **Phenomenon**: Many authors use a formatting style where the "dialogue comes first, followed by the speech tag on a separate new line" (e.g., \\n"Hello."\\nThe man said.\\n"Hello to you too.").
   - **Directive**: When you encounter a short, standalone line without dialogue (e.g., "Someone said.", "Someone sighed."), you MUST treat it as a supplementary description for the [PREVIOUS] line of dialogue; it identifies the Speaker of the PREVIOUS line. You absolutely MUST NOT treat it as the speech tag for the [NEXT] line of dialogue. Strictly prevent characters from shifting and misaligning downwards!

10. Ping-Pong Dialogue Continuity Lock (Important):
   - **Phenomenon**: The text often features continuous quoted dialogue without speech tags like "someone said" (e.g., A speaks, B speaks, A speaks, B speaks), and there may be large chunks of psychological description or narration inserted in between these lines.
   - **Critical Red Line**: Once entering this "two-person conversation" mode, you MUST strictly track exactly who the [ONLY TWO] characters involved in the current exchange are! You are strictly forbidden from letting a third character suddenly interrupt out of nowhere without explicit speech tags!
   - **Preventative Action**: When you see a relational title in the dialogue (e.g., "Mother, do you want to drink some more?"), you absolutely MUST NOT mindlessly match the literal relationship. You MUST look forward and backward to find [who is performing the current action].

11. **Unfamiliar Descriptive Title Attribution (HIGHEST PRIORITY — Universal novel rule)**:
   - **Background**: The aliases listed under GROUP A are **strictly filtered reliable mappings**. All ambiguity-prone descriptive aliases (phrases containing "of/with/in", titles ending in generic identity nouns like "girl/boy/old woman/young man", overly long descriptive phrases) **have been proactively stripped out by the upstream system**. This means:
     - **Any descriptive title appearing in the text that is NOT in any VIP's alias list MUST be re-judged from context. You absolutely MUST NOT cram it into some VIP just because the meaning "feels close"**.
     - Example: If the VIP list does NOT contain "the fairy" as an alias, and the text says "the fairy said...", you **CANNOT** attribute that line to a VIP just because that VIP happens to be "a cultivator".

   - **Attribution Decision Flow (follow in strict order)**:
     **Step 1**: Scan **±3 sentences** around the descriptive title for any of these **strong signals**:
       (A) A VIP's **real name** appears in the same paragraph AND the action described matches (e.g., "Mary swung her sword — the woman's swordsmanship was swift" — real name in same paragraph, picking up the same action).
       (B) The action/ability/appearance described matches a VIP's **unique established trait** (e.g., a previously established trait for VIP X is "can fly on a sword", and the text says "the woman flying on a sword", with no other VIPs present).
       (C) The title is **explicitly addressed by another VIP using a real name** (e.g., an Elder says "Mary, seal his retreat with your sword!", while surrounding narration just says "the woman did this and that").
       (D) The role, occupation, affiliation or stance implied by the title matches a VIP's `〔profile〕`
           **specifically and uniquely**, and no other VIP's profile matches equally well. Merely belonging
           to the same category does not count — the detail must distinguish this VIP from similar characters.
     **Step 2**: If none of the strong signals hold, **NEVER attribute to any VIP**. Choose one of:
       - The title is a generic identity noun (e.g., "old woman", "young man"): use it directly as the NPC name (`speaker_name = "OldWoman"`, `speaker_id = "OldWoman"`).
       - The title is a descriptive phrase (e.g., "woman in white"): compress into PascalCase NPC name (`speaker_name = "WomanInWhite"`, `speaker_id = "WomanInWhite"`).
     **Step 3**: Even if a VIP in the list "looks like a good match", **as long as Step 1 signals are absent, treat the title as an NPC**.

   - **Slogan**: **"Better tag as NPC than mis-bind to a VIP."** This rule's priority is **HIGHER than** "must use VIP real names". A mis-bound VIP pollutes an entire chapter's dialogue; a missed NPC merely means one less voice slot.

   - **Counter-example (must avoid)**:
     Text: `"The fairy descended on her sword, landed before Daniel, and snorted. 'Pervert, your time is up!'"`
     VIP list: `Daniel` / `Mary` (no aliases). Daniel's action is "looking up from grass"; Mary has no action/real-name presence in this paragraph.
     **WRONG**: Attribute "the fairy" to Mary because "Mary is a female VIP".
     **CORRECT**: Step 1 — Mary's real name is absent, no unique trait match, no addressed call → Step 2: "the fairy" is a generic identity term → `speaker_name = "Fairy"`, `speaker_id = "Fairy"`, treat as NPC.

12. **Splitting Collective Speech Tags Line by Line (universal narrative device — CRITICAL)**:
   - **Phenomenon**: Authors very often use **one collective speech tag to introduce several consecutive lines**,
     where those lines actually come from **different people** in the group, one line each.
     Common forms: "The colleagues speculated:", "Voices chimed in:", "The disciples all spoke at once:",
     "Someone shouted... then another added...".
   - **Red line**: `speaker_name` **must NEVER be a collective label**. Do not output "Crowd", "Everyone",
     "Colleagues", "Villagers", "TheOthers", "SeveralDisciples" or anything else denoting multiple people.
     Reason: `speaker_name` is the identity of **one** speaker. A collective label collapses N different people
     into one voice slot, and produces the logical contradiction of "a line + someone agreeing with that line"
     sharing a single speaker.

   - **First distinguish two kinds of group speech**:
     (a) **Turn-taking (the vast majority)**: the collective tag is followed by N lines with differing stances
         or tones, often **questioning, contradicting or agreeing with each other** ("Exactly", "Not the same",
         "I saw that too"). These are **N people speaking one line each** and must be split into N speakers.
     (b) **In-unison (rare)**: the text explicitly says "in unison", "as one", "chorused", "all shouted together".
         This is genuinely one collective utterance — emit **one** beat and fall through to Step 3 below for naming.

   - **Naming flow for turn-taking (follow in strict order)**:
     **Step 1 — Look for named individuals first**: if a line can be locked to a VIP by Rule 11's strong signals
       (real name in the same paragraph, unique trait match, addressed by name, or a specifically-and-uniquely
       matching `〔profile〕`), attribute it to that VIP as usual. A collective tag does **not** prevent individual
       lines from belonging to named characters. Groups frequently contain already-named characters whose own
       speech tags appear inline — those lines belong to them.
     **Step 2 — Continue the series (preferred)**: if GROUP A's [Serial extra series] contains a series of the
       **same identity** as the group (e.g. the group is "the colleagues" and the list has ColleagueA/B/C/D),
       **continue from the next available letter**: first unattributed speaker = ColleagueE, second = ColleagueF,
       third = ColleagueG, and so on.
       - The list states each series' next starting letter — use it, do not skip letters or reuse taken ones.
       - **NEVER** force new lines onto existing letters (ColleagueA–D). Each existing letter is bound to the
         **specific lines** recorded in its `〔profile〕`; lines from a new scene do not belong to them, and forcing
         them merges different people into one.
       - Within one group exchange, **one letter = one person**: if someone speaks twice (asks, then adds), reuse
         the same letter; different people must get different letters.
     **Step 3 — No series to continue**: if GROUP A has no serial series of that identity (e.g. group is
       "the villagers" and there is no VillagerA/B), build your own numbering from the **singular identity noun**
       plus a capital letter: "VillagerA", "VillagerB", "VillagerC".
       - Derive the identity noun by stripping the plural/collective marker ("the colleagues" → Colleague,
         "the disciples" → Disciple, "a group of villagers" → Villager).
       - Start at A; one letter per person within the passage.
     **Step 4 — When the head count is genuinely undeterminable**: if there is truly a single collective line and
       you cannot tell how many people spoke, emit that one line with the singular identity plus A
       (e.g. "VillagerA") rather than a collective label.

   - **Example (turn-taking + series continuation)**:
     Text:
     ```
     "Chen just got in and he's off again? What's he up to."
     The colleagues speculated:
     "Looks like he's lined up another job?"
     "What other job is there in this industry?" Old Song shook his head.
     "Didn't he run deliveries for half a month last time? Don't tell me he's going street vending."
     "Street vending is rough."
     "Right, can that even make money?"
     ```
     GROUP A serial series: "Colleague" has A/B/C/D taken → new ones start at ColleagueE. The list also has
     VIP `SongLiwei` (alias: Old Song).
     **WRONG**: give the last 4 lines `speaker_name = "Colleagues"` (4 people crammed into 1 voice slot).
     **ALSO WRONG**: push them back onto ColleagueA–D (whose profiles record lines from a different scene).
     **CORRECT**:
     ```json
     [
       {{ "beat_type": "dialogue", "speaker_name": "SongLiwei", "content": "Chen just got in and he's off again? What's he up to.", "emotion": "surprised" }},
       {{ "beat_type": "dialogue", "speaker_name": "ColleagueE", "content": "Looks like he's lined up another job?", "emotion": "calm" }},
       {{ "beat_type": "dialogue", "speaker_name": "SongLiwei", "content": "What other job is there in this industry?", "emotion": "calm" }},
       {{ "beat_type": "dialogue", "speaker_name": "ColleagueF", "content": "Didn't he run deliveries for half a month last time? Don't tell me he's going street vending.", "emotion": "surprised" }},
       {{ "beat_type": "dialogue", "speaker_name": "ColleagueG", "content": "Street vending is rough.", "emotion": "calm" }},
       {{ "beat_type": "dialogue", "speaker_name": "ColleagueH", "content": "Right, can that even make money?", "emotion": "calm" }}
     ]
     ```
     Key points: "What other job is there" carries its own tag "Old Song shook his head" → Rule 9 assigns it to
     `SongLiwei` (Step 1 takes precedence); the other 4 lines are 4 unnamed colleagues numbered from E; "Right,"
     agrees with the previous line, proving it **cannot** be the same person as that line.

   - **The collective tag itself**: tags like "The colleagues speculated:" / "Voices chimed in:" carry no dialogue
     content and only introduce speech — strip them per Rule 7's dialogue cleaning. Do not emit them as a beat and
     do not fold them into any character's line.

   - **Slogan**: **"Zero tolerance for collective labels — as many lines as there are, that many people. Named
     individuals first, then continue the series."**

# ONE-SHOT EXAMPLE (Complete Demo — signal-based emotion judgment)
NOTE: All names below are format placeholders. Use the actual VIPs from GROUP A above.

## Input:
Character profiles in GROUP A:
[CharacterA - stoic_restrained] [CharacterB - tsundere_masked]

<current_text>
In the year 2042, the Neo-Virus wiped out half of the Earth's population. Survival became the only law.
The full moon shone on the castle. CharacterA sat on the velvet chair.
"Leave this place," he said, his voice low, fingers tightening on the armrest.
CharacterB watched him, eyes suddenly going red. She turned her face away sharply.
"Fine! Who cares!" Her voice cracked despite the harsh words.
Did he know about the betrayal all along? No, that's impossible! CharacterB thought, her chest tightening.
Outside the door, an armed driver shouted, "We are under attack!"
</current_text>

## Output JSON:
[
  {{ "beat_type": "nvl_narration", "speaker_name": "Narrator", "content": "In the year 2042, the Neo-Virus wiped out half of the Earth's population. Survival became the only law.", "emotion": "calm" }},
  {{ "beat_type": "narration", "speaker_name": "Narrator", "content": "CharacterA sat on the velvet chair.", "emotion": "calm" }},
  {{ "beat_type": "dialogue", "speaker_name": "CharacterA", "content": "Leave this place.", "emotion": "sad" }},
  {{ "beat_type": "narration", "speaker_name": "Narrator", "content": "CharacterB's eyes went red, and she turned her face away.", "emotion": "calm" }},
  {{ "beat_type": "dialogue", "speaker_name": "CharacterB", "content": "Fine! Who cares!", "emotion": "sad" }},
  {{ "beat_type": "nvl_thought", "speaker_name": "CharacterB", "content": "Did he know about the betrayal all along? No, that's impossible!", "emotion": "surprised" }},
  {{ "beat_type": "dialogue", "speaker_name": "ArmedDriver", "content": "We are under attack!", "emotion": "fearful" }}
]

# Teaching notes (why these emotions):
- CharacterA (stoic_restrained): literal "calm" line "Leave this place" + Category 1 sadness signals
  ("voice low" + "fingers tightening") → judge SAD per Decision-Tree rule (1). His type
  (stoic_restrained) confirms his calm surface masks real sadness.
- CharacterB (tsundere_masked): literal "angry" line "Fine! Who cares!" + Category 1 signals
  ("eyes going red" + "voice cracked") in a caring/parting scene context → judge SAD,
  reversing the literal anger per Decision-Tree rule (2).
- CharacterB's "Did he know about the betrayal all along?" with "chest tightening" matches
  Category 3 surprise + disbelief → judge SURPRISED.
- ArmedDriver: literal alarm-cry + scene type "standoff/attack" → judge FEARFUL.

# Output Format

Strict requirements — any violation breaks downstream parsing:

1. **Return ONLY the JSON array**, starting with `[` and ending with `]`.
2. **JSON must close properly**: after the last beat, output `}}` to close the object, then `]` to close the array. **Better to emit fewer beats than to leave the JSON half-finished.** Never stop mid-generation.
3. **DO NOT** wrap with markdown code fences (e.g. ```json ... ```).
4. **DO NOT** prepend or append any title, explanation, comment (e.g. `## Output JSON:` or `Here is the result:`).
5. **DO NOT** append "(Note: ...)" interpretive remarks after the JSON.

# String Quote Integrity Rule (Prevent JSON Corruption - CRITICAL)

JSON strings are wrapped in straight double quotes `"..."`. If the content field internally contains straight quotes, JSON parsing breaks. Rules:

- Any straight double quote `"` inside content → MUST be replaced with curly double quote `"` or `"`
- Any straight single quote / apostrophe `'` inside content (e.g. "it's", "don't") → MUST be replaced with curly single quote `'` or fully removed
- Curly quotes `"` `"` `'` `'` and CJK brackets `「」` → keep as-is, no escaping needed
- FORBIDDEN: bare straight `"` or straight `'` inside content string
- FORBIDDEN: `\\n` or `\\t` characters inside content; replace with a space

Your output's first character MUST be `[`, last character MUST be `]`.
"""

    async with semaphore:
        response, used_model = await _model_fallback.achat(
            client,
            messages=[
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_content_str}
            ],
            temperature=0.3
        )
        if _usage_tracker and response.usage:
            _usage_tracker.add_llm_usage(
                response.usage, used_model, provider="openai_compatible"
            )

        raw_content = response.choices[0].message.content

        try:
            parsed = robust_json_parse(raw_content, context="beats")
            if not isinstance(parsed, list):
                raise ValueError(f"Expected JSON list, got {type(parsed).__name__}: {str(parsed)[:100]}")
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"⚠️ JSON 校验失败，将触发重试。Error: {e}\n原始输出前300字:\n{raw_content[:300]}")
            raise  # 让 tenacity 捕获并重试

        return raw_content


def sanitize_full_name(full_name, canonical_names):
    """full_name 出口处的最终合法性闸门 (单点防御)。

    无论前面哪个环节 (post_process / reconcile / 兜底定稿) 出了岔子,
    这道闸在数据写入最终输出前统一兜底:**描述性短语绝不允许出现在 full_name**。

    合法的 full_name 只有两种:
      1. 角色库主名 (精确命中 canonical_names) —— confirmed
      2. 简洁的 NPC 称呼 (非描述性短语) —— 如"老者""青袍少年"

    非法 (描述性短语,如"御剑飞天的女子""穿白衣的男子") → 一律清成 None,
    speaker_id 也随之清 None,诚实标记"身份未确认"。

    返回:(合法 full_name 或 None, 对应 speaker_id 或 None)
    """
    if not full_name:
        return None, None
    # 主名集合命中 → 合法
    if canonical_names and full_name in canonical_names:
        return full_name, to_pascal_case_speaker_id(full_name)
    # 描述性短语 → 非法,清空
    if _has_descriptive_pattern(full_name):
        return None, None
    # 群体称呼("同事们""众人""三名老者")→ 非法:它不是任何个体的身份,
    # 若放行会让群体中 N 个人共用一个 speaker_id。
    if _is_collective_reference(full_name):
        return None, None
    # 简洁 NPC 称呼 → 合法
    return full_name, to_pascal_case_speaker_id(full_name)


def resolve_full_name(speaker_name, canonical_names):
    """根据表层称呼判定主名 (full_name)。

    双名机制核心:
    - speaker_name = 当前台词处角色被怎么称呼 (忠于原文表层,永不改写)
    - full_name    = 角色主名 (身份层)
    - speaker_id   = 由 full_name 转化 (full_name=None → speaker_id=None)

    判定口径 (用户锁定:精确匹配 name):
    - speaker_name 精确命中角色库主名集合 → confirmed,full_name = speaker_name
    - 否则 → pending,full_name = None (等待 reconcile pass 回填)

    为什么不认 other_names:
    上游 other_names 可能含错误别名 (历史教训:唐雪茹的别名被错绑给小柔),
    只信 name 字段最稳。描述性占位名 (如"御剑飞天的女子") 自然落入 pending,
    由后续共指消解 pass 基于原文证据回填。

    返回:(full_name, name_status)
      - (主名, "confirmed")  命中
      - (None, "pending")    未命中,待回填
    """
    if not speaker_name:
        return None, "pending"
    if canonical_names and speaker_name in canonical_names:
        return speaker_name, "confirmed"
    return None, "pending"


def post_process_beats_final(beats_stream_data, role_mapping):
    """清洗并将数据扁平化返回，恢复 speaker_id 逻辑

    A1 软约束: 对 dialogue
    超过硬天花板的记 ERROR,作为下一轮迭代的输入数据。
    """
    processed_list = []

    # 长度阈值(与提示词中的硬天花板对齐)
    CN_DIALOGUE_SOFT = 100  # 软目标
    CN_DIALOGUE_HARD = 150  # 硬天花板
    EN_DIALOGUE_SOFT = 150
    EN_DIALOGUE_HARD = 220

    # nvl_narration 最小长度门槛:不达标则降级为 narration。
    # nvl_narration 定位为"全屏字幕式"的长文本(设定科普/世界观/历史背景),
    # 必须有足够篇幅;过短的不配做 nvl_narration,归为普通 narration。
    CN_NVL_NARRATION_MIN = 80  # 中文按字符数
    EN_NVL_NARRATION_MIN = 150  # 英文按字符数

    soft_violations = 0
    hard_violations = 0
    nvl_downgraded = 0  # nvl_narration 因长度不足降级为 narration 的计数

    # 双名机制:角色库主名集合 (role_mapping 的 values 即所有角色的 name 字段)
    # 用于精确判定 speaker_name 是否为主名。
    # 防御:剔除 name 字段本身就是描述短语的情况 (角色库可能有错),
    # 否则会把表层称呼"御剑飞天的女子"当成 confirmed 主名,污染 full_name。
    canonical_names = set()
    if role_mapping:
        for _n in role_mapping.values():
            if _n and not _has_descriptive_pattern(_n):
                canonical_names.add(_n)

    for beat in beats_stream_data:
        if beat is None:
            continue

        if beat.get("speaker_id") == "System":
            # System hook/branch beats:full_name = speaker_name (confirmed)
            if "full_name" not in beat:
                beat["full_name"] = beat.get("speaker_name")
            processed_list.append(beat)
            continue

        # 系统文增强:system_panel 已由 parse_markdown_with_branches 预解析,
        # 不经过 LLM 抽取,这里直接透传保留(不参与情绪/长度等校验)
        if beat.get("beat_type") == "system_panel":
            # 兜底补齐必要字段
            panel_content = beat.get("content", "").strip()
            if not panel_content:
                continue
            processed_list.append({
                "beat_type": "system_panel",
                "speaker_name": beat.get("speaker_name") or "旁白",
                "full_name": beat.get("speaker_name") or "旁白",  # 旁白即主名,confirmed
                "speaker_id": beat.get("speaker_id") or "PangBai",
                "content": panel_content,
                "emotion": "calm",
                "_src_lines": beat.get("_src_lines", [-1, -1]),
            })
            continue

        raw_content = beat.get("content") or beat.get("text", "")
        clean_content = clean_universal_content(raw_content)

        if not clean_content:
            continue

        raw_speaker_name = beat.get("speaker_name")
        raw_speaker_id = beat.get("speaker_id")
        beat_type = beat.get("beat_type", "narration")

        if beat_type not in ["dialogue", "narration", "nvl_thought", "nvl_narration"]:
            beat_type = "narration"

        # nvl_narration 长度门槛:content 未达最小长度的,降级为普通 narration。
        # 判据按内容语言选择字符数阈值(中文 80 / 英文 150)。
        if beat_type == "nvl_narration":
            _is_cn_nvl = is_chinese_content(clean_content)
            _nvl_min = CN_NVL_NARRATION_MIN if _is_cn_nvl else EN_NVL_NARRATION_MIN
            if len(clean_content) < _nvl_min:
                beat_type = "narration"
                nvl_downgraded += 1

        # 系统文增强:识别"系统语音"(LLM 输出 speaker_name="系统" 或 "System" 形态)
        # 这里要在常规 narrator 判断**之前**处理,以避免被合并进 "旁白/PangBai"。
        # 注意:speaker_id == "System" 已在最上方早返回,这里只处理 LLM 输出的 speaker_name 字面值。
        is_system_voice = False
        if isinstance(raw_speaker_name, str) and raw_speaker_name.strip() in {
            "系统", "System", "system", "XiTong"
        }:
            is_system_voice = True

        if is_system_voice:
            # 系统语音强制走 narration(即便 LLM 误判为 dialogue)
            if beat_type == "dialogue":
                beat_type = "narration"
            final_speaker_name = "系统"
            final_speaker_id = "XiTong"
            final_emotion = "calm"

            processed_list.append({
                "beat_type": beat_type,
                "speaker_name": final_speaker_name,
                "full_name": "系统",  # 系统语音主名即"系统",confirmed
                "speaker_id": final_speaker_id,
                "content": clean_content,
                "emotion": final_emotion,
                "_src_lines": beat.get("_src_lines", [-1, -1]),
            })
            continue

        is_narrator = False
        if beat_type in ["narration", "nvl_narration"]:
            is_narrator = True
        if raw_speaker_name in ["Narrator", "旁白", "Story"]:
            is_narrator = True

        # 双名机制:
        #   final_speaker_name = 表层称呼 (忠于原文,不做 role_mapping 替换)
        #   final_full_name    = 主名 (confirmed) 或 None (pending)
        #   final_speaker_id   = 由 full_name 转化;pending 时为 None
        if is_narrator:
            # 旁白:表层名与主名一致,confirmed
            if is_chinese_content(clean_content):
                final_speaker_name = "旁白"
            else:
                final_speaker_name = "Narrator"
            final_full_name = final_speaker_name
            final_speaker_id = "PangBai"
        else:
            # 普通角色:speaker_name 保留 LLM 给出的表层称呼,不再用 role_mapping 改写。
            source_name = raw_speaker_name if raw_speaker_name else raw_speaker_id
            final_speaker_name = source_name or "Unknown"

            # 判定主名:精确命中角色库 name → confirmed;否则 pending 等待回填
            final_full_name, _name_status = resolve_full_name(final_speaker_name, canonical_names)

            # 额外:如果表层名是某个已知别名 (role_mapping 命中且映射到主名),
            # 也可当场确认 — 但只在该别名已通过 build_local_context 的过滤后仍存在时
            # (此时 role_mapping[别名] = 主名,是可信的)。
            if final_full_name is None and final_speaker_name in role_mapping:
                mapped = role_mapping.get(final_speaker_name)
                if mapped and mapped in canonical_names:
                    final_full_name = mapped

            # speaker_id 由 full_name 转化;pending (full_name=None) → speaker_id=None
            if final_full_name:
                final_speaker_id = to_pascal_case_speaker_id(final_full_name)
            else:
                final_speaker_id = None

            if beat_type != "nvl_thought":
                beat_type = "dialogue"

        raw_emotion = beat.get("emotion", "calm")
        if raw_emotion not in ALLOWED_EMOTIONS:
            raw_emotion = "calm"

        final_emotion = refine_emotion_by_keywords(clean_content, raw_emotion, beat_type)

        # A1: dialogue / nvl_thought 长度软约束告警
        # A1: 仅对 dialogue 进行长度软约束告警(nvl_thought 不限制)
        if beat_type == "dialogue":
            is_cn = is_chinese_content(clean_content)
            content_len = len(clean_content)
            soft_limit = CN_DIALOGUE_SOFT if is_cn else EN_DIALOGUE_SOFT
            hard_limit = CN_DIALOGUE_HARD if is_cn else EN_DIALOGUE_HARD

            if content_len > hard_limit:
                hard_violations += 1
                logger.error(
                    f"🔥 [dialogue] 超硬天花板 ({content_len} > {hard_limit}): "
                    f"{final_speaker_name} - {clean_content[:40]}..."
                )
            elif content_len > soft_limit:
                soft_violations += 1
                # 软违规只在前 3 条详细打,后续静默,避免日志爆炸
                if soft_violations <= 3:
                    logger.warning(
                        f"⚠️ [dialogue] 超软目标 ({content_len} > {soft_limit}): "
                        f"{final_speaker_name} - {clean_content[:30]}..."
                    )

        new_beat = {
            "beat_type": beat_type,
            "speaker_name": final_speaker_name,
            "full_name": final_full_name,
            "speaker_id": final_speaker_id,
            "content": clean_content,
            "emotion": final_emotion,
            "_src_lines": beat.get("_src_lines", [-1, -1]),
        }
        processed_list.append(new_beat)

    # A1: 汇总长度违规情况(只在有违规时打印)
    if soft_violations > 0 or hard_violations > 0:
        total_dialogue = sum(
            1 for b in processed_list
            if b.get("beat_type") == "dialogue"
        )
        if total_dialogue > 0:
            soft_rate = 100 * soft_violations / total_dialogue
            hard_rate = 100 * hard_violations / total_dialogue
            logger.info(
                f"📊 dialogue 长度统计: "
                f"软违规 {soft_violations} ({soft_rate:.1f}%), "
                f"硬违规 {hard_violations} ({hard_rate:.1f}%)"
            )

    if nvl_downgraded > 0:
        logger.info(
            f"📊 nvl_narration 长度门槛:{nvl_downgraded} 条因长度不足"
            f"(中<{CN_NVL_NARRATION_MIN}/英<{EN_NVL_NARRATION_MIN}字符)降级为 narration"
        )

    return processed_list


LONG_TEXT_BEAT_SPLIT_THRESHOLD = 80
LONG_TEXT_BEAT_TYPES = {"narration", "nvl_thought"}


def _find_long_text_beat_split_index(content):
    """返回最接近文本中点的自然切点。

    优先使用中文句号、英文句点和全角句点；当当前片段只剩一个超长句时，依次
    使用分号/逗号/空格等更弱的边界，最后才按字符中点兜底。这样递归拆分不会
    因为某个单句超过阈值而留下无法处理的超长 beat。句号及紧随的右引号归前半段。
    """
    text = str(content or "").strip()
    if not text:
        return None

    def valid_split_end(split_end):
        return text[:split_end].strip() and text[split_end:].strip()

    candidate_ends = []
    for index, char in enumerate(text):
        if char not in "。．.":
            continue
        if char == ".":
            previous_char = text[index - 1] if index > 0 else ""
            next_char = text[index + 1] if index + 1 < len(text) else ""
            # 小数点和英文省略号不是句界；数字结尾的正常句点仍可切分。
            if (
                (previous_char.isdigit() and next_char.isdigit())
                or previous_char == "."
                or next_char == "."
            ):
                continue

        split_end = index + 1
        while split_end < len(text) and text[split_end] in "”’\"'」』":
            split_end += 1

        # 必须保证句号两侧都有实际内容；末尾句号不能作为切点。
        if valid_split_end(split_end):
            candidate_ends.append(split_end)

    midpoint = len(text) / 2
    if candidate_ends:
        return min(candidate_ends, key=lambda pos: (abs(pos - midpoint), pos))

    # 句号不足以继续拆分时，优先选择较弱的语义边界。
    fallback_ends = []
    for index, char in enumerate(text):
        if char in "；;，,、：:":
            split_end = index + 1
            if valid_split_end(split_end):
                fallback_ends.append(split_end)
    if fallback_ends:
        return min(fallback_ends, key=lambda pos: (abs(pos - midpoint), pos))

    # 英文长句通常有空格；在空格后切分，避免切断单词。
    whitespace_ends = []
    for index, char in enumerate(text):
        if char.isspace() and valid_split_end(index + 1):
            whitespace_ends.append(index + 1)
    if whitespace_ends:
        return min(whitespace_ends, key=lambda pos: (abs(pos - midpoint), pos))

    # 最终兜底：按字符中点切。调用方只会对超过阈值的内容使用此函数，
    # 因而该切点始终能推动递归终止，不会产生空片段或死循环。
    return max(1, min(len(text) - 1, round(midpoint)))


def split_long_text_beats(beats, threshold=LONG_TEXT_BEAT_SPLIT_THRESHOLD):
    """递归拆分超过阈值的 narration / nvl_thought，直到片段不超长。

    此函数只扩展 beat 序列，不生成或修改 beat_id。调用方必须在最终 beat_id
    编号之前执行它，这样主线与分支编号都会沿用原有编号器并自然保持连贯。
    拆出的两段继承原 beat 的全部元数据，仅 content 不同。

    每次在当前片段中选择最接近中点的句号切分；拆出的片段会重新进入待处理
    队列，因此一个原始 beat 可以被拆成多个 beat。返回:
    (拆分后的 beats, 成功拆分数, 因无内部句号而跳过数)
    """
    expanded = []
    split_count = 0
    skipped_count = 0

    pending = deque(beats)
    while pending:
        beat = pending.popleft()
        content = str(beat.get("content", "")).strip()
        if beat.get("beat_type") not in LONG_TEXT_BEAT_TYPES or len(content) <= threshold:
            expanded.append(beat)
            continue

        split_index = _find_long_text_beat_split_index(content)
        if split_index is None:
            expanded.append(beat)
            skipped_count += 1
            continue

        first_beat = dict(beat)
        second_beat = dict(beat)
        first_beat["content"] = content[:split_index].strip()
        second_beat["content"] = content[split_index:].strip()
        # 先入队前半段，再入队后半段，保持原文顺序；两段都可能继续拆分。
        pending.appendleft(second_beat)
        pending.appendleft(first_beat)
        split_count += 1

    return expanded, split_count, skipped_count


async def process_node_content(client, node_id, raw_content, semaphore, pov_semaphore, char_data):
    """
    处理单个节点的内容 (融入块切分机制)

    P1 修复:
    1. POV 嗅探使用独立的 pov_semaphore,与正文提取的 semaphore 隔离
    2. POV 嗅探与文本切分阶段并行化,不再串行 await 阻塞整个节点
    3. tqdm pbar 使用 with 语句保证异常路径下也能释放
    4. 块处理失败时统计失败数,在节点末尾汇总告警
    """
    local_role_mapping, local_vip_prompt_str, conflict_warning, speaker_emotion_types = \
        build_local_context(node_id, char_data)

    raw_content = universal_merge_speech_tags(raw_content)

    blocks = parse_markdown_with_branches(raw_content)

    # ========== 阶段一: 收集所有需要嗅探的文本块,并行 POV 探测 ==========
    # 先做一次扫描,把所有 text 块的元信息收集起来
    text_block_infos = []  # [(block_index, clean_content, in_branch, src_start, src_end), ...]
    in_branch = False

    for i, block in enumerate(blocks):
        if block["type"] in ["branch_start", "option"]:
            in_branch = True
        elif block["type"] == "branch_end":
            in_branch = False

        if block["type"] == "text":
            clean_content = clean_markdown_content(block["content"])
            if clean_content.strip():
                text_block_infos.append((
                    i,
                    clean_content,
                    in_branch,
                    block.get("src_start_line", -1),
                    block.get("src_end_line", -1),
                ))

    # 为长文本块并发执行 POV 嗅探(在 pov_semaphore 控制下,不会和正文提取互相干扰)
    # B7: 准备 VIP 真名集合,用于 POV 嗅探的白名单兜底
    vip_real_names = set(local_role_mapping.values()) if local_role_mapping else set()

    pov_tasks = {}  # block_index -> Task
    for block_idx, clean_content, _, _, _ in text_block_infos:
        if len(clean_content) > 100:
            pov_tasks[block_idx] = asyncio.create_task(
                detect_pov_async(client, clean_content, local_vip_prompt_str, pov_semaphore, vip_real_names)
            )

    # 等待所有 POV 嗅探完成(此时正文 semaphore 完全空闲,提取阶段可以全速跑)
    pov_results = {}
    if pov_tasks:
        sniffed_list = await asyncio.gather(*pov_tasks.values(), return_exceptions=True)
        for block_idx, result in zip(pov_tasks.keys(), sniffed_list):
            if isinstance(result, Exception):
                logger.warning(f"⚠️ [节点 {node_id}] POV 嗅探异常 (block {block_idx}): {result}")
                pov_results[block_idx] = {"pov_type": "INHERIT", "pov_anchor_id": "INHERIT"}
            else:
                pov_results[block_idx] = result

    # ========== 阶段二: 按顺序应用 POV 状态机,然后并发跑正文提取 ==========
    text_tasks = []
    text_block_indices = []
    text_task_src_lines = []  # 每个 task 对应的 chunk src_lines,用于 beat 行号对齐
    global_context_buffer = ""

    # POV 状态需要按顺序更新(INHERIT 依赖前序状态)
    current_pov_data = {"pov_type": "3rd_omniscient", "pov_anchor_id": "PangBai"}

    for block_idx, clean_content, blk_in_branch, blk_src_start, blk_src_end in text_block_infos:
        # 按顺序应用 POV 嗅探结果到状态机
        if block_idx in pov_results:
            sniffed_pov = pov_results[block_idx]
            if sniffed_pov.get("pov_anchor_id") != "INHERIT":
                current_pov_data["pov_anchor_id"] = sniffed_pov.get("pov_anchor_id")
            if sniffed_pov.get("pov_type") != "INHERIT":
                current_pov_data["pov_type"] = sniffed_pov.get("pov_type")
            logger.info(f"🔍 [节点 {node_id}] block {block_idx} 应用视角状态: {current_pov_data}")

        # B7: 根据当前文本块的语言,选择中文/英文版的 POV 规则与 One-Shot
        block_is_cn = is_chinese_content(clean_content)
        dynamic_pov_rules = build_dynamic_pov_rules(current_pov_data, is_cn=block_is_cn)

        chunk_size = 2000 if is_chinese_content(clean_content) else 6000
        overlap_size = 300 if is_chinese_content(clean_content) else 900

        # 关键:把 block 的源行号偏移传进去,这样 chunk.src_lines 就是相对原文的绝对行号
        chunks_data, global_context_buffer = split_text_with_global_context(
            clean_content,
            global_context_buffer,
            is_main_line=(not blk_in_branch),
            chunk_size=chunk_size,
            overlap_size=overlap_size,
            base_line_offset=blk_src_start,
        )

        for chunk in chunks_data:
            text_tasks.append(
                extract_beats_async(client, chunk, semaphore, local_vip_prompt_str, conflict_warning,
                                    dynamic_pov_rules)
            )
            text_block_indices.append(block_idx)
            text_task_src_lines.append(chunk.get("src_lines", [blk_src_start, blk_src_end]))

    # ========== 阶段三: 并发执行所有正文提取,统计失败数 ==========
    failed_chunks = 0

    # P1 修复: with 语句保证 pbar 在异常路径也能释放
    with tqdm(total=len(text_tasks), desc=f"Processing {node_id}", leave=False) as pbar:

        async def wrap_with_progress(task):
            nonlocal failed_chunks
            try:
                return await task
            except Exception as e:
                failed_chunks += 1
                logger.error(f"❌ [节点 {node_id}] 块处理异常 (已耗尽重试,失败计数 #{failed_chunks}): {e}")
                return None
            finally:
                pbar.update(1)

        wrapped_tasks = [wrap_with_progress(t) for t in text_tasks]
        raw_outputs = await asyncio.gather(*wrapped_tasks)

    # 节点级失败汇总告警
    if failed_chunks > 0:
        total_chunks = len(text_tasks)
        fail_rate = failed_chunks / total_chunks if total_chunks else 0
        if fail_rate >= 0.3:
            logger.error(
                f"🔥 [节点 {node_id}] 块处理失败率过高: {failed_chunks}/{total_chunks} "
                f"({fail_rate:.1%}),该节点剧情可能严重不完整!"
            )
        else:
            logger.warning(
                f"⚠️ [节点 {node_id}] 共 {failed_chunks}/{total_chunks} 个块提取失败 "
                f"({fail_rate:.1%}),结果可能存在缺失"
            )

    # ========== 阶段四: 缝合输出 ==========
    # 每个 beat 注入它来自 chunk 的 src_lines,用于下游 scenes 阶段行号对齐。
    stream_data = []
    task_idx = 0
    for i, block in enumerate(blocks):
        if block["type"] == "text":
            while task_idx < len(text_block_indices) and text_block_indices[task_idx] == i:
                ai_output = raw_outputs[task_idx]
                chunk_src_lines = text_task_src_lines[task_idx]
                if ai_output:
                    try:
                        chunk_beats = robust_json_parse(
                            ai_output, context="chunk_beats"
                        )
                        if isinstance(chunk_beats, dict):
                            for v in chunk_beats.values():
                                if isinstance(v, list):
                                    chunk_beats = v
                                    break
                        if isinstance(chunk_beats, list):
                            for b in chunk_beats:
                                # 注入源行号区间,后续 post_process 会保留它
                                b["_src_lines"] = chunk_src_lines
                                stream_data.append(b)
                    except json.JSONDecodeError as e:
                        logger.error(f"❌ JSON 解析最终失败 (已重试5次)! Node: {node_id}, Error: {e}")
                        logger.error(f"📦 原始截断数据:\n{clean_json[:500]}")
                task_idx += 1
        else:
            # 结构标记块:用所在 block 的源行号区间;无对应行号时为 [-1,-1]。
            blk_src_start = block.get("src_start_line", -1)
            blk_src_end = block.get("src_end_line", -1)
            if block["type"] in ["side_story", "side_story_end"]:
                sys_beat = {
                    "beat_type": "narration",
                    "content": block["content"],
                    "speaker_name": "旁白",
                    "speaker_id": "PangBai",
                    "emotion": "calm",
                    "_src_lines": [blk_src_start, blk_src_end],
                }
            elif block["type"] == "system_panel":
                # 系统文增强:预解析的系统面板,直接塞入流,不经过 LLM
                # speaker 用 "旁白/PangBai" 走 narration 系亲缘;
                # beat_type 保留 "system_panel",由 post_process_beats_final 透传
                sys_beat = {
                    "beat_type": "system_panel",
                    "content": block["content"],
                    "speaker_name": "旁白",
                    "speaker_id": "PangBai",
                    "emotion": "calm",
                    "_src_lines": [blk_src_start, blk_src_end],
                }
            else:
                sys_beat = {
                    "beat_type": block["type"], "content": block["content"],
                    "speaker_name": "System", "speaker_id": "System",
                    "emotion": "calm",
                    "_src_lines": [blk_src_start, blk_src_end],
                }
            if block["type"] == "option":
                sys_beat["raw_option"] = block["raw"]
            stream_data.append(sys_beat)

    if stream_data:
        processed = post_process_beats_final(stream_data, local_role_mapping)

        # ===================== NPC ↔ VIP 共指消解 pass =====================
        # 在 post_process(speaker 已规范化)之后、情绪复审(依赖正确 speaker)之前。
        # 解决三类问题:
        #  1. 角色缺失别称、前期不知名 → 后期真名锚点反查
        #  2. 同一角色被不同 chunk 打成不同 NPC 字面 → 整章视野合并
        #  3. 上游别名错误 → 只信原文证据,不传上游别名给模型
        # 失败兜底:catch + log + 保留原 speaker(已在函数内实现)
        if processed:
            try:
                processed = await reconcile_npc_speakers_async(
                    client, processed, local_role_mapping, semaphore
                )
            except Exception as e:
                logger.warning(
                    f"⚠️ [节点 {node_id}] 共指消解 pass 失败,保留原 speaker: {e}"
                )

        # ===================== full_name 兜底定稿 =====================
        # 双名机制收尾,区分两类 pending:
        #  (1) 简洁真 NPC (青袍少年、老者、蒙面人):表层称呼即其身份 →
        #      full_name = speaker_name,生成 speaker_id。
        #  (2) 描述性占位名 (御剑飞天的女子、穿白衣的男子):这是 reconcile
        #      未能锁定真名的"待定身份占位符",描述短语**绝不可冒充主名** →
        #      full_name 保持 None,speaker_id 保持 None (诚实标记"身份未确认")。
        #  (3) 群体称呼 (同事们、众人、三名老者):指向多人,不是任何个体的身份 →
        #      同样保持 None。reconcile 已把群体名词判为"永远 keep",此处口径必须
        #      与之一致,否则前一阶段谨慎保留的 pending 会在这里被洗白成主名。
        #
        # 判据:_has_descriptive_pattern (含"的"字/动作修饰/长度≥6 的描述短语)
        #      + _is_collective_reference (们/众/三名…)。
        # 这样进不了别名表的描述短语与群体称呼,也当不了主名,逻辑统一。
        if processed:
            promoted = 0
            kept_null = 0
            for b in processed:
                if b.get("full_name") is not None:
                    continue
                if b.get("beat_type") in (
                        "branch_start", "branch_end", "option", "variable_update"
                ):
                    continue
                surface = b.get("speaker_name")
                if not surface:
                    continue
                if _has_descriptive_pattern(surface) or _is_collective_reference(surface):
                    # 描述性占位名 / 群体称呼:保持 null,不洗白成主名
                    b["speaker_id"] = None  # 双保险:确保 pending 的 id 为 null
                    kept_null += 1
                else:
                    # 简洁真 NPC:表层名即主名
                    b["full_name"] = surface
                    b["speaker_id"] = to_pascal_case_speaker_id(surface)
                    promoted += 1
            if promoted:
                logger.info(
                    f"   📌 [节点 {node_id}] full_name 兜底定稿:{promoted} 个简洁 NPC "
                    f"以表层称呼作主名"
                )
            if kept_null:
                logger.warning(
                    f"   ⚠️ [节点 {node_id}] {kept_null} 个描述性占位名/群体称呼未能确认主名 "
                    f"(full_name 保持 null) — reconcile 未锁定真名,留待上游/人工复核"
                )

        # ===================== 情绪复审 pass =====================
        # 把 processed 切成滑动窗口,逐窗调用 re_label_emotions_async 重新审定情绪。
        # 设计权衡:
        #  - 窗口大小 8 个 beat,带 2 个重叠,既能让 LLM 看到足够上下文,又控制 token 成本
        #  - 失败由调用层 catch,失败窗口保留原标注 (不阻断主流程)
        #  - 复用正文 semaphore (其在此阶段已完全空闲,无抢占风险)
        if processed:
            EMO_REVIEW_WINDOW = 8
            EMO_REVIEW_OVERLAP = 2
            step = max(1, EMO_REVIEW_WINDOW - EMO_REVIEW_OVERLAP)

            review_tasks = []
            for i in range(0, len(processed), step):
                window_slice = processed[i:i + EMO_REVIEW_WINDOW]
                if not window_slice:
                    continue
                # 检查窗口里是否有 dialogue,没有则跳过省一次 LLM 调用。
                # 注:旁白瘦身只处理"前一条是 dialogue"的 narration,该 dialogue 与
                # 待瘦身 narration 相邻,必同窗出现,故"窗口含 dialogue"即可覆盖所有可瘦身项;
                # 无 dialogue 的窗口里不存在可瘦身的 narration,跳过安全。
                if not any(b.get("beat_type") == "dialogue" for b in window_slice):
                    continue
                review_tasks.append(
                    re_label_emotions_async(client, window_slice, speaker_emotion_types, semaphore)
                )

            if review_tasks:
                logger.info(f"   🎭 [节点 {node_id}] 启动情绪复审:{len(review_tasks)} 个窗口")
                # 复审是原地修改 processed 中的 beats,所以这里只关注成功/失败计数
                results = await asyncio.gather(*review_tasks, return_exceptions=True)
                emo_failed = sum(1 for r in results if isinstance(r, Exception))
                if emo_failed > 0:
                    logger.warning(
                        f"⚠️ [节点 {node_id}] 情绪复审:{emo_failed}/{len(review_tasks)} 窗口失败,"
                        f"对应位置保留原标注"
                    )

        return node_id, processed, failed_chunks, len(text_tasks)
    return node_id, [], failed_chunks, len(text_tasks)


@retry(stop=stop_after_attempt(3), wait=wait_random_exponential(min=1, max=30))
async def reconcile_npc_speakers_async(client, processed_beats, role_mapping, semaphore):
    """章节级 NPC ↔ VIP 共指消解 pass。

    解决三类问题(用户案例驱动):
    1. **角色缺失别称,前期不知名**:章节早期角色用泛称称呼,中后期出现真名锚点,
       此 pass 从全局视野反查前期的 NPC 是否对应该 VIP。
    2. **同一角色在不同 chunk 被打成不同 NPC**:模型按 chunk 独立判断,跨 chunk 时
       字面选择有漂移(如"仙女"/"女子"),此 pass 在整章视野下合并。
    3. **上游别名错误**:不传上游别名表给 LLM,只传 VIP 真名清单,让模型从原文证据
       独立判断,避免被错误别名误导。

    设计原则:
    - **不传上游别名表**(只传 VIP 真名清单),防止再次被错误映射污染
    - **传"行为证据"**:每个可疑 speaker 附带前后 narration 上下文,这是同人识别的关键线索
    - **保守口径**:证据不足时 `keep`,不强行合并
    - **VIP 真名零容忍**:代码硬过滤,绝不允许 VIP 被改名成其他 VIP
    - **失败不阻断**:任何异常都保留原 speaker,只记日志

    时机要求:必须在 `post_process_beats_final` 之后、`re_label_emotions_async` 之前调用,
    因为情绪复审依赖正确的 speaker。

    参数:
        client: AsyncOpenAI
        processed_beats: list[dict],已经过 post_process_beats_final 的 beats
        role_mapping: dict[alias→real_name],用于提取 VIP 真名集合
        semaphore: 并发控制(复用主流程的 semaphore)

    返回:
        processed_beats(原地修改 speaker_name/speaker_id 后返回引用)
    """
    if not processed_beats:
        return processed_beats

    # ---------- Step 1. 提取 VIP 主名集合与保护名单 ----------
    # 防御:同 post_process,剔除 name 本身是描述短语的情况
    vip_names = set()
    if role_mapping:
        for _n in role_mapping.values():
            if _n and not _has_descriptive_pattern(_n):
                vip_names.add(_n)
    protected_names = {"旁白", "Narrator", "系统", "System", "Story"}

    # ---------- Step 2. 收集"待定 speaker" (双名机制) ----------
    # 待定 = full_name 为 None 的 beat (post_process 未能确认主名)。
    # 这正是需要回填的对象 — 比旧的"非保护 speaker"口径更精准:
    # 已 confirmed 的角色 (真名直接出场) 根本不会进来,信噪比高。
    # 按表层 speaker_name 分组 (同一表层称呼视作同一待定实体)。
    suspect_index = {}  # surface_name -> list[(beat_idx, beat_dict)]
    for idx, b in enumerate(processed_beats):
        if b.get("beat_type") not in ("dialogue", "nvl_thought"):
            continue
        if b.get("full_name") is not None:
            continue  # 已确认主名,跳过
        sname = b.get("speaker_name")
        if not sname or sname in protected_names:
            continue
        suspect_index.setdefault(sname, []).append((idx, b))

    if not suspect_index:
        return processed_beats

    # 触发条件:至少 1 个待定 + 有 VIP 锚点 (可回溯) 或 至少 2 个待定 (可互相合并)
    if len(suspect_index) < 2 and not vip_names:
        return processed_beats

    # ---------- Step 3. 为每个 suspect 构造证据档案 ----------
    def _collect_context(beat_idx, max_n=2, max_len=80):
        """收集 beat_idx 附近的 narration/nvl_narration 上下文(前 max_n + 后 max_n)。"""
        before, after = [], []
        j = beat_idx - 1
        while j >= 0 and len(before) < max_n:
            cb = processed_beats[j]
            if cb.get("beat_type") in ("narration", "nvl_narration"):
                txt = (cb.get("content") or "")[:max_len]
                if txt:
                    before.insert(0, txt)
            j -= 1
        j = beat_idx + 1
        while j < len(processed_beats) and len(after) < max_n:
            cb = processed_beats[j]
            if cb.get("beat_type") in ("narration", "nvl_narration"):
                txt = (cb.get("content") or "")[:max_len]
                if txt:
                    after.append(txt)
            j += 1
        return before, after

    suspect_evidence = []
    for surface_name, occurrences in suspect_index.items():
        n = len(occurrences)
        # 采样策略:≤6 全取,>6 取首/中/尾共 6 个,保证时间分布
        if n <= 6:
            sample_pick = list(range(n))
        else:
            sample_pick = sorted({0, 1, n // 2 - 1, n // 2, n - 2, n - 1})

        samples = []
        for si in sample_pick:
            beat_idx, b = occurrences[si]
            before, after = _collect_context(beat_idx)
            samples.append({
                "pos": beat_idx,  # 全局位置,用于 LLM 判断时序
                "type": b.get("beat_type"),
                "content": (b.get("content") or "")[:120],
                "ctx_before": " ⤴ ".join(before),
                "ctx_after": " ⤵ ".join(after),
            })

        # suspect 的唯一标识 = 表层称呼 (待定组内唯一)。LLM 决策针对此 key。
        suspect_evidence.append({
            "speaker_id": surface_name,  # 复用字段名,实际是表层称呼 key
            "speaker_name": surface_name,
            "occurrences": n,
            "samples": samples,
        })

    # ---------- Step 4. 收集 VIP 锚点(出现位置 + 次数 + 被提及位置) ----------
    vip_anchors = []
    for vname in vip_names:
        # "以主名说话"的位置:full_name 已确认为该 VIP 的 beat
        positions = [
            idx for idx, b in enumerate(processed_beats)
            if b.get("full_name") == vname
        ]
        # 额外:扫描该 VIP 主名在「其他角色台词 / 主角内心独白 / narration」中被提及的位置。
        # 这是跨场景共指的关键证据 —— 待定 NPC 与 VIP 直接对话位置可能相隔很远,
        # 但只要主名在某处文本中被点出(如主角心想"那个唐雪茹害我"),就能锚定身份。
        mention_positions = []
        if vname and len(vname) >= 2:  # 单字名易误匹配,跳过
            for idx, b in enumerate(processed_beats):
                if b.get("full_name") == vname:
                    continue  # 本人说话不算"被提及"
                content = b.get("content") or ""
                if vname in content:
                    mention_positions.append(idx)

        if positions or mention_positions:
            vid = next(
                (b.get("speaker_id") for b in processed_beats if b.get("full_name") == vname),
                to_pascal_case_speaker_id(vname)
            )
            anchor = {
                "speaker_id": vid,
                "name": vname,
                "first_pos": positions[0] if positions else None,
                "last_pos": positions[-1] if positions else None,
                "count": len(positions),
                "mention_pos": mention_positions[:8],  # 最多 8 个提及位置
            }
            vip_anchors.append(anchor)

    # 没有 VIP 锚点 + suspect 单一 = 无消解空间
    if not vip_anchors and len(suspect_evidence) < 2:
        return processed_beats

    # ---------- Step 5. 选语言、构造 prompt ----------
    def _mention_snippets(anchor, max_n=3, max_len=60):
        """提取该 VIP 被提及处的简短文本片段,作为跨场景锚定证据展示。"""
        snippets = []
        for pos in anchor.get("mention_pos", [])[:max_n]:
            if 0 <= pos < len(processed_beats):
                b = processed_beats[pos]
                txt = (b.get("content") or "")[:max_len]
                speaker = b.get("speaker_name") or "?"
                btype = b.get("beat_type") or "?"
                if txt:
                    snippets.append(f"pos={pos}({speaker}/{btype}):「{txt}」")
        return snippets

    sample_text = " ".join(
        s.get("content", "")
        for ev in suspect_evidence[:3]
        for s in ev["samples"][:2]
    )
    is_cn = is_chinese_content(sample_text)

    if is_cn:
        system_prompt = """你是小说角色共指消解专家。任务:**章节级**全局视野下,判断那些被临时标记为 NPC 的 speaker 中,哪些其实是同一个角色,并将它们合并。

# 背景(请认真阅读)

LLM 按 chunk 抽取台词时,**同一个角色经常被拆成多个不同的 NPC speaker**,主要原因:
- 章节早期角色还没说出真名,只能用泛称(如"那女子")
- 不同 chunk 里作者用了不同的称呼字眼("仙女" / "女子" / "白衣女子" / "蒙面人" 全是同一人)
- 第三人称视角下,旁观者对陌生角色的称呼会随情节进展变化

**这种漂移是常态,不是例外。你的核心任务就是把这些被错拆的 NPC 合并回去。**

保守不动看似"安全",但其实**保守 keep 会让同一角色被永久拆成多份**,污染整章音色、立绘、剧情连贯性 — 后果同样严重。

# 决策框架(对每个待消解 speaker 三选一)

1. **merge_to_vip**:该 NPC 实际就是 VIP 名单中某角色,因前期真名未出现而被错标。
2. **merge_to_npc**:多个不同字面的 NPC 是同一非 VIP 角色 — 合并到一个**简洁的、可以作为主名**的 speaker_id。
   **target_id 选择规则(必须遵守)**:
   - 优先选**简洁专名**(如"老者""少年""蒙面人"),而**不要**选含"的"字的描述性长名(如"御剑飞天的女子""穿白衣的男子")。
   - 描述短语不是合法主名,即便它在 suspect 列表里也不要选作目标。
   - 如果待合并的几个 suspect 全是描述短语(没有简洁专名可选),**优先 keep**,不要硬合并。
3. **keep**:确实是独立角色,或证据明显冲突,不合并。

# 决策依据(按可靠性排序)

**(a) 行为/能力一致性 [权重最高]**:周围 narration 中的动作/能力/武器/服饰是否一致。
   - 例:多个 NPC 都"御剑飞行"、"挥舞流光"、"手持飞剑" → 强证据,大概率同一人
   - 例:一个 NPC 描述"持剑"、另一个描述"持笛" → 不同人

**(b) 场景与时序连续性**:NPC 出现位置是否连续、其间是否有真名 VIP 穿插。
   - 时序连续 + 无其他角色穿插 → 强证据
   - 时序交错(NPC 出现在两 VIP 流畅对话之间)→ 谨慎

**(c) VIP 真名锚点**:VIP 首次出现位置**之前**的同特征 NPC,大概率就是该 VIP(因为该 VIP 之前没用真名)。
   - 例:NPC 出现在 pos=10-30,某 VIP 首次出现 pos=35 → 强嫌疑
   - **重要**:NPC 与 VIP 之间即便**隔着一大段别的角色的剧情**(如主角换了场景、插入了主角的内心戏),也**不影响**判断。关键看 NPC 的行为特征是否与 VIP 延续,而非两者位置是否紧挨。
   - **跨场景锚定**:如果 NPC 的真名在**其他角色的台词或主角的内心独白**中被点出(如主角心想"那个 XX 害得我好惨"),且该真名正是某 VIP,则这是**极强的合并证据** — 哪怕 NPC 与该 VIP 的直接对话位置相隔很远。

**(d) 称呼字面相近度** [弱信号]:仙女/女子/姑娘语义相近 → 单凭这个**不能**合并,必须配合 (a)(b)(c)

# 合并触发条件(积极判断)

满足以下任一条件应当合并(merge_to_vip 或 merge_to_npc):
- 多个 NPC 的 narration 描述至少有 2 处行为/能力特征匹配 + 时序无矛盾
- NPC 的独有能力(如"御剑飞行")与某 VIP 的能力完全对应,且无另一在场角色具备该能力
- NPC 出现在 VIP 首次出现位置之前,且其周围 narration 与 VIP 首次出场的 narration 描写衔接顺畅

# 必须 keep 的情况(消极判断)

只在以下明确情况下才 keep:
- **群体名词**:"三名老者"、"众弟子"、"众人"等永远 keep,不可合并到任何具体个体
- **序号型群演**:形如「身份名 + 单个大写字母」的 speaker(同事E、村民A、业主F)是**上游/提取阶段
  刻意用字母区分开的不同个体**,字母不同即人不同。**永远不可把它们互相合并**,也不可合并到同身份的
  其他编号。只有在出现真名锚点、可确认某个编号其实就是某 VIP 时,才允许该编号单独 merge_to_vip。
- **时序交错矛盾**:NPC 出现在两个 VIP 流畅对话之间,无法插入任何一方
- **能力/特征明显冲突**:NPC1 用剑,NPC2 用笛,行为模式完全无交集
- **场景隔离**:NPC1 在回忆/幻象/选项分支中,NPC2 在主线场景

# 输出格式

严格 JSON,无任何前后缀、无 markdown 围栏:
```
{
  "<待消解 speaker_id>": {
    "action": "merge_to_vip" | "merge_to_npc" | "keep",
    "target_id": "<目标 speaker_id,仅 action != keep 时需要>",
    "target_name": "<目标 speaker_name,仅 action != keep 时需要>",
    "reason": "<≤30 字简短理由,说明哪条依据>"
  },
  ...
}
```

# 正面参考案例(合并)

**案例 A** — NPC 早期出现 + 中间隔了大段主角剧情 + VIP 后期出现真名(**最常见,务必学会**):
- NPC `XianNv`(仙女)在 pos=7,17,21,前后 narration 描述"踩剑飞掠"、"踩着利剑朝徐缺杀来"
- NPC `NvZi`(女子)在 pos=33-85,narration 描述"手持飞剑指着他"、"挥舞起一道流光"、自报家门是宗门弟子
- **pos=86-106 是主角徐缺进入藏宝阁的独立剧情**(换了场景,插入内心戏和选项分支),这一段与女子无关
- 其间 pos=91 主角心想:"唐雪茹、老嫗、三长老……他们视我如蝼蚁" ← **主角内心点出了真名"唐雪茹",正对应前面追杀他的女子**
- VIP `TangXueRu`(唐雪茹)首次以真名说话在 pos=107
- 时序:XianNv (7-21) → NvZi (33-85) → [主角藏宝阁剧情 86-106] → TangXueRu (107+)
- 行为证据:仙女/女子/唐雪茹 三者都"御剑飞行"、都骂"淫贼"、都是天武宗弟子
- → **正确决策**:`XianNv` 和 `NvZi` 都 `merge_to_vip` 到 `TangXueRu`
- **关键认知**:中间隔着主角换场景的剧情,**完全不影响**判断。"仙女""女子"就是真名出现前的唐雪茹。**不要因为位置隔得远就退缩成只做 NPC 内部合并而放弃 merge_to_vip!**

**案例 B** — 多 NPC 合并为同一 NPC(无 VIP 锚点时):
- NPC `BaiYiNanZi`(白衣男子)在 pos=20-30,narration "佩剑、戴帷帽"
- NPC `MengMianRen`(蒙面人)在 pos=40-50,narration "佩剑、戴帷帽、武功路数与之前白衣男子一致"
- 全章无任何 VIP 锚点匹配
- → **正确决策**:`MengMianRen` `merge_to_npc` 到 `BaiYiNanZi`(合并到**最早出现**的 speaker_id)
- **注意**:merge_to_npc 的 target_id **必须**是已存在于待消解列表中的某个 speaker_id(用最早出现的那个),**不可**自己编造新名字

# 反面参考案例(keep)

**反例 C** — 群体名词:
- NPC `SanMingLaoZhe`(三名老者),即便上下文有 VIP "老嫗",**永远不可合并**,因为"三名"是群体
- → **正确决策**:`keep`

**反例 D** — 名字相似但场景隔离:
- NPC `XiaoXianNv`(小仙女) 在主线追杀场景
- VIP `XiaoRou`(小柔) 只在回忆/选项分支中被提到
- 名字都带"小",但场景完全不同
- → **正确决策**:`keep`(不能凭名字相似合并)
"""

        # 构造 user payload
        lines = ["# 待消解的 NPC speakers"]
        for ev in suspect_evidence:
            lines.append(
                f"\n## speaker_id: {ev['speaker_id']}  (名称: {ev['speaker_name']}, 出现 {ev['occurrences']} 次)")
            for s in ev["samples"]:
                lines.append(f"  [pos={s['pos']}] ({s['type']}) 「{s['content']}」")
                if s["ctx_before"]:
                    lines.append(f"     前文 narration: {s['ctx_before']}")
                if s["ctx_after"]:
                    lines.append(f"     后文 narration: {s['ctx_after']}")

        if vip_anchors:
            lines.append("\n# 可用的 VIP 真名锚点(只能合并到这里列出的 VIP)")
            for v in vip_anchors:
                if v["count"] > 0:
                    lines.append(
                        f"  - speaker_id={v['speaker_id']}, name={v['name']}, "
                        f"以真名说话 {v['count']} 次(首次 pos={v['first_pos']}, 末次 pos={v['last_pos']})"
                    )
                else:
                    lines.append(
                        f"  - speaker_id={v['speaker_id']}, name={v['name']}, "
                        f"(本章未以真名说话,仅在文本中被提及)"
                    )
                snips = _mention_snippets(v)
                if snips:
                    lines.append(f"     真名被提及处(跨场景锚定证据): {' | '.join(snips)}")
        else:
            lines.append("\n# 注意:本章节无 VIP 锚点,只能在 NPC 之间合并(merge_to_npc)或保持(keep)")

        user_prompt = "\n".join(lines)
    else:
        system_prompt = """You are an expert in coreference resolution for novel characters. Your task: at the **chapter level** (full global view), decide which of the temporarily-labeled NPC speakers are actually the same character, and merge them.

# Background (read carefully)

When LLMs extract dialogue chunk-by-chunk, **the same character is often split into multiple different NPC speakers** because:
- Early in a chapter the character has not yet said their real name (only a generic title like "the woman")
- Different chunks use different literal labels ("the fairy" / "the woman" / "the white-robed girl" = same person)
- Third-person narration's label for an unfamiliar character drifts as the plot progresses

**This drift is the norm, not the exception. Your core task is to merge these mis-split NPCs back together.**

Conservatively choosing keep seems "safe", but **conservative keep permanently splits one character into many** — polluting voice casting, art assets, and plot continuity equally severely.

# Decision framework (choose one per suspect)

1. **merge_to_vip**: This NPC is actually a VIP listed in the anchor list — mislabeled because the real name had not yet appeared.
2. **merge_to_npc**: Multiple differently-labeled NPCs are the same non-VIP — merge them all into a **concise, name-worthy** speaker_id.
   **target_id selection rule (mandatory)**:
   - Prefer **concise proper-noun-like names** (e.g., "OldMan", "Boy", "MaskedFigure"), NOT descriptive long phrases containing relational particles like "the X-ing Y" or "X of Y".
   - Descriptive phrases are not legitimate identity names; do not pick them even if they appear in the suspect list.
   - If all candidates to merge are descriptive phrases (no concise option), **prefer keep** rather than forcing a merge.
3. **keep**: Genuinely independent character, or evidence clearly conflicts.

# Decision criteria (in reliability order)

**(a) Behavioral/ability consistency [HIGHEST WEIGHT]**: Do surrounding narration actions/abilities/weapons/clothing align?
   - Example: Multiple NPCs all "fly on a sword", "wield streaming light", "hold a flying sword" → strong evidence, likely same person
   - Example: One NPC described "holding sword", another "holding flute" → different people

**(b) Scene & temporal continuity**: Are the NPC positions consecutive without a named VIP interleaved?
   - Consecutive timing + no other characters in between → strong evidence
   - Interleaved timing (NPC sitting between fluid two-VIP dialogue) → careful

**(c) VIP anchor reasoning**: Same-trait NPCs appearing **before** a VIP's first appearance position are likely that VIP (because the VIP wasn't named yet).
   - Example: NPC at pos=10-30, some VIP first appears pos=35 → strong suspicion

**(d) Label-string similarity [WEAK]**: "fairy"/"woman"/"girl" semantically close → **cannot** merge on this alone, must combine with (a)(b)(c)

# When to merge (positive triggers)

Merge if ANY of the following hold:
- Multiple NPCs have at least 2 matching behavior/ability features in narration + no temporal contradiction
- An NPC has a unique ability (e.g., "fly on a sword") exactly matching some VIP, and no other present character has that ability
- An NPC appears before a VIP's first position, and the surrounding narration flows seamlessly into the VIP's first scene

# When to keep (negative triggers)

Choose keep ONLY in these explicit cases:
- **Collective nouns**: "three elders", "the disciples", "the crowd" — ALWAYS keep, never merge into specific individuals
- **Serial extras**: speakers shaped "identity + a single capital letter" (ColleagueE, VillagerA, OwnerF) are
  **distinct individuals deliberately separated by letter** upstream/at extraction time — a different letter means
  a different person. **Never merge them into each other** or into another letter of the same identity. Only when a
  real-name anchor proves one specific letter is actually a VIP may that letter alone be merge_to_vip.
- **Temporal interleaving conflict**: NPC sits between fluid dialogue between two VIPs, cannot fit either
- **Clear ability conflict**: NPC1 uses sword, NPC2 uses flute, no behavioral overlap
- **Scene isolation**: NPC1 in flashback/illusion/option branch, NPC2 in main timeline

# Output format

Strict JSON, no preamble, no markdown fences:
```
{
  "<suspect_speaker_id>": {
    "action": "merge_to_vip" | "merge_to_npc" | "keep",
    "target_id": "<target speaker_id, only if action != keep>",
    "target_name": "<target speaker_name, only if action != keep>",
    "reason": "<≤30 chars, which criterion>"
  },
  ...
}
```

# Positive reference cases (merge)

**Case A** — NPC early + VIP real name later:
- NPC `Fairy` at pos=7,17,21; narration: "flew off on a sword", "rode her sword at Daniel"
- NPC `Woman` at pos=33-85; narration: "pointed flying sword at him", "wielded streaming light", self-claim as sect disciple
- VIP `Mary` first appears pos=109
- Timing: Fairy (7-21) → Woman (33-85) → Mary (109+), no interleaving
- Behavioral evidence: all "fly on sword", all curse "pervert"
- → **Correct decision**: both `Fairy` and `Woman` → `merge_to_vip` to `Mary`

**Case B** — Multi-NPC merge:
- NPC `WhiteRobedMan` at pos=20-30, narration: "wears sword, veiled hat"
- NPC `MaskedFigure` at pos=40-50, narration: "wears sword, veiled hat, same martial style"
- No VIP anchor
- → **Correct decision**: `MaskedFigure` → `merge_to_npc` to `WhiteRobedMan`

# Negative reference cases (keep)

**Case C** — Collective noun:
- NPC `ThreeElders`, even if context has VIP "OldDame", **NEVER merge**, because "three" implies a group
- → **Correct decision**: `keep`

**Case D** — Similar name but isolated scenes:
- NPC `LittleFairy` in main-line chase
- VIP `LittleRou` only mentioned in flashback/option branch
- Both start with "Little", but scenes are completely different
- → **Correct decision**: `keep`
"""
        lines = ["# Suspect NPC speakers to resolve"]
        for ev in suspect_evidence:
            lines.append(
                f"\n## speaker_id: {ev['speaker_id']}  (name: {ev['speaker_name']}, occurrences: {ev['occurrences']})")
            for s in ev["samples"]:
                lines.append(f"  [pos={s['pos']}] ({s['type']}) \"{s['content']}\"")
                if s["ctx_before"]:
                    lines.append(f"     narration before: {s['ctx_before']}")
                if s["ctx_after"]:
                    lines.append(f"     narration after: {s['ctx_after']}")

        if vip_anchors:
            lines.append("\n# Available VIP anchors (merge_to_vip targets must be from this list)")
            for v in vip_anchors:
                if v["count"] > 0:
                    lines.append(
                        f"  - speaker_id={v['speaker_id']}, name={v['name']}, "
                        f"speaks under real name {v['count']} times (first pos={v['first_pos']}, last pos={v['last_pos']})"
                    )
                else:
                    lines.append(
                        f"  - speaker_id={v['speaker_id']}, name={v['name']}, "
                        f"(never speaks under real name in this chapter, only mentioned)"
                    )
                snips = _mention_snippets(v)
                if snips:
                    lines.append(f"     real name mentioned at (cross-scene anchor evidence): {' | '.join(snips)}")
        else:
            lines.append("\n# Note: no VIP anchors in this chapter; only merge_to_npc or keep is allowed")

        user_prompt = "\n".join(lines)

    # ---------- Step 6. 调用 LLM ----------
    async with semaphore:
        try:
            response, used_model = await _model_fallback.achat(
                client,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.0,
                response_format={"type": "json_object"},
                max_tokens=2000,
            )
            if _usage_tracker and response.usage:
                _usage_tracker.add_llm_usage(
                    response.usage, used_model, provider="openai_compatible"
                )
            raw = response.choices[0].message.content or "{}"
        except Exception as e:
            logger.warning(f"⚠️ [reconcile] LLM 调用失败,保留原 speaker: {e}")
            return processed_beats

    # ---------- Step 7. 解析与安全校验 ----------
    try:
        decisions = parse_model_json(
            raw, context="reconcile", logger=logger, expect=dict
        )
        if not isinstance(decisions, dict):
            raise ValueError(f"返回顶层非 dict: {type(decisions).__name__}")
    except Exception as e:
        logger.warning(f"⚠️ [reconcile] JSON 解析失败,保留原 speaker: {e}")
        return processed_beats

    # 校验 + 构造 final mapping: {old_sid: (new_sid, new_name, action, reason)}
    valid_target_ids = {v["speaker_id"]: v["name"] for v in vip_anchors}
    suspect_ids = {ev["speaker_id"]: ev["speaker_name"] for ev in suspect_evidence}
    # NPC 合并目标允许是任何 suspect_id (不只是 VIP)
    npc_target_ids = dict(suspect_ids)

    final_mapping = {}
    merge_stats = {"merge_to_vip": 0, "merge_to_npc": 0, "keep": 0, "rejected": 0}

    for old_sid, decision in decisions.items():
        if old_sid not in suspect_ids:
            # LLM 返回了不在列表里的 speaker_id,忽略
            continue
        if not isinstance(decision, dict):
            merge_stats["rejected"] += 1
            continue

        action = decision.get("action", "keep")
        reason = (decision.get("reason") or "")[:50]

        if action == "keep":
            merge_stats["keep"] += 1
            continue

        target_id = decision.get("target_id")
        target_name = decision.get("target_name")

        if not target_id or not target_name:
            merge_stats["rejected"] += 1
            continue

        # 安全校验
        if action == "merge_to_vip":
            if target_id not in valid_target_ids:
                logger.warning(
                    f"⚠️ [reconcile] 拒绝 {old_sid} → {target_id}(merge_to_vip 目标不在 VIP 锚点列表)"
                )
                merge_stats["rejected"] += 1
                continue
            # 真名一致性校验
            if valid_target_ids.get(target_id) != target_name:
                logger.warning(
                    f"⚠️ [reconcile] 拒绝 {old_sid} → {target_id}(target_name 不一致: "
                    f"模型给 '{target_name}' 但 VIP 库里是 '{valid_target_ids[target_id]}')"
                )
                # 仍然合并,但用 VIP 库里的真名作为权威
                target_name = valid_target_ids[target_id]

        elif action == "merge_to_npc":
            if target_id not in npc_target_ids:
                logger.warning(
                    f"⚠️ [reconcile] 拒绝 {old_sid} → {target_id}(merge_to_npc 目标不在 suspect 列表)"
                )
                merge_stats["rejected"] += 1
                continue
            if target_id == old_sid:
                # 自环,等同于 keep
                merge_stats["keep"] += 1
                continue

        else:
            # 未知 action
            merge_stats["rejected"] += 1
            continue

        final_mapping[old_sid] = {
            "new_sid": target_id,
            "new_name": target_name,
            "action": action,
            "reason": reason,
        }
        merge_stats[action] += 1

    # ---------- Step 8. 链式映射展平 ----------
    # 防止 A→B, B→C 的链式映射只走一步导致结果不一致;先做并查集式展平。
    def _resolve_chain(sid, visited=None):
        if visited is None:
            visited = set()
        if sid in visited:
            return None  # 循环,拒绝
        if sid not in final_mapping:
            return sid
        visited.add(sid)
        return _resolve_chain(final_mapping[sid]["new_sid"], visited)

    flat_mapping = {}
    for old_sid, info in final_mapping.items():
        final_target = _resolve_chain(info["new_sid"])
        if final_target is None or final_target == old_sid:
            continue
        # 决定最终主名 new_name:
        #  - final_target 是 VIP 锚点 id → 用 VIP 主名
        #  - final_target 是另一个待定 NPC (surface_name) → 用该 surface_name 作主名
        if final_target in valid_target_ids:
            flat_name = valid_target_ids[final_target]
        elif final_target in npc_target_ids:
            # merge_to_npc 的目标是某个 suspect 的 surface_name,它本身即主名
            flat_name = npc_target_ids[final_target]
        elif final_target in final_mapping:
            flat_name = final_mapping[final_target]["new_name"]
        else:
            flat_name = info["new_name"]
        flat_mapping[old_sid] = {"new_sid": final_target, "new_name": flat_name}

    # ---------- Step 9. 应用映射:填 full_name + 重算 speaker_id ----------
    # 双名机制核心:不改写 speaker_name (忠于原文表层),只回填 full_name,
    # 并由 full_name 重算 speaker_id。这样表层称呼各异的碎片,因 full_name 一致
    # 而获得相同 speaker_id,下游按 id 分组即天然合并。
    if not flat_mapping:
        return processed_beats

    applied = 0
    rejected_descriptive = 0
    for b in processed_beats:
        if b.get("full_name") is not None:
            continue  # 已确认,不动 (含所有 confirmed VIP / 旁白 / 系统)
        surface = b.get("speaker_name")
        if surface in protected_names:
            continue  # 双保险:保护名字永不回填
        if surface in flat_mapping:
            new_full = flat_mapping[surface]["new_name"]
            # 内层防御:即便 LLM 选择把碎片合并到一个描述短语 (merge_to_npc 选了带"的"字
            # 的长描述作目标),也不让描述短语进入 full_name。此时降级为 keep:
            # full_name 保持 None,留给出口闸门或下游处理。
            sanitized_full, sanitized_sid = sanitize_full_name(new_full, vip_names)
            if sanitized_full is None:
                rejected_descriptive += 1
                continue
            b["full_name"] = sanitized_full
            b["speaker_id"] = sanitized_sid
            applied += 1

    # ---------- Step 10. 日志 ----------
    if applied or final_mapping:
        details = []
        for old_key, info in flat_mapping.items():
            details.append(f"{old_key}→{info['new_name']}({to_pascal_case_speaker_id(info['new_name'])})")
        logger.info(
            f"   🔗 [reconcile] 共指消解:merge_to_vip={merge_stats['merge_to_vip']}, "
            f"merge_to_npc={merge_stats['merge_to_npc']}, keep={merge_stats['keep']}, "
            f"rejected={merge_stats['rejected']} | 回填 {applied} 个 beat 的 full_name"
        )
        if rejected_descriptive:
            logger.warning(
                f"   ⚠️ [reconcile] 拒绝 {rejected_descriptive} 个描述短语回填 "
                f"(LLM 选择把碎片合并到带'的'字的长描述,违反 full_name 合法性) — "
                f"这些 beat 的 full_name 保持 None,留给下游识别"
            )
        if details:
            logger.info(f"   🔗 [reconcile] 主名回填明细: {'; '.join(details[:10])}")

    return processed_beats


@retry(stop=stop_after_attempt(3), wait=wait_random_exponential(min=1, max=30))
async def re_label_emotions_async(client, beats_window, speaker_emotion_types, semaphore):
    """情绪复审 pass:对一段连续 beats 中的 dialogue 做情绪重审。

    设计要点:
    1. 仅修改 dialogue 的 emotion;narration / nvl_narration / nvl_thought / 系统 beat 一律保留
       (nvl_thought 的 emotion 在产品里基本不被消费,无需复审;但它仍作为上下文线索传给 LLM)
    2. 输入只传 minimal payload(speaker / speaker_type / beat_type / content / current_emotion),
       不传原文,降低 token 成本
    3. 利用情绪信号分类体系 + 角色情绪类型策略 + 决策树这套通用方法论
    4. 失败不阻断主流程(由调用方 catch),保留原标注作为兜底

    参数:
        client: AsyncOpenAI client
        beats_window: list[dict],一段连续的、已经过 post_process_beats_final 的 beats
        speaker_emotion_types: dict, {speaker_name: emotion_type}, 来自 build_local_context
        semaphore: 并发控制信号量

    返回:
        list[dict]: 原 beats_window 列表(原地修改 emotion 字段后返回引用)
    """
    if not beats_window:
        return beats_window

    # 需要 LLM 处理的 beats:dialogue(情绪复审)+ narration/nvl_narration(旁白瘦身)
    reviewable_indices = [
        i for i, b in enumerate(beats_window)
        if b.get("beat_type") in ("dialogue", "narration", "nvl_narration")
    ]
    if not reviewable_indices:
        return beats_window

    # 构造 payload。
    # - 对 narration/nvl_narration,额外标注"前一条是否为 dialogue + 其内容",
    #   作为旁白瘦身的判据:只有当旁白在转述相邻台词的发声方式时才删,
    #   无相邻 dialogue 的声音描写(环境音/法术音)一律保留。
    payload = []
    for i, b in enumerate(beats_window):
        bt = b.get("beat_type")
        # 情绪类型按主名 (full_name) 查;主名缺失时回退表层称呼
        speaker = b.get("full_name") or b.get("speaker_name") or "Unknown"
        speaker_type = speaker_emotion_types.get(speaker, "direct_expressive")
        entry = {
            "idx": i,
            "beat_type": bt,
            "speaker": speaker,
            "speaker_type": speaker_type,
            "content": b.get("content", ""),
            "current_emotion": b.get("emotion", "calm"),
        }
        # 为 narration/nvl_narration 附加相邻 dialogue 线索(瘦身判据)
        # 发声转述有两个方向:
        #   - 收尾型:旁白在前一条台词之后转述其发声(如"一道娇斥声传来")→ 看 prev
        #   - 领起型:旁白在句尾预告下一条台词的发声(如"...转身大喊。"接着是台词)→ 看 next
        if bt in ("narration", "nvl_narration"):
            def _nearest_dialogue(start, direction):
                j = start
                while 0 <= j < len(beats_window):
                    jt = beats_window[j].get("beat_type")
                    if jt == "dialogue":
                        return (beats_window[j].get("content") or "")[:60]
                    if jt in ("narration", "nvl_narration", "nvl_thought"):
                        return None  # 撞到实质性叙述/对白以外内容,停止
                    j += direction  # 跳过系统/变量类
                return None

            prev_dia = _nearest_dialogue(i - 1, -1)
            next_dia = _nearest_dialogue(i + 1, +1)
            entry["prev_is_dialogue"] = prev_dia is not None
            entry["next_is_dialogue"] = next_dia is not None
            if prev_dia is not None:
                entry["prev_dialogue_content"] = prev_dia
            if next_dia is not None:
                entry["next_dialogue_content"] = next_dia
        payload.append(entry)

    # 判断语言,选择对应的 prompt 版本
    sample_text = " ".join(p.get("content", "") for p in payload[:3])
    is_cn = is_chinese_content(sample_text)

    if is_cn:
        system_prompt = f"""你是视觉小说脚本的后期处理专家,负责两项任务:
(A) 情绪复审:重新审定每个 dialogue 的情绪标注;
(B) 旁白瘦身:删除 narration / nvl_narration 中"转述相邻台词如何被说出"的发声描写。

合法 emotion 值: calm, happy, sad, angry, fearful, surprised, disgusted

{EMOTION_SIGNAL_TAXONOMY_CN}

{EMOTION_TYPE_PLAYBOOK_CN}

{EMOTION_DECISION_TREE_CN}

[任务 A — 情绪复审]
1. 仅修改 dialogue 的 emotion
2. payload 中的 narration 与 nvl_thought 作为"信号线索池",提供体征/姿态/声学/言语等上下文
3. 严格遵守每个角色的 speaker_type 判读策略
4. current_emotion 仅作参考,发现误判应大胆修正;但信号确实指向相同情绪则一致输出

[任务 B — 旁白瘦身(仅 narration / nvl_narration)] ⚠️ 这是和任务 A 同等重要的任务,不要因为忙于改情绪而忽略它!
背景:下游会对 dialogue 台词配音,角色台词的"声音"已由配音演员表现。因此旁白中
"转述某句台词如何被发声"的描写就与配音重复,需删除。

发声转述有两个方向,**都要删**:
- **收尾型**:旁白在台词之后转述其发声方式(该 narration 的 `prev_is_dialogue` 为 true)。
  例:台词"大胆淫贼!" 后接 narration "一道娇斥声传来,仙女去而复返..." → 删"一道娇斥声传来"
- **领起型**:旁白在句尾预告紧接着的台词(该 narration 的 `next_is_dialogue` 为 true)。
  例:narration "...摸出一块令牌转身大喊。" 后接台词 → 删句尾"大喊"(改为"...摸出一块令牌转身。")

删除规则:
1. **只要 `prev_is_dialogue` 或 `next_is_dialogue` 任一为 true,就检查该 narration 是否含发声转述并删除。**
   纯发声量级词:大喊/大叫/娇斥/暴喝/嘶吼/喊道/叫道/吼道/大喝/怒吼/尖叫/喊/叫/暗骂一声/娇喝 等。
2. **发声与动作混在一句,只摘发声部分,保留动作/表情/画面/环境/心理。** 例:
   - "徐缺大喊一声,拔腿就跑。" → "徐缺拔腿就跑。"
   - "徐缺暗骂一声,转身拔腿就跑。" → "徐缺转身拔腿就跑。"
   - "一道娇斥声传来,仙女去而复返,踩着利剑朝徐缺杀来。" → "仙女去而复返,踩着利剑朝徐缺杀来。"
   - "...摸出一块令牌转身大喊。" → "...摸出一块令牌转身。"
3. **带表情或情绪的发声(冷笑、苦笑、叹气、嗤笑、狞笑等)一律保留** —— 它们是表情动作,不是纯发声量级。
   例:"徐缺冷笑一声。" → 保留不动。
4. **没有相邻台词对应的纯声音/音效(环境音、法术破空声、无台词的物理声响)完全保留。**
   判据:`prev_is_dialogue` 与 `next_is_dialogue` 均为 false,或声音明显是物理音效而非转述台词
   (如"远处传来破空之声""只听一声闷响""三缕流光破空之声")。
5. **删除后若整句变空、只剩孤立主语、或读不通顺,则保留原句不动。**
6. dialogue / nvl_thought 的 content 一律不改。
7. 拿不准动作信息会不会被误删 → 保留(保守优先)。但发声词本身该删就删,不要因噎废食。

[输出格式]
严格 JSON 数组。两类结果合并输出:
- 情绪复审项: {{"idx": N, "type": "emotion", "emotion": "..."}}
- 旁白瘦身项: {{"idx": N, "type": "trim", "content": "瘦身后的完整文本"}}
仅输出**确实需要修改**的项(emotion 变了 或 content 被瘦身了)。无需修改的 beat 不要输出。
不要任何解释、markdown 代码块、前后说明文字。第一个字符是 [,最后一个字符是 ]。
"""
        user_prompt = f"""请对下列 beats 执行两项任务:
(A) 复审所有 dialogue 的 emotion;
(B) 瘦身 narration / nvl_narration 中转述相邻台词发声方式的描写。
nvl_thought 仅作上下文线索,不输出结果。

```json
{json.dumps(payload, ensure_ascii=False, indent=2)}
```

只输出需要修改的项,JSON 数组形式。"""
    else:
        system_prompt = f"""You are a post-processing expert for visual novel scripts, handling two tasks:
(A) Emotion review: re-judge the emotion of each dialogue beat;
(B) Narration trimming: remove descriptions in narration / nvl_narration that merely re-narrate HOW an adjacent line was vocalized.

Allowed emotion values: calm, happy, sad, angry, fearful, surprised, disgusted

{EMOTION_SIGNAL_TAXONOMY_EN}

{EMOTION_TYPE_PLAYBOOK_EN}

{EMOTION_DECISION_TREE_EN}

[Task A — Emotion Review]
1. Modify ONLY the emotion field of dialogue beats
2. narration and nvl_thought in payload serve as a "signal pool" (physiological/posture/acoustic/verbal context)
3. Strictly follow each speaker's speaker_type playbook
4. current_emotion is reference only; correct misjudgments boldly; if signals confirm it, keep it

[Task B — Narration Trimming (narration / nvl_narration only)] ⚠️ As important as Task A — do NOT neglect it while busy fixing emotions!
Background: downstream voices the dialogue lines, so the "sound" of a line is already performed by
the voice actor. Narration that re-narrates HOW a line was vocalized is therefore redundant.

Vocalization re-narration comes in two directions, BOTH should be trimmed:
- **Trailing type**: narration after a line re-narrates its vocalization (`prev_is_dialogue` true).
  e.g. line "How dare you!" followed by "A sharp cry came; the fairy returned..." → drop "A sharp cry came".
- **Leading type**: narration ends by announcing the line that immediately follows (`next_is_dialogue` true).
  e.g. "...drew a token and turned to shout." followed by a line → drop the trailing "shout".

Rules:
1. **If either `prev_is_dialogue` or `next_is_dialogue` is true, check the narration for vocalization re-narration and remove it.**
   Pure volume words: shouted/yelled/cried out/bellowed/roared/screamed/exclaimed, etc.
2. **When vocalization and action are mixed, drop only vocalization, keep action/expression/scene/psychology.** e.g.
   - "Daniel shouted and bolted." → "Daniel bolted."
   - "Daniel cursed under his breath and spun to flee." → "Daniel spun to flee."
   - "...drew a token and turned to shout." → "...drew a token and turned."
3. **Always keep expression-bearing vocalizations (cold laugh, wry smile, sigh, sneer).** e.g. "Daniel gave a cold laugh." → unchanged.
4. **Fully keep pure sound/SFX with no adjacent line** (environment sound, spell whoosh, untagged physical sounds).
   Criterion: both `prev_is_dialogue` and `next_is_dialogue` false, or clearly physical SFX not re-narrating a line.
5. **If trimming leaves an empty / dangling / ungrammatical sentence, keep original unchanged.**
6. Never modify content of dialogue / nvl_thought.
7. When unsure whether action info would be lost → keep (conservative). But do remove genuine vocalization words.

[Output Format]
Strict JSON array, two kinds merged:
- emotion review: {{"idx": N, "type": "emotion", "emotion": "..."}}
- narration trim: {{"idx": N, "type": "trim", "content": "trimmed full text"}}
Output ONLY items that actually change. Do not output unchanged beats.
NO explanation, NO markdown fences, NO preamble. First char `[`, last char `]`.
"""
        user_prompt = f"""Perform two tasks on the beats below:
(A) review emotion of all dialogue;
(B) trim narration / nvl_narration that re-narrates how an adjacent line was vocalized.
nvl_thought is context-only — do not output results for it.

```json
{json.dumps(payload, ensure_ascii=False, indent=2)}
```

Output only items that need changes, as a JSON array."""

    try:
        async with semaphore:
            response, used_model = await _model_fallback.achat(
                client,
                messages=[
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': user_prompt}
                ],
                temperature=0.1
            )
        if _usage_tracker and response.usage:
            _usage_tracker.add_llm_usage(
                response.usage, used_model, provider="openai_compatible"
            )

        raw = response.choices[0].message.content
        parsed = robust_json_parse(raw, context="emotion")
        if not isinstance(parsed, list):
            raise ValueError(f"Expected JSON list, got {type(parsed).__name__}")
    except Exception as e:
        logger.warning(f"⚠️ 情绪复审 pass 失败,保留原标注: {e}")
        raise  # 让 tenacity 捕获并重试

    # 写回:两类结果分别处理
    #  - type=emotion: 仅改 dialogue 的 emotion
    #  - type=trim:    仅改 narration/nvl_narration 的 content,带长度保护与回退
    emotion_updates = 0
    trim_updates = 0
    for item in parsed:
        if not isinstance(item, dict):
            continue
        idx = item.get("idx")
        if not isinstance(idx, int) or idx < 0 or idx >= len(beats_window):
            continue
        beat = beats_window[idx]
        bt = beat.get("beat_type")
        # 兼容:旧格式无 type 字段时,按 emotion 处理
        item_type = item.get("type", "emotion")

        if item_type == "emotion":
            new_emo = item.get("emotion")
            if not isinstance(new_emo, str) or new_emo not in ALLOWED_EMOTIONS:
                continue
            # 严格守门:仅允许修改 dialogue
            if bt != "dialogue":
                continue
            if beat.get("emotion") != new_emo:
                beat["emotion"] = new_emo
                emotion_updates += 1

        elif item_type == "trim":
            new_content = item.get("content")
            if not isinstance(new_content, str):
                continue
            # 严格守门:仅允许瘦身 narration / nvl_narration
            if bt not in ("narration", "nvl_narration"):
                continue
            old_content = beat.get("content") or ""
            new_content = new_content.strip()
            # 安全机制:
            #  1. 瘦身只能变短或相等,绝不允许变长(防 LLM 擅自扩写/改写语义)
            #  2. 变空 → 回退
            #  3. 长度骤减 (< 原长 30%) → 回退 (大概率误删了动作信息)
            #  4. 新内容必须是"删减"而非"改写":这里用宽松校验 — 仅长度门槛把关,
            #     语义由 prompt 约束;长度门槛兜住最严重的误删
            if not new_content:
                continue
            if len(new_content) >= len(old_content):
                continue  # 没变短(或反而变长),视为无效瘦身
            if len(new_content) < len(old_content) * 0.3:
                logger.warning(
                    f"   ✂️ 旁白瘦身回退(idx={idx}):瘦身后过短 "
                    f"({len(old_content)}→{len(new_content)}),疑似误删动作信息"
                )
                continue
            beat["content"] = new_content
            trim_updates += 1

    if emotion_updates or trim_updates:
        logger.info(
            f"   🎭 复审/瘦身:窗口 {len(beats_window)} beats,"
            f"改情绪 {emotion_updates} 条,瘦身旁白 {trim_updates} 条"
        )
    return beats_window


def clean_orphan_beats(beats, threshold=2):
    """[已废弃] 原用于消除微观移动幻觉(基于 _loc/_time 合并场景孤岛)。

    plots 阶段已彻底纯化:不再判 scene_id,改用 _src_lines 行号占位,
    场景切分/归并下放到下游 scenes 阶段处理。故此函数现为 no-op 直通,
    保留签名仅为兼容调用点。
    """
    return beats


# ================= 主入口 =================


def _normalize_cg_visual_entities(value):
    """Normalize the people that must remain visually identifiable in one CG.

    The annotation LLM is only allowed to declare people/humanoids here.  Other
    transient visual elements remain part of the later CG prompt and do not
    create an identity-reference obligation in this minimal CG consistency
    contract.
    """
    if not isinstance(value, list):
        return []

    normalized = []
    seen = set()
    for raw in value:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or raw.get("entity_name") or "").strip()
        if not name:
            continue
        entity_type = str(raw.get("entity_type") or "human").strip().lower()
        if entity_type not in {"human", "humanoid"}:
            continue
        policy = str(raw.get("reference_policy") or "required_identity").strip().lower()
        if policy not in {"required_identity", "recommended_identity", "prompt_only"}:
            policy = "required_identity"
        aliases = raw.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        aliases = [
            str(alias).strip()
            for alias in aliases
            if str(alias).strip() and str(alias).strip() != name
        ]
        key = (name, entity_type)
        if key in seen:
            continue
        seen.add(key)
        normalized.append(
            {
                "name": name,
                "aliases": aliases,
                "entity_type": entity_type,
                "reference_policy": policy,
            }
        )
    return normalized


async def annotate_cg_for_node_async(client, node_id, ordered_beats, semaphore, cg_start_index):
    """CG(插画/立绘高光图)标注 pass —— 节点级,全局连续编号。

    定位:剧情重大转折、高光时刻、结局等"值得定格成一张插画"的片段会配 CG 图。
    CG 很稀疏(一个章节 0~1 张,有时几章才一张),且一张 CG 通常横跨连续多个 beat。

    设计:
    - 在主组装之后调用(beat_id 已最终确定),用 beat_id 区间表达"哪几个 beat 是一组 CG"。
    - 整节点 beat 一次性喂给 LLM(只喂精简视图:beat_id/类型/说话人/content),
      让模型在全章脉络下判断高光点,避免分块导致的视野缺失。
    - 仅正文 beat(dialogue/narration/nvl_narration/nvl_thought)可成为 CG;
      system_panel/variable_update/软分支选项等结构性 beat 不参与。
    - 全局连续编号:cg_start_index 是本节点起始序号(上个节点用到的最大号 + 1),
      返回本节点用掉后的下一个起始序号,供下个节点接力,保证 cg_1/cg_2... 跨节点连续。
    - 失败不阻断:任何异常都不标 CG,只记日志。

    参数:
        cg_start_index: 本节点第一张 CG 应使用的序号(从 1 开始的全局计数)
    返回:
        next_cg_index: 本节点处理完后,下一个可用的全局序号
    """
    if not ordered_beats:
        return cg_start_index

    # 仅正文 beat 可成为 CG;建立 beat_id -> beat 的索引(只含可参与的)
    CG_ELIGIBLE_TYPES = {"dialogue", "narration", "nvl_narration", "nvl_thought"}
    eligible_ids = set()
    payload_lines = []
    for b in ordered_beats:
        bid = b.get("beat_id")
        btype = b.get("beat_type")
        if btype not in CG_ELIGIBLE_TYPES:
            continue
        eligible_ids.add(str(bid))
        speaker = b.get("speaker_name") or "旁白"
        content = (b.get("content") or "").replace("\n", " ")[:160]
        payload_lines.append(f'[{bid}] ({btype}|{speaker}) {content}')

    if not payload_lines:
        return cg_start_index

    beats_text = "\n".join(payload_lines)

    system_prompt = """你是视觉小说(AVG)的 CG 插画策划,服务于各种题材的小说(言情、武侠、悬疑、科幻、历史、奇幻等)。你的任务是从本节点中**找出最值得定格成精美插画的高潮画面**,为它配 CG 图。

【你的核心任务:主动找出高潮】
每个节点通常都有它的情绪/剧情顶点。你要做的是**识别出这个顶点在哪里**,而不是默认"没有 CG"。
满足以下任一,就是 CG 候选(不限题材):
- **剧情重大转折**:命运的关键拐点(真相揭露、重大抉择落定、立场逆转、生死关头)
- **情感/戏剧高光**:全章的情绪顶点(生离死别、久别重逢、情感表露、关键突破、对决定格、震撼揭晓)
- **结局/段落定格**:一个段落/支线收束时值得回味的画面(尤其结尾处的定格画面,往往该配 CG)

【一张 CG 对应一个事件(关键,务必理解)】
一张 CG 对应**一个事件/一个画面**。判断一段高光内容该怎么框:
- 如果这段内容**自始至终讲的是同一件事**(同一个场景、同一个持续的画面,如一整段拥抱、一场连贯的对视告白、一幅逐渐展开的收束背影)→ **整段保留为一张 CG**,区间覆盖它,**该多宽多宽,不必切短**。
- 如果这段内容里**混入了不同的事件**(画面/场景发生了切换,如"先激烈对抗、后情感爆发",对抗和情感是两件事)→ **不要拆成多张 CG**,而是**只保留其中一个核心事件**:
  - **核心事件 = 情绪/戏剧顶点**,通常是这段的高潮或收束(例:一段"对抗→受创→逆转→情感爆发"里,核心是结尾的情感爆发;对抗过程是铺垫,舍弃)。
  - 区间只覆盖这一个核心事件的**完整画面**(它前后连贯的那几个 beat),其余事件不纳入。
- 一句话:**同一件事就整段留(一张),不同事件就只留核心那一个(仍是一张)。任何一段高光最终只产出一张 CG。**

【数量与位置原则 — 少而精】
- 以**真正的高潮**为主。一个平淡的过渡节点可以 **0 张**;有明确顶点的节点通常 **1 张**。一个节点里若确实有**彼此独立、互不相关**的多个高光事件(不是同一场戏的不同阶段),才会各出一张。**不要为了凑数硬找,也不要把同一场戏切成多张。**
- **区间完整性**:一张 CG 至少要覆盖能表现这个画面的完整动作/对白,**不要切到只剩孤零零一句台词或一个动作**(避免 1 个 beat 的干瘪 CG)。
- **画面定格门槛**:必须是**画面感强、值得定格**的瞬间。纯靠对话推进、画面平淡的片段不要标,哪怕台词有意思。
- **位置压在真正的顶点上**。宁可不标,也不要标在"次高潮"或"日常片段"上。
- **克制优先**:日常互动、玩笑调侃(见下方反例)**始终不标**——不要因为在"找画面"就把日常也标进来。反例门槛高于一切。

{POSITIVE_NEGATIVE_EXAMPLES}

【输出格式】
  严格输出 JSON,不要任何解释或 markdown 代码块:
  {"cgs": [{"start_beat_id": "起始beat_id", "end_beat_id": "结束beat_id", "visual_entities": [{"name": "原文中的人物主名或稳定称谓", "aliases": ["原文别称"], "entity_type": "human|humanoid", "reference_policy": "required_identity|recommended_identity|prompt_only"}]}]}
  如果整章没有任何值得配 CG 的高光,输出:{"cgs": []}
  注意:start_beat_id 和 end_beat_id 都必须是下面列表中真实存在的正文 beat_id;每个区间应只覆盖**一个**画面。

  【visual_entities 规则】
  - 只列出在这张定格画面里肉眼实际可见的人物或类人角色，不列仅被提及、回忆、通话、内心想到或画外发声的人。
  - 没有台词但在叙述动作中实际出现的人，也必须列出；不能只看说话人。
  - 会在当前或后续 CG 中复用身份、必须保持脸部/体型/发型/基础服装一致的人，标 required_identity。
  - 次要但希望保持身份的人可标 recommended_identity；纯路人且不应建立稳定身份时才用 prompt_only。
  - name 必须使用正文中可识别的真实名字或稳定称谓，不得虚构新姓名。"""

    # 正反例(题材中立,适配通用小说):校准判定与区间边界。
    _cg_examples = """【正例 — 这种该标 CG(题材中立)】
- **段落收束的定格画面**:一个支线/章节结尾,人物以一个充满画面感的姿态收束(如并肩走向远方的背影、伸手相握的瞬间、独自伫立的剪影)——情感顶点 + 收束定格,典型的 CG。区间只覆盖这个定格画面本身。
- **戏剧/情感顶点的单一瞬间**:全章张力的爆发点凝固成一个画面(如关键告白的对视、真相揭穿时的震动、对决中一记决定胜负的定格)。注意只取**那一个定格瞬间**,不要把前面的铺垫过程一起圈进来。
这类画面的共同点:情绪/剧情到达顶点,且**画面感强、是单一的可定格瞬间**。

【关于宽区间的正确处理(重要)】
先看这段高光**讲的是不是同一件事**:
- 情况一(同一件事):某段自始至终是"两人相拥、互诉衷肠"这一个持续画面 → **整段保留为一张 CG**,区间该多宽多宽,不要切短。
- 情况二(混入不同事件):某段是"角色冲入险境对抗 → 受创 → 局势逆转 → 最终情感爆发"——"对抗"和"情感爆发"是**两件不同的事**。
  ❌ 错误一:把整段(对抗到情感爆发)框成一张宽 CG。
  ❌ 错误二:拆成"对抗定格"+"情感定格"两张 CG。
  ✅ 正确:**只保留一个核心事件**。核心 = 情绪/戏剧顶点(这里是结尾的情感爆发),区间只覆盖这个事件的完整画面;前面的"对抗/受创/逆转"是铺垫,**舍弃,不单独出图**。
  → 结果:这一段高光只产出**一张** CG(情感爆发),而不是一张宽的、也不是拆开的多张。

【反例 — 这种不要标 CG(务必避开)】
- **日常互动/玩笑调侃**:角色间的逗趣、打闹、调情,即使气氛亲昵,也只是日常,画面平淡,不标。
- **悬念钩子/过渡引子**:用来勾起下文的搭话、留白、转折提示(如结尾突然有人发问),是过渡而非定格,不标。
- **普通问答与信息推进**:平淡的对话、交代背景、解释设定,无画面高潮,不标。
- **日常过场**:赶路、用餐、更衣、寒暄、场景切换的叙述等,即使略带情绪也不标。
- **一般的小冲突/小波折**:没到"重大转折"或"情感顶点"级别的,不标。
- **判断窍门**:把这段画成一张插画,观众会觉得"这画面值得珍藏"吗?如果只是"几个人在说话/走动",就不够格。"""
    system_prompt = system_prompt.replace("{POSITIVE_NEGATIVE_EXAMPLES}", _cg_examples)

    user_prompt = f"""下面是本章节按顺序排列的正文 beats(格式:[beat_id] (类型|说话人) 内容):

{beats_text}

请按上述标准,判断本节点**是否存在**真正够格的高潮事件(画面感强、值得定格)。
- 如果有:为每个高潮事件框一个 beat_id 区间。判断每段高光"讲的是不是同一件事":
  · 同一件事 → 整段保留为一张 CG(区间该宽就宽,覆盖完整画面,不要切短)。
  · 混入不同事件(画面切换)→ 只保留一个核心事件(情绪/戏剧顶点),区间覆盖它的完整画面,其余舍弃。不要拆成多张,也不要框成一张宽的。
- 如果没有(只是日常推进、玩笑互动、过渡钩子,没有够格的定格画面):**返回空 {{"cgs": []}}**,这是完全正常的,不要勉强凑。
宁缺毋滥;每个区间对应一个事件;日常/玩笑始终不标。"""

    try:
        async with semaphore:
            response, used_model = await _model_fallback.achat(
                client,
                messages=[
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': user_prompt}
                ],
                temperature=0.4
            )
        if _usage_tracker and response.usage:
            _usage_tracker.add_llm_usage(
                response.usage, used_model, provider="openai_compatible"
            )
        raw_reply = response.choices[0].message.content
        result = robust_json_parse(raw_reply, context="cg")
    except Exception as e:
        logger.warning(f"⚠️ [节点 {node_id}] CG 标注失败,本节点不标 CG: {e}")
        logger.warning(f"   LLM 原始返回(截断): {str(raw_reply)[:300] if 'raw_reply' in dir() else 'N/A'}")
        return cg_start_index

    # 注意:robust_json_extract 会把 {"cgs": [...]} 的外层 dict 剥掉,直接返回内层 list。
    # 所以这里要兼容两种形态:
    #   - list  → 就是 cg 区间数组本身(robust_json_extract 剥壳后的常见情况)
    #   - dict  → 取 result["cgs"](未被剥壳时)
    if isinstance(result, list):
        cg_ranges = result
    elif isinstance(result, dict):
        cg_ranges = result.get("cgs", [])
    else:
        cg_ranges = []
    # 诊断日志:把 LLM 返回的 CG 区间数打出来,便于定位"0 CG"是模型不标还是解析问题
    logger.info(f"🎨 [节点 {node_id}] LLM 返回 {len(cg_ranges)} 个 CG 候选区间")
    if not cg_ranges:
        logger.info(f"   LLM 原始返回(截断): {str(raw_reply)[:200]}")
        return cg_start_index

    # beat_id -> 在 ordered_beats 中的位置,用于展开区间
    id_to_pos = {str(b.get("beat_id")): i for i, b in enumerate(ordered_beats)}

    cg_index = cg_start_index
    applied = 0
    for rng in cg_ranges:
        if not isinstance(rng, dict):
            continue
        sid = str(rng.get("start_beat_id", "")).strip()
        eid = str(rng.get("end_beat_id", "")).strip()
        # 校验:两端都必须是真实存在的可参与 beat
        if sid not in id_to_pos or eid not in id_to_pos:
            logger.warning(f"⚠️ [节点 {node_id}] CG 区间 beat_id 不存在,跳过: {sid}~{eid}")
            continue
        start_pos, end_pos = id_to_pos[sid], id_to_pos[eid]
        if start_pos > end_pos:
            start_pos, end_pos = end_pos, start_pos
        # 展开区间:把区间内所有"可参与正文 beat"标上同一 cg_id
        cg_id = f"cg_{cg_index}"
        visual_entities = _normalize_cg_visual_entities(rng.get("visual_entities"))
        marked = 0
        for pos in range(start_pos, end_pos + 1):
            b = ordered_beats[pos]
            if str(b.get("beat_id")) in eligible_ids:
                b["cg"] = cg_id
                if visual_entities:
                    # 仅在本步骤内存中暂存；保存 plots 前会先原子写入 cg 表，
                    # 然后删除该重复字段。持久化 plots 只保留 cg 区间标记。
                    b["cg_visual_entities"] = visual_entities
                marked += 1
        if marked > 0:
            logger.info(f"🎨 [节点 {node_id}] {cg_id}: beat {sid}~{eid} ({marked} 个 beat)")
            cg_index += 1
            applied += 1

    if applied:
        logger.info(f"🎨 [节点 {node_id}] 共标注 {applied} 张 CG")
    return cg_index


async def main(target_task_id=None):
    """执行提取流程"""
    task_id, char_data, md_contents = fetch_source_context(target_task_id)

    # 需求3: 加载 full_name→speaker_id 转换表(name_mappings 表 → 本地文件 → 空表)
    if not task_id:
        raise PlotsExtractionError(
            f"未获取到剧情源数据，无法提取 plots (target_task_id: {target_task_id})"
        )
    load_name_conversion_map(task_id)

    # 加载已知角色名集合,供锚定型关系称呼("<角色>的<关系词>")识别使用
    load_known_character_names(char_data)

    if _usage_tracker:
        _usage_tracker.set_context(task_id, "info_extractor")

    client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL)

    # P1 修复: 两个独立 semaphore,避免 POV 嗅探与正文提取相互抢占
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    pov_semaphore = asyncio.Semaphore(MAX_POV_CONCURRENT)

    master_script = {}
    hook_beats_map = {}

    # 全局失败统计
    global_failed_chunks = 0
    global_total_chunks = 0
    failed_nodes = []  # 记录有失败的节点,便于排查

    logger.info("--- [Plots] 开始提取 (DB Mode) ---")

    tasks = []
    for node_id, content in md_contents.items():
        if content.strip().startswith("PangBai"):
            logger.info(f"⏭️ Skipping {node_id} because content starts with 'PangBai'")
            continue

        if node_id == "chapter_1":
            pattern = re.compile(r'═+[\r\n]+◆◆◆\s*序幕结束\s*·\s*正文开始\s*◆◆◆[\r\n]+═+')
            parts = pattern.split(content)
            if len(parts) == 2:
                hook_text = parts[0]
                content = parts[1]
                hook_beats = process_hook_section(hook_text)
                if hook_beats:
                    hook_beats_map[node_id] = hook_beats
                    await asyncio.to_thread(update_story_tree_hook_id, task_id, len(hook_beats))
            else:
                logger.warning(f"⚠️ 在 {node_id} 中未找到合规的序幕分割线，将视为全正文处理。")

        tasks.append(process_node_content(client, node_id, content, semaphore, pov_semaphore, char_data))

    for completed_task in tqdm(asyncio.as_completed(tasks), total=len(tasks), desc="Extracting"):
        try:
            node_id, beats, node_failed, node_total = await completed_task
            global_failed_chunks += node_failed
            global_total_chunks += node_total
            if node_failed > 0:
                failed_nodes.append((node_id, node_failed, node_total))
            if beats:
                master_script[node_id] = beats
                logger.info(f"   ✅ {node_id}: {len(beats)} beats (raw)")
        except Exception as e:
            # 节点级异常(不是块级)
            logger.error(f"🔥 节点处理整体异常,可能导致整章丢失: {e}")

    for node_id, hook_b in hook_beats_map.items():
        if node_id in master_script:
            master_script[node_id] = hook_b + master_script[node_id]
        else:
            master_script[node_id] = hook_b

    # 全局失败汇总
    if global_failed_chunks > 0:
        global_fail_rate = global_failed_chunks / global_total_chunks if global_total_chunks else 0
        logger.warning(
            f"📊 全局块处理统计: {global_failed_chunks}/{global_total_chunks} 失败 ({global_fail_rate:.1%})"
        )
        for nid, nf, nt in failed_nodes:
            logger.warning(f"   - 节点 {nid}: {nf}/{nt} 失败")
        if global_fail_rate >= 0.2:
            logger.error("🚨 全局失败率超过 20%,强烈建议人工检查输出结果完整性!")

    if master_script:

        # full_name 出口闸门用的主名集合 (角色库所有 name 字段)
        # 防御:即便角色库里有角色 name 本身就是描述短语 (如错把"御剑飞天的女子"当主名),
        # 也不收进合法主名集合 — 否则 sanitize_full_name 会把这种描述短语判为"合法"放行。
        # 角色库的 name 字段并非绝对可信。
        global_canonical_names = set()
        rejected_canonical = []
        if char_data:
            for _ck, _ci in char_data.items():
                _nm = _ci.get("name") if isinstance(_ci, dict) else None
                if not _nm:
                    continue
                if _has_descriptive_pattern(_nm):
                    rejected_canonical.append(_nm)
                    continue
                global_canonical_names.add(_nm)
        if rejected_canonical:
            logger.warning(
                f"⚠️ 角色库中检测到 {len(rejected_canonical)} 个描述性 name 字段,"
                f"已从合法主名集合中剔除: {rejected_canonical}。"
                f"建议上游角色提取流程将这类描述短语放入 other_names 而非 name。"
            )

        sorted_nodes = sorted(master_script.keys(), key=_natural_key)

        # ================= 全局 beat_id 编号 (不再做 scene_id 统筹) =================
        # plots 阶段已纯化:不再判断场景。每个 beat 输出 scene_id=None 占位,
        # 并携带 _src_lines: [start_line, end_line] 行号区间,
        # 由下游 scenes 阶段通过行号对齐回填真正的 scene_id。

        for node_id in sorted_nodes:
            (
                beats,
                long_text_split_count,
                long_text_split_skipped,
            ) = split_long_text_beats(master_script[node_id])
            if long_text_split_count:
                logger.info(
                    f"   ✂️ [节点 {node_id}] 超过 {LONG_TEXT_BEAT_SPLIT_THRESHOLD} 字的 "
                    f"narration/nvl_thought "
                    f"按句号拆分: {long_text_split_count} 条 → "
                    f"{long_text_split_count * 2} 条"
                )
            if long_text_split_skipped:
                logger.warning(
                    f"   ⚠️ [节点 {node_id}] {long_text_split_skipped} 条 "
                    f"narration/nvl_thought 超过 {LONG_TEXT_BEAT_SPLIT_THRESHOLD} 字但没有 "
                    f"可用的内部句号，已保留原 beat"
                )
            final_ordered_beats = []

            node_beat_idx = 0
            in_branch = False
            current_option = None
            option_counters = {'A': 0, 'B': 0}
            pending_option_prefix = None

            variable_update_counters = {}

            def consume_option_prefix(raw_content):
                """把当前选项标题消费到首个可展示正文 beat。"""
                nonlocal pending_option_prefix
                if not pending_option_prefix or not str(raw_content).strip():
                    return raw_content
                prefixed_content = f"{pending_option_prefix}||{raw_content}"
                pending_option_prefix = None
                return prefixed_content

            for beat in beats:
                is_hook = beat.pop("is_hook", False)
                raw_b_type = beat.get("beat_type", "dialogue")
                content = str(beat.get("content", "")).strip()
                src_lines = beat.get("_src_lines", [-1, -1])

                if is_hook:
                    node_beat_idx += 1
                    # hook 也可能是可展示正文；若它紧跟选项，不能让标题悬挂到
                    # 分支结束后再被清掉。
                    content = consume_option_prefix(content)
                    _fn, _sid = sanitize_full_name(beat.get("full_name"), global_canonical_names)
                    ordered_beat = {
                        "beat_id": str(node_beat_idx),
                        "scene_id": None,  # 占位,下游用 _src_lines 行号对齐回填
                        "beat_type": beat.get("beat_type"),
                        "speaker_name": beat.get("speaker_name"),
                        "full_name": _fn,
                        "speaker_id": _sid if _fn else beat.get("speaker_id"),
                        "content": content,
                        "emotion": beat.get("emotion"),
                        "speaker_combination": None,
                        "cg": None,
                        "_src_lines": src_lines,
                    }
                    final_ordered_beats.append(ordered_beat)
                    continue

                # 系统文增强:system_panel 走最小路径。在分支内时沿用分支 beat_id 命名规则。
                if raw_b_type == "system_panel":
                    if not in_branch:
                        node_beat_idx += 1
                        current_beat_id = str(node_beat_idx)
                    else:
                        if not current_option:
                            current_option = 'A'
                        option_counters[current_option] += 1
                        current_beat_id = f"{node_beat_idx}-{current_option}{option_counters[current_option]}"

                    # system_panel 也是可展示正文，可能是选项后的第一个 beat。
                    content = consume_option_prefix(content)

                    ordered_beat = {
                        "beat_id": current_beat_id,
                        "scene_id": None,
                        "beat_type": "system_panel",
                        "speaker_name": beat.get("speaker_name") or "旁白",
                        "full_name": beat.get("full_name") or beat.get("speaker_name") or "旁白",
                        "speaker_id": beat.get("speaker_id") or "PangBai",
                        "content": content,
                        "emotion": "calm",
                        "speaker_combination": None,
                        "cg": None,
                        "_src_lines": src_lines,
                    }
                    final_ordered_beats.append(ordered_beat)
                    continue

                if raw_b_type == "branch_start":
                    in_branch = True
                    current_option = None
                    option_counters = {'A': 0, 'B': 0}
                    pending_option_prefix = None
                    continue

                if raw_b_type == "branch_end":
                    if pending_option_prefix:
                        logger.warning(
                            "⚠️ 软分支选项 '%s' 未提取到任何正文 beat，标题无法写入 plots",
                            pending_option_prefix,
                        )
                    in_branch = False
                    current_option = None
                    pending_option_prefix = None
                    continue

                if raw_b_type == "option":
                    if pending_option_prefix:
                        logger.warning(
                            "⚠️ 软分支选项 '%s' 未提取到正文，已开始处理下一选项",
                            pending_option_prefix,
                        )
                    in_branch = True
                    raw_opt_text = beat.get("raw_option", content)
                    match = re.search(r'选项\s*([A-Z])', raw_opt_text)
                    if match:
                        current_option = match.group(1)
                    else:
                        current_option = 'B' if option_counters.get('A', 0) > 0 else 'A'
                    if current_option not in option_counters:
                        option_counters[current_option] = 0

                    pending_option_prefix = content
                    continue

                if raw_b_type == "variable_update":
                    vc = variable_update_counters.get(node_beat_idx, 0) + 1
                    variable_update_counters[node_beat_idx] = vc
                    current_beat_id = f"{node_beat_idx}-C{vc}"
                    effective_b_type = "variable_update"
                else:
                    effective_b_type = raw_b_type
                    if not in_branch:
                        node_beat_idx += 1
                        current_beat_id = str(node_beat_idx)
                    else:
                        if not current_option:
                            current_option = 'A'
                        option_counters[current_option] += 1
                        current_beat_id = f"{node_beat_idx}-{current_option}{option_counters[current_option]}"

                # 所有非结构、非 variable_update beat 都是可展示正文。
                # 不限定 dialogue/narration，避免 nvl_thought、nvl_narration
                # 等类型导致选项标题丢失；variable_update 只记录控制效果，
                # 不应消费标题，标题仍需写入其后的首个正文 beat。
                if pending_option_prefix and effective_b_type != "variable_update":
                    content = consume_option_prefix(content)

                current_emotion = beat.get("emotion", "calm")

                if effective_b_type == "variable_update":
                    ordered_beat = {
                        "beat_id": current_beat_id,
                        "scene_id": None,
                        "beat_type": effective_b_type,
                        "speaker_name": None,
                        "full_name": None,
                        "speaker_id": None,
                        "content": content,
                        "emotion": None,
                        "speaker_combination": None,
                        "cg": None,
                        "_src_lines": src_lines,
                    }
                else:
                    # full_name 出口闸门:描述性短语一律清成 None,speaker_id 随之 None
                    _fn, _sid = sanitize_full_name(beat.get("full_name"), global_canonical_names)
                    ordered_beat = {
                        "beat_id": current_beat_id,
                        "scene_id": None,  # 占位,下游用 _src_lines 行号对齐回填
                        "beat_type": effective_b_type,
                        "speaker_name": beat.get("speaker_name"),
                        "full_name": _fn,
                        "speaker_id": _sid,
                        "content": content,
                        "emotion": current_emotion,
                        "speaker_combination": None,
                        "cg": None,
                        "_src_lines": src_lines,
                    }
                final_ordered_beats.append(ordered_beat)

            master_script[node_id] = final_ordered_beats

        # ========== CG 标注 pass(主组装之后,beat_id 已定;全局连续编号) ==========
        # 跨节点累加计数器:cg_1/cg_2... 在整个任务下连续递增。
        cg_counter = 1
        cg_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        for node_id in sorted_nodes:
            cg_counter = await annotate_cg_for_node_async(
                client, node_id, master_script[node_id], cg_semaphore, cg_counter
            )
        total_cgs = cg_counter - 1
        if total_cgs > 0:
            logger.info(f"🎨 全任务共标注 {total_cgs} 张 CG (cg_1 ~ cg_{total_cgs})")
        else:
            logger.info("🎨 本任务未标注任何 CG")

        # 完整 CG 人物合同必须先落到 cg 表；只有写入成功后，才从 plots
        # 删除重复字段，确保无台词可见人物不会因规范化而丢失。
        cg_annotations = build_cg_annotation_draft(master_script)
        stripped_cg_details = strip_cg_details_from_plots(master_script)
        logger.info(
            "🎨 CG 数据归一化完成: cg 表 %s 条，plots 移除 %s 个重复字段",
            len(cg_annotations),
            stripped_cg_details,
        )

        logger.info(f"✅ 提取完成，包含 {len(master_script)} 个节点的剧情。")
        db_saved, local_saved = await asyncio.to_thread(
            save_to_plots_table,
            task_id,
            master_script,
            cg_annotations,
        )
        if _usage_tracker:
            _usage_tracker.flush()
        await client.close()
        if not db_saved and not local_saved:
            raise PlotsExtractionError(
                f"plots 既未写入数据库也未落盘 (TaskID: {task_id})"
            )
        if not db_saved:
            logger.warning(
                "⚠️ [Plots/CG] 未写入数据库，已降级为共享 "
                "plots.json + cg.json，下游按降级合同读取"
            )
        return task_id
    else:
        await client.close()
        if _usage_tracker:
            _usage_tracker.flush()
        raise PlotsExtractionError(
            f"未提取到任何有效剧情，plots 无产出 (TaskID: {task_id})"
        )


if __name__ == "__main__":
    tid = asyncio.run(main(target_task_id="test_fb89e1c4"))
    if tid:
        logger.info(f"Process finished for Task ID: {tid}")
