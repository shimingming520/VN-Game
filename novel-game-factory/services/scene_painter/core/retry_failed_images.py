#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
图片补生成工具 - 检查指定 task_id 的输出目录，找出缺失的图片，
从数据库重新读取提示词并补生成（场景图片 + 封面图片）。

用法:
    python retry_failed_images.py --task-id <task_id> [--type scenes|backgrounds|all]

# retry everything
python novel-game-factory/services/scene_painter/core/retry_failed_images.py --task-id

# only scenes
python novel-game-factory/services/scene_painter/core/retry_failed_images.py --type scenes --task-id

# only backgrounds
python novel-game-factory/services/scene_painter/core/retry_failed_images.py --type backgrounds --task-id
"""

import os
import re
import sys
import json
import logging
import argparse
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from openai import OpenAI

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from common.config import OUTPUTS_PATH
from common.platform_db import platform_engine
from common.task_models import engine as business_engine
from common.visual_style import (
    ASSET_SCENE,
    apply_asset_style,
    compose_anchor_guided_prompt,
    ensure_asset_style,
    get_asset_style,
    resolve_asset_style_plan,
)
from common.visual_checkpoint import (
    assert_retry_budget,
    load_checkpoint,
    mark_failed as checkpoint_mark_failed,
    mark_running as checkpoint_mark_running,
    mark_success as checkpoint_mark_success,
    STATUS_SUCCESS,
)
from sqlalchemy import text

try:
    from services.scene_painter.core.scene_instances import copy_scene_instances
except ImportError:
    from scene_instances import copy_scene_instances  # type: ignore
try:
    from services.scene_painter.core.artifact_contract import (
        atomic_copy_image,
        atomic_image_output,
        validate_image_file,
    )
except ImportError:
    from artifact_contract import (  # type: ignore
        atomic_copy_image,
        atomic_image_output,
        validate_image_file,
    )


try:
    from common.usage_tracker import tracker as _usage_tracker
except Exception:
    _usage_tracker = None

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 失败记录
# ---------------------------------------------------------------------------

def _categorize_error(msg: str) -> str:
    m = msg.lower()
    if "401" in m or "invalid api" in m or "api密钥无效" in m or "no api-key" in m or "invalidapikey" in m:
        return "API_AUTH_ERROR"
    if "403" in m or "无权限" in m or "permission" in m:
        return "API_PERMISSION_ERROR"
    if "datainspectionfailed" in m or "inappropriate content" in m or "任务失败" in m or "审核" in m or "moderat" in m:
        return "CONTENT_MODERATION"
    if "超时" in m or "timeout" in m or "轮询" in m:
        return "TIMEOUT"
    if "400" in m:
        return "BAD_REQUEST"
    if "下载" in m or "download" in m:
        return "DOWNLOAD_ERROR"
    if "image_prompt" in m or "没有 image_prompt" in m or "没有 prompt" in m:
        return "MISSING_PROMPT"
    return "UNKNOWN"


class FailureRecord:
    def __init__(self, asset_id: str, asset_type: str, prompt: str, error: str):
        self.asset_id = asset_id
        self.asset_type = asset_type          # "scene" | "chapter_cover" | "novel_cover"
        self.prompt_preview = prompt[:80] + "..." if len(prompt) > 80 else prompt
        self.error = error
        self.category = _categorize_error(error)

    def __str__(self):
        return (
            f"  [{self.category}] {self.asset_type} / {self.asset_id}\n"
            f"    prompt : {self.prompt_preview}\n"
            f"    error  : {self.error}"
        )


# ---------------------------------------------------------------------------
# DB helpers (reuse logic from scene_generator / cover_generator)
# ---------------------------------------------------------------------------

def _engine():
    return business_engine


def _platform_engine():
    """scene_prompts/backgrounds/novel_tasks 已迁平台库(agent_platform);scenes 仍在业务库"""
    return platform_engine


def fetch_scenes_data(task_id: str) -> dict:
    """返回 {scene_id: {"image_prompt": "..."}}，读自 scene_prompts 表（与 scene_generator 保持一致）"""
    with _platform_engine().connect() as conn:
        row = conn.execute(
            text("SELECT data FROM scene_prompts WHERE task_id = :tid ORDER BY create_time DESC LIMIT 1"),
            {"tid": task_id}
        ).fetchone()
    if not row:
        return {}
    data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
    return data or {}


def fetch_backgrounds_data(task_id: str) -> dict:
    """返回 backgrounds 表的 data 字段"""
    with _platform_engine().connect() as conn:
        row = conn.execute(
            text("SELECT data FROM backgrounds WHERE task_id = :tid LIMIT 1"),
            {"tid": task_id}
        ).fetchone()
    if not row:
        return {}
    data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
    return data


# ---------------------------------------------------------------------------
# Missing-image detection
# ---------------------------------------------------------------------------

def _safe_name(scene_id: str) -> str:
    return re.sub(r'[^\w\-]', '_', scene_id)


def find_missing_scenes(task_id: str, scenes_data: dict) -> list:
    """Return missing, damaged or checkpoint-stale scene assets."""
    scenes_dir = os.path.join(OUTPUTS_PATH, task_id, "images", "scenes")
    try:
        from scene_generator import (
            ASPECT_RATIO_SUFFIX,
            SCENE_STYLE_SUFFIX,
            SCENE_HEIGHT,
            SCENE_WIDTH,
            build_scene_checkpoint_signature,
            _parent_scene_id,
        )
    except ImportError:
        from services.scene_painter.core.scene_generator import (  # type: ignore
            ASPECT_RATIO_SUFFIX,
            SCENE_STYLE_SUFFIX,
            SCENE_HEIGHT,
            SCENE_WIDTH,
            build_scene_checkpoint_signature,
            _parent_scene_id,
        )
    style_plan = resolve_asset_style_plan(task_id, ASSET_SCENE)
    api_model = os.getenv("API_MODEL", "gpt-image-2")
    scene_width = int(os.getenv("SCENE_WIDTH", str(SCENE_WIDTH)))
    scene_height = int(os.getenv("SCENE_HEIGHT", str(SCENE_HEIGHT)))
    api_quality = os.getenv("GPT_IMAGE_QUALITY", "low")
    missing = []
    for scene_id, scene_info in scenes_data.items():
        expected = os.path.join(scenes_dir, f"{_safe_name(scene_id)}.png")
        if not validate_image_file(expected):
            missing.append(scene_id)
            continue
        positive_prompt = apply_asset_style(
            style_plan, (scene_info or {}).get("image_prompt", "")
        )
        positive_prompt = f"{positive_prompt}{SCENE_STYLE_SUFFIX}{ASPECT_RATIO_SUFFIX}"
        negative_prompt = SCENE_NEGATIVE_PROMPT
        parent = _parent_scene_id(scene_id)
        parent_path = (
            Path(scenes_dir) / f"{_safe_name(parent)}.png" if parent else None
        )
        effective_parent = (
            parent_path
            if parent_path is not None and validate_image_file(parent_path)
            else None
        )
        signature_config = SimpleNamespace(
            api_model=api_model,
            api_quality=api_quality,
            scene=SimpleNamespace(width=scene_width, height=scene_height),
        )
        input_digest, dependency_digest = build_scene_checkpoint_signature(
            task_id,
            signature_config,
            style_plan,
            scene_id,
            positive_prompt,
            negative_prompt,
            effective_parent,
        )
        record = load_checkpoint(
            task_id, "scene_painter", "generate_scene", scene_id
        )
        if (
            record is None
            or record.status != STATUS_SUCCESS
            or record.input_digest != input_digest
            or record.generator_version != "scene-image-v2-resume"
            or record.dependency_digest != dependency_digest
        ):
            missing.append(scene_id)
    return missing


def find_missing_backgrounds(task_id: str, bg_data: dict) -> dict:
    """
    返回 {"chapters": [node_id, ...], "novel_cover": bool}
    chapters: 缺失的章节封面 node_id 列表
    novel_cover: True 表示 novel_cover.png 缺失
    """
    bg_dir = os.path.join(OUTPUTS_PATH, task_id, "images", "backgrounds")
    chapter_prompts = bg_data.get("generated_prompts", {})
    missing_chapters = []
    style_plan = resolve_asset_style_plan(task_id, ASSET_SCENE)
    model = os.getenv("API_MODEL", "gpt-image-2")
    image_size = (1792, 1008)
    quality = os.getenv("GPT_IMAGE_QUALITY", "high")
    try:
        from cover_generator import (
            _ensure_aspect_ratio,
            build_cover_checkpoint_digest,
        )
    except ImportError:
        from services.scene_painter.core.cover_generator import (  # type: ignore
            _ensure_aspect_ratio,
            build_cover_checkpoint_digest,
        )
    for node_id, prompt_data in chapter_prompts.items():
        expected = os.path.join(bg_dir, f"{node_id}.png")
        prompt = apply_asset_style(style_plan, (prompt_data or {}).get("prompt", ""))
        prompt = _ensure_aspect_ratio(prompt)
        input_digest = build_cover_checkpoint_digest(
            str(node_id),
            prompt,
            (prompt_data or {}).get("negative_prompt", ""),
            style_plan,
            model=model,
            image_size=image_size,
            quality=quality,
        )
        record = load_checkpoint(
            task_id, "scene_painter", "generate_cover", str(node_id)
        )
        if (
            not validate_image_file(expected)
            or record is None
            or record.status != STATUS_SUCCESS
            or record.input_digest != input_digest
            or record.generator_version != "cover-image-v2-resume"
        ):
            missing_chapters.append(node_id)

    novel_cover_path = os.path.join(bg_dir, "novel_cover.png")
    novel_meta = bg_data.get("novel_cover_metadata", {})
    novel_raw = str(novel_meta.get("prompt") or "").strip()
    missing_novel = False
    if novel_raw:
        novel_prompt = _ensure_aspect_ratio(apply_asset_style(style_plan, novel_raw))
        novel_digest = build_cover_checkpoint_digest(
            "novel_cover",
            novel_prompt,
            novel_meta.get("negative_prompt", ""),
            style_plan,
            model=model,
            image_size=image_size,
            quality=quality,
        )
        record = load_checkpoint(
            task_id, "scene_painter", "generate_cover", "novel_cover"
        )
        missing_novel = (
            not validate_image_file(novel_cover_path)
            or record is None
            or record.status != STATUS_SUCCESS
            or record.input_digest != novel_digest
            or record.generator_version != "cover-image-v2-resume"
        )
    return {"chapters": missing_chapters, "novel_cover": missing_novel}


# ---------------------------------------------------------------------------
# Image generator (shared)
# ---------------------------------------------------------------------------

def _make_image_generator():
    """构建 ImageGenerator，复用 scene_generator 中的实现，按 API_MODEL 路由 gpt / wan"""
    # 延迟导入，避免循环依赖
    from scene_generator import ImageGenerator, _is_qwen_model, _is_wan_model
    import dotenv
    dotenv.load_dotenv(os.path.join(project_root, "novel-game-factory", ".env"))
    api_model = os.getenv("API_MODEL", "gpt-image-2")
    api_timeout = int(os.getenv("API_TIMEOUT", "300"))
    api_max_retries = int(os.getenv("API_MAX_RETRIES", "3"))
    outer_request_attempts = max(
        1,
        int(os.getenv("SCENE_OUTER_REQUEST_ATTEMPTS", "1")),
    )
    api_quality = os.getenv("GPT_IMAGE_QUALITY", "low")

    if _is_qwen_model(api_model):
        api_key = os.getenv("GPT_IMAGE_API_KEY") or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("Qwen 需要 GPT_IMAGE_API_KEY / OPENAI_API_KEY")
        base_url = os.getenv("GPT_IMAGE_BASE_URL", "https://ai.farmmx.com")
    elif _is_wan_model(api_model):
        # === DashScope Wan ===
        api_key = os.getenv("DASHSCOPE_API_KEY")
        if not api_key:
            raise ValueError("DASHSCOPE_API_KEY 未设置（wan 模型需要）")
        base_url = os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/api/v1")
    else:
        # === GPT Image 私有部署 ===
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
        quality=api_quality,
        max_retries=api_max_retries,
    ), outer_request_attempts


# ---------------------------------------------------------------------------
# Reference-based prompt synthesis helpers
# ---------------------------------------------------------------------------

def find_reference_scene_ids(failed_id: str, all_scene_ids: list, min_refs: int = 2, max_refs: int = 5) -> list:
    """
    找到与 failed_id 前缀最相近的参考场景 ID 列表（2~5个）。

    策略：
    1. 去掉末尾序号段（如 _03），得到最长前缀。
    2. 找所有共享该前缀的其他场景，按序号与目标序号的距离升序排列（最近的优先）。
    3. 若不足 min_refs，去掉前缀最后一个 _segment 继续找，依此类推。
    4. 找到 >= min_refs 个即停，取最多 max_refs 个返回。
    5. 全部缩略后仍不足，兜底返回距离最近的若干个。
    """
    def _seq(sid: str) -> int:
        m = re.search(r'_(\d+)$', sid)
        return int(m.group(1)) if m else 0

    candidates_pool = [s for s in all_scene_ids if s != failed_id]
    target_seq = _seq(failed_id)

    def _sort_by_distance(matches: list) -> list:
        return sorted(matches, key=lambda s: abs(_seq(s) - target_seq))

    segments = failed_id.split('_')
    start = len(segments) - 1 if segments and segments[-1].isdigit() else len(segments)

    for end in range(start, 0, -1):
        prefix = '_'.join(segments[:end])
        matches = [s for s in candidates_pool if s.startswith(prefix + '_') or s == prefix]
        if len(matches) >= min_refs:
            return _sort_by_distance(matches)[:max_refs]

    # 兜底：全量按距离排序
    return _sort_by_distance(candidates_pool)[:max_refs]


_SYNTHESIZE_SYSTEM = """# Role
你是一位顶级的视觉小说 (Visual Novel) 环境概念美术师和 AI 绘图 Prompt 专家。

