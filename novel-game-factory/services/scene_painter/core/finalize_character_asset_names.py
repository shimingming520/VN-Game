"""Finalize public character asset names after all scene/CG generation.

Character and CG generation intentionally keep using the original speaker IDs.
Only after scene_painter has finished consuming those files do we publish the
anonymized IDs from the versioned ``name_mappings`` contract.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, Mapping, MutableMapping, Optional

from sqlalchemy import text

from common.artifact_io import atomic_output_path
from common.config import OUTPUTS_PATH
from common.cg_data import load_cg_contract, write_local_cg_contract
from common.name_mapping_utils import (
    get_change_conversion,
    get_change_name_section,
    get_original_conversion,
)
from common.progress_utils import ensure_stage_lease_owned
from common.visual_checkpoint import file_sha256
from services.scene_painter.core.artifact_contract import (
    atomic_copy_image,
    validate_image_file,
)


logger = logging.getLogger(__name__)

CHARACTER_DIRECTORY = PurePosixPath("images/characters")
REFERENCE_DIRECTORY = PurePosixPath("images/references")
MANIFEST_PATH = PurePosixPath("manifests/character_image_manifest.json")
DEBUG_DIRECTORY = PurePosixPath("style_anchors/debug_assets")

_OSS_ENV_KEYS = ("OSS_ENDPOINT", "OSS_ACCESS_KEY", "OSS_SECRET_KEY", "OSS_BUCKET")
_RESOURCE_ID_FORBIDDEN = frozenset("/\\\x00")


def normalize_resource_id(value: Any, *, field: str = "resource_id") -> str:
    """Validate an upstream Unicode resource ID without changing its text."""
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


class CharacterAssetNameFinalizationError(RuntimeError):
    """The final character filename contract cannot be safely published."""


@dataclass(frozen=True)
class AssetPathPair:
    old_path: Path
    new_path: Path


@dataclass
class CharacterAssetNameFinalizationResult:
    rename_map: Dict[str, str]
    renamed_files: list[str]
    updated_json_files: list[str]
    cg_references_updated: bool
    cg_data: Optional[Dict[str, Any]] = None


def _task_directory(outputs_root: str | os.PathLike, task_id: str) -> Path:
    task = str(task_id or "").strip()
    if (
        not task
        or task != str(task_id)
        or Path(task).name != task
        or task in {".", ".."}
        or "/" in task
        or "\\" in task
    ):
        raise CharacterAssetNameFinalizationError(f"非法 task_id: {task_id!r}")

    root = Path(outputs_root).resolve()
    task_dir = (root / task).resolve()
    try:
        task_dir.relative_to(root)
    except ValueError as exc:
        raise CharacterAssetNameFinalizationError(
            f"任务目录越过 OUTPUTS_PATH: {task_id!r}"
        ) from exc
    return task_dir


def _decode_json_object(value: Any, *, label: str) -> Dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise CharacterAssetNameFinalizationError(
                f"{label} 不是合法 JSON"
            ) from exc
    if not isinstance(value, Mapping):
        raise CharacterAssetNameFinalizationError(f"{label} 必须是 JSON object")
    return dict(value)


def load_name_mappings(
    task_id: str,
    *,
    outputs_root: str | os.PathLike = OUTPUTS_PATH,
) -> Dict[str, Any]:
    """Load the authoritative mapping from platform DB, then local fallback."""
    try:
        from common.platform_db import platform_engine

        with platform_engine.connect() as connection:
            row = connection.execute(
                text("SELECT data FROM name_mappings WHERE task_id = :tid LIMIT 1"),
                {"tid": task_id},
            ).fetchone()
        if row and row[0] is not None:
            return _decode_json_object(row[0], label="name_mappings.data")
    except CharacterAssetNameFinalizationError:
        raise
    except Exception as exc:
        logger.warning("读取 name_mappings 表失败，尝试本地文件: %s", exc)

    local_path = _task_directory(outputs_root, task_id) / "name_mappings.json"
    if not local_path.is_file():
        # 兼容历史任务和只验证 scene_painter 控制流的测试任务；只有已经
        # 发布新版结构却缺少中间映射链时才阻断。
        logger.warning(
            "数据库和本地均缺少 name_mappings，跳过人物资源最终改名: task_id=%s",
            task_id,
        )
        return {}
    try:
        return _decode_json_object(
            local_path.read_text(encoding="utf-8"),
            label=str(local_path),
        )
    except OSError as exc:
        raise CharacterAssetNameFinalizationError(
            f"读取 name_mappings 失败: {local_path}"
        ) from exc


def build_character_id_rename_map(mapping_data: Mapping[str, Any]) -> Dict[str, str]:
    """Build ``original speaker ID -> changed speaker ID`` without guessing."""
    data = dict(mapping_data)
    versioned = isinstance(data.get("original_name"), Mapping) or isinstance(
        data.get("change_name"), Mapping
    )
    if not versioned:
        # Historical mappings only describe one public ID set and therefore
        # have no deterministic old -> new rename operation.
        return {}

    original_conversion = get_original_conversion(data)
    change_section = get_change_name_section(data)
    new_names = change_section.get("new-name")
    new_names = dict(new_names) if isinstance(new_names, Mapping) else {}
    changed_conversion = get_change_conversion(data)

    if not original_conversion and (new_names or changed_conversion):
        raise CharacterAssetNameFinalizationError(
            "name_mappings.original_name.conversion 为空，无法建立最终改名链"
        )

    rename_map: Dict[str, str] = {}
    incomplete: list[str] = []
    for original_name, raw_old_id in original_conversion.items():
        source_name = str(original_name or "").strip()
        old_id = str(raw_old_id or "")
        changed_name = str(new_names.get(original_name) or "").strip()
        new_id = str(changed_conversion.get(changed_name) or "")
        if not source_name or not old_id or not changed_name or not new_id:
            incomplete.append(source_name or repr(original_name))
            continue
        try:
            old_id = normalize_resource_id(
                old_id, field="original_name.conversion"
            )
            new_id = normalize_resource_id(
                new_id, field="change_name.conversion"
            )
        except ValueError as exc:
            raise CharacterAssetNameFinalizationError(str(exc)) from exc
        previous = rename_map.get(old_id)
        if previous is not None and previous != new_id:
            raise CharacterAssetNameFinalizationError(
                f"同一旧人物 ID 映射到多个新 ID: {old_id} -> {previous}, {new_id}"
            )
        rename_map[old_id] = new_id

    if incomplete:
        raise CharacterAssetNameFinalizationError(
            "name_mappings 改名链不完整（original -> new-name -> conversion）: "
            + ", ".join(sorted(incomplete))
        )

    destinations: Dict[str, str] = {}
    for old_id, new_id in rename_map.items():
        other_old_id = destinations.get(new_id)
        if other_old_id is not None and other_old_id != old_id:
            raise CharacterAssetNameFinalizationError(
                f"多个旧人物 ID 指向同一新 ID: {other_old_id}, {old_id} -> {new_id}"
            )
        destinations[new_id] = old_id

    effective = {old_id: new_id for old_id, new_id in rename_map.items() if old_id != new_id}
    overlapping = sorted(set(effective.values()) & set(effective))
    if overlapping:
        raise CharacterAssetNameFinalizationError(
            "新人物 ID 与另一旧人物 ID 重叠，无法安全执行最终改名: "
            + ", ".join(overlapping)
        )
    return effective


def _relative(path: Path, task_dir: Path) -> str:
    return path.resolve().relative_to(task_dir.resolve()).as_posix()


def _character_filename(filename: str, rename_map: Mapping[str, str]) -> str:
    for old_id, new_id in rename_map.items():
        prefix = f"{old_id}_"
        if filename.startswith(prefix):
            return f"{new_id}_{filename[len(prefix):]}"
    return filename


def _reference_filename(filename: str, rename_map: Mapping[str, str]) -> str:
    for old_id, new_id in rename_map.items():
        if filename == f"{old_id}.png":
            return f"{new_id}.png"
    return filename


def _rewrite_character_relative_path(
    value: Any,
    rename_map: Mapping[str, str],
) -> str:
    raw = str(value or "")
    if not raw or "\\" in raw:
        return raw
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return raw
    if path.parent == CHARACTER_DIRECTORY:
        filename = _character_filename(path.name, rename_map)
    elif path.parent == REFERENCE_DIRECTORY:
        filename = _reference_filename(path.name, rename_map)
    else:
        return raw
    return str(path.with_name(filename))


def _collect_asset_pairs(
    task_dir: Path,
    rename_map: Mapping[str, str],
) -> list[AssetPathPair]:
    pairs: Dict[tuple[str, str], AssetPathPair] = {}

    character_dir = task_dir / Path(*CHARACTER_DIRECTORY.parts)
    if character_dir.is_dir():
        for path in sorted(character_dir.iterdir(), key=lambda item: item.name):
            if not path.is_file() or path.suffix.lower() != ".png":
                continue
            for old_id, new_id in rename_map.items():
                old_prefix = f"{old_id}_"
                new_prefix = f"{new_id}_"
                if path.name.startswith(old_prefix):
                    new_path = path.with_name(f"{new_id}_{path.name[len(old_prefix):]}")
                    pair = AssetPathPair(path, new_path)
                    pairs[(_relative(path, task_dir), _relative(new_path, task_dir))] = pair
                    break
                if path.name.startswith(new_prefix):
                    old_path = path.with_name(f"{old_id}_{path.name[len(new_prefix):]}")
                    pair = AssetPathPair(old_path, path)
                    pairs[(_relative(old_path, task_dir), _relative(path, task_dir))] = pair
                    break

    reference_dir = task_dir / Path(*REFERENCE_DIRECTORY.parts)
    for old_id, new_id in rename_map.items():
        old_path = reference_dir / f"{old_id}.png"
        new_path = reference_dir / f"{new_id}.png"
        if old_path.is_file() or new_path.is_file():
            pair = AssetPathPair(old_path, new_path)
            pairs[(_relative(old_path, task_dir), _relative(new_path, task_dir))] = pair

    return list(pairs.values())


def _ls_matches_resume_source(ls_path: Path, resume_source: Path) -> bool:
    """Prove that an LS is the current raw source, optionally top-padded.

    ``expand_canvas`` only adds transparent rows above the Provider output and
    keeps the original pixels bottom-aligned.  Rechecking that transform is
    stronger than trusting PNG metadata and also covers characters whose height
    requires no expansion.
    """
    if not validate_image_file(ls_path) or not validate_image_file(resume_source):
        return False
    try:
        from PIL import Image

        with Image.open(resume_source) as raw_image, Image.open(ls_path) as ls_image:
            raw = raw_image.convert("RGBA")
            ls = ls_image.convert("RGBA")
            if raw.width != ls.width or raw.height > ls.height:
                return False
            top_height = ls.height - raw.height
            if (
                ls.crop((0, top_height, ls.width, ls.height)).tobytes()
                != raw.tobytes()
            ):
                return False
            if top_height <= 0:
                return True
            alpha = ls.crop((0, 0, ls.width, top_height)).getchannel("A")
            return alpha.getbbox() is None
    except Exception:
        return False


def _derived_shot_matches_ls(shot_path: Path, ls_path: Path) -> bool:
    """Prove that MS/CS pixels are an unchanged top crop of the current LS."""
    if not validate_image_file(shot_path) or not validate_image_file(ls_path):
        return False
    try:
        from PIL import Image

        with Image.open(ls_path) as ls_image, Image.open(shot_path) as shot_image:
            ls = ls_image.convert("RGBA")
            shot = shot_image.convert("RGBA")
            if shot.width != ls.width or shot.height <= 0 or shot.height > ls.height:
                return False
            expected = ls.crop((0, 0, ls.width, shot.height))
            return expected.tobytes() == shot.tobytes()
    except Exception:
        return False


def _replayed_file_matches_resume_lineage(
    task_dir: Path,
    asset_path: Path,
    *,
    resume_filename: Optional[str] = None,
) -> bool:
    """Validate a replayed old-ID file against the canonical raw LS source.

    Only LS files are retained in ``.resume_sources``.  MS/CS are deterministic
    local crops, so requiring same-name private sources for them would reject a
    valid partial replay after final public IDs have already been published.
    """
    active_match = re.fullmatch(r"(.+)_(LS|MS|CS)\.png", asset_path.name)
    source_name = str(resume_filename or asset_path.name)
    source_match = re.fullmatch(r"(.+)_(LS|MS|CS)\.png", source_name)
    if active_match is None or source_match is None:
        resume_source = (
            task_dir
            / ".resume_sources"
            / "char_designer"
            / "ls"
            / source_name
        )
        return (
            validate_image_file(resume_source)
            and validate_image_file(asset_path)
            and file_sha256(resume_source) == file_sha256(asset_path)
        )

    active_base, active_shot = active_match.groups()
    source_base, source_shot = source_match.groups()
    if active_shot != source_shot:
        return False
    ls_path = asset_path.with_name(f"{active_base}_LS.png")
    resume_source = (
        task_dir
        / ".resume_sources"
        / "char_designer"
        / "ls"
        / f"{source_base}_LS.png"
    )
    if not _ls_matches_resume_source(ls_path, resume_source):
        return False
    return active_shot == "LS" or _derived_shot_matches_ls(asset_path, ls_path)


def _validate_asset_pairs(
    pairs: Iterable[AssetPathPair],
    *,
    allow_equivalent_duplicates: bool = False,
    allow_missing: bool = False,
) -> None:
    for pair in pairs:
        old_exists = pair.old_path.is_file()
        new_exists = pair.new_path.is_file()
        if old_exists and new_exists:
            if not (
                allow_equivalent_duplicates
                and validate_image_file(pair.old_path)
                and validate_image_file(pair.new_path)
                and file_sha256(pair.old_path) == file_sha256(pair.new_path)
            ):
                raise CharacterAssetNameFinalizationError(
                    f"人物资源新旧文件同时存在且内容不一致，拒绝猜测覆盖: "
                    f"{pair.old_path} / {pair.new_path}"
                )
            continue
        active_path = pair.old_path if old_exists else pair.new_path
        if not active_path.is_file() or not validate_image_file(active_path):
            if allow_missing:
                continue
            raise CharacterAssetNameFinalizationError(
                f"人物资源缺失或不可解码: {active_path}"
            )


def _load_cg_data_optional(task_id: str) -> Optional[Dict[str, Any]]:
    try:
        return load_cg_contract(task_id) or None
    except CharacterAssetNameFinalizationError:
        raise
    except Exception as exc:
        logger.warning("最终改名前置检查无法读取 cg.data: %s", exc)
        return None


def _validate_required_cg_character_references(
    task_dir: Path,
    cg_data: Optional[Mapping[str, Any]],
    rename_map: Mapping[str, str],
    *,
    allow_missing: bool = False,
) -> None:
    if not cg_data:
        return
    reverse_map = {new_id: old_id for old_id, new_id in rename_map.items()}
    errors: list[str] = []
    for cg_id, spec in cg_data.items():
        if not isinstance(spec, Mapping):
            continue
        for requirement in spec.get("reference_requirements") or []:
            if (
                not isinstance(requirement, Mapping)
                or requirement.get("role") != "character"
                or not requirement.get("required")
            ):
                continue
            candidates = [str(item) for item in requirement.get("file_candidates") or []]
            if any(
                (task_dir / Path(*PurePosixPath(candidate).parts)).is_file()
                and validate_image_file(task_dir / Path(*PurePosixPath(candidate).parts))
                for candidate in candidates
                if candidate and not PurePosixPath(candidate).is_absolute()
                and ".." not in PurePosixPath(candidate).parts
            ):
                continue

            old_matches: list[str] = []
            for candidate in candidates:
                path = PurePosixPath(candidate)
                if path.parent == CHARACTER_DIRECTORY:
                    for new_id, old_id in reverse_map.items():
                        prefix = f"{new_id}_"
                        if path.name.startswith(prefix):
                            old_matches.append(
                                str(path.with_name(f"{old_id}_{path.name[len(prefix):]}"))
                            )
                elif path.parent == REFERENCE_DIRECTORY:
                    for new_id, old_id in reverse_map.items():
                        if path.name == f"{new_id}.png":
                            old_matches.append(str(path.with_name(f"{old_id}.png")))
            available_old = [
                item
                for item in old_matches
                if validate_image_file(task_dir / Path(*PurePosixPath(item).parts))
            ]
            if available_old:
                errors.append(
                    f"cg[{cg_id}] 已提前要求新 ID，但磁盘仍是旧 ID: "
                    + ", ".join(available_old)
                )
            elif not allow_missing:
                entity = requirement.get("entity_name") or requirement.get("entity_id")
                errors.append(f"cg[{cg_id}] required 人物参考不存在: {entity}")
    if errors:
        raise CharacterAssetNameFinalizationError(
            "人物资源最终改名前置检查失败；未调用 scene/CG Provider。" + "；".join(errors)
        )


def validate_character_asset_name_finalization(
    task_id: str,
    *,
    outputs_root: str | os.PathLike = OUTPUTS_PATH,
    mapping_data: Optional[Mapping[str, Any]] = None,
    cg_data: Optional[Mapping[str, Any]] = None,
    allow_equivalent_duplicates: bool = False,
    allow_partial: bool = False,
) -> Dict[str, str]:
    """Read-only validation performed before scene/CG Provider calls."""
    task_dir = _task_directory(outputs_root, task_id)
    data = dict(mapping_data) if mapping_data is not None else load_name_mappings(
        task_id,
        outputs_root=outputs_root,
    )
    rename_map = build_character_id_rename_map(data)
    if not rename_map:
        return {}
    pairs = _collect_asset_pairs(task_dir, rename_map)
    _validate_asset_pairs(
        pairs,
        allow_equivalent_duplicates=allow_equivalent_duplicates,
        allow_missing=allow_partial,
    )
    active_cg_data = cg_data if cg_data is not None else _load_cg_data_optional(task_id)
    _validate_required_cg_character_references(
        task_dir,
        active_cg_data,
        rename_map,
        allow_missing=allow_partial,
    )
    return rename_map


def synchronize_replayed_character_assets(
    task_id: str,
    *,
    outputs_root: str | os.PathLike = OUTPUTS_PATH,
    mapping_data: Optional[Mapping[str, Any]] = None,
    allow_partial: bool = False,
) -> Dict[str, str]:
    """Publish replayed old-ID files over stale final-ID copies before CG reads.

    A char_designer replay intentionally writes the internal/original ID again.
    If a previous scene_painter attempt already published the renamed file, both
    paths coexist.  Copy the newly replayed old-ID image atomically over the
    public final-ID image while retaining the old path until finalization.
    """
    task_dir = _task_directory(outputs_root, task_id)
    data = dict(mapping_data) if mapping_data is not None else load_name_mappings(
        task_id, outputs_root=outputs_root
    )
    rename_map = build_character_id_rename_map(data)
    if not rename_map:
        return {}
    pairs = _collect_asset_pairs(task_dir, rename_map)
    for pair in pairs:
        if not pair.old_path.is_file() or not pair.new_path.is_file():
            continue
        if not validate_image_file(pair.old_path):
            raise CharacterAssetNameFinalizationError(
                f"重跑后的旧 ID 人物资源不可解码: {pair.old_path}"
            )
        if (
            validate_image_file(pair.new_path)
            and file_sha256(pair.old_path) == file_sha256(pair.new_path)
        ):
            continue
        if not _replayed_file_matches_resume_lineage(task_dir, pair.old_path):
            raise CharacterAssetNameFinalizationError(
                "人物资源新旧文件内容不同，但旧 ID 文件无法由当前 char_designer "
                f"恢复源证明，拒绝覆盖: {pair.old_path} / {pair.new_path}"
            )
        ensure_stage_lease_owned(task_id, "scene_painter")
        atomic_copy_image(pair.old_path, pair.new_path)
        logger.info(
            "重跑人物资源已同步到最终 ID: %s -> %s",
            pair.old_path.name,
            pair.new_path.name,
        )
    _validate_asset_pairs(
        pairs,
        allow_equivalent_duplicates=True,
        allow_missing=allow_partial,
    )
    active_cg_data = _load_cg_data_optional(task_id)
    _validate_required_cg_character_references(
        task_dir,
        active_cg_data,
        rename_map,
        allow_missing=allow_partial,
    )
    return rename_map


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    def _valid_json(candidate: str | os.PathLike) -> bool:
        try:
            decoded = json.loads(Path(candidate).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return isinstance(decoded, dict)

    with atomic_output_path(path, validator=_valid_json) as temporary:
        temporary.write_bytes(encoded)


def _rename_images(
    task_id: str,
    task_dir: Path,
    pairs: Iterable[AssetPathPair],
) -> list[str]:
    renamed: list[str] = []
    for pair in pairs:
        if not pair.old_path.is_file():
            continue
        if pair.new_path.exists():
            if (
                validate_image_file(pair.old_path)
                and validate_image_file(pair.new_path)
                and file_sha256(pair.old_path) == file_sha256(pair.new_path)
            ):
                ensure_stage_lease_owned(task_id, "scene_painter")
                pair.old_path.unlink()
                renamed.append(_relative(pair.new_path, task_dir))
                continue
            raise CharacterAssetNameFinalizationError(
                f"目标人物资源已存在且内容不同，拒绝覆盖: {pair.new_path}"
            )
        if not validate_image_file(pair.old_path):
            raise CharacterAssetNameFinalizationError(
                f"改名前人物资源不可解码: {pair.old_path}"
            )
        ensure_stage_lease_owned(task_id, "scene_painter")
        os.replace(pair.old_path, pair.new_path)
        if not validate_image_file(pair.new_path):
            raise CharacterAssetNameFinalizationError(
                f"改名后人物资源不可解码: {pair.new_path}"
            )
        renamed.append(_relative(pair.new_path, task_dir))
    return renamed


def _replace_manifest_id(value: Any, rename_map: Mapping[str, str]) -> Any:
    raw = str(value or "")
    return rename_map.get(raw, value)


def _validate_manifest_after_rename(
    payload: Mapping[str, Any],
    task_dir: Path,
    *,
    allow_partial: bool = False,
) -> None:
    if payload.get("task_id") != task_dir.name:
        raise CharacterAssetNameFinalizationError(
            "人物 Manifest task_id 与任务目录不一致"
        )
    assets = payload.get("assets")
    if not isinstance(assets, list) or (not assets and not allow_partial):
        raise CharacterAssetNameFinalizationError("人物 Manifest assets 必须是非空 array")
    if not isinstance(assets, list):
        raise CharacterAssetNameFinalizationError("人物 Manifest assets 必须是 array")
    seen_asset_ids: set[str] = set()
    for index, asset in enumerate(assets):
        if not isinstance(asset, Mapping):
            raise CharacterAssetNameFinalizationError(
                f"人物 Manifest assets[{index}] 必须是 object"
            )
        speaker_id = str(asset.get("speaker_id") or "")
        logical_id = str(asset.get("logical_character_id") or "")
        asset_id = str(asset.get("asset_id") or "")
        file_path = str(asset.get("file_path") or "")
        try:
            speaker_id = normalize_resource_id(speaker_id, field="Manifest.speaker_id")
        except ValueError as exc:
            raise CharacterAssetNameFinalizationError(
                f"人物 Manifest assets[{index}] 人物 ID 不一致: {exc}"
            ) from exc
        if logical_id != speaker_id:
            raise CharacterAssetNameFinalizationError(
                f"人物 Manifest assets[{index}] 人物 ID 不一致"
            )
        if not asset_id.startswith(f"character:{speaker_id}:"):
            raise CharacterAssetNameFinalizationError(
                f"人物 Manifest assets[{index}] asset_id 未同步"
            )
        if asset_id in seen_asset_ids:
            raise CharacterAssetNameFinalizationError(
                f"人物 Manifest 出现重复 asset_id: {asset_id}"
            )
        seen_asset_ids.add(asset_id)
        relative = PurePosixPath(file_path)
        if (
            relative.is_absolute()
            or relative.parent != CHARACTER_DIRECTORY
            or ".." in relative.parts
            or not relative.name.startswith(f"{speaker_id}_")
        ):
            raise CharacterAssetNameFinalizationError(
                f"人物 Manifest assets[{index}] file_path 未同步: {file_path}"
            )
        image_path = task_dir / Path(*relative.parts)
        if not validate_image_file(image_path):
            if allow_partial:
                continue
            raise CharacterAssetNameFinalizationError(
                f"人物 Manifest 指向的图片缺失或不可解码: {file_path}"
            )


def _update_manifest(
    task_id: str,
    task_dir: Path,
    rename_map: Mapping[str, str],
    *,
    allow_partial: bool = False,
) -> Optional[Path]:
    manifest_path = task_dir / Path(*MANIFEST_PATH.parts)
    if not manifest_path.is_file():
        return None
    try:
        payload = _decode_json_object(
            manifest_path.read_text(encoding="utf-8"),
            label=str(manifest_path),
        )
    except OSError as exc:
        raise CharacterAssetNameFinalizationError(
            f"读取人物 Manifest 失败: {manifest_path}"
        ) from exc

    changed = False
    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise CharacterAssetNameFinalizationError("人物 Manifest assets 必须是 array")
    for asset in assets:
        if not isinstance(asset, MutableMapping):
            raise CharacterAssetNameFinalizationError("人物 Manifest asset 必须是 object")
        old_speaker_id = str(asset.get("speaker_id") or "")
        new_speaker_id = str(rename_map.get(old_speaker_id) or old_speaker_id)
        for key in ("speaker_id", "logical_character_id"):
            new_value = _replace_manifest_id(asset.get(key), rename_map)
            if new_value != asset.get(key):
                asset[key] = new_value
                changed = True
        old_asset_id = str(asset.get("asset_id") or "")
        prefix = f"character:{old_speaker_id}:"
        if new_speaker_id != old_speaker_id and old_asset_id.startswith(prefix):
            asset["asset_id"] = f"character:{new_speaker_id}:{old_asset_id[len(prefix):]}"
            changed = True
        old_path = str(asset.get("file_path") or "")
        new_path = _rewrite_character_relative_path(old_path, rename_map)
        if new_path != old_path:
            asset["file_path"] = new_path
            changed = True

    if changed:
        ensure_stage_lease_owned(task_id, "scene_painter")
        _write_json_atomic(manifest_path, payload)

    # scene_painter 镜像不包含 char_designer 源码，因此在本服务内只校验本次
    # 机械改名会影响的 ID、路径及图片完整性，不重建人物 Manifest。
    _validate_manifest_after_rename(
        payload,
        task_dir,
        allow_partial=allow_partial,
    )
    return manifest_path if changed else None


def _update_debug_records(
    task_id: str,
    task_dir: Path,
    rename_map: Mapping[str, str],
) -> tuple[list[Path], list[AssetPathPair]]:
    debug_root = task_dir / Path(*DEBUG_DIRECTORY.parts)
    if not debug_root.is_dir():
        return [], []

    updated_paths: list[Path] = []
    renamed_pairs: list[AssetPathPair] = []
    for category in ("characters", "cg"):
        category_dir = debug_root / category
        if not category_dir.is_dir():
            continue
        for source_path in sorted(category_dir.glob("*.json"), key=lambda item: item.name):
            try:
                payload = _decode_json_object(
                    source_path.read_text(encoding="utf-8"),
                    label=str(source_path),
                )
            except OSError as exc:
                raise CharacterAssetNameFinalizationError(
                    f"读取生图调试记录失败: {source_path}"
                ) from exc

            changed = False
            old_filename = str(payload.get("filename") or "")
            if category == "characters":
                new_filename = _character_filename(old_filename, rename_map)
                new_filename = _reference_filename(new_filename, rename_map)
                if new_filename != old_filename:
                    payload["filename"] = new_filename
                    changed = True

            references = payload.get("reference_images")
            if isinstance(references, list):
                for reference in references:
                    if not isinstance(reference, MutableMapping):
                        continue
                    old_relative = str(reference.get("relative_path") or "")
                    new_relative = _rewrite_character_relative_path(old_relative, rename_map)
                    if new_relative != old_relative:
                        reference["relative_path"] = new_relative
                        reference["name"] = PurePosixPath(new_relative).name
                        reference["exists"] = validate_image_file(
                            task_dir / Path(*PurePosixPath(new_relative).parts)
                        )
                        changed = True
                    elif not old_relative:
                        old_name = str(reference.get("name") or "")
                        new_name = _character_filename(old_name, rename_map)
                        new_name = _reference_filename(new_name, rename_map)
                        if new_name != old_name:
                            reference["name"] = new_name
                            changed = True

            target_path = source_path
            if category == "characters" and source_path.name.endswith(".json"):
                asset_filename = source_path.name[:-5]
                new_asset_filename = _character_filename(asset_filename, rename_map)
                new_asset_filename = _reference_filename(new_asset_filename, rename_map)
                if new_asset_filename != asset_filename:
                    target_path = source_path.with_name(f"{new_asset_filename}.json")
                    if target_path.exists():
                        existing = _decode_json_object(
                            target_path.read_text(encoding="utf-8"),
                            label=str(target_path),
                        )
                        if existing != payload:
                            replayed_asset = (
                                task_dir
                                / Path(*CHARACTER_DIRECTORY.parts)
                                / asset_filename
                            )
                            if not replayed_asset.is_file():
                                replayed_asset = (
                                    task_dir
                                    / Path(*REFERENCE_DIRECTORY.parts)
                                    / asset_filename
                                )
                            if not replayed_asset.is_file():
                                replayed_asset = (
                                    task_dir
                                    / Path(*CHARACTER_DIRECTORY.parts)
                                    / new_asset_filename
                                )
                            if not replayed_asset.is_file():
                                replayed_asset = (
                                    task_dir
                                    / Path(*REFERENCE_DIRECTORY.parts)
                                    / new_asset_filename
                                )
                            if not _replayed_file_matches_resume_lineage(
                                task_dir,
                                replayed_asset,
                                resume_filename=asset_filename,
                            ):
                                raise CharacterAssetNameFinalizationError(
                                    "生图调试记录新旧文件同时存在且内容不一致，"
                                    "且旧记录无法由当前人物恢复源证明，拒绝覆盖: "
                                    f"{source_path} / {target_path}"
                                )
                            ensure_stage_lease_owned(task_id, "scene_painter")
                            _write_json_atomic(target_path, payload)
                        ensure_stage_lease_owned(task_id, "scene_painter")
                        source_path.unlink()
                        renamed_pairs.append(AssetPathPair(source_path, target_path))
                        updated_paths.append(target_path)
                        continue
                    changed = True

                # 重试时本地可能已经只剩新文件；仍重建 old -> new 对，确保 OSS
                # 上遗留的旧调试对象可以被精确删除。
                if target_path == source_path:
                    for old_id, new_id in rename_map.items():
                        old_asset_filename = None
                        prefix = f"{new_id}_"
                        if asset_filename.startswith(prefix):
                            old_asset_filename = (
                                f"{old_id}_{asset_filename[len(prefix):]}"
                            )
                        elif asset_filename == f"{new_id}.png":
                            old_asset_filename = f"{old_id}.png"
                        if old_asset_filename:
                            renamed_pairs.append(
                                AssetPathPair(
                                    source_path.with_name(f"{old_asset_filename}.json"),
                                    source_path,
                                )
                            )
                            break

            if not changed:
                continue
            ensure_stage_lease_owned(task_id, "scene_painter")
            _write_json_atomic(target_path, payload)
            if target_path != source_path:
                source_path.unlink()
                renamed_pairs.append(AssetPathPair(source_path, target_path))
            updated_paths.append(target_path)
    return updated_paths, renamed_pairs


def rewrite_cg_reference_candidates(
    cg_data: Mapping[str, Any],
    rename_map: Mapping[str, str],
) -> tuple[Dict[str, Any], bool]:
    """Place final paths first while retaining old paths for compatibility."""
    payload = copy.deepcopy(dict(cg_data))
    changed = False
    for spec in payload.values():
        if not isinstance(spec, MutableMapping):
            continue
        requirements = spec.get("reference_requirements")
        if not isinstance(requirements, list):
            continue
        for requirement in requirements:
            if not isinstance(requirement, MutableMapping) or requirement.get("role") != "character":
                continue
            candidates = requirement.get("file_candidates")
            if not isinstance(candidates, list):
                continue
            reordered: list[str] = []
            for candidate in candidates:
                raw = str(candidate)
                rewritten = _rewrite_character_relative_path(raw, rename_map)
                if rewritten != raw and rewritten not in reordered:
                    reordered.append(rewritten)
                if raw not in reordered:
                    reordered.append(raw)
            if reordered != candidates:
                requirement["file_candidates"] = reordered
                changed = True
    return payload, changed


def _update_cg_data(
    task_id: str,
    rename_map: Mapping[str, str],
    *,
    supplied_cg_data: Optional[Mapping[str, Any]],
    persist: bool,
) -> tuple[Optional[Dict[str, Any]], bool]:
    if supplied_cg_data is not None:
        payload, changed = rewrite_cg_reference_candidates(supplied_cg_data, rename_map)
        return payload, changed
    if not persist:
        return None, False

    from common.platform_db import platform_engine

    ensure_stage_lease_owned(task_id, "scene_painter")
    is_postgresql = platform_engine.dialect.name == "postgresql"
    lock_suffix = " FOR UPDATE" if is_postgresql else ""
    try:
        with platform_engine.begin() as connection:
            row = connection.execute(
                text(f"SELECT data FROM cg WHERE task_id = :tid LIMIT 1{lock_suffix}"),
                {"tid": task_id},
            ).fetchone()
            if not row or row[0] is None:
                raise LookupError("cg.data row missing")
            current = _decode_json_object(row[0], label="cg.data")
            payload, changed = rewrite_cg_reference_candidates(current, rename_map)
            if changed:
                ensure_stage_lease_owned(task_id, "scene_painter")
                update_sql = (
                    "UPDATE cg SET data = CAST(:data AS JSONB), update_time = NOW() "
                    "WHERE task_id = :tid"
                    if is_postgresql
                    else "UPDATE cg SET data = :data, update_time = CURRENT_TIMESTAMP "
                    "WHERE task_id = :tid"
                )
                result = connection.execute(
                    text(update_sql),
                    {
                        "tid": task_id,
                        "data": json.dumps(payload, ensure_ascii=False),
                    },
                )
                if result.rowcount != 1:
                    raise CharacterAssetNameFinalizationError(
                        f"CG 最终名称候选写回未命中唯一记录: task_id={task_id}"
                    )
            return payload, changed
    except CharacterAssetNameFinalizationError:
        raise
    except Exception as exc:
        logger.warning("CG 数据库无法更新，改写共享 cg.json: %s", exc)
        current = load_cg_contract(task_id)
        if not current:
            return None, False
        payload, changed = rewrite_cg_reference_candidates(current, rename_map)
        if changed:
            ensure_stage_lease_owned(task_id, "scene_painter")
            write_local_cg_contract(task_id, payload)
        return payload, changed


def _oss_is_configured() -> bool:
    from common.oss_utils import oss_disabled

    return not oss_disabled() and all(os.getenv(key) for key in _OSS_ENV_KEYS)


def _sync_oss(
    task_dir: Path,
    upload_paths: Iterable[Path],
    renamed_pairs: Iterable[AssetPathPair],
) -> None:
    if not _oss_is_configured():
        return

    from common import oss_utils
    from common.output_mirror import mirror_paths

    paths = sorted({path.resolve() for path in upload_paths if path.is_file()})
    if paths:
        mirror_paths(paths, force=True)

    prefix_parts = Path(OUTPUTS_PATH).parts[-2:]
    prefix = "/".join(prefix_parts).lstrip("/")

    def _key(path: Path) -> str:
        return f"{prefix}/{task_dir.name}/{_relative(path, task_dir)}"

    # mirror_paths deliberately degrades gracefully for ordinary producers;
    # final renaming is stricter because leaving only the old OSS key lets the
    # game_builder download a stale filename over the shared output contract.
    for path in paths:
        key = _key(path)
        expected_size = path.stat().st_size
        if expected_size <= 0 or oss_utils.oss_get_size(key) != expected_size:
            raise CharacterAssetNameFinalizationError(
                f"最终人物资源 OSS 发布校验失败: {key}"
            )

    for pair in renamed_pairs:
        old_key = _key(pair.old_path)
        new_key = _key(pair.new_path)
        if old_key == new_key:
            continue
        if oss_utils.oss_exists(old_key):
            if not oss_utils.oss_delete(old_key) or oss_utils.oss_exists(old_key):
                raise CharacterAssetNameFinalizationError(
                    f"旧人物资源 OSS 对象删除失败: {old_key}"
                )


def finalize_character_asset_names(
    task_id: str,
    *,
    outputs_root: str | os.PathLike = OUTPUTS_PATH,
    mapping_data: Optional[Mapping[str, Any]] = None,
    cg_data: Optional[Mapping[str, Any]] = None,
    persist_cg: bool = True,
    sync_oss: bool = True,
    allow_partial: bool = False,
) -> CharacterAssetNameFinalizationResult:
    """Rename character assets and mechanically publish their new references."""
    task_dir = _task_directory(outputs_root, task_id)
    data = dict(mapping_data) if mapping_data is not None else load_name_mappings(
        task_id,
        outputs_root=outputs_root,
    )
    rename_map = build_character_id_rename_map(data)
    if not rename_map:
        return CharacterAssetNameFinalizationResult({}, [], [], False, cg_data=dict(cg_data) if cg_data else None)

    pairs = _collect_asset_pairs(task_dir, rename_map)
    _validate_asset_pairs(
        pairs,
        allow_equivalent_duplicates=True,
        allow_missing=allow_partial,
    )

    # 先把新路径放到 CG 候选首位，同时保留旧路径。这样即使后续文件改名或
    # JSON 发布中断，下一次执行仍能从至少一套候选路径恢复。
    rewritten_cg, cg_changed = _update_cg_data(
        task_id,
        rename_map,
        supplied_cg_data=cg_data,
        persist=persist_cg,
    )

    ensure_stage_lease_owned(task_id, "scene_painter")
    renamed_files = _rename_images(task_id, task_dir, pairs)

    updated_json: list[Path] = []
    manifest_path = _update_manifest(
        task_id,
        task_dir,
        rename_map,
        allow_partial=allow_partial,
    )
    if manifest_path is not None:
        updated_json.append(manifest_path)
    debug_paths, debug_renames = _update_debug_records(task_id, task_dir, rename_map)
    updated_json.extend(debug_paths)

    all_renames = [*pairs, *debug_renames]
    if sync_oss:
        ensure_stage_lease_owned(task_id, "scene_painter")
        manifest_upload_path = task_dir / Path(*MANIFEST_PATH.parts)
        _sync_oss(
            task_dir,
            [
                *(pair.new_path for pair in all_renames),
                *updated_json,
                *([manifest_upload_path] if manifest_upload_path.is_file() else []),
            ],
            all_renames,
        )

    return CharacterAssetNameFinalizationResult(
        rename_map=dict(rename_map),
        renamed_files=renamed_files,
        updated_json_files=[_relative(path, task_dir) for path in updated_json],
        cg_references_updated=cg_changed,
        cg_data=rewritten_cg,
    )
