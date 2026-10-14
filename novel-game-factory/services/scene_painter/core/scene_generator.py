#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
场景图片生成器 - 从数据库读取场景提示词并调用项目图片 provider

负责：
- 从 scene_prompts 表读取由 info_extractor/scene/scene_prompts.py 生成的提示词
- 调用 DashScope wan2.5 API 生成场景图片（每个场景10张不同镜头）
- 保存图片到本地
- 按 copy_count 保留原图并复制连续剧情区域实例（scene_id_1、scene_id_2...）

"""

import json
import logging
import sys
import time
import requests
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Dict, Any, Optional, Tuple
from tqdm import tqdm
import dotenv
from sqlalchemy import text

# 添加项目根目录到路径
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from common.config import OUTPUTS_PATH
from common.image_concurrency import (
    ThreadLocalResource,
    resolve_image_concurrency,
    run_dependency_layer,
)
from common.visual_checkpoint import (
    assert_retry_budget,
    canonical_digest,
    file_sha256,
    mark_failed as checkpoint_mark_failed,
    mark_running as checkpoint_mark_running,
    mark_success as checkpoint_mark_success,
    reusable_or_adopt_existing,
)
from common.platform_db import platform_engine
from common.visual_style import (
    ASSET_SCENE,
    AssetStylePlan,
    apply_asset_style,
    compose_anchor_guided_prompt,
    resolve_asset_style_plan,
)
from common.visual_debug import (
    record_derived_visual_asset,
    record_visual_generation,
)

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

# 支持 GPT Image 私有部署 与 DashScope Wan / 千问 系列三类 provider，按模型名自动路由
# - wan2.5* / wanx*  → WanLegacyImageTextProvider (旧异步任务接口)
# - wan2.6+ / wan2.7  → WanImageTextProvider (新同步多模态接口)
# - qwen-image*       → QwenImageTextProvider (千问同步多模态接口)
# - gpt-image-*       → GPTImageTextProvider (私有部署)
try:
    from services.scene_painter.image_providers.gpt_image import GPTImageTextProvider, GPTImageEditProvider
    from services.scene_painter.image_providers.wan_image import WanImageTextProvider
    from services.scene_painter.image_providers.wan_image_25 import WanLegacyImageTextProvider
    from services.scene_painter.image_providers.wan_config import is_legacy_wan as _is_legacy_wan
    from services.scene_painter.image_providers.qwen_image import QwenImageTextProvider, QwenImageEditProvider
    from services.scene_painter.image_providers.qwen_config import is_qwen_model as _is_qwen_model
except ImportError:
    from scene_painter.image_providers.gpt_image import GPTImageTextProvider, GPTImageEditProvider  # type: ignore
    from scene_painter.image_providers.wan_image import WanImageTextProvider  # type: ignore
    from scene_painter.image_providers.wan_image_25 import WanLegacyImageTextProvider  # type: ignore
    from scene_painter.image_providers.wan_config import is_legacy_wan as _is_legacy_wan  # type: ignore
    from scene_painter.image_providers.qwen_image import QwenImageTextProvider, QwenImageEditProvider  # type: ignore
    from scene_painter.image_providers.qwen_config import is_qwen_model as _is_qwen_model  # type: ignore


def _is_wan_model(model: str) -> bool:
    """判断是否走 DashScope 链路（wan / 千问）。

    保持向后兼容：旧调用点（cover_generator / regenerate / redraw_scene /
    retry_failed_images / Config）依然用这个函数挑 DASHSCOPE_API_KEY，
    所以 qwen-image* 也归属此处。``ImageGenerator`` 内部会再细分到具体 provider。
    """
    if not model:
        return False
    name = model.lower()
    return (
        name.startswith("wan")
        or name.startswith("wanx")
        or name.startswith("qwen-image")
    )


def _is_gpt_image_model(model: str) -> bool:
    if not model:
        return False
    return model.lower().startswith("gpt-image")

try:
    from common.usage_tracker import tracker as _usage_tracker
except Exception:
    _usage_tracker = None

dotenv.load_dotenv()

SCENE_WIDTH = 1792
SCENE_HEIGHT = 1008
# Provider 自身保留现有 API_MAX_RETRIES；主链包装层只提交一次，避免
# wrapper attempts × provider attempts 的重复乘法。C 类入口仍可显式传入原值。
SCENE_OUTER_REQUEST_ATTEMPTS = max(
    1,
    int(os.getenv("SCENE_OUTER_REQUEST_ATTEMPTS", "1")),
)


def get_scene_output_paths(task_id=None):
    """返回基于task_id的场景输出路径"""
    if not task_id:
        raise ValueError("task_id is required for scene output paths")
    output_dir = os.path.join(OUTPUTS_PATH, task_id, "images", "scenes")
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    return output_dir


# ============================================================================
# 数据模型
# ============================================================================

@dataclass
class ImageData:
    data: bytes
    format: str = 'png'
    width: int = 1792
    height: int = 1008

    def save_to_file(self, file_path: str) -> None:
        with atomic_image_output(file_path) as temporary_path:
            temporary_path.write_bytes(self.data)


@dataclass
class ProcessResult:
    asset_name: str
    asset_type: str
    success: bool
    image_path: Optional[str] = None
    error_message: Optional[str] = None
    generation_time: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'asset_name': self.asset_name,
            'asset_type': self.asset_type,
            'success': self.success,
            'image_path': self.image_path,
            'error_message': self.error_message,
            'generation_time': self.generation_time
        }


# ============================================================================
# 图片生成器
# ============================================================================

class ImageGenerator:
    """图片生成器 - 按 model 名路由到 GPT Image 私有部署 或 DashScope Wan 系列"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: int = 300,
        model: str = "gpt-image-2",
        size: str = "1792x1008",
        quality: str = "high",
        max_retries: int = 3,
    ):
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout
        self.model = model
        self.size = size
        self.quality = quality
        self.api_max_retries = max_retries
        self._edit_provider = None
        if _is_qwen_model(model):
            # SCENE_SIZE 的 1792x1008 是 GPT/Wan 尺寸，不在 Qwen-Image Plus
            # 的固定尺寸档位内。Qwen 使用自己的 16:9 默认档位。
            qwen_size = os.getenv("QWEN_IMAGE_SIZE", "1664*928")
            self._provider = QwenImageTextProvider(
                api_key=api_key,
                base_url=base_url,
                model=model,
                size=qwen_size,
                timeout_seconds=timeout,
                max_retries=max_retries,
            )
        elif _is_wan_model(model):
            wan_cls = WanLegacyImageTextProvider if _is_legacy_wan(model) else WanImageTextProvider
            self._provider = wan_cls(
                api_key=api_key,
                base_url=base_url,
                model=model,
                size=size,
                timeout_seconds=timeout,
                max_retries=max_retries,
            )
        else:
            self._provider = GPTImageTextProvider(
                api_key=api_key,
                base_url=base_url,
                model=model,
                size=size,
                quality=quality,
                timeout_seconds=timeout,
                max_retries=max_retries,
            )

    @property
    def supports_edit(self) -> bool:
        """当前链路是否支持图生图。Qwen 文生图统一配 Qwen-Image-Edit。"""
        return _is_gpt_image_model(self.model) or _is_qwen_model(self.model)

    @property
    def supports_anchor_edit(self) -> bool:
        """Anchor 模式可为 Wan 文生图链路配套 Qwen Edit。"""
        return self.supports_edit or _is_wan_model(self.model)

    def _get_edit_provider(self):
        """按文生图链路懒加载对应的图生图 provider。"""
        if self._edit_provider is None:
            if _is_qwen_model(self.model):
                self._edit_provider = QwenImageEditProvider(
                    api_key=self.api_key,
                    base_url=self.base_url,
                    model=os.getenv("QWEN_VARIANT_MODEL", "qwen-image-edit-plus"),
                    size=os.getenv("QWEN_EDIT_SIZE", os.getenv("QWEN_IMAGE_SIZE", "1664*928")),
                    timeout_seconds=self.timeout,
                    max_retries=self.api_max_retries,
                )
            elif _is_wan_model(self.model):
                # Wan 仍负责文生图；仅在 Anchor/自定义风格需要图生图时，
                # 使用项目已有的 OpenAI-compatible Qwen Edit 链路。
                edit_api_key = os.getenv("GPT_IMAGE_API_KEY") or os.getenv("OPENAI_API_KEY")
                if not edit_api_key:
                    raise ValueError(
                        "Wan 的 Anchor 图生图需要 GPT_IMAGE_API_KEY / OPENAI_API_KEY"
                    )
                self._edit_provider = QwenImageEditProvider(
                    api_key=edit_api_key,
                    base_url=os.getenv("GPT_IMAGE_BASE_URL", "https://ai.farmmx.com"),
                    model=os.getenv("QWEN_VARIANT_MODEL", "qwen-image-edit-plus"),
                    size=os.getenv("QWEN_EDIT_SIZE", "1664*928"),
                    timeout_seconds=self.timeout,
                    max_retries=self.api_max_retries,
                )
            else:
                self._edit_provider = GPTImageEditProvider(
                    api_key=self.api_key,
                    base_url=self.base_url,
                    model=self.model,
                    size=self.size,
                    quality=self.quality,
                    timeout_seconds=self.timeout,
                    max_retries=self.api_max_retries,
                )
        return self._edit_provider

    def generate_single_image_with_retry(
        self,
        prompt: str,
        negative_prompt: str = "",
        width: int = 1792,
        height: int = 1008,
        max_retries: int = 3,
        **_kwargs,
    ) -> ImageData:
        """串行生成单张图片，带重试机制（gpt-image-2 不支持 negative_prompt，参数保留以兼容旧调用）"""
        logger = logging.getLogger(__name__)

        last_error: Optional[Exception] = None
        for attempt in range(max_retries):
            try:
                data_items = self._provider.generate(prompt)
                if not data_items:
                    raise RuntimeError("GPT Image 响应中没有 data")
                item = data_items[0]
                image_bytes = self._extract_image_bytes(item)
                return ImageData(data=image_bytes, format='png', width=width, height=height)
            except Exception as e:
                last_error = e
                logger.error(f"图片生成失败 (重试 {attempt + 1}/{max_retries}): {e}")
                if attempt == max_retries - 1:
                    raise
                time.sleep(2 ** attempt)
        if last_error is not None:
            raise last_error
        raise RuntimeError("GPT Image 图片生成异常退出")

    def retry_on_failure(
        self,
        prompt: str,
        negative_prompt: str = "",
        width: int = 1792,
        height: int = 1008,
        num_inference_steps: int = 30,
        max_retries: int = 3,
        **_kwargs,
    ) -> ImageData:
        """带重试机制的图片生成（向后兼容；num_inference_steps 不再使用）"""
        return self.generate_single_image_with_retry(
            prompt=prompt, negative_prompt=negative_prompt,
            width=width, height=height, max_retries=max_retries
        )

    def edit_image_with_retry(
        self,
        base_image_path: Path,
        prompt: str,
        width: int = 1792,
        height: int = 1008,
        max_retries: int = 3,
    ) -> ImageData:
        """以 base_image_path 为参考图做图生图，带重试机制（仅 gpt-image 链路支持）。"""
        logger = logging.getLogger(__name__)
        provider = self._get_edit_provider()

        last_error: Optional[Exception] = None
        for attempt in range(max_retries):
            try:
                data_items = provider.edit(Path(base_image_path), prompt)
                if not data_items:
                    raise RuntimeError("GPT Image 图生图响应中没有 data")
                item = data_items[0]
                image_bytes = self._extract_image_bytes(item)
                return ImageData(data=image_bytes, format='png', width=width, height=height)
            except Exception as e:
                last_error = e
                logger.error(f"图生图失败 (重试 {attempt + 1}/{max_retries}): {e}")
                if attempt == max_retries - 1:
                    raise
                time.sleep(2 ** attempt)
        if last_error is not None:
            raise last_error
        raise RuntimeError("GPT Image 图生图异常退出")

    @staticmethod
    def _extract_image_bytes(item: Dict[str, Any]) -> bytes:
        url = item.get("url")
        if url:
            response = requests.get(url, timeout=120)
            response.raise_for_status()
            return response.content
        b64 = item.get("b64_json")
        if b64:
            import base64 as _b64
            return _b64.b64decode(b64)
        raise RuntimeError(f"GPT Image 响应缺少 url 和 b64_json: {item}")

