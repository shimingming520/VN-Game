"""Normalize player-visible ``story_tree_json`` text inside story_planner.

Only presentation fields are localized. Node IDs, variable keys, route
thresholds, terminal states, and every other internal field remain untouched.
"""

from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from typing import Any, Callable, Dict, Iterable, Optional
from services.story_planner.core.chapter_title import (
    normalize_chapter_title,
    untitled_chapter_title,
)


Translator = Callable[[Dict[str, Any]], Dict[str, Any]]

DEFAULT_STORY_TREE_LANGUAGES = ("en-US", "ja-JP")
_LANGUAGE_TAG_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")

_CJK_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff]"
)
_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_WHITESPACE_RE = re.compile(r"\s+")


def get_story_tree_player_language() -> str:
    """Return the language used by player-visible story-tree metadata.

    This is intentionally independent from ``novel_tasks.output_language``. The
    latter controls planner prose generation, while this value selects the
    default locale used by downstream packaged-game navigation.
    """
    return (
        os.getenv("STORY_PLANNER_STORY_TREE_PLAYER_LANGUAGE", "en-US").strip()
        or "en-US"
    )


def _normalize_language_tag(value: Any) -> str:
    """Normalize a locale tag while retaining an extensible BCP-47-like shape."""
    raw = str(value or "").strip().replace("_", "-")
    if not raw:
        return ""
    aliases = {
        "en": "en-US",
        "ja": "ja-JP",
        "jp": "ja-JP",
        "zh": "zh-CN",
        "ko": "ko-KR",
        "ru": "ru-RU",
    }
    if raw.lower() in aliases:
        return aliases[raw.lower()]
    if not _LANGUAGE_TAG_RE.fullmatch(raw):
        return ""
    parts = raw.split("-")
    return "-".join(
        [parts[0].lower(), *[part.upper() if len(part) in (2, 3) else part for part in parts[1:]]]
    )


def get_story_tree_player_languages(
    selected_languages: Optional[Iterable[str]] = None,
) -> list[str]:
    """Return locales to materialize in the multilingual story-tree bundle."""
    configured = os.getenv("STORY_PLANNER_STORY_TREE_LANGUAGES", "")
    raw_values = (
        configured.split(",")
        if configured.strip()
        else list(DEFAULT_STORY_TREE_LANGUAGES)
    )
    raw_values.append(get_story_tree_player_language())
    if selected_languages:
        raw_values.extend(selected_languages)

    languages: list[str] = []
    for value in raw_values:
        normalized = _normalize_language_tag(value)
        if normalized and normalized not in languages:
            languages.append(normalized)
    return languages or list(DEFAULT_STORY_TREE_LANGUAGES)


def _language_prefix(language: Optional[str]) -> str:
    return str(language or "").strip().lower().split("-", 1)[0]


def _is_english_language(language: Optional[str]) -> bool:
    return _language_prefix(language) == "en"


def _language_name(language: Optional[str]) -> str:
    return {
        "en": "English",
        "ja": "Japanese",
        "ko": "Korean",
        "ru": "Russian",
        "zh": "Simplified Chinese",
    }.get(_language_prefix(language), str(language or "the target language"))


def _contains_cjk(value: Any) -> bool:
    return bool(_CJK_RE.search(str(value or "")))


def _contains_kana(value: Any) -> bool:
    return bool(_KANA_RE.search(str(value or "")))


def _existing_text_for_language(
    value: Any,
    *,
    language: Optional[str],
    max_chars: int,
) -> str:
    candidate = _compact_localized(value, max_chars=max_chars, language=language)
    # Avoid exposing Chinese source text as a Japanese player-facing fallback.
    if (
        _language_prefix(language) == "ja"
        and _contains_cjk(candidate)
        and not _contains_kana(candidate)
    ):
        return ""
    return candidate


