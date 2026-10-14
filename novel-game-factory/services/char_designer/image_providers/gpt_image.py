import base64
import json
import logging
import os
import re
import time
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional

import requests

from common.gpt_image_prompt_cleanliness import prepare_gpt_image_prompt
from services.char_designer.utils.image_input_flatten_transparency import opaque_rgb_png_bytes
from services.char_designer.utils.native_transparency import (
    NATIVE_TRANSPARENCY_REPAIR_PROMPT,
    inspect_native_transparency,
    is_gpt_image_2,
    prepare_transparent_character_prompt,
    rgba_png_bytes,
    validate_native_transparent_character_file,
)

logger = logging.getLogger(__name__)


class GPTImageModerationError(RuntimeError):
    """GPT Image 明确拒绝当前提示词时抛出的结构化异常。"""

    def __init__(
        self,
        *,
        status_code: int,
        message: str,
        error_type: str = "",
        code: str = "moderation_blocked",
        violations: Optional[List[str]] = None,
        response_text: str = "",
    ):
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type
        self.code = code
        self.violations = violations or []
        self.response_text = response_text

    @property
    def reason(self) -> str:
        violation_text = ",".join(self.violations)
        suffix = f": {violation_text}" if violation_text else ""
        return f"{self.code}{suffix}"


class GPTImageTextProvider:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str = "gpt-image-2",
        size: str = "1024x1536",
        quality: str = "high",
        n: int = 1,
        response_format: str = "",
        output_format: str = "",
        background: str = "",
        moderation: str = "",
        timeout_seconds: int = 300,
        max_retries: int = 3,
        retry_delay: int = 5,
        transparent_character_output: bool = False,
    ):
        if transparent_character_output:
            background = "transparent"
            output_format = "png"
        _validate_gpt_config(
            api_key,
            base_url,
            model,
            background,
            output_format,
            transparent_character_output,
        )
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.size = size
        self.quality = quality
        self.n = n
        self.response_format = response_format
        self.output_format = output_format
        self.background = background
        self.moderation = moderation
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.transparent_character_output = transparent_character_output
        logger.info(f"GPT Image 文生图 Provider 初始化，模型: {self.model}, 尺寸: {self.size}, 质量: {self.quality}")

    def generate(
        self,
        prompt: str,
        *,
        prompt_profile: str = "balanced",
    ) -> List[Dict]:
        url = f"{self.base_url}/v1/images/generations"
        final_prompt = prepare_gpt_image_prompt(
            prompt,
            model=self.model,
            prompt_profile=prompt_profile,
        )
        if self.transparent_character_output:
            final_prompt = prepare_transparent_character_prompt(final_prompt)
        self.last_prompt = final_prompt
        self.last_prompt_profile = prompt_profile
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        body = {
            "model": self.model,
            "prompt": final_prompt,
            "size": self.size,
            "quality": self.quality,
            "n": self.n,
        }
        _add_optional_fields(body, {
            "response_format": self.response_format,
            "output_format": self.output_format,
            "background": self.background,
            "moderation": self.moderation,
        })
        logger.info(
            "GPT Image 文生图 API 参数: model=%s size=%s quality=%s "
            "output_format=%s background=%s native_rgba=%s",
            self.model,
            self.size,
            self.quality,
            body.get("output_format") or "(default)",
            body.get("background") or "(default)",
            self.transparent_character_output,
        )

        last_error = None
        for attempt in range(self.max_retries):
            try:
                logger.info(f"提交 GPT Image 文生图任务 (尝试 {attempt + 1}/{self.max_retries})")
                response = requests.post(url, json=body, headers=headers, timeout=self.timeout_seconds)
                if response.ok:
                    data_items = response.json().get("data", [])
                    if data_items:
                        return data_items
                    raise RuntimeError(f"响应中没有 data: {response.text}")
                moderation_error = _moderation_error_from_response(response)
                if moderation_error is not None:
                    logger.error(
                        "GPT Image 文生图提示词被安全审核拒绝，不再原样重试: %s",
                        moderation_error.reason,
                    )
                    raise moderation_error
                last_error = RuntimeError(f"请求失败 HTTP {response.status_code}: {response.text}")
                logger.error(last_error)
            except GPTImageModerationError:
                raise
            except Exception as e:
                last_error = e
                logger.error(f"GPT Image 文生图请求异常 (尝试 {attempt + 1}): {e}")
            if attempt < self.max_retries - 1:
                time.sleep(self.retry_delay)
        raise RuntimeError(f"GPT Image 文生图失败: {last_error}")

    def save_image_item(self, item: Dict, output_path: Path) -> bool:
        return _save_image_item(
            item,
            output_path,
            require_native_transparency=self.transparent_character_output,
            transparency_repair_provider=self,
        )


