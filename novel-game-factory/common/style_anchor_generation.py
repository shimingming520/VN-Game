"""Generate or normalize task style anchors with the project's image providers.

Provider imports stay lazy so API Gateway can reuse upload normalization without
shipping the complete scene-painter runtime in its image. Real image generation
is executed by the scene-painter Celery worker, where those providers exist.

================================================================================
本地命令行用法 (Local CLI Usage)
================================================================================
只给一个 task_id，本地复刻 char_designer/tasks.py 里的两步（取 global_style →
生成 character anchor），不经过 Celery。

  # 在项目根目录 novel-game-factory 下执行
  # 1. 最简用法（默认 gpt-image-2）
  python common/style_anchor_generation.py --task-id your_task_id

  # 2. 指定模型 / 资产类型
  python common/style_anchor_generation.py --task-id your_task_id \
      --image-model wan2.5-t2i-preview
  python common/style_anchor_generation.py --task-id your_task_id \
      --asset-kind scene --provider-scope scene_painter

  # 3. 本地连不上库、或想手填风格时，跳过 DB 直接给 global_style
  python common/style_anchor_generation.py --task-id your_task_id \
      --global-style "水墨风格，柔和光照，写意线条"

命令行参数：
  --task-id         任务 ID（必填）
  --image-model     生图模型，默认 gpt-image-2
                    (gpt-image-2 / qwen-image-plus / wan2.5-t2i-preview)
  --asset-kind      character / scene，默认 character
  --provider-scope  char_designer / scene_painter，默认 char_designer
  --global-style    跳过数据库，手动指定风格文本（不填则从 story_tree_json 读取）
  --verbose         输出 DEBUG 日志

前置组件：
  1. sys.path：脚本自动把项目根目录加入 sys.path，可直接 python 运行。
  2. .env（项目根）：import common.config 时加载，需按需配置——
     - 生图：GPT_IMAGE_API_KEY（或 OPENAI_API_KEY），可选 GPT_IMAGE_BASE_URL /
       GPT_IMAGE_QUALITY；wan 模型用 DASHSCOPE_API_KEY。
     - 读 global_style（不加 --global-style 时）：PostgreSQL 连接
       (DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD)，且该 task 已跑过
       story_planner；库里若还没有 style，会连 Redis + STORY_PLANNER_LLM_API_KEY
       现算一个。
     - Redis：ensure_llm_style_anchor 始终加锁，需 REDIS_HOST/REDIS_PORT
       （config.py 默认 redis:6379，本地一般改成 localhost）。
  3. 产物：storage/outputs/<task_id>/style_anchors/character_anchor.png +
     config.json（幂等，已生成会直接复用）。
================================================================================
"""

from __future__ import annotations

import io
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# 允许 `python common/style_anchor_generation.py` 直接运行：把项目根目录加入
# sys.path，否则 `from common...` 与 lazy 的 `from services...` 无法解析。
if __package__ in (None, ""):
    _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _project_root not in sys.path:
        sys.path.insert(0, _project_root)

import requests
from PIL import Image, UnidentifiedImageError

from common.task_models import ImageModel
from common.config import REDIS_URL
from common.gpt_image_prompt_cleanliness import (
    PROFILE_ANCHOR_CLEANUP,
    PROFILE_BALANCED,
    PROFILE_OFF,
    gpt_image_artifact_suppression_enabled,
    gpt_image_keep_raw_anchor_enabled,
    gpt_image_two_pass_anchor_enabled,
)
from common.visual_style import (
    ANCHOR_STATUS_READY,
    ASSET_CHARACTER,
    ASSET_SCENE,
    STYLE_MODE_LLM,
    file_sha256,
    load_visual_style_config,
    save_visual_style_config,
    style_anchor_path,
    visual_style_timestamp,
)
try:
    from common.usage_tracker import tracker as _usage_tracker
except Exception:
    _usage_tracker = None


