#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OSS 操作公共模块

统一封装 oss2 操作，供所有服务重生成模块使用。
"""

import os
import logging
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Union
from urllib.parse import quote

from common.local_runtime import oss_disabled

logger = logging.getLogger(__name__)

# oss2 可能在 .env 加载后才安装，延迟 import
_oss2 = None
_client = None
_client_init_lock = threading.Lock()
_thread_local = threading.local()


def _local_storage_root() -> Path:
    """Return the mounted local storage root without importing static config."""
    return Path(os.getenv("STORAGE_PATH", "/storage")).expanduser()


def _local_path_for_key(key: str) -> Path:
    """Map an OSS-style key to a safe path below the local storage root."""
    normalized = str(key).replace("\\", "/").lstrip("/")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ValueError(f"非法本地对象 key: {key}")
    if parts and parts[0] == "storage":
        parts = parts[1:]
    elif parts:
        # Runtime/regeneration keys do not carry the canonical "storage/"
        # prefix; keep them in a separate local namespace.
        parts = ["oss", *parts]
    root = _local_storage_root().resolve()
    path = (root.joinpath(*parts)).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"本地对象路径越界: {key}") from exc
    return path


def local_path_for_key(key: str) -> Path:
    """Return the validated local path for a canonical storage key."""
    return _local_path_for_key(key)


def local_asset_url(key: str) -> str:
    """Build the API-relative URL used to serve local storage assets."""
    normalized = str(key).replace("\\", "/").lstrip("/")
    return f"/api/v1/local-assets/{quote(normalized, safe='/')}"


def _local_key_for_path(path: Path) -> str:
    root = _local_storage_root().resolve()
    relative = path.resolve().relative_to(root).as_posix()
    if relative.startswith("oss/"):
        return relative.removeprefix("oss/")
    return f"storage/{relative}"


class _ExpectedMissingObjectLogFilter(logging.Filter):
    """Hide only the OSS SDK's noisy, expected missing-object INFO record."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not (
            record.name == "oss2.api"
            and message.startswith("Exception:")
            and "NoSuchKey" in message
        )


_oss_missing_object_filter = _ExpectedMissingObjectLogFilter()


def _install_oss_missing_object_filter() -> None:
    """Install the SDK-log filter once per process."""
    sdk_logger = logging.getLogger("oss2.api")
    if _oss_missing_object_filter not in sdk_logger.filters:
        sdk_logger.addFilter(_oss_missing_object_filter)


def _is_missing_object_error(error: BaseException) -> bool:
    """Return whether an OSS exception means that the object is absent."""
    try:
        oss2 = _get_oss2()
        details = getattr(error, "details", {})
        error_code = str(getattr(error, "code", "") or "")
        if isinstance(error, oss2.exceptions.NoSuchBucket) or error_code == "NoSuchBucket":
            return False
        if isinstance(details, dict) and details.get("Code") == "NoSuchBucket":
            return False
        return isinstance(
            error,
            (
                oss2.exceptions.NoSuchKey,
                oss2.exceptions.NotFound,
            ),
        )
    except (AttributeError, ImportError):
        return False


def _get_oss2():
    global _oss2
    if _oss2 is None:
        try:
            import oss2
            _oss2 = oss2
            _install_oss_missing_object_filter()
        except ImportError:
            raise ImportError(
                "oss2 未安装，请运行: pip install oss2\n"
                "或: pip install -r requirements.txt"
            )
    return _oss2


def init_oss_client():
    """
    从环境变量初始化 OSS 客户端。

    读取以下环境变量：
    - OSS_ENDPOINT
    - OSS_ACCESS_KEY
    - OSS_SECRET_KEY
    - OSS_BUCKET
    """
    global _client

    if oss_disabled():
        logger.info("OSS_DISABLED=1，跳过 OSS 客户端初始化")
        return None

    if _client is not None:
        return _client

    # Celery/多线程上传可能同时首次初始化；只允许一个线程创建全局客户端。
    with _client_init_lock:
        if _client is not None:
            return _client
        _client = _build_client()
    logger.info(
        "OSS 客户端初始化成功: bucket=%s, endpoint=%s",
        os.getenv("OSS_BUCKET"), os.getenv("OSS_ENDPOINT"),
    )
    return _client