class GPTImageEditProvider:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str = "gpt-image-2",
        size: str = "1024x1536",
        quality: str = "high",
        n: int = 1,
        response_format: str = "",
        output_format: str = "",
        background: str = "",
        moderation: str = "",
        timeout_seconds: int = 300,
        max_retries: int = 3,
        retry_delay: int = 5,
        transparent_character_output: bool = False,
        preserve_input_transparency: Optional[bool] = None,
    ):
        if transparent_character_output:
            background = "transparent"
            output_format = "png"
        _validate_gpt_config(
            api_key,
            base_url,
            model,
            background,
            output_format,
            transparent_character_output,
        )
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.size = size
        self.quality = quality
        self.n = n
        self.response_format = response_format
        self.output_format = output_format
        self.background = background
        self.moderation = moderation
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.transparent_character_output = transparent_character_output
        self.preserve_input_transparency = (
            transparent_character_output
            if preserve_input_transparency is None
            else bool(preserve_input_transparency)
        )
        logger.info(f"GPT Image 图生图 Provider 初始化，模型: {self.model}, 尺寸: {self.size}, 质量: {self.quality}")

    def edit(
        self,
        base_image_path: Path,
        prompt: str,
        *,
        prompt_profile: str = "balanced",
    ) -> List[Dict]:
        url = f"{self.base_url}/v1/images/edits"
        final_prompt = prepare_gpt_image_prompt(
            prompt,
            model=self.model,
            has_reference_images=True,
            prompt_profile=prompt_profile,
        )
        if self.transparent_character_output:
            final_prompt = prepare_transparent_character_prompt(final_prompt)
        self.last_prompt = final_prompt
        self.last_prompt_profile = prompt_profile
        headers = {"Authorization": f"Bearer {self.api_key}"}
        data = {
            "model": self.model,
            "prompt": final_prompt,
            "size": self.size,
            "quality": self.quality,
            "n": str(self.n),
        }
        _add_optional_fields(data, {
            "response_format": self.response_format,
            "output_format": self.output_format,
            "background": self.background,
            "moderation": self.moderation,
        })

        if self.preserve_input_transparency:
            # 人物变体/情绪编辑必须保留上一张角色图的 Alpha；官方透明
            # 编辑合同也要求整个下游链路持续保留 PNG/WebP 透明通道。
            image_bytes = rgba_png_bytes(base_image_path)
            upload_name = f"{base_image_path.stem}_rgba.png"
        else:
            # 非透明用途（例如风格 Anchor）仍把输入完整合成到白底，避免
            # 隐藏 RGB 像素影响普通图生图。
            image_bytes = opaque_rgb_png_bytes(base_image_path)
            upload_name = f"{base_image_path.stem}_white_rgb.png"

        logger.info(
            "GPT Image 图生图 API 参数: model=%s size=%s quality=%s "
            "output_format=%s background=%s native_rgba=%s input_alpha=%s",
            self.model,
            self.size,
            self.quality,
            data.get("output_format") or "(default)",
            data.get("background") or "(default)",
            self.transparent_character_output,
            self.preserve_input_transparency,
        )

        last_error = None
        for attempt in range(self.max_retries):
            try:
                logger.info(f"提交 GPT Image 图生图任务 (尝试 {attempt + 1}/{self.max_retries})")
                with BytesIO(image_bytes) as image_file:
                    files = {"image[]": (upload_name, image_file, "image/png")}
                    response = requests.post(url, data=data, files=files, headers=headers, timeout=self.timeout_seconds)

                if response.ok:
                    data_items = response.json().get("data", [])
                    if data_items:
                        return data_items
                    raise RuntimeError(f"响应中没有 data: {response.text}")

                moderation_error = _moderation_error_from_response(response)
                if moderation_error is not None:
                    logger.error(
                        "GPT Image 图生图提示词被安全审核拒绝，不再原样重试: %s",
                        moderation_error.reason,
                    )
                    raise moderation_error
                last_error = RuntimeError(f"请求失败 HTTP {response.status_code}: {response.text}")
                logger.error(last_error)
            except GPTImageModerationError:
                raise
            except Exception as e:
                last_error = e
                logger.error(f"GPT Image 图生图请求异常 (尝试 {attempt + 1}): {e}")

            if attempt < self.max_retries - 1:
                time.sleep(self.retry_delay)

        raise RuntimeError(f"GPT Image 图生图失败: {last_error}")

    def save_image_item(self, item: Dict, output_path: Path) -> bool:
        return _save_image_item(
            item,
            output_path,
            require_native_transparency=self.transparent_character_output,
            transparency_repair_provider=self,
        )

    @staticmethod
    def _content_type(image_path: Path) -> str:
        suffix = image_path.suffix.lower()
        if suffix in {".jpg", ".jpeg"}:
            return "image/jpeg"
        if suffix == ".webp":
            return "image/webp"
        return "image/png"