MAX_UPLOAD_BYTES = 20 * 1024 * 1024
LLM_ANCHOR_SOURCE = "llm_generated"
LLM_ANCHOR_PROMPT_VERSION_SINGLE_PASS = "llm-style-anchor-v2-independent-scene"
LLM_ANCHOR_PROMPT_VERSION_TWO_PASS = "llm-style-anchor-v3-independent-scene-two-pass-cleanup"
CHARACTER_RGBA_ANCHOR_VERSION_SUFFIX = "native-rgba-v1"
logger = logging.getLogger(__name__)

CHARACTER_ANCHOR_CONTENT = (
    "Create one original full-body character style anchor on a genuinely transparent canvas with a real alpha channel. "
    "Show exactly one anonymous adult human standing in a neutral relaxed pose, fully visible from head to toe "
    "with transparent empty space below the feet and around the silhouette. Use plain, modest, unadorned everyday clothing with no jewelry, luxury "
    "materials, rank symbols, weapons, text, logos, or culturally specific identity markers. Keep facial features "
    "natural and neutral. Do not render a white, colored, scenic, studio, checkerboard, floor, or other visible background. "
    "The purpose of this image is to make the requested medium, linework, palette, shading, lighting, and material "
    "treatment easy to review, not to establish a story character. Safe for all ages."
)

SCENE_ANCHOR_CONTENT = (
    "Create one original uninhabited environment style anchor in a 16:9 cinematic widescreen composition. "
    "Depict a simple fictional outdoor passage opening into a small courtyard, with a balanced mix of stone, wood, "
    "plaster, foliage, sky, and soft atmospheric depth so the requested medium, linework, palette, lighting, and "
    "material rendering are easy to review. No people, animals, readable text, logos, famous landmarks, distinctive "
    "story architecture, or dramatic plot event. Use balanced neutral exposure with readable midtones, distinct "
    "material colors, and a complete tonal range; avoid a uniform gray wash, muddy low contrast, or crushed shadows. "
    "The image is only a neutral visual-style sample, so story-specific day/night mood remains controlled by later prompts."
)

ANCHOR_CLEANUP_CONTENT = (
    "Refine the attached raw visual-style Anchor without redesigning it. "
    "Keep the same composition, identity or environment, proportions, palette, linework, brushwork, lighting, "
    "shadows, atmosphere, material differences, and intentional detail. Remove only unintended digital noise, "
    "dirty speckles, moire, diagonal-grid interference, repetitive fish-scale-like or cellular artifacts, "
    "compression ringing, and sharpening halos. The result remains a faithful style Anchor, not a new design."
)


class AnchorGenerationError(RuntimeError):
    pass


def build_anchor_prompt(
    asset_kind: str,
    global_style: str,
    *,
    has_style_reference: bool = False,
) -> str:
    style = str(global_style or "").strip()
    if not style:
        raise AnchorGenerationError("生成 Anchor 前请先填写对应的 global style")
    if asset_kind == ASSET_CHARACTER:
        content = CHARACTER_ANCHOR_CONTENT
    elif asset_kind == ASSET_SCENE:
        content = SCENE_ANCHOR_CONTENT
    else:
        raise AnchorGenerationError(f"不支持的 asset_kind: {asset_kind}")
    prompt = (
        f"Requested visual style:\n{style}\n\n"
        f"Anchor content requirements:\n{content}"
    )
    if has_style_reference:
        prompt += (
            "\n\nReference-image restriction:\n"
            "Use the attached character Anchor only to transfer medium, linework, palette, "
            "shading, lighting, material treatment, and texture into the new environment. "
            "Do not copy any person, face, body, clothing, social status, prop, white background, "
            "spatial layout, object, architecture, camera angle, or composition from it. "
            "The output must contain no people or character-like figures."
        )
    return prompt


