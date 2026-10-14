"""DashScope Wan 系列文生图 Provider 共享配置。

把 wan2.5（旧异步任务接口）和 wan2.6+/wan2.7（同步多模态接口）共用的常量
集中到这里，保证两个 provider 拼到 prompt 末尾的质量后缀一致，
也方便统一调整默认 base_url / 尺寸 / 轮询节奏等参数。

任何对这些默认值的修改，wan2.5 与 wan2.7 两条链路同时生效。
"""

from __future__ import annotations

# DashScope 公网网关
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/api/v1"

# 旧异步任务接口（wan2.5 / wanx*）
LEGACY_SUBMIT_PATH = "/services/aigc/text2image/image-synthesis"
LEGACY_TASK_PATH = "/tasks/{task_id}"

# 新同步多模态接口（wan2.6+ / wan2.7）
NEW_SYNC_PATH = "/services/aigc/multimodal-generation/generation"

# Wan 模型靠提示词质量词补足画面细节，没有 GPT Image 那样显式的 quality 等级。
# 由 wan2.5 / wan2.7 两个 provider 共用，确保入参提示词外壳完全一致。
WAN_QUALITY_SUFFIX = (
    "masterpiece, best quality, highres, coherent lighting, "
    "detailed background, coherent composition"
)

# 默认尺寸 / 轮询参数
DEFAULT_SIZE = "1792x1008"
DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_DELAY = 5
DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_POLL_MAX_ATTEMPTS = 120


def is_legacy_wan(model: str) -> bool:
    """wan2.5* / wanx* 走旧的异步任务接口；wan2.6+ / wan2.7 走新的多模态接口。"""
    if not model:
        return False
    name = model.lower()
    return name.startswith("wan2.5") or name.startswith("wanx")


def normalize_size(size: str) -> str:
    """wan 系列既接受 '1K'/'2K'/'4K'，也接受 'W*H'；'WxH' 统一转 'W*H'。"""
    if not size:
        return "2K"
    s = size.strip()
    if s.upper() in {"1K", "2K", "4K"}:
        return s.upper()
    return s.replace("x", "*").replace("X", "*")


def with_quality_suffix(prompt: str, quality_suffix: str = WAN_QUALITY_SUFFIX) -> str:
    """把质量后缀拼到 prompt 末尾；prompt 里已经包含同串则跳过，避免重复。"""
    suffix = (quality_suffix or "").strip()
    if not suffix:
        return prompt
    if suffix in (prompt or ""):
        return prompt
    if not prompt:
        return suffix
    return f"{prompt.rstrip().rstrip(',')}, {suffix}"
