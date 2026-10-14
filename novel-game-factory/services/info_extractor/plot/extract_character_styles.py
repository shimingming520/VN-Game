import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
utils_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "utils")
for path in (project_root, os.path.normpath(utils_dir)):
    if path not in sys.path:
        sys.path.insert(0, path)

from openai import AsyncOpenAI
from sqlalchemy import text as sa_text
from tenacity import retry, stop_after_attempt, wait_random_exponential

from common.config import OUTPUTS_PATH
from common.llm_fallback import ModelFallback
from common.model_json import parse_model_json
from common.name_mapping_utils import get_source_to_change_conversion
from common.platform_db import platform_engine
from logger_config import setup_logger

try:
    from common.usage_tracker import init_usage_table
    from common.usage_tracker import tracker as _usage_tracker

    init_usage_table()
except Exception:
    _usage_tracker = None

logger = setup_logger("Step1_CharStyle", "step1_char_style.log")

API_KEY = os.getenv("DASHSCOPE_API_KEY")
# BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
# BASE_URL = "https://ai.farmmx.com/v1"
BASE_URL = os.getenv("PLOT_BASE_URL","http://sefutech-api.kube-test.svc:3000/v1")

MODEL_NAME = "qwen3.7-max"
# 主模型偶发不稳定时自动改用备选模型重发同一请求（同中转站、同调用参数）。
_model_fallback = ModelFallback(MODEL_NAME, name="CharStyle", logger=logger)
MAX_CONCURRENT_REQUESTS = 5
MAX_NODE_CHARS = 6000
MAX_OUTPUT_TOKENS = 16000
MERGE_BATCH_SIZE = 4
SINGLE_CHARACTER_MERGE_ATTEMPTS = 3
MAX_LINES_PER_CHARACTER = 8
MAX_WORK_NOTES = 12
ALLOWED_FORMALITY = {"formal", "neutral", "casual"}
ALLOWED_DIRECTNESS = {"direct", "balanced", "indirect"}
ALLOWED_CONTRACTIONS = {"prefer", "allow", "avoid"}
ALLOWED_PROFANITY = {"none", "mild", "moderate", "strong"}
ALLOWED_EN_VARIANTS = {"US", "UK"}
ALLOWED_JA_POLITENESS = {"plain", "polite"}
ALLOWED_JA_HONORIFIC = {"none", "light", "strict"}
ALLOWED_PRONOUN_OMISSION = {"prefer", "allow", "avoid"}
ALLOWED_JA_ERA_STYLE = {"modern", "light_historical", "strong_historical"}
STYLE_SCHEMA_VERSION = 2
# 旁白/系统占位不是角色，不生成语体（conversion 上游未过滤，这里自行兜底）
SKIP_SPEAKERS = {"pangbai", "旁白", "narration", "narrator"}

engine = platform_engine


def _as_dict(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _unwrap_entry(value):
    """characters 表的值存在 [dict] 与 dict 两种历史形态。"""
    if isinstance(value, list):
        value = value[0] if value else {}
    return value if isinstance(value, dict) else {}


def _string_list(value, limit):
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(
        str(item).strip() for item in value if str(item or "").strip()
    ))[:limit]


def _string_dict(value, limit):
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, item in value.items():
        key_text = str(key or "").strip()
        item_text = str(item or "").strip()
        if key_text and item_text:
            result[key_text] = item_text
        if len(result) >= limit:
            break
    return result


