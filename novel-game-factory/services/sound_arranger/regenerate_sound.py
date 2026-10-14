#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sound_arranger BGM/SFX 重生成接口

参数规范：
- task_id + node_id（OSS 格式）+ material_id（= bgm_id 或 sfx_id）
- node_id 格式：OSS 格式（chapter_1_chapter_1、chapter_2_7a80a1c2）
- 支持 new_prompt 修改提示词（只改 prompt，不改其他字段）
- material_id=None 时重生成该 node_id 下所有 BGM 或 SFX

存储：最终存储在 OSS，本地仅作 Stable Audio API 中转。
"""

import os
import sys
import json
import logging
import tempfile
import hashlib
from pathlib import Path
from typing import List, Optional

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from common.config import DATABASE_URL
from common.node_utils import parse_oss_node_id
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

def _oss_key_music(task_id: str, node_id: str, filename: str) -> str:
    """BGM OSS key: {task_id}/{node_id}/assets/music/{filename}"""
    return f"{task_id}/{node_id}/assets/music/{filename}"


def _oss_key_sounds(task_id: str, node_id: str, filename: str) -> str:
    """SFX OSS key: {task_id}/{node_id}/assets/sounds/{filename}"""
    return f"{task_id}/{node_id}/assets/sounds/{filename}"


def _oss_url(key: str) -> str:
    return oss_utils.oss_public_url(key) if oss_utils else key


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _engine():
    return create_engine(DATABASE_URL)


def _platform_engine():
    """sound 表已迁平台库(agent_platform);material_regen_sessions 仍在业务库"""
    from common.config import PLATFORM_DATABASE_URL
    return create_engine(PLATFORM_DATABASE_URL)


def _fetch_sound_data(task_id: str) -> dict:
    """
    从 sound 表读取数据。
    结构：{node_id: {"bgm_events": [...], "sfx_events": [...]}}
    """
    with _platform_engine().connect() as conn:
        row = conn.execute(
            text("SELECT data FROM sound WHERE task_id = :tid LIMIT 1"),
            {"tid": task_id}
        ).fetchone()
    if not row:
        return {}
    return row[0] if isinstance(row[0], dict) else json.loads(row[0])


def _update_sound_prompt(task_id: str, node_id: str, material_id: str, new_prompt: str) -> bool:
    """
    更新 sound 表中指定 bgm_id 或 sfx_id 的 prompt。
    只改 prompt 字段，不改其他字段。
    返回是否找到并更新。
    """
    with _platform_engine().begin() as conn:
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


def _find_event(sound_data: dict, node_id: str, material_id: str) -> Optional[dict]:
    """在 sound_data 中找到指定 node_id + material_id 的事件"""
    # 去掉扩展名，DB 中存的是不带扩展名的 id
    clean_id = material_id.replace(".mp3", "").replace(".wav", "")
    node_data = sound_data.get(node_id, {})
    for event in node_data.get("bgm_events", []):
        if event.get("bgm_id") == clean_id:
            return {"type": "bgm", **event}
    for event in node_data.get("sfx_events", []):
        if event.get("sfx_id") == clean_id:
            return {"type": "sfx", **event}
    return None


# ---------------------------------------------------------------------------
# 单条重生成
# ---------------------------------------------------------------------------

def _run_single_sound(
    task_id: str,
    node_id: str,
    material_id: str,
    audio_type: str,
    force: bool,
    session_no: int,
    prompt_override: Optional[str] = None,
) -> dict:
    """
    执行单条 BGM 或 SFX 的生成。
    使用 generate_sound.py 的 fetch_tasks_from_db() 和 run_batch_execution()。
    生成结果写入 regenerate 目录，不覆盖主路径。
    """
    if oss_utils is None:
        return {"status": "failed", "path": None, "error": "oss_utils 未安装"}

    # 调用 generate_sound.py 生成音频
    service_dir = os.path.dirname(os.path.abspath(__file__))
    if service_dir not in sys.path:
        sys.path.insert(0, service_dir)

    import generate_sound as gs

    # node_id 传入的是 oss 格式，fetch_tasks_from_db 需要 db 格式
    db_node_id, oss_node_id = parse_oss_node_id(node_id)

    # 使用改进后的 fetch_tasks_from_db，只获取指定的 material_id
    tasks = gs.fetch_tasks_from_db(
        target_task_id=task_id,
        node_ids=[db_node_id],
        sound_type=audio_type,
        material_ids=[material_id]
    )

    if not tasks:
        return {"status": "failed", "path": None, "error": f"未找到 material_id={material_id} 的任务"}

    # 获取任务信息（node_id_from_db 是 generate_sound.py 实际写文件用的路径）。
    # new_prompt 只覆盖本次内存任务，生成成功前不修改 sound.data。
    task_values = list(gs.unpack_audio_task(tasks[0]))
    if prompt_override:
        task_values[3] = prompt_override
    task_tuple = tuple(task_values)
    tasks = [task_tuple]
    (
        _,
        node_id_from_db,
        audio_type_actual,
        prompt,
        _,
        base_name,
        event_id,
        _,
    ) = gs.unpack_audio_task(task_tuple)

    # 构建主路径上的精确 canonical WAV key。
    if audio_type_actual == "bgm":
        scan_prefix = f"{task_id}/{oss_node_id}/assets/music/"
        subdir = "music"
    else:
        scan_prefix = f"{task_id}/{oss_node_id}/assets/sounds/"
        subdir = "sounds"

    expected_filename = f"{event_id}.wav"
    canonical_key = f"{scan_prefix}{expected_filename}"
    canonical_exists = oss_utils.oss_get_size(canonical_key) > 0

    # 只有精确 canonical WAV 可以触发 skipped；历史 MP3 不阻止迁移到 WAV。
    if (
        not force
        and not prompt_override
        and canonical_exists
    ):
        return {
            "status": "skipped",
            "path": _oss_url(canonical_key),
            "actual_filename": expected_filename,
            "error": None,
        }

    # 执行生成
    original_force = gs.FORCE_OVERWRITE
    gs.FORCE_OVERWRITE = True
    try:
        count = gs.run_batch_execution(
            tasks,
            f"regen_{oss_node_id}_{material_id}",
            cache_mode="bypass" if force else "reuse",
        )
        if count > 0:
            from common.config import OUTPUTS_PATH
            dir_name = "bgm" if audio_type_actual == "bgm" else "sfx"
            # generate_sound.py 写文件用的是 DB 格式的 node_id
            output_dir = str(Path(OUTPUTS_PATH) / task_id / node_id_from_db / "audios" / dir_name)

            # 主生成器当前固定输出精确的 {event_id}.wav。
            local_path = os.path.join(output_dir, expected_filename)

            if not (os.path.exists(local_path) and os.path.getsize(local_path) > 0):
                return {
                    "status": "failed",
                    "path": None,
                    "error": f"生成成功但找不到精确 WAV: {local_path}",
                }

            actual_filename = os.path.basename(local_path)

            # 上传到 regenerate 目录，不覆盖主路径
            regen_key = regen_version.regen_oss_key(task_id, session_no, oss_node_id, subdir, actual_filename)
            with open(local_path, "rb") as f:
                audio_data = f.read()
            if (
                len(audio_data) < 12
                or audio_data[:4] != b"RIFF"
                or audio_data[8:12] != b"WAVE"
            ):
                return {
                    "status": "failed",
                    "path": None,
                    "error": "生成结果不是 RIFF/WAVE 文件",
                }
            ok = oss_utils.oss_write(regen_key, audio_data, content_type="audio/wav")
            if ok:
                return {
                    "status": "success",
                    "path": _oss_url(regen_key),
                    "actual_filename": actual_filename,
                    "action": "regenerate" if canonical_exists else "generate",
                    "error": None
                }
            return {"status": "failed", "path": None, "error": "上传 OSS 失败"}
        return {"status": "failed", "path": None, "error": "run_batch_execution 返回 0"}
    except Exception as e:
        return {"status": "failed", "path": None, "error": str(e)}
    finally:
        gs.FORCE_OVERWRITE = original_force


# ---------------------------------------------------------------------------
# 公开接口
# ---------------------------------------------------------------------------

def regenerate_sound(
    task_id: str,
    node_id: str,
    material_id: Optional[str] = None,
    new_prompt: Optional[str] = None,
    sound_type: str = "all",
    force: bool = True,
    session_no: Optional[int] = None,
    user_id: Optional[str] = None,
    user_name: Optional[str] = None,
) -> dict:
    """
    重生成 BGM 或 SFX。

    Args:
        task_id: 任务 ID
        node_id: OSS 格式的 node_id（如 chapter_1_chapter_1、chapter_2_7a80a1c2）
        material_id: bgm_id 或 sfx_id；None=重生成该节点下所有
        new_prompt: 新提示词（若提供则在 WAV 发布成功后更新 DB）
        sound_type: "bgm"|"sfx"|"all"，仅在 material_id=None 时生效
        force: True=强制覆盖已有文件

    Returns:
        {"total": int, "success": int, "failed": int, "items": [...], "errors": [...]}
    """
    if oss_utils is None:
        return {
            "total": 0, "success": 0, "failed": 1,
            "items": [], "errors": ["oss_utils 未安装或导入失败"]
        }
    try:
        oss_utils.init_oss_client()
    except Exception as e:
        return {
            "total": 0, "success": 0, "failed": 1,
            "items": [], "errors": [f"OSS 客户端初始化失败: {e}"]
        }

    # 解析 node_id：db_node_id 用于查 DB，oss_node_id 用于 OSS 路径
    db_node_id, oss_node_id = parse_oss_node_id(node_id)

    sound_data = _fetch_sound_data(task_id)
    if not sound_data:
        return {"total": 0, "success": 0, "failed": 0, "items": [], "errors": ["sound 表中无数据"]}

    if db_node_id not in sound_data:
        return {"total": 0, "success": 0, "failed": 0, "items": [], "errors": [f"node_id={db_node_id} 不存在"]}

    # 若指定了 material_id，只处理该条
    if material_id is not None:
        # 去掉扩展名，DB 中存的是不带扩展名的 id
        clean_material_id = material_id.replace(".mp3", "").replace(".wav", "")

        # 确定 audio_type（在更新 DB 前先读，保留旧 prompt 用于初始备份）
        event = _find_event(sound_data, db_node_id, clean_material_id)
        if not event:
            return {
                "total": 0, "success": 0, "failed": 1,
                "items": [], "errors": [f"未找到 node_id={db_node_id} 下的 material_id={clean_material_id}"]
            }
        original_prompt = event.get("prompt")  # 更新前的旧 prompt，用于初始备份

        # 本次生成实际使用的 prompt
        current_prompt = new_prompt if new_prompt else original_prompt

        subdir = "music" if event["type"] == "bgm" else "sounds"

        # 检查上一版是否为 failed → 重试模式
        retry_record = None
        latest = regen_version.find_latest_record(task_id, oss_node_id, clean_material_id)
        if latest and latest["status"] == "failed":
            retry_record = latest
            logger.info(f"[sound_regen] 检测到上一版 failed，进入重试模式: id={latest['id']} session_no={latest['session_no']}")
            session_no = latest["session_no"]
        elif session_no is None:
            session_no = regen_version.allocate_session_no(task_id)

        # 初始备份优先精确 canonical WAV；历史同时间轴 MP3 仅作兼容回退。
        timeline_suffix = clean_material_id.split("_")[-1]  # e.g. "1-27"
        scan_prefix = f"{task_id}/{oss_node_id}/assets/{subdir}/"
        canonical_filename = f"{clean_material_id}.wav"
        canonical_key = f"{scan_prefix}{canonical_filename}"
        if oss_utils.oss_get_size(canonical_key) > 0:
            actual_oss_filename = canonical_filename
        else:
            legacy_mp3_keys = [
                key for key in oss_utils.oss_list_keys(scan_prefix)
                if key.endswith(f"_{timeline_suffix}.mp3")
            ]
            actual_oss_filename = (
                os.path.basename(legacy_mp3_keys[0])
                if legacy_mp3_keys
                else None
            )
        regen_version.ensure_initial_backup(
            task_id, "sound_arranger", oss_node_id, clean_material_id, subdir,
            original_prompt, filename=actual_oss_filename,
            user_id=user_id, user_name=user_name
        )

        # === 生成音频（最多 3 次重试）===
        import time
        last_error = None
        success = False
        result = None
        for attempt in range(1, 4):
            result = _run_single_sound(
                task_id,
                oss_node_id,
                clean_material_id,
                event["type"],
                force,
                session_no,
                prompt_override=new_prompt,
            )
            if result.get("status") == "success":
                success = True
                break
            if result.get("status") == "skipped":
                break
            last_error = result.get("error") or "音效生成失败"
            logger.warning(f"[sound_regen] {clean_material_id} 第 {attempt}/3 次失败: {last_error}")
            if attempt < 3:
                time.sleep(5)

        if success:
            actual_filename = result.get("actual_filename", f"{clean_material_id}.wav")
            regen_key = regen_version.regen_oss_key(task_id, session_no, oss_node_id, subdir, actual_filename)

            # 先发布精确 canonical WAV 到主 OSS；发布失败不得提交 prompt/成功记录。
            try:
                from common.config import OUTPUTS_PATH
                db_node_id_local, _ = parse_oss_node_id(oss_node_id)
                local_subdir = "bgm" if subdir == "music" else "sfx"
                local_wav = (
                    Path(OUTPUTS_PATH)
                    / task_id
                    / db_node_id_local
                    / "audios"
                    / local_subdir
                    / f"{clean_material_id}.wav"
                )
                if not (local_wav.exists() and local_wav.stat().st_size > 0):
                    raise RuntimeError(f"找不到正式 WAV: {local_wav}")

                audio_data = local_wav.read_bytes()
                main_filename = f"{clean_material_id}.wav"
                main_key = f"{task_id}/{oss_node_id}/assets/{subdir}/{main_filename}"
                if not oss_utils.oss_write(main_key, audio_data, content_type="audio/wav"):
                    raise RuntimeError(f"覆盖 OSS 主路径失败: {main_key}")
                logger.info(f"[sound_regen] 已覆盖 OSS 主路径: {main_key}")

                # 镜像仍是 best-effort，失败只记 warning。
                try:
                    from common.output_mirror import mirror_paths
                    mirrored_count = mirror_paths([local_wav], force=True)
                    if not mirrored_count:
                        logger.warning(
                            "[sound_regen] 镜像到 storage/outputs 失败"
                            "（不影响主流程）: 未镜像任何文件"
                        )
                except Exception as e:
                    logger.warning(f"[sound_regen] 镜像到 storage/outputs 失败（不影响主流程）: {e}")
            except Exception as e:
                last_error = f"发布主 WAV 失败: {e}"
                logger.error(f"[sound_regen] {last_error}")
                result = {"status": "failed", "path": None, "error": last_error}
                success = False

        if success and new_prompt:
            prompt_error = None
            try:
                prompt_updated = _update_sound_prompt(
                    task_id,
                    db_node_id,
                    clean_material_id,
                    new_prompt,
                )
            except Exception as e:
                prompt_updated = False
                prompt_error = f"更新 prompt 异常: {e}"
            if not prompt_updated:
                last_error = prompt_error or f"更新 prompt 失败: material_id={clean_material_id}"
                result = {**result, "status": "failed", "error": last_error}
                success = False

        if success:
            # DB 记录
            if retry_record is not None:
                regen_version.update_record_to_success(
                    record_id=retry_record["id"], oss_key=regen_key, prompt=current_prompt,
                    user_id=user_id, user_name=user_name,
                )
            else:
                regen_version.record_regen(
                    task_id, session_no, "sound_arranger", oss_node_id, clean_material_id,
                    regen_key, current_prompt,
                    user_id=user_id, user_name=user_name, status="success",
                )
        elif result and result.get("status") == "failed":
            # 3 次都失败：记 failed 行
            regen_key_dummy = regen_version.regen_oss_key(
                task_id, session_no, oss_node_id, subdir, f"{clean_material_id}.wav"
            )
            if retry_record is not None:
                from sqlalchemy import text as _t
                with _engine().begin() as conn:
                    conn.execute(
                        _t("""
                            UPDATE public.material_regen_sessions
                            SET error_msg = :emsg, user_id = COALESCE(:uid, user_id),
                                user_name = COALESCE(:uname, user_name)
                            WHERE id = :rid
                        """),
                        {"emsg": last_error, "uid": user_id, "uname": user_name, "rid": retry_record["id"]}
                    )
            else:
                regen_version.record_regen(
                    task_id, session_no, "sound_arranger", oss_node_id, clean_material_id,
                    regen_key_dummy, current_prompt,
                    user_id=user_id, user_name=user_name,
                    status="failed", error_msg=last_error,
                )

        final_status = result.get("status") if result else "failed"
        return {
            "total": 1,
            "success": 1 if final_status == "success" else 0,
            "failed": 1 if final_status == "failed" else 0,
            "session_no": session_no,
            "items": [{
                "material_id": material_id,
                "material_subtype": event["type"],
                "status": final_status,
                "session_no": session_no,
                "path": result.get("path") if result else None,
                "error": (result.get("error") if result else last_error),
            }],
            "errors": [result["error"]] if (result and result.get("error")) else [],
        }

    # 批量模式暂不开放（material_id=None 时重生成该节点下所有匹配事件）
    return {
        "total": 0, "success": 0, "failed": 1,
        "items": [], "errors": ["material_id 不能为空，批量重生成暂不开放"]
    }


# ---------------------------------------------------------------------------
# 元数据恢复（供 activate 端点调用）
# ---------------------------------------------------------------------------

def restore_sound_prompt(
    task_id: str,
    node_id: str,
    material_id: str,
    original_prompt: str,
) -> dict:
    """
    回滚时恢复 BGM/SFX 提示词：把原始 prompt 写回 sound 表。

    Args:
        task_id:         任务 ID
        node_id:         DB 格式 node_id（如 chapter_1）
        material_id:     不含后缀的 bgm_id 或 sfx_id
        original_prompt: 原始提示词

    Returns:
        {"status": "success"|"failed", "errors": [...]}
    """
    db_node_id, _ = parse_oss_node_id(node_id)
    updated = _update_sound_prompt(task_id, db_node_id, material_id, original_prompt)
    if updated:
        logger.info(f"[sound_regen] 已恢复 sound 表 prompt: {db_node_id}/{material_id}")
        return {"status": "success", "errors": []}
    return {"status": "failed", "errors": [f"写回 sound 表 prompt 失败: {db_node_id}/{material_id}"]}
