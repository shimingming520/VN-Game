"""DashScope Wan 2.5 text-to-image provider for character generation."""

import logging
import os
import time
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class Wan25TextImageProvider:
    """Use Wan 2.5's asynchronous image-synthesis API.

    The public interface intentionally matches ``QwenTextImageProvider`` so the
    character pipeline can switch text-to-image models without changing its
    download/save logic. Character variants and emotions continue to use the
    Qwen-Image-Edit provider.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        size: Optional[str] = None,
        max_retries: int = 3,
        retry_delay: int = 5,
        request_timeout: int = 180,
        poll_interval: float = 2.0,
        poll_max_attempts: int = 150,
    ):
        self.api_key = api_key or os.environ.get("DASHSCOPE_API_KEY", "")
        if not self.api_key:
            raise ValueError("API Key 未设置，请设置 DASHSCOPE_API_KEY 环境变量")
        self.base_url = os.environ.get(
            "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/api/v1"
        ).rstrip("/")
        self.model = model or os.environ.get("WAN_MODEL", "wan2.5-t2i-preview")
        self.size = (size or os.environ.get("WAN_CHARACTER_SIZE", "720*1280")).replace("x", "*")
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.request_timeout = request_timeout
        self.poll_interval = poll_interval
        self.poll_max_attempts = poll_max_attempts

    def generate(self, prompt: str) -> Optional[str]:
        last_error = None
        for attempt in range(self.max_retries):
            try:
                return self._submit_and_poll(prompt)
            except Exception as exc:
                last_error = exc
                logger.error("Wan2.5 文生图异常 (尝试 %s/%s): %s", attempt + 1, self.max_retries, exc)
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay)
        logger.error("Wan2.5 文生图最终失败: %s", last_error)
        return None

    def _submit_and_poll(self, prompt: str) -> str:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        }
        payload = {
            "model": self.model,
            "input": {"prompt": prompt},
            "parameters": {"size": self.size, "n": 1, "watermark": False},
        }
        response = requests.post(
            f"{self.base_url}/services/aigc/text2image/image-synthesis",
            json=payload,
            headers=headers,
            timeout=self.request_timeout,
        )
        response.raise_for_status()
        task_id = (response.json().get("output") or {}).get("task_id")
        if not task_id:
            raise RuntimeError(f"Wan2.5 返回缺少 task_id: {response.text}")

        task_headers = {"Authorization": f"Bearer {self.api_key}"}
        for _ in range(self.poll_max_attempts):
            time.sleep(self.poll_interval)
            task_response = requests.get(
                f"{self.base_url}/tasks/{task_id}",
                headers=task_headers,
                timeout=self.request_timeout,
            )
            task_response.raise_for_status()
            output = task_response.json().get("output") or {}
            status = output.get("task_status")
            if status == "SUCCEEDED":
                results = output.get("results") or []
                if results and results[0].get("url"):
                    return results[0]["url"]
                raise RuntimeError("Wan2.5 响应中没有图片 URL")
            if status == "FAILED":
                raise RuntimeError(output.get("message") or str(output))
            if status not in {"PENDING", "RUNNING"}:
                raise RuntimeError(f"Wan2.5 未知任务状态: {status}")
        raise TimeoutError(f"Wan2.5 任务轮询超时: {task_id}")

    def download(self, url: str, path: str | Path) -> bool:
        try:
            response = requests.get(url, timeout=60, stream=True)
            response.raise_for_status()
            with open(path, "wb") as output:
                for chunk in response.iter_content(8192):
                    if chunk:
                        output.write(chunk)
            return True
        except Exception as exc:
            logger.error("下载 Wan2.5 图片失败: %s", exc)
            return False
