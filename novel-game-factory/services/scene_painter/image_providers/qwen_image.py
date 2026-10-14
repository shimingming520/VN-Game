"""DashScope 千问图像模型 Provider（qwen-image* 系列同步多模态接口）。

接口签名对齐 ``WanImageTextProvider`` / ``GPTImageTextProvider``：
    provider = QwenImageTextProvider(api_key=..., model="qwen-image-2.0-pro")
    data_items = provider.generate(prompt)  # -> List[{"url": "https://..."}]

图生图（qwen-image-edit / qwen-image-edit-plus / qwen-image-edit-max /
qwen-image-2.0[-pro]）：
    provider = QwenImageEditProvider(api_key=..., model="qwen-image-edit-plus")
    data_items = provider.edit(base_image_path, prompt)
    data_items = provider.edit_multi([img1, img2, img3], prompt)
    # -> List[{"url": "https://..."}]
    # 之后可调用 provider.save_image_item(item, output_path) 落盘。

实现要点（来自 markdown/千问图像模型技术文档.md / 千问图生图new.md /
qwen-image-edit-plus技术文档.md）：

- 文生图与图生图共用 ``/services/aigc/multimodal-generation/generation``，
  请求体外壳与 wan2.7 一致；图生图就是在 ``messages[0].content`` 里多塞
  1-3 个 ``{"image": "<url|data:image/...;base64,...>"}`` 元素。
- ``parameters`` 支持 ``size / n / negative_prompt / prompt_extend / seed /
  watermark``。``n`` 会按系列自动归一化（文生 2.0 系列 1-6，max/plus/v1 强制 1；
  图生 ``qwen-image-edit`` 强制 1，其他 edit-capable 1-6）。
- 千问对 prompt 长度限制：2.0 系列 1300 token，其他 800 token；这里不做截断，
  调用方控制即可。
- 默认开启 ``prompt_extend``，与官方推荐一致；``qwen-image-edit`` 不支持，
  自动跳过。
- 返回值 ``output.choices[0].message.content[*].image`` 为 24 小时有效的图片 URL。

提示词外壳（quality suffix）通过 ``wan_config`` 与 wan2.5 / wan2.7 同源；
``negative_prompt`` 默认与 wan 一致不传，需要时调用方显式注入
``qwen_config.QWEN_NEGATIVE_PROMPT``。

仅 ``qwen-image-plus / qwen-image`` 支持旧异步任务接口；本 provider 暂不切走，
统一走同步接口即可，足以覆盖现有 scene_painter / cover_generator / cg_generator 链路。
"""

from __future__ import annotations

import logging
import mimetypes
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Union

import requests

from .qwen_config import (
    DEFAULT_BASE_URL,
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRY_DELAY,
    DEFAULT_TIMEOUT_SECONDS,
    default_size_for,
    is_qwen_edit_capable,
    is_qwen_edit_only,
    is_qwen_model,
    normalize_edit_n,
    normalize_n,
    normalize_size,
    supports_edit_prompt_extend,
    supports_edit_size,
)
from .wan_config import WAN_QUALITY_SUFFIX, with_quality_suffix

logger = logging.getLogger(__name__)


