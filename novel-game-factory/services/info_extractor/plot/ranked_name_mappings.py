"""Generate ``change_name`` after character ``important_rank`` is available.

Step 2 creates only ``original_name.conversion``.  This module is the later,
independent rename step: it reads ranked characters, tries to rename every
character, gives every rank above 1 three total attempts and a surname-change
fallback, links descriptive names such as ``凌妙妙的丫鬟`` to their important
owner, and writes the completed ``change_name`` section back to the same
name_mappings record and local file.

Every character degrades independently to ``old_name -> old_name``.  Model,
translation, parsing, or individual validation failures never abort the rename
of other characters.
"""

import json
import logging
import os
import argparse
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from openai import OpenAI
from sqlalchemy import create_engine, text

from common.config import OUTPUTS_PATH, PLATFORM_DATABASE_URL
from common.llm_fallback import ModelFallback
from common.model_json import parse_model_json
from common.generic_appellations import (
    GENERIC_ROLE_WORDS,
    GENERIC_STANDALONE,
    INTERPUNCT_RE,
    family_segment,
    is_generic_appellation,
    split_person_name,
    split_surname_and_role,
    split_transliterated_name,
)
try:
    from common.generic_appellations import split_confident_person_name
except ImportError:  # compatibility with lightweight test/import stubs
    split_confident_person_name = split_person_name
from info_extractor.character.name_mappings import (
    _build_conversion_for_names,
    _save_replacements_to_local,
    _save_to_db,
    _save_to_local,
)

try:
    from common.usage_tracker import tracker as _usage_tracker
except ImportError:
    _usage_tracker = None


logger = logging.getLogger(__name__)

MODEL = "qwen3.7-max"
RENAME_MAX_ATTEMPTS = 3
LLM_MAX_RETRIES = 1
# 主模型偶发不稳定时自动改用备选模型重发同一请求（同中转站、同调用参数）。
_model_fallback = ModelFallback(MODEL, name="RankedRename", logger=logger)
LLM_TIMEOUT_BASE_SECONDS = 60.0
LLM_TIMEOUT_PER_NAME_SECONDS = 8.0
LLM_TIMEOUT_MAX_SECONDS = 1200.0
TRANSLATION_BATCH_SIZE = 25
TRANSLATION_TIMEOUT_MAX_SECONDS = 180.0
FALLBACK_SURNAMES = (
    "沈", "苏", "林", "顾", "陆", "江", "周", "许", "程", "宋",
    "秦", "萧", "叶", "温", "谢", "傅", "唐", "白", "韩", "夏",
)
# 音译名的家族名兜底池。上面的单字汉姓套到"林雷·巴鲁克"上会拼出"林雷·沈"，
# 破坏西幻/外国题材的名字风格，所以音译名单独用音译风格的多字候选。
FALLBACK_FAMILY_NAMES = (
    "巴罗特", "维恩", "德拉克", "凯尔文", "奥兰", "费尔南",
    "洛伦", "格温", "塞拉斯", "瓦伦", "诺德", "艾伯特",
)

