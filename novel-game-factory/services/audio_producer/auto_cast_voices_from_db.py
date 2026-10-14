import os
import json
import re
import sys
from collections import Counter
from openai import OpenAI
from sqlalchemy import text
from logger_config import setup_logger

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from common.config import DATABASE_URL, OUTPUTS_PATH
from common.audio_languages import AUDIO_LANG_CONFIGS
from common.llm_fallback import ModelFallback
from common.name_mapping_utils import get_source_to_change_conversion
try:
    from common.audio_db import get_business_engine, get_platform_engine
except ImportError:  # 离线单测的 common 桩包只提供旧共享 engine
    from common.platform_db import platform_engine as _platform_engine

    def get_platform_engine():
        return _platform_engine

    def get_business_engine():
        return _platform_engine
from resume_state import ResumeState, atomic_write_json, file_digest, stable_digest, valid_json

try:
    from common.usage_tracker import tracker as _usage_tracker
except ImportError:
    _usage_tracker = None

logger = setup_logger("Step3_Cast", "step3_cast.log")

try:
    from pypinyin import lazy_pinyin
except ImportError as e:
    logger.error("❌ 缺少 pypinyin 库，请运行：pip install pypinyin")
    raise ImportError(
        "pypinyin 是必需依赖，否则中文角色 ID 无法转拼音，会导致下游音色映射全部失效。"
        "请先 pip install pypinyin。"
    ) from e

# BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
# BASE_URL = "https://ai.farmmx.com/v1"
BASE_URL = os.getenv("PLOT_BASE_URL", "http://sefutech-api.kube-test.svc:3000/v1")

# DashScope OpenAI 兼容模式客户端
client = OpenAI(
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url=BASE_URL,
    max_retries=0,
    timeout=max(
        60.0, float(os.getenv("MINIMAX_CAST_TIMEOUT_SECONDS", "600"))
    ),
)

# ================= 配置区域 =================

DB_URL = DATABASE_URL
# characters/plots/name_mappings 已迁平台库(agent_platform)
from common.config import PLATFORM_DATABASE_URL as PLATFORM_DB_URL

ALLOWED_TABLES = {"characters", "plots", "voice_mappings"}

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))

VOICES_FILE_PATH = os.path.join(CURRENT_DIR, "voice/minimax_voices.json")

OUTPUT_BASE_DIR = OUTPUTS_PATH

MODEL_NAME = "qwen3.7-max"
# 主模型偶发不稳定时自动改用备选模型重发同一请求（同中转站、同调用参数）。
_model_fallback = ModelFallback(MODEL_NAME, name="Step3Cast", logger=logger)
STEP_CACHE_VERSION = "minimax-casting-v3"
MAIN_CAST_BATCH_SIZE = max(1, int(os.getenv("MINIMAX_CAST_BATCH_SIZE", "8")))
MAIN_CAST_CANDIDATES_PER_ROLE = max(
    1, int(os.getenv("MINIMAX_CAST_CANDIDATES_PER_ROLE", "8"))
)
NPC_PROFILE_BATCH_SIZE = max(
    1, int(os.getenv("MINIMAX_NPC_PROFILE_BATCH_SIZE", "40"))
)

INCOMPATIBLE_TAG_RULES = {
    "villain_traits": [
        "反派", "贪婪", "阴险", "残忍", "粗暴", "流氓", "恶少", "凶狠", "狡诈", "虚伪", "势利",
        "villain", "greedy", "sinister", "cruel", "violent", "bully", "vicious", "cunning", "hypocritical", "snobbish",
        "evil", "ruthless", "manipulative"
    ],
    "villain_avoid_tags": ["阳光", "清澈", "可爱", "正能量", "治愈", "天真", "高僧", "萝莉", "暖男", "稚气", "乖巧",
                           "邻家"],

    "rough_traits": [
        "粗犷", "豪迈", "打手", "农民", "土气", "大老粗", "无赖",
        "rough", "bold", "thug", "peasant", "rustic", "boorish", "rascal", "wild", "aggressive"
    ],
    "rough_avoid_tags": ["清脆", "甜美", "撒娇", "温润", "优雅", "知性", "高贵", "御姐", "绅士", "儒雅"]
}

NPC_POOL_CONFIG = {
    "Male": 3,
    "Female": 3,
    "Child": 2
}

# MiniMax 链只负责日语；英语由 ElevenLabs 中转链选角。
MINIMAX_LOCALES = tuple(
    item.strip()
    for item in os.getenv("MINIMAX_LOCALES", "ja-JP").split(",")
    if item.strip()
)
NON_VOICED_ROLE_IDS = {"xitong", "system"}


def is_non_voiced_role(role_id):
    return str(role_id or "").strip().lower() in NON_VOICED_ROLE_IDS


# 儿童(10)和少年(20)的年龄权重。童声/成人的边界是独立的排序维度，
# 不再用大数罚分模拟，避免年龄罚分反过来压过性别硬约束。
YOUNG_AGE_WEIGHTS = (10, 20)


def is_young_age_weight(age_weight):
    return age_weight in YOUNG_AGE_WEIGHTS


# 跨语言借音色的偏好顺序。日语只借中文（音系更接近），不退到英文。
LANGUAGE_FALLBACK_ORDER = {
    "日语": ("中文",),
    "英语": ("中文",),
    "中文": ("英语",),
}


def _canonical_language_name(language):
    value = str(language or "").strip().lower()
    if value in ("中文", "汉语", "普通话", "chinese", "zh", "zh-cn"):
        return "中文"
    if value in ("英语", "英文", "english", "en", "en-us", "en-gb"):
        return "英语"
    if value in ("日语", "日文", "japanese", "ja", "ja-jp"):
        return "日语"
    return str(language or "").strip()


def _voice_language_entries(voice):
    value = voice.get("language", "") if isinstance(voice, dict) else voice
    if isinstance(value, (list, tuple, set)):
        return [str(item or "").strip().lower() for item in value]
    return [str(value or "").strip().lower()]


def normalize_gender(value):
    """将中英文性别标签归一为“男/女/未知”。"""
    text = str(value or "").strip().lower()
    if text in {"male", "m", "man", "boy", "男", "男性", "少男"}:
        return "男"
    if text in {"female", "f", "woman", "girl", "女", "女性", "少女"}:
        return "女"
    return "未知"


def _language_entry_matches(entry, canonical):
    if any(marker in entry for marker in ("multi", "多语")):
        return True
    if canonical == "中文":
        if any(marker in entry for marker in ("中文", "中语", "汉语", "普通话", "chinese", "mandarin")):
            return True
        return bool(re.search(r"(^|[^a-z])zh(?:[-_][a-z]+)?($|[^a-z])", entry))
    if canonical == "英语":
        if any(marker in entry for marker in ("英语", "英文", "english")):
            return True
        return bool(re.search(r"(^|[^a-z])en(?:[-_][a-z]+)?($|[^a-z])", entry))
    if canonical == "日语":
        if any(marker in entry for marker in ("日语", "日文", "japanese")):
            return True
        return bool(re.search(r"(^|[^a-z])ja(?:[-_][a-z]+)?($|[^a-z])", entry))
    return canonical.lower() == entry


def is_voice_language_match(voice, target_lang):
    """音色是否直接支持目标语言； multi/多语视为直接匹配。"""
    canonical = _canonical_language_name(target_lang)
    return any(
        _language_entry_matches(entry, canonical)
        for entry in _voice_language_entries(voice)
    )


