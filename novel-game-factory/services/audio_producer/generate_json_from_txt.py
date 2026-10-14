# coding=utf-8
import sys
import os
import re
import time
import json
import threading
import unicodedata
from openai import OpenAI
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from sqlalchemy import text
from logger_config import setup_logger

# 1. 动态添加项目根目录到 sys.path，对齐基础环境配置
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# 【核心配置】引入 DATABASE_URL 和 OUTPUTS_PATH
from common.config import DATABASE_URL, OUTPUTS_PATH
from common.llm_fallback import ModelFallback
try:
    from common.audio_db import get_business_engine
except ImportError:  # 离线单测不访问数据库，仅需模块可导入
    from common.platform_db import platform_engine as _business_engine

    def get_business_engine():
        return _business_engine
from common.audio_languages import (
    AUDIO_OUTPUT_LOCALES,
    AUDIO_SUBTITLE_DIRS,
    KNOWN_AUDIO_LOCALES,
)
from resume_state import (
    ResumeState,
    atomic_write_json,
    file_digest,
    stable_digest,
    valid_json,
    valid_wav,
)

# 兼容 tracker 导入
try:
    from common.usage_tracker import tracker as _usage_tracker
except ImportError:
    _usage_tracker = None

# 初始化日志
logger = setup_logger("JSON_Gen", "json_gen.log")

load_dotenv()

# BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
# BASE_URL = "https://ai.farmmx.com/v1"
BASE_URL = os.getenv("PLOT_BASE_URL", "http://sefutech-api.kube-test.svc:3000/v1")
# API Key & Model 配置（DashScope OpenAI 兼容模式）
client = OpenAI(
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url=BASE_URL,
    timeout=max(
        60.0, float(os.getenv("AUDIO_JSON_LLM_TIMEOUT_SECONDS", "600"))
    ),
    max_retries=0,
)
AGENT_MODEL = "qwen3.7-max"
# 主模型偶发不稳定时自动改用备选模型重发同一请求（同中转站、同调用参数）。
_model_fallback = ModelFallback(AGENT_MODEL, name="Step4Json", logger=logger)

# 路径配置
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_BASE_DIR = OUTPUTS_PATH
JSON_OUTPUT_BASE_DIR = OUTPUTS_PATH
VOICE_MAPPING_DIR = OUTPUTS_PATH

# 并发配置
MAX_WORKERS = 6
STEP_CACHE_VERSION = "minimax-json-v2"

# 全局变量
CURRENT_VOICE_MAP = {}

# MiniMax 链只负责日语；英语走 ElevenLabs 中转链的 dialogue-json。
MINIMAX_LOCALES = tuple(
    item.strip()
    for item in os.getenv("MINIMAX_LOCALES", "ja-JP").split(",")
    if item.strip()
)
LANG_SUBTITLE_DIRS = frozenset(
    name for name in AUDIO_SUBTITLE_DIRS
    if name.replace("-subtitle", "") in MINIMAX_LOCALES
)
NON_VOICED_ROLE_IDS = {"xitong", "system", "系统"}


def is_non_voiced_role(role_id):
    return str(role_id or "").strip().lower() in NON_VOICED_ROLE_IDS


def starts_with_non_voiced_role(raw_text):
    prefix = str(raw_text or "").strip().split("：", 1)[0].split(":", 1)[0]
    role_prefix = prefix.split("_", 1)[0]
    return is_non_voiced_role(role_prefix)

# ================= 情感与样式映射 =================

MINIMAX_EMOTION_MAP = {
    "sad": "sad",
    "happy": "happy",
    "excited": "happy",
    "angry": "angry",
    "fearful": "fearful",
    "surprised": "surprised",
    "disgusted": "disgusted",
    "neutral": "calm",
    "calm": "calm",
    "tired": "whisper",
    "default": "calm"
}

