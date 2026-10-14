"""人物图片产物校验与原子化发布辅助工具。

本模块有意只包含服务本地的校验规则。对外的输出路径与清单（manifest）结构保持不变；
调用方先写入同目录下的暂存文件，仅在 Pillow 能够完整解码结果后才正式发布。
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import shutil
from typing import Callable, Iterator, Optional

from PIL import Image

from common.artifact_io import atomic_output_path
from common.config import OUTPUTS_PATH


class CharacterImageArtifactError(RuntimeError):
    """Raised when a required character image is missing or damaged."""


OwnershipCheck = Callable[[], object]


def validate_image_file(path: str | Path) -> bool:
    """Return ``True`` only for a non-empty, fully decodable image."""
    image_path = Path(path)
    try:
        if not image_path.is_file() or image_path.stat().st_size <= 0:
            return False
        with Image.open(image_path) as image:
            image.verify()
        with Image.open(image_path) as image:
            image.load()
            width, height = image.size
            if width <= 0 or height <= 0:
                return False
            if not image.format:
                return False
        return True
    except (OSError, ValueError, SyntaxError):
        return False


def require_valid_image(path: str | Path, *, label: str = "character image") -> Path:
    """Return the resolved path or raise when the image contract is not met."""
    image_path = Path(path)
    if not validate_image_file(image_path):
        raise CharacterImageArtifactError(f"invalid {label}: {image_path}")
    return image_path


def validate_transparent_character_file(path: str | Path) -> bool:
    """Require the current native-RGBA character output contract."""
    from services.char_designer.utils.native_transparency import (
        validate_native_transparent_character_file,
    )

    return validate_native_transparent_character_file(path)


@contextmanager
def atomic_image_output(
    final_path: str | Path,
    *,
    ownership_check: Optional[OwnershipCheck] = None,
    artifact_validator: Optional[Callable[[str | Path], bool]] = None,
) -> Iterator[Path]:
    """Yield a staging path and atomically publish a validated image.

    ``ownership_check`` is intentionally optional until the platform-owned
    fencing API is available.  When supplied, it runs immediately before the
    common atomic helper replaces the final path.
    """

    def _validator(path: str | Path) -> bool:
        if not validate_image_file(path):
            return False
        if artifact_validator is not None and not artifact_validator(path):
            return False
        check = ownership_check
        if check is None:
            from common.progress_utils import ensure_inherited_stage_lease_owned

            check = ensure_inherited_stage_lease_owned
        if check() is False:
            return False
        return True

    with atomic_output_path(final_path, validator=_validator) as temporary:
        yield temporary


def atomic_copy_image(source: str | Path, target: str | Path) -> Path:
    """Copy one valid image without exposing a partially written target."""
    source_path = require_valid_image(source, label="source character image")
    target_path = Path(target)
    with atomic_image_output(target_path) as temporary:
        shutil.copy2(source_path, temporary)
    return target_path


def character_resume_source_path(
    task_id: str,
    public_path: str | Path,
    *,
    fetch_remote: bool = False,
) -> Path:
    """Private shared-PVC source kept before canvas/shot post-processing."""
    source = Path(public_path)
    result = (
        Path(OUTPUTS_PATH)
        / task_id
        / ".resume_sources"
        / "char_designer"
        / "ls"
        / source.name
    )
    if fetch_remote and not validate_image_file(result):
        try:
            from common.output_mirror import fetch_output_file

            relative = result.relative_to(Path(OUTPUTS_PATH).resolve() / task_id)
            fetched = fetch_output_file(task_id, relative)
            if fetched is not None:
                result = fetched
        except Exception:
            pass
    return result


def publish_character_resume_source(
    task_id: str, public_path: str | Path
) -> Path:
    source_path = character_resume_source_path(task_id, public_path)
    return atomic_copy_image(public_path, source_path)


def restore_character_working_copy(
    task_id: str, public_path: str | Path
) -> Path:
    source_path = character_resume_source_path(task_id, public_path)
    return atomic_copy_image(source_path, public_path)
