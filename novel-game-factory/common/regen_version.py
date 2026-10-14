#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
素材重生成版本管理公共模块

职责：
- 分配 session_no（DB 事务内原子递增，全局唯一，用于 OSS 路径）
- 分配 material_version（每个素材独立计数，0-indexed，显示为 1.0/1.1/...）
- 备份初始版本（session_no=0，第一次重生成前自动触发）
- 构建 regenerate 目录的 OSS key
- 激活指定版本（从 OSS regenerate 路径下载，写到本地 OUTPUTS_PATH，同步覆盖 OSS 主路径 +
  镜像到 OSS storage/outputs/ 路径，保证游戏引擎与 game_builder 都读到激活版本）
- 查询历史记录
"""

import os
import logging
from pathlib import Path
from typing import Optional, List
from sqlalchemy import text

from common.task_models import engine as business_engine

logger = logging.getLogger(__name__)

# regenerate 目录后缀
REGEN_SUFFIX = "-regenerate"


def _engine():
    # 复用进程级共享池。旧实现每次调用都 create_engine，会不断创建临时连接池，
    # 素材连续重生成/回滚时容易把 PostgreSQL 总连接数顶满。
    return business_engine


# ---------------------------------------------------------------------------
# OSS key 构建
# ---------------------------------------------------------------------------

def regen_oss_key(task_id: str, session_no: int, node_id: str, subdir: str, filename: str,
                  language: str = "") -> str:
    """
    构建 regenerate 目录下的 OSS key。

    格式：{task_id}-regenerate/{session_no}/{node_id}/assets/{subdir}/[{language}/]{filename}

    Args:
        task_id:    任务 ID
        session_no: 操作序号（0=初始备份，1/2/3...=重生成）
        node_id:    OSS 格式 node_id，如 chapter_1_chapter_1
        subdir:     素材子目录，如 characters / scenes / voices / music / sounds / images
        filename:   文件名，如 LiYu_1_calm.png
        language:   语言维度（audio_producer 用 zh-CN/en-US/ja-JP）；空串=无语言维度，不加子目录

    Returns:
        完整 OSS key
    """
    if language:
        return f"{task_id}{REGEN_SUFFIX}/{session_no}/{node_id}/assets/{subdir}/{language}/{filename}"
    return f"{task_id}{REGEN_SUFFIX}/{session_no}/{node_id}/assets/{subdir}/{filename}"


def main_oss_key(task_id: str, node_id: str, subdir: str, filename: str, language: str = "") -> str:
    """构建主路径 OSS key（游戏引擎读取的路径）。language 非空时插入 {subdir}/{language}/ 子层。"""
    if language:
        return f"{task_id}/{node_id}/assets/{subdir}/{language}/{filename}"
    return f"{task_id}/{node_id}/assets/{subdir}/{filename}"


# ---------------------------------------------------------------------------
# session_no 分配
# ---------------------------------------------------------------------------

def allocate_session_no(task_id: str) -> int:
    """
    在事务内原子分配下一个 session_no（全局递增，用于 OSS 路径唯一性）。
    同一 task_id 内从 1 开始递增（0 保留给初始备份）。

    Returns:
        新分配的 session_no（>= 1）
    """
    with _engine().begin() as conn:
        # FOR UPDATE 不能直接用于聚合查询，先锁行再取 MAX
        conn.execute(
            text("""
                SELECT id FROM public.material_regen_sessions
                WHERE task_id = :tid AND session_no >= 1
                ORDER BY session_no DESC
                LIMIT 1
                FOR UPDATE
            """),
            {"tid": task_id}
        ).fetchone()
        row = conn.execute(
            text("""
                SELECT COALESCE(MAX(session_no), 0) + 1
                FROM public.material_regen_sessions
                WHERE task_id = :tid AND session_no >= 1
            """),
            {"tid": task_id}
        ).fetchone()
        return row[0]


def allocate_material_version(task_id: str, node_id: str, material_id: str, language: str = "") -> int:
    """
    分配该素材的下一个 material_version（每个素材独立计数，从 1 开始）。
    初始备份（session_no=0）固定为 0，重生成版本从 1 开始递增。

    Returns:
        新分配的 material_version（>= 1）
    """
    with _engine().connect() as conn:
        row = conn.execute(
            text("""
                SELECT COALESCE(MAX(material_version), 0) + 1
                FROM public.material_regen_sessions
                WHERE task_id = :tid AND node_id = :nid AND material_id = :mid
                  AND language = :lang
                  AND session_no >= 1
            """),
            {"tid": task_id, "nid": node_id, "mid": material_id, "lang": language}
        ).fetchone()
        return row[0]


def version_str(material_version: int) -> str:
    """将 material_version 整数转为显示字符串，如 0 → '1'（原始版本），1 → '1.1'，2 → '1.2'"""
    if material_version == 0:
        return "1"
    return f"1.{material_version}"


# ---------------------------------------------------------------------------
# 初始备份（session_no=0）
# ---------------------------------------------------------------------------

def ensure_initial_backup(
    task_id: str,
    service_type: str,
    node_id: str,
    material_id: str,
    subdir: str,
    prompt: Optional[str] = None,
    filename: Optional[str] = None,
    user_id: Optional[str] = None,
    user_name: Optional[str] = None,
    language: str = "",
) -> bool:
    """
    确保 session_no=0 的初始备份存在。
    若已存在则跳过；若不存在则从主路径 copy 到 regenerate/0/ 并插入记录。

    Args:
        material_id: DB 中存储的素材标识（不含后缀）
        filename:    OSS 上的实际文件名（含后缀）；不传则默认等于 material_id
        user_id/user_name: 触发首次备份的操作人（即第一次发起重生成的用户），仅在新建备份时记录
        language:    语言维度（audio_producer 用 zh-CN/en-US/ja-JP；其它服务空串）

    Returns:
        True=备份成功或已存在，False=主路径文件不存在（跳过备份）
    """
    # 检查 DB 中是否已有 session_no=0 的记录
    with _engine().connect() as conn:
        row = conn.execute(
            text("""
                SELECT id, prompt FROM public.material_regen_sessions
                WHERE task_id = :tid AND session_no = 0
                  AND node_id = :nid AND material_id = :mid AND language = :lang
            """),
            {"tid": task_id, "nid": node_id, "mid": material_id, "lang": language}
        ).fetchone()
    if row:
        # 已备份；若 prompt 字段为空但现在有值，补填（首次无 new_content，后续有 new_content 时补存旧值）
        if prompt and row[1] is None:
            with _engine().begin() as conn:
                conn.execute(
                    text("""
                        UPDATE public.material_regen_sessions
                        SET prompt = :prompt
                        WHERE task_id = :tid AND session_no = 0
                          AND node_id = :nid AND material_id = :mid AND language = :lang
                    """),
                    {"prompt": prompt, "tid": task_id, "nid": node_id, "mid": material_id, "lang": language}
                )
            logger.info(f"[regen_version] 补填初始备份 prompt: {material_id}")
        return True

    try:
        from common import oss_utils
    except ImportError:
        logger.warning("[regen_version] oss_utils 未安装，跳过初始备份")
        return False

    oss_filename = filename or material_id
    src_key = main_oss_key(task_id, node_id, subdir, oss_filename, language=language)
    try:
        exists = oss_utils.oss_exists(src_key)
    except Exception as e:
        logger.warning(f"[regen_version] OSS 连接失败，跳过初始备份: {src_key}, error={e}")
        return False

    if not exists:
        # 外部组可能只交付热插拔契约中的 storage/outputs 标准产物，没有复制
        # 一组运行时 assets 主路径。首次版本备份允许从标准产物读取。
        standard_key = None
        try:
            from common.node_utils import parse_oss_node_id

            db_node_id, _ = parse_oss_node_id(node_id)
            if service_type == "char_designer" and subdir == "characters":
                standard_key = f"storage/outputs/{task_id}/images/characters/{oss_filename}"
            elif service_type == "scene_painter" and subdir == "scenes":
                standard_key = f"storage/outputs/{task_id}/images/scenes/{oss_filename}"
            elif service_type == "scene_painter" and subdir == "images":
                stored_name = "novel_cover.png" if material_id == "novel_cover" else f"{db_node_id}.png"
                standard_key = f"storage/outputs/{task_id}/images/backgrounds/{stored_name}"
            elif service_type == "audio_producer" and subdir == "voices":
                lang = language or "zh-CN"
                standard_key = (
                    f"storage/outputs/{task_id}/{db_node_id}/audios/"
                    f"{lang}-audio/{oss_filename}"
                )
        except Exception as e:
            logger.warning(f"[regen_version] 标准初始产物路径解析失败: {e}")

        if standard_key:
            try:
                exists = oss_utils.oss_exists(standard_key)
            except Exception:
                exists = False
            if exists:
                logger.info(f"[regen_version] 主路径不存在，改从标准产物备份: {standard_key}")
                src_key = standard_key
        if not exists:
            logger.warning(f"[regen_version] 主路径和标准产物均不存在，跳过初始备份: {src_key}")
            return False

    dst_key = regen_oss_key(task_id, 0, node_id, subdir, oss_filename, language=language)

    # OSS copy
    data = oss_utils.oss_read(src_key)
    if not data:
        logger.warning(f"[regen_version] 读取主路径文件失败: {src_key}")
        return False

    import mimetypes
    content_type, _ = mimetypes.guess_type(oss_filename)
    ok = oss_utils.oss_write(dst_key, data, content_type=content_type or "application/octet-stream")
    if not ok:
        logger.error(f"[regen_version] 初始备份写入 OSS 失败: {dst_key}")
        return False

    # 插入 DB 记录
    _insert_record(task_id, 0, service_type, node_id, material_id, dst_key, prompt,
                   is_active=True, user_id=user_id, user_name=user_name, language=language)
    logger.info(f"[regen_version] 初始备份完成: {dst_key}")
    return True


# ---------------------------------------------------------------------------
# 插入版本记录
# ---------------------------------------------------------------------------

def _insert_record(
    task_id: str,
    session_no: int,
    service_type: str,
    node_id: str,
    material_id: str,
    oss_key: str,
    prompt: Optional[str],
    is_active: bool = False,
    material_version: int = 0,
    user_id: Optional[str] = None,
    user_name: Optional[str] = None,
    status: str = "success",
    error_msg: Optional[str] = None,
    model_name: Optional[str] = None,
    language: str = "",
    voice_id: Optional[str] = None,
) -> int:
    """插入一条版本记录，返回 id"""
    with _engine().begin() as conn:
        row = conn.execute(
            text("""
                INSERT INTO public.material_regen_sessions
                    (task_id, session_no, service_type, node_id, material_id, oss_key, prompt,
                     is_active, material_version, user_id, user_name, status, error_msg, model_name,
                     language, voice_id, created_at)
                VALUES
                    (:tid, :sno, :stype, :nid, :mid, :okey, :prompt,
                     :active, :mver, :uid, :uname, :status, :emsg, :model_name, :lang, :voice_id, NOW())
                ON CONFLICT (task_id, session_no, node_id, material_id, language) DO UPDATE
                    SET oss_key          = EXCLUDED.oss_key,
                        prompt           = EXCLUDED.prompt,
                        material_version = EXCLUDED.material_version,
                        user_id          = EXCLUDED.user_id,
                        user_name        = EXCLUDED.user_name,
                        status           = EXCLUDED.status,
                        error_msg        = EXCLUDED.error_msg,
                        model_name       = EXCLUDED.model_name,
                        voice_id         = EXCLUDED.voice_id
                RETURNING id
            """),
            {
                "tid": task_id, "sno": session_no, "stype": service_type,
                "nid": node_id, "mid": material_id, "okey": oss_key,
                "prompt": prompt, "active": is_active, "mver": material_version,
                "uid": user_id, "uname": user_name,
                "status": status, "emsg": error_msg, "model_name": model_name,
                "lang": language, "voice_id": voice_id,
            }
        ).fetchone()
        return row[0]


def record_regen(
    task_id: str,
    session_no: int,
    service_type: str,
    node_id: str,
    material_id: str,
    oss_key: str,
    prompt: Optional[str] = None,
    user_id: Optional[str] = None,
    user_name: Optional[str] = None,
    status: str = "success",
    error_msg: Optional[str] = None,
    is_active: Optional[bool] = None,
    model_name: Optional[str] = None,
    language: str = "",
    voice_id: Optional[str] = None,
) -> int:
    """
    记录一次重生成结果（生成成功后调用）。
    自动分配 material_version（该素材独立计数）。
    成功的版本默认设为 is_active=True，同时清除同素材其他版本的 is_active；
    失败的版本默认 is_active=False，不影响当前激活状态。

    Args:
        status:     "success" | "failed"
        error_msg:  status="failed" 时的错误描述
        is_active:  显式指定是否激活；None=按 status 自动判定（success→True, failed→False）
        model_name: 本次生成使用的模型名（char_designer/scene_painter 图片模型）
        language:   语言维度（audio_producer 用 zh-CN/en-US/ja-JP；其它服务空串）
        voice_id:   本次生成使用的音色 ID（audio_producer TTS）

    Returns:
        插入记录的 id
    """
    if is_active is None:
        is_active = (status == "success")

    mat_ver = allocate_material_version(task_id, node_id, material_id, language=language)
    if is_active:
        # 仅当本次成功并要激活时，先清除同素材所有版本的 is_active
        with _engine().begin() as conn:
            conn.execute(
                text("""
                    UPDATE public.material_regen_sessions
                    SET is_active = FALSE
                    WHERE task_id = :tid AND node_id = :nid AND material_id = :mid
                      AND language = :lang
                """),
                {"tid": task_id, "nid": node_id, "mid": material_id, "lang": language}
            )
    return _insert_record(
        task_id, session_no, service_type, node_id, material_id,
        oss_key, prompt, is_active=is_active, material_version=mat_ver,
        user_id=user_id, user_name=user_name,
        status=status, error_msg=error_msg, model_name=model_name, language=language,
        voice_id=voice_id,
    )


def find_latest_record(task_id: str, node_id: str, material_id: str, language: str = "") -> Optional[dict]:
    """
    查该素材最新的一条记录（按 material_version 倒序），返回 dict 或 None。
    用于判断"上一版是不是 failed 可以原地复用"。
    """
    with _engine().connect() as conn:
        row = conn.execute(
            text("""
                SELECT id, session_no, service_type, oss_key, prompt, is_active,
                       material_version, status, error_msg, user_id, user_name, language
                FROM public.material_regen_sessions
                WHERE task_id = :tid AND node_id = :nid AND material_id = :mid
                  AND language = :lang
                  AND material_version >= 1
                ORDER BY material_version DESC
                LIMIT 1
            """),
            {"tid": task_id, "nid": node_id, "mid": material_id, "lang": language}
        ).fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "session_no": row[1],
        "service_type": row[2],
        "oss_key": row[3],
        "prompt": row[4],
        "is_active": row[5],
        "material_version": row[6],
        "status": row[7],
        "error_msg": row[8],
        "user_id": row[9],
        "user_name": row[10],
        "language": row[11],
    }


def update_record_to_success(
    record_id: int,
    oss_key: str,
    prompt: Optional[str] = None,
    user_id: Optional[str] = None,
    user_name: Optional[str] = None,
    model_name: Optional[str] = None,
    voice_id: Optional[str] = None,
) -> None:
    """
    把 status=failed 的行原地刷新为 success，并更新 oss_key/prompt/created_at/user。
    用于"重试 failed 行"场景，保持 session_no/material_version 不变。
    成功后自动激活该行（清除同素材其他 is_active）。
    model_name / voice_id 传 None 时保留原值（COALESCE）。
    """
    with _engine().begin() as conn:
        # 先取出 task/node/material/language 用于清理 is_active
        row = conn.execute(
            text("""
                SELECT task_id, node_id, material_id, language
                FROM public.material_regen_sessions
                WHERE id = :rid
            """),
            {"rid": record_id}
        ).fetchone()
        if not row:
            raise ValueError(f"找不到 record_id={record_id} 的记录")
        tid, nid, mid, lang = row[0], row[1], row[2], row[3]

        # 清除同素材所有 is_active
        conn.execute(
            text("""
                UPDATE public.material_regen_sessions
                SET is_active = FALSE
                WHERE task_id = :tid AND node_id = :nid AND material_id = :mid
                  AND language = :lang
            """),
            {"tid": tid, "nid": nid, "mid": mid, "lang": lang}
        )
        # 更新当前行
        conn.execute(
            text("""
                UPDATE public.material_regen_sessions
                SET oss_key    = :okey,
                    prompt     = :prompt,
                    user_id    = COALESCE(:uid, user_id),
                    user_name  = COALESCE(:uname, user_name),
                    model_name = COALESCE(:model_name, model_name),
                    voice_id   = COALESCE(:voice_id, voice_id),
                    status     = 'success',
                    error_msg  = NULL,
                    is_active  = TRUE,
                    created_at = NOW()
                WHERE id = :rid
            """),
            {"okey": oss_key, "prompt": prompt, "uid": user_id, "uname": user_name,
             "model_name": model_name, "voice_id": voice_id, "rid": record_id}
        )
    logger.info(f"[regen_version] 已将 failed 记录 id={record_id} 刷新为 success: {oss_key}")


def find_session_emotions(task_id: str, node_id: str, session_no: int,
                           speaker_id: str, combination_num: str,
                           exclude_emotion: Optional[str] = None,
                           shot: str = "") -> List[dict]:
    """
    查询同一 session_no 下，指定 combination 的所有 emotion 记录（用于 calm 激活时联动 emotion）。

    通过 material_id 前缀匹配 {speaker_id}_{combination_num}_，
    可选排除 calm（exclude_emotion="calm"）以避免自联动。
    char_designer 无语言维度，固定 language=''。

    material_id 兼容两种格式（景别 shot ∈ {CS,MS,LS}）：
      旧：{speaker}_{combo}_{emotion}          → emotion=末段，shot=""
      新：{speaker}_{combo}_{emotion}_{shot}   → emotion=倒数第二段，shot=末段
    shot 非空时只返回同景别的记录（不同景别是不同视角，不互相联动）；
    shot 为空时只返回旧格式（无景别）记录，避免旧调用误联动到新景别记录。

    Returns:
        每条 dict: id / material_id / oss_key / prompt / status / material_version / emotion / shot
    """
    _SHOTS = {"CS", "MS", "LS"}
    prefix = f"{speaker_id}_{combination_num}_"
    with _engine().connect() as conn:
        rows = conn.execute(
            text("""
                SELECT id, material_id, oss_key, prompt, status, material_version
                FROM public.material_regen_sessions
                WHERE task_id = :tid AND node_id = :nid AND session_no = :sno
                  AND language = ''
                  AND material_id LIKE :prefix
            """),
            {"tid": task_id, "nid": node_id, "sno": session_no, "prefix": f"{prefix}%"}
        ).fetchall()
    result = []
    for r in rows:
        mid = r[1]
        # 解析 emotion / shot：末段是景别则 emotion 取倒数第二段
        segs = mid.rsplit("_", 2)  # [.._combo] emotion shot  或  [..] emotion
        if len(segs) == 3 and segs[2] in _SHOTS:
            rec_emotion, rec_shot = segs[1], segs[2]
        else:
            try:
                rec_emotion = mid.rsplit("_", 1)[1]
            except IndexError:
                continue
            rec_shot = ""
        # 景别过滤：只联动同景别（含"都无景别"的旧格式）
        if rec_shot != shot:
            continue
        if exclude_emotion and rec_emotion == exclude_emotion:
            continue
        result.append({
            "id": r[0],
            "material_id": mid,
            "oss_key": r[2],
            "prompt": r[3],
            "status": r[4],
            "material_version": r[5],
            "emotion": rec_emotion,
            "shot": rec_shot,
        })
    return result


# ---------------------------------------------------------------------------
# 本地路径解析
# ---------------------------------------------------------------------------

def _resolve_local_path(outputs_path: str, task_id: str, node_id: str,
                        service_type: str, subdir: str, filename: str,
                        language: str = "") -> Path:
    """
    根据 service_type 和 subdir 映射到各服务实际的本地输出路径。
    node_id 传入 OSS 格式，内部自动转为本地（DB）格式。

    各服务本地路径规则：
      char_designer  / characters → {outputs}/{task_id}/images/characters/{filename}
      scene_painter  / scenes     → {outputs}/{task_id}/images/scenes/{filename}
      scene_painter  / images     → {outputs}/{task_id}/images/backgrounds/{filename}
      audio_producer / voices     → {outputs}/{task_id}/{db_node_id}/audios/{lang}-audio/{filename}
      sound_arranger / music      → {outputs}/{task_id}/{db_node_id}/audios/bgm/{filename}
      sound_arranger / sounds     → {outputs}/{task_id}/{db_node_id}/audios/sfx/{filename}
    """
    from common.node_utils import parse_oss_node_id
    db_node_id, _ = parse_oss_node_id(node_id)
    base = Path(outputs_path)

    if service_type == "char_designer":
        return base / task_id / "images" / "characters" / filename

    if service_type == "scene_painter":
        if subdir == "scenes":
            return base / task_id / "images" / "scenes" / filename
        else:  # images（封面）
            # OSS 上章节封面叫 beijing.png，本地叫 {db_node_id}.png；novel_cover 两边一致
            local_bg_filename = filename if filename == "novel_cover.png" else f"{db_node_id}.png"
            return base / task_id / "images" / "backgrounds" / local_bg_filename

    if service_type == "audio_producer":
        # 多语言：本地音频目录为 audios/{lang}-audio/（lang 默认 zh-CN）
        lang = language or "zh-CN"
        return base / task_id / db_node_id / "audios" / f"{lang}-audio" / filename

    if service_type == "sound_arranger":
        local_subdir = "bgm" if subdir == "music" else "sfx"
        return base / task_id / db_node_id / "audios" / local_subdir / filename

    # 兜底：保持旧行为
    return base / task_id / db_node_id / "assets" / subdir / filename


def _write_sound_local(outputs_path: str, task_id: str, node_id: str,
                       subdir: str, material_id: str, data: bytes) -> Path:
    """
    BGM/SFX 本地写入：固定使用 {material_id}.mp3 作为本地文件名（与 generate_sound.py
    最终输出命名保持一致），与 OSS regenerate 上的文件名（可能带哈希）解耦。

    流程：
      1. 按 material_id 末尾的时间轴后缀（如 1-27）通配匹配目录下所有同后缀文件并删除
         （兼容历史遗留的 *_<hash>_<suffix>.mp3、以及现行的 {material_id}.mp3）
      2. 写入新文件 {material_id}.mp3

    node_id 传入 OSS 格式，内部自动转为本地（DB）格式。
    返回实际写入的本地路径。
    """
    import glob as _glob
    import re as _re
    from common.node_utils import parse_oss_node_id

    db_node_id, _ = parse_oss_node_id(node_id)
    base = Path(outputs_path)
    local_subdir = "bgm" if subdir == "music" else "sfx"
    target_dir = base / task_id / db_node_id / "audios" / local_subdir
    target_dir.mkdir(parents=True, exist_ok=True)

    # 从 material_id 末尾提取时间轴后缀（如 "1-27"）
    m = _re.search(r"_(\d+-\d+)$", material_id)
    if m:
        timeline_suffix = m.group(1)
        # 删除同时间轴后缀的所有旧文件（无论是新格式 {material_id}.mp3 还是旧的带哈希格式）
        for old in _glob.glob(str(target_dir / f"*_{timeline_suffix}.mp3")):
            try:
                Path(old).unlink()
            except Exception:
                pass

    dst = target_dir / f"{material_id}.mp3"
    dst.write_bytes(data)
    return dst


# ---------------------------------------------------------------------------
# 激活（确认使用）
# ---------------------------------------------------------------------------

def _resolve_main_oss_filename(service_type: str, subdir: str, material_id: str) -> str:
    """
    根据 service_type / subdir / material_id 推断 OSS 主路径上的实际文件名。

    各服务 OSS 主路径文件名规则：
      char_designer  / characters  → {material_id}.png
      scene_painter  / scenes      → {material_id}.png
      scene_painter  / images      → beijing.png（章节封面）/ novel_cover.png（小说总封面）
      audio_producer / voices      → {material_id}.wav
      sound_arranger / music|sounds → 主路径可能存在带哈希的历史文件，本函数返回新规范名 {material_id}.mp3，
                                       清理工作由 _overwrite_main_oss 处理（按 timeline 后缀通配删除旧文件）
    """
    if service_type == "char_designer":
        return f"{material_id}.png"
    if service_type == "scene_painter":
        if subdir == "scenes":
            return f"{material_id}.png"
        # images（封面）
        return "novel_cover.png" if material_id == "novel_cover" else "beijing.png"
    if service_type == "audio_producer":
        return f"{material_id}.wav"
    if service_type == "sound_arranger":
        return f"{material_id}.mp3"
    # 兜底
    return material_id


def _overwrite_main_oss(
    service_type: str,
    task_id: str,
    node_id: str,
    subdir: str,
    material_id: str,
    data: bytes,
    language: str = "",
) -> tuple:
    """
    把激活版本的内容覆盖写到 OSS 主路径。

    - char_designer / scene_painter / audio_producer：直接 oss_write 到固定文件名
      （audio_producer 主路径含 {language} 子层：assets/voices/{lang}/{filename}）
    - sound_arranger（BGM/SFX）：先按 timeline 后缀通配清理同后缀旧文件（含历史带哈希格式），
      再写入 {material_id}.mp3，保证主路径同时间轴只有一份
    - audio_producer 的字幕 .txt 由 restore_audio_metadata 单独处理，本函数不管

    Returns:
        (ok: bool, main_key: str, error: str|None)
    """
    try:
        from common import oss_utils
    except ImportError:
        return False, "", "oss_utils 未安装"

    main_filename = _resolve_main_oss_filename(service_type, subdir, material_id)
    main_key = main_oss_key(task_id, node_id, subdir, main_filename, language=language)

    # BGM/SFX：先清理 OSS 主路径下同时间轴的所有旧文件
    if service_type == "sound_arranger":
        import re as _re
        m = _re.search(r"_(\d+-\d+)$", material_id)
        if m:
            timeline_suffix = m.group(1)
            scan_prefix = f"{task_id}/{node_id}/assets/{subdir}/"
            try:
                old_keys = [
                    k for k in oss_utils.oss_list_keys(scan_prefix)
                    if k.endswith(f"_{timeline_suffix}.mp3") and k != main_key
                ]
                for old in old_keys:
                    try:
                        oss_utils.oss_delete(old)
                        logger.info(f"[regen_version] 已清理 OSS 主路径同后缀旧文件: {old}")
                    except Exception as e:
                        logger.warning(f"[regen_version] 清理 OSS 旧文件失败 {old}: {e}")
            except Exception as e:
                logger.warning(f"[regen_version] 列举 OSS 旧文件失败: prefix={scan_prefix}, error={e}")

    # 写入主路径
    import mimetypes
    content_type, _ = mimetypes.guess_type(main_filename)
    ok = oss_utils.oss_write(main_key, data, content_type=content_type or "application/octet-stream")
    if not ok:
        return False, main_key, f"覆盖 OSS 主路径失败: {main_key}"
    logger.info(f"[regen_version] 已覆盖 OSS 主路径: {main_key}")
    return True, main_key, None


def _activate_one_record(
    task_id: str,
    session_no: int,
    node_id: str,
    material_id: str,
    subdir: Optional[str] = None,
    language: str = "",
) -> dict:
    """
    激活单条版本记录的核心逻辑：从 OSS regenerate 下载 → 写本地 + 写 OSS 主路径 → 更新 DB is_active。
    不做任何联动，仅处理 (task_id, session_no, node_id, material_id, language) 这一行。
    """
    try:
        from common import oss_utils
    except ImportError:
        return {"status": "failed", "local_path": None, "error": "oss_utils 未安装"}

    # 查 DB 确认版本存在，同时取 oss_key / service_type / prompt / status
    with _engine().connect() as conn:
        row = conn.execute(
            text("""
                SELECT oss_key, service_type, prompt, status FROM public.material_regen_sessions
                WHERE task_id = :tid AND session_no = :sno
                  AND node_id = :nid AND material_id = :mid AND language = :lang
            """),
            {"tid": task_id, "sno": session_no, "nid": node_id, "mid": material_id, "lang": language}
        ).fetchone()

    if not row:
        return {"status": "failed", "local_path": None,
                "error": f"session_no={session_no} material_id={material_id} 的版本记录不存在"}

    src_key, service_type, saved_prompt, rec_status = row[0], row[1], row[2], row[3]

    # failed 行不能激活（没有可用的 OSS 文件）
    if rec_status == "failed":
        return {"status": "failed", "local_path": None,
                "error": f"该版本生成失败（status=failed），无法激活：{material_id} session_no={session_no}"}

    # 从 oss_key 解析 subdir
    if not subdir:
        try:
            subdir = src_key.split("/assets/")[1].split("/")[0]
        except (IndexError, AttributeError):
            return {"status": "failed", "local_path": None,
                    "error": f"无法从 oss_key 解析 subdir: {src_key}"}

    # 从 OSS 下载文件内容
    data = oss_utils.oss_read(src_key)
    if not data:
        return {"status": "failed", "local_path": None, "error": f"读取 OSS 文件失败: {src_key}"}

    actual_filename = src_key.split("/")[-1]

    # 写入本地
    from common.config import OUTPUTS_PATH
    if service_type == "sound_arranger":
        local_path = _write_sound_local(OUTPUTS_PATH, task_id, node_id, subdir, material_id, data)
    else:
        local_path = _resolve_local_path(OUTPUTS_PATH, task_id, node_id, service_type, subdir,
                                          actual_filename, language=language)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            local_path.write_bytes(data)
        except Exception as e:
            return {"status": "failed", "local_path": None, "error": f"写入本地文件失败: {e}"}

    # 同步覆盖 OSS 主路径
    main_oss_ok, main_oss_key_, main_oss_err = _overwrite_main_oss(
        service_type, task_id, node_id, subdir, material_id, data, language=language
    )
    if not main_oss_ok:
        logger.error(f"[regen_version] 警告：OSS 主路径覆盖失败，本地已激活: {main_oss_err}")

    # 同步镜像到 OSS storage/outputs/ 路径（game_builder 构建时优先从此拉取素材）。
    # 不同步的话，game_builder 的 fetch_task_from_oss 会用镜像里的旧版本覆盖回本地，激活失效。
    try:
        from common.output_mirror import mirror_paths
        mirror_paths([local_path], force=True)
    except Exception as e:
        logger.warning(f"[regen_version] 镜像激活版本到 storage/outputs 失败（本地已激活）: {e}")

    # 更新 DB：清除同素材其他 is_active，设置当前
    with _engine().begin() as conn:
        conn.execute(
            text("""
                UPDATE public.material_regen_sessions
                SET is_active = FALSE
                WHERE task_id = :tid AND node_id = :nid AND material_id = :mid AND language = :lang
            """),
            {"tid": task_id, "nid": node_id, "mid": material_id, "lang": language}
        )
        conn.execute(
            text("""
                UPDATE public.material_regen_sessions
                SET is_active = TRUE
                WHERE task_id = :tid AND session_no = :sno
                  AND node_id = :nid AND material_id = :mid AND language = :lang
            """),
            {"tid": task_id, "sno": session_no, "nid": node_id, "mid": material_id, "lang": language}
        )

    logger.info(f"[regen_version] 激活成功: {material_id} session_no={session_no} lang={language or '-'} → {local_path}")
    return {
        "status": "success",
        "local_path": str(local_path),
        "main_oss_key": main_oss_key_ if main_oss_ok else None,
        "main_oss_error": main_oss_err,
        "service_type": service_type,
        "saved_prompt": saved_prompt,
        "language": language,
        "error": None,
    }


def activate_version(
    task_id: str,
    session_no: int,
    node_id: str,
    material_id: str,
    subdir: Optional[str] = None,
    language: str = "",
) -> dict:
    """
    激活指定版本：将 regenerate/{session_no}/... 的文件下载到本地 OUTPUTS_PATH，
    **同时覆盖 OSS 主路径**（保证 OSS 主路径和本地始终是当前激活版本），
    **并镜像到 OSS storage/outputs/ 路径**（game_builder 构建时优先从此拉取素材，
    否则会用镜像里的旧版本覆盖回本地导致激活失效），
    并更新 DB 中 is_active 标记。

    联动逻辑：当 service_type=char_designer 且 emotion=calm 时，自动联动激活
    同 session_no 下该 combination 的所有 emotion 记录（status=success 的才联动）。

    session_no=0 表示回滚到初始版本。

    Returns:
        {"status": "success"|"failed", "local_path": str, "main_oss_key": str,
         "linked_emotions": [...] (calm 联动时有), "error": str|None}
    """
    main_result = _activate_one_record(task_id, session_no, node_id, material_id, subdir, language=language)
    if main_result["status"] == "failed":
        return main_result

    # === 联动：char_designer 的 calm 激活时，连带激活同 session_no 下的 emotion 行 ===
    linked_emotions = []
    service_type = main_result.get("service_type")
    if service_type == "char_designer":
        try:
            import re as _re
            # 兼容旧(Name_1-1-1_calm)与新(Name_1-1-1_calm_LS，末段为景别 CS/MS/LS)两种格式
            m_new = _re.match(r"^(.+?)_(\d+-\d+-\d+)_([^_]+)_(CS|MS|LS)$", material_id)
            m_old = _re.match(r"^(.+?)_(\d+-\d+-\d+)_([^_]+)$", material_id)
            m = m_new or m_old
            cur_emotion = m.group(3) if m else None
            cur_shot = m_new.group(4) if m_new else ""
            if m and cur_emotion == "calm":
                speaker_id = m.group(1)
                combination_num = m.group(2)
                # 只联动同景别的其它 emotion（景别不同视角不同，不应互相联动）
                emotions = find_session_emotions(
                    task_id, node_id, session_no,
                    speaker_id, combination_num,
                    exclude_emotion="calm",
                    shot=cur_shot,
                )
                for emo_rec in emotions:
                    if emo_rec["status"] == "failed":
                        linked_emotions.append({
                            "material_id": emo_rec["material_id"],
                            "emotion": emo_rec["emotion"],
                            "status": "skipped",
                            "reason": f"该 session_no={session_no} 版本生成失败 (status=failed)",
                        })
                        continue
                    sub_result = _activate_one_record(
                        task_id, session_no, node_id, emo_rec["material_id"], subdir, language=language
                    )
                    linked_emotions.append({
                        "material_id": emo_rec["material_id"],
                        "emotion": emo_rec["emotion"],
                        "status": sub_result["status"],
                        "local_path": sub_result.get("local_path"),
                        "error": sub_result.get("error"),
                    })
                logger.info(
                    f"[regen_version] calm 激活联动 emotion: speaker={speaker_id} "
                    f"combination={combination_num} session_no={session_no} 共 {len(linked_emotions)} 个 emotion"
                )
        except Exception as e:
            logger.warning(f"[regen_version] 联动 emotion 激活异常（不影响 calm 激活）: {e}")

    main_result["linked_emotions"] = linked_emotions
    return main_result


def rollback_version(
    task_id: str,
    node_id: str,
    material_id: str,
    current_material_version: int,
    language: str = "",
) -> dict:
    """
    回滚到上一个版本：找到 current_material_version - 1 对应的记录并激活。
    current_material_version=1 时回滚到 session_no=0（原始版本）。

    Args:
        task_id:                  任务 ID
        node_id:                  OSS 格式 node_id
        material_id:              素材 ID（不含后缀）
        current_material_version: 当前版本号（前端传入，回滚到 version-1）
        language:                 语言维度（audio_producer 用 zh-CN/en-US/ja-JP；其它服务空串）

    Returns:
        activate_version 的返回值，或 {"status": "failed", ...}
    """
    prev_version = current_material_version - 1

    if prev_version < 0:
        return {"status": "failed", "local_path": None,
                "error": "已是最早版本，无法继续回滚"}

    if prev_version == 0:
        # 回滚到原始版本（session_no=0）
        target_session_no = 0
    else:
        # 查找 material_version = prev_version 对应的 session_no
        with _engine().connect() as conn:
            row = conn.execute(
                text("""
                    SELECT session_no FROM public.material_regen_sessions
                    WHERE task_id = :tid AND node_id = :nid AND material_id = :mid
                      AND language = :lang
                      AND material_version = :mver
                """),
                {"tid": task_id, "nid": node_id, "mid": material_id, "lang": language, "mver": prev_version}
            ).fetchone()
        if not row:
            return {"status": "failed", "local_path": None,
                    "error": f"找不到 material_version={prev_version} 的版本记录"}
        target_session_no = row[0]

    return activate_version(task_id, target_session_no, node_id, material_id, language=language)

# ---------------------------------------------------------------------------
# 查询历史
# ---------------------------------------------------------------------------

def get_history(task_id: str, node_id: Optional[str] = None,
                material_id: Optional[str] = None,
                language: Optional[str] = None) -> List[dict]:
    """
    查询重生成历史记录（含 session_no=0 的原始版本）。

    Args:
        task_id:     任务 ID
        node_id:     可选，过滤指定章节
        material_id: 可选，过滤指定素材
        language:    可选，过滤指定语言（audio_producer：zh-CN/en-US/ja-JP）；
                     传 None 不过滤，传 '' 精确匹配无语言维度的素材

    Returns:
        记录列表，按 node_id / material_id / language / material_version ASC 排序
    """
    conditions = ["task_id = :tid"]
    params: dict = {"tid": task_id}
    if node_id:
        conditions.append("node_id = :nid")
        params["nid"] = node_id
    if material_id:
        conditions.append("material_id = :mid")
        params["mid"] = material_id
    if language is not None:
        conditions.append("language = :lang")
        params["lang"] = language

    where = " AND ".join(conditions)
    with _engine().connect() as conn:
        rows = conn.execute(
            text(f"""
                SELECT session_no, service_type, node_id, material_id,
                       oss_key, prompt, is_active, material_version,
                       user_id, user_name, status, error_msg, model_name, language, voice_id, created_at
                FROM public.material_regen_sessions
                WHERE {where}
                ORDER BY node_id, material_id, language, material_version ASC
            """),
            params
        ).fetchall()

    try:
        from common import oss_utils
        def _url(key): return oss_utils.oss_public_url(key)
    except ImportError:
        def _url(key): return key

    return [
        {
            "session_no":        r[0],
            "service_type":      r[1],
            "node_id":           r[2],
            "material_id":       r[3],
            "oss_key":           r[4],
            "url":               _url(r[4]),
            "prompt":            r[5],
            "is_active":         r[6],
            "material_version":  r[7],
            "version_str":       version_str(r[7]),
            "user_id":           r[8],
            "user_name":         r[9],
            "status":            r[10],
            "error_msg":         r[11],
            "model_name":        r[12],
            "language":          r[13],
            "voice_id":          r[14],
            "created_at":        r[15].isoformat() if r[15] else None,
        }
        for r in rows
    ]


def get_current_session_no(task_id: str) -> int:
    """返回该 task 当前最大 session_no（不含 0），无记录返回 0"""
    with _engine().connect() as conn:
        row = conn.execute(
            text("""
                SELECT COALESCE(MAX(session_no), 0)
                FROM public.material_regen_sessions
                WHERE task_id = :tid AND session_no >= 1
            """),
            {"tid": task_id}
        ).fetchone()
    return row[0]


def get_session_no_by_version(task_id: str, node_id: str, material_id: str,
                               material_version: int, language: str = "") -> Optional[int]:
    """
    根据 material_version 查对应的 session_no。
    material_version=0 直接返回 0（原始版本固定为 session_no=0）。
    找不到返回 None。
    """
    if material_version == 0:
        return 0
    with _engine().connect() as conn:
        row = conn.execute(
            text("""
                SELECT session_no FROM public.material_regen_sessions
                WHERE task_id = :tid AND node_id = :nid AND material_id = :mid
                  AND language = :lang
                  AND material_version = :mver
            """),
            {"tid": task_id, "nid": node_id, "mid": material_id, "lang": language, "mver": material_version}
        ).fetchone()
    return row[0] if row else None