# === 旧版 wan2.5 ImageGenerator（DashScope 异步任务式 API）保留备查 ===
# class ImageGenerator:
#     """图片生成器 - 调用 DashScope wan2.5 API"""
#
#     def __init__(self, api_endpoint: str, api_key: str, timeout: int = 60, model: str = "wan2.5-t2i-preview", max_retries: int = 3):
#         self.api_endpoint = api_endpoint
#         self.api_key = api_key
#         self.timeout = timeout
#         self.model = model
#         self.api_max_retries = max_retries
#
#     def generate_single_image_with_retry(self, prompt: str, negative_prompt: str = "", width: int = 1690, height: int = 960, max_retries: int = 3) -> ImageData:
#         """串行生成单张图片，带重试机制"""
#         logger = logging.getLogger(__name__)
#
#         for attempt in range(max_retries):
#             try:
#                 payload = {
#                     "model": self.model,
#                     "input": {"prompt": prompt, "negative_prompt": negative_prompt},
#                     "parameters": {"size": f"{width}*{height}", "n": 1}
#                 }
#                 headers = {
#                     "Authorization": f"Bearer {self.api_key}",
#                     "Content-Type": "application/json",
#                     "X-DashScope-Async": "enable"
#                 }
#
#                 response = requests.post(
#                     self.api_endpoint + "/services/aigc/text2image/image-synthesis",
#                     json=payload, headers=headers, timeout=self.timeout
#                 )
#                 if not response.ok:
#                     try:
#                         err_body = response.json()
#                     except Exception:
#                         err_body = response.text
#                     raise Exception(f"HTTP {response.status_code}: {err_body}")
#                 response.raise_for_status()
#                 result = response.json()
#
#                 if 'output' not in result or 'task_id' not in result['output']:
#                     raise Exception(f"API返回格式错误: {result}")
#
#                 task_id = result['output']['task_id']
#                 logger.info(f"任务已提交，task_id: {task_id}")
#
#                 for poll_attempt in range(60):
#                     time.sleep(2)
#                     task_response = requests.get(
#                         f"{self.api_endpoint}/tasks/{task_id}",
#                         headers={"Authorization": f"Bearer {self.api_key}"},
#                         timeout=self.timeout
#                     )
#                     task_response.raise_for_status()
#                     task_result = task_response.json()
#                     task_status = task_result['output'].get('task_status', 'UNKNOWN')
#
#                     if task_status == 'SUCCEEDED':
#                         image_url = task_result['output']['results'][0]['url']
#                         img_response = requests.get(image_url, timeout=120)
#                         img_response.raise_for_status()
#                         return ImageData(data=img_response.content, format='png', width=width, height=height)
#                     elif task_status == 'FAILED':
#                         raise Exception(f"任务失败: {task_result['output'].get('message', '未知错误')}")
#                     elif task_status in ['PENDING', 'RUNNING']:
#                         continue
#                     else:
#                         raise Exception(f"未知任务状态: {task_status}")
#
#                 raise Exception("任务超时: 轮询60次后仍未完成")
#
#             except Exception as e:
#                 logger.error(f"图片生成失败 (重试 {attempt+1}/{max_retries}): {e}")
#                 if attempt == max_retries - 1:
#                     raise
#                 time.sleep(2 ** attempt)
#
#     def retry_on_failure(self, prompt: str, negative_prompt: str = "", width: int = 1690, height: int = 960, num_inference_steps: int = 30, max_retries: int = 3) -> ImageData:
#         """带重试机制的图片生成（向后兼容）"""
#         return self.generate_single_image_with_retry(
#             prompt=prompt, negative_prompt=negative_prompt,
#             width=width, height=height, max_retries=max_retries
#         )


