"""Persistent per-asset checkpoints for resumable visual generation.

The checkpoint row is the authority for reuse.  A file merely existing is not
enough: its input digest, generator version, declared outputs and recorded file
hashes must still match.  Rows live in the platform database so all Pods see the
same state; images themselves continue to live under the shared ``/storage``
output tree.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence

from sqlalchemy import text

from common.config import OUTPUTS_PATH
from common.platform_db import platform_engine
from common.stage_fencing import lock_and_validate_stage_fence_connection


STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
VALID_STATUSES = {STATUS_RUNNING, STATUS_SUCCESS, STATUS_FAILED}
STEP_ASSET_ID = "__step__"
CHECKPOINT_SCHEMA_VERSION = "visual-asset-checkpoint-v1"

_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY_ENGINES: set[int] = set()


class VisualCheckpointError(RuntimeError):
    """A checkpoint could not be read, validated or persisted safely."""


@dataclass(frozen=True)
class CheckpointRecord:
    task_id: str
    stage: str
    step_name: str
    asset_id: str
    status: str
    input_digest: str
    generator_version: str
    dependency_digest: str
    output_contract: Dict[str, Any]
    execution_id: str
    platform_attempt: int
    fencing_token: Optional[int]
    retry_count: int
    result_mode: str
    last_error: str


@dataclass(frozen=True)
class ReuseDecision:
    reusable: bool
    adopted: bool = False
    reason: str = ""


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(value[key])
            for key in sorted(value, key=lambda item: str(item))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, set):
        normalized = [_canonical(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        )
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, bytes):
        return {"sha256": hashlib.sha256(value).hexdigest(), "length": len(value)}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def canonical_digest(value: Any) -> str:
    payload = json.dumps(
        _canonical(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _table_name(engine) -> str:
    return (
        "visual_asset_checkpoints"
        if engine.dialect.name == "sqlite"
        else "public.visual_asset_checkpoints"
    )


def ensure_visual_checkpoint_schema(engine=platform_engine) -> None:
    key = id(engine)
    if key in _SCHEMA_READY_ENGINES:
        return
    with _SCHEMA_LOCK:
        if key in _SCHEMA_READY_ENGINES:
            return
        sqlite = engine.dialect.name == "sqlite"
        table = _table_name(engine)
        json_type = "TEXT" if sqlite else "JSONB"
        json_default = "'{}'" if sqlite else "'{}'::jsonb"
        with engine.begin() as connection:
            if not sqlite:
                connection.execute(
                    text("SELECT pg_advisory_xact_lock(1735357402, 20260825)")
                )
            connection.execute(text(f"""
                CREATE TABLE IF NOT EXISTS {table} (
                    task_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    step_name TEXT NOT NULL,
                    asset_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    input_digest TEXT NOT NULL DEFAULT '',
                    generator_version TEXT NOT NULL DEFAULT '',
                    dependency_digest TEXT NOT NULL DEFAULT '',
                    output_contract {json_type} NOT NULL DEFAULT {json_default},
                    execution_id TEXT NOT NULL DEFAULT '',
                    platform_attempt INTEGER NOT NULL DEFAULT 0,
                    fencing_token BIGINT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    result_mode TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    started_at TIMESTAMP,
                    completed_at TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (task_id, stage, step_name, asset_id)
                )
            """))
        _SCHEMA_READY_ENGINES.add(key)


def reset_visual_checkpoint_schema_cache_for_tests() -> None:
    with _SCHEMA_LOCK:
        _SCHEMA_READY_ENGINES.clear()


def _decode_json(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, (str, bytes, bytearray)):
        try:
            value = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise VisualCheckpointError("checkpoint output_contract is invalid JSON") from exc
    return dict(value) if isinstance(value, Mapping) else {}


def load_checkpoint(
    task_id: str,
    stage: str,
    step_name: str,
    asset_id: str,
    *,
    engine=platform_engine,
) -> Optional[CheckpointRecord]:
    ensure_visual_checkpoint_schema(engine)
    table = _table_name(engine)
    with engine.connect() as connection:
        row = connection.execute(
            text(f"""
                SELECT status, input_digest, generator_version,
                       dependency_digest, output_contract, execution_id,
                       platform_attempt, fencing_token, retry_count,
                       result_mode, last_error
                  FROM {table}
                 WHERE task_id = :task_id AND stage = :stage
                   AND step_name = :step_name AND asset_id = :asset_id
            """),
            {
                "task_id": task_id,
                "stage": stage,
                "step_name": step_name,
                "asset_id": asset_id,
            },
        ).first()
    if row is None:
        return None
    return CheckpointRecord(
        task_id=task_id,
        stage=stage,
        step_name=step_name,
        asset_id=asset_id,
        status=str(row[0] or ""),
        input_digest=str(row[1] or ""),
        generator_version=str(row[2] or ""),
        dependency_digest=str(row[3] or ""),
        output_contract=_decode_json(row[4]),
        execution_id=str(row[5] or ""),
        platform_attempt=int(row[6] or 0),
        fencing_token=int(row[7]) if row[7] is not None else None,
        retry_count=int(row[8] or 0),
        result_mode=str(row[9] or ""),
        last_error=str(row[10] or ""),
    )


def _effective_identity(task_id: str, stage: str):
    # Delayed import avoids making command-line utilities depend on Redis unless
    # they are actually running under a fenced stage execution.
    from common.progress_utils import effective_stage_identity

    return effective_stage_identity(task_id, stage)


def _execution_metadata() -> tuple[str, int]:
    execution_id = os.getenv("NOVEL_GAME_EXECUTION_ID", "").strip()
    attempt_raw = os.getenv("NOVEL_GAME_PLATFORM_ATTEMPT", "0").strip()
    if not execution_id:
        try:
            from celery import current_task

            request = getattr(current_task, "request", None)
            execution_id = str(getattr(request, "id", "") or "")
            headers = getattr(request, "headers", None) or {}
            if not attempt_raw or attempt_raw == "0":
                attempt_raw = str(headers.get("platform_attempt") or "0")
        except Exception:
            pass
    try:
        attempt = max(0, int(attempt_raw or 0))
    except ValueError:
        attempt = 0
    return execution_id, attempt


def _write_checkpoint(
    *,
    task_id: str,
    stage: str,
    step_name: str,
    asset_id: str,
    status: str,
    input_digest: str,
    generator_version: str,
    dependency_digest: str = "",
    output_contract: Optional[Mapping[str, Any]] = None,
    result_mode: str = "",
    last_error: str = "",
    increment_retry: bool = False,
    engine=platform_engine,
) -> None:
    if status not in VALID_STATUSES:
        raise ValueError(f"unsupported checkpoint status: {status}")
    from common.progress_utils import ensure_visual_stage_ownership

    ensure_visual_stage_ownership(task_id)
    ensure_visual_checkpoint_schema(engine)
    table = _table_name(engine)
    current_ref = table.rsplit(".", 1)[-1]
    identity = _effective_identity(task_id, stage)
    execution_id, platform_attempt = _execution_metadata()
    contract_json = json.dumps(dict(output_contract or {}), ensure_ascii=False)
    sqlite = engine.dialect.name == "sqlite"
    output_value = ":output_contract" if sqlite else "CAST(:output_contract AS JSONB)"
    retry_expression = (
        f"CASE WHEN {current_ref}.input_digest = excluded.input_digest "
        f"AND {current_ref}.generator_version = excluded.generator_version "
        f"AND {current_ref}.dependency_digest = excluded.dependency_digest "
        f"THEN {current_ref}.retry_count + 1 ELSE 1 END"
        if increment_retry
        else f"CASE WHEN {current_ref}.input_digest = excluded.input_digest "
        f"AND {current_ref}.generator_version = excluded.generator_version "
        f"AND {current_ref}.dependency_digest = excluded.dependency_digest "
        f"THEN {current_ref}.retry_count ELSE 0 END"
    )
    completed_at = "CURRENT_TIMESTAMP" if status in {STATUS_SUCCESS, STATUS_FAILED} else "NULL"
    with engine.begin() as connection:
        if identity is not None:
            lock_and_validate_stage_fence_connection(connection, identity)
        result = connection.execute(
            text(f"""
                INSERT INTO {table} (
                    task_id, stage, step_name, asset_id, status,
                    input_digest, generator_version, dependency_digest,
                    output_contract, execution_id, platform_attempt,
                    fencing_token, retry_count, result_mode, last_error,
                    started_at, completed_at, updated_at
                ) VALUES (
                    :task_id, :stage, :step_name, :asset_id, :status,
                    :input_digest, :generator_version, :dependency_digest,
                    {output_value}, :execution_id, :platform_attempt,
                    :fencing_token, :initial_retry_count, :result_mode,
                    :last_error, CURRENT_TIMESTAMP, {completed_at},
                    CURRENT_TIMESTAMP
                )
                ON CONFLICT (task_id, stage, step_name, asset_id) DO UPDATE SET
                    status = excluded.status,
                    input_digest = excluded.input_digest,
                    generator_version = excluded.generator_version,
                    dependency_digest = excluded.dependency_digest,
                    output_contract = excluded.output_contract,
                    execution_id = excluded.execution_id,
                    platform_attempt = excluded.platform_attempt,
                    fencing_token = excluded.fencing_token,
                    retry_count = {retry_expression},
                    result_mode = excluded.result_mode,
                    last_error = excluded.last_error,
                    started_at = CASE
                        WHEN excluded.status = 'running' THEN CURRENT_TIMESTAMP
                        ELSE {current_ref}.started_at
                    END,
                    completed_at = {completed_at},
                    updated_at = CURRENT_TIMESTAMP
            """),
            {
                "task_id": task_id,
                "stage": stage,
                "step_name": step_name,
                "asset_id": asset_id,
                "status": status,
                "input_digest": input_digest,
                "generator_version": generator_version,
                "dependency_digest": dependency_digest,
                "output_contract": contract_json,
                "execution_id": execution_id,
                "platform_attempt": platform_attempt,
                "fencing_token": identity.token if identity is not None else None,
                "initial_retry_count": 1 if increment_retry else 0,
                "result_mode": result_mode,
                "last_error": str(last_error or "")[:4000],
            },
        )
        if result.rowcount not in {-1, 1}:
            raise VisualCheckpointError(
                f"checkpoint upsert did not affect one row: task={task_id} "
                f"stage={stage} step={step_name} asset={asset_id}"
            )


def mark_running(
    task_id: str,
    stage: str,
    step_name: str,
    asset_id: str,
    *,
    input_digest: str,
    generator_version: str,
    dependency_digest: str = "",
    engine=platform_engine,
) -> None:
    _write_checkpoint(
        task_id=task_id,
        stage=stage,
        step_name=step_name,
        asset_id=asset_id,
        status=STATUS_RUNNING,
        input_digest=input_digest,
        generator_version=generator_version,
        dependency_digest=dependency_digest,
        engine=engine,
    )


def mark_failed(
    task_id: str,
    stage: str,
    step_name: str,
    asset_id: str,
    *,
    input_digest: str,
    generator_version: str,
    dependency_digest: str = "",
    error: str,
    engine=platform_engine,
) -> None:
    _write_checkpoint(
        task_id=task_id,
        stage=stage,
        step_name=step_name,
        asset_id=asset_id,
        status=STATUS_FAILED,
        input_digest=input_digest,
        generator_version=generator_version,
        dependency_digest=dependency_digest,
        last_error=error,
        increment_retry=True,
        engine=engine,
    )


def _task_root(task_id: str, outputs_root: str | Path = OUTPUTS_PATH) -> Path:
    root = Path(outputs_root).resolve()
    task_root = (root / task_id).resolve()
    try:
        task_root.relative_to(root)
    except ValueError as exc:
        raise VisualCheckpointError(f"unsafe task_id: {task_id!r}") from exc
    return task_root


def build_output_contract(
    task_id: str,
    paths: Iterable[str | Path],
    *,
    validator: Optional[Callable[[str | Path], bool]] = None,
    outputs_root: str | Path = OUTPUTS_PATH,
) -> Dict[str, Any]:
    task_root = _task_root(task_id, outputs_root)
    outputs = []
    for value in paths:
        path = Path(value).resolve()
        try:
            relative = path.relative_to(task_root).as_posix()
        except ValueError as exc:
            raise VisualCheckpointError(
                f"checkpoint output escapes task directory: {path}"
            ) from exc
        if not path.is_file() or path.stat().st_size <= 0:
            raise VisualCheckpointError(f"checkpoint output is missing or empty: {path}")
        if validator is not None and not validator(path):
            raise VisualCheckpointError(f"checkpoint output failed validation: {path}")
        outputs.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": file_sha256(path),
            }
        )
    outputs.sort(key=lambda item: item["path"])
    return {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "outputs": outputs,
    }


def mark_success(
    task_id: str,
    stage: str,
    step_name: str,
    asset_id: str,
    *,
    input_digest: str,
    generator_version: str,
    output_paths: Sequence[str | Path] = (),
    dependency_digest: str = "",
    validator: Optional[Callable[[str | Path], bool]] = None,
    result_mode: str = "generated",
    outputs_root: str | Path = OUTPUTS_PATH,
    engine=platform_engine,
) -> None:
    contract = build_output_contract(
        task_id,
        output_paths,
        validator=validator,
        outputs_root=outputs_root,
    )
    _write_checkpoint(
        task_id=task_id,
        stage=stage,
        step_name=step_name,
        asset_id=asset_id,
        status=STATUS_SUCCESS,
        input_digest=input_digest,
        generator_version=generator_version,
        dependency_digest=dependency_digest,
        output_contract=contract,
        result_mode=result_mode,
        engine=engine,
    )


def checkpoint_is_reusable(
    record: Optional[CheckpointRecord],
    *,
    task_id: str,
    input_digest: str,
    generator_version: str,
    output_paths: Sequence[str | Path] = (),
    dependency_digest: str = "",
    validator: Optional[Callable[[str | Path], bool]] = None,
    outputs_root: str | Path = OUTPUTS_PATH,
) -> ReuseDecision:
    if record is None:
        return ReuseDecision(False, reason="checkpoint_missing")
    if record.status != STATUS_SUCCESS:
        return ReuseDecision(False, reason=f"status_{record.status or 'empty'}")
    if record.input_digest != input_digest:
        return ReuseDecision(False, reason="input_digest_changed")
    if record.generator_version != generator_version:
        return ReuseDecision(False, reason="generator_version_changed")
    if record.dependency_digest != dependency_digest:
        return ReuseDecision(False, reason="dependency_digest_changed")
    contract = record.output_contract or {}
    if contract.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
        return ReuseDecision(False, reason="output_contract_version_changed")

    task_root = _task_root(task_id, outputs_root)
    expected = []
    for value in output_paths:
        path = Path(value).resolve()
        try:
            expected.append(path.relative_to(task_root).as_posix())
        except ValueError:
            return ReuseDecision(False, reason="unsafe_expected_output")
    expected.sort()
    recorded_outputs = contract.get("outputs") or []
    recorded_paths = sorted(
        str(item.get("path") or "")
        for item in recorded_outputs
        if isinstance(item, Mapping)
    )
    if expected != recorded_paths:
        return ReuseDecision(False, reason="output_set_changed")

    by_path = {
        str(item.get("path") or ""): item
        for item in recorded_outputs
        if isinstance(item, Mapping)
    }
    for relative in expected:
        path = task_root / relative
        metadata = by_path.get(relative) or {}
        try:
            if not path.is_file() or path.stat().st_size <= 0:
                return ReuseDecision(False, reason=f"output_missing:{relative}")
            if validator is not None and not validator(path):
                return ReuseDecision(False, reason=f"output_invalid:{relative}")
            if int(metadata.get("size") or -1) != path.stat().st_size:
                return ReuseDecision(False, reason=f"output_size_changed:{relative}")
            if str(metadata.get("sha256") or "") != file_sha256(path):
                return ReuseDecision(False, reason=f"output_hash_changed:{relative}")
        except OSError:
            return ReuseDecision(False, reason=f"output_unreadable:{relative}")
    return ReuseDecision(True, reason="checkpoint_match")


def reusable_or_adopt_existing(
    task_id: str,
    stage: str,
    step_name: str,
    asset_id: str,
    *,
    input_digest: str,
    generator_version: str,
    output_paths: Sequence[str | Path] = (),
    dependency_digest: str = "",
    validator: Optional[Callable[[str | Path], bool]] = None,
    allow_legacy_adoption: Optional[bool] = None,
    outputs_root: str | Path = OUTPUTS_PATH,
    engine=platform_engine,
) -> ReuseDecision:
    record = load_checkpoint(
        task_id, stage, step_name, asset_id, engine=engine
    )
    decision = checkpoint_is_reusable(
        record,
        task_id=task_id,
        input_digest=input_digest,
        generator_version=generator_version,
        output_paths=output_paths,
        dependency_digest=dependency_digest,
        validator=validator,
        outputs_root=outputs_root,
    )
    if decision.reusable:
        return decision

    if allow_legacy_adoption is None:
        allow_legacy_adoption = os.getenv(
            "VISUAL_RESUME_ADOPT_LEGACY", "0"
        ).strip().lower() in {"1", "true", "yes", "on"}
    if record is not None or not allow_legacy_adoption:
        return decision

    try:
        contract = build_output_contract(
            task_id,
            output_paths,
            validator=validator,
            outputs_root=outputs_root,
        )
    except VisualCheckpointError:
        return decision
    _write_checkpoint(
        task_id=task_id,
        stage=stage,
        step_name=step_name,
        asset_id=asset_id,
        status=STATUS_SUCCESS,
        input_digest=input_digest,
        generator_version=generator_version,
        dependency_digest=dependency_digest,
        output_contract=contract,
        result_mode="legacy_adopted",
        engine=engine,
    )
    return ReuseDecision(True, adopted=True, reason="legacy_adopted")


def checkpoint_retry_count(
    task_id: str,
    stage: str,
    step_name: str,
    asset_id: str,
    *,
    input_digest: str,
    generator_version: Optional[str] = None,
    dependency_digest: Optional[str] = None,
    engine=platform_engine,
) -> int:
    record = load_checkpoint(task_id, stage, step_name, asset_id, engine=engine)
    if record is None or record.input_digest != input_digest:
        return 0
    if generator_version is not None and record.generator_version != generator_version:
        return 0
    if dependency_digest is not None and record.dependency_digest != dependency_digest:
        return 0
    return record.retry_count


def assert_retry_budget(
    task_id: str,
    stage: str,
    step_name: str,
    asset_id: str,
    *,
    input_digest: str,
    generator_version: Optional[str] = None,
    dependency_digest: Optional[str] = None,
    limit: Optional[int] = None,
    engine=platform_engine,
) -> None:
    if limit is None:
        limit = max(1, int(os.getenv("VISUAL_ASSET_MAX_FAILED_ATTEMPTS", "8")))
    count = checkpoint_retry_count(
        task_id,
        stage,
        step_name,
        asset_id,
        input_digest=input_digest,
        generator_version=generator_version,
        dependency_digest=dependency_digest,
        engine=engine,
    )
    if count >= limit:
        raise VisualCheckpointError(
            f"asset retry budget exhausted: task={task_id} stage={stage} "
            f"step={step_name} asset={asset_id} failures={count} limit={limit}"
        )


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()
