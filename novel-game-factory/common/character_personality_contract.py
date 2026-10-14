"""Pure helpers for the combination-level character personality contract."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional


CHARACTER_PROMPT_CONTRACT_VERSION = 2
PERSONALITY_FIELDS = ("main_traits", "secondary_traits", "virtues", "flaws")


def _normalise_text_list(value: Any) -> List[str]:
    if isinstance(value, str):
        candidates = re.split(r"[，,、；;\n]", value)
    elif isinstance(value, (list, tuple, set)):
        candidates = value
    else:
        candidates = []

    result: List[str] = []
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _normalise_personality_id(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def personality_id_from_combination(combination: str) -> Optional[int]:
    """Return the personality ID from ``Name-p-b-c`` or ``None``."""
    parts = str(combination or "").rsplit("-", 3)
    if len(parts) != 4 or not all(part.isdigit() for part in parts[1:]):
        return None
    return int(parts[1])


def normalize_personality(personality: Any) -> Dict[str, Any]:
    """Return the stable personality subset shared by all image entry points."""
    source = personality if isinstance(personality, Mapping) else {}
    return {
        "personality_id": _normalise_personality_id(source.get("personality_id")),
        "main_traits": _normalise_text_list(source.get("main_traits")),
        "secondary_traits": _normalise_text_list(source.get("secondary_traits")),
        "virtues": _normalise_text_list(source.get("virtues")),
        "flaws": _normalise_text_list(source.get("flaws")),
    }


def has_personality_content(personality: Any) -> bool:
    normalised = normalize_personality(personality)
    return any(normalised[field] for field in PERSONALITY_FIELDS)


def select_personality_variant(character_data: Any, combination: str) -> Dict[str, Any]:
    """Select the exact personality variant referenced by ``combination``."""
    if isinstance(character_data, list):
        character_data = character_data[0] if character_data else {}
    if not isinstance(character_data, Mapping):
        return normalize_personality({})

    variants = character_data.get("personality_variants") or []
    if not isinstance(variants, list):
        return normalize_personality({})

    expected_id = personality_id_from_combination(combination)
    selected: Optional[Mapping[str, Any]] = None
    if expected_id is not None:
        for variant in variants:
            if not isinstance(variant, Mapping):
                continue
            if _normalise_personality_id(variant.get("personality_id")) == expected_id:
                selected = variant
                break

    # A valid combination is authoritative.  If its personality ID is absent,
    # returning another variant would silently attach the wrong character
    # temperament to the generated expression.  Only legacy callers without a
    # parseable combination may fall back to the first variant.
    if selected is None and expected_id is None and variants:
        first = variants[0]
        selected = first if isinstance(first, Mapping) else None

    normalised = normalize_personality(selected or {})
    if expected_id is not None:
        normalised["personality_id"] = expected_id
    return normalised


def build_character_prompt_record(
    *,
    name: str,
    speaker_id: str,
    positive_prompt: str,
    negative_prompt: str,
    height: Any,
    personality: Any,
    combination: str = "",
) -> Dict[str, Any]:
    """Build one JSONB record for ``character_prompts.data``."""
    normalised_personality = normalize_personality(personality)
    expected_id = personality_id_from_combination(combination)
    if expected_id is not None:
        normalised_personality["personality_id"] = expected_id

    record: Dict[str, Any] = {
        "prompt_contract_version": CHARACTER_PROMPT_CONTRACT_VERSION,
        "name": name,
        "speaker_id": speaker_id,
        "positive_prompt": positive_prompt,
        "negative_prompt": negative_prompt,
        "height": str(height or "").strip(),
        "personality": normalised_personality,
    }
    if combination:
        record["combination"] = combination
    return record