EMOTION_STYLES = {
    "sad": {"speed": 1.03, "vol": 1.95},
    "happy": {"speed": 1.02, "vol": 2.3},
    "angry": {"speed": 1.05, "vol": 2.6},
    "fearful": {"speed": 1.0, "vol": 2.1},
    "calm": {"speed": 1.0, "vol": 2.1},
    "disgusted": {"speed": 1.0, "vol": 2.05},
    "surprised": {"speed": 1.01, "vol": 2.3},
    "neutral": {"speed": 1.0, "vol": 2.1},
    "default": {"speed": 1.0, "vol": 2.1}
}

# ================= 核心工具函数 =================

print_lock = threading.Lock()


def safe_print(msg):
    with print_lock:
        logger.info(msg)


def parse_line_info(text):
    """
    解析输入格式。
    返回: is_valid_format, full_prefix, role, emotion, content
    只有严格匹配类似 Name_1-2-3_emotion 或 Name_1_emotion 的格式，is_valid_format 才为 True。
    """
    text = text.strip()

    # 彻底移除开头的标签及其后的空格
    clean_text = re.sub(r'^\s*', '', text)

    if "：" in clean_text:
        parts = clean_text.split("：", 1)
        full_prefix = parts[0].strip()
        content = parts[1].strip()
    else:
        # 兜底：如果没有冒号，尝试直接匹配
        full_prefix = clean_text
        content = clean_text

    # 匹配角色和情感，支持 Name_1_emotion 和 Name_1-2-3_emotion 两种格式
    match = re.match(r"^(.+?)_[\d][\d\-]*_(.+?)$", full_prefix, re.IGNORECASE)

    is_valid_format = False

    if match:
        role = match.group(1).lower()
        emotion = match.group(2).lower()
        is_valid_format = True
    else:
        role = full_prefix.lower()
        emotion = "neutral"

    return is_valid_format, full_prefix, role, emotion, content


PLACEHOLDER_WRAPPERS = (("[", "]"), ("【", "】"), ("(", ")"), ("（", "）"))
# 系统播报公告也整句包在【】/[] 里，但内部是完整句子。占位是"整句就是一个标记"，
# 播报是"标记里包着一句话"，实测两者字数不重叠（占位 ≤14 字符，播报 ≥21）。
PLACEHOLDER_MAX_WORDS = 3
PLACEHOLDER_MAX_CJK = 6
SENTENCE_PUNCTUATION = frozenset("。．！？!?；;：:…、，,")
# Eleven v3 的情绪标签由 step 4 拼在台词前，形如 "[cheerfully] [疑惑]"。判据要在
# 拼装前后给出同样结论，否则运行时防线看到的文本永远带标签，判不出占位。这里只认
# 固定的情绪词，否则 "[cheerfully] [Burns Incense]" 里的占位标记会被当成标签剥掉。
EMOTION_TAG_WORDS = frozenset({
    "sad", "cheerfully", "excited", "angry", "fearful",
    "surprised", "disgusted", "tired", "neutral", "calm", "happy",
})
LEADING_EMOTION_TAG_RE = re.compile(
    r"^(?:\s*\[(" + "|".join(sorted(EMOTION_TAG_WORDS)) + r")\]\s*)+",
    re.IGNORECASE,
)
# 连续图片/UI 标记也属于纯占位，例如 "[Image][Image][Image]"。不能用
# “任意方括号文本”判断，否则 [Link Share: a complete sentence!] 这类需要播报
# 的正文会被误删。
REPEATED_MEDIA_PLACEHOLDER_RE = re.compile(
    r"^\s*(?:\[(?:image|picture|photo|screenshot|icon|图片|图像|截图|照片)\]\s*)+$",
    re.IGNORECASE,
)
# 台词的成对引号不影响发音，但会挡住"整句就是一个标记"的判定：「[Image]」既不
# 以 [ 开头也不以 ] 结尾，占位判据看不出来。ElevenLabs 链路合成前会剥引号
#（_build_spoken_text），MiniMax 链路则连引号一起念，两边都不该为占位配音。
DIALOGUE_QUOTE_PAIRS = (("「", "」"), ("『", "』"), ("“", "”"), ('"', '"'))


