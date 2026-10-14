"""Read helpers for the versioned ``name_mappings.json`` structure."""

from typing import Any, Dict, Mapping


LANGUAGE_KEYS = ("zh-CN", "en-US", "ja-JP")


def _dict(value: Any) -> Dict:
    return dict(value) if isinstance(value, Mapping) else {}


def get_original_name_section(data: Any) -> Dict:
    """Return ``original_name``; accept legacy flat mappings as fallback."""
    source = _dict(data)
    section = source.get("original_name")
    if isinstance(section, Mapping):
        return _dict(section)

    # Legacy conversion keys may already be changed names.  Re-key them with
    # new-name so pre-anonymization source names can still be resolved.
    conversion = _dict(source.get("conversion"))
    new_names = _dict(source.get("new-name"))
    if new_names:
        source_conversion = {}
        for original_name, changed_name in new_names.items():
            speaker_id = conversion.get(changed_name) or conversion.get(original_name)
            if speaker_id:
                source_conversion[str(original_name)] = str(speaker_id)
        return {"conversion": source_conversion}
    return {"conversion": conversion}


def get_change_name_section(data: Any) -> Dict:
    """Return ``change_name``; accept legacy flat mappings as fallback."""
    source = _dict(data)
    section = source.get("change_name")
    return _dict(section) if isinstance(section, Mapping) else source


def get_original_conversion(data: Any) -> Dict[str, str]:
    return _dict(get_original_name_section(data).get("conversion"))


def get_change_conversion(data: Any) -> Dict[str, str]:
    return _dict(get_change_name_section(data).get("conversion"))


def get_source_to_change_conversion(data: Any) -> Dict[str, str]:
    """Map every known source/localized name to its canonical speaker ID.

    ``change_name.conversion`` contains the source-language names, while the
    locale sections are keyed by speaker ID (``id -> translated name``).  The
    latter must also be inverted here; otherwise a plot using a translated
    ``speaker_name`` (for example ``艾琳娜``) is mistaken for a new NPC and
    subtitle generation later falls back to the default voice.
    """
    change = get_change_name_section(data)
    new_names = _dict(change.get("new-name"))
    changed_conversion = get_change_conversion(data)
    result = dict(changed_conversion)
    localized_aliases = {}
    ambiguous_aliases = set()
    for language in LANGUAGE_KEYS:
        localized = _dict(change.get(language))
        for speaker_id, localized_name in localized.items():
            if localized_name is None:
                continue
            name = str(localized_name).strip()
            if not name or name in result:
                continue
            speaker_id = str(speaker_id)
            previous = localized_aliases.get(name)
            if previous is None:
                localized_aliases[name] = speaker_id
            elif previous != speaker_id:
                ambiguous_aliases.add(name)
    for name, speaker_id in localized_aliases.items():
        if name not in ambiguous_aliases and name not in result:
            result[name] = speaker_id
    for original_name, changed_name in new_names.items():
        speaker_id = changed_conversion.get(changed_name)
        if speaker_id:
            result[str(original_name)] = str(speaker_id)
    return result


def get_localized_names(data: Any, language: str) -> Dict[str, str]:
    return _dict(get_change_name_section(data).get(language))
