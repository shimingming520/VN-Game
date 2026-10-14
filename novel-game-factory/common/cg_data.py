"""Authoritative CG contract loading with shared-storage fallback."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, Mapping, Optional

from sqlalchemy import text

from common.artifact_io import atomic_write_bytes, local_artifact_roots
from common.config import APP_TZ, OUTPUTS_PATH
from common.platform_db import platform_engine


logger = logging.getLogger(__name__)


class CgContractError(RuntimeError):
    """The CG contract is present but unreadable or structurally invalid."""


def _datetime_timestamp(value: Any) -> float:
    if isinstance(value, str) and value.strip():
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return 0.0
    if isinstance(value, datetime):
        # cg.update_time is a PostgreSQL TIMESTAMP without timezone and the
        # project writes it in APP_TZ.  Containers may themselves run in UTC,
        # so datetime.timestamp() must not infer the host timezone here.
        if value.tzinfo is None:
            value = value.replace(tzinfo=APP_TZ)
        return value.timestamp()
    return 0.0


def _decode(value: Any, *, label: str) -> Dict[str, Any]:
    if isinstance(value, (str, bytes, bytearray)):
        try:
            value = json.loads(value)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CgContractError(f"{label} 不是合法 JSON") from exc
    if not isinstance(value, Mapping):
        raise CgContractError(f"{label} 必须是 JSON object")
    return dict(value)


def _load_local_candidate(
    task_id: str,
) -> Optional[tuple[Dict[str, Any], Path, float]]:
    for root in local_artifact_roots():
        path = root / str(task_id) / "cg.json"
        if not path.is_file():
            continue
        try:
            payload = _decode(path.read_bytes(), label=str(path))
            modified_at = path.stat().st_mtime
        except (OSError, CgContractError) as exc:
            logger.warning("共享 CG 降级产物不可用: %s", exc)
            continue
        return payload, path, modified_at
    return None


def _load_database_candidate(
    task_id: str,
    *,
    db_engine,
) -> Optional[tuple[Dict[str, Any], Optional[float]]]:
    """Read cg.data and its comparable update timestamp when available.

    Some lightweight tests and legacy local schemas only expose ``data``.  A
    second connection is intentional: PostgreSQL marks the current transaction
    failed after an unknown-column error, so the compatibility query cannot be
    issued on that same connection.
    """
    try:
        with db_engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT data, update_time FROM cg "
                    "WHERE task_id = :tid LIMIT 1"
                ),
                {"tid": task_id},
            ).fetchone()
    except Exception as timestamp_query_error:
        try:
            with db_engine.connect() as connection:
                row = connection.execute(
                    text("SELECT data FROM cg WHERE task_id = :tid LIMIT 1"),
                    {"tid": task_id},
                ).fetchone()
        except Exception as data_query_error:
            raise data_query_error from timestamp_query_error
        if not row or row[0] is None:
            return None
        logger.warning(
            "cg.update_time 不可读取，按数据库优先兼容旧结构: %s",
            timestamp_query_error,
        )
        return _decode(row[0], label="cg.data"), None

    if not row or row[0] is None:
        return None
    return _decode(row[0], label="cg.data"), _datetime_timestamp(row[1])


def load_local_cg_contract(task_id: str) -> Optional[Dict[str, Any]]:
    """Load even an intentionally empty ``cg.json`` from shared local roots."""
    candidate = _load_local_candidate(task_id)
    if candidate is None:
        return None
    payload, path, _mtime = candidate
    logger.info("📁 已从共享降级产物加载 CG 合同: %s", path)
    return payload


def load_cg_contract(
    task_id: str,
    *,
    db_engine=platform_engine,
    required: bool = False,
) -> Dict[str, Any]:
    """Prefer ``cg.data`` and fall back to shared ``outputs/<task>/cg.json``."""
    database_error: Optional[Exception] = None
    local_candidate = _load_local_candidate(task_id)
    try:
        database_candidate = _load_database_candidate(
            task_id,
            db_engine=db_engine,
        )
        if database_candidate is not None:
            database_payload, database_timestamp = database_candidate
            if (
                database_timestamp is not None
                and local_candidate is not None
                and local_candidate[2] > database_timestamp + 0.001
            ):
                payload, path, _mtime = local_candidate
                logger.warning(
                    "共享 cg.json 比 cg.data 更新，使用降级合同: %s",
                    path,
                )
                return payload
            return database_payload
    except CgContractError:
        raise
    except Exception as exc:
        database_error = exc
        logger.warning("CG 数据库读取失败，尝试共享降级产物: %s", exc)

    if local_candidate is not None:
        payload, path, _mtime = local_candidate
        logger.info("📁 已从共享降级产物加载 CG 合同: %s", path)
        return payload
    if required:
        suffix = f"; database_error={database_error}" if database_error else ""
        raise CgContractError(
            f"任务 {task_id} 的 cg.data 和共享 cg.json 均不可用{suffix}"
        )
    return {}


def write_local_cg_contract(
    task_id: str,
    payload: Mapping[str, Any],
    *,
    outputs_root: str | Path = OUTPUTS_PATH,
) -> Path:
    target = Path(outputs_root).resolve() / str(task_id) / "cg.json"
    encoded = (
        json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")

    def _valid(path) -> bool:
        try:
            return isinstance(json.loads(Path(path).read_text(encoding="utf-8")), dict)
        except (OSError, json.JSONDecodeError):
            return False

    atomic_write_bytes(target, encoded, validator=_valid)
    logger.info("📁 CG 合同已原子落到共享降级产物: %s", target)
    return target


__all__ = [
    "CgContractError",
    "load_cg_contract",
    "load_local_cg_contract",
    "write_local_cg_contract",
]