def voice_language_tier(voice, target_lang):
    """返回确定性语言层级：0=目标语言，1..N=配置的降级顺序，N+1=其他语言。"""
    canonical = _canonical_language_name(target_lang)
    if is_voice_language_match(voice, canonical):
        return 0
    for tier, fallback in enumerate(
        LANGUAGE_FALLBACK_ORDER.get(canonical, ()), start=1
    ):
        if is_voice_language_match(voice, fallback):
            return tier
    return len(LANGUAGE_FALLBACK_ORDER.get(canonical, ())) + 1


def unsupported_language_tier(voice, target_lang):
    """未在「目标语言 + 显式降级链」里的音色排在可复用音色之后。"""
    canonical = _canonical_language_name(target_lang)
    max_supported_tier = len(LANGUAGE_FALLBACK_ORDER.get(canonical, ()))
    return int(voice_language_tier(voice, canonical) > max_supported_tier)


def cross_language_penalty(voice, target_lang):
    """兼容旧调用方：返回语言层级，数字越小越优。"""
    return voice_language_tier(voice, target_lang)


def _contains_age_marker(text_value, markers):
    for marker in markers:
        if re.search(r"[a-z]", marker):
            if re.search(
                rf"(?<![a-z]){re.escape(marker)}(?![a-z])",
                text_value,
            ):
                return True
        elif marker in text_value:
            return True
    return False


def _age_weight_or_none(age_cat):
    if not age_cat:
        return None
    value = str(age_cat).lower().strip()
    if value in {"未知", "unknown", "none", "other", "u", "n/a", "na"}:
        return None

    age_markers = (
        (10, (
            "child", "儿童", "幼儿", "baby", "toddler", "infant", "newborn",
            "kid", "婴儿", "宝宝",
        )),
        (20, (
            "teen", "teenager", "少年", "adolescent", "juvenile", "preteen", "tween",
        )),
        # middle-aged 同时包含 aged，必须先于老年规则。
        (40, ("middle", "中年", "mid-life", "middle-aged")),
        (50, (
            "elderly", "old", "老年", "老人", "senior", "aged", "retired",
            "grandpa", "grandma", "grandfather", "grandmother", "granny",
        )),
        (30, (
            "youth", "young", "青年", "student", "college", "young adult", "adult",
        )),
    )
    for weight, markers in age_markers:
        if _contains_age_marker(value, markers):
            return weight
    return None


def get_age_weight(age_cat):
    """将中英文年龄段描述转换为数值权重；未知值兼容返回青年权重。"""
    return _age_weight_or_none(age_cat) or 30


def normalize_character_age(age_cat):
    """规范化 characters 表中的角色年龄；缺失/未知按业务约定归为青年。"""
    return "青年" if _age_weight_or_none(age_cat) is None else age_cat


def fetch_data_from_db(table_name, target_task_id=None):
    """
    通用数据库读取函数
    """
    if table_name not in ALLOWED_TABLES:
        raise ValueError(f"不允许的表名：{table_name}。允许的表：{ALLOWED_TABLES}")

    # characters/plots 已迁平台库(agent_platform);voice_mappings 走业务库,不经此函数读取
    with get_platform_engine().connect() as conn:
        task_id = target_task_id

        if not task_id:
            logger.info(f"🔍 [{table_name}] 未指定 Task ID，查找最新数据……")
            sql_latest = text(f"SELECT task_id FROM {table_name} ORDER BY id DESC LIMIT 1")
            result = conn.execute(sql_latest).fetchone()
            if not result:
                logger.error(f"❌ {table_name} 表为空")
                return None, None
            task_id = result[0]
            logger.info(f"自动锁定最新任务：{task_id}")
        else:
            logger.info(f"🚀 [{table_name}] 锁定指定任务：{task_id}")

        sql_data = text(f"SELECT data FROM {table_name} WHERE task_id = :tid")
        result_data = conn.execute(sql_data, {"tid": task_id}).fetchone()

        if not result_data:
            logger.error(f"❌ 未找到 task_id='{task_id}' 的数据")
            return None, None

        return task_id, result_data[0]


def save_mapping_to_db(task_id, mapping_data):
    """
    将生成的音色映射表保存到 PostgreSQL 数据库
    使用 ON CONFLICT 实现有则更新、无则插入
    返回 True 表示保存成功，False 表示失败，方便调用方判断。
    """
    if not task_id:
        logger.warning("⚠️ 缺少 task_id，无法保存到数据库")
        return False

    try:
        with get_business_engine().begin() as conn:
            sql = text("""
                       INSERT INTO voice_mappings (task_id, data, create_time, update_time)
                       VALUES (:task_id, :data, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) ON CONFLICT (task_id) DO
                       UPDATE
                           SET data = EXCLUDED.data,
                           update_time = CURRENT_TIMESTAMP;
                       """)
            conn.execute(sql, {
                "task_id": str(task_id),
                "data": json.dumps(mapping_data, ensure_ascii=False)
            })
        logger.info(f"✅ [DB] 成功保存映射数据到 voice_mappings 表 (Task: {task_id})")
        return True
    except Exception as e:
        logger.error(f"❌ [DB] 保存到数据库失败：{e}")
        return False


def fetch_character_data(target_task_id=None):
    return fetch_data_from_db("characters", target_task_id)


def fetch_plot_data(target_task_id=None):
    return fetch_data_from_db("plots", target_task_id)


# 全局 full_name -> speaker_id 转换映射（运行时由 fetch_name_mappings 填充）
_NAME_CONVERSION = {}


def to_pascal_case_speaker_id(raw_text):
    """生成合规的 speaker_id (拼音规则与驼峰规则)"""
    if not raw_text:
        return None

    if re.search(r'[\u4e00-\u9fa5]', raw_text):
        return "".join([p.capitalize() for p in lazy_pinyin(raw_text)])

    raw_text = re.sub(r"['‘’]s\b", "", raw_text, flags=re.IGNORECASE)
    raw_text = re.sub(r"['‘’]", "", raw_text)
    raw_text = re.sub(r"[^a-zA-Z0-9]", " ", raw_text)
    words = raw_text.split()
    return "".join(word.capitalize() for word in words)


