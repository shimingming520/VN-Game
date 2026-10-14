import json
import os
import re
from collections import Counter

from openai import OpenAI

import auto_cast_voices_from_db as casting
from common.audio_languages import AUDIO_LANG_CONFIGS
from common.llm_fallback import ModelFallback
from common.output_mirror import mirror_outputs
from resume_state import ResumeState, atomic_write_json, file_digest, stable_digest, valid_json


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
VOICES_FILE_PATH = os.getenv(
    "ELEVENLABS_VOICES_FILE_PATH",
    os.path.join(CURRENT_DIR, "voice", "elevenlabs_voices.json"),
)
OUTPUT_FILENAME = "elevenlabs_voice_mappings.json"

# ElevenLabs 官方音色库没有儿童和少年，这两个年龄段只能用 MiniMax 音色，
# 并且必须走单句 /v1/audio/speech，不能进 text-to-dialogue 批次。
MINIMAX_VOICES_FILE_PATH = os.getenv(
    "MINIMAX_VOICES_FILE_PATH",
    os.path.join(CURRENT_DIR, "voice", "minimax_voices.json"),
)
YOUNG_AGE_CATEGORIES = ("儿童", "少年")
# MiniMax 没有日语儿童/少年音色，日语角色优先借中文音色（音系更接近），
# 中文也不够时才退到英文。
YOUNG_LANGUAGE_FALLBACKS = {
    "日语": ("日语", "中文", "英语"),
    "英语": ("英语", "中文"),
    "中文": ("中文", "英语"),
}
PROVIDER_ELEVENLABS = "elevenlabs"
PROVIDER_MINIMAX = "minimax"
PROVIDER_FILENAME = "elevenlabs_voice_providers.json"

# ElevenLabs 链只负责英语；日语由 MiniMax 链选角。不能复用
# auto_cast_voices_from_db.LANG_CONFIGS，那份现在已收窄成日语。
RELAY_LOCALES = tuple(
    item.strip()
    for item in os.getenv("ELEVENLABS_RELAY_LOCALES", "en-US").split(",")
    if item.strip()
)
LANG_CONFIGS = [
    (lang_key, target_lang)
    for lang_key, target_lang in AUDIO_LANG_CONFIGS
    if lang_key in RELAY_LOCALES
]

MODEL_NAME = os.getenv("ELEVENLABS_CAST_MODEL", "qwen3.7-max")
# 主模型偶发不稳定时自动改用备选模型重发同一请求（同中转站、同调用参数）。
_model_fallback = ModelFallback(
    MODEL_NAME, name="ElevenLabsCast", logger=casting.logger
)
STEP_CACHE_VERSION = "elevenlabs-casting-v4"
PLOT_BASE_URL = os.getenv("PLOT_BASE_URL", "http://sefutech-api.kube-test.svc:3000/v1")
CANDIDATES_PER_ROLE = max(
    1, int(os.getenv("ELEVENLABS_CAST_CANDIDATES_PER_ROLE", "24"))
)
CAST_RETRIES = int(os.getenv("ELEVENLABS_CAST_RETRIES", "0"))
CAST_TIMEOUT_SECONDS = max(
    60.0, float(os.getenv("ELEVENLABS_CAST_TIMEOUT_SECONDS", "600"))
)
PROFILE_BATCH_SIZE = max(
    1, int(os.getenv("ELEVENLABS_PROFILE_BATCH_SIZE", "8"))
)
CORE_CAST_BATCH_SIZE = max(
    1, int(os.getenv("ELEVENLABS_CORE_CAST_BATCH_SIZE", "5"))
)

LANGUAGE_LABELS = {
    "中文": "中文（普通话）",
    "英语": "英文",
    "日语": "日语",
}
NATIVE_ACCENTS = {
    "中文": ("standard", "beijing mandarin", "mandarin", "chinese"),
    "日语": ("standard", "japanese", "tokyo", "kansai"),
}
VISUAL_NOVEL_USE_CASES = {
    "characters animation": 0,
    "conversational": 200,
    "narrative story": 500,
    "entertainment tv": 700,
}
VALID_GENDERS = {"男", "女", "未知"}
VALID_AGES = {"儿童", "少年", "青年", "中年", "老年", "未知"}
NON_VOICED_ROLE_IDS = {"xitong", "system", "pangbai", "narrator", "story", "nb"}
MISSING_SPEAKER_IDS = {"", "none", "null", "unknown"}


def _is_missing_speaker_id(value):
    normalized = str(value or "").strip().casefold()
    return normalized in MISSING_SPEAKER_IDS or normalized.startswith("unknown_")


AGE_WEIGHT_LABELS = {
    10: "儿童",
    20: "少年",
    30: "青年",
    40: "中年",
    50: "老年",
}


def _age_bucket(value):
    """用共享的 age_markers 归类角色和音色库年龄。

    auto_cast_voices_from_db.get_age_weight 是年龄规则的唯一事实来源；
    这里仅把权重转成当前脚本使用的标签，避免两条选角链的 marker 列表
    逐渐漂移。未知值按项目约定落到青年。
    """
    return AGE_WEIGHT_LABELS[casting.get_age_weight(value)]
# Lin En is a key character in this project.  Keep the explicitly approved
# British child voice stable across reruns and never offer it to another role.
PINNED_YOUNG_VOICES = {"LinEn·BaLuSen": "BritishChild_male_1_v1"}
PLOT_PROFILE_ALIASES = {
    # Plot uses AiLiSi/艾琳娜 while characters stores 艾琳娜·达芙.
    "AiLiSi": {"gender": "女", "age_category": "少年", "important_rank": 1},
}

_MODEL_CLIENT = None


def get_model_client():
    global _MODEL_CLIENT
    if _MODEL_CLIENT is None:
        api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("DASHSCOPE_API_KEY 未设置，无法执行 ElevenLabs AI 选角")
        _MODEL_CLIENT = OpenAI(
            api_key=api_key,
            base_url=PLOT_BASE_URL,
            max_retries=CAST_RETRIES,
            timeout=CAST_TIMEOUT_SECONDS,
        )
    return _MODEL_CLIENT


