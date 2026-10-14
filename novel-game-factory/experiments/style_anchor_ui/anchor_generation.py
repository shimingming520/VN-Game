"""Generate or normalize style anchors with the project's image providers."""

from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Any, Dict, Tuple

import requests
from PIL import Image, UnidentifiedImageError

from common.task_models import ImageModel
from common.visual_style import ASSET_CHARACTER, ASSET_SCENE, file_sha256
from services.scene_painter.image_providers.gpt_image import GPTImageTextProvider
from services.scene_painter.image_providers.qwen_image import QwenImageTextProvider
from services.scene_painter.image_providers.wan_config import is_legacy_wan
from services.scene_painter.image_providers.wan_image import WanImageTextProvider
from services.scene_painter.image_providers.wan_image_25 import WanLegacyImageTextProvider

try:
    from common.usage_tracker import tracker as _usage_tracker
except Exception:
    _usage_tracker = None


MAX_UPLOAD_BYTES = 20 * 1024 * 1024

CHARACTER_ANCHOR_CONTENT = (
    "Create one original full-body character style anchor on a completely pure white background. "
    "Show exactly one anonymous adult human standing in a neutral relaxed pose, fully visible from head to toe "
    "with empty space below the feet. Use plain, modest, unadorned everyday clothing with no jewelry, luxury "
    "materials, rank symbols, weapons, text, logos, or culturally specific identity markers. Keep facial features "
    "natural and neutral. The purpose of this image is to make the requested medium, linework, palette, shading, "
    "lighting, and material treatment easy to review, not to establish a story character. Safe for all ages."
)

SCENE_ANCHOR_CONTENT = (
    "Create one original uninhabited environment style anchor in a 16:9 cinematic widescreen composition. "
    "Depict a simple fictional outdoor passage opening into a small courtyard, with a balanced mix of stone, wood, "
    "plaster, foliage, sky, and soft atmospheric depth so the requested medium, linework, palette, lighting, and "
    "material rendering are easy to review. No people, animals, readable text, logos, famous landmarks, distinctive "
    "story architecture, or dramatic plot event. The image is only a neutral visual-style sample."
)


class AnchorGenerationError(RuntimeError):
    pass


def build_anchor_prompt(asset_kind: str, global_style: str) -> str:
    style = str(global_style or "").strip()
    if not style:
        raise AnchorGenerationError("生成 Anchor 前请先填写对应的 global style")
    if asset_kind == ASSET_CHARACTER:
        content = CHARACTER_ANCHOR_CONTENT
    elif asset_kind == ASSET_SCENE:
        content = SCENE_ANCHOR_CONTENT
    else:
        raise AnchorGenerationError(f"不支持的 asset_kind: {asset_kind}")
    return f"Requested visual style:\n{style}\n\nAnchor content requirements:\n{content}"


def _provider_for(asset_kind: str, image_model: str):
    model = str(image_model or ImageModel.DEFAULT).strip()
    if model not in ImageModel.VALID_MODELS:
        raise AnchorGenerationError(
            f"image_model 只支持: {', '.join(ImageModel.VALID_MODELS)}"
        )

    if asset_kind == ASSET_CHARACTER:
        gpt_size, qwen_size, wan_size = "1024x1536", "1140*1472", "1024*1536"
    else:
        gpt_size, qwen_size, wan_size = "1536x1024", "1664*928", "1792*1008"

    if model.startswith("gpt-image"):
        api_key = os.getenv("GPT_IMAGE_API_KEY") or os.getenv("OPENAI_API_KEY")
        return GPTImageTextProvider(
            api_key=api_key,
            base_url=os.getenv("GPT_IMAGE_BASE_URL", "https://ai.farmmx.com"),
            model=model,
            size=gpt_size,
            quality=os.getenv("GPT_IMAGE_QUALITY", "high"),
            timeout_seconds=int(os.getenv("API_TIMEOUT", "300")),
            max_retries=int(os.getenv("API_MAX_RETRIES", "3")),
        )

    if model.startswith("qwen-image"):
        api_key = os.getenv("GPT_IMAGE_API_KEY") or os.getenv("OPENAI_API_KEY")
        return QwenImageTextProvider(
            api_key=api_key,
            base_url=os.getenv("GPT_IMAGE_BASE_URL", "https://ai.farmmx.com"),
            model=model,
            size=qwen_size,
            timeout_seconds=int(os.getenv("API_TIMEOUT", "300")),
            max_retries=int(os.getenv("API_MAX_RETRIES", "3")),
        )

    api_key = os.getenv("DASHSCOPE_API_KEY")
    provider_cls = WanLegacyImageTextProvider if is_legacy_wan(model) else WanImageTextProvider
    return provider_cls(
        api_key=api_key,
        base_url=os.getenv(
            "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/api/v1"
        ),
        model=model,
        size=wan_size,
        timeout_seconds=int(os.getenv("API_TIMEOUT", "300")),
        max_retries=int(os.getenv("API_MAX_RETRIES", "3")),
    )


