"""Task-level visual-style routing for automatic LLM and user-approved anchors.

The production task table remains unchanged.  The optional experiment config is
stored beside task outputs and mirrored with the existing OSS output contract::

    storage/outputs/<task_id>/style_anchors/config.json

Missing config means the automatic LLM route.  That route always consumes the
delayed ``global_style`` plus automatically generated Anchors.  A custom config
is deliberately fail-closed: downstream image generation may not silently
ignore an unconfirmed or missing anchor.
"""

from __future__ import annotations

import json
import hashlib
import os
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from PIL import Image, UnidentifiedImageError

from common.config import OUTPUTS_PATH
from common.global_style import (
    get_global_style,
    prepend_global_style,
)
from common.output_mirror import fetch_output_file, mirror_outputs
from common.local_runtime import oss_disabled
from common.oss_utils import oss_read, oss_write


STYLE_MODE_LLM = "llm"
STYLE_MODE_CUSTOM = "custom"
VALID_STYLE_MODES = {STYLE_MODE_LLM, STYLE_MODE_CUSTOM}

REMOVE_CHARACTER_BACKGROUND_KEY = "remove_character_background"

ASSET_CHARACTER = "character"
ASSET_SCENE = "scene"
VALID_ASSET_KINDS = {ASSET_CHARACTER, ASSET_SCENE}

ANCHOR_STATUS_MISSING = "missing"
ANCHOR_STATUS_GENERATING = "generating"
ANCHOR_STATUS_READY = "ready"
ANCHOR_STATUS_CONFIRMED = "confirmed"
ANCHOR_STATUS_STALE = "stale"
ANCHOR_STATUS_FAILED = "failed"
VALID_ANCHOR_STATUSES = {
    ANCHOR_STATUS_MISSING,
    ANCHOR_STATUS_GENERATING,
    ANCHOR_STATUS_READY,
    ANCHOR_STATUS_CONFIRMED,
    ANCHOR_STATUS_STALE,
    ANCHOR_STATUS_FAILED,
}

STYLE_CONFIG_RELATIVE_PATH = Path("style_anchors/config.json")
ANCHOR_RELATIVE_PATHS = {
    ASSET_CHARACTER: Path("style_anchors/character_anchor.png"),
    ASSET_SCENE: Path("style_anchors/scene_anchor.png"),
}


class VisualStyleConfigError(ValueError):
    """Raised when a custom style configuration cannot be safely consumed."""


@dataclass(frozen=True)
class AssetStylePlan:
    task_id: str
    asset_kind: str
    mode: str
    style: str
    anchor_path: Optional[Path] = None
    anchor_source: str = ""
    anchor_sha256: str = ""

    @property
    def uses_anchor(self) -> bool:
        return self.anchor_path is not None


def _validate_task_id(task_id: str) -> str:
    task = str(task_id or "").strip()
    if (
        not task
        or Path(task).name != task
        or task in {".", ".."}
        or "/" in task
        or "\\" in task
    ):
        raise VisualStyleConfigError(f"非法 task_id: {task_id}")
    return task


def _validate_asset_kind(asset_kind: str) -> str:
    kind = str(asset_kind or "").strip().lower()
    if kind not in VALID_ASSET_KINDS:
        raise VisualStyleConfigError(
            f"asset_kind 只支持: {', '.join(sorted(VALID_ASSET_KINDS))}"
        )
    return kind


def task_output_dir(task_id: str, *, outputs_path: Optional[str | Path] = None) -> Path:
    return Path(outputs_path or OUTPUTS_PATH).resolve() / _validate_task_id(task_id)


def style_config_path(task_id: str, *, outputs_path: Optional[str | Path] = None) -> Path:
    return task_output_dir(task_id, outputs_path=outputs_path) / STYLE_CONFIG_RELATIVE_PATH


