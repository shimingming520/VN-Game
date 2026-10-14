"""DashScope 千问图像模型共享配置。

千问系列（qwen-image / qwen-image-plus / qwen-image-max / qwen-image-2.0[-pro]）
在百炼平台上与 wan2.7 共享同一个同步多模态接口
``/services/aigc/multimodal-generation/generation``，调用流程一致。

提示词外壳（quality suffix 与拼接函数）直接复用 ``wan_config``，确保 wan 与
qwen 两条链路写进 prompt 末尾的字串完全同源；改一处即可同步生效。

不同点（来自 markdown/千问图像模型技术文档.md）：

- 默认分辨率与可选分辨率按系列拆分：
    * qwen-image-2.0 系列：512*512 ~ 2048*2048，默认 2048*2048。
    * qwen-image-max / plus / v1 系列：默认 1664*928，仅支持几个固定档位。
- ``n`` 参数：2.0 系列允许 1~6 张；max/plus/v1 强制为 1，传别的值会报错。
- 支持 ``negative_prompt`` / ``prompt_extend`` / ``seed`` / ``watermark``。
- 仅 ``qwen-image-plus`` / ``qwen-image`` 支持旧异步任务接口
  ``/services/aigc/text2image/image-synthesis``。当前 provider 只走同步接口，
  足以覆盖所有模型；这里保留 helper 以备后续如果需要切到异步链路。
"""

from __future__ import annotations

# 与 wan_config 同源的质量后缀 / 拼接函数
from .wan_config import WAN_QUALITY_SUFFIX, with_quality_suffix  # noqa: F401

# 千问统一走 GPT_IMAGE 中转站的 OpenAI 兼容接口（/v1/images/*），
# 不再直连 DashScope 官方，避免 DASHSCOPE_API_KEY 被封。
DEFAULT_BASE_URL = "https://ai.farmmx.com"

# 同步多模态接口（与 wan2.7 共享）
SYNC_PATH = "/services/aigc/multimodal-generation/generation"

# 旧异步任务接口（仅 qwen-image-plus / qwen-image 支持）
LEGACY_SUBMIT_PATH = "/services/aigc/text2image/image-synthesis"
LEGACY_TASK_PATH = "/tasks/{task_id}"

# 文档示例里的反向提示词，作为可选默认；provider 默认不传
QWEN_NEGATIVE_PROMPT = (
    "低分辨率，低画质，肢体畸形，手指畸形，画面过饱和，蜡像感，人脸无细节，"
    "过度光滑，画面具有AI感。构图混乱。文字模糊，扭曲。"
)

# 各系列默认尺寸（按官方文档 size 参数）
QWEN_V2_DEFAULT_SIZE = "2048*2048"
QWEN_MAX_PLUS_DEFAULT_SIZE = "1664*928"

# 其它默认参数
DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_DELAY = 5
DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_POLL_MAX_ATTEMPTS = 120


def is_qwen_model(model: str) -> bool:
    """识别千问图像模型（按命名前缀）。"""
    if not model:
        return False
    return model.lower().startswith("qwen-image")


def is_qwen_v2(model: str) -> bool:
    """qwen-image-2.0 / qwen-image-2.0-pro 及对应日期快照。"""
    if not model:
        return False
    return model.lower().startswith("qwen-image-2")


def is_qwen_async_capable(model: str) -> bool:
    """仅 qwen-image-plus / qwen-image 支持旧异步任务接口。"""
    if not model:
        return False
    name = model.lower()
    return name == "qwen-image" or name.startswith("qwen-image-plus")


def is_qwen_edit_only(model: str) -> bool:
    """``qwen-image-edit``：固定输出 1 张，不支持 size / prompt_extend。"""
    if not model:
        return False
    return model.lower() == "qwen-image-edit"


def is_qwen_edit_plus_or_max(model: str) -> bool:
    """``qwen-image-edit-plus`` / ``qwen-image-edit-max``（带日期快照）。

    支持 size / prompt_extend / n=1-6。当前 CG 链路推荐使用这一档。
    """
    if not model:
        return False
    name = model.lower()
    return name.startswith("qwen-image-edit-plus") or name.startswith("qwen-image-edit-max")


def is_qwen_edit_capable(model: str) -> bool:
    """是否支持图生图（图像编辑）接口。

    包括：
    - ``qwen-image-2.0`` / ``qwen-image-2.0-pro``（文生 + 图生统一入口）
    - ``qwen-image-edit`` / ``qwen-image-edit-plus`` / ``qwen-image-edit-max``
    """
    if not model:
        return False
    name = model.lower()
    return (
        is_qwen_v2(model)
        or name.startswith("qwen-image-edit")
    )


def is_qwen_text_capable(model: str) -> bool:
    """是否支持纯文生图（无参考图）。

    ``qwen-image-edit*`` 系列只能图生图，不能纯文生图，需排除。
    """
    if not model:
        return False
    name = model.lower()
    if name.startswith("qwen-image-edit"):
        return False
    return name.startswith("qwen-image")


def supports_edit_size(model: str) -> bool:
    """图生图时是否允许指定 ``size``。``qwen-image-edit`` 例外，其他 edit-capable 都允许。"""
    return is_qwen_edit_capable(model) and not is_qwen_edit_only(model)


def supports_edit_prompt_extend(model: str) -> bool:
    """图生图时是否支持 ``prompt_extend``。仅 ``qwen-image-edit`` 不支持。"""
    return is_qwen_edit_capable(model) and not is_qwen_edit_only(model)


def default_size_for(model: str) -> str:
    """按系列返回官方推荐的默认尺寸。"""
    return QWEN_V2_DEFAULT_SIZE if is_qwen_v2(model) else QWEN_MAX_PLUS_DEFAULT_SIZE


def normalize_size(size: str, model: str = "") -> str:
    """qwen 接口只接受 'W*H'；'WxH' 统一转换。空串回退到模型默认尺寸。"""
    if not size:
        return default_size_for(model)
    return size.strip().replace("x", "*").replace("X", "*")


def normalize_n(n: int, model: str) -> int:
    """qwen-image-2.0 系列支持 1-6；max/plus/v1 强制为 1。"""
    if n is None:
        return 1
    if is_qwen_v2(model):
        return max(1, min(int(n), 6))
    return 1


def normalize_edit_n(n: int, model: str) -> int:
    """图生图：``qwen-image-edit`` 强制 1；其他 edit-capable 支持 1-6。"""
    if n is None:
        return 1
    if is_qwen_edit_only(model):
        return 1
    return max(1, min(int(n), 6))