# 翻译失败时不对单个称谓维护词典；统一按批次重试并显式降级。
''' 英文翻译的确定性兜底。名称翻译接口不可用时，泛称不能退化为
# ``LZ`` 之类的拼音缩写，否则会把叙事中的身份信息丢掉。
EN_GENERIC_NAME_MAP = {
    "老者": "the old man", "老人": "the elderly man", "老头": "the old man",
    "老妇": "the old woman", "老婆婆": "the old woman", "老太太": "the old woman",
    "孩子": "the child", "娃子": "the child", "男人": "the man", "女人": "the woman",
    "男子": "the man", "女子": "the woman", "汉子": "the man", "壮汉": "the burly man",
    "少年": "the young man", "少女": "the young woman", "青年": "the young man",
    "姑娘": "the young woman", "美人": "the beauty", "佳人": "the beauty",
    "书生": "the scholar", "秀才": "the scholar", "仙女": "the fairy",
    "仙子": "the immortal", "路人": "a passerby", "陌生人": "a stranger",
    "旁白": "narrator", "画外音": "voice-over", "声音": "voice",
}

EN_ROLE_MAP = {
    "司机": "driver", "医生": "doctor", "护士": "nurse", "警察": "police officer",
    "老师": "teacher", "教授": "professor", "校长": "principal", "主任": "director",
    "老板": "boss", "秘书": "secretary", "律师": "lawyer", "记者": "reporter",
    "歌手": "singer", "演员": "actor", "厨师": "chef", "服务员": "server",
    "保安": "security guard", "队长": "captain", "班长": "class monitor",
    "将军": "general", "掌柜": "shopkeeper", "小二": "waiter", "郎中": "physician",
    "车夫": "carriage driver", "丫鬟": "maid", "侍女": "maid", "侍从": "attendant",
    "书童": "page", "家丁": "house servant", "管家": "steward", "护卫": "guard",
    "保镖": "bodyguard", "随从": "attendant", "仆人": "servant", "婢女": "maid",
    "小厮": "manservant", "师父": "master", "师傅": "master", "师兄": "senior disciple",
    "师姐": "senior disciple", "师弟": "junior disciple", "师妹": "junior disciple",
    "徒弟": "disciple", "弟子": "disciple", "长老": "elder", "前辈": "senior",
    "大人": "lord", "小姐": "miss", "公子": "young master", "先生": "mister",
    "夫人": "madam", "老爷": "master", "少爷": "young master", "公主": "princess",
    "王妃": "princess consort", "陛下": "your majesty", "殿下": "your highness",
}

JA_GENERIC_NAME_MAP = {
    "老者": "老人", "老人": "老人", "老头": "老人",
    "老妇": "老女", "老婆婆": "老婆", "老太太": "おばあさん",
    "孩子": "子ども", "娃子": "子ども", "男人": "男", "女人": "女",
    "男子": "男", "女子": "女", "汉子": "男", "壮汉": "大男",
    "少年": "少年", "少女": "少女", "青年": "青年", "姑娘": "娘さん",
    "美人": "美女", "佳人": "美女", "书生": "書生", "秀才": "秀才",
    "仙女": "仙女", "仙子": "仙人", "路人": "通行人", "陌生人": "見知らぬ人",
    "旁白": "ナレーション", "画外音": "ナレーション", "声音": "声",
}

JA_ROLE_MAP = {
    "司机": "運転手", "医生": "医者", "护士": "看護師", "警察": "警察官",
    "老师": "先生", "教授": "教授", "校长": "校長", "主任": "主任",
    "老板": "主人", "秘书": "秘書", "律师": "弁護士", "记者": "記者",
    "歌手": "歌手", "演员": "俳優", "厨师": "料理人", "服务员": "店員",
    "保安": "警備員", "队长": "隊長", "将军": "将軍", "掌柜": "店主",
    "小二": "店員", "郎中": "医者", "车夫": "御者", "丫鬟": "侍女",
    "侍女": "侍女", "侍从": "従者", "书童": "小姓", "家丁": "家僕",
    "管家": "執事", "护卫": "護衛", "保镖": "ボディーガード", "随从": "従者",
    "仆人": "使用人", "婢女": "侍女", "小厮": "小間使い", "师父": "師匠",
    "师傅": "師匠", "师兄": "兄弟子", "师姐": "姉弟子", "师弟": "弟弟子",
    "师妹": "妹弟子", "徒弟": "弟子", "弟子": "弟子", "长老": "長老",
    "前辈": "先輩", "大人": "様", "小姐": "お嬢様", "公子": "若様",
    "先生": "先生", "夫人": "夫人", "老爷": "旦那様", "少爷": "若様",
    "公主": "姫", "王妃": "王妃", "陛下": "陛下", "殿下": "殿下",
}


def _japanese_fallback(name: str) -> Optional[str]:
    value = str(name or "").strip()
    if value in JA_GENERIC_NAME_MAP:
        return JA_GENERIC_NAME_MAP[value]
    if value in JA_ROLE_MAP:
        return JA_ROLE_MAP[value]
    if value in {"三名老者", "两名老者", "几名老者"}:
        count = {"两": "二人の", "三": "三人の", "几": "数人の"}[value[0]]
        return f"{count}老人"
    if "的" in value:
        owner, role = value.rsplit("的", 1)
        role_ja = JA_ROLE_MAP.get(role)
        if role_ja:
            return f"{owner}の{role_ja}"
    surname_role = split_surname_and_role(value)
    if surname_role:
        surname, role = surname_role
        return f"{surname}{JA_ROLE_MAP.get(role, role)}"
    return None


def _english_fallback(name: str) -> Optional[str]:
    """Return a semantic English fallback for generic/descriptive appellations."""
    value = str(name or "").strip()
    if value in EN_GENERIC_NAME_MAP:
        return EN_GENERIC_NAME_MAP[value]
    if value in EN_ROLE_MAP:
        return EN_ROLE_MAP[value]

    # 常见数量短语也保留群体语义（如“三名老者”），不能按 speaker ID 处理。
    if value in {"三名老者", "两名老者", "几名老者"}:
        count = value[:1]
        count_en = {"两": "two", "三": "three", "几": "several"}[count]
        return f"the {count_en} old men"

    # “X的Y”描述式称谓：保留关系/身份语义，避免失败时变成拼音缩写。
    if "的" in value:
        owner, role = value.rsplit("的", 1)
        role_en = EN_ROLE_MAP.get(role)
        if role_en:
            try:
                from pypinyin import lazy_pinyin
                owner_en = "".join(part.capitalize() for part in lazy_pinyin(owner))
            except Exception:
                owner_en = owner
            return f"the {role_en} of {owner_en}"

    # “姓+身份词”保留身份词，并将姓氏转为常规英文拼音顺序：Teacher Wang。
    surname_role = split_surname_and_role(value)
    if surname_role:
        surname, role = surname_role
        role_en = EN_ROLE_MAP.get(role, role)
        try:
            from pypinyin import lazy_pinyin
            surname_en = "".join(part.capitalize() for part in lazy_pinyin(surname))
        except Exception:
            surname_en = surname
        return f"{role_en.capitalize()} {surname_en}"
    return None


def _usable_english_translation(value: Any) -> bool:
    """Reject empty/CJK/initial-only model output before applying semantic fallback."""
    if not isinstance(value, str) or not value.strip():
        return False
    text = value.strip()
    if any("\u4e00" <= ch <= "\u9fff" for ch in text):
        return False
    # 两到四个大写字母通常是 speaker ID 缩写（如 LZ、LM），不算称谓翻译。
    if False:
        return False
    return True
'''