# ============================================================================
# 工具类
# ============================================================================

class OutputManager:
    def __init__(self, output_dir: str):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)


# ============================================================================
# 配置
# ============================================================================

class Config:
    def __init__(self, task_id=None):
        # 默认仍走 gpt-image-2 私有部署；当 API_MODEL 设为 wan*/qwen-image* 时自动切到 DashScope
        self.api_model = os.getenv('API_MODEL', 'gpt-image-2')
        self.api_timeout = int(os.getenv('API_TIMEOUT', '300'))
        self.api_max_retries = int(os.getenv('API_MAX_RETRIES', '3'))
        self.api_quality = os.getenv('GPT_IMAGE_QUALITY', 'low')

        if _is_qwen_model(self.api_model):
            # === 千问：走 GPT_IMAGE 中转站的 OpenAI 兼容接口（与 GPT 生图同一 key）===
            self.base_url = os.getenv('GPT_IMAGE_BASE_URL', 'https://ai.farmmx.com')
            self.api_key = os.getenv('GPT_IMAGE_API_KEY') or os.getenv('OPENAI_API_KEY')
        elif _is_wan_model(self.api_model):
            # === DashScope Wan（仍直连百炼官方，需要 DASHSCOPE_API_KEY）===
            self.base_url = os.getenv(
                'DASHSCOPE_BASE_URL', 'https://dashscope.aliyuncs.com/api/v1'
            )
            self.api_key = os.getenv('DASHSCOPE_API_KEY')
            if not self.api_key:
                raise ValueError("DASHSCOPE_API_KEY 未设置（wan 模型需要）")
        else:
            # === GPT Image 私有部署 ===
            self.base_url = os.getenv('GPT_IMAGE_BASE_URL', 'https://ai.farmmx.com')
            self.api_key = os.getenv('GPT_IMAGE_API_KEY') or os.getenv('OPENAI_API_KEY')

        # 兼容字段：旧调用点仍读 self.api_endpoint
        self.api_endpoint = self.base_url

        # === 旧版：DashScope wan2.5（保留备查） ===
        # self.api_endpoint = os.getenv('API_ENDPOINT', 'https://dashscope.aliyuncs.com/api/v1')
        # self.api_key = os.getenv('DASHSCOPE_API_KEY')
        # self.api_timeout = int(os.getenv('API_TIMEOUT', '60'))
        # self.api_model = os.getenv('API_MODEL', 'wan2.5-t2i-preview')
        # self.api_max_retries = int(os.getenv('API_MAX_RETRIES', '3'))

        self.scene = type('SceneConfig', (), {})()
        self.scene.output_directory = get_scene_output_paths(task_id)
        self.scene.width = int(os.getenv('SCENE_WIDTH', str(SCENE_WIDTH)))
        self.scene.height = int(os.getenv('SCENE_HEIGHT', str(SCENE_HEIGHT)))
        self.scene.num_inference_steps = int(os.getenv('SCENE_NUM_INFERENCE_STEPS', '30'))

        self.concurrent_requests = int(os.getenv('CONCURRENT_REQUESTS', '1'))
        self.logging_file = os.getenv('LOGGING_FILE', 'logs/scene_generation.log')
        self.logging_console = os.getenv('LOGGING_CONSOLE', 'true').lower() != 'false'
        self.logging_level = os.getenv('LOGGING_LEVEL', 'INFO')

        if not self.api_key or self.api_key == 'your-real-api-key-here':
            if _is_wan_model(self.api_model) and not _is_qwen_model(self.api_model):
                raise ValueError("API Key 未设置，请在 .env 中配置 DASHSCOPE_API_KEY")
            raise ValueError("API Key 未设置，请在 .env 文件中配置 GPT_IMAGE_API_KEY 或 OPENAI_API_KEY")


