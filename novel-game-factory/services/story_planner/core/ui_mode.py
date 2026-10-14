"""Infer the public story-tree UI mode from original-novel signals."""

from __future__ import annotations

from typing import Any


ANCIENT = "Ancient"
OTHER = "Other"

_ANCIENT_MARKERS = (
    "ancient",
    "古风",
    "古代",
    "仙侠",
    "修仙",
    "修真",
    "武侠",
    "宫廷",
    "朝堂",
    "架空历史",
    "历史权谋",
    "玄幻",
    "xianxia",
    "wuxia",
)
_OTHER_MARKERS = (
    "other",
    "现代",
    "都市",
    "校园",
    "科幻",
    "赛博",
    "末世",
    "现实",
    "悬疑",
    "推理",
    "modern",
    "urban",
    "sci-fi",
    "science fiction",
)

_ANCIENT_TEXT_SIGNALS = {
    "陛下": 3,
    "皇帝": 3,
    "太子": 3,
    "王爷": 3,
    "丞相": 3,
    "朝廷": 3,
    "御书房": 3,
    "臣妾": 3,
    "微臣": 3,
    "本宫": 3,
    "侯府": 3,
    "侍卫": 2,
    "将军": 2,
    "公主": 2,
    "江湖": 2,
    "门派": 2,
    "宗门": 3,
    "仙门": 3,
    "师尊": 3,
    "道友": 3,
    "灵力": 3,
    "修为": 3,
    "筑基": 3,
    "金丹": 3,
    "元婴": 3,
    "御剑": 3,
    "飞升": 3,
}
_OTHER_TEXT_SIGNALS = {
    "手机": 3,
    "微信": 3,
    "电脑": 3,
    "网络": 2,
    "公司": 2,
    "办公室": 2,
    "学校": 2,
    "大学": 2,
    "医院": 2,
    "警察": 2,
    "汽车": 2,
    "地铁": 3,
    "电梯": 2,
    "银行卡": 3,
    "身份证": 3,
}


def normalize_ui_mode(value: Any) -> str | None:
    """Map existing/legacy labels to the only two public values."""
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    if normalized == ANCIENT.lower() or any(
        marker in normalized for marker in _ANCIENT_MARKERS
    ):
        return ANCIENT
    if normalized == OTHER.lower() or any(
        marker in normalized for marker in _OTHER_MARKERS
    ):
        return OTHER
    return None


def _metadata_text(metadata: Any) -> str:
    if metadata is None:
        return ""
    if isinstance(metadata, dict):
        preferred_keys = (
            "UI_mode",
            "ui_mode",
            "genre",
            "art_style",
            "era",
            "setting",
            "time_period",
            "background",
        )
        return " ".join(
            _metadata_text(metadata.get(key))
            for key in preferred_keys
            if metadata.get(key) is not None
        )
    if isinstance(metadata, (list, tuple, set)):
        return " ".join(_metadata_text(item) for item in metadata)
    return str(metadata)


def _signal_score(text: str, signals: dict[str, int]) -> int:
    source = str(text or "")
    return sum(
        min(source.count(marker), 8) * weight
        for marker, weight in signals.items()
    )


def infer_ui_mode(
    *,
    text: str = "",
    metadata: Any = None,
    existing: Any = None,
) -> str:
    """Return ``Ancient`` for ancient-style works, otherwise ``Other``.

    Existing values are normalized first.  LLM-derived genre/setting metadata is
    preferred because it was inferred from the complete original novel; original
    mode falls back to deterministic signals in the source text.
    """
    existing_mode = normalize_ui_mode(existing)
    if existing_mode:
        return existing_mode

    metadata_mode = normalize_ui_mode(_metadata_text(metadata))
    if metadata_mode:
        return metadata_mode

    ancient_score = _signal_score(text, _ANCIENT_TEXT_SIGNALS)
    other_score = _signal_score(text, _OTHER_TEXT_SIGNALS)
    if ancient_score >= 6 and ancient_score >= other_score + 2:
        return ANCIENT
    return OTHER
