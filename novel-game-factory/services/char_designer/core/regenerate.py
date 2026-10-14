#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
char_designer 素材重生成接口

【层级】产品层，前端"重新生成"按钮的后端实现。
        负责：失败重试（3 次×5s）、初始备份、版本化 OSS 路径、material_regen_sessions 记账、
              OSS 主路径覆盖、calm 成功后联动重生所有 emotion、提示词更新与回滚。
        绘图能力内聚在本文件，仅使用 char_designer 模块内的图像能力。

参数规范：
- task_id + node_id（OSS 格式）+ material_id（文件名，如 Liang_1-1-4_calm.png）
- material_id 包含完整信息：{speaker_id}_{combination_num}_{emotion}.png
- 支持 new_prompt 修改该 combination 的 positive_prompt
- emotion=calm → 本文件内文生图/变体图生图
- emotion!=calm → 本文件内以 calm 图为基图生成情绪图

重生成只针对已存在的素材，能传进来 material_id 就说明该文件在 OSS 上存在。
生成结果写入本地 OUTPUTS_PATH，再上传到 OSS regenerate 路径。
"""

import os
import sys
import json
import re
import time
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from common.config import DATABASE_URL, OUTPUTS_PATH, PLATFORM_DATABASE_URL
from common.visual_style import (
    ASSET_CHARACTER,
    compose_anchor_guided_prompt,
    ensure_asset_style,
    resolve_asset_style_plan,
)
from common.node_utils import parse_oss_node_id
from common import regen_version
from sqlalchemy import create_engine, text

# char_designer 模块内的绘图叶子依赖
from services.char_designer.core.generate_calm import (
    DEFAULT_SHOT,
    _parse_combination_str,
    _init_conversion_cache,
    chinese_to_camel_case,
)
from services.char_designer.core.emotion_prompt_builder import (
    EMOTIONS,
    has_personality_content,
    modify_prompt_for_emotion,
    normalize_personality,
    select_personality_variant,
)
from services.char_designer.core.generate_variant_calm import IDENTITY_CONSISTENCY_PREFIX
from services.char_designer.image_providers.gpt_image import GPTImageEditProvider, GPTImageTextProvider
from services.char_designer.image_providers.qwen_image import QwenImageEditProvider, QwenTextImageProvider
from services.char_designer.core.artifact_contract import validate_image_file
from services.char_designer.utils.native_transparency import (
    CharacterOutputPolicy,
    resolve_character_output_policy,
    validator_for_policy,
)

try:
    from common.usage_tracker import tracker as _usage_tracker
except Exception:
    _usage_tracker = None

try:
    from common import oss_utils
except ImportError:
    oss_utils = None

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# material_id 解析
# ---------------------------------------------------------------------------

def _parse_material_id(material_id: str) -> tuple:
    """
    解析 material_id，返回 (speaker_id, combination_num, emotion, shot)。
    兼容两种格式（shot=景别 CS/MS/LS，旧格式无景别时 shot=""）：
      旧：{speaker_id}_{p-b-c}_{emotion}            例：Liang_1-1-4_calm      → ("Liang","1-1-4","calm","")
      新：{speaker_id}_{p-b-c}_{emotion}_{shot}     例：Liang_1-1-4_calm_LS   → ("Liang","1-1-4","calm","LS")

    景别集合固定为 {CS, MS, LS}：末段命中才当景别，否则按旧格式把末段当 emotion，
    避免把本身含下划线（理论上不该有）或未来其它后缀误判成景别。
    """
    name = material_id.replace(".png", "")  # 兼容带后缀的旧调用
    # 新格式：4 段，末段是景别
    m_new = re.match(r"^(.+?)_(\d+-\d+-\d+)_([^_]+)_(CS|MS|LS)$", name)
    if m_new:
        return m_new.group(1), m_new.group(2), m_new.group(3), m_new.group(4)
    # 旧格式：3 段，无景别
    m_old = re.match(r"^(.+?)_(\d+-\d+-\d+)_([^_]+)$", name)
    if m_old:
        return m_old.group(1), m_old.group(2), m_old.group(3), ""
    raise ValueError(f"无法解析 material_id: {material_id}，期望格式 "
                     f"{{speaker_id}}_{{p-b-c}}_{{emotion}}[_{{CS|MS|LS}}]")


def _build_char_material_id(speaker_id: str, combination_num: str, emotion: str, shot: str = "") -> str:
    """按 shot 是否存在拼接角色 material_id（不含后缀）。shot 为空走旧格式。"""
    base = f"{speaker_id}_{combination_num}_{emotion}"
    return f"{base}_{shot}" if shot else base


# ---------------------------------------------------------------------------
# 模型选择：model_name → (image_provider, calm 文生图 model, emotion 图生图 model)
# ---------------------------------------------------------------------------

# 支持的图片模型
SUPPORTED_MODELS = {"gpt-image-2", "qwen-image-plus", "wan2.5-t2i-preview"}


def _resolve_provider(model_name: Optional[str]) -> tuple:
    """
    把入参 model_name 映射为本文件绘图逻辑需要的
    (image_provider, calm_model, emotion_model)。

    - qwen-image-plus → provider=qwen；calm 用 qwen-image-plus；
      emotion(图生图) 传 None，走 provider 默认 qwen-image-edit-plus（不能把文生图模型塞给图生图）
    - gpt-image-2     → provider=gpt；calm/emotion 都用 gpt-image-2
    - None / 不识别   → (None, None, None)：使用本文件的既定默认（默认 gpt）

    Returns:
        (image_provider|None, calm_model|None, emotion_model|None)
    """
    if not model_name:
        return None, None, None
    if model_name == "qwen-image-plus":
        return "qwen", "qwen-image-plus", None
    if model_name == "wan2.5-t2i-preview":
        # calm 使用 Wan2.5 文生图；表情/变体仍统一走 Qwen-Image-Edit。
        return "wan", "wan2.5-t2i-preview", None
    if model_name == "gpt-image-2":
        return "gpt", "gpt-image-2", "gpt-image-2"
    logger.warning(f"[char_regen] 不识别的 model_name={model_name}，回退到系统默认 provider")
    return None, None, None


# ---------------------------------------------------------------------------
# OSS 路径构建
# ---------------------------------------------------------------------------

def _oss_key(task_id: str, node_id: str, filename: str) -> str:
    return f"{task_id}/{node_id}/assets/characters/{filename}"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _engine():
    return create_engine(DATABASE_URL)


def _platform_engine():
    """character_prompts 已迁平台库(agent_platform);material_regen_sessions 仍在业务库"""
    return create_engine(PLATFORM_DATABASE_URL)


def _combination_suffix(value: object) -> str:
    """兼容 `角色名-1-2-3`、`1-2-3` 与下划线分隔的组合编号。"""
    match = re.search(r"(?:^|[-_])(\d+)[-_](\d+)[-_](\d+)$", str(value or "").strip())
    return "-".join(match.groups()) if match else ""


def _prompt_record_matches(item: dict, speaker_id: str, combination_num: str) -> bool:
    record_speaker_id = str(
        item.get("speaker_id")
        or item.get("logical_character_id")
        or item.get("character_id")
        or ""
    ).strip()
    if record_speaker_id:
        if record_speaker_id != speaker_id:
            return False
    else:
        combination_name = str(item.get("combination") or "").rsplit("-", 3)[0]
        names = [item.get("name"), combination_name]
        if not any(
            name and (str(name) == speaker_id or chinese_to_camel_case(str(name)) == speaker_id)
            for name in names
        ):
            return False
    return _combination_suffix(
        item.get("combination_num")
        or item.get("combination")
        or item.get("material_id")
    ) == combination_num


def _prompt_value(item: dict) -> Optional[str]:
    return item.get("positive_prompt") or item.get("prompt") or item.get("image_prompt")


def _fetch_prompt_record_by_combination(
    task_id: str,
    speaker_id: str,
    combination_num: str,
) -> Optional[Dict]:
    """按角色和组合编号读取完整 prompt Contract，并兼容外部管线字段。"""
    with _platform_engine().connect() as conn:
        row = conn.execute(
            text("SELECT data FROM character_prompts WHERE task_id = :tid"),
            {"tid": task_id}
        ).fetchone()
    if not row:
        return None
    items = row[0] if isinstance(row[0], list) else json.loads(row[0])
    for item in items:
        if isinstance(item, dict) and _prompt_record_matches(item, speaker_id, combination_num):
            return item
    return None


def _fetch_prompt_by_combination(task_id: str, speaker_id: str, combination_num: str) -> Optional[str]:
    """从 character_prompts 表按组合读取兼容格式的图片提示词。"""
    record = _fetch_prompt_record_by_combination(task_id, speaker_id, combination_num)
    prompt = _prompt_value(record) if record else None
    return str(prompt) if prompt else None


def _update_prompt_by_combination(task_id: str, speaker_id: str, combination_num: str, new_prompt: str) -> bool:
    """按 speaker_id + combination 数字部分更新 positive_prompt，返回是否找到并更新。"""
    with _platform_engine().begin() as conn:
        row = conn.execute(
            text("SELECT data FROM character_prompts WHERE task_id = :tid"),
            {"tid": task_id}
        ).fetchone()
        if not row:
            return False
        items = row[0] if isinstance(row[0], list) else json.loads(row[0])
        updated = False
        for item in items:
            if isinstance(item, dict) and _prompt_record_matches(item, speaker_id, combination_num):
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


def _fetch_emotions_by_combination(task_id: str, speaker_id: str, combination_num: str) -> list:
    """从 character_prompts 表读取该 combination 的 emotions 列表，找不到返回 []。"""
    record = _fetch_prompt_record_by_combination(task_id, speaker_id, combination_num)
    return (record.get("emotions", []) or []) if record else []


def _personality_for_combination(
    task_id: str,
    speaker_id: str,
    combination_num: str,
) -> Dict:
    record = _fetch_prompt_record_by_combination(task_id, speaker_id, combination_num)
    personality = normalize_personality(record.get("personality") if record else None)
    if has_personality_content(personality):
        return personality

    char_info = get_character_by_speaker_id(task_id, speaker_id)
    if not char_info:
        return personality
    combination = (
        str(record.get("combination") or "")
        if record
        else f"{char_info['name']}-{combination_num}"
    )
    personality = select_personality_variant(char_info["data"], combination)
    if has_personality_content(personality):
        logger.warning(
            f"speaker={speaker_id} combination={combination_num} 的 character_prompts "
            "缺少 personality，本次从 characters 做只读 legacy 回退；建议重跑 Step 8 --force"
        )
    return personality


def _emotion_prompt_for_combination(
    task_id: str,
    speaker_id: str,
    combination_num: str,
    emotion: str,
) -> str:
    personality = _personality_for_combination(task_id, speaker_id, combination_num)
    prompt = modify_prompt_for_emotion("", emotion, personality=personality)
    logger.info(
        f"[char_regen] speaker={speaker_id} combination={combination_num} emotion={emotion} "
        f"personality_id={personality.get('personality_id')} prompt={prompt}"
    )
    return prompt


def _emotion_edit_prompt(
    task_id: str,
    speaker_id: str,
    combination_num: str,
    emotion: str,
    character_prompt: Optional[str] = None,
) -> str:
    """组合角色外观与性格化情绪要求，确保编辑提示词和人物身份都实际生效。"""
    emotion_prompt = _emotion_prompt_for_combination(
        task_id, speaker_id, combination_num, emotion
    )
    base_prompt = character_prompt or _fetch_prompt_by_combination(
        task_id, speaker_id, combination_num
    )
    if not base_prompt:
        return ensure_asset_style(task_id, ASSET_CHARACTER, emotion_prompt)
    return ensure_asset_style(
        task_id,
        ASSET_CHARACTER,
        f"{IDENTITY_CONSISTENCY_PREFIX}{base_prompt}\n{emotion_prompt}",
    )


# ---------------------------------------------------------------------------
# 公开接口
# ---------------------------------------------------------------------------

def regenerate_character(
    task_id: str,
    node_id: str,
    material_id: str,
    new_prompt: Optional[str] = None,
    force: bool = True,
    session_no: Optional[int] = None,
    user_id: Optional[str] = None,
    user_name: Optional[str] = None,
    model_name: Optional[str] = None,
    on_main_done=None,
) -> dict:
    """
    重生成角色图片。

    行为：
    - 单条 material 重生成内部最多 3 次重试（每次失败间隔 5s）
    - 当 emotion=calm 时：calm 成功后联动同 combination 下 character_prompts.emotions 里的所有 emotion，
      用新 calm 做基图逐个图生图（每张内部各 3 次重试，独立成功/失败）
    - 任何成功的图都会立即：写入本地 + 覆盖 OSS 主路径 + 写一行 status=success 记录
    - 失败的图：保持本地/OSS 主路径不变 + 写一行 status=failed 记录（记 error_msg）
    - 重试 failed 行：调用方传同一 material_id，后端检测到上一版是 failed → 复用同一行原地刷新
      （不递增 material_version，不分配新 session_no）

    Args:
        on_main_done: 可选回调，主图（calm 或单张 emotion）一出结果就立即调用一次，
            早于 emotion 联动。入参 (main_item: dict, session_no: int)。
            用途：让上层 Celery task 提前把 calm 结果发布出去，前端轮询即可拿到，
            不必等所有 emotion 联动跑完。回调内部异常会被吞掉，不影响生成主流程。

    Returns:
        {"total": int, "success": int, "failed": int, "session_no": int, "items": [...], "errors": [...]}
    """
    if oss_utils is None:
        return {"total": 0, "success": 0, "failed": 1, "items": [], "errors": ["oss_utils 未安装或导入失败"]}
    try:
        oss_utils.init_oss_client()
    except Exception as e:
        return {"total": 0, "success": 0, "failed": 1, "items": [], "errors": [f"OSS 客户端初始化失败: {e}"]}

    # 解析 material_id（shot=景别，旧格式为空）
    try:
        speaker_id, combination_num, emotion, shot = _parse_material_id(material_id)
    except ValueError as e:
        return {"total": 0, "success": 0, "failed": 1, "items": [], "errors": [str(e)]}

    # 校验 model_name（可选，不传则用系统默认）
    if model_name and model_name not in SUPPORTED_MODELS:
        return {"total": 0, "success": 0, "failed": 1, "items": [],
                "errors": [f"不支持的 model_name={model_name}，仅支持 {sorted(SUPPORTED_MODELS)}"]}

    material_id_clean = material_id.replace(".png", "")
    filename = f"{material_id_clean}.png"
    _, oss_node_id = parse_oss_node_id(node_id)

    old_prompt = _fetch_prompt_by_combination(task_id, speaker_id, combination_num)

    # 若提供新 prompt，写库和本次 provider 请求都保存同一份最终合同。
    if new_prompt:
        new_prompt = ensure_asset_style(task_id, ASSET_CHARACTER, new_prompt)
        ok = _update_prompt_by_combination(task_id, speaker_id, combination_num, new_prompt)
        if not ok:
            return {"total": 0, "success": 0, "failed": 1, "items": [],
                    "errors": [f"更新提示词失败: speaker_id={speaker_id} combination={combination_num}"]}

    if emotion == "calm":
        backup_prompt = old_prompt
        used_prompt = ensure_asset_style(
            task_id,
            ASSET_CHARACTER,
            new_prompt or old_prompt or "",
        )
    else:
        used_prompt = _emotion_edit_prompt(
            task_id,
            speaker_id,
            combination_num,
            emotion,
            character_prompt=new_prompt or old_prompt,
        )
        backup_prompt = used_prompt

    # force=False 时检查 OSS 主路径已存在
    main_oss_key_check = _oss_key(task_id, oss_node_id, filename)
    if not force and oss_utils.oss_exists(main_oss_key_check) and oss_utils.oss_get_size(main_oss_key_check) > 0:
        return {
            "total": 1, "success": 0, "failed": 0,
            "items": [{"material_id": material_id_clean, "material_subtype": "character",
                       "status": "skipped", "path": oss_utils.oss_public_url(main_oss_key_check), "error": None}],
            "errors": []
        }

    # 检查上一版是否为 failed → 重试模式（复用 session_no 和 material_version）
    retry_record = None
    latest = regen_version.find_latest_record(task_id, oss_node_id, material_id_clean)
    if latest and latest["status"] == "failed":
        retry_record = latest
        logger.info(f"[char_regen] 检测到上一版 failed，进入重试模式: id={latest['id']} session_no={latest['session_no']}")
        session_no = latest["session_no"]
    else:
        # 正常路径：分配新 session_no
        if session_no is None:
            session_no = regen_version.allocate_session_no(task_id)

    # 初始备份（calm 行触发即可，emotion 联动不重复触发）
    regen_version.ensure_initial_backup(
        task_id, "char_designer", oss_node_id, material_id_clean, "characters", backup_prompt,
        filename=filename, user_id=user_id, user_name=user_name
    )

    # === 生成主 material（calm 或 emotion）===
    items = []
    main_item = _generate_with_retry_and_record(
        task_id=task_id, oss_node_id=oss_node_id,
        speaker_id=speaker_id, combination_num=combination_num,
        emotion=emotion, session_no=session_no,
        used_prompt=used_prompt,
        retry_record_id=retry_record["id"] if retry_record else None,
        retry_material_version=retry_record["material_version"] if retry_record else None,
        user_id=user_id, user_name=user_name,
        model_name=model_name,
        shot=shot,
    )
    items.append(main_item)

    # 是否会有 emotion 联动：只有 calm 成功且该 combination 下确实配了其它 emotion 才会联动
    will_chain_emotions = False
    emotions_list = []
    if emotion == "calm" and main_item["status"] == "success":
        emotions_list = _fetch_emotions_by_combination(task_id, speaker_id, combination_num)
        will_chain_emotions = any(e and e != "calm" for e in emotions_list)

    # 主图已出结果：先抛给上层发布，让前端立刻拿到 calm 的消息，不必等 emotion 联动跑完
    if on_main_done is not None:
        try:
            on_main_done(main_item, session_no, will_chain_emotions)
        except Exception as e:
            logger.warning(f"[char_regen] on_main_done 回调失败（不影响主流程）: {e}")

    # === 当 emotion=calm 时联动其他 emotion ===
    if will_chain_emotions:
        other_emotions = [e for e in emotions_list if e and e != "calm"]
        logger.info(
            f"[char_regen] calm 成功，联动 emotion: {other_emotions} "
            f"speaker={speaker_id} combination={combination_num} session_no={session_no}"
        )
        for emo in other_emotions:
            # emotion 与 calm 共享同一景别（景别是立绘视角，不随情绪变）
            emo_material_id = _build_char_material_id(speaker_id, combination_num, emo, shot)
            emo_prompt = _emotion_edit_prompt(
                task_id, speaker_id, combination_num, emo
            )
            # emotion 也支持 failed 重试（复用同 session_no 下的 failed 行）
            emo_retry = None
            with _engine().connect() as conn:
                row = conn.execute(
                    text("""
                        SELECT id, material_version, status FROM public.material_regen_sessions
                        WHERE task_id = :tid AND node_id = :nid AND material_id = :mid
                          AND session_no = :sno
                    """),
                    {"tid": task_id, "nid": oss_node_id, "mid": emo_material_id, "sno": session_no}
                ).fetchone()
            if row and row[2] == "failed":
                emo_retry = {"id": row[0], "material_version": row[1]}

            # 也对 OSS 主路径 emotion 做初始备份
            regen_version.ensure_initial_backup(
                task_id, "char_designer", oss_node_id, emo_material_id, "characters",
                emo_prompt,
                filename=f"{emo_material_id}.png",
                user_id=user_id, user_name=user_name
            )

            emo_item = _generate_with_retry_and_record(
                task_id=task_id, oss_node_id=oss_node_id,
                speaker_id=speaker_id, combination_num=combination_num,
                emotion=emo, session_no=session_no,
                used_prompt=emo_prompt,
                retry_record_id=emo_retry["id"] if emo_retry else None,
                retry_material_version=emo_retry["material_version"] if emo_retry else None,
                user_id=user_id, user_name=user_name,
                model_name=model_name,
                shot=shot,
            )
            items.append(emo_item)

    success_count = sum(1 for it in items if it["status"] == "success")
    failed_count = sum(1 for it in items if it["status"] == "failed")
    errors = [it["error"] for it in items if it.get("error")]

    return {
        "total": len(items),
        "success": success_count,
        "failed": failed_count,
        "session_no": session_no,
        "items": items,
        "errors": errors,
    }


def _generate_with_retry_and_record(
    task_id: str,
    oss_node_id: str,
    speaker_id: str,
    combination_num: str,
    emotion: str,
    session_no: int,
    used_prompt: Optional[str],
    retry_record_id: Optional[int],
    retry_material_version: Optional[int],
    user_id: Optional[str],
    user_name: Optional[str],
    model_name: Optional[str] = None,
    shot: str = "",
) -> dict:
    """
    生成单张图片（最多 3 次内层重试），成功后覆盖本地 + OSS 主路径 + 写记录；
    失败后写 status=failed 记录。返回 items[] 中的一项。

    retry_record_id 不为空时：原地刷新该行（成功）/ 更新错误信息（失败），不分配新 version。
    retry_record_id 为空时：成功 → record_regen 新建 success 行；失败 → record_regen 新建 failed 行。
    model_name：可选，指定图片模型（gpt-image-2 / qwen-image-plus），透传给 redraw provider 并记入版本表。
    shot：景别（CS/MS/LS），非空时 material_id/filename 带 _{shot} 后缀；旧格式为空。
    """
    import time
    material_id_clean = _build_char_material_id(speaker_id, combination_num, emotion, shot)
    filename = f"{material_id_clean}.png"

    image_provider, calm_model, emotion_model = _resolve_provider(model_name)
    if emotion != "calm" and not used_prompt:
        used_prompt = _emotion_edit_prompt(
            task_id, speaker_id, combination_num, emotion
        )

    last_error = None
    success = False
    for attempt in range(1, 4):
        try:
            if emotion == "calm":
                result = _regen_calm(task_id, oss_node_id, material_id_clean, filename,
                                     speaker_id, combination_num, session_no,
                                     image_provider=image_provider, model=calm_model,
                                     shot=shot)
            else:
                result = _regen_emotion(task_id, oss_node_id, material_id_clean, filename,
                                        speaker_id, combination_num, emotion, session_no,
                                        image_provider=image_provider, model=emotion_model,
                                        shot=shot, emotion_prompt=used_prompt)
            if result.get("status") == "success":
                success = True
                # 覆盖本地（_regen_calm/_regen_emotion 已经把本地 OUTPUTS_PATH 写入了）
                # 覆盖 OSS 主路径（regen_version 内部会处理）
                _overwrite_main_oss_for_char(task_id, oss_node_id, material_id_clean, filename)

                # DB 记录
                regen_key = regen_version.regen_oss_key(
                    task_id, session_no, oss_node_id, "characters", filename
                )
                if retry_record_id is not None:
                    regen_version.update_record_to_success(
                        record_id=retry_record_id,
                        oss_key=regen_key,
                        prompt=used_prompt,
                        user_id=user_id,
                        user_name=user_name,
                        model_name=model_name,
                    )
                else:
                    regen_version.record_regen(
                        task_id, session_no, "char_designer", oss_node_id,
                        material_id_clean, regen_key, used_prompt,
                        user_id=user_id, user_name=user_name,
                        status="success",
                        model_name=model_name,
                    )
                break
            else:
                last_error = result.get("error") or "生成失败（无错误信息）"
                logger.warning(
                    f"[char_regen] {material_id_clean} 第 {attempt}/3 次失败: {last_error}"
                )
        except Exception as e:
            last_error = f"异常: {e}"
            logger.warning(
                f"[char_regen] {material_id_clean} 第 {attempt}/3 次异常: {e}"
            )
        if attempt < 3:
            time.sleep(5)

    if not success:
        # 3 次都失败：记 failed 行
        regen_key_dummy = regen_version.regen_oss_key(
            task_id, session_no, oss_node_id, "characters", filename
        )
        if retry_record_id is not None:
            # 重试时仍失败：保持原行 status=failed，更新 error_msg
            from sqlalchemy import text as _t
            with _engine().begin() as conn:
                conn.execute(
                    _t("""
                        UPDATE public.material_regen_sessions
                        SET error_msg = :emsg, user_id = COALESCE(:uid, user_id),
                            user_name = COALESCE(:uname, user_name)
                        WHERE id = :rid
                    """),
                    {"emsg": last_error, "uid": user_id, "uname": user_name, "rid": retry_record_id}
                )
            logger.info(f"[char_regen] {material_id_clean} 重试 3 次仍失败，更新 failed 行 error_msg")
        else:
            regen_version.record_regen(
                task_id, session_no, "char_designer", oss_node_id,
                material_id_clean, regen_key_dummy, used_prompt,
                user_id=user_id, user_name=user_name,
                status="failed", error_msg=last_error,
                model_name=model_name,
            )
            logger.info(f"[char_regen] {material_id_clean} 重试 3 次仍失败，新增 failed 行")

    return {
        "material_id": material_id_clean,
        "material_subtype": "character",
        "status": "success" if success else "failed",
        "session_no": session_no,
        "path": oss_utils.oss_public_url(
            regen_version.regen_oss_key(task_id, session_no, oss_node_id, "characters", filename)
        ) if success else None,
        "error": None if success else last_error,
    }


def _overwrite_main_oss_for_char(
    task_id: str, oss_node_id: str, material_id_clean: str, filename: str
) -> None:
    """把本地 outputs/{task_id}/images/characters/{filename} 上传到 OSS 主路径，覆盖旧文件。"""
    local_path = Path(OUTPUTS_PATH) / task_id / "images" / "characters" / filename
    if not local_path.exists():
        logger.warning(f"[char_regen] 本地文件不存在，跳过 OSS 主路径覆盖: {local_path}")
        return
    main_key = f"{task_id}/{oss_node_id}/assets/characters/{filename}"
    try:
        ok = oss_utils.oss_write(main_key, local_path.read_bytes(), content_type="image/png")
        if ok:
            logger.info(f"[char_regen] 已覆盖 OSS 主路径: {main_key}")
        else:
            logger.error(f"[char_regen] 覆盖 OSS 主路径失败（oss_write 返回 False）: {main_key}")
    except Exception as e:
        logger.error(f"[char_regen] 覆盖 OSS 主路径异常: {main_key} {e}")

    # 镜像到 OSS storage/outputs/ 路径（game_builder 构建时优先从此拉取，否则会用旧镜像覆盖回新素材）
    try:
        from common.output_mirror import mirror_paths
        mirror_paths([local_path], force=True)
    except Exception as e:
        logger.warning(f"[char_regen] 镜像到 storage/outputs 失败（不影响主流程）: {e}")


# ---------------------------------------------------------------------------
# 内部生成逻辑
# ---------------------------------------------------------------------------

@dataclass
class ImageProviderOptions:
    image_provider: str = "gpt"
    api_key: Optional[str] = None
    base_url: str = field(default_factory=lambda: os.getenv("GPT_IMAGE_BASE_URL", "https://ai.farmmx.com"))
    model: Optional[str] = None
    size: Optional[str] = None
    quality: str = "high"
    timeout_seconds: int = 300
    output_format: str = ""
    background: str = ""
    native_transparency: bool = False
    output_policy: Optional[CharacterOutputPolicy] = None


def get_character_by_speaker_id(task_id: str, speaker_id: str) -> Optional[Dict]:
    """从 characters 标准表查询角色，兼容字典、数组及显式 speaker_id 格式。"""
    try:
        with _platform_engine().connect() as conn:
            row = conn.execute(
                text("SELECT data FROM characters WHERE task_id = :tid"),
                {"tid": task_id},
            ).fetchone()
        if not row:
            return None

        char_data = row[0] if isinstance(row[0], (dict, list)) else json.loads(row[0])
        records = []
        if isinstance(char_data, dict):
            for char_key, char_value in char_data.items():
                values = char_value if isinstance(char_value, list) else [char_value]
                records.extend((char_key, value) for value in values)
        elif isinstance(char_data, list):
            records.extend((str(index), value) for index, value in enumerate(char_data))

        for char_key, char_record in records:
            if not isinstance(char_record, dict):
                continue
            char_name = str(char_record.get("name") or char_record.get("character_name") or char_key)
            explicit_id = str(
                char_record.get("speaker_id")
                or char_record.get("logical_character_id")
                or char_record.get("character_id")
                or ""
            ).strip()
            if (
                explicit_id == speaker_id
                or str(char_key) == speaker_id
                or chinese_to_camel_case(char_name) == speaker_id
            ):
                return {"key": char_key, "name": char_name, "data": char_record}
        return None
    except Exception as e:
        logger.error(f"[char_regen] 查询角色数据失败: {e}")
        return None


def _get_active_combinations(char_data: Dict) -> List[Dict]:
    if isinstance(char_data, list):
        char_data = char_data[0] if char_data else {}
    if not isinstance(char_data, dict):
        return []

    active = [
        item for item in char_data.get("valid_status_combinations", [])
        if isinstance(item, dict) and item.get("is_active") and item.get("combination")
    ]

    def sort_key(item: Dict) -> Tuple[int, int, int]:
        try:
            _, p_id, b_id, c_id = _parse_combination_str(item["combination"])
            return p_id, b_id, c_id
        except Exception:
            return 999, 999, 999

    return sorted(active, key=sort_key)


def _cleanup_temp_file(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


def _safe_replace(temp_path: Path, final_path: Path) -> bool:
    try:
        final_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temp_path, final_path)
        return True
    except Exception as e:
        logger.error(f"[char_regen] 替换文件失败 {final_path}: {e}")
        _cleanup_temp_file(temp_path)
        return False


def _create_text_provider(options: ImageProviderOptions):
    if options.image_provider == "qwen":
        return QwenTextImageProvider(
            api_key=options.api_key,
            model=options.model,
            size=options.size,
            max_retries=int(os.environ.get("QWEN_MAX_RETRIES", "3")),
            retry_delay=int(os.environ.get("QWEN_RETRY_DELAY", "5")),
        )
    return GPTImageTextProvider(
        api_key=options.api_key or os.getenv("GPT_IMAGE_API_KEY") or os.getenv("OPENAI_API_KEY"),
        base_url=options.base_url,
        model=options.model or "gpt-image-2",
        size=options.size or "1024x1536",
        quality=options.quality,
        n=1,
        output_format=options.output_format,
        background=options.background,
        timeout_seconds=options.timeout_seconds,
        transparent_character_output=options.native_transparency,
    )


def _create_edit_provider(options: ImageProviderOptions):
    if options.image_provider == "qwen":
        return QwenImageEditProvider(
            api_key=options.api_key,
            model=options.model or os.environ.get("QWEN_VARIANT_MODEL", "qwen-image-edit-plus"),
            size=options.size or "1140*1472",
            max_retries=int(os.environ.get("QWEN_MAX_RETRIES", "3")),
            retry_delay=int(os.environ.get("QWEN_RETRY_DELAY", "5")),
        )
    return GPTImageEditProvider(
        api_key=options.api_key or os.getenv("GPT_IMAGE_API_KEY") or os.getenv("OPENAI_API_KEY"),
        base_url=options.base_url,
        model=options.model or "gpt-image-2",
        size=options.size or "1024x1536",
        quality=options.quality,
        n=1,
        output_format=options.output_format,
        background=options.background,
        timeout_seconds=options.timeout_seconds,
        transparent_character_output=options.native_transparency,
    )


def _save_text_image(provider, image_provider: str, prompt: str, output_path: Path) -> bool:
    if image_provider == "qwen":
        image_url = provider.generate(prompt)
        return bool(image_url and provider.download(image_url, output_path))
    data_items = provider.generate(prompt)
    return bool(data_items and provider.save_image_item(data_items[0], output_path))


def _save_edit_image(provider, image_provider: str, base_image: Path, prompt: str, output_path: Path) -> bool:
    if image_provider == "qwen":
        image_url = provider.generate(base_image, prompt)
        return bool(image_url and provider.download(image_url, output_path))
    data_items = provider.edit(base_image, prompt)
    return bool(data_items and provider.save_image_item(data_items[0], output_path))


def _provider_options(
    task_id: str,
    image_provider: Optional[str],
    model: Optional[str],
) -> ImageProviderOptions:
    options = ImageProviderOptions(image_provider=image_provider or "gpt", model=model)
    resolved_model = options.model or {
        "gpt": "gpt-image-2",
        "qwen": "qwen-image-plus",
        "wan": "wan2.5-t2i-preview",
    }[options.image_provider]
    policy = resolve_character_output_policy(
        task_id,
        image_provider=options.image_provider,
        model=resolved_model,
    )
    options.output_policy = policy
    options.output_format = policy.output_format
    options.background = policy.background
    options.native_transparency = policy.native_transparency
    return options


def _record_image_usage(provider) -> None:
    if _usage_tracker:
        _usage_tracker.add_image(1, getattr(provider, "model", None))


def _generate_text_image(prompt: str, final_path: Path, options: ImageProviderOptions) -> Tuple[bool, str]:
    final_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = final_path.parent / f".{final_path.name}.tmp.png"
    _cleanup_temp_file(temp_path)
    provider = _create_text_provider(options)
    if not _save_text_image(provider, options.image_provider, prompt, temp_path) or not temp_path.exists():
        _cleanup_temp_file(temp_path)
        return False, "文生图调用失败"
    validator = (
        validator_for_policy(options.output_policy)
        if options.output_policy is not None
        else validate_image_file
    )
    if not validator(temp_path):
        _cleanup_temp_file(temp_path)
        return False, "文生图未满足人物透明输出合同"
    if not _safe_replace(temp_path, final_path):
        return False, f"写入本地文件失败: {final_path}"
    _record_image_usage(provider)
    return True, ""


def _generate_edit_image(
    base_image: Path,
    prompt: str,
    final_path: Path,
    options: ImageProviderOptions,
) -> Tuple[bool, str]:
    final_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = final_path.parent / f".{final_path.name}.tmp.png"
    _cleanup_temp_file(temp_path)
    provider = _create_edit_provider(options)
    if not _save_edit_image(provider, options.image_provider, base_image, prompt, temp_path) or not temp_path.exists():
        _cleanup_temp_file(temp_path)
        return False, "图生图调用失败"
    validator = (
        validator_for_policy(options.output_policy)
        if options.output_policy is not None
        else validate_image_file
    )
    if not validator(temp_path):
        _cleanup_temp_file(temp_path)
        return False, "图生图未满足人物透明输出合同"
    if not _safe_replace(temp_path, final_path):
        return False, f"写入本地文件失败: {final_path}"
    _record_image_usage(provider)
    return True, ""


def _generate_base_calm_image(
    task_id: str,
    prompt: str,
    final_path: Path,
    options: ImageProviderOptions,
) -> Tuple[bool, str]:
    """Generate a base calm image through the active character-style route."""
    style_plan = resolve_asset_style_plan(task_id, ASSET_CHARACTER)
    styled_prompt = ensure_asset_style(task_id, ASSET_CHARACTER, prompt)
    if not style_plan.uses_anchor:
        return _generate_text_image(styled_prompt, final_path, options)

    edit_options = options
    if options.image_provider in {"qwen", "wan"}:
        # Wan/Qwen 文生图模型不直接承担 Anchor 图生图，与主流程一致
        # 收口到项目现有 Qwen Edit Provider。
        edit_options = ImageProviderOptions(
            image_provider="qwen",
            api_key=options.api_key,
            base_url=options.base_url,
            model=None,
            size=os.getenv("QWEN_EDIT_CHARACTER_SIZE", "1140*1472"),
            quality=options.quality,
            timeout_seconds=options.timeout_seconds,
        )
    return _generate_edit_image(
        style_plan.anchor_path,
        compose_anchor_guided_prompt(style_plan, styled_prompt),
        final_path,
        edit_options,
    )


def _combination_num(combination: str) -> str:
    parts = combination.rsplit("-", 3)
    return "-".join(parts[-3:]) if len(parts) == 4 else ""


def _find_calm_image(
    output_dir: Path,
    speaker_id: str,
    combination_num: str,
    shot: str = "",
    task_id: Optional[str] = None,
) -> Optional[Path]:
    """
    找该 combination 的 calm 底图。

    文件名以 material_id 为准（_build_char_material_id），与 OSS 主路径、版本表记录同一套命名，
    因此不能用 _combination_to_filename——后者会无条件补 DEFAULT_SHOT="LS"，
    对无景别的旧格式 material_id 会拼出不存在的 *_calm_LS.png。

    查找顺序：先按传入 shot 精确匹配，再尝试旧格式无后缀及 LS/MS/CS 其它景别。
    外部管线可能只保留一张 calm 参考图，跨景别回落仍可用于维持角色一致性。
    """
    candidate_shots = [shot, "", DEFAULT_SHOT, "MS", "CS"]
    candidates = list(dict.fromkeys(
        _build_char_material_id(speaker_id, combination_num, "calm", candidate_shot)
        for candidate_shot in candidate_shots
    ))

    for name in candidates:
        path = output_dir / f"{name}.png"
        if path.exists():
            return path

    # 外部组不共享一组 PVC，但契约要求把角色图片镜像到标准 OSS outputs 前缀。
    # 按候选文件名逐个补齐到本地，之后沿用原有图生图逻辑即可。
    if task_id:
        try:
            from common.output_mirror import fetch_output_file

            for name in candidates:
                fetched = fetch_output_file(
                    task_id,
                    Path("images") / "characters" / f"{name}.png",
                )
                if fetched is not None and fetched.exists() and fetched.stat().st_size > 0:
                    return fetched
        except Exception as e:
            logger.warning(
                f"[char_regen] 从标准 OSS 产物补 calm 底图失败: "
                f"task={task_id} speaker={speaker_id} combination={combination_num}: {e}"
            )
    return None


def _ensure_base_calm(
    task_id: str,
    speaker_id: str,
    base_combination: str,
    output_dir: Path,
    options: ImageProviderOptions,
    shot: str = "",
) -> Optional[Path]:
    """变体 calm 需要基础 calm 当参考图；本地没有就按基础组合的提示词补生一张。"""
    base_combination_num = _combination_num(base_combination)
    existing = _find_calm_image(
        output_dir, speaker_id, base_combination_num, shot, task_id=task_id
    )
    if existing:
        return existing
    base_prompt = _fetch_prompt_by_combination(task_id, speaker_id, base_combination_num)
    if not base_prompt:
        logger.error(f"[char_regen] 无法获取基础组合提示词: {base_combination}")
        return None
    target = output_dir / f"{_build_char_material_id(speaker_id, base_combination_num, 'calm', shot)}.png"
    success, error = _generate_base_calm_image(
        task_id,
        base_prompt,
        target,
        options,
    )
    if not success:
        logger.error(f"[char_regen] 自动补生基础 calm 失败: {error}")
        return None
    return target


def _get_char_name(task_id: str, speaker_id: str) -> Optional[str]:
    info = get_character_by_speaker_id(task_id, speaker_id)
    return info["name"] if info else None


def _regen_calm(task_id, oss_node_id, material_id, filename, speaker_id, combination_num, session_no,
                image_provider=None, model=None, shot="") -> dict:
    """在本模块内生成 calm 图，完成后上传 OSS regenerate 路径。"""
    char_info = get_character_by_speaker_id(task_id, speaker_id)
    if not char_info:
        return {"status": "failed", "path": None, "error": f"未找到 speaker_id={speaker_id} 的角色信息"}

    _init_conversion_cache(task_id)
    combination = f"{char_info['name']}-{combination_num}"
    output_dir = Path(OUTPUTS_PATH) / task_id / "images" / "characters"
    local_path = Path(OUTPUTS_PATH) / task_id / "images" / "characters" / filename
    prompt = _fetch_prompt_by_combination(task_id, speaker_id, combination_num)
    if not prompt:
        return {"status": "failed", "path": None, "error": f"无法获取 {combination} 的提示词"}
    prompt = ensure_asset_style(task_id, ASSET_CHARACTER, prompt)

    active = _get_active_combinations(char_info["data"])
    base_combination = active[0]["combination"] if active else combination
    options = _provider_options(task_id, image_provider, model)
    if combination == base_combination:
        success, error = _generate_base_calm_image(
            task_id,
            prompt,
            local_path,
            options,
        )
    else:
        base_calm = _ensure_base_calm(task_id, speaker_id, base_combination, output_dir, options, shot)
        if not base_calm:
            return {"status": "failed", "path": None, "error": f"未找到基础 calm 图且自动补生失败: {base_combination}"}
        success, error = _generate_edit_image(
            base_calm, IDENTITY_CONSISTENCY_PREFIX + prompt, local_path, options
        )
    if not success:
        return {"status": "failed", "path": None, "error": error or "立绘生成失败"}

    regen_key = regen_version.regen_oss_key(task_id, session_no, oss_node_id, "characters", filename)
    if oss_utils.oss_write(regen_key, local_path.read_bytes(), content_type="image/png"):
        return {"status": "success", "path": oss_utils.oss_public_url(regen_key), "error": None}
    return {"status": "failed", "path": None, "error": "上传 OSS 失败"}


def _regen_emotion(task_id, oss_node_id, material_id, filename, speaker_id, combination_num, emotion, session_no,
                   image_provider=None, model=None, shot="", emotion_prompt=None) -> dict:
    """在本模块内以对应 calm 图生成单张情绪图。"""
    if emotion not in EMOTIONS:
        return {"status": "failed", "path": None, "error": f"不支持的情绪类型: {emotion}"}

    _init_conversion_cache(task_id)
    output_dir = Path(OUTPUTS_PATH) / task_id / "images" / "characters"
    calm_image = _find_calm_image(
        output_dir, speaker_id, combination_num, shot, task_id=task_id
    )
    if not calm_image:
        expected = _build_char_material_id(speaker_id, combination_num, "calm", shot)
        return {"status": "failed", "path": None,
                "error": f"未找到 calm 底图，无法生成情绪图（期望 {expected}.png 于 {output_dir}）"}

    local_path = output_dir / filename
    prompt = emotion_prompt or _emotion_edit_prompt(
        task_id, speaker_id, combination_num, emotion
    )
    success, error = _generate_edit_image(
        calm_image,
        prompt,
        local_path,
        _provider_options(task_id, image_provider, model),
    )
    if not success:
        return {"status": "failed", "path": None, "error": error or f"表情图生成失败: {emotion}"}

    regen_key = regen_version.regen_oss_key(task_id, session_no, oss_node_id, "characters", filename)
    if oss_utils.oss_write(regen_key, local_path.read_bytes(), content_type="image/png"):
        return {"status": "success", "path": oss_utils.oss_public_url(regen_key), "error": None}
    return {"status": "failed", "path": None, "error": "上传 OSS 失败"}


# ---------------------------------------------------------------------------
# 元数据恢复（供 activate 端点调用）
# ---------------------------------------------------------------------------

def restore_char_prompt(
    task_id: str,
    speaker_id: str,
    combination_num: str,
    original_prompt: str,
) -> dict:
    """
    回滚时恢复角色提示词：把原始 positive_prompt 写回 character_prompts 表。

    Returns:
        {"status": "success"|"failed", "errors": [...]}
    """
    updated = _update_prompt_by_combination(task_id, speaker_id, combination_num, original_prompt)
    if updated:
        logger.info(f"[char_regen] 已恢复 character_prompts: speaker_id={speaker_id} combination={combination_num}")
        return {"status": "success", "errors": []}
    return {"status": "failed", "errors": [f"写回 character_prompts 失败: speaker_id={speaker_id} combination={combination_num}"]}
