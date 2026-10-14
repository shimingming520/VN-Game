"""角色身份与图片文件名统一工具。

用途：

- 将 ``角色名-性格编号-体型编号-服装编号`` 解析为标准角色组合；
- 根据数据库中的 ``speaker_id``、已有基础立绘文件或姓名转换结果，确定角色的统一 ID；
- 为基础立绘、第二组及后续变体、情绪图片生成一致的文件名；
- 在局部重新运行流程时，从已有 calm 图片恢复之前使用的角色 ID，避免同一角色被重复命名；
- 校验 ``speaker_id``，防止路径分隔符等不安全内容进入图片文件名。

本文件不负责生图、图生图或抠图，只负责让不同生成进程使用同一套角色身份和文件命名规则。
"""

from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Tuple


# ``name_mappings`` is the identity authority.  Its conversion values may
# contain ordinary Unicode punctuation (for example full-width parentheses or
# Chinese quotation marks), so the visual service must not impose an ASCII
# regular expression.  They are still used in filenames and path joins, hence
# path separators, whitespace, controls and traversal sentinels are rejected.
_RESOURCE_ID_FORBIDDEN = frozenset("/\\\x00")


def normalize_resource_id(value: Any, *, field: str = "resource_id") -> str:
    """Validate and return an upstream resource ID without transliteration."""
    if value is None:
        raise ValueError(f"{field} 不能为空")
    raw = str(value)
    normalized = unicodedata.normalize("NFC", raw)
    if not normalized or normalized != normalized.strip():
        raise ValueError(f"{field} 不能为空或不能包含首尾空白")
    if normalized in {".", ".."}:
        raise ValueError(f"{field} 不能是路径保留值: {normalized!r}")
    if any(ch in _RESOURCE_ID_FORBIDDEN for ch in normalized):
        raise ValueError(f"{field} 含路径或平台保留字符: {normalized!r}")
    if any(ch.isspace() for ch in normalized):
        raise ValueError(f"{field} 不能包含空白字符: {normalized!r}")
    if any(unicodedata.category(ch).startswith("C") for ch in normalized):
        raise ValueError(f"{field} 含控制或不可见字符: {normalized!r}")
    if normalized.endswith((".", " ")):
        raise ValueError(f"{field} 不能以点或空格结尾: {normalized!r}")
    return raw


def is_valid_resource_id(value: Any) -> bool:
    try:
        normalize_resource_id(value)
    except (TypeError, ValueError):
        return False
    return True


def split_combination(combination: str) -> Optional[Tuple[str, str, str, str]]:
    """Return ``(name, personality, body, clothing)`` for a valid combination."""
    parts = str(combination or "").rsplit("-", 3)
    if len(parts) != 4 or not parts[0] or not all(part.isdigit() for part in parts[1:]):
        return None
    return parts[0], parts[1], parts[2], parts[3]


def _safe_speaker_id(value: object) -> str:
    """Return an upstream speaker ID when it is safe as one resource segment.

    Unicode punctuation is intentional here: ``name_mappings`` is the
    authority for identity text and may contain parenthesized aliases.  The
    shared helper still rejects path separators, controls and whitespace so a
    caller cannot turn an ID into another path component.
    """
    try:
        return normalize_resource_id(value, field="speaker_id")
    except (TypeError, ValueError):
        return ""


def validate_speaker_id(value: object) -> str:
    """Raise ``ValueError`` unless *value* is a valid upstream resource ID."""
    return normalize_resource_id(value, field="speaker_id")


def authoritative_speaker_id(
    name: object,
    fallback: object = "",
    conversion: Optional[Mapping[str, object]] = None,
) -> str:
    """Resolve the exact upstream ID for a human name.

    ``conversion`` is keyed by the original human-readable name.  A supplied
    value wins over any locally re-derived pinyin value; this is what keeps
    ``character_prompts``, generated filenames and CG references on one ID
    contract.  Invalid/path-like values are rejected and the caller may then
    report the input contract failure instead of silently inventing an ID.
    """
    source_name = str(name or "").strip()
    candidate = (conversion or {}).get(source_name) if source_name else None
    candidate = candidate if candidate not in (None, "") else fallback
    try:
        return validate_speaker_id(candidate)
    except ValueError:
        return ""


