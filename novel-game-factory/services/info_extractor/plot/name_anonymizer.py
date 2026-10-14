"""Apply all character-name anonymization in the pipeline's final Step 16.

``name_mappings.json`` contains public main-name mappings.  Mentioned names
and aliases remain in the internal ``name_replacements.json`` file.  Step 16
combines both sets with original-to-changed speaker ID rules, then updates all
already-generated artifacts in one direct pass.
"""

import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
services_dir = os.path.join(project_root, "services")
utils_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "utils")
for p in (project_root, services_dir, os.path.normpath(utils_dir)):
    if p not in sys.path:
        sys.path.insert(0, p)

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Set, Tuple

from pypinyin import lazy_pinyin
from sqlalchemy import create_engine, text
from logger_config import setup_logger

from common.config import DATABASE_URL, OUTPUTS_PATH, PLATFORM_DATABASE_URL

try:
    from common.config import now_local
except ImportError:  # 离线单测的 common.config 桩包只提供数据库常量
    from datetime import datetime
    from zoneinfo import ZoneInfo

    def now_local():
        """全站统一北京时间；naive datetime 与库里的无时区 timestamp 对齐。"""
        return datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
from common.generic_appellations import (
    GENERIC_ROLE_WORDS,
    is_generic_appellation,
    split_confident_person_name,
    split_transliterated_name,
)
from common.llm_fallback import ModelFallback
from common.model_json import parse_model_json
from common.name_mapping_utils import (
    get_change_name_section,
    get_original_conversion,
    get_source_to_change_conversion,
)

try:
    from common.usage_tracker import tracker as _usage_tracker
except ImportError:  # 离线单测的桩包不提供用量统计
    _usage_tracker = None

# 独立运行时使用的默认 logger；作为流水线第 16 步被调用时由 main.py 传入。
_DEFAULT_LOGGER = setup_logger("Step16_NameAnon", "step16_name_anonymizer.log")


# Only generated artifacts are handled here.  Source character records and
# name_mappings are deliberately excluded.
PLATFORM_TABLES: Tuple[str, ...] = (
    "plots",
    "character_styles",
)
BUSINESS_TABLES: Tuple[str, ...] = (
    "character_position",
)

LOCAL_ARTIFACTS: Dict[str, Tuple[str, ...]] = {
    "plots": ("plots.json",),
    "character_styles": ("character_styles.json",),
    "character_position": ("character_position.json",),
}

# scene_prompts 的正文只在每个 scene 的 image_prompt 字段里；scene_id（键）、
# source_node、copy_count 都是机器标识，绝不能改。因此这张表不走整树替换的
# PLATFORM_TABLES，而是单独走 SCENE_PROMPTS_TABLE 的定点替换路径。
# 只写数据库、无本地文件产物，故不登记到 LOCAL_ARTIFACTS。
SCENE_PROMPTS_TABLE = "scene_prompts"

# 含中文正文、需要做裸姓氏复查的产物。
# character_position / story_tree 的变量名等只有机器标识；scene_prompts 的
# image_prompt 是英文提示词，裸的中文姓氏在那里不构成"我姓李"这类表达。
# character_styles 的 identity / address_terms / address_overrides 会被
# extract_subtitles_from_db 原样拼进日英翻译提示词，残留的旧姓会指挥翻译
# 用原姓称呼已改名的角色，所以它同样要复查。
PROSE_ARTIFACTS: Tuple[str, ...] = ("plots", "character_styles")

# 只同步机器标识、不改正文人名的产物。
# story_tree 的 affection_<角色拼音> 与 plots 的 variable_update 是同一个变量名，
# plots 换了而这里不换，游戏按变量名加分和判定结局分支就会失配。
# 所以只对这类拼音标识做替换，正文人名仍不在本步骤处理范围内。
VARIABLE_ONLY_PLATFORM_TABLES: Tuple[str, ...] = ("story_tree_json",)
VARIABLE_ONLY_LOCAL_ARTIFACTS: Dict[str, Tuple[str, ...]] = {
    "story_tree_json": ("story_tree.json", "story_tree_json.json"),
}


def _mapping_dict(value: Any) -> Dict:
    """把脏字段安全降级为空字典，避免一处格式异常阻断整个换名步骤。"""
    return dict(value) if isinstance(value, Mapping) else {}


def normalise_mappings(
    data: Optional[Mapping[str, Any]],
) -> Dict[str, Dict[str, str]]:
    """Return the changed-name section in a replacement-safe shape."""
    source = get_change_name_section(data)
    new_name = {
        old: new
        for old, new in _mapping_dict(source.get("new-name")).items()
        if isinstance(old, str) and isinstance(new, str) and old and new
    }
    conversion = {
        old: new
        for old, new in _mapping_dict(source.get("conversion")).items()
        if isinstance(old, str) and isinstance(new, str) and old and new
    }

    # Compatibility for the former flat structure.
    if not new_name and "new-name" not in source:
        new_name = {name: name for name in conversion}

    return {
        "new-name": new_name,
        "conversion": conversion,
        "zh-CN": _mapping_dict(source.get("zh-CN")),
        "en-US": _mapping_dict(source.get("en-US")),
        "ja-JP": _mapping_dict(source.get("ja-JP")),
    }


def load_name_replacements(
    task_id: str,
    outputs_path: str,
    logger: Optional[logging.Logger] = None,
) -> Dict[str, str]:
    """Load internal alias and mentioned-name replacement rules."""
    replacement_path = Path(outputs_path) / str(task_id) / "name_replacements.json"
    if not replacement_path.exists():
        if logger:
            logger.info(f"ℹ️ 别称替换表不存在，将只替换角色主名: {replacement_path}")
        return {}

    try:
        with replacement_path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        if logger:
            logger.warning(f"⚠️ 读取别称替换表失败，将只替换角色主名: {exc}")
        return {}

    if not isinstance(data, dict):
        if logger:
            logger.warning(f"⚠️ 别称替换表不是 JSON 对象，已忽略: {replacement_path}")
        return {}

    replacements = {
        old.strip(): new.strip()
        for old, new in data.items()
        if isinstance(old, str)
        and isinstance(new, str)
        and old.strip()
        and new.strip()
        and old.strip() != new.strip()
    }
    if logger:
        logger.info(f"✅ 已加载别称替换表: {len(replacements)} 条")
    return replacements


def _speaker_id_replacements(
    mappings: Optional[Mapping[str, Any]],
) -> Dict[str, str]:
    """Build original speaker ID -> changed speaker ID rules."""
    try:
        original_conversion = get_original_conversion(mappings)
    except Exception:
        # 旧扁平脏数据无法推导 ID 时，只放弃 ID 规则，主名规则继续执行。
        original_conversion = {}
    changed = normalise_mappings(mappings)
    new_names = changed["new-name"]
    changed_conversion = changed["conversion"]
    result: Dict[str, str] = {}

    for original_name, changed_name in new_names.items():
        old_id = original_conversion.get(original_name)
        new_id = changed_conversion.get(changed_name)
        if (
            isinstance(old_id, str)
            and isinstance(new_id, str)
            and old_id
            and new_id
            and old_id != new_id
        ):
            result[old_id] = new_id
    return result


# 由角色 speaker_id 派生的变量名/标识符里的人名拼写形式。
# 游戏用 affection_<角色拼音> 作为好感度变量名，bgm_<角色拼音> 作为音乐 ID，
# 这些位置的人名同样要换，否则原名会以拼音形式留在成品数据里。
# 只处理明确由人名 ID 派生的写法，不做通用拼音猜测。
def _pinyin_variant_replacements(
    speaker_id_replacements: Mapping[str, str],
) -> Dict[str, str]:
    """为每条 speaker_id 规则补充小写与下划线两种拼写。

    MuSheng -> ShenYan 会额外产生 musheng -> shenyan、mu_sheng -> shen_yan。
    过短的标识符跳过：两三个字母极易命中英文单词或其他字段，得不偿失。
    """
    result: Dict[str, str] = {}
    for old_id, new_id in speaker_id_replacements.items():
        if not isinstance(old_id, str) or not isinstance(new_id, str):
            continue
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", old_id) or len(old_id) < 4:
            continue
        for convert in (_to_lower_flat, _to_snake_case):
            old_variant, new_variant = convert(old_id), convert(new_id)
            if old_variant and new_variant and old_variant != new_variant:
                result.setdefault(old_variant, new_variant)
    # 与原始 ID 相同的写法不必重复登记。
    return {
        old: new
        for old, new in result.items()
        if old not in speaker_id_replacements
    }


# 家族上下文后缀：只有紧跟这些词时，才把前面的音译段当家族名替换。
# 与中文姓氏那档用同一份多字后缀，理由相同：单字后缀（家/府/宅/氏）会与普通词
# 撞车。音译段虽然是多字词，但段尾若是泛称（"米娅·姑娘"这类脏数据），
# "姑娘家的脸皮薄"就会被改成"少女家的脸皮薄"、"那位老人家"改成"青年家"。
# 多字后缀自身即成词，不依赖前字断句，不会有这个问题。
_FAMILY_CONTEXT_SUFFIXES = ("家族", "家主", "世家", "一族")