def build_anchor_cleanup_prompt(asset_kind: str, global_style: str) -> str:
    """Build the dedicated raw-Anchor refinement prompt.

    This deliberately does not reuse ``build_anchor_prompt(...,
    has_style_reference=True)`` because that reference restriction is specific
    to transferring a character Anchor into a new uninhabited scene.
    """
    style = str(global_style or "").strip()
    if not style:
        raise AnchorGenerationError("清洁 Anchor 前请先提供 global style")
    if asset_kind not in {ASSET_CHARACTER, ASSET_SCENE}:
        raise AnchorGenerationError(f"不支持的 asset_kind: {asset_kind}")
    kind_label = "character" if asset_kind == ASSET_CHARACTER else "environment"
    transparency_guard = (
        "\nPreserve the real alpha channel and fully transparent canvas. Do not add a white, "
        "colored, checkerboard, scenic, studio, floor, or other visible background."
        if asset_kind == ASSET_CHARACTER
        else ""
    )
    return (
        f"Requested visual style:\n{style}\n\n"
        f"Anchor cleanup task ({kind_label}):\n{ANCHOR_CLEANUP_CONTENT}"
        f"{transparency_guard}"
    )


def _validate_model(image_model: str) -> str:
    model = str(image_model or ImageModel.DEFAULT).strip()
    if model not in ImageModel.VALID_MODELS:
        raise AnchorGenerationError(
            f"image_model 只支持: {', '.join(ImageModel.VALID_MODELS)}"
        )
    return model


def _scene_text_provider(asset_kind: str, image_model: str):
    from services.scene_painter.image_providers.gpt_image import GPTImageTextProvider
    from services.scene_painter.image_providers.qwen_image import QwenImageTextProvider
    from services.scene_painter.image_providers.wan_config import is_legacy_wan
    from services.scene_painter.image_providers.wan_image import WanImageTextProvider
    from services.scene_painter.image_providers.wan_image_25 import WanLegacyImageTextProvider

    model = _validate_model(image_model)

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


def _character_text_provider(asset_kind: str, image_model: str):
    from services.char_designer.image_providers.gpt_image import GPTImageTextProvider
    from services.char_designer.image_providers.qwen_image import QwenTextImageProvider
    from services.char_designer.image_providers.wan_image_25 import (
        Wan25TextImageProvider,
    )

    model = _validate_model(image_model)
    if asset_kind != ASSET_CHARACTER:
        raise AnchorGenerationError("char_designer Provider 只生成 character Anchor")
    timeout = int(os.getenv("API_TIMEOUT", "300"))
    max_retries = int(os.getenv("API_MAX_RETRIES", "3"))
    if model.startswith("gpt-image"):
        return GPTImageTextProvider(
            api_key=os.getenv("GPT_IMAGE_API_KEY") or os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("GPT_IMAGE_BASE_URL", "https://ai.farmmx.com"),
            model=model,
            size="1024x1536",
            quality=os.getenv("GPT_IMAGE_QUALITY", "high"),
            timeout_seconds=timeout,
            max_retries=max_retries,
            transparent_character_output=True,
        )
    if model.startswith("qwen-image"):
        return QwenTextImageProvider(
            api_key=os.getenv("GPT_IMAGE_API_KEY") or os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("GPT_IMAGE_BASE_URL", "https://ai.farmmx.com"),
            model=model,
            size="1140*1472",
            request_timeout=timeout,
            max_retries=max_retries,
        )
    return Wan25TextImageProvider(
        api_key=os.getenv("DASHSCOPE_API_KEY"),
        model=model,
        size="1024*1536",
        request_timeout=timeout,
        max_retries=max_retries,
    )