def strip_dialogue_quotes(content):
    text_value = str(content or "").strip()
    for opening, closing in DIALOGUE_QUOTE_PAIRS:
        if text_value.startswith(opening) and text_value.endswith(closing):
            return text_value[len(opening):-len(closing)].strip()
    return text_value


def is_non_verbal_placeholder(content):
    """判断台词是不是纯非语言占位，例如游戏聊天里的 [Anxious]、(Screenshot)、【疑惑】。

    这类内容整句就是一个界面标记，没有可念的台词：送进 ElevenLabs 会被当成
    audio tag 或非语言指示，整批请求直接 400；送进 MiniMax 会拿回空音频。
    两条链都因此连坐同批次的正常台词，所以在生成 JSON 阶段就不为它们配音。

    系统播报公告（【You can use positive-review points to redeem skills.】）同样
    整句带包裹，但那是要念出来的，所以只在内容极短且没有句末标点时才判占位。
    """
    text_value = strip_dialogue_quotes(content)
    if REPEATED_MEDIA_PLACEHOLDER_RE.fullmatch(text_value):
        return True
    stripped = LEADING_EMOTION_TAG_RE.sub("", text_value).strip()
    if stripped and stripped != text_value:
        # 只剩情绪标签时（"[cheerfully]"）标签本身就是要表达的内容，交给下面的
        # 整句包裹分支判定，不在这里当成空文本。
        # 情绪标签可能在引号外（'[sad] 「[疑惑]」'），剥完标签要再剥一层引号，
        # 否则判定结果会依赖两者的拼装顺序。
        text_value = strip_dialogue_quotes(stripped)
    for opening, closing in PLACEHOLDER_WRAPPERS:
        if not (text_value.startswith(opening) and text_value.endswith(closing)):
            continue
        inner = text_value[len(opening):-len(closing)].strip()
        if not inner or inner.count(opening) or inner.count(closing):
            # 空标记同样没得念；嵌套包裹说明是含子句的播报文本，不算占位。
            return not inner
        if SENTENCE_PUNCTUATION & set(inner):
            return False
        words = re.findall(r"[A-Za-z][A-Za-z'’-]*", inner)
        cjk = re.findall(r"[぀-ヿ㐀-䶿一-鿿]", inner)
        if words and len(words) > PLACEHOLDER_MAX_WORDS:
            return False
        if len(cjk) > PLACEHOLDER_MAX_CJK:
            return False
        return True
    return False


def has_pronounceable_content(content):
    """台词送进 TTS 后还有没有能发出声音的内容。

    「……」这类纯停顿台词翻成英文是 '...'，和非语言占位一样只会拿回空音频。

    情绪标签必须先剥掉再看有没有可念字符：step 4 会把 "[surprised]" 拼在台词
    前，标签里的字母会让 '...' 这种纯停顿台词被误判成可发音，一路送到 Forced
    Alignment 才因"没有有效发音字符"整批失败。
    """
    text_value = str(content or "")
    if is_non_verbal_placeholder(text_value):
        return False
    spoken = LEADING_EMOTION_TAG_RE.sub("", text_value.strip()).strip()
    return any(
        unicodedata.category(character)[0] in {"L", "M", "N"}
        for character in spoken
    )


def strip_option_prefix(content):
    """
    分支选项的首条 beat（beat_id 形如 86-A1 / 86-B1）内容被 extract_plots_to_db
    拼成「选项文案||实际台词」，选项文案只用于游戏里的按钮，配音只念 || 之后的部分。
    切分会带走成对引号的前半个，故补回开引号。
    """
    if "||" not in content:
        return content
    content = content.split("||", 1)[1].strip()
    if content.endswith("」") and not content.startswith("「"):
        content = "「" + content
    return content