# 音译名的家族名段（"林雷·巴鲁克" 的 "巴鲁克"）在正文里会脱离全名单独出现：
# "巴鲁克家族"、"巴鲁克家的长子"。整名规则 林雷·巴鲁克 -> 林恩·巴鲁森 命不中
# 这些位置，原作的家族名就留在成品里，脱敏等于没做。改名侧已经定下了
# 巴鲁克 -> 巴鲁森 这个事实，这里把它派生成独立规则补上。
def _family_segment_replacements(
    new_names: Mapping[str, str],
    logger: Optional[logging.Logger] = None,
) -> Dict[str, str]:
    """从全名对派生段级规则（巴鲁克 -> 巴鲁森）。

    只处理家族名段（最后一段），且只在锚定到家族上下文时替换。裸段替换会改坏
    正文：`达夫 -> 达芙` 会把"马车抵达夫人的宅邸"改成"抵达芙人"。名段（"林雷"）
    一律不派生——它在正文里独立出现时就是这个角色本人，由整名规则和别称表负责。

    四种情况一律跳过——宁可漏换，也不能错换：
    1. 段数不等：无法确定哪段对哪段。
    2. 段本身是某个角色的完整原名："林雷" 有自己的档案和改名结果，
       按段规则改会把两个身份混成一个（历史故障就是这个形态）。
    3. 单字段：极易命中正文里的普通汉字。
    4. 同一段被派生出不同新值：同族角色没换成同一个家族名，此时无从选择。
    """
    candidates: Dict[str, Set[str]] = {}
    original_names = {
        name for name in new_names if isinstance(name, str) and name
    }

    for old_name, new_name in new_names.items():
        if not isinstance(old_name, str) or not isinstance(new_name, str):
            continue
        old_segments = split_transliterated_name(old_name)
        new_segments = split_transliterated_name(new_name)
        if not old_segments or not new_segments:
            continue
        if len(old_segments) != len(new_segments):
            if logger:
                logger.warning(
                    f"⚠️ {old_name} -> {new_name} 段数不一致，跳过段级换名派生"
                )
            continue
        old_segment, new_segment = old_segments[-1], new_segments[-1]
        if len(old_segment) < 2 or old_segment == new_segment:
            continue
        # 段是另一个角色的完整原名时，那个角色的改名结果才是权威口径。
        if old_segment in original_names:
            continue
        # 泛称不是家族名。上游若产出"米娅·姑娘"这类脏数据，把"姑娘"当家族名会
        # 波及"姑娘家""老人家"等正常表达。
        if is_generic_appellation(old_segment) or old_segment in GENERIC_ROLE_WORDS:
            continue
        candidates.setdefault(old_segment, set()).add(new_segment)

    replacements: Dict[str, str] = {}
    for old_segment, new_segments in candidates.items():
        if len(new_segments) > 1:
            if logger:
                logger.warning(
                    f"⚠️ 段 {old_segment} 被派生出多个新值 "
                    f"{sorted(new_segments)}，指向不明，跳过该段"
                )
            continue
        new_segment = next(iter(new_segments))
        # 锚定家族上下文后再替换。"巴鲁克家族"会换，"抵达夫人"不会。
        for suffix in _FAMILY_CONTEXT_SUFFIXES:
            if (
                suffix.startswith("家")
                and old_segment[-1] in _SURNAMES_FORMING_WORD_WITH_JIA
            ):
                # 段尾与"家"连写成词时同样会跨词边界（"…回家族"）。
                continue
            replacements[f"{old_segment}{suffix}"] = f"{new_segment}{suffix}"
    return replacements


# 中文姓氏的家族称法后缀。裸姓氏绝对不能替换——实测一部小说的正文里"应"作为
# 普通字出现 104 次、"施"51 次、"周"33 次，把它们当姓氏换掉会毁掉大量正常句子。
# 所以规则必须锚定到"姓 + 家族后缀"的完整词上。
#
# 后缀只收多字词。曾经想连"家/府/宅"一起做（"周家""苏府"确实是家族称法），但
# 单字后缀在"姓+后缀"这个字形上无法与普通词区分：一旦有角色姓管/于/和/居，
# "管家"（39 次，且本身就在 GENERIC_ROLE_WORDS 里）、"至于家族"、"生活和家族"、
# "居家办公"就会被改坏。姓氏是封闭集合但覆盖极广，"这个字不会是角色的姓"这种
# 假设站不住。于是只保留自身即成词、不依赖前字断句的多字后缀。
# 代价是"周家""苏府"这类称法漏换，这是为不改坏正文付的必要代价。
_SURNAME_FAMILY_SUFFIXES = ("家族", "家主", "世家", "一族")

# 与"家"连写就成常见词的姓氏。"家族""家主"以"家"开头，所以姓"国"的角色会让
# "国家族群"变成"沈家族群"、"国家主席"变成"沈家主席"。多字后缀并不能自动免疫
# 跨词边界，这一类必须显式排除。
# （"世家""一族"不以"家"开头，不受影响，仍对这些姓氏生效。）
_SURNAMES_FORMING_WORD_WITH_JIA = frozenset(
    "国管安全农富居东公万本自私娘婆老回搬出独厂店买卖赢玩作画"
)


def _surname_family_replacements(
    new_names: Mapping[str, str],
    logger: Optional[logging.Logger] = None,
) -> Dict[str, str]:
    """从中文姓名改名对派生家族称法规则（凌氏 -> 沈氏、凌家族 -> 沈家族）。

    换姓之后，正文里的"凌家族""凌氏"仍指向原作家族，和音译名的"巴鲁克家族"
    是同一个问题。这里只处理姓氏可信、且确实换了姓的角色。

    守卫：
    1. 只认 COMMON_SURNAMES 里的姓氏——姓氏是封闭集合，"家长"不能被拆成姓"家"。
    2. 新旧姓都必须可信，且确实不同。
    3. 至少两个角色同姓才认定为家族。虚词类姓氏（和/于/在/从…共 23 个字同时是
       常用字和姓氏）最多只会命中一个角色，这条守卫把它们全部挡在外面，
       "生活和家族""至于家族"就不会被改坏。真家族本来就有多个成员。
    4. 同一个旧姓派生出不同新姓时冲突跳过（同姓角色本该换成同一个新姓）。
    5. 多个旧姓被换成同一个新姓时，两个家族会在正文里合并成一个，也跳过。
    6. 只生成"姓+后缀"的完整词规则，绝不生成裸姓氏规则。
    """
    candidates: Dict[str, Set[str]] = {}
    surname_members: Dict[str, Set[str]] = {}
    for old_name, new_name in new_names.items():
        if not isinstance(old_name, str) or not isinstance(new_name, str):
            continue
        old_parts = split_confident_person_name(old_name)
        if not old_parts:
            continue
        # 同姓成员数按"原名"统计，与是否改名无关：家族规模是原文事实。
        surname_members.setdefault(old_parts[0], set()).add(old_name)
        new_parts = split_confident_person_name(new_name)
        if not new_parts:
            continue
        old_surname, new_surname = old_parts[0], new_parts[0]
        if old_surname == new_surname:
            continue
        candidates.setdefault(old_surname, set()).add(new_surname)

    resolved: Dict[str, str] = {}
    for old_surname, new_surnames in candidates.items():
        if len(surname_members.get(old_surname, ())) < 2:
            # 单个角色不足以证明"存在这个家族"；虚词类姓氏都止步于此。
            continue
        if len(new_surnames) > 1:
            if logger:
                logger.warning(
                    f"⚠️ 姓氏 {old_surname} 被派生出多个新姓 "
                    f"{sorted(new_surnames)}，指向不明，跳过其家族称法"
                )
            continue
        resolved[old_surname] = next(iter(new_surnames))

    # 两个旧姓换成同一个新姓时，"岑家族"和"周家族"会双双变成"沈家族"，
    # 正文里两个家族就此合并。宁可都不换，也不能把不同家族说成同一个。
    merged: Dict[str, Set[str]] = {}
    for old_surname, new_surname in resolved.items():
        merged.setdefault(new_surname, set()).add(old_surname)
    replacements: Dict[str, str] = {}
    for new_surname, old_surnames in merged.items():
        if len(old_surnames) > 1:
            if logger:
                logger.warning(
                    f"⚠️ 姓氏 {sorted(old_surnames)} 都被换成 {new_surname}，"
                    "家族称法会合并成同一个，跳过这些姓氏"
                )
            continue
        old_surname = next(iter(old_surnames))
        for suffix in _SURNAME_FAMILY_SUFFIXES:
            if (
                suffix.startswith("家")
                and old_surname in _SURNAMES_FORMING_WORD_WITH_JIA
            ):
                # "国家族群""安家主意"这类跨词边界的命中，宁可漏换。
                continue
            replacements[f"{old_surname}{suffix}"] = f"{new_surname}{suffix}"
    return replacements


# ============================================================
# 裸姓氏残留的模型辅助洗稿
#
# 规则替换只认完整词。台词里的"我姓李""方氏的大少""李兄"用的是角色姓氏的裸字，
# 规则要么命不中，要么会误伤（"方才""方向""李相夷"里的同一个字并不指这个人）。
# 实测《莲花楼》：李莲花->沈莲花、方多病->沈多病 之后，plots 里仍有
# "原来阁下是方氏的大少，多愁公子沈多病！"——同一句里半换，一眼能看出改过名。
#
# 判定这些位置需要上下文："李兄，你与那李神医同姓"里两个"李"指代不同对象，
# 任何字符串规则都做不到。所以这里把可疑位置交给模型判断。
#
# 为了不让模型逐条看完所有正文（几千个 beat，太慢），召回分两步：
#   1. 先跑完全部规则，再把所有已知人名（原名+新名）整体挖掉；
#   2. 剩下的文本里若仍出现"改了名的角色"的姓氏/音译段，才是可疑位置。
# 这个判据不依赖任何"姓氏语境词表"——词表按题材写死，换个题材就漏，而漏掉的
# 位置既不进模型也不告警，会静默留在成品里。从 name_mappings 推导则与题材无关。
# ============================================================

RESIDUE_MODEL = "qwen3.8-max"
_residue_fallback = ModelFallback(RESIDUE_MODEL, name="NameResidue")
RESIDUE_BATCH_SIZE = 60
RESIDUE_MAX_ATTEMPTS = 3
RESIDUE_TIMEOUT_BASE_SECONDS = 60.0
RESIDUE_TIMEOUT_PER_ITEM_SECONDS = 3.0
RESIDUE_TIMEOUT_MAX_SECONDS = 600.0
# 单次替换只作用于一个字/段，取词前后各若干字作为模型判断的依据。
RESIDUE_CONTEXT_CHARS = 24
# 主备模型都确认不可用之后，本次运行不再尝试后续批次。
# 一批要打满 3 次 x 2 个模型，每次超时最长 240 秒；模型真的挂了的话，
# 《莲花楼》那种 4 批的量级最坏要在重试上耗掉 96 分钟，而结果注定是"没有修复
# 指令"。既然主备都挂了，后面几批不可能有别的结果，直接跳过。
_RESIDUE_CIRCUIT_OPEN = False


def reset_residue_circuit() -> None:
    """重置模型熔断标记。进程内多任务连跑时，每个任务开始前调用。"""
    global _RESIDUE_CIRCUIT_OPEN
    _RESIDUE_CIRCUIT_OPEN = False


