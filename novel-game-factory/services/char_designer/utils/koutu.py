"""角色立绘两阶段抠图模块。

核心流程与同目录“抠图方法/src/two_stage_matting.py”保持一致：

    原图 RGB
      -> BiRefNet HR-matting 生成粗 Alpha
      -> 自动生成 0/128/255 Trimap
      -> ViTMatte 预测精细 Alpha
      -> PyMatting 估计前景颜色
      -> RGBA PNG

本文件将批处理参考实现适配为服务内部的进程级单例，并保留历史接口：

    ensure_koutu_model()
    prepare_koutu_model()
    create_remover()
    remove_background_inplace()

算法参考同目录的“抠图方法”，两套模型从
services/char_designer/models/koutu 离线加载，不会访问 Hugging Face 网络。
"""

from __future__ import annotations

import contextlib
import importlib.util
import logging
import os
import tempfile
from pathlib import Path
from threading import Lock
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 运行环境
# ---------------------------------------------------------------------------
# torch、PyMatting、Numba 和 BLAS 可能加载不同的线程运行时，必须在导入这些
# 依赖前固定线程配置。Numba 缓存放到系统临时目录，避免只读 site-packages
# 环境出现 “no locator available”。
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("VECLIB_MAXIMUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")
os.environ.setdefault("NUMBA_NUM_THREADS", "1")
os.environ.setdefault("NUMBA_THREADING_LAYER", "workqueue")
os.environ.setdefault(
    "NUMBA_CACHE_DIR",
    str(Path(tempfile.gettempdir()) / "novel-game-factory-numba-cache"),
)

try:
    from PIL import Image

    HAS_PIL = True
except ImportError:
    Image = None  # type: ignore[assignment]
    HAS_PIL = False

# ---------------------------------------------------------------------------
# 本地模型和默认参数
# ---------------------------------------------------------------------------
MATTING_METHOD_DIR = Path(__file__).resolve().parent / "抠图方法"
KOUTU_DIR = Path(__file__).resolve().parent.parent / "models" / "koutu"
BIREFNET_MODEL_DIR = KOUTU_DIR / "BiRefNet_HR-matting"
VITMATTE_MODEL_DIR = KOUTU_DIR / "vitmatte-small-composition-1k"

REQUIRED_MODEL_FILES = (
    BIREFNET_MODEL_DIR / "config.json",
    BIREFNET_MODEL_DIR / "BiRefNet_config.py",
    BIREFNET_MODEL_DIR / "birefnet.py",
    BIREFNET_MODEL_DIR / "model.safetensors",
    VITMATTE_MODEL_DIR / "config.json",
    VITMATTE_MODEL_DIR / "preprocessor_config.json",
    VITMATTE_MODEL_DIR / "model.safetensors",
)

REQUIRED_MODULES = (
    "numpy",
    "scipy",
    "torch",
    "torchvision",
    "transformers",
    "huggingface_hub",
    "safetensors",
    "timm",
    "kornia",
    "einops",
    "pymatting",
    "cv2",
    "skimage",
    "accelerate",
)


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


DEVICE = "auto"
COARSE_SIZE = 1024
MAX_SIDE = 2048
MASK_THRESHOLD = 0.50
FOREGROUND_CONFIDENCE = 0.97
BACKGROUND_CONFIDENCE = 0.03
TRIMAP_RADIUS_RATIO = 0.006
FOREGROUND_MODE = "pymatting"

# mode 和 resize 仅为兼容历史 transparent-background 调用签名。
MODE = "birefnet"
RESIZE = "dynamic"