def fetch_name_mappings(target_task_id=None):
    """
    获取 name_mappings 数据（结构同 plots 表：task_id + data）。
    优先 DB（name_mappings 表），失败降级本地 storage/outputs/{task_id}/name_mappings.json，
    都没有则返回空字典（回退到默认转换规则）。
    返回 conversion 字典：{full_name: speaker_id}
    """
    data = None
    if target_task_id:
        try:
            with get_platform_engine().connect() as conn:
                sql = text("SELECT data FROM name_mappings WHERE task_id = :tid")
                row = conn.execute(sql, {"tid": target_task_id}).fetchone()
                if row:
                    data = row[0]
        except Exception as e:
            logger.warning(f"⚠️ name_mappings 数据库读取失败：{e}，尝试本地文件……")

    if data is None and target_task_id:
        fallback_path = os.path.join(
            OUTPUT_BASE_DIR, str(target_task_id), "name_mappings.json"
        )
        if os.path.exists(fallback_path):
            try:
                with open(fallback_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                logger.info(f"📁 [Cast] 从本地加载 name_mappings：{fallback_path}")
            except Exception as e:
                logger.warning(f"⚠️ name_mappings 本地读取失败：{e}")

    if data is None:
        logger.info("ℹ️ 未找到 name_mappings，使用默认转换规则。")
        return {}

    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception as e:
            logger.warning(f"⚠️ name_mappings JSON 解析失败：{e}")
            return {}

    conversion = get_source_to_change_conversion(data)
    logger.info(f"✅ [Cast] 已加载 conversion 映射 {len(conversion)} 条。")
    return conversion


def get_clean_char_id(name):
    """
    根据规则生成清洗后的 ID（PascalCase）。
    优先：full_name 命中 conversion，直接使用键对应的值；
    未命中则交给 to_pascal_case_speaker_id（中文转拼音驼峰；
    英文保留空格分词、每词首字母大写、去所有格 's）。
    """
    name = str(name).strip()

    # 优先用原始 full_name 命中 conversion
    if name in _NAME_CONVERSION:
        return _NAME_CONVERSION[name]

    result = to_pascal_case_speaker_id(name)
    return result or "UnknownRole"


def get_primary_personality_text(character_data):
    """
    提取 personality_id=1 的基础性格描述。

    characters 表的新结构使用 info + traits 描述性格；其他 personality_id
    表示角色在特定阶段的性格变化，不参与基础音色判断。
    """
    variants = character_data.get("personality_variants", [])
    if not isinstance(variants, list):
        return []

    primary_variant = None
    for variant in variants:
        if not isinstance(variant, dict):
            continue

        personality_id = variant.get("personality_id")
        is_primary = personality_id == 1 or (
            isinstance(personality_id, str) and personality_id.strip() == "1"
        )

        if is_primary:
            primary_variant = variant
            break

    if primary_variant is None:
        return []

    personality_text = []

    info = primary_variant.get("info")
    if isinstance(info, str) and info.strip():
        personality_text.append(info.strip())

    traits = primary_variant.get("traits", [])
    if isinstance(traits, list):
        personality_text.extend(
            trait.strip()
            for trait in traits
            if isinstance(trait, str) and trait.strip()
        )
    elif isinstance(traits, str) and traits.strip():
        personality_text.append(traits.strip())

    return personality_text


def parse_character_profiles(all_data):
    """
    将数据库中的 JSON (Dict) 转换为 AI 选角所需的 List 格式
    """
    profiles = []
    if not all_data: return []

    logger.info(f"📂 解析人物数据")

    for char_key, data in all_data.items():
        try:
            if not isinstance(data, dict):
                logger.warning(f"⚠️ 跳过非字典格式角色：{char_key}")
                continue

            if "personality_variants" not in data:
                continue

            char_id = get_clean_char_id(char_key)

            personalities = get_primary_personality_text(data)

            occupation = "未知身份"
            if data.get("clothing_variants"):
                occupation = data["clothing_variants"][0].get("occupation", "") or "未知身份"

            # characters 表中的年龄缺失或未知，统一按青年角色进入选角。
            age_cat = "青年"
            gender = "男"

            variants = data.get("whole_body_variants", [])

            if variants:
                valid_variants = [v for v in variants if isinstance(v, dict)]
                primary_variant = next(
                    (
                        v for v in valid_variants
                        if str(v.get("whole_body_id")).strip() == "1"
                    ),
                    {},
                )
                age_value = primary_variant.get("age_category")
                if age_value and not _is_unknown_age(age_value):
                    age_cat = normalize_character_age(age_value)
                gender_value = primary_variant.get("gender")
                if _gender_code(gender_value) == "F":
                    gender = "女"
                else:
                    # 业务约定：角色性别缺失或未知时统一按男性处理。
                    gender = "男"

            try:
                important_rank = int(data.get("important_rank", 1))
            except (ValueError, TypeError):
                logger.warning(
                    f"⚠️ {char_key} 的 important_rank 非法值 "
                    f"({data.get('important_rank')!r})，回退到 1"
                )
                important_rank = 1

            profiles.append({
                "id": char_id,
                "original_name": char_key,
                "gender": gender,
                "age_category": age_cat,
                "occupation": occupation,
                "traits": ", ".join(personalities[:30]),
                "important_rank": important_rank
            })
        except Exception as e:
            # 单角色失败不影响其他角色
            logger.error(f"❌ 解析角色 {char_key} 失败：{e}，跳过")
            continue

    profiles.sort(
        key=lambda x: (
            -x["important_rank"],
            str(x["id"]).casefold(),
            str(x["id"]),
        )
    )
    return profiles


def extract_npc_list(plot_data, existing_char_ids):
    """
    从剧情中提取 NPC
    """
    npc_names = []
    seen_npc_ids = set()
    logger.info("🕵️ 正在扫描剧情提取 NPC……")

    for chapter_key, beats in plot_data.items():
        if isinstance(beats, dict):
            beats = beats.get("script_timeline")
        if not isinstance(beats, list): continue

        for beat in beats:
            speaker = beat.get("speaker_name")
            if not speaker: continue

            clean_id = get_clean_char_id(speaker)

            if clean_id in ["PangBai", "Narrator", "XiTong", "System"]:
                continue

            if clean_id in existing_char_ids:
                continue

            if clean_id in seen_npc_ids:
                continue
            seen_npc_ids.add(clean_id)
            npc_names.append(speaker)

    return npc_names


def load_voice_library(path):
    """加载本地音色库"""
    if not os.path.exists(path):
        logger.error(f"❌ 错误：音色库文件不存在 -> {path}")
        return []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        logger.error(f"❌ 读取 voices.json 失败：{e}")
        return []

    simplified_voices = []
    seen_voice_ids = set()
    voice_list = data.get("voices_list", []) if isinstance(data, dict) else data
    for v in voice_list:
        voice_id = str(v.get("voice_id") or "").strip() if isinstance(v, dict) else ""
        if isinstance(v, dict) and v.get("status") == "OK" and voice_id:
            if voice_id in seen_voice_ids:
                logger.warning(f"⚠️ 音色库存在重复 voice_id，已忽略后续记录：{voice_id}")
                continue
            seen_voice_ids.add(voice_id)
            simplified_voices.append({
                "voice_id": voice_id,
                "gender": normalize_gender(v.get("gender")),
                "age_category": v.get("age_category", "未知"),
                "tags": v.get("tags", []),
                "description": v.get("description", ""),
                "language": v.get("language", []),
            })
    return simplified_voices


def _get_ai_casting_batch(chars, voices, target_lang="中文"):
    """AI 为一小批主要角色选角。"""
    if not chars or not voices: return {}

    system_prompt = f"""
你是一位专业的配音选角导演。
你的任务是根据【剧本人物设定】从【可用音色库】中为每个角色挑选最匹配的 voice_id。

【重要原则】
1. **真实性**：你必须严格从【可用音色库】中选择 `voice_id`，**严禁臆造**不存在的 ID。
2. **唯一性**：**绝对禁止重复**。每个角色必须分配一个**独占且唯一**的 voice_id。
3. **Key 一致性**：返回 JSON 的 key 必须与输入【待分配人物列表】中的 `id` 字段**完全一致**，
   不要翻译、不要改写、不要使用 `original_name`。

【匹配逻辑】
1. **硬性指标**：
   - **性别（gender）** 必须严格一致（中英文等价："男"="male"，"女"="female"）。
   - **年龄段（age_category）** 必须接近（中英文等价）。
   - **语言（language）**：优先选择支持 **{target_lang}** 的音色。

2. **软性指标（核心）**：
   - **身份优先**：必须重点参考人物的 occupation（身份/职业）。
     例如：皇帝/高官应匹配带有"威严"、"统摄力"的音色；
     太监/仆人应避开"霸气"、"傲慢"，选择"顺从"、"卑微"的音色。
     如果 occupation 是"未知身份"，则忽略此项，只看性格。
   - **性格契合**：利用人物的 traits 与音色的 tags 和 description 进行气质匹配。
   - **避雷指南**：反派/粗俗角色绝对避开"阳光"、"治愈"标签；粗犷角色避开"温润"、"知性"。

【输出格式】
只返回一个纯 JSON 对象，格式为 {{"角色ID": "voice_id"}}。不要包含 Markdown 标记。
"""
    user_content = f"""
【待分配人物列表】：
{json.dumps(chars, ensure_ascii=False, indent=2)}

【可用音色库】：
{json.dumps(voices, ensure_ascii=False, indent=2)}

请开始分配。
"""
    logger.info(f"🤖 (Main Cast) 正在为 {len(chars)} 个主要角色选角……")
    try:
        response, used_model = _model_fallback.chat(
            client,
            messages=[
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_content},
            ],
            temperature=0,
        )
        if _usage_tracker and getattr(response, 'usage', None):
            try:
                _usage_tracker.add_llm_usage(
                    response.usage, used_model, provider="dashscope"
                )
            except Exception as exc:
                logger.warning(f"⚠️ API 用量记录失败，不影响选角：{exc}")
        content = response.choices[0].message.content
        content = content.replace("```json", "").replace("```", "").strip()
        try:
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                logger.error(f"❌ AI 返回根节点不是 JSON 对象：{type(parsed).__name__}")
                return {}
            valid_role_ids = {str(char.get("id")) for char in chars}
            valid_voice_ids = {str(voice.get("voice_id")) for voice in voices}
            cleaned = {}
            for role_id, voice_id in parsed.items():
                role_id = str(role_id)
                voice_id = str(voice_id)
                if role_id in valid_role_ids and voice_id in valid_voice_ids:
                    cleaned[role_id] = voice_id
                else:
                    logger.warning(
                        f"⚠️ 忽略 AI 的无效选角建议：{role_id!r} -> {voice_id!r}"
                    )
            return cleaned
        except json.JSONDecodeError as je:
            # 给出更详细的诊断，而不是简单返回 {}
            preview = content[:200] if content else "（空）"
            logger.error(f"❌ AI 返回非合法 JSON：{je}，content 前 200 字：{preview}")
            return {}
    except Exception as e:
        logger.error(f"❌ get_ai_casting 发生错误：{e}")
        return {}