# 判定"模型挂了"的依据：错误性质是不是**与这一批数据无关**。
#
# 而且必须按模型分别判定。qwen 挂了不代表 gpt 也挂——只有**每一个**候选模型都
# 确认服务级不可用，才谈得上"没有模型可用"。只看最后一个错误会误判：
# qwen 限流（可恢复）+ gpt 连不上，最后一个错误是连接失败，就会错误地熔断，
# 而其实等一下 qwen 还能用。
#
# 连不上、超时、认证失败、余额不足、模型名不存在——换一批数据同样失败，
# 属于服务级不可用，后续批次没必要再等一遍超时。
#
# 反过来，这些不算模型挂：
#   * 429 限流——模型活着，只是要排队；
#   * 内容审核、单批 prompt 过长——是这一批的问题，换一批可能就正常；
#   * 返回的不是合法 JSON——模型活着，只是这次输出坏了。
# 把它们判成"挂了"会让本来能修的批次一起被放弃。
_SERVICE_DOWN_MARKERS = (
    "connection", "connect", "timeout", "timed out", "unreachable",
    "econnrefused", "refused", "dns", "ssl", "socket",
    "bad gateway", "gateway timeout", "service unavailable",
    "internal server error", "temporarily unavailable",
    "authentication", "unauthorized", "invalid api key", "api key",
    "permission denied", "forbidden",
    "insufficient balance", "insufficient_quota", "quota",
    "model not found", "does not exist", "no such model",
)
_SERVICE_DOWN_STATUS = ("500", "502", "503", "504", "401", "403", "402", "404")
# 明确不算服务不可用的信号，优先于上面的关键词判断。
_TRANSIENT_MARKERS = (
    "rate limit", "429", "too many requests",
    "content filter", "content_filter", "moderation", "safety",
    "context length", "maximum context", "too long", "token limit",
)


def _looks_like_service_down(exc: Optional[BaseException]) -> bool:
    """错误是否表明模型服务整体不可用（而非这一批数据的问题）。"""
    if exc is None:
        return False
    for error in (getattr(exc, "__cause__", None), exc):
        if error is None:
            continue
        message = f"{type(error).__name__}: {error}".lower()
        if any(marker in message for marker in _TRANSIENT_MARKERS):
            return False
        status = getattr(error, "status_code", None) or getattr(error, "code", None)
        if str(status) in _SERVICE_DOWN_STATUS:
            return True
        if any(marker in message for marker in _SERVICE_DOWN_MARKERS):
            return True
        if isinstance(error, (ConnectionError, TimeoutError, OSError)):
            return True
    return False
# 判断结果字典里的失败标记。"这一批没有任何需要替换的"和"这一批请求失败了"
# 都表现为空结果，但前者可以记为完成、后者必须留待重跑，所以要能区分。
# 用负数 key，不会与真实的 index 冲突。
_RESIDUE_FAILURE_FLAG = -1

# 判断缓存的键：(片段, 上下文, 说话人)。三者相同才是同一个判断。
_ResidueKey = Tuple[str, str, str]


def _renamed_name_fragments(
    new_names: Mapping[str, str],
) -> Dict[str, str]:
    """从改名映射推导"值得复查的片段" -> 该片段所属的角色原名。

    中文名取姓氏，音译名取每一段。只覆盖确实改了名的角色：没改名的角色，
    它的姓氏出现在正文里本来就不该动。
    """
    fragments: Dict[str, str] = {}
    for old_name, new_name in new_names.items():
        if not isinstance(old_name, str) or not isinstance(new_name, str):
            continue
        if not old_name or old_name == new_name:
            continue
        segments = split_transliterated_name(old_name)
        if segments:
            for segment in segments:
                if len(segment) >= 2:
                    fragments.setdefault(segment, old_name)
            continue
        parts = split_confident_person_name(old_name)
        if parts:
            fragments.setdefault(parts[0], old_name)
    return fragments


def _mask_known_names(text: str, known_names: Sequence[str]) -> str:
    """把已知人名替换成占位符，避免它们内部的姓氏被当成裸姓氏残留。

    "李相夷"是另一个角色的完整名、"沈多病"是已经换好的新名，两者内部的
    "李""方"都不该再动。长名优先，避免短名先把长名切开。
    """
    masked = text
    for name in known_names:
        if name:
            masked = masked.replace(name, "\x00" * len(name))
    return masked


def find_residual_name_fragments(
    data: Any,
    mappings: Optional[Mapping[str, Any]],
    replacements: Mapping[str, str],
    pattern=None,
) -> list:
    """找出规则跑完后仍可疑的裸姓氏位置。

    返回 [{path, field, text, fragment, owner, start, context}]，每项对应正文里
    的一次出现。按位置而不是按"搭配"返回：同一个"李兄"在不同场景可能指不同的人，
    按搭配去重会把歧义信息丢掉，而处理歧义正是这一步存在的意义。
    """
    normalised = normalise_mappings(mappings)
    fragments = _renamed_name_fragments(normalised["new-name"])
    if not fragments:
        return []
    known_names = sorted(
        {
            name
            for name in list(normalised["new-name"])
            + list(normalised["new-name"].values())
            if isinstance(name, str) and name
        },
        key=len,
        reverse=True,
    )
    found: list = []

    def visit(node: Any, path: str, speaker: str = "") -> None:
        if isinstance(node, dict):
            # 说话人是判断第一人称台词的关键依据："我姓李"这句若出自已改名的
            # 角色，就必须改；出自另一个真姓李的角色则不能动。beat 自带
            # full_name/speaker_name，向下传给同一 beat 内的文本字段。
            current = speaker
            for field in ("full_name", "speaker_name"):
                value = node.get(field)
                if isinstance(value, str) and value.strip():
                    current = value.strip()
                    break
            for key, value in node.items():
                if key in _RESIDUE_SKIP_FIELDS:
                    continue
                visit(value, f"{path}.{key}" if path else str(key), current)
            return
        if isinstance(node, list):
            for index, value in enumerate(node):
                visit(value, f"{path}[{index}]", speaker)
            return
        if not isinstance(node, str) or not node:
            return
        masked = _mask_known_names(node, known_names)
        for fragment, owner in fragments.items():
            start = masked.find(fragment)
            while start != -1:
                found.append(
                    {
                        "path": path,
                        "text": node,
                        "fragment": fragment,
                        "owner": owner,
                        "speaker": speaker,
                        "start": start,
                        "context": node[
                            max(0, start - RESIDUE_CONTEXT_CHARS):
                            start + len(fragment) + RESIDUE_CONTEXT_CHARS
                        ],
                    }
                )
                start = masked.find(fragment, start + len(fragment))
    visit(data, "")
    return found


# 机器标识字段不参与裸姓氏复查：它们是 ID 而不是正文，改了会与其他产物失配。
# 后一组是 character_styles 里的语体枚举字段：取值是语法档位和词尾形式
# （丁寧語 / ～てください / 現代 / casual），结构上装不下人名。实测这些字段的
# 全部召回都是普通词撞姓氏（丁寧語 撞"丁"、陈述 撞"陈"、現代 撞"时"），
# 按形态判定的可疑项为 0，跳过它们只减量、不漏人名。
#
# style_note / vocabulary / humor_style / sentence_style / thought_style 这类
# 自由描述**故意不跳过**，虽然它们占召回量的大头（734 处，几乎全是"夸张"撞
# "张"、"温和"撞"温"这种）：实测里有一条 request_style 写的是
# "对下属直接命令，对林恩温和叮嘱"，自由描述能提到具体角色。宁可多问模型，
# 也不靠"这个字段应该不会有人名"的猜测把位置永久排除在复查之外。
_RESIDUE_SKIP_FIELDS = frozenset(
    {
        "speaker_combination",
        "speaker_id",
        "scene_id",
        "beat_id",
        "cg",
        "publication",
        "_src_lines",
        "source_node",
        "copy_count",
        # character_styles 的语体枚举字段
        "register",
        "politeness",
        "honorific_level",
        "humble_level",
        "pronoun_omission",
        "avoid_second_person",
        "sentence_end",
        "preferred_endings",
        "allowed_endings",
        "avoid_endings",
        "era_style",
        "dialect",
        "gender_marking",
        "formality",
        "directness",
        "contractions",
        "profanity_level",
        "status",
        "evidence_count",
    }
)


def _tracked_residue_chat(
    client: Any,
    prompt: str,
    errors_by_model: Dict[str, BaseException],
) -> Tuple[Any, str]:
    """调用 ModelFallback，同时记下每个模型各自的错误。

    ModelFallback 只向外抛最后一个异常，拿不到"主模型失败的原因"。判断"是否
    所有模型都服务级不可用"需要每个模型自己的错误，所以在 client 外面包一层，
    按 model 参数记账。仍然完全复用 ModelFallback 的主备切换与粘滞逻辑。
    """

    class _Recording:
        def __init__(self, inner: Any):
            self._inner = inner

        @property
        def chat(self):
            outer = self

            class _Chat:
                @property
                def completions(self):
                    class _Completions:
                        @staticmethod
                        def create(model=None, **kwargs):
                            try:
                                return outer._inner.chat.completions.create(
                                    model=model, **kwargs
                                )
                            except BaseException as exc:
                                if model:
                                    errors_by_model[model] = exc
                                raise

                    return _Completions()

            return _Chat()

    return _residue_fallback.chat(
        _Recording(client),
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )


def _all_models_down(
    errors_by_model: Mapping[str, BaseException],
) -> bool:
    """本次**实际尝试过**的模型是否都确认服务级不可用。

    只看试过的模型：ModelFallback 在主模型连续失败后会粘滞到备选，此后不再
    调用主模型，那它自然没有错误记录——若要求"所有候选模型都有服务级错误"，
    这种情况永远不会熔断，而实际上备选也挂了就真没模型可用了。

    反过来，任一试过的模型报的是限流/审核这类可恢复错误，就不算没模型可用：
    qwen 挂了不代表 gpt 也挂，gpt 限流也不代表 qwen 用不了。
    """
    if not errors_by_model:
        return False
    return all(
        _looks_like_service_down(error) for error in errors_by_model.values()
    )


def _residue_timeout_for(count: int) -> float:
    estimated = (
        RESIDUE_TIMEOUT_BASE_SECONDS
        + RESIDUE_TIMEOUT_PER_ITEM_SECONDS * max(count, 0)
    )
    return min(estimated, RESIDUE_TIMEOUT_MAX_SECONDS)