def _validate_gpt_config(
    api_key: str,
    base_url: str,
    model: str,
    background: str,
    output_format: str,
    transparent_character_output: bool,
) -> None:
    if not api_key:
        raise ValueError("API Key 未设置，请填写 API_KEY，或设置 GPT_IMAGE_API_KEY / OPENAI_API_KEY 环境变量")
    if not base_url:
        raise ValueError("BASE_URL 未设置")
    if transparent_character_output and not is_gpt_image_2(model):
        raise ValueError("人物原生透明输出目前只支持 gpt-image-2")
    if background == "transparent" and output_format not in {"png", "webp"}:
        raise ValueError("透明背景必须使用 png 或 webp 输出")


def _add_optional_fields(target: Dict, fields: Dict) -> None:
    for key, value in fields.items():
        if value:
            target[key] = value


def _moderation_error_from_response(response) -> Optional[GPTImageModerationError]:
    """只识别明确的 moderation_blocked；其它 HTTP 错误保持原有重试行为。"""
    response_text = getattr(response, "text", "") or ""
    try:
        payload = response.json()
    except Exception:
        try:
            payload = json.loads(response_text)
        except Exception:
            payload = {}

    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    if not isinstance(error, dict) or error.get("code") != "moderation_blocked":
        return None

    message = str(error.get("message") or response_text or "GPT Image 提示词被安全审核拒绝")
    violations = []
    match = re.search(r"safety_violations=\[([^\]]*)\]", message)
    if match:
        violations = [item.strip() for item in match.group(1).split(",") if item.strip()]

    return GPTImageModerationError(
        status_code=int(getattr(response, "status_code", 0) or 0),
        message=message,
        error_type=str(error.get("type") or ""),
        code=str(error.get("code") or "moderation_blocked"),
        violations=violations,
        response_text=response_text,
    )