def _ai_candidate_pool(chars, voices, target_lang):
    """为一批角色构造精简候选并集，避免把整个音色库发给模型。"""
    selected = {}
    for character in chars:
        ranked = sorted(
            _prepare_voice_pool(voices),
            key=lambda voice: _main_candidate_key(
                character, voice, {}, target_lang, {}
            ),
        )
        for voice in ranked[:MAIN_CAST_CANDIDATES_PER_ROLE]:
            selected.setdefault(voice["voice_id"], voice)
    return list(selected.values())


def get_ai_casting(chars, voices, target_lang="中文"):
    """分批生成主要角色 AI 选角建议，最终唯一性由本地规则统一校正。"""
    if not chars or not voices:
        return {}
    merged = {}
    total_batches = (len(chars) + MAIN_CAST_BATCH_SIZE - 1) // MAIN_CAST_BATCH_SIZE
    for start in range(0, len(chars), MAIN_CAST_BATCH_SIZE):
        batch = chars[start:start + MAIN_CAST_BATCH_SIZE]
        batch_number = start // MAIN_CAST_BATCH_SIZE + 1
        candidates = _ai_candidate_pool(batch, voices, target_lang)
        logger.info(
            f"🤖 (Main Cast) 批次 {batch_number}/{total_batches}："
            f"{len(batch)} 个角色，{len(candidates)} 个候选音色"
        )
        merged.update(_get_ai_casting_batch(batch, candidates, target_lang))
    return merged


def _analyze_npc_profiles_batch(npc_names):
    """
    AI 批量推断 NPC 的性别和年龄
    """
    if not npc_names: return {}

    system_prompt = """
你是一个专业的剧本分析助手。请分析角色名称，并推断出他们的【性别】和【年龄段】。

规则：
1. 性别选项："男"、"女"、"未知"。没有可靠线索时必须输出"未知"，不得默认为男性。
2. 年龄段选项："儿童"、"少年"、"青年"、"中年"、"老年"、"未知"。没有可靠线索时输出"未知"。
3. **重要提示**：即使角色的名字是英文，也请务必使用**中文**输出其属性结果。
4. **Key 一致性**：返回 JSON 的 key 必须与输入中给出的角色名**完全一致**（逐字符，
   不翻译、不改写、不归一化大小写）。

推断逻辑参考：
- 带有 Kid, Boy, Girl, Child 等字眼（如：小孩、男孩、女孩、童） -> 推断为 "儿童" 或 "少年"
- 带有 Old, Grandpa, Granny, Elder 等字眼（如：老、爷、奶、长者） -> 推断为 "老年"
- 带有 Uncle, Aunt, Boss, Mrs. 等字眼（如：叔、姨、老板、夫人） -> 推断为 "中年"
- 带有 Driver, Guard, Assistant 等职业/身份字眼（如：司机、保安、助手、丫鬟）
  -> 根据普遍的刻板印象推断（例如：司机->男，女仆/丫鬟->女）

输出 JSON 格式（请严格参考此格式，只输出纯 JSON）：
{
    "Driver": {"gender": "男", "age_category": "青年"},
    "Little Mary": {"gender": "女", "age_category": "儿童"}
}
"""
    user_content = f"Analyze these characters: {json.dumps(npc_names, ensure_ascii=False)}"

    logger.info(f"🤖 (NPC Profile) 正在分析 {len(npc_names)} 个临时角色……")
    try:
        response, used_model = _model_fallback.chat(
            client,
            messages=[
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_content},
            ],
            temperature=0,
        )
        if _usage_tracker and getattr(response, 'usage', None):
            try:
                _usage_tracker.add_llm_usage(
                    response.usage, used_model, provider="dashscope"
                )
            except Exception as exc:
                logger.warning(f"⚠️ API 用量记录失败，不影响 NPC 分析：{exc}")
        content = response.choices[0].message.content
        content = content.replace("```json", "").replace("```", "").strip()
        try:
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                logger.error(f"❌ NPC Profile AI 返回根节点不是 JSON 对象")
                return {}
            expected_names = set(npc_names)
            return {
                str(name): profile
                for name, profile in parsed.items()
                if str(name) in expected_names and isinstance(profile, dict)
            }
        except json.JSONDecodeError as je:
            preview = content[:200] if content else "（空）"
            logger.error(f"❌ NPC Profile AI 返回非合法 JSON：{je}，content 前 200 字：{preview}")
            return {}
    except Exception as e:
        logger.error(f"❌ analyze_npc_profiles 发生错误：{e}")
        return {}


def analyze_npc_profiles(npc_names):
    """分批推断 NPC 性别和年龄，单批失败时由调用方补默认画像。"""
    if not npc_names:
        return {}
    merged = {}
    total_batches = (
        len(npc_names) + NPC_PROFILE_BATCH_SIZE - 1
    ) // NPC_PROFILE_BATCH_SIZE
    for start in range(0, len(npc_names), NPC_PROFILE_BATCH_SIZE):
        batch = npc_names[start:start + NPC_PROFILE_BATCH_SIZE]
        batch_number = start // NPC_PROFILE_BATCH_SIZE + 1
        logger.info(
            f"🤖 (NPC Profile) 批次 {batch_number}/{total_batches}："
            f"{len(batch)} 个角色"
        )
        merged.update(_analyze_npc_profiles_batch(batch))
    return merged