def _residue_client(count: int):
    """与其他步骤一致：走中转站的 OpenAI 兼容接口。"""
    from openai import OpenAI

    base_url = os.getenv(
        "GPT_IMAGE_BASE_URL", "http://sefutech-api.kube-test.svc:3000"
    ).rstrip("/")
    api_key = (
        os.getenv("GPT_IMAGE_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("DEEPSEEK_API_KEY")
    )
    if not api_key:
        return None
    return OpenAI(
        api_key=api_key,
        base_url=f"{base_url}/v1",
        timeout=_residue_timeout_for(count),
        max_retries=0,
    )


def _build_residue_prompt(
    items: Sequence[Mapping[str, Any]],
    rename_pairs: Mapping[str, str],
) -> str:
    lines = []
    for index, item in enumerate(items):
        speaker = item.get("speaker") or ""
        speaker_note = f" 说话人：{speaker}" if speaker else ""
        lines.append(
            f"{index}. 片段「{item['fragment']}」{speaker_note}"
            f" 上下文：{item['context']}"
        )
    pairs = "、".join(
        f"{old}→{new}" for old, new in list(rename_pairs.items())[:60]
    )
    return (
        "小说已经改名脱敏，正文里的完整人名都替换过了。现在要检查残留的**单独姓氏**。\n"
        f"改名对照（原名→新名）：{pairs}\n\n"
        "对每一处，判断这个片段是否就是上面某个已改名角色的姓氏：\n"
        "- 是（如「我姓李」「李兄」「方氏的少主」指的就是该角色）→ 换成新名的对应姓氏。\n"
        "- 不是 → 保持原样。以下都属于「不是」：\n"
        "  * 另一个角色或历史/典故人物的名字（如「李相夷」「李靖」「李神医」另有其人）；\n"
        "  * 普通词汇里恰好含这个字（如「方才」「方向」「方法」「地方」）；\n"
        "  * 地名、门派、招式等专有名词。\n"
        "同一个字在同一句里可能一处该换、一处不该换，按各自指代分别判断。\n"
        "「说话人」是重要线索：第一人称自述（「我姓X」「免贵姓X」）说的就是说话人\n"
        "自己，说话人已改名就要换；但转述别人（「他姓X」「那位X神医」）要看指的是谁。\n\n"
        "只输出 JSON：{\"results\": [{\"index\": 0, \"replace\": true, \"to\": \"沈\"}, "
        "{\"index\": 1, \"replace\": false}]}\n"
        "replace 为 true 时必须给出 to（只写替换后的姓氏本身，不要写整句）。\n"
        "每一项都必须返回。\n\n"
        "待判断：\n" + "\n".join(lines)
    )


def _residue_cache_key(item: Mapping[str, Any]) -> "_ResidueKey":
    """判断依据只有片段和上下文，两者相同就是同一个判断。

    plots 同时是数据库表和本地文件，同一份内容会走两条路径；这个键让第二次
    直接复用第一次的判断，既省一次请求，也保证两处结果一致。
    """
    # 说话人参与缓存键：同一句"我姓李"出自不同角色，判断结果可能相反。
    return (
        str(item.get("fragment", "")),
        str(item.get("context", "")),
        str(item.get("speaker", "")),
    )


def _request_residue_decisions(
    items: Sequence[Mapping[str, Any]],
    rename_pairs: Mapping[str, str],
    logger: Optional[logging.Logger],
    cache: Optional[Dict[_ResidueKey, Optional[str]]] = None,
) -> Dict[int, str]:
    """返回 {index: 新姓氏}；只包含模型明确要求替换的项。"""
    if cache is not None:
        pending_items = []
        pending_indexes = []
        decisions: Dict[int, str] = {}
        for index, item in enumerate(items):
            key = _residue_cache_key(item)
            if key in cache:
                cached = cache[key]
                if cached:
                    decisions[index] = cached
                continue
            pending_items.append(item)
            pending_indexes.append(index)
        if not pending_items:
            return decisions
        fresh = _request_residue_decisions(
            pending_items, rename_pairs, logger, cache=None
        )
        failed = _RESIDUE_FAILURE_FLAG in fresh
        if failed:
            # 失败的判断不进缓存：否则同一处内容在后续产物里会沿用"不改写"，
            # 把一次请求失败放大成永久漏洗。
            decisions[_RESIDUE_FAILURE_FLAG] = ""
        for local_index, item in enumerate(pending_items):
            target = fresh.get(local_index)
            if not failed:
                cache[_residue_cache_key(item)] = target
            if target:
                decisions[pending_indexes[local_index]] = target
        return decisions

    global _RESIDUE_CIRCUIT_OPEN
    if _RESIDUE_CIRCUIT_OPEN:
        # 主备都已确认挂掉，后续批次不可能有别的结果，不再浪费时间重试。
        return {_RESIDUE_FAILURE_FLAG: ""}

    client = _residue_client(len(items))
    if client is None:
        if logger:
            logger.warning("⚠️ 未配置模型 API Key，跳过裸姓氏复查（正文保持原样）")
        # 没有 key 是配置问题，重试同样没有意义。
        _RESIDUE_CIRCUIT_OPEN = True
        return {_RESIDUE_FAILURE_FLAG: ""}

    prompt = _build_residue_prompt(items, rename_pairs)
    last_error = None
    # 每个模型各自的最后一次错误。熔断要求所有候选模型都服务级不可用，
    # 所以必须分模型记录，不能只留最后一个。
    errors_by_model: Dict[str, BaseException] = {}
    for attempt in range(1, RESIDUE_MAX_ATTEMPTS + 1):
        try:
            response, used_model = _tracked_residue_chat(
                client,
                prompt,
                errors_by_model,
            )
            if _usage_tracker is not None and getattr(response, "usage", None):
                _usage_tracker.add_llm_usage(
                    response.usage, used_model, provider="openai_compatible"
                )
            data = parse_model_json(
                response.choices[0].message.content,
                context="name_residue",
                logger=logger,
            )
            return _clean_residue_decisions(data, items, logger)
        except Exception as exc:
            last_error = exc
            if logger:
                logger.warning(
                    f"⚠️ 裸姓氏复查第 {attempt}/{RESIDUE_MAX_ATTEMPTS} 次失败: {exc}"
                )
    # 打满重试仍失败。ModelFallback 每次都把主备两个模型都试过了，所以此时
    # 若错误是服务级的（连不上/超时/认证/余额/模型不存在），后续批次注定同样失败，
    # 直接熔断。限流、内容审核、超长、格式坏则只是这一批的问题，不牵连其他批次。
    if _all_models_down(errors_by_model):
        _RESIDUE_CIRCUIT_OPEN = True
        if logger:
            detail = "；".join(
                f"{name}: {error}" for name, error in errors_by_model.items()
            )
            logger.error(
                f"❌ 裸姓氏复查已尝试的模型全部不可用，本次运行剩余批次一并跳过"
                f"（正文保持原样，不阻断换名）: {detail}"
            )
    elif logger:
        logger.warning(
            f"⚠️ 本批裸姓氏复查失败，保持原样并继续其余批次"
            f"（仍有模型可用，不阻断换名）: {last_error}"
        )
    return {_RESIDUE_FAILURE_FLAG: ""}


def _clean_residue_decisions(
    data: Any,
    items: Sequence[Mapping[str, Any]],
    logger: Optional[logging.Logger],
) -> Dict[int, str]:
    """校验模型返回：只接受结构正确、且改写目标合理的项。"""
    results = data.get("results") if isinstance(data, Mapping) else None
    if not isinstance(results, list):
        if logger:
            logger.warning("⚠️ 裸姓氏复查返回结构异常，本批保持原样")
        return {_RESIDUE_FAILURE_FLAG: ""}
    decisions: Dict[int, str] = {}
    for entry in results:
        if not isinstance(entry, Mapping):
            continue
        index = entry.get("index")
        if not isinstance(index, int) or not 0 <= index < len(items):
            continue
        if not entry.get("replace"):
            continue
        target = entry.get("to")
        if not isinstance(target, str):
            continue
        target = target.strip()
        fragment = items[index]["fragment"]
        # 只接受"同长度的另一个片段"：模型若返回整句或空串，说明它没按指令做，
        # 照搬会破坏台词。长度一致也顺带保证了替换不会改变正文结构。
        if not target or target == fragment or len(target) != len(fragment):
            if logger:
                logger.warning(
                    f"⚠️ 裸姓氏复查返回的替换目标不合规，已忽略: "
                    f"{fragment} -> {target!r}"
                )
            continue
        decisions[index] = target
    return decisions


def _apply_residue_decisions(
    data: Any,
    items: Sequence[Mapping[str, Any]],
    decisions: Mapping[int, str],
    logger: Optional[logging.Logger] = None,
) -> Tuple[Any, int]:
    """按位置改写原文里的裸姓氏。

    同一个字符串可能有多处待改，按 start 倒序替换，避免前面的替换移动后面的下标。
    模型只给出"这一处换成什么"，替换动作由这里执行——模型不接触整句，就不可能
    顺手改动台词的其他部分。
    """
    if not decisions:
        return data, 0

    # 按 (path, 原字符串) 归并，同一处文本的多个位置一起处理。
    by_text: Dict[Tuple[str, str], list] = {}
    for index, target in decisions.items():
        item = items[index]
        by_text.setdefault((item["path"], item["text"]), []).append(
            (item["start"], item["fragment"], target)
        )

    rewritten: Dict[Tuple[str, str], str] = {}
    for key, edits in by_text.items():
        _, original = key
        updated = original
        for start, fragment, target in sorted(edits, reverse=True):
            if updated[start:start + len(fragment)] != fragment:
                # 位置对不上说明数据在召回之后被改过，跳过这一处而不是盲改。
                if logger:
                    logger.warning(
                        f"⚠️ 裸姓氏位置已失效，跳过该处改写: {fragment}@{start}"
                    )
                continue
            updated = updated[:start] + target + updated[start + len(fragment):]
        if updated != original:
            rewritten[key] = updated

    if not rewritten:
        return data, 0

    applied = 0

    def rebuild(node: Any, path: str) -> Any:
        nonlocal applied
        if isinstance(node, dict):
            return {
                key: (
                    value
                    if key in _RESIDUE_SKIP_FIELDS
                    else rebuild(value, f"{path}.{key}" if path else str(key))
                )
                for key, value in node.items()
            }
        if isinstance(node, list):
            return [
                rebuild(value, f"{path}[{index}]")
                for index, value in enumerate(node)
            ]
        if isinstance(node, str):
            updated = rewritten.get((path, node))
            if updated is not None:
                applied += 1
                return updated
        return node

    return rebuild(data, ""), applied


def anonymize_residual_fragments(
    data: Any,
    mappings: Optional[Mapping[str, Any]],
    replacements: Mapping[str, str],
    pattern=None,
    logger: Optional[logging.Logger] = None,
    cache: Optional[Dict[_ResidueKey, Optional[str]]] = None,
    incomplete: Optional[list] = None,
) -> Tuple[Any, int]:
    """规则替换之后的兜底：让模型判断残留的裸姓氏该不该换。

    任何异常都只告警并返回原数据——这一步是增强，不能阻断第 16 步。

    模型没能完整判断时（未配置 key、请求失败、返回不合规），把原因追加到
    ``incomplete``。调用方据此不把该产物标记为已验证：裸姓氏残留不在
    replacements 里，``find_unreplaced_names`` 查不到它，若照常记为完成，
    此后每次重跑都会因"规则和内容都没变"而跳过，残留就永远补不上了。
    """
    try:
        items = find_residual_name_fragments(
            data, mappings, replacements, pattern
        )
    except Exception as exc:
        if logger:
            logger.warning(f"⚠️ 裸姓氏召回失败，跳过模型复查: {exc}")
        if incomplete is not None:
            incomplete.append(f"召回失败: {exc}")
        return data, 0
    if not items:
        return data, 0

    normalised = normalise_mappings(mappings)
    rename_pairs = {
        old: new
        for old, new in normalised["new-name"].items()
        if isinstance(old, str) and isinstance(new, str) and old and old != new
    }
    if logger:
        logger.info(
            f"🔍 裸姓氏复查：{len(items)} 处可疑位置交由 {RESIDUE_MODEL} 判断"
        )

    decisions: Dict[int, str] = {}
    for start in range(0, len(items), RESIDUE_BATCH_SIZE):
        batch = items[start:start + RESIDUE_BATCH_SIZE]
        try:
            batch_decisions = _request_residue_decisions(
                batch, rename_pairs, logger, cache=cache
            )
        except Exception as exc:
            if logger:
                logger.warning(f"⚠️ 裸姓氏复查批次异常，该批保持原样: {exc}")
            if incomplete is not None:
                incomplete.append(f"批次 {start} 异常: {exc}")
            continue
        # 一批全部判定为"无需替换"是正常结果；但模型失败时也会返回空字典。
        # 用 _residue_incomplete 标记区分这两种情况。
        if _RESIDUE_FAILURE_FLAG in batch_decisions:
            batch_decisions.pop(_RESIDUE_FAILURE_FLAG, None)
            if incomplete is not None:
                incomplete.append(f"批次 {start} 未获得完整判断")
        for local_index, target in batch_decisions.items():
            decisions[start + local_index] = target

    if not decisions:
        if logger:
            logger.info("ℹ️ 裸姓氏复查未产生改写")
        return data, 0

    try:
        changed, applied = _apply_residue_decisions(
            data, items, decisions, logger
        )
    except Exception as exc:
        if logger:
            logger.warning(f"⚠️ 裸姓氏改写失败，保留原数据: {exc}")
        return data, 0
    if logger:
        logger.info(
            f"✅ 裸姓氏复查完成：{len(decisions)} 处判定需改写，"
            f"实际改写 {applied} 段文本"
        )
    return changed, applied


def _without_cascading_rules(
    derived: Mapping[str, str],
    existing: Mapping[str, str],
    logger: Optional[logging.Logger] = None,
) -> Dict[str, str]:
    """剔除会与其他规则形成接力的派生规则。

    A 的新姓恰好是 B 的旧姓时（许->陆 且 陆->秦），单遍替换本身正确，但重跑会把
    第一遍的产物再换一次："许家族"->"陆家族"->"秦家族"。第 16 步设计上可以重复
    执行，`find_unreplaced_names` 又只检查"该换的换没换"、发现不了这种过度替换，
    于是产物会被标记为已完成，之后每次重跑都继续变化。

    判据：某条派生规则的原文如果出现在任何一条规则（含整名、别称与其他派生规则）
    的结果里，这条规则就会吃到别人的产物，直接放弃。
    """
    if not derived:
        return {}
    all_new_values = list(existing.values()) + list(derived.values())
    safe: Dict[str, str] = {}
    for old, new in derived.items():
        cascading = [
            value for value in all_new_values if value != new and old in value
        ]
        if cascading:
            if logger:
                logger.warning(
                    f"⚠️ 派生规则 {old} -> {new} 会接力替换其他规则的结果"
                    f"（如 {cascading[0]}），跳过以保证重跑结果稳定"
                )
            continue
        safe[old] = new
    return safe


def _to_lower_flat(identifier: str) -> str:
    return identifier.lower()


def _to_snake_case(identifier: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", identifier).lower()


def replacement_map(
    mappings: Optional[Mapping[str, Any]],
    alias_replacements: Optional[Mapping[str, str]] = None,
    include_speaker_ids: bool = False,
    logger: Optional[logging.Logger] = None,
) -> Dict[str, str]:
    """Combine main names, aliases and optionally speaker IDs."""
    normalised = normalise_mappings(mappings)
    replacements = {
        old: new
        for old, new in normalised["new-name"].items()
        if isinstance(old, str)
        and isinstance(new, str)
        and old
        and new
        and old != new
    }
    # 主名规则优先。别称表若意外包含某个角色主名，不能覆盖正式映射。
    aliases = alias_replacements if isinstance(alias_replacements, Mapping) else {}
    for old, new in aliases.items():
        if (
            isinstance(old, str)
            and isinstance(new, str)
            and old
            and new
            and old != new
        ):
            replacements.setdefault(old, new)
    # 家族名段与中文姓氏家族称法最后登记：整名与别称都是显式定稿的口径，
    # 派生规则不能覆盖它们。_compile_replacement_pattern 按长度倒序匹配，
    # 全名先命中，派生规则只会落在全名之外的位置（"巴鲁克家族"）。
    derived = dict(_family_segment_replacements(normalised["new-name"], logger))
    derived.update(_surname_family_replacements(normalised["new-name"], logger))
    for old, new in _without_cascading_rules(
        derived, replacements, logger
    ).items():
        replacements.setdefault(old, new)
    if include_speaker_ids:
        speaker_ids = _speaker_id_replacements(mappings)
        for old, new in speaker_ids.items():
            replacements.setdefault(old, new)
        for old, new in _pinyin_variant_replacements(speaker_ids).items():
            replacements.setdefault(old, new)
    return replacements


def variable_replacement_map(
    mappings: Optional[Mapping[str, Any]],
) -> Dict[str, str]:
    """只含 speaker_id 及其拼音变体的规则，用于同步变量名等机器标识。"""
    speaker_ids = _speaker_id_replacements(mappings)
    replacements = dict(speaker_ids)
    replacements.update(_pinyin_variant_replacements(speaker_ids))
    return replacements


def _compile_replacement_pattern(replacements: Mapping[str, str]):
    alternatives = []
    for old in sorted(replacements, key=len, reverse=True):
        escaped = re.escape(old)
        if re.fullmatch(r"[A-Za-z0-9 _.'’'-]+", old):
            alternatives.append(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])")
        else:
            alternatives.append(escaped)
    return re.compile("|".join(alternatives)) if alternatives else None


def _replace_with_pattern(
    content: Any,
    replacements: Mapping[str, str],
    pattern,
) -> Any:
    if not isinstance(content, str) or pattern is None:
        return content
    return pattern.sub(lambda match: replacements[match.group(0)], content)


def replace_names_in_text(
    content: Any,
    mappings: Optional[Mapping[str, Any]],
    alias_replacements: Optional[Mapping[str, str]] = None,
) -> Any:
    """Replace main names and aliases in one pass, longest match first."""
    try:
        replacements = replacement_map(mappings, alias_replacements)
        pattern = _compile_replacement_pattern(replacements)
        return _replace_with_pattern(content, replacements, pattern)
    except Exception:
        # 这是兼容入口；任何脏数据都保持原内容，不能把调用方流程带崩。
        return content


def _to_pascal_case_id(raw_text: str) -> str:
    """Generate a fallback speaker ID when conversion has no entry."""
    if not raw_text:
        return ""
    if re.search(r"[一-鿿]", raw_text):
        return "".join(part.capitalize() for part in lazy_pinyin(raw_text))

    value = re.sub(r"['‘’]s\b", "", raw_text, flags=re.IGNORECASE)
    value = re.sub(r"['‘’]", "", value)
    value = re.sub(r"[^a-zA-Z0-9]", " ", value)
    return "".join(part.capitalize() for part in value.split())


def _replace_json_tree(
    value: Any,
    replacements: Mapping[str, str],
    pattern,
    logger: Optional[logging.Logger] = None,
) -> Any:
    """Recursively replace JSON string values and dictionary keys."""
    if isinstance(value, str):
        return _replace_with_pattern(value, replacements, pattern)
    if isinstance(value, list):
        return [_replace_json_tree(item, replacements, pattern, logger) for item in value]
    if not isinstance(value, dict):
        return value

    result = {}
    for key, item in value.items():
        changed_key = (
            _replace_with_pattern(key, replacements, pattern)
            if isinstance(key, str)
            else key
        )
        # publication is a release/version contract, not player text or a
        # story-tree variable.  Never rewrite its run_id/timestamp/fingerprint
        # while synchronizing renamed machine identifiers in locale trees.
        if key == "publication":
            result[changed_key] = item
            continue
        # speaker_combination 是 Step 8 生成的状态组合 ID，后续会用它反查
        # characters.valid_status_combinations；这里改掉角色名前缀会导致 ID
        # 与 characters 中的组合定义失配，因此字段值必须保持原样。
        if key == "speaker_combination":
            result[changed_key] = item
            continue
        result[changed_key] = _replace_json_tree(
            item, replacements, pattern, logger
        )
    return result


def _refresh_character_styles_hash(data: Any, mappings: Mapping[str, Any]) -> Any:
    if not isinstance(data, dict):
        return data
    metadata = data.get("metadata")
    if not isinstance(metadata, dict) or "name_mapping_hash" not in metadata:
        return data

    conversion = get_source_to_change_conversion(mappings)
    payload = json.dumps(
        conversion,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    metadata["name_mapping_hash"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return data


def _replace_artifact(
    data: Any,
    mappings: Mapping[str, Any],
    replacements: Mapping[str, str],
    pattern,
    artifact_name: str,
    logger: Optional[logging.Logger] = None,
    residue_check: bool = False,
    residue_cache: Optional[Dict[_ResidueKey, Optional[str]]] = None,
    residue_incomplete: Optional[Dict[str, list]] = None,
) -> Any:
    # scene_prompts 只替换 image_prompt 正文，scene_id/source_node/copy_count
    # 是机器标识，不能走整树替换以免被误改。
    if artifact_name == SCENE_PROMPTS_TABLE:
        return replace_scene_prompts(data, replacements, pattern)

    changed = _replace_json_tree(data, replacements, pattern, logger)

    # 规则替换之后再让模型复查裸姓氏残留（"我姓李""方氏的少主"）。
    # 只对正文类产物开启：变量名同步、机器标识类产物没有正文，跑它没有意义。
    if residue_check:
        changed, _ = anonymize_residual_fragments(
            changed,
            mappings,
            replacements,
            pattern,
            logger,
            cache=residue_cache,
            incomplete=(
                None
                if residue_incomplete is None
                else residue_incomplete.setdefault(artifact_name, [])
            ),
        )

    # Ensure plot records with a full_name always point to the changed ID,
    # including legacy data that had an empty or malformed speaker_id.
    if artifact_name == "plots":
        changed_conversion = normalise_mappings(mappings)["conversion"]

        def fill_speaker_id(value: Any) -> None:
            if isinstance(value, list):
                for item in value:
                    fill_speaker_id(item)
                return
            if not isinstance(value, dict):
                return
            full_name = value.get("full_name")
            if isinstance(full_name, str) and full_name:
                value["speaker_id"] = changed_conversion.get(
                    full_name,
                    value.get("speaker_id") or _to_pascal_case_id(full_name),
                )
            for item in value.values():
                if isinstance(item, (dict, list)):
                    fill_speaker_id(item)

        fill_speaker_id(changed)

    if artifact_name == "character_styles":
        _refresh_character_styles_hash(changed, mappings)
    return changed


def replace_scene_prompts(
    data: Any,
    replacements: Mapping[str, str],
    pattern,
) -> Any:
    """只替换 scene_prompts 里每个 scene 的 image_prompt 正文。

    scene_id（键）、source_node、copy_count 是机器标识，保持原样；只对
    image_prompt 字符串做人名替换。data 结构：{scene_id: {source_node,
    image_prompt, copy_count}}。结构异常时原样返回，绝不阻断整步换名。
    """
    if not isinstance(data, dict):
        return data
    result: Dict[str, Any] = {}
    for scene_id, record in data.items():
        if not isinstance(record, dict):
            result[scene_id] = record
            continue
        new_record = dict(record)
        prompt = new_record.get("image_prompt")
        if isinstance(prompt, str):
            new_record["image_prompt"] = _replace_with_pattern(
                prompt, replacements, pattern
            )
        result[scene_id] = new_record
    return result


def _scene_prompts_image_text(data: Any) -> str:
    """把所有 image_prompt 拼成一段文本，供写后校验只针对正文扫描。"""
    if not isinstance(data, dict):
        return ""
    parts = []
    for record in data.values():
        if isinstance(record, dict):
            prompt = record.get("image_prompt")
            if isinstance(prompt, str):
                parts.append(prompt)
    return "\n".join(parts)


def anonymize_plots(
    plots_data: Any,
    mappings: Optional[Mapping[str, Any]],
    alias_replacements: Optional[Mapping[str, str]] = None,
    logger: Optional[logging.Logger] = None,
) -> Any:
    """Compatibility entry point for applying all plot replacements."""
    if not isinstance(mappings, Mapping):
        return plots_data
    try:
        replacements = replacement_map(
            mappings,
            alias_replacements,
            include_speaker_ids=True,
            logger=logger,
        )
        pattern = _compile_replacement_pattern(replacements)
        changed = _replace_artifact(
            plots_data,
            mappings,
            replacements,
            pattern,
            "plots",
            logger,
        )
    except Exception as exc:
        if logger:
            logger.warning(f"⚠️ plots 内存数据换名失败，保留原数据并继续: {exc}")
        return plots_data
    if isinstance(plots_data, dict) and isinstance(changed, dict):
        plots_data.clear()
        plots_data.update(changed)
        return plots_data
    if isinstance(plots_data, list) and isinstance(changed, list):
        plots_data[:] = changed
        return plots_data
    return changed


def anonymize_plots_for_task(
    plots_data: Any,
    mappings: Optional[Mapping[str, Any]],
    task_id: str,
    outputs_path: str,
    logger: Optional[logging.Logger] = None,
) -> Any:
    alias_replacements = load_name_replacements(task_id, outputs_path, logger)
    return anonymize_plots(plots_data, mappings, alias_replacements, logger)


def _is_missing_table_error(exc: BaseException) -> bool:
    """仅识别「表/视图不存在」。Postgres SQLSTATE 42P01。"""
    for error in (getattr(exc, "orig", None), exc):
        if error is None:
            continue
        if getattr(error, "pgcode", None) == "42P01":
            return True
        message = str(error).lower()
        if "does not exist" in message and "relation" in message:
            return True
        if "undefinedtable" in type(error).__name__.lower():
            return True
    return False


def _decode_json_data(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


# ============================================================
# 换名状态表（name_anonymizer_state）
#
# 只有本模块读写。记录「用哪套规则、对什么内容」换过名，而不是简单的
# 布尔标志——plots 等表还有别的写入方（Step 3/8/12、regenerate_audio），
# 布尔值会在别人改动正文后说谎，导致漏脱敏。内容指纹则会自然失配。
# 表不存在时全部降级为「无状态」，行为退回到逐产物内容比对。
# ============================================================

_STATE_TABLE = "name_anonymizer_state"


def _digest(value: Any) -> str:
    """对任意 JSON 可序列化结构取稳定哈希（排序键，保证同内容同哈希）。"""
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _mappings_digest(replacements: Mapping[str, str]) -> str:
    """替换规则集合的哈希；Step 5/6 重生成映射后会变化。"""
    return _digest(dict(replacements))


def load_rename_state(
    engine, task_id: str, logger: Optional[logging.Logger] = None
) -> Dict[str, Dict[str, Any]]:
    """读取该任务已记录的换名状态，返回 {artifact: {...}}。"""
    if engine is None:
        return {}
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT artifact, mappings_digest, content_digest, verified "
                    f"FROM {_STATE_TABLE} WHERE task_id = :tid"
                ),
                {"tid": task_id},
            ).fetchall()
    except Exception as exc:
        if logger:
            reason = "状态表不存在" if _is_missing_table_error(exc) else "读取失败"
            logger.warning(
                f"⚠️ 换名状态读取跳过（{reason}），本次按内容比对判断: {exc}"
            )
        return {}
    return {
        row[0]: {
            "mappings_digest": row[1],
            "content_digest": row[2],
            "verified": bool(row[3]),
        }
        for row in rows
    }


def _state_allows_skip(
    state: Mapping[str, Dict[str, Any]],
    artifact: str,
    mappings_digest: str,
    content: Any,
) -> bool:
    """已验证 + 规则未变 + 内容未被改动，才认为该产物换名已确认完成。"""
    entry = state.get(artifact)
    if not entry or not entry.get("verified"):
        return False
    if entry.get("mappings_digest") != mappings_digest:
        return False
    return entry.get("content_digest") == _digest(content)


def record_rename_state(
    engine,
    task_id: str,
    artifact: str,
    mappings_digest: str,
    content: Any,
    replacement_count: int,
    verified: bool,
    logger: Optional[logging.Logger] = None,
) -> bool:
    """写回换名状态。失败只告警——状态表是加速与审计手段，不是业务结果。"""
    if engine is None:
        return False
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"INSERT INTO {_STATE_TABLE} ("
                    "  task_id, artifact, mappings_digest, content_digest,"
                    "  replacement_count, verified, renamed_at"
                    ") VALUES ("
                    "  :tid, :artifact, :mdigest, :cdigest,"
                    "  :count, :verified, :now"
                    ") ON CONFLICT (task_id, artifact) DO UPDATE SET"
                    "  mappings_digest = EXCLUDED.mappings_digest,"
                    "  content_digest = EXCLUDED.content_digest,"
                    "  replacement_count = EXCLUDED.replacement_count,"
                    "  verified = EXCLUDED.verified,"
                    "  renamed_at = EXCLUDED.renamed_at"
                ),
                {
                    "tid": task_id,
                    "artifact": artifact,
                    "mdigest": mappings_digest,
                    "cdigest": _digest(content),
                    "count": replacement_count,
                    "verified": verified,
                    # 用统一的北京时间，避免容器漏配 TZ 时 renamed_at 早 8 小时。
                    "now": now_local(),
                },
            )
        return True
    except Exception as exc:
        if logger:
            reason = "状态表不存在" if _is_missing_table_error(exc) else "写入失败"
            logger.warning(
                f"⚠️ {artifact} 换名状态未记录（{reason}），不影响换名结果: {exc}"
            )
        return False


