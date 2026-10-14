"""DashScope Wan 文生图 Provider（wan2.6+ / wan2.7 同步多模态接口）。

接口签名对齐 GPTImageTextProvider：
    provider = WanImageTextProvider(api_key=..., model="wan2.7-image-pro", size="1792x1008")
    data_items = provider.generate(prompt)  # -> List[{"url": "https://..."}]

历史说明：
- 早期 wan2.5* / wanx* 走旧的异步任务接口 /services/aigc/text2image/image-synthesis，
  现已迁出到 ``wan25_image.WanLegacyImageTextProvider``，本模块不再处理这一分支。
- wan2.6+ / wan2.7-* 仍由本 provider 处理，调用同步多模态接口
  /services/aigc/multimodal-generation/generation
  （同步返回 output.choices[0].message.content[].image）。

提示词外壳与 wan2.5 通过 ``wan_config.WAN_QUALITY_SUFFIX`` 同源。
"""

import logging
import time
from typing import Dict, List, Optional

import requests

from .wan_config import (
    DEFAULT_BASE_URL,
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRY_DELAY,
    DEFAULT_SIZE,
    DEFAULT_TIMEOUT_SECONDS,
    NEW_SYNC_PATH,
    WAN_QUALITY_SUFFIX,
    is_legacy_wan,
    normalize_size,
    with_quality_suffix,
)

logger = logging.getLogger(__name__)


class WanImageTextProvider:
    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        model: str = "wan2.7-image-pro",
        size: str = DEFAULT_SIZE,
        n: int = 1,
        watermark: bool = False,
        thinking_mode: bool = True,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_delay: int = DEFAULT_RETRY_DELAY,
        quality_suffix: str = WAN_QUALITY_SUFFIX,
    ):
        if not api_key:
            raise ValueError("API Key 未设置，请填写 DASHSCOPE_API_KEY")
        if not base_url:
            raise ValueError("BASE_URL 未设置")
        if is_legacy_wan(model):
            raise ValueError(
                f"模型 {model} 属于 wan2.5/wanx 旧异步任务接口，请改用 "
                "WanLegacyImageTextProvider（wan25_image.py）"
            )
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.size = normalize_size(size)
        self.n = n
        self.watermark = watermark
        self.thinking_mode = thinking_mode
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.quality_suffix = (quality_suffix or "").strip()
        logger.info(
            f"Wan 文生图 Provider 初始化（sync-multimodal），模型: {self.model}, "
            f"尺寸: {self.size}, base: {self.base_url}"
        )

    def generate(self, prompt: str) -> List[Dict]:
        final_prompt = with_quality_suffix(prompt, self.quality_suffix)
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                logger.info(
                    f"提交 Wan 文生图任务 (尝试 {attempt + 1}/{self.max_retries}, 模型 {self.model})"
                )
                results = self._call_sync(final_prompt)
                if results:
                    return results
                raise RuntimeError("Wan 响应中没有图片 URL")
            except Exception as e:
                last_error = e
                logger.error(f"Wan 文生图请求异常 (尝试 {attempt + 1}): {e}")
            if attempt < self.max_retries - 1:
                time.sleep(self.retry_delay)
        raise RuntimeError(f"Wan 文生图失败: {last_error}")

    # ------------------------------------------------------------------
    # wan2.6 / wan2.7 新同步多模态接口
    # ------------------------------------------------------------------
    def _call_sync(self, prompt: str) -> List[Dict]:
        url = f"{self.base_url}{NEW_SYNC_PATH}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "input": {
                "messages": [
                    {
                        "role": "user",
                        "content": [{"text": prompt}],
                    }
                ]
            },
            "parameters": {
                "size": self.size,
                "n": self.n,
                "watermark": self.watermark,
                "thinking_mode": self.thinking_mode,
            },
        }
        resp = requests.post(url, json=payload, headers=headers, timeout=self.timeout_seconds)
        if not resp.ok:
            raise RuntimeError(f"请求失败 HTTP {resp.status_code}: {resp.text}")
        return self._extract_image_urls_from_sync(resp.json())

    @staticmethod
    def _extract_image_urls_from_sync(body: Dict) -> List[Dict]:
        if body.get("code"):
            raise RuntimeError(f"Wan 接口返回错误 {body.get('code')}: {body.get('message')}")
        output = body.get("output") or {}
        choices = output.get("choices") or []
        items: List[Dict] = []
        for choice in choices:
            message = choice.get("message") or {}
            for content in message.get("content") or []:
                if content.get("type") == "image" and content.get("image"):
                    items.append({"url": content["image"]})
        return items
