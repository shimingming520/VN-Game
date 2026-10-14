import os
import sys
import re
import hashlib
import json
import time
import shutil
from pathlib import Path
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from openai import OpenAI
from sqlalchemy import text
from logger_config import setup_logger

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from common.config import DATABASE_URL, OUTPUTS_PATH, PLATFORM_DATABASE_URL
from common.audio_languages import AUDIO_OUTPUT_LANGUAGES
from common.llm_fallback import ALTERNATE_FALLBACK_MODEL, ModelFallback
from common.name_mapping_utils import get_source_to_change_conversion, get_localized_names
try:
    from pypinyin import lazy_pinyin
except ImportError:  # pragma: no cover - casting 环境会提供该依赖
    lazy_pinyin = None
try:
    from common.audio_db import get_platform_engine
except ImportError:  # 离线单测的 common 桩包只提供旧共享 engine
    from common.platform_db import platform_engine as _platform_engine

    def get_platform_engine():
        return _platform_engine
try:
    from resume_state import (
        ResumeState,
        atomic_write_text,
        file_digest,
        stable_digest,
        valid_text,
    )
except ImportError:  # 支持按文件路径加载本模块的离线测试
    import importlib.util as _importlib_util

    _resume_spec = _importlib_util.spec_from_file_location(
        "audio_producer_resume_state",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "resume_state.py"),
    )
    _resume_module = _importlib_util.module_from_spec(_resume_spec)
    _resume_spec.loader.exec_module(_resume_module)
    ResumeState = _resume_module.ResumeState
    atomic_write_text = _resume_module.atomic_write_text
    file_digest = _resume_module.file_digest
    stable_digest = _resume_module.stable_digest
    valid_text = _resume_module.valid_text

try:
    from common.usage_tracker import tracker as _usage_tracker
except ImportError:
    _usage_tracker = None

logger = setup_logger("Step2_Subs", "step2_subs.log")

DB_URL = PLATFORM_DATABASE_URL  # 跨模块共用表已迁平台库(agent_platform)
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = OUTPUTS_PATH

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
# BASE_URL = "https://api.deepseek.com/v1"
# BASE_URL = "https://ai.farmmx.com/v1"
BASE_URL = os.getenv("PLOT_BASE_URL", "http://sefutech-api.kube-test.svc:3000/v1")
# 当前字幕线使用 gpt-5.6-sol；ModelFallback 会在中转服务异常时自动切换备用模型。
MODEL = os.getenv("SUBTITLE_TRANSLATION_MODEL", "gpt-5.6-sol")
# 主模型偶发不稳定时自动改用备选模型重发同一请求（同中转站、同调用参数）。
_model_fallback = ModelFallback(MODEL, name="Step2Subs", logger=logger)


def _env_float(name, default, *, minimum=None, maximum=None):
    """读取可选数值配置；配置写错时回退默认值而不中断字幕流程。"""
    raw = os.getenv(name)
    try:
        value = float(raw) if raw is not None else float(default)
    except (TypeError, ValueError):
        logger.warning(f"⚠️ {name} 配置无效 {raw!r}，使用默认值 {default}")
        value = float(default)
    if minimum is not None:
        value = max(float(minimum), value)
    if maximum is not None:
        value = min(float(maximum), value)
    return value


def _env_int(name, default, *, minimum=None, maximum=None):
    """读取可选整数配置；配置写错时回退默认值而不中断字幕流程。"""
    raw = os.getenv(name)
    try:
        value = int(raw) if raw is not None else int(default)
    except (TypeError, ValueError):
        logger.warning(f"⚠️ {name} 配置无效 {raw!r}，使用默认值 {default}")
        value = int(default)
    if minimum is not None:
        value = max(int(minimum), value)
    if maximum is not None:
        value = min(int(maximum), value)
    return value


# 单次请求超时，不是整批字幕任务的总超时。15 分钟足够覆盖大多数长输出，
# 同时避免中转服务失联时一个线程无限期占住。超时后仍走现有重试/原文兜底。
SUBTITLE_LLM_TIMEOUT_SECONDS = max(
    60.0, _env_float("SUBTITLE_LLM_TIMEOUT_SECONDS", 900)
)

LANG_DIRS = {
    item["code"]: item["subtitle_dir"] for item in AUDIO_OUTPUT_LANGUAGES
}

# 从语体表的第一人称描述里取出日语词形。长词在前，避免「私」抢先匹配「わたくし」
_FIRST_PERSON_WORDS = (
    "わたくし", "わたし", "あたし", "俺様", "吾輩", "小生", "当方", "わらわ",
    "拙者", "父ちゃん", "老奴", "我々", "自分", "わし", "私", "僕", "俺", "余",
    "我", "妾", "朕", "某",
)
_FIRST_PERSON_RE = re.compile("(" + "|".join(_FIRST_PERSON_WORDS) + ")")
# 条件式写法的标志词：出现即说明人称随对象变化，不能锁成单一值
_CONDITIONAL_HINT = re.compile(r'(对|時|时|场合|場合|使い分|外|内心|不使用)')


def _pin_first_person(desc):
    """
    从第一人称描述里取出可强制锁定的唯一人称。
    条件式（如「对女儿用俺，对外用私」）或多选（如「俺、我々」）一律返回 None，
    交给语体表原文让模型按上下文判断，避免锁错。
    """
    desc = (desc or "").strip()
    if not desc or _CONDITIONAL_HINT.search(desc):
        return None
    found = set(_FIRST_PERSON_RE.findall(desc))
    # 「私」是「わたくし」的子串，命中后者时不算歧义
    if "わたくし" in found:
        found.discard("私")
    return found.pop() if len(found) == 1 else None


def _has_first_person_override(ja_style):
    overrides = ja_style.get("address_overrides") if isinstance(ja_style, dict) else []
    return any(
        isinstance(rule, dict) and str(rule.get("first_person") or "").strip()
        for rule in (overrides or [])
    )


def _pinned_first_person_forms(ja_style):
    """返回允许视为同一角色规范第一人称的表记集合；存在条件覆盖时不做静态锁定。"""
    if not isinstance(ja_style, dict) or _has_first_person_override(ja_style):
        return None
    first_person = str(ja_style.get("first_person") or "").strip()
    if not first_person or _CONDITIONAL_HINT.search(first_person):
        return None

    forms = set(_FIRST_PERSON_RE.findall(first_person))
    if first_person in _FIRST_PERSON_WORDS:
        forms.add(first_person)
    for value in [ja_style.get("first_person_reading"), *(ja_style.get("first_person_aliases") or [])]:
        value = str(value or "").strip()
        if value in _FIRST_PERSON_WORDS:
            forms.add(value)
    if forms:
        return forms
    pinned = _pin_first_person(first_person)
    return {pinned} if pinned else None


def _canonical_first_person(ja_style, forms):
    first_person = str((ja_style or {}).get("first_person") or "").strip()
    if first_person in forms:
        return first_person
    match = _FIRST_PERSON_RE.search(first_person)
    if match and match.group(1) in forms:
        return match.group(1)
    return sorted(forms, key=lambda value: (-len(value), value))[0]

# 翻译并发与分块配置
TRANSLATE_CHUNK_SIZE = 30      # 每个请求翻译的字幕条数（越小越稳）
# 英语的角色语体表 style_note 是整句描述，提示词体积远大于日语的短字段表，
# 叠加英译本身 token 更多，30 条/块时超时与连接中断明显增多，故单独调小。
TRANSLATE_CHUNK_SIZE_BY_LANG = {"en": 15}
TRANSLATE_MAX_WORKERS = 5      # 翻译并发线程数
# 限制同时排队的请求数，避免上万条字幕一次性创建大量 future，造成内存
# 峰值、服务端突发流量和取消困难；默认仅保留 2 个并发窗口。
TRANSLATE_MAX_IN_FLIGHT = max(
    TRANSLATE_MAX_WORKERS,
    _env_int("SUBTITLE_TRANSLATION_MAX_IN_FLIGHT", TRANSLATE_MAX_WORKERS * 2),
)
TRANSLATE_MAX_RETRIES = 3      # 网络/解析等瞬时错误的重试次数
# 单条最终救援的尝试次数。走到这一步说明整块和二分都已失败，此时只剩一条台词，
# 请求体最小、最容易成功；但判定规则可能误伤，不能让一条可疑字幕在两个模型上各
# 默认每个模型 2 次（总预算 4 次），仍可用环境变量临时提高。
TRANSLATE_RESCUE_ATTEMPTS = max(
    1, _env_int("SUBTITLE_TRANSLATION_RESCUE_ATTEMPTS", 2)
)
# 仅用于"仍是源语言"这一种事故的换模型兜底：不能倒在单个模型上。
# 质量问题不走这条路——那类拿到目标语言文本就接受，换模型也未必更好。
TRANSLATE_RESCUE_MODELS = tuple(
    name.strip() for name in os.getenv(
        "SUBTITLE_TRANSLATION_RESCUE_MODELS",
        f"{MODEL},{ALTERNATE_FALLBACK_MODEL}",
    ).split(",") if name.strip()
)
# 空字符串配置不能让救援阶段静默跳过所有模型；至少保留当前主模型，
# 否则缺译文会全部留空，只能等下一次任务才有机会恢复。
if not TRANSLATE_RESCUE_MODELS:
    TRANSLATE_RESCUE_MODELS = (MODEL,)
TRANSLATE_RESCUE_MAX_BACKOFF = max(
    1.0, _env_float("SUBTITLE_TRANSLATION_RESCUE_MAX_BACKOFF", 30)
)
# 熔断阈值：连续这么多条救援都一次没成功，说明是服务整体不可用而非个别难句。
# 此时继续逐条厚重试只会让 Step 2 卡上几小时，不如尽快收尾、让上层拦下重跑。
TRANSLATE_RESCUE_CIRCUIT_BREAK = max(
    1, _env_int("SUBTITLE_TRANSLATION_RESCUE_CIRCUIT_BREAK", 3)
)
# 未翻译校验触发的重译次数。与瞬时错误分开计数：重发同样的内容若两次仍被判失败，
# 大概率是判定规则不适配该条内容而非模型问题，继续重试只是白烧 token。
TRANSLATE_VALIDATE_RETRIES = 2
# 关掉未翻译校验：宁可不判，也不要瞎判。判定规则若在新语种/新题材上误伤，
# 置为 False 即完全跳过校验与重译，直接采用模型首次返回的译文。
TRANSLATE_VALIDATE_ENABLED = True

# 质量审校不是字符串术语替换：审校模型只报告高置信度的事实/结构错误，
# 低置信度的风格差异只记录不自动改写。
# 翻译质量审校（事实、实体、数字、自然度和风格）已按生产要求永久停用。
# 语言残留与控制结构检查仍由 _translation_hard_violation_reasons 负责，
# 但不会触发这套独立 LLM 审校流程或生成质量报告。
TRANSLATION_QA_ENABLED = False
TRANSLATION_QA_CHUNK_SIZE = max(
    1, _env_int("SUBTITLE_TRANSLATION_QA_CHUNK_SIZE", 20)
)
TRANSLATION_QA_MAX_REPAIRS = max(
    0, _env_int("SUBTITLE_TRANSLATION_QA_MAX_REPAIRS", 120)
)
TRANSLATION_QA_REPAIR_MIN_CONFIDENCE = min(
    1.0, max(0.0, _env_float("SUBTITLE_TRANSLATION_QA_REPAIR_MIN_CONFIDENCE", 0.92))
)
TRANSLATION_CONTEXT_VERSION = "v2"
# 送 LLM 复审的长度比阈值。中译英/日的正常比例跨度很大（实测同一批字幕英文中位
# 78 字符、日文 37），所以阈值只用来抓"整段漏译"和"离谱膨胀"这种粗暴异常，
# 不做精细判断——误伤的代价是多审几条，漏判由抽样兜底。
QA_LENGTH_RATIO_LOW = _env_float("SUBTITLE_QA_LENGTH_RATIO_LOW", 0.25, minimum=0.0)
QA_LENGTH_RATIO_HIGH = _env_float(
    "SUBTITLE_QA_LENGTH_RATIO_HIGH", 4.0, minimum=QA_LENGTH_RATIO_LOW
)
# 短句长度比波动天然很大（"1"→"one"、"嗯"→"Mhm"），只对够长的句子套比例判据。
QA_LENGTH_RATIO_MIN_LEN = _env_int("SUBTITLE_QA_LENGTH_RATIO_MIN_LEN", 12, minimum=1)

# 断点缓存目录名。放在 task 目录下且以点开头：下游 step 按精确目录名匹配语言目录，
# 不会把它当成产物。
TRANSLATE_CACHE_DIRNAME = "subtitle-translate-resume"
# 兼容旧执行留下的隐藏目录；新执行只创建可见名称。
LEGACY_TRANSLATE_CACHE_DIRNAME = ".subtitle-translate-resume"
# key 计算方式变更时递增，使旧缓存整体失效
# 语言残留规则和失败兜底策略变化后，旧缓存必须整体失效；否则历史中文/英文
# 残留可能在未请求模型的情况下重新进入正式字幕。
TRANSLATE_CACHE_VERSION = "v7"
STEP_CACHE_VERSION = "subtitle-assets-v2"

def build_dynamic_translation_context(items, name_conversion=None, localized_names=None, styles=None):
    """从当前任务/场景动态提取翻译上下文，不维护跨小说固定术语表。

    这里输出的是“候选实体和事实线索”，不是强制替换规则。候选只来自当前
    字幕及其场景，并保留原文 span；模型必须结合句意判断是否真的是实体。
    """
    items = list(items or [])
    source_blob = "\n".join(str(item.get("content") or "") for item in items)
    entities = []
    seen = set()
    for source, speaker_id in sorted(
        (name_conversion or {}).items(), key=lambda pair: (-len(str(pair[0])), str(pair[0]))
    ):
        source = str(source or "").strip()
        if not source or source in seen or source not in source_blob:
            continue
        seen.add(source)
        target_name = str((localized_names or {}).get(str(speaker_id)) or "").strip()
        # name_mappings 中缺少某语言时，宁可沿用当前剧情中的改名结果，也不要
        # 让模型根据拼音/英语上下文自行猜读法（例如 郑立伟 -> ジョン・リーウェイ）。
        if not target_name:
            target_name = source
        entities.append({
            "source_span": source,
            "speaker_id": str(speaker_id or ""),
            "target_name": target_name,
            "occurrences": source_blob.count(source),
            "status": "candidate",
        })

    speakers = list(dict.fromkeys(str(item.get("speaker") or "") for item in items))
    quantity_facts = []
    quantity_re = re.compile(
        r"(?P<value>\d+(?:\.\d+)?|[零一二三四五六七八九十百千万两半]+)"
        r"(?P<unit>斤|两|克|公斤|千克|块|元|毛|万|千|百)"
    )
    for item in items:
        for match in quantity_re.finditer(str(item.get("content") or "")):
            quantity_facts.append({
                "source": match.group(0),
                "value": match.group("value"),
                "unit": match.group("unit"),
                "uid": item.get("uid"),
            })
    return {
        "version": TRANSLATION_CONTEXT_VERSION,
        "scene": {
            "node_id": str(items[0].get("node_id") or "") if items else "",
            "scene_id": str(items[0].get("scene_id") or "") if items else "",
            "speakers": speakers,
        },
        "entities": entities,
        "quantity_facts": quantity_facts,
        "style_speakers": [
            speaker for speaker in speakers
            if styles and _style_info_usable((styles.get("characters") or {}).get(speaker))
        ],
    }


def _render_dynamic_translation_context(context):
    if not context:
        return ""
    entities = context.get("entities") or []
    lines = [
        "Context policy (not a fixed glossary): infer meaning from the current sentence and nearby context.",
        "The candidate entities below are hypotheses only. Preserve identity when the source span clearly refers "
        "to that entity; do not force a candidate into ordinary words, substrings, or unrelated senses. Prefer "
        "natural target-language wording over literal or mechanical substitutions.",
    ]
    if entities:
        lines.append("Dynamic entity candidates in this scene:")
        lines.append(
            "When a candidate is clearly a person/character or named entity, preserve its identity and use "
            "the target rendering from the task mapping exactly; do not invent a different transliteration. "
            "The mapping is task-local identity data, not a cross-novel glossary."
        )
        for entity in entities:
            lines.append(
                f"- source span={entity['source_span']!r}; speaker/entity id={entity['speaker_id']!r}; "
                f"target rendering={entity['target_name']!r}; occurrences={entity['occurrences']}"
            )
    quantities = context.get("quantity_facts") or []
    if quantities:
        lines.append(
            "Quantity facts are source-grounded and must remain numerically equivalent. "
            "Choose one coherent unit policy for the work; never change a value merely to sound natural:"
        )
        for fact in quantities:
            lines.append(
                f"- id={fact.get('uid')}; source quantity={fact.get('source')!r}; "
                f"numeric value={fact.get('value')!r}; source unit={fact.get('unit')!r}"
            )
    return "\n".join(lines)


def _entity_identity_violation_reasons(source_text, translated_text, context):
    """Detect only unambiguous identity regressions, never force synonyms or prose wording.

    A source name that has an explicit task-local localized rendering must not be copied
    back into a target-language subtitle when that rendering is different.  We do not
    require the localized name to appear (Japanese may naturally omit a subject), and
    we do not attempt substring/transliteration replacement.
    """
    reasons = []
    for entity in (context or {}).get("entities") or []:
        source_span = str(entity.get("source_span") or "").strip()
        target_name = str(entity.get("target_name") or "").strip()
        if (
            source_span
            and target_name
            and source_span != target_name
            and source_span in str(source_text or "")
            and source_span in str(translated_text or "")
        ):
            reasons.append(
                f"mapped entity copied in source form: {source_span!r}; "
                f"expected task-local rendering {target_name!r} when explicitly named"
            )
    return reasons


# 分支首条字幕使用「选项文案||实际正文」。这是下游构建游戏依赖的控制符，
# 不是自然语言标点；必须由程序拆分、重组和校验，不能交给模型自由改写。
CONTROL_SEPARATOR = "||"
INVALID_CONTROL_TRANSLATION = "[invalid content_parts response]"


_MISSING_SPEAKER_VALUES = {"", "none", "null", "unknown"}


def _usable_speaker_value(value):
    """Return false for null/sentinel speaker values emitted by old plots."""
    normalized = str(value or "").strip().lower()
    return (
        normalized not in _MISSING_SPEAKER_VALUES
        and not normalized.startswith("unknown_")
    )