def _scene_edit_provider(image_model: str):
    from services.scene_painter.image_providers.gpt_image import GPTImageEditProvider
    from services.scene_painter.image_providers.qwen_image import QwenImageEditProvider

    model = _validate_model(image_model)
    timeout = int(os.getenv("API_TIMEOUT", "300"))
    max_retries = int(os.getenv("API_MAX_RETRIES", "3"))
    api_key = os.getenv("GPT_IMAGE_API_KEY") or os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("GPT_IMAGE_BASE_URL", "https://ai.farmmx.com")
    if model.startswith("gpt-image"):
        return GPTImageEditProvider(
            api_key=api_key,
            base_url=base_url,
            model=model,
            size="1536x1024",
            quality=os.getenv("GPT_IMAGE_QUALITY", "high"),
            timeout_seconds=timeout,
            max_retries=max_retries,
        )
    # qwen-image-plus itself is text-only, and Wan has no edit endpoint in this
    # project. Both routes deliberately reuse the existing Qwen Edit fallback.
    return QwenImageEditProvider(
        api_key=api_key,
        base_url=base_url,
        model=os.getenv("QWEN_VARIANT_MODEL", "qwen-image-edit-plus"),
        size=os.getenv("QWEN_EDIT_SIZE", "1664*928"),
        timeout_seconds=timeout,
        max_retries=max_retries,
    )


def _character_edit_provider(image_model: str):
    from services.char_designer.image_providers.gpt_image import GPTImageEditProvider

    model = _validate_model(image_model)
    if not model.startswith("gpt-image"):
        raise AnchorGenerationError(
            "两阶段 character Anchor 清洁目前只支持 GPT Image"
        )
    timeout = int(os.getenv("API_TIMEOUT", "300"))
    max_retries = int(os.getenv("API_MAX_RETRIES", "3"))
    api_key = os.getenv("GPT_IMAGE_API_KEY") or os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("GPT_IMAGE_BASE_URL", "https://ai.farmmx.com")
    return GPTImageEditProvider(
        api_key=api_key,
        base_url=base_url,
        model=model,
        size="1024x1536",
        quality=os.getenv("GPT_IMAGE_QUALITY", "high"),
        timeout_seconds=timeout,
        max_retries=max_retries,
        transparent_character_output=True,
    )


def _save_provider_result(provider: Any, result: Any, target: Path) -> None:
    if isinstance(result, str):
        if hasattr(provider, "download") and provider.download(result, target):
            return
        response = requests.get(result, timeout=300)
        response.raise_for_status()
        target.write_bytes(response.content)
        return

    items = result if isinstance(result, list) else [result]
    if not items:
        raise AnchorGenerationError("项目 Provider 未返回图片")
    item = items[0]
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


def _normalize_image(
    source: Path,
    target: Path,
    *,
    preserve_transparency: bool = False,
) -> None:
    try:
        with Image.open(source) as image:
            image.load()
            if preserve_transparency:
                normalized = image.convert("RGBA")
            elif image.mode in {"RGBA", "LA"} or "transparency" in image.info:
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

    target.parent.mkdir(parents=True, exist_ok=True)
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
    *,
    provider_scope: str = "scene_painter",
    reference_image: Optional[Path] = None,
    usage_service: str = "style_anchor",
    prompt_profile: str = PROFILE_BALANCED,
    cleanup_reference: bool = False,
) -> Tuple[Dict[str, Any], str]:
    if provider_scope not in {"char_designer", "scene_painter"}:
        raise AnchorGenerationError(
            f"不支持的 Anchor Provider scope: {provider_scope}"
        )
    prompt = (
        build_anchor_cleanup_prompt(asset_kind, global_style)
        if cleanup_reference
        else build_anchor_prompt(
            asset_kind,
            global_style,
            has_style_reference=reference_image is not None,
        )
    )
    if reference_image is not None:
        reference = Path(reference_image)
        if not _valid_anchor_file(reference):
            raise AnchorGenerationError(f"Anchor 参考图不可解码: {reference}")
        provider = (
            _character_edit_provider(image_model)
            if asset_kind == ASSET_CHARACTER
            else _scene_edit_provider(image_model)
        )
        generation_mode = "image_to_image"
    else:
        provider = (
            _character_text_provider(asset_kind, image_model)
            if asset_kind == ASSET_CHARACTER
            else _scene_text_provider(asset_kind, image_model)
        )
        generation_mode = "text_to_image"
    raw_path = target.with_name(f".{target.name}.{os.getpid()}.generated")
    try:
        from common.image_concurrency import image_provider_slot

        with image_provider_slot(
            stage=f"{asset_kind}_anchor", task_id=task_id
        ):
            result = (
                _provider_edit(
                    provider,
                    Path(reference_image),
                    prompt,
                    prompt_profile=prompt_profile,
                )
                if reference_image is not None
                else _provider_generate(
                    provider,
                    prompt,
                    prompt_profile=prompt_profile,
                )
            )
        if not result:
            raise AnchorGenerationError("项目 Provider 未返回图片")
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        _save_provider_result(provider, result, raw_path)
        _normalize_image(
            raw_path,
            target,
            preserve_transparency=(
                asset_kind == ASSET_CHARACTER
                and str(image_model or "").startswith("gpt-image-2")
            ),
        )
    finally:
        raw_path.unlink(missing_ok=True)

    if _usage_tracker:
        _usage_tracker.set_context(task_id, usage_service)
        _usage_tracker.add_image(
            1,
            getattr(provider, "model", image_model),
            provider=provider_scope,
        )
        _usage_tracker.flush()

    with Image.open(target) as image:
        width, height = image.size
    return (
        {
            "sha256": file_sha256(target),
            "width": width,
            "height": height,
            "model": getattr(provider, "model", image_model),
            "generation_mode": generation_mode,
            "prompt_profile": prompt_profile,
            "provider_prompt": str(getattr(provider, "last_prompt", prompt)),
        },
        prompt,
    )


