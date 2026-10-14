#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
单个场景图片重生成工具

功能：
- 根据 task_id 和 scene_id 重新生成单个场景图片
- 从 scenes 表读取该场景的提示词
- 调用 DashScope wan2.5 API 生成图片
- 保存到对应目录

用法：
python novel-game-factory/services/scene_painter/core/redraw_scene.py --task-id <task_id> --scene-id <scene_id>
python novel-game-factory/services/scene_painter/core/redraw_scene.py --task-id abc123 --scene-id chapter_1_scene_1
"""

import json
import logging
import sys
import time
import requests
import os
import re
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

# 添加项目根目录到路径
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from common.config import DATABASE_URL, OUTPUTS_PATH
from common.visual_style import (
    ASSET_SCENE,
    apply_asset_style,
    compose_anchor_guided_prompt,
    resolve_asset_style_plan,
)

try:
    from common.usage_tracker import tracker as _usage_tracker
except Exception:
    _usage_tracker = None

load_dotenv()

# ============================================================================
# 配置
# ============================================================================

SCENE_WIDTH = 1792
SCENE_HEIGHT = 1008

# 复用 scene_generator 中的 ImageGenerator（按 API_MODEL 自动路由 gpt / wan）
try:
    from services.scene_painter.core.scene_generator import (
        ImageGenerator as _SceneImageGenerator,
        _is_qwen_model,
        _is_wan_model,
        _is_gpt_image_model,
        _parent_scene_id,
        ASPECT_RATIO_SUFFIX,
        SCENE_STYLE_SUFFIX,
        compose_img2img_prompt,
    )
except ImportError:
    from scene_generator import (  # type: ignore
        ImageGenerator as _SceneImageGenerator,
        _is_qwen_model,
        _is_wan_model,
        _is_gpt_image_model,
        _parent_scene_id,
        ASPECT_RATIO_SUFFIX,
        SCENE_STYLE_SUFFIX,
        compose_img2img_prompt,
    )

ImageGenerator = _SceneImageGenerator


def _resolve_api_credentials(model: str) -> tuple:
    """根据模型名挑 base_url / api_key（全部走 .env）"""
    if _is_qwen_model(model):
        api_key = os.getenv("GPT_IMAGE_API_KEY") or os.getenv("OPENAI_API_KEY")
        base_url = os.getenv("GPT_IMAGE_BASE_URL", "https://ai.farmmx.com")
    elif _is_wan_model(model):
        api_key = os.getenv("DASHSCOPE_API_KEY")
        base_url = os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/api/v1")
    else:
        api_key = os.getenv("GPT_IMAGE_API_KEY") or os.getenv("OPENAI_API_KEY")
        base_url = os.getenv("GPT_IMAGE_BASE_URL", "https://ai.farmmx.com")
    return base_url, api_key


_REDRAW_MODEL = os.getenv("API_MODEL", "gpt-image-2")
_REDRAW_BASE_URL, _REDRAW_API_KEY = _resolve_api_credentials(_REDRAW_MODEL)

CONFIG = {
    "base_url": _REDRAW_BASE_URL,
    "api_key": _REDRAW_API_KEY,
    "api_timeout": int(os.getenv("API_TIMEOUT", "300")),
    "api_model": _REDRAW_MODEL,
    "api_quality": os.getenv("GPT_IMAGE_QUALITY", "high"),
    "api_max_retries": int(os.getenv("API_MAX_RETRIES", "3")),
}

# ============================================================================
# 数据模型
# ============================================================================

class ImageData:
    """图片数据"""
    def __init__(self, data: bytes, format: str = 'png', width: int = 1792, height: int = 1008):
        self.data = data
        self.format = format
        self.width = width
        self.height = height

    def save_to_file(self, file_path: str) -> None:
        with open(file_path, 'wb') as f:
            f.write(self.data)


# ============================================================================
# 图片生成器
# ============================================================================

# 注意：ImageGenerator 已在文件顶部从 scene_generator 引入并按 API_MODEL 路由


# === 旧版 wan2.5 ImageGenerator（DashScope 异步任务式 API）保留备查 ===
# class ImageGenerator:
#     """图片生成器 - 调用 DashScope wan2.5 API"""
#
#     def __init__(self, api_endpoint: str, api_key: str, timeout: int = 60, model: str = "wan2.5-t2i-preview"):
#         self.api_endpoint = api_endpoint
#         self.api_key = api_key
#         self.timeout = timeout
#         self.model = model
#
#     def generate_image(self, prompt: str, negative_prompt: str = "", width: int = 1690, height: int = 960) -> ImageData:
#         """提交异步任务并轮询结果"""
#         payload = {
#             "model": self.model,
#             "input": {"prompt": prompt, "negative_prompt": negative_prompt},
#             "parameters": {"size": f"{width}*{height}", "n": 1}
#         }
#
#         headers = {
#             "Authorization": f"Bearer {self.api_key}",
#             "Content-Type": "application/json",
#             "X-DashScope-Async": "enable"
#         }
#
#         logging.info(f"提交图片生成任务: {prompt[:80]}...")
#
#         response = requests.post(
#             f"{self.api_endpoint}/services/aigc/text2image/image-synthesis",
#             json=payload, headers=headers, timeout=self.timeout
#         )
#
#         if response.status_code == 403:
#             raise Exception("API密钥无权限访问wan2.5-t2i-preview模型，请确认账户权限")
#         elif response.status_code == 401:
#             raise Exception("API密钥无效，请检查DASHSCOPE_API_KEY设置")
#
#         response.raise_for_status()
#         result = response.json()
#
#         if 'output' not in result or 'task_id' not in result['output']:
#             raise Exception(f"API返回格式错误: {result}")
#
#         task_id = result['output']['task_id']
#         logging.info(f"任务已提交，task_id: {task_id}")
#
#         # 轮询任务状态
#         for i in range(60):
#             time.sleep(2)
#             task_response = requests.get(
#                 f"{self.api_endpoint}/tasks/{task_id}",
#                 headers={"Authorization": f"Bearer {self.api_key}"},
#                 timeout=self.timeout
#             )
#             task_response.raise_for_status()
#             task_result = task_response.json()
#             task_status = task_result['output'].get('task_status', 'UNKNOWN')
#
#             if task_status == 'SUCCEEDED':
#                 image_url = task_result['output']['results'][0]['url']
#                 logging.info(f"图片生成成功，下载: {image_url}")
#                 img_response = requests.get(image_url, timeout=120)
#                 img_response.raise_for_status()
#                 return ImageData(data=img_response.content, format='png', width=width, height=height)
#             elif task_status == 'FAILED':
#                 raise Exception(f"任务失败: {task_result['output'].get('message', '未知错误')}")
#             elif task_status in ['PENDING', 'RUNNING']:
#                 continue
#             else:
#                 raise Exception(f"未知任务状态: {task_status}")
#
#         raise Exception("任务超时: 轮询60次后仍未完成")
#
#     def retry_on_failure(self, prompt: str, negative_prompt: str = "", width: int = 1690, height: int = 960, max_retries: int = 3) -> ImageData:
#         """带重试机制的图片生成"""
#         last_exception = None
#         for attempt in range(max_retries):
#             try:
#                 return self.generate_image(prompt=prompt, negative_prompt=negative_prompt, width=width, height=height)
#             except requests.RequestException as e:
#                 last_exception = e
#                 wait_time = 2 ** attempt
#                 logging.warning(f"第 {attempt + 1} 次尝试失败，{wait_time}秒后重试...")
#                 time.sleep(wait_time)
#         raise last_exception


# ============================================================================
# 数据库操作
# ============================================================================

def fetch_single_scene_from_db(task_id: str, scene_id: str) -> Optional[dict]:
    """
    从 scenes 表读取指定场景的提示词数据
    返回 {"image_prompt": ..., "negative_prompt": ...} 或 None
    """
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT data FROM scenes WHERE task_id = :tid LIMIT 1"),
            {"tid": task_id}
        ).fetchone()

    if not row:
        return None

    data = row[0] if isinstance(row[0], dict) else json.loads(row[0])

    # 去掉可能带的文件扩展名（如 .png）
    scene_key = Path(scene_id).stem if '.' in scene_id else scene_id

    scene_info = None

    # 方式1: 直接作为 key（先尝试去扩展名的版本，再尝试原始值）
    if scene_key in data:
        scene_info = data[scene_key]
    elif scene_id in data:
        scene_info = data[scene_id]
    else:
        # 方式2: 模糊匹配
        for key, value in data.items():
            if key == scene_key or key.endswith(scene_key) or scene_key.endswith(key):
                scene_info = value
                break

    return scene_info


def get_scene_output_dir(task_id: str) -> Path:
    """返回场景图片输出目录"""
    output_dir = Path(OUTPUTS_PATH) / task_id / "images" / "scenes"
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


# ============================================================================
# 主函数
# ============================================================================

def regenerate_single_scene(task_id: str, scene_id: str) -> bool:
    """
    重新生成单个场景图片

    Args:
        task_id: 任务ID
        scene_id: 场景ID/文件名（不含扩展名）

    Returns:
        True 如果成功，False 如果失败
    """
    # 1. 检查 API Key
    if not CONFIG["api_key"]:
        logging.error("API Key 未设置，请在 .env 文件中配置 GPT_IMAGE_API_KEY 或 OPENAI_API_KEY")
        return False

    # 2. 从数据库读取场景数据
    logging.info(f"从数据库读取场景 {scene_id} 的提示词 (task_id: {task_id})...")
    scene_info = fetch_single_scene_from_db(task_id, scene_id)

    if not scene_info:
        logging.error(f"未找到场景 {scene_id}，请检查 scene_id 是否正确")
        return False

    # 3. 获取正向提示词
    positive_prompt = scene_info.get("image_prompt", "")
    if not positive_prompt:
        logging.error(f"场景 {scene_id} 没有 image_prompt")
        return False

    # 4. 应用当前场景风格路由
    style_plan = resolve_asset_style_plan(task_id, ASSET_SCENE)
    logging.info(
        "场景美术风格路由: mode=%s style=%s anchor=%s",
        style_plan.mode,
        style_plan.style,
        style_plan.anchor_path or "(none)",
    )
    positive_prompt = apply_asset_style(style_plan, positive_prompt)

    # 统一追加画风 + 16:9 比例提示（与主流程一致）
    positive_prompt = f"{positive_prompt}{SCENE_STYLE_SUFFIX}{ASPECT_RATIO_SUFFIX}"

    # 5. 构造负向提示词
    negative_prompt = (
        "people, person, human, character, face, body, hands, limbs, head, eyes, nose, mouth, "
        "portrait, figure, man, woman, child, individual, crowd, group, "
        "facial features, human presence, human figures, humans, "
        "blurry, low quality, distorted, artifact, watermark, text, signature, "
        "simple, plain, boring, repetitive patterns"
    )

    # 6. 初始化生成器
    generator = ImageGenerator(
        base_url=CONFIG["base_url"],
        api_key=CONFIG["api_key"],
        timeout=CONFIG["api_timeout"],
        model=CONFIG["api_model"],
        size=f"{SCENE_WIDTH}x{SCENE_HEIGHT}",
        quality=CONFIG.get("api_quality", "high"),
        max_retries=CONFIG["api_max_retries"],
    )
    # 旧版 wan2.5 实例化（保留备查）：
    # generator = ImageGenerator(
    #     api_endpoint=CONFIG["api_endpoint"],
    #     api_key=CONFIG["api_key"],
    #     timeout=CONFIG["api_timeout"],
    #     model=CONFIG["api_model"]
    # )

    # 7. 生成图片
    logging.info(f"开始生成场景图片: {scene_id}")
    try:
        if style_plan.uses_anchor:
            reference_path = style_plan.anchor_path
            provider_prompt = compose_anchor_guided_prompt(style_plan, positive_prompt)
            parent = _parent_scene_id(scene_id)
            if parent:
                safe_parent = re.sub(r'[^\w\-]', '_', parent)
                parent_path = get_scene_output_dir(task_id) / f"{safe_parent}.png"
                if parent_path.exists() and parent_path.stat().st_size > 0:
                    reference_path = parent_path
                    provider_prompt = compose_img2img_prompt(positive_prompt)
            image_data = generator.edit_image_with_retry(
                base_image_path=reference_path,
                prompt=provider_prompt,
                width=SCENE_WIDTH,
                height=SCENE_HEIGHT,
                max_retries=CONFIG["api_max_retries"],
            )
        else:
            image_data = generator.retry_on_failure(
                prompt=positive_prompt,
                negative_prompt=negative_prompt,
                width=SCENE_WIDTH,
                height=SCENE_HEIGHT,
                max_retries=CONFIG["api_max_retries"]
            )
    except Exception as e:
        logging.error(f"图片生成失败: {e}")
        return False

    # 8. 保存图片
    output_dir = get_scene_output_dir(task_id)
    safe_name = re.sub(r'[^\w\-]', '_', scene_id)
    image_path = output_dir / f"{safe_name}.png"

    with open(image_path, 'wb') as f:
        f.write(image_data.data)

    logging.info(f"✓ 成功保存图片: {image_path}")

    # 9. 更新 usage tracker
    if _usage_tracker:
        _usage_tracker.set_context(task_id, "scene_painter")
        _usage_tracker.add_image(1, CONFIG["api_model"])
        _usage_tracker.flush()

    return True


def main():
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )

    parser = argparse.ArgumentParser(
        description="单个场景图片重生成工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例用法:
    python regenerate_single_scene.py --task-id abc123 --scene-id chapter_1_scene_1
    python regenerate_single_scene.py -t abc123 -s chapter_2_scene_3
        """
    )
    parser.add_argument(
        "--task-id", "-t",
        type=str,
        required=True,
        help="任务ID (task_id)"
    )
    parser.add_argument(
        "--scene-id", "-s",
        type=str,
        required=True,
        help="场景ID/文件名（不含扩展名），如 chapter_1_scene_1"
    )

    args = parser.parse_args()

    success = regenerate_single_scene(args.task_id, args.scene_id)

    if success:
        print(f"\n✓ 场景 {args.scene_id} 图片重生成成功！")
        return 0
    else:
        print(f"\n✗ 场景 {args.scene_id} 图片重生成失败，请检查日志")
        return 1


if __name__ == '__main__':
    sys.exit(main())
