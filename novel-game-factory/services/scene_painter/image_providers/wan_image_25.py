"""DashScope Wan 2.5 / wanx 旧异步任务接口的 Provider。

接口签名对齐 ``WanImageTextProvider`` 与 ``GPTImageTextProvider``：
    provider = WanLegacyImageTextProvider(api_key=..., model="wan2.5-t2i-preview")
    data_items = provider.generate(prompt)  # -> List[{"url": "https://..."}]

为什么单独成文件：
- wan2.5* / wanx* 必须走 ``/services/aigc/text2image/image-synthesis``，
  调用模式是「提交任务 → 轮询 /tasks/{id} → 取 results[].url」，
  与 wan2.6+/wan2.7 的同步多模态接口完全不同。
- ``wan_image.WanImageTextProvider`` 现在只处理 wan2.6+/wan2.7。
- 提示词外壳（quality suffix）通过 ``wan_config`` 与 wan2.7 同源，保证两条
  链路写进图片的画风/质量后缀完全一致。

scene_generator 会按 ``wan_config.is_legacy_wan(model)`` 判断模型版本，
自动选择本 provider 还是 ``WanImageTextProvider``。
"""

import logging
import time
from typing import Dict, List, Optional

import requests

from .wan_config import (
    DEFAULT_BASE_URL,
    DEFAULT_MAX_RETRIES,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_POLL_MAX_ATTEMPTS,
    DEFAULT_RETRY_DELAY,
    DEFAULT_SIZE,
    DEFAULT_TIMEOUT_SECONDS,
    LEGACY_SUBMIT_PATH,
    LEGACY_TASK_PATH,
    WAN_QUALITY_SUFFIX,
    is_legacy_wan,
    normalize_size,
    with_quality_suffix,
)

logger = logging.getLogger(__name__)


class WanLegacyImageTextProvider:
    """DashScope Wan 2.5 / wanx 异步任务接口 Provider。"""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        model: str = "wan2.5-t2i-preview",
        size: str = DEFAULT_SIZE,
        n: int = 1,
        watermark: bool = False,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_delay: int = DEFAULT_RETRY_DELAY,
        quality_suffix: str = WAN_QUALITY_SUFFIX,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        poll_max_attempts: int = DEFAULT_POLL_MAX_ATTEMPTS,
    ):
        if not api_key:
            raise ValueError("API Key 未设置，请填写 DASHSCOPE_API_KEY")
        if not base_url:
            raise ValueError("BASE_URL 未设置")
        if not is_legacy_wan(model):
            raise ValueError(
                f"模型 {model} 不属于 wan2.5/wanx 旧异步任务接口，"
                "请改用 WanImageTextProvider（wan_image.py）"
            )
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.size = normalize_size(size)
        self.n = n
        self.watermark = watermark
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.quality_suffix = (quality_suffix or "").strip()
        self.poll_interval = poll_interval
        self.poll_max_attempts = poll_max_attempts
        logger.info(
            f"Wan2.5 文生图 Provider 初始化（legacy-async），模型: {self.model}, "
            f"尺寸: {self.size}, base: {self.base_url}"
        )

    def generate(self, prompt: str) -> List[Dict]:
        final_prompt = with_quality_suffix(prompt, self.quality_suffix)
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                logger.info(
                    f"提交 Wan2.5 文生图任务 (尝试 {attempt + 1}/{self.max_retries}, 模型 {self.model})"
                )
                results = self._call_legacy(final_prompt)
                if results:
                    return results
                raise RuntimeError("Wan2.5 响应中没有图片 URL")
            except Exception as e:
                last_error = e
                logger.error(f"Wan2.5 文生图请求异常 (尝试 {attempt + 1}): {e}")
            if attempt < self.max_retries - 1:
                time.sleep(self.retry_delay)
        raise RuntimeError(f"Wan2.5 文生图失败: {last_error}")

    # ------------------------------------------------------------------
    # wan2.5 / wanx 旧异步任务接口
    # ------------------------------------------------------------------
    def _call_legacy(self, prompt: str) -> List[Dict]:
        submit_url = f"{self.base_url}{LEGACY_SUBMIT_PATH}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        }
        payload = {
            "model": self.model,
            "input": {"prompt": prompt},
            "parameters": {
                "size": self.size,
                "n": self.n,
                "watermark": self.watermark,
            },
        }
        resp = requests.post(submit_url, json=payload, headers=headers, timeout=self.timeout_seconds)
        if not resp.ok:
            raise RuntimeError(f"提交任务失败 HTTP {resp.status_code}: {resp.text}")
        body = resp.json()
        task_id = (body.get("output") or {}).get("task_id")
        if not task_id:
            raise RuntimeError(f"提交任务返回缺少 task_id: {body}")

        task_url = f"{self.base_url}{LEGACY_TASK_PATH.format(task_id=task_id)}"
        task_headers = {"Authorization": f"Bearer {self.api_key}"}
        for _ in range(self.poll_max_attempts):
            time.sleep(self.poll_interval)
            tr = requests.get(task_url, headers=task_headers, timeout=self.timeout_seconds)
            if not tr.ok:
                raise RuntimeError(f"查询任务失败 HTTP {tr.status_code}: {tr.text}")
            output = tr.json().get("output") or {}
            status = output.get("task_status", "UNKNOWN")
            if status == "SUCCEEDED":
                results = output.get("results") or []
                return [{"url": item["url"]} for item in results if item.get("url")]
            if status == "FAILED":
                raise RuntimeError(f"任务失败: {output.get('message', output)}")
            if status not in {"PENDING", "RUNNING"}:
                raise RuntimeError(f"未知任务状态: {status}")
        raise RuntimeError(f"任务超时: 轮询 {self.poll_max_attempts} 次后仍未完成 (task_id={task_id})")
