#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
试听音色推荐（独立实现，供 api_gateway 同步调用）

放在 common/ 下，不依赖 services.audio_producer 包（api_gateway 容器只 COPY 了 common/ 和
自身，没有 audio_producer 代码）。仅做只读查询：读音色库 json + 查 characters/voice_mappings 表
+ 拼 OSS 试听 URL，不调 TTS、不生成。

规则：
- 只推荐中文音色（language 含 "中文（普通话）"）
- gender 精确匹配（兼容 male/female 与 男/女）
- age_category 归一到 5 档后同档匹配（不做相邻补全，保证试听真实接近）
- 排除该角色当前正在使用的 voice_id（从 voice_mappings[language][speaker_id] 读）
- display_name 取自音色库 json 手填字段；未填回退为 "{gender}·{age_category}"
- 试听音频 URL：timbre/{male|female}/{age英文目录}/{voice_id}.wav（与 {task_id} 目录同级）

speaker_id 匹配不依赖 pypinyin：
- 优先用 name_mappings 的 change_name 映射把原名/新名对齐到脱敏 speaker_id
- 未命中且角色名为纯 ASCII 时用无拼音的驼峰规范化兜底
- 中文名且不在 conversion 中时无法匹配（返回空推荐，日志告警）
"""

import os
import re
import json
import logging
from typing import Optional

from common.config import DATABASE_URL
from common.name_mapping_utils import get_source_to_change_conversion
from sqlalchemy import create_engine, text

try:
    from common import oss_utils
except ImportError:
    oss_utils = None

logger = logging.getLogger(__name__)

# 音色库 json：优先 common/ 下的副本（api 容器可读），降级 audio_producer 目录（worker 容器）
_COMMON_DIR = os.path.dirname(os.path.abspath(__file__))
_VOICES_CANDIDATES = [
    os.path.join(_COMMON_DIR, "minimax_voices.json"),
    os.path.join(_COMMON_DIR, "voice", "minimax_voices.json"),
    os.path.join(os.path.dirname(_COMMON_DIR), "services", "audio_producer", "voice", "minimax_voices.json"),
]

CHINESE_LANG_TAG = "中文（普通话）"

AGE_CN_TO_DIR = {
    "儿童": "child",
    "少年": "adolescent",
    "青年": "young_adult",
    "中年": "middle_aged",
    "老年": "elderly",
}


def _engine():
    return create_engine(DATABASE_URL)


def _platform_engine():
    """characters/name_mappings 已迁平台库(agent_platform);voice_mappings 仍在业务库"""
    from common.config import PLATFORM_DATABASE_URL
    return create_engine(PLATFORM_DATABASE_URL)


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------

def _age_weight(age_cat: Optional[str]) -> int:
    if not age_cat:
        return 30
    s = str(age_cat).lower().strip()
    if any(k in s for k in ["child", "儿童", "baby", "toddler", "infant", "kid"]):
        return 10
    if any(k in s for k in ["teen", "少年", "adolescent", "juvenile"]):
        return 20
    if any(k in s for k in ["youth", "young", "青年", "student", "college", "young adult", "adult"]):
        return 30
    if any(k in s for k in ["middle", "中年", "mid-life", "middle-aged"]):
        return 40
    if any(k in s for k in ["elderly", "old", "老年", "senior", "aged", "retired", "grand"]):
        return 50
    return 30


def _weight_to_cn(weight: int) -> str:
    return {10: "儿童", 20: "少年", 30: "青年", 40: "中年", 50: "老年"}.get(weight, "青年")


def _norm_gender(raw: Optional[str]) -> Optional[str]:
    """性别归一到 男/女；兼容 male/female。未知返回 None。"""
    if not raw:
        return None
    s = str(raw).strip().lower()
    if s in ["男", "male", "m", "man", "boy"]:
        return "男"
    if s in ["女", "female", "f", "woman", "girl"]:
        return "女"
    if str(raw).strip() in ["男", "女"]:
        return str(raw).strip()
    return None


def _gender_to_dir(gender_cn: str) -> str:
    return "female" if gender_cn == "女" else "male"


def _ascii_pascal(name: str) -> Optional[str]:
    """纯 ASCII 名的驼峰规范化（无拼音依赖）；含中文则返回 None。"""
    if re.search(r"[一-龥]", name):
        return None
    s = re.sub(r"[''']s\b", "", name, flags=re.IGNORECASE)
    s = re.sub(r"[''']", "", s)
    s = re.sub(r"[^a-zA-Z0-9]", " ", s)
    words = s.split()
    return "".join(w.capitalize() for w in words) if words else None


# ---------------------------------------------------------------------------
# 数据读取
# ---------------------------------------------------------------------------

def _load_voice_library() -> list:
    for path in _VOICES_CANDIDATES:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data.get("voices_list", []) or []
                if isinstance(data, list):
                    return data
            except Exception as e:
                logger.error(f"[voice_audition] 读取音色库失败 {path}: {e}")
    logger.error(f"[voice_audition] 未找到音色库 json，候选路径: {_VOICES_CANDIDATES}")
    return []


def _fetch_name_conversion(task_id: str) -> dict:
    """读取“原名/新名 → 脱敏 speaker_id”映射；无则空 dict。"""
    try:
        with _platform_engine().connect() as conn:
            row = conn.execute(
                text("SELECT data FROM name_mappings WHERE task_id = :tid LIMIT 1"),
                {"tid": task_id}
            ).fetchone()
    except Exception as e:
        logger.warning(f"[voice_audition] 读取 name_mappings 失败: {e}")
        return {}
    if not row or not row[0]:
        return {}
    data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
    if not isinstance(data, dict):
        return {}
    return get_source_to_change_conversion(data)


def _fetch_character_gender_age(task_id: str, speaker_id: str) -> Optional[dict]:
    """
    从 characters 表查指定 speaker_id 的 gender / age_category。
    characters.data = {char_key(full_name): {..., whole_body_variants:[{gender, age_category}]}}。
    用 name_mappings 的 change_name 映射把 char_key → speaker_id 后与入参比对；
    未命中用纯 ASCII 驼峰兜底。取 age_weight 最接近青年(30) 的 variant。

    返回 {"gender": 男/女/None, "age_category": str} 或 None（找不到）。
    """
    with _platform_engine().connect() as conn:
        row = conn.execute(
            text("SELECT data FROM characters WHERE task_id = :tid LIMIT 1"),
            {"tid": task_id}
        ).fetchone()
    if not row:
        return None
    all_data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
    if not isinstance(all_data, dict):
        return None

    conversion = _fetch_name_conversion(task_id)
    target = str(speaker_id).strip()

    for char_key, data in all_data.items():
        if not isinstance(data, dict):
            continue
        # char_key → speaker_id：优先 conversion，其次纯 ASCII 驼峰
        cid = conversion.get(char_key) or _ascii_pascal(str(char_key).strip())
        if cid != target:
            continue

        variants = data.get("whole_body_variants", []) or []
        if not variants:
            return {"gender": None, "age_category": "未知"}
        best = min(variants, key=lambda v: abs(_age_weight(v.get("age_category")) - 30))
        return {
            "gender": _norm_gender(best.get("gender")),
            "age_category": best.get("age_category", "未知"),
        }
    return None


def _current_voice_id(task_id: str, speaker_id: str, language: str) -> Optional[str]:
    """从 voice_mappings 读该角色当前在用的 voice_id（三语结构 data[language][speaker_id]）。"""
    with _engine().connect() as conn:
        row = conn.execute(
            text("SELECT data FROM voice_mappings WHERE task_id = :tid LIMIT 1"),
            {"tid": task_id}
        ).fetchone()
    if not row:
        return None
    data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
    if not isinstance(data, dict):
        return None
    lang_keys = {"zh-CN", "en-US", "ja-JP"}
    is_multilang = bool(lang_keys & set(data.keys()))
    sub = data.get(language, {}) if is_multilang else data
    if not isinstance(sub, dict):
        return None
    if speaker_id in sub:
        return sub[speaker_id]
    lower_map = {str(k).lower(): v for k, v in sub.items()}
    return lower_map.get(str(speaker_id).lower())


def _audition_url(gender_cn: str, age_cat: str, voice_id: str) -> str:
    """拼本地试听音频 URL：storage/timbre/{gender}/{age}/{voice_id}.wav。"""
    gender_dir = _gender_to_dir(gender_cn)
    age_dir = AGE_CN_TO_DIR.get(age_cat) or AGE_CN_TO_DIR.get(_weight_to_cn(_age_weight(age_cat)), "young_adult")
    key = f"storage/timbre/{gender_dir}/{age_dir}/{voice_id}.wav"
    if not oss_utils:
        return ""
    try:
        # 试听样例必须真实存在于共享本地 storage，不能返回一个必然 404
        # 的“占位 URL”，否则前端只能显示“音色试听失败”。
        if not oss_utils.local_path_for_key(key).is_file():
            return ""
        return oss_utils.oss_public_url(key)
    except (OSError, ValueError):
        return ""


# ---------------------------------------------------------------------------
# 公开接口
# ---------------------------------------------------------------------------

def recommend_voices(task_id: str, speaker_id: str, language: str = "zh-CN") -> dict:
    """
    为指定角色推荐可试听的中文音色列表。

    Returns:
        {task_id, speaker_id, gender, age_category, current_voice_id, total, voices:[...], errors}
    """
    profile = _fetch_character_gender_age(task_id, speaker_id)
    if profile is None:
        return {
            "task_id": task_id, "speaker_id": speaker_id,
            "gender": None, "age_category": None, "current_voice_id": None,
            "total": 0, "voices": [], "errors": [f"未找到角色 speaker_id={speaker_id}"]
        }

    char_gender = profile["gender"]
    char_age = profile["age_category"]
    char_age_w = _age_weight(char_age)

    current_vid = _current_voice_id(task_id, speaker_id, language)

    library = _load_voice_library()
    voices = []
    for v in library:
        if CHINESE_LANG_TAG not in (v.get("language") or []):
            continue
        v_gender = _norm_gender(v.get("gender"))
        if char_gender and v_gender != char_gender:
            continue
        if _age_weight(v.get("age_category")) != char_age_w:
            continue
        vid = v.get("voice_id")
        if not vid or vid == current_vid:
            continue

        v_gender_cn = v_gender or "男"
        v_age_cat = v.get("age_category", "")
        display_name = (v.get("display_name") or "").strip() or f"{v_gender_cn}·{v_age_cat}"
        voices.append({
            "voice_id": vid,
            "display_name": display_name,
            "gender": v_gender_cn,
            "age_category": v_age_cat,
            "tags": v.get("tags", []),
            "description": v.get("description", ""),
            "audition_url": _audition_url(v_gender_cn, v_age_cat, vid),
        })

    return {
        "task_id": task_id,
        "speaker_id": speaker_id,
        "gender": char_gender,
        "age_category": char_age,
        "current_voice_id": current_vid,
        "total": len(voices),
        "voices": voices,
        "errors": [],
    }