def _save_image_item(
    item: Dict,
    output_path: Path,
    *,
    require_native_transparency: bool = False,
    transparency_repair_provider=None,
) -> bool:
    try:
        if item.get("url"):
            _download_image(item["url"], output_path)
        elif item.get("b64_json"):
            output_path.write_bytes(base64.b64decode(item["b64_json"]))
        else:
            logger.warning(f"图片响应缺少 url 和 b64_json: {item}")
            return False
        if require_native_transparency:
            metrics = inspect_native_transparency(output_path)
            if not validate_native_transparent_character_file(output_path):
                logger.warning(
                    "GPT Image 首次返回未保持人物 Alpha，启动 Provider 原生透明整理: path=%s metrics=%s",
                    output_path,
                    metrics,
                )
                if transparency_repair_provider is None or not _repair_native_transparency(
                    transparency_repair_provider,
                    output_path,
                ):
                    logger.error(
                        "GPT Image Provider 原生透明整理失败: path=%s",
                        output_path,
                    )
                    output_path.unlink(missing_ok=True)
                    return False
                metrics = inspect_native_transparency(output_path)
            logger.info(
                "GPT Image 原生透明校验通过: path=%s mode=%s alpha=%s-%s transparent_ratio=%.4f",
                output_path,
                metrics.get("mode"),
                metrics.get("alpha_min"),
                metrics.get("alpha_max"),
                metrics.get("fully_transparent_ratio", 0.0),
            )
        logger.info(f"图片已保存: {output_path}")
        return True
    except Exception as e:
        logger.error(f"保存图片失败 {output_path}: {e}")
        return False


def _repair_native_transparency(provider, output_path: Path) -> bool:
    """Use GPT Image itself to isolate an opaque character; no local matting."""
    temporary = output_path.with_name(f".{output_path.name}.provider-rgba-repair")
    temporary.unlink(missing_ok=True)
    repair_quality = os.getenv(
        "GPT_IMAGE_TRANSPARENCY_REPAIR_QUALITY",
        "medium",
    ).strip().lower()
    if repair_quality not in {"low", "medium", "high", "auto"}:
        repair_quality = "medium"
    try:
        repair_attempts = max(
            1,
            min(
                5,
                int(os.getenv("GPT_IMAGE_TRANSPARENCY_REPAIR_MAX_ATTEMPTS", "3")),
            ),
        )
    except ValueError:
        repair_attempts = 3
    try:
        repair_provider = GPTImageEditProvider(
            api_key=provider.api_key,
            base_url=provider.base_url,
            model=provider.model,
            size=provider.size,
            quality=repair_quality,
            n=1,
            response_format=provider.response_format,
            output_format="png",
            background="transparent",
            moderation=provider.moderation,
            timeout_seconds=provider.timeout_seconds,
            max_retries=provider.max_retries,
            retry_delay=provider.retry_delay,
            # The dedicated prompt below already asks only for background
            # isolation. Keep automatic character-prompt expansion disabled so
            # it cannot dilute this minimal repair instruction; the explicit
            # API background/output parameters and strict validator remain on.
            transparent_character_output=False,
            preserve_input_transparency=False,
        )
        for attempt in range(1, repair_attempts + 1):
            temporary.unlink(missing_ok=True)
            items = repair_provider.edit(
                output_path,
                NATIVE_TRANSPARENCY_REPAIR_PROMPT,
                prompt_profile="off",
            )
            if items:
                try:
                    from common.usage_tracker import tracker

                    tracker.add_image(len(items), provider.model, provider="gpt")
                except Exception:
                    logger.warning(
                        "Provider 原生透明整理用量记录失败",
                        exc_info=True,
                    )
            if items and _save_image_item(
                items[0],
                temporary,
                require_native_transparency=True,
                transparency_repair_provider=None,
            ):
                os.replace(temporary, output_path)
                logger.info(
                    "GPT Image Provider 原生透明整理完成: path=%s quality=%s attempt=%s/%s",
                    output_path,
                    repair_quality,
                    attempt,
                    repair_attempts,
                )
                return True
            logger.warning(
                "GPT Image Provider 原生透明整理未通过，准备有限重试: path=%s attempt=%s/%s",
                output_path,
                attempt,
                repair_attempts,
            )
        return False
    except Exception:
        logger.exception("GPT Image Provider 原生透明整理异常: %s", output_path)
        return False
    finally:
        temporary.unlink(missing_ok=True)


def _download_image(url: str, output_path: Path) -> None:
    response = requests.get(url, stream=True, timeout=300)
    response.raise_for_status()
    with open(output_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)