# ================= 专职翻译数字的 AI 代理 =================
def call_number_translation_agent(content):
    system_prompt = """
你是一个专业的英文文本预处理专家。

【核心任务】
仔细阅读上下文，将文本中的所有阿拉伯数字准确翻译为符合英语语境的英文单词拼写。
- 常规数字：如 "37" 转换为 "thirty-seven"
- 年份：如 "1973" 转换为 "nineteen seventy-three"
- 百分数：如 "260%" 转换为 "two hundred and sixty percent"
- 序数词：如 "1st" 转换为 "first"

【严格纪律】
1. 绝对保留原文的其他所有单词、拼写、大小写和标点符号，不做任何润色。
2. 不要加任何多余的解释，直接返回处理后的原文。
3. 严禁添加任何 XML、HTML 或 SSML 标签。 
"""
    try:
        response, used_model = _model_fallback.chat(
            client,
            messages=[
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': content},
            ],
        )
        if _usage_tracker and getattr(response, 'usage', None):
            try:
                _usage_tracker.add_llm_usage(
                    response.usage, used_model, provider="dashscope"
                )
            except Exception as exc:
                logger.warning(f"⚠️ API 用量记录失败，不影响数字翻译：{exc}")
        result = response.choices[0].message.content
        result = result.replace("```xml", "").replace("```", "").strip()
        return result
    except Exception as e:
        logger.warning(f"⚠️ 数字翻译代理请求异常: {e}")
        return content


def build_minimax_json(content, role, emotion_label, voice_map):
    """构建 MiniMax 接口所需的 JSON 结构"""
    voice_id = voice_map.get(role, voice_map.get("default", "male-qn-qingse"))
    style = EMOTION_STYLES.get(emotion_label, EMOTION_STYLES["default"])
    minimax_emotion = MINIMAX_EMOTION_MAP.get(emotion_label, MINIMAX_EMOTION_MAP["default"])

    data = {
        "text": content,
        "voice_id": voice_id,
        "speed": style["speed"],
        "vol": style["vol"],
        "emotion": minimax_emotion
    }
    return data


# ================= 数据库交互逻辑 =================

def fetch_voice_mapping_from_db(task_id):
    """
    优先从 PostgreSQL 数据库中读取指定 task_id 的音色映射表
    """
    if not task_id or not DATABASE_URL:
        return None

    try:
        with get_business_engine().connect() as conn:
            sql_data = text("SELECT data FROM voice_mappings WHERE task_id = :tid")
            result = conn.execute(sql_data, {"tid": str(task_id)}).fetchone()

            if result and result[0]:
                logger.info(f"✅ [DB] 成功从数据库 voice_mappings 表读取映射 (Task: {task_id})")
                return result[0]
            else:
                logger.warning(f"⚠️ [DB] 数据库中未找到 task_id='{task_id}' 的记录")
                return None
    except Exception as e:
        logger.error(f"❌ [DB] 从数据库读取音色表失败: {e}")
        return None


# ================= 并发执行逻辑 =================