# Task
我提供了一个【目标场景 ID】以及若干【参考场景 ID 及其已生成的图片提示词】。
请从参考提示词中提炼出该场景类型的通用模式，然后为目标场景生成一个全新的提示词。


# Rules（核心铁律）
1. **严格复用参考中的已有元素，禁止引入新物体**：
   - 只能使用参考提示词中已经出现过的【位置、环境物件】元素
   - 绝对禁止引入参考中不存在的任何物体或元素（例如：参考里没有"窗户"，就不能出现"window"；参考里没有"雨伞"，就不能出现"umbrella"）
   - 严禁出现任何形式的人物描述（people, human, character, man, woman, person, figure 等绝对不能出现）
2. **必须根据目标场景 ID 语义决定的维度**：
   - **光线色温**：由目标场景 ID 的时间词决定（day→暖白自然光, night→昏暗冷光/暖灯光, dusk→橙红暖光, dawn→冷蓝晨光）
   - **光线强度**：由目标场景 ID 的时间词和氛围词共同决定（quiet/calm→柔和, tense/dramatic→强烈对比, intimate→低调暖光）
   - **氛围情绪**：严格由目标场景 ID 的氛围词决定，不得沿用参考场景的氛围
   - **镜头角度/构图**：可以在 wide shot、medium shot、close-up、low angle、high angle、dutch angle、bird's eye view 等之间自由切换