class QwenImageTextProvider:
    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        model: str = "qwen-image-2.0-pro",
        size: str = "",
        n: int = 1,
        watermark: bool = False,
        prompt_extend: bool = True,
        negative_prompt: str = "",
        seed: Optional[int] = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_delay: int = DEFAULT_RETRY_DELAY,
        quality_suffix: str = WAN_QUALITY_SUFFIX,
    ):
        if not api_key:
            raise ValueError("API Key 未设置，请填写 GPT_IMAGE_API_KEY")
        if not base_url:
            raise ValueError("BASE_URL 未设置")
        if not is_qwen_model(model):
            raise ValueError(
                f"模型 {model} 不属于 qwen-image* 系列，"
                "请改用 WanImageTextProvider / WanLegacyImageTextProvider / GPTImageTextProvider"
            )
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.size = normalize_size(size or default_size_for(model), model)
        self.n = normalize_n(n, model)
        self.watermark = watermark
        self.prompt_extend = prompt_extend
        self.negative_prompt = (negative_prompt or "").strip()
        self.seed = seed
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.quality_suffix = (quality_suffix or "").strip()
        logger.info(
            f"Qwen 文生图 Provider 初始化（sync-multimodal），模型: {self.model}, "
            f"尺寸: {self.size}, n: {self.n}, base: {self.base_url}"
        )

    def generate(self, prompt: str) -> List[Dict]:
        final_prompt = with_quality_suffix(prompt, self.quality_suffix)
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                logger.info(
                    f"提交 Qwen 文生图任务 (尝试 {attempt + 1}/{self.max_retries}, 模型 {self.model})"
                )
                results = self._call_sync(final_prompt)
                if results:
                    return results
                raise RuntimeError("Qwen 响应中没有图片 URL")
            except Exception as e:
                last_error = e
                logger.error(f"Qwen 文生图请求异常 (尝试 {attempt + 1}): {e}")
            if attempt < self.max_retries - 1:
                time.sleep(self.retry_delay)
        raise RuntimeError(f"Qwen 文生图失败: {last_error}")

    # ------------------------------------------------------------------
    # qwen-image* 同步多模态接口
    # ------------------------------------------------------------------
    def _call_sync(self, prompt: str) -> List[Dict]:
        url = f"{self.base_url}/v1/images/generations"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body: Dict = {
            "model": self.model,
            "prompt": prompt,
            "size": self.size,
            "n": self.n,
        }
        if self.negative_prompt:
            body["negative_prompt"] = self.negative_prompt
        if self.seed is not None:
            body["seed"] = int(self.seed)

        resp = requests.post(url, json=body, headers=headers, timeout=self.timeout_seconds)
        if not resp.ok:
            raise RuntimeError(f"请求失败 HTTP {resp.status_code}: {resp.text}")
        return self._extract_image_urls_from_sync(resp.json())

    @staticmethod
    def _extract_image_urls_from_sync(body: Dict) -> List[Dict]:
        if body.get("code"):
            raise RuntimeError(f"Qwen 接口返回错误 {body.get('code')}: {body.get('message')}")
        items: List[Dict] = []
        for item in body.get("data") or []:
            image_url = item.get("url") if isinstance(item, dict) else None
            if image_url:
                items.append({"url": image_url})
        return items


# ============================================================================
# 图生图 Provider（qwen-image-edit / qwen-image-edit-plus / qwen-image-edit-max
# / qwen-image-2.0[-pro]）
# ============================================================================


def _guess_mime(image_path: Path) -> str:
    """按文件后缀猜测 image/* MIME 类型，无法识别时回退 image/png。"""
    suffix = image_path.suffix.lower().lstrip(".")
    if suffix:
        mime, _ = mimetypes.guess_type(f"x.{suffix}")
    else:
        mime, _ = mimetypes.guess_type(str(image_path))
    if not mime or not mime.startswith("image/"):
        mime = "image/png"
    return mime