def _extract_json_object(raw_text):
    text = (raw_text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end < start:
            raise ValueError("模型返回内容中没有 JSON 对象")
        text = text[start:end + 1]
    parsed = parse_model_json(
        text, context="character_styles", logger=logger, expect=dict
    )
    if not isinstance(parsed, dict):
        raise ValueError("模型返回结果不是 JSON 对象")
    return parsed


def fetch_source_context(target_task_id=None):
    with engine.connect() as conn:
        task_id = target_task_id
        if not task_id:
            row = conn.execute(
                sa_text("SELECT task_id FROM story_tree_md ORDER BY id DESC LIMIT 1")
            ).fetchone()
            if not row:
                logger.error("❌ story_tree_md 表为空")
                return None, {}, {}, {}
            task_id = row[0]

        char_row = conn.execute(
            sa_text("SELECT data FROM characters WHERE task_id = :tid LIMIT 1"),
            {"tid": task_id},
        ).fetchone()
        characters = _as_dict(char_row[0]) if char_row else {}

        mapping_row = conn.execute(
            sa_text("SELECT data FROM name_mappings WHERE task_id = :tid LIMIT 1"),
            {"tid": task_id},
        ).fetchone()
        mapping_data = _as_dict(mapping_row[0]) if mapping_row else {}
        conversion = get_source_to_change_conversion(mapping_data)

        story_rows = conn.execute(
            sa_text("SELECT name, data FROM story_tree_md WHERE task_id = :tid ORDER BY id"),
            {"tid": task_id},
        ).fetchall()

    if not story_rows:
        logger.error(f"❌ 未找到原文数据 (Task: {task_id})")
        return None, {}, {}, []

    # 用列表而非 dict：story_tree_md.name 无唯一约束，同名节点不能互相覆盖
    stories = [(str(row[0]), str(row[1] or "")) for row in story_rows]
    logger.info(
        f"📦 已加载 Task={task_id}: {len(stories)} 个原文节点，"
        f"{len(characters)} 个角色，{len(conversion)} 条名称映射"
    )
    return str(task_id), characters, conversion, stories


def load_local_conversion(task_id):
    path = os.path.join(OUTPUTS_PATH, str(task_id), "name_mappings.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as file:
            return get_source_to_change_conversion(_as_dict(json.load(file)))
    except Exception as exc:
        logger.warning(f"⚠️ 读取本地 name_mappings.json 失败: {exc}")
        return {}


def build_character_context(characters, conversion):
    """以 conversion 为准生成名单：NPC 只在 conversion 里，但同样有 speaker_id 和台词。"""
    detail_by_id = {}
    for source_name, raw_info in characters.items():
        info = _unwrap_entry(raw_info)
        name = str(info.get("name") or source_name)
        other_names = [str(item) for item in (info.get("other_names") or [])]
        candidate_names = [name, str(source_name), *other_names]
        speaker_id = next((conversion.get(item) for item in candidate_names if conversion.get(item)), None)
        if not speaker_id:
            logger.warning(f"⚠️ 角色 {name} 不在 change_name 映射中，已跳过")
            continue

        occupations = []
        for variant in info.get("clothing_variants") or []:
            if isinstance(variant, dict) and variant.get("occupation"):
                occupations.append(str(variant["occupation"]))

        personalities = []
        for variant in info.get("personality_variants") or []:
            if not isinstance(variant, dict):
                continue
            personalities.extend(variant.get("main_traits") or [])
            personalities.extend(variant.get("secondary_traits") or [])

        detail_by_id[str(speaker_id)] = {
            "speaker_id": str(speaker_id),
            "name": name,
            "other_names": other_names,
            "important_rank": info.get("important_rank"),
            "occupations": list(dict.fromkeys(occupations)),
            "personality_traits": list(dict.fromkeys(str(item) for item in personalities)),
            "is_npc": False,
        }

    result_by_id = {}
    npc_names = []
    for source_name, speaker_id in conversion.items():
        speaker_id = str(speaker_id or "").strip()
        name = str(source_name).strip()
        if (
            not speaker_id
            or name.lower() in SKIP_SPEAKERS
            or speaker_id.lower() in {"pangbai", "narrator", "narration", "system", "xitong"}
        ):
            continue
        detail = detail_by_id.get(speaker_id)
        if detail:
            existing = result_by_id.get(speaker_id)
            if existing is None:
                existing = dict(detail)
                existing["other_names"] = list(detail["other_names"])
                result_by_id[speaker_id] = existing
            if name != existing["name"] and name not in existing["other_names"]:
                existing["other_names"].append(name)
            continue
        # 仅存在于 conversion 的 NPC：无角色资料，语体完全依据原文台词推断
        existing = result_by_id.get(speaker_id)
        if existing is None:
            npc_names.append(name)
            result_by_id[speaker_id] = {
                "speaker_id": speaker_id,
                "name": name,
                "other_names": [],
                "important_rank": None,
                "occupations": [],
                "personality_traits": [],
                "is_npc": True,
            }
        elif name != existing["name"] and name not in existing["other_names"]:
            existing["other_names"].append(name)

    if npc_names:
        logger.info(f"👥 纳入 {len(npc_names)} 个仅存在于 conversion 的 NPC: {'、'.join(npc_names)}")
    return list(result_by_id.values())


def build_alias_index(character_context):
    """中文主名与别名 → speaker_id，用于把阶段一的中文键确定性归一。"""
    index = {}
    for item in character_context:
        index[item["name"]] = item["speaker_id"]
        for alias in item["other_names"]:
            index.setdefault(alias, item["speaker_id"])
    return index


def regroup_observations(observations, character_context):
    """把逐段观察重组成逐角色证据，控制合并阶段输入规模。"""
    alias_index = build_alias_index(character_context)
    per_character = {}
    work_notes = []
    unmapped = set()

    for observation in observations:
        work = observation.get("work_observation")
        if isinstance(work, dict):
            note = {k: str(v) for k, v in work.items() if str(v or "").strip()}
            if note and note not in work_notes:
                work_notes.append(note)

        for item in observation.get("unmapped_speakers") or []:
            if str(item).strip():
                unmapped.add(str(item).strip())

        characters = observation.get("characters")
        if not isinstance(characters, dict):
            continue
        for raw_name, evidence in characters.items():
            if not isinstance(evidence, dict):
                continue
            speaker_id = alias_index.get(str(raw_name).strip())
            if not speaker_id:
                unmapped.add(str(raw_name).strip())
                continue
            bucket = per_character.setdefault(
                speaker_id,
                {"identity": [], "speech": [], "thought": [], "lines": [], "thoughts": []},
            )
            for field, key in (
                ("identity", "identity_evidence"),
                ("speech", "speech_evidence"),
                ("thought", "thought_evidence"),
            ):
                value = str(evidence.get(key) or "").strip()
                if value and value not in bucket[field]:
                    bucket[field].append(value)
            for line in evidence.get("representative_lines") or []:
                line = str(line).strip()
                if line and line not in bucket["lines"]:
                    bucket["lines"].append(line)
            for line in evidence.get("representative_thoughts") or []:
                line = str(line).strip()
                if line and line not in bucket["thoughts"]:
                    bucket["thoughts"].append(line)

    for bucket in per_character.values():
        bucket["lines"] = spread_sample(bucket["lines"], MAX_LINES_PER_CHARACTER)
        bucket["thoughts"] = spread_sample(bucket["thoughts"], max(3, MAX_LINES_PER_CHARACTER // 2))

    return per_character, spread_sample(work_notes, MAX_WORK_NOTES), sorted(unmapped)


def spread_sample(items, limit):
    """从有序证据中均匀取样，避免只保留故事开头的台词。"""
    if len(items) <= limit:
        return list(items)
    if limit <= 1:
        return [items[0]]
    indexes = [round(i * (len(items) - 1) / (limit - 1)) for i in range(limit)]
    return [items[index] for index in dict.fromkeys(indexes)]


def chunk_list(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]


def build_input_hashes(stories, conversion):
    """生成用于判断语体表是否仍与当前原文和名称映射一致的哈希。"""
    source_payload = json.dumps(stories, ensure_ascii=False, separators=(",", ":"))
    mapping_payload = json.dumps(
        conversion, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return {
        "source_hash": hashlib.sha256(source_payload.encode("utf-8")).hexdigest(),
        "name_mapping_hash": hashlib.sha256(mapping_payload.encode("utf-8")).hexdigest(),
    }


def summarize_style_quality(data):
    counts = {"complete": 0, "inferred": 0, "insufficient": 0, "failed": 0}
    characters = data.get("characters") if isinstance(data, dict) else {}
    characters = characters if isinstance(characters, dict) else {}
    for info in characters.values():
        quality = info.get("quality") if isinstance(info, dict) else {}
        quality = quality if isinstance(quality, dict) else {}
        status = str(quality.get("status") or "insufficient").lower()
        if status not in counts:
            status = "insufficient"
        counts[status] += 1
    return counts


def _quality_status(character):
    if not isinstance(character, dict):
        return "insufficient"
    quality = character.get("quality")
    if not isinstance(quality, dict):
        return "insufficient"
    return str(quality.get("status") or "insufficient").lower()


def validate_complete_result(data, expected_speaker_ids):
    """严格验证可入库结果；不允许用空白兜底伪装成已完成。"""
    expected = {str(item) for item in expected_speaker_ids}
    characters = data.get("characters") if isinstance(data, dict) else None
    if not isinstance(characters, dict):
        return False, "characters 不是对象", summarize_style_quality({})

    actual = set(characters)
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    quality_counts = summarize_style_quality(data)
    unusable = sorted(
        speaker_id for speaker_id in expected & actual
        if _quality_status(characters.get(speaker_id)) not in {"complete", "inferred"}
    )
    if missing:
        return False, f"缺少角色: {'、'.join(missing)}", quality_counts
    if unexpected:
        return False, f"包含未知角色: {'、'.join(unexpected)}", quality_counts
    if unusable:
        return False, f"角色语体不可用: {'、'.join(unusable)}", quality_counts
    if not expected:
        return False, "预期角色列表为空", quality_counts
    return True, "", quality_counts


def split_node_text(text, max_chars=MAX_NODE_CHARS):
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    parts = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            boundary = max(
                text.rfind("\n", start, end),
                text.rfind("。", start, end),
                text.rfind("！", start, end),
                text.rfind("？", start, end),
            )
            if boundary > start + max_chars // 2:
                end = boundary + 1
        parts.append(text[start:end])
        start = end
    return parts


async def call_json_model(client, semaphore, system_prompt, user_prompt):
    async with semaphore:
        response, used_model = await _model_fallback.achat(
            client,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
            max_tokens=MAX_OUTPUT_TOKENS,
        )
    if _usage_tracker and response.usage:
        # add_llm_usage 内部是同步写库，放线程池避免阻塞事件循环
        await asyncio.to_thread(
            _usage_tracker.add_llm_usage,
            response.usage, used_model, provider="openai_compatible",
        )
    if not response.choices:
        raise ValueError("模型未返回任何 choices")
    choice = response.choices[0]
    if choice.finish_reason == "length":
        raise ValueError(f"模型输出被截断(max_tokens={MAX_OUTPUT_TOKENS})，请减小批次规模")
    return _extract_json_object(choice.message.content)


def build_roster_text(character_context):
    """阶段一只需要主名和别名来约束键名，不必重复传完整角色资料。"""
    lines = []
    for item in character_context:
        aliases = "、".join(item["other_names"]) or "无"
        tag = "，配角/NPC，可能以身份称谓出场" if item["is_npc"] else ""
        lines.append(f"- {item['name']}（别名：{aliases}{tag}）")
    return "\n".join(lines)


@retry(stop=stop_after_attempt(3), wait=wait_random_exponential(min=1, max=30), reraise=True)
async def analyze_text_chunk(client, semaphore, node_id, chunk_index, text, roster_text):
    system_prompt = """你是小说人物语言风格分析专家。请从原文而不是剧情梗概中识别人物真实说话方式，为后续日语和英语翻译建立稳定语体依据。

只返回 JSON 对象，结构必须为：
{
  "work_observation": {
    "genre": "从本段能判断的题材，无法判断则为空字符串",
    "era": "时代与社会背景，无法判断则为空字符串",
    "social_structure": "影响说话等级的社会关系，无法判断则为空字符串",
    "overall_tone": "作品整体语言气质，无法判断则为空字符串",
    "narrative_person": "只能是 first、third 或空字符串"
  },
  "characters": {
    "角色中文主名": {
      "identity_evidence": "本段可验证的身份或社会地位",
      "speech_evidence": "本段体现出的口头表达、措辞、语气和礼貌特点",
      "thought_evidence": "本段体现出的内心独白节奏、人称和措辞特点",
      "representative_lines": ["最多3条有代表性的说出口台词"],
      "representative_thoughts": ["最多2条有代表性的内心独白"]
    }
  },
  "unmapped_speakers": ["原文中明确说话但角色资料未覆盖的称呼"]
}

规则：
1. 只分析本段原文有直接证据的内容，不得依据常识编造。
2. characters 的键只能使用给定角色资料里的中文主名；无法对应时放进 unmapped_speakers。
3. 旁白、系统提示、选项文本不作为人物风格；内心独白必须与说出口台词分开记录。
4. 不翻译原文，不在此阶段不决定目标语言的语体结论。
5. 不输出对话对象，不得出现 to 字段。
6. 同一角色有多种状态时要记录差异证据，但不要擅自拆成多个角色。"""
    user_prompt = (
        f"【节点】{node_id}，分段 {chunk_index}\n\n"
        f"【角色名单】\n{roster_text}\n\n"
        f"【原文】\n{text}"
    )
    result = await call_json_model(client, semaphore, system_prompt, user_prompt)
    result["_node"] = node_id
    result["_chunk"] = chunk_index
    return result


_WORK_MERGE_SYSTEM_PROMPT = """你是小说本地化总监。请合并多段原文观察，生成一份全书唯一、保守且可验证的作品语体规范。

只返回 JSON 对象：
{
  "work": {
    "genre": "作品题材",
    "era": "时代与世界背景",
    "social_structure": "影响语言等级的社会结构",
    "overall_tone": "作品整体语言气质",
    "ja_narration": "日语旁白的时态、文体和节奏",
    "ja_orthography": "日语汉字、假名和外来语的统一书写原则",
    "ja_punctuation": "日语标点、引号和省略号规则",
    "ja_historical_level": "只能是 modern、light_historical 或 strong_historical",
    "ja_dialect_policy": "标准语和方言的统一处理原则",
    "ja_name_reading_policy": "中文姓名、官职和专名的日语读法原则",
    "en_narration": "英语旁白的语域和节奏",
    "en_variant": "US 或 UK；本项目默认 US",
    "narrative_tense": "past 或 present",
    "narrative_person": "first 或 third",
    "historical_language_level": "modern、light 或 strong",
    "dialect_policy": "方言和口音的统一处理原则",
    "terminology_policy": "专名、官职和世界观术语的统一原则"
  }
}

规则：只合并有直接证据的结论；观察冲突时采用出现最多的保守结论；不得编造世界观。"""


_MERGE_SYSTEM_PROMPT = """你是小说本地化总监。请依据原文观察证据和已冻结的作品规范，为指定角色生成全书统一、可直接用于中译日和中译英的语体表。

只返回 JSON 对象，严格使用以下结构：
{
  "characters": {
    "PascalCaseSpeakerId": {
      "name": "中文主名",
      "identity": "身份、地位及其对语言风格的影响",
      "speech_traits": "基于原文归纳的稳定说话特点",
      "ja": {
        "register": "默认主语体，例如简体だ体、丁寧語、謙譲語、尊敬語",
        "politeness": "只能是 plain 或 polite",
        "honorific_level": "尊敬语强度，只能是 none、light 或 strict",
        "humble_level": "谦让语强度，只能是 none、light 或 strict",
        "first_person": "默认第一人称，如私/僕/俺/わし",
        "first_person_reading": "第一人称的固定读音，如 わたし 或 わたくし",
        "first_person_aliases": ["允许视为同一规范形式的少量写法；没有则为空数组"],
        "pronoun_omission": "只能是 prefer、allow 或 avoid；自然日语通常 prefer",
        "second_person": "默认第二人称策略；没有固定代词时写称呼姓名或身份，避免滥用あなた",
        "address_terms": {"关系或对象类别": "固定称谓，如 陛下、師匠、兄上"},
        "avoid_second_person": ["禁止使用的具体第二人称或称谓，如 あなた；没有则为空数组"],
        "request_style": "请求时的固定表达层级",
        "command_style": "命令时的固定表达层级",
        "sentence_end": "稳定句末语气与需要回避的说法",
        "preferred_endings": ["优先使用的具体日语句末形式"],
        "allowed_endings": ["允许使用的具体日语句末形式"],
        "avoid_endings": ["禁止使用的具体日语句末形式"],
        "era_style": "只能是 modern、light_historical 或 strong_historical",
        "dialect": "standard 或明确方言名称",
        "gender_marking": "性别化表达策略，避免无证据的刻板女性语或男性语",
        "thought_style": "内心独白的简体节奏、人称与用词特点",
        "address_overrides": [
          {"when": "仅限证据明确的对象或关系", "politeness": "plain/polite", "honorific_level": "none/light/strict", "humble_level": "none/light/strict", "first_person": "覆盖人称", "address": "覆盖称谓"}
        ]
      },
      "en": {
        "formality": "只能是 formal、neutral 或 casual 之一",
        "style_note": "英语措辞、句式、缩写和语气要求",
        "directness": "只能是 direct、balanced 或 indirect 之一",
        "contractions": "只能是 prefer、allow 或 avoid 之一",
        "address_strategy": "姓名、身份称谓、Sir、My Lord 等称呼策略",
        "sentence_style": "句长、完整句或碎句、节奏特点",
        "vocabulary": "时代感、词汇难度、俚语与惯用语要求",
        "profanity_level": "只能是 none、mild、moderate 或 strong 之一",
        "humor_style": "幽默、讽刺或反讽特点；没有则为空",
        "thought_style": "英语内心独白的节奏、句式和语气",
        "signature_phrases": ["最多3个应稳定复用的英文口头表达；没有则为空数组"],
        "avoid": ["必须是具体禁止出现的英文单词或短语；没有则为空数组"],
        "address_overrides": [
          {"when": "仅限证据明确的对象或关系", "formality": "formal/neutral/casual", "directness": "direct/balanced/indirect", "contractions": "prefer/allow/avoid", "address": "覆盖称谓", "style_note": "覆盖要求"}
        ]
      }
    }
  }
}

强制规则：
1. characters 的键必须精确使用输入给出的 speaker_id，禁止改写、翻译或新建。
2. 每个角色必须有一套稳定的默认结论。address_overrides 只能写原文证据反复支持的少量规则；无证据时返回空数组，禁止“视情况而定”。
3. 分别描述角色说出口的台词和内心独白；旁白由作品规范控制，不得混入人物风格。
4. 日语语体要符合身份、时代、社会等级、性格与原文措辞，不要机械地让所有古代角色都用敬语。
5. 英语风格要保留角色区分度，避免所有角色都写成同一种现代口语。
6. 不输出证据、置信度或未定义字段。
7. 证据冲突时，取出现最多且最符合角色身份的稳定风格。
8. 必须覆盖本次输入的每一个 speaker_id；证据不足时依据身份和性格给出保守但明确的方案。
9. 所有字段一律用中文写说明，只允许插入具体日语词形（如 私、僕、～ます、～だ、～でございます）。
   禁止出现日语助词连接的句子，例如「丁寧語を基調としつつ、命令時は威圧的な口調に転じる」这种写法必须改成
   「以丁寧語为基调，命令时转为威压口吻」。无论角色是否使用敬语，说明文字都不得整句日语化。
10. is_npc 为 true 的条目是没有角色档案的配角，通常以身份称谓出场（如丫鬟、官兵、下人）。
    这类角色只依据其身份称谓和原文台词推断语体：对主人或上位者说话时用与身份相符的敬语等级，
    人称与句末同样必须给出唯一确定方案，不能因为资料少就留空或写模糊表述。
11. 英语不得把所有角色统一成现代美式口语。缩写、直接程度、称谓、粗口和时代词汇必须分别给出明确结论。
12. avoid 只能填写可直接匹配的英文词或短语，不得填写“避免现代俚语”之类抽象说明。
13. 日语 politeness、honorific_level、humble_level 必须分开判断：描述上位者行为用尊敬语，描述己方行为用谦让语，不能只写一个笼统的“敬语”。
14. 日语第一、第二人称能自然省略时应优先省略；pronoun_omission 通常写 prefer，不得为了展示角色人称而机械补出主语。
15. preferred_endings、allowed_endings、avoid_endings 和 avoid_second_person 只能填写可直接匹配的具体日语形式，不得填写抽象说明。
16. 无直接证据时 dialect 使用 standard，gender_marking 使用 neutral，禁止擅自添加刻板女性语尾或夸张男性语。"""


@retry(stop=stop_after_attempt(3), wait=wait_random_exponential(min=1, max=30), reraise=True)
async def merge_work_profile(client, semaphore, work_notes):
    if not work_notes:
        return {}
    result = await call_json_model(
        client,
        semaphore,
        _WORK_MERGE_SYSTEM_PROMPT,
        json.dumps(work_notes, ensure_ascii=False),
    )
    return result.get("work") if isinstance(result.get("work"), dict) else {}


def fallback_work_profile(work_notes):
    """作品规范合并失败时，按字段取第一个非空观察，避免让角色批次各自重新推断。"""
    merged = {}
    for key in ("genre", "era", "social_structure", "overall_tone", "narrative_person"):
        merged[key] = next(
            (str(note.get(key)).strip() for note in work_notes if str(note.get(key) or "").strip()),
            "",
        )
    merged.setdefault("ja_narration", "")
    merged.setdefault("ja_orthography", "")
    merged.setdefault("ja_punctuation", "")
    merged.setdefault("ja_historical_level", "modern")
    merged.setdefault("ja_dialect_policy", "")
    merged.setdefault("ja_name_reading_policy", "")
    merged.setdefault("en_narration", "")
    merged.setdefault("en_variant", "US")
    merged.setdefault("narrative_tense", "past")
    merged["narrative_person"] = (
        str(merged.get("narrative_person") or "third").lower()
        if str(merged.get("narrative_person") or "third").lower() in {"first", "third"}
        else "third"
    )
    merged.setdefault("historical_language_level", "modern")
    merged.setdefault("dialect_policy", "")
    merged.setdefault("terminology_policy", "")
    return merged


async def merge_character_batch(client, semaphore, batch, evidence, work_profile):
    payload = []
    for item in batch:
        bucket = evidence.get(item["speaker_id"]) or {}
        payload.append({
            "speaker_id": item["speaker_id"],
            "name": item["name"],
            "is_npc": item["is_npc"],
            "other_names": item["other_names"],
            "important_rank": item["important_rank"],
            "occupations": item["occupations"],
            "personality_traits": item["personality_traits"],
            "identity_evidence": bucket.get("identity") or [],
            "speech_evidence": bucket.get("speech") or [],
            "thought_evidence": bucket.get("thought") or [],
            "representative_lines": bucket.get("lines") or [],
            "representative_thoughts": bucket.get("thoughts") or [],
        })

    user_prompt = (
        f"【已冻结的作品规范（不得改写）】\n{json.dumps(work_profile, ensure_ascii=False)}\n\n"
        f"【本批角色与原文证据】\n{json.dumps(payload, ensure_ascii=False)}"
    )
    result = await call_json_model(client, semaphore, _MERGE_SYSTEM_PROMPT, user_prompt)
    characters = result.get("characters") if isinstance(result, dict) else None
    expected = {item["speaker_id"] for item in batch}
    actual = set(characters) if isinstance(characters, dict) else set()
    if not expected.issubset(actual):
        missing = "、".join(sorted(expected - actual))
        raise ValueError(f"角色合并响应缺少 speaker_id: {missing}")
    return result


async def merge_character_batch_resilient(
    client, semaphore, batch, evidence, work_profile
):
    """失败时递归拆批；单角色仍失败才判定该角色无法生成。"""
    speaker_ids = [item["speaker_id"] for item in batch]
    batch_label = "、".join(speaker_ids)
    try:
        return await merge_character_batch(
            client, semaphore, batch, evidence, work_profile
        )
    except Exception as exc:
        if len(batch) > 1:
            midpoint = len(batch) // 2
            left = batch[:midpoint]
            right = batch[midpoint:]
            logger.warning(
                f"⚠️ 角色批次合并失败({batch_label})，自动拆分为 "
                f"{len(left)}+{len(right)} 人重试: {type(exc).__name__}: {exc}"
            )
            children = await asyncio.gather(
                merge_character_batch_resilient(
                    client, semaphore, left, evidence, work_profile
                ),
                merge_character_batch_resilient(
                    client, semaphore, right, evidence, work_profile
                ),
                return_exceptions=True,
            )
            failures = [item for item in children if isinstance(item, Exception)]
            if failures:
                failed_text = "; ".join(str(item) for item in failures)
                raise RuntimeError(
                    f"拆分批次后仍有角色失败({batch_label}): {failed_text}"
                ) from failures[0]
            merged = {"characters": {}}
            for child in children:
                merged["characters"].update(child.get("characters") or {})
            return merged

        last_exc = exc
        for attempt in range(2, SINGLE_CHARACTER_MERGE_ATTEMPTS + 1):
            delay = min(2 ** (attempt - 2), 5)
            logger.warning(
                f"⚠️ 单角色合并失败({batch_label})，{delay}s 后进行第 "
                f"{attempt}/{SINGLE_CHARACTER_MERGE_ATTEMPTS} 次尝试: "
                f"{type(last_exc).__name__}: {last_exc}"
            )
            await asyncio.sleep(delay)
            try:
                return await merge_character_batch(
                    client, semaphore, batch, evidence, work_profile
                )
            except Exception as retry_exc:
                last_exc = retry_exc
        raise RuntimeError(
            f"角色 {batch_label} 合并连续失败 "
            f"{SINGLE_CHARACTER_MERGE_ATTEMPTS} 次: "
            f"{type(last_exc).__name__}: {last_exc}"
        ) from last_exc


async def merge_observations(client, semaphore, character_context, observations):
    """按角色分批合并，避免单次请求塞入全部观察导致超上下文或截断。"""
    evidence, work_notes, unmapped = regroup_observations(observations, character_context)
    try:
        work_profile = await merge_work_profile(client, semaphore, work_notes)
    except Exception as exc:
        logger.warning(f"⚠️ 作品语体合并失败，使用确定性降级结果: {exc}")
        work_profile = fallback_work_profile(work_notes)
    if not work_profile:
        work_profile = fallback_work_profile(work_notes)
    batches = chunk_list(character_context, MERGE_BATCH_SIZE)
    logger.info(f"🔄 合并阶段：{len(character_context)} 个角色，分 {len(batches)} 批")

    results = await asyncio.gather(*[
        merge_character_batch_resilient(
            client, semaphore, batch, evidence, work_profile
        )
        for batch in batches
    ], return_exceptions=True)

    merged_characters = {}
    failed_batches = 0
    for batch, result in zip(batches, results):
        if isinstance(result, Exception):
            failed_batches += 1
            names = "、".join(item["speaker_id"] for item in batch)
            logger.error(f"❌ 角色批次合并失败({names}): {result}")
            continue
        if isinstance(result.get("characters"), dict):
            merged_characters.update(result["characters"])

    if failed_batches:
        raise RuntimeError(
            f"角色合并不完整：{failed_batches}/{len(batches)} 个初始批次最终失败；"
            "为保护已有数据，本次不会入库"
        )

    combined = {
        "work": work_profile,
        "characters": merged_characters,
        "unmapped": unmapped,
    }
    return normalize_result(combined, character_context, evidence)


def normalize_result(result, character_context, evidence=None):
    allowed = {str(item["speaker_id"]): item for item in character_context}
    raw_characters = result.get("characters")
    raw_characters = raw_characters if isinstance(raw_characters, dict) else {}
    normalized = {}

    missing = []
    for speaker_id, source in allowed.items():
        style = raw_characters.get(speaker_id)
        if not isinstance(style, dict):
            # 缺失时给保守兜底，避免下游翻译拿不到该角色语体而退化
            missing.append(speaker_id)
            style = {}
        character_evidence = (evidence or {}).get(speaker_id) or {}
        evidence_count = sum(
            len(character_evidence.get(key) or [])
            for key in ("identity", "speech", "thought", "lines", "thoughts")
        )
        ja = style.get("ja") if isinstance(style.get("ja"), dict) else {}
        en = style.get("en") if isinstance(style.get("en"), dict) else {}
        ja_overrides = ja.get("address_overrides") if isinstance(ja.get("address_overrides"), list) else []
        en_overrides = en.get("address_overrides") if isinstance(en.get("address_overrides"), list) else []
        formality = str(en.get("formality") or "").strip().lower()
        if formality not in ALLOWED_FORMALITY:
            formality = "neutral"
        directness = str(en.get("directness") or "").strip().lower()
        if directness not in ALLOWED_DIRECTNESS:
            directness = "balanced"
        contractions = str(en.get("contractions") or "").strip().lower()
        if contractions not in ALLOWED_CONTRACTIONS:
            contractions = "allow"
        profanity_level = str(en.get("profanity_level") or "").strip().lower()
        if profanity_level not in ALLOWED_PROFANITY:
            profanity_level = "none"
        politeness = str(ja.get("politeness") or "").strip().lower()
        if politeness not in ALLOWED_JA_POLITENESS:
            register_text = str(ja.get("register") or "")
            politeness = "polite" if re.search(r"丁寧|です|ます|敬語", register_text) else "plain"
        honorific_level = str(ja.get("honorific_level") or "").strip().lower()
        if honorific_level not in ALLOWED_JA_HONORIFIC:
            honorific_level = "light" if "尊敬" in str(ja.get("register") or "") else "none"
        humble_level = str(ja.get("humble_level") or "").strip().lower()
        if humble_level not in ALLOWED_JA_HONORIFIC:
            humble_level = "light" if re.search(r"謙譲|谦让", str(ja.get("register") or "")) else "none"
        pronoun_omission = str(ja.get("pronoun_omission") or "prefer").strip().lower()
        if pronoun_omission not in ALLOWED_PRONOUN_OMISSION:
            pronoun_omission = "prefer"
        era_style = str(ja.get("era_style") or "").strip().lower()
        if era_style not in ALLOWED_JA_ERA_STYLE:
            era_style = "modern"
        has_any_style = any(str(value or "").strip() for value in (
            ja.get("register"), ja.get("politeness"), ja.get("honorific_level"),
            ja.get("first_person"), ja.get("sentence_end"), ja.get("request_style"),
            en.get("style_note"), en.get("sentence_style"), en.get("vocabulary"),
            en.get("address_strategy"), ja.get("thought_style"), en.get("thought_style"),
        ))
        normalized[speaker_id] = {
            "name": str(style.get("name") or source["name"]),
            "identity": str(
                style.get("identity") or "、".join(source["occupations"]) or source["name"]
            ),
            "speech_traits": str(
                style.get("speech_traits") or "、".join(source["personality_traits"][:6])
            ),
            "ja": {
                "register": str(ja.get("register") or ""),
                "politeness": politeness,
                "honorific_level": honorific_level,
                "humble_level": humble_level,
                "first_person": str(ja.get("first_person") or ""),
                "first_person_reading": str(ja.get("first_person_reading") or ""),
                "first_person_aliases": _string_list(ja.get("first_person_aliases"), 6),
                "pronoun_omission": pronoun_omission,
                "second_person": str(ja.get("second_person") or ""),
                "address_terms": _string_dict(ja.get("address_terms"), 12),
                "avoid_second_person": _string_list(ja.get("avoid_second_person"), 12),
                "request_style": str(ja.get("request_style") or ""),
                "command_style": str(ja.get("command_style") or ""),
                "sentence_end": str(ja.get("sentence_end") or ""),
                "preferred_endings": _string_list(ja.get("preferred_endings"), 10),
                "allowed_endings": _string_list(ja.get("allowed_endings"), 12),
                "avoid_endings": _string_list(ja.get("avoid_endings"), 12),
                "era_style": era_style,
                "dialect": str(ja.get("dialect") or "standard"),
                "gender_marking": str(ja.get("gender_marking") or "neutral"),
                "thought_style": str(ja.get("thought_style") or ""),
                "address_overrides": [item for item in ja_overrides if isinstance(item, dict)][:6],
            },
            "en": {
                "formality": formality,
                "style_note": str(en.get("style_note") or ""),
                "directness": directness,
                "contractions": contractions,
                "address_strategy": str(en.get("address_strategy") or ""),
                "sentence_style": str(en.get("sentence_style") or ""),
                "vocabulary": str(en.get("vocabulary") or ""),
                "profanity_level": profanity_level,
                "humor_style": str(en.get("humor_style") or ""),
                "thought_style": str(en.get("thought_style") or ""),
                "signature_phrases": _string_list(en.get("signature_phrases"), 3),
                "avoid": _string_list(en.get("avoid"), 12),
                "address_overrides": [item for item in en_overrides if isinstance(item, dict)][:6],
            },
            "quality": {
                "status": "complete" if (
                    bool(str(ja.get("register") or ja.get("politeness") or "").strip())
                    and bool(str(
                        en.get("style_note") or en.get("sentence_style") or en.get("vocabulary") or ""
                    ).strip())
                    and evidence_count > 0
                ) else ("inferred" if has_any_style else "insufficient"),
                "evidence_count": evidence_count,
            },
        }
    if missing:
        logger.warning(f"⚠️ 以下角色缺少模型结论，已用角色资料兜底: {'、'.join(missing)}")

    work = result.get("work") if isinstance(result.get("work"), dict) else {}
    unmapped = result.get("unmapped") if isinstance(result.get("unmapped"), list) else []
    return {
        "schema_version": STYLE_SCHEMA_VERSION,
        "work": {
            "genre": str(work.get("genre") or ""),
            "era": str(work.get("era") or ""),
            "social_structure": str(work.get("social_structure") or ""),
            "overall_tone": str(work.get("overall_tone") or ""),
            "ja_narration": str(work.get("ja_narration") or ""),
            "ja_orthography": str(work.get("ja_orthography") or ""),
            "ja_punctuation": str(work.get("ja_punctuation") or ""),
            "ja_historical_level": (
                str(work.get("ja_historical_level") or "modern").lower()
                if str(work.get("ja_historical_level") or "modern").lower()
                in ALLOWED_JA_ERA_STYLE
                else "modern"
            ),
            "ja_dialect_policy": str(work.get("ja_dialect_policy") or ""),
            "ja_name_reading_policy": str(work.get("ja_name_reading_policy") or ""),
            "en_narration": str(work.get("en_narration") or ""),
            "en_variant": (
                str(work.get("en_variant") or "US").upper()
                if str(work.get("en_variant") or "US").upper() in ALLOWED_EN_VARIANTS
                else "US"
            ),
            "narrative_tense": (
                str(work.get("narrative_tense") or "past").lower()
                if str(work.get("narrative_tense") or "past").lower() in {"past", "present"}
                else "past"
            ),
            "narrative_person": (
                str(work.get("narrative_person") or "third").lower()
                if str(work.get("narrative_person") or "third").lower() in {"first", "third"}
                else "third"
            ),
            "historical_language_level": (
                str(work.get("historical_language_level") or "modern").lower()
                if str(work.get("historical_language_level") or "modern").lower()
                in {"modern", "light", "strong"}
                else "modern"
            ),
            "dialect_policy": str(work.get("dialect_policy") or ""),
            "terminology_policy": str(work.get("terminology_policy") or ""),
        },
        "characters": normalized,
        "unmapped": list(dict.fromkeys(str(item) for item in unmapped if str(item).strip())),
    }


def save_local(task_id, data):
    output_dir = os.path.join(OUTPUTS_PATH, str(task_id))
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, "character_styles.json")
    with open(output_path, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
    logger.info(f"📁 角色语体表已保存到本地: {output_path}")
    return output_path


def save_character_styles(task_id, data):
    try:
        with engine.begin() as conn:
            row = conn.execute(
                sa_text("""
                    INSERT INTO character_styles (task_id, data, create_time, update_time)
                    VALUES (:tid, CAST(:data AS JSONB), NOW(), NOW())
                    ON CONFLICT (task_id) DO UPDATE SET
                        data = EXCLUDED.data,
                        update_time = NOW()
                    RETURNING id
                """),
                {
                    "tid": str(task_id),
                    "data": json.dumps(data, ensure_ascii=False),
                },
            ).fetchone()
        if not row:
            raise RuntimeError("RETURNING id 未返回结果")
        logger.info(f"💾 角色语体表入库成功，DB_ID: {row[0]}")
        return row[0]
    except Exception as exc:
        logger.error(f"❌ 角色语体表入库失败: {exc}")
        try:
            save_local(task_id, data)
        except Exception as local_exc:
            logger.error(f"❌ 本地降级保存也失败: {local_exc}")
        return None


def load_saved_character_styles(task_id):
    try:
        with engine.connect() as conn:
            row = conn.execute(
                sa_text("SELECT data FROM character_styles WHERE task_id = :tid LIMIT 1"),
                {"tid": str(task_id)},
            ).fetchone()
        return _as_dict(row[0]) if row else {}
    except Exception as exc:
        logger.warning(f"⚠️ 检查已有角色语体表失败，将重新生成: {exc}")
        return {}


def validate_saved_character_styles(saved, expected_speaker_ids, current_hashes):
    """验证已有记录能否安全复用，返回 (是否可用, 原因)。"""
    valid, reason, _ = validate_complete_result(saved, expected_speaker_ids)
    if not valid:
        return False, reason

    metadata = saved.get("metadata") if isinstance(saved, dict) else {}
    metadata = metadata if isinstance(metadata, dict) else {}
    if metadata.get("status") != "complete":
        return False, "缺少完整生成标记"
    if metadata.get("expected_character_count") != len(set(expected_speaker_ids)):
        return False, "预期角色数量不匹配"
    if metadata.get("generated_character_count") != len(set(expected_speaker_ids)):
        return False, "已生成角色数量不匹配"
    for key, value in current_hashes.items():
        if metadata.get(key) != value:
            return False, f"{key} 已过期"
    return True, ""


def saved_character_styles_are_current(target_task_id):
    """主管道跳过 Step 7 前使用：必须完整且匹配当前输入。"""
    task_id, characters, conversion, stories = fetch_source_context(target_task_id)
    if not task_id:
        return False
    if not conversion:
        conversion = load_local_conversion(task_id)
    context = build_character_context(characters, conversion)
    saved = load_saved_character_styles(task_id)
    current_hashes = build_input_hashes(stories, conversion)
    valid, reason = validate_saved_character_styles(
        saved,
        [item["speaker_id"] for item in context],
        current_hashes,
    )
    if not valid:
        logger.info(f"♻️ 已有角色语体表不可复用，将重新生成: {reason}")
    return valid


async def run_extraction(client, semaphore, character_context, stories):
    roster_text = build_roster_text(character_context)
    jobs = []
    for node_id, text in stories:
        for chunk_index, chunk in enumerate(split_node_text(text), 1):
            jobs.append((
                node_id,
                chunk_index,
                analyze_text_chunk(client, semaphore, node_id, chunk_index, chunk, roster_text),
            ))

    logger.info(f"🚀 开始分析原文角色语体，共 {len(jobs)} 个分段")
    results = await asyncio.gather(*[job[2] for job in jobs], return_exceptions=True)

    observations = []
    for (node_id, chunk_index, _), result in zip(jobs, results):
        if isinstance(result, Exception):
            logger.error(f"❌ 分段分析失败 {node_id}#{chunk_index}: {result}")
        else:
            observations.append(result)

    failed = len(jobs) - len(observations)
    if not observations:
        raise RuntimeError("所有原文分段分析均失败")
    if failed and failed / len(jobs) > 0.3:
        raise RuntimeError(f"原文分段失败过多({failed}/{len(jobs)})，中止以避免生成劣质语体表")
    if failed:
        logger.warning(f"⚠️ 有 {failed}/{len(jobs)} 个分段失败，仍继续合并")
    return observations


async def main(target_task_id=None):
    """提取角色语体表。

    语体表用于字幕翻译时控制敬语与人称，缺失只降低翻译精细度。因此这里的
    任何失败（含上游数据缺失）都只告警并返回 None，绝不抛异常中断流水线。
    """
    try:
        task_id, characters, conversion, stories = await asyncio.to_thread(
            fetch_source_context, target_task_id
        )
    except Exception as exc:
        logger.warning(f"⚠️ 读取上游数据失败，跳过语体表提取: {exc}")
        return None
    if not task_id:
        logger.warning("⚠️ 未获取到 task_id，跳过语体表提取")
        return None
    if not stories:
        logger.warning("⚠️ 未获取到剧情原文，跳过语体表提取")
        return None

    try:
        if not conversion:
            conversion = load_local_conversion(task_id)
        character_context = build_character_context(characters, conversion)
    except Exception as exc:
        logger.warning(f"⚠️ 构建角色资料失败，跳过语体表提取: {exc}")
        return None
    if not character_context:
        logger.warning(
            "⚠️ 无法构建角色资料：characters 为空或缺少 change_name 映射，跳过语体表提取"
        )
        return None
    if not API_KEY:
        logger.warning("⚠️ 未设置 DASHSCOPE_API_KEY，跳过语体表提取")
        return None

    if _usage_tracker:
        _usage_tracker.set_context(task_id, "info_extractor")

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    try:
        async with AsyncOpenAI(
            api_key=API_KEY, base_url=BASE_URL, timeout=300.0, max_retries=0
        ) as client:
            observations = await run_extraction(
                client, semaphore, character_context, stories
            )
            final_data = await merge_observations(
                client, semaphore, character_context, observations
            )
    except Exception as exc:
        logger.warning(f"⚠️ 角色语体提取失败，本任务不产出语体表: {exc}")
        return None
    finally:
        if _usage_tracker:
            _usage_tracker.flush()
            _usage_tracker.clear_context()

    expected_speaker_ids = [item["speaker_id"] for item in character_context]
    is_complete, incomplete_reason, quality_counts = validate_complete_result(
        final_data, expected_speaker_ids
    )
    if not is_complete:
        logger.warning(
            f"⚠️ 角色语体结果不完整，跳过入库以保护已有数据: {incomplete_reason}"
        )
        return None

    final_data["metadata"] = {
        "model": MODEL_NAME,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "complete",
        "expected_character_count": len(expected_speaker_ids),
        "generated_character_count": len(final_data["characters"]),
        "quality_counts": quality_counts,
        **build_input_hashes(stories, conversion),
    }

    try:
        saved_id = await asyncio.to_thread(save_character_styles, task_id, final_data)
    except Exception as exc:
        logger.warning(f"⚠️ 语体表写入失败，本任务不产出语体表: {exc}")
        return None
    if not saved_id:
        logger.warning("⚠️ 完整语体表未能写入数据库，本任务不产出语体表")
        return None
    logger.info(
        f"✅ 角色语体提取完成: {len(final_data['characters'])} 个角色，"
        f"complete={quality_counts['complete']}，inferred={quality_counts['inferred']}，"
        f"{len(final_data['unmapped'])} 个未映射称呼"
    )
    return task_id


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="从原文提取角色语体表")
    parser.add_argument("--task-id", default="", help="任务 ID，留空取最新任务")
    args = parser.parse_args()
    try:
        result_task_id = asyncio.run(main(target_task_id=args.task_id))
    finally:
        engine.dispose()
    if result_task_id:
        logger.info(f"Process finished for Task ID: {result_task_id}")
    else:
        sys.exit(1)