def normalize_speaker_smart(speaker_id, full_name, fallback_key=None):
    def is_chinese(string):
        for ch in str(string):
            if '一' <= ch <= '鿿': return True
        return False

    if _usable_speaker_value(full_name) and not is_chinese(full_name):
        s_text = re.sub(r"['']s\b", "", str(full_name), flags=re.IGNORECASE)
        s_text = re.sub(r"['']", "", s_text)
        clean_text = re.sub(r'[^\w]', ' ', s_text)
        words = clean_text.split()
        return "".join(word.capitalize() for word in words)

    raw_source = (
        speaker_id if _usable_speaker_value(speaker_id)
        else full_name if _usable_speaker_value(full_name)
        else None
    )
    if raw_source is None:
        digest_source = str(fallback_key or "unidentified-speaker")
        digest = hashlib.sha1(digest_source.encode("utf-8")).hexdigest()[:12]
        return f"UnidentifiedSpeaker_{digest}"
    if re.search(r'[^a-zA-Z0-9]', str(raw_source)):
        if lazy_pinyin is not None and re.search(r'[\u4e00-\u9fa5]', str(raw_source)):
            return "".join(part.capitalize() for part in lazy_pinyin(str(raw_source)))
        clean_text = re.sub(r'[^\w]', ' ', str(raw_source))
        words = clean_text.split()
        return "".join(word.capitalize() for word in words)
    else:
        s = str(raw_source)
        if not s:
            digest_source = str(fallback_key or "unidentified-speaker")
            digest = hashlib.sha1(digest_source.encode("utf-8")).hexdigest()[:12]
            return f"UnidentifiedSpeaker_{digest}"
    return s[0].upper() + s[1:]


def normalize_scene_id(text_input):
    if not text_input: return "UnknownScene"
    return str(text_input).strip()