DEFAULT_DEVICE = os.environ.get(
    "KOUTU_REMOVER_DEVICE",
    os.environ.get("KOUTU_DEVICE", DEVICE),
)
DEFAULT_MODE = os.environ.get("KOUTU_REMOVER_MODE", MODE)
DEFAULT_RESIZE = os.environ.get("KOUTU_REMOVER_RESIZE", RESIZE)
DEFAULT_COARSE_SIZE = _env_int("KOUTU_COARSE_SIZE", COARSE_SIZE)
DEFAULT_MAX_SIDE = _env_int("KOUTU_MAX_SIDE", MAX_SIDE)
DEFAULT_MASK_THRESHOLD = _env_float("KOUTU_MASK_THRESHOLD", MASK_THRESHOLD)
DEFAULT_FG_CONFIDENCE = _env_float(
    "KOUTU_FG_CONFIDENCE",
    FOREGROUND_CONFIDENCE,
)
DEFAULT_BG_CONFIDENCE = _env_float(
    "KOUTU_BG_CONFIDENCE",
    BACKGROUND_CONFIDENCE,
)
DEFAULT_TRIMAP_RADIUS_RATIO = _env_float(
    "KOUTU_TRIMAP_RADIUS_RATIO",
    TRIMAP_RADIUS_RATIO,
)
DEFAULT_FOREGROUND_MODE = os.environ.get(
    "KOUTU_FOREGROUND_MODE",
    FOREGROUND_MODE,
)

HAS_REQUIRED_MODULES = all(_module_available(name) for name in REQUIRED_MODULES)
HAS_LOCAL_MODELS = all(path.is_file() for path in REQUIRED_MODEL_FILES)

# 变量名保留给所有历史调用方；现在代表两阶段抠图能力是否具备。
HAS_REMOVER = HAS_PIL and HAS_REQUIRED_MODULES and HAS_LOCAL_MODELS


# ---------------------------------------------------------------------------
# 依赖和模型检查
# ---------------------------------------------------------------------------
def ensure_koutu_model() -> bool:
    """检查两阶段抠图依赖和本地模型文件，不加载模型。"""
    missing_modules = [name for name in REQUIRED_MODULES if not _module_available(name)]
    if not HAS_PIL:
        missing_modules.insert(0, "Pillow")
    if missing_modules:
        logger.warning("两阶段抠图依赖缺失: %s", ", ".join(missing_modules))
        return False

    missing_files = [str(path) for path in REQUIRED_MODEL_FILES if not path.is_file()]
    if missing_files:
        logger.warning("两阶段抠图模型文件缺失: %s", ", ".join(missing_files))
        return False
    return True


# ---------------------------------------------------------------------------
# 两阶段算法基础函数
# 以下实现来自“抠图方法/src/two_stage_matting.py”的服务化适配。
# ---------------------------------------------------------------------------
def resolve_device(requested: str):
    import torch

    if requested != "auto":
        return torch.device(requested)
    if torch.cuda.is_available():
        return torch.device("cuda")
    mps = getattr(torch.backends, "mps", None)
    if mps is not None and mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def first_tensor(value: Any):
    """从不同 Transformers/自定义模型返回结构中递归提取 Tensor。"""
    import torch

    if torch.is_tensor(value):
        return value
    if isinstance(value, dict):
        for key in ("logits", "pred", "prediction", "out"):
            if key in value:
                found = first_tensor(value[key])
                if found is not None:
                    return found
        for item in reversed(list(value.values())):
            found = first_tensor(item)
            if found is not None:
                return found
    if isinstance(value, (list, tuple)):
        for item in reversed(value):
            found = first_tensor(item)
            if found is not None:
                return found
    for name in ("logits", "pred", "prediction"):
        if hasattr(value, name):
            found = first_tensor(getattr(value, name))
            if found is not None:
                return found
    return None


def cuda_autocast(device):
    if device.type != "cuda":
        return contextlib.nullcontext()
    import torch

    return torch.autocast(device_type="cuda", dtype=torch.float16)


def normalize_tensor(image: "Image.Image", device):
    import numpy as np
    import torch

    arr = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0
    mean = np.asarray([0.485, 0.456, 0.406], dtype=np.float32)
    std = np.asarray([0.229, 0.224, 0.225], dtype=np.float32)
    arr = (arr - mean) / std
    return torch.from_numpy(arr.transpose(2, 0, 1)).unsqueeze(0).to(device)


