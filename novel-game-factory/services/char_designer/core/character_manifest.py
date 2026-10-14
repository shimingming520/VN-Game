"""构建并校验对外公开的人物图片清单（manifest）。

该清单是跨服务的人物参考图契约。它有意仅由 ``character_prompts`` 以及任务输出
目录中实际存在的文件推导得出；私有的拼图（contact sheet）与派生的 MS/CS 图片
在 v1 版本中不予发布。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from common.config import OUTPUTS_PATH
from common.name_mapping_utils import get_original_conversion
from common.progress_utils import ensure_stage_lease_owned
from services.char_designer.core.artifact_contract import validate_image_file
from services.char_designer.utils.character_identity import (
    apply_authoritative_speaker_id,
    normalize_resource_id,
)


SCHEMA_VERSION = "character-image-v1"
MANIFEST_TYPE = "character_image"
MANIFEST_RELATIVE_PATH = "manifests/character_image_manifest.json"
CHARACTER_IMAGE_DIRECTORY = "images/characters"
SHOT_TYPE = "LS"

VALID_EMOTIONS = (
    "calm",
    "happy",
    "sad",
    "angry",
    "fearful",
    "disgusted",
    "surprised",
)
VALID_STATUSES = {"generated", "skipped", "success"}
VALID_ASSET_ROLES = {"identity_base", "costume_base", "expression_variant"}

_EMOTION_ORDER = {emotion: index for index, emotion in enumerate(VALID_EMOTIONS)}
_LS_FILENAME_RE = re.compile(
    r"^(?P<speaker_id>.+)_(?P<p>\d+)-(?P<b>\d+)-(?P<c>\d+)_"
    r"(?P<emotion>calm|happy|sad|angry|fearful|disgusted|surprised)_LS\.png$"
)

_REQUIRED_ASSET_FIELDS = {
    "asset_id",
    "asset_type",
    "status",
    "file_path",
    "speaker_id",
    "logical_character_id",
    "combination",
    "emotion",
    "shot_type",
    "asset_role",
    "consistency_roles",
}


class CharacterManifestError(ValueError):
    """Raised when the public character manifest contract cannot be satisfied."""


def _task_directory(outputs_root: os.PathLike | str, task_id: str) -> Path:
    if not isinstance(task_id, str) or not task_id.strip():
        raise CharacterManifestError("task_id must be a non-empty string")
    if (
        task_id != task_id.strip()
        or "/" in task_id
        or "\\" in task_id
        or Path(task_id).name != task_id
        or task_id in {".", ".."}
    ):
        raise CharacterManifestError(f"unsafe task_id: {task_id!r}")

    root = Path(outputs_root).resolve()
    task_dir = (root / task_id).resolve()
    try:
        task_dir.relative_to(root)
    except ValueError as exc:
        raise CharacterManifestError(f"task directory escapes outputs root: {task_id!r}") from exc
    return task_dir


def _parse_combination(combination: object) -> Tuple[str, str, Tuple[int, int, int]]:
    value = str(combination or "").strip()
    parts = value.rsplit("-", 3)
    if len(parts) != 4 or not parts[0]:
        raise CharacterManifestError(f"invalid combination: {value!r}")
    name, p_raw, b_raw, c_raw = parts
    if not all(part.isdigit() for part in (p_raw, b_raw, c_raw)):
        raise CharacterManifestError(f"invalid combination: {value!r}")
    suffix = f"{p_raw}-{b_raw}-{c_raw}"
    return name, suffix, (int(p_raw), int(b_raw), int(c_raw))


def _validate_speaker_id(value: object) -> str:
    try:
        return normalize_resource_id(value, field="speaker_id")
    except ValueError as exc:
        raise CharacterManifestError(str(exc)) from exc


def _normalise_prompt_records(
    records: Iterable[Mapping[str, object]],
) -> Tuple[Dict[Tuple[str, str], Dict[str, object]], Dict[str, Tuple[int, int, int]]]:
    prompt_by_key: Dict[Tuple[str, str], Dict[str, object]] = {}
    base_combination_by_speaker: Dict[str, Tuple[int, int, int]] = {}

    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            raise CharacterManifestError(f"character_prompts[{index}] must be an object")
        speaker_id = _validate_speaker_id(record.get("speaker_id"))
        combination = str(record.get("combination") or "").strip()
        _, suffix, numeric_combination = _parse_combination(combination)
        key = (speaker_id, suffix)
        if key in prompt_by_key:
            raise CharacterManifestError(
                f"duplicate character prompt combination: speaker_id={speaker_id}, combination={suffix}"
            )
        prompt_by_key[key] = {
            "speaker_id": speaker_id,
            "combination": combination,
            "numeric_combination": numeric_combination,
        }
        current_base = base_combination_by_speaker.get(speaker_id)
        if current_base is None or numeric_combination < current_base:
            base_combination_by_speaker[speaker_id] = numeric_combination

    if not prompt_by_key:
        raise CharacterManifestError("character_prompts contains no publishable combinations")
    return prompt_by_key, base_combination_by_speaker


def load_character_prompt_records(task_id: str) -> List[Dict[str, object]]:
    """Load the active character combination metadata produced by Info Extractor."""
    from sqlalchemy import text

    from common.platform_db import platform_engine

    with platform_engine.connect() as connection:
        row = connection.execute(
            text("SELECT data FROM character_prompts WHERE task_id = :tid"),
            {"tid": task_id},
        ).fetchone()
        try:
            mapping_row = connection.execute(
                text("SELECT data FROM name_mappings WHERE task_id = :tid LIMIT 1"),
                {"tid": task_id},
            ).fetchone()
        except Exception:
            # Historical deployments may not have the optional mapping table;
            # the prompt record's own ID remains a valid fallback.
            mapping_row = None

    if not row or not row[0]:
        raise CharacterManifestError(f"character_prompts not found for task_id={task_id}")
    data = row[0]
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as exc:
            raise CharacterManifestError("character_prompts.data is not valid JSON") from exc
    if not isinstance(data, list):
        raise CharacterManifestError("character_prompts.data must be a JSON array")
    mapping_data = mapping_row[0] if mapping_row else None
    if isinstance(mapping_data, str):
        try:
            mapping_data = json.loads(mapping_data)
        except json.JSONDecodeError:
            mapping_data = None
    conversion = get_original_conversion(mapping_data)
    return [
        apply_authoritative_speaker_id(item, conversion)
        if isinstance(item, Mapping)
        else item
        for item in data
    ]


def _asset_role(
    emotion: str,
    numeric_combination: Tuple[int, int, int],
    base_combination: Tuple[int, int, int],
) -> str:
    if emotion != "calm":
        return "expression_variant"
    if numeric_combination == base_combination:
        return "identity_base"
    return "costume_base"


def _asset_id(speaker_id: str, combination_suffix: str, emotion: str) -> str:
    return f"character:{speaker_id}:{combination_suffix}:{emotion}:{SHOT_TYPE}"


def build_character_image_manifest(
    task_id: str,
    *,
    outputs_root: os.PathLike | str = OUTPUTS_PATH,
    prompt_records: Optional[Iterable[Mapping[str, object]]] = None,
    allow_partial: bool = False,
) -> Dict[str, object]:
    """Build a v1 manifest from prompt metadata and existing calm/emotion LS PNGs."""
    task_dir = _task_directory(outputs_root, task_id)
    character_dir = task_dir / CHARACTER_IMAGE_DIRECTORY
    if not character_dir.is_dir():
        raise CharacterManifestError(f"character image directory not found: {character_dir}")

    records = load_character_prompt_records(task_id) if prompt_records is None else prompt_records
    prompt_by_key, base_by_speaker = _normalise_prompt_records(records)

    assets: List[Dict[str, object]] = []
    for image_path in sorted(character_dir.iterdir(), key=lambda item: item.name):
        if not image_path.is_file():
            continue
        match = _LS_FILENAME_RE.fullmatch(image_path.name)
        if not match:
            continue

        speaker_id = match.group("speaker_id")
        suffix = f"{match.group('p')}-{match.group('b')}-{match.group('c')}"
        metadata = prompt_by_key.get((speaker_id, suffix))
        if metadata is None:
            # Stale/inactive images may remain in the task directory.  They are
            # deliberately not part of the current public asset contract.
            continue

        emotion = match.group("emotion")
        numeric_combination = metadata["numeric_combination"]
        assets.append(
            {
                "asset_id": _asset_id(speaker_id, suffix, emotion),
                "asset_type": "character",
                "status": "generated",
                "file_path": f"{CHARACTER_IMAGE_DIRECTORY}/{image_path.name}",
                "speaker_id": speaker_id,
                "logical_character_id": speaker_id,
                "combination": metadata["combination"],
                "emotion": emotion,
                "shot_type": SHOT_TYPE,
                "asset_role": _asset_role(
                    emotion,
                    numeric_combination,
                    base_by_speaker[speaker_id],
                ),
                # Public enum values have not been frozen yet.  Publishing an
                # empty list is explicit and avoids leaking Group 1 internals.
                "consistency_roles": [],
            }
        )

    assets.sort(
        key=lambda asset: (
            str(asset["speaker_id"]).casefold(),
            _parse_combination(asset["combination"])[2],
            _EMOTION_ORDER[str(asset["emotion"])],
            str(asset["asset_id"]),
        )
    )

    calm_keys = {
        (str(asset["speaker_id"]), _parse_combination(asset["combination"])[1])
        for asset in assets
        if asset["emotion"] == "calm"
    }
    missing_calm = sorted(set(prompt_by_key) - calm_keys)
    if missing_calm and not allow_partial:
        formatted = ", ".join(f"{speaker_id}:{suffix}" for speaker_id, suffix in missing_calm)
        raise CharacterManifestError(f"required calm LS image missing for: {formatted}")

    manifest: Dict[str, object] = {
        "task_id": task_id,
        "schema_version": SCHEMA_VERSION,
        "manifest_type": MANIFEST_TYPE,
        "assets": assets,
    }
    validate_character_image_manifest(
        manifest,
        outputs_root=outputs_root,
        allow_partial=allow_partial,
    )
    return manifest


def _validated_relative_path(file_path: object) -> PurePosixPath:
    value = str(file_path or "")
    if not value or "\\" in value:
        raise CharacterManifestError(f"unsafe file_path: {value!r}")
    raw_parts = value.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise CharacterManifestError(f"unsafe file_path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or tuple(path.parts[:2]) != ("images", "characters"):
        raise CharacterManifestError(f"character file_path must be under images/characters: {value!r}")
    return path


def validate_character_image_manifest(
    manifest: Mapping[str, object],
    *,
    outputs_root: os.PathLike | str = OUTPUTS_PATH,
    allow_partial: bool = False,
) -> None:
    """Validate schema, deterministic IDs, role semantics, and local delivery."""
    if not isinstance(manifest, Mapping):
        raise CharacterManifestError("manifest must be an object")
    task_id = manifest.get("task_id")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise CharacterManifestError(f"schema_version must be {SCHEMA_VERSION}")
    if manifest.get("manifest_type") != MANIFEST_TYPE:
        raise CharacterManifestError(f"manifest_type must be {MANIFEST_TYPE}")
    task_dir = _task_directory(outputs_root, task_id)

    assets = manifest.get("assets")
    if not isinstance(assets, list) or (not assets and not allow_partial):
        raise CharacterManifestError("assets must be a non-empty array")
    if not isinstance(assets, list):
        raise CharacterManifestError("assets must be an array")

    seen_asset_ids = set()
    calm_combinations = set()
    combinations_by_speaker: Dict[str, set] = {}
    parsed_assets = []

    for index, asset in enumerate(assets):
        if not isinstance(asset, Mapping):
            raise CharacterManifestError(f"assets[{index}] must be an object")
        missing_fields = sorted(_REQUIRED_ASSET_FIELDS - set(asset))
        if missing_fields:
            raise CharacterManifestError(f"assets[{index}] missing fields: {', '.join(missing_fields)}")

        asset_id = str(asset["asset_id"])
        if asset_id in seen_asset_ids:
            raise CharacterManifestError(f"duplicate asset_id: {asset_id}")
        seen_asset_ids.add(asset_id)

        if asset["asset_type"] != "character":
            raise CharacterManifestError(f"assets[{index}].asset_type must be character")
        if asset["status"] not in VALID_STATUSES:
            raise CharacterManifestError(f"assets[{index}] has invalid status: {asset['status']!r}")
        if asset["shot_type"] != SHOT_TYPE:
            raise CharacterManifestError(f"assets[{index}].shot_type must be {SHOT_TYPE}")
        if asset["asset_role"] not in VALID_ASSET_ROLES:
            raise CharacterManifestError(f"assets[{index}] has invalid asset_role: {asset['asset_role']!r}")
        if not isinstance(asset["consistency_roles"], list):
            raise CharacterManifestError(f"assets[{index}].consistency_roles must be an array")

        speaker_id = _validate_speaker_id(asset["speaker_id"])
        if asset["logical_character_id"] != speaker_id:
            raise CharacterManifestError(
                f"assets[{index}].logical_character_id must equal speaker_id in v1"
            )
        _, suffix, numeric_combination = _parse_combination(asset["combination"])
        emotion = str(asset["emotion"])
        if emotion not in VALID_EMOTIONS or emotion != emotion.lower():
            raise CharacterManifestError(f"assets[{index}] has invalid emotion: {emotion!r}")
        expected_asset_id = _asset_id(speaker_id, suffix, emotion)
        if asset_id != expected_asset_id:
            raise CharacterManifestError(
                f"assets[{index}].asset_id must be deterministic: {expected_asset_id}"
            )

        relative_path = _validated_relative_path(asset["file_path"])
        local_path = (task_dir / Path(*relative_path.parts)).resolve()
        try:
            local_path.relative_to(task_dir)
        except ValueError as exc:
            raise CharacterManifestError(f"file_path escapes task directory: {relative_path}") from exc
        if not validate_image_file(local_path):
            raise CharacterManifestError(
                f"character asset is missing, empty, or damaged: {relative_path}"
            )

        filename_match = _LS_FILENAME_RE.fullmatch(local_path.name)
        if not filename_match:
            raise CharacterManifestError(f"file_path is not a valid LS character image: {relative_path}")
        filename_suffix = (
            f"{filename_match.group('p')}-{filename_match.group('b')}-{filename_match.group('c')}"
        )
        if (
            filename_match.group("speaker_id") != speaker_id
            or filename_suffix != suffix
            or filename_match.group("emotion") != emotion
        ):
            raise CharacterManifestError(f"asset metadata does not match filename: {relative_path}")

        combination_key = (speaker_id, suffix)
        combinations_by_speaker.setdefault(speaker_id, set()).add(numeric_combination)
        if emotion == "calm":
            calm_combinations.add(combination_key)
        parsed_assets.append((index, asset, speaker_id, suffix, numeric_combination, emotion))

    for speaker_id, combinations in combinations_by_speaker.items():
        base_combination = min(combinations)
        for index, asset, parsed_speaker, suffix, numeric_combination, emotion in parsed_assets:
            if parsed_speaker != speaker_id:
                continue
            if (speaker_id, suffix) not in calm_combinations:
                if not allow_partial:
                    raise CharacterManifestError(
                        f"combination has no calm LS base: speaker_id={speaker_id}, combination={suffix}"
                    )
            expected_role = _asset_role(emotion, numeric_combination, base_combination)
            if asset["asset_role"] != expected_role:
                raise CharacterManifestError(
                    f"assets[{index}].asset_role must be {expected_role}"
                )


def write_character_image_manifest(
    task_id: str,
    *,
    outputs_root: os.PathLike | str = OUTPUTS_PATH,
    prompt_records: Optional[Iterable[Mapping[str, object]]] = None,
    allow_partial: bool = False,
) -> Path:
    """Atomically write, read back, and revalidate the public manifest."""
    manifest = build_character_image_manifest(
        task_id,
        outputs_root=outputs_root,
        prompt_records=prompt_records,
        allow_partial=allow_partial,
    )
    task_dir = _task_directory(outputs_root, task_id)
    manifest_path = task_dir / MANIFEST_RELATIVE_PATH
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=manifest_path.parent,
            prefix=".character_image_manifest.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary_path = Path(handle.name)
        ensure_stage_lease_owned(task_id, "char_designer")
        os.replace(temporary_path, manifest_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    try:
        written_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CharacterManifestError(f"failed to read back manifest: {manifest_path}") from exc
    validate_character_image_manifest(
        written_manifest,
        outputs_root=outputs_root,
        allow_partial=allow_partial,
    )
    return manifest_path
