"""构建感知人物性格的提示词，用于表情与肢体语言的编辑。

本模块有意保持纯粹：不读取数据库、文件或环境变量。批量生成、命令行重绘与产品级
重生成都调用相同的函数，从而保证它们的表情契约不会悄然产生偏差。
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from common.character_personality_contract import (
    has_personality_content,
    normalize_personality,
    select_personality_variant,
)


EMOTIONS = ["happy", "sad", "angry", "fearful", "disgusted", "surprised"]
EMOTION_PROMPT_CONTRACT_VERSION = "personality-expression-body-language-v2"


def _personality_text(personality: Mapping[str, Any]) -> str:
    labels = (
        ("core traits", "main_traits"),
        ("secondary traits", "secondary_traits"),
        ("virtues", "virtues"),
        ("flaws", "flaws"),
    )
    sections = []
    for label, field in labels:
        values = personality.get(field) or []
        if values:
            sections.append(f"{label}: {', '.join(values)}")
    return "; ".join(sections)


def build_emotion_prompt(
    emotion: str,
    personality: Any = None,
    *,
    intensity: Optional[str] = None,
    expression_hint: Optional[str] = None,
) -> str:
    """Build the single provider prompt for one emotion-driven image edit."""
    emotion_name = str(emotion or "").strip().lower()
    if not emotion_name:
        raise ValueError("emotion must be a non-empty string")

    normalised = normalize_personality(personality)
    parts = [
        "Edit the character's expression and, only when it feels natural for the target emotion and personality, "
        "allow one restrained and coherent upper-body gesture; do not redesign the character.",
        f"Target emotion: {emotion_name}.",
    ]

    personality_text = _personality_text(normalised)
    if personality_text:
        parts.append(f"Character personality: {personality_text}.")
        parts.append(
            "Render the target emotion as it would naturally appear on this specific character. "
            "Let the personality control both the facial expression and body language: the intensity, openness, "
            "and restraint of the expression, whether a gesture appears at all, how large it is, and how mature, "
            "lively, timid, or composed it should feel. Prefer personality-consistent micro-expressions and body "
            "language over a generic or exaggerated performance."
        )
    else:
        parts.append(
            "Render the target emotion as a clear but natural expression, using believable micro-expressions "
            "instead of an exaggerated performance. If body language helps communicate the emotion, use at most "
            "one subtle and natural upper-body gesture."
        )

    if intensity:
        parts.append(f"Requested expression intensity: {str(intensity).strip()}.")
    if expression_hint:
        parts.append(f"Additional expression direction: {str(expression_hint).strip()}.")

    parts.extend([
        "Allowed changes: eyebrows, eyes, eyelids, cheeks, mouth, subtle facial muscle tension, a slight head or "
        "neck-angle adjustment, shoulder tension, hand and arm placement, and a small upper-torso weight shift. "
        "Body-language changes are optional; use at most one coherent gesture and keep it secondary to the target "
        "emotion.",
        "Keep unchanged: character identity, age, facial structure, hairstyle, outfit, accessories, weapons, "
        "props, body proportions, hips, legs, feet, ground anchor, full-body scale, overall x/y placement, "
        "the established global visual style, camera, framing, background, and full-body visibility.",
        "Do not add, remove, transfer, multiply, or redesign accessories, weapons, papers, talismans, or other "
        "props. Do not invent or alter readable text, letters, calligraphy, labels, or symbols.",
        "Do not introduce coy, cute, flirtatious, comedic, or theatrical acting unless it is clearly supported by "
        "both the target emotion and the character personality.",
        "Keep the hands anatomically valid; do not create extra fingers, duplicate hands, broken wrists, or extra "
        "limbs.",
        "Keep the entire character visible from head to toe, including the feet; do not crop or zoom.",
        "Do not add text, letters, punctuation marks, exclamation marks, emoji, manga reaction symbols, "
        "comic effects, speech bubbles, or outlines around the character.",
    ])
    return " ".join(parts)


def modify_prompt_for_emotion(
    original_prompt: str,
    emotion: str,
    personality: Any = None,
    *,
    intensity: Optional[str] = None,
    expression_hint: Optional[str] = None,
) -> str:
    """Backward-compatible wrapper used by existing generation entry points.

    The old ``original_prompt`` parameter remains in the public signature, but
    expression edits intentionally consume the structured personality contract
    instead of copying the full calm text-to-image prompt into an edit request.
    """
    del original_prompt
    return build_emotion_prompt(
        emotion,
        personality,
        intensity=intensity,
        expression_hint=expression_hint,
    )