def _strip_node_id(value: Any, node_id: str) -> str:
    text = str(value or "").strip()
    suffix = str(node_id or "").strip()
    if suffix and text.lower().endswith(suffix.lower()):
        text = text[: -len(suffix)].rstrip(" -:：")
    if suffix:
        text = re.sub(
            rf"(?<![A-Za-z0-9_]){re.escape(suffix)}(?![A-Za-z0-9_])",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip(" -:：")
        text = _WHITESPACE_RE.sub(" ", text).strip()
    return text


def _compact_localized(
    value: Any,
    *,
    max_chars: int,
    language: Optional[str],
) -> str:
    text = _WHITESPACE_RE.sub(" ", str(value or "")).strip().strip('"“”')
    if not text:
        return ""
    # English must not leak source CJK text. Other locales are allowed to use
    # their native scripts (Japanese includes kanji and kana).
    if _is_english_language(language) and _contains_cjk(text):
        return ""
    if len(text) <= max_chars:
        return text
    shortened = text[: max_chars + 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return shortened or text[:max_chars].rstrip(" ,;:-")


def _compact_english(value: Any, *, max_chars: int) -> str:
    return _compact_localized(value, max_chars=max_chars, language="en-US")


def _safe_node_title(
    node_id: str,
    node: dict,
    translated: Any,
    *,
    language: Optional[str] = "en-US",
) -> str:
    title = normalize_chapter_title(
        _compact_localized(
            _strip_node_id(translated, node_id),
            max_chars=80,
            language=language,
        ),
        language=language,
    )
    if not title:
        existing = normalize_chapter_title(
            _existing_text_for_language(
                _strip_node_id(node.get("title"), node_id),
                max_chars=80,
                language=language,
            ),
            language=language,
        )
        title = existing
    if not title:
        if _language_prefix(language) == "ja":
            title = "結末" if not (node.get("children") or []) else untitled_chapter_title(language)
        else:
            title = "Ending" if not (node.get("children") or []) else untitled_chapter_title(language)
    return title


def _safe_node_summary(
    node_id: str,
    node: dict,
    translated: Any,
    *,
    language: Optional[str] = "en-US",
) -> str:
    """Return a player-facing chapter summary without leaking internal IDs."""
    summary = _compact_localized(
        _strip_node_id(translated, node_id),
        max_chars=450,
        language=language,
    )
    if summary:
        return summary
    summary = _existing_text_for_language(
        _strip_node_id(node.get("summary"), node_id),
        max_chars=450,
        language=language,
    )
    if summary:
        return summary
    depth = int(node.get("depth") or 1)
    return (
        f"第{depth}章のあらすじ"
        if _language_prefix(language) == "ja"
        else f"Chapter {depth} summary"
    )


def _safe_book_title(
    existing: Any,
    translated: Any,
    *,
    language: Optional[str] = "en-US",
) -> str:
    return (
        _compact_localized(translated, max_chars=90, language=language)
        or _existing_text_for_language(existing, max_chars=90, language=language)
        or ("ビジュアルノベル" if _language_prefix(language) == "ja" else "Visual Novel")
    )


def _safe_book_summary(
    existing: Any,
    translated: Any,
    *,
    language: Optional[str] = "en-US",
) -> str:
    return (
        _compact_localized(translated, max_chars=450, language=language)
        or _existing_text_for_language(existing, max_chars=450, language=language)
        or (
            "謎と選択と結果をめぐるインタラクティブな物語。"
            if _language_prefix(language) == "ja"
            else "An interactive story of mystery, choice, and consequence."
        )
    )


def _safe_choice_text(
    existing: Any,
    translated: Any,
    *,
    fallback_index: int,
    is_terminal_target: bool = False,
    language: Optional[str] = "en-US",
) -> str:
    if is_terminal_target or not str(existing or "").strip():
        # Empty ending choices are automatic routes and must remain empty.
        return ""
    if _language_prefix(language) == "ja":
        fallback = "続行" if fallback_index <= 0 else f"経路{fallback_index + 1}を選ぶ"
    else:
        fallback = "Continue" if fallback_index <= 0 else f"Choose Path {fallback_index + 1}"
    return (
        _compact_localized(translated, max_chars=48, language=language)
        or _existing_text_for_language(existing, max_chars=48, language=language)
        or fallback
    )


def _localize_content_warning_fields(
    story_tree: dict,
    *,
    player_language: str,
) -> None:
    """Localize the structured work-level warning without using free-form LLM text.

    Content-warning category codes are a cross-locale contract.  They are copied
    unchanged while the short player-facing labels are produced by the reviewed
    locale dictionary in ``content_warnings``.  Historical trees without the
    field remain untouched.
    """

    if "content_warnings" not in story_tree:
        return
    try:
        from services.story_planner.core.content_warnings import (
            localize_content_warnings,
        )

        story_tree["content_warnings"] = localize_content_warnings(
            story_tree.get("content_warnings"),
            player_language,
        )
    except Exception as exc:
        # Warning metadata must never make a long-running publication fail.  Keep
        # the canonical copy if a future locale dictionary is unavailable.
        print(f"⚠️  content_warnings 本地化失败，保留原始提示字段：{exc}")


def _dedupe_choice_text(
    value: str,
    *,
    seen: set[str],
    fallback_index: int,
    language: Optional[str] = "en-US",
) -> str:
    """Keep sibling player choices distinct without touching automatic routes."""
    text = str(value or "").strip()
    if not text:
        return ""
    normalized = text.casefold()
    if normalized not in seen:
        seen.add(normalized)
        return text

    sequence = max(fallback_index + 1, 2)
    candidate = (
        f"経路{sequence}を選ぶ"
        if _language_prefix(language) == "ja"
        else f"Choose Path {sequence}"
    )
    while candidate.casefold() in seen:
        sequence += 1
        candidate = (
            f"経路{sequence}を選ぶ"
            if _language_prefix(language) == "ja"
            else f"Choose Path {sequence}"
        )
    seen.add(candidate.casefold())
    return candidate


def build_player_localization_payload(
    story_tree: dict,
    *,
    source_text: str = "",
) -> dict:
    """Build a compact, ID-addressable payload for one localization request."""
    nodes = story_tree.get("nodes") or {}
    payload_nodes = []
    payload_choices = []

    if isinstance(nodes, dict):
        for node_id, node in nodes.items():
            if not isinstance(node, dict):
                continue
            payload_nodes.append(
                {
                    "node_id": node_id,
                    "depth": node.get("depth"),
                    "title": _strip_node_id(node.get("title"), node_id),
                    "summary": str(node.get("summary") or "")[:500],
                    "is_terminal": not bool(node.get("children") or []),
                }
            )
            for index, choice in enumerate(node.get("children") or []):
                if not isinstance(choice, dict) or not str(
                    choice.get("choice_text") or ""
                ).strip():
                    continue
                child_id = str(choice.get("child_id") or "")
                target = nodes.get(child_id) if isinstance(nodes.get(child_id), dict) else {}
                payload_choices.append(
                    {
                        "key": f"{node_id}:{index}",
                        "parent_id": node_id,
                        "child_id": child_id,
                        "choice_text": choice.get("choice_text", ""),
                        "target_title": _strip_node_id(
                            target.get("title", ""), child_id
                        ),
                    }
                )

    return {
        "book_title": story_tree.get("book_title", ""),
        "book_summary": story_tree.get("book_summary", ""),
        "source_excerpt": str(source_text or "")[:8000],
        "nodes": payload_nodes,
        "choices": payload_choices,
    }


def _llm_translate(
    payload: dict,
    *,
    provider: Optional[str],
    target_language: str = "en-US",
) -> dict:
    # Lazy import keeps the normalization helpers independently testable and avoids
    # creating an LLM client for non-English jobs.
    from services.story_planner.core.llm_client import LLMClient

    language_name = _language_name(target_language)
    script_instruction = (
        "Never output Chinese, Japanese, Korean, or Cyrillic characters. "
        "Preserve proper-name identity through consistent romanization."
        if _is_english_language(target_language)
        else f"Write polished natural {language_name}. Do not mix in an unrelated language."
    )
    system_prompt = (
        f"You localize player-visible metadata for a visual novel into {language_name}. "
        "Return one JSON object only. "
        f"{script_instruction} "
        "Do not translate IDs, variable keys, or routing metadata. "
        "Required schema: {book_title: string, book_summary: string, "
        "node_titles: {node_id: title}, node_summaries: {node_id: summary}, "
        "choice_texts: {parent_id:index: text}}. "
        "book_title must be concise; book_summary must be 120-450 characters; "
        "Translate book_title faithfully from the supplied title and do not invent a different title; "
        "node titles must be concise themes without node IDs; node summaries must "
        "be faithful, concise player-facing chapter descriptions without node IDs; "
        "choice texts must be "
        "specific player actions, preferably 2-8 words and at most 48 characters. "
        "When an input choice is generic (for example Continue/继续), derive a "
        "specific action from target_title. Return every supplied node and choice key."
    )
    client = LLMClient(provider=provider, temperature=0.2, max_tokens=8000)
    result = client.chat_completion_json(
        system_prompt=system_prompt,
        user_prompt=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        temperature=0.2,
        max_tokens=8000,
    )
    return result if isinstance(result, dict) else {}


def localize_story_tree_player_fields(
    story_tree: dict,
    *,
    player_language: Optional[str],
    source_text: str = "",
    provider: Optional[str] = None,
    translator: Optional[Translator] = None,
) -> dict:
    """Return a copy with only player-visible fields localized to one language.

    Localization failure is non-fatal: deterministic language-aware fallbacks
    prevent a long-running planner task from failing during metadata publication.
    """
    localized = deepcopy(story_tree or {})
    # ``publication`` is attached by the transactional publisher at the bundle
    # root. It is fencing metadata, never a locale-owned player field.
    localized.pop("publication", None)
    player_language = _normalize_language_tag(player_language) or str(
        player_language or "en-US"
    )
    source_language = str(localized.get("book_language") or "").strip()
    if (
        _language_prefix(player_language) == "zh"
        and _language_prefix(source_language) == "zh"
    ):
        if not str(localized.get("book_title") or "").strip():
            localized["book_title"] = _safe_book_title(
                localized.get("book_title"), None, language=player_language
            )
        if not str(localized.get("book_summary") or "").strip():
            localized["book_summary"] = _safe_book_summary(
                localized.get("book_summary")
                or localized.get("summary")
                or localized.get("one_liner")
                or localized.get("narrative_arc"),
                None,
                language=player_language,
            )
        _localize_content_warning_fields(
            localized,
            player_language=player_language,
        )
        return localized

    payload = build_player_localization_payload(
        localized,
        source_text=source_text,
    )
    response: dict = {}
    try:
        response = (
            translator(payload)
            if translator is not None
            else _llm_translate(
                payload,
                provider=provider,
                target_language=player_language,
            )
        )
        if not isinstance(response, dict):
            response = {}
    except Exception as exc:
        print(f"⚠️  story_tree 玩家可见字段英文归一化失败，使用安全英文兜底：{exc}")
        response = {}

    if source_language and source_language != player_language:
        localized.setdefault("source_language", source_language)
    localized["player_language"] = player_language
    localized["book_title"] = _safe_book_title(
        localized.get("book_title"),
        response.get("book_title"),
        language=player_language,
    )
    localized["book_summary"] = _safe_book_summary(
        localized.get("book_summary")
        or localized.get("summary")
        or localized.get("one_liner")
        or localized.get("narrative_arc"),
        response.get("book_summary"),
        language=player_language,
    )
    _localize_content_warning_fields(
        localized,
        player_language=player_language,
    )

    translated_titles = response.get("node_titles")
    if not isinstance(translated_titles, dict):
        translated_titles = {}
    translated_summaries = response.get("node_summaries")
    if not isinstance(translated_summaries, dict):
        translated_summaries = {}
    translated_choices = response.get("choice_texts")
    if not isinstance(translated_choices, dict):
        translated_choices = {}

    nodes = localized.get("nodes") or {}
    ending_route_ids = {
        str(node_id)
        for node_id in (localized.get("branch_thresholds") or {})
        if str(node_id)
    }
    if isinstance(nodes, dict):
        for node_id, node in nodes.items():
            if not isinstance(node, dict):
                continue
            node["title"] = _safe_node_title(
                node_id,
                node,
                translated_titles.get(node_id),
                language=player_language,
            )
            node["summary"] = _safe_node_summary(
                node_id,
                node,
                translated_summaries.get(node_id),
                language=player_language,
            )
            seen_choice_texts: set[str] = set()
            for index, choice in enumerate(node.get("children") or []):
                if not isinstance(choice, dict):
                    continue
                target = nodes.get(str(choice.get("child_id") or ""), {})
                choice["choice_text"] = _dedupe_choice_text(
                    _safe_choice_text(
                        choice.get("choice_text"),
                        translated_choices.get(f"{node_id}:{index}"),
                        fallback_index=index,
                        is_terminal_target=(
                            str(choice.get("child_id") or "") in ending_route_ids
                            or (
                                isinstance(target, dict)
                                and target.get("is_original") is True
                                and not bool(target.get("children") or [])
                            )
                        ),
                        language=player_language,
                    ),
                    seen=seen_choice_texts,
                    fallback_index=index,
                    language=player_language,
                )

    print(
        f"🌐 story_tree 玩家可见字段已归一化为 {player_language}："
        f"nodes={len(nodes) if isinstance(nodes, dict) else 0}, "
        f"choices={len(payload.get('choices', []))}"
    )
    return localized


def build_story_tree_language_bundle(
    story_tree: dict,
    *,
    languages: Optional[Iterable[str]] = None,
    selected_language: Optional[str] = None,
    source_text: str = "",
    provider: Optional[str] = None,
    translators: Optional[Dict[str, Translator]] = None,
) -> dict[str, dict]:
    """Build independent locale snapshots keyed by BCP-47-like language tags."""
    source_tree = _unwrap_story_tree_bundle(story_tree)
    selected = _normalize_language_tag(selected_language) if selected_language else ""
    target_languages = get_story_tree_player_languages(languages)
    if selected and selected not in target_languages:
        target_languages.append(selected)

    result: dict[str, dict] = {}
    for language in target_languages:
        translator = (translators or {}).get(language)
        result[language] = localize_story_tree_player_fields(
            source_tree,
            player_language=language,
            source_text=source_text,
            provider=provider,
            translator=translator,
        )
    return result


def _unwrap_story_tree_bundle(story_tree: dict) -> dict:
    """Return the canonical tree from either a single tree or locale bundle."""
    if not isinstance(story_tree, dict):
        return {}
    if isinstance(story_tree.get("nodes"), dict):
        return deepcopy(story_tree)
    preferred_languages = ["en-US", *get_story_tree_player_languages()]
    for language in dict.fromkeys(preferred_languages):
        candidate = story_tree.get(language)
        if isinstance(candidate, dict) and isinstance(candidate.get("nodes"), dict):
            return deepcopy(candidate)
    for candidate in story_tree.values():
        if isinstance(candidate, dict) and isinstance(candidate.get("nodes"), dict):
            return deepcopy(candidate)
    return deepcopy(story_tree)


def select_story_tree_language(
    story_tree_payload: dict,
    language: Optional[str],
) -> dict:
    """Select one locale snapshot from a multilingual story-tree payload.

    Unknown locales fall back to English, then to the first available locale. A
    deep copy prevents a consumer from mutating the stored bundle in memory.
    """
    payload = story_tree_payload if isinstance(story_tree_payload, dict) else {}
    requested = _normalize_language_tag(language)
    if requested in payload and isinstance(payload[requested], dict):
        return deepcopy(payload[requested])
    if "en-US" in payload and isinstance(payload["en-US"], dict):
        return deepcopy(payload["en-US"])
    for value in payload.values():
        if isinstance(value, dict) and isinstance(value.get("nodes"), dict):
            return deepcopy(value)
    return {}


def wrap_story_tree_language_bundles(
    story_tree: dict,
    bundles: Dict[str, dict],
    *,
    default_language: Optional[str] = None,
) -> dict:
    """Return the locale-keyed story-tree payload stored in ``story_tree.json``.

    The serialized shape intentionally has no shared ``nodes`` root: every locale
    is a complete independent snapshot, matching the contract consumed by the
    packaged game. ``default_language`` is accepted for API symmetry and future
    callers, but does not add a legacy alias to the payload.
    """
    del story_tree, default_language
    if not bundles:
        return {}
    normalized_bundles = {}
    for language, payload in bundles.items():
        if not isinstance(payload, dict):
            continue
        snapshot = deepcopy(payload)
        snapshot.pop("publication", None)
        normalized_bundles[_normalize_language_tag(language) or str(language)] = snapshot
    return normalized_bundles
