"""图生图（img2img）输入预处理：把带透明度的图片用白色背景合成拉平。

在把图片喂给图像生成 provider 之前，将 RGBA 图片的透明/半透明区域
与白色背景做 alpha 合成，输出不带 alpha 通道的不透明 RGB PNG，避免
provider 拿到透明通道后产生异常结果。
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Tuple

from PIL import Image, ImageOps


WHITE: Tuple[int, int, int] = (255, 255, 255)


def opaque_rgb_png_bytes(
    image_path: str | Path,
    background: Tuple[int, int, int] = WHITE,
) -> bytes:
    """Return a lossless RGB PNG, compositing every alpha level over ``background``.

    The conversion stays in memory and preserves the original pixel dimensions.  A
    full alpha composite is required here: merely dropping the alpha channel would
    expose hidden RGB values and turn semi-transparent hair edges fully opaque.
    """

    with Image.open(image_path) as opened:
        image = ImageOps.exif_transpose(opened)
        rgba = image.convert("RGBA")

    canvas = Image.new("RGBA", rgba.size, (*background, 255))
    rgb = Image.alpha_composite(canvas, rgba).convert("RGB")
    output = BytesIO()
    rgb.save(output, format="PNG", optimize=False)
    return output.getvalue()


__all__ = ["opaque_rgb_png_bytes"]