3. **禁止改变以下内容**：
   - 物件种类（参考中有什么物件，新提示词只能用这些物件）
   - 物件数量
   - 物件布局
4. **严格遵循目标场景 ID 的语义前缀**：
   - 目标场景 ID 的前缀定义了位置、时间、氛围，必须确保生成的提示词与之匹配
   - scene_bedroom_day_quiet_03 → 位置=bedroom, 时间=day, 氛围=quiet（安静、柔和）
   - scene_bedroom_night_intimate_01 → 位置=bedroom, 时间=night, 氛围=intimate（亲密感、暖光、私密）
   - scene_house_interior_day_calm_37 → 位置=house_interior, 时间=day, 氛围=calm（平静、温馨）
   - scene_house_interior_night_quiet_03 → 位置=house_interior, 时间=night, 氛围=quiet（安静、昏暗）
4. **提示词格式（Stable Diffusion 风格）**：
   - 纯英文逗号分隔的短语
   - 必须包含：画面主体描述, 环境细节, 时间/光线, 氛围情绪, 摄影镜头
   - 结尾必须包含：no humans
5. **不写画风词 / 不写质量词 (No Style/Quality Tags)**：
   - 严禁出现：masterpiece, best quality, highres, illustration, key visual, cinematic lighting, detailed background, dramatic atmosphere 等通用质量词。
   - 严禁出现：Ancient Chinese Style / Cyberpunk Style / Japanese Anime Style / 2D Anime Art Style / photorealistic / watercolor painting 等画风大类。
   - 这些词会由后处理从 story_planner 的 global_style 统一追加，写进 image_prompt 会造成冲突。
   - 镜头/光影描述本身（如 wide shot, close-up, rim light, backlight, golden hour, neon glow, lens flare）属于画面要素，可以保留。

# Output Format
只输出一个严格符合 JSON 格式的对象，不包含任何额外文字、解释或标记：
{
  "image_prompt": "...（完整的英文提示词）, no humans"
}

# 操作示例（严格遵守，禁止创造示例之外的新元素）
参考1: "dark cyberpunk street at midnight, neon blue and pink signs, wet pavement reflecting lights, puddles, trash scattered, oppressive tension, wide shot, no humans"
参考2: "dark alleyway at night, red neon sign glowing, wet ground, old brick wall, steam rising from grates, isolated silence, medium shot, no humans"
→ 目标: scene_cyberpunk_street_rain_night_03
→ 提取可用元素（均来自参考）: cyberpunk street, midnight/night, neon signs, wet pavement/puddles, steam, brick wall, oppressive/tense/silent, trash, grates
→ 禁止引入: 参考中无"雨伞"、"外卖盒"、"监控摄像头"等 → 绝对不能出现
→ 生成（复用元素 + 仅改变镜头构图）:
"dark cyberpunk street at midnight, neon blue and pink signs glowing, wet pavement reflecting lights, puddles on the ground, trash scattered near old brick wall, steam rising from vent grates, oppressive and isolated silence, low angle shot, no humans"

