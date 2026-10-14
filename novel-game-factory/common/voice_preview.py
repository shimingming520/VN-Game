"""Generate and cache local voice audition samples."""

from __future__ import annotations

import os
import re
from pathlib import Path

import requests

from common.oss_utils import local_asset_url, local_path_for_key


AGE_CN_TO_DIR = {
    "儿童": "child",
    "少年": "adolescent",
    "青年": "young_adult",
    "中年": "middle_aged",
    "老年": "elderly",
}


def _gender_to_dir(gender: str) -> str:
    value = str(gender or "").strip().lower()
    return "female" if value in {"女", "female", "f"} else "male"


def _age_to_dir(age_category: str) -> str:
    value = str(age_category or "").strip()
    if value in AGE_CN_TO_DIR:
        return AGE_CN_TO_DIR[value]
    lowered = value.lower()
    if any(item in lowered for item in ("child", "kid", "baby", "儿童")):
        return "child"
    if any(item in lowered for item in ("teen", "adolescent", "少年")):
        return "adolescent"
    if any(item in lowered for item in ("middle", "中年")):
        return "middle_aged"
    if any(item in lowered for item in ("elderly", "senior", "老年")):
        return "elderly"
    return "young_adult"


def _safe_voice_id(voice_id: str) -> str:
    value = str(voice_id or "").strip()
    if not value or len(value) > 180 or not re.fullmatch(r"[A-Za-z0-9_.()\- ]+", value):
        raise ValueError("非法 voice_id")
    return value


def _sample_key(gender: str, age_category: str, voice_id: str) -> str:
    return (
        f"storage/timbre/{_gender_to_dir(gender)}/"
        f"{_age_to_dir(age_category)}/{_safe_voice_id(voice_id)}.wav"
    )


def generate_local_audition(
    *,
    voice_id: str,
    gender: str,
    age_category: str,
    text: str = "你好，很高兴认识你，这是我的声音试听。",
) -> str:
    """Generate one MiniMax sample and persist it below local storage."""
    key = _sample_key(gender, age_category, voice_id)
    path = local_path_for_key(key)
    if path.is_file() and path.stat().st_size > 44:
        return local_asset_url(key)

    relay_base = os.getenv("AUDIO_BASE_URL", "").strip().rstrip("/")
    if relay_base:
        api_key = os.getenv("ELEVENLABS_RELAY_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "已配置 AUDIO_BASE_URL，但未配置 ELEVENLABS_RELAY_API_KEY"
            )
        endpoint = f"{relay_base}/v1/audio/speech"
        payload = {
            "model": os.getenv("MINIMAX_SPEECH_MODEL", "speech-2.8-hd"),
            "input": text[:300],
            "voice": _safe_voice_id(voice_id),
            "response_format": "hex",
            "speed": 1.0,
            "metadata": {
                "voice_setting": {
                    "voice_id": _safe_voice_id(voice_id),
                    "speed": 1.0,
                    "vol": 2.1,
                    "pitch": 0,
                    "emotion": "calm",
                    "text_normalization": True,
                },
                "audio_setting": {
                    "sample_rate": 44100,
                    "format": "wav",
                    "channel": 1,
                    "output_format": "hex",
                },
                "subtitle_enable": False,
            },
        }
    else:
        api_key = os.getenv("MINIMAX_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("MINIMAX_API_KEY 未配置")
        endpoint = os.getenv(
            "MINIMAX_T2A_URL",
            "https://api.minimaxi.com/v1/t2a_v2",
        ).strip()
        payload = {
            "model": "speech-2.8-hd",
            "text": text[:300],
            "stream": False,
            "voice_setting": {
                "voice_id": _safe_voice_id(voice_id),
                "speed": 1.0,
                "vol": 2.1,
                "pitch": 0,
                "emotion": "calm",
                "text_normalization": True,
            },
            "audio_setting": {
                "sample_rate": 44100,
                "format": "wav",
                "channel": 1,
            },
            "subtitle_enable": False,
        }
    response = requests.post(
        endpoint,
        json=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        timeout=90,
    )
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        detail = response.text.strip()[:500]
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(
            f"试听 TTS 请求失败（HTTP {response.status_code}）{suffix}"
        ) from exc
    content_type = response.headers.get("Content-Type", "").lower()
    if content_type.startswith("audio/") and response.content:
        audio = response.content
    else:
        body = response.json()
        base_resp = body.get("base_resp") or {}
        if base_resp.get("status_code", 0) not in (0, None):
            raise RuntimeError(base_resp.get("status_msg") or "TTS 服务返回失败")
        encoded = (body.get("data") or {}).get("audio")
        if not encoded:
            raise RuntimeError("TTS 服务没有返回音频数据")
        try:
            audio = bytes.fromhex(encoded)
        except ValueError as exc:
            raise RuntimeError("TTS 返回的音频数据格式错误") from exc
    if len(audio) <= 44:
        raise RuntimeError("TTS 返回的音频为空")

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    temp_path.write_bytes(audio)
    os.replace(temp_path, path)
    return local_asset_url(key)