def letterbox_for_birefnet(
    image: "Image.Image",
    size: int,
) -> tuple["Image.Image", tuple[int, int, int, int]]:
    """保持宽高比生成 BiRefNet 方形输入，并返回有效内容框。"""
    import numpy as np

    rgb = image.convert("RGB")
    width, height = rgb.size
    scale = min(size / width, size / height)
    resized_size = (
        max(1, round(width * scale)),
        max(1, round(height * scale)),
    )
    resized = rgb.resize(resized_size, Image.Resampling.LANCZOS)

    arr = np.asarray(rgb, dtype=np.uint8)
    patch = max(1, min(32, height // 2, width // 2))
    corners = np.concatenate(
        [
            arr[:patch, :patch].reshape(-1, 3),
            arr[:patch, -patch:].reshape(-1, 3),
            arr[-patch:, :patch].reshape(-1, 3),
            arr[-patch:, -patch:].reshape(-1, 3),
        ],
        axis=0,
    )
    fill = tuple(int(value) for value in np.median(corners, axis=0))
    left = (size - resized_size[0]) // 2
    top = (size - resized_size[1]) // 2
    canvas = Image.new("RGB", (size, size), fill)
    canvas.paste(resized, (left, top))
    return canvas, (left, top, left + resized_size[0], top + resized_size[1])


class BiRefNetCoarsePredictor:
    """使用包内 BiRefNet HR-matting 权重预测粗 Alpha。"""

    def __init__(self, model_ref: Path, device, size: int) -> None:
        try:
            from transformers import AutoModelForImageSegmentation
        except ImportError as exc:
            raise RuntimeError("BiRefNet 后端需要 transformers") from exc

        logger.info("加载 BiRefNet 粗分割模型: %s", model_ref)
        self.model = AutoModelForImageSegmentation.from_pretrained(
            str(model_ref),
            trust_remote_code=True,
            local_files_only=True,
        )
        if device.type != "cuda":
            self.model = self.model.float()
        self.model.to(device).eval()
        self.device = device
        self.size = int(size)

    def predict(self, image: "Image.Image"):
        import numpy as np
        import torch
        import torch.nn.functional as functional

        original_size = image.size
        square, content_box = letterbox_for_birefnet(image, self.size)
        tensor = normalize_tensor(square, self.device)
        with torch.inference_mode(), cuda_autocast(self.device):
            output = self.model(tensor)
            logits = first_tensor(output)
            if logits is None:
                raise RuntimeError("无法从 BiRefNet 输出中找到 Tensor")
            if logits.ndim == 2:
                logits = logits[None, None]
            elif logits.ndim == 3:
                logits = logits[:, None]
            logits = functional.interpolate(
                logits.float(),
                size=(self.size, self.size),
                mode="bilinear",
                align_corners=False,
            )
            alpha_square = torch.sigmoid(logits)[0, 0].cpu().numpy()

        alpha_image = Image.fromarray(
            np.clip(alpha_square * 255.0, 0, 255).astype(np.uint8),
            mode="L",
        ).crop(content_box)
        alpha_image = alpha_image.resize(original_size, Image.Resampling.LANCZOS)
        return np.asarray(alpha_image, dtype=np.float32) / 255.0


def generate_trimap(
    coarse_alpha,
    radius: int,
    mask_threshold: float,
    foreground_confidence: float,
    background_confidence: float,
):
    """由粗 Alpha 生成取值为 0、128、255 的自动 Trimap。"""
    import numpy as np
    from scipy import ndimage

    alpha = np.clip(coarse_alpha.astype(np.float32), 0.0, 1.0)
    hard_foreground = alpha >= mask_threshold
    radius = max(1, int(radius))

    eroded_foreground = ndimage.binary_erosion(
        hard_foreground,
        iterations=radius,
        border_value=0,
    )
    eroded_background = ndimage.binary_erosion(
        ~hard_foreground,
        iterations=radius,
        border_value=1,
    )
    sure_foreground = eroded_foreground & (alpha >= foreground_confidence)
    sure_background = eroded_background & (alpha <= background_confidence)

    if not np.any(sure_foreground):
        sure_foreground = eroded_foreground
    if not np.any(sure_background):
        sure_background = eroded_background

    trimap = np.full(alpha.shape, 128, dtype=np.uint8)
    trimap[sure_background] = 0
    trimap[sure_foreground] = 255
    return trimap


def resize_for_model(
    image: "Image.Image",
    coarse_alpha,
    max_side: int,
):
    """按长边限制 ViTMatte 工作尺寸，并同步缩放粗 Alpha。"""
    import numpy as np

    if max_side <= 0 or max(image.size) <= max_side:
        return image, coarse_alpha
    scale = max_side / max(image.size)
    size = (
        max(1, round(image.width * scale)),
        max(1, round(image.height * scale)),
    )
    resized_image = image.resize(size, Image.Resampling.LANCZOS)
    alpha_image = Image.fromarray(
        np.clip(coarse_alpha * 255.0, 0, 255).astype(np.uint8),
        mode="L",
    ).resize(size, Image.Resampling.LANCZOS)
    return resized_image, np.asarray(alpha_image, dtype=np.float32) / 255.0


def load_vitmatte(model_ref: Path, device):
    try:
        from transformers import VitMatteForImageMatting, VitMatteImageProcessor
    except ImportError as exc:
        raise RuntimeError("完整两阶段抠图需要支持 ViTMatte 的 transformers") from exc

    logger.info("加载 ViTMatte 精细抠图模型: %s", model_ref)
    processor = VitMatteImageProcessor.from_pretrained(
        str(model_ref),
        local_files_only=True,
    )
    model = VitMatteForImageMatting.from_pretrained(
        str(model_ref),
        local_files_only=True,
    )
    model.to(device).eval()
    return processor, model


def infer_vitmatte(
    processor,
    model,
    image: "Image.Image",
    trimap,
    device,
):
    """运行 ViTMatte，并将 Trimap 的确定区域强制回写为 0/1。"""
    import numpy as np
    import torch

    trimap_image = Image.fromarray(trimap, mode="L")
    inputs = processor(images=image, trimaps=trimap_image, return_tensors="pt")
    inputs = {name: value.to(device) for name, value in inputs.items()}
    with torch.inference_mode(), cuda_autocast(device):
        alpha = model(**inputs).alphas[0, 0].float().cpu().numpy()

    alpha = alpha[: image.height, : image.width]
    alpha = np.clip(alpha, 0.0, 1.0)
    alpha[trimap == 0] = 0.0
    alpha[trimap == 255] = 1.0
    return alpha


def foreground_rgba(
    image: "Image.Image",
    alpha,
    mode: str = DEFAULT_FOREGROUND_MODE,
) -> "Image.Image":
    """使用最终 Alpha 估计前景颜色并合成为 RGBA。"""
    import numpy as np

    rgb = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0
    alpha = np.clip(alpha.astype(np.float32), 0.0, 1.0)
    foreground = rgb
    if mode == "pymatting":
        try:
            from pymatting import estimate_foreground_ml

            foreground = estimate_foreground_ml(rgb, alpha)
        except Exception as exc:
            logger.warning("PyMatting 前景颜色估计失败，回退原始 RGB: %s", exc)
    rgba = np.dstack(
        [
            np.clip(foreground * 255.0, 0, 255).astype(np.uint8),
            np.clip(alpha * 255.0, 0, 255).astype(np.uint8),
        ]
    )
    return Image.fromarray(rgba, mode="RGBA")


class TwoStageMattingRemover:
    """常驻内存的 BiRefNet + ViTMatte 两阶段抠图器。"""

    def __init__(
        self,
        device: str = DEFAULT_DEVICE,
        coarse_size: int = DEFAULT_COARSE_SIZE,
        max_side: int = DEFAULT_MAX_SIDE,
        mask_threshold: float = DEFAULT_MASK_THRESHOLD,
        foreground_confidence: float = DEFAULT_FG_CONFIDENCE,
        background_confidence: float = DEFAULT_BG_CONFIDENCE,
        trimap_radius_ratio: float = DEFAULT_TRIMAP_RADIUS_RATIO,
        foreground_mode: str = DEFAULT_FOREGROUND_MODE,
    ) -> None:
        import torch

        try:
            torch.set_num_threads(1)
        except Exception:
            pass

        self.device = resolve_device(device)
        self.coarse_size = max(1, int(coarse_size))
        self.max_side = int(max_side)
        self.mask_threshold = float(mask_threshold)
        self.foreground_confidence = float(foreground_confidence)
        self.background_confidence = float(background_confidence)
        self.trimap_radius_ratio = float(trimap_radius_ratio)
        self.foreground_mode = foreground_mode
        self._inference_lock = Lock()

        logger.info("两阶段抠图设备: %s", self.device)
        self.coarse_predictor = BiRefNetCoarsePredictor(
            BIREFNET_MODEL_DIR,
            self.device,
            self.coarse_size,
        )
        self.vitmatte_processor, self.vitmatte_model = load_vitmatte(
            VITMATTE_MODEL_DIR,
            self.device,
        )

    def predict_alpha(self, image: "Image.Image"):
        """执行粗 Alpha、Trimap 和 ViTMatte，返回原图尺寸的最终 Alpha。"""
        import numpy as np

        original = image.convert("RGB")
        coarse_original = np.clip(
            self.coarse_predictor.predict(original),
            0.0,
            1.0,
        )
        process_image, process_coarse = resize_for_model(
            original,
            coarse_original,
            self.max_side,
        )
        radius = max(
            2,
            round(max(process_image.size) * self.trimap_radius_ratio),
        )
        trimap_process = generate_trimap(
            process_coarse,
            radius,
            self.mask_threshold,
            self.foreground_confidence,
            self.background_confidence,
        )
        final_process = infer_vitmatte(
            self.vitmatte_processor,
            self.vitmatte_model,
            process_image,
            trimap_process,
            self.device,
        )

        final_image = Image.fromarray(
            np.clip(final_process * 255.0, 0, 255).astype(np.uint8),
            mode="L",
        ).resize(original.size, Image.Resampling.LANCZOS)
        final_alpha = np.asarray(final_image, dtype=np.float32) / 255.0

        trimap_original = np.asarray(
            Image.fromarray(trimap_process, mode="L").resize(
                original.size,
                Image.Resampling.NEAREST,
            ),
            dtype=np.uint8,
        )
        final_alpha[trimap_original == 0] = 0.0
        final_alpha[trimap_original == 255] = 1.0
        logger.debug("两阶段抠图 Trimap 半径: %d", radius)
        return final_alpha

    def process(
        self,
        image,
        type: str = "rgba",
        threshold: Optional[float] = None,
        reverse: bool = False,
    ):
        """提供与旧 Remover.process 相近的调用接口。"""
        import numpy as np

        is_numpy = isinstance(image, np.ndarray)
        if is_numpy:
            original = Image.fromarray(image.astype(np.uint8)).convert("RGB")
        elif isinstance(image, Image.Image):
            original = image.convert("RGB")
        else:
            raise TypeError("image 必须是 PIL.Image 或 numpy.ndarray")

        with self._inference_lock:
            alpha = self.predict_alpha(original)
            if threshold is not None:
                alpha = (alpha > float(threshold)).astype(np.float32)
            if reverse:
                alpha = 1.0 - alpha

            if type == "rgba":
                result = foreground_rgba(original, alpha, self.foreground_mode)
            elif type == "map":
                result = Image.fromarray(
                    np.clip(alpha * 255.0, 0, 255).astype(np.uint8),
                    mode="L",
                ).convert("RGB")
            else:
                raise ValueError("两阶段抠图器仅支持 type='rgba' 或 type='map'")

        if is_numpy:
            return np.asarray(result, dtype=np.uint8)
        return result


# ---------------------------------------------------------------------------
# 模型单例和历史兼容接口
# ---------------------------------------------------------------------------
_remover_singleton: Optional[TwoStageMattingRemover] = None
_remover_lock = Lock()


def create_remover(
    mode: str = DEFAULT_MODE,
    device: str = DEFAULT_DEVICE,
    resize: str = DEFAULT_RESIZE,
    *,
    coarse_size: int = DEFAULT_COARSE_SIZE,
    max_side: int = DEFAULT_MAX_SIDE,
    mask_threshold: float = DEFAULT_MASK_THRESHOLD,
    foreground_confidence: float = DEFAULT_FG_CONFIDENCE,
    background_confidence: float = DEFAULT_BG_CONFIDENCE,
    trimap_radius_ratio: float = DEFAULT_TRIMAP_RADIUS_RATIO,
    foreground_mode: str = DEFAULT_FOREGROUND_MODE,
) -> Optional[TwoStageMattingRemover]:
    """创建或复用进程级两阶段抠图器。

    mode 和 resize 为历史兼容参数，不参与新流程。第一次成功调用确定该进程
    中单例使用的设备和算法参数，后续调用直接复用。
    """
    global _remover_singleton

    del mode, resize
    if not ensure_koutu_model():
        return None
    if _remover_singleton is not None:
        return _remover_singleton

    with _remover_lock:
        if _remover_singleton is not None:
            return _remover_singleton
        try:
            _remover_singleton = TwoStageMattingRemover(
                device=device,
                coarse_size=coarse_size,
                max_side=max_side,
                mask_threshold=mask_threshold,
                foreground_confidence=foreground_confidence,
                background_confidence=background_confidence,
                trimap_radius_ratio=trimap_radius_ratio,
                foreground_mode=foreground_mode,
            )
            logger.info("BiRefNet + ViTMatte 两阶段抠图模型加载完成")
        except Exception as exc:
            logger.exception("加载两阶段抠图模型失败: %s", exc)
            _remover_singleton = None
    return _remover_singleton


def prepare_koutu_model(
    mode: str = DEFAULT_MODE,
    device: str = DEFAULT_DEVICE,
    resize: str = DEFAULT_RESIZE,
) -> bool:
    """在服务启动阶段预加载两套本地模型。"""
    if not ensure_koutu_model():
        return False
    return create_remover(mode=mode, device=device, resize=resize) is not None


def remove_background_inplace(
    image_path,
    remover: Optional[TwoStageMattingRemover] = None,
    *legacy_args,
    **legacy_options,
) -> bool:
    """按方案三移除背景，并以 RGBA PNG 原子覆盖原路径。

    legacy_args 和 legacy_options 只用于兼容旧版后处理参数；新流程不会使用
    alpha_floor、alpha_contrast、fill_bg_tol 等 transparent-background 参数。
    """
    if not HAS_PIL:
        return False
    target = remover or create_remover()
    if target is None:
        return False
    if legacy_args or legacy_options:
        logger.debug("忽略旧版抠图后处理参数")

    path = Path(image_path)
    temporary_path: Optional[Path] = None
    try:
        with Image.open(path) as opened:
            original = opened.convert("RGB")
        result = target.process(original, type="rgba")
        if not isinstance(result, Image.Image):
            result = Image.fromarray(result)
        result = result.convert("RGBA")
        if result.size != original.size:
            raise RuntimeError(
                f"抠图输出尺寸异常: input={original.size}, output={result.size}"
            )

        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.stem}.koutu.",
            suffix=".png",
            dir=str(path.parent),
        )
        os.close(file_descriptor)
        temporary_path = Path(temporary_name)
        result.save(temporary_path, format="PNG")
        os.replace(temporary_path, path)
        temporary_path = None
        return True
    except Exception as exc:
        logger.warning("两阶段抠图失败，保留原图 %s: %s", path, exc)
        return False
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


__all__ = [
    "HAS_REMOVER",
    "KOUTU_DIR",
    "MATTING_METHOD_DIR",
    "BIREFNET_MODEL_DIR",
    "VITMATTE_MODEL_DIR",
    "TwoStageMattingRemover",
    "ensure_koutu_model",
    "prepare_koutu_model",
    "create_remover",
    "remove_background_inplace",
    "resolve_device",
    "letterbox_for_birefnet",
    "generate_trimap",
    "resize_for_model",
    "infer_vitmatte",
    "foreground_rgba",
]
