import base64
import json
import logging
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

import requests

from common.gpt_image_prompt_cleanliness import prepare_gpt_image_prompt

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
    ):
        _validate_gpt_config(api_key, base_url, model, background)
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
        return _save_image_item(item, output_path)


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
    ):
        _validate_gpt_config(api_key, base_url, model, background)
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
        logger.info(f"GPT Image 图生图 Provider 初始化，模型: {self.model}, 尺寸: {self.size}, 质量: {self.quality}")

    def edit(
        self,
        base_image_path: Path,
        prompt: str,
        *,
        prompt_profile: str = "balanced",
    ) -> List[Dict]:
        return self.edit_multi(
            [base_image_path],
            prompt,
            prompt_profile=prompt_profile,
        )

    def edit_multi(
        self,
        base_image_paths: List[Path],
        prompt: str,
        *,
        prompt_profile: str = "balanced",
    ) -> List[Dict]:
        """图生图，支持一张或多张参考图。

        多图时所有图片以 multipart 字段 `image[]` 重复提交，与单图接口一致；
        生效顺序由后端决定，调用方应把"最锁定外观"的图放第一张。
        """
        if not base_image_paths:
            raise ValueError("edit_multi 至少需要 1 张参考图")

        url = f"{self.base_url}/v1/images/edits"
        final_prompt = prepare_gpt_image_prompt(
            prompt,
            model=self.model,
            has_reference_images=True,
            prompt_profile=prompt_profile,
        )
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

        last_error = None
        for attempt in range(self.max_retries):
            opened: List = []
            try:
                logger.info(
                    f"提交 GPT Image 图生图任务 (尝试 {attempt + 1}/{self.max_retries}, 参考图 {len(base_image_paths)} 张)"
                )
                files: List = []
                for p in base_image_paths:
                    fp = open(p, "rb")
                    opened.append(fp)
                    files.append(("image[]", (p.name, fp, self._content_type(p))))
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
            finally:
                for fp in opened:
                    try:
                        fp.close()
                    except Exception:
                        pass

            if attempt < self.max_retries - 1:
                time.sleep(self.retry_delay)

        raise RuntimeError(f"GPT Image 图生图失败: {last_error}")

    def save_image_item(self, item: Dict, output_path: Path) -> bool:
        return _save_image_item(item, output_path)

    @staticmethod
    def _content_type(image_path: Path) -> str:
        suffix = image_path.suffix.lower()
        if suffix in {".jpg", ".jpeg"}:
            return "image/jpeg"
        if suffix == ".webp":
            return "image/webp"
        return "image/png"


def _validate_gpt_config(api_key: str, base_url: str, model: str, background: str) -> None:
    if not api_key:
        raise ValueError("API Key 未设置，请填写 API_KEY，或设置 GPT_IMAGE_API_KEY / OPENAI_API_KEY 环境变量")
    if not base_url:
        raise ValueError("BASE_URL 未设置")


def _add_optional_fields(target: Dict, fields: Dict) -> None:
    for key, value in fields.items():
        if value:
            target[key] = value


def _moderation_error_from_response(response) -> Optional[GPTImageModerationError]:
    """只识别明确的 moderation_blocked，避免误把普通 HTTP 错误当审核失败。"""

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

    message = str(
        error.get("message")
        or response_text
        or "GPT Image 提示词被安全审核拒绝"
    )
    violations: List[str] = []
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


def _save_image_item(item: Dict, output_path: Path) -> bool:
    try:
        if item.get("url"):
            _download_image(item["url"], output_path)
        elif item.get("b64_json"):
            output_path.write_bytes(base64.b64decode(item["b64_json"]))
        else:
            logger.warning(f"图片响应缺少 url 和 b64_json: {item}")
            return False
        logger.info(f"图片已保存: {output_path}")
        return True
    except Exception as e:
        logger.error(f"保存图片失败 {output_path}: {e}")
        return False


def _download_image(url: str, output_path: Path) -> None:
    response = requests.get(url, stream=True, timeout=300)
    response.raise_for_status()
    with open(output_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)
