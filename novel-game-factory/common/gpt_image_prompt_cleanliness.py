"""Request-level prompt profiles for GPT Image 2 texture artifacts.

GPT Image does not expose a separate ``negative_prompt`` field, so these
constraints must be part of the positive prompt actually sent to the provider.
Keeping the logic at the provider boundary ensures that characters, style
anchors, scenes, covers, and CG images receive the intended protection without
mutating process-wide environment variables between concurrent requests.
"""

from __future__ import annotations

import os


_FALSE_VALUES = {"0", "false", "no", "off"}
PROFILE_OFF = "off"
PROFILE_ANCHOR_CLEANUP = "anchor_cleanup"
PROFILE_BALANCED = "balanced"
VALID_PROMPT_PROFILES = {
    PROFILE_OFF,
    PROFILE_ANCHOR_CLEANUP,
    PROFILE_BALANCED,
}

_CLEANLINESS_MARKER = "Final rendering cleanliness constraints (highest priority"
_REFERENCE_MARKER = "Reference-image cleanliness constraint"
_ANCHOR_CLEANUP_MARKER = "Anchor cleanup constraints (highest priority"


GPT_IMAGE_CLEAN_RENDERING_SUFFIX = """
Final rendering cleanliness constraints (highest priority; override any conflicting earlier texture or detail wording):
Render clean, smooth, low-noise surfaces with broad coherent color fields, smooth gradients, clean edges, and restrained selective detail. Preserve all intentional identity, facial features, anatomy, clothing, props, architecture, composition, story information, and designed material differences. Express materials mainly through large-form color, lighting, and shape rather than dense repeated microtexture. Use less detail only in nonessential surface microtexture and tiny background clutter. Flat and softly shaded areas must remain uniform and visually calm. Do not add digital noise, film grain, paper grain, speckling, stippling, dithering, halftone dots, moire, diagonal grid artifacts, interference patterns, checkerboard patterns, tiled or repetitive micro-patterns, unintended fish-scale-like or cellular textures, shimmering high-frequency detail, compression artifacts, ringing, sharpening halos, or oversharpening.
""".strip()


GPT_IMAGE_REFERENCE_CLEANLINESS_SUFFIX = """
Reference-image cleanliness constraint: Re-render all surfaces cleanly from scratch. Use the reference images only for the identity, content, layout, composition, and overall art direction explicitly requested above. Do not copy, preserve, propagate, or amplify any noise, grain, speckling, moire, tiling, diagonal grid, repetitive micro-pattern, unintended fish-scale-like or cellular texture, sharpening halo, or compression artifact present in any reference image.
""".strip()


GPT_IMAGE_ANCHOR_CLEANUP_SUFFIX = """
Anchor cleanup constraints (highest priority for this refinement pass):
Faithfully re-render the attached raw Anchor as the same composition and visual design. Preserve identity, silhouette, proportions, pose or spatial layout, palette, clothing, props, architecture, story information, and designed material differences. Do not add or remove subjects or meaningful design elements.

Final rendering cleanliness constraints (highest priority; override any conflicting earlier texture or detail wording):
Render clean, smooth, low-noise surfaces with broad coherent color fields, smooth gradients, clean edges, and restrained selective detail. Preserve all intentional identity, facial features, anatomy, clothing, props, architecture, composition, story information, and designed material differences. Express materials mainly through large-form color, lighting, and shape rather than dense repeated microtexture. Use less detail only in nonessential surface microtexture and tiny background clutter. Flat and softly shaded areas must remain uniform and visually calm. Do not add digital noise, film grain, paper grain, speckling, stippling, dithering, halftone dots, moire, diagonal grid artifacts, interference patterns, checkerboard patterns, tiled or repetitive micro-patterns, unintended fish-scale-like or cellular textures, shimmering high-frequency detail, compression artifacts, ringing, sharpening halos, or oversharpening.

Reference-image cleanliness constraint: Re-render all surfaces cleanly from scratch. Use the attached raw Anchor only for the identity, content, layout, composition, and overall art direction explicitly requested above. Do not copy, preserve, propagate, or amplify any noise, grain, speckling, moire, tiling, diagonal grid, repetitive micro-pattern, unintended fish-scale-like or cellular texture, sharpening halo, or compression artifact present in it.
""".strip()


def gpt_image_artifact_suppression_enabled() -> bool:
    """Return whether the experimental GPT Image 2 prompt suffix is enabled."""
    value = os.getenv("GPT_IMAGE_ARTIFACT_SUPPRESSION_ENABLED", "1")
    return value.strip().lower() not in _FALSE_VALUES


def gpt_image_two_pass_anchor_enabled() -> bool:
    """Return whether automatic GPT Image Anchors use raw + cleanup passes."""
    value = os.getenv("GPT_IMAGE_TWO_PASS_ANCHOR_ENABLED", "0")
    return value.strip().lower() not in _FALSE_VALUES


def gpt_image_keep_raw_anchor_enabled() -> bool:
    """Return whether validated raw Anchors are retained for local review."""
    value = os.getenv("GPT_IMAGE_KEEP_RAW_ANCHOR", "0")
    return value.strip().lower() not in _FALSE_VALUES


def normalize_gpt_image_prompt_profile(profile: str) -> str:
    normalized = str(profile or PROFILE_BALANCED).strip().lower()
    if normalized not in VALID_PROMPT_PROFILES:
        raise ValueError(
            "GPT Image prompt profile 只支持: "
            + ", ".join(sorted(VALID_PROMPT_PROFILES))
        )
    return normalized


def prepare_gpt_image_prompt(
    prompt: str,
    *,
    model: str,
    has_reference_images: bool = False,
    prompt_profile: str = PROFILE_BALANCED,
) -> str:
    """Append the selected idempotent constraints for GPT Image 2."""
    text = str(prompt or "").strip()
    profile = normalize_gpt_image_prompt_profile(prompt_profile)
    if not gpt_image_artifact_suppression_enabled():
        return text
    if not str(model or "").strip().lower().startswith("gpt-image-2"):
        return text
    if profile == PROFILE_OFF:
        return text

    if profile == PROFILE_ANCHOR_CLEANUP:
        if _ANCHOR_CLEANUP_MARKER in text:
            return text
        return "\n\n".join(
            part for part in (text, GPT_IMAGE_ANCHOR_CLEANUP_SUFFIX) if part
        )

    parts = [text] if text else []
    if _CLEANLINESS_MARKER not in text:
        parts.append(GPT_IMAGE_CLEAN_RENDERING_SUFFIX)
    combined = "\n\n".join(parts)

    if has_reference_images and _REFERENCE_MARKER not in combined:
        combined = "\n\n".join(
            [combined, GPT_IMAGE_REFERENCE_CLEANLINESS_SUFFIX]
        )
    return combined