# ============================================================================
# 场景批量处理器
# ============================================================================

# 16:9 比例后缀：后端网关忽略 API 的 size 参数，仅根据 prompt 里的比例措辞决定输出宽高比
# （实测：prompt 含 "16:9" -> 输出 16:9(1672x941)，不含 -> 默认 3:2(1536x1024)）。
# 所有场景最终 prompt 统一追加此后缀，保证输出 16:9。
ASPECT_RATIO_SUFFIX = ", 16:9 aspect ratio, cinematic widescreen composition"

# 中性质量后缀：只约束清晰度和结构完整性，不指定媒介、笔触或轮廓风格。
SCENE_STYLE_SUFFIX = (
    ", high clarity, coherent lighting, well-defined complete shapes,"
    " clean spatial relationships, stable perspective"
)


# 图生图参考说明：放在已含任务级 global_style 的场景描述之后。
IMG2IMG_CONSISTENCY_GUIDANCE = (
    "Use the reference image only to understand the spatial layout, orientation, and architecture of this location, "
    "and to keep the same place identity and art style. "
    "Do NOT copy the reference image's framing or composition: the final composition, camera view, focal point, "
    "and how much of the space is shown are determined entirely by the preceding text description "
    "(it may depict only part of the space, a different angle, or use the location merely as background). "
)


def compose_img2img_prompt(positive_prompt: str) -> str:
    """Append reference-image guidance without displacing global_style."""
    return f"{positive_prompt.rstrip()} {IMG2IMG_CONSISTENCY_GUIDANCE}"


def build_scene_checkpoint_signature(
    task_id: str,
    config,
    style_plan: AssetStylePlan,
    scene_id: str,
    positive_prompt: str,
    negative_prompt: str,
    base_path: Optional[Path],
) -> tuple[str, str]:
    dependency = {}
    mode = "text_to_image"
    if base_path is not None:
        dependency = {
            "role": "parent_scene",
            "path": base_path.name,
            "sha256": file_sha256(base_path),
        }
        mode = "parent_image_to_image"
    elif style_plan.uses_anchor:
        anchor = style_plan.anchor_path
        dependency = {
            "role": "scene_anchor",
            "path": anchor.name if anchor else "",
            "sha256": style_plan.anchor_sha256,
        }
        mode = "anchor_image_to_image"
    return (
        canonical_digest(
            {
                "scene_id": scene_id,
                "positive_prompt": positive_prompt,
                "negative_prompt": negative_prompt,
                "generation_mode": mode,
                "model": config.api_model,
                "width": config.scene.width,
                "height": config.scene.height,
                "quality": getattr(config, "api_quality", "low"),
                "style": {
                    "mode": style_plan.mode,
                    "style": style_plan.style,
                    "anchor_sha256": style_plan.anchor_sha256,
                },
            }
        ),
        canonical_digest(dependency),
    )


def _parent_scene_id(scene_id: str) -> Optional[str]:
    """判断 scene_id 是否为二级变体，返回其父级一级场景 id；一级场景返回 None。

    命名规律（见 extract_merge.assign_scene_ids）：
      - 一级场景点：scene_<拼音>
      - 二级变体：  scene_<拼音>_<变体序号>
    即去掉 'scene_' 前缀后、若末段为纯数字则为变体，父级为去掉该末段的部分。
    """
    if not scene_id.startswith("scene_"):
        return None
    body = scene_id[len("scene_"):]
    if "_" not in body:
        return None
    head, _, tail = body.rpartition("_")
    if head and tail.isdigit():
        return f"scene_{head}"
    return None


SHOT_VARIANTS = [
    "",
    "wide angle shot, panoramic view, ",
    "close-up shot, detailed view, ",
    "bird's eye view, aerial perspective, ",
    "low angle shot, worm's eye view, ",
    "medium shot, ",
    "extreme close-up, macro detail, ",
    "overhead shot, ",
    "tilted angle, diagonal composition, ",
    "telephoto compression, ",
]


