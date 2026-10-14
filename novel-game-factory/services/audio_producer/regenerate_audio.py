#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audio_producer 音频重生成接口

参数规范：
- task_id + node_id + material_id（= 字幕文件名，如 scene_alley-1.txt 或 UnknownScene-2.txt）
- node_id 格式：OSS 格式（chapter_1_chapter_1、chapter_2_7a80a1c2）
- 支持 new_content 修改 plots 表中的内容
- new_content=None 时只重新执行步骤5（MiniMax JSON → MP3）
- new_content 不为空时：更新 plots → 重新生成字幕 → JSON → MP3（全链）

存储：字幕文件和 MP3 最终存储在 OSS，本地仅作 MiniMax JSON 中转。
"""

import os
import sys
import re
import json
import logging
import tempfile
from pathlib import Path
from typing import Optional

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from common.config import DATABASE_URL
from common.node_utils import db_to_oss_node_id, parse_oss_node_id
from common import regen_version
from sqlalchemy import create_engine, text

try:
    from common.usage_tracker import tracker as _usage_tracker
except Exception:
    _usage_tracker = None

# OSS 公共模块
try:
    from common import oss_utils
except ImportError:
    oss_utils = None

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# OSS 路径构建
# ---------------------------------------------------------------------------

def _oss_key_voice(task_id: str, node_id: str, filename: str, language: str = "zh-CN") -> str:
    """OSS voices 路径: {task_id}/{node_id}/assets/voices/{language}/{filename}"""
    return f"{task_id}/{node_id}/assets/voices/{language}/{filename}"


def _oss_key_subtitle(task_id: str, node_id: str, filename: str, language: str = "zh-CN") -> str:
    """OSS subtitles 路径: {task_id}/{node_id}/assets/subtitles/{language}/{filename}（与 voices 同级）"""
    return f"{task_id}/{node_id}/assets/subtitles/{language}/{filename}"


def _standard_output_subtitle_key(
    task_id: str,
    node_id: str,
    filename: str,
    language: str = "zh-CN",
) -> str:
    """热插拔契约中的标准字幕产物 key。"""
    db_node_id, _ = parse_oss_node_id(node_id)
    return (
        f"storage/outputs/{task_id}/{db_node_id}/audios/"
        f"{language}-subtitle/{filename}"
    )


def _oss_url(key: str) -> str:
    return oss_utils.oss_public_url(key) if oss_utils else key


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _engine():
    return create_engine(DATABASE_URL)


def _platform_engine():
    """plots 表已迁平台库(agent_platform);voice_mappings/material_regen_sessions 仍在业务库"""
    from common.config import PLATFORM_DATABASE_URL
    return create_engine(PLATFORM_DATABASE_URL)


def _fetch_plots_data(task_id: str) -> dict:
    """返回 {node_id: [beat, ...], ...}"""
    with _platform_engine().connect() as conn:
        row = conn.execute(
            text("SELECT data FROM plots WHERE task_id = :tid LIMIT 1"),
            {"tid": task_id}
        ).fetchone()
    if not row:
        return {}
    return row[0] if isinstance(row[0], dict) else json.loads(row[0])


def _update_beat_content(task_id: str, node_id: str, beat_id: str, new_content: str) -> bool:
    """更新 plots 表中指定 beat 的 content 字段，返回是否找到"""
    with _platform_engine().begin() as conn:
        row = conn.execute(
            text("SELECT data FROM plots WHERE task_id = :tid LIMIT 1"),
            {"tid": task_id}
        ).fetchone()
        if not row:
            return False
        data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        node_beats = data.get(node_id, [])
        updated = False
        for beat in node_beats:
            if beat.get("beat_id") == beat_id:
                beat["content"] = new_content
                updated = True
                break
        if updated:
            data[node_id] = node_beats
            conn.execute(
                text("UPDATE plots SET data = :data WHERE task_id = :tid"),
                {"data": json.dumps(data, ensure_ascii=False), "tid": task_id}
            )
        return updated


def _find_beat_by_material_id(task_id: str, material_id: str) -> Optional[dict]:
    """
    根据 material_id（不含后缀）找到对应的 beat 记录。
    格式：{scene_id}-{beat_id} 或 UnknownScene-{beat_id}
    返回 {"node_id": ..., "beat": ...} 或 None
    """
    name = material_id.replace(".txt", "")  # 兼容带后缀的旧调用
    if name.startswith("UnknownScene-"):
        beat_id = name.replace("UnknownScene-", "")
        scene_id = None
    elif "-" in name:
        parts = name.rsplit("-", 1)
        scene_id = parts[0]
        beat_id = parts[1]
    else:
        return None

    plots_data = _fetch_plots_data(task_id)
    for node_id, beats in plots_data.items():
        for beat in beats:
            if str(beat.get("beat_id")) == str(beat_id):
                if beat.get("scene_id") == scene_id or (scene_id is None and beat.get("scene_id") is None):
                    return {"node_id": node_id, "beat": beat}
    return None


def _load_voice_map(task_id: str, language: str = "zh-CN") -> dict:
    """
    加载指定语言的音色映射表（优先从 DB 读取，降级到本地文件）。

    voice_mappings.data 为三语结构：{"zh-CN": {speaker: voice_id}, "en-US": {...}, "ja-JP": {...}}，
    本函数返回展平后的 {speaker_lower: voice_id}（含默认兜底）。兼容旧的扁平结构。
    """
    default_map = {
        "pangbai": "male-qn-qingse",
        "narrator": "male-qn-qingse",
        "default": "male-qn-qingse"
    }
    lang_keys = {"zh-CN", "en-US", "ja-JP"}

    def _flatten(data: dict) -> dict:
        """从三语结构取出 language 子表；兼容旧扁平结构。key 统一小写。"""
        if not isinstance(data, dict):
            return {}
        is_multilang = bool(lang_keys & set(data.keys()))
        sub = data.get(language, {}) if is_multilang else data
        if not isinstance(sub, dict):
            sub = {}
        return {str(k).lower(): v for k, v in sub.items()}

    # 优先从 voice_mappings 表读取
    try:
        with _engine().connect() as conn:
            row = conn.execute(
                text("SELECT data FROM voice_mappings WHERE task_id = :tid LIMIT 1"),
                {"tid": task_id}
            ).fetchone()
        if row:
            data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
            sub = _flatten(data)
            if sub:
                result = {**default_map, **sub}
                logger.info(f"从 DB 加载 voice_map[{language}]: {len(sub)} 个映射")
                return result
    except Exception as e:
        logger.warning(f"从 DB 读取 voice_mappings 失败: {e}，尝试读取本地文件")

    # 降级：从本地文件读取
    from common.config import OUTPUTS_PATH
    voice_map_path = Path(OUTPUTS_PATH) / task_id / "minimax_voice_mappings.json"

    if not voice_map_path.exists():
        logger.warning(f"minimax_voice_mappings.json 不存在: {voice_map_path}，使用默认音色")
        return default_map

    try:
        with open(voice_map_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        sub = _flatten(data)
        if sub:
            result = {**default_map, **sub}
            logger.info(f"从本地文件加载 voice_map[{language}]: {len(sub)} 个映射")
            return result
        return default_map
    except Exception as e:
        logger.error(f"读取 minimax_voice_mappings.json 失败: {e}")
        return default_map


# ---------------------------------------------------------------------------
# MiniMax API
# ---------------------------------------------------------------------------

MINIMAX_URL = os.getenv(
    "MINIMAX_T2A_URL", "https://api.minimaxi.com/v1/t2a_v2"
)
MINIMAX_EMOTION_MAP = {
    "sad": "sad", "happy": "happy", "excited": "happy",
    "angry": "angry", "fearful": "fearful", "surprised": "surprised",
    "disgusted": "disgusted", "neutral": "calm", "calm": "calm",
    "tired": "whisper", "default": "calm"
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


def _call_minimax(payload: dict) -> tuple:
    """调用 MiniMax TTS API，返回 (ok, audio_bytes, error)"""
    import os as _os
    import requests
    api_key = _os.getenv("MINIMAX_API_KEY")
    if not api_key:
        return False, None, "MINIMAX_API_KEY 未设置"

    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    full_payload = {
        "model": "speech-2.8-hd",
        "text": payload["text"],
        "stream": False,
        "voice_setting": {
            "voice_id": payload["voice_id"],
            "speed": payload["speed"],
            "vol": payload["vol"],
            "pitch": 0,
            "emotion": payload["emotion"],
            "text_normalization": True
        },
        "audio_setting": {"sample_rate": 44100, "format": "wav", "channel": 1},
        "subtitle_enable": True
    }

    try:
        resp = requests.post(MINIMAX_URL, json=full_payload, headers=headers, timeout=60)
        if resp.status_code == 200:
            rj = resp.json()
            if rj.get("base_resp", {}).get("status_code") == 0:
                hex_audio = rj.get("data", {}).get("audio")
                if hex_audio:
                    return True, bytes.fromhex(hex_audio), None
                return False, None, "No audio data"
            msg = rj.get("base_resp", {}).get("status_msg", "Unknown")
            return False, None, f"Business Error: {msg}"
        return False, None, f"HTTP {resp.status_code}: {resp.text[:100]}"
    except Exception as e:
        return False, None, str(e)


def _count_tts_chars(text: str) -> int:
    count = 0
    for ch in text:
        cp = ord(ch)
        if (0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or
                0x20000 <= cp <= 0x2A6DF or 0x2A700 <= cp <= 0x2CEAF or
                0xF900 <= cp <= 0xFAFF):
            count += 2
        else:
            count += 1
    return count


# ---------------------------------------------------------------------------
# 字幕文件读写（OSS）
# ---------------------------------------------------------------------------

def _read_subtitle_from_oss(task_id: str, node_id: str, material_id: str, language: str = "zh-CN") -> Optional[str]:
    """读取字幕；优先游戏运行时路径，回落到标准 outputs 产物路径。"""
    if oss_utils is None:
        return None
    runtime_key = _oss_key_subtitle(task_id, node_id, material_id, language=language)
    data = oss_utils.oss_read(runtime_key)
    if data is not None:
        return data.decode("utf-8")
    try:
        standard_key = _standard_output_subtitle_key(
            task_id, node_id, material_id, language=language
        )
        data = oss_utils.oss_read(standard_key)
        if data is not None:
            return data.decode("utf-8")
    except Exception as e:
        logger.warning(
            f"[audio_regen] 标准字幕路径解析/读取失败: task={task_id} node={node_id}: {e}"
        )
    return None


def _write_subtitle_to_oss(task_id: str, node_id: str, material_id: str, content: str, language: str = "zh-CN") -> bool:
    """写运行时字幕，并同步标准 outputs 字幕产物。"""
    if oss_utils is None:
        return False
    data = content.encode("utf-8")
    runtime_key = _oss_key_subtitle(task_id, node_id, material_id, language=language)
    runtime_ok = oss_utils.oss_write(
        runtime_key, data, content_type="text/plain; charset=utf-8"
    )
    standard_ok = False
    try:
        standard_key = _standard_output_subtitle_key(
            task_id, node_id, material_id, language=language
        )
        standard_ok = oss_utils.oss_write(
            standard_key, data, content_type="text/plain; charset=utf-8"
        )
        if not standard_ok:
            logger.warning(f"[audio_regen] 标准字幕产物同步失败: {standard_key}")
    except Exception as e:
        logger.warning(
            f"[audio_regen] 标准字幕路径解析/同步失败: task={task_id} node={node_id}: {e}"
        )
    return runtime_ok and standard_ok


def _parse_line_info(text: str):
    """
    解析字幕文件内容: {speaker_id}_{组合}_{emotion}：{content}

    返回 (is_valid_format, full_prefix, role, emotion, content)，与主流程
    generate_json_from_txt.parse_line_info 语义对齐：

    - 前缀支持两种格式：Name_1_emotion（旧）和 Name_1-1-1_emotion（新），
      正则统一用 ^(.+?)_[\\d][\\d\\-]*_(.+?)$ 覆盖两者。
    - is_valid_format=True 才是"会生成音频"的角色台词行；
      纯 speaker 名（如 "Liang" 无 _数字_情绪 后缀）与旁白 is_valid_format=False，
      主流程会跳过、不产音频。换音色按 speaker 匹配台词时必须用它过滤，
      否则会给没有音频的行凭空生成音频。
    """
    text = text.strip()
    if "：" in text:
        parts = text.split("：", 1)
        full_prefix = parts[0].strip()
        content = parts[1]
    else:
        return False, "PangBai", "pangbai", "neutral", text

    if full_prefix.lower() in ["pangbai", "narrator"]:
        return False, full_prefix, "pangbai", "neutral", content

    match = re.match(r"^(.+?)_[\d][\d\-]*_(.+?)$", full_prefix, re.IGNORECASE)
    if match:
        return True, full_prefix, match.group(1).lower(), match.group(2).lower(), content
    # 前缀无 _数字_情绪 结构（如纯 "Liang"）：主流程视为非法格式，不生成音频
    return False, full_prefix, full_prefix.lower(), "neutral", content


_NARRATOR_SPEAKER_IDS = {"pangbai", "narrator", "nb", "旁白"}


def _load_master_audio_index(task_id: str) -> dict:
    """从标准 master JSON 建立 ``(db_node_id, subtitle_id) -> beat`` 索引。"""
    from common.config import OUTPUTS_PATH

    master_path = Path(OUTPUTS_PATH) / task_id / f"{task_id}.json"
    if not master_path.is_file():
        try:
            from common.output_mirror import fetch_output_file

            fetched = fetch_output_file(task_id, f"{task_id}.json")
            if fetched is not None:
                master_path = fetched
        except Exception as e:
            logger.warning(f"[audio_regen] 标准 master JSON 拉取失败: task={task_id}: {e}")
    raw = None
    if master_path.is_file():
        try:
            raw = json.loads(master_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[audio_regen] 标准 master JSON 读取失败: {master_path}: {e}")
    if not isinstance(raw, dict):
        try:
            raw = _fetch_plots_data(task_id)
        except Exception as e:
            logger.warning(f"[audio_regen] plots 回落读取失败: task={task_id}: {e}")
            raw = {}
    if not isinstance(raw, dict):
        return {}

    indexed = {}
    for db_node_id, beats in raw.items():
        if not isinstance(beats, list):
            continue
        for beat in beats:
            if not isinstance(beat, dict):
                continue
            subtitle_id = str(beat.get("subtitle_id") or "").strip()
            if not subtitle_id and beat.get("beat_id") is not None:
                scene_id = beat.get("scene_id") or "UnknownScene"
                subtitle_id = f"{scene_id}-{beat.get('beat_id')}"
            if subtitle_id:
                indexed[(str(db_node_id), subtitle_id)] = beat
    return indexed


def _resolve_subtitle_line_info(
    task_id: str,
    node_id: str,
    material_id: str,
    subtitle_text: str,
    master_index: Optional[dict] = None,
):
    """解析字幕角色；旧前缀不完整时回落 master JSON 的 speaker_id。

    二组只要遵守原 master/subtitle 契约即可，不必额外把字幕改造成一组私有的
    ``speaker_组合_emotion：正文`` 格式。
    """
    parsed = _parse_line_info(subtitle_text)
    if parsed[0]:
        return parsed

    db_node_id, _ = parse_oss_node_id(node_id)
    index = master_index if master_index is not None else _load_master_audio_index(task_id)
    beat = index.get((db_node_id, material_id))
    if beat is None:
        # 兼容调用方传入的 node_id 与 master 节点格式不一致：subtitle_id 全局唯一时可回落。
        matches = [
            candidate
            for (candidate_node, subtitle_id), candidate in index.items()
            if subtitle_id == material_id
        ]
        if len(matches) == 1:
            beat = matches[0]
    if beat is None:
        try:
            beat_info = _find_beat_by_material_id(task_id, material_id)
            beat = beat_info.get("beat") if beat_info else None
        except Exception:
            beat = None

    speaker_id = str((beat or {}).get("speaker_id") or "").strip()
    if not speaker_id:
        return parsed
    role = speaker_id.lower()
    if role in _NARRATOR_SPEAKER_IDS:
        return False, speaker_id, role, "neutral", parsed[4]

    emotion = str((beat or {}).get("emotion") or parsed[3] or "neutral").lower()
    return True, speaker_id, role, emotion, parsed[4]


def _strip_option_prefix(content: str) -> str:
    """
    分支选项首条 beat 的内容格式为「选项文案||实际台词」，选项文案只用于游戏按钮，
    配音只念 || 之后的部分。与 generate_json_from_txt.strip_option_prefix 对齐。
    """
    if "||" not in content:
        return content
    content = content.split("||", 1)[1].strip()
    if content.endswith("」") and not content.startswith("「"):
        content = "「" + content
    return content


def _build_minimax_payload(
    subtitle_text: str,
    task_id: str,
    language: str = "zh-CN",
    override_voice_id: Optional[str] = None,
    line_info=None,
) -> dict:
    """
    从字幕文本构建 MiniMax API payload。

    音色选择优先级：override_voice_id（角色级换音色时直接指定）> voice_map[role] > default。
    """
    voice_map = _load_voice_map(task_id, language=language)
    voice_id = voice_map.get("default", "male-qn-qingse")

    if not subtitle_text:
        raise ValueError("字幕内容为空")

    _, _, role, emotion, content = line_info or _parse_line_info(subtitle_text)
    content = _strip_option_prefix(content)

    # 旁白检测（对齐 generate_json_from_txt.py 的逻辑）
    if role.lower() in ["pangbai", "narrator"]:
        raise ValueError("旁白不生成音频，跳过")

    if override_voice_id:
        voice_id = override_voice_id
    else:
        voice_id = voice_map.get(role, voice_map.get("default", "male-qn-qingse"))
    style = EMOTION_STYLES.get(emotion, EMOTION_STYLES["default"])
    minimax_emotion = MINIMAX_EMOTION_MAP.get(emotion, "calm")

    logger.info(f"[audio_regen] TTS 角色: {role} → 音色: {voice_id} | 情绪: {minimax_emotion}")

    return {
        "text": content,
        "voice_id": voice_id,
        "speed": style["speed"],
        "vol": style["vol"],
        "emotion": minimax_emotion
    }


# ---------------------------------------------------------------------------
# 批量重生成
# ---------------------------------------------------------------------------

def _regenerate_node_audios(task_id: str, db_node_id: str, oss_node_id: str, force: bool) -> dict:
    """
    批量重生成某个节点下的所有角色音频（过滤旁白）。
    通过 OSS 列出该节点下已有的字幕文件，过滤旁白后逐一重生成。
    """
    # 列出 OSS 上该节点的所有字幕文件
    prefix = f"{task_id}/{oss_node_id}/assets/voices/"
    try:
        txt_files = [
            os.path.basename(k)
            for k in oss_utils.oss_list_keys(prefix)
            if k.endswith(".txt")
        ]
    except Exception as e:
        return {
            "total": 0, "success": 0, "failed": 0,
            "items": [], "errors": [f"列出 OSS 字幕文件失败: {e}"]
        }

    if not txt_files:
        return {
            "total": 0, "success": 0, "failed": 0,
            "items": [], "errors": [f"节点 {oss_node_id} 无字幕文件"]
        }

    # 过滤旁白：读取文件内容判断 speaker
    character_files = []
    for fname in txt_files:
        content = _read_subtitle_from_oss(task_id, oss_node_id, fname)
        if not content:
            continue
        is_valid, _, role, _, _ = _parse_line_info(content)
        if is_valid and role.lower() not in ["pangbai", "narrator"]:
            character_files.append(fname)

    if not character_files:
        logger.info(f"[audio_regen] 节点 {oss_node_id} 无角色台词，全部为旁白")
        return {"total": 0, "success": 0, "failed": 0, "items": [], "errors": []}

    logger.info(f"[audio_regen] 节点 {oss_node_id} 共 {len(txt_files)} 个字幕文件，{len(character_files)} 个角色台词")

    total = len(character_files)
    success = 0
    failed = 0
    items = []
    errors = []

    for fname in character_files:
        try:
            result = _run_single_audio(task_id, oss_node_id, fname, force)
            status = result["status"]
            items.append({
                "material_id": fname,
                "material_subtype": "audio",
                "status": status,
                "path": result.get("path"),
                "error": result.get("error"),
            })
            if status == "success":
                success += 1
            elif status == "failed":
                failed += 1
                if result.get("error"):
                    errors.append(f"{fname}: {result['error']}")
        except Exception as e:
            failed += 1
            errors.append(f"{fname}: {str(e)}")
            items.append({
                "material_id": fname,
                "material_subtype": "audio",
                "status": "failed",
                "path": None,
                "error": str(e),
            })

    return {"total": total, "success": success, "failed": failed, "items": items, "errors": errors}


# ---------------------------------------------------------------------------
# 单条重生成
# ---------------------------------------------------------------------------

def _run_single_audio(task_id: str, node_id: str, material_id: str, force: bool, session_no: int,
                      language: str = "zh-CN", override_voice_id: Optional[str] = None) -> dict:
    """
    执行单条音频的重生成。material_id 不含后缀。
    字幕文件从 OSS subtitles/{language}/ 读取 → 生成 JSON → 调用 MiniMax → WAV 写入 regenerate 目录。
    override_voice_id 非空时强制用该音色（角色级换音色）。
    """
    if oss_utils is None:
        return {"status": "failed", "path": None, "error": "oss_utils 未安装"}

    txt_filename = f"{material_id}.txt"
    audio_filename = f"{material_id}.wav"
    oss_audio_key = _oss_key_voice(task_id, node_id, audio_filename, language=language)

    # 检查 OSS 是否存在（验证路径正确性）
    exists = oss_utils.oss_exists(oss_audio_key)
    size_ok = exists and oss_utils.oss_get_size(oss_audio_key) > 0

    if not exists:
        logger.warning(f"[audio_regen] OSS 文件不存在，将作为首次生成: {oss_audio_key}")
    else:
        logger.info(f"[audio_regen] OSS 文件已存在，准备重生成: {oss_audio_key}")

    # 跳过检查
    if not force and size_ok:
        return {"status": "skipped", "path": _oss_url(oss_audio_key), "error": None}

    # Step 1: 从 OSS 读取字幕文件
    subtitle_text = _read_subtitle_from_oss(task_id, node_id, txt_filename, language=language)
    if subtitle_text is None:
        return {"status": "failed", "path": None, "error": f"字幕文件不存在: {language}/{txt_filename}"}

    # Step 2: 构建 MiniMax payload
    try:
        line_info = _resolve_subtitle_line_info(
            task_id, node_id, material_id, subtitle_text
        )
        payload = _build_minimax_payload(
            subtitle_text,
            task_id,
            language=language,
            override_voice_id=override_voice_id,
            line_info=line_info,
        )
    except Exception as e:
        return {"status": "failed", "path": None, "error": f"构建 MiniMax payload 失败: {e}"}

    used_voice_id = payload.get("voice_id")

    # Step 3: 调用 MiniMax TTS
    ok, audio_bytes, err = _call_minimax(payload)
    if not ok:
        return {"status": "failed", "path": None, "error": f"MiniMax API 失败: {err}"}
    if _usage_tracker:
        _usage_tracker.add_tts(
            _count_tts_chars(payload.get("text", "")),
            "speech-2.8-hd",
            provider="minimax",
        )

    # Step 4: 上传 WAV 到 regenerate 目录，不覆盖主路径
    regen_key = regen_version.regen_oss_key(task_id, session_no, node_id, "voices", audio_filename, language=language)
    ok2 = oss_utils.oss_write(regen_key, audio_bytes, content_type="audio/wav")
    if not ok2:
        return {"status": "failed", "path": None, "error": "上传 WAV 到 OSS 失败"}

    # 写到本地 audios/{language}-audio/ 目录（最新生成默认激活，直接覆盖本地）
    try:
        from common.config import OUTPUTS_PATH
        db_node_id, _ = parse_oss_node_id(node_id)
        local_audio = Path(OUTPUTS_PATH) / task_id / db_node_id / "audios" / f"{language}-audio" / audio_filename
        local_audio.parent.mkdir(parents=True, exist_ok=True)
        local_audio.write_bytes(audio_bytes)
        logger.info(f"[audio_regen] 已写入本地音频: {local_audio}")
    except Exception as e:
        logger.warning(f"[audio_regen] 写入本地音频失败（不影响主流程）: {e}")

    return {
        "status": "success",
        "path": _oss_url(regen_key),
        "voice_id": used_voice_id,
        "action": "regenerate" if exists else "generate",
        "error": None
    }


# ---------------------------------------------------------------------------
# 公开接口
# ---------------------------------------------------------------------------

def regenerate_audio(
    task_id: str,
    node_id: str,
    material_id: Optional[str] = None,
    new_content: Optional[str] = None,
    force: bool = True,
    session_no: Optional[int] = None,
    user_id: Optional[str] = None,
    user_name: Optional[str] = None,
    language: str = "zh-CN",
    override_voice_id: Optional[str] = None,
) -> dict:
    """
    重生成 TTS 音频（单条）。

    Args:
        task_id: 任务 ID
        node_id: OSS 格式的 node_id（如 chapter_1_chapter_1、chapter_2_7a80a1c2）
        material_id: 字幕文件名，如 scene_alley-1.txt；None=该节点下所有角色音频
        new_content: 新内容（仅单条时生效，先更新 plots 表，再重新生成字幕文件）
        force: True=强制覆盖已有音频
        language: 语言（zh-CN/en-US/ja-JP）
        override_voice_id: 强制使用的音色 ID（角色级换音色时传入）；None=按 voice_mappings 选音色

    Returns:
        {"total": int, "success": int, "failed": int, "items": [...], "errors": [...]}
    """
    if oss_utils is None:
        return {
            "total": 0, "success": 0, "failed": 1,
            "items": [], "errors": ["oss_utils 未安装或导入失败"]
        }

    if _usage_tracker:
        _usage_tracker.set_context(task_id, "audio_producer")
    try:
        oss_utils.init_oss_client()
    except Exception as e:
        return {
            "total": 0, "success": 0, "failed": 1,
            "items": [], "errors": [f"OSS 客户端初始化失败: {e}"]
        }

    # 解析 node_id：db_node_id 用于查 DB，oss_node_id 用于 OSS 路径
    db_node_id, oss_node_id = parse_oss_node_id(node_id)

    # 批量模式暂不开放（material_id=None 时重生成该节点所有角色音频）
    # if material_id is None:
    #     result = _regenerate_node_audios(task_id, db_node_id, oss_node_id, force)
    #     if _usage_tracker:
    #         _usage_tracker.flush()
    #     return result

    if material_id is None:
        return {
            "total": 0, "success": 0, "failed": 1,
            "items": [], "errors": ["material_id 不能为空，批量重生成暂不开放"]
        }

    # 统一去掉后缀
    material_id = material_id.replace(".txt", "")

    # 单条模式
    # 解析 beat 信息（用 db_node_id 查 plots 表）
    beat_info = _find_beat_by_material_id(task_id, material_id)
    if not beat_info:
        return {
            "total": 0, "success": 0, "failed": 1,
            "items": [], "errors": [f"未找到 material_id={material_id} 对应的 beat 记录"]
        }

    resolved_db_node_id = beat_info["node_id"]
    beat = beat_info["beat"]
    beat_id = beat.get("beat_id", "")

    # 若提供新内容，先备份原始 .txt，再更新 plots 表和写新字幕文件到 OSS
    if new_content:
        # 读取原始字幕内容（用于备份）
        original_subtitle = _read_subtitle_from_oss(task_id, oss_node_id, f"{material_id}.txt", language=language)

        updated = _update_beat_content(task_id, resolved_db_node_id, beat_id, new_content)
        if not updated:
            return {
                "total": 0, "success": 0, "failed": 1,
                "items": [], "errors": [f"更新 plots 表失败: node={resolved_db_node_id}, beat_id={beat_id}"]
            }
        # 构建新字幕内容并写入 OSS
        emotion = beat.get("emotion", "neutral")
        speaker = beat.get("speaker_id", "pangbai")
        full_prefix = f"{speaker}_1_{emotion}"
        subtitle_text = f"{full_prefix}：{new_content}"
        ok = _write_subtitle_to_oss(task_id, oss_node_id, f"{material_id}.txt", subtitle_text, language=language)
        if not ok:
            return {
                "total": 0, "success": 0, "failed": 1,
                "items": [], "errors": [f"写入字幕文件到 OSS 失败: {oss_node_id}/subtitles/{language}/{material_id}.txt"]
            }
        logger.info(f"[audio_regen] 已更新字幕文件: {oss_node_id}/subtitles/{language}/{material_id}.txt")
        # 同步写到本地 audios/{language}-subtitle/ 目录（最新生成默认激活，直接覆盖本地）
        try:
            from common.config import OUTPUTS_PATH
            db_node_id_sub, _ = parse_oss_node_id(oss_node_id)
            local_txt = Path(OUTPUTS_PATH) / task_id / db_node_id_sub / "audios" / f"{language}-subtitle" / f"{material_id}.txt"
            local_txt.parent.mkdir(parents=True, exist_ok=True)
            local_txt.write_text(subtitle_text, encoding="utf-8")
            logger.info(f"[audio_regen] 已写入本地字幕文件: {local_txt}")
        except Exception as e:
            logger.warning(f"[audio_regen] 写入本地字幕文件失败（不影响主流程）: {e}")
    else:
        # 读取当前字幕内容，用于初始备份记录（确保回滚到原始版本时能恢复字幕）
        original_subtitle = _read_subtitle_from_oss(task_id, oss_node_id, f"{material_id}.txt", language=language)

    # 检查上一版是否为 failed → 重试模式
    retry_record = None
    latest = regen_version.find_latest_record(task_id, oss_node_id, material_id, language=language)
    if latest and latest["status"] == "failed":
        retry_record = latest
        logger.info(f"[audio_regen] 检测到上一版 failed，进入重试模式: id={latest['id']} session_no={latest['session_no']}")
        session_no = latest["session_no"]
    elif session_no is None:
        session_no = regen_version.allocate_session_no(task_id)

    # 初始备份（material_id 不带后缀，filename 单独拼；prompt 字段存原始字幕内容）
    audio_filename = f"{material_id}.wav"
    regen_version.ensure_initial_backup(
        task_id, "audio_producer", oss_node_id, material_id, "voices", original_subtitle,
        filename=audio_filename, user_id=user_id, user_name=user_name, language=language
    )

    # 读取本次生成实际使用的字幕内容
    current_subtitle = subtitle_text if new_content else original_subtitle

    # === 执行音频生成（最多 3 次重试）===
    import time
    last_error = None
    success = False
    result = None
    for attempt in range(1, 4):
        result = _run_single_audio(task_id, oss_node_id, material_id, force, session_no,
                                   language=language, override_voice_id=override_voice_id)
        if result.get("status") == "success":
            success = True
            break
        if result.get("status") == "skipped":
            # skipped 不算失败，直接退出循环
            break
        last_error = result.get("error") or "音频生成失败"
        logger.warning(f"[audio_regen] {material_id} 第 {attempt}/3 次失败: {last_error}")
        if attempt < 3:
            time.sleep(5)

    regen_key = regen_version.regen_oss_key(task_id, session_no, oss_node_id, "voices", audio_filename, language=language)
    # 本次实际使用的音色（成功时由 _run_single_audio 回传；override 优先）
    used_voice_id = (result.get("voice_id") if result else None) or override_voice_id

    if success:
        # 覆盖 OSS 主路径
        try:
            from common.config import OUTPUTS_PATH
            db_node_id_local, _ = parse_oss_node_id(oss_node_id)
            local_audio = Path(OUTPUTS_PATH) / task_id / db_node_id_local / "audios" / f"{language}-audio" / audio_filename
            if local_audio.exists():
                main_key = _oss_key_voice(task_id, oss_node_id, audio_filename, language=language)
                ok = oss_utils.oss_write(main_key, local_audio.read_bytes(), content_type="audio/wav")
                if ok:
                    logger.info(f"[audio_regen] 已覆盖 OSS 主路径: {main_key}")
                else:
                    logger.error(f"[audio_regen] 覆盖 OSS 主路径失败: {main_key}")

            # 镜像音频+字幕到 OSS storage/outputs/ 路径（game_builder 构建时优先从此拉取，否则会用旧镜像覆盖回新素材）
            local_txt_mirror = Path(OUTPUTS_PATH) / task_id / db_node_id_local / "audios" / f"{language}-subtitle" / f"{material_id}.txt"
            mirror_targets = [p for p in (local_audio, local_txt_mirror) if p.exists()]
            if mirror_targets:
                try:
                    from common.output_mirror import mirror_paths
                    mirror_paths(mirror_targets, force=True)
                except Exception as e:
                    logger.warning(f"[audio_regen] 镜像到 storage/outputs 失败（不影响主流程）: {e}")
        except Exception as e:
            logger.error(f"[audio_regen] 覆盖 OSS 主路径异常: {e}")

        # DB 记录
        if retry_record is not None:
            regen_version.update_record_to_success(
                record_id=retry_record["id"], oss_key=regen_key, prompt=current_subtitle,
                user_id=user_id, user_name=user_name, voice_id=used_voice_id,
            )
        else:
            regen_version.record_regen(
                task_id, session_no, "audio_producer", oss_node_id, material_id,
                regen_key, current_subtitle,
                user_id=user_id, user_name=user_name, status="success", language=language,
                voice_id=used_voice_id,
            )
    elif result.get("status") == "failed":
        # 3 次都失败：记 failed 行
        if retry_record is not None:
            from sqlalchemy import text as _t
            with _engine().begin() as conn:
                conn.execute(
                    _t("""
                        UPDATE public.material_regen_sessions
                        SET error_msg = :emsg, user_id = COALESCE(:uid, user_id),
                            user_name = COALESCE(:uname, user_name),
                            voice_id = COALESCE(:vid, voice_id)
                        WHERE id = :rid
                    """),
                    {"emsg": last_error, "uid": user_id, "uname": user_name,
                     "vid": used_voice_id, "rid": retry_record["id"]}
                )
        else:
            regen_version.record_regen(
                task_id, session_no, "audio_producer", oss_node_id, material_id,
                regen_key, current_subtitle,
                user_id=user_id, user_name=user_name, status="failed", error_msg=last_error,
                language=language, voice_id=used_voice_id,
            )

    if _usage_tracker:
        _usage_tracker.flush()

    final_status = result.get("status") if result else "failed"
    return {
        "total": 1,
        "success": 1 if final_status == "success" else 0,
        "failed": 1 if final_status == "failed" else 0,
        "session_no": session_no,
        "items": [{
            "material_id": material_id,
            "material_subtype": "audio",
            "status": final_status,
            "session_no": session_no,
            "voice_id": used_voice_id,
            "path": result.get("path") if result else None,
            "error": (result.get("error") if result else last_error),
        }],
        "errors": [result["error"]] if (result and result.get("error")) else [],
    }


# ---------------------------------------------------------------------------
# 角色级换音色（更新 voice_mappings + 联动重生成该角色全部台词）
# ---------------------------------------------------------------------------

def _update_voice_mapping(task_id: str, speaker_id: str, voice_id: str, language: str = "zh-CN") -> bool:
    """
    更新 voice_mappings 表中 data[language][speaker_id] = voice_id（三语结构）。
    兼容旧扁平结构：若 data 不是三语结构，则原地把 data 升级为三语结构再写入。
    返回是否成功写入。
    """
    lang_keys = {"zh-CN", "en-US", "ja-JP"}
    with _engine().begin() as conn:
        row = conn.execute(
            text("SELECT data FROM voice_mappings WHERE task_id = :tid LIMIT 1"),
            {"tid": task_id}
        ).fetchone()
        if not row:
            # 没有映射记录则新建一条三语结构
            data = {language: {speaker_id: voice_id}}
            conn.execute(
                text("""
                    INSERT INTO voice_mappings (task_id, data, create_time, update_time)
                    VALUES (:tid, :data, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT (task_id) DO UPDATE
                        SET data = EXCLUDED.data, update_time = CURRENT_TIMESTAMP
                """),
                {"tid": task_id, "data": json.dumps(data, ensure_ascii=False)}
            )
            return True

        data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        if not isinstance(data, dict):
            data = {}
        is_multilang = bool(lang_keys & set(data.keys()))
        if not is_multilang:
            # 旧扁平结构 {speaker: voice_id} → 升级为三语，把旧表当作当前语言的初始内容
            data = {language: dict(data)}
        sub = data.get(language, {})
        if not isinstance(sub, dict):
            sub = {}
        sub[speaker_id] = voice_id
        data[language] = sub
        conn.execute(
            text("UPDATE voice_mappings SET data = :data, update_time = CURRENT_TIMESTAMP WHERE task_id = :tid"),
            {"tid": task_id, "data": json.dumps(data, ensure_ascii=False)}
        )
    return True


def _find_speaker_material_ids(task_id: str, speaker_id: str, language: str = "zh-CN") -> list:
    """
    扫运行时及标准 subtitles，优先用字幕前缀、回落 master JSON 判定 speaker，
    返回属于该 speaker 的 (oss_node_id, material_id) 列表（material_id 不含后缀，过滤旁白）。

    speaker 判定与 _build_minimax_payload 取音色逻辑一致（同一个解析回落），
    保证"哪条属于该角色"与实际选音色完全对应。
    """
    if oss_utils is None:
        return []
    target = str(speaker_id).strip().lower()
    runtime_prefix = f"{task_id}/"
    standard_prefix = f"storage/outputs/{task_id}/"
    result = []
    seen = set()
    master_index = _load_master_audio_index(task_id)

    candidates = []
    runtime_marker = f"/assets/subtitles/{language}/"
    for key in oss_utils.oss_list_keys(runtime_prefix):
        if runtime_marker not in key or not key.endswith(".txt"):
            continue
        rest = key[len(runtime_prefix):]
        oss_node_id = rest.split("/assets/subtitles/", 1)[0]
        if oss_node_id:
            candidates.append((key, oss_node_id))

    standard_marker = f"/audios/{language}-subtitle/"
    for key in oss_utils.oss_list_keys(standard_prefix):
        if standard_marker not in key or not key.endswith(".txt"):
            continue
        rest = key[len(standard_prefix):]
        db_node_id = rest.split(standard_marker, 1)[0]
        if not db_node_id or "/" in db_node_id:
            continue
        try:
            oss_node_id = db_to_oss_node_id(db_node_id, task_id)
        except Exception:
            continue
        candidates.append((key, oss_node_id))

    for key, oss_node_id in candidates:
        filename = os.path.basename(key)
        material_id = filename[:-4]  # 去掉 .txt
        content = oss_utils.oss_read(key)
        if not content:
            continue
        try:
            is_valid, _, role, _, _ = _resolve_subtitle_line_info(
                task_id,
                oss_node_id,
                material_id,
                content.decode("utf-8"),
                master_index=master_index,
            )
        except Exception:
            continue
        # 只收可确认 speaker 的角色台词；旁白继续跳过。
        if not is_valid:
            continue
        if role.lower() in ["pangbai", "narrator"]:
            continue
        if role.lower() != target:
            continue
        dedup_key = (oss_node_id, material_id)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        result.append(dedup_key)
    return result


def regenerate_character_voice(
    task_id: str,
    speaker_id: str,
    voice_id: str,
    user_id: Optional[str] = None,
    user_name: Optional[str] = None,
    language: str = "zh-CN",
) -> dict:
    """
    角色级换音色：更新 voice_mappings 中该角色的 voice_id，并联动重生成该角色在该语言下的全部台词音频。

    流程：
      1. 更新 voice_mappings.data[language][speaker_id] = 新 voice_id（写 DB）
      2. 扫 OSS subtitles/{language}/ 找出该 speaker 的全部台词（过滤旁白）
      3. 逐条调用 regenerate_audio（override_voice_id=新音色, force=True），共用同一 session_no
      4. 汇总 items / errors

    Args:
        task_id:    任务 ID
        speaker_id: 角色 speaker_id（与字幕前缀一致，如 LiangChen）
        voice_id:   新音色 ID
        language:   语言（默认 zh-CN，只改中文，不影响日英）

    Returns:
        {"total", "success", "failed", "session_no", "voice_id", "speaker_id", "items", "errors"}
    """
    if oss_utils is None:
        return {"total": 0, "success": 0, "failed": 1, "items": [], "errors": ["oss_utils 未安装或导入失败"]}

    if _usage_tracker:
        _usage_tracker.set_context(task_id, "audio_producer")
    try:
        oss_utils.init_oss_client()
    except Exception as e:
        return {"total": 0, "success": 0, "failed": 1, "items": [], "errors": [f"OSS 客户端初始化失败: {e}"]}

    # 1. 更新 voice_mappings
    try:
        _update_voice_mapping(task_id, speaker_id, voice_id, language=language)
        logger.info(f"[voice_regen] 已更新 voice_mappings[{language}][{speaker_id}] = {voice_id}")
    except Exception as e:
        return {"total": 0, "success": 0, "failed": 1, "items": [],
                "errors": [f"更新 voice_mappings 失败: {e}"]}

    # 2. 找出该角色全部台词
    targets = _find_speaker_material_ids(task_id, speaker_id, language=language)
    if not targets:
        return {"total": 0, "success": 0, "failed": 0, "session_no": None,
                "voice_id": voice_id, "speaker_id": speaker_id, "items": [],
                "errors": [f"未找到 speaker_id={speaker_id} 在 {language} 下的台词音频"]}

    logger.info(f"[voice_regen] speaker={speaker_id} 共 {len(targets)} 条台词待重生成（{language}）")

    # 3. 分配一个共用 session_no（整批换音色算一次操作）
    session_no = regen_version.allocate_session_no(task_id)

    items = []
    errors = []
    success = 0
    failed = 0
    for oss_node_id, material_id in targets:
        single = regenerate_audio(
            task_id=task_id,
            node_id=oss_node_id,
            material_id=material_id,
            new_content=None,
            force=True,
            session_no=session_no,
            user_id=user_id,
            user_name=user_name,
            language=language,
            override_voice_id=voice_id,
        )
        # regenerate_audio 返回 items 列表（单条），取第一项汇总
        sub_items = single.get("items", [])
        for it in sub_items:
            it["node_id"] = oss_node_id
            items.append(it)
        success += single.get("success", 0)
        failed += single.get("failed", 0)
        errors.extend(single.get("errors", []))

    if _usage_tracker:
        _usage_tracker.flush()

    return {
        "total": len(targets),
        "success": success,
        "failed": failed,
        "session_no": session_no,
        "voice_id": voice_id,
        "speaker_id": speaker_id,
        "items": items,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# 元数据恢复（供 activate 端点调用）
# ---------------------------------------------------------------------------

def restore_audio_metadata(
    task_id: str,
    node_id: str,
    material_id: str,
    original_subtitle: str,
    language: str = "zh-CN",
) -> dict:
    """
    回滚时恢复音频元数据：
    - 把原始字幕内容写回 OSS subtitles/{language}/ 的 .txt 文件
    - 把原始字幕内容写到本地 audios/{language}-subtitle/ 目录
    - 把原始台词内容写回 plots 表对应 beat 的 content 字段（仅 zh-CN，plots 存源语言文本，
      回写翻译后的内容会污染源文本）

    Args:
        task_id:           任务 ID
        node_id:           OSS 格式 node_id
        material_id:       不含后缀的 material_id（如 scene_alley-1）
        original_subtitle: 原始字幕全文（格式 "{speaker}_1_{emotion}：{content}"）
        language:          语言（zh-CN/en-US/ja-JP）

    Returns:
        {"status": "success"|"partial"|"failed", "errors": [...]}
    """
    errors = []

    # 1. 写回 OSS .txt
    ok = _write_subtitle_to_oss(task_id, node_id, f"{material_id}.txt", original_subtitle, language=language)
    if not ok:
        errors.append(f"写回 OSS .txt 失败: {node_id}/subtitles/{language}/{material_id}.txt")
    else:
        logger.info(f"[audio_regen] 已恢复 OSS 字幕文件: {node_id}/subtitles/{language}/{material_id}.txt")

    # 2. 写到本地 audios/{language}-subtitle/ 目录
    try:
        from common.config import OUTPUTS_PATH
        db_node_id_r, _ = parse_oss_node_id(node_id)
        local_txt = Path(OUTPUTS_PATH) / task_id / db_node_id_r / "audios" / f"{language}-subtitle" / f"{material_id}.txt"
        local_txt.parent.mkdir(parents=True, exist_ok=True)
        local_txt.write_text(original_subtitle, encoding="utf-8")
        logger.info(f"[audio_regen] 已恢复本地字幕文件: {local_txt}")
    except Exception as e:
        errors.append(f"写本地字幕文件失败: {e}")

    # 3. 解析原始台词内容，写回 plots 表（仅 zh-CN：plots 存源语言文本）
    if language == "zh-CN":
        try:
            _, _, _, _, content = _parse_line_info(original_subtitle)
            beat_info = _find_beat_by_material_id(task_id, material_id)
            if beat_info:
                db_node_id = beat_info["node_id"]
                beat_id = beat_info["beat"].get("beat_id", "")
                updated = _update_beat_content(task_id, db_node_id, beat_id, content)
                if not updated:
                    errors.append(f"写回 plots 表失败: beat_id={beat_id}")
                else:
                    logger.info(f"[audio_regen] 已恢复 plots 台词: beat_id={beat_id}")
            else:
                errors.append(f"未找到 material_id={material_id} 对应的 beat，跳过 plots 恢复")
        except Exception as e:
            errors.append(f"恢复 plots 表异常: {e}")

    if not errors:
        return {"status": "success", "errors": []}
    return {"status": "partial" if ok else "failed", "errors": errors}