def style_anchor_path(
    task_id: str,
    asset_kind: str,
    *,
    outputs_path: Optional[str | Path] = None,
) -> Path:
    kind = _validate_asset_kind(asset_kind)
    return task_output_dir(task_id, outputs_path=outputs_path) / ANCHOR_RELATIVE_PATHS[kind]


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_resolved_anchor_file(
    path: str | Path,
    asset_kind: str,
    requested_model: str,
) -> bool:
    """Verify that a published automatic Anchor is a decodable image.

    GPT Image 2 character Anchors additionally retain the native RGBA
    contract.  The import is intentionally lazy to avoid a module cycle with
    the character transparency helpers.
    """
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            image.load()
        if asset_kind == ASSET_CHARACTER and requested_model.startswith("gpt-image-2"):
            from services.char_designer.utils.native_transparency import (
                validate_native_transparent_character_file,
            )

            return validate_native_transparent_character_file(path)
        return True
    except (OSError, ValueError, SyntaxError, UnidentifiedImageError):
        return False


def default_visual_style_config() -> Dict[str, Any]:
    return {
        "version": 1,
        "mode": STYLE_MODE_LLM,
        # Backward-compatible key/default: character assets require transparent
        # output. GPT Image 2 now fulfills it natively instead of local matting.
        REMOVE_CHARACTER_BACKGROUND_KEY: True,
        ASSET_CHARACTER: {
            "global_style": "",
            "anchor_file": ANCHOR_RELATIVE_PATHS[ASSET_CHARACTER].as_posix(),
            "source": "",
            "confirmed": False,
            "stale": False,
            "sha256": "",
            "model": "",
            "requested_model": "",
            "prompt": "",
            "prompt_version": "",
            "generation_mode": "",
            "prompt_profile": "",
            "provider_prompt": "",
            "raw_anchor_file": "",
            "raw_anchor_sha256": "",
            "raw_prompt": "",
            "raw_provider_prompt": "",
            "raw_prompt_profile": "",
            "reference_file": "",
            "reference_sha256": "",
            "status": ANCHOR_STATUS_MISSING,
            "job_id": "",
            "error": "",
            "updated_at": "",
        },
        ASSET_SCENE: {
            "global_style": "",
            "anchor_file": ANCHOR_RELATIVE_PATHS[ASSET_SCENE].as_posix(),
            "source": "",
            "confirmed": False,
            "stale": False,
            "sha256": "",
            "model": "",
            "requested_model": "",
            "prompt": "",
            "prompt_version": "",
            "generation_mode": "",
            "prompt_profile": "",
            "provider_prompt": "",
            "raw_anchor_file": "",
            "raw_anchor_sha256": "",
            "raw_prompt": "",
            "raw_provider_prompt": "",
            "raw_prompt_profile": "",
            "reference_file": "",
            "reference_sha256": "",
            "status": ANCHOR_STATUS_MISSING,
            "job_id": "",
            "error": "",
            "updated_at": "",
        },
    }