def _provider_generate(provider: Any, prompt: str, *, prompt_profile: str):
    if str(getattr(provider, "model", "")).startswith("gpt-image-2"):
        return provider.generate(prompt, prompt_profile=prompt_profile)
    return provider.generate(prompt)


def _provider_edit(
    provider: Any,
    reference_image: Path,
    prompt: str,
    *,
    prompt_profile: str,
):
    if str(getattr(provider, "model", "")).startswith("gpt-image-2"):
        return provider.edit(
            reference_image,
            prompt,
            prompt_profile=prompt_profile,
        )
    return provider.edit(reference_image, prompt)


def _valid_anchor_file(path: Path, *, require_transparency: bool = False) -> bool:
    if not path.is_file() or path.stat().st_size <= 0:
        return False
    try:
        with Image.open(path) as image:
            image.verify()
        if require_transparency:
            from services.char_designer.utils.native_transparency import (
                validate_native_transparent_character_file,
            )

            return validate_native_transparent_character_file(path)
        return True
    except (UnidentifiedImageError, OSError):
        return False


def _reference_metadata(task_id: str, reference_image: Optional[Path]) -> tuple[str, str]:
    if reference_image is None:
        return "", ""
    reference = Path(reference_image).resolve()
    if not _valid_anchor_file(reference):
        raise AnchorGenerationError(f"Anchor 参考图不可用: {reference}")
    task_root = style_anchor_path(task_id, ASSET_CHARACTER).parents[1].resolve()
    try:
        relative = reference.relative_to(task_root).as_posix()
    except ValueError:
        relative = reference.name
    return relative, file_sha256(reference)


def _raw_anchor_path(target: Path) -> Path:
    return target.parent / "intermediate" / f"{target.stem}_raw.png"


def _two_pass_anchor_enabled(image_model: str) -> bool:
    return (
        str(image_model or "").strip().lower().startswith("gpt-image-2")
        and gpt_image_artifact_suppression_enabled()
        and gpt_image_two_pass_anchor_enabled()
    )


def _anchor_prompt_version(asset_kind: str, image_model: str) -> str:
    base = (
        LLM_ANCHOR_PROMPT_VERSION_TWO_PASS
        if _two_pass_anchor_enabled(image_model)
        else LLM_ANCHOR_PROMPT_VERSION_SINGLE_PASS
    )
    if asset_kind == ASSET_CHARACTER and str(image_model).startswith("gpt-image-2"):
        return f"{base}-{CHARACTER_RGBA_ANCHOR_VERSION_SUFFIX}"
    return base


