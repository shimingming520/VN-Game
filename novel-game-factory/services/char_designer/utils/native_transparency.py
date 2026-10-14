"""Native transparent-output contract for character image assets.

GPT Image 2 can return PNG images with a real alpha channel. Character
generation must request that capability explicitly and validate the returned
pixels before publishing an asset; merely mentioning transparency in the
prompt or observing ``mode == RGBA`` is not sufficient.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import re
from typing import Any, Callable, Dict

from PIL import Image, ImageOps

from common.visual_style import should_remove_character_background


NATIVE_TRANSPARENCY_CONTRACT_VERSION = "gpt-image-2-native-rgba-v3-provider-repair"
NATIVE_TRANSPARENCY_MODE = "native_rgba"
OPAQUE_DEBUG_MODE = "opaque_debug"

TRANSPARENT_OUTPUT_FORMAT = "png"
TRANSPARENT_BACKGROUND = "transparent"

_BACKGROUND_CONFLICT_PATTERNS = (
    re.compile(
        r"the background is completely pure white\s*,?\s*pure white background\s*,?\s*",
        re.IGNORECASE,
    ),
    re.compile(r"(?:on|against|with) a completely pure white background", re.IGNORECASE),
    re.compile(r"(?:on|against|with) a plain pure white background", re.IGNORECASE),
    re.compile(r"(?:on|against|with) a pure white background", re.IGNORECASE),
    re.compile(r"\b(?:plain\s+|completely\s+)?pure white background\b", re.IGNORECASE),
)

_TRANSPARENCY_MARKER = "Final character transparency contract (highest priority"
TRANSPARENT_CHARACTER_PROMPT_SUFFIX = """
Final character transparency contract (highest priority; override every earlier background instruction):
Return exactly one isolated character on a genuinely transparent canvas with a real alpha channel. Keep the complete character visible from head to toe, including both feet, with clear empty transparent margin around the silhouette. Preserve natural semi-transparent edge detail in hair, fabric, veils, smoke, and soft accessories. Do not render a white, colored, gradient, scenic, studio, checkerboard, or other visible backdrop. Do not add a floor, wall, frame, pedestal, environment, background prop, detached glow, or cast shadow outside the character silhouette. Output a PNG whose background pixels are fully transparent, not an opaque image that merely depicts transparency.
""".strip()

NATIVE_TRANSPARENCY_REPAIR_PROMPT = """
Keep exactly the same single character and preserve every visible content detail: identity, face, age, hairstyle, body proportions, anatomy, clothing, accessories, pose, framing, colors, linework, shading, lighting, texture, and intentional semi-transparent hair or fabric edges. Change only the output canvas. Remove the entire visible background and return the isolated character on a genuinely transparent background with a real alpha channel. Do not redesign, restyle, crop, move, duplicate, or add anything. No white or colored backdrop, no scenery, no checkerboard, no floor, no pedestal, and no cast shadow outside the character silhouette.
""".strip()


class CharacterTransparencyError(RuntimeError):
    """The selected character provider cannot satisfy the output contract."""


@dataclass(frozen=True)
class CharacterOutputPolicy:
    mode: str
    image_provider: str
    model: str
    require_transparency: bool
    native_transparency: bool
    output_format: str
    background: str
    contract_version: str = NATIVE_TRANSPARENCY_CONTRACT_VERSION

    def digest_payload(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "provider": self.image_provider,
            "model": self.model,
            "require_transparency": self.require_transparency,
            "native_transparency": self.native_transparency,
            "output_format": self.output_format,
            "background": self.background,
            "contract_version": self.contract_version,
        }


def is_gpt_image_2(model: str) -> bool:
    normalized = str(model or "").strip().lower()
    return normalized == "gpt-image-2" or normalized.startswith("gpt-image-2-")


def resolve_character_output_policy(
    task_id: str,
    *,
    image_provider: str,
    model: str,
    requested_background: str = "",
    requested_output_format: str = "",
) -> CharacterOutputPolicy:
    """Resolve one task's character-output behavior without silent fallback."""
    provider = str(image_provider or "").strip().lower()
    resolved_model = str(model or "").strip()
    # gpt-image-2 is the production character provider. Its public character
    # contract is always native RGBA, even when an old task or client still
    # carries the legacy ``remove_character_background=false`` debug flag.
    # The flag may continue to opt non-GPT providers into opaque local
    # debugging, but it must never weaken production GPT Image output.
    requires_transparency = (
        is_gpt_image_2(resolved_model)
        if provider == "gpt"
        else should_remove_character_background(task_id)
    )

    if not requires_transparency:
        return CharacterOutputPolicy(
            mode=OPAQUE_DEBUG_MODE,
            image_provider=provider,
            model=resolved_model,
            require_transparency=False,
            native_transparency=False,
            output_format=str(requested_output_format or "png").strip().lower(),
            background=str(requested_background or "auto").strip().lower(),
        )

    if provider != "gpt" or not is_gpt_image_2(resolved_model):
        raise CharacterTransparencyError(
            "当前任务要求透明人物资产；移除本地抠图模型后，正式人物立绘只支持 "
            "image_provider=gpt 且 model=gpt-image-2。若要调试其它模型，请显式关闭 "
            "remove_character_background。"
        )

    return CharacterOutputPolicy(
        mode=NATIVE_TRANSPARENCY_MODE,
        image_provider=provider,
        model=resolved_model,
        require_transparency=True,
        native_transparency=True,
        output_format=TRANSPARENT_OUTPUT_FORMAT,
        background=TRANSPARENT_BACKGROUND,
    )