def _llm_timeout_for(name_count: int) -> float:
    estimated = LLM_TIMEOUT_BASE_SECONDS + LLM_TIMEOUT_PER_NAME_SECONDS * max(
        name_count, 0
    )
    return min(estimated, LLM_TIMEOUT_MAX_SECONDS)


def to_initials(pascal: str) -> str:
    initials = "".join(ch for ch in pascal if ch.isupper())
    return initials or pascal.upper()


def _normalise_english_name(value: Any) -> str:
    return re.sub(r"[^a-z]", "", str(value or "").lower())


def _is_reversed_english_name(chinese_name: str, english_name: Any) -> bool:
    """Detect only an exact given-name/surname reversal.

    Foreign-style names containing ``·`` and descriptive appellations are left to
    the model; this check is intentionally conservative to avoid rejecting valid
    forms such as ``Xiao Yu`` or ``Lynn Balusen``.
    """
    if "·" in str(chinese_name) or "的" in str(chinese_name):
        return False
    parts = split_confident_person_name(chinese_name)
    if not parts or not isinstance(english_name, str):
        return False
    surname, given_name = parts
    try:
        from pypinyin import lazy_pinyin
        surname_py = "".join(lazy_pinyin(surname)).lower()
        given_py = "".join(lazy_pinyin(given_name)).lower()
    except Exception:
        # 没有拼音库时不做猜测，保留模型结果，避免校验本身阻断流程。
        return False
    actual = _normalise_english_name(english_name)
    return bool(
        surname_py
        and given_py
        and surname_py != given_py
        and actual == given_py + surname_py
        and actual != surname_py + given_py
    )


def _filter_reversed_english(names: List[str], translations: Dict[str, str]) -> Dict[str, str]:
    filtered: Dict[str, str] = {}
    for name, value in translations.items():
        if _is_reversed_english_name(name, value):
            logger.warning("⚠️ 英文姓名顺序反转，舍弃本次结果: %s -> %s", name, value)
            continue
        filtered[name] = value
    return filtered


def has_invalid_english_order(change_name: Mapping[str, Any]) -> bool:
    """Return whether an existing change_name contains an unambiguous reversal."""
    zh_cn = _mapping(change_name.get("zh-CN"))
    en_us = _mapping(change_name.get("en-US"))
    for speaker_id, chinese_name in zh_cn.items():
        if _is_reversed_english_name(str(chinese_name), en_us.get(speaker_id)):
            return True
    return False


def _translate_names_chunk(
    names: List[str], api_key: str
) -> Dict[str, Dict[str, str]]:
    base_url = os.getenv(
        "GPT_IMAGE_BASE_URL", "http://sefutech-api.kube-test.svc:3000"
    ).rstrip("/")
    client = OpenAI(
        api_key=api_key,
        base_url=f"{base_url}/v1",
        timeout=min(_llm_timeout_for(len(names)), TRANSLATION_TIMEOUT_MAX_SECONDS),
        max_retries=0,
    )
    prompt = (
        "翻译以下角色名或称谓（中文），用于小说对白 speaker 显示。"
        "英文（en）规则：中文人名保持中文顺序，姓在前、名在后（凌妙妙→Ling Miaomiao）；"
        "纯泛称和描述式称谓必须意译并保留语义（老者→the old man），"
        "不要输出拼音缩写。姓+身份词要同时保留姓名和身份（王老师→Teacher Wang）。"
        "日文（ja）规则：中文人名使用自然的日语读法/片假名或日文汉字，全文保持一致；"
        "纯泛称、官职、亲属和社会身份必须翻译成自然日语（老者→老人，丫鬟→侍女，"
        "王老师→王先生）；‘X的Y’必须保留所属关系，译为‘XのY’。"
        "不能把描述式称谓当作人名音译，也不要原样返回中文输入。只输出 JSON："
        '{"en": {"中文": "English"}, "ja": {"中文": "日本語"}}。'
        "\n名称列表：" + json.dumps(names, ensure_ascii=False)
    )
    response, used_model = _model_fallback.chat(
        client,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    if _usage_tracker and getattr(response, "usage", None):
        _usage_tracker.add_llm_usage(response.usage, used_model, provider="qwen")
    data = parse_model_json(
        response.choices[0].message.content, context="ranked_rename", logger=logger
    )
    allowed = set(names)

    def clean(value: Any) -> Dict[str, str]:
        return {
            old.strip(): new.strip()
            for old, new in _mapping(value).items()
            if isinstance(old, str)
            and isinstance(new, str)
            and old.strip() in allowed
            and new.strip()
        }

    en = clean(data.get("en"))
    return {"en": _filter_reversed_english(names, en), "ja": clean(data.get("ja"))}


def _translate_names_batch(
    names: List[str], api_key: str
) -> Dict[str, Dict[str, str]]:
    merged: Dict[str, Dict[str, str]] = {"en": {}, "ja": {}}
    for start in range(0, len(names), TRANSLATION_BATCH_SIZE):
        batch = names[start : start + TRANSLATION_BATCH_SIZE]
        translated = None
        last_error = None
        for attempt in range(1, 4):
            try:
                translated = _translate_names_chunk(batch, api_key)
                break
            except Exception as exc:
                last_error = exc
                logger.warning(f"⚠️ 名称翻译第 {attempt}/3 次失败: {exc}")
        if translated is None:
            logger.error(f"❌ 名称翻译批次最终失败，保留未翻译项: {last_error}")
            continue
        merged["en"].update(translated["en"])
        merged["ja"].update(translated["ja"])
    return merged


def _mapping(value: Any) -> Dict:
    return dict(value) if isinstance(value, Mapping) else {}


def _entry(value: Any) -> Dict[str, Any]:
    if isinstance(value, list) and value:
        value = value[0]
    return dict(value) if isinstance(value, Mapping) else {}


def _load_json_row(engine, table_name: str, task_id: str) -> Optional[Dict]:
    with engine.connect() as connection:
        row = connection.execute(
            text(f"SELECT data FROM {table_name} WHERE task_id = :tid LIMIT 1"),
            {"tid": task_id},
        ).fetchone()
    if not row or row[0] is None:
        return None
    data = row[0]
    if isinstance(data, str):
        data = json.loads(data)
    return dict(data) if isinstance(data, Mapping) else None


def _load_name_mappings(engine, task_id: str, outputs_path: str) -> Dict:
    try:
        mappings = _load_json_row(engine, "name_mappings", task_id)
        if mappings is not None:
            return mappings
    except Exception as exc:
        logger.warning(f"⚠️ 从 name_mappings 表读取失败，将尝试本地文件: {exc}")

    local_path = Path(outputs_path) / task_id / "name_mappings.json"
    try:
        with local_path.open("r", encoding="utf-8") as file:
            data = json.load(file)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"⚠️ 本地 name_mappings 读取失败: {exc}")
    return {}