def _matching_llm_anchor(
    task_id: str,
    asset_kind: str,
    section: Dict[str, Any],
    *,
    global_style: str,
    image_model: str,
    reference_sha256: str,
    prompt_version: str,
) -> Optional[Path]:
    target = style_anchor_path(task_id, asset_kind)
    if (
        str(section.get("source") or "") != LLM_ANCHOR_SOURCE
        or str(section.get("status") or "") != ANCHOR_STATUS_READY
        or str(section.get("global_style") or "").strip() != global_style.strip()
        or str(section.get("requested_model") or "").strip() != image_model
        or str(section.get("prompt_version") or "") != prompt_version
        or str(section.get("reference_sha256") or "") != reference_sha256
    ):
        return None
    require_transparency = (
        asset_kind == ASSET_CHARACTER
        and str(image_model or "").startswith("gpt-image-2")
    )
    if not _valid_anchor_file(
        target,
        require_transparency=require_transparency,
    ):
        try:
            from common.output_mirror import fetch_output_file

            fetched = fetch_output_file(
                task_id,
                Path(section.get("anchor_file") or target.name),
            )
            if fetched is not None:
                target = fetched
        except Exception:
            pass
    if not _valid_anchor_file(
        target,
        require_transparency=require_transparency,
    ):
        return None
    expected = str(section.get("sha256") or "").strip().lower()
    if not expected or file_sha256(target) != expected:
        return None
    return target