def detect_language(texts):
    sample = " ".join(texts)
    kana_count = len(_HAS_KANA.findall(sample))
    cjk_count = len(_HAS_CJK.findall(sample))
    latin_count = len(re.findall(r'[A-Za-z]', sample))
    if kana_count >= max(3, cjk_count // 20):
        return "ja"
    if cjk_count >= max(1, latin_count // 2):
        return "zh"
    return "en"


_LANG_NAMES = {"zh": "Chinese", "ja": "Japanese", "en": "English"}
_HAS_CJK = re.compile(r'[一-鿿]')
_HAS_KANA = re.compile(r'[぀-ヿ]')


def _is_non_japanese_kanji(ch):
    """
    该汉字是否为日语不使用的字形。cp932(Shift-JIS) 覆盖日本常用汉字而不含简化字，
    以此区分中日纯汉字文本，比枚举中文虚词可靠：後者会把「完了」「到着」「把握」
    「被害」「旦那」等日语词判成中文。
    注意 cp932 基于 JIS X 0208，不含 2010 年常用汉字表的「頰剝塡」等字形，
    故单独用它会误伤合法日语，必须配合下面的"字符须来自原文"约束。
    """
    if not _HAS_CJK.match(ch):
        return False
    try:
        ch.encode("cp932")
    except UnicodeEncodeError:
        return True
    return False


def _protected_target_entity_spans(dynamic_context=None):
    """Return mapped identity spans that are intentionally kept verbatim.

    A Chinese source name may legitimately remain in a Japanese subtitle when
    no target-language rendering exists.  Such a name must not make the whole
    sentence look untranslated.  Only exact source==target mappings are
    protected; an explicit different target rendering is still checked by the
    existing identity rules.
    """
    protected = []
    for entity in (dynamic_context or {}).get("entities") or []:
        source_span = str(entity.get("source_span") or "").strip()
        target_name = str(entity.get("target_name") or "").strip()
        if source_span and target_name and source_span == target_name:
            protected.append(target_name)
    return tuple(sorted(set(protected), key=len, reverse=True))


def _is_protected_entity_only_echo(text, dynamic_context=None):
    """Whether an exact echo consists only of an explicitly preserved entity."""
    value = str(text or "")
    if not value or not dynamic_context:
        return False
    remainder = value
    for span in _protected_target_entity_spans(dynamic_context):
        remainder = remainder.replace(span, "")
    return not _WORD_CHARS.search(remainder)


def _shares_source_only_kanji(text, source_text, dynamic_context=None):
    """
    译文是否照抄了原文里"日语不用的字形"。
    只认同时满足两条的字符：日语不使用，且确实出现在本条原文中。
    这样判据不依赖题材字表——繁简、专有名词、生僻字都由原文自身决定，
    模型译出的合法日语（含 JIS2004 新字形）不会被误伤。
    """
    if not source_text or not text:
        return False
    suspects = {ch for ch in source_text if _is_non_japanese_kanji(ch)}
    if not suspects:
        return False

    # Remove exact mapped identity spans before measuring residue.  A single
    # preserved name is a soft suspicion, not enough evidence to spend another
    # ten model calls on the subtitle.
    probe = str(text)
    for span in _protected_target_entity_spans(dynamic_context):
        probe = probe.replace(span, " " * len(span))

    suspicious_positions = [index for index, ch in enumerate(probe) if ch in suspects]
    if not suspicious_positions:
        return False

    # One or two isolated characters are commonly a name/term.  Do not reject
    # an otherwise normal Japanese sentence merely because it contains a
    # preserved Chinese name: kana is strong evidence that the sentence has
    # actually been translated.  Only keep a high-precision path for a mostly
    # CJK/no-kana string, where Chinese residue is much more likely.
    longest_run = 1
    current_run = 1
    for previous, current in zip(suspicious_positions, suspicious_positions[1:]):
        if current == previous + 1:
            current_run += 1
            longest_run = max(longest_run, current_run)
        else:
            current_run = 1
    suspicious_count = len(suspicious_positions)
    cjk_count = len(_HAS_CJK.findall(probe))
    kana_count = len(_HAS_KANA.findall(probe))
    if kana_count:
        return (
            suspicious_count >= 3
            and longest_run >= 2
            and cjk_count >= 6
            and suspicious_count / cjk_count >= 0.5
        )
    return (
        cjk_count >= 6
        and suspicious_count >= 2
        and suspicious_count / cjk_count >= 0.3
    )


_WORD_CHARS = re.compile(r'[^\W_]', re.UNICODE)
# 整段回显判据的最短实词数。短到只有姓名、地名、缩写或拟声词时，原样保留
# 可能是合理的；六个以上正文字符的中日整句相同则按高置信度残留处理。更短的
# 中文整句仍由 source-only 汉字连续残留规则识别，姓名/地名则交给实体保护。
_ECHO_MIN_LEN = 6
_JAPANESE_FORCE_TRANSLATE_PHRASES = frozenset({
    "impossible is nothing",
    "just do it",
    "opening questions",
    "the fight",
    "sun and young",
})


def _word_len(text):
    """实词字符数，忽略标点与空白。"""
    return len(_WORD_CHARS.findall(text or ""))


def _looks_untranslated(text, src_lang, tgt_lang, source_text="", dynamic_context=None):
    """
    判断译文是否疑似未翻译（整段仍是源语言）。
    仅对能可靠区分的方向做判断，避免误杀：误判会触发无效重试并退化成逐条兜底，
    代价远高于漏判，而模型整段回显原文的常见情形已由等值比较拦住。
    """
    if not text.strip():
        return True
    if not TRANSLATE_VALIDATE_ENABLED:
        return False
    # 长文整段与原文完全一致，任何题材下都不可能是正确译文。
    # 只在够长时才据此判定：缩写、专名、拟声词、纯标点（CEO、慕声、……）的
    # 正确译文本就等于原文，对短文本套用等值判据会让重试必然复现同一结果。
    # 不满足时继续走下面的语种判据，不在此提前放行。
    if (source_text and text.strip() == source_text.strip()
            and not _is_protected_entity_only_echo(text, dynamic_context)
            and _word_len(source_text) >= _ECHO_MIN_LEN):
        return True
    # 译成英文：英语字幕正文不允许出现任何中日韩字符。专名也必须转写，
    # 否则中文残留会直接被 TTS 念出来。
    if tgt_lang == "en":
        return bool(_HAS_CJK.search(text) or _HAS_KANA.search(text))
    # 日语字幕正文不允许保留英文短语。品牌口号、产品名或语境中有意保留的
    # 短句（如 Impossible is nothing、Just do it、Opening Questions）仍须
    # 翻译成日语。单个大写缩写、字母等级、城市代号和专有品牌名（B市、F班、
    # X-Dream、In Vision 等）属于名称/标记，不按普通英文残留处理。
    if tgt_lang == "ja":
        # 纯中文残留没有拉丁字母，不能漏判；仅识别原文中的日语不使用字形，
        # 不改变既有的英文残留硬校验。
        if _shares_source_only_kanji(text, source_text, dynamic_context):
            return True
        source_tokens = {
            token.casefold()
            for token in re.findall(r"[A-Za-z]+(?:[-'][A-Za-z]+)?", source_text or "")
        }
        latin_phrases = re.findall(
            r"(?i)(?<![A-Za-z])(?:[A-Za-z]{2,}(?:[-'][A-Za-z]+)?\s+){1,}"
            r"[A-Za-z]{2,}(?:[-'][A-Za-z]+)?(?![A-Za-z])",
            text,
        )
        for phrase in latin_phrases:
            normalized = " ".join(phrase.casefold().split())
            if normalized in _JAPANESE_FORCE_TRANSLATE_PHRASES:
                return True
            phrase_tokens = re.findall(r"[A-Za-z]+(?:[-'][A-Za-z]+)?", normalized)
            # Multiword Latin names/brands already present in the Chinese source
            # (for example In Vision and X-Dream) are identity data, not English
            # prose. They must remain stable in the Japanese translation.
            if phrase_tokens and not all(token in source_tokens for token in phrase_tokens):
                return True
        return False
    return False


_WRAP_QUOTES = {'"': '"', "'": "'", '“': '”', '‘': '’', '「': '」', '『': '』', '《': '》'}


def _quote_pair_at_edges(text):
    value = str(text or "").strip()
    if len(value) < 2:
        return None
    opener = value[0]
    closer = _WRAP_QUOTES.get(opener)
    if closer and value[-1] == closer:
        return opener, closer
    return None


def _strip_wrapping_quotes(text, source_text=None):
    """Remove only model-added whole-string wrappers.

    Quote marks present in the source are semantic content.  They must survive
    translation and the subtitle writer's own outer ``「」`` envelope; stripping
    them would change the source's punctuation semantics.  For source text with
    no quotes, a complete wrapper is presumed to be an accidental model envelope
    and is removed (including repeated accidental wrappers).
    """
    value = str(text or "").strip()
    source_value = str(source_text or "").strip()
    source_has_quotes = _contains_quote(source_value)
    source_is_whole_quote = _quote_pair_at_edges(source_value) is not None
    while True:
        pair = _quote_pair_at_edges(value)
        if not pair:
            break
        inner = value[1:-1].strip()
        # A source that is wholly quoted owns one semantic pair.  If the model
        # returned two complete edge pairs (``「“text”」`` or ``「「text」」``),
        # discard only the outer transport wrapper and keep the inner pair.
        if source_has_quotes and source_is_whole_quote:
            if _quote_pair_at_edges(inner):
                value = inner
            break
        # For source text with embedded quotes, remove a wrapper only when an
        # inner quote remains.  This turns ``「「name」を選ぶ」`` into
        # ``「name」を選ぶ`` while preserving the source-derived inner quote.
        if source_has_quotes and not _contains_quote(inner):
            break
        value = inner
    return value


# A bare ASCII apostrophe is overwhelmingly a word-internal apostrophe in
# English (don't, character's), not a quotation envelope.  Treat only paired
# quotation glyphs as source-semantic quote evidence.
_QUOTE_CHARS = frozenset('"“”‘’「」『』《》〈〉')


def _contains_quote(text):
    value = str(text or "")
    if any(char in _QUOTE_CHARS for char in value):
        return True
    # ASCII single quotes are ambiguous with apostrophes.  Count them as
    # semantic quotation only when a complete quoted span is present.
    return bool(re.search(r"(?:^|[\s([{])'[^'\n]+?'(?=$|[\s.,!?;:)\]}])", value))


def _format_subtitle_file_text(beat_info, content, target_lang=None):
    """Build one final subtitle line with exactly one program-owned envelope."""
    # The source decides whether quote punctuation is semantic.  If the source
    # contains quotes, keep the translated quote structure intact and add the
    # program-owned outer envelope.  If it contains none, remove only a model's
    # accidental whole-string wrapper before adding our envelope.
    source_text = beat_info.get("content")
    content = _strip_duplicate_speaker_prefix(content, beat_info, source_text)
    content = _clean_translated_text(content, source_text)
    speaker = beat_info["final_speaker_id"]
    if speaker == "System":
        return f"System：{_clean_system_translation(content, beat_info.get('content'))}"
    if beat_info["beat_type"] == "nvl_thought":
        return f"{speaker}：{content}"
    if speaker == "PangBai":
        return f"PangBai：{content}"
    return (
        f"{speaker}_{beat_info['combination_suffix']}_{beat_info['emotion']}："
        f"「{content}」"
    )


def _subtitle_format_violation_reasons(beat_info, content):
    """Validate the final line's envelope without interpreting its prose."""
    value = str(content or "")
    source = str(beat_info.get("content") or "")
    if beat_info.get("final_speaker_id") == "System":
        if value.startswith(("System：", "System:")):
            return ["system prefix would be duplicated"]
        return []
    if beat_info.get("beat_type") == "nvl_thought" or beat_info.get("final_speaker_id") == "PangBai":
        return []
    # Only an edge pair unsupported by the source is a transport-level wrapper
    # problem.  Source-derived quotation marks are valid content.
    if _quote_pair_at_edges(value) and not _contains_quote(source):
        return ["dialogue translation already contains the outer dialogue envelope"]
    return []


def _subtitle_file_is_canonical(path, beat_info, target_lang, *, validate_language=True):
    """Validate an existing subtitle against the current writer contract.

    A non-empty file is not enough for resume: old runs may contain duplicated
    wrappers or echoed speaker prefixes.  Reconstructing the line through the
    same formatter makes the check source-aware and prevents a bad old artifact
    from being silently skipped.
    """
    try:
        value = Path(path).read_text("utf-8").strip()
    except (OSError, UnicodeError):
        return False
    speaker = str(beat_info.get("final_speaker_id") or "")
    if speaker == "System":
        prefix = "System："
    elif beat_info.get("beat_type") == "nvl_thought" or speaker == "PangBai":
        prefix = f"{speaker}："
    else:
        prefix = (
            f"{speaker}_{beat_info.get('combination_suffix', '1-1-1')}_"
            f"{beat_info.get('emotion') or 'calm'}："
        )
    if not value.startswith(prefix):
        return False
    payload = value[len(prefix):]
    if speaker != "System" and beat_info.get("beat_type") == "dialogue" and speaker != "PangBai":
        if len(payload) < 2 or not (payload.startswith("「") and payload.endswith("」")):
            return False
        payload = payload[1:-1]
    canonical = _format_subtitle_file_text(beat_info, payload, target_lang)
    if value != canonical:
        return False
    # 正常断点复用时验证正文语言，避免旧的坏译文被静默复用。部分发布收尾
    # 时可以关闭这一项：问题条目仍需落盘并交给下游照常处理，不能因为语言
    # 告警把整条音频流水线再次拦死。
    # variable_update 是原样透传的控制字段，里面的 effects/flags 等英文键名
    # 不是自然语言残留；不参与翻译和目标语言检查。
    if validate_language and beat_info.get("beat_type") != "variable_update":
        source = str(beat_info.get("content") or "")
        source_lang = detect_language([source]) if source else "zh"
        if target_lang in {"en", "ja"}:
            if _looks_untranslated(payload, source_lang, target_lang, source):
                return False
    return True


_META_PREFIX = re.compile(r'^\s*\[[^\]]{0,60}\]\s*')


def _strip_meta_prefix(text):
    """剥掉模型可能回显的 [speaker|type] 元数据前缀。"""
    return _META_PREFIX.sub('', text, count=1).strip()


def build_style_samples(unique_items, per_speaker=6, max_speakers=15):
    """
    按角色分组抽样，保证每个主要角色都有代表台词进入语域分析。
    返回 (样本行列表, 角色名列表)，角色按台词数降序。
    """
    by_speaker = {}
    for content, speaker, beat_type in unique_items:
        if beat_type not in ('dialogue', 'nvl_thought'):
            continue
        by_speaker.setdefault(speaker, []).append((content, beat_type))

    ranked = sorted(by_speaker.items(), key=lambda kv: -len(kv[1]))[:max_speakers]
    lines = []
    for speaker, rows in ranked:
        for content, beat_type in rows[:per_speaker]:
            lines.append(f"[{speaker}|{beat_type}] {content}")
    return lines, [spk for spk, _ in ranked]


def fetch_character_styles(task_id):
    """读取 character_styles 表的角色语体表(由 info_extractor 从原文提取)。"""
    if not task_id:
        return None
    try:
        with get_platform_engine().connect() as conn:
            row = conn.execute(
                text("SELECT data FROM character_styles WHERE task_id = :tid LIMIT 1"),
                {"tid": str(task_id)},
            ).fetchone()
    except Exception as e:
        logger.warning(f"⚠️ 读取 character_styles 表失败: {e}")
        return None

    data = row[0] if row else None
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            logger.warning("⚠️ character_styles 内容不是合法 JSON")
            return None
    if not isinstance(data, dict) or not isinstance(data.get("characters"), dict) or not data.get("characters"):
        if isinstance(data, dict) and data.get("characters") is not None:
            logger.warning("⚠️ character_styles.characters 不是对象，忽略该语体表并使用通用规则")
        return None
    return data


def _style_info_usable(info):
    if not isinstance(info, dict):
        return False
    quality = info.get("quality") if isinstance(info.get("quality"), dict) else {}
    if quality.get("status"):
        return quality["status"] not in ("insufficient", "failed")
    ja = info.get("ja") if isinstance(info.get("ja"), dict) else {}
    en = info.get("en") if isinstance(info.get("en"), dict) else {}
    return any(str(value or "").strip() for value in (
        ja.get("register"), ja.get("politeness"), ja.get("honorific_level"),
        ja.get("first_person"), ja.get("sentence_end"), ja.get("request_style"),
        en.get("style_note"), en.get("sentence_style"), en.get("vocabulary"),
        en.get("address_strategy"), en.get("contractions"),
    ))


def render_style_guide(styles, tgt_lang, speakers):
    """把 character_styles 渲染成翻译提示词。只输出本批出现的角色，避免无关信息干扰。"""
    characters = styles.get("characters") or {}
    work = styles.get("work") or {}
    wanted = []
    for speaker in speakers:
        info = characters.get(speaker)
        if not _style_info_usable(info):
            continue
        wanted.append(speaker)

    lines = []
    work_bits = [
        f"{label}：{work[key]}"
        for key, label in (
            ("genre", "题材"), ("era", "时代"),
            ("social_structure", "社会结构"), ("overall_tone", "整体语感"),
        )
        if work.get(key)
    ]
    if work_bits:
        lines.append("作品背景：" + "；".join(work_bits))

    narration_key = "ja_narration" if tgt_lang == "ja" else "en_narration"
    if work.get(narration_key):
        lines.append(f"旁白规范：{work[narration_key]}")
    if tgt_lang == "ja":
        lines.append(
            "日本語全局规则："
            f"时代语言={work.get('ja_historical_level', 'modern')}"
        )
        if work.get("ja_orthography"):
            lines.append(f"日语书写规范：{work['ja_orthography']}")
        if work.get("ja_punctuation"):
            lines.append(f"日语标点规范：{work['ja_punctuation']}")
        if work.get("ja_dialect_policy"):
            lines.append(f"日语方言策略：{work['ja_dialect_policy']}")
        if work.get("ja_name_reading_policy"):
            lines.append(f"姓名与专名读法策略：{work['ja_name_reading_policy']}")
    else:
        lines.append(
            "English global rules: "
            f"spelling={work.get('en_variant', 'US')}; "
            f"narrative_tense={work.get('narrative_tense', 'past')}; "
            f"narrative_person={work.get('narrative_person', 'third')}; "
            f"historical_language={work.get('historical_language_level', 'modern')}"
        )
        if work.get("dialect_policy"):
            lines.append(f"Dialect policy: {work['dialect_policy']}")
        if work.get("terminology_policy"):
            lines.append(f"Terminology policy: {work['terminology_policy']}")

    if tgt_lang == "ja":
        if wanted:
            lines.append(
                "日语角色语体表（角色 | 身份 | 丁寧度 | 尊敬语 | 谦让语 | 第一人称 | 人称省略 | 第二人称策略 | 请求 | 命令 | 时代感 | 方言）："
            )
        for speaker in wanted:
            info = characters[speaker]
            ja = info.get("ja") or {}
            lines.append(
                f"{speaker} | {info.get('identity', '')} | {ja.get('politeness', 'plain')} | "
                f"{ja.get('honorific_level', 'none')} | {ja.get('humble_level', 'none')} | "
                f"{ja.get('first_person', '')} | {ja.get('pronoun_omission', 'prefer')} | "
                f"{ja.get('second_person', '')} | {ja.get('request_style', '')} | "
                f"{ja.get('command_style', '')} | {ja.get('era_style', 'modern')} | "
                f"{ja.get('dialect', 'standard')}"
            )
            if ja.get("register") or ja.get("sentence_end"):
                lines.append(
                    f"{speaker} 补充语体：{ja.get('register', '')}；句末特点={ja.get('sentence_end', '')}"
                )
            if ja.get("address_terms"):
                lines.append(
                    f"{speaker} 固定称谓：{json.dumps(ja['address_terms'], ensure_ascii=False)}"
                )
            for key, label in (
                ("preferred_endings", "优先句末"),
                ("allowed_endings", "允许句末"),
                ("avoid_endings", "禁止句末"),
                ("avoid_second_person", "禁止第二人称/称谓"),
            ):
                if ja.get(key):
                    lines.append(f"{speaker} {label}：" + "；".join(str(item) for item in ja[key]))
            if ja.get("gender_marking"):
                lines.append(f"{speaker} 性别化表达策略：{ja['gender_marking']}")
            if ja.get("thought_style"):
                lines.append(f"{speaker} 内心独白：{ja['thought_style']}")
            overrides = ja.get("address_overrides") if isinstance(ja.get("address_overrides"), list) else []
            for rule in overrides:
                if isinstance(rule, dict) and rule.get("when"):
                    lines.append(f"{speaker} 对象覆盖规则：{json.dumps(rule, ensure_ascii=False)}")
        # 人称埋在表格第4列时容易被忽略，单独提成硬约束。
        # 只锁"唯一且无条件"的人称：像「对女儿用俺，对外用私」这种条件式规则若强行取第一个词，
        # 会把角色错锁成只在特定对象前才用的人称，反而破坏正确语体。
        pins = []
        for speaker in wanted:
            ja_style = characters[speaker].get("ja") or {}
            forms = _pinned_first_person_forms(ja_style)
            if forms:
                canonical = _canonical_first_person(ja_style, forms)
                reading = str(ja_style.get("first_person_reading") or "").strip()
                display = canonical + (f"（读作 {reading}）" if reading else "")
                pins.append(f"{speaker}={display}")
        if pins:
            lines.append(
                "第一人称硬约束（需要明确说出第一人称时只能使用指定形式；能自然省略时优先省略）："
                + "；".join(pins)
            )

        # 语体/人称/句末随对象变化的角色单独点出来。plots 没有"听话人"字段，
        # 但同一 chunk 内的台词是连续对话，模型能从上下文判断在跟谁说。
        conditional = []
        for speaker in wanted:
            ja = characters[speaker].get("ja") or {}
            varying = [
                f"{label}={ja.get(key, '')}"
                for key, label in (
                    ("register", "语体"), ("first_person", "第一人称"),
                    ("second_person", "第二人称"), ("sentence_end", "句末"),
                )
                if _CONDITIONAL_HINT.search(ja.get(key, "") or "")
            ]
            if varying:
                conditional.append(f"{speaker}：" + "；".join(varying))
        if conditional:
            lines.append(
                "以下角色的说法随对象变化。请先从上下文判断这句在跟谁说，再按规则选择；"
                "对同一个对象必须始终保持同一套说法，不得在相邻台词间来回切换：\n"
                + "\n".join(conditional)
            )
    else:
        if wanted:
            lines.append(
                "English character voice table "
                "(speaker | identity | formality | directness | contractions | address | sentence | vocabulary | profanity | notes):"
            )
        for speaker in wanted:
            info = characters[speaker]
            en = info.get("en") or {}
            lines.append(
                f"{speaker} | {info.get('identity', '')} | "
                f"{en.get('formality', 'neutral')} | {en.get('directness', 'balanced')} | "
                f"{en.get('contractions', 'allow')} | {en.get('address_strategy', '')} | "
                f"{en.get('sentence_style', '')} | {en.get('vocabulary', '')} | "
                f"{en.get('profanity_level', 'none')} | {en.get('style_note', '')}"
            )
            if en.get("thought_style"):
                lines.append(f"{speaker} inner monologue: {en['thought_style']}")
            if en.get("signature_phrases"):
                lines.append(
                    f"{speaker} signature phrases (use only when natural): "
                    + "; ".join(str(item) for item in en["signature_phrases"])
                )
            if en.get("avoid"):
                lines.append(
                    f"{speaker} forbidden English words/phrases: "
                    + "; ".join(str(item) for item in en["avoid"])
                )
            overrides = en.get("address_overrides") if isinstance(en.get("address_overrides"), list) else []
            for rule in overrides:
                if isinstance(rule, dict) and rule.get("when"):
                    lines.append(f"{speaker} addressee override: {json.dumps(rule, ensure_ascii=False)}")
    return "\n".join(lines)


def analyze_style_guide(sample_lines, speakers, src_lang, tgt_lang):
    """
    用一次 LLM 调用分析作品整体语域，产出翻译语体指引 + 角色语体对照表。
    仅在 character_styles 表缺失时作为降级方案使用(样本来自删减后的字幕，精度低于原文提取)。
    失败时返回空串，翻译退回通用规则。
    """
    if tgt_lang not in ("ja", "en"):
        return ""

    if tgt_lang == "ja":
        focus = "敬语体系（丁寧語/尊敬語/謙譲語）的层级划分、第一/第二人称代词、句末语气"
        table_spec = (
            "然后为下列每个角色固定一套说话风格，格式为每行：\n"
            "角色名 | 身份推断 | 语体(简体だ体/丁寧語/謙譲語等) | 第一人称 | 第二人称 | 句末语气特点\n"
        )
    elif tgt_lang == "en":
        parts.append(
            "English register: politeness is expressed through titles, modal verbs, indirectness, sentence "
            "shape, contractions and lexical choice. Follow each character's formality, directness, contraction, "
            "address, vocabulary and profanity rules. Preserve social hierarchy without making every formal "
            "character sound archaic. Apply addressee overrides only when the surrounding conversation clearly "
            "matches them. Narration must follow the global spelling variant, tense and narrative person. "
            "Do not invent phonetic eye-dialect unless the dialect policy explicitly requires it."
        )
    else:
        focus = "语域正式度、人称与称谓、时代感词汇"
        table_spec = (
            "然后为下列每个角色固定一套说话风格，格式为每行：\n"
            "角色名 | 身份推断 | 语域(正式/中性/口语) | 用词与句式特点\n"
        )

    prompt = (
        f"你是文学翻译的语域分析专家。阅读台词样本，推断作品题材、时代背景、人物身份与相互关系，"
        f"据此为翻译成{_LANG_NAMES[tgt_lang]}制定语体规范，重点说明{focus}。\n\n"
        f"先输出 100 字以内的整体语域要点，再输出角色对照表。\n"
        f"{table_spec}"
        f"需要覆盖的角色：{', '.join(speakers)}\n"
        f"同一角色必须只有一套风格，不得摇摆。直接给结论，不要复述样本或解释推断过程。"
    )

    try:
        client = OpenAI(
            api_key=DEEPSEEK_API_KEY,
            base_url=BASE_URL,
            timeout=SUBTITLE_LLM_TIMEOUT_SECONDS,
            max_retries=0,
        )
        resp, used_model = _model_fallback.chat(
            client,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": "\n".join(sample_lines)}
            ],
            temperature=0.3,
        )
        if _usage_tracker and getattr(resp, "usage", None):
            try:
                _usage_tracker.add_llm_usage(resp.usage, used_model, provider="openai")
            except Exception as exc:
                logger.warning(f"⚠️ API 用量记录失败，不影响语域分析：{exc}")
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.warning(f"⚠️ 语域分析失败 ({tgt_lang})，使用通用规则: {e}")
        return ""


_PRON_BOUNDARY = r'(?:は|が|も|の|を|に|と|へ|だ|です|ね|よ|、|。|」|？|！|…|$)'


def detect_first_persons(text):
    """
    找出译文里作为第一人称使用的词。
    只认作为独立词出现的（后接助词或标点），避免把「汚らわしい」「わずか」里的假名
    误当成「わし」，也避免把「わたくし」中的「私」重复计入。
    """
    found = set()
    for pron in _FIRST_PERSON_WORDS:
        for m in re.finditer(re.escape(pron), text):
            prev = text[m.start() - 1:m.start()]
            nxt = text[m.end():m.end() + 1]
            if prev and re.match(r'[ぁ-んァ-ヶ一-龯]', prev):
                continue
            if nxt == '' or re.match(_PRON_BOUNDARY, nxt):
                found.add(pron)
                break
    if "わたくし" in found:
        found.discard("私")
    return found


def find_pronoun_violations(trans_map, styles, items):
    """
    校验译文是否违反第一人称硬约束，返回违规的 item 列表。
    只校验"唯一且无条件"的人称（条件式规则本就随对象变化，无法静态判定）。
    """
    if not styles:
        return []
    characters = styles.get("characters") or {}
    item_by_uid = {item["uid"]: item for item in items}
    violations = []
    for uid, translated in trans_map.items():
        item = item_by_uid.get(uid)
        if not item:
            continue
        info = characters.get(item["speaker"])
        if not info:
            continue
        ja_style = info.get("ja") or {}
        expected_forms = _pinned_first_person_forms(ja_style)
        if not expected_forms:
            continue
        used = detect_first_persons(_strip_wrapping_quotes(str(translated)))
        if used and used.isdisjoint(expected_forms):
            canonical = _canonical_first_person(ja_style, expected_forms)
            violations.append((item, translated, canonical, sorted(expected_forms), sorted(used)))
    return violations


def repair_pronoun_violations(trans_map, src_lang, tgt_lang, styles, items):
    """
    定点重译：违反人称硬约束的句子逐条重译。
    单条重译时 prompt 只含该角色一行规则，遵守率明显高于整块翻译。
    重译后仍不合规则保留原译文（至少是通顺的译文，不至于退回中文）。
    """
    violations = find_pronoun_violations(trans_map, styles, items)
    if not violations:
        logger.info(f"✅ [{tgt_lang}] 人称校验通过，无需重译")
        return 0

    logger.info(f"🔧 [{tgt_lang}] 人称校验检出 {len(violations)} 条违规，开始定点重译")
    fixed = 0
    for item, old, expected, expected_forms, used in violations:
        guide = render_style_guide(styles, tgt_lang, [item["speaker"]])
        guide += (
            f"\n【最高优先级】{item['speaker']} 的第一人称必须是「{expected}」。"
            f"上一次翻译错误地使用了「{'、'.join(used)}」，本次必须改为「{expected}」，"
            f"允许的同一规范表记为「{'、'.join(expected_forms)}」。能自然省略第一人称时优先省略，"
            f"不得为了满足规则机械补出主语；其余措辞与语体保持不变。"
        )
        new = _translate_single(item, src_lang, tgt_lang, guide)
        recheck = detect_first_persons(_strip_wrapping_quotes(new))
        if new and new != item["content"] and (
            not recheck or not recheck.isdisjoint(set(expected_forms))
        ):
            trans_map[item["uid"]] = new
            fixed += 1
            logger.info(f"   ✅ {item['speaker']}: {'、'.join(used)} → {expected}")
        else:
            logger.warning(f"   ⚠️ {item['speaker']} 重译后仍不合规，保留原译文: {str(old)[:40]}")
    logger.info(f"🔧 [{tgt_lang}] 定点重译完成: 修正 {fixed}/{len(violations)} 条")
    return fixed


def _normalize_japanese_rule_token(value):
    return str(value or "").strip().lstrip("～〜~").strip()


def _contains_japanese_ending(text, token):
    return bool(re.search(rf"{re.escape(token)}(?=[。！？?!…]|$)", text))


def _contains_japanese_address(text, token):
    for match in re.finditer(re.escape(token), text):
        if re.fullmatch(r"[一-龯]+", token):
            previous = text[match.start() - 1:match.start()]
            following = text[match.end():match.end() + 1]
            if (previous and re.match(r"[一-龯]", previous)) or (
                following and re.match(r"[一-龯]", following)
            ):
                continue
        return True
    return False


def _japanese_style_violation_reasons(item, translated, styles):
    if item.get("beat_type") != "dialogue":
        return []
    characters = (styles or {}).get("characters") or {}
    info = characters.get(item["speaker"])
    if not _style_info_usable(info):
        return []
    ja = info.get("ja") if isinstance(info.get("ja"), dict) else {}
    reasons = []

    for value in ja.get("avoid_second_person") or []:
        token = _normalize_japanese_rule_token(value)
        if token and _contains_japanese_address(translated, token):
            reasons.append(f"禁止使用第二人称或称谓「{token}」")
    for value in ja.get("avoid_endings") or []:
        token = _normalize_japanese_rule_token(value)
        if token and _contains_japanese_ending(translated, token):
            reasons.append(f"禁止使用句末形式「{token}」")
    return reasons


def find_japanese_style_violations(trans_map, styles, items):
    if not styles:
        return []
    item_by_uid = {item["uid"]: item for item in items}
    violations = []
    for uid, translated in trans_map.items():
        item = item_by_uid.get(uid)
        if not item:
            continue
        reasons = _japanese_style_violation_reasons(item, str(translated), styles)
        if reasons:
            violations.append((item, translated, reasons))
    return violations


def repair_japanese_style_violations(trans_map, src_lang, styles, items):
    """修复可直接验证的日语禁用称谓和禁用句末形式。"""
    violations = find_japanese_style_violations(trans_map, styles, items)
    if not violations:
        logger.info("✅ [ja] 日语称谓与句末硬规则校验通过，无需重译")
        return 0

    logger.info(f"🔧 [ja] 检出 {len(violations)} 条称谓/句末违规，开始定点重译")
    fixed = 0
    for item, old, reasons in violations:
        guide = render_style_guide(styles, "ja", [item["speaker"]])
        guide += (
            "\n【最高优先级日语修订】保持原义、人物意图和既有敬语层级。"
            f"上一次译文是：{json.dumps(str(old), ensure_ascii=False)}。"
            "必须修正以下问题：" + "；".join(reasons)
        )
        new = _translate_single(item, src_lang, "ja", guide)
        remaining = _japanese_style_violation_reasons(item, new, styles)
        if new and new != item["content"] and len(remaining) < len(reasons):
            trans_map[item["uid"]] = new
            fixed += 1
        else:
            logger.warning(
                f"   ⚠️ {item['speaker']} 日语定点重译未改善，保留原译文: {str(old)[:50]}"
            )
    logger.info(f"🔧 [ja] 定点重译完成: 修正 {fixed}/{len(violations)} 条")
    return fixed


_EN_CONTRACTION_RE = re.compile(
    r"\b(?:I'm|you're|he's|she's|it's|we're|they're|I've|you've|we've|they've|"
    r"I'll|you'll|he'll|she'll|we'll|they'll|I'd|you'd|he'd|she'd|we'd|they'd|"
    r"isn't|aren't|wasn't|weren't|don't|doesn't|didn't|haven't|hasn't|hadn't|"
    r"won't|wouldn't|can't|couldn't|shouldn't|mustn't|let's|that's|there's|what's)\b",
    re.IGNORECASE,
)
_EN_STRONG_PROFANITY_RE = re.compile(
    r"\b(?:fuck(?:ing|ed|er|ers)?|shit(?:ty)?|bullshit|bitch(?:es)?|bastard(?:s)?|asshole(?:s)?)\b",
    re.IGNORECASE,
)
_US_TO_UK_SPELLINGS = {
    "color": "colour", "favor": "favour", "honor": "honour", "center": "centre",
    "theater": "theatre", "realize": "realise", "organize": "organise", "defense": "defence",
}


def _contains_english_phrase(text, phrase):
    phrase = str(phrase or "").strip()
    if not phrase:
        return False
    if re.fullmatch(r"[A-Za-z][A-Za-z' -]{0,60}", phrase):
        return bool(re.search(rf"(?<![A-Za-z]){re.escape(phrase)}(?![A-Za-z])", text, re.IGNORECASE))
    return phrase.casefold() in text.casefold()


def _english_style_violation_reasons(item, translated, styles):
    reasons = []
    normalized_text = translated.replace("’", "'")
    work = (styles or {}).get("work") or {}
    variant = str(work.get("en_variant") or "US").upper()
    spelling_pairs = (
        {uk: us for us, uk in _US_TO_UK_SPELLINGS.items()}
        if variant == "US" else _US_TO_UK_SPELLINGS
    )
    for forbidden, preferred in spelling_pairs.items():
        if _contains_english_phrase(normalized_text, forbidden):
            reasons.append(f"spelling must be {variant}: use {preferred}, not {forbidden}")

    characters = (styles or {}).get("characters") or {}
    info = characters.get(item["speaker"])
    if not _style_info_usable(info):
        return reasons
    en = info.get("en") if isinstance(info.get("en"), dict) else {}
    overrides = en.get("address_overrides") if isinstance(en.get("address_overrides"), list) else []
    contractions_vary = any(
        isinstance(rule, dict)
        and rule.get("contractions")
        and rule.get("contractions") != en.get("contractions")
        for rule in overrides
    )

    if (
        en.get("contractions") == "avoid"
        and not contractions_vary
        and _EN_CONTRACTION_RE.search(normalized_text)
    ):
        reasons.append("this character must avoid contractions")
    if en.get("profanity_level") in (None, "", "none", "mild") and _EN_STRONG_PROFANITY_RE.search(normalized_text):
        reasons.append(f"profanity exceeds the allowed level {en.get('profanity_level') or 'none'}")
    for phrase in en.get("avoid") or []:
        if _contains_english_phrase(normalized_text, phrase):
            reasons.append(f"forbidden word or phrase used: {phrase}")
    return reasons


def find_english_style_violations(trans_map, styles, items):
    if not styles:
        return []
    item_by_uid = {item["uid"]: item for item in items}
    violations = []
    for uid, translated in trans_map.items():
        item = item_by_uid.get(uid)
        if not item:
            continue
        reasons = _english_style_violation_reasons(item, str(translated), styles)
        if reasons:
            violations.append((item, translated, reasons))
    return violations


def repair_english_style_violations(trans_map, src_lang, styles, items):
    """对可静态验证的英语违规（缩写、禁用词、粗口、英美拼写）进行定点重译。"""
    violations = find_english_style_violations(trans_map, styles, items)
    if not violations:
        logger.info("✅ [en] 英语语体硬规则校验通过，无需重译")
        return 0

    logger.info(f"🔧 [en] 检出 {len(violations)} 条英语语体违规，开始定点重译")
    fixed = 0
    for item, old, reasons in violations:
        guide = render_style_guide(styles, "en", [item["speaker"]])
        guide += (
            "\n[HIGHEST PRIORITY ENGLISH REVISION] Keep the original meaning and character intent. "
            f"The previous translation was: {json.dumps(str(old), ensure_ascii=False)}. "
            "Correct all of these violations: " + "; ".join(reasons)
        )
        new = _translate_single(item, src_lang, "en", guide)
        remaining = _english_style_violation_reasons(item, new, styles)
        if new and new != item["content"] and len(remaining) < len(reasons):
            trans_map[item["uid"]] = new
            fixed += 1
        else:
            logger.warning(
                f"   ⚠️ {item['speaker']} 英语定点重译未改善，保留原译文: {str(old)[:50]}"
            )
    logger.info(f"🔧 [en] 定点重译完成: 修正 {fixed}/{len(violations)} 条")
    return fixed



def _build_system_prompt(src_lang, tgt_lang, count, style_guide, retry_note="",
                         dynamic_context=""):
    """组装翻译 system prompt：使用稳定 ID 回填，避免按分隔符切分导致错位。"""
    parts = [
        f"Translate from {_LANG_NAMES[src_lang]} to {_LANG_NAMES[tgt_lang]}.",
        f"Translate exactly the {count} objects in target_segments. context_before and context_after are "
        "read-only context and must not appear in the output.",
        "Use node_id, scene_id, speaker, beat_type and emotion only to resolve meaning and register.",
        "If a target segment includes mapped_target_name, it is the task-local rendering for that speaker. "
        "When the speaker/name is explicitly mentioned, use that rendering exactly; do not invent a new reading "
        "or switch between Chinese characters and alternative katakana spellings across segments. "
        "The name may be omitted when natural grammar permits, but identity must remain unchanged.",
        "Return one JSON object only: {\"translations\":[...rows...]}. For a normal target with content, "
        "its row is {\"id\":\"input id\",\"text\":\"translation\"}. For a target with content_parts, "
        "its row is {\"id\":\"input id\",\"parts\":[\"translated part 1\",\"translated part 2\"]}.",
        "Return every target id exactly once. Do not add, remove, merge or reorder ids.",
        "content_parts are separated control fields, not prose punctuation. Translate every part independently, "
        "return exactly the same number of parts in the same order, and never merge them or replace the boundary "
        "with punctuation. Do not include the literal control separator || inside any returned part.",
        "The text value must contain translation only and no metadata. Do not add a transport-level outer dialogue envelope; "
        "however, preserve and translate quotation marks that are part of the source sentence's meaning (including quoted names, "
        "titles, or citations).",
        # 首轮就下发这条硬约束。原来只有失败后的 retry_note 才提"别抄原文"，
        # 首轮完全没有约束；而整段回显源语言的产物会被 TTS 直接念出来。
        f"Every returned value must read as natural {_LANG_NAMES[tgt_lang]}, never as an untranslated "
        f"{_LANG_NAMES[src_lang]} sentence copied verbatim. Game UI markers, emotes, interjections, screenshot "
        f"placeholders and system notices must be rendered in {_LANG_NAMES[tgt_lang]} too, not passed through. "
        "If a term has no established target rendering, transliterate or paraphrase it rather than copying the "
        "source string unchanged. Proper nouns follow the mapped_target_name rule above.",
        "Preserve every explicit number, percentage, price and quantity relationship. For Chinese traditional "
        "weight units, use one coherent policy within the work (default: metric conversion) and never mix units "
        "arbitrarily between nearby lines.",
        "Preserve semantic viewpoint and discourse force: first/third person, who perceives or acts, negation, "
        "causal links, modality (certainty vs guess), aspect/tense, and singular/plural must not drift. "
        "Do not add an unstated reason, reaction, subject, or emotional judgment. Resolve pronouns from the local "
        "scene context, not from a generic default narrator.",
        "Prefer idiomatic target-language phrasing over literal calques, but keep every concrete fact. "
        "When a literal phrase sounds unnatural, rewrite the whole local phrase naturally instead of replacing "
        "one word mechanically. Preserve names, titles, quoted terms, and punctuation roles from the source.",
    ]

    if tgt_lang == "ja":
        parts.append(
            "Strict language consistency: every subtitle text must be Japanese. Translate ordinary Latin-letter words "
            "and phrases, including brand slogans, English quotations and interface labels; do not "
            "leave English such as 'Impossible is nothing', 'Just do it' or 'Opening Questions' in the Japanese "
            "output. Preserve source-authored proper names, group names, acronyms, city/class labels and other identity "
            "markers (for example X-Dream, In Vision, B市 or Fクラス) when they are part of the source identity. "
            "Japanese register: narration follows the work narration, orthography, punctuation, historical-language "
            "and dialect rules. nvl_thought follows the character thought style and normally uses natural plain form. "
            "For dialogue, separately obey politeness, respectful-language level, humble-language level, address terms, "
            "request style, command style, sentence endings, era style and dialect. Use respectful verbs for the "
            "addressee or superior's actions and humble verbs for the speaker or in-group's actions; do not treat both "
            "as a single generic 'keigo' setting. Apply an addressee override only when context clearly matches it. "
            "Japanese normally omits first- and second-person pronouns: when pronoun_omission=prefer, omit them whenever "
            "the sentence remains clear. Never insert 私は/俺は merely to display the configured pronoun. Avoid あなた "
            "when a name, title, role or omission is more natural. Do not invent stereotyped gendered endings or a "
            "dialect that is not specified."
        )
    elif tgt_lang == "en":
        parts.append(
            "Strict language consistency: every subtitle text must be English. Translate or transliterate all Chinese, "
            "Japanese and other CJK characters in dialogue, narration, names, UI labels and interjections; do not "
            "leave source-language characters embedded in an otherwise English sentence."
        )

    if style_guide:
        parts.append(
            f"Work style guide and per-character style table (MUST follow exactly):\n{style_guide}"
        )

    if dynamic_context:
        parts.append(dynamic_context)

    if retry_note:
        parts.append(retry_note)

    return "\n".join(parts)


def _translation_payload_item(item, tgt_lang=None):
    payload = {
        "id": item["uid"],
        "node_id": item["node_id"],
        "scene_id": item["scene_id"],
        "speaker": item["speaker"],
        "beat_type": item["beat_type"],
        "emotion": item.get("emotion") or "calm",
    }
    if tgt_lang:
        localized = item.get("localized_names") or {}
        target_name = str(
            item.get("mapped_target_name") or localized.get(tgt_lang) or ""
        ).strip()
        if target_name:
            payload["mapped_target_name"] = target_name
    content = item["content"]
    if CONTROL_SEPARATOR in content:
        content_parts = content.split(CONTROL_SEPARATOR)
        payload["content_parts"] = content_parts
        if len(content_parts) == 2:
            payload["content_part_roles"] = ["choice_option", "subtitle_body"]
    else:
        payload["content"] = content
    return payload


def _clean_translated_text(text, source_text=None):
    cleaned = _strip_meta_prefix(_strip_wrapping_quotes(text, source_text))
    # speaker/system 前缀由写盘层统一添加；模型偶发只回显一个冒号，若不剥离会生成
    # ``System：: ...``。正常译文本身不会以裸冒号开头。
    return re.sub(r"^\s*[:：]\s*", "", cleaned, count=1).strip()


def _clean_system_translation(text, source_text=None):
    """系统前缀由产物层负责，剥掉模型可能重复返回的 System/冒号。"""
    value = _clean_translated_text(text, source_text)
    return re.sub(r"^\s*(?:System|系统)\s*[:：-]+\s*", "", value, count=1, flags=re.IGNORECASE).strip()


def _strip_duplicate_speaker_prefix(text, beat_info, source_text=None):
    """Remove a transport prefix the model may have echoed in the text field."""
    value = str(text or "").strip()
    speaker = str(beat_info.get("final_speaker_id") or "").strip()
    if not speaker or speaker == "System":
        return value
    suffix = re.escape(str(beat_info.get("combination_suffix") or ""))
    emotion = re.escape(str(beat_info.get("emotion") or ""))
    patterns = [
        rf"^\s*{re.escape(speaker)}_{suffix}_{emotion}\s*[:：]\s*",
        rf"^\s*{re.escape(speaker)}\s*[:：]\s*",
    ]
    for pattern in patterns:
        if re.match(pattern, str(source_text or "").strip()):
            continue
        candidate = re.sub(pattern, "", value, count=1)
        if candidate != value:
            return candidate.strip()
    return value


def _control_structure_matches(source_text, translated_text):
    """译文必须保留与原文完全相同数量的 || 控制边界。"""
    return str(source_text or "").count(CONTROL_SEPARATOR) == str(
        translated_text or ""
    ).count(CONTROL_SEPARATOR)


# 事故级问题：产物根本不可用（空译文、整段还是源语言、控制符结构被破坏）。
# 这类必须死磕重试，因为留下来就是把中文念出来或让游戏解析失败。
UNTRANSLATED_REASON = "translation appears untranslated or echoes the source"
EMPTY_REASON = "translation is empty"
CONTROL_REASON = "control separator structure changed"
ACCIDENT_REASONS = frozenset({UNTRANSLATED_REASON, EMPTY_REASON, CONTROL_REASON})


def _is_accident_reason(reason):
    """区分事故与质量：事故必须死磕，质量重试一次不过就接受。"""
    return str(reason) in ACCIDENT_REASONS


def _translation_hard_violation_reasons(item, translated, src_lang, tgt_lang,
                                        dynamic_context=None):
    """Deterministic, source-grounded checks shared by retries and final QA."""
    source = str(item.get("content") or "")
    value = str(translated or "")
    reasons = []
    if not value.strip():
        reasons.append(EMPTY_REASON)
    if not _control_structure_matches(source, value):
        reasons.append(CONTROL_REASON)
    if _looks_untranslated(value, src_lang, tgt_lang, source, dynamic_context):
        reasons.append(UNTRANSLATED_REASON)
    return reasons


_EXPLICIT_NUMBER_RE = re.compile(r"(?<![A-Za-z])\d+(?:[,.]\d+)?%?(?![A-Za-z])")
_CN_QUANTITY_RE = re.compile(
    r"(?P<num>[零〇一二三四五六七八九十百千万两半]+)"
    r"(?P<unit>斤|两|克|公斤|千克|个|只|件|块|元|毛|万|千|百)"
)
_TARGET_QUANTITY_RE = re.compile(
    r"(?P<num>\d+(?:\.\d+)?(?:[・～〜-]\d+(?:\.\d+)?)?|"
    r"[〇零一二三四五六七八九十百千万两半]+(?:[・～〜-][〇零一二三四五六七八九十百千万两半]+)?)"
    r"(?P<unit>斤|両|キロ|kg|公斤|千克|グラム|g|個|件|元|円|ドル|％|%)",
    re.IGNORECASE,
)


def _parse_small_chinese_number(value):
    """Return one value or range endpoints for common subtitle numerals."""
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
              "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    value = str(value or "")
    if value == "半":
        return [0.5]
    compact_range = re.fullmatch(
        r"([一二两三四五六七八九])([一二两三四五六七八九])(十|百|千|万)",
        value,
    )
    if compact_range:
        scale_value = {"十": 10, "百": 100, "千": 1000, "万": 10000}[
            compact_range.group(3)
        ]
        return [
            float(digits[compact_range.group(1)] * scale_value),
            float(digits[compact_range.group(2)] * scale_value),
        ]
    if all(ch in digits for ch in value):
        return [float(digits[ch]) for ch in value]  # 四五 = a range
    scale = {"十": 10, "百": 100, "千": 1000, "万": 10000}
    total = section = number = 0
    for ch in value:
        if ch in digits:
            number = digits[ch]
        elif ch in scale:
            unit = scale[ch]
            if unit == 10000:
                section = (section + number) * unit
                total += section
                section = 0
            else:
                section += (number or 1) * unit
            number = 0
        else:
            return []
    return [float(total + section + number)]


def _quantity_facts(text, target=False):
    pattern = _TARGET_QUANTITY_RE if target else _CN_QUANTITY_RE
    value_text = str(text or "")
    if not target:
        value_text = re.sub(
            r"(?<!\d)(\d+)\s*(块|元)\s*(\d)(?!\d)",
            lambda m: f"{m.group(1)}.{m.group(3)}{m.group(2)}",
            value_text,
        )
    facts = []
    for match in pattern.finditer(value_text):
        raw_num = match.group("num")
        try:
            if any(sep in raw_num for sep in ("・", "～", "〜", "-")):
                values = []
                for part in re.split(r"[・～〜-]", raw_num):
                    values.extend(
                        [float(part.replace(",", ""))]
                        if part and part[0].isdigit()
                        else _parse_small_chinese_number(part)
                    )
            else:
                values = [float(raw_num.replace(",", ""))] if raw_num[0].isdigit() else _parse_small_chinese_number(raw_num)
        except ValueError:
            values = []
        if values:
            unit = match.group("unit").lower()
            if unit in {"斤", "両", "キロ", "kg", "公斤", "千克", "克", "グラム", "g"}:
                family = "mass"
                # Internal canonical unit: kilograms.
                factor = {"斤": 0.5, "両": 0.05, "克": 0.001, "g": 0.001,
                          "グラム": 0.001, "公斤": 1.0, "千克": 1.0,
                          "キロ": 1.0, "kg": 1.0}[unit]
            elif unit in {"元", "円", "ドル", "块", "毛"}:
                family, factor = "currency", (0.1 if unit == "毛" else 1.0)
            elif unit in {"个", "只", "件", "個"}:
                family, factor = "count", 1.0
            else:
                continue
            facts.append((family, [round(v * factor, 6) for v in values], match.group(0)))
    return facts


def _quantity_fact_violation_reasons(source_text, translated_text):
    """Catch concrete quantity/range drift, including Chinese ``四五斤`` forms."""
    source_facts = _quantity_facts(source_text)
    target_facts = _quantity_facts(translated_text, target=True)
    if not source_facts or not target_facts:
        return []
    reasons = []
    target_by_family = {}
    for target_family, values, _ in target_facts:
        target_by_family.setdefault(target_family, []).append(values)
    used_by_family = {}
    for family, expected, raw in source_facts:
        candidates = target_by_family.get(family) or []
        index = used_by_family.get(family, 0)
        if index >= len(candidates):
            continue  # wording-only translation or an intentionally omitted measure; QA model handles it
        actual = candidates[index]
        used_by_family[family] = index + 1
        if len(expected) > 1:
            if len(actual) < 2 or any(abs(actual[i] - expected[i]) > 1e-6 for i in range(2)):
                reasons.append(f"quantity/range changed: source {raw!r} -> target values {actual}")
        elif not any(abs(actual_value - expected[0]) <= 1e-6 for actual_value in actual):
            reasons.append(f"quantity changed: source {raw!r} -> target values {actual}")
    return reasons
def _normalized_explicit_numbers(text):
    """提取阿拉伯数字事实，忽略千位逗号，供确定性 QA 使用。"""
    value_text = str(text or "")
    values = []
    # Chinese price notation ``4块5`` / ``13元5`` means 4.5 / 13.5,
    # not two independent numeric facts.
    consumed = []
    for match in re.finditer(r"(?<!\d)(\d+)\s*(?:块|元)\s*(\d)(?!\d)", value_text):
        values.append(float(f"{match.group(1)}.{match.group(2)}"))
        consumed.append(match.span())

    def outside_consumed(start, end):
        return not any(start < b and end > a for a, b in consumed)

    for match in _EXPLICIT_NUMBER_RE.finditer(value_text):
        if not outside_consumed(*match.span()):
            continue
        raw = match.group(0)
        try:
            values.append(float(raw.replace(",", "").rstrip("%")))
        except ValueError:
            continue
    return values


def _explicit_number_violation_reasons(source_text, translated_text):
    """只拦截确定的数字丢失/改变，不把中文数词和英文拼写强行一一对应。"""
    source_numbers = _normalized_explicit_numbers(source_text)
    if not source_numbers:
        return []
    target_numbers = _normalized_explicit_numbers(translated_text)
    missing = []
    remaining = list(target_numbers)
    for value in source_numbers:
        match_index = next(
            (index for index, candidate in enumerate(remaining)
             if abs(candidate - value) < 1e-9),
            None,
        )
        if match_index is None:
            missing.append(value)
        else:
            remaining.pop(match_index)
    if not missing:
        return []
    return [f"explicit numeric facts missing or changed: {', '.join(str(v) for v in missing)}"]


def _parse_translation_response(raw_content, expected_items=None):
    content = (raw_content or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", content, re.DOTALL)
    if fenced:
        content = fenced.group(1)
    else:
        start, end = content.find("{"), content.rfind("}")
        if start < 0 or end < start:
            raise ValueError("翻译响应中没有 JSON 对象")
        content = content[start:end + 1]
    payload = json.loads(content)
    rows = payload.get("translations") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("翻译响应缺少 translations 数组")
    expected_by_id = {
        str(item.get("uid") or ""): item for item in (expected_items or [])
    }
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        uid = str(row.get("id") or "").strip()
        expected = expected_by_id.get(uid)
        source_text = str((expected or {}).get("content") or "")
        source_parts = source_text.split(CONTROL_SEPARATOR)

        translated = ""
        if expected and len(source_parts) > 1:
            returned_parts = row.get("parts")
            if isinstance(returned_parts, list) and len(returned_parts) == len(source_parts):
                cleaned_parts = [
                    _clean_translated_text(part, source_part)
                    for part, source_part in zip(returned_parts, source_parts)
                ]
                if all(cleaned_parts) and all(
                    CONTROL_SEPARATOR not in part for part in cleaned_parts
                ):
                    translated = CONTROL_SEPARATOR.join(cleaned_parts)
            # 兼容模型未按 parts schema 返回的情况：保留 text 交给后续
            # 结构校验定点重译，不在解析层把整块判为 ID 缺失。
            if not translated:
                translated = _clean_translated_text(
                    row.get("text"), source_text
                ) or INVALID_CONTROL_TRANSLATION
        else:
            translated = _clean_translated_text(row.get("text"), source_text)
        if expected and expected.get("speaker") == "System":
            translated = _clean_system_translation(translated, source_text)
        if uid and translated and uid not in result:
            result[uid] = translated
    return result


def _call_translate_api(items, src_lang, tgt_lang, style_guide="",
                        context_before=None, context_after=None,
                        retry_note="", temperature=0.1,
                        dynamic_context="", model=None):
    """
    单次 API 调用，返回切分后的译文列表（不做校验）。
    items: 带稳定 uid 的目标字幕记录；前后文只辅助消歧，不参与输出。
    retry_note/temperature: 重试时下发上一轮的失败原因并提高随机性，
    否则同一 prompt 会稳定复现同一结果，重试没有信息增量。
    """
    client = OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url=BASE_URL,
        timeout=SUBTITLE_LLM_TIMEOUT_SECONDS,
        max_retries=0,
    )
    payload = {
        "context_before": [_translation_payload_item(item, tgt_lang) for item in (context_before or [])],
        "target_segments": [_translation_payload_item(item, tgt_lang) for item in items],
        "context_after": [_translation_payload_item(item, tgt_lang) for item in (context_after or [])],
    }
    messages = [
        {"role": "system",
         "content": _build_system_prompt(
             src_lang, tgt_lang, len(items), style_guide, retry_note,
             dynamic_context
         )},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}
    ]
    if model:
        # 指定模型时绕过 ModelFallback：救援阶段要自己控制换哪个模型、换几次，
        # 不能让共享的 fallback 状态机改写选择。
        resp = client.chat.completions.create(
            model=model, messages=messages, temperature=temperature
        )
        used_model = model
    else:
        resp, used_model = _model_fallback.chat(
            client, messages=messages, temperature=temperature,
        )
    if _usage_tracker and getattr(resp, "usage", None):
        try:
            _usage_tracker.add_llm_usage(
                resp.usage, used_model, provider="openai"
            )
        except Exception as exc:
            logger.warning(f"⚠️ API 用量记录失败，不影响翻译结果：{exc}")
    return _parse_translation_response(resp.choices[0].message.content or "", items)