def _build_client():
    """Create an independent OSS Bucket client from the current environment."""
    oss2 = _get_oss2()
    endpoint = os.getenv("OSS_ENDPOINT")
    access_key_id = os.getenv("OSS_ACCESS_KEY")
    access_key_secret = os.getenv("OSS_SECRET_KEY")
    bucket_name = os.getenv("OSS_BUCKET")
    if not all([endpoint, access_key_id, access_key_secret, bucket_name]):
        raise ValueError(
            "OSS 环境变量未完整配置，请检查 .env 文件：\n"
            f"  OSS_ENDPOINT = {endpoint}\n"
            f"  OSS_ACCESS_KEY = {access_key_id and '***' or '(未设置)'}\n"
            f"  OSS_SECRET_KEY = {access_key_secret and '***' or '(未设置)'}\n"
            f"  OSS_BUCKET = {bucket_name}"
        )
    auth = oss2.Auth(access_key_id, access_key_secret)
    return oss2.Bucket(auth, f"https://{endpoint}", bucket_name)


@contextmanager
def thread_local_client():
    """Use an independent Bucket client for the current worker thread.

    ``oss2.Bucket`` wraps an HTTP session; keeping one client per upload thread
    avoids sharing mutable connection state while retaining the process-global
    client for existing serial callers.
    """
    if oss_disabled():
        yield None
        return
    previous = getattr(_thread_local, "client", None)
    if previous is None:
        try:
            _thread_local.client = _build_client()
        except ImportError:
            # Lightweight local tests may patch the process client without
            # installing oss2.  Production has already initialized oss2 before
            # worker submission, so this compatibility path is not used there.
            if _client is None:
                raise
            _thread_local.client = _client
    try:
        yield _thread_local.client
    finally:
        # Keep the client attached to the executor worker for the lifetime of
        # that thread.  Rebuilding it after every file would erase the benefit
        # of connection reuse and make large mirrors substantially slower.
        pass


def _bucket():
    local_client = getattr(_thread_local, "client", None)
    if local_client is not None:
        return local_client
    if _client is None:
        init_oss_client()
    return _client


# ---------------------------------------------------------------------------
# OSS 路径构建
# ---------------------------------------------------------------------------

# 各素材类型在 OSS 上的基础路径（相对于 bucket 根目录，不含前导斜杠）
#
# 多语言维度（language）：
# - voices（音频 .wav）与 subtitles（字幕 .txt）按语言分子目录：assets/{voices|subtitles}/{language}/
#   语言取值 zh-CN / en-US / ja-JP，由 oss_key(..., language=) 传入。
# - 其它素材类型（图片/音效）无语言维度，language 留空即可。
OSS_PATHS = {
    # 角色图片: {task_id}/{node_id}/assets/characters/
    "characters": "{task_id}/{node_id}/assets/characters/",
    # 封面图片: {task_id}/{node_id}/assets/images/
    "images": "{task_id}/{node_id}/assets/images/",
    # 场景图片: {task_id}/{node_id}/assets/scenes/
    "scenes": "{task_id}/{node_id}/assets/scenes/",
    # 语音音频: {task_id}/{node_id}/assets/voices/{language}/（含 language 子层）
    "voices": "{task_id}/{node_id}/assets/voices/",
    # 字幕文本: {task_id}/{node_id}/assets/subtitles/{language}/（含 language 子层）
    "subtitles": "{task_id}/{node_id}/assets/subtitles/",
    # BGM: {task_id}/{node_id}/assets/music/
    "music": "{task_id}/{node_id}/assets/music/",
    # SFX: {task_id}/{node_id}/assets/sounds/
    "sounds": "{task_id}/{node_id}/assets/sounds/",
}

# 需要 language 子层的素材类型（音频/字幕按语言隔离）
LANGUAGED_ASSET_TYPES = {"voices", "subtitles"}


def oss_key(asset_type: str, task_id: str, node_id: str, filename: str, language: str = "") -> str:
    """
    构建 OSS object key。

    Args:
        asset_type: 素材类型，见 OSS_PATHS
        task_id: 任务 ID
        node_id: 节点 ID
        filename: 文件名
        language: 语言维度（zh-CN/en-US/ja-JP）。非空时在 asset_type 目录下插入 {language}/ 子层，
                  用于 voices / subtitles；其它素材类型留空。

    Returns:
        OSS object key，如：
          "shuangxuezhucha_ed2f4b09/chapter_1/assets/voices/zh-CN/scene_alley-1.wav"（含 language）
          "shuangxuezhucha_ed2f4b09/chapter_1/assets/scenes/scene_alley_01.png"（无 language）
    """
    if asset_type not in OSS_PATHS:
        raise ValueError(f"未知的 asset_type: {asset_type}，可用值: {list(OSS_PATHS.keys())}")

    base = OSS_PATHS[asset_type].format(task_id=task_id, node_id=node_id).rstrip("/")
    # base 末尾去掉 /，filename 不要带前导 /
    if language:
        return f"{base}/{language}/{filename}"
    return f"{base}/{filename}"