def apply_authoritative_speaker_id(
    record: Mapping[str, Any],
    conversion: Optional[Mapping[str, object]] = None,
) -> dict[str, Any]:
    """Copy a prompt record and replace only its identity ID when authoritative."""
    result = dict(record)
    name = str(result.get("name") or "").strip()
    if not name:
        combination = str(result.get("combination") or "").strip()
        parts = combination.rsplit("-", 3)
        name = parts[0] if len(parts) == 4 else ""
    resolved = authoritative_speaker_id(
        name,
        result.get("speaker_id") or result.get("logical_character_id") or "",
        conversion,
    )
    if resolved:
        result["speaker_id"] = resolved
        if "logical_character_id" in result:
            result["logical_character_id"] = resolved
    return result


def speaker_combination_key(speaker_id: object, combination: str) -> str:
    """Build the filename-style combination key for a canonical speaker ID."""
    parsed = split_combination(combination)
    safe_speaker_id = _safe_speaker_id(speaker_id)
    if not parsed or not safe_speaker_id:
        return ""
    _, personality, body, clothing = parsed
    return f"{safe_speaker_id}-{personality}-{body}-{clothing}"


def infer_existing_calm_speaker_id(output_dir: Path, combination: str) -> str:
    """Infer a speaker ID from one unambiguous existing calm image."""
    parsed = split_combination(combination)
    if not parsed:
        return ""
    _, personality, body, clothing = parsed
    suffix = f"{personality}-{body}-{clothing}"
    candidates = set()
    for ending in (f"_{suffix}_calm_LS.png", f"_{suffix}_calm.png"):
        for path in output_dir.glob(f"*{ending}"):
            speaker_id = _safe_speaker_id(path.name[:-len(ending)])
            if speaker_id:
                candidates.add(speaker_id)
    return next(iter(candidates)) if len(candidates) == 1 else ""


def resolve_combination_speaker_id(
    combination: str,
    *,
    prompt_record: Optional[Mapping[str, object]] = None,
    output_dir: Optional[Path] = None,
    fallback_converter: Optional[Callable[[str], str]] = None,
    prefer_existing: bool = False,
) -> str:
    """Resolve the canonical ID used in character image filenames.

    Resolution sources are the prompt's public ``speaker_id``, an existing calm
    filename, then the normal name conversion fallback.  ``prefer_existing`` is
    useful for downstream reruns that must continue from an already-generated
    base image even if metadata has since changed.
    """
    parsed = split_combination(combination)
    if not parsed:
        return _safe_speaker_id(combination)
    raw_name = parsed[0]
    prompt_speaker_id = _safe_speaker_id(
        prompt_record.get("speaker_id") if prompt_record else ""
    )
    existing_speaker_id = (
        infer_existing_calm_speaker_id(Path(output_dir), combination)
        if output_dir is not None
        else ""
    )

    if prefer_existing and existing_speaker_id:
        return existing_speaker_id
    if prompt_speaker_id:
        return prompt_speaker_id
    if existing_speaker_id:
        return existing_speaker_id
    if fallback_converter is not None:
        try:
            converted = _safe_speaker_id(fallback_converter(raw_name))
            if converted:
                return converted
        except Exception:
            pass
    return _safe_speaker_id(raw_name)


def combination_filename(
    combination: str,
    emotion: str = "calm",
    shot: str = "LS",
    *,
    prompt_record: Optional[Mapping[str, object]] = None,
    output_dir: Optional[Path] = None,
    fallback_converter: Optional[Callable[[str], str]] = None,
    prefer_existing: bool = False,
) -> str:
    """Return the standard ``speaker_p-b-c_emotion_shot.png`` filename."""
    parsed = split_combination(combination)
    if not parsed:
        return f"{combination}_{emotion}_{shot}.png"
    _, personality, body, clothing = parsed
    speaker_id = resolve_combination_speaker_id(
        combination,
        prompt_record=prompt_record,
        output_dir=output_dir,
        fallback_converter=fallback_converter,
        prefer_existing=prefer_existing,
    )
    return f"{speaker_id}_{personality}-{body}-{clothing}_{emotion}_{shot}.png"