class QwenImageEditProvider:
    """qwen-image* 图生图 Provider。

    与 ``QwenImageTextProvider`` 共用同一个同步多模态 endpoint，只是
    ``messages[0].content`` 里多塞 1-3 张参考图。``size`` / ``prompt_extend``
    会按模型能力自动取舍：

    - ``qwen-image-edit``：固定 n=1，不传 size / prompt_extend。
    - 其他 edit-capable（含 ``qwen-image-2.0[-pro]``）：n=1-6，size /
      prompt_extend 正常生效。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        model: str = "qwen-image-edit-plus",
        size: str = "",
        n: int = 1,
        watermark: bool = False,
        prompt_extend: bool = True,
        negative_prompt: str = "",
        seed: Optional[int] = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_delay: int = DEFAULT_RETRY_DELAY,
        quality_suffix: str = WAN_QUALITY_SUFFIX,
    ):
        if not api_key:
            raise ValueError("API Key 未设置，请填写 GPT_IMAGE_API_KEY")
        if not base_url:
            raise ValueError("BASE_URL 未设置")
        if not is_qwen_edit_capable(model):
            raise ValueError(
                f"模型 {model} 不支持图生图（qwen-image 编辑能力）；"
                "可选 qwen-image-edit / qwen-image-edit-plus / qwen-image-edit-max / "
                "qwen-image-2.0 / qwen-image-2.0-pro"
            )
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.size = (
            normalize_size(size or default_size_for(model), model)
            if supports_edit_size(model)
            else ""
        )
        self.n = normalize_edit_n(n, model)
        self.watermark = watermark
        self.prompt_extend = prompt_extend if supports_edit_prompt_extend(model) else False
        self.negative_prompt = (negative_prompt or "").strip()
        self.seed = seed
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.quality_suffix = (quality_suffix or "").strip()
        logger.info(
            f"Qwen 图生图 Provider 初始化（sync-multimodal），模型: {self.model}, "
            f"尺寸: {self.size or '(自动)'}, n: {self.n}, base: {self.base_url}"
        )

    # ------------------------------------------------------------------
    # 公开 API（与 GPTImageEditProvider 对齐）
    # ------------------------------------------------------------------
    def edit(self, base_image_path: Path, prompt: str) -> List[Dict]:
        return self.edit_multi([base_image_path], prompt)

    def edit_multi(self, base_image_paths: Sequence[Path], prompt: str) -> List[Dict]:
        """1-3 张参考图 + 单条编辑指令。

        与 GPTImageEditProvider 一致：调用方应把"最锁定外观"的图放第一张，
        最后一张决定输出图比例（千问官方约定）。
        """
        if not base_image_paths:
            raise ValueError("edit_multi 至少需要 1 张参考图")
        if len(base_image_paths) > 3:
            logger.warning(
                f"千问图像编辑接口最多支持 3 张参考图，传入 {len(base_image_paths)} 张，超出部分将被截断"
            )
        paths = list(base_image_paths)[:3]

        final_prompt = with_quality_suffix(prompt, self.quality_suffix)
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                logger.info(
                    f"提交 Qwen 图生图任务 (尝试 {attempt + 1}/{self.max_retries}, "
                    f"模型 {self.model}, 参考图 {len(paths)} 张)"
                )
                results = self._call_sync(final_prompt, paths)
                if results:
                    return results
                raise RuntimeError("Qwen 响应中没有图片 URL")
            except Exception as e:
                last_error = e
                logger.error(f"Qwen 图生图请求异常 (尝试 {attempt + 1}): {e}")
            if attempt < self.max_retries - 1:
                time.sleep(self.retry_delay)
        raise RuntimeError(f"Qwen 图生图失败: {last_error}")

    def save_image_item(self, item: Dict, output_path: Path) -> bool:
        """把 ``edit*`` 返回的某一项落盘。仅支持 ``url``（千问 API 不返回 b64）。"""
        return _save_qwen_image_item(item, output_path)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _call_sync(self, prompt: str, image_paths: Sequence[Path]) -> List[Dict]:
        url = f"{self.base_url}/v1/images/edits"
        headers = {"Authorization": f"Bearer {self.api_key}"}

        data: Dict = {
            "model": self.model,
            "prompt": prompt,
            "n": str(self.n),
        }
        if supports_edit_prompt_extend(self.model):
            data["prompt_extend"] = str(self.prompt_extend).lower()
        if self.size and supports_edit_size(self.model):
            data["size"] = self.size
        if self.negative_prompt:
            data["negative_prompt"] = self.negative_prompt
        if self.seed is not None:
            data["seed"] = str(int(self.seed))

        files = []
        for p in image_paths:
            path = Path(p)
            files.append(("image[]", (path.name, path.read_bytes(), _guess_mime(path))))

        resp = requests.post(url, data=data, files=files, headers=headers, timeout=self.timeout_seconds)
        if not resp.ok:
            raise RuntimeError(f"请求失败 HTTP {resp.status_code}: {resp.text}")
        return QwenImageTextProvider._extract_image_urls_from_sync(resp.json())


def _save_qwen_image_item(item: Dict, output_path: Path) -> bool:
    """落盘千问图像接口返回的单项。千问只下发 ``url``，不返回 b64。"""
    try:
        url = item.get("url")
        if not url:
            logger.warning(f"千问图像响应缺少 url: {item}")
            return False
        resp = requests.get(url, stream=True, timeout=300)
        resp.raise_for_status()
        with open(output_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        logger.info(f"图片已保存: {output_path}")
        return True
    except Exception as e:
        logger.error(f"保存图片失败 {output_path}: {e}")
        return False