# ---------------------------------------------------------------------------
# 基础操作
# ---------------------------------------------------------------------------

def oss_exists(key: str) -> bool:
    """检查 OSS 上文件是否存在。网络异常时抛出异常（不静默返回 False）。"""
    if oss_disabled():
        return _local_path_for_key(key).is_file()
    bucket = _bucket()
    _install_oss_missing_object_filter()
    try:
        return bucket.object_exists(key)
    except Exception as e:
        logger.warning(f"OSS 存在性检查失败: key={key}, error={e}")
        raise


def oss_read(key: str) -> Optional[bytes]:
    """
    读取 OSS 上的文件内容，返回 bytes。
    文件不存在时返回 None，不抛异常。
    """
    if oss_disabled():
        path = _local_path_for_key(key)
        try:
            return path.read_bytes() if path.is_file() else None
        except OSError as exc:
            logger.warning("本地对象读取失败: key=%s, error=%s", key, exc)
            return None
    bucket = _bucket()
    try:
        result = bucket.get_object(key)
        if result.status == 200:
            return result.read()
        return None
    except Exception as e:
        logger.warning(f"OSS 读取失败: key={key}, error={e}")
        return None


def oss_read_text(key: str, encoding: str = "utf-8") -> Optional[str]:
    """读取 OSS 上的文本文件，返回 str。"""
    data = oss_read(key)
    if data is None:
        return None
    return data.decode(encoding)