class SceneProcessor:
    """场景批量处理器 - 生成每个场景的10张不同镜头图片"""

    def __init__(
        self,
        config: Config,
        image_generator: ImageGenerator,
        style_plan: AssetStylePlan,
        image_generator_factory: Optional[Callable[[], ImageGenerator]] = None,
    ):
        self.config = config
        self._image_generators = ThreadLocalResource(
            image_generator_factory or (lambda: image_generator),
            initial=image_generator,
        )
        self.style_plan = style_plan
        self.output_manager = OutputManager(output_dir=config.scene.output_directory)
        self.results: List[ProcessResult] = []

    @property
    def image_generator(self) -> ImageGenerator:
        """Return one mutable Provider wrapper per concurrent worker thread."""
        return self._image_generators.get()

    def _checkpoint_signature(
        self,
        scene_id: str,
        positive_prompt: str,
        negative_prompt: str,
        base_path: Optional[Path],
    ) -> tuple[str, str]:
        return build_scene_checkpoint_signature(
            self.style_plan.task_id,
            self.config,
            self.style_plan,
            scene_id,
            positive_prompt,
            negative_prompt,
            base_path,
        )

    def process(self, scenes_data: Dict[str, Any]) -> List[ProcessResult]:
        """两阶段生成：
          阶段一：一级场景点底图 scene_<拼音> —— 文生图。
          阶段二：二级变体 scene_<拼音>_<n> —— 以父级底图为参考做图生图；
                  父级底图缺失或模型不支持图生图时，降级为文生图。

        Args:
            scenes_data: {scene_id: {"image_prompt": "..."}} 来自 scene_prompts 表
        """
        logging.info(f"开始处理 {len(scenes_data)} 个场景")
        self.results = []

        base_ids = [sid for sid in scenes_data if not _parent_scene_id(sid)]
        variant_ids = [sid for sid in scenes_data if _parent_scene_id(sid)]

        # 阶段一：一级底图彼此独立，可并发文生图；必须等待整层完成后，
        # 才能进入依赖父级底图的二级变体阶段。
        logging.info(f"===== 阶段一：{len(base_ids)} 个一级场景底图（文生图）=====")
        base_results = run_dependency_layer(
            base_ids,
            lambda scene_id: self._process_single(scene_id, scenes_data[scene_id]),
            concurrency=resolve_image_concurrency(
                "SCENE_BASE_IMAGE_CONCURRENCY",
                default=self.config.concurrent_requests,
            ),
            stage="scene_base",
            task_id=self.style_plan.task_id,
            label=str,
            logger=logging.getLogger(__name__),
        )
        self.results.extend(base_results)

        # 阶段二：父级底图已经全部发布；不同二级变体之间没有依赖，可并发。
        logging.info(f"===== 阶段二：{len(variant_ids)} 个二级变体（图生图）=====")
        variant_results = run_dependency_layer(
            variant_ids,
            lambda scene_id: self._process_single(scene_id, scenes_data[scene_id]),
            concurrency=resolve_image_concurrency(
                "SCENE_VARIANT_IMAGE_CONCURRENCY",
                default=self.config.concurrent_requests,
            ),
            stage="scene_variant",
            task_id=self.style_plan.task_id,
            label=str,
            logger=logging.getLogger(__name__),
        )
        self.results.extend(variant_results)

        logging.info(f"处理完成，成功: {self._count_success()}，失败: {self._count_failure()}")
        return self.results

    def _base_image_path(self, scene_id: str) -> Optional[Path]:
        """返回该变体父级底图的本地路径（已成功生成时），否则 None。"""
        parent = _parent_scene_id(scene_id)
        if not parent:
            return None
        safe_parent = re.sub(r'[^\w\-]', '_', parent)
        path = self.output_manager.output_dir / f"{safe_parent}.png"
        return path if validate_image_file(path) else None

    def _process_single(self, scene_id: str, scene_info: Dict[str, Any]) -> ProcessResult:
        start_time = time.time()
        checkpoint_digest = ""
        dependency_digest = ""
        image_path: Optional[Path] = None
        standard_task_root = Path(OUTPUTS_PATH).resolve() / self.style_plan.task_id
        try:
            self.output_manager.output_dir.resolve().relative_to(standard_task_root)
            checkpoint_enabled = True
        except ValueError:
            checkpoint_enabled = False
        try:
            positive_prompt = scene_info.get("image_prompt", "")
            if not positive_prompt:
                raise ValueError(f"场景 {scene_id} 没有 image_prompt")

            positive_prompt = apply_asset_style(self.style_plan, positive_prompt)

            # 统一追加画风 + 16:9 比例提示：后端网关忽略 size 参数，仅靠 prompt 里的比例词
            # 决定输出宽高比（实测：含 16:9 措辞 -> 输出 16:9，否则默认 3:2）。
            positive_prompt = f"{positive_prompt}{SCENE_STYLE_SUFFIX}{ASPECT_RATIO_SUFFIX}"

            negative_prompt = (
                "people, person, human, character, face, body, hands, limbs, head, eyes, nose, mouth, "
                "portrait, figure, man, woman, child, individual, crowd, group, "
                "facial features, human presence, human figures, humans, "
                "blurry, low quality, distorted, artifact, watermark, text, signature, "
                "simple, plain, boring, repetitive patterns"
            )

            # scene_id 本身已是英文，直接用作文件名（只清理不安全字符）
            safe_name = re.sub(r'[^\w\-]', '_', scene_id)
            image_path = self.output_manager.output_dir / f"{safe_name}.png"

            # 二级变体：若父级底图已生成且模型支持图生图，则以底图为参考做图生图；
            # 否则降级为文生图。
            can_use_parent = self.image_generator.supports_edit or (
                self.style_plan.uses_anchor and self.image_generator.supports_anchor_edit
            )
            base_path = self._base_image_path(scene_id) if can_use_parent else None
            checkpoint_digest, dependency_digest = self._checkpoint_signature(
                scene_id, positive_prompt, negative_prompt, base_path
            )
            if checkpoint_enabled:
                decision = reusable_or_adopt_existing(
                    self.style_plan.task_id,
                    "scene_painter",
                    "generate_scene",
                    scene_id,
                    input_digest=checkpoint_digest,
                    generator_version="scene-image-v2-resume",
                    dependency_digest=dependency_digest,
                    output_paths=[image_path],
                    validator=validate_image_file,
                )
                if decision.reusable:
                    logging.info(
                        "复用有效场景图片 %s (%s)", image_path, decision.reason
                    )
                    return ProcessResult(
                        asset_name=scene_id,
                        asset_type="scene",
                        success=True,
                        image_path=str(image_path),
                        generation_time=time.time() - start_time,
                    )
                assert_retry_budget(
                    self.style_plan.task_id,
                    "scene_painter",
                    "generate_scene",
                    scene_id,
                    input_digest=checkpoint_digest,
                    generator_version="scene-image-v2-resume",
                    dependency_digest=dependency_digest,
                )
                checkpoint_mark_running(
                    self.style_plan.task_id,
                    "scene_painter",
                    "generate_scene",
                    scene_id,
                    input_digest=checkpoint_digest,
                    generator_version="scene-image-v2-resume",
                    dependency_digest=dependency_digest,
                )
            if base_path is not None:
                edit_prompt = compose_img2img_prompt(positive_prompt)
                final_provider_prompt = edit_prompt
                reference_images = [{"path": base_path, "role": "父级场景底图"}]
                generation_mode = "image_to_image"
                generation_logic = [
                    "二级场景变体以父级场景底图做图生图。",
                    "父图保持地点身份与空间关系，提示词决定新的镜头、焦点与构图。",
                ]
                image_data = self.image_generator.edit_image_with_retry(
                    base_image_path=base_path,
                    prompt=edit_prompt,
                    width=self.config.scene.width,
                    height=self.config.scene.height,
                    max_retries=SCENE_OUTER_REQUEST_ATTEMPTS,
                )
                logging.info(f"[图生图] {scene_id} 基于底图 {base_path.name} 生成")
            elif self.style_plan.uses_anchor:
                anchor_prompt = compose_anchor_guided_prompt(
                    self.style_plan,
                    positive_prompt,
                )
                final_provider_prompt = anchor_prompt
                reference_images = [
                    {"path": self.style_plan.anchor_path, "role": "场景风格 Anchor"}
                ]
                generation_mode = "image_to_image"
                generation_logic = [
                    "场景图以任务级场景 Anchor 做图生图。",
                    "Anchor 仅控制媒介、线条、色彩、光影、材质和纹理，地点内容与构图由文字决定。",
                ]
                image_data = self.image_generator.edit_image_with_retry(
                    base_image_path=self.style_plan.anchor_path,
                    prompt=anchor_prompt,
                    width=self.config.scene.width,
                    height=self.config.scene.height,
                    max_retries=SCENE_OUTER_REQUEST_ATTEMPTS,
                )
                logging.info(
                    f"[Anchor 图生图] {scene_id} 基于 "
                    f"{self.style_plan.anchor_path.name} 生成"
                )
            else:
                final_provider_prompt = positive_prompt
                reference_images = []
                generation_mode = "text_to_image"
                generation_logic = ["场景图使用上游场景提示词直接文生图。"]
                image_data = self.image_generator.retry_on_failure(
                    prompt=positive_prompt,
                    negative_prompt=negative_prompt,
                    width=self.config.scene.width,
                    height=self.config.scene.height,
                    num_inference_steps=self.config.scene.num_inference_steps,
                    max_retries=SCENE_OUTER_REQUEST_ATTEMPTS,
                )

            if _usage_tracker:
                _usage_tracker.add_image(
                    1,
                    self.config.api_model,
                    provider=os.getenv("IMAGE_PROVIDER", "openai_compatible"),
                )

            with atomic_image_output(image_path) as temporary_path:
                temporary_path.write_bytes(image_data.data)

            record_visual_generation(
                self.style_plan.task_id,
                "scenes",
                image_path,
                prompt=final_provider_prompt,
                generation_mode=generation_mode,
                reference_images=reference_images,
                logic=generation_logic,
                model=str(self.image_generator.model),
                provider=os.getenv("IMAGE_PROVIDER", "openai_compatible"),
                extra={"scene_id": scene_id},
            )
            if checkpoint_enabled:
                checkpoint_mark_success(
                    self.style_plan.task_id,
                    "scene_painter",
                    "generate_scene",
                    scene_id,
                    input_digest=checkpoint_digest,
                    generator_version="scene-image-v2-resume",
                    dependency_digest=dependency_digest,
                    output_paths=[image_path],
                    validator=validate_image_file,
                )

            print(f"已生成并保存图片: {image_path}")

            return ProcessResult(
                asset_name=scene_id,
                asset_type='scene',
                success=True,
                image_path=str(image_path),
                generation_time=time.time() - start_time
            )

        except Exception as e:
            logging.error(f"处理场景失败 {scene_id}: {e}")
            if checkpoint_enabled and checkpoint_digest:
                try:
                    checkpoint_mark_failed(
                        self.style_plan.task_id,
                        "scene_painter",
                        "generate_scene",
                        scene_id,
                        input_digest=checkpoint_digest,
                        generator_version="scene-image-v2-resume",
                        dependency_digest=dependency_digest,
                        error=str(e),
                    )
                except Exception:
                    logging.exception("记录场景失败 checkpoint 时发生异常")
            return ProcessResult(
                asset_name=scene_id,
                asset_type='scene',
                success=False,
                error_message=str(e),
                generation_time=time.time() - start_time
            )

    def retry_failed(self, results: List[ProcessResult], scenes_data: Dict[str, Any]) -> List[ProcessResult]:
        """对第一轮失败的场景进行兜底重试"""
        failed = [r for r in results if not r.success]
        if not failed:
            return results

        logging.info(f"兜底重试: 共 {len(failed)} 个失败场景")
        print(f"\n===== 兜底重试: {len(failed)} 个失败场景 =====")

        retry_map: Dict[str, ProcessResult] = {}
        for r in tqdm(failed, desc="兜底重试"):
            scene_info = scenes_data.get(r.asset_name)
            if scene_info:
                retry_map[r.asset_name] = self._process_single(r.asset_name, scene_info)

        final = []
        for r in results:
            final.append(retry_map.get(r.asset_name, r))

        retry_success = sum(1 for r in retry_map.values() if r.success)
        print(f"兜底重试完成: {retry_success}/{len(failed)} 成功")
        return final

    def _count_success(self) -> int:
        return sum(1 for r in self.results if r.success)

    def _count_failure(self) -> int:
        return sum(1 for r in self.results if not r.success)