def ensure_llm_style_anchor(
    task_id: str,
    asset_kind: str,
    global_style: str,
    image_model: str,
    *,
    provider_scope: str,
    reference_image: Optional[Path] = None,
    usage_service: str,
) -> Path:
    """Generate or reuse one automatic LLM-route Anchor without user confirmation."""
    style = str(global_style or "").strip()
    if not style:
        raise AnchorGenerationError("自动 Anchor 缺少 global_style")
    model = _validate_model(image_model)
    from common.progress_utils import ensure_visual_stage_ownership

    ensure_visual_stage_ownership(task_id)
    prompt_version = _anchor_prompt_version(asset_kind, model)
    reference_file, reference_sha256 = _reference_metadata(
        task_id, reference_image
    )

    config = load_visual_style_config(task_id, fetch_remote=True)
    if config.get("mode") != STYLE_MODE_LLM:
        raise AnchorGenerationError(
            f"任务 {task_id} 不是 LLM 风格路线，禁止自动生成 Anchor"
        )
    existing = _matching_llm_anchor(
        task_id,
        asset_kind,
        config[asset_kind],
        global_style=style,
        image_model=model,
        reference_sha256=reference_sha256,
        prompt_version=prompt_version,
    )
    if existing is not None:
        return existing

    import redis

    client = redis.Redis.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    )
    lock = client.lock(
        f"novel-game:llm-style-anchor:{task_id}:{asset_kind}",
        timeout=max(60, int(os.getenv("STYLE_ANCHOR_LOCK_TTL", "1200"))),
        blocking_timeout=max(
            1, int(os.getenv("STYLE_ANCHOR_LOCK_WAIT_SECONDS", "1200"))
        ),
    )
    if not lock.acquire(blocking=True):
        raise TimeoutError(
            f"等待自动 {asset_kind} Anchor 生成锁超时: task_id={task_id}"
        )
    try:
        config = load_visual_style_config(task_id, fetch_remote=True)
        if config.get("mode") != STYLE_MODE_LLM:
            raise AnchorGenerationError(
                f"任务 {task_id} 已切换为 custom，停止自动 Anchor"
            )
        existing = _matching_llm_anchor(
            task_id,
            asset_kind,
            config[asset_kind],
            global_style=style,
            image_model=model,
            reference_sha256=reference_sha256,
            prompt_version=prompt_version,
        )
        if existing is not None:
            return existing

        target = style_anchor_path(task_id, asset_kind)
        two_pass = _two_pass_anchor_enabled(model)
        raw_published = _raw_anchor_path(target)
        raw_temporary = target.with_name(
            f".{target.name}.{os.getpid()}.llm-raw"
        )
        clean_temporary = target.with_name(
            f".{target.name}.{os.getpid()}.llm-clean"
        )
        raw_temporary.unlink(missing_ok=True)
        clean_temporary.unlink(missing_ok=True)
        published_two_pass = False
        try:
            raw_metadata, raw_prompt = generate_anchor(
                task_id,
                asset_kind,
                style,
                model,
                raw_temporary,
                provider_scope=provider_scope,
                reference_image=reference_image,
                usage_service=usage_service,
                prompt_profile=PROFILE_OFF if two_pass else PROFILE_BALANCED,
            )
            require_transparency = (
                asset_kind == ASSET_CHARACTER
                and str(model).startswith("gpt-image-2")
            )
            if not _valid_anchor_file(
                raw_temporary,
                require_transparency=require_transparency,
            ):
                raise AnchorGenerationError(
                    f"自动 {asset_kind} raw Anchor 未通过图片校验"
                )

            if two_pass:
                try:
                    metadata, prompt = generate_anchor(
                        task_id,
                        asset_kind,
                        style,
                        model,
                        clean_temporary,
                        provider_scope=provider_scope,
                        reference_image=raw_temporary,
                        usage_service=usage_service,
                        prompt_profile=PROFILE_ANCHOR_CLEANUP,
                        cleanup_reference=True,
                    )
                    if not _valid_anchor_file(
                        clean_temporary,
                        require_transparency=require_transparency,
                    ):
                        raise AnchorGenerationError(
                            f"自动 {asset_kind} 清洁 Anchor 未通过图片校验"
                        )
                    publication = clean_temporary
                    published_two_pass = True
                except AnchorGenerationError as exc:
                    if not require_transparency:
                        raise
                    logger.warning(
                        "人物清洁 Anchor 未保持原生 Alpha，回退已验证 raw Anchor: %s",
                        exc,
                    )
                    metadata, prompt = raw_metadata, raw_prompt
                    publication = raw_temporary
            else:
                metadata, prompt = raw_metadata, raw_prompt
                publication = raw_temporary

            target.parent.mkdir(parents=True, exist_ok=True)
            ensure_visual_stage_ownership(task_id)
            os.replace(publication, target)
            if published_two_pass and gpt_image_keep_raw_anchor_enabled():
                raw_published.parent.mkdir(parents=True, exist_ok=True)
                os.replace(raw_temporary, raw_published)
            else:
                raw_published.unlink(missing_ok=True)
        finally:
            raw_temporary.unlink(missing_ok=True)
            clean_temporary.unlink(missing_ok=True)

        # character_anchor 与 scene_anchor 现在允许并行生成。两条任务必须在
        # 发布配置前重新读取并串行合并各自 section，避免后写者用旧快照覆盖
        # 另一张已经完成的 Anchor 元数据。
        config_lock = client.lock(
            f"novel-game:visual-style-config:{task_id}",
            timeout=max(30, int(os.getenv("STYLE_CONFIG_LOCK_TTL", "120"))),
            blocking_timeout=max(
                1, int(os.getenv("STYLE_CONFIG_LOCK_WAIT_SECONDS", "120"))
            ),
        )
        if not config_lock.acquire(blocking=True):
            raise TimeoutError(
                f"等待视觉风格配置发布锁超时: task_id={task_id}"
            )
        try:
            config = load_visual_style_config(task_id, fetch_remote=True)
            if config.get("mode") != STYLE_MODE_LLM:
                raise AnchorGenerationError(
                    f"任务 {task_id} 已切换为 custom，停止发布自动 Anchor"
                )
            section = config[asset_kind]
            section.update(
                global_style=style,
                source=LLM_ANCHOR_SOURCE,
                confirmed=False,
                stale=False,
                sha256=file_sha256(target),
                model=str(metadata.get("model") or model),
                requested_model=model,
                prompt=prompt,
                prompt_version=prompt_version,
                generation_mode=str(metadata.get("generation_mode") or ""),
                prompt_profile=str(metadata.get("prompt_profile") or ""),
                provider_prompt=str(metadata.get("provider_prompt") or prompt),
                raw_anchor_file=(
                    raw_published.relative_to(target.parents[1]).as_posix()
                    if published_two_pass and gpt_image_keep_raw_anchor_enabled()
                    else ""
                ),
                raw_anchor_sha256=(
                    file_sha256(raw_published)
                    if published_two_pass and _valid_anchor_file(raw_published)
                    else ""
                ),
                raw_prompt=raw_prompt,
                raw_provider_prompt=str(
                    raw_metadata.get("provider_prompt") or raw_prompt
                ),
                raw_prompt_profile=str(raw_metadata.get("prompt_profile") or ""),
                reference_file=reference_file,
                reference_sha256=reference_sha256,
                status=ANCHOR_STATUS_READY,
                job_id="",
                error="",
                updated_at=visual_style_timestamp(),
            )
            ensure_visual_stage_ownership(task_id)
            save_visual_style_config(task_id, config, mirror=False)
        finally:
            try:
                config_lock.release()
            except redis.exceptions.LockError:
                logger.warning(
                    "视觉风格配置锁已过期或被释放: task_id=%s",
                    task_id,
                )
        logger.info(
            "自动 Anchor 已发布: task_id=%s kind=%s model=%s reference=%s two_pass=%s",
            task_id,
            asset_kind,
            section["model"],
            reference_file or "(none)",
            published_two_pass,
        )
        return target
    finally:
        try:
            lock.release()
        except redis.exceptions.LockError:
            logger.warning(
                "自动 Anchor 锁已过期或被释放: task_id=%s kind=%s",
                task_id,
                asset_kind,
            )


