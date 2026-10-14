"""批量抠图脚本。

输入一个图片目录，自动在该目录下生成 koutu 子文件夹，把该目录内所有图片
抠图后以 RGBA PNG 保存到 koutu 文件夹。

抠图逻辑复用 char_designer/utils/koutu.py 的两阶段抠图器
(BiRefNet HR-matting 生成粗 Alpha -> ViTMatte 精细 Alpha -> PyMatting 前景估计)。

使用方法
    python services/char_designer/useless/batch_koutu.py <图片目录>
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# 让脚本能找到 char_designer/utils 下的 koutu 模块
_current_dir = Path(__file__).resolve().parent
_utils_dir = _current_dir.parent / "utils"
if str(_utils_dir) not in sys.path:
    sys.path.insert(0, str(_utils_dir))

from koutu import HAS_PIL, create_remover, ensure_koutu_model  # noqa: E402

if HAS_PIL:
    from PIL import Image

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("batch_koutu")

# koutu.py 依赖 PIL 打开图片，抠图结果统一输出为 PNG。
SUPPORTED_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def collect_images(input_dir: Path) -> list[Path]:
    """收集目录内（非递归）受支持的图片文件。"""
    files = [
        path
        for path in sorted(input_dir.iterdir())
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
    ]
    return files


def process_directory(input_dir: Path) -> int:
    if not HAS_PIL:
        logger.error("缺少 Pillow，无法读取图片")
        return 1
    if not ensure_koutu_model():
        logger.error("两阶段抠图依赖或本地模型文件缺失，无法运行")
        return 1
    if not input_dir.is_dir():
        logger.error("输入路径不是目录: %s", input_dir)
        return 1

    images = collect_images(input_dir)
    if not images:
        logger.warning("目录下没有可处理的图片: %s", input_dir)
        return 0

    remover = create_remover()
    if remover is None:
        logger.error("加载两阶段抠图模型失败")
        return 1

    output_dir = input_dir / "koutu"
    output_dir.mkdir(exist_ok=True)

    total = len(images)
    success = 0
    for index, image_path in enumerate(images, start=1):
        # koutu 文件夹自身是输出目录，跳过其中的文件，避免二次处理。
        if image_path.parent == output_dir:
            continue
        output_path = output_dir / f"{image_path.stem}.png"
        try:
            with Image.open(image_path) as opened:
                original = opened.convert("RGB")
            result = remover.process(original, type="rgba")
            if not isinstance(result, Image.Image):
                result = Image.fromarray(result)
            result = result.convert("RGBA")
            if result.size != original.size:
                raise RuntimeError(
                    f"抠图输出尺寸异常: input={original.size}, output={result.size}"
                )
            result.save(output_path, format="PNG")
            success += 1
            logger.info("[%d/%d] 抠图完成: %s", index, total, output_path.name)
        except Exception as exc:
            logger.warning("[%d/%d] 抠图失败，跳过 %s: %s", index, total, image_path.name, exc)

    logger.info("全部完成: 成功 %d / 共 %d，输出目录: %s", success, total, output_dir)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="批量抠图：输入目录，输出到该目录下的 koutu 文件夹")
    parser.add_argument("input_dir", help="待抠图的图片所在目录")
    args = parser.parse_args()
    return process_directory(Path(args.input_dir).expanduser().resolve())


if __name__ == "__main__":
    raise SystemExit(main())