def call_model_json(system_prompt, user_prompt, operation_name):
    try:
        response, used_model = _model_fallback.chat(
            get_model_client(),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
    except Exception as exc:
        raise RuntimeError(f"{operation_name}模型调用失败：{exc}") from exc

    if casting._usage_tracker and getattr(response, "usage", None):
        try:
            casting._usage_tracker.add_llm_usage(
                response.usage,
                used_model,
                provider="dashscope",
            )
        except Exception as exc:
            casting.logger.warning(f"⚠️ API 用量记录失败，不影响选角：{exc}")

    content = response.choices[0].message.content or ""
    content = content.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        preview = content[:300] or "（空）"
        raise RuntimeError(
            f"{operation_name}返回的不是合法 JSON：{exc}；内容开头：{preview}"
        ) from exc


def normalize_gender(value):
    text = str(value or "").strip().lower()
    if text in {"male", "m", "man", "boy", "男", "男性", "少男"}:
        return "男"
    if text in {"female", "f", "woman", "girl", "女", "女性", "少女"}:
        return "女"
    female_match = re.search(
        r"(?<![a-z])(?:female|woman|girl)(?![a-z])", text
    ) or any(marker in text for marker in ("女性", "女人", "女孩", "少女"))
    male_match = re.search(
        r"(?<![a-z])(?:male|man|boy)(?![a-z])", text
    ) or any(marker in text for marker in ("男性", "男人", "男孩", "少年"))
    if female_match and not male_match:
        return "女"
    if male_match and not female_match:
        return "男"
    return "未知"


def _contains_age_marker(text, marker):
    """英文年龄标记按单词边界匹配，中文标记按子串匹配。"""
    marker = str(marker).strip().lower()
    if not marker:
        return False
    if marker.isascii() and any(char.isalpha() for char in marker):
        return re.search(
            rf"(?<![a-z]){re.escape(marker)}(?![a-z])", text
        ) is not None
    return marker in text


def normalize_age(value):
    return _age_bucket(value)


def normalize_character_age(value):
    """规范化 characters 表中的角色年龄；缺失/未知按业务约定归为青年。"""
    return normalize_age(value)


def age_distance(left, right):
    weights = {"儿童": 0, "少年": 1, "青年": 2, "中年": 3, "老年": 4}
    if left not in weights or right not in weights:
        return 1
    return abs(weights[left] - weights[right])


def load_voice_library(path):
    with open(path, "r", encoding="utf-8") as file_obj:
        data = json.load(file_obj)
    voice_list = data.get("voices_list", []) if isinstance(data, dict) else data
    voices = []
    for rank, voice in enumerate(voice_list):
        if voice.get("status") != "OK" or not voice.get("voice_id"):
            continue
        voices.append({
            "voice_id": voice["voice_id"],
            "display_name": voice.get("display_name", ""),
            "language": voice.get("language", []),
            "gender": normalize_gender(voice.get("gender")),
            "age_category": normalize_age(voice.get("age_category")),
            "tags": voice.get("tags", []),
            "description": voice.get("description", ""),
            "quality_rank": rank,
        })
    return voices


def most_common_known(values, unknown):
    known = [value for value in values if value != unknown]
    return Counter(known).most_common(1)[0][0] if known else unknown


def get_primary_body_variant(variants):
    """业务口径：人物声音画像只使用 whole_body_id=1 的形态。"""
    valid_variants = [item for item in (variants or []) if isinstance(item, dict)]
    return next(
        (
            item for item in valid_variants
            if str(item.get("whole_body_id")).strip() == "1"
        ),
        {},
    )


def parse_character_profiles(all_data):
    """按 whole_body_id=1 解析人物；未知性别按业务约定归为男性。"""
    profiles = []
    for original_name, data in (all_data or {}).items():
        if not isinstance(data, dict) or "personality_variants" not in data:
            continue

        traits = casting.get_primary_personality_text(data)

        clothing = (data.get("clothing_variants") or [{}])[0]
        occupation = clothing.get("occupation") or "未知身份"
        primary_variant = get_primary_body_variant(
            data.get("whole_body_variants") or []
        )
        gender = normalize_gender(primary_variant.get("gender"))
        if gender == "未知":
            gender = "男"
        # characters 表中的年龄缺失或未知，统一按青年角色进入选角。
        age = normalize_character_age(primary_variant.get("age_category"))
        try:
            important_rank = int(data.get("important_rank", 1))
        except (TypeError, ValueError):
            important_rank = 1

        profiles.append({
            "id": casting.get_clean_char_id(original_name),
            "original_name": original_name,
            "role_type": "main",
            "gender": gender,
            "age_category": age,
            "occupation": occupation,
            "traits": ", ".join(str(item) for item in traits[:30]),
            "important_rank": important_rank,
        })
    profiles.sort(key=lambda item: item["important_rank"], reverse=True)
    return profiles


def extract_plot_context(plot_data, role_names, limit=6):
    contexts = {name: [] for name in role_names}
    for beats in (plot_data or {}).values():
        if not isinstance(beats, list):
            continue
        for beat in beats:
            speaker = str(beat.get("speaker_name") or "")
            content = str(beat.get("content") or "").strip()
            if speaker in contexts and content and len(contexts[speaker]) < limit:
                contexts[speaker].append(content[:180])
    return contexts


def _enrich_role_profiles_batch(roles, plot_data):
    """补全一小批角色画像，并生成英文声音方向供本地候选检索。"""
    if not roles:
        return []
    contexts = extract_plot_context(
        plot_data,
        {role["original_name"] for role in roles},
    )
    input_roles = []
    for role in roles:
        item = dict(role)
        item["dialogue_samples"] = contexts.get(role["original_name"], [])
        input_roles.append(item)

    system_prompt = """
你是视觉小说角色与声音设计师。请补全角色画像，并给出用于检索英文音色库的声音方向。

规则：
1. 保留每个角色的 id，不能增加、删除或改名。
2. 已知且合理的性别、年龄不得擅自修改；未知时结合姓名、身份、性格和台词判断。
3. gender 只能是“男”“女”“未知”；age_category 只能是“儿童”“少年”“青年”“中年”“老年”“未知”。
4. voice_traits 输出 3～8 个简短英文声音标签，例如 playful、warm、deep、calm、authoritative。
5. avoid_traits 输出 0～6 个需要避开的英文声音标签。
6. voice_summary 用中文简述期望声线、能量、音高、角色原型和长期听感。

只返回 JSON 对象，格式：
{"roles":[{"id":"...","gender":"...","age_category":"...","voice_traits":["..."],"avoid_traits":["..."],"voice_summary":"..."}]}
"""
    result = call_model_json(
        system_prompt,
        json.dumps(input_roles, ensure_ascii=False, indent=2),
        "人物声音画像",
    )
    returned = result.get("roles") if isinstance(result, dict) else None
    if not isinstance(returned, list):
        raise RuntimeError("人物声音画像结果缺少 roles 数组")
    returned_by_id = {item.get("id"): item for item in returned if isinstance(item, dict)}

    enriched = []
    for role in roles:
        supplement = returned_by_id.get(role["id"])
        if not supplement:
            casting.logger.warning(
                f"⚠️ 人物声音画像缺少角色 {role['id']}，保留原画像"
            )
            item = dict(role)
            item.setdefault("voice_traits", [])
            item.setdefault("avoid_traits", [])
            item.setdefault("voice_summary", "")
            enriched.append(item)
            continue
        item = dict(role)
        suggested_gender = normalize_gender(supplement.get("gender"))
        suggested_age = normalize_age(supplement.get("age_category"))
        if item["gender"] == "未知":
            # 业务默认：模型仍无法判断时按男性处理，不能把未知留给候选
            # 排序，否则会绕过性别约束并造成女性/男性音色漂移。
            item["gender"] = "男" if suggested_gender == "未知" else suggested_gender
        if item["age_category"] == "未知":
            # 业务默认：模型仍无法判断时按青年处理。这样未知年龄不会被
            # 当成“无年龄约束”而误分到老年或其他音色。
            item["age_category"] = (
                "青年" if suggested_age == "未知" else suggested_age
            )
        item["voice_traits"] = [
            str(value).strip().lower()
            for value in supplement.get("voice_traits", [])
            if str(value).strip()
        ][:8]
        item["avoid_traits"] = [
            str(value).strip().lower()
            for value in supplement.get("avoid_traits", [])
            if str(value).strip()
        ][:6]
        item["voice_summary"] = str(supplement.get("voice_summary") or "").strip()
        enriched.append(item)
    return enriched


def enrich_role_profiles(roles, plot_data):
    """分批补全角色画像；单批失败由上层统一保留原画像。"""
    if not roles:
        return []
    enriched = []
    total_batches = (len(roles) + PROFILE_BATCH_SIZE - 1) // PROFILE_BATCH_SIZE
    for start in range(0, len(roles), PROFILE_BATCH_SIZE):
        batch = roles[start:start + PROFILE_BATCH_SIZE]
        batch_number = start // PROFILE_BATCH_SIZE + 1
        casting.logger.info(
            f"🧠 人物声音画像批次 {batch_number}/{total_batches}："
            f"{len(batch)} 个角色"
        )
        try:
            enriched.extend(_enrich_role_profiles_batch(batch, plot_data))
        except Exception as exc:
            casting.logger.warning(
                f"⚠️ 人物声音画像批次 {batch_number}/{total_batches} 失败，"
                f"保留本批原画像：{exc}"
            )
            for role in batch:
                item = dict(role)
                item.setdefault("voice_traits", [])
                item.setdefault("avoid_traits", [])
                item.setdefault("voice_summary", "")
                enriched.append(item)
    return enriched


def is_lang_compatible(voice, target_lang):
    expected = LANGUAGE_LABELS[target_lang]
    return expected in voice.get("language", [])


def voice_search_text(voice):
    return (
        " ".join(voice.get("tags", []))
        + " "
        + voice.get("display_name", "")
        + " "
        + voice.get("description", "")
    ).lower()


def accent_penalty(voice, target_lang):
    if target_lang not in NATIVE_ACCENTS:
        return 0
    tags = " ".join(voice.get("tags", [])).lower()
    if any(accent in tags for accent in NATIVE_ACCENTS[target_lang]):
        return 0
    return 400


def candidate_score(role, voice, target_lang):
    score = 0
    score += age_distance(role["age_category"], voice["age_category"]) * 1_000
    score += accent_penalty(voice, target_lang)
    score += min(voice.get("quality_rank", 0), 3_000) // 20

    tags_text = " ".join(voice.get("tags", [])).lower()
    use_case_penalty = min(
        (
            penalty
            for use_case, penalty in VISUAL_NOVEL_USE_CASES.items()
            if use_case in tags_text
        ),
        default=900,
    )
    score += use_case_penalty

    searchable = voice_search_text(voice)
    score -= sum(220 for trait in role.get("voice_traits", []) if trait in searchable)
    score += sum(800 for trait in role.get("avoid_traits", []) if trait in searchable)
    return score


def young_language_priority(voice, target_lang):
    """儿童/少年复用时的语言优先级，数值越小越优。"""
    for index, language in enumerate(
        YOUNG_LANGUAGE_FALLBACKS.get(target_lang, (target_lang,))
    ):
        if is_lang_compatible(voice, language):
            return index
    return len(YOUNG_LANGUAGE_FALLBACKS.get(target_lang, (target_lang,))) + 1


def is_young_role(role):
    return role.get("age_category") in YOUNG_AGE_CATEGORIES


def build_role_candidates(role, language_voices, target_lang):
    # 选角函数也可能被外部调用方直接使用，不能假定 role 已经经过
    # parse_character_profiles。统一规范化 male/female、M/F 等别名，避免
    # 原始英文值绕过下面的性别候选过滤。
    # 未知性别按业务约定视为男性；已知性别绝不放宽到异性音色。
    normalized_gender = normalize_gender(role.get("gender"))
    if normalized_gender == "未知":
        normalized_gender = "男"
    candidates = list(language_voices)
    if normalized_gender in ("男", "女"):
        gender_matched = [
            voice for voice in candidates if voice["gender"] == normalized_gender
        ]
        if gender_matched:
            candidates = gender_matched
        else:
            raise RuntimeError(
                f"{target_lang}/{role['id']} 没有可用的{normalized_gender}性音色；"
                "拒绝使用异性音色"
            )

    # 年龄是角色身份约束。存在同年龄候选时只允许在同年龄候选中分配，
    # 避免“优先不复用音色”把儿童/青年分配到其他年龄段。
    if role.get("age_category") in VALID_AGES - {"未知"}:
        age_matched = [
            voice for voice in candidates
            if voice["age_category"] == role["age_category"]
        ]
        if age_matched:
            candidates = age_matched
        else:
            casting.logger.warning(
                f"⚠️ [{target_lang}] {role['id']} 无{role['age_category']}年龄段音色，"
                "放宽年龄条件继续分配"
            )

    candidates.sort(key=lambda voice: (
        candidate_score(role, voice, target_lang),
        voice["voice_id"],
    ))
    if not candidates:
        raise RuntimeError(f"{target_lang}/{role['id']} 没有可用候选音色")
    # 全量候选用于本地分层匹配；发给模型时再截取前 N 个。
    # 否则角色较多时会因所有人都只看到同一小段候选而过早复用。
    return candidates


def build_young_role_candidates(
    role, minimax_voices, target_lang, *, include_fallbacks=False
):
    """儿童/少年角色的 MiniMax 候选：本语言 → 跨语言 → 全年龄段兜底。

    ElevenLabs 官方库没有这两个年龄段，MiniMax 的日语儿童音色也是空的，
    所以跨语言降级是常态而非异常。
    """
    same_age = [
        voice for voice in minimax_voices
        if voice["age_category"] == role["age_category"]
    ]
    # 儿童和少年之间可以互相顶替，比掉到成人音色好得多。
    near_age = [
        voice for voice in minimax_voices
        if voice["age_category"] in YOUNG_AGE_CATEGORIES
    ]

    # 性别是硬约束：只要整个 MiniMax 年轻音色池里存在同性别音色，
    # 所有语言层级都只从同性别中选，不能因为英语候选先出现就误用异性。
    gender = normalize_gender(role.get("gender"))
    if gender == "未知":
        gender = "男"

    def by_gender(pool):
        return [voice for voice in pool if voice.get("gender") == gender]

    tiers = []
    for age_pool, age_label in (
        (same_age, "同龄段"),
        (near_age, "邻近年龄段"),
    ):
        for fallback_lang in YOUNG_LANGUAGE_FALLBACKS.get(
            target_lang, (target_lang,)
        ):
            label = (
                f"本语言{age_label}"
                if fallback_lang == target_lang
                else f"{fallback_lang}{age_label}"
            )
            tiers.append((
                [v for v in age_pool if is_lang_compatible(v, fallback_lang)],
                label,
            ))
        # 语言全部落空时还允许任意语言，避免整条链因为音色不足中断。
        tiers.append((age_pool, f"任意语言{age_label}"))

    # 选角器需要完整的降级池，才能在英语儿童/少年音色耗尽后启用中文。
    # 保留默认的“首个语言层级”行为，兼容外部候选预览和日语链路；
    # cast_young_roles 显式请求完整池。
    if not include_fallbacks:
        for pool, tier in tiers:
            candidates = by_gender(pool)
            if candidates:
                candidates.sort(key=lambda voice: (
                    age_distance(role["age_category"], voice["age_category"]),
                    candidate_score(role, voice, target_lang),
                    voice["voice_id"],
                ))
                return candidates[:CANDIDATES_PER_ROLE], tier

    # 不在第一个非空层级直接结束。英语儿童/少年音色耗尽后，
    # 仍需看到中文备选；按层级顺序合并即可保持英语优先、中文次之。
    merged = []
    seen_ids = set()
    for tier_index, (pool, tier) in enumerate(tiers):
        candidates = by_gender(pool)
        candidates.sort(key=lambda voice: (
            age_distance(role["age_category"], voice["age_category"]),
            candidate_score(role, voice, target_lang),
            voice["voice_id"],
        ))
        for voice in candidates:
            if voice["voice_id"] in seen_ids:
                continue
            seen_ids.add(voice["voice_id"])
            merged.append((tier_index, tier, voice))
    if merged:
        merged.sort(key=lambda item: (
            item[0],
            candidate_score(role, item[2], target_lang),
            item[2]["voice_id"],
        ))
        first_tier = merged[0][1]
        merged_voices = [item[2] for item in merged]
        return (
            merged_voices if include_fallbacks
            else merged_voices[:CANDIDATES_PER_ROLE],
            first_tier,
        )

    # MiniMax 库中没有童声时，最后使用任意年龄音色。
    # 音色不理想也不能因资源枯竭中断整条生产链。
    candidates = by_gender(minimax_voices)
    if candidates:
        candidates.sort(key=lambda voice: (
            age_distance(role["age_category"], voice["age_category"]),
            candidate_score(role, voice, target_lang),
            voice["voice_id"],
        ))
        return candidates[:CANDIDATES_PER_ROLE], "任意年龄降级"
    return [], f"MiniMax 库没有可用的{gender}性音色"


def compact_voice(voice):
    return {
        "voice_id": voice["voice_id"],
        "name": voice.get("display_name", ""),
        "gender": voice["gender"],
        "age_category": voice["age_category"],
        "tags": voice.get("tags", []),
        "description": re.sub(r"\s+", " ", voice.get("description", ""))[:260],
    }


def validate_casting(
    result, roles, candidates_by_role, target_lang, *, require_unique=True
):
    if not isinstance(result, dict):
        return ["结果不是 JSON 对象"]
    expected_ids = {role["id"] for role in roles}
    errors = []
    missing = sorted(expected_ids - set(result))
    extra = sorted(set(result) - expected_ids)
    if missing:
        errors.append(f"缺少角色：{', '.join(missing)}")
    if extra:
        errors.append(f"多余角色：{', '.join(extra)}")

    role_by_id = {role["id"]: role for role in roles}
    if require_unique:
        used = Counter(result.values())
        duplicates = sorted(
            voice_id for voice_id, count in used.items() if count > 1
        )
        if duplicates:
            errors.append(f"重复音色：{', '.join(duplicates)}")

    for role_id, voice_id in result.items():
        if role_id not in role_by_id:
            continue
        candidate_map = {
            voice["voice_id"]: voice for voice in candidates_by_role[role_id]
        }
        voice = candidate_map.get(voice_id)
        if not voice:
            errors.append(f"{role_id} 使用了专属候选外音色：{voice_id}")
            continue
        role = role_by_id[role_id]
        role_gender = normalize_gender(role.get("gender"))
        if role_gender == "未知":
            role_gender = "男"
        if voice["gender"] != role_gender:
            errors.append(f"{role_id} 性别不匹配")
        has_age_match = any(
            item["age_category"] == role["age_category"]
            for item in candidates_by_role[role_id]
        )
        if (
            role["age_category"] in VALID_AGES - {"未知"}
            and has_age_match
            and voice["age_category"] != role["age_category"]
        ):
            errors.append(f"{role_id} 年龄不匹配")
    return errors


def safe_important_rank(role):
    try:
        return int((role or {}).get("important_rank", 0))
    except (TypeError, ValueError):
        return 0


def allocation_candidate_key(
    role, voice, target_lang, ai_voice_map, usage_counts
):
    """与 MiniMax 主分配器一致：先保护未使用音色，再比匹配度。"""
    voice_id = voice["voice_id"]
    usage_count = usage_counts.get(voice_id, 0)
    return (
        int(usage_count > 0),
        usage_count,
        age_distance(role["age_category"], voice["age_category"]),
        accent_penalty(voice, target_lang),
        int(voice_id != ai_voice_map.get(role["id"])),
        candidate_score(role, voice, target_lang),
        voice_id.casefold(),
        voice_id,
    )


def match_unique_voices_for_rank_group(
    roles, candidates_by_role, target_lang, ai_voice_map, usage_counts
):
    """在同一 important_rank 内做最大唯一匹配，避免先到角色抢音色。"""
    candidates_by_id = {}
    for role in roles:
        unused = [
            voice for voice in candidates_by_role[role["id"]]
            if usage_counts.get(voice["voice_id"], 0) == 0
        ]
        unused.sort(key=lambda voice: allocation_candidate_key(
            role, voice, target_lang, ai_voice_map, usage_counts
        ))
        candidates_by_id[role["id"]] = [
            voice["voice_id"] for voice in unused
        ]

    voice_to_role = {}
    role_to_voice = {}

    def assign(role_id, visited_voice_ids):
        for voice_id in candidates_by_id.get(role_id, []):
            if voice_id in visited_voice_ids:
                continue
            visited_voice_ids.add(voice_id)
            current_role = voice_to_role.get(voice_id)
            if current_role is None or assign(current_role, visited_voice_ids):
                voice_to_role[voice_id] = role_id
                role_to_voice[role_id] = voice_id
                if (
                    current_role is not None
                    and current_role != role_id
                    and role_to_voice.get(current_role) == voice_id
                ):
                    role_to_voice.pop(current_role, None)
                return True
        return False

    for role in roles:
        assign(role["id"], set())
    return role_to_voice


def choose_ranked_character_roles(
    roles, candidates_by_role, ai_voice_map, target_lang
):
    """
    characters 角色按 important_rank 从高到低分层分配。

    每层先做最大唯一匹配；音色真正耗尽时才按已用次数均匀复用。
    AI 结果是匹配偏好，不会破坏同层全局分配。
    """
    result = {}
    usage_counts = Counter()
    ordered_roles = sorted(
        roles,
        key=lambda role: (
            -safe_important_rank(role),
            role["id"].casefold(),
            role["id"],
        ),
    )
    rank_groups = []
    for role in ordered_roles:
        rank = safe_important_rank(role)
        if not rank_groups or rank_groups[-1][0] != rank:
            rank_groups.append((rank, []))
        rank_groups[-1][1].append(role)

    voice_by_id = {
        voice["voice_id"]: voice
        for candidates in candidates_by_role.values()
        for voice in candidates
    }

    def commit(role, voice_id):
        result[role["id"]] = voice_id
        usage_counts[voice_id] += 1
        voice = voice_by_id[voice_id]
        if usage_counts[voice_id] > 1:
            casting.logger.warning(
                f"   ⚠️ [Reuse] {role['id']}"
                f"(Rank:{safe_important_rank(role)}) 资源枯竭，复用音色："
                f"{voice.get('display_name', voice_id)}"
            )
        else:
            casting.logger.info(
                f"   🎭 [{target_lang}] {role['id']}"
                f"(Rank:{safe_important_rank(role)}) -> "
                f"{voice.get('display_name', voice_id)}"
            )

    for _rank, group in rank_groups:
        unique_assignments = match_unique_voices_for_rank_group(
            group,
            candidates_by_role,
            target_lang,
            ai_voice_map,
            usage_counts,
        )
        for role in group:
            voice_id = unique_assignments.get(role["id"])
            if voice_id:
                commit(role, voice_id)
        for role in group:
            if role["id"] in unique_assignments:
                continue
            selected = min(
                candidates_by_role[role["id"]],
                key=lambda voice: allocation_candidate_key(
                    role, voice, target_lang, ai_voice_map, usage_counts
                ),
            )
            commit(role, selected["voice_id"])
    return result, usage_counts


def choose_npc_roles(roles, candidates_by_role, initial_usage_counts, target_lang):
    """主角分配完成后为 NPC 选音色；资源耗尽时允许均匀复用。"""
    result = {}
    usage_counts = Counter(initial_usage_counts)
    ordered_roles = sorted(
        roles,
        key=lambda role: (
            -int(role.get("important_rank", 0)),
            role["id"],
        ),
    )
    for role in ordered_roles:
        candidates = candidates_by_role[role["id"]]
        unused = [
            voice for voice in candidates
            if usage_counts[voice["voice_id"]] == 0
        ]
        if unused:
            selected = unused[0]
            reused = False
        else:
            # candidates 已按角色匹配度排序；先选复用次数最少的音色，
            # 同次数时保留原候选顺序，避免所有 NPC 堆到同一音色。
            selected = min(
                enumerate(candidates),
                key=lambda item: (
                    usage_counts[item[1]["voice_id"]],
                    item[0],
                ),
            )[1]
            reused = True
        voice_id = selected["voice_id"]
        result[role["id"]] = voice_id
        usage_counts[voice_id] += 1
        casting.logger.info(
            f"   🎭 [{target_lang}] NPC 分配 {role['id']} -> "
            f"{selected.get('display_name', voice_id)}"
            f"{'（复用）' if reused else ''}"
        )
    return result


def cast_young_roles(
    young_roles,
    minimax_voices,
    target_lang,
    protected_role_ids=None,
):
    """给儿童/少年角色分配 MiniMax 音色。

    候选顺序固定为英语优先、中文次之（由 build_young_role_candidates 保证）。
    important_rank 前两档的儿童/少年角色先做全局唯一分配，并将其音色加入
    reserved 集合；普通角色只有在所有可用音色都被占用后才允许复用，且复用时
    仍按英语候选优先。这样不会因为儿童音色资源少而牺牲重要角色的辨识度。
    """
    protected_role_ids = set(protected_role_ids or ())
    protected_role_ids.update(
        role["id"] for role in young_roles
        if role.get("id") in PINNED_YOUNG_VOICES
    )
    result = {}
    usage_counts = Counter()
    candidate_rows = {}
    for role in young_roles:
        candidates, tier = build_young_role_candidates(
            role, minimax_voices, target_lang, include_fallbacks=True
        )
        if not candidates:
            raise RuntimeError(
                f"{target_lang}/{role['id']} 没有可用 MiniMax 儿童/少年音色"
            )
        candidate_rows[role["id"]] = (role, candidates, tier)

    # 重要角色优先，并优先处理候选较少的角色，减少唯一匹配失败。
    protected = [
        row for role_id, row in candidate_rows.items()
        if role_id in protected_role_ids
    ]
    protected.sort(key=lambda row: (
        int(row[0]["id"] not in PINNED_YOUNG_VOICES),
        len(row[1]),
        -safe_important_rank(row[0]),
        row[0]["id"],
    ))
    reserved_ids = set()
    for role, candidates, tier in protected:
        pinned_id = PINNED_YOUNG_VOICES.get(role["id"])
        if pinned_id:
            selected = next(
                (voice for voice in minimax_voices
                 if voice["voice_id"] == pinned_id),
                None,
            )
            if selected is None:
                raise RuntimeError(
                    f"{role['id']} 指定的固定儿童音色不存在：{pinned_id}"
                )
            if pinned_id in reserved_ids:
                raise RuntimeError(f"固定儿童音色被重复占用：{pinned_id}")
            result[role["id"]] = pinned_id
            reserved_ids.add(pinned_id)
            usage_counts[pinned_id] += 1
            casting.logger.info(
                f"   🧒 [{target_lang}] MiniMax 固定重要角色独占 "
                f"{role['id']}({role['age_category']},Rank:{safe_important_rank(role)}) -> "
                f"{selected.get('display_name') or pinned_id}"
            )
            continue
        unused = [voice for voice in candidates if voice["voice_id"] not in reserved_ids]
        if not unused:
            # 重要角色之间绝不复用；放宽到完整的同龄候选池重新寻找唯一音色。
            broad_candidates = [
                voice for voice in minimax_voices
                if voice["age_category"] in YOUNG_AGE_CATEGORIES
                and voice["voice_id"] not in reserved_ids
            ]
            if role.get("gender") in ("男", "女"):
                same_gender = [
                    voice for voice in broad_candidates
                    if voice.get("gender") == role["gender"]
                ]
                if same_gender:
                    broad_candidates = same_gender
            broad_candidates.sort(key=lambda voice: (
                age_distance(role["age_category"], voice["age_category"]),
                candidate_score(role, voice, target_lang),
                voice["voice_id"],
            ))
            unused = broad_candidates
        if not unused:
            raise RuntimeError(
                f"{target_lang} 重要儿童/少年角色没有足够的独立音色："
                f"{role['id']}"
            )
        selected = unused[0]
        result[role["id"]] = selected["voice_id"]
        reserved_ids.add(selected["voice_id"])
        usage_counts[selected["voice_id"]] += 1
        casting.logger.info(
            f"   🧒 [{target_lang}] MiniMax {tier}（重要角色独占） "
            f"{role['id']}({role['age_category']},Rank:{safe_important_rank(role)}) -> "
            f"{selected.get('display_name') or selected['voice_id']}"
        )

    # 普通角色先使用未占用候选；资源耗尽才复用。reserved_ids 在所有普通角色
    # 分配期间都不可抢占，确保重要角色音色不会被普通角色复用。
    ordinary = [
        row for role_id, row in candidate_rows.items()
        if role_id not in protected_role_ids
    ]
    ordinary.sort(key=lambda row: (-safe_important_rank(row[0]), row[0]["id"]))
    for role, candidates, tier in ordinary:
        unused = [
            voice for voice in candidates
            if voice["voice_id"] not in reserved_ids
            and usage_counts[voice["voice_id"]] == 0
        ]
        if unused:
            selected = unused[0]
            reused = ""
        else:
            reusable = [
                voice for voice in candidates
                if voice["voice_id"] not in reserved_ids
            ]
            if not reusable:
                # 固定角色音色（尤其 Lin En）永远不能被普通角色复用。
                reusable = [
                    voice for voice in candidates
                    if voice["voice_id"] not in PINNED_YOUNG_VOICES.values()
                ]
                if not reusable:
                    raise RuntimeError(
                        f"{target_lang} 普通儿童/少年角色 {role['id']} "
                        "没有可复用音色（固定重要角色音色禁止复用）"
                    )
            selected = min(
                enumerate(reusable),
                key=lambda item: (
                    young_language_priority(item[1], target_lang),
                    usage_counts[item[1]["voice_id"]],
                    item[0],
                ),
            )[1]
            reused = "（复用）"
        result[role["id"]] = selected["voice_id"]
        usage_counts[selected["voice_id"]] += 1
        casting.logger.info(
            f"   🧒 [{target_lang}] MiniMax {tier}{reused} "
            f"{role['id']}({role['age_category']}) -> "
            f"{selected.get('display_name') or selected['voice_id']}"
        )
    return result


def cast_language(
    roles, language_voices, target_lang, ai_roles=None, minimax_voices=None
):
    # 保证所有下游分支（成人、儿童、AI 结果校验、NPC）看到同一套性别枚举。
    # 这层防御尤其重要：部分历史调用方会传入英文 male/female，而不是
    # parse_character_profiles 产出的中文“男/女”。
    roles = [
        {
            **role,
            "gender": (
                normalize_gender(role.get("gender"))
                if normalize_gender(role.get("gender")) in {"男", "女"}
                else "男"
            ),
        }
        for role in roles
    ]
    ai_roles = ai_roles or []
    young_roles = [role for role in roles if is_young_role(role)]
    adult_roles = [role for role in roles if not is_young_role(role)]

    young_result = {}
    if young_roles and minimax_voices:
        # “前2”按 important_rank 的前两个不同档位解释，而不是前两个角色。
        # 这样同一重要性档位的多个儿童/少年角色都能获得独立音色。
        rank_source = [
            role for role in roles
            if role.get("role_type") != "npc"
            and safe_important_rank(role) > 0
        ] or list(roles)
        rank_tiers = sorted(
            {safe_important_rank(role) for role in rank_source},
            reverse=True,
        )[:2]
        protected_young_ids = {
            role["id"] for role in young_roles
            if safe_important_rank(role) in set(rank_tiers)
        }
        # 固定音色角色无论 rank 如何都必须先锁定，避免被普通角色占用。
        protected_young_ids.update(
            role["id"] for role in young_roles
            if role["id"] in PINNED_YOUNG_VOICES
        )
        young_result = cast_young_roles(
            young_roles,
            minimax_voices,
            target_lang,
            protected_role_ids=protected_young_ids,
        )
    elif young_roles:
        # MiniMax 库不可用时，儿童/少年也交给 ElevenLabs 全库
        # 按 rank 分配。音色年龄感可能不理想，但流程必须继续。
        casting.logger.warning(
            f"⚠️ [{target_lang}] MiniMax 音色库不可用，"
            f"{len(young_roles)} 个儿童/少年角色降级使用 ElevenLabs"
        )
        adult_roles.extend(young_roles)

    # 与 MiniMax 流程一致：characters 角色按 rank 分层分配，
    # 高 rank 自然先获得独立音色；剧情临时 NPC 在所有已建档角色之后分配。
    adult_character_roles = [
        role for role in adult_roles if role.get("role_type") != "npc"
    ]
    adult_npc_roles = [
        role for role in adult_roles if role.get("role_type") == "npc"
    ]
    ai_roles = [
        role for role in ai_roles
        if not is_young_role(role) and role.get("role_type") != "npc"
    ]
    if not adult_roles:
        return young_result

    candidates_by_role = {
        role["id"]: build_role_candidates(role, language_voices, target_lang)
        for role in adult_roles
    }
    ai_result = {}
    if ai_roles:
        system_prompt = f"""
你是专业的视觉小说配音选角导演，为{target_lang}版本选择核心角色音色。

硬性规则：
1. 每个角色只能从其自己的 candidates 中选择 voice_id。
2. 每个角色必须有结果，角色 ID 不得修改。
3. 本次角色之间 voice_id 必须全局唯一。
4. 不得违反性别；儿童角色必须使用儿童音色。

选择标准：主角长期听感、辨识度、情绪表现空间、人物身份、性格、能量、音高、
年龄感、voice_traits、avoid_traits、voice_summary，以及候选 tags 和 description。
只返回 JSON：{{"角色ID":"voice_id"}}，不要解释或 Markdown。
"""
        total_batches = (
            len(ai_roles) + CORE_CAST_BATCH_SIZE - 1
        ) // CORE_CAST_BATCH_SIZE
        used_ai_ids = set()
        for start in range(0, len(ai_roles), CORE_CAST_BATCH_SIZE):
            batch = ai_roles[start:start + CORE_CAST_BATCH_SIZE]
            batch_number = start // CORE_CAST_BATCH_SIZE + 1
            batch_candidates = {
                role["id"]: [
                    voice for voice in candidates_by_role[role["id"]]
                    if voice["voice_id"] not in used_ai_ids
                ][:CANDIDATES_PER_ROLE]
                for role in batch
            }
            # 候选被前批占完的角色交给本地全局唯一分配，不发送无解请求。
            request_roles = [
                role for role in batch if batch_candidates[role["id"]]
            ]
            if not request_roles:
                continue
            prompt_roles = [{
                "profile": role,
                "candidates": [
                    compact_voice(voice)
                    for voice in batch_candidates[role["id"]]
                ],
            } for role in request_roles]
            user_payload = {
                "target_language": target_lang,
                "roles": prompt_roles,
            }
            casting.logger.info(
                f"🧠 {target_lang}核心选角批次 "
                f"{batch_number}/{total_batches}：{len(request_roles)} 个角色"
            )
            try:
                proposed_result = call_model_json(
                    system_prompt,
                    json.dumps(user_payload, ensure_ascii=False, indent=2),
                    f"{target_lang}核心角色选角批次 {batch_number}",
                )
                errors = validate_casting(
                    proposed_result,
                    request_roles,
                    batch_candidates,
                    target_lang,
                )
                if errors:
                    raise RuntimeError("；".join(errors))
            except Exception as exc:
                casting.logger.warning(
                    f"⚠️ {target_lang}核心角色 AI 选角批次 "
                    f"{batch_number}/{total_batches} 失败，"
                    f"本批退回确定性规则：{exc}"
                )
                continue
            ai_result.update(proposed_result)
            used_ai_ids.update(proposed_result.values())

    character_result, usage_counts = choose_ranked_character_roles(
        adult_character_roles,
        candidates_by_role,
        ai_result,
        target_lang,
    )
    npc_result = choose_npc_roles(
        adult_npc_roles,
        candidates_by_role,
        usage_counts,
        target_lang,
    )
    result = {**npc_result, **character_result}
    errors = validate_casting(
        result,
        adult_roles,
        candidates_by_role,
        target_lang,
        require_unique=False,
    )
    if errors:
        raise RuntimeError(f"{target_lang}最终选角失败：" + "；".join(errors))
    return {**young_result, **result}


def build_npc_profiles(npc_names):
    def infer(name):
        """ElevenLabs 链自己的 NPC 名称画像规则，不依赖另一条选角器。"""
        text = str(name or "").strip().lower()

        def has_marker(marker):
            if marker.isascii() and any(char.isalpha() for char in marker):
                # NPC 命名常用 childA/girlB 这类无空格后缀；允许恰好一个
                # 字母后缀，但不把 childish/womanly 等普通单词误识别。
                return re.search(
                    rf"(?<![a-z]){re.escape(marker)}(?:[a-z](?![a-z]))?(?![a-z])",
                    text,
                ) is not None
            return marker in text

        child = any(has_marker(token) for token in (
            "child", "kid", "baby", "infant", "toddler", "newborn",
            "boy", "girl", "小孩", "孩子", "孩童", "儿童", "男孩", "女孩", "婴儿", "宝宝",
        ))
        teen = any(has_marker(token) for token in (
            "teen", "teenager", "adolescent", "juvenile", "preteen", "tween",
            "少年", "少女",
        ))
        old = any(has_marker(token) for token in (
            "old", "elder", "elderly", "senior", "aged", "retired",
            "grandpa", "grandma", "grandfather", "grandmother", "granny",
            "老年", "老人", "老者", "长者", "退休", "爷爷", "奶奶",
        ))
        middle = any(has_marker(token) for token in (
            "uncle", "aunt", "boss", "mrs", "middle", "middle-aged",
            "夫人", "叔叔", "大叔", "阿姨", "老板", "中年",
        ))
        female = any(has_marker(token) for token in (
            "female", "woman", "girl", "waitress", "女", "少女", "女孩",
            "女士", "女佣", "女仆", "女招待", "母亲", "妈妈", "奶奶", "祖母", "夫人", "公主", "王后", "阿姨",
        ))
        male = any(has_marker(token) for token in (
            "male", "man", "boy", "男", "少年", "男孩", "父亲", "爸爸", "爷爷", "祖父",
            "先生", "王子", "国王", "叔叔", "大叔", "老板",
        ))
        gender = "女" if female and not male else "男"
        # 明确 teen/adolescent 优先于 girl/boy 的泛年龄暗示，避免
        # “Teen Girl/Preteen Boy” 被错误归到儿童。
        if teen:
            age = "少年"
        elif child:
            age = "儿童"
        elif old:
            age = "老年"
        elif middle:
            age = "中年"
        else:
            age = "青年"
        return gender, age

    profiles = []
    seen_ids = set()
    for name in npc_names:
        role_id = casting.get_clean_char_id(name)
        role_key = str(role_id or "").casefold()
        if not role_id or role_key in seen_ids:
            continue
        seen_ids.add(role_key)
        gender, age = infer(name)
        alias_profile = PLOT_PROFILE_ALIASES.get(role_id)
        important_rank = 0
        if alias_profile:
            gender = alias_profile["gender"]
            age = alias_profile["age_category"]
            important_rank = alias_profile["important_rank"]
        profiles.append({
            "id": role_id,
            "original_name": name,
            "role_type": "npc",
            "gender": gender,
            "age_category": age,
            "occupation": name,
            "traits": "",
            "important_rank": important_rank,
        })
    return profiles


def extract_plot_speaker_profiles(plot_data, known_role_ids):
    """补齐剧情实际使用的 speaker_id，避免字幕阶段因别名/Unknown 回退默认音色。"""
    names_by_id = {}
    known_id_keys = {str(role_id).casefold() for role_id in known_role_ids}
    for beats in (plot_data or {}).values():
        if isinstance(beats, dict):
            beats = beats.get("script_timeline")
        if not isinstance(beats, list):
            continue
        for beat in beats:
            if not isinstance(beat, dict):
                continue
            speaker_id = str(beat.get("speaker_id") or "").strip()
            full_name = str(beat.get("full_name") or "").strip()
            speaker_name = str(beat.get("speaker_name") or "").strip()
            usable_id = (
                speaker_id
                and speaker_id.lower() not in NON_VOICED_ROLE_IDS
                and not _is_missing_speaker_id(speaker_id)
            )
            identity = speaker_id if usable_id else (
                full_name or speaker_name
            )
            if not identity:
                continue
            canonical_id = (
                speaker_id if usable_id
                else casting.get_clean_char_id(identity)
            )
            if not canonical_id or canonical_id.lower() in NON_VOICED_ROLE_IDS:
                continue
            if canonical_id.casefold() in known_id_keys:
                continue
            display_name = speaker_name or full_name or speaker_id
            names_by_id.setdefault(canonical_id.casefold(), {
                "speaker_id": canonical_id,
                "names": [],
            })["names"].append(display_name)

    profiles = []
    for speaker_key, details in sorted(names_by_id.items()):
        speaker_id = details["speaker_id"]
        name = Counter(details["names"]).most_common(1)[0][0]
        inferred = build_npc_profiles([name])[0]
        gender, age = inferred["gender"], inferred["age_category"]
        alias_profile = PLOT_PROFILE_ALIASES.get(speaker_id)
        important_rank = 0
        if alias_profile:
            gender = alias_profile["gender"]
            age = alias_profile["age_category"]
            important_rank = alias_profile["important_rank"]
        profiles.append({
            "id": speaker_id,
            "original_name": name,
            "role_type": "npc",
            "gender": gender,
            "age_category": age,
            "occupation": name,
            "traits": "",
            "important_rank": important_rank,
        })
    return profiles


def extract_plot_speaker_ids(plot_data):
    """Return canonical speaker IDs present in plot beats (excluding narration).

    Older plot rows may have a useful ``speaker_name`` but null
    ``speaker_id``/``full_name``.  Treat that name as the identity fallback so
    the NPC is not removed by the authoritative-ID filter below.
    """
    speaker_ids = set()
    for beats in (plot_data or {}).values():
        if isinstance(beats, dict):
            beats = beats.get("script_timeline")
        if not isinstance(beats, list):
            continue
        for beat in beats:
            if not isinstance(beat, dict):
                continue
            speaker_id = str(beat.get("speaker_id") or "").strip()
            if (
                speaker_id
                and speaker_id.lower() not in NON_VOICED_ROLE_IDS
                and not _is_missing_speaker_id(speaker_id)
            ):
                speaker_ids.add(speaker_id)
                continue
            speaker_name = str(beat.get("speaker_name") or "").strip()
            if not speaker_name:
                continue
            fallback_id = casting.get_clean_char_id(speaker_name)
            if fallback_id and fallback_id.lower() not in NON_VOICED_ROLE_IDS:
                speaker_ids.add(fallback_id)
    return speaker_ids


def select_core_roles(main_roles):
    """按同一任务内 important_rank 最高的两个不同值选核心角色。"""
    distinct_ranks = sorted(
        {safe_important_rank(role) for role in main_roles},
        reverse=True,
    )
    core_ranks = set(distinct_ranks[:2])
    return [
        role for role in main_roles
        if safe_important_rank(role) in core_ranks
    ]


def validate_final_mapping(combined_map, roles, all_voices, minimax_voices=None):
    required_ids = {role["id"] for role in roles}
    available_ids = {voice["voice_id"] for voice in all_voices}
    minimax_ids = {voice["voice_id"] for voice in (minimax_voices or [])}
    valid_voice_ids = available_ids | minimax_ids
    role_by_id = {role["id"]: role for role in roles}
    voice_by_id = {
        voice["voice_id"]: voice
        for voice in [*all_voices, *(minimax_voices or [])]
    }
    errors = []
    for lang_key, _ in LANG_CONFIGS:
        mapping = combined_map.get(lang_key, {})
        missing = sorted(required_ids - set(mapping))
        invalid = sorted(
            role_id for role_id, voice_id in mapping.items()
            if voice_id not in valid_voice_ids
        )
        if missing:
            errors.append(f"{lang_key} 缺少角色：{', '.join(missing)}")
        if invalid:
            errors.append(f"{lang_key} 无效音色角色：{', '.join(invalid)}")
        for role_id, voice_id in mapping.items():
            role = role_by_id.get(role_id)
            voice = voice_by_id.get(voice_id)
            if not role or not voice:
                continue
            expected_gender = normalize_gender(role.get("gender"))
            if expected_gender == "未知":
                expected_gender = "男"
            if voice.get("gender") != expected_gender:
                errors.append(
                    f"{lang_key}/{role_id} 性别不匹配："
                    f"角色={expected_gender}, 音色={voice.get('gender')}"
                )
            expected_age = normalize_age(role.get("age_category"))
            actual_age = normalize_age(voice.get("age_category"))
            if expected_age in YOUNG_AGE_CATEGORIES:
                if voice_id not in minimax_ids or actual_age not in YOUNG_AGE_CATEGORIES:
                    errors.append(
                        f"{lang_key}/{role_id} 儿童/少年角色使用了错误音色：{voice_id}"
                    )
            elif voice_id in minimax_ids:
                errors.append(
                    f"{lang_key}/{role_id} 成人角色使用了 MiniMax 音色：{voice_id}"
                )
    if errors:
        raise RuntimeError("ElevenLabs 最终映射校验失败；" + "；".join(errors))


def validate_cached_mapping(mapping, provider_map, roles, all_voices, minimax_voices):
    """断点命中前校验性别、年龄和 provider，防止旧错映射被复用。"""
    role_by_id = {role["id"]: role for role in roles}
    voice_by_id = {
        voice["voice_id"]: voice
        for voice in [*all_voices, *minimax_voices]
    }
    minimax_ids = {voice["voice_id"] for voice in minimax_voices}
    errors = []
    for lang_key, _ in LANG_CONFIGS:
        lang_mapping = mapping.get(lang_key)
        lang_providers = provider_map.get(lang_key)
        if not isinstance(lang_mapping, dict) or not isinstance(lang_providers, dict):
            errors.append(f"{lang_key} 映射/provider 不是对象")
            continue
        for role_id, role in role_by_id.items():
            voice_id = lang_mapping.get(role_id)
            declared_provider = lang_providers.get(role_id)
            voice = voice_by_id.get(voice_id)
            if voice is None:
                errors.append(f"{lang_key}/{role_id} 音色不存在：{voice_id}")
                continue
            inferred_provider = (
                PROVIDER_MINIMAX if voice_id in minimax_ids
                else PROVIDER_ELEVENLABS
            )
            if declared_provider != inferred_provider:
                errors.append(
                    f"{lang_key}/{role_id} provider 不一致："
                    f"{declared_provider!r}!={inferred_provider!r}"
                )
            expected_gender = normalize_gender(role.get("gender"))
            if expected_gender in {"男", "女"} and voice.get("gender") != expected_gender:
                errors.append(
                    f"{lang_key}/{role_id} 性别不匹配："
                    f"角色={expected_gender}, 音色={voice.get('gender')}"
                )
            expected_age = normalize_age(role.get("age_category"))
            actual_age = normalize_age(voice.get("age_category"))
            if expected_age in YOUNG_AGE_CATEGORIES:
                if voice_id not in minimax_ids or actual_age not in YOUNG_AGE_CATEGORIES:
                    errors.append(
                        f"{lang_key}/{role_id} 儿童/少年角色使用了非 MiniMax 年轻音色："
                        f"{voice_id}/{actual_age}"
                    )
            elif voice_id in minimax_ids:
                errors.append(
                    f"{lang_key}/{role_id} 成人角色使用了 MiniMax 音色：{voice_id}"
                )
    return errors


def run_auto_casting(target_task_id=None):
    casting.logger.info("🎬 开始 ElevenLabs 视觉小说 AI 选角流程……")
    task_id, char_json_data = casting.fetch_character_data(target_task_id)
    effective_task_id = target_task_id or task_id
    _, plot_json_data = casting.fetch_plot_data(effective_task_id)
    casting._NAME_CONVERSION = casting.fetch_name_mappings(effective_task_id)

    if not char_json_data or not plot_json_data:
        raise RuntimeError("ElevenLabs 选角数据缺失：characters 或 plots 为空")
    if not os.path.exists(VOICES_FILE_PATH):
        raise RuntimeError(f"未找到 ElevenLabs 音色库：{VOICES_FILE_PATH}")

    all_voices = load_voice_library(VOICES_FILE_PATH)
    if not all_voices:
        raise RuntimeError(f"ElevenLabs 音色库暂无可用音色：{VOICES_FILE_PATH}")
    minimax_voices = []
    if os.path.exists(MINIMAX_VOICES_FILE_PATH):
        minimax_voices = load_voice_library(MINIMAX_VOICES_FILE_PATH)
    if not minimax_voices:
        casting.logger.warning(
            f"⚠️ MiniMax 音色库不可用：{MINIMAX_VOICES_FILE_PATH}；"
            "儿童/少年角色将降级复用 ElevenLabs 音色"
        )
    minimax_voice_ids = {voice["voice_id"] for voice in minimax_voices}
    if casting._usage_tracker and task_id:
        try:
            casting._usage_tracker.set_context(task_id, "audio_producer")
        except Exception as exc:
            casting.logger.warning(
                f"⚠️ API 用量上下文设置失败，继续 ElevenLabs 选角：{exc}"
            )

    main_roles = parse_character_profiles(char_json_data)
    excluded_roles = [
        role["id"] for role in main_roles
        if str(role["id"]).strip().lower() in NON_VOICED_ROLE_IDS
    ]
    main_roles = [
        role for role in main_roles
        if str(role["id"]).strip().lower() not in NON_VOICED_ROLE_IDS
    ]
    if excluded_roles:
        casting.logger.info(
            "🔇 ElevenLabs 跳过无需配音角色：" + ", ".join(excluded_roles)
        )
    if not main_roles:
        raise RuntimeError("没有解析到主要角色")
    main_ids = {role["id"] for role in main_roles}
    npc_names = casting.extract_npc_list(plot_json_data, main_ids)
    npc_roles = build_npc_profiles(npc_names)
    actual_plot_ids = extract_plot_speaker_ids(plot_json_data)
    # speaker_id 大小写只影响字幕文件的文本表现，不应制造第二个角色。
    main_ids_by_key = {role_id.casefold(): role_id for role_id in main_ids}
    actual_plot_ids = {
        main_ids_by_key.get(role_id.casefold(), role_id)
        for role_id in actual_plot_ids
    }
    if actual_plot_ids:
        # speaker_id is authoritative.  A localized/legacy speaker_name such
        # as “暗衣人A” must not create a second AnYiRenA role when the beat's
        # canonical ID is CiKeShouLing.
        npc_roles = [
            role for role in npc_roles
            if role["id"] in actual_plot_ids
        ]
    roles = main_roles + npc_roles
    # extract_npc_list is name-based for historical compatibility.  Also scan
    # the canonical speaker_id field so a plot entry whose localized name is
    # missing or stale can never disappear from the final mapping.
    known_ids = {role["id"] for role in roles}
    plot_id_roles = extract_plot_speaker_profiles(plot_json_data, known_ids)
    if plot_id_roles:
        roles.extend(plot_id_roles)
        casting.logger.info(
            "🧩 根据剧情 speaker_id 补齐角色映射："
            + ", ".join(role["id"] for role in plot_id_roles)
        )
    role_ids = [role["id"] for role in roles]
    duplicate_ids = sorted(
        role_id for role_id, count in Counter(role_ids).items() if count > 1
    )
    duplicate_casefold = sorted(
        role_key for role_key, count in Counter(
            str(role_id).casefold() for role_id in role_ids
        ).items() if count > 1
    )
    if duplicate_ids or duplicate_casefold:
        raise RuntimeError(
            "ElevenLabs 选角角色 ID 冲突，拒绝生成可能覆盖的映射："
            + ", ".join(duplicate_ids or duplicate_casefold)
        )
    # 与 MiniMax 原流程一致：仅前两个不同 rank 层级提交 AI，
    # 但 characters 中其余角色仍保留为已建档角色，按 rank 分层分配。
    core_roles = select_core_roles(main_roles)
    output_dir = os.path.join(casting.OUTPUT_BASE_DIR, str(task_id))
    output_path = os.path.join(output_dir, OUTPUT_FILENAME)
    provider_path = os.path.join(output_dir, PROVIDER_FILENAME)
    voice_file_hashes = [file_digest(VOICES_FILE_PATH)]
    if os.path.isfile(MINIMAX_VOICES_FILE_PATH):
        voice_file_hashes.append(file_digest(MINIMAX_VOICES_FILE_PATH))
    step_digest = stable_digest(
        STEP_CACHE_VERSION,
        char_json_data,
        plot_json_data,
        casting._NAME_CONVERSION,
        voice_file_hashes,
        LANG_CONFIGS,
        MODEL_NAME,
        CANDIDATES_PER_ROLE,
        file_digest(__file__),
    )
    state = ResumeState(output_dir)
    required_ids = {role["id"] for role in roles}
    available_ids = {
        voice["voice_id"] for voice in [*all_voices, *minimax_voices]
    }
    mapping_valid = valid_json(
        output_path,
        lambda payload: isinstance(payload, dict) and all(
            isinstance(payload.get(lang_key), dict)
            and set(payload[lang_key]) == required_ids
            and all(value in available_ids for value in payload[lang_key].values())
            for lang_key, _ in LANG_CONFIGS
        ),
    )
    providers_valid = valid_json(
        provider_path,
        lambda payload: isinstance(payload, dict) and all(
            isinstance(payload.get(lang_key), dict)
            and set(payload[lang_key]) == required_ids
            and set(payload[lang_key].values()) <= {
                PROVIDER_ELEVENLABS, PROVIDER_MINIMAX,
            }
            for lang_key, _ in LANG_CONFIGS
        ),
    )
    cached_mapping = None
    cached_provider_map = None
    if mapping_valid and providers_valid:
        try:
            with open(output_path, "r", encoding="utf-8") as file_obj:
                cached_mapping = json.load(file_obj)
            with open(provider_path, "r", encoding="utf-8") as file_obj:
                cached_provider_map = json.load(file_obj)
            cache_errors = validate_cached_mapping(
                cached_mapping, cached_provider_map, roles,
                all_voices, minimax_voices,
            )
            if cache_errors:
                mapping_valid = False
                providers_valid = False
                casting.logger.warning(
                    "⚠️ 已有 ElevenLabs 映射未通过性别/年龄/provider 校验，"
                    f"将重新选角：{'; '.join(cache_errors[:8])}"
                    + (" …" if len(cache_errors) > 8 else "")
                )
        except (OSError, ValueError, TypeError) as exc:
            mapping_valid = False
            providers_valid = False
            casting.logger.warning(f"⚠️ 读取缓存选角映射失败，将重新选角：{exc}")
    if (
        state.step_matches("relay_casting", step_digest)
        and mapping_valid and providers_valid
    ):
        if cached_mapping is None:
            with open(output_path, "r", encoding="utf-8") as file_obj:
                cached_mapping = json.load(file_obj)
        casting.logger.info(
            "♻️ ElevenLabs Step 3 输入未变且映射/provider 完整，跳过模型选角"
        )
        # 即使本地命中断点缓存，也要补传 OSS，避免上次上传失败导致下游拿不到
        # 两个选角旁挂产物。
        try:
            mirror_outputs(
                task_id,
                [OUTPUT_FILENAME, PROVIDER_FILENAME],
                force=True,
                strict=False,
            )
        except Exception as exc:
            casting.logger.warning(f"⚠️ ElevenLabs 映射 OSS 镜像失败（缓存路径）: {exc}")
        return cached_mapping
    casting.logger.info(
        f"🧠 ElevenLabs 核心卡司：{len(core_roles)}/{len(main_roles)} 个角色进入 AI 选角"
    )
    # 画像增强只处理少量核心主角。大量 NPC 使用已有名称/身份资料做确定性
    # 分配，避免一次请求携带上百角色与台词上下文而稳定超时。
    profile_roles = core_roles
    if profile_roles:
        try:
            enriched_profiles = enrich_role_profiles(profile_roles, plot_json_data)
        except Exception as exc:
            casting.logger.warning(
                f"⚠️ 人物声音画像生成失败，保留原角色资料继续选角：{exc}"
            )
        else:
            enriched_by_id = {role["id"]: role for role in enriched_profiles}
            core_roles = [
                enriched_by_id.get(role["id"], role) for role in core_roles
            ]
            for index, role in enumerate(roles):
                if role["id"] in enriched_by_id:
                    roles[index] = enriched_by_id[role["id"]]

    combined_map = {}
    for lang_key, target_lang in LANG_CONFIGS:
        language_voices = [
            voice for voice in all_voices if is_lang_compatible(voice, target_lang)
        ]
        if not language_voices:
            language_voices = list(all_voices)
            casting.logger.warning(
                f"⚠️ ElevenLabs 库中没有标记支持{target_lang}的音色，"
                "降级使用全音色库继续分配"
            )
        # 不做“角色数 > 音色数”的硬中断；与 MiniMax 分配器一样，
        # 高 rank 先用独立音色，资源耗尽后自动均匀复用。
        combined_map[lang_key] = cast_language(
            roles,
            language_voices,
            target_lang,
            ai_roles=core_roles,
            minimax_voices=minimax_voices,
        )

    validate_final_mapping(
        combined_map, roles, all_voices, minimax_voices
    )
    os.makedirs(output_dir, exist_ok=True)
    atomic_write_json(output_path, combined_map)
    casting.logger.info(f"💾 ElevenLabs AI 音色映射已保存：{output_path}")

    # 音色映射本身保持 {语言: {角色: voice_id}} 不变，provider 走独立旁挂文件，
    # 让 step4 知道哪些台词必须走单句 MiniMax 合成。
    provider_map = {
        lang_key: {
            role_id: (
                PROVIDER_MINIMAX
                if voice_id in minimax_voice_ids
                else PROVIDER_ELEVENLABS
            )
            for role_id, voice_id in mapping.items()
        }
        for lang_key, mapping in combined_map.items()
    }
    atomic_write_json(provider_path, provider_map)
    minimax_count = sum(
        1 for mapping in provider_map.values()
        for provider in mapping.values()
        if provider == PROVIDER_MINIMAX
    )
    casting.logger.info(
        f"💾 音色 provider 映射已保存：{provider_path}"
        f"（MiniMax 单句合成角色 {minimax_count} 个）"
    )
    try:
        mirror_outputs(
            task_id,
            [OUTPUT_FILENAME, PROVIDER_FILENAME],
            force=True,
            strict=False,
        )
    except Exception as exc:
        # OSS 是旁路同步，不能因为镜像故障阻断本地选角产物。
        casting.logger.warning(f"⚠️ ElevenLabs 映射 OSS 镜像失败: {exc}")
    state.mark_step(
        "relay_casting", step_digest, status="success",
        artifacts=[output_path, provider_path],
    )
    return combined_map


if __name__ == "__main__":
    run_auto_casting(target_task_id="")