def find_unreplaced_names(
    data: Any,
    replacements: Mapping[str, str],
    pattern=None,
) -> list:
    """扫描产物里"本该换却没换"的原名。

    判据是把同一套规则再跑一遍：内容还会变，说明确实有漏换的位置。这样自动
    继承替换侧的全部约定——标识符边界（``Qin`` 不匹配 ``QinShu``）、
    ``publication`` / ``speaker_combination`` 字段豁免——不会因为两边标准
    不一致而误报。

    早期版本用裸子串匹配（``old in haystack``），比替换标准更严，会在三种
    正常情况下误报：短名是长名的前缀、新名本身含旧名、循环规则双方互换。
    """
    targets = {
        old: new for old, new in replacements.items()
        if isinstance(old, str) and isinstance(new, str) and old and old != new
    }
    if not targets:
        return []
    if pattern is None:
        pattern = _compile_replacement_pattern(targets)
    if pattern is None:
        return []

    # 已经换成新名的位置会被再次匹配：新名本身含旧名（卫队 -> 卫队队长），
    # 或循环规则双方互换（A -> B 且 B -> A）。这两种都是替换正确执行的结果，
    # 把「旧名同时也是某条规则的新名」排除掉，只留真正可能漏换的规则。
    new_names = set(targets.values())
    checkable = {
        old: new for old, new in targets.items()
        if not any(old in new_name for new_name in new_names)
    }
    if not checkable:
        return []

    residual = []
    for old, new in checkable.items():
        single_pattern = _compile_replacement_pattern({old: new})
        if single_pattern is None:
            continue
        if _replace_json_tree(data, {old: new}, single_pattern) != data:
            residual.append(old)
    return sorted(residual)