def oss_write(key: str, data: Union[bytes, str], content_type: str = "application/octet-stream") -> bool:
    """
    写入数据到 OSS。

    Args:
        key: OSS object key
        data: bytes 或 str
        content_type: Content-Type，str 时默认 "text/plain; charset=utf-8"

    Returns:
        是否写入成功
    """
    if oss_disabled():
        payload = data.encode("utf-8") if isinstance(data, str) else data
        path = _local_path_for_key(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        except OSError as exc:
            logger.error("本地对象写入失败: key=%s, error=%s", key, exc)
            return False
        logger.info("本地存储写入成功: %s (%s bytes) -> %s", key, len(payload), path)
        return True
    bucket = _bucket()
    if isinstance(data, str):
        if content_type in ("application/octet-stream",):
            content_type = "text/plain; charset=utf-8"
        data = data.encode("utf-8")

    try:
        result = bucket.put_object(key, data, headers={"Content-Type": content_type})
        if result.status == 200:
            logger.info(f"OSS 写入成功: {key} ({len(data)} bytes)")
            return True
        logger.warning(f"OSS 写入失败: {key}, status={result.status}")
        return False
    except Exception as e:
        logger.error(f"OSS 写入异常: key={key}, error={e}")
        return False


def oss_delete(key: str) -> bool:
    """删除 OSS 上的文件。文件不存在时返回 True。"""
    if oss_disabled():
        path = _local_path_for_key(key)
        try:
            path.unlink(missing_ok=True)
            logger.info("本地存储删除: %s", key)
            return True
        except OSError as exc:
            logger.error("本地对象删除失败: key=%s, error=%s", key, exc)
            return False
    bucket = _bucket()
    try:
        result = bucket.delete_object(key)
        logger.info(f"OSS 删除: {key}, status={result.status}")
        return True
    except Exception as e:
        logger.error(f"OSS 删除失败: key={key}, error={e}")
        return False


def oss_list_keys(prefix: str) -> list:
    """列出 OSS 上指定前缀下的所有文件 key。"""
    if oss_disabled():
        prefix_path = _local_path_for_key(prefix)
        if not prefix_path.exists():
            return []
        if prefix_path.is_file():
            return [_local_key_for_path(prefix_path)]
        return sorted(
            _local_key_for_path(path)
            for path in prefix_path.rglob("*")
            if path.is_file()
        )
    bucket = _bucket()
    keys = []
    try:
        for obj in _get_oss2().ObjectIterator(bucket, prefix=prefix):
            keys.append(obj.key)
    except Exception as e:
        logger.error(f"OSS 列举失败: prefix={prefix}, error={e}")
    return keys


def oss_get_size(key: str) -> int:
    """获取 OSS 上文件大小，不存在返回 -1。

    新对象在镜像上传前没有远端元数据是正常情况。将该探测结果保持为
    ``-1``（兼容现有调用方），但不再让 oss2 把预期的 ``NoSuchKey`` 打成
    ``INFO ... Exception``；其它元数据查询异常仍记录为告警，便于区分
    权限、网络或服务端问题。
    """
    if oss_disabled():
        path = _local_path_for_key(key)
        try:
            return path.stat().st_size if path.is_file() else -1
        except OSError as exc:
            logger.warning("本地对象大小查询失败: key=%s, error=%s", key, exc)
            return -1
    bucket = _bucket()
    _install_oss_missing_object_filter()
    try:
        meta = bucket.get_object_meta(key)
        return int(meta.content_length)
    except Exception as exc:
        if _is_missing_object_error(exc):
            logger.debug("OSS 远端对象不存在，准备首次上传: %s", key)
            return -1
        logger.warning("OSS 元数据查询失败: key=%s, error=%s", key, exc)
        return -1


def oss_get_meta(key: str) -> tuple:
    """返回 ``(size, etag)``；对象不存在或查询失败时返回 ``(-1, "")``。

    etag 是内容指纹，同名覆盖后大小可能不变但 etag 一定变化，因此比单纯
    比对大小更能识别“本地是旧版本”。非分片上传时 etag 即内容 MD5（大写十
    六进制，oss2 已去掉引号）；分片上传的 etag 带 ``-N`` 后缀，不是 MD5，
    调用方只做相等性比较即可，不要反推内容。
    """
    if oss_disabled():
        path = _local_path_for_key(key)
        try:
            if not path.is_file():
                return (-1, "")
            return (path.stat().st_size, local_etag(path))
        except OSError as exc:
            logger.warning("本地对象元数据查询失败: key=%s, error=%s", key, exc)
            return (-1, "")
    bucket = _bucket()
    _install_oss_missing_object_filter()
    try:
        meta = bucket.get_object_meta(key)
        etag = str(getattr(meta, "etag", "") or "").strip('"').upper()
        return (int(meta.content_length), etag)
    except Exception as exc:
        if _is_missing_object_error(exc):
            logger.debug("OSS 远端对象不存在: %s", key)
            return (-1, "")
        logger.warning("OSS 元数据查询失败: key=%s, error=%s", key, exc)
        return (-1, "")


def local_etag(path) -> str:
    """本地文件的 MD5（大写十六进制），与 OSS 非分片 etag 同格式。"""
    import hashlib

    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


# ---------------------------------------------------------------------------
# 高层封装：带类型和路径解析的读写
# ---------------------------------------------------------------------------

def read_asset(asset_type: str, task_id: str, node_id: str, filename: str, language: str = "") -> Optional[bytes]:
    """读取 OSS 素材文件，返回 bytes 或 None。voices/subtitles 传 language 定位语言子目录。"""
    key = oss_key(asset_type, task_id, node_id, filename, language=language)
    return oss_read(key)


def write_asset(asset_type: str, task_id: str, node_id: str, filename: str,
                data: Union[bytes, str], language: str = "") -> bool:
    """写入素材文件到 OSS。voices/subtitles 传 language 定位语言子目录。"""
    key = oss_key(asset_type, task_id, node_id, filename, language=language)
    return oss_write(key, data)


def asset_exists(asset_type: str, task_id: str, node_id: str, filename: str, language: str = "") -> bool:
    """检查 OSS 素材文件是否存在。voices/subtitles 传 language 定位语言子目录。"""
    key = oss_key(asset_type, task_id, node_id, filename, language=language)
    return oss_exists(key)


def delete_asset(asset_type: str, task_id: str, node_id: str, filename: str, language: str = "") -> bool:
    """删除 OSS 素材文件。voices/subtitles 传 language 定位语言子目录。"""
    key = oss_key(asset_type, task_id, node_id, filename, language=language)
    return oss_delete(key)


def get_asset_size(asset_type: str, task_id: str, node_id: str, filename: str, language: str = "") -> int:
    """获取 OSS 素材文件大小。voices/subtitles 传 language 定位语言子目录。"""
    key = oss_key(asset_type, task_id, node_id, filename, language=language)
    return oss_get_size(key)


# ---------------------------------------------------------------------------
# CDN URL 构建（如果有配置的话）
# ---------------------------------------------------------------------------

def oss_public_url(key: str) -> str:
    """
    构建 OSS 公开访问 URL。

    如果配置了 CDN_BASE_URL 则使用 CDN，否则用 OSS 原生域名。
    """
    if oss_disabled():
        return local_asset_url(key)

    cdn = os.getenv("CDN_BASE_URL", "").strip().rstrip("/")
    if cdn:
        if not cdn.startswith(("http://", "https://", "//")):
            cdn = f"https://{cdn}"
        return f"{cdn}/{key}"

    bucket = os.getenv("OSS_BUCKET", "")
    endpoint = os.getenv("OSS_ENDPOINT", "")
    if bucket and endpoint:
        return f"https://{bucket}.{endpoint}/{key}"

    return f"https://{key}"  # 兜底