def _save_provider_item(provider: Any, item: Dict[str, Any], target: Path) -> None:
    if hasattr(provider, "save_image_item"):
        if provider.save_image_item(item, target):
            return
        raise AnchorGenerationError("项目 Provider 返回图片保存失败")

    url = item.get("url") if isinstance(item, dict) else None
    if not url:
        raise AnchorGenerationError("项目 Provider 响应缺少图片 URL")
    response = requests.get(url, timeout=300)
    response.raise_for_status()
    target.write_bytes(response.content)


def _normalize_image(source: Path, target: Path) -> None:
    try:
        with Image.open(source) as image:
            image.load()
            if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
                rgba = image.convert("RGBA")
                background = Image.new("RGBA", rgba.size, "white")
                background.alpha_composite(rgba)
                normalized = background.convert("RGB")
            else:
                normalized = image.convert("RGB")
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
            try:
                normalized.save(temp, format="PNG", optimize=True)
                os.replace(temp, target)
            finally:
                temp.unlink(missing_ok=True)
    except (UnidentifiedImageError, OSError) as exc:
        raise AnchorGenerationError(f"无法读取 Anchor 图片: {exc}") from exc


def normalize_uploaded_anchor(content: bytes, target: Path) -> Dict[str, Any]:
    if not content:
        raise AnchorGenerationError("上传图片为空")
    if len(content) > MAX_UPLOAD_BYTES:
        raise AnchorGenerationError("Anchor 图片不能超过 20MB")

    raw_path = target.with_name(f".{target.name}.{os.getpid()}.upload")
    try:
        raw_path.write_bytes(content)
        _normalize_image(raw_path, target)
    finally:
        raw_path.unlink(missing_ok=True)

    with Image.open(target) as image:
        width, height = image.size
    return {
        "sha256": file_sha256(target),
        "width": width,
        "height": height,
    }


def generate_anchor(
    task_id: str,
    asset_kind: str,
    global_style: str,
    image_model: str,
    target: Path,
) -> Tuple[Dict[str, Any], str]:
    prompt = build_anchor_prompt(asset_kind, global_style)
    provider = _provider_for(asset_kind, image_model)
    raw_path = target.with_name(f".{target.name}.{os.getpid()}.generated")
    try:
        items = provider.generate(prompt)
        if not items:
            raise AnchorGenerationError("项目 Provider 未返回图片")
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        _save_provider_item(provider, items[0], raw_path)
        _normalize_image(raw_path, target)
    finally:
        raw_path.unlink(missing_ok=True)

    if _usage_tracker:
        _usage_tracker.set_context(task_id, "style_anchor_ui")
        _usage_tracker.add_image(1, getattr(provider, "model", image_model))
        _usage_tracker.flush()

    with Image.open(target) as image:
        width, height = image.size
    return (
        {
            "sha256": file_sha256(target),
            "width": width,
            "height": height,
            "model": getattr(provider, "model", image_model),
        },
        prompt,
    )