def process_one_file(file_info):
    (
        index, total, src_path, task_id, node_id, lang_key, voice_map,
        *runtime_options,
    ) = file_info
    output_task_root = (
        runtime_options[0]
        if runtime_options
        else os.path.join(JSON_OUTPUT_BASE_DIR, str(task_id))
    )
    json_state_key = runtime_options[1] if len(runtime_options) > 1 else "minimax_json"
    audio_state_key = runtime_options[2] if len(runtime_options) > 2 else "minimax_audio"
    file_name = os.path.basename(src_path)

    target_file_name = os.path.splitext(file_name)[0] + ".json"
    target_dir = os.path.join(output_task_root, node_id, f"{lang_key}-json")
    dst_path = os.path.join(target_dir, target_file_name)
    stale_audio_path = os.path.join(
        output_task_root, node_id, "audios", f"{lang_key}-audio",
        f"{os.path.splitext(file_name)[0]}.wav",
    )

    def remove_stale_outputs():
        # 字幕为空、变成旁白/系统文本或格式不再需要配音时，旧 JSON/WAV 已经
        # 不对应当前输入，必须一起删除，不能让断点状态把它误判为有效资产。
        for stale_path in (dst_path, stale_audio_path):
            try:
                os.remove(stale_path)
            except OSError:
                pass

    try:
        with open(src_path, 'r', encoding='utf-8') as f:
            raw_text = f.read().strip()

        if not raw_text:
            remove_stale_outputs()
            return "skipped", f"⚠️ [{index}/{total}] 跳过空文件: {node_id}/{file_name}"

        lowered_text = raw_text.lower()
        if lowered_text.startswith("pangbai") or lowered_text.startswith("narrator"):
            remove_stale_outputs()
            return "skipped", f"⏭️ [{index}/{total}] 跳过旁白: {node_id}/{file_name}"
        if starts_with_non_voiced_role(raw_text):
            remove_stale_outputs()
            return "skipped", f"⏭️ [{index}/{total}] 跳过系统文本: {node_id}/{file_name}"

        is_valid_format, full_prefix, role, emotion_label, content = parse_line_info(raw_text)

        if not is_valid_format:
            remove_stale_outputs()
            return "skipped", f"⏭️ [{index}/{total}] 跳过内心独白/非配音文本: {node_id}/{file_name}"

        if is_non_voiced_role(role):
            remove_stale_outputs()
            return "skipped", f"⏭️ [{index}/{total}] 跳过系统文本: {node_id}/{file_name}"

        content = strip_option_prefix(content)

        if not has_pronounceable_content(content):
            remove_stale_outputs()
            return "skipped", (
                f"⏭️ [{index}/{total}] 跳过无可发音内容: {node_id}/{file_name} "
                f"-> {json.dumps(content, ensure_ascii=False)}"
            )

        item_key = f"{node_id}/{lang_key}/{os.path.splitext(file_name)[0]}"
        selected_voice = voice_map.get(
            role, voice_map.get("default", "male-qn-qingse")
        )
        input_digest = stable_digest(
            STEP_CACHE_VERSION,
            raw_text,
            lang_key,
            role,
            emotion_label,
            selected_voice,
            AGENT_MODEL,
            file_digest(__file__),
            file_digest(
                os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    "generate_audio_from_speech.py",
                )
            ),
            "speech-2.8-hd",
        )
        state = ResumeState(output_task_root)
        audio_path = os.path.join(
            output_task_root, node_id, "audios", f"{lang_key}-audio",
            f"{os.path.splitext(file_name)[0]}.wav",
        )
        if (
            state.item_matches(audio_state_key, item_key, input_digest)
            and valid_wav(audio_path)
        ):
            return (
                "skipped",
                f"⏭️ [{index}/{total}] 音频已验收且输入未变: "
                f"{node_id}/{os.path.basename(audio_path)}",
            )

        if re.search(r'\d', content):
            if lang_key == "en-US":
                safe_print(f"🔄 [{index}/{total}] 英文环境检测到数字，触发翻译代理: {node_id}/{file_name}")
                content = call_number_translation_agent(content)
                content = content.replace('%', ' percent')
            else:
                safe_print(f"ℹ️ [{index}/{total}] 非英文环境包含数字，跳过翻译代理: {node_id}/{file_name}")

        final_json_data = build_minimax_json(content, role, emotion_label, voice_map)
        final_json_data["_resume_input_digest"] = input_digest
        final_json_data["_resume_item_key"] = item_key

        if not os.path.exists(target_dir):
            os.makedirs(target_dir, exist_ok=True)

        if valid_json(dst_path, lambda payload: payload == final_json_data):
            state.mark_item(
                json_state_key, item_key, input_digest,
                status="success", artifacts=[dst_path],
            )
            return "skipped", f"⏭️ [{index}/{total}] JSON 已完整且输入未变: {node_id}/{target_file_name}"

        atomic_write_json(dst_path, final_json_data)
        state.mark_item(
            json_state_key, item_key, input_digest,
            status="success", artifacts=[dst_path],
        )

        return "success", f"✅ [{index}/{total}] 完成: {node_id}/{target_file_name} (Role: {role} -> Voice: {final_json_data['voice_id']})"

    except Exception as e:
        return "failed", f"❌ [{index}/{total}] 失败: {node_id}/{file_name} -> {e}"