def _verify_artifact(
    artifact: str,
    data: Any,
    replacements: Mapping[str, str],
    logger: Optional[logging.Logger],
    pattern=None,
) -> bool:
    """确认产物里已没有该换却没换的原名。这是「名称确实换了」的判据。"""
    residual = find_unreplaced_names(data, replacements, pattern)
    if not residual:
        return True
    if logger:
        preview = residual[:8]
        logger.warning(
            f"⚠️ {artifact} 换名后仍残留 {len(residual)} 个原名，"
            f"未标记为已完成（下次重跑会重试）: {preview}"
            f"{'...' if len(residual) > 8 else ''}"
        )
    return False


def _load_mappings(
    engine,
    task_id: str,
    local_path: Path,
    logger: Optional[logging.Logger],
) -> Any:
    if engine is not None:
        try:
            with engine.connect() as connection:
                row = connection.execute(
                    text("SELECT data FROM name_mappings WHERE task_id = :tid LIMIT 1"),
                    {"tid": task_id},
                ).fetchone()
            if row and row[0] is not None:
                return _decode_json_data(row[0])
        except Exception as exc:
            if logger:
                logger.warning(f"⚠️ 从 name_mappings 表读取失败，将尝试本地文件: {exc}")

    if local_path.exists():
        try:
            with local_path.open("r", encoding="utf-8") as file:
                return json.load(file)
        except (OSError, json.JSONDecodeError) as exc:
            if logger:
                logger.warning(f"⚠️ 本地 name_mappings 读取失败，已跳过换名: {exc}")
    return None