"""

def synthesize_prompt_from_references(failed_id: str, ref_id_prompts: list) -> str:
    """
    根据参考场景提示词，用 LLM 合成 failed_id 的新提示词。
    ref_id_prompts: [(scene_id, prompt_text), ...]
    返回合成后的 image_prompt 字符串（不含 global_style）。
    """
    import dotenv
    dotenv.load_dotenv(os.path.join(project_root, "novel-game-factory", ".env"))
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        raise ValueError("DASHSCOPE_API_KEY 未设置")

    # 从目标场景 ID 按位置拆解语义：scene_[地点...]_[时间]_[氛围]_[序号]
    # 去掉 "scene" 前缀和末尾序号后，倒数第1=氛围，倒数第2=时间，其余=地点
    parts = [p for p in failed_id.split('_') if p and p != 'scene']
    if parts and parts[-1].isdigit():
        parts = parts[:-1]

    if len(parts) >= 3:
        mood_words  = [parts[-1]]
        time_words  = [parts[-2]]
        place_words = parts[:-2]
    elif len(parts) == 2:
        mood_words  = [parts[-1]]
        time_words  = []
        place_words = [parts[0]]
    else:
        mood_words  = []
        time_words  = []
        place_words = parts

    semantic_lines = []
    if place_words:
        semantic_lines.append(f"  - Location : {', '.join(place_words)}")
    if time_words:
        semantic_lines.append(f"  - Time/Light: {', '.join(time_words)}")
    if mood_words:
        semantic_lines.append(f"  - Mood/Atmos: {', '.join(mood_words)}")
    target_semantics_block = "\n".join(semantic_lines) if semantic_lines else f"  - Keywords: {', '.join(parts)}"

    ref_lines = "\n".join(
        f"- [{sid}]: {prompt}" for sid, prompt in ref_id_prompts
    )
    user_content = (
        f"Target scene ID: {failed_id}\n"
        f"Semantic breakdown of target scene ID:\n{target_semantics_block}\n\n"
        f"Reference scenes (use ONLY the elements that appear in these prompts):\n{ref_lines}\n\n"
        f"Please synthesize a new image prompt for the target scene, "
        f"strictly reusing elements from the reference prompts above, "
        f"and adjusting location/lighting/mood to match the target scene's semantic breakdown."
    )
    client = OpenAI(
        api_key=api_key,
        base_url=os.getenv(
            "DASHSCOPE_COMPATIBLE_BASE_URL",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        ),
        timeout=float(os.getenv("SCENE_LLM_TIMEOUT_SECONDS", "120")),
        max_retries=max(0, int(os.getenv("SCENE_LLM_MAX_RETRIES", "2"))),
    )
    response = client.chat.completions.create(
        model="qwen3-max",
        messages=[
            {"role": "system", "content": _SYNTHESIZE_SYSTEM},
            {"role": "user", "content": user_content},
        ],
        extra_body={"enable_thinking": False},
    )
    raw = response.choices[0].message.content.strip()
    match = re.search(r'\{.*?"image_prompt"\s*:\s*"(.+?)"\s*\}', raw, re.DOTALL)
    if match:
        return match.group(1).strip()
    raise ValueError(f"LLM 返回格式无法解析: {raw[:200]}")


def _persist_synthesized_prompt(task_id: str, scene_id: str, new_prompt: str,
                                ref_id_prompts: list = None) -> None:
    """将合成的提示词写回 scenes 表的 data 和 failed_scenes 字段。
    ref_id_prompts: [(scene_id, prompt_text), ...]
    """
    # scene_prompts is the authoritative input consumed by scene_generator and
    # by the next resume attempt.  Persist it first; otherwise a successful
    # synthesized image would be invalidated and synthesized again forever.
    platform = _platform_engine()
    with platform.begin() as conn:
        lock_suffix = " FOR UPDATE" if conn.dialect.name == "postgresql" else ""
        row = conn.execute(
            text(
                "SELECT data FROM scene_prompts WHERE task_id = :tid "
                f"ORDER BY create_time DESC LIMIT 1{lock_suffix}"
            ),
            {"tid": task_id},
        ).fetchone()
        if not row:
            raise RuntimeError(
                f"scene_prompts 表缺少 task_id={task_id}，无法写回合成 prompt"
            )
        prompt_data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        if scene_id not in prompt_data:
            raise RuntimeError(f"scene_prompts.data 缺少 scene_id={scene_id}")
        prompt_data[scene_id]["image_prompt"] = new_prompt
        update_sql = (
            "UPDATE scene_prompts SET data = CAST(:data AS JSONB), "
            "update_time = NOW() WHERE task_id = :tid"
            if conn.dialect.name == "postgresql"
            else "UPDATE scene_prompts SET data = :data WHERE task_id = :tid"
        )
        result = conn.execute(
            text(update_sql),
            {
                "data": json.dumps(prompt_data, ensure_ascii=False),
                "tid": task_id,
            },
        )
        if result.rowcount != 1:
            raise RuntimeError(
                f"scene_prompts 合成 prompt 写回未命中唯一记录: task_id={task_id}"
            )

    engine = _engine()
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT data, failed_scenes FROM scenes WHERE task_id = :tid ORDER BY create_time DESC LIMIT 1"),
            {"tid": task_id}
        ).fetchone()
        if not row:
            raise RuntimeError(f"scenes 表缺少 task_id={task_id}，无法写回合成 prompt")
        data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        raw_fs = row[1]
        if raw_fs:
            failed_scenes = raw_fs if isinstance(raw_fs, dict) else json.loads(raw_fs)
        else:
            failed_scenes = {}

        if scene_id not in data:
            raise RuntimeError(f"scenes.data 缺少 scene_id={scene_id}")
        data[scene_id]["image_prompt"] = new_prompt

        entry = failed_scenes.get(scene_id, {})
        entry["status"] = "synthesized"
        entry["synthesized_prompt"] = new_prompt
        if "reason" not in entry:
            entry["reason"] = ""
        if ref_id_prompts is not None:
            entry["references"] = {sid: prompt for sid, prompt in ref_id_prompts}
        failed_scenes[scene_id] = entry

        result = conn.execute(
            text("UPDATE scenes SET data = :d, failed_scenes = :fs WHERE task_id = :tid"),
            {
                "d": json.dumps(data, ensure_ascii=False),
                "fs": json.dumps(failed_scenes, ensure_ascii=False),
                "tid": task_id,
            }
        )
        if result.rowcount != 1:
            raise RuntimeError(
                f"scenes 合成 prompt 写回未命中唯一记录: task_id={task_id}"
            )


# ---------------------------------------------------------------------------
# Prompt rewriter for content moderation failures
# ---------------------------------------------------------------------------

_REWRITE_SYSTEM = (
    "You are an expert AI image prompt engineer for visual novel backgrounds. "
    "Your task is to rewrite an image generation prompt that was blocked by content moderation, "
    "while preserving the original scene's atmosphere, location, time of day, and visual style as closely as possible. "
    "Rules:\n"
    "1. Keep the same location, lighting, and mood.\n"
    "2. Remove or rephrase any words that may trigger content moderation (e.g. sensitive cultural/political terms, "
    "explicit references, potentially sensitive scene names).\n"
    "3. Never add any human characters.\n"
    "4. Output ONLY a JSON object: {\"image_prompt\": \"...rewritten prompt...\"}"
)


def rewrite_prompt_for_moderation(original_prompt: str) -> str:
    """调用 LLM 重写被内容审核拦截的 prompt，保持场景近似"""
    import dotenv
    dotenv.load_dotenv(os.path.join(project_root, "novel-game-factory", ".env"))
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        raise ValueError("DASHSCOPE_API_KEY 未设置")

    client = OpenAI(
        api_key=api_key,
        base_url=os.getenv(
            "DASHSCOPE_COMPATIBLE_BASE_URL",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        ),
        timeout=float(os.getenv("SCENE_LLM_TIMEOUT_SECONDS", "120")),
        max_retries=max(0, int(os.getenv("SCENE_LLM_MAX_RETRIES", "2"))),
    )
    response = client.chat.completions.create(
        model="qwen3-max",
        messages=[
            {"role": "system", "content": _REWRITE_SYSTEM},
            {"role": "user", "content": f"Original prompt:\n{original_prompt}"},
        ],
        extra_body={"enable_thinking": False},
    )
    raw = response.choices[0].message.content.strip()
    # 提取 JSON
    match = re.search(r'\{.*?"image_prompt"\s*:\s*"(.+?)"\s*\}', raw, re.DOTALL)
    if match:
        return match.group(1).strip()
    raise ValueError(f"LLM 返回格式无法解析: {raw[:200]}")


# ---------------------------------------------------------------------------
# Retry logic
# ---------------------------------------------------------------------------

SCENE_NEGATIVE_PROMPT = (
    "people, person, human, character, face, body, hands, limbs, head, eyes, nose, mouth, "
    "portrait, figure, man, woman, child, individual, crowd, group, "
    "facial features, human presence, human figures, humans, "
    "blurry, low quality, distorted, artifact, watermark, text, signature, "
    "simple, plain, boring, repetitive patterns"
)


def _write_image_data(out_path: str | Path, data: bytes) -> None:
    with atomic_image_output(out_path) as temporary_path:
        temporary_path.write_bytes(data)


def _render_with_active_scene_style(
    task_id: str,
    prompt: str,
    negative_prompt: str,
    image_gen,
    max_retries: int,
    *,
    scene_id: Optional[str] = None,
):
    """Render through text-to-image or the confirmed scene Anchor."""
    style_plan = resolve_asset_style_plan(task_id, ASSET_SCENE)
    styled_prompt = apply_asset_style(style_plan, prompt)
    if not style_plan.uses_anchor:
        return image_gen.retry_on_failure(
            prompt=styled_prompt,
            negative_prompt=negative_prompt,
            width=int(os.getenv("SCENE_WIDTH", "1792")),
            height=int(os.getenv("SCENE_HEIGHT", "1008")),
            max_retries=max_retries,
        )

    reference_path = style_plan.anchor_path
    edit_prompt = compose_anchor_guided_prompt(style_plan, styled_prompt)
    if scene_id:
        try:
            from scene_generator import _parent_scene_id, compose_img2img_prompt
        except ImportError:
            from services.scene_painter.core.scene_generator import (  # type: ignore
                _parent_scene_id,
                compose_img2img_prompt,
            )
        parent = _parent_scene_id(scene_id)
        if parent:
            candidate = Path(OUTPUTS_PATH) / task_id / "images" / "scenes" / f"{_safe_name(parent)}.png"
            if validate_image_file(candidate):
                reference_path = candidate
                edit_prompt = compose_img2img_prompt(styled_prompt)

    return image_gen.edit_image_with_retry(
        base_image_path=reference_path,
        prompt=edit_prompt,
        width=int(os.getenv("SCENE_WIDTH", "1792")),
        height=int(os.getenv("SCENE_HEIGHT", "1008")),
        max_retries=max_retries,
    )


def _generate_scene_image(task_id: str, scene_id: str, prompt: str, out_path: str, image_gen, max_retries: int,
                           label: str = "") -> tuple:
    """
    尝试生成单张场景图片。
    返回 (ok: bool, failure: FailureRecord | None)
    label 用于日志区分（如 "重写后"、"合成后"）。
    """
    suffix = f" ({label})" if label else ""
    # 统一追加画风 + 16:9 比例提示（与主流程一致）
    try:
        from scene_generator import (
            ASPECT_RATIO_SUFFIX,
            SCENE_STYLE_SUFFIX,
            _parent_scene_id,
            build_scene_checkpoint_signature,
        )
    except ImportError:
        from services.scene_painter.core.scene_generator import (
            ASPECT_RATIO_SUFFIX,
            SCENE_STYLE_SUFFIX,
            _parent_scene_id,
            build_scene_checkpoint_signature,
        )
    if SCENE_STYLE_SUFFIX not in prompt:
        prompt = f"{prompt}{SCENE_STYLE_SUFFIX}"
    if ASPECT_RATIO_SUFFIX not in prompt:
        prompt = f"{prompt}{ASPECT_RATIO_SUFFIX}"
    style_plan = resolve_asset_style_plan(task_id, ASSET_SCENE)
    parent = _parent_scene_id(scene_id)
    parent_path = (
        Path(OUTPUTS_PATH)
        / task_id
        / "images"
        / "scenes"
        / f"{_safe_name(parent)}.png"
        if parent
        else None
    )
    effective_parent = (
        parent_path
        if parent_path is not None and validate_image_file(parent_path)
        else None
    )
    signature_config = SimpleNamespace(
        api_model=str(getattr(image_gen, "model", os.getenv("API_MODEL", "gpt-image-2"))),
        api_quality=os.getenv("GPT_IMAGE_QUALITY", "low"),
        scene=SimpleNamespace(
            width=int(os.getenv("SCENE_WIDTH", "1792")),
            height=int(os.getenv("SCENE_HEIGHT", "1008")),
        ),
    )
    input_digest, dependency_digest = build_scene_checkpoint_signature(
        task_id,
        signature_config,
        style_plan,
        scene_id,
        prompt,
        SCENE_NEGATIVE_PROMPT,
        effective_parent,
    )
    try:
        assert_retry_budget(
            task_id,
            "scene_painter",
            "generate_scene",
            scene_id,
            input_digest=input_digest,
            generator_version="scene-image-v2-resume",
            dependency_digest=dependency_digest,
        )
        checkpoint_mark_running(
            task_id,
            "scene_painter",
            "generate_scene",
            scene_id,
            input_digest=input_digest,
            generator_version="scene-image-v2-resume",
            dependency_digest=dependency_digest,
        )
        img = _render_with_active_scene_style(
            task_id,
            prompt,
            SCENE_NEGATIVE_PROMPT,
            image_gen,
            max_retries,
            scene_id=scene_id,
        )
        if _usage_tracker:
            _usage_tracker.add_image(
                1,
                image_gen.model,
                provider=os.getenv("IMAGE_PROVIDER", "openai_compatible"),
            )
        _write_image_data(out_path, img.data)
        checkpoint_mark_success(
            task_id,
            "scene_painter",
            "generate_scene",
            scene_id,
            input_digest=input_digest,
            generator_version="scene-image-v2-resume",
            dependency_digest=dependency_digest,
            output_paths=[out_path],
            validator=validate_image_file,
        )
        logger.info(f"  ✓ {scene_id}{suffix} -> {out_path}")
        return True, None
    except Exception as e:
        if _categorize_error(str(e)) == "CONTENT_MODERATION":
            logger.warning(f"  内容审核拦截 {scene_id}{suffix}，尝试 LLM 重写 prompt...")
            try:
                rewritten = ensure_asset_style(
                    task_id,
                    ASSET_SCENE,
                    rewrite_prompt_for_moderation(prompt),
                )
                logger.info(f"  重写后 prompt: {rewritten[:80]}...")
                img = _render_with_active_scene_style(
                    task_id,
                    rewritten,
                    SCENE_NEGATIVE_PROMPT,
                    image_gen,
                    max_retries,
                    scene_id=scene_id,
                )
                if _usage_tracker:
                    _usage_tracker.add_image(
                        1,
                        image_gen.model,
                        provider=os.getenv("IMAGE_PROVIDER", "openai_compatible"),
                    )
                _write_image_data(out_path, img.data)
                checkpoint_mark_success(
                    task_id,
                    "scene_painter",
                    "generate_scene",
                    scene_id,
                    input_digest=input_digest,
                    generator_version="scene-image-v2-resume",
                    dependency_digest=dependency_digest,
                    output_paths=[out_path],
                    validator=validate_image_file,
                    result_mode="moderation_rewrite",
                )
                logger.info(f"  ✓ {scene_id}{suffix} (重写后) -> {out_path}")
                return True, None
            except Exception as e2:
                err = f"{label + ' ' if label else ''}重写后失败: {e2}"
                checkpoint_mark_failed(
                    task_id,
                    "scene_painter",
                    "generate_scene",
                    scene_id,
                    input_digest=input_digest,
                    generator_version="scene-image-v2-resume",
                    dependency_digest=dependency_digest,
                    error=err,
                )
                logger.error(f"  ✗ {scene_id} {err}")
                return False, FailureRecord(scene_id, "scene", prompt, err)
        else:
            logger.error(f"  ✗ {scene_id}{suffix} 生成失败: {e}")
            checkpoint_mark_failed(
                task_id,
                "scene_painter",
                "generate_scene",
                scene_id,
                input_digest=input_digest,
                generator_version="scene-image-v2-resume",
                dependency_digest=dependency_digest,
                error=str(e),
            )
            return False, FailureRecord(scene_id, "scene", prompt, str(e))


def retry_scenes(task_id: str, missing_ids: list, scenes_data: dict, global_style: str) -> tuple:
    """重新生成缺失的场景图片，返回 (success, total, failures)"""
    if not missing_ids:
        logger.info("没有缺失的场景图片，跳过")
        return 0, 0, []

    scenes_dir = os.path.join(OUTPUTS_PATH, task_id, "images", "scenes")
    os.makedirs(scenes_dir, exist_ok=True)

    image_gen, max_retries = _make_image_generator()
    success = 0
    failures = []
    # 收集本轮无 prompt 的场景，留给第二阶段合成
    no_prompt_ids = []

    logger.info(f"开始重新生成 {len(missing_ids)} 个缺失场景图片...")
    for scene_id in missing_ids:
        scene_info = scenes_data.get(scene_id, {})
        raw_prompt = scene_info.get("image_prompt", "")
        if not raw_prompt:
            no_prompt_ids.append(scene_id)
            continue

        prompt = ensure_asset_style(task_id, ASSET_SCENE, raw_prompt)
        out_path = os.path.join(scenes_dir, f"{_safe_name(scene_id)}.png")
        ok, rec = _generate_scene_image(
            task_id, scene_id, prompt, out_path, image_gen, max_retries
        )
        if ok:
            success += 1
        else:
            failures.append(rec)

    # -----------------------------------------------------------------------
    # 第二阶段：对无 prompt 的场景，参考同前缀兄弟场景合成新提示词
    # -----------------------------------------------------------------------
    if no_prompt_ids:
        all_scene_ids = list(scenes_data.keys())
        logger.info(f"[SYNTHESIZE] 开始为 {len(no_prompt_ids)} 个无提示词场景合成 prompt...")

        for scene_id in no_prompt_ids:
            ref_ids = find_reference_scene_ids(scene_id, all_scene_ids)
            # 过滤掉自身及无 prompt 的参考
            ref_id_prompts = [
                (sid, scenes_data[sid]["image_prompt"])
                for sid in ref_ids
                if sid in scenes_data and scenes_data[sid].get("image_prompt")
            ]

            if len(ref_id_prompts) < 2:
                logger.warning(f"  [SYNTHESIZE] {scene_id} 找不到足够参考场景（找到 {len(ref_id_prompts)} 个），跳过")
                failures.append(FailureRecord(scene_id, "scene", "", "参考场景不足，无法合成 prompt"))
                continue

            logger.info(f"  [SYNTHESIZE] {scene_id} 参考: {[s for s, _ in ref_id_prompts]}")
            try:
                synthesized = synthesize_prompt_from_references(scene_id, ref_id_prompts)
                logger.info(f"  [SYNTHESIZE] 合成 prompt: {synthesized[:100]}...")
                # 写回 DB，记录参考和合成结果
                _persist_synthesized_prompt(task_id, scene_id, synthesized,
                                            ref_id_prompts=ref_id_prompts)
                # 同步写回内存，避免下一轮重试时重复合成
                if scene_id in scenes_data:
                    scenes_data[scene_id]["image_prompt"] = synthesized
                else:
                    scenes_data[scene_id] = {"image_prompt": synthesized}
            except Exception as e:
                logger.error(f"  [SYNTHESIZE] {scene_id} 合成失败: {e}")
                failures.append(FailureRecord(scene_id, "scene", "", f"prompt 合成失败: {e}"))
                continue

            prompt = ensure_asset_style(task_id, ASSET_SCENE, synthesized)
            out_path = os.path.join(scenes_dir, f"{_safe_name(scene_id)}.png")
            ok, rec = _generate_scene_image(
                task_id,
                scene_id,
                prompt,
                out_path,
                image_gen,
                max_retries,
                label="合成后",
            )
            if ok:
                success += 1
            else:
                failures.append(rec)

    total = len(missing_ids)
    logger.info(f"场景重试完成: {success}/{total} 成功")
    return success, total, failures


def _scene_image_path(task_id: str, scene_id: str) -> str:
    return os.path.join(OUTPUTS_PATH, task_id, "images", "scenes", f"{_safe_name(scene_id)}.png")


def _mark_scene_fallback_checkpoint(
    task_id: str,
    scene_id: str,
    scene_info: dict,
    source_path: str | Path,
    target_path: str | Path,
) -> None:
    try:
        from scene_generator import (
            ASPECT_RATIO_SUFFIX,
            SCENE_STYLE_SUFFIX,
            build_scene_checkpoint_signature,
        )
    except ImportError:
        from services.scene_painter.core.scene_generator import (  # type: ignore
            ASPECT_RATIO_SUFFIX,
            SCENE_STYLE_SUFFIX,
            build_scene_checkpoint_signature,
        )
    style_plan = resolve_asset_style_plan(task_id, ASSET_SCENE)
    prompt = apply_asset_style(style_plan, (scene_info or {}).get("image_prompt", ""))
    prompt = f"{prompt}{SCENE_STYLE_SUFFIX}{ASPECT_RATIO_SUFFIX}"
    config = SimpleNamespace(
        api_model=os.getenv("API_MODEL", "gpt-image-2"),
        api_quality=os.getenv("GPT_IMAGE_QUALITY", "low"),
        scene=SimpleNamespace(
            width=int(os.getenv("SCENE_WIDTH", "1792")),
            height=int(os.getenv("SCENE_HEIGHT", "1008")),
        ),
    )
    input_digest, dependency_digest = build_scene_checkpoint_signature(
        task_id,
        config,
        style_plan,
        scene_id,
        prompt,
        SCENE_NEGATIVE_PROMPT,
        Path(source_path),
    )
    checkpoint_mark_success(
        task_id,
        "scene_painter",
        "generate_scene",
        scene_id,
        input_digest=input_digest,
        generator_version="scene-image-v2-resume",
        dependency_digest=dependency_digest,
        output_paths=[target_path],
        validator=validate_image_file,
        result_mode="fallback_copy",
    )


def fallback_copy_from_reference_images(task_id: str, failed_scene_ids: list,
                                        scenes_data: dict) -> tuple:
    """
    最后兜底：对仍然失败的场景，直接复制最相似且已存在图片的参考场景图片。
    返回 (copied_ids, still_failed_ids)
    """
    if not failed_scene_ids:
        return [], []

    # 只选有图片在盘上的场景作为参考池
    candidate_pool = []
    for sid in scenes_data.keys():
        if sid in failed_scene_ids:
            continue
        path = _scene_image_path(task_id, sid)
        if validate_image_file(path):
            candidate_pool.append(sid)

    if not candidate_pool:
        logger.warning("[FALLBACK] 没有任何已成功的场景图片可用作参考，跳过兜底拷贝")
        return [], list(failed_scene_ids)

    copied = []
    still_failed = []
    logger.info(f"[FALLBACK] 开始兜底拷贝，{len(failed_scene_ids)} 个失败场景，参考池 {len(candidate_pool)} 个")

    for failed_id in failed_scene_ids:
        ref_ids = find_reference_scene_ids(failed_id, candidate_pool)
        if not ref_ids:
            logger.warning(f"  [FALLBACK] {failed_id} 找不到参考场景")
            still_failed.append(failed_id)
            continue

        ref_id = ref_ids[0]
        src = _scene_image_path(task_id, ref_id)
        dst = _scene_image_path(task_id, failed_id)
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            atomic_copy_image(src, dst)
            _mark_scene_fallback_checkpoint(
                task_id,
                failed_id,
                scenes_data.get(failed_id, {}),
                src,
                dst,
            )
            logger.info(f"  [FALLBACK] {failed_id} <- {ref_id} ({src} -> {dst})")
            copied.append(failed_id)
        except Exception as e:
            logger.error(f"  [FALLBACK] {failed_id} 拷贝失败: {e}")
            still_failed.append(failed_id)

    return copied, still_failed


def retry_scenes_multi_round(task_id: str, missing_ids: list, scenes_data: dict,
                             global_style: str, max_rounds: int = 5) -> tuple:
    """
    多轮重试 + 最终兜底拷贝。
    每轮调用 retry_scenes 重试当前剩余失败列表；最多 max_rounds 轮。
    多轮后仍失败，则用 fallback_copy_from_reference_images 复制最相似参考图片兜底。
    返回 (success, total, failures)，total 为最初的缺失数。
    """
    if not missing_ids:
        logger.info("没有缺失的场景图片，跳过")
        return 0, 0, []

    total = len(missing_ids)
    pending = list(missing_ids)
    cumulative_success = 0
    last_failures = []

    for round_idx in range(1, max_rounds + 1):
        logger.info(f"=== 场景重试第 {round_idx}/{max_rounds} 轮，待重试 {len(pending)} 个 ===")
        success, _, failures = retry_scenes(task_id, pending, scenes_data, global_style)
        cumulative_success += success
        last_failures = failures

        failed_ids = [f.asset_id for f in failures]
        if not failed_ids:
            logger.info(f"=== 第 {round_idx} 轮全部成功，结束多轮重试 ===")
            break
        if round_idx == max_rounds:
            logger.warning(f"=== 已达最大轮数 {max_rounds}，仍有 {len(failed_ids)} 个失败 ===")
            break
        pending = failed_ids

    # 最终兜底：复制最相似参考场景的图片
    final_failed_ids = [f.asset_id for f in last_failures]
    if final_failed_ids:
        copied, still_failed = fallback_copy_from_reference_images(
            task_id, final_failed_ids, scenes_data
        )
        cumulative_success += len(copied)
        if copied:
            still_failed_set = set(still_failed)
            last_failures = [f for f in last_failures if f.asset_id in still_failed_set]

    logger.info(f"场景多轮重试 + 兜底完成: {cumulative_success}/{total} 成功")
    return cumulative_success, total, last_failures


def retry_backgrounds(task_id: str, missing: dict, bg_data: dict) -> tuple:
    """重新生成缺失的封面图片，返回 (success, total, failures)"""
    missing_chapters = missing.get("chapters", [])
    missing_novel = missing.get("novel_cover", False)
    total = len(missing_chapters) + (1 if missing_novel else 0)

    if total == 0:
        logger.info("没有缺失的封面图片，跳过")
        return 0, 0, []

    bg_dir = os.path.join(OUTPUTS_PATH, task_id, "images", "backgrounds")
    os.makedirs(bg_dir, exist_ok=True)

    image_gen, max_retries = _make_image_generator()
    style_plan = resolve_asset_style_plan(task_id, ASSET_SCENE)
    try:
        from cover_generator import (
            _ensure_aspect_ratio,
            build_cover_checkpoint_digest,
        )
    except ImportError:
        from services.scene_painter.core.cover_generator import (  # type: ignore
            _ensure_aspect_ratio,
            build_cover_checkpoint_digest,
        )
    cover_size = (1792, 1008)
    cover_quality = os.getenv("GPT_IMAGE_QUALITY", "high")
    chapter_prompts = bg_data.get("generated_prompts", {})
    success = 0
    failures = []

    if missing_chapters:
        logger.info(f"开始重新生成 {len(missing_chapters)} 个缺失章节封面...")
        for node_id in missing_chapters:
            prompt_data = chapter_prompts.get(node_id, {})
            prompt = ensure_asset_style(
                task_id,
                ASSET_SCENE,
                prompt_data.get("prompt", ""),
            )
            negative_prompt = prompt_data.get("negative_prompt", "")
            prompt = _ensure_aspect_ratio(prompt)
            if not prompt:
                logger.warning(f"节点 {node_id} 没有 prompt，跳过")
                failures.append(FailureRecord(node_id, "chapter_cover", "", "没有 prompt"))
                continue
            out_path = os.path.join(bg_dir, f"{node_id}.png")
            input_digest = build_cover_checkpoint_digest(
                str(node_id),
                prompt,
                negative_prompt,
                style_plan,
                model=str(image_gen.model),
                image_size=cover_size,
                quality=cover_quality,
            )
            try:
                assert_retry_budget(
                    task_id,
                    "scene_painter",
                    "generate_cover",
                    str(node_id),
                    input_digest=input_digest,
                    generator_version="cover-image-v2-resume",
                )
                checkpoint_mark_running(
                    task_id,
                    "scene_painter",
                    "generate_cover",
                    str(node_id),
                    input_digest=input_digest,
                    generator_version="cover-image-v2-resume",
                )
                img = _render_with_active_scene_style(
                    task_id,
                    prompt,
                    negative_prompt,
                    image_gen,
                    max_retries,
                )
                if _usage_tracker:
                    _usage_tracker.add_image(
                        1,
                        image_gen.model,
                        provider=os.getenv("IMAGE_PROVIDER", "openai_compatible"),
                    )
                _write_image_data(out_path, img.data)
                checkpoint_mark_success(
                    task_id,
                    "scene_painter",
                    "generate_cover",
                    str(node_id),
                    input_digest=input_digest,
                    generator_version="cover-image-v2-resume",
                    output_paths=[out_path],
                    validator=validate_image_file,
                )
                success += 1
                logger.info(f"  ✓ {node_id} -> {out_path}")
            except Exception as e:
                if _categorize_error(str(e)) == "CONTENT_MODERATION":
                    logger.warning(f"  内容审核拦截 {node_id}，尝试 LLM 重写 prompt...")
                    try:
                        rewritten = ensure_asset_style(
                            task_id,
                            ASSET_SCENE,
                            rewrite_prompt_for_moderation(prompt),
                        )
                        logger.info(f"  重写后 prompt: {rewritten[:80]}...")
                        img = _render_with_active_scene_style(
                            task_id,
                            rewritten,
                            negative_prompt,
                            image_gen,
                            max_retries,
                        )
                        if _usage_tracker:
                            _usage_tracker.add_image(
                                1,
                                image_gen.model,
                                provider=os.getenv("IMAGE_PROVIDER", "openai_compatible"),
                            )
                        _write_image_data(out_path, img.data)
                        checkpoint_mark_success(
                            task_id,
                            "scene_painter",
                            "generate_cover",
                            str(node_id),
                            input_digest=input_digest,
                            generator_version="cover-image-v2-resume",
                            output_paths=[out_path],
                            validator=validate_image_file,
                            result_mode="moderation_rewrite",
                        )
                        success += 1
                        logger.info(f"  ✓ {node_id} (重写后) -> {out_path}")
                    except Exception as e2:
                        checkpoint_mark_failed(
                            task_id,
                            "scene_painter",
                            "generate_cover",
                            str(node_id),
                            input_digest=input_digest,
                            generator_version="cover-image-v2-resume",
                            error=str(e2),
                        )
                        logger.error(f"  ✗ {node_id} 重写后仍失败: {e2}")
                        failures.append(FailureRecord(node_id, "chapter_cover", prompt, f"重写后失败: {e2}"))
                else:
                    checkpoint_mark_failed(
                        task_id,
                        "scene_painter",
                        "generate_cover",
                        str(node_id),
                        input_digest=input_digest,
                        generator_version="cover-image-v2-resume",
                        error=str(e),
                    )
                    logger.error(f"  ✗ {node_id} 生成失败: {e}")
                    failures.append(FailureRecord(node_id, "chapter_cover", prompt, str(e)))

    if missing_novel:
        novel_meta = bg_data.get("novel_cover_metadata", {})
        prompt = ensure_asset_style(
            task_id,
            ASSET_SCENE,
            novel_meta.get("prompt", ""),
        )
        negative_prompt = novel_meta.get("negative_prompt", "")
        prompt = _ensure_aspect_ratio(prompt)
        out_path = os.path.join(bg_dir, "novel_cover.png")
        input_digest = build_cover_checkpoint_digest(
            "novel_cover",
            prompt,
            negative_prompt,
            style_plan,
            model=str(image_gen.model),
            image_size=cover_size,
            quality=cover_quality,
        )
        logger.info("开始重新生成游戏总封面...")
        try:
            assert_retry_budget(
                task_id,
                "scene_painter",
                "generate_cover",
                "novel_cover",
                input_digest=input_digest,
                generator_version="cover-image-v2-resume",
            )
            checkpoint_mark_running(
                task_id,
                "scene_painter",
                "generate_cover",
                "novel_cover",
                input_digest=input_digest,
                generator_version="cover-image-v2-resume",
            )
            img = _render_with_active_scene_style(
                task_id,
                prompt,
                negative_prompt,
                image_gen,
                max_retries,
            )
            if _usage_tracker:
                _usage_tracker.add_image(
                    1,
                    image_gen.model,
                    provider=os.getenv("IMAGE_PROVIDER", "openai_compatible"),
                )
            _write_image_data(out_path, img.data)
            checkpoint_mark_success(
                task_id,
                "scene_painter",
                "generate_cover",
                "novel_cover",
                input_digest=input_digest,
                generator_version="cover-image-v2-resume",
                output_paths=[out_path],
                validator=validate_image_file,
            )
            success += 1
            logger.info(f"  ✓ novel_cover -> {out_path}")
        except Exception as e:
            if _categorize_error(str(e)) == "CONTENT_MODERATION":
                logger.warning(f"  内容审核拦截 novel_cover，尝试 LLM 重写 prompt...")
                try:
                    rewritten = ensure_asset_style(
                        task_id,
                        ASSET_SCENE,
                        rewrite_prompt_for_moderation(prompt),
                    )
                    logger.info(f"  重写后 prompt: {rewritten[:80]}...")
                    img = _render_with_active_scene_style(
                        task_id,
                        rewritten,
                        negative_prompt,
                        image_gen,
                        max_retries,
                    )
                    if _usage_tracker:
                        _usage_tracker.add_image(
                            1,
                            image_gen.model,
                            provider=os.getenv("IMAGE_PROVIDER", "openai_compatible"),
                        )
                    _write_image_data(out_path, img.data)
                    checkpoint_mark_success(
                        task_id,
                        "scene_painter",
                        "generate_cover",
                        "novel_cover",
                        input_digest=input_digest,
                        generator_version="cover-image-v2-resume",
                        output_paths=[out_path],
                        validator=validate_image_file,
                        result_mode="moderation_rewrite",
                    )
                    success += 1
                    logger.info(f"  ✓ novel_cover (重写后) -> {out_path}")
                except Exception as e2:
                    checkpoint_mark_failed(
                        task_id,
                        "scene_painter",
                        "generate_cover",
                        "novel_cover",
                        input_digest=input_digest,
                        generator_version="cover-image-v2-resume",
                        error=str(e2),
                    )
                    logger.error(f"  ✗ novel_cover 重写后仍失败: {e2}")
                    failures.append(FailureRecord("novel_cover", "novel_cover", prompt, f"重写后失败: {e2}"))
            else:
                checkpoint_mark_failed(
                    task_id,
                    "scene_painter",
                    "generate_cover",
                    "novel_cover",
                    input_digest=input_digest,
                    generator_version="cover-image-v2-resume",
                    error=str(e),
                )
                logger.error(f"  ✗ novel_cover 生成失败: {e}")
                failures.append(FailureRecord("novel_cover", "novel_cover", prompt, str(e)))

    logger.info(f"封面重试完成: {success}/{total} 成功")
    return success, total, failures


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(task_id: str, mode: str = "all") -> int:
    """
    检查并重试失败图片。
    mode: "scenes" | "backgrounds" | "all"
    返回 0 表示全部成功，1 表示有失败。
    """
    total_success = 0
    total_count = 0
    all_failures = []
    instance_copy_failures = []

    if mode in ("scenes", "all"):
        scenes_data = fetch_scenes_data(task_id)
        if not scenes_data:
            logger.warning(f"数据库中未找到 task_id={task_id} 的 scenes 数据")
        else:
            missing = find_missing_scenes(task_id, scenes_data)
            logger.info(f"场景图片: 共 {len(scenes_data)} 个，缺失 {len(missing)} 个")
            if missing:
                global_style = get_asset_style(task_id, ASSET_SCENE)
                s, t, failures = retry_scenes_multi_round(
                    task_id,
                    missing,
                    scenes_data,
                    global_style,
                )
                total_success += s
                total_count += t
                all_failures.extend(failures)

            # 无论本轮是否需要补图，都覆盖同步一次区域实例。
            scenes_dir = os.path.join(OUTPUTS_PATH, task_id, "images", "scenes")
            copied, instance_copy_failures = copy_scene_instances(scenes_data, scenes_dir)
            logger.info(f"场景区域实例复制: 成功 {copied} 张，失败 {len(instance_copy_failures)} 张")
            for instance_id, error in instance_copy_failures:
                all_failures.append(FailureRecord(instance_id, "scene_instance", "", error))

    if mode in ("backgrounds", "all"):
        bg_data = fetch_backgrounds_data(task_id)
        if not bg_data:
            logger.warning(f"数据库中未找到 task_id={task_id} 的 backgrounds 数据")
        else:
            missing = find_missing_backgrounds(task_id, bg_data)
            n_missing = len(missing["chapters"]) + (1 if missing["novel_cover"] else 0)
            logger.info(f"封面图片: 缺失 {n_missing} 个 (章节: {len(missing['chapters'])}, 总封面: {missing['novel_cover']})")
            if n_missing:
                s, t, failures = retry_backgrounds(task_id, missing, bg_data)
                total_success += s
                total_count += t
                all_failures.extend(failures)

    if total_count == 0 and not instance_copy_failures:
        logger.info("没有需要重试的图片")
        return 0

    logger.info(f"全部重试完成: {total_success}/{total_count} 成功")

    if all_failures:
        # 按错误类型分组打印
        from collections import defaultdict
        by_category = defaultdict(list)
        for f in all_failures:
            by_category[f.category].append(f)

        print("\n" + "=" * 60)
        print(f"失败报告: {len(all_failures)} 个图片生成失败")
        print("=" * 60)
        for category, records in by_category.items():
            print(f"\n[{category}] ({len(records)} 个)")
            for r in records:
                print(str(r))
        print("=" * 60)

    if _usage_tracker:
        _usage_tracker.flush()

    # Asset-level retry exhaustion is reportable and recoverable by the parent
    # visual task; infrastructure/command failures still raise before here.
    return 2 if (total_success != total_count or instance_copy_failures) else 0


def main():
    parser = argparse.ArgumentParser(description="检查并重新生成失败的图片")
    parser.add_argument("--task-id", required=True, help="指定 task_id")
    parser.add_argument(
        "--type",
        choices=["scenes", "backgrounds", "all"],
        default="all",
        help="重试类型: scenes / backgrounds / all (默认 all)",
    )
    args = parser.parse_args()

    if _usage_tracker:
        _usage_tracker.set_context(
            args.task_id, "scene_painter", force_new_run=True
        )

    sys.exit(run(args.task_id, args.type))


if __name__ == "__main__":
    main()
