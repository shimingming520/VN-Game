"""
结果缓存机制（P1-13）
用于缓存LLM API调用结果，避免重复处理和节省成本。
"""
import hashlib
import json
import pickle
import logging
import os
from pathlib import Path
import time
from collections import Counter
from typing import Any, Callable, Optional, Dict
import uuid

logger = logging.getLogger(__name__)


CACHE_FORMAT_VERSION = 2


class ResultCache:
    """
    LLM结果缓存管理器（P1-13）

    功能：
    - 基于输入内容和参数生成缓存键
    - 持久化存储到磁盘（使用pickle）
    - 支持缓存失效和清理
    """

    def __init__(
        self,
        cache_dir: str = "./cache",
        *,
        namespace: str = "default-v1",
        max_age_seconds: Optional[float] = None,
        clock: Optional[Callable[[], float]] = None,
    ):
        """
        初始化缓存管理器。

        Args:
            cache_dir: 缓存文件存储目录
            namespace: 全局缓存版本；变更后旧键自动失效
            max_age_seconds: 缓存最大存活时间；None 表示不按时间失效
            clock: 时间源，默认使用 time.time
        """
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.namespace = str(namespace or "default-v1")
        self.max_age_seconds = (
            float(max_age_seconds)
            if max_age_seconds is not None and float(max_age_seconds) > 0
            else None
        )
        self._clock = clock or time.time
        self._metrics = Counter()
        self._miss_reasons = Counter()
        logger.info(
            "缓存目录: %s, namespace=%s, ttl_seconds=%s",
            self.cache_dir,
            self.namespace,
            self.max_age_seconds,
        )

    def get_cache_key(self, text_chunk: str, params: Dict[str, Any]) -> str:
        """
        生成缓存键（基于命名空间、文本内容和参数的 SHA-256 哈希）。

        Args:
            text_chunk: 输入文本
            params: 参数字典（如model, temperature, max_tokens等）

        Returns:
            64字符的 SHA-256 哈希字符串
        """
        content = json.dumps(
            {
                "namespace": self.namespace,
                "text": text_chunk,
                "params": params,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _record_miss(
        self,
        reason: str,
        *,
        cache_file: Optional[Path] = None,
        invalidated: bool = False,
    ) -> None:
        self._metrics["misses"] += 1
        self._miss_reasons[str(reason)] += 1
        if invalidated:
            self._metrics["invalidations"] += 1
            if cache_file is not None:
                cache_file.unlink(missing_ok=True)

    def get(self, key: str) -> Optional[Any]:
        """
        从缓存中获取结果。

        Args:
            key: 缓存键

        Returns:
            缓存的结果，如果不存在则返回None
        """
        cache_file = self.cache_dir / f"{key}.pkl"

        if not cache_file.exists():
            self._record_miss("not_found")
            logger.debug("缓存未命中: %s", key)
            return None

        try:
            with cache_file.open("rb") as handle:
                envelope = pickle.load(handle)
        except Exception as exc:
            self._metrics["read_errors"] += 1
            self._record_miss("corrupt", cache_file=cache_file, invalidated=True)
            logger.warning("缓存读取失败并已失效: key=%s, error=%s", key, exc)
            return None

        if (
            not isinstance(envelope, dict)
            or envelope.get("format_version") != CACHE_FORMAT_VERSION
            or "value" not in envelope
        ):
            self._record_miss(
                "format_mismatch",
                cache_file=cache_file,
                invalidated=True,
            )
            return None
        if envelope.get("namespace") != self.namespace:
            self._record_miss(
                "namespace_mismatch",
                cache_file=cache_file,
                invalidated=True,
            )
            return None
        if envelope.get("key") != key:
            self._record_miss(
                "key_mismatch",
                cache_file=cache_file,
                invalidated=True,
            )
            return None

        created_at = envelope.get("created_at")
        if self.max_age_seconds is not None:
            try:
                expired = self._clock() - float(created_at) > self.max_age_seconds
            except (TypeError, ValueError):
                expired = True
            if expired:
                self._record_miss(
                    "expired",
                    cache_file=cache_file,
                    invalidated=True,
                )
                return None

        self._metrics["hits"] += 1
        logger.debug("缓存命中: %s", key)
        return envelope["value"]

    def set(self, key: str, value: Any) -> None:
        """
        将结果存入缓存。

        Args:
            key: 缓存键
            value: 要缓存的结果
        """
        cache_file = self.cache_dir / f"{key}.pkl"
        temp_file = self.cache_dir / f".{key}.{uuid.uuid4().hex}.tmp"
        envelope = {
            "format_version": CACHE_FORMAT_VERSION,
            "namespace": self.namespace,
            "key": key,
            "created_at": self._clock(),
            "value": value,
        }

        try:
            with temp_file.open("xb") as handle:
                pickle.dump(envelope, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_file, cache_file)
            self._metrics["writes"] += 1
            logger.debug("缓存已保存: %s", key)
        except Exception as exc:
            self._metrics["write_errors"] += 1
            temp_file.unlink(missing_ok=True)
            logger.warning("缓存保存失败: key=%s, error=%s", key, exc)

    def clear(self) -> None:
        """清空所有缓存。"""
        deleted_count = 0
        for pattern in ("*.pkl", "*.tmp"):
            for cache_file in self.cache_dir.glob(pattern):
                try:
                    cache_file.unlink(missing_ok=True)
                    deleted_count += 1
                except OSError as exc:
                    logger.warning("缓存文件删除失败: file=%s, error=%s", cache_file, exc)
        if deleted_count:
            self._metrics["invalidations"] += deleted_count
            self._miss_reasons["cleared"] += deleted_count
        logger.info("缓存已清空: files=%s", deleted_count)

    def clean_old_cache(self, max_age_days: int = 7) -> int:
        """
        清理超过指定天数的缓存文件。

        Args:
            max_age_days: 最大缓存天数

        Returns:
            删除的文件数量
        """
        now = time.time()
        max_age_seconds = max_age_days * 86400  # 转换为秒
        deleted_count = 0

        for cache_file in self.cache_dir.glob("*.pkl"):
            try:
                file_age = now - cache_file.stat().st_mtime
                if file_age > max_age_seconds:
                    cache_file.unlink(missing_ok=True)
                    deleted_count += 1
                    logger.debug("删除过期缓存: %s", cache_file.name)
            except FileNotFoundError:
                continue

        if deleted_count > 0:
            self._metrics["invalidations"] += deleted_count
            self._miss_reasons["age_cleanup"] += deleted_count
            logger.info(f"清理了 {deleted_count} 个过期缓存文件（>{max_age_days}天）")

        return deleted_count

    def get_cache_stats(self) -> Dict[str, Any]:
        """
        获取缓存统计信息。

        Returns:
            包含缓存文件数量、总大小等信息的字典
        """
        cache_files = list(self.cache_dir.glob("*.pkl"))
        total_size = sum(f.stat().st_size for f in cache_files)

        return {
            "total_files": len(cache_files),
            "total_size_mb": total_size / (1024 * 1024),
            "cache_dir": str(self.cache_dir),
            **self.get_metrics(),
        }

    def get_metrics(self) -> Dict[str, Any]:
        """返回当前进程内可核对的命中、失效和读写统计。"""
        hits = int(self._metrics["hits"])
        misses = int(self._metrics["misses"])
        lookups = hits + misses
        return {
            "namespace": self.namespace,
            "hits": hits,
            "misses": misses,
            "writes": int(self._metrics["writes"]),
            "invalidations": int(self._metrics["invalidations"]),
            "read_errors": int(self._metrics["read_errors"]),
            "write_errors": int(self._metrics["write_errors"]),
            "hit_rate": round(hits / lookups, 4) if lookups else 0.0,
            "miss_reasons": dict(sorted(self._miss_reasons.items())),
        }