def infer_npc_profile_from_name(name):
    """按 NPC 名称中的明确称谓推断画像；无可靠线索时走业务默认。

    NPC 没有 characters 画像，名称规则必须优先于任何模型猜测：明确出现
    child/boy/girl/少年/少女等标记时才进入年轻年龄段；否则统一按男性青年。
    """
    text = str(name or "").strip().lower()
    def has_marker(marker):
        if marker.isascii() and marker.replace("-", "").isalpha():
            return re.search(
                # 兼容 childA/girlB 等 NPC 标记；仅允许一个字母后缀，
                # 避免把 childish/womanly 等普通单词误判为角色标签。
                rf"(?<![a-z]){re.escape(marker)}(?:[a-z](?![a-z]))?(?![a-z])",
                text,
            ) is not None
        return marker in text

    child_markers = (
        "child", "kid", "baby", "infant", "toddler", "newborn", "小孩", "孩子",
        "孩童", "儿童", "男孩", "女孩", "婴儿", "宝宝", "boy", "girl",
    )
    teen_markers = (
        "teen", "teenager", "adolescent", "juvenile", "preteen", "tween",
        "少年", "少女",
    )
    old_markers = (
        "old", "elder", "elderly", "senior", "aged", "retired", "grandpa",
        "grandma", "grandfather", "grandmother", "granny", "爷爷", "奶奶",
        "老人", "老者", "长者", "退休",
    )
    middle_markers = (
        "uncle", "aunt", "boss", "mrs", "夫人", "叔叔", "大叔", "阿姨", "姨",
        "老板", "中年",
    )
    female_markers = (
        "female", "woman", "girl", "女", "少女", "女孩", "女士", "女佣", "女仆", "女招待",
        "waitress", "母亲", "妈妈", "奶奶", "祖母", "夫人", "公主", "王后", "阿姨",
    )
    male_markers = (
        "male", "man", "boy", "男", "少年", "男孩", "父亲", "爸爸", "爷爷", "祖父",
        "先生", "王子", "国王", "叔叔", "大叔", "老板",
    )
    female = any(has_marker(marker) for marker in female_markers)
    male = any(has_marker(marker) for marker in male_markers)
    gender = "女" if female and not male else "男"
    # 明确 teen/adolescent 优先于 girl/boy 的泛年龄暗示。
    if any(has_marker(marker) for marker in teen_markers):
        age = "少年"
    elif any(has_marker(marker) for marker in child_markers):
        age = "儿童"
    elif any(has_marker(marker) for marker in old_markers):
        age = "老年"
    elif any(has_marker(marker) for marker in middle_markers):
        age = "中年"
    else:
        age = "青年"
    return gender, age


def is_gender_compatible(g1, g2):
    """
    判断性别是否兼容
    """
    s1 = str(g1).strip() if g1 else ""
    s2 = str(g2).strip() if g2 else ""

    if not s1 or not s2 or s1 in ['未知', 'Unknown', 'unknown', 'None', 'none'] or s2 in ['未知', 'Unknown', 'unknown',
                                                                                          'None', 'none']:
        return True

    def norm(g):
        male_keywords = [
            'Male', 'male', 'MALE',
            'M', 'm',
            'Man', 'man', 'Boy', 'boy',
            '男', '男性', '少男'
        ]

        female_keywords = [
            'Female', 'female', 'FEMALE',
            'F', 'f',
            'Woman', 'woman', 'Girl', 'girl',
            '女', '女性', '少女'
        ]

        if g in male_keywords: return 'M'
        if g in female_keywords: return 'F'

        g_lower = g.lower()
        if g_lower in ['male', 'm', 'man', 'boy']: return 'M'
        if g_lower in ['female', 'f', 'woman', 'girl']: return 'F'

        return 'U'

    n1, n2 = norm(s1), norm(s2)

    if n1 == 'U' or n2 == 'U': return True

    return n1 == n2


def _gender_code(gender):
    value = normalize_gender(gender)
    if value == "男":
        return "M"
    if value == "女":
        return "F"
    return "U"


def gender_match_tier(target_gender, voice_gender):
    """性别匹配层级：确定同性=0，任意一方未知=1，确定错配=2。"""
    target_code = _gender_code(target_gender)
    voice_code = _gender_code(voice_gender)
    if target_code == "U" or voice_code == "U":
        return 1
    return 0 if target_code == voice_code else 2


def _is_unknown_age(age_category):
    return _age_weight_or_none(age_category) is None


def age_group_mismatch(target_age, voice_age):
    """年龄组层级：同为童声/成人=0，音色年龄未知=1，确定跨界=2。"""
    if _is_unknown_age(target_age):
        return 0
    if _is_unknown_age(voice_age):
        return 1
    mismatch = (
        is_young_age_weight(get_age_weight(target_age))
        != is_young_age_weight(get_age_weight(voice_age))
    )
    return 2 if mismatch else 0


def exact_age_match_tier(target_age, voice_age):
    """已知年龄优先精确匹配；音色年龄未知次之；确定错龄最后。"""
    target_weight = _age_weight_or_none(target_age)
    if target_weight is None:
        return 0
    voice_weight = _age_weight_or_none(voice_age)
    if voice_weight is None:
        return 1
    return 0 if target_weight == voice_weight else 2


def _safe_important_rank(item):
    try:
        return int((item or {}).get("important_rank", 1))
    except (TypeError, ValueError):
        return 1


def _safe_first_appearance(item):
    try:
        value = int((item or {}).get("first_appearance"))
        return value if value >= 0 else 10 ** 9
    except (TypeError, ValueError):
        return 10 ** 9


def _prepare_voice_pool(voices):
    """去掉无效/重复 ID，并预计算年龄权重。候选最终用 voice_id 破同分。"""
    prepared = []
    seen = set()
    for voice in voices or []:
        if not isinstance(voice, dict):
            continue
        voice_id = str(voice.get("voice_id") or "").strip()
        if not voice_id or voice_id in seen:
            continue
        seen.add(voice_id)
        copy = voice.copy()
        copy["voice_id"] = voice_id
        copy["_age_weight"] = get_age_weight(copy.get("age_category"))
        prepared.append(copy)
    return prepared


def _character_match_count(character, voice):
    """完全相同的关键词只计一次，避免重复 traits 放大权重。"""
    traits = str((character or {}).get("traits") or "")
    keywords = {
        token.strip().lower()
        for token in re.split(r"[,，;；|/\n]+", traits)
        if len(token.strip()) > 1
    }
    occupation = str((character or {}).get("occupation") or "").strip().lower()
    if occupation and occupation != "未知身份":
        keywords.add(occupation)

    tags = voice.get("tags") or []
    if not isinstance(tags, (list, tuple, set)):
        tags = [tags]
    haystack = " ".join(
        [*(str(tag) for tag in tags), str(voice.get("description") or "")]
    ).lower()
    return sum(keyword in haystack for keyword in keywords)


def _age_sort_components(target_age, voice):
    voice_age = voice.get("age_category") or "未知"
    if _is_unknown_age(target_age):
        return 0, 0
    age_unknown_tier = int(_is_unknown_age(voice_age))
    age_diff = 0 if age_unknown_tier else abs(
        voice.get("_age_weight", 30) - get_age_weight(target_age)
    )
    return age_unknown_tier, age_diff