def _plan_database_artifact(
    engine,
    table_name: str,
    task_id: str,
    mappings: Mapping[str, Any],
    replacements: Mapping[str, str],
    pattern,
    logger: Optional[logging.Logger],
    state: Optional[Mapping[str, Dict[str, Any]]] = None,
    mappings_digest: str = "",
    unrecorded_ok: Optional[list] = None,
    residue_check: bool = False,
    residue_cache: Optional[Dict[_ResidueKey, Optional[str]]] = None,
    residue_incomplete: Optional[Dict[str, list]] = None,
) -> Tuple[bool, bool, Optional[str]]:
    """只读取并计算换名结果，不写库。

    返回 (exists, changed, payload)。全部产物都算完并校验通过后才统一写入，
    避免中途失败留下"一半产物已换名、一半未换"的不一致状态。

    若状态表确认该产物已用同一套规则换名、且内容未被其他写入方改动过，
    直接跳过换名计算。
    """
    if engine is None:
        if logger:
            logger.warning(f"⚠️ 跳过 {table_name} 表的人名替换：数据库引擎不可用")
        return False, False, None

    row_exists = False
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text(f"SELECT data FROM {table_name} WHERE task_id = :tid LIMIT 1"),
                {"tid": task_id},
            ).fetchone()
        if not row or row[0] is None:
            return False, False, None
        row_exists = True
    except Exception as exc:
        if logger:
            reason = "表不存在" if _is_missing_table_error(exc) else "读取失败"
            logger.warning(f"⚠️ 跳过 {table_name} 表的人名替换（{reason}）: {exc}")
        return False, False, None

    try:
        current = _decode_json_data(row[0])
        if state and _state_allows_skip(
            state, table_name, mappings_digest, current
        ):
            if logger:
                logger.info(f"⏭️ {table_name} 换名已确认完成且内容未变，跳过重算")
            return True, False, None
        changed_data = _replace_artifact(
            current,
            mappings,
            replacements,
            pattern,
            table_name,
            logger,
            residue_check=residue_check,
            residue_cache=residue_cache,
            residue_incomplete=residue_incomplete,
        )
        if current == changed_data:
            # 内容已是目标态但状态表还没记录（首次启用状态表、或上次校验没过）。
            # 交给阶段三校验并补记，避免此后每次重跑都要重算这个产物。
            if unrecorded_ok is not None:
                unrecorded_ok.append((table_name, current, table_name))
            return True, False, None
        return True, True, json.dumps(changed_data, ensure_ascii=False)
    except Exception as exc:
        if logger:
            logger.warning(
                f"⚠️ {table_name} 表数据无法完成换名，已跳过该产物并继续: {exc}"
            )
        return row_exists, False, None


def _commit_database_artifact(engine, table_name: str, task_id: str, payload: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                f"UPDATE {table_name} "
                "SET data = CAST(:data AS jsonb) WHERE task_id = :tid"
            ),
            {"tid": task_id, "data": payload},
        )


def local_state_key(filename: str) -> str:
    """本地产物在状态表里的标识。加 file: 前缀避免与表名撞车。"""
    return f"file:{filename}"


def _residue_failure_reason(
    residue_incomplete: Mapping[str, Sequence[str]],
    artifact_name: str,
) -> Optional[str]:
    """该产物的裸姓氏复查是否没跑完；没跑完就返回第一条原因。

    裸姓氏残留不在 rules 里，_verify_artifact 看不见它。若照常记为已验证，
    下次重跑会因"规则和内容都没变"而跳过这个产物，残留就永久留在成品里。

    只看**本产物自己**的失败：换名的正确性逐产物成立，别的产物没判完不影响
    这一份的结论。曾经这里用的是一个所有产物共用的 list，只要任何产物失败，
    每个正文产物都会被判为未验证、下次全部重问一遍模型。
    """
    if artifact_name not in PROSE_ARTIFACTS:
        return None
    reasons = residue_incomplete.get(artifact_name) or ()
    return reasons[0] if reasons else None


def _plan_local_artifacts(
    output_dir: Path,
    artifact_name: str,
    filenames: Sequence[str],
    mappings: Mapping[str, Any],
    replacements: Mapping[str, str],
    pattern,
    logger: Optional[logging.Logger],
    state: Optional[Mapping[str, Dict[str, Any]]] = None,
    mappings_digest: str = "",
    unrecorded_ok: Optional[list] = None,
    residue_check: bool = False,
    residue_cache: Optional[Dict[_ResidueKey, Optional[str]]] = None,
    residue_incomplete: Optional[Dict[str, list]] = None,
) -> Tuple[bool, bool, list]:
    """只读取并计算本地产物的换名结果，返回 (exists, changed, 待写入列表)。

    状态表按文件名逐个记录，因此同一 artifact 的多个候选文件互不影响。
    """
    exists = False
    pending = []
    for filename in filenames:
        path = output_dir / filename
        if not path.exists():
            continue
        exists = True
        try:
            with path.open("r", encoding="utf-8") as file:
                current = json.load(file)
            if state and _state_allows_skip(
                state, local_state_key(filename), mappings_digest, current
            ):
                if logger:
                    logger.info(f"⏭️ 本地产物 {filename} 换名已确认完成，跳过重算")
                continue
            changed_data = _replace_artifact(
                current,
                mappings,
                replacements,
                pattern,
                artifact_name,
                logger,
                residue_check=residue_check,
                residue_cache=residue_cache,
                residue_incomplete=residue_incomplete,
            )
            if current == changed_data:
                # 已是目标态但状态表未记录，交给阶段三补记。
                if unrecorded_ok is not None:
                    unrecorded_ok.append(
                        (local_state_key(filename), current, artifact_name)
                    )
                continue
            pending.append((path, changed_data))
        except Exception as exc:
            if logger:
                logger.warning(
                    f"⚠️ 本地产物 {path} 无法完成换名，已跳过该文件并继续: {exc}"
                )
    return exists, bool(pending), pending


