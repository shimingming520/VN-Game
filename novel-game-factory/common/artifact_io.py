"""最终产物的原子落盘与轻量完整性校验。"""
from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import threading
import uuid
from typing import Any, Callable, Iterator, List, Optional, Union


PathLike = Union[str, os.PathLike]
Validator = Callable[[PathLike], bool]

# 各步骤入库失败时的降级落盘位置有两套：一套走 OUTPUTS_PATH（STORAGE_PATH 挂载点），
# 一套是早期代码写死的 <repo>/storage/outputs。容器里两者分别是 /storage/outputs 与
# /app/storage/outputs，读产物时必须都找一遍，否则降级文件写了也读不到。
_REPO_OUTPUTS_ROOT = Path(__file__).resolve().parents[1] / "storage" / "outputs"


def local_artifact_roots() -> List[Path]:
    """本地产物根目录候选，按优先级去重返回。"""
    from common.config import OUTPUTS_PATH

    roots: List[Path] = []
    for root in (Path(OUTPUTS_PATH), _REPO_OUTPUTS_ROOT):
        if root not in roots:
            roots.append(root)
    return roots


def load_local_json_artifact(
    task_id: str,
    filename: str,
    *,
    logger: Optional[logging.Logger] = None,
) -> Optional[Any]:
    """按候选根目录读取 {root}/{task_id}/{filename}，全部读不到时返回 None。"""
    log = logger or logging.getLogger(__name__)
    for root in local_artifact_roots():
        path = root / str(task_id) / filename
        if not path.is_file():
            continue
        try:
            with path.open("r", encoding="utf-8") as file_obj:
                data = json.load(file_obj)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning(f"⚠️ 本地产物 {path} 读取失败，继续尝试其他位置: {exc}")
            continue
        if data:
            log.info(f"📁 已从本地产物加载: {path}")
            return data
    return None


class ArtifactValidationError(RuntimeError):
    """临时产物未通过格式/完整性校验，不能发布到最终路径。"""


def _temp_path(final_path: Path) -> Path:
    return final_path.with_name(
        f".{final_path.name}.{os.getpid()}.{threading.get_ident()}."
        f"{uuid.uuid4().hex}.part"
    )


def _fsync_file(path: Path) -> None:
    with path.open("rb") as file_obj:
        os.fsync(file_obj.fileno())


def _fsync_directory(path: Path) -> None:
    flags = getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY
    try:
        descriptor = os.open(str(path), flags)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            # Some NFS/CIFS/PVC drivers reject directory fsync even though the
            # file fsync and atomic os.replace above succeeded.  Treat directory
            # durability as best effort; artifact validation/file fsync remain
            # mandatory and must still fail loudly.
            pass
    finally:
        os.close(descriptor)


@contextmanager
def atomic_output_path(
    final_path: PathLike,
    *,
    validator: Optional[Validator] = None,
) -> Iterator[Path]:
    """把调用方写入临时文件，校验后用 ``os.replace`` 一次性发布。"""
    final = Path(final_path)
    final.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temp_path(final)
    try:
        yield temporary
        if not temporary.is_file() or temporary.stat().st_size <= 0:
            raise ArtifactValidationError(f"empty artifact: {temporary}")
        _fsync_file(temporary)
        if validator is not None and not validator(temporary):
            raise ArtifactValidationError(
                f"artifact validation failed: {temporary}"
            )
        os.replace(temporary, final)
        _fsync_directory(final.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_write_bytes(
    final_path: PathLike,
    data: bytes,
    *,
    validator: Optional[Validator] = None,
) -> None:
    with atomic_output_path(final_path, validator=validator) as temporary:
        with temporary.open("wb") as file_obj:
            file_obj.write(data)
            file_obj.flush()
            os.fsync(file_obj.fileno())
