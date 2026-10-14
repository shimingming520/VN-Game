#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回滚/激活时的元数据恢复（独立实现，供 api_gateway 同步调用）

放在 common/ 下，不依赖各 service 包。api_gateway 容器只 COPY 了 common/、story_planner/
和自身，没有 audio_producer / char_designer / scene_painter / sound_arranger 代码，
因此不能直接 import 这些 service 的 regenerate 模块（会报 No module named）。

这里把 4 个 service 的 restore 逻辑内联为纯 DB/OSS 只读+写操作（不调 TTS/生图）：
- audio_producer：把原始字幕写回 OSS subtitles/{lang}/、本地 {lang}-subtitle/，并回写 plots 表（仅 zh-CN）
- char_designer：把原始 positive_prompt 写回 character_prompts 表
- scene_painter：把原始 prompt 写回 scene_prompts / backgrounds 表
- sound_arranger：把原始 prompt 写回 sound 表

逻辑与各 service regenerate.py 中的 restore_* 函数保持一致；若那边改了表结构，这里需同步。
"""

import os
import re
import json
import logging
from pathlib import Path
from typing import Optional

from common.node_utils import parse_oss_node_id
from common.platform_db import platform_engine
from sqlalchemy import text

try:
    from common import oss_utils
except ImportError:
    oss_utils = None

logger = logging.getLogger(__name__)


def _engine():
    # 回滚会连续执行多次元数据读写，统一复用共享平台库连接池。
    return platform_engine


# ===========================================================================
# audio_producer
# ===========================================================================

def _oss_key_subtitle(task_id: str, node_id: str, filename: str, language: str = "zh-CN") -> str:
    return f"{task_id}/{node_id}/assets/subtitles/{language}/{filename}"


def _standard_output_subtitle_key(
    task_id: str,
    node_id: str,
    filename: str,
    language: str = "zh-CN",
) -> str:
    db_node_id, _ = parse_oss_node_id(node_id)
    return (
        f"storage/outputs/{task_id}/{db_node_id}/audios/"
        f"{language}-subtitle/{filename}"
    )


def _write_subtitle_to_oss(task_id: str, node_id: str, material_id: str, content: str,
                           language: str = "zh-CN") -> bool:
    if oss_utils is None:
        return False
    data = content.encode("utf-8")
    runtime_key = _oss_key_subtitle(task_id, node_id, material_id, language=language)
    standard_key = _standard_output_subtitle_key(
        task_id, node_id, material_id, language=language
    )
    runtime_ok = oss_utils.oss_write(
        runtime_key, data, content_type="text/plain; charset=utf-8"
    )
    standard_ok = oss_utils.oss_write(
        standard_key, data, content_type="text/plain; charset=utf-8"
    )
    if not standard_ok:
        logger.warning(f"[restore] 标准字幕产物写回失败: {standard_key}")
    return runtime_ok and standard_ok


def _parse_line_info(text_line: str):
    """解析字幕: {speaker}_{组合}_{emotion}：{content}，返回 (is_valid, full_prefix, role, emotion, content)。
    与 audio_producer/regenerate_audio.py 的同名函数对齐。"""
    text_line = text_line.strip()
    if "：" in text_line:
        parts = text_line.split("：", 1)
        full_prefix = parts[0].strip()
        content = parts[1]
    else:
        return False, "PangBai", "pangbai", "neutral", text_line

    if full_prefix.lower() in ["pangbai", "narrator"]:
        return False, full_prefix, "pangbai", "neutral", content

    match = re.match(r"^(.+?)_[\d][\d\-]*_(.+?)$", full_prefix, re.IGNORECASE)
    if match:
        return True, full_prefix, match.group(1).lower(), match.group(2).lower(), content
    return False, full_prefix, full_prefix.lower(), "neutral", content


def _fetch_plots_data(task_id: str) -> dict:
    with _engine().connect() as conn:
        row = conn.execute(
            text("SELECT data FROM plots WHERE task_id = :tid LIMIT 1"),
            {"tid": task_id}
        ).fetchone()
    if not row:
        return {}
    return row[0] if isinstance(row[0], dict) else json.loads(row[0])


def _find_beat_by_material_id(task_id: str, material_id: str) -> Optional[dict]:
    """根据 material_id（不含后缀）找到对应 beat；格式 {scene_id}-{beat_id} 或 UnknownScene-{beat_id}。"""
    name = material_id.replace(".txt", "")
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


def _update_beat_content(task_id: str, node_id: str, beat_id: str, new_content: str) -> bool:
    with _engine().begin() as conn:
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


def restore_audio_metadata(task_id: str, node_id: str, material_id: str,
                           original_subtitle: str, language: str = "zh-CN") -> dict:
    """恢复音频元数据：写回 OSS 字幕、本地字幕，plots 表（仅 zh-CN）。"""
    errors = []

    ok = _write_subtitle_to_oss(task_id, node_id, f"{material_id}.txt", original_subtitle, language=language)
    if not ok:
        errors.append(f"写回运行时或标准 OSS 字幕失败: {node_id}/{language}/{material_id}.txt")
    else:
        logger.info(f"[restore] 已恢复运行时及标准 OSS 字幕: {node_id}/{language}/{material_id}.txt")

    try:
        from common.config import OUTPUTS_PATH
        db_node_id_r, _ = parse_oss_node_id(node_id)
        local_txt = Path(OUTPUTS_PATH) / task_id / db_node_id_r / "audios" / f"{language}-subtitle" / f"{material_id}.txt"
        local_txt.parent.mkdir(parents=True, exist_ok=True)
        local_txt.write_text(original_subtitle, encoding="utf-8")
        logger.info(f"[restore] 已恢复本地字幕文件: {local_txt}")
    except Exception as e:
        errors.append(f"写本地字幕文件失败: {e}")

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
                    logger.info(f"[restore] 已恢复 plots 台词: beat_id={beat_id}")
            else:
                errors.append(f"未找到 material_id={material_id} 对应的 beat，跳过 plots 恢复")
        except Exception as e:
            errors.append(f"恢复 plots 表异常: {e}")

    if not errors:
        return {"status": "success", "errors": []}
    return {"status": "partial" if ok else "failed", "errors": errors}


# ===========================================================================
# char_designer
# ===========================================================================

def _character_combination_suffix(value: object) -> str:
    match = re.search(r"(?:^|[-_])(\d+)[-_](\d+)[-_](\d+)$", str(value or "").strip())
    return "-".join(match.groups()) if match else ""


def _update_prompt_by_combination(task_id: str, speaker_id: str, combination_num: str,
                                   new_prompt: str) -> bool:
    with _engine().begin() as conn:
        row = conn.execute(
            text("SELECT data FROM character_prompts WHERE task_id = :tid"),
            {"tid": task_id}
        ).fetchone()
        if not row:
            return False
        items = row[0] if isinstance(row[0], list) else json.loads(row[0])
        updated = False
        for item in items:
            if not isinstance(item, dict):
                continue
            record_speaker_id = (
                item.get("speaker_id")
                or item.get("logical_character_id")
                or item.get("character_id")
            )
            combo_num = _character_combination_suffix(
                item.get("combination_num")
                or item.get("combination")
                or item.get("material_id")
            )
            if str(record_speaker_id or "") == speaker_id and combo_num == combination_num:
                item["positive_prompt"] = new_prompt
                if "prompt" in item:
                    item["prompt"] = new_prompt
                if "image_prompt" in item:
                    item["image_prompt"] = new_prompt
                updated = True
                break
        if updated:
            conn.execute(
                text("UPDATE character_prompts SET data = :data WHERE task_id = :tid"),
                {"data": json.dumps(items, ensure_ascii=False), "tid": task_id}
            )
        return updated


def restore_char_prompt(task_id: str, speaker_id: str, combination_num: str,
                        original_prompt: str) -> dict:
    updated = _update_prompt_by_combination(task_id, speaker_id, combination_num, original_prompt)
    if updated:
        logger.info(f"[restore] 已恢复 character_prompts: speaker_id={speaker_id} combination={combination_num}")
        return {"status": "success", "errors": []}
    return {"status": "failed",
            "errors": [f"写回 character_prompts 失败: speaker_id={speaker_id} combination={combination_num}"]}


# ===========================================================================
# scene_painter
# ===========================================================================

def _fetch_scenes_data(task_id: str) -> dict:
    with _engine().connect() as conn:
        row = conn.execute(
            text("SELECT data FROM scene_prompts WHERE task_id = :tid ORDER BY create_time DESC LIMIT 1"),
            {"tid": task_id}
        ).fetchone()
    if not row:
        return {}
    return row[0] if isinstance(row[0], dict) else json.loads(row[0])


def _update_scene_prompt(task_id: str, scene_id: str, new_prompt: str) -> None:
    with _engine().begin() as conn:
        row = conn.execute(
            text("SELECT data FROM scene_prompts WHERE task_id = :tid ORDER BY create_time DESC LIMIT 1"),
            {"tid": task_id}
        ).fetchone()
        if not row:
            return
        data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        if scene_id in data:
            data[scene_id]["image_prompt"] = new_prompt
            conn.execute(
                text("UPDATE scene_prompts SET data = :data WHERE task_id = :tid"),
                {"data": json.dumps(data, ensure_ascii=False), "tid": task_id}
            )


def _update_background_prompt(task_id: str, node_id: str, new_prompt: str) -> None:
    with _engine().begin() as conn:
        row = conn.execute(
            text("SELECT data FROM backgrounds WHERE task_id = :tid LIMIT 1"),
            {"tid": task_id}
        ).fetchone()
        if not row:
            return
        data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        if node_id == "novel_cover":
            if "novel_cover_metadata" in data:
                data["novel_cover_metadata"]["prompt"] = new_prompt
        else:
            gp = data.get("generated_prompts", {})
            if node_id in gp:
                gp[node_id]["prompt"] = new_prompt
                data["generated_prompts"] = gp
        conn.execute(
            text("UPDATE backgrounds SET data = :data WHERE task_id = :tid"),
            {"data": json.dumps(data, ensure_ascii=False), "tid": task_id}
        )


def restore_scene_prompt(task_id: str, material_id: str, original_prompt: str) -> dict:
    scenes_data = _fetch_scenes_data(task_id)
    if material_id in scenes_data:
        _update_scene_prompt(task_id, material_id, original_prompt)
        logger.info(f"[restore] 已恢复 scenes prompt: {material_id}")
        return {"status": "success", "errors": []}

    _update_background_prompt(
        task_id,
        material_id if material_id == "novel_cover" else parse_oss_node_id(material_id)[0],
        original_prompt,
    )
    logger.info(f"[restore] 已恢复 backgrounds prompt: {material_id}")
    return {"status": "success", "errors": []}


# ===========================================================================
# sound_arranger
# ===========================================================================

def _update_sound_prompt(task_id: str, node_id: str, material_id: str, new_prompt: str) -> bool:
    with _engine().begin() as conn:
        row = conn.execute(
            text("SELECT data FROM sound WHERE task_id = :tid LIMIT 1"),
            {"tid": task_id}
        ).fetchone()
        if not row:
            return False
        data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        node_data = data.get(node_id, {})
        updated = False
        for event in node_data.get("bgm_events", []):
            if event.get("bgm_id") == material_id:
                event["prompt"] = new_prompt
                updated = True
                break
        if not updated:
            for event in node_data.get("sfx_events", []):
                if event.get("sfx_id") == material_id:
                    event["prompt"] = new_prompt
                    updated = True
                    break
        if updated:
            data[node_id] = node_data
            conn.execute(
                text("UPDATE sound SET data = :data WHERE task_id = :tid"),
                {"data": json.dumps(data, ensure_ascii=False), "tid": task_id}
            )
        return updated


def restore_sound_prompt(task_id: str, node_id: str, material_id: str, original_prompt: str) -> dict:
    db_node_id, _ = parse_oss_node_id(node_id)
    updated = _update_sound_prompt(task_id, db_node_id, material_id, original_prompt)
    if updated:
        logger.info(f"[restore] 已恢复 sound 表 prompt: {db_node_id}/{material_id}")
        return {"status": "success", "errors": []}
    return {"status": "failed", "errors": [f"写回 sound 表 prompt 失败: {db_node_id}/{material_id}"]}