def _commit_local_artifact(path: Path, changed_data: Any) -> None:
    # 先写临时文件再原子替换，避免中途失败留下截断的 JSON。
    temp_path = path.with_name(path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(changed_data, file, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)


def _create_engine_safely(
    database_url: str,
    label: str,
    logger: Optional[logging.Logger],
):
    try:
        return create_engine(database_url, pool_pre_ping=True)
    except Exception as exc:
        if logger:
            logger.warning(f"⚠️ {label}数据库引擎创建失败，将跳过对应数据库产物: {exc}")
        return None


def process_task(
    task_id: str,
    outputs_path: str = OUTPUTS_PATH,
    logger: Optional[logging.Logger] = None,
) -> Dict[str, Any]:
    """Run Step 16 by applying name_mappings exactly as provided."""
    logger = logger or _DEFAULT_LOGGER
    output_dir = Path(outputs_path) / str(task_id)
    logger.info(f"🔒 开始第 16 步全面换名: task_id={task_id}, 产物目录={output_dir}")
    # 熔断是"本次运行"的状态：worker 进程连跑多个任务时，上一个任务遇到的
    # 模型故障不该让这个任务直接放弃复查。
    reset_residue_circuit()
    platform_engine = _create_engine_safely(
        PLATFORM_DATABASE_URL, "平台", logger
    )
    business_engine = _create_engine_safely(DATABASE_URL, "业务", logger)

    mappings = _load_mappings(
        platform_engine,
        str(task_id),
        output_dir / "name_mappings.json",
        logger,
    )
    if not isinstance(mappings, dict):
        logger.warning(f"⚠️ 未找到可用的 name_mappings，跳过第 16 步: task_id={task_id}")
        return {
            "task_id": str(task_id),
            "changed": False,
            "changed_artifacts": [],
            "existing_artifacts": [],
            "replacement_count": 0,
            "failed_artifacts": ["name_mappings"],
        }

    aliases = load_name_replacements(str(task_id), outputs_path, logger)
    replacements = replacement_map(
        mappings, aliases, include_speaker_ids=True, logger=logger
    )
    pattern = _compile_replacement_pattern(replacements)
    logger.info(f"✅ 已构建替换规则: {len(replacements)} 条（含主名、别称与 speaker_id）")
    if not replacements:
        # Step 5 降级时可能生成严格的 identity 映射。此时没有任何名字
        # 或 speaker_id 需要替换，继续查询 plots/其他产物只会引入无意义的失败点。
        # 直接把 Step 16 视为成功，保证兜底路径可以继续进入 Step 17。
        logger.warning("⚠️ 替换规则为空，跳过全部产物读写并将第 16 步视为成功")
        return {
            "task_id": str(task_id),
            "changed": False,
            "changed_artifacts": [],
            "existing_artifacts": [],
            "replacement_count": 0,
        }

    # 变量名等机器标识必须与 plots 保持一致，否则分支判定会失配。
    variable_replacements = variable_replacement_map(mappings)
    variable_pattern = _compile_replacement_pattern(variable_replacements)
    logger.info(f"✅ 已构建变量名同步规则: {len(variable_replacements)} 条")

    # scene_prompts 的 image_prompt 是英文正文，混入的人名是中文原名。只用
    # 主名+别称规则，不带 speaker_id/拼音变体——那些在英文正文里易误伤单词。
    scene_prompt_replacements = replacement_map(
        mappings, aliases, include_speaker_ids=False, logger=logger
    )
    scene_prompt_pattern = _compile_replacement_pattern(scene_prompt_replacements)
    digest_of_scene_prompts = _mappings_digest(scene_prompt_replacements)
    logger.info(
        f"✅ 已构建场景提示词换名规则: {len(scene_prompt_replacements)} 条"
        "（仅主名与别称，不含 speaker_id）"
    )

    # 换名状态：确认哪些产物已用同一套规则换过名且内容未被改动。
    # 状态表读不到（未建表/读失败）时退化为纯内容比对，行为与加表前一致。
    rename_state = load_rename_state(platform_engine, str(task_id), logger)
    digest_of_replacements = _mappings_digest(replacements)
    digest_of_variables = _mappings_digest(variable_replacements)
    if rename_state:
        logger.info(f"📖 已加载 {len(rename_state)} 条换名状态记录")

    # 裸姓氏判断结果在本次运行内共享：plots 同时是数据库表和本地文件，
    # 同一份内容走两条路径，缓存保证只问模型一次、两处结果一致。
    residue_cache: Dict[_ResidueKey, Optional[str]] = {}
    # 模型未能完整判断裸姓氏时记在这里，阶段三据此不把正文产物记为已验证，
    # 下次重跑会重新复查。
    # 按 artifact_name 分桶：一个正文产物复查失败不能连带另一个。character_styles
    # 的批次数比 plots 高一个量级（实测某任务 25 批 vs 4 批），撞上"至少一批失败"
    # 的概率高得多，共用一个标记会让 plots 明明判完了也要全量重跑。
    # 分桶键用 artifact_name 而不是 state_key：plots 同时是库表和本地文件，两条
    # 路径共享 residue_cache、判断结果一致，所以它们本就该共享失败状态。
    residue_incomplete: Dict[str, list] = {}

    planned_changed_artifacts = set()
    existing_artifacts = set()
    db_pending = []
    local_pending = []
    # 内容已是目标态但状态表尚无记录的产物，阶段三统一校验并补记。
    unrecorded_ok = []

    # 阶段一：逐项读取和计算；单项异常只跳过该产物，不影响其他产物。
    for engine, table_names, source in (
        (platform_engine, PLATFORM_TABLES, "platform"),
        (business_engine, BUSINESS_TABLES, "business"),
    ):
        for table_name in table_names:
            exists, changed, payload = _plan_database_artifact(
                engine,
                table_name,
                str(task_id),
                mappings,
                replacements,
                pattern,
                logger,
                state=rename_state,
                mappings_digest=digest_of_replacements,
                unrecorded_ok=unrecorded_ok,
                residue_check=table_name in PROSE_ARTIFACTS,
                residue_cache=residue_cache,
                residue_incomplete=residue_incomplete,
            )
            if exists:
                existing_artifacts.add(table_name)
                logger.info(
                    f"{'📝 待更新' if changed else '⏭️ 无需改动'} "
                    f"数据库产物 {source}.{table_name}"
                )
            else:
                logger.debug(f"➖ 数据库产物不存在: {source}.{table_name}")
            if changed:
                planned_changed_artifacts.add(table_name)
                db_pending.append(
                    (engine, table_name, payload, f"db:{source}.{table_name}")
                )

    # scene_prompts：只替换 image_prompt 正文，用不含 speaker_id 的主名+别称规则。
    # _plan_database_artifact 内部经 _replace_artifact 按表名分派到 replace_scene_prompts。
    sp_exists, sp_changed, sp_payload = _plan_database_artifact(
        platform_engine,
        SCENE_PROMPTS_TABLE,
        str(task_id),
        mappings,
        scene_prompt_replacements,
        scene_prompt_pattern,
        logger,
        state=rename_state,
        mappings_digest=digest_of_scene_prompts,
        unrecorded_ok=unrecorded_ok,
    )
    if sp_exists:
        existing_artifacts.add(SCENE_PROMPTS_TABLE)
        logger.info(
            f"{'📝 待更新' if sp_changed else '⏭️ 无需改动'} "
            f"数据库产物 platform.{SCENE_PROMPTS_TABLE}"
        )
    else:
        logger.debug(f"➖ 数据库产物不存在: platform.{SCENE_PROMPTS_TABLE}")
    if sp_changed:
        planned_changed_artifacts.add(SCENE_PROMPTS_TABLE)
        db_pending.append(
            (platform_engine, SCENE_PROMPTS_TABLE, sp_payload,
             f"db:platform.{SCENE_PROMPTS_TABLE}")
        )

    # 只同步变量名的产物：规则集仅含 speaker_id 及其拼音变体，不动正文人名。
    for table_name in VARIABLE_ONLY_PLATFORM_TABLES:
        exists, changed, payload = _plan_database_artifact(
            platform_engine,
            table_name,
            str(task_id),
            mappings,
            variable_replacements,
            variable_pattern,
            logger,
            state=rename_state,
            mappings_digest=digest_of_variables,
            unrecorded_ok=unrecorded_ok,
        )
        if exists:
            existing_artifacts.add(table_name)
            logger.info(
                f"{'📝 待同步变量名' if changed else '⏭️ 变量名已一致'} "
                f"数据库产物 platform.{table_name}"
            )
        else:
            logger.debug(f"➖ 数据库产物不存在: platform.{table_name}")
        if changed:
            planned_changed_artifacts.add(table_name)
            db_pending.append(
                (platform_engine, table_name, payload, f"db:platform.{table_name}")
            )

    for artifact_name, filenames in VARIABLE_ONLY_LOCAL_ARTIFACTS.items():
        exists, changed, pending = _plan_local_artifacts(
            output_dir,
            artifact_name,
            filenames,
            mappings,
            variable_replacements,
            variable_pattern,
            logger,
            state=rename_state,
            mappings_digest=digest_of_variables,
            unrecorded_ok=unrecorded_ok,
        )
        if exists:
            existing_artifacts.add(artifact_name)
            logger.info(
                f"{'📝 待同步变量名' if changed else '⏭️ 变量名已一致'} "
                f"本地产物 {artifact_name}"
            )
        else:
            logger.debug(f"➖ 本地产物不存在: {artifact_name}")
        if changed:
            planned_changed_artifacts.add(artifact_name)
            local_pending.extend(
                (path, changed_data, artifact_name, f"file:{path}")
                for path, changed_data in pending
            )

    for artifact_name, filenames in LOCAL_ARTIFACTS.items():
        exists, changed, pending = _plan_local_artifacts(
            output_dir,
            artifact_name,
            filenames,
            mappings,
            replacements,
            pattern,
            logger,
            state=rename_state,
            mappings_digest=digest_of_replacements,
            unrecorded_ok=unrecorded_ok,
            residue_check=artifact_name in PROSE_ARTIFACTS,
            residue_cache=residue_cache,
            residue_incomplete=residue_incomplete,
        )
        if exists:
            existing_artifacts.add(artifact_name)
            logger.info(
                f"{'📝 待更新' if changed else '⏭️ 无需改动'} 本地产物 {artifact_name}"
            )
        else:
            logger.debug(f"➖ 本地产物不存在: {artifact_name}")
        if changed:
            planned_changed_artifacts.add(artifact_name)
            local_pending.extend(
                (path, changed_data, artifact_name, f"file:{path}")
                for path, changed_data in pending
            )

    if "plots" not in existing_artifacts:
        logger.warning(
            f"⚠️ 未找到可用的 plots，仍继续处理其他已有产物: task_id={task_id}"
        )

    # 阶段二：各产物独立落盘。单项失败只记录并跳过，不阻断其他产物。
    changed_artifacts = set()
    failed_artifacts = []
    # (状态表标识, 写入后的内容, 产物名) —— 供阶段三校验与记录状态。
    written_artifacts = []
    for engine, table_name, payload, location in db_pending:
        try:
            _commit_database_artifact(engine, table_name, str(task_id), payload)
            changed_artifacts.add(table_name)
            written_artifacts.append(
                (table_name, _decode_json_data(payload), table_name)
            )
        except Exception as exc:
            failed_artifacts.append(location)
            logger.warning(
                f"⚠️ {location} 换名结果写入失败，已跳过并继续其他产物: {exc}"
            )
    for path, changed_data, artifact_name, location in local_pending:
        try:
            _commit_local_artifact(path, changed_data)
            changed_artifacts.add(artifact_name)
            written_artifacts.append(
                (local_state_key(path.name), changed_data, artifact_name)
            )
        except Exception as exc:
            failed_artifacts.append(location)
            logger.warning(
                f"⚠️ {location} 换名结果写入失败，已跳过并继续其他产物: {exc}"
            )

    # 阶段三：写后校验 + 记录状态。
    # 「名称确实换了」的判据是产物里不再残留任何待替换原名，而不是写入成功。
    # 校验不通过就不记状态，下次重跑会重新处理该产物。
    unverified_artifacts = []
    for state_key, written_data, artifact_name in written_artifacts + unrecorded_ok:
        is_variable = (
            artifact_name in VARIABLE_ONLY_PLATFORM_TABLES
            or artifact_name in VARIABLE_ONLY_LOCAL_ARTIFACTS
        )
        is_scene_prompts = artifact_name == SCENE_PROMPTS_TABLE
        if is_variable:
            rules, verify_pattern = variable_replacements, variable_pattern
        elif is_scene_prompts:
            rules, verify_pattern = scene_prompt_replacements, scene_prompt_pattern
        else:
            rules, verify_pattern = replacements, pattern

        # scene_prompts 只校验 image_prompt 正文，避免整树扫到 scene_id/
        # source_node 里的地名拼音而误报残留。
        verify_target = (
            _scene_prompts_image_text(written_data)
            if is_scene_prompts
            else written_data
        )
        verified = _verify_artifact(
            state_key, verify_target, rules, logger, pattern=verify_pattern,
        )
        residue_failure = _residue_failure_reason(residue_incomplete, artifact_name)
        if verified and residue_failure:
            verified = False
            logger.warning(
                f"⚠️ {state_key} 的裸姓氏复查未完成（{residue_failure}），"
                "本次不标记为已验证，下次重跑会重新复查"
            )
        if not verified:
            unverified_artifacts.append(state_key)
        record_rename_state(
            platform_engine,
            str(task_id),
            state_key,
            _mappings_digest(rules),
            written_data,
            len(rules),
            verified,
            logger,
        )

    changed_list = sorted(changed_artifacts)
    logger.info(
        f"🔒 第 16 步全面换名完成: {len(replacements)} 条规则；"
        f"已检查 {len(existing_artifacts)} 类产物；"
        f"已更新 {', '.join(changed_list) if changed_list else '无'}；"
        f"失败 {len(failed_artifacts)} 项；"
        f"未通过校验 {len(unverified_artifacts)} 项"
    )
    return {
        "task_id": str(task_id),
        "changed": bool(changed_artifacts),
        "changed_artifacts": changed_list,
        "existing_artifacts": sorted(existing_artifacts),
        "replacement_count": len(replacements),
        "planned_changed_artifacts": sorted(planned_changed_artifacts),
        "failed_artifacts": failed_artifacts,
        "unverified_artifacts": unverified_artifacts,
    }


if __name__ == '__main__':
    cli_task_id = ""
    try:
        summary = process_task(task_id=cli_task_id)
        _DEFAULT_LOGGER.info(f"📋 执行结果: {json.dumps(summary, ensure_ascii=False)}")
    except Exception as exc:
        _DEFAULT_LOGGER.error(
            f"⚠️ 第 16 步发生未预期异常，但脚本不以失败状态退出: {exc}",
            exc_info=True,
        )
