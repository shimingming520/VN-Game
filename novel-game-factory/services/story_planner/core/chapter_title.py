"""Normalize player-facing chapter titles.

Chapter numbers are structural metadata in the story tree (``depth``), so
they should not be duplicated inside the title shown to players.  This module
strips only a leading numeric chapter/volume marker and its separator; semantic
labels such as ``What-If:`` and ``Ending:`` are preserved.
"""

from __future__ import annotations

import re
from typing import Any, Optional


_CHINESE_NUMERALS = "0-9０-９零〇一二三四五六七八九十百千万两"
_CHINESE_PREFIX_RE = re.compile(
    rf"^第\s*[{_CHINESE_NUMERALS}]+\s*[章节回卷]\s*"
)
_ENGLISH_PREFIX_RE = re.compile(r"^(?:Chapter|CHAPTER)\s*[0-9０-９]+\s*")
_NO_PREFIX_RE = re.compile(
    r"^(?:No\.?|NO\.?|Ｎｏ．?|ＮＯ．?)\s*[0-9０-９]+\s*"
)
_SEPARATOR_RE = re.compile(r"^[：:、.．·•\-—–]+\s*")


def untitled_chapter_title(language: Optional[str] = None) -> str:
    """Return a language-appropriate fallback without a numeric prefix."""

    language_prefix = str(language or "").lower().split("-", 1)[0]
    if language_prefix == "en":
        return "Untitled Chapter"
    if language_prefix == "ja":
        return "章タイトル未設定"
    return "未命名章节"


def normalize_chapter_title(
    value: Any,
    *,
    language: Optional[str] = None,
) -> str:
    """Remove a leading chapter number from a player-facing title.

    ``language`` is optional and only controls the fallback for a bare numeric
    heading.  The input language is otherwise inferred from its prefix.
    Parentheses around a What-If title are presentation noise and are removed,
    while the ``What-If:`` label itself remains available to the player.
    """

    text = str(value or "").strip()
    if not text:
        return ""

    matched_kind = ""
    for kind, pattern in (
        ("zh", _CHINESE_PREFIX_RE),
        ("en", _ENGLISH_PREFIX_RE),
        ("no", _NO_PREFIX_RE),
    ):
        match = pattern.match(text)
        if match:
            text = text[match.end() :]
            matched_kind = kind
            break

    if not matched_kind:
        return text

    text = _SEPARATOR_RE.sub("", text.lstrip())
    if len(text) >= 2 and (
        (text.startswith("（") and text.endswith("）"))
        or (text.startswith("(") and text.endswith(")"))
    ):
        text = text[1:-1].strip()

    if text:
        return text

    language_prefix = str(language or "").lower().split("-", 1)[0]
    return untitled_chapter_title("en-US" if matched_kind == "en" else language)