# ================= 主程序 =================

def main(
    target_task_id,
    memory_voice_map=None,
    *,
    locales=None,
    input_task_root=None,
    output_task_root=None,
    mapping_filename="minimax_voice_mappings.json",
    use_db_mapping=True,
    state_key="minimax_json",
    audio_state_key="minimax_audio",
):
    """生成指定语言的 MiniMax 请求 JSON。

    默认参数兼容日语主链；英语灾备可把输出定向到独立暂存根目录，
    从而不会触碰正式 ``en-US-audio``。
    """
    if _usage_tracker and target_task_id:
        try:
            _usage_tracker.set_context(target_task_id, "audio_producer")
        except Exception as exc:
            logger.warning(f"⚠️ API 用量上下文设置失败，继续 Step 4：{exc}")

    _default_sub = {"pangbai": "male-qn-qingse", "narrator": "male-qn-qingse", "default": "male-qn-qingse"}

    loaded_map = {}
    requested_locales = tuple(locales or MINIMAX_LOCALES)
    unsupported = sorted(set(requested_locales) - set(AUDIO_OUTPUT_LOCALES))
    if unsupported:
        raise ValueError(f"MiniMax Step 4 不支持的语言：{', '.join(unsupported)}")
    input_task_root = input_task_root or os.path.join(
        OUTPUT_BASE_DIR, str(target_task_id)
    )
    output_task_root = output_task_root or os.path.join(
        JSON_OUTPUT_BASE_DIR, str(target_task_id)
    )
    language_subtitle_dirs = {
        f"{locale}-subtitle" for locale in requested_locales
    }
    file_map_path = os.path.join(output_task_root, mapping_filename)
    if memory_voice_map:
        logger.info("⚡ [JSON_Gen] 使用内存传递的音色映射表")
        loaded_map = memory_voice_map
    else:
        db_data = fetch_voice_mapping_from_db(target_task_id) if use_db_mapping else None
        if db_data:
            loaded_map = json.loads(db_data) if isinstance(db_data, str) else db_data
        if not loaded_map and os.path.exists(file_map_path):
            try:
                with open(file_map_path, 'r', encoding='utf-8') as f:
                    loaded_map = json.load(f)
                logger.info(f"📂 [Local] 成功从本地加载音色表: {file_map_path}")
            except Exception as e:
                logger.error(f"⚠️ 读取本地音色表失败: {e}")
        if not loaded_map:
            logger.warning("⚠️ [JSON_Gen] 内存、数据库、本地均未找到映射数据，将使用默认兜底。")

    lang_keys = set(requested_locales)
    is_multilang = bool(loaded_map and set(KNOWN_AUDIO_LOCALES) & set(loaded_map.keys()))

    def build_voice_map(lang_key):
        vm = {k.lower(): v for k, v in _default_sub.items()}
        if is_multilang and lang_key not in loaded_map:
            logger.warning(
                f"⚠️ Step 4 音色映射缺少 {lang_key}，"
                "本语言使用默认音色继续"
            )
            return vm
        sub = loaded_map.get(lang_key, loaded_map) if is_multilang else loaded_map
        if sub:
            vm.update({k.lower(): v for k, v in sub.items()})
        return vm

    lang_voice_maps = {lang: build_voice_map(lang) for lang in lang_keys}

    scan_root = input_task_root
    if not os.path.exists(scan_root):
        logger.warning(f"⚠️ Step 4 找不到输入目录，跳过：{scan_root}")
        return {"success": 0, "skipped": 0, "failed": 0}

    all_tasks = []
    logger.info(f"🔍 正在扫描字幕文件 (Task: {target_task_id})...")

    for root, dirs, files in os.walk(scan_root):
        dir_name = os.path.basename(root)
        if dir_name not in language_subtitle_dirs:
            continue
        lang_key = dir_name.replace("-subtitle", "")
        try:
            node_id = Path(root).parents[1].name
        except IndexError:
            node_id = "UnknownNode"
        voice_map = lang_voice_maps.get(lang_key)
        if voice_map is None:
            logger.warning(f"⚠️ Step 4 跳过不支持的字幕语言: {lang_key}")
            continue
        for file in files:
            if file.endswith(".txt"):
                all_tasks.append((os.path.join(root, file), target_task_id, node_id, lang_key, voice_map))

    total_files = len(all_tasks)
    if total_files == 0:
        logger.warning(f"⚠️ Step 4 未找到字幕文件，跳过：{scan_root}")
        return {"success": 0, "skipped": 0, "failed": 0}

    source_json_paths = {
        os.path.abspath(os.path.join(
            output_task_root, node_id,
            f"{lang_key}-json", f"{Path(path).stem}.json",
        ))
        for path, _task_id, node_id, lang_key, _voice_map in all_tasks
    }
    orphaned = 0
    for root, _, files in os.walk(output_task_root):
        if os.path.basename(root) not in {f"{lang}-json" for lang in lang_keys}:
            continue
        for filename in files:
            if not filename.endswith(".json"):
                continue
            json_path = os.path.abspath(os.path.join(root, filename))
            if json_path not in source_json_paths:
                os.remove(json_path)
                orphaned += 1
    if orphaned:
        logger.info(f"🧹 已删除 {orphaned} 个无对应字幕的旧 MiniMax JSON")

    logger.info(f"🚀 开始生成 JSON | 输入: {scan_root} | 并发: {MAX_WORKERS} | 任务数: {total_files}")
    start_time = time.time()

    task_args = [(
        i, total_files, path, tid, node, lang, vm,
        output_task_root, state_key, audio_state_key,
    )
                 for i, (path, tid, node, lang, vm) in enumerate(all_tasks, 1)]

    stats = {"success": 0, "skipped": 0, "failed": 0}
    errors = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process_one_file, arg): arg for arg in task_args}
        for future in as_completed(futures):
            try:
                status, message = future.result()
            except Exception as exc:
                status, message = "failed", f"❌ Step 4 worker 异常：{exc}"
            stats[status] += 1
            safe_print(message)
            if status == "failed":
                errors.append(message)

    logger.info("-" * 30)
    logger.info(
        f"📊 Step 4 完成 | 成功: {stats['success']} | "
        f"过滤: {stats['skipped']} | 失败: {stats['failed']} | "
        f"耗时: {time.time() - start_time:.2f}秒"
    )

    if _usage_tracker:
        try:
            _usage_tracker.flush()
        except Exception as exc:
            logger.warning(f"⚠️ API 用量落库失败，不影响 Step 4：{exc}")

    if errors:
        preview = "；".join(errors[:5])
        logger.error(
            f"⚠️ Step 4 有 {stats['failed']} 个 JSON 生成失败，"
            f"保留成功产物并继续。{preview}"
        )
        ResumeState(output_task_root).mark_step(
            state_key,
            stable_digest(STEP_CACHE_VERSION, sorted(errors)),
            status="failed", error=preview,
        )
        raise RuntimeError(
            f"Step 4 有 {stats['failed']} 个 JSON 资产失败；" + preview
        )

    if stats["success"] == 0:
        logger.warning("⚠️ Step 4 没有生成 TTS JSON，所有字幕均为无需配音的内容。")

    item_states = ResumeState(output_task_root).list_items(state_key)
    ResumeState(output_task_root).mark_step(
        state_key,
        stable_digest(
            STEP_CACHE_VERSION,
            sorted(
                (key, value.get("input_digest"), value.get("status"))
                for key, value in item_states.items()
            ),
        ),
        status="success",
    )

    return stats


if __name__ == "__main__":
    TARGET_ID = ""
    main(TARGET_ID)