def normalize_visual_style_config(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    config = default_visual_style_config()
    if raw is None:
        return config
    if not isinstance(raw, dict):
        raise VisualStyleConfigError("视觉风格配置必须是 JSON object")

    mode = str(raw.get("mode") or STYLE_MODE_LLM).strip().lower()
    if mode not in VALID_STYLE_MODES:
        raise VisualStyleConfigError(
            f"mode 只支持: {', '.join(sorted(VALID_STYLE_MODES))}"
        )
    config["version"] = int(raw.get("version") or 1)
    config["mode"] = mode
    if REMOVE_CHARACTER_BACKGROUND_KEY in raw:
        config[REMOVE_CHARACTER_BACKGROUND_KEY] = bool(
            raw.get(REMOVE_CHARACTER_BACKGROUND_KEY)
        )

    for kind in sorted(VALID_ASSET_KINDS):
        incoming = raw.get(kind) or {}
        if not isinstance(incoming, dict):
            raise VisualStyleConfigError(f"{kind} 配置必须是 JSON object")
        section = config[kind]
        for key in (
            "global_style",
            "anchor_file",
            "source",
            "sha256",
            "model",
            "requested_model",
            "prompt",
            "prompt_version",
            "generation_mode",
            "prompt_profile",
            "provider_prompt",
            "raw_anchor_file",
            "raw_anchor_sha256",
            "raw_prompt",
            "raw_provider_prompt",
            "raw_prompt_profile",
            "reference_file",
            "reference_sha256",
            "status",
            "job_id",
            "error",
            "updated_at",
        ):
            if key in incoming:
                section[key] = str(incoming.get(key) or "").strip()
        for key in ("confirmed", "stale"):
            if key in incoming:
                section[key] = bool(incoming.get(key))

        anchor_file = Path(section["anchor_file"])
        if (
            anchor_file.is_absolute()
            or not anchor_file.parts
            or any(part in {"", ".", ".."} for part in anchor_file.parts)
        ):
            raise VisualStyleConfigError(f"{kind}.anchor_file 非法: {anchor_file}")

        status = str(section.get("status") or "").strip().lower()
        if status not in VALID_ANCHOR_STATUSES:
            status = ANCHOR_STATUS_MISSING
        if section.get("confirmed"):
            status = ANCHOR_STATUS_CONFIRMED
        elif section.get("stale"):
            status = ANCHOR_STATUS_STALE
        elif status == ANCHOR_STATUS_CONFIRMED:
            status = ANCHOR_STATUS_READY if section.get("sha256") else ANCHOR_STATUS_MISSING
        elif (
            status == ANCHOR_STATUS_MISSING
            and section.get("sha256")
            and section.get("source")
        ):
            status = ANCHOR_STATUS_READY
        section["status"] = status

    return config


def load_visual_style_config(
    task_id: str,
    *,
    outputs_path: Optional[str | Path] = None,
    fetch_remote: bool = True,
) -> Dict[str, Any]:
    task = _validate_task_id(task_id)
    config_file = style_config_path(task, outputs_path=outputs_path)

    # Only the configured OUTPUTS_PATH participates in the shared OSS contract.
    if outputs_path is None and fetch_remote and not config_file.exists():
        fetched = fetch_output_file(task, STYLE_CONFIG_RELATIVE_PATH)
        if fetched is not None:
            config_file = fetched

    if not config_file.exists():
        return default_visual_style_config()
    try:
        raw = json.loads(config_file.read_text(encoding="utf-8"))
    except Exception as exc:
        raise VisualStyleConfigError(f"读取视觉风格配置失败: {config_file}: {exc}") from exc
    return normalize_visual_style_config(raw)


def save_visual_style_config(
    task_id: str,
    config: Dict[str, Any],
    *,
    outputs_path: Optional[str | Path] = None,
    mirror: bool = True,
) -> Path:
    task = _validate_task_id(task_id)
    normalized = normalize_visual_style_config(config)
    target = style_config_path(task, outputs_path=outputs_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(
            json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)

    if mirror and outputs_path is None:
        mirror_outputs(task, subpaths=["style_anchors"], force=True)
    return target


def visual_style_timestamp() -> str:
    """Return a stable ISO timestamp for persisted Anchor state changes."""
    return datetime.now().astimezone().isoformat()


def validate_visual_style_for_run(
    task_id: str,
    *,
    outputs_path: Optional[str | Path] = None,
    fetch_remote: bool = False,
) -> Dict[str, Any]:
    """Validate the task-level style contract immediately before Pipeline start.

    LLM mode deliberately does not try to load story-planner ``global_style`` or
    require Anchors at submission time: both are produced/reused inside the
    visual stages.  Custom mode is fail-closed and validates both confirmed
    Anchor files and their hashes through the same resolver used by downstream
    image services.
    """
    config = load_visual_style_config(
        task_id,
        outputs_path=outputs_path,
        fetch_remote=fetch_remote,
    )
    if config.get("mode") == STYLE_MODE_CUSTOM:
        resolve_asset_style_plan(
            task_id,
            ASSET_CHARACTER,
            outputs_path=outputs_path,
            fetch_remote=fetch_remote,
        )
        resolve_asset_style_plan(
            task_id,
            ASSET_SCENE,
            outputs_path=outputs_path,
            fetch_remote=fetch_remote,
        )
    return config


def _bytes_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def persist_visual_style_bundle_for_run(
    task_id: str,
    config: Optional[Dict[str, Any]] = None,
    *,
    outputs_path: Optional[str | Path] = None,
    strict_remote: bool = True,
) -> Path:
    """Persist the exact style bundle consumed by a newly submitted Pipeline.

    In production, custom Anchor files are uploaded and verified first; the
    config is uploaded last and acts as the commit marker for workers that only
    share OSS. Local experiment runs (``OSS_DISABLED=1`` or an explicit
    ``outputs_path``) remain local-only.
    """
    task = _validate_task_id(task_id)
    normalized = normalize_visual_style_config(
        config if config is not None else load_visual_style_config(
            task,
            outputs_path=outputs_path,
            fetch_remote=False,
        )
    )
    if normalized.get("mode") == STYLE_MODE_CUSTOM:
        # Reuse the downstream contract so confirmation, staleness, existence,
        # and SHA checks cannot drift between API Gateway and image workers.
        for kind in (ASSET_CHARACTER, ASSET_SCENE):
            _resolve_custom_anchor(
                task,
                kind,
                normalized[kind],
                outputs_path=outputs_path,
                fetch_remote=False,
            )

    config_path = save_visual_style_config(
        task,
        normalized,
        outputs_path=outputs_path,
        mirror=False,
    )
    if outputs_path is not None or not strict_remote or oss_disabled():
        return config_path

    prefix = f"storage/outputs/{task}/style_anchors"
    if normalized.get("mode") == STYLE_MODE_CUSTOM:
        for kind in (ASSET_CHARACTER, ASSET_SCENE):
            section = normalized[kind]
            anchor = _resolve_custom_anchor(
                task,
                kind,
                section,
                outputs_path=None,
                fetch_remote=False,
            )
            content = anchor.read_bytes()
            key = f"{prefix}/{anchor.name}"
            if not oss_write(key, content, "image/png"):
                raise VisualStyleConfigError(f"{kind} Anchor 持久化 OSS 失败")
            remote = oss_read(key)
            if remote is None or _bytes_sha256(remote) != section.get("sha256"):
                raise VisualStyleConfigError(f"{kind} Anchor OSS 校验失败")

    config_bytes = config_path.read_bytes()
    config_key = f"{prefix}/config.json"
    if not oss_write(config_key, config_bytes, "application/json"):
        raise VisualStyleConfigError("视觉风格配置持久化 OSS 失败")
    remote_config = oss_read(config_key)
    if remote_config is None or _bytes_sha256(remote_config) != _bytes_sha256(config_bytes):
        raise VisualStyleConfigError("视觉风格配置 OSS 校验失败")
    return config_path


def _resolve_custom_anchor(
    task_id: str,
    asset_kind: str,
    section: Dict[str, Any],
    *,
    outputs_path: Optional[str | Path],
    fetch_remote: bool,
) -> Path:
    if not section.get("confirmed"):
        raise VisualStyleConfigError(f"任务 {task_id} 的 {asset_kind} Anchor 尚未确认")
    if section.get("stale"):
        raise VisualStyleConfigError(
            f"任务 {task_id} 的 {asset_kind} Anchor 已因风格文本变化而失效，请重新生成并确认"
        )

    relative_path = Path(section.get("anchor_file") or ANCHOR_RELATIVE_PATHS[asset_kind])
    local_path = task_output_dir(task_id, outputs_path=outputs_path) / relative_path
    if outputs_path is None and fetch_remote and not local_path.exists():
        fetched = fetch_output_file(task_id, relative_path)
        if fetched is not None:
            local_path = fetched
    if not local_path.exists() or local_path.stat().st_size <= 0:
        raise VisualStyleConfigError(
            f"任务 {task_id} 缺少已确认的 {asset_kind} Anchor: {relative_path}"
        )
    expected_sha256 = str(section.get("sha256") or "").strip().lower()
    if not expected_sha256:
        raise VisualStyleConfigError(
            f"任务 {task_id} 的 {asset_kind} Anchor 缺少确认版本 sha256"
        )
    actual_sha256 = file_sha256(local_path) if expected_sha256 else ""
    if (
        expected_sha256
        and actual_sha256 != expected_sha256
        and outputs_path is None
        and fetch_remote
    ):
        fetched = fetch_output_file(task_id, relative_path, force=True)
        if fetched is not None and fetched.exists() and fetched.stat().st_size > 0:
            local_path = fetched
            actual_sha256 = file_sha256(local_path)
    if expected_sha256 and actual_sha256 != expected_sha256:
        raise VisualStyleConfigError(
            f"任务 {task_id} 的 {asset_kind} Anchor 文件与确认版本不一致"
        )
    return local_path


def _resolve_llm_anchor(
    task_id: str,
    asset_kind: str,
    section: Dict[str, Any],
    *,
    outputs_path: Optional[str | Path],
    fetch_remote: bool,
) -> Path:
    if str(section.get("source") or "").strip() != "llm_generated":
        raise VisualStyleConfigError(
            f"任务 {task_id} 尚未生成自动 {asset_kind} Anchor"
        )
    if section.get("stale") or section.get("status") != ANCHOR_STATUS_READY:
        raise VisualStyleConfigError(
            f"任务 {task_id} 的自动 {asset_kind} Anchor 状态不可用"
        )
    relative_path = Path(
        section.get("anchor_file") or ANCHOR_RELATIVE_PATHS[asset_kind]
    )
    local_path = task_output_dir(task_id, outputs_path=outputs_path) / relative_path
    if outputs_path is None and fetch_remote and not local_path.exists():
        fetched = fetch_output_file(task_id, relative_path)
        if fetched is not None:
            local_path = fetched
    if not local_path.exists() or local_path.stat().st_size <= 0:
        raise VisualStyleConfigError(
            f"任务 {task_id} 缺少自动 {asset_kind} Anchor: {relative_path}"
        )
    expected_sha256 = str(section.get("sha256") or "").strip().lower()
    if not expected_sha256:
        raise VisualStyleConfigError(
            f"任务 {task_id} 的自动 {asset_kind} Anchor 缺少 sha256"
        )
    requested_model = str(
        section.get("requested_model") or section.get("model") or ""
    ).strip().lower()
    if not _valid_resolved_anchor_file(
        local_path,
        asset_kind,
        requested_model,
    ):
        if outputs_path is None and fetch_remote:
            fetched = fetch_output_file(task_id, relative_path, force=True)
            if fetched is not None:
                local_path = fetched
        if not _valid_resolved_anchor_file(
            local_path,
            asset_kind,
            requested_model,
        ):
            raise VisualStyleConfigError(
                f"任务 {task_id} 的自动 {asset_kind} Anchor 图片不可解析或不满足透明合同"
            )
    actual_sha256 = file_sha256(local_path)
    if (
        actual_sha256 != expected_sha256
        and outputs_path is None
        and fetch_remote
    ):
        fetched = fetch_output_file(task_id, relative_path, force=True)
        if fetched is not None and fetched.exists() and fetched.stat().st_size > 0:
            local_path = fetched
            actual_sha256 = file_sha256(local_path)
    if actual_sha256 != expected_sha256:
        raise VisualStyleConfigError(
            f"任务 {task_id} 的自动 {asset_kind} Anchor 文件与配置不一致"
        )
    return local_path


def resolve_asset_style_plan(
    task_id: str,
    asset_kind: str,
    *,
    outputs_path: Optional[str | Path] = None,
    fetch_remote: bool = True,
    global_style_loader: Optional[Callable[[str], str]] = None,
) -> AssetStylePlan:
    task = _validate_task_id(task_id)
    kind = _validate_asset_kind(asset_kind)
    config = load_visual_style_config(
        task,
        outputs_path=outputs_path,
        fetch_remote=fetch_remote,
    )

    if config["mode"] == STYLE_MODE_LLM:
        loader = global_style_loader or get_global_style
        style = str(loader(task) or "").strip()
        section = config[kind]
        anchor = _resolve_llm_anchor(
            task,
            kind,
            section,
            outputs_path=outputs_path,
            fetch_remote=fetch_remote,
        )
        return AssetStylePlan(
            task_id=task,
            asset_kind=kind,
            mode=STYLE_MODE_LLM,
            style=style,
            anchor_path=anchor,
            anchor_source=str(section.get("source") or "").strip(),
            anchor_sha256=str(section.get("sha256") or "").strip(),
        )

    section = config[kind]
    anchor = _resolve_custom_anchor(
        task,
        kind,
        section,
        outputs_path=outputs_path,
        fetch_remote=fetch_remote,
    )
    return AssetStylePlan(
        task_id=task,
        asset_kind=kind,
        mode=STYLE_MODE_CUSTOM,
        style=str(section.get("global_style") or "").strip(),
        anchor_path=anchor,
        anchor_source=str(section.get("source") or "").strip(),
        anchor_sha256=str(section.get("sha256") or "").strip(),
    )


def get_asset_style(task_id: str, asset_kind: str) -> str:
    """Return the active text style for an asset category.

    An uploaded custom anchor may intentionally have no accompanying text style;
    in that case an empty string is valid and the image remains the sole style
    source.
    """
    return resolve_asset_style_plan(task_id, asset_kind).style


def should_remove_character_background(
    task_id: str,
    *,
    outputs_path: Optional[str | Path] = None,
    fetch_remote: bool = True,
) -> bool:
    """Return whether character assets must have a transparent background.

    The legacy key name remains stable for API compatibility, but GPT Image 2
    now satisfies the contract through native RGBA output rather than local
    matting. Missing configs deliberately preserve the historical default
    (transparent characters required).
    """
    config = load_visual_style_config(
        task_id,
        outputs_path=outputs_path,
        fetch_remote=fetch_remote,
    )
    return bool(config.get(REMOVE_CHARACTER_BACKGROUND_KEY, True))


def apply_asset_style(plan: AssetStylePlan, prompt: str) -> str:
    prompt = str(prompt or "").strip()
    if not plan.style:
        return prompt
    return prepend_global_style(plan.style, prompt)


def ensure_asset_style(task_id: str, asset_kind: str, prompt: str) -> str:
    return apply_asset_style(resolve_asset_style_plan(task_id, asset_kind), prompt)


def strip_leading_style(prompt: str, style: str) -> str:
    """Remove the exact active style only when it is the prompt prefix."""
    text = str(prompt or "").strip()
    normalized_style = str(style or "").strip().rstrip(" ,.;")
    if not text or not normalized_style:
        return text
    if text.startswith(normalized_style):
        return text[len(normalized_style):].lstrip(" \t\r\n,.;:")
    return text


def compose_anchor_guided_prompt(plan: AssetStylePlan, prompt: str) -> str:
    """Compose a content-first edit prompt whose anchor controls style only."""
    if not plan.uses_anchor:
        return apply_asset_style(plan, prompt)

    semantic_prompt = strip_leading_style(prompt, plan.style)
    if plan.asset_kind == ASSET_CHARACTER:
        lead = (
            "Create a completely new character image. Highest-priority character requirements:\n"
        )
        guard = (
            "Use the attached image only as a visual-style reference for medium, linework, palette, "
            "shading, lighting, and texture. Do not copy its identity, face, facial proportions, age, "
            "body shape, clothing, social status, accessories, props, pose, or composition. The textual "
            "character requirements override the reference image for every content detail."
        )
    else:
        lead = (
            "Create a completely new environment image. Highest-priority scene requirements:\n"
        )
        guard = (
            "Use the attached image only as a visual-style reference for medium, linework, palette, "
            "shading, lighting, material rendering, and texture. Do not copy its location, architecture, "
            "objects, spatial layout, camera angle, focal subject, or composition. The textual scene "
            "requirements override the reference image for every content detail."
        )

    parts = [f"{lead}{semantic_prompt}", guard]
    if plan.style:
        label = (
            "Optional user style notes"
            if plan.mode == STYLE_MODE_CUSTOM
            else "Global visual style direction"
        )
        parts.append(f"{label}:\n{plan.style}")
    return "\n\n".join(parts)