def _main_candidate_key(character, voice, ai_voice_map, target_lang, usage_counts):
    voice_id = voice["voice_id"]
    target_gender = character.get("gender") or "未知"
    target_age = character.get("age_category") or "未知"
    age_unknown_tier, age_diff = _age_sort_components(target_age, voice)
    usage_count = usage_counts.get(voice_id, 0)
    return (
        gender_match_tier(target_gender, voice.get("gender")),
        age_group_mismatch(target_age, voice.get("age_category")),
        exact_age_match_tier(target_age, voice.get("age_category")),
        unsupported_language_tier(voice, target_lang),
        int(usage_count > 0),
        usage_count,
        voice_language_tier(voice, target_lang),
        age_unknown_tier,
        age_diff,
        int(not check_tag_compatibility(
            character.get("traits", ""), voice.get("tags", [])
        )),
        int(voice_id != ai_voice_map.get(character["id"])),
        -_character_match_count(character, voice),
        voice_id.casefold(),
        voice_id,
    )


def _strict_voice_candidates(character, voice_pool):
    """返回不会发生性转或童声/成人跨界的候选池。

    排序可以允许年龄近似和音色复用，但最终兜底绝不能跨过性别或
    儿童/成人边界。未知角色属性按业务默认：男性、青年。
    """
    target_gender = normalize_gender(character.get("gender"))
    if target_gender == "未知":
        target_gender = "男"
    target_age_weight = get_age_weight(character.get("age_category"))
    target_is_young = is_young_age_weight(target_age_weight)
    candidates = [
        voice for voice in voice_pool
        if normalize_gender(voice.get("gender")) == target_gender
        and is_young_age_weight(get_age_weight(voice.get("age_category")))
        == target_is_young
    ]
    return candidates


def _match_unique_voices_for_rank_group(
        characters, available_pool, ai_voice_map, target_lang, usage_counts):
    """
    在同一 important_rank 内做最大唯一匹配。

    每个角色只在其「最优性别 + 最优童声边界 + 允许语言链」中参与匹配，
    回溯可以把早先角色调到次优音色，为后续角色让出其唯一可用音色。
    不会为了去重跨过性别、童声/成人或配置外语言边界。
    """
    unused_pool = [
        voice for voice in available_pool
        if usage_counts.get(voice["voice_id"], 0) == 0
    ]
    if not unused_pool:
        return {}

    candidates_by_role = {}
    characters_by_id = {character["id"]: character for character in characters}
    for character in characters:
        strict_candidates = _strict_voice_candidates(character, available_pool)
        if not strict_candidates:
            candidates_by_role[character["id"]] = []
            continue
        overall_ranked = sorted(
            strict_candidates,
            key=lambda voice: _main_candidate_key(
                character, voice, ai_voice_map, target_lang, usage_counts
            ),
        )
        best_hard_prefix = _main_candidate_key(
            character, overall_ranked[0], ai_voice_map, target_lang, usage_counts
        )[:4]
        ranked_unused = sorted(
            [
                voice for voice in unused_pool
                if voice["voice_id"] in {item["voice_id"] for item in strict_candidates}
            ],
            key=lambda voice: _main_candidate_key(
                character, voice, ai_voice_map, target_lang, usage_counts
            ),
        )
        if not ranked_unused:
            candidates_by_role[character["id"]] = []
            continue
        candidates_by_role[character["id"]] = [
            voice["voice_id"] for voice in ranked_unused
            if _main_candidate_key(
                character, voice, ai_voice_map, target_lang, usage_counts
            )[:4] == best_hard_prefix
        ]

    voice_to_role = {}
    role_to_voice = {}

    def assign(role_id, visited_voices):
        for voice_id in candidates_by_role.get(role_id, []):
            if voice_id in visited_voices:
                continue
            visited_voices.add(voice_id)
            current_role = voice_to_role.get(voice_id)
            if current_role is None or assign(current_role, visited_voices):
                voice_to_role[voice_id] = role_id
                role_to_voice[role_id] = voice_id
                if (current_role is not None
                        and current_role != role_id
                        and role_to_voice.get(current_role) == voice_id):
                    role_to_voice.pop(current_role, None)
                return True
        return False

    for character in characters:
        assign(character["id"], set())

    # 角色 ID 已由 validate_and_fix_casting 校验唯一；返回顺序交给调用方固定。
    return {
        role_id: role_to_voice[role_id]
        for role_id in characters_by_id
        if role_id in role_to_voice
    }


def check_tag_compatibility(char_traits, voice_tags):
    traits_str, tags_str = str(char_traits).lower(), str(voice_tags).lower()
    if any(k in traits_str for k in INCOMPATIBLE_TAG_RULES["villain_traits"]):
        for avoid in INCOMPATIBLE_TAG_RULES["villain_avoid_tags"]:
            if avoid in tags_str: return False
    if any(k in traits_str for k in INCOMPATIBLE_TAG_RULES["rough_traits"]):
        for avoid in INCOMPATIBLE_TAG_RULES["rough_avoid_tags"]:
            if avoid in tags_str: return False
    return True


def validate_and_fix_casting(ai_voice_map, all_voices, characters, target_lang="中文"):
    """
    主要角色按严格字典序选角。前一维一旦分出优劣，后续任何特征都不能翻转：
      性别 > 童声/成人边界 > 精确年龄 > 是否属于语言降级链 > 主角去重
      > 语言层 > 年龄 > 性格避雷
      > AI 建议 > 正向特征 > voice_id 稳定破同分。
    """
    if not characters: return {}
    if ai_voice_map is None: ai_voice_map = {}

    normalized_characters = []
    seen_role_ids = set()
    for character in characters:
        if not isinstance(character, dict):
            raise ValueError("主要角色资料必须是字典")
        role_id = str(character.get("id") or "").strip()
        if not role_id:
            raise ValueError("主要角色缺少有效 ID")
        if role_id in seen_role_ids:
            raise ValueError(f"主要角色 ID 重复：{role_id}")
        seen_role_ids.add(role_id)
        normalized = character.copy()
        normalized["id"] = role_id
        normalized_characters.append(normalized)
    characters = normalized_characters

    logger.info("🛡️ [Validation] 开始执行严格分层选角……")

    available_pool = _prepare_voice_pool(all_voices)
    if not available_pool:
        return {}

    final_map = {}
    used_vids_tracker = {}

    # 同 rank 按角色 ID 固定顺序，避免 DB JSON 键顺序影响谁先挑音色。
    sorted_chars = sorted(
        characters,
        key=lambda item: (
            -_safe_important_rank(item),
            str(item.get("id") or "").casefold(),
            str(item.get("id") or ""),
        ),
    )

    rank_groups = []
    for character in sorted_chars:
        rank = _safe_important_rank(character)
        if not rank_groups or rank_groups[-1][0] != rank:
            rank_groups.append((rank, []))
        rank_groups[-1][1].append(character)

    def commit(character, voice_id):
        role_id = character["id"]
        final_map[role_id] = voice_id
        used_vids_tracker[voice_id] = used_vids_tracker.get(voice_id, 0) + 1
        if used_vids_tracker[voice_id] > 1:
            logger.warning(
                f"   ⚠️ [Reuse] {role_id}(Rank:{_safe_important_rank(character)}) "
                f"资源枯竭，复用音色：{voice_id}"
            )
        else:
            logger.info(
                f"   ✅ [Assigned] {role_id}(Rank:{_safe_important_rank(character)}) "
                f"分配音色：{voice_id}"
            )

    for _rank, group in rank_groups:
        unique_assignments = _match_unique_voices_for_rank_group(
            group, available_pool, ai_voice_map, target_lang, used_vids_tracker
        )

        # 先提交同级最大唯一匹配，再处理确实资源不足的角色，
        # 避免未匹配角色提前抢走为其他角色保留的唯一音色。
        for character in group:
            voice_id = unique_assignments.get(character["id"])
            if voice_id:
                commit(character, voice_id)

        for character in group:
            if character["id"] in unique_assignments:
                continue
            strict_pool = _strict_voice_candidates(character, available_pool)
            if not strict_pool:
                raise RuntimeError(
                    f"{target_lang}/{character['id']} 没有符合性别和年龄边界的音色，"
                    "拒绝使用异性或错误年龄音色"
                )
            best_voice = min(
                strict_pool,
                key=lambda voice: _main_candidate_key(
                    character, voice, ai_voice_map, target_lang, used_vids_tracker
                ),
            )
            commit(character, best_voice["voice_id"])

    return final_map