def _collect_ranked_characters(
    characters_data: Mapping[str, Any],
) -> Tuple[Dict[str, List[str]], Dict[str, int]]:
    names_info: Dict[str, List[str]] = {}
    ranks: Dict[str, int] = {}
    for char_key, raw_entry in characters_data.items():
        entry = _entry(raw_entry)
        name = str(entry.get("name") or char_key or "").strip()
        if not name:
            continue
        aliases: List[str] = []
        for field in ("other_names", "aliases", "nicknames", "alias"):
            raw_aliases = entry.get(field)
            if isinstance(raw_aliases, str):
                raw_aliases = [raw_aliases]
            if not isinstance(raw_aliases, list):
                continue
            for alias in raw_aliases:
                if (
                    isinstance(alias, str)
                    and alias.strip()
                    and alias.strip() != name
                ):
                    aliases.append(alias.strip())
        names_info[name] = list(dict.fromkeys(aliases))
        try:
            rank = int(entry.get("important_rank", 1))
        except (TypeError, ValueError):
            rank = 1
        ranks[name] = min(5, max(1, rank))
    return names_info, ranks


def _top_rank_names(ranks: Mapping[str, int]) -> Set[str]:
    # rank 1 明确属于“尝试即可、失败保留原名”，不进入必须改名的前两档。
    top_values = sorted({rank for rank in ranks.values() if rank > 1}, reverse=True)[:2]
    return {name for name, rank in ranks.items() if rank in top_values}


def _surname_required_names(ranks: Mapping[str, int]) -> Set[str]:
    """Ranks above 1 must ultimately change at least their surname."""
    return {name for name, rank in ranks.items() if rank > 1}


def _is_plain_generic(name: str) -> bool:
    return (
        _is_numbered_label(name)
        or name in GENERIC_ROLE_WORDS
        or name in GENERIC_STANDALONE
        or (is_generic_appellation(name) and "的" not in name)
    )


def _is_numbered_label(name: str) -> bool:
    """编号称谓不是人名，不能被匿名改成另一个编号。"""
    value = str(name or "").strip()
    return bool(
        re.fullmatch(
            r"第?[0-9０-９A-Za-zＡ-Ｚａ-ｚ一二三四五六七八九十百千万]+号",
            value,
        )
    )


def _linked_descriptive_name(
    old_name: str,
    accepted: Mapping[str, str],
    important_names: Set[str],
) -> Optional[str]:
    if "的" not in old_name:
        return None
    changed = old_name
    for owner in sorted(important_names, key=len, reverse=True):
        new_owner = accepted.get(owner, owner)
        if owner in changed and new_owner != owner:
            changed = changed.replace(owner, new_owner)
    return changed if changed != old_name else None


def _cascading_name_error(
    old_name: str,
    new_name: str,
    changing_names: Optional[Set[str]],
) -> Optional[str]:
    """拦住"新名里含着另一个要改名角色的原名"这种映射。

    第 16 步换名可以重复执行。若 `何鹤 -> 穆云鹤` 而"云"自己又要改成"凌"，
    第一遍得到"穆云鹤"，第二遍就被"云 -> 凌"再吃一次变成"穆凌鹤"——一个谁也
    没定过的名字。而 `find_unreplaced_names` 只查"该换的换没换"，查不出这种
    过度替换，错误会静默固化。

    只有被包含的原名**自己也要改名**时才有害：`许家亲戚A -> 陆家亲戚A` 含
    "亲戚A"，但"亲戚A"是 old == new 的不改名角色，永远不会触发二次替换，
    这类必须放行，否则会把大量合法改名判为失败。
    """
    if not changing_names:
        return None
    for other in changing_names:
        if other == old_name or not other:
            continue
        if other in new_name:
            return f"新名里含着另一个要改名角色的原名 {other}，重跑会被二次替换"
    return None


