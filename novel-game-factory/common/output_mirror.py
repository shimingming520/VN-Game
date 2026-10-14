#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地产物 ↔ OSS 镜像工具。

目标：各服务产出到本地 /storage/outputs/{task_id}/... 的文件，同步一份到 OSS，
      OSS 上保持与本地完全一致的目录结构（object key = 相对 OUTPUTS_PATH 的路径）。
      game_builder 构建前反向拉取：OSS 有就下载覆盖本地，OSS 没有的保留本地（本地兜底）。

约定：
- object key = 本地文件相对 OUTPUTS_PATH 的相对路径（POSIX 分隔符）。
  例：/storage/outputs/abc/images/scenes/s1.png → "abc/images/scenes/s1.png"
- 不改变本地结构，只是多存一份到 OSS。
- OSS 环境变量未配置时，普通镜像调用安全降级为 no-op；strict=True
  的调用会明确抛错，避免把“未上传”误报为成功。

一个服务产出即可调用 mirror_paths 推送自己那部分产物，无需等待全部服务完成。
"""

import os
import logging
import shutil
import threading
import time
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Iterable, Optional, Union

from common.config import OUTPUTS_PATH
from common import oss_utils

logger = logging.getLogger(__name__)


class OutputMirrorError(RuntimeError):
    """Raised when a strict OSS mirror operation cannot be completed."""


# 拉取失败可以放行的素材类型。BGM / ambience 单文件可达十几 MB，跨境读取偶发
# 超时；缺了背景音游戏仍然可玩，而 game_builder 生成的引擎代码已经把媒体加载
# 失败当成“静音继续”处理。story_tree.json、字幕和图片不在此列——缺了会构建出
# 残缺的包，那种“构建成功但游戏是坏的”比构建失败更难排查。
_OPTIONAL_FETCH_SUFFIXES = frozenset({".wav", ".mp3", ".ogg", ".m4a", ".flac"})

# 扩展名 → Content-Type
_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json",
}

# 不上传的文件名
_SKIP_NAMES = {".gitignore", ".gitkeep", ".DS_Store", "Thumbs.db"}


def _oss_configured() -> bool:
    """OSS 环境变量是否齐全。缺失时上层降级为 no-op。"""
    if oss_utils.oss_disabled():
        return False
    return all(
        os.getenv(k)
        for k in ("OSS_ENDPOINT", "OSS_ACCESS_KEY", "OSS_SECRET_KEY", "OSS_BUCKET")
    )


def _content_type(filename: str) -> str:
    return _CONTENT_TYPES.get(Path(filename).suffix.lower(), "application/octet-stream")


def _outputs_root() -> Path:
    return Path(OUTPUTS_PATH).resolve()


def _oss_prefix() -> str:
    """OSS key 的公共前缀，固定为 "storage/outputs"（取 OUTPUTS_PATH 末两段）。

    只用配置里的路径末尾两段，不做绝对路径 resolve —— 否则当 STORAGE_PATH 是
    相对/本地路径时会混入本机绝对路径（如 Users/xxx/.../storage/outputs）。

    例：OUTPUTS_PATH=/storage/outputs 或 .../repo/storage/outputs
        → 前缀均为 "storage/outputs"
        → object key = "storage/outputs/{task_id}/...", 与本地目录结构一致。
    """
    parts = Path(OUTPUTS_PATH).parts
    return "/".join(parts[-2:]).lstrip("/")


def _key_for(local_file: Path) -> str:
    """本地路径 → OSS object key（含 storage/outputs 前缀，与本地路径一致）。"""
    rel = local_file.resolve().relative_to(_outputs_root())
    return f"{_oss_prefix()}/{rel.as_posix()}"


def _iter_files(paths: Iterable[Union[str, Path]]) -> Iterable[Path]:
    """展开传入的文件/目录为文件列表（目录递归）。"""
    for p in paths:
        path = Path(p)
        if not path.exists():
            continue
        if path.is_file():
            yield path
        elif path.is_dir():
            for f in path.rglob("*"):
                if f.is_file():
                    yield f


def mirror_paths(
    paths: Iterable[Union[str, Path]],
    force: bool = False,
    strict: bool = False,
    progress_callback: Optional[Callable[[], None]] = None,
) -> int:
    """
    将本地文件/目录镜像上传到 OSS，保持相对 OUTPUTS_PATH 的目录结构。

    Args:
        paths: 文件或目录路径列表（目录会递归）。必须位于 OUTPUTS_PATH 之内。
        force: True=覆盖上传；False=OSS 已存在且大小一致时跳过。
        strict: True=初始化、上传或远端校验失败时抛出 OutputMirrorError。
        progress_callback: 每个文件处理完成后调用一次，用于任务租约续期。

    Returns:
        实际上传的文件数量。OSS 未配置或无文件时返回 0（不抛异常）。
    """
    if isinstance(paths, (str, Path)):
        paths = [paths]

    if oss_utils.oss_disabled():
        local_files = [
            local_file
            for local_file in _iter_files(paths)
            if local_file.name not in _SKIP_NAMES
        ]
        if strict and not local_files:
            raise OutputMirrorError("本地模式下未匹配到任何产物")
        logger.info("OSS_DISABLED=1，产物已保留在本地，跳过远程镜像: %s 个文件", len(local_files))
        return len(local_files)

    if not _oss_configured():
        if strict:
            raise OutputMirrorError(
                "OSS 未配置，严格镜像要求 OSS_ENDPOINT/OSS_ACCESS_KEY/"
                "OSS_SECRET_KEY/OSS_BUCKET 全部存在"
            )
        logger.info("OSS 未配置，跳过产物镜像上传")
        return 0

    root = _outputs_root()
    uploaded = 0

    try:
        oss_utils.init_oss_client()
    except Exception as e:
        if strict:
            raise OutputMirrorError(f"OSS 初始化失败: {e}") from e
        logger.warning(f"OSS 初始化失败，跳过镜像上传: {e}")
        return 0

    # Keep a group per input path.  A single mirror_paths call may receive
    # several directories (for example scene_painter's scenes/backgrounds/cg)
    # and the contract is bounded concurrency *inside* one directory, with
    # directories uploaded sequentially.  De-duplicate globally because glob
    # patterns can overlap.
    file_groups = []
    local_files = []
    seen_files = set()
    for path in paths:
        group = []
        for local_file in _iter_files([path]):
            if local_file.name in _SKIP_NAMES:
                continue
            try:
                local_file.resolve().relative_to(root)
            except ValueError:
                logger.warning(f"跳过 OUTPUTS_PATH 之外的文件: {local_file}")
                continue
            resolved = local_file.resolve()
            if resolved in seen_files:
                continue
            seen_files.add(resolved)
            group.append(local_file)
            local_files.append(local_file)
        if group:
            file_groups.append(group)

    if strict and not local_files:
        raise OutputMirrorError("未匹配到任何本地产物")
    if not local_files:
        logger.info("没有匹配到本地产物，跳过 OSS 镜像上传")
        return 0

    try:
        max_attempts = max(1, int(os.getenv("OSS_MIRROR_MAX_ATTEMPTS", "3")))
    except (TypeError, ValueError):
        max_attempts = 3

    try:
        workers = max(1, min(32, int(os.getenv("OSS_MIRROR_WORKERS", "12"))))
    except (TypeError, ValueError):
        workers = 12
    abort_event = threading.Event()

    def mirror_one(local_file: Path) -> dict:
        """Process one file and return a result instead of raising early.

        Keeping failures as data lets strict mode drain all in-flight uploads,
        produce a complete failure list, and only then fail the stage.
        """
        key = _key_for(local_file)
        client_context = (
            oss_utils.thread_local_client()
            if hasattr(oss_utils, "thread_local_client")
            else nullcontext()
        )
        with client_context:
            try:
                if abort_event.is_set():
                    return {
                        "status": "cancelled",
                        "path": local_file,
                        "key": key,
                    }
                size = local_file.stat().st_size
                if not force and oss_utils.oss_get_size(key) == size and size > 0:
                    return {"status": "skipped", "path": local_file, "key": key}

                data = local_file.read_bytes()
                expected_size = len(data)
                last_error = None
                for attempt in range(1, max_attempts + 1):
                    if abort_event.is_set():
                        return {
                            "status": "cancelled",
                            "path": local_file,
                            "key": key,
                        }
                    try:
                        ok = oss_utils.oss_write(
                            key, data, content_type=_content_type(local_file.name)
                        )
                        if ok and not strict:
                            return {
                                "status": "uploaded",
                                "path": local_file,
                                "key": key,
                            }
                        remote_size = oss_utils.oss_get_size(key) if ok else -1
                        if (
                            ok
                            and isinstance(remote_size, int)
                            and remote_size == expected_size
                        ):
                            return {
                                "status": "uploaded",
                                "path": local_file,
                                "key": key,
                            }
                        last_error = (
                            f"写入返回 {ok!r} 或远端大小 {remote_size!r} != "
                            f"本地 {expected_size}"
                        )
                    except Exception as exc:
                        last_error = str(exc)
                    if attempt < max_attempts:
                        # Wake immediately when the stage lease is lost rather
                        # than sleeping through the whole backoff interval.
                        if abort_event.wait(min(2 ** (attempt - 1), 8)):
                            return {
                                "status": "cancelled",
                                "path": local_file,
                                "key": key,
                            }
                return {
                    "status": "failed",
                    "path": local_file,
                    "key": key,
                    "error": f"已重试 {max_attempts} 次: {last_error}",
                }
            except Exception as exc:
                return {
                    "status": "failed",
                    "path": local_file,
                    "key": key,
                    "error": str(exc),
                }

    logger.info(
        "开始 OSS 镜像: %s 个文件，%s 个目录组，上传并发 %s，严格模式=%s",
        len(local_files), len(file_groups), workers, strict,
    )
    failures = []
    skipped = 0
    callback_error = None
    completed_total = 0
    for group in file_groups:
        if abort_event.is_set():
            break
        # A separate executor per directory makes the sequencing guarantee
        # explicit: no request from the next directory starts until this one
        # has fully drained (including running retries).
        with ThreadPoolExecutor(max_workers=min(workers, len(group))) as executor:
            future_map = {
                executor.submit(mirror_one, local_file): local_file
                for local_file in group
            }
            for future in as_completed(future_map):
                completed_total += 1
                local_file = future_map[future]
                try:
                    result = future.result()
                except Exception as exc:
                    # A worker must never make strict mode stop before all
                    # other in-flight uploads have drained; report it like
                    # any other failed file and aggregate it below.
                    result = {
                        "status": "failed",
                        "path": local_file,
                        "key": _key_for(local_file),
                        "error": str(exc),
                    }
                status = result["status"]
                if status == "uploaded":
                    uploaded += 1
                elif status == "skipped":
                    skipped += 1
                elif status != "cancelled":
                    failures.append(result)
                    logger.warning(
                        "镜像上传失败: %s -> %s: %s",
                        result["path"], result["key"], result.get("error"),
                    )
                if progress_callback is not None and not abort_event.is_set():
                    try:
                        progress_callback()
                    except Exception as exc:
                        # A lost stage lease must stop queued uploads. Running
                        # HTTP calls may finish, but no pending work is
                        # allowed to start after the owner check fails.
                        abort_event.set()
                        callback_error = exc
                        for pending in future_map:
                            if not pending.done():
                                pending.cancel()
                        break
                if completed_total == 1 or completed_total == len(local_files) or completed_total % 100 == 0:
                    logger.info(
                        "OSS 镜像进度: %s/%s（上传 %s，跳过 %s，失败 %s）",
                        completed_total, len(local_files), uploaded, skipped, len(failures),
                    )
        if callback_error is not None:
            break

    if callback_error is not None:
        raise callback_error

    if failures and strict:
        sample = "; ".join(
            f"{item['key']}: {item.get('error', 'unknown error')}"
            for item in failures[:20]
        )
        suffix = f"；另有 {len(failures) - 20} 个失败" if len(failures) > 20 else ""
        raise OutputMirrorError(
            f"镜像上传失败 {len(failures)}/{len(local_files)} 个文件{suffix}: {sample}"
        )

    if uploaded:
        logger.info(
            "产物镜像上传完成: 上传 %s，跳过 %s，失败 %s → OSS",
            uploaded, skipped, len(failures),
        )
    return uploaded


def mirror_outputs(
    task_id: str,
    subpaths: Iterable[str] = None,
    force: bool = False,
    strict: bool = False,
    progress_callback: Optional[Callable[[], None]] = None,
) -> int:
    """
    镜像某个 task 输出目录（或其指定子路径）到 OSS。供各服务产出后调用。

    Args:
        task_id: 任务 ID。
        subpaths: 相对 "{OUTPUTS_PATH}/{task_id}/" 的子路径列表，支持 glob
                  （如 "images/characters"、"*/audios"、"story_tree.json"）。
                  None=镜像整个 task 目录。
        force: True=覆盖上传。
        strict: True=没有匹配产物或镜像失败时抛出 OutputMirrorError。

    Returns:
        实际上传的文件数量。
    """
    task_id = _validate_task_id(task_id)
    task_dir = _outputs_root() / task_id
    if subpaths is None:
        return mirror_paths(
            [task_dir], force=force, strict=strict,
            progress_callback=progress_callback,
        )

    targets = []
    for sp in subpaths:
        targets.extend(task_dir.glob(sp))
    return mirror_paths(
        targets, force=force, strict=strict,
        progress_callback=progress_callback,
    )


def verify_mirrored_outputs(
    task_id: str,
    subpaths: Iterable[str],
) -> None:
    """Fail unless every selected local file has the same non-zero OSS size."""
    task = _validate_task_id(task_id)
    if not _oss_configured():
        raise RuntimeError(
            "VISUAL_OUTPUT_DURABILITY=oss_required but OSS is not configured"
        )
    oss_utils.init_oss_client()
    task_dir = _outputs_root() / task
    targets = []
    for subpath in subpaths:
        targets.extend(task_dir.glob(subpath))
    failures = []
    for local_file in _iter_files(targets):
        if local_file.name in _SKIP_NAMES:
            continue
        size = local_file.stat().st_size
        key = _key_for(local_file)
        if size <= 0 or oss_utils.oss_get_size(key) != size:
            failures.append(key)
    if failures:
        raise RuntimeError(
            "visual output OSS verification failed: " + ", ".join(failures[:20])
        )


def mirror_outputs_with_policy(
    task_id: str,
    subpaths: Iterable[str],
    *,
    force: bool = False,
) -> int:
    """Mirror outputs under the configured durability policy.

    ``pvc`` keeps today's shared-PVC behaviour. ``oss_required`` is intended for
    deployments whose Pods do not share one PVC; upload/verification failure is
    then a real stage failure rather than a warning.
    """
    policy = os.getenv("VISUAL_OUTPUT_DURABILITY", "pvc").strip().lower()
    if policy not in {"pvc", "oss_required"}:
        raise RuntimeError(f"unsupported VISUAL_OUTPUT_DURABILITY: {policy}")
    selected_subpaths = tuple(subpaths)
    uploaded = mirror_outputs(task_id, selected_subpaths, force=force)
    if policy == "oss_required":
        verify_mirrored_outputs(task_id, selected_subpaths)
    return uploaded
def _validate_task_id(task_id: str) -> str:
    task = str(task_id or "").strip()
    if (
        not task
        or Path(task).name != task
        or task in {".", ".."}
        or "/" in task
        or "\\" in task
    ):
        raise ValueError(f"非法 task_id: {task_id}")
    return task


def cleanup_disabled_audio_outputs(task_id: str) -> int:
    """精确删除 OSS 上已停产语言的字幕、JSON 和配音对象。

    只匹配 ``{task}/{node}/audios/{disabled-dir}/...``，不会触碰同级的
    BGM、ambience、SFX 或仍在生产的英语/日语目录。
    """
    # 音频语言配置只属于本清理入口。延迟导入可避免 API Gateway、人物和场景
    # 服务仅使用普通 OSS 镜像能力时，被无关的音频模块装载问题连带阻断。
    from common.audio_languages import DISABLED_AUDIO_DIRS

    task = _validate_task_id(task_id)
    if not _oss_configured():
        logger.info("OSS 未配置，跳过停产语言产物清理")
        return 0

    try:
        oss_utils.init_oss_client()
    except Exception as e:
        logger.warning(f"OSS 初始化失败，跳过停产语言产物清理: {e}")
        return 0

    task_prefix = f"{_oss_prefix()}/{task}/"
    deleted = 0
    for key in oss_utils.oss_list_keys(task_prefix):
        if not key.startswith(task_prefix):
            continue
        relative_parts = Path(key[len(task_prefix):]).parts
        if (
            len(relative_parts) < 3
            or relative_parts[1] != "audios"
            or relative_parts[2] not in DISABLED_AUDIO_DIRS
        ):
            continue
        if oss_utils.oss_delete(key):
            deleted += 1
        else:
            logger.warning(f"停产语言 OSS 产物删除失败: {key}")

    if deleted:
        logger.info(f"已清理 OSS 停产语言产物: {deleted} 个对象")
    return deleted


def delete_output_files(task_id: str, relative_paths: Iterable[str]) -> int:
    """按相对路径精确删除 OSS 上的产物对象。

    本地删了孤儿产物但 OSS 仍留旧对象时，下游按 OSS 取素材等于没删。调用方
    只传自己刚在本地删掉的路径，避免这里做任何前缀级批量匹配。
    """
    task = _validate_task_id(task_id)
    paths = [
        str(item).replace(os.sep, "/").strip().strip("/")
        for item in relative_paths
    ]
    paths = [item for item in paths if item and ".." not in Path(item).parts]
    if not paths:
        return 0
    if not _oss_configured():
        logger.info("OSS 未配置，跳过孤儿产物清理")
        return 0

    try:
        oss_utils.init_oss_client()
    except Exception as e:
        logger.warning(f"OSS 初始化失败，跳过孤儿产物清理: {e}")
        return 0

    deleted = 0
    for relative_path in paths:
        key = f"{_oss_prefix()}/{task}/{relative_path}"
        if oss_utils.oss_delete(key):
            deleted += 1
        else:
            logger.warning(f"孤儿 OSS 产物删除失败: {key}")
    return deleted


def fetch_output_file(
    task_id: str,
    relative_path: Union[str, Path],
    force: bool = False,
) -> Optional[Path]:
    """从标准 OSS 产物前缀拉取单个文件到本地 OUTPUTS_PATH。

    外部组只共享 ``storage/outputs/{task_id}/...``，不会挂载一组 PVC。
    交互式重生成需要 calm 底图、父场景图或 master JSON 时，用本函数按需
    补齐单文件，避免为了一个素材下载整个任务目录。

    返回本地文件路径；OSS 未配置、对象不存在或下载失败时返回已有本地文件，
    本地也不存在则返回 ``None``。
    """
    task = _validate_task_id(task_id)

    rel = Path(relative_path)
    if rel.is_absolute() or not rel.parts or any(part in {"", ".", ".."} for part in rel.parts):
        raise ValueError(f"非法产物相对路径: {relative_path}")

    local_file = _outputs_root() / task / rel
    if local_file.exists() and local_file.stat().st_size > 0 and not force:
        return local_file
    if not _oss_configured():
        return local_file if local_file.exists() else None

    try:
        oss_utils.init_oss_client()
        key = f"{_oss_prefix()}/{task}/{rel.as_posix()}"
        data = oss_utils.oss_read(key)
        if not data:
            return local_file if local_file.exists() else None

        local_file.parent.mkdir(parents=True, exist_ok=True)
        temp_file = local_file.with_name(
            f".{local_file.name}.{os.getpid()}.{time.time_ns()}.download"
        )
        try:
            temp_file.write_bytes(data)
            os.replace(temp_file, local_file)
        finally:
            temp_file.unlink(missing_ok=True)
        logger.info(f"已从 OSS 补齐标准产物: {key} -> {local_file}")
        return local_file
    except Exception as e:
        logger.warning(f"从 OSS 补齐标准产物失败: task={task} path={rel}: {e}")
        return local_file if local_file.exists() else None


def _remote_meta(key: str) -> tuple:
    """取远端 ``(size, etag)``，兼容只提供 ``oss_get_size`` 的旧桩件/测试。"""
    getter = getattr(oss_utils, "oss_get_meta", None)
    if callable(getter):
        return getter(key)
    return (oss_utils.oss_get_size(key), "")


def fetch_task_from_oss(
    task_id: str,
    force: bool = False,
    replace_existing: bool = False,
) -> int:
    """
    从 OSS 反向拉取某个 task 的全部产物到本地 OUTPUTS_PATH，保持目录结构。

    OSS 有的下载到本地对应路径（本地缺失或大小不一致时）；OSS 没有的不动，
    保留本地已有文件作为兜底。用于 game_builder 构建前优先取 OSS 素材。
    ``replace_existing=True`` 时，只有确认 OSS 前缀存在对象后，才删除本地
    ``outputs/{task_id}`` 整个目录，再把这一版 OSS 产物完整写回，避免旧素材
    与新任务发生冲突。OSS 未配置或无对象时不会删除本地目录。

    Args:
        task_id: 任务 ID（OSS key 前缀 = "storage/outputs/{task_id}/"）。
        force: True=无条件覆盖本地；False=本地已存在且大小一致时跳过。

    Returns:
        实际下载的文件数量。OSS 未配置时返回 0（不抛异常）。
    """
    task_id = _validate_task_id(task_id)
    if not _oss_configured():
        logger.info("OSS 未配置，game_builder 仅使用本地素材")
        return 0

    try:
        oss_utils.init_oss_client()
    except Exception as e:
        logger.warning(f"OSS 初始化失败，game_builder 仅使用本地素材: {e}")
        return 0

    root = _outputs_root()
    prefix = f"{_oss_prefix()}/{task_id}/"
    downloaded = 0

    keys = oss_utils.oss_list_keys(prefix)
    if not keys:
        logger.info(f"OSS 上未找到 task 产物: {prefix}")
        return 0

    task_root = root / task_id
    outputs_root = root.resolve()
    # ``task_id`` 已经过单段校验；这里仍然检查 resolve 后的直属关系，防止
    # OUTPUTS_PATH 被替换成符号链接或调用方传入异常的 Path 配置。
    if task_root.resolve().parent != outputs_root:
        raise ValueError(f"非法 task_id 路径: {task_id}")
    if task_root.is_symlink():
        # 绝不能沿着 outputs/{task_id} 符号链接写入 PVC 外部路径。
        raise ValueError(f"拒绝替换符号链接任务目录: {task_root}")
    if task_root.exists() and not task_root.is_dir():
        raise ValueError(f"拒绝替换非目录任务路径: {task_root}")
    staging_root = None
    write_root = task_root
    if replace_existing:
        # 先在 outputs 根目录下准备同级临时目录，完整下载成功后再整体替换。
        # OSS 短暂故障时保留旧目录，避免构建器读到半套新素材。
        staging_root = root / f".{task_id}.download.{os.getpid()}.{time.time_ns()}"
        staging_root.mkdir(parents=True, exist_ok=False)
        write_root = staging_root

    # 先做纯路径校验，把可下载的对象收集起来再并发处理。校验本身不涉及
    # 网络，放在循环外可以保证并发任务只处理已确认安全的目标路径。
    targets = []
    valid_object_count = 0
    for key in keys:
        if key.endswith("/"):
            continue  # 目录占位符

        # 只接受本次 prefix 返回的对象；即便 OSS SDK/Mock 返回了越界 key，
        # 也不能把它写到 outputs 根目录之外或其它 task 目录。
        if not isinstance(key, str) or not key.startswith(prefix):
            logger.warning(f"跳过 OSS 前缀之外的对象: {key!r}")
            continue
        relative_key = key[len(prefix):]
        rel = Path(relative_key)
        if (
            rel.is_absolute()
            or not rel.parts
            or any(part in {"", ".", ".."} for part in rel.parts)
        ):
            logger.warning(f"跳过非法 OSS 产物路径: {key!r}")
            continue
        local_file = write_root / rel
        try:
            local_file.resolve().relative_to(write_root.resolve())
        except ValueError:
            logger.warning(f"跳过越界 OSS 产物路径: {key!r}")
            continue
        # Only count objects after the resolved-path containment check succeeds.
        # Otherwise an SDK/mock returning an escaping key could make
        # replace_existing=True treat an empty staging directory as valid and
        # replace a complete local task directory with it.
        valid_object_count += 1
        targets.append((key, local_file))

    try:
        workers = max(1, min(32, int(os.getenv("OSS_FETCH_WORKERS", "16"))))
    except (TypeError, ValueError):
        workers = 16

    def fetch_one(key: str, local_file: Path) -> dict:
        """下载单个对象。失败作为数据返回，交由调用方决定补拉或报错。"""
        client_context = (
            oss_utils.thread_local_client()
            if hasattr(oss_utils, "thread_local_client")
            else nullcontext()
        )
        with client_context:
            try:
                if not force and local_file.exists():
                    # 同名覆盖时远端大小可能不变，因此以 etag（内容指纹）为准，
                    # 只在拿不到 etag 时退回大小比对。
                    remote_size, remote_etag = _remote_meta(key)
                    if not isinstance(remote_size, int) or remote_size < 0:
                        logger.warning(
                            f"无法确认 OSS 对象元数据，保留本地文件: {key} -> {local_file}"
                        )
                        return {"status": "skipped", "key": key}
                    if remote_etag and "-" not in remote_etag:
                        if oss_utils.local_etag(local_file) == remote_etag:
                            return {"status": "skipped", "key": key}
                    elif local_file.stat().st_size == remote_size:
                        return {"status": "skipped", "key": key}

                data = oss_utils.oss_read(key)
                if data is None:
                    return {
                        "status": "failed",
                        "key": key,
                        "error": "OSS 对象读取为空",
                    }

                local_file.parent.mkdir(parents=True, exist_ok=True)
                # 先写同目录临时文件，再原子替换，避免构建器读到半个 JSON/音频。
                temp_file = local_file.with_name(
                    f".{local_file.name}.{os.getpid()}.{time.time_ns()}.download"
                )
                try:
                    temp_file.write_bytes(data)
                    os.replace(temp_file, local_file)
                finally:
                    temp_file.unlink(missing_ok=True)
                return {"status": "downloaded", "key": key}
            except Exception as exc:
                return {"status": "failed", "key": key, "error": str(exc)}

    def run_batch(batch: list) -> tuple:
        """并发跑一批下载，返回 ``(成功数, 失败列表)``。"""
        got = 0
        failed = []
        if not batch:
            return (got, failed)
        with ThreadPoolExecutor(max_workers=min(workers, len(batch))) as executor:
            future_map = {
                executor.submit(fetch_one, key, local_file): key
                for key, local_file in batch
            }
            for future in as_completed(future_map):
                key = future_map[future]
                try:
                    result = future.result()
                except Exception as exc:
                    failed.append((key, str(exc)))
                    continue
                if result["status"] == "downloaded":
                    got += 1
                elif result["status"] == "failed":
                    failed.append((key, result.get("error", "unknown")))
        return (got, failed)

    logger.info(
        "开始从 OSS 拉取: %s 个对象，下载并发 %s，replace_existing=%s",
        len(targets), workers, replace_existing,
    )
    downloaded, failures = run_batch(targets)

    # 只补漏：失败多为跨境瞬时抖动，每轮只重拉上一轮失败的对象，绝不重跑全量。
    try:
        retry_rounds = max(0, int(os.getenv("OSS_FETCH_RETRY_ROUNDS", "2")))
    except (TypeError, ValueError):
        retry_rounds = 2

    for round_no in range(1, retry_rounds + 1):
        if not failures:
            break
        retry_keys = {key for key, _ in failures}
        retry_batch = [item for item in targets if item[0] in retry_keys]
        logger.warning(
            "第 %s 轮补拉：仅重试 %s 个失败对象（共 %s 个）",
            round_no, len(retry_batch), len(targets),
        )
        retry_downloaded, failures = run_batch(retry_batch)
        downloaded += retry_downloaded
        if not failures:
            logger.info("第 %s 轮补拉全部成功", round_no)
            break
        # 下一轮前稍作等待，给瞬时拥塞一点恢复时间。
        if round_no < retry_rounds:
            time.sleep(min(2 ** round_no, 8))

    # 音频类缺失降级为告警：允许构建继续，但必须在日志里点名，不做真正的静默。
    optional_failures = [
        (key, error) for key, error in failures
        if Path(key).suffix.lower() in _OPTIONAL_FETCH_SUFFIXES
    ]
    failures = [item for item in failures if item not in optional_failures]
    if optional_failures:
        logger.warning(
            "以下 %s 个音频素材拉取失败，按缺失音频继续构建（游戏会静音播放）：",
            len(optional_failures),
        )
        for key, error in optional_failures[:20]:
            logger.warning("  缺失音频: %s: %s", key, error)
        if len(optional_failures) > 20:
            logger.warning("  ...另有 %s 个未列出", len(optional_failures) - 20)
    if failures:
        logger.warning("补拉后仍有 %s 个关键对象失败：", len(failures))
        for key, error in failures[:10]:
            logger.warning("  拉取失败: %s: %s", key, error)

    if replace_existing:
        try:
            if valid_object_count == 0:
                # 只有目录占位符或非法越界 key 时，不能把现有完整目录替换成空目录。
                logger.warning(f"OSS 前缀没有可写入的有效文件，保留本地旧目录: {prefix}")
                return 0
            if failures:
                # 只有关键素材（story_tree.json / 字幕 / 图片等）缺失才放弃整批；
                # 音频缺失已在上面降级为告警，不该让整个构建失败。
                raise OSError(
                    f"OSS 关键产物下载不完整（{len(failures)}/{len(targets)} 个对象失败），保留本地旧目录"
                )
            if task_root.exists():
                shutil.rmtree(task_root)
                logger.info(f"已删除本地旧 task 产物，准备切换 OSS 新版本: {task_root}")
            os.replace(staging_root, task_root)
            staging_root = None
        finally:
            if staging_root is not None:
                shutil.rmtree(staging_root, ignore_errors=True)

    logger.info(f"game_builder 已从 OSS 拉取 {downloaded} 个素材到本地")
    return downloaded