def assign_npc_voices(npc_profiles, all_available_voices, main_used_vids, target_lang="中文"):
    """
    全量池智能NPC分配（阶梯式惩罚，完美解决资源枯竭导致的性转问题）。
    严格层级：性别 > 童声/成人边界 > 精确年龄 > 是否属于语言降级链
      > 主角音色保护 > 语言层 > 年龄 > NPC 间复用次数 > voice_id。
    因此复用次数再多也不会把 NPC 推向错语言/错性别音色。
    """
    npc_map = {}
    # 记录每个音色被 NPC 用了几次，用于均匀分摊而不是全堆在一个音色上。
    used_npc_voices = {}

    available_pool = _prepare_voice_pool(all_available_voices)
    if not available_pool:
        return {}

    # 让重要的 NPC 先选
    npc_items = list(npc_profiles.items())
    npc_items.sort(
        key=lambda item: (
            -_safe_important_rank(item[1]),
            _safe_first_appearance(item[1]),
            str(get_clean_char_id(item[0])),
            str(item[0]),
        )
    )

    for npc_name, npc_info in npc_items:
        clean_id = get_clean_char_id(npc_name)
        target_gender = npc_info.get('gender') or '未知'
        target_age = npc_info.get('age_category') or '未知'
        target_age_w = get_age_weight(target_age)

        strict_pool = _strict_voice_candidates(
            {"gender": target_gender, "age_category": target_age},
            available_pool,
        )
        if not strict_pool:
            raise RuntimeError(
                f"NPC {npc_name} 没有符合性别和年龄边界的音色，"
                "拒绝使用异性或错误年龄音色"
            )

        def npc_sort_key(v):
            voice_age = v.get('age_category') or '未知'
            if _is_unknown_age(target_age):
                age_unknown_tier = 0
                age_diff = 0
            else:
                age_unknown_tier = int(_is_unknown_age(voice_age))
                age_diff = (
                    0 if age_unknown_tier
                    else abs(v.get('_age_weight', 30) - target_age_w)
                )
            npc_usage = used_npc_voices.get(v['voice_id'], 0)
            vid = v['voice_id']
            return (
                gender_match_tier(target_gender, v.get('gender')),
                age_group_mismatch(target_age, voice_age),
                exact_age_match_tier(target_age, voice_age),
                unsupported_language_tier(v, target_lang),
                int(vid in main_used_vids),
                voice_language_tier(v, target_lang),
                age_unknown_tier,
                age_diff,
                npc_usage,
                vid.casefold(),
                vid,
            )

        best_voice = min(strict_pool, key=npc_sort_key)
        if best_voice:
            npc_map[clean_id] = best_voice['voice_id']
            used_npc_voices[best_voice['voice_id']] = (
                used_npc_voices.get(best_voice['voice_id'], 0) + 1
            )

            reused = "（复用）" if best_voice['voice_id'] in main_used_vids else ""
            cross_lang = "（跨语言）" if not is_voice_language_match(best_voice, target_lang) else ""
            logger.info(
                f"   🎭 [NPC] {npc_name} 分配：{best_voice['voice_id']} "
                f"(性别：{target_gender}){reused}{cross_lang}"
            )

    return npc_map


def merge_cast_maps(main_map, npc_map):
    """合并选角结果；任何 ID 冲突都由主角映射覆盖 NPC 映射。"""
    normalized_main = dict(main_map or {})
    normalized_npc = dict(npc_map or {})
    overlapping_ids = sorted(set(normalized_main) & set(normalized_npc))
    if overlapping_ids:
        logger.warning(
            "⚠️ NPC 与主要角色 ID 冲突，保留主要角色音色："
            + ", ".join(overlapping_ids)
        )
    combined = normalized_npc
    combined.update(normalized_main)
    return combined


def detect_story_language(plot_data):
    """
    根据剧情内容判断剧本主要语言
    """
    if not plot_data:
        return "中文"

    contents = []
    for chapter_key, beats in plot_data.items():
        if isinstance(beats, dict):
            beats = beats.get("script_timeline")
        if not isinstance(beats, list): continue
        for beat in beats:
            content = str(beat.get("content", "")).strip()
            if content:
                contents.append(content)
            if len(contents) >= 3:
                break
        if len(contents) >= 3:
            break

    if not contents:
        return "中文"

    japanese_char_pattern = re.compile(r'[\u3040-\u30ff]')
    chinese_char_pattern = re.compile(r'[\u4e00-\u9fff]')
    for txt in contents:
        if japanese_char_pattern.search(txt):
            return "日语"
        if chinese_char_pattern.search(txt):
            return "中文"

    return "英语"


def _resolve_lang_configs(locales=None):
    requested = tuple(locales or MINIMAX_LOCALES)
    configs = [
        (lang_key, target_lang)
        for lang_key, target_lang in AUDIO_LANG_CONFIGS
        if lang_key in requested
    ]
    unsupported = sorted(set(requested) - {lang_key for lang_key, _ in configs})
    if unsupported:
        raise ValueError(f"MiniMax Step 3 不支持的语言：{', '.join(unsupported)}")
    if not configs:
        raise ValueError("MiniMax Step 3 至少需要指定一个输出语言")
    return configs