# ============================================================================
# 主函数
# ============================================================================

def fetch_scenes_from_db(task_id: str = None) -> tuple:
    """从 scene_prompts 表读取提示词数据，返回 (task_id, {scene_id: {image_prompt: ...}})"""
    with platform_engine.connect() as conn:
        if task_id:
            sql = text("SELECT task_id, data FROM scene_prompts WHERE task_id = :tid ORDER BY create_time DESC LIMIT 1")
            row = conn.execute(sql, {"tid": task_id}).fetchone()
        else:
            sql = text("SELECT task_id, data FROM scene_prompts ORDER BY create_time DESC LIMIT 1")
            row = conn.execute(sql).fetchone()

    if not row:
        return None, {}

    found_task_id = row[0]
    data = row[1] if isinstance(row[1], dict) else json.loads(row[1])
    # 过滤掉 cg_* 条目，CG 由 cg_generator 单独生图
    data = {k: v for k, v in (data or {}).items() if not (isinstance(k, str) and k.startswith("cg_"))}
    return found_task_id, data


def main():
    import argparse
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    parser = argparse.ArgumentParser(description="场景图片生成器 - 从数据库获取提示词生成场景图片")
    parser.add_argument("--output-dir", type=str, default=None, help="输出目录路径")
    parser.add_argument("--task-id", type=str, default=None, help="指定task_id")
    args = parser.parse_args()

    # 1. 从 scene_prompts 表读取提示词
    logging.info("从数据库获取场景提示词...")
    task_id, scenes_data = fetch_scenes_from_db(args.task_id)

    if not scenes_data:
        logging.error("未从数据库获取到任何场景数据，请先运行 scene_prompts.py")
        return 1

    if not task_id:
        task_id = args.task_id

    logging.info(f"成功获取 {len(scenes_data)} 个场景提示词，task_id: {task_id}")

    # 2. 读取资产级风格路由：LLM 自动路线和 custom 路线均使用
    #    对应场景 Anchor。
    style_plan = resolve_asset_style_plan(task_id, ASSET_SCENE)
    logging.info(
        "场景美术风格路由: mode=%s style=%s anchor=%s",
        style_plan.mode,
        style_plan.style,
        style_plan.anchor_path or "(none)",
    )

    # 3. 初始化配置和生成器
    config = Config(task_id=task_id)
    if args.output_dir:
        config.scene.output_directory = args.output_dir

    if _usage_tracker and task_id:
        _usage_tracker.set_context(
            task_id, "scene_painter", force_new_run=True
        )

    def build_image_generator() -> ImageGenerator:
        return ImageGenerator(
            base_url=config.base_url,
            api_key=config.api_key,
            timeout=config.api_timeout,
            model=config.api_model,
            size=f"{config.scene.width}x{config.scene.height}",
            quality=config.api_quality,
            max_retries=config.api_max_retries,
        )

    try:
        image_generator = build_image_generator()
        # 旧版 wan2.5 实例化（保留备查）：
        # image_generator = ImageGenerator(
        #     api_endpoint=config.api_endpoint,
        #     api_key=config.api_key,
        #     timeout=config.api_timeout,
        #     model=config.api_model
        # )
    except ValueError as e:
        print(f"配置错误: {e}")
        return 1

    # 4. 生成图片（第一遍）
    processor = SceneProcessor(
        config,
        image_generator,
        style_plan,
        image_generator_factory=build_image_generator,
    )
    results = processor.process(scenes_data)

    # 5. 兜底重试：每轮对失败场景重试5次，累计最多25次（不含第一遍）
    # Provider 自身已经会重试，外层再固定 25 次会形成乘法放大。并发 10 时
    # 一张坏图可能触发几十次付费请求，因此统一改成可配置的小上限。
    MAX_TOTAL_RETRIES = int(os.getenv("ASSET_MAX_TOTAL_RETRIES", "5"))
    PER_ROUND_RETRIES = int(os.getenv("ASSET_RETRIES_PER_ROUND", "2"))

    # 记录每张图片已累计重试次数
    retry_counts: dict = {}

    round_num = 0
    while True:
        failed = [r for r in results if not r.success]
        if not failed:
            break

        # 过滤掉已达上限的
        to_retry = [r for r in failed if retry_counts.get(r.asset_name, 0) < MAX_TOTAL_RETRIES]
        if not to_retry:
            logging.warning(f"所有失败场景均已达到 {MAX_TOTAL_RETRIES} 次重试上限，停止重试")
            break

        round_num += 1
        logging.info(f"===== 兜底重试第 {round_num} 轮：{len(to_retry)} 个失败场景，每张最多重试 {PER_ROUND_RETRIES} 次 =====")

        def retry_scene(r):
            scene_info = scenes_data.get(r.asset_name)
            if not scene_info:
                return r.asset_name, r
            # 本轮对该场景最多尝试 PER_ROUND_RETRIES 次
            best: ProcessResult = r
            for attempt in range(PER_ROUND_RETRIES):
                retry_counts[r.asset_name] = retry_counts.get(r.asset_name, 0) + 1
                res = processor._process_single(r.asset_name, scene_info)
                if res.success:
                    best = res
                    break
                best = res
                if retry_counts[r.asset_name] >= MAX_TOTAL_RETRIES:
                    logging.warning(f"  {r.asset_name} 已达累计上限 {MAX_TOTAL_RETRIES} 次，停止")
                    break
            return r.asset_name, best

        retry_pairs = run_dependency_layer(
            to_retry,
            retry_scene,
            concurrency=resolve_image_concurrency(
                "SCENE_RETRY_IMAGE_CONCURRENCY",
                default=config.concurrent_requests,
            ),
            stage=f"scene_retry_{round_num}",
            task_id=task_id,
            label=lambda result: str(result.asset_name),
            logger=logging.getLogger(__name__),
        )
        retry_map = dict(retry_pairs)

        # 合并结果
        results = [retry_map.get(r.asset_name, r) for r in results]

        retry_success = sum(1 for r in retry_map.values() if r.success)
        logging.info(f"第 {round_num} 轮重试完成: {retry_success}/{len(to_retry)} 成功")

    total = len(results)
    success = sum(1 for r in results if r.success)
    print(f"处理完成：总共 {total} 个场景，成功 {success} 个，失败 {total - success} 个")

    # 6. 最终兜底：仍失败的二级变体，直接拷贝其父级一级底图，文件名沿用二级变体自身
    still_failed = [r for r in results if not r.success]
    if still_failed:
        output_dir = processor.output_manager.output_dir
        fallback_count = 0
        for r in still_failed:
            safe_name = re.sub(r'[^\w\-]', '_', r.asset_name)
            target_path = output_dir / f"{safe_name}.png"

            parent = _parent_scene_id(r.asset_name)
            if not parent:
                # 一级底图自身失败，没有可兜底的父级
                logging.error(f"  [FALLBACK] {r.asset_name} 是一级底图且生成失败，无父级可兜底")
                continue

            safe_parent = re.sub(r'[^\w\-]', '_', parent)
            src_path = output_dir / f"{safe_parent}.png"
            if validate_image_file(src_path):
                atomic_copy_image(src_path, target_path)
                scene_info = scenes_data.get(r.asset_name) or {}
                fallback_prompt = apply_asset_style(
                    style_plan, scene_info.get("image_prompt", "")
                )
                fallback_prompt = (
                    f"{fallback_prompt}{SCENE_STYLE_SUFFIX}{ASPECT_RATIO_SUFFIX}"
                )
                fallback_negative = (
                    "people, person, human, character, face, body, hands, limbs, head, eyes, nose, mouth, "
                    "portrait, figure, man, woman, child, individual, crowd, group, "
                    "facial features, human presence, human figures, humans, "
                    "blurry, low quality, distorted, artifact, watermark, text, signature, "
                    "simple, plain, boring, repetitive patterns"
                )
                expected_input, expected_dependency = build_scene_checkpoint_signature(
                    task_id,
                    config,
                    style_plan,
                    r.asset_name,
                    fallback_prompt,
                    fallback_negative,
                    src_path,
                )
                checkpoint_mark_success(
                    task_id,
                    "scene_painter",
                    "generate_scene",
                    r.asset_name,
                    input_digest=expected_input,
                    generator_version="scene-image-v2-resume",
                    dependency_digest=expected_dependency,
                    output_paths=[target_path],
                    validator=validate_image_file,
                    result_mode="fallback_copy",
                )
                record_derived_visual_asset(
                    task_id,
                    "scenes",
                    target_path,
                    src_path,
                    generation_mode="fallback_copy",
                    logic=(
                        "目标二级场景多次生成失败，项目在本地复制父级场景底图作为兜底；"
                        "该文件没有新的 Provider 生图结果。"
                    ),
                )
                fallback_count += 1
                logging.warning(f"  [FALLBACK] {r.asset_name} 生成失败，拷贝一级底图 {safe_parent} 替代")
            else:
                logging.error(f"  [FALLBACK] {r.asset_name} 生成失败，且父级底图 {safe_parent} 也不存在")

        if fallback_count:
            print(f"兜底拷贝完成：{fallback_count} 张失败的二级变体已用一级底图替代")

    # 7. 原图始终保留；按 scene_prompts.copy_count 复制剧情区域实例。
    copied_count, copy_failures = copy_scene_instances(
        scenes_data,
        processor.output_manager.output_dir,
    )
    for scene_id, scene_info in scenes_data.items():
        if not isinstance(scene_info, dict):
            continue
        try:
            copy_count = max(0, int(scene_info.get("copy_count", 0) or 0))
        except (TypeError, ValueError):
            copy_count = 0
        safe_name = re.sub(r'[^\w\-]', '_', scene_id)
        source_path = processor.output_manager.output_dir / f"{safe_name}.png"
        if not validate_image_file(source_path):
            continue
        for copy_index in range(1, copy_count + 1):
            instance_id = re.sub(r'[^\w\-]', '_', f"{scene_id}_{copy_index}")
            target_path = processor.output_manager.output_dir / f"{instance_id}.png"
            if validate_image_file(target_path):
                record_derived_visual_asset(
                    task_id,
                    "scenes",
                    target_path,
                    source_path,
                    generation_mode="local_copy",
                    logic=(
                        "该图片是剧情连续区域实例，按 scene_prompts.copy_count "
                        "在本地复制场景原图，不会再次调用 Provider。"
                    ),
                )
    logging.info(f"场景区域实例复制完成: 成功 {copied_count} 张，失败 {len(copy_failures)} 张")

    if _usage_tracker:
        _usage_tracker.flush()

    # 2 denotes asset-level incompleteness.  The parent scene_painter task
    # records the missing files and continues to CG/game packaging; 1 remains
    # reserved for configuration or process-level failures.
    return 2 if (success != total or copy_failures) else 0


if __name__ == '__main__':
    sys.exit(main())
