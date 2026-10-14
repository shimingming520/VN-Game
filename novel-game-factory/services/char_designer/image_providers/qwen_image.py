import base64
import logging
import os
import time
from pathlib import Path
from typing import Optional, Tuple

import requests

from services.char_designer.utils.image_input_flatten_transparency import opaque_rgb_png_bytes

logger = logging.getLogger(__name__)


class QwenTextImageProvider:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        aspect_ratio: Optional[str] = None,
        size: Optional[str] = None,
        max_retries: int = 3,
        retry_delay: int = 5,
        request_timeout: int = 180,
    ):
        qwen_api_key = api_key or os.environ.get("GPT_IMAGE_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
        if not qwen_api_key:
            raise ValueError("API Key 未设置，请设置 GPT_IMAGE_API_KEY 环境变量")
        self.api_key = qwen_api_key
        relay_base_url = base_url or os.environ.get(
            "GPT_IMAGE_BASE_URL", "https://ai.farmmx.com"
        )
        self.base_url = relay_base_url.rstrip("/")
        self.model = model or os.environ.get("QWEN_MODEL", "qwen-image-plus")
        self.aspect_ratio = aspect_ratio or os.environ.get("QWEN_ASPECT_RATIO", "3:4")
        self.size = size or self._get_image_size(self.aspect_ratio)
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.request_timeout = request_timeout
        logger.info(f"Qwen 文生图 Provider 初始化，模型: {self.model}, 尺寸: {self.size}, 超时: {request_timeout}s")

    def generate(self, prompt: str) -> Optional[str]:
        url = f"{self.base_url}/v1/images/generations"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self.model,
            "prompt": prompt,
            "size": self.size,
            "n": 1,
        }
        for attempt in range(self.max_retries):
            try:
                logger.info(f"提交 Qwen 文生图任务 (尝试 {attempt + 1}/{self.max_retries}, 超时 {self.request_timeout}s)")
                response = requests.post(url, json=body, headers=headers, timeout=self.request_timeout)
                if response.ok:
                    image_url = self._extract_url(response.json())
                    if image_url:
                        return image_url
                    logger.error(f"未获取到图片URL: {response.text}")
                else:
                    logger.error(f"任务失败 (HTTP {response.status_code}): {response.text}")
            except Exception as e:
                logger.error(f"Qwen 文生图异常 (尝试 {attempt + 1}): {e}")
            if attempt < self.max_retries - 1:
                time.sleep(self.retry_delay)
        return None

    def download(self, url: str, path: str | Path) -> bool:
        return _download(url, path)

    @staticmethod
    def _parse_aspect_ratio(aspect_ratio: str) -> Tuple[int, int]:
        try:
            width, height = map(int, aspect_ratio.split(":"))
            return width, height
        except Exception:
            return 3, 4

    def _get_image_size(self, aspect_ratio: str) -> str:
        width, height = self._parse_aspect_ratio(aspect_ratio)
        if width == 1 and height == 1:
            return "1328*1328"
        if width == 3 and height == 4:
            return "1140*1472"
        if width == 4 and height == 3:
            return "1472*1140"
        if width == 9 and height == 16:
            return "928*1664"
        if width == 16 and height == 9:
            return "1664*928"
        return "1140*1472"

    @staticmethod
    def _extract_url(response) -> Optional[str]:
        return _extract_url(response)


class QwenImageEditProvider:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        size: str = "1140*1472",
        n: int = 1,
        watermark: bool = False,
        prompt_extend: bool = True,
        negative_prompt: str = "",
        max_retries: int = 3,
        retry_delay: int = 5,
        request_timeout: int = 180,
    ):
        qwen_api_key = api_key or os.environ.get("GPT_IMAGE_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
        if not qwen_api_key:
            raise ValueError("API Key 未设置，请设置 GPT_IMAGE_API_KEY 环境变量")
        self.api_key = qwen_api_key
        relay_base_url = base_url or os.environ.get(
            "GPT_IMAGE_BASE_URL", "https://ai.farmmx.com"
        )
        self.base_url = relay_base_url.rstrip("/")
        self.model = model or os.environ.get("QWEN_VARIANT_MODEL", "qwen-image-edit-plus")
        self.size = size
        self.n = n
        self.watermark = watermark
        self.prompt_extend = prompt_extend
        self.negative_prompt = negative_prompt
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.request_timeout = request_timeout
        logger.info(f"Qwen 图生图 Provider 初始化，模型: {self.model}, 尺寸: {self.size}, 超时: {request_timeout}s")

    def generate(self, base_image_path: str | Path, prompt: str) -> Optional[str]:
        try:
            image_base64 = self._encode_image_to_base64(str(base_image_path))
        except Exception as e:
            logger.error(f"编码图片失败: {base_image_path}, 错误: {e}")
            return None

        url = f"{self.base_url}/v1/images/edits"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        data = {
            "model": self.model,
            "prompt": prompt,
            "size": self.size,
            "n": str(self.n),
            "watermark": str(self.watermark).lower(),
            "prompt_extend": str(self.prompt_extend).lower(),
        }
        if self.negative_prompt:
            data["negative_prompt"] = self.negative_prompt

        for attempt in range(self.max_retries):
            try:
                logger.info(f"提交 Qwen 图生图任务 (尝试 {attempt + 1}/{self.max_retries}, 超时 {self.request_timeout}s)")
                image_bytes = base64.b64decode(image_base64.removeprefix("data:image/png;base64,"))
                files = {"image": ("input.png", image_bytes, "image/png")}
                response = requests.post(
                    url,
                    data=data,
                    files=files,
                    headers=headers,
                    timeout=self.request_timeout,
                )
                if response.ok:
                    image_url = self._extract_url(response.json())
                    if image_url:
                        return image_url
                    logger.error(f"未获取到图片URL: {response.text}")
                else:
                    logger.error(f"任务失败 (HTTP {response.status_code}): {response.text}")
            except Exception as e:
                logger.error(f"Qwen 图生图异常 (尝试 {attempt + 1}): {e}")
            if attempt < self.max_retries - 1:
                time.sleep(self.retry_delay)
        return None

    def download(self, url: str, path: str | Path) -> bool:
        return _download(url, path)

    @staticmethod
    def _encode_image_to_base64(image_path: str) -> str:
        # 图生图模型只接收内存中合成好的白底 RGB PNG，绝不直接上传 RGBA。
        encoded = base64.b64encode(opaque_rgb_png_bytes(image_path)).decode("utf-8")
        return f"data:image/png;base64,{encoded}"

    @staticmethod
    def _extract_url(response) -> Optional[str]:
        return _extract_url(response)


def _extract_url(response) -> Optional[str]:
    try:
        for item in response.get("data", []):
            if item.get("url"):
                return item["url"]
    except Exception as e:
        logger.error(f"提取URL失败: {e}")
    return None


def _download(url: str, path: str | Path) -> bool:
    try:
        response = requests.get(url, timeout=30, stream=True)
        if response.status_code == 200:
            with open(path, "wb") as f:
                for chunk in response.iter_content(8192):
                    if chunk:
                        f.write(chunk)
            return True
        logger.error(f"下载失败 HTTP {response.status_code}")
    except Exception as e:
        logger.error(f"下载异常: {e}")
    return False