def _candidate_error(
    old_name: str,
    new_name: Any,
    important: bool,
    original_names: Set[str],
    used_new_names: Set[str],
    changing_names: Optional[Set[str]] = None,
) -> Optional[str]:
    if not isinstance(new_name, str) or not new_name.strip():
        return "未返回有效字符串"
    new_name = new_name.strip()
    if new_name == old_name:
        return "仍是原名"
    if new_name in original_names - {old_name}:
        return "使用了其他角色的原名"
    if new_name in used_new_names:
        return "与其他角色的新名重复"
    cascading = _cascading_name_error(old_name, new_name, changing_names)
    if cascading:
        return cascading

    # 音译复合名："名·家族名"。家族名段会独立出现在正文（"巴鲁克家族"），
    # 只换名段等于没脱敏，所以重要角色必须连家族名段一起换。
    old_segments = split_transliterated_name(old_name)
    if old_segments:
        new_segments = split_transliterated_name(new_name)
        if not new_segments:
            return "新名不是同结构的音译复合名"
        if len(new_segments) != len(old_segments):
            return "新名与原名的音译段数不一致"
        if important and new_segments[-1] == old_segments[-1]:
            return "重要角色至少需要更换家族名"
        return None

    surname_role = split_surname_and_role(old_name)
    if surname_role:
        old_surname, role_word = surname_role
        if not new_name.endswith(role_word) or len(new_name) <= len(role_word):
            return f"没有保留身份词 {role_word}"
        if important and new_name[: -len(role_word)] == old_surname:
            return "重要角色至少需要更换姓氏"
        return None

    old_person = split_person_name(old_name)
    if old_person:
        new_person = split_person_name(new_name)
        if not new_person:
            return "新名不是完整姓名"
        if important and new_person[0] == old_person[0]:
            return "重要角色至少需要更换姓氏"
    return None


def _person_surname(name: str) -> Optional[str]:
    """Extract a person/role surname for relationship consistency checks."""
    # 音译名（"林雷·巴鲁克"）的家族名段等价于中文姓氏：同族角色必须一起换，
    # 且它会以"巴鲁克家族"的形式独立出现在正文里。不识别它的话，下面这些
    # 依赖姓氏的检查（同族一致、别名一致、换姓兜底）对音译名全部空转。
    family = family_segment(name)
    if family:
        return family
    role = split_surname_and_role(name)
    if role:
        return role[0]
    person = split_person_name(name)
    return person[0] if person else None


def _candidate_surname(name: str, candidate: Any) -> Optional[str]:
    if not isinstance(candidate, str):
        return None
    old_segments = split_transliterated_name(name)
    if old_segments:
        # 段数一致才谈得上"对应的家族名段"；不一致由 _candidate_error 拒掉。
        new_segments = split_transliterated_name(candidate)
        if new_segments and len(new_segments) == len(old_segments):
            return new_segments[-1]
        return None
    role = split_surname_and_role(name)
    if role and candidate.endswith(role[1]) and len(candidate) > len(role[1]):
        return candidate[: -len(role[1])]
    person = split_person_name(name)
    if person:
        parsed = split_person_name(candidate)
        return parsed[0] if parsed else None
    return None


def _alias_error(
    old_alias: str,
    new_alias: Any,
    old_main: str,
    new_main: str,
    original_names: Set[str],
    used_names: Set[str],
) -> Optional[str]:
    """Validate a returned alias without ever raising into the main pipeline."""
    if new_alias is None:
        return None
    if not isinstance(new_alias, str) or not new_alias.strip():
        return "别名不是有效字符串"
    candidate = new_alias.strip()
    if candidate == old_alias:
        return None
    if candidate in original_names or candidate in used_names:
        return "别名与其他角色名称冲突"
    old_main_surname = _person_surname(old_main)
    new_main_surname = _candidate_surname(old_main, new_main)
    old_alias_surname = _person_surname(old_alias)
    new_alias_surname = _candidate_surname(old_alias, candidate)
    if (
        old_main_surname
        and new_main_surname
        and old_alias_surname == old_main_surname
        and new_alias_surname
        and new_alias_surname != new_main_surname
    ):
        return "别名与主名的新姓不一致"
    return None


def _surname_fallback(
    old_name: str,
    original_names: Set[str],
    used_new_names: Set[str],
    preferred_surname: Optional[str] = None,
    changing_names: Optional[Set[str]] = None,
) -> Optional[str]:
    """重要角色模型失败时，机械换姓；找不到安全结果才返回 None。

    兜底的做法是保留原名的一段（名段或身份词）只换姓，所以保留下来的那段若
    自己也要改名，重跑就会被二次替换（"沈妙妙"里的"妙妙"再被换一次）。这类
    候选一律放弃，宁可该角色保留原名。
    """
    def _is_safe(candidate: str) -> bool:
        if candidate in original_names or candidate in used_new_names:
            return False
        return not _cascading_name_error(old_name, candidate, changing_names)

    # 音译名换家族名段，且必须用音译风格的候选：单字汉姓会拼出"林雷·沈"，
    # 比不换更糟。名段保持不变，只换家族名段，与"至少换姓"的口径一致。
    old_segments = split_transliterated_name(old_name)
    if old_segments:
        separator = INTERPUNCT_RE.search(old_name).group(0)
        prefix = old_segments[:-1]
        old_family = old_segments[-1]
        families = (
            (preferred_surname,) + FALLBACK_FAMILY_NAMES
            if preferred_surname else FALLBACK_FAMILY_NAMES
        )
        for family in families:
            if not family or family == old_family:
                continue
            candidate = separator.join(prefix + [family])
            if _is_safe(candidate):
                return candidate
        return None

    surname_role = split_surname_and_role(old_name)
    person = split_person_name(old_name)
    if surname_role:
        old_surname, role_word = surname_role
        suffix = role_word
    elif person:
        old_surname, given_name = person
        suffix = given_name
    else:
        return None
    surnames = (
        (preferred_surname,) + FALLBACK_SURNAMES
        if preferred_surname else FALLBACK_SURNAMES
    )
    for surname in surnames:
        candidate = surname + suffix
        if surname != old_surname and _is_safe(candidate):
            return candidate
    return None