def run_auto_casting(
    target_task_id=None,
    *,
    locales=None,
    task_output_dir=None,
    output_filename="minimax_voice_mappings.json",
    persist_db=True,
    state_key="minimax_casting",
):
    """为指定语言运行 MiniMax 选角。

    默认参数保持原日语主链行为。英语灾备通过 ``locales=("en-US",)``
    和独立 ``task_output_dir`` 复用同一实现，并禁止写入日语使用的
    ``voice_mappings`` 数据库记录。
    """
    logger.info("🎬 开始自动化选角流程 (DB Mode)……")

    lang_configs = _resolve_lang_configs(locales)

    task_id, char_json_data = fetch_character_data(target_task_id)
    effective_task_id = target_task_id or task_id
    _, plot_json_data = fetch_plot_data(effective_task_id)

    global _NAME_CONVERSION
    _NAME_CONVERSION = fetch_name_mappings(effective_task_id)

    if not char_json_data or not plot_json_data:
        raise RuntimeError("Step 3 数据缺失：characters 或 plots 为空")

    if _usage_tracker and task_id:
        try:
            _usage_tracker.set_context(task_id, "audio_producer")
        except Exception as exc:
            logger.warning(f"⚠️ API 用量上下文设置失败，继续 Step 3：{exc}")

    if not os.path.exists(VOICES_FILE_PATH):
        raise RuntimeError(f"Step 3 未找到音色库：{VOICES_FILE_PATH}")

    all_voices = load_voice_library(VOICES_FILE_PATH)
    if not all_voices:
        raise RuntimeError(f"Step 3 音色库无可用音色：{VOICES_FILE_PATH}")
    reserved_ids = set()
    main_characters = parse_character_profiles(char_json_data)
    excluded_main_roles = [
        character["id"]
        for character in main_characters
        if is_non_voiced_role(character["id"])
    ]
    main_characters = [
        character
        for character in main_characters
        if not is_non_voiced_role(character["id"])
    ]
    role_sources = {}
    for character in main_characters:
        role_sources.setdefault(character["id"], []).append(character["original_name"])
    duplicate_role_ids = {
        role_id: names for role_id, names in role_sources.items() if len(names) > 1
    }
    if duplicate_role_ids:
        details = "；".join(
            f"{role_id} <- {', '.join(names)}"
            for role_id, names in sorted(duplicate_role_ids.items())
        )
        raise RuntimeError(f"Step 3 主要角色 ID 冲突，停止选角：{details}")
    if excluded_main_roles:
        logger.info(
            "🔇 Step 3 跳过无需配音角色："
            + ", ".join(excluded_main_roles)
        )
    main_char_ids = {c["id"] for c in main_characters}

    _pool_cache = {}

    def build_lang_pool(t_lang):
        # 同语言音色排前，其他语言始终追加作为降级备选；
        # 具体的语言/性别/年龄取舍交给下游打分函数处理（语言不符会被扣分，但优于性别错）。
        if t_lang not in _pool_cache:
            pool = [v for v in all_voices if v["voice_id"] not in reserved_ids]
            _pool_cache[t_lang] = sorted(
                pool,
                key=lambda voice: (
                    voice_language_tier(voice, t_lang),
                    str(voice["voice_id"]).casefold(),
                    str(voice["voice_id"]),
                ),
            )
        return _pool_cache[t_lang]

    # 剧本源语言只用于日志。音色必须按目标输出语言分别选择，不能先选中文音色再修补。
    detected_lang = detect_story_language(plot_json_data)
    logger.info(f"🌐 自动检测到剧本语言为：{detected_lang}")

    TOP_TIERS = 2
    distinct_ranks = sorted(
        {c.get("important_rank", 1) for c in main_characters}, reverse=True
    )
    top_ranks = set(distinct_ranks[:TOP_TIERS])
    core_chars = [
        c for c in main_characters if c.get("important_rank", 1) in top_ranks
    ]
    npc_names = extract_npc_list(plot_json_data, main_char_ids)
    required_role_ids = main_char_ids | {get_clean_char_id(name) for name in npc_names}
    task_output_dir = task_output_dir or os.path.join(
        OUTPUT_BASE_DIR, str(task_id)
    )
    output_file = os.path.join(task_output_dir, output_filename)
    step_digest = stable_digest(
        STEP_CACHE_VERSION,
        char_json_data,
        plot_json_data,
        _NAME_CONVERSION,
        file_digest(VOICES_FILE_PATH),
        lang_configs,
        persist_db,
        state_key,
        MODEL_NAME,
        MAIN_CAST_BATCH_SIZE,
        MAIN_CAST_CANDIDATES_PER_ROLE,
        file_digest(__file__),
    )
    state = ResumeState(task_output_dir)
    if state.step_matches(state_key, step_digest) and valid_json(
        output_file,
        lambda payload: isinstance(payload, dict) and all(
            isinstance(payload.get(lang_key), dict)
            and required_role_ids <= set(payload[lang_key])
            and all(
                voice_id in {voice["voice_id"] for voice in all_voices}
                for voice_id in payload[lang_key].values()
            )
            for lang_key, _ in lang_configs
        ),
    ):
        with open(output_file, "r", encoding="utf-8") as file_obj:
            cached_mapping = json.load(file_obj)
        logger.info("♻️ Step 3 音色映射输入未变且产物完整，跳过模型选角")
        return cached_mapping

    ai_voice_maps = {}
    for lang_key, target_lang in lang_configs:
        if not core_chars:
            ai_voice_maps[lang_key] = {}
            continue
        logger.info(
            f"🧠 [{lang_key}] 核心卡司：{len(core_chars)} 个角色提交 AI 决策"
        )
        ai_voice_maps[lang_key] = get_ai_casting(
            core_chars, build_lang_pool(target_lang), target_lang
        )

    # NPC 画像只按名称中的明确称谓确定；名称看不出来时使用默认男性青年。
    logger.info(f"📋 发现 {len(npc_names)} 个 NPC")
    npc_profiles = {}
    for name in npc_names:
        normalized_gender, normalized_age = infer_npc_profile_from_name(name)
        npc_profiles[name] = {
            "gender": normalized_gender,
            "age_category": normalized_age,
            # NPC 重要度不由该 AI 推断，防止额外字段擅自改变挑选顺序。
            "important_rank": 1,
            "first_appearance": len(npc_profiles),
        }

    # 日英两条轨道相互独立；跨语言允许复用同一 voice_id。
    combined_map = {}
    for lang_key, target_lang in lang_configs:
        logger.info(f"🔤 [{lang_key}] 主角选角……")
        lang_pool = build_lang_pool(target_lang)
        main_map = validate_and_fix_casting(
            ai_voice_maps.get(lang_key), lang_pool, main_characters, target_lang
        )
        logger.info(f"🔤 [{lang_key}] NPC 选角……")
        npc_map = assign_npc_voices(
            npc_profiles, lang_pool, set(main_map.values()), target_lang
        )
        # 防御性地让主角最后覆盖：即使未来 NPC 提取规则变化导致 ID
        # 意外碰撞，也不能改写已经锁定的主要角色音色。
        combined_map[lang_key] = merge_cast_maps(main_map, npc_map)

    available_voice_ids = {voice["voice_id"] for voice in all_voices}
    validation_errors = []
    for lang_key, _ in lang_configs:
        lang_map = combined_map.get(lang_key) or {}
        missing_roles = sorted(required_role_ids - set(lang_map))
        invalid_roles = sorted(
            role_id for role_id, voice_id in lang_map.items()
            if voice_id not in available_voice_ids
        )
        if missing_roles:
            validation_errors.append(
                f"{lang_key} 缺少角色映射: {', '.join(missing_roles)}"
            )
        if invalid_roles:
            validation_errors.append(
                f"{lang_key} 使用了无效音色的角色: {', '.join(invalid_roles)}"
            )

    if validation_errors:
        raise RuntimeError("Step 3 音色映射校验失败；" + "；".join(validation_errors))

    os.makedirs(task_output_dir, exist_ok=True)
    local_saved = False
    try:
        atomic_write_json(output_file, combined_map)
        local_saved = True
        logger.info(f"💾 [Local] 映射表已保存：{output_file}")
    except Exception as e:
        logger.error(f"⚠️ [Local] 本地保存失败：{e}")

    db_saved = save_mapping_to_db(task_id, combined_map) if persist_db else None
    if persist_db and not db_saved and local_saved:
        logger.warning("⚠️ 数据库保存失败，但本地文件已写入，流程继续。")
    elif persist_db and not db_saved:
        raise RuntimeError("Step 3 音色映射保存失败：数据库和本地文件均未写入")
    elif not persist_db:
        logger.info("🧯 灾备音色映射仅写独立目录，不覆盖日语 voice_mappings")

    state.mark_step(
        state_key, step_digest, status="success",
        artifacts=[output_file],
    )

    logger.info(f"📊 选角完成，共 {len(lang_configs)} 套语言映射")
    return combined_map


if __name__ == "__main__":
    TARGET_TASK_ID = ""
    result_map = run_auto_casting(TARGET_TASK_ID)
    if _usage_tracker:
        try:
            from common.usage_tracker import init_usage_table

            init_usage_table()
        except Exception:
            pass
        _usage_tracker.flush()