class ResumeCache:
    """
    输入寻址的断点缓存：既服务进程中断，也服务产物缺失后的定点重建。

    缓存只按严格输入摘要复用，不按 task_id 复用：
    - key 含 style_guide 与模型名，语体表一改即整体失效；
    - 只写通过校验的条目，逐条兜底失败退回原文的结果绝不入缓存；
    - key 含代码缓存版本；翻译逻辑变化时递增版本即可使旧缓存失效；
    - 成功后保留在共享 task 目录，字幕文件缺失时无需再次消耗模型额度。
    """

    def __init__(self, task_id, context_digest=""):
        task_root = os.path.join(OUTPUT_DIR, str(task_id))
        self.root = os.path.join(task_root, TRANSLATE_CACHE_DIRNAME)
        legacy_root = os.path.join(task_root, LEGACY_TRANSLATE_CACHE_DIRNAME)
        # 旧执行的隐藏缓存目录直接改名沿用，避免白丢一批已付费的译文。
        if os.path.isdir(legacy_root) and not os.path.exists(self.root):
            try:
                os.replace(legacy_root, self.root)
                logger.info(f"👁️ 旧隐藏翻译缓存已改为可见目录：{self.root}")
            except OSError as e:
                logger.warning(f"⚠️ 旧翻译缓存改名失败，将重建：{e}")
        self.context_digest = str(context_digest or "")
        self.hits = 0
        self.enabled = True
        try:
            os.makedirs(self.root, exist_ok=True)
        except OSError as e:
            self.enabled = False
            logger.warning(f"⚠️ 断点缓存不可用，本次不做续跑保护: {e}")

    def key_for(self, items, src_lang, tgt_lang, style_guide, context_digest=""):
        digest = hashlib.sha256()
        for part in (TRANSLATE_CACHE_VERSION, src_lang, tgt_lang,
                     MODEL, file_digest(__file__), style_guide or "",
                     str(context_digest or self.context_digest)):
            digest.update(part.encode("utf-8"))
            digest.update(b"\x00")
        for item in items:
            digest.update(f"{item['uid']}\x01{item['content']}\x00".encode("utf-8"))
        return digest.hexdigest()

    def _path(self, tgt_lang, key):
        return os.path.join(self.root, tgt_lang, f"{key}.json")

    def load(self, tgt_lang, key, expected_uids):
        """读取缓存块；内容不完整或已损坏时视为未命中。"""
        if not self.enabled:
            return None
        try:
            with open(self._path(tgt_lang, key), "r", encoding="utf-8") as f:
                entries = json.load(f)
        except (OSError, ValueError):
            return None
        if not isinstance(entries, dict) or set(entries) != set(expected_uids):
            return None
        if not all(isinstance(v, str) and v.strip() for v in entries.values()):
            return None
        self.hits += 1
        return entries

    def save(self, tgt_lang, key, entries):
        """先写临时文件再原子替换，避免中断时留下半个 JSON 被后续命中。"""
        if not self.enabled or not entries:
            return
        path = self._path(tgt_lang, key)
        tmp = f"{path}.{os.getpid()}.tmp"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(entries, f, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError as e:
            logger.warning(f"⚠️ 断点缓存写入失败（不影响本次翻译）: {e}")
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError as cleanup_error:
                    logger.warning(
                        f"⚠️ 断点缓存临时文件清理失败（不影响本次翻译）："
                        f"{tmp}: {cleanup_error}"
                    )

    def discard(self):
        """产物写盘成功后清空，确保缓存不会跨运行固化译文。"""
        if not self.enabled or not os.path.exists(self.root):
            return
        try:
            shutil.rmtree(self.root)
        except OSError as e:
            logger.warning(f"⚠️ 断点缓存清理失败，请手动删除 {self.root}: {e}")


def _retry_temperature(attempt):
    """首轮低温求稳，重试提高随机性，避免确定性地复现同一条坏译文。"""
    return 0.1 if attempt <= 1 else 0.6


def _untranslated_retry_note(bad_items, src_lang, tgt_lang, reasons_by_uid=None):
    """把上一轮未通过校验的原文和具体失败原因回传给模型。

    只说"别抄原文"信息量太小：控制符结构、数字、实体身份各有各的修法。带上
    逐条原因，模型才知道该改什么，重试才有信息增量。
    """
    reasons_by_uid = reasons_by_uid or {}
    details = []
    for item in bad_items[:5]:
        source = json.dumps(item["content"][:60], ensure_ascii=False)
        reasons = reasons_by_uid.get(item.get("uid"))
        details.append(
            f"{source} -> {'; '.join(reasons)}" if reasons else source
        )
    return (
        f"RETRY: the previous attempt produced {len(bad_items)} invalid segment(s). "
        f"Rewrite every segment fully in {_LANG_NAMES[tgt_lang]}; "
        "for content_parts, return the parts array with exactly the same length and order, without merging parts. "
        f"Failed segments and why: {'; '.join(details)}. "
        f"Never copy the source wording; output must be {_LANG_NAMES[tgt_lang]}."
    )


def _translate_single(item, src_lang, tgt_lang, style_guide="", dynamic_context=""):
    """单条定点重译，供语体修复复用。失败返回空串，交由上层统一救援。"""
    source_text = item["content"]
    request_item = dict(item)
    if isinstance(dynamic_context, dict):
        for entity in dynamic_context.get("entities") or []:
            if str(entity.get("speaker_id") or "") == str(item.get("speaker") or ""):
                request_item["mapped_target_name"] = str(entity.get("target_name") or "")
                break
        rendered_context = _render_dynamic_translation_context(dynamic_context)
    else:
        rendered_context = dynamic_context
    retry_note = ""
    attempts = max(TRANSLATE_VALIDATE_RETRIES + 1, 1)
    for attempt in range(1, attempts + 1):
        try:
            translated = _call_translate_api(
                [request_item], src_lang, tgt_lang, style_guide,
                retry_note=retry_note, temperature=_retry_temperature(attempt),
                dynamic_context=rendered_context,
            )
            cand = translated.get(item["uid"], "").strip()
            if cand and not _translation_hard_violation_reasons(
                item, cand, src_lang, tgt_lang,
                dynamic_context if isinstance(dynamic_context, dict) else None,
            ):
                return cand
            retry_note = _untranslated_retry_note([item], src_lang, tgt_lang)
        except Exception as e:
            logger.warning(f"⚠️ 单条翻译异常 (尝试 {attempt}/{attempts}): {e}")
            time.sleep(min(2 * attempt, 6))
    logger.error(f"❌ 单条翻译最终失败，留空等待上层救援: {source_text[:40]}...")
    return ""


def _rescue_translate_one(item, src_lang, tgt_lang, style_guide, dynamic_context):
    """单条最终救援：换模型 + 长退避，尽一切努力拿到目标语言译文。

    走到这里说明整块和二分都失败了。只剩一条台词时请求体最小、最容易成功，
    而放弃的代价是源语言原文进产物并被 TTS 念出来。

    重试预算按事故性质分配：
    - 仍是源语言 → 每个模型按 TRANSLATE_RESCUE_ATTEMPTS 次预算尝试，然后换下一个
      模型。同一模型连续失败多半是它对这条内容的固有行为，换模型才有信息增量。
    - 空译文/控制符损坏 → 同模型内继续重试，不换模型（换了也是同样的结构问题）。
    - 纯质量问题 → 拿到目标语言文本立即接受，不再重试。

    返回译文，或在彻底无望时返回 None 由调用方决定如何处置。
    """
    rendered = (
        _render_dynamic_translation_context(dynamic_context)
        if isinstance(dynamic_context, dict) else (dynamic_context or "")
    )
    request_item = dict(item)
    if isinstance(dynamic_context, dict):
        for entity in dynamic_context.get("entities") or []:
            if str(entity.get("speaker_id") or "") == str(item.get("speaker") or ""):
                request_item["mapped_target_name"] = str(entity.get("target_name") or "")
                break
    best_effort = None
    retry_note = ""
    label = f"{item['scene_id']}-{item['beat_id']}"
    for model_index, model_name in enumerate(TRANSLATE_RESCUE_MODELS):
        if model_index:
            logger.warning(
                f"🔀 [{tgt_lang}] {label} 在 {TRANSLATE_RESCUE_MODELS[model_index - 1]} 上 "
                f"{TRANSLATE_RESCUE_ATTEMPTS} 次仍是源语言，换用 {model_name} 重试"
            )
        for attempt in range(1, TRANSLATE_RESCUE_ATTEMPTS + 1):
            try:
                translated = _call_translate_api(
                    [request_item], src_lang, tgt_lang, style_guide,
                    retry_note=retry_note, temperature=_retry_temperature(attempt),
                    dynamic_context=rendered, model=model_name,
                )
            except Exception as exc:
                # 指数退避但设上限：中转站限流/抖动往往几十秒就恢复，一直等下去
                # 会拖垮整步，等到上限仍失败就换模型或交给下一次 attempt。
                wait_seconds = min(2 ** attempt, TRANSLATE_RESCUE_MAX_BACKOFF)
                logger.warning(
                    f"⚠️ [{tgt_lang}] {label} 救援请求失败 "
                    f"({model_name} {attempt}/{TRANSLATE_RESCUE_ATTEMPTS})，"
                    f"{wait_seconds:.0f}s 后重试：{exc}"
                )
                time.sleep(wait_seconds)
                continue
            candidate = str(translated.get(item["uid"]) or "").strip()
            if not candidate:
                retry_note = _untranslated_retry_note([item], src_lang, tgt_lang)
                continue
            reasons = _translation_hard_violation_reasons(
                item, candidate, src_lang, tgt_lang,
                dynamic_context if isinstance(dynamic_context, dict) else None,
            )
            if not reasons:
                if model_index or attempt > 1:
                    logger.info(
                        f"✅ [{tgt_lang}] {label} 救援成功"
                        f"（{model_name} 第 {attempt} 次）"
                    )
                return candidate
            # Keep the newest candidate.  A later model/temperature may have
            # produced a better target-language attempt even when the local
            # detector still rejects it; the caller will record it for review.
            best_effort = candidate
            # 只有"仍是源语言"才值得换模型继续磕：它意味着产物不可用。其余问题
            # 拿到目标语言文本就收下——再试未必更好，额度留给真正的事故。
            item_reasons = {item["uid"]: reasons}
            if UNTRANSLATED_REASON not in reasons:
                if any(_is_accident_reason(reason) for reason in reasons):
                    retry_note = _untranslated_retry_note(
                        [item], src_lang, tgt_lang, item_reasons
                    )
                    continue
                logger.warning(
                    f"⚠️ [{tgt_lang}] {label} 译文质量存疑但已是目标语言，"
                    f"接受并记录待复核：{'；'.join(reasons)}"
                )
                return candidate
            retry_note = _untranslated_retry_note(
                [item], src_lang, tgt_lang, item_reasons
            )
    if best_effort:
        logger.error(
            f"❌ [{tgt_lang}] {label} 已用尽 {list(TRANSLATE_RESCUE_MODELS)} "
            f"各 {TRANSLATE_RESCUE_ATTEMPTS} 次仍是源语言，采用最后一次模型译文待复核"
        )
    return best_effort


def _safe_rescue_translate_one(item, src_lang, tgt_lang, style_guide, dynamic_context):
    """执行单条救援且把普通 worker 异常收敛为缺译文。

    救援是在 ``ThreadPoolExecutor`` 中运行的。即使 API 调用本身已经捕获了
    ``Exception``，上下文渲染、响应结构或第三方 SDK 的边界异常仍可能从 worker
    冒出；直接使用 ``executor.map`` 会在消费结果时重新抛出，进而中断整个 Step 2。
    普通异常应记录并交给统一的硬校验/报告流程，只有 KeyboardInterrupt、SystemExit
    等控制流异常才允许继续向上冒泡。
    """
    try:
        return _rescue_translate_one(
            item, src_lang, tgt_lang, style_guide, dynamic_context
        )
    except Exception as exc:
        logger.error(
            f"❌ [{tgt_lang}] 单条翻译救援 worker 异常，留空并继续："
            f"{item.get('scene_id')}-{item.get('beat_id')}: {exc}"
        )
        return ""


def _translate_chunk(items, src_lang, tgt_lang, style_guide="",
                     context_before=None, context_after=None, cache=None,
                     dynamic_context=None):
    """
    翻译一个 chunk，带重试与校验。
    - ID 不匹配 -> 整块重试（无法判断哪几条错位）
    - 部分疑似未翻译 -> 只重译这几条，已合格的译文保留
    - 重试耗尽后，对仍未合格的条目并发逐条兜底，杜绝用源文整段填充
    items: 当前场景中的连续目标字幕记录。
    返回 (译文映射, 是否整块合格)。未合格的块不写断点缓存。
    """
    rendered_context = _render_dynamic_translation_context(dynamic_context)
    mapped_names = {
        str(entity.get("speaker_id")): str(entity.get("target_name") or "")
        for entity in (dynamic_context or {}).get("entities", [])
    }

    def enrich_name(item):
        value = dict(item)
        mapped = mapped_names.get(str(item.get("speaker") or ""))
        if mapped:
            value["mapped_target_name"] = mapped
        return value

    request_before = [enrich_name(item) for item in (context_before or [])]
    request_after = [enrich_name(item) for item in (context_after or [])]
    context_digest = stable_digest(dynamic_context or {})
    cache_key = cache.key_for(
        items, src_lang, tgt_lang, style_guide, context_digest
    ) if cache else None
    if cache_key:
        cached = cache.load(tgt_lang, cache_key, {item["uid"] for item in items})
        if cached is not None:
            invalid = [
                item for item in items
                if _translation_hard_violation_reasons(
                    item, cached.get(item["uid"], ""), src_lang, tgt_lang,
                    dynamic_context,
                )
            ]
            if not invalid:
                return cached, True
            cache.hits = max(0, cache.hits - 1)
            logger.warning(
                f"⚠️ [{tgt_lang}] 忽略 {len(invalid)} 条语言/结构校验失败的断点缓存，重新翻译"
            )

    accepted = {}
    pending = list(items)
    retry_note = ""
    # 多条块的瞬时错误只试一次就转去二分：这类失败多半源于请求体过大，重发同样
    # 大小的块通常还是失败，而每层递归都用满 3 次预算会让调用量翻好几倍。
    # 单条已无法再拆，才用完整重试预算。
    transient_left = TRANSLATE_MAX_RETRIES if len(pending) <= 1 else 1
    validate_left = TRANSLATE_VALIDATE_RETRIES
    attempt = 0
    latest = {}
    last_transient_error = None

    while pending and (transient_left > 0 or validate_left >= 0):
        attempt += 1
        try:
            translated = _call_translate_api(
                [enrich_name(item) for item in pending], src_lang, tgt_lang, style_guide,
                request_before, request_after,
                retry_note=retry_note, temperature=_retry_temperature(attempt),
                dynamic_context=rendered_context,
            )
        except Exception as e:
            transient_left -= 1
            last_transient_error = e
            logger.warning(
                f"⚠️ 翻译请求异常 ({tgt_lang}, 剩余重试 {transient_left}): {e}"
            )
            if transient_left <= 0:
                break
            time.sleep(min(2 * attempt, 6))
            continue

        expected_ids = {item["uid"] for item in pending}
        if set(translated) != expected_ids:
            transient_left -= 1
            last_transient_error = RuntimeError(
                f"字幕 ID 不匹配：期望 {len(expected_ids)} 得到 {len(translated)}"
            )
            logger.warning(
                f"⚠️ 字幕 ID 不匹配 ({tgt_lang}, 剩余重试 {transient_left}): "
                f"期望 {len(expected_ids)} 得到 {len(translated)}"
            )
            if transient_left <= 0:
                break
            time.sleep(1)
            continue

        latest.update(translated)
        bad_items = []
        bad_reasons = {}
        for item in pending:
            text = translated[item["uid"]]
            reasons = _translation_hard_violation_reasons(
                item, text, src_lang, tgt_lang, dynamic_context
            )
            if reasons:
                bad_items.append(item)
                bad_reasons[item["uid"]] = reasons
            else:
                accepted[item["uid"]] = text

        pending = bad_items
        if not pending:
            if cache_key:
                cache.save(tgt_lang, cache_key, accepted)
            return accepted, True

        # 校验预算独立于瞬时错误：重发同样内容仍被判失败，多半是判定规则不适配该条，
        # 继续重试只会白烧 token，因此额度用尽即停。
        if validate_left <= 0:
            break
        validate_left -= 1
        logger.warning(
            f"⚠️ 检测到 {len(bad_items)} 条未通过翻译/结构校验 ({tgt_lang})，"
            f"仅重译这 {len(bad_items)} 条（剩余重译额度 {validate_left}）"
        )
        retry_note = _untranslated_retry_note(
            bad_items, src_lang, tgt_lang, bad_reasons
        )
        time.sleep(1)

    # 瞬时错误耗尽且一条译文都没拿到：整块请求从未成功。这类失败多半是块太大
    # 引起的超时/连接中断，先二分成更小的块重试；拆到单条仍失败也只留空，
    # 交由统一救援和报告处理，绝不把异常抛回主流程。
    # 绝不能退回原文——那会让中文静默流进目标语言产物，一路念进配音。
    if pending and not latest:
        if len(pending) > 1:
            logger.warning(
                f"⚠️ [{tgt_lang}] 整块 {len(pending)} 条翻译始终失败，"
                f"二分拆块重试：{last_transient_error}"
            )
            middle = len(pending) // 2
            merged = {}
            all_clean = True
            for part in (pending[:middle], pending[middle:]):
                part_map, part_clean = _translate_chunk(
                    part, src_lang, tgt_lang, style_guide,
                    context_before=context_before, context_after=context_after,
                    cache=cache, dynamic_context=dynamic_context,
                )
                merged.update(part_map)
                all_clean = all_clean and part_clean
            accepted.update(merged)
            if cache_key and all_clean:
                cache.save(tgt_lang, cache_key, accepted)
            return accepted, all_clean
        # 拆到单条仍失败：留空不写该 uid，交由 translate_texts 收尾的逐条救援统一
        # 处理。厚预算重试只放在那一处，否则递归拆块时每条都重试一轮，服务整体
        # 不可用时会把 Step 2 拖上几小时。绝不用源语言原文冒充译文。
        logger.warning(
            f"⚠️ [{tgt_lang}] {pending[0]['scene_id']}-{pending[0]['beat_id']} "
            f"常规重试耗尽，留空待收尾救援：{last_transient_error}"
        )
        return accepted, False

    # 模型返回了译文但判据仍判不合格：保留模型结果，不退回原文。判据可能误伤，
    # 问题条目由上层记录报告，但不阻止正式字幕和音频链继续运行。
    if pending:
        logger.error(
            f"❌ [{tgt_lang}] {len(pending)} 条未通过未翻译校验，"
            f"采用模型最后一次返回并标记待人工复核："
        )
        for item in pending:
            fallback = latest.get(item["uid"])
            if not fallback:
                # 没有任何模型译文可用：留空交由上层兜底，不写源语言原文。
                continue
            accepted[item["uid"]] = fallback
            logger.error(
                f"   - {item['scene_id']}-{item['beat_id']} ({item['speaker']}) "
                f"原文 {item['content'][:30]!r} -> 译文 {fallback[:40]!r}"
            )
    # 未通过校验的块不入缓存：否则一条可疑译文会在重跑时被直接命中
    clean = not pending
    if cache_key and clean:
        cache.save(tgt_lang, cache_key, accepted)
    return accepted, clean


def translate_texts(items, src_lang, tgt_lang, style_guide="", chunk_size=None,
                    styles=None, cache=None, dynamic_context_by_scene=None,
                    rescue_attempted_uids=None):
    """
    按连续 node/scene 片段并发翻译，返回 {uid: 译文}。
    chunk_size: 不传则按目标语言取值（英语提示词更大，用更小的块）。
    styles: character_styles 数据。传入时按 chunk 只下发本块出现的角色，
            避免整表十几行摊薄模型注意力导致人称/语体被忽略。
    cache: ResumeCache，用于中断后按块续跑。
    """
    if not items:
        return {}

    if chunk_size is None:
        chunk_size = TRANSLATE_CHUNK_SIZE_BY_LANG.get(tgt_lang, TRANSLATE_CHUNK_SIZE)

    chunks = build_scene_chunks(items, chunk_size)
    results = [None] * len(chunks)
    hits_before = cache.hits if cache else 0

    def guide_for(chunk_spec):
        if not styles:
            return style_guide
        visible = chunk_spec["before"] + chunk_spec["targets"] + chunk_spec["after"]
        speakers = list(dict.fromkeys(item["speaker"] for item in visible))
        try:
            return render_style_guide(styles, tgt_lang, speakers) or style_guide
        except Exception as exc:
            # 角色语体表是增强信息，不应因格式损坏阻断翻译；退回本块通用
            # 指引，后续仍由语言/结构硬校验兜底。
            logger.warning(
                f"⚠️ [{tgt_lang}] 角色语体表渲染失败，使用通用规则继续：{exc}"
            )
            return style_guide

    def context_for(chunk_spec):
        if not dynamic_context_by_scene:
            return None
        target = chunk_spec["targets"]
        key = (target[0]["node_id"], target[0]["scene_id"]) if target else None
        return dynamic_context_by_scene.get(key)

    with ThreadPoolExecutor(max_workers=TRANSLATE_MAX_WORKERS) as executor:
        # 只保持有限数量的请求在队列中。旧实现会为所有 chunk 立即 submit；
        # 字幕达到数万条时会积累数千个 future，并在上游限流恢复前持续占用内存。
        future_map = {}
        next_idx = 0

        def submit_one(idx):
            spec = chunks[idx]
            future_map[executor.submit(
                _translate_chunk,
                spec["targets"], src_lang, tgt_lang, guide_for(spec),
                spec["before"], spec["after"], cache,
                context_for(spec),
            )] = idx

        while next_idx < len(chunks) and len(future_map) < TRANSLATE_MAX_IN_FLIGHT:
            submit_one(next_idx)
            next_idx += 1
        done = 0
        dirty = 0
        while future_map:
            completed, _ = wait(tuple(future_map), return_when=FIRST_COMPLETED)
            for future in completed:
                idx = future_map.pop(future)
                try:
                    results[idx], clean = future.result()
                except Exception as exc:
                    # 预期外异常不能中断整个 Step 2，但也不能用源语言原文冒充译文。
                    # 留空本块，由下面的缺失兜底统一救援。
                    targets = chunks[idx]["targets"]
                    results[idx] = {}
                    clean = False
                    logger.error(
                        f"❌ [{tgt_lang}] 翻译块发生未捕获异常，"
                        f"本块 {len(targets)} 条留空待救援：{exc}"
                    )
                if not clean:
                    dirty += 1
                done += 1
                logger.info(f"   翻译进度 [{tgt_lang}]: {done}/{len(chunks)} 块完成")
                if next_idx < len(chunks):
                    submit_one(next_idx)
                    next_idx += 1

    lang_hits = (cache.hits - hits_before) if cache else 0
    if lang_hits:
        logger.info(f"   ♻️ [{tgt_lang}] 命中断点缓存 {lang_hits}/{len(chunks)} 块，已跳过")
    if dirty:
        logger.warning(f"   ⚠️ [{tgt_lang}] {dirty} 个块含未通过校验的条目，不写入断点缓存")

    merged = {}
    for part in results:
        merged.update(part)

    # 最后一道救援：逐条把仍然缺失的条目翻出来。前面所有路径宁可留空也不写源文，
    # 缺口都收敛到这里统一处理，保证「不中断」与「不污染」同时成立。
    missing = [item for item in items if not str(merged.get(item["uid"]) or "").strip()]
    if missing:
        logger.warning(
            f"⚠️ [{tgt_lang}] 分块结束后仍有 {len(missing)} 条缺译文，逐条救援中…"
        )
        rescued_count = 0
        consecutive_failures = 0
        with ThreadPoolExecutor(max_workers=TRANSLATE_MAX_WORKERS) as rescue_pool:
            for batch_start in range(0, len(missing), TRANSLATE_MAX_WORKERS):
                batch = missing[batch_start:batch_start + TRANSLATE_MAX_WORKERS]
                if rescue_attempted_uids is not None:
                    rescue_attempted_uids.update(item["uid"] for item in batch)
                outcomes = list(rescue_pool.map(
                    lambda item: (item, _safe_rescue_translate_one(
                        item, src_lang, tgt_lang,
                        guide_for({"before": [], "targets": [item], "after": []}),
                        (dynamic_context_by_scene or {}).get(
                            (item["node_id"], item["scene_id"])
                        ),
                    )),
                    batch,
                ))
                for item, rescued in outcomes:
                    if rescued:
                        merged[item["uid"]] = rescued
                        rescued_count += 1
                        consecutive_failures = 0
                    else:
                        consecutive_failures += 1
                if consecutive_failures >= TRANSLATE_RESCUE_CIRCUIT_BREAK:
                    logger.error(
                        f"❌ [{tgt_lang}] 连续 {consecutive_failures} 条救援均失败，"
                        "判定为翻译服务整体不可用，停止救援；"
                        f"剩余 {len(missing) - rescued_count - consecutive_failures} "
                        "条留空，由报告记录并在下一次运行继续重试"
                    )
                    break
        logger.info(
            f"🔧 [{tgt_lang}] 逐条救援完成：{rescued_count}/{len(missing)} 条已补齐"
        )
    return merged


def build_scene_chunks(items, chunk_size, context_size=3):
    """按连续的 node_id + scene_id 分段，再切目标块并附带场景内前后文。"""
    if not items:
        return []
    segments = []
    current = []
    current_key = None
    for item in items:
        key = (item["node_id"], item["scene_id"])
        if current and key != current_key:
            segments.append(current)
            current = []
        current_key = key
        current.append(item)
    if current:
        segments.append(current)

    chunks = []
    for segment in segments:
        for start in range(0, len(segment), chunk_size):
            end = min(start + chunk_size, len(segment))
            chunks.append({
                "before": segment[max(0, start - context_size):start],
                "targets": segment[start:end],
                "after": segment[end:min(len(segment), end + context_size)],
            })
    return chunks


def _parse_quality_review(raw_content, expected_items):
    """解析审校模型结果；格式异常时返回空结果，不阻断字幕生产。"""
    try:
        content = (raw_content or "").strip()
        start, end = content.find("{"), content.rfind("}")
        if start < 0 or end < start:
            return []
        payload = json.loads(content[start:end + 1])
        rows = payload.get("reviews") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return []
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    expected = {str(item.get("uid")): item for item in expected_items}
    result = []
    for row in rows:
        if not isinstance(row, dict) or str(row.get("id")) not in expected:
            continue
        severity = str(row.get("severity") or "none").lower()
        try:
            confidence = float(row.get("confidence", 0) or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        raw_repairable = row.get("repairable", False)
        if isinstance(raw_repairable, str):
            raw_repairable = raw_repairable.strip().lower() in {"1", "true", "yes"}
        result.append({
            "id": str(row["id"]),
            "verdict": str(row.get("verdict") or "pass").lower(),
            "severity": severity,
            "confidence": max(0.0, min(confidence, 1.0)),
            "issues": [str(v) for v in (row.get("issues") or []) if str(v).strip()],
            "repair_instruction": str(row.get("repair_instruction") or "").strip(),
            "issue_type": str(row.get("issue_type") or "").strip().lower(),
            "repairable": bool(raw_repairable),
        })
    return result


def _neighbor_context(items, position_by_uid, targets, context_size=2):
    """从全量字幕里取候选条目的场景内相邻台词，供审校判断视角与指代。

    只取同 node+scene 的邻居，且排除已经在 targets 里的条目，避免同一条既作
    审校目标又作上下文。
    """
    target_uids = {item["uid"] for item in targets}
    before_positions = set()
    after_positions = set()
    for item in targets:
        center = position_by_uid.get(item["uid"])
        if center is None:
            continue
        scene = (item["node_id"], item["scene_id"])
        for offset in range(1, context_size + 1):
            for position, bucket in (
                (center - offset, before_positions),
                (center + offset, after_positions),
            ):
                if not 0 <= position < len(items):
                    continue
                neighbor = items[position]
                if (neighbor["node_id"], neighbor["scene_id"]) != scene:
                    continue
                if neighbor["uid"] in target_uids:
                    continue
                bucket.add(position)
    before = [items[position] for position in sorted(before_positions)]
    after = [items[position] for position in sorted(after_positions)]
    return before, after


def _quality_review_candidates(items, translations, src_lang, tgt_lang,
                               dynamic_context_by_scene=None):
    """挑出值得送 LLM 复审的条目，全部判据都是本地零成本计算。

    命中任一条即入选：
    - 硬校验有 reason（控制符、数字、实体、漏译回显）
    - 译文/原文长度比异常：整体缩水或膨胀往往意味着漏译或增译
    - 译文为空
    这些判据抓不到"通顺但语义拧了"的译文，所以另有抽样兜底（见调用方）。
    """
    scene_contexts = dynamic_context_by_scene or {}
    candidates = []
    for item in items:
        translated = str(translations.get(item["uid"]) or "")
        if not translated.strip():
            candidates.append(item)
            continue
        if _translation_hard_violation_reasons(
            item, translated, src_lang, tgt_lang,
            scene_contexts.get((item["node_id"], item["scene_id"])),
        ):
            candidates.append(item)
            continue
        source_len = _word_len(item["content"])
        target_len = _word_len(translated)
        if source_len >= QA_LENGTH_RATIO_MIN_LEN and target_len:
            ratio = target_len / source_len
            if ratio < QA_LENGTH_RATIO_LOW or ratio > QA_LENGTH_RATIO_HIGH:
                candidates.append(item)
    return candidates


def review_translation_quality(items, translations, src_lang, tgt_lang,
                               dynamic_context_by_scene=None):
    """用独立审校请求检查事实、实体、数字和自然度；不使用固定术语表。

    审校模型只能报告问题，只有 high/critical 且置信度足够高的项目才会进入
    定点重译，避免把合法的语言变体当成错误强行替换。
    """
    if not TRANSLATION_QA_ENABLED or not items:
        return []
    # 只审本地判据挑出的可疑条目。本地判据零成本且已覆盖控制符结构、数字单位、
    # 实体身份、语域和漏译回显；逐条送 LLM 复审这些已经查过的维度纯属浪费——
    # 4048 条会产生 200+ 次串行请求、耗时两小时以上，而真正需要改的只有几十条。
    suspicious = _quality_review_candidates(
        items, translations, src_lang, tgt_lang, dynamic_context_by_scene
    )
    # 只审本地判据挑出的可疑条目，不做全量抽样：抽样命中的多是 medium/low，
    # 按修复门槛基本只记录不修，而且没有任何代码消费"抽样水位"这个信号，
    # 等于花钱买一份没人读的报告。
    candidates = suspicious
    if not candidates:
        logger.info(f"✅ [{tgt_lang}] 本地判据未发现可疑译文，跳过 LLM 质量审校")
        return []
    logger.info(
        f"🔎 [{tgt_lang}] LLM 审校范围 {len(candidates)}/{len(items)} 条可疑译文，"
        f"预计 {-(-len(candidates) // TRANSLATION_QA_CHUNK_SIZE)} 次请求"
    )
    chunks = build_scene_chunks(candidates, TRANSLATION_QA_CHUNK_SIZE, context_size=2)
    # 候选是稀疏的，build_scene_chunks 只能在候选之间取上下文，结果恒为空。
    # 审校要判断视角、代词指代和语域，必须给真实的场景相邻台词，所以上下文
    # 从全量 items 里按位置重新取。
    position_by_uid = {item["uid"]: index for index, item in enumerate(items)}
    reports = []
    client = OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url=BASE_URL,
        timeout=SUBTITLE_LLM_TIMEOUT_SECONDS,
        max_retries=0,
    )
    for spec in chunks:
        targets = spec["targets"]
        if not targets:
            continue
        scene_key = (targets[0]["node_id"], targets[0]["scene_id"])
        scene_context = (dynamic_context_by_scene or {}).get(scene_key) or {}
        context_before, context_after = _neighbor_context(
            items, position_by_uid, targets
        )
        mapped_by_speaker = {
            str(entity.get("speaker_id")): str(entity.get("target_name") or "")
            for entity in (scene_context.get("entities") or [])
            if entity.get("speaker_id")
        }
        payload = {
            "context_before": [
                {"id": x["uid"], "speaker": x["speaker"], "content": x["content"]}
                for x in context_before
            ],
            "segments": [
                {
                    "id": x["uid"], "node_id": x["node_id"], "scene_id": x["scene_id"],
                    "speaker": x["speaker"], "beat_type": x["beat_type"],
                    "source": x["content"], "translation": translations.get(x["uid"], ""),
                    "mapped_target_name": mapped_by_speaker.get(x["speaker"], ""),
                }
                for x in targets
            ],
            "context_after": [
                {"id": x["uid"], "speaker": x["speaker"], "content": x["content"]}
                for x in context_after
            ],
        }
        context_note = _render_dynamic_translation_context(
            scene_context
        )
        prompt = (
            f"Review {_LANG_NAMES[src_lang]} to {_LANG_NAMES[tgt_lang]} translations. "
            "Judge meaning, entity identity, numbers/units, omissions/additions, control structure, "
            "and character register. Do not enforce a fixed glossary or one preferred wording: "
            "natural synonyms are acceptable. Return JSON only: "
            "{\"reviews\":[{\"id\":\"...\",\"verdict\":\"pass|repair|review\","
            "\"severity\":\"none|low|medium|high|critical\",\"confidence\":0.0,"
            "\"issue_type\":\"entity|fact|viewpoint|format|style|other\","
            "\"repairable\":true,\"issues\":[\"...\"],\"repair_instruction\":\"...\"}]}\n"
            "Only mark high/critical when there is concrete evidence of a factual or structural error.\n"
            "Set repairable=true only for a concrete, local correction that preserves the source meaning; "
            "do not auto-repair a merely subjective style preference.\n"
            + context_note
        )
        try:
            resp, used_model = _model_fallback.chat(
                client,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                temperature=0.0,
            )
            rows = _parse_quality_review(resp.choices[0].message.content or "", targets)
        except Exception as exc:
            logger.warning(f"⚠️ [{tgt_lang}] 翻译审校请求失败，保留当前译文: {exc}")
            continue
        for row in rows:
            item = next((x for x in targets if x["uid"] == row["id"]), None)
            if not item:
                continue
            row.update({
                "node_id": item["node_id"], "scene_id": item["scene_id"],
                "beat_id": item["beat_id"], "speaker": item["speaker"],
                "source": item["content"], "translation": translations.get(item["uid"], ""),
                "target_lang": tgt_lang, "model": used_model,
            })
            if row["severity"] in {"medium", "high", "critical"} or row["verdict"] != "pass":
                reports.append(row)
    return reports


def repair_translation_quality(trans_map, items, src_lang, tgt_lang,
                               dynamic_context_by_scene=None):
    reports = review_translation_quality(
        items, trans_map, src_lang, tgt_lang, dynamic_context_by_scene
    )
    repairs = 0
    item_by_uid = {item["uid"]: item for item in items}
    for row in reports:
        if repairs >= TRANSLATION_QA_MAX_REPAIRS:
            break
        repairable_medium = (
            row.get("severity") == "medium"
            and row.get("repairable")
            and row.get("issue_type") in {"entity", "fact", "viewpoint", "format"}
            and row.get("confidence", 0.0) >= TRANSLATION_QA_REPAIR_MIN_CONFIDENCE
        )
        if (
            not (
                row["severity"] in {"high", "critical"}
                or repairable_medium
            )
            or row["confidence"] < 0.85
        ):
            continue
        item = item_by_uid.get(row["id"])
        if not item:
            continue
        scene_key = (item["node_id"], item["scene_id"])
        guide = (
            "QUALITY REPAIR: preserve the source meaning, entities, numbers, units, "
            "speaker intent and control structure. Do not apply mechanical word replacement. "
            "Concrete reviewer findings: " + "; ".join(row["issues"])
        )
        if row.get("repair_instruction"):
            guide += " Required correction: " + row["repair_instruction"]
        guide += (
            " The previous candidate was: "
            + json.dumps(str(row.get("translation") or trans_map.get(item["uid"], "")), ensure_ascii=False)
            + ". Rewrite the source into the target language; do not mention this review."
        )
        new = _translate_single(
            item, src_lang, tgt_lang, guide,
            dynamic_context=(dynamic_context_by_scene or {}).get(scene_key),
        )
        if new and new != item["content"] and not _translation_hard_violation_reasons(
            item, new, src_lang, tgt_lang,
            (dynamic_context_by_scene or {}).get(scene_key),
        ):
            trans_map[item["uid"]] = new
            row["repaired"] = True
            row["repaired_translation"] = new
            repairs += 1
        else:
            row["repaired"] = False
        logger.warning(
            f"⚠️ [{tgt_lang}] QA {row['severity']} scene={item['scene_id']} "
            f"beat={item['beat_id']} confidence={row['confidence']:.2f} "
            f"issues={'；'.join(row['issues'])}"
        )
    return reports


def translate_all(items, src_lang, tgt_lang, style_guide="", styles=None, cache=None,
                  dynamic_context_by_scene=None, rescue_attempted_uids=None):
    """
    全量翻译，只复用本次中断遗留的断点缓存，不做跨运行的翻译记忆。
    items: 每次出现均保留的字幕记录。
    返回 {uid: 译文} 映射。
    """
    logger.info(f"🔄 翻译到 {tgt_lang}: 共 {len(items)} 条（按场景全量翻译）")
    return translate_texts(
        items, src_lang, tgt_lang, style_guide, styles=styles, cache=cache,
        dynamic_context_by_scene=dynamic_context_by_scene,
        rescue_attempted_uids=rescue_attempted_uids,
    )




def fetch_plot_data(target_task_id=None):
    task_id = target_task_id

    try:
        with get_platform_engine().connect() as conn:
            if not task_id:
                logger.info("🔍 未指定 Task ID，正在查找最新生成的剧情...")
                sql_latest = text("SELECT task_id FROM plots ORDER BY id DESC LIMIT 1")
                result = conn.execute(sql_latest).fetchone()

                if not result:
                    logger.error("❌ plots 表为空，无法提取。")
                    return None, None
                task_id = result[0]
                logger.warning(f"⚠️ 自动锁定最新任务: {task_id}")
            else:
                logger.info(f"🚀 [Subtitle] 锁定指定任务: {task_id}")

            sql_data = text("SELECT data FROM plots WHERE task_id = :tid")
            result_data = conn.execute(sql_data, {"tid": task_id}).fetchone()

            if result_data:
                return task_id, result_data[0]

            logger.warning(f"⚠️ 未在 plots 表中找到 task_id='{task_id}' 的数据，尝试读取本地文件...")
    except Exception as e:
        logger.warning(f"⚠️ 数据库读取失败: {e}，尝试读取本地文件...")

    if task_id:
        fallback_path = os.path.join(OUTPUT_DIR, str(task_id), "plots.json")
        if os.path.exists(fallback_path):
            try:
                with open(fallback_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, UnicodeError, ValueError, TypeError) as exc:
                # 本地文件存在但损坏时仍归类为前置 plots 数据不可用，
                # 不让 JSONDecodeError 以普通异常打断并触发无意义重试。
                logger.error(f"❌ 本地 plots.json 无法读取或解析: {fallback_path}: {exc}")
            else:
                logger.info(f"📁 [Subtitle] 从本地文件加载数据: {fallback_path}")
                return task_id, data

    logger.error(f"❌ 数据库和本地文件均未找到 task_id='{task_id}' 的 plots 数据")
    return None, None


def fetch_name_mappings(target_task_id=None):
    data = None
    if target_task_id:
        try:
            with get_platform_engine().connect() as conn:
                sql = text("SELECT data FROM name_mappings WHERE task_id = :tid")
                row = conn.execute(sql, {"tid": target_task_id}).fetchone()
                if row:
                    data = row[0]
        except Exception as e:
            logger.warning(f"⚠️ name_mappings 数据库读取失败: {e}，尝试本地文件...")

    if data is None and target_task_id:
        fallback_path = os.path.join(
            OUTPUT_DIR, str(target_task_id), "name_mappings.json"
        )
        if os.path.exists(fallback_path):
            try:
                with open(fallback_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                logger.info(f"📁 [Subtitle] 从本地加载 name_mappings: {fallback_path}")
            except Exception as e:
                logger.warning(f"⚠️ name_mappings 本地读取失败: {e}")

    if data is None:
        logger.info("ℹ️ 未找到 name_mappings，使用默认转换规则。")
        return {}

    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception as e:
            logger.warning(f"⚠️ name_mappings JSON 解析失败: {e}")
            return {}

    conversion = get_source_to_change_conversion(data)
    logger.info(f"✅ [Subtitle] 已加载 conversion 映射 {len(conversion)} 条。")
    return conversion


def fetch_localized_name_mappings(target_task_id=None):
    """读取当前任务的语言名映射，仅作为动态上下文证据，不做文本替换。"""
    data = None
    if target_task_id:
        try:
            with get_platform_engine().connect() as conn:
                row = conn.execute(
                    text("SELECT data FROM name_mappings WHERE task_id = :tid"),
                    {"tid": target_task_id},
                ).fetchone()
                if row:
                    data = row[0]
        except Exception as exc:
            logger.warning(f"⚠️ 本地化 name_mappings 数据库读取失败，尝试本地文件: {exc}")
    if data is None and target_task_id:
        path = os.path.join(OUTPUT_DIR, str(target_task_id), "name_mappings.json")
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
        except Exception as exc:
            logger.warning(f"⚠️ 本地化 name_mappings 文件读取失败: {exc}")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return {}
    if not isinstance(data, dict):
        return {}
    return {
        language: get_localized_names(data, language)
        for language in ("en-US", "ja-JP")
    }


class PlotsDataMissingError(RuntimeError):
    """audio_producer 唯一允许中断主流程的业务前置异常。"""


def _process_subtitles(target_task_id=None):
    task_id, data = fetch_plot_data(target_task_id)
    if not data:
        raise PlotsDataMissingError(
            f"plots 表和本地 plots.json 均缺少任务数据："
            f"task_id={target_task_id or task_id}"
        )

    # task_id 在本步骤内才确定（全新生成模式下 main.py 尚未设置上下文），
    # 故在此绑定，保证字幕翻译的 LLM 消耗能归属到当前任务。
    if _usage_tracker and task_id:
        try:
            _usage_tracker.set_context(task_id, "audio_producer")
        except Exception as exc:
            logger.warning(f"⚠️ API 用量上下文设置失败，继续字幕流程：{exc}")

    logger.info(f"🎬 开始提取字幕... (Task: {task_id})")

    name_conversion = fetch_name_mappings(task_id)
    localized_name_data = fetch_localized_name_mappings(task_id)

    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception as e:
            raise PlotsDataMissingError(
                f"plots 数据不是合法 JSON：task_id={task_id}: {e}"
            ) from e
    if not isinstance(data, dict):
        raise PlotsDataMissingError(
            f"plots 数据根节点必须是对象：task_id={task_id}，实际为 {type(data).__name__}"
        )

    # First pass: parse all beats. 每次出现都保留，不能让相同短句跨场景复用译文。
    parsed_nodes = []  # list of (node_id, list of beat_info dicts)
    translation_items = []
    # variable_update 是剧情控制数据：不进入 LLM 翻译，但仍必须输出
    # 字幕文件，供下游按 beat_id 对齐。
    nontranslated_items = []

    for node_id, node_beats in data.items():
        timeline = node_beats
        if isinstance(node_beats, dict) and 'script_timeline' in node_beats:
            timeline = node_beats['script_timeline']
        if not isinstance(timeline, list):
            logger.warning(f"⚠️ 跳过节点 {node_id}: 数据格式非列表")
            continue

        beats_info = []
        for beat in timeline:
            if not isinstance(beat, dict):
                logger.warning(
                    f"⚠️ 跳过节点 {node_id} 中的非对象 beat，继续处理其余字幕"
                )
                continue
            raw_content = (beat.get('content') or '').strip().replace('\n', ' ').replace('\r', '')
            beat_type = beat.get('beat_type')

            if beat_type in ['system_panel', 'cg'] or not raw_content:
                continue

            beat_id = str(beat.get('beat_id', 1)).strip()

            raw_combination = beat.get('speaker_combination')
            combination_suffix = "1-1-1"
            if raw_combination:
                parts = str(raw_combination).split("-", 1)
                if len(parts) == 2 and parts[1].strip():
                    combination_suffix = parts[1].strip()

            raw_speaker_id = beat.get('speaker_id')
            raw_full_name = beat.get('full_name')
            raw_speaker_name = beat.get('speaker_name')
            raw_speaker_id_lower = str(raw_speaker_id).strip().lower()
            raw_full_name_lower = str(raw_full_name).strip().lower()

            is_system = (raw_speaker_id_lower in ['xitong', 'system', '系统']) or \
                        (raw_full_name_lower in ['xitong', 'system', '系统'])

            if is_system:
                final_speaker_id = "System"
            elif beat_type == 'dialogue':
                if raw_speaker_id == 'PangBai':
                    final_speaker_id = "PangBai"
                elif raw_full_name and str(raw_full_name).strip() in name_conversion:
                    final_speaker_id = name_conversion[str(raw_full_name).strip()]
                elif raw_speaker_name and str(raw_speaker_name).strip() in name_conversion:
                    final_speaker_id = name_conversion[str(raw_speaker_name).strip()]
                else:
                    final_speaker_id = normalize_speaker_smart(
                        raw_speaker_id
                        if _usable_speaker_value(raw_speaker_id)
                        else raw_speaker_name,
                        raw_full_name
                        if _usable_speaker_value(raw_full_name)
                        else raw_speaker_name,
                        fallback_key=f"{node_id}:{beat.get('scene_id') or 'UnknownScene'}:{beat_id}",
                    )
            elif beat_type == 'nvl_thought':
                if raw_speaker_id == 'PangBai':
                    final_speaker_id = "PangBai"
                elif raw_full_name and str(raw_full_name).strip() in name_conversion:
                    final_speaker_id = name_conversion[str(raw_full_name).strip()]
                elif raw_speaker_name and str(raw_speaker_name).strip() in name_conversion:
                    final_speaker_id = name_conversion[str(raw_speaker_name).strip()]
                else:
                    final_speaker_id = normalize_speaker_smart(
                        raw_speaker_id
                        if _usable_speaker_value(raw_speaker_id)
                        else raw_speaker_name,
                        raw_full_name
                        if _usable_speaker_value(raw_full_name)
                        else raw_speaker_name,
                        fallback_key=f"{node_id}:{beat.get('scene_id') or 'UnknownScene'}:{beat_id}",
                    )
            else:
                final_speaker_id = "PangBai"

            final_scene_id = normalize_scene_id(beat.get('scene_id', 'UnknownScene'))
            emotion = beat.get('emotion', 'calm') or "calm"
            uid = f"subtitle-{len(translation_items) + len(nontranslated_items) + 1}"

            beat_info = {
                "uid": uid,
                "beat_id": beat_id,
                "beat_type": beat_type,
                "content": raw_content,
                "final_speaker_id": final_speaker_id,
                "combination_suffix": combination_suffix,
                "final_scene_id": final_scene_id,
                "emotion": emotion,
            }
            beats_info.append(beat_info)
            item = {
                "uid": uid,
                "node_id": str(node_id),
                "scene_id": final_scene_id,
                "beat_id": beat_id,
                "content": raw_content,
                "speaker": final_speaker_id,
                "beat_type": beat_type,
                "emotion": emotion,
            }
            if beat_type == "variable_update":
                nontranslated_items.append(item)
            else:
                translation_items.append(item)

        parsed_nodes.append((node_id, beats_info))

    if not translation_items and not nontranslated_items:
        raise PlotsDataMissingError(
            f"plots 中没有可翻译的有效字幕内容：task_id={task_id}"
        )

    # 仅用需要翻译的自然语言文本检测源语言；variable_update 的控制字段
    # （effects_a={} 等）不能影响语言判断。极端情况下任务只有控制节点时，
    # 按默认中文源语言处理并仍输出两种目标目录。
    src_lang = (
        detect_language([item["content"] for item in translation_items])
        if translation_items else "zh"
    )
    logger.info(f"🌐 检测到语言: {src_lang}")

    # 按角色分组抽样，仅在 character_styles 缺失时用于降级分析
    style_items = [
        (item["content"], item["speaker"], item["beat_type"])
        for item in translation_items
    ]
    sample_lines, speakers = build_style_samples(style_items)

    # 优先用 info_extractor 从原文提取的角色语体表，缺失才退回现场分析
    char_styles = fetch_character_styles(task_id)
    if char_styles:
        stored_mapping_hash = (char_styles.get("metadata") or {}).get("name_mapping_hash")
        if name_conversion:
            current_mapping_payload = json.dumps(
                name_conversion, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            current_mapping_hash = hashlib.sha256(
                current_mapping_payload.encode("utf-8")
            ).hexdigest()
        else:
            current_mapping_hash = None
        if stored_mapping_hash and current_mapping_hash is None:
            logger.warning(
                "⚠️ 当前任务缺少 name_mappings，无法验证 character_styles 映射版本；"
                "将仅使用 plots 中已能直接对齐 speaker_id 的角色语体"
            )
        elif stored_mapping_hash and stored_mapping_hash != current_mapping_hash:
            logger.warning(
                "⚠️ character_styles 使用的 name_mappings 已变化；将仅使用仍能按 speaker_id 对齐的语体"
            )
        style_characters = char_styles.get("characters") or {}
        covered = [s for s in speakers if _style_info_usable(style_characters.get(s))]
        missing = [s for s in speakers if not _style_info_usable(style_characters.get(s))]
        logger.info(
            f"🎭 已加载原文角色语体表: 覆盖 {len(covered)}/{len(speakers)} 个有台词的角色"
        )
        if missing:
            logger.warning(
                f"⚠️ 以下角色不在语体表中，将由模型自行推断: {'、'.join(missing)}"
            )
    else:
        logger.warning("⚠️ 未找到 character_styles 记录，降级为按字幕样本现场分析语域")

    # 仅基于当前任务的 plots、人物映射和场景构建动态上下文；不加载/合并任何
    # 跨小说固定术语表。候选实体会在 prompt 中标为 hypothesis，模型必须按语境取舍。
    # 每种目标语言必须使用自己的名称映射。过去这里固定传 en-US，导致日语模型
    # 看到 Zhou Hang / Zheng Liwei 后自行猜出多套片假名读法。
    localized_key_by_lang = {"en": "en-US", "ja": "ja-JP"}
    dynamic_context_by_lang_and_scene = {}
    scene_keys = list(dict.fromkeys(
        (item["node_id"], item["scene_id"]) for item in translation_items
    ))
    for target_lang in [lang for lang in LANG_DIRS if lang != src_lang]:
        localized_names = localized_name_data.get(
            localized_key_by_lang.get(target_lang, "")
        ) or {}
        per_scene = {}
        for scene_key in scene_keys:
            scene_items = [
                item for item in translation_items
                if (item["node_id"], item["scene_id"]) == scene_key
            ]
            per_scene[scene_key] = build_dynamic_translation_context(
                scene_items, name_conversion=name_conversion,
                localized_names=localized_names, styles=char_styles
            )
        dynamic_context_by_lang_and_scene[target_lang] = per_scene
    # 内部场景索引使用 tuple 便于精确查找，但断点摘要最终要经过 JSON
    # 序列化；先将 tuple 键转换为稳定字符串，避免 ``keys must be str...``。
    context_digest_payload = {
        lang: {
            f"{scene_key[0]}\x1f{scene_key[1]}": context
            for scene_key, context in scene_map.items()
        }
        for lang, scene_map in dynamic_context_by_lang_and_scene.items()
    }
    context_digest = stable_digest(context_digest_payload)

    task_root = os.path.join(OUTPUT_DIR, str(task_id))
    expected_subtitles = []
    expected_subtitle_records = []
    for node_id, beats_info in parsed_nodes:
        for bi in beats_info:
            filename = f"{bi['final_scene_id']}-{bi['beat_id']}.txt"
            for lang, lang_dir in LANG_DIRS.items():
                path = os.path.join(task_root, node_id, "audios", lang_dir, filename)
                expected_subtitles.append(path)
                expected_subtitle_records.append((path, bi, lang))
    step_digest = stable_digest(
        STEP_CACHE_VERSION,
        data,
        name_conversion,
        char_styles,
        LANG_DIRS,
        MODEL,
        TRANSLATE_CACHE_VERSION,
        TRANSLATION_CONTEXT_VERSION,
        context_digest,
        file_digest(__file__),
    )
    resume_state = ResumeState(task_root)
    same_step_inputs = (
        resume_state.get_step("subtitles").get("input_digest") == step_digest
    )
    if (
        expected_subtitles
        and resume_state.step_matches("subtitles", step_digest)
        and all(
            _subtitle_file_is_canonical(path, beat_info, lang)
            for path, beat_info, lang in expected_subtitle_records
        )
    ):
        logger.info(
            f"♻️ Step 2 输入未变且 {len(expected_subtitles)} 个字幕产物全部可解析，"
            "跳过翻译与重写"
        )
        return task_id

    # 中文可继续作为源文本，但生产产物只包含配置中的日语和英语。
    trans_maps = {
        src_lang: {item["uid"]: item["content"] for item in translation_items}
    }
    resume_cache = ResumeCache(task_id, context_digest)
    rescue_attempted_by_lang = {}
    for tgt_lang in [l for l in LANG_DIRS if l != src_lang]:
        if not translation_items:
            # 任务可能只有 variable_update 控制节点；此时没有任何自然语言
            # 需要翻译，避免向 LLM 发空批次，同时保留 C 字幕写盘流程。
            trans_maps[tgt_lang] = {}
            rescue_attempted_by_lang[tgt_lang] = set()
            continue
        try:
            rescue_attempted_uids = set()
            rescue_attempted_by_lang[tgt_lang] = rescue_attempted_uids
            if char_styles:
                style_guide = render_style_guide(char_styles, tgt_lang, speakers)
            else:
                style_guide = analyze_style_guide(sample_lines, speakers, src_lang, tgt_lang)
            if style_guide:
                logger.info(f"📖 [{tgt_lang}] 生效的角色语体表:\n{style_guide}")
            trans_maps[tgt_lang] = translate_all(
                translation_items, src_lang, tgt_lang, style_guide,
                styles=char_styles, cache=resume_cache,
                dynamic_context_by_scene=dynamic_context_by_lang_and_scene.get(tgt_lang) or {},
                rescue_attempted_uids=rescue_attempted_uids,
            )
        except Exception as exc:
            # 单个目标语言的调度/上下文异常不能抹掉另一语言已经得到的结果。
            # 当前语言统一以缺译文进入后续旧译文复用、硬校验报告和 partial 发布。
            trans_maps[tgt_lang] = {}
            logger.exception(
                f"❌ [{tgt_lang}] 翻译阶段发生未捕获异常，保留为空并继续其它语言：{exc}"
            )
        # 翻译后的质量审校/风格重译已停用。当前只保留后面的硬性安全检查：
        # 空译文、控制符结构和源语言残留；语义、数字、实体及自然度问题按用户要求接受。

    # variable_update 不参与翻译，也不进入质量检查；在写盘前把原文注入
    # 每种语言的 map，确保 -C1/-C2… 文件仍按统一格式生成。
    nontranslated_by_uid = {
        item["uid"]: item["content"] for item in nontranslated_items
    }
    for lang in LANG_DIRS:
        trans_maps.setdefault(lang, {}).update(nontranslated_by_uid)

    expected_uids = {item["uid"] for item in translation_items}
    item_by_uid = {item["uid"]: item for item in translation_items}
    # 上一次成功发布的译文，作为救援彻底失败时的回退源：旧译文虽然可能不是最新
    # 语体，但它是目标语言，远好过把中文原文念出来。
    previous_translation_by_uid = {}
    for node_id, beats_info in parsed_nodes:
        for bi in beats_info:
            filename = f"{bi['final_scene_id']}-{bi['beat_id']}.txt"
            for lang, lang_dir in LANG_DIRS.items():
                if lang == src_lang:
                    continue
                path = os.path.join(task_root, node_id, "audios", lang_dir, filename)
                try:
                    raw = Path(path).read_text("utf-8").strip()
                except (OSError, UnicodeError):
                    continue
                body = raw.split("：", 1)[1].strip() if "：" in raw else ""
                # 存盘时 _format_subtitle_file_text 会补上「」外壳，回读要剥掉，
                # 否则复用时会套成「「…」」。
                if body.startswith("「") and body.endswith("」"):
                    body = body[1:-1].strip()
                if body:
                    previous_translation_by_uid[(lang, bi["uid"])] = body
    for lang in LANG_DIRS:
        lang_map = trans_maps.setdefault(lang, {})
        translated_uids = set(lang_map)
        missing_uids = expected_uids - translated_uids
        if missing_uids:
            # 分块、二分、逐条厚救援全部失败。绝不能把源语言原文当译文发布；
            # 优先沿用上一次成功发布的目标语言译文，没有就留空，最终由硬校验
            # 报告并阻止本次字幕发布。
            reused = 0
            for uid in missing_uids:
                previous = previous_translation_by_uid.get((lang, uid))
                item = item_by_uid.get(uid)
                previous_ok = bool(
                    previous and item and not _translation_hard_violation_reasons(
                        item, previous, src_lang, lang,
                        (dynamic_context_by_lang_and_scene.get(lang) or {}).get(
                            (item["node_id"], item["scene_id"])
                        ),
                    )
                )
                if previous_ok:
                    lang_map[uid] = previous
                    reused += 1
                else:
                    lang_map[uid] = ""
            logger.error(
                f"❌ [{lang}] 逐条救援后仍缺 {len(missing_uids)} 条译文："
                f"沿用旧译文 {reused} 条，"
                f"无旧译文的 {len(missing_uids) - reused} 条留空（见报告）"
            )

    # 控制符结构问题只记录，不再清空或阻止发布。用户要求硬校验只作为
    # 诊断信号；模型已经返回的译文必须原样进入字幕产物和后续音频链。
    for lang in LANG_DIRS:
        structural_errors = [
            item for item in translation_items
            if not _control_structure_matches(
                item["content"], trans_maps[lang][item["uid"]]
            )
        ]
        if structural_errors:
            examples = ", ".join(
                f"{item['scene_id']}-{item['beat_id']}" for item in structural_errors[:10]
            )
            logger.error(
                f"[{lang}] {len(structural_errors)} 条字幕的 {CONTROL_SEPARATOR} 控制符结构不一致，"
                f"仅记录报告并继续发布：{examples}"
            )

    # 兜底失败会留空；仅校验 uid 齐全不能代替语言残留检查。
    for lang in LANG_DIRS:
        if lang == src_lang:
            continue
        scene_contexts = dynamic_context_by_lang_and_scene.get(lang) or {}
        leftovers = [
            item for item in translation_items
            if _looks_untranslated(
                trans_maps[lang][item["uid"]], src_lang, lang, item["content"],
                scene_contexts.get((item["node_id"], item["scene_id"])),
            )
        ]
        if leftovers:
            logger.error(
                f"❌ [{lang}] {len(leftovers)}/{len(translation_items)} 条仍是源语言，"
                "将带原文写入产物，需人工复核："
            )
            for item in leftovers[:20]:
                logger.error(
                    f"   - {item['scene_id']}-{item['beat_id']} "
                    f"({item['speaker']}): {item['content'][:50]}"
                )
            if len(leftovers) > 20:
                logger.error(f"   ... 另有 {len(leftovers) - 20} 条")

    # Write a deterministic, source-grounded report even when the LLM reviewer
    # is disabled or unavailable.  This gives operators exact scene/beat
    # coordinates for any residual fact, identity, structure, or untranslated
    # failure instead of burying it in a generic aggregate log.
    hard_violation_total = 0
    # 硬校验问题先在本次运行内定点重译；无论是否修复，都保留最终译文并继续发布。
    for lang in LANG_DIRS:
        if lang == src_lang:
            continue
        scene_contexts = dynamic_context_by_lang_and_scene.get(lang) or {}
        pending_fix = [
            item for item in translation_items
            if item["uid"] not in rescue_attempted_by_lang.get(lang, set())
            if _translation_hard_violation_reasons(
                item, trans_maps[lang].get(item["uid"], ""), src_lang, lang,
                scene_contexts.get((item["node_id"], item["scene_id"])),
            )
        ]
        if not pending_fix:
            continue
        logger.warning(
            f"🔧 [{lang}] 硬校验检出 {len(pending_fix)} 条问题，先定点重译，完成后继续发布"
        )
        fixed = 0
        with ThreadPoolExecutor(max_workers=TRANSLATE_MAX_WORKERS) as fix_pool:
            def fix_one(item):
                try:
                    guide = (
                        render_style_guide(char_styles, lang, [item["speaker"]])
                        if char_styles else ""
                    )
                    context = scene_contexts.get((item["node_id"], item["scene_id"]))
                    return item, _safe_rescue_translate_one(
                        item, src_lang, lang, guide, context
                    )
                except Exception as exc:
                    # 单条修复是尽力而为；某条上下文或语体数据损坏时，
                    # 不能让 map() 把异常重新抛回主线程而中断整批字幕。
                    logger.error(
                        f"❌ [{lang}] 硬校验定点修复 worker 异常，保留原译文继续："
                        f"{item.get('scene_id')}-{item.get('beat_id')}: {exc}"
                    )
                    return item, None
            for item, new_text in fix_pool.map(fix_one, pending_fix):
                if not new_text:
                    continue
                if _translation_hard_violation_reasons(
                    item, new_text, src_lang, lang,
                    scene_contexts.get((item["node_id"], item["scene_id"])),
                ):
                    continue
                trans_maps[lang][item["uid"]] = new_text
                fixed += 1
        logger.info(
            f"🔧 [{lang}] 硬校验定点重译完成：修好 {fixed}/{len(pending_fix)} 条"
        )

    for lang in LANG_DIRS:
        if lang == src_lang:
            continue
        hard_violations = []
        scene_contexts = dynamic_context_by_lang_and_scene.get(lang) or {}
        for item in translation_items:
            translated = trans_maps[lang].get(item["uid"], "")
            reasons = _translation_hard_violation_reasons(
                item, translated, src_lang, lang,
                scene_contexts.get((item["node_id"], item["scene_id"])),
            )
            if reasons:
                hard_violations.append({
                    "id": item["uid"],
                    "node_id": item["node_id"],
                    "scene_id": item["scene_id"],
                    "beat_id": item["beat_id"],
                    "speaker": item["speaker"],
                    "source": item["content"],
                    "translation": translated,
                    "reasons": reasons,
                })
        if hard_violations:
            hard_violation_total += len(hard_violations)
            report_path = os.path.join(task_root, f"translation-hard-violations-{lang}.json")
            try:
                atomic_write_text(
                    report_path,
                    json.dumps({
                        "schema_version": 1,
                        "task_id": task_id,
                        "target_language": lang,
                        "model": MODEL,
                        "violations": hard_violations,
                    }, ensure_ascii=False, indent=2),
                )
            except OSError as exc:
                logger.warning(f"⚠️ [{lang}] 硬校验报告写入失败: {exc}")
            logger.error(
                f"❌ [{lang}] 最终仍有 {len(hard_violations)} 条硬校验问题，"
                f"详见 {report_path}；本次仍写入正式字幕"
            )
        else:
            stale_report = os.path.join(task_root, f"translation-hard-violations-{lang}.json")
            try:
                if os.path.exists(stale_report):
                    os.remove(stale_report)
            except OSError:
                pass

    # 硬校验问题不再阻断整条流水线。报告落盘后仍发布本次得到的全部字幕文本，
    # 包括未通过校验的译文；Step 4/5 照常扫描全部文件。Step 2 标记为 partial，
    # 下一次同 task_id 运行时不会命中全量成功断点，会继续修复报告中的问题条目。
    if hard_violation_total:
        logger.error(
            f"❌ 字幕翻译残留 {hard_violation_total} 条硬校验问题，已写报告；"
            "本次按 partial 发布全部字幕，下一次运行将继续重试"
        )

    # 先写临时目录，单节点全部成功后再替换正式目录，避免半套字幕覆盖旧产物。
    total_files = 0
    for node_id, beats_info in parsed_nodes:
        audios_dir = os.path.join(OUTPUT_DIR, str(task_id), node_id, "audios")
        if same_step_inputs:
            for bi in beats_info:
                for lang, lang_dir in LANG_DIRS.items():
                    part_content = trans_maps[lang][bi["uid"]]
                    file_text = _format_subtitle_file_text(bi, part_content, lang)
                    filename = f"{bi['final_scene_id']}-{bi['beat_id']}.txt"
                    file_path = os.path.join(audios_dir, lang_dir, filename)
                    try:
                        current_text = Path(file_path).read_text("utf-8")
                    except OSError:
                        current_text = None
                    if current_text != file_text:
                        atomic_write_text(file_path, file_text)
                    total_files += 1
            continue

        temp_dirs = {
            lang: os.path.join(audios_dir, f"{lang_dir}.building")
            for lang, lang_dir in LANG_DIRS.items()
        }
        try:
            for temp_dir in temp_dirs.values():
                if os.path.exists(temp_dir):
                    shutil.rmtree(temp_dir)
                os.makedirs(temp_dir, exist_ok=True)

            for bi in beats_info:
                for lang, lang_dir in LANG_DIRS.items():
                    part_content = trans_maps[lang][bi["uid"]]
                    file_text = _format_subtitle_file_text(bi, part_content, lang)

                    filename = f"{bi['final_scene_id']}-{bi['beat_id']}.txt"
                    file_path = os.path.join(temp_dirs[lang], filename)
                    with open(file_path, 'w', encoding='utf-8') as f_out:
                        f_out.write(file_text)
                    total_files += 1

            for lang, lang_dir in LANG_DIRS.items():
                target_dir = os.path.join(audios_dir, lang_dir)
                backup_dir = f"{target_dir}.previous-{os.getpid()}"
                if os.path.exists(backup_dir):
                    shutil.rmtree(backup_dir)
                had_previous = os.path.exists(target_dir)
                if had_previous:
                    os.replace(target_dir, backup_dir)
                try:
                    os.replace(temp_dirs[lang], target_dir)
                except Exception:
                    if had_previous and os.path.exists(backup_dir):
                        os.replace(backup_dir, target_dir)
                    raise
                else:
                    if os.path.exists(backup_dir):
                        shutil.rmtree(backup_dir)
        except Exception:
            for temp_dir in temp_dirs.values():
                if os.path.exists(temp_dir):
                    shutil.rmtree(temp_dir)
            raise

    # 保留输入寻址的翻译块缓存。后续重跑仍会重新计算 key；plots、语体表、
    # name_mappings、模型或缓存版本任一变化，都不会命中过期译文。

    logger.info("-" * 30)
    logger.info(
        f"✅ [Subtitle] 提取完成！共生成 {total_files} 个字幕文件"
        f"（{', '.join(LANG_DIRS.values())}）。"
    )
    logger.info(f"📂 存储路径: {os.path.abspath(os.path.join(OUTPUT_DIR, str(task_id)))}")
    invalid_records = [
        (path, beat_info, lang)
        for path, beat_info, lang in expected_subtitle_records
        if not _subtitle_file_is_canonical(
            path, beat_info, lang, validate_language=False
        )
    ]
    if invalid_records:
        examples = ", ".join(
            f"{item[1]['scene_id']}-{item[1]['beat_id']}[{item[2]}]"
            for item in invalid_records[:10]
        )
        # 字幕文件已经写入，最终格式复核只记录 partial 告警，不再抛异常阻断
        # 音频流水线。下游仍可处理可用文件，问题坐标通过状态和日志保留。
        logger.warning(
            f"⚠️ Step 2 结果中有 {len(invalid_records)} 个字幕未通过最终格式复核，"
            f"保留并继续下游：{examples}"
        )
    partial_result = bool(hard_violation_total or invalid_records)
    resume_state.mark_step(
        "subtitles", step_digest,
        status="partial" if partial_result else "success",
        error=(
            (
                f"{hard_violation_total} 条硬校验问题，详见 translation-hard-violations-*.json"
                if hard_violation_total else "存在字幕格式复核告警"
            )
            if partial_result else None
        ),
        artifacts=expected_subtitles,
    )
    return task_id


def process_subtitles(target_task_id=None):
    """运行字幕流程，并确保进程退出前尝试落盘 API 用量。"""
    try:
        return _process_subtitles(target_task_id)
    except PlotsDataMissingError:
        raise
    except Exception as exc:
        logger.exception(
            "❌ [Subtitle] 失败；保留已有字幕和翻译断点，交由同 task_id 新 attempt 续跑：%s",
            exc,
        )
        raise
    finally:
        if _usage_tracker:
            try:
                flushed = _usage_tracker.flush()
            except Exception as exc:
                logger.warning(f"⚠️ API 用量落库失败，字幕产物不受影响：{exc}")
            else:
                if not flushed:
                    logger.warning(
                        "⚠️ API 用量数据库仍不可用；"
                        "字幕产物不受影响，但本进程缓存的用量未能落库"
                    )


if __name__ == "__main__":
    TARGET_TASK_ID = ""
    process_subtitles(TARGET_TASK_ID)