def _create_client(api_key: str, name_count: int):
    base_url = os.getenv(
        "GPT_IMAGE_BASE_URL", "http://sefutech-api.kube-test.svc:3000"
    ).rstrip("/")
    return OpenAI(
        api_key=api_key,
        base_url=f"{base_url}/v1",
        timeout=_llm_timeout_for(name_count),
        max_retries=LLM_MAX_RETRIES,
    )


def _request_renames(
    client,
    names_info: Mapping[str, List[str]],
    important_names: Set[str],
    surname_required_names: Set[str],
    feedback: List[str],
) -> Dict[str, Dict[str, str]]:
    lines = []
    for name, aliases in names_info.items():
        if name in important_names:
            priority = "最高优先级，至少换姓"
        elif name in surname_required_names:
            priority = "普通优先级，但至少换姓"
        else:
            priority = "普通优先级，尽力改名"
        alias_text = "、".join(aliases) if aliases else "无"
        lines.append(f"- {name}（{priority}；别名：{alias_text}）")
    prompt = """请为小说角色生成匿名新名。规则：
1. 每个输入主名必须返回；确实无法改时可返回原名。
2. 完整姓名优先同时更换姓和名，保持时代、人物气质及人物关系；原先同姓的角色改后仍同姓。
3. 标记为”至少换姓”的角色必须改名，至少更换姓氏；新名不能使用任何其他输入角色的原名，
   也不能把其他角色的原名当作新名的一部分（如”云”是别的角色时，不能取名”穆云鹤”）。
4. 不同角色不能生成相同新名。同一角色的主名和别名应保持新姓或核心字一致。
5. 小、老、哥、姐、老师、队长等词缀完整保留，只替换其中的人名核心。
5.1 带间隔号的音译名（"林雷·巴鲁克"）保持"名·家族名"结构和段数，名段与家族名段
   都要更换（家族名会以"巴鲁克家族"的形式出现在正文，不换就等于没改）；
   原先同家族的角色改名后必须共用同一个新家族名。
6. 爸爸、司机、系统、旁白等纯泛称原样返回；“姓+身份词”只换姓并保留身份词。
   纯编号称谓（如“2号”“第3号”“A号”）原样返回，不得更改编号。
7. 别名放在 alias-new-name，主名和别名不要混放。
8. 只输出 JSON：{"new-name": {"原名": "新名"}, "alias-new-name": {"原别名": "新别名"}}。

角色：
""" + "\n".join(lines)
    if feedback:
        prompt += "\n\n上一次以下角色失败，请修正：\n" + "\n".join(
            f"- {item}" for item in feedback
        )
    response, used_model = _model_fallback.chat(
        client,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    if _usage_tracker and getattr(response, "usage", None):
        _usage_tracker.add_llm_usage(response.usage, used_model, provider="qwen")
    data = parse_model_json(
        response.choices[0].message.content, context="ranked_rename", logger=logger
    )
    if not isinstance(data, dict):
        raise ValueError("改名接口未返回 JSON 对象")
    if not data.get("new-name") and not data.get("alias-new-name"):
        data = {"new-name": data, "alias-new-name": {}}

    def clean(value: Any, allowed: Set[str]) -> Dict[str, str]:
        return {
            old.strip(): new.strip()
            for old, new in _mapping(value).items()
            if isinstance(old, str)
            and isinstance(new, str)
            and old.strip() in allowed
            and new.strip()
        }

    main_names = set(names_info)
    aliases = {
        alias
        for values in names_info.values()
        for alias in values
        if alias not in main_names
    }
    return {
        "new-name": clean(data.get("new-name"), main_names),
        "alias-new-name": clean(data.get("alias-new-name"), aliases),
    }


def generate_ranked_renames(
    names_info: Mapping[str, List[str]],
    ranks: Mapping[str, int],
    api_key: Optional[str],
) -> Dict[str, Dict[str, str]]:
    """Generate per-character mappings; failures degrade independently."""
    accepted = {name: name for name in names_info}
    alias_result = {
        alias: alias
        for aliases in names_info.values()
        for alias in aliases
        if alias not in accepted
    }
    important_names = _top_rank_names(ranks)
    surname_required_names = _surname_required_names(ranks)
    plain_generic = {name for name in names_info if _is_plain_generic(name)}
    descriptive = {
        name
        for name in names_info
        if "的" in name and not split_person_name(name)
    }
    pending = set(names_info) - plain_generic - descriptive
    if not pending:
        return {"new-name": accepted, "alias-new-name": alias_result}

    if not api_key:
        logger.warning(
            "⚠️ 未配置模型 API Key，important_rank > 1 的角色尝试换姓兜底，"
            "important_rank = 1 的角色保留原名"
        )
        original_names = set(names_info)
        used_new_names: Set[str] = set()
        # 这条路径上只有 surname_required 且仍 pending 的角色会改名。
        fallback_changing = surname_required_names & pending
        for old_name in names_info:
            if old_name not in surname_required_names or old_name not in pending:
                continue
            fallback = _surname_fallback(
                old_name,
                original_names,
                used_new_names,
                changing_names=fallback_changing,
            )
            if fallback:
                accepted[old_name] = fallback
                used_new_names.add(fallback)
        changed_owners = {name for name, new in accepted.items() if new != name}
        for old_name in descriptive:
            linked = _linked_descriptive_name(old_name, accepted, changed_owners)
            if linked:
                accepted[old_name] = linked
        return {"new-name": accepted, "alias-new-name": alias_result}

    client = _create_client(
        api_key,
        len(pending) + sum(len(names_info[name]) for name in pending),
    )
    feedback: List[str] = []
    original_names = set(names_info)
    for attempt in range(1, RENAME_MAX_ATTEMPTS + 1):
        if not pending:
            break
        request_info = {name: names_info[name] for name in names_info if name in pending}
        try:
            generated = _request_renames(
                client,
                request_info,
                important_names & pending,
                surname_required_names & pending,
                feedback,
            )
        except Exception as exc:
            logger.warning(f"⚠️ 角色改名第 {attempt}/{RENAME_MAX_ATTEMPTS} 次请求失败: {exc}")
            feedback = [f"{name}: 上一次请求失败" for name in sorted(pending)]
            if attempt == 1:
                # rank 1 只尝试一次；其余档位首次失败后再重试两次。
                pending.intersection_update(surname_required_names)
            continue

        failures: List[str] = []
        used_new_names = {
            new for old, new in accepted.items() if old not in pending and new != old
        }
        # "会改名的原名"集合，用于拦住新名含它们的候选（重跑会被二次替换）。
        # 已定稿的按结果算；还在 pending 的按"预期会改"算——本轮它们大概率也会
        # 拿到新名，等它们定稿后再回头拦已通过的候选就来不及了。
        changing_names = {
            old for old, new in accepted.items() if old not in pending and new != old
        } | set(pending)
        # 同姓角色必须共享新姓；先在本批次收集候选，避免处理顺序影响结果。
        surname_groups: Dict[str, Dict[str, Optional[str]]] = {}
        for old_name in pending:
            old_surname = _person_surname(old_name)
            if old_surname:
                surname_groups.setdefault(old_surname, {})[old_name] = _candidate_surname(
                    old_name, generated["new-name"].get(old_name)
                )
        group_target: Dict[str, str] = {}
        group_conflicts: Set[str] = set()
        for old_surname, candidates in surname_groups.items():
            values = {value for value in candidates.values() if value}
            if len(values) == 1:
                group_target[old_surname] = next(iter(values))
            elif len(values) > 1:
                group_conflicts.add(old_surname)
        for old_name in names_info:
            if old_name not in pending:
                continue
            candidate = generated["new-name"].get(old_name)
            error = _candidate_error(
                old_name,
                candidate,
                old_name in surname_required_names,
                original_names,
                used_new_names,
                changing_names,
            )
            if error:
                failures.append(f"{old_name}: {error}")
                if attempt == 1 and old_name not in surname_required_names:
                    pending.remove(old_name)
                continue
            old_surname = _person_surname(old_name)
            candidate_surname = _candidate_surname(old_name, candidate)
            if old_surname and old_surname in group_conflicts:
                failures.append(f"{old_name}: 同姓角色候选新姓不一致")
                continue
            if old_surname and old_surname in group_target and candidate_surname != group_target[old_surname]:
                failures.append(f"{old_name}: 未保持同姓角色的新姓一致")
                continue
            if old_surname and old_surname in group_target and old_name in surname_required_names and candidate_surname == old_surname:
                failures.append(f"{old_name}: 重要角色未更换姓氏")
                continue
            accepted[old_name] = str(candidate).strip()
            used_new_names.add(accepted[old_name])
            pending.remove(old_name)
            alias_updates: Dict[str, str] = {}
            alias_failed = False
            for alias in names_info[old_name]:
                candidate_alias = generated["alias-new-name"].get(alias)
                alias_error = _alias_error(
                    alias, candidate_alias, old_name, accepted[old_name],
                    original_names, used_new_names,
                )
                if alias_error:
                    failures.append(f"{alias}: {alias_error}")
                    alias_failed = True
                elif candidate_alias and candidate_alias != alias:
                    alias_updates[alias] = candidate_alias
            if alias_failed:
                # 只让该角色重试；撤销本轮暂存，不影响已经成功的其他角色。
                used_new_names.discard(accepted[old_name])
                accepted[old_name] = old_name
                pending.add(old_name)
                continue
            alias_result.update(alias_updates)
        feedback = failures
        if failures and attempt < RENAME_MAX_ATTEMPTS:
            logger.warning(
                f"⚠️ 第 {attempt} 次仍有 {len(failures)} 个角色未得到可用新名，将只重试这些角色"
            )

    used_after_attempts = {
        new for old, new in accepted.items() if old not in pending and new != old
    }
    # 已定稿的改名角色，加上这一轮兜底还可能改名的角色。
    final_changing = {
        old for old, new in accepted.items() if old not in pending and new != old
    } | (surname_required_names & pending)
    for old_name in sorted(pending):
        fallback = None
        if old_name in surname_required_names:
            fallback = _surname_fallback(
                old_name,
                original_names,
                used_after_attempts,
                changing_names=final_changing,
            )
        if fallback:
            accepted[old_name] = fallback
            used_after_attempts.add(fallback)
            logger.warning(
                f"⚠️ {old_name} 连续 {RENAME_MAX_ATTEMPTS} 次未得到可用新名，"
                f"已使用换姓兜底: {old_name} -> {fallback}"
            )
        else:
            logger.warning(
                f"⚠️ {old_name} 连续 {RENAME_MAX_ATTEMPTS} 次未得到可用新名，"
                "已单独退回原名对应原名"
            )

    # 仅联动最高两个 important_rank 档位的人名；纯泛称不动。
    changed_owners = {name for name, new in accepted.items() if new != name}
    for old_name in descriptive:
        linked = _linked_descriptive_name(old_name, accepted, changed_owners)
        if linked:
            accepted[old_name] = linked

    return {"new-name": accepted, "alias-new-name": alias_result}


def _build_change_name(
    names: List[str],
    rename_map: Mapping[str, str],
    api_key: Optional[str],
) -> Dict[str, Dict[str, str]]:
    changed_names: List[str] = []
    for old_name in names:
        new_name = str(rename_map.get(old_name, old_name)).strip() or old_name
        if new_name not in changed_names:
            changed_names.append(new_name)
    conversion = _build_conversion_for_names(changed_names)
    zh_cn = {speaker_id: name for name, speaker_id in conversion.items()}

    translations = {"en": {}, "ja": {}}
    if api_key:
        translations = _translate_names_batch(list(zh_cn.values()), api_key)
    en_map = translations.get("en", {})
    ja_map = translations.get("ja", {})
    en_us = {}
    for speaker_id, name in zh_cn.items():
        # 模型结果优先；对泛称/身份称谓使用语义兜底，最后才对真正的人名使用 ID 缩写。
        # 模型翻译失败时保留完整 PascalCase speaker ID，不压缩为首字母。
        en_us[speaker_id] = en_map.get(name) or speaker_id
    ja_jp = {}
    for speaker_id, name in zh_cn.items():
        model_value = ja_map.get(name)
        # 泛称/描述式称谓若模型原样返回中文，视为未翻译，使用日语语义兜底。
        ja_jp[speaker_id] = model_value or name
    return {
        "new-name": dict(rename_map),
        "conversion": conversion,
        "zh-CN": zh_cn,
        "en-US": en_us,
        "ja-JP": ja_jp,
    }


def process_task(
    task_id: str,
    force: bool = False,
    outputs_path: str = OUTPUTS_PATH,
) -> Dict[str, Any]:
    """Fill change_name from ranked characters and persist both copies."""
    task_id = str(task_id)
    engine = create_engine(PLATFORM_DATABASE_URL, pool_pre_ping=True)
    characters = _load_json_row(engine, "characters", task_id)
    if not characters:
        raise ValueError(f"characters 表中未找到 task_id={task_id}")
    names_info, ranks = _collect_ranked_characters(characters)
    if not names_info:
        raise ValueError("characters 中没有可用于改名的角色")

    mappings = _load_name_mappings(engine, task_id, outputs_path)
    change_name = _mapping(mappings.get("change_name"))
    if (
        not force
        and _mapping(change_name.get("new-name"))
        and not has_invalid_english_order(change_name)
    ):
        logger.info("✅ change_name 已存在，跳过 ranked rename；如需重生成请使用 --force")
        return {
            "task_id": task_id,
            "changed": False,
            "renamed_count": sum(
                old != new
                for old, new in _mapping(change_name.get("new-name")).items()
            ),
        }

    original = _mapping(_mapping(mappings.get("original_name")).get("conversion"))
    if not original:
        original = _build_conversion_for_names(list(names_info))

    # 三个变量名不同但值都是同一个中转站 key，DEEPSEEK_API_KEY 只是历史遗留的
    # 变量名（不是 DeepSeek 官方 key），作为最后一层兜底保留。
    api_key = (
        os.getenv("GPT_IMAGE_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("DEEPSEEK_API_KEY")
    )
    generated = generate_ranked_renames(names_info, ranks, api_key)
    completed = {
        "original_name": {"conversion": original},
        "change_name": _build_change_name(
            list(names_info), generated["new-name"], api_key
        ),
    }
    _save_to_local(task_id, completed, outputs_path)
    _save_replacements_to_local(
        task_id, generated["alias-new-name"], outputs_path
    )
    _save_to_db(engine, task_id, completed)

    renamed_count = sum(
        old != new for old, new in generated["new-name"].items()
    )
    important_names = _top_rank_names(ranks)
    logger.info(
        f"✅ ranked rename 完成：{renamed_count}/{len(names_info)} 个角色改名；"
        f"最高两档角色 {len(important_names)} 个"
    )
    return {
        "task_id": task_id,
        "changed": True,
        "renamed_count": renamed_count,
        "character_count": len(names_info),
        "important_names": sorted(important_names),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="按 characters.important_rank 生成 name_mappings.change_name"
    )
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    result = process_task(args.task_id, force=args.force)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
