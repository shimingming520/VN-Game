#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scene_painter 素材重生成接口

参数规范：
- 场景图：task_id + material_id（= scene_id，如 scene_alley_day_frost_01）
- 章节封面：task_id + material_id（= node_id，如 chapter_1、node_14beaa8b）
- 小说总封面：task_id + material_id（= "novel_cover"）

支持 new_prompt 修改提示词（只改 prompt，不改 negative_prompt）。

存储：最终存储在 OSS，本地仅作临时生成目录。
"""

import os
import sys
import re
import json
import logging
import tempfile
from pathlib import Path
from typing import List, Optional

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from common.config import DATABASE_URL
from common.visual_style import (
    ASSET_SCENE,
    compose_anchor_guided_prompt,
    ensure_asset_style,
    resolve_asset_style_plan,
)
from common.node_utils import parse_oss_node_id, db_to_oss_node_id
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

SCENE_NEGATIVE_PROMPT = (
    "people, person, human, character, face, body, hands, limbs, head, eyes, nose, mouth, "
    "portrait, figure, man, woman, child, individual, crowd, group, "
    "blurry, low quality, distorted, artifact, watermark, text, signature"
)

# 支持的场景图片模型（ImageGenerator 内部按模型名自动路由：gpt-image-* → 私有部署，wan* → DashScope）
SUPPORTED_MODELS = {"gpt-image-2", "wan2.5-t2i-preview", "qwen-image-plus"}


# ---------------------------------------------------------------------------
# OSS 路径构建
# ---------------------------------------------------------------------------

def _oss_key_images(task_id: str, node_id: str, filename: str) -> str:
    """封面图 OSS key: {task_id}/{node_id}/assets/images/{filename}"""
    return f"{task_id}/{node_id}/assets/images/{filename}"


def _oss_key_scenes(task_id: str, node_id: str, filename: str) -> str:
    """场景图 OSS key: {task_id}/{node_id}/assets/scenes/{filename}"""
    return f"{task_id}/{node_id}/assets/scenes/{filename}"


def _oss_url(key: str) -> str:
    return oss_utils.oss_public_url(key) if oss_utils else key


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _engine():
    return create_engine(DATABASE_URL)


def _platform_engine():
    """scene_prompts/backgrounds 已迁平台库(agent_platform);material_regen_sessions 仍在业务库"""
    from common.config import PLATFORM_DATABASE_URL
    return create_engine(PLATFORM_DATABASE_URL)


def _fetch_scenes_data(task_id: str) -> dict:
    """返回 {scene_id: {"image_prompt": ..., "source_node": ...}}（读 scene_prompts 表）"""
    with _platform_engine().connect() as conn:
        row = conn.execute(
            text("SELECT data FROM scene_prompts WHERE task_id = :tid ORDER BY create_time DESC LIMIT 1"),
            {"tid": task_id}
        ).fetchone()
    if not row:
        return {}
    return row[0] if isinstance(row[0], dict) else json.loads(row[0])


def _resolve_scene_key(material_id: str, scenes_data: dict) -> Optional[str]:
    """
    把传入的 material_id 解析成 scene_prompts 表里真实存在的 scene_id。

    直接命中就用原值；否则只做一次回退：仅当末尾形如 `_<数字>_<数字>` 时剥掉最后一段
    （scene_xxx_1_3 → scene_xxx_1、scene_xxx_2_5 → scene_xxx_2）。
    调用方偶尔会在二级变体 id 后面多拼一层序号，这里兜住这一种情况，
    避免明明有素材却报"不存在"。

    只认末尾连续两段数字、且只回退一层，是为了不误伤本身就以单个数字结尾的合法 scene_id
    （如 scene_room_2 不会被剥成 scene_room）。

    找不到返回 None。
    """
    if material_id in scenes_data:
        return material_id
    trimmed = re.sub(r'_(\d+)_\d+$', r'_\1', material_id)
    if trimmed != material_id and trimmed in scenes_data:
        return trimmed
    return None


def _fetch_backgrounds_data(task_id: str) -> dict:
    """返回 backgrounds 表的完整 data 字段"""
    with _platform_engine().connect() as conn:
        row = conn.execute(
            text("SELECT data FROM backgrounds WHERE task_id = :tid LIMIT 1"),
            {"tid": task_id}
        ).fetchone()
    if not row:
        return {}
    return row[0] if isinstance(row[0], dict) else json.loads(row[0])


def _update_scene_prompt(task_id: str, scene_id: str, new_prompt: str) -> None:
    """更新 scene_prompts 表中指定 scene_id 的 image_prompt"""
    with _platform_engine().begin() as conn:
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
    """更新 backgrounds 表中指定 node_id 的 prompt（generated_prompts 或 novel_cover_metadata）"""
    with _platform_engine().begin() as conn:
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


# ---------------------------------------------------------------------------
# Image generator
# ---------------------------------------------------------------------------

def _make_generator(model_name: Optional[str] = None):
    core_dir = os.path.dirname(os.path.abspath(__file__))
    if core_dir not in sys.path:
        sys.path.insert(0, core_dir)
    import dotenv
    dotenv.load_dotenv(os.path.join(project_root, "novel-game-factory", ".env"))
    from scene_generator import ImageGenerator, _is_qwen_model, _is_wan_model

    # model_name 非空时覆盖默认 API_MODEL（用户选模型）；否则用环境默认
    api_model = model_name or os.getenv("API_MODEL", "gpt-image-2")
    api_timeout = int(os.getenv("API_TIMEOUT", "300"))
    api_max_retries = int(os.getenv("API_MAX_RETRIES", "3"))

    if _is_qwen_model(api_model):
        api_key = os.getenv("GPT_IMAGE_API_KEY") or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("Qwen 需要 GPT_IMAGE_API_KEY / OPENAI_API_KEY")
        base_url = os.getenv("GPT_IMAGE_BASE_URL", "https://ai.farmmx.com")
    elif _is_wan_model(api_model):
        api_key = os.getenv("DASHSCOPE_API_KEY")
        if not api_key:
            raise ValueError("DASHSCOPE_API_KEY 未设置（wan 模型需要）")
        base_url = os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/api/v1")
    else:
        api_key = os.getenv("GPT_IMAGE_API_KEY") or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("GPT_IMAGE_API_KEY / OPENAI_API_KEY 未设置")
        base_url = os.getenv("GPT_IMAGE_BASE_URL", "https://ai.farmmx.com")

    return ImageGenerator(
        base_url=base_url,
        api_key=api_key,
        timeout=api_timeout,
        model=api_model,
        size=os.getenv("SCENE_SIZE", "1792x1008"),
        quality=os.getenv("GPT_IMAGE_QUALITY", "high"),
        max_retries=api_max_retries,
    )

    # === 旧的"只走 DashScope wan"分支（保留备查，已被上方的 GPT-Image / Wan 自动路由替代） ===
    # from scene_generator import ImageGenerator
    # api_key = os.getenv("DASHSCOPE_API_KEY")
    # if not api_key:
    #     raise ValueError("DASHSCOPE_API_KEY 未设置")
    # return ImageGenerator(
    #     api_endpoint=os.getenv("API_ENDPOINT", "https://dashscope.aliyuncs.com/api/v1"),
    #     api_key=api_key,
    #     timeout=int(os.getenv("API_TIMEOUT", "60")),
    #     model=os.getenv("API_MODEL", "wan2.5-t2i-preview"),
    #     max_retries=int(os.getenv("API_MAX_RETRIES", "3")),
    # )


def _scene_output_dir(task_id: str) -> Path:
    """主流程场景底图的本地目录（与 scene_generator 一致）。"""
    from common.config import OUTPUTS_PATH
    return Path(OUTPUTS_PATH) / task_id / "images" / "scenes"


def _find_parent_base_image(task_id: str, scene_id: str) -> Optional[Path]:
    """
    二级变体场景返回其父级一级底图的本地路径，一级场景/底图不存在时返回 None。
    与主流程 SceneProcessor._base_image_path 保持同样的命名与查找规则。
    """
    try:
        from scene_generator import _parent_scene_id
    except ImportError:
        from services.scene_painter.core.scene_generator import _parent_scene_id
    parent = _parent_scene_id(scene_id)
    if not parent:
        return None
    safe_parent = re.sub(r'[^\w\-]', '_', parent)
    path = _scene_output_dir(task_id) / f"{safe_parent}.png"
    if path.exists() and path.stat().st_size > 0:
        return path

    # 二组等外部实现只需按契约写标准 OSS outputs，不共享一组 PVC。
    # 二级场景图生图前按需把父级原图拉回本地。
    try:
        from common.output_mirror import fetch_output_file

        fetched = fetch_output_file(
            task_id,
            Path("images") / "scenes" / f"{safe_parent}.png",
        )
        if fetched is not None and fetched.exists() and fetched.stat().st_size > 0:
            return fetched
    except Exception as e:
        logger.warning(
            f"[scene_regen] 从标准 OSS 产物补父场景图失败: "
            f"task={task_id} scene={scene_id} parent={safe_parent}: {e}"
        )
    return None


def _build_scene_prompt(prompt: str, task_id: str, model_name: Optional[str] = None) -> str:
    """
    按主流程顺序组装：global_style + 原始 prompt + 中性质量后缀 + 16:9 后缀。
    """
    try:
        from scene_generator import (
            ASPECT_RATIO_SUFFIX, SCENE_STYLE_SUFFIX,
        )
    except ImportError:
        from services.scene_painter.core.scene_generator import (
            ASPECT_RATIO_SUFFIX, SCENE_STYLE_SUFFIX,
        )

    prompt = ensure_asset_style(task_id, ASSET_SCENE, prompt)

    if SCENE_STYLE_SUFFIX not in prompt:
        prompt = f"{prompt}{SCENE_STYLE_SUFFIX}"
    if ASPECT_RATIO_SUFFIX not in prompt:
        prompt = f"{prompt}{ASPECT_RATIO_SUFFIX}"
    return prompt


def _generate_image_to_temp(
    prompt: str,
    model_name: Optional[str] = None,
    task_id: Optional[str] = None,
    scene_id: Optional[str] = None,
) -> tuple:
    """
    在本地临时目录生成图片，返回 (ok: bool, local_path: str|None, error: str|None)
    model_name 非空时用指定模型（否则走环境默认）。

    传入 task_id 时按主流程规则拼 global_style；再传 scene_id 且该场景是二级变体、
    父级底图在本地存在且模型支持图生图时，以父级底图做图生图（与主流程阶段二一致），
    否则降级为文生图。
    """
    try:
        gen = _make_generator(model_name=model_name)
        # 创建临时文件（不会自动删除）
        tmp_file = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp_path = tmp_file.name
        tmp_file.close()

        # 统一追加画风 + 16:9 比例提示（与主流程一致）
        if task_id:
            prompt = _build_scene_prompt(prompt, task_id, model_name=model_name)
        else:
            try:
                from scene_generator import ASPECT_RATIO_SUFFIX, SCENE_STYLE_SUFFIX
            except ImportError:
                from services.scene_painter.core.scene_generator import ASPECT_RATIO_SUFFIX, SCENE_STYLE_SUFFIX
            if SCENE_STYLE_SUFFIX not in prompt:
                prompt = f"{prompt}{SCENE_STYLE_SUFFIX}"
            if ASPECT_RATIO_SUFFIX not in prompt:
                prompt = f"{prompt}{ASPECT_RATIO_SUFFIX}"

        style_plan = (
            resolve_asset_style_plan(task_id, ASSET_SCENE)
            if task_id
            else None
        )

        # 二级变体优先以父级底图图生图；自定义模式的一级场景/封面
        # 则直接以用户已确认的场景 Anchor 作风格参考。
        base_path = None
        if task_id and scene_id and (
            gen.supports_edit or (style_plan and style_plan.uses_anchor and gen.supports_anchor_edit)
        ):
            base_path = _find_parent_base_image(task_id, scene_id)

        if base_path is not None:
            try:
                from scene_generator import compose_img2img_prompt
            except ImportError:
                from services.scene_painter.core.scene_generator import compose_img2img_prompt
            logger.info(f"[scene_regen] [图生图] {scene_id} 基于父级底图 {base_path.name} 生成")
            img = gen.edit_image_with_retry(
                base_image_path=base_path,
                prompt=compose_img2img_prompt(prompt),
                width=1792, height=1008,
                max_retries=3,
            )
        elif style_plan is not None and style_plan.uses_anchor:
            img = gen.edit_image_with_retry(
                base_image_path=style_plan.anchor_path,
                prompt=compose_anchor_guided_prompt(style_plan, prompt),
                width=1792,
                height=1008,
                max_retries=3,
            )
        else:
            img = gen.generate_single_image_with_retry(
                prompt=prompt,
                negative_prompt=SCENE_NEGATIVE_PROMPT,
                width=1792, height=1008,
                # 旧默认尺寸（保留备查）：width=1690, height=960
                max_retries=3
            )
        if _usage_tracker:
            _usage_tracker.add_image(
                1,
                model_name or os.getenv("API_MODEL", "gpt-image-2"),
                provider=os.getenv("IMAGE_PROVIDER", "openai_compatible"),
            )
        # 保存到临时文件
        img.save_to_file(tmp_path)
        if not os.path.exists(tmp_path):
            return False, None, "图片生成成功但文件未创建"
        return True, tmp_path, None
    except Exception as e:
        return False, None, str(e)


# ---------------------------------------------------------------------------
# 场景图重生成
# ---------------------------------------------------------------------------

def _overwrite_main_oss_scene(task_id: str, oss_source_node: str, material_id: str, img_data: bytes) -> None:
    """场景图覆盖 OSS 主路径"""
    main_key = _oss_key_scenes(task_id, oss_source_node, f"{material_id}.png")
    try:
        ok = oss_utils.oss_write(main_key, img_data, content_type="image/png")
        if ok:
            logger.info(f"[scene_regen] 已覆盖 OSS 主路径: {main_key}")
        else:
            logger.error(f"[scene_regen] 覆盖 OSS 主路径失败: {main_key}")
    except Exception as e:
        logger.error(f"[scene_regen] 覆盖 OSS 主路径异常: {main_key} {e}")


def _overwrite_main_oss_background(task_id: str, oss_node_id: str, oss_filename: str, img_data: bytes) -> None:
    """封面图覆盖 OSS 主路径（章节封面 oss_filename=beijing.png；小说总封面=novel_cover.png）"""
    main_key = _oss_key_images(task_id, oss_node_id, oss_filename)
    try:
        ok = oss_utils.oss_write(main_key, img_data, content_type="image/png")
        if ok:
            logger.info(f"[background_regen] 已覆盖 OSS 主路径: {main_key}")
        else:
            logger.error(f"[background_regen] 覆盖 OSS 主路径失败: {main_key}")
    except Exception as e:
        logger.error(f"[background_regen] 覆盖 OSS 主路径异常: {main_key} {e}")


def regenerate_scene(
    task_id: str,
    material_id: str,
    new_prompt: Optional[str] = None,
    force: bool = True,
    session_no: Optional[int] = None,
    user_id: Optional[str] = None,
    user_name: Optional[str] = None,
    model_name: Optional[str] = None,
    asset_id: Optional[str] = None,
) -> dict:
    """
    重生成单张场景图。

    Args:
        task_id: 任务 ID
        material_id: scene_prompts 表里的 scene_id，决定用哪条提示词和 source_node
        new_prompt: 新提示词（若提供则先更新 DB 再生成）
        force: True=强制覆盖已有文件
        session_no: 操作序号（None=自动分配）
        asset_id: 实际要覆盖的素材 id（文件名、OSS 主路径、版本记录都用它）。
            默认与 material_id 相同。二者不同的场景：有些图是从别的场景图复制出来的
            副本（如 scene_xxx_1_1.png 复制自 scene_xxx_1.png），引擎引用副本，
            但表里只有源图那条提示词记录——此时读提示词用 material_id，
            覆盖则必须落到 asset_id，否则会改错图。

    Returns:
        {"status": "success"|"skipped"|"failed", "session_no": int, "path": str|None, "error": str|None}
    """
    if oss_utils is None:
        return {"status": "failed", "path": None, "error": "oss_utils 未安装"}

    # 校验 model_name（可选，不传则用环境默认）
    if model_name and model_name not in SUPPORTED_MODELS:
        return {"status": "failed", "path": None,
                "error": f"不支持的 model_name={model_name}，仅支持 {sorted(SUPPORTED_MODELS)}"}

    # 读取 scenes_data 获取 source_node
    scenes_data = _fetch_scenes_data(task_id)
    if material_id not in scenes_data:
        return {"status": "failed", "path": None, "error": f"scene_id={material_id} 在 scene_prompts 表中不存在"}

    # 提示词/source_node 按 material_id 查；产物落到 asset_id
    asset_id = asset_id or material_id
    # asset_id 形如 xxx_n_m 时说明它是源图 xxx_n 的副本，成功后两张一起覆盖
    scene_copy_source = None
    if asset_id != material_id:
        stripped = re.sub(r'_(\d+)_\d+$', r'_\1', asset_id)
        if stripped != asset_id:
            scene_copy_source = stripped

    scene_info = scenes_data[material_id]
    source_node = scene_info.get("source_node")
    if not source_node:
        return {"status": "failed", "path": None, "error": f"scene_id={material_id} 缺少 source_node"}

    # source_node 存的是 DB 格式，转换为 OSS 格式
    oss_source_node = db_to_oss_node_id(source_node, task_id)
    oss_key = _oss_key_scenes(task_id, oss_source_node, f"{asset_id}.png")

    # 检查 OSS 是否存在（验证路径正确性）
    exists = oss_utils.oss_exists(oss_key)
    size_ok = exists and oss_utils.oss_get_size(oss_key) > 0

    if not exists:
        logger.warning(f"[scene_regen] OSS 文件不存在，将作为首次生成: {oss_key}")
    else:
        logger.info(f"[scene_regen] OSS 文件已存在，准备重生成: {oss_key}")

    # 跳过检查
    if not force and size_ok:
        return {"status": "skipped", "path": _oss_url(oss_key), "error": None}

    # 先读旧 prompt（用于初始备份记录，确保回滚时能恢复原始提示词）
    old_prompt = scene_info.get("image_prompt", "")
    prompt = new_prompt or old_prompt
    if not prompt:
        return {"status": "failed", "path": None, "error": f"scene_id={material_id} 无 image_prompt"}

    # 若有新 prompt，写库和 provider 请求都使用最终带 global_style 的版本。
    if new_prompt:
        new_prompt = ensure_asset_style(task_id, ASSET_SCENE, new_prompt)
        prompt = new_prompt
        _update_scene_prompt(task_id, material_id, new_prompt)

    # 检查上一版是否为 failed → 重试模式（复用 session_no 和 material_version）
    retry_record = None
    latest = regen_version.find_latest_record(task_id, oss_source_node, asset_id)
    if latest and latest["status"] == "failed":
        retry_record = latest
        logger.info(f"[scene_regen] 检测到上一版 failed，进入重试模式: id={latest['id']} session_no={latest['session_no']}")
        session_no = latest["session_no"]
    elif session_no is None:
        session_no = regen_version.allocate_session_no(task_id)

    # 初始备份
    filename = f"{asset_id}.png"
    regen_version.ensure_initial_backup(
        task_id, "scene_painter", oss_source_node, asset_id, "scenes", old_prompt,
        filename=filename, user_id=user_id, user_name=user_name
    )

    # === 生成（最多 3 次重试）===
    import time
    last_error = None
    success = False
    img_data = None
    regen_key = None
    for attempt in range(1, 4):
        ok, local_path, err = _generate_image_to_temp(
            prompt, model_name=model_name, task_id=task_id, scene_id=material_id
        )
        if not ok:
            last_error = err or "图片生成失败"
            logger.warning(f"[scene_regen] {material_id} 第 {attempt}/3 次生成失败: {last_error}")
            if attempt < 3:
                time.sleep(5)
            continue
        try:
            with open(local_path, "rb") as f:
                img_data = f.read()
        except Exception as e:
            last_error = f"读取本地临时文件失败: {e}"
            if attempt < 3:
                time.sleep(5)
            continue
        finally:
            if local_path and os.path.exists(local_path):
                try:
                    os.remove(local_path)
                except Exception:
                    pass

        regen_key = regen_version.regen_oss_key(task_id, session_no, oss_source_node, "scenes", filename)
        if not oss_utils.oss_write(regen_key, img_data, content_type="image/png"):
            last_error = "上传 OSS regenerate 失败"
            logger.warning(f"[scene_regen] {material_id} 第 {attempt}/3 次上传失败")
            if attempt < 3:
                time.sleep(5)
            continue

        success = True
        break

    if success:
        # 覆盖目标：asset_id 本身；若它是 xxx_n_m 形式的副本，源图 xxx_n 也一并覆盖，
        # 两张图内容本就相同（副本由源图复制而来），必须同步更新否则画面会不一致。
        overwrite_ids = [asset_id]
        if scene_copy_source and scene_copy_source != asset_id:
            overwrite_ids.append(scene_copy_source)
            logger.info(f"[scene_regen] {asset_id} 为 {scene_copy_source} 的副本，两张图一并覆盖")

        # 写本地
        local_imgs = []
        try:
            from common.config import OUTPUTS_PATH
            scenes_dir = Path(OUTPUTS_PATH) / task_id / "images" / "scenes"
            scenes_dir.mkdir(parents=True, exist_ok=True)
            for oid in overwrite_ids:
                p = scenes_dir / f"{oid}.png"
                p.write_bytes(img_data)
                local_imgs.append(p)
                logger.info(f"[scene_regen] 已写入本地场景图: {p}")
        except Exception as e:
            logger.warning(f"[scene_regen] 写入本地场景图失败（不影响主流程）: {e}")

        # 覆盖 OSS 主路径
        for oid in overwrite_ids:
            _overwrite_main_oss_scene(task_id, oss_source_node, oid, img_data)

        # 镜像到 OSS storage/outputs/ 路径（game_builder 构建时优先从此拉取，否则会用旧镜像覆盖回新素材）
        if local_imgs:
            try:
                from common.output_mirror import mirror_paths
                mirror_paths(local_imgs, force=True)
            except Exception as e:
                logger.warning(f"[scene_regen] 镜像到 storage/outputs 失败（不影响主流程）: {e}")

        # DB 记录
        if retry_record is not None:
            regen_version.update_record_to_success(
                record_id=retry_record["id"], oss_key=regen_key, prompt=prompt,
                user_id=user_id, user_name=user_name, model_name=model_name,
            )
        else:
            regen_version.record_regen(
                task_id, session_no, "scene_painter", oss_source_node, asset_id, regen_key, prompt,
                user_id=user_id, user_name=user_name, status="success", model_name=model_name,
            )

        return {
            "status": "success",
            "session_no": session_no,
            "path": oss_utils.oss_public_url(regen_key),
            "action": "regenerate" if exists else "generate",
            "error": None
        }

    # 3 次都失败：记 failed 行
    regen_key_dummy = regen_version.regen_oss_key(task_id, session_no, oss_source_node, "scenes", filename)
    if retry_record is not None:
        from sqlalchemy import text as _t
        with _engine().begin() as conn:
            conn.execute(
                _t("""
                    UPDATE public.material_regen_sessions
                    SET error_msg = :emsg, user_id = COALESCE(:uid, user_id),
                        user_name = COALESCE(:uname, user_name),
                        model_name = COALESCE(:model_name, model_name)
                    WHERE id = :rid
                """),
                {"emsg": last_error, "uid": user_id, "uname": user_name,
                 "model_name": model_name, "rid": retry_record["id"]}
            )
    else:
        regen_version.record_regen(
            task_id, session_no, "scene_painter", oss_source_node, asset_id, regen_key_dummy, prompt,
            user_id=user_id, user_name=user_name, status="failed", error_msg=last_error,
            model_name=model_name,
        )
    return {"status": "failed", "session_no": session_no, "path": None, "error": last_error}


# ---------------------------------------------------------------------------
# 封面图重生成（章节封面 + 小说总封面）
# ---------------------------------------------------------------------------

def regenerate_background(
    task_id: str,
    material_id: str,
    new_prompt: Optional[str] = None,
    force: bool = True,
    session_no: Optional[int] = None,
    user_id: Optional[str] = None,
    user_name: Optional[str] = None,
    model_name: Optional[str] = None,
) -> dict:
    """
    重生成封面图。

    Args:
        task_id: 任务 ID
        material_id: OSS 格式的 node_id（如 chapter_1_chapter_1、chapter_2_7a80a1c2）或 "novel_cover"
        new_prompt: 新提示词（若提供则先更新 DB 再生成）
        force: True=强制覆盖已有文件
        session_no: 操作序号（None=自动分配）

    Returns:
        {"status": "success"|"skipped"|"failed", "session_no": int, "path": str|None, "error": str|None}
    """
    if oss_utils is None:
        return {"status": "failed", "path": None, "error": "oss_utils 未安装"}

    # 校验 model_name（可选，不传则用环境默认）
    if model_name and model_name not in SUPPORTED_MODELS:
        return {"status": "failed", "path": None,
                "error": f"不支持的 model_name={model_name}，仅支持 {sorted(SUPPORTED_MODELS)}"}

    bg_data = _fetch_backgrounds_data(task_id)
    if not bg_data:
        return {"status": "failed", "path": None, "error": f"backgrounds 表中无 task_id={task_id} 的数据"}

    # 解析 node_id：db_node_id 用于查 DB，oss_node_id 用于 OSS 路径
    if material_id == "novel_cover":
        db_node_id = "novel_cover"
        oss_node_id = "novel_cover"
        filename = "novel_cover.png"
        local_filename = "novel_cover.png"
        ncm = bg_data.get("novel_cover_metadata", {})
        old_prompt = ncm.get("prompt", "")
        prompt = new_prompt or old_prompt
        if not prompt:
            return {"status": "failed", "path": None, "error": "novel_cover_metadata 无 prompt"}
    else:
        db_node_id, oss_node_id = parse_oss_node_id(material_id)
        filename = "beijing.png"          # OSS 上的文件名
        local_filename = f"{db_node_id}.png"  # 本地文件名
        gp = bg_data.get("generated_prompts", {})
        if db_node_id not in gp:
            return {"status": "failed", "path": None, "error": f"node_id={db_node_id} 在 generated_prompts 中不存在"}
        old_prompt = gp[db_node_id].get("prompt", "")
        prompt = new_prompt or old_prompt
        if not prompt:
            return {"status": "failed", "path": None, "error": f"node_id={db_node_id} 无 prompt"}

    oss_key = _oss_key_images(task_id, oss_node_id, filename)

    # 检查 OSS 是否存在（验证路径正确性）
    exists = oss_utils.oss_exists(oss_key)
    size_ok = exists and oss_utils.oss_get_size(oss_key) > 0

    if not exists:
        logger.warning(f"[background_regen] OSS 文件不存在，将作为首次生成: {oss_key}")
    else:
        logger.info(f"[background_regen] OSS 文件已存在，准备重生成: {oss_key}")

    # 跳过检查
    if not force and size_ok:
        return {"status": "skipped", "path": _oss_url(oss_key), "error": None}

    # 若有新 prompt，写库和 provider 请求都使用最终带 global_style 的版本。
    if new_prompt:
        new_prompt = ensure_asset_style(task_id, ASSET_SCENE, new_prompt)
        prompt = new_prompt
        _update_background_prompt(task_id, db_node_id, new_prompt)

    # 检查上一版是否为 failed → 重试模式
    retry_record = None
    latest = regen_version.find_latest_record(task_id, oss_node_id, material_id)
    if latest and latest["status"] == "failed":
        retry_record = latest
        logger.info(f"[background_regen] 检测到上一版 failed，进入重试模式: id={latest['id']} session_no={latest['session_no']}")
        session_no = latest["session_no"]
    elif session_no is None:
        session_no = regen_version.allocate_session_no(task_id)

    # 初始备份
    regen_version.ensure_initial_backup(
        task_id, "scene_painter", oss_node_id, material_id, "images", old_prompt,
        filename=filename, user_id=user_id, user_name=user_name
    )

    # === 生成（最多 3 次重试）===
    import time
    last_error = None
    success = False
    img_data = None
    regen_key = None
    for attempt in range(1, 4):
        ok, local_path, err = _generate_image_to_temp(
            prompt,
            model_name=model_name,
            task_id=task_id,
        )
        if not ok:
            last_error = err or "图片生成失败"
            logger.warning(f"[background_regen] {material_id} 第 {attempt}/3 次生成失败: {last_error}")
            if attempt < 3:
                time.sleep(5)
            continue
        try:
            with open(local_path, "rb") as f:
                img_data = f.read()
        except Exception as e:
            last_error = f"读取本地临时文件失败: {e}"
            if attempt < 3:
                time.sleep(5)
            continue
        finally:
            if local_path and os.path.exists(local_path):
                try:
                    os.remove(local_path)
                except Exception:
                    pass

        regen_key = regen_version.regen_oss_key(task_id, session_no, oss_node_id, "images", filename)
        if not oss_utils.oss_write(regen_key, img_data, content_type="image/png"):
            last_error = "上传 OSS regenerate 失败"
            logger.warning(f"[background_regen] {material_id} 第 {attempt}/3 次上传失败")
            if attempt < 3:
                time.sleep(5)
            continue

        success = True
        break

    if success:
        # 写本地
        try:
            from common.config import OUTPUTS_PATH
            local_img = Path(OUTPUTS_PATH) / task_id / "images" / "backgrounds" / local_filename
            local_img.parent.mkdir(parents=True, exist_ok=True)
            local_img.write_bytes(img_data)
            logger.info(f"[background_regen] 已写入本地封面图: {local_img}")
        except Exception as e:
            logger.warning(f"[background_regen] 写入本地封面图失败（不影响主流程）: {e}")

        # 覆盖 OSS 主路径
        _overwrite_main_oss_background(task_id, oss_node_id, filename, img_data)

        # 镜像到 OSS storage/outputs/ 路径（game_builder 构建时优先从此拉取，否则会用旧镜像覆盖回新素材）
        try:
            from common.output_mirror import mirror_paths
            mirror_paths([local_img], force=True)
        except Exception as e:
            logger.warning(f"[background_regen] 镜像到 storage/outputs 失败（不影响主流程）: {e}")

        # DB 记录
        if retry_record is not None:
            regen_version.update_record_to_success(
                record_id=retry_record["id"], oss_key=regen_key, prompt=prompt,
                user_id=user_id, user_name=user_name, model_name=model_name,
            )
        else:
            regen_version.record_regen(
                task_id, session_no, "scene_painter", oss_node_id, material_id, regen_key, prompt,
                user_id=user_id, user_name=user_name, status="success", model_name=model_name,
            )

        return {
            "status": "success",
            "session_no": session_no,
            "path": oss_utils.oss_public_url(regen_key),
            "action": "regenerate" if exists else "generate",
            "error": None
        }

    # 3 次都失败：记 failed 行
    regen_key_dummy = regen_version.regen_oss_key(task_id, session_no, oss_node_id, "images", filename)
    if retry_record is not None:
        from sqlalchemy import text as _t
        with _engine().begin() as conn:
            conn.execute(
                _t("""
                    UPDATE public.material_regen_sessions
                    SET error_msg = :emsg, user_id = COALESCE(:uid, user_id),
                        user_name = COALESCE(:uname, user_name),
                        model_name = COALESCE(:model_name, model_name)
                    WHERE id = :rid
                """),
                {"emsg": last_error, "uid": user_id, "uname": user_name,
                 "model_name": model_name, "rid": retry_record["id"]}
            )
    else:
        regen_version.record_regen(
            task_id, session_no, "scene_painter", oss_node_id, material_id, regen_key_dummy, prompt,
            user_id=user_id, user_name=user_name, status="failed", error_msg=last_error,
            model_name=model_name,
        )
    return {"status": "failed", "session_no": session_no, "path": None, "error": last_error}


# ---------------------------------------------------------------------------
# 批量入口
# ---------------------------------------------------------------------------

def regenerate_scene_materials(
    task_id: str,
    material_ids: List[str],
    new_prompts: Optional[dict] = None,
    force: bool = True,
    session_no: Optional[int] = None,
    user_id: Optional[str] = None,
    user_name: Optional[str] = None,
    model_name: Optional[str] = None,
) -> dict:
    """
    批量重生成场景/封面素材。

    Args:
        task_id: 任务 ID
        material_ids: 素材 ID 列表，可混合 scene_id、node_id、"novel_cover"
        new_prompts: {material_id: new_prompt}，可选
        force: True=强制覆盖

    Returns:
        {"total": int, "success": int, "failed": int, "items": [...], "errors": [...]}
    """
    if new_prompts is None:
        new_prompts = {}

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

    # 分配 session_no（批量共用同一个）
    if session_no is None:
        session_no = regen_version.allocate_session_no(task_id)

    # 判断每个 material_id 属于场景图还是封面图
    scenes_data = _fetch_scenes_data(task_id)
    bg_data = _fetch_backgrounds_data(task_id)
    bg_keys_db = set(bg_data.get("generated_prompts", {}).keys()) | {"novel_cover"}

    from common.node_utils import parse_oss_node_id as _parse

    items = []
    success = failed = 0
    errors = []

    for mid in material_ids:
        new_prompt = new_prompts.get(mid)
        scene_key = _resolve_scene_key(mid, scenes_data)
        if scene_key:
            if scene_key != mid:
                logger.info(
                    f"[scene_regen] material_id={mid} 在 scene_prompts 表中无记录，"
                    f"回退用 scene_id={scene_key} 的提示词，产物仍覆盖 {mid}"
                )
                new_prompt = new_prompt if new_prompt is not None else new_prompts.get(scene_key)
            result = regenerate_scene(task_id, scene_key, new_prompt, force, session_no,
                                      user_id=user_id, user_name=user_name, model_name=model_name,
                                      asset_id=mid)
            subtype = "scene"
        elif mid == "novel_cover" or mid in bg_keys_db or _parse(mid)[0] in bg_keys_db:
            result = regenerate_background(task_id, mid, new_prompt, force, session_no,
                                           user_id=user_id, user_name=user_name, model_name=model_name)
            subtype = "background"
        else:
            result = {
                "status": "failed", "path": None,
                "error": f"material_id={mid} 在 scene_prompts 和 backgrounds 表中均不存在"
            }
            subtype = "unknown"

        items.append({
            "material_id": mid,
            "material_subtype": subtype,
            "status": result["status"],
            "session_no": result.get("session_no", session_no),
            "path": result.get("path"),
            "error": result.get("error"),
        })
        if result["status"] == "success":
            success += 1
        elif result["status"] == "failed":
            failed += 1
            errors.append(f"{mid}: {result['error']}")

    return {
        "total": len(material_ids),
        "success": success,
        "failed": failed,
        "session_no": session_no,
        "items": items,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# 元数据恢复（供 activate 端点调用）
# ---------------------------------------------------------------------------

def restore_scene_prompt(
    task_id: str,
    material_id: str,
    original_prompt: str,
) -> dict:
    """
    回滚时恢复场景图或封面图的提示词：把原始 prompt 写回 scenes 或 backgrounds 表。

    material_id 为 scene_id 时写 scenes 表；为 node_id 或 "novel_cover" 时写 backgrounds 表。

    Returns:
        {"status": "success"|"failed", "errors": [...]}
    """
    scenes_data = _fetch_scenes_data(task_id)
    if material_id in scenes_data:
        _update_scene_prompt(task_id, material_id, original_prompt)
        logger.info(f"[scene_regen] 已恢复 scenes prompt: {material_id}")
        return {"status": "success", "errors": []}

    # 封面图（novel_cover 或 node_id）
    _update_background_prompt(task_id, material_id if material_id == "novel_cover" else parse_oss_node_id(material_id)[0], original_prompt)
    logger.info(f"[scene_regen] 已恢复 backgrounds prompt: {material_id}")
    return {"status": "success", "errors": []}