def _cli_main(argv: Optional[list[str]] = None) -> int:
    """本地命令行入口：给一个 task_id，复刻 tasks.py 的两步生成 character anchor。

    与 char_designer/tasks.py 不同，这里不经过 Celery——直接串联
    ensure_global_style_generated + ensure_llm_style_anchor。
    """
    import argparse

    from common.global_style import ensure_global_style_generated
    from common.visual_style import ASSET_CHARACTER, VALID_ASSET_KINDS

    parser = argparse.ArgumentParser(
        description="本地给 task_id 生成风格 Anchor（复刻 char_designer 两步）",
    )
    parser.add_argument("--task-id", required=True, help="任务 ID（必填）")
    parser.add_argument(
        "--image-model",
        default=ImageModel.DEFAULT,
        choices=ImageModel.VALID_MODELS,
        help=f"生图模型，默认 {ImageModel.DEFAULT}",
    )
    parser.add_argument(
        "--asset-kind",
        default=ASSET_CHARACTER,
        choices=sorted(VALID_ASSET_KINDS),
        help=f"资产类型，默认 {ASSET_CHARACTER}",
    )
    parser.add_argument(
        "--provider-scope",
        default="char_designer",
        choices=["char_designer", "scene_painter"],
        help="Provider 来源，默认 char_designer",
    )
    parser.add_argument(
        "--global-style",
        default=None,
        help="跳过数据库，手动指定风格文本；不填则从 story_tree_json 读取",
    )
    parser.add_argument("--verbose", action="store_true", help="输出 DEBUG 日志")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    model = _validate_model(args.image_model)

    if args.global_style is not None:
        global_style = str(args.global_style).strip()
        if not global_style:
            print("❌ --global-style 不能为空", file=sys.stderr)
            return 1
    else:
        print(f"📖 从数据库读取 global_style: task_id={args.task_id}")
        global_style = ensure_global_style_generated(args.task_id)
    print(f"🎨 global_style: {global_style}")

    try:
        anchor_path = ensure_llm_style_anchor(
            args.task_id,
            args.asset_kind,
            global_style,
            model,
            provider_scope=args.provider_scope,
            usage_service="style_anchor_cli",
        )
    except AnchorGenerationError as exc:
        print(f"❌ 生成失败: {exc}", file=sys.stderr)
        return 1

    print(f"✅ 完成，Anchor 已就绪: {anchor_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli_main())