def prepare_transparent_character_prompt(prompt: str) -> str:
    """Remove known opaque-background conflicts and append an idempotent guard."""
    text = str(prompt or "").strip()
    for pattern in _BACKGROUND_CONFLICT_PATTERNS:
        text = pattern.sub("", text)
    text = re.sub(r"\s+([,.;:])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text).strip(" ,")
    if _TRANSPARENCY_MARKER in text:
        return text
    return "\n\n".join(
        part for part in (text, TRANSPARENT_CHARACTER_PROMPT_SUFFIX) if part
    )


def rgba_png_bytes(image_path: str | Path) -> bytes:
    """Normalize an edit reference to PNG while preserving its alpha channel."""
    with Image.open(image_path) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGBA")
        output = BytesIO()
        image.save(output, format="PNG", optimize=False)
        return output.getvalue()


def inspect_native_transparency(path: str | Path) -> Dict[str, Any]:
    """Return deterministic alpha diagnostics for logs and validation."""
    image_path = Path(path)
    with Image.open(image_path) as image:
        image.load()
        image_format = str(image.format or "")
        mode = image.mode
        width, height = image.size
        if mode != "RGBA":
            return {
                "format": image_format,
                "mode": mode,
                "width": width,
                "height": height,
                "alpha_min": None,
                "alpha_max": None,
                "fully_transparent_pixels": 0,
                "fully_transparent_ratio": 0.0,
                "partially_transparent_pixels": 0,
                "corner_alpha": [],
            }
        alpha = image.getchannel("A")
        histogram = alpha.histogram()
        total = max(1, width * height)
        corners = [
            alpha.getpixel((0, 0)),
            alpha.getpixel((width - 1, 0)),
            alpha.getpixel((0, height - 1)),
            alpha.getpixel((width - 1, height - 1)),
        ]
        alpha_min, alpha_max = alpha.getextrema()
        return {
            "format": image_format,
            "mode": mode,
            "width": width,
            "height": height,
            "alpha_min": int(alpha_min),
            "alpha_max": int(alpha_max),
            "fully_transparent_pixels": int(histogram[0]),
            "fully_transparent_ratio": histogram[0] / total,
            "partially_transparent_pixels": int(sum(histogram[1:255])),
            "corner_alpha": [int(value) for value in corners],
        }


def validate_native_transparent_character_file(path: str | Path) -> bool:
    """Require a PNG/RGBA sprite with substantial real transparent canvas."""
    try:
        metrics = inspect_native_transparency(path)
    except (OSError, ValueError, SyntaxError):
        return False
    return bool(
        metrics["format"] == "PNG"
        and metrics["mode"] == "RGBA"
        and metrics["alpha_min"] == 0
        and int(metrics["alpha_max"] or 0) > 0
        and float(metrics["fully_transparent_ratio"]) >= 0.01
        and len(metrics["corner_alpha"]) == 4
        and all(value <= 8 for value in metrics["corner_alpha"])
    )


def validator_for_policy(policy: CharacterOutputPolicy) -> Callable[[str | Path], bool]:
    if policy.require_transparency:
        return validate_native_transparent_character_file
    from services.char_designer.core.artifact_contract import validate_image_file

    return validate_image_file


__all__ = [
    "CharacterOutputPolicy",
    "CharacterTransparencyError",
    "NATIVE_TRANSPARENCY_CONTRACT_VERSION",
    "NATIVE_TRANSPARENCY_MODE",
    "NATIVE_TRANSPARENCY_REPAIR_PROMPT",
    "OPAQUE_DEBUG_MODE",
    "TRANSPARENT_BACKGROUND",
    "TRANSPARENT_CHARACTER_PROMPT_SUFFIX",
    "TRANSPARENT_OUTPUT_FORMAT",
    "inspect_native_transparency",
    "is_gpt_image_2",
    "prepare_transparent_character_prompt",
    "rgba_png_bytes",
    "resolve_character_output_policy",
    "validate_native_transparent_character_file",
    "validator_for_policy",
]
