"""audio_producer 共享 PVC 上的幂等/断点状态。

状态文件只是一份索引，不能代替产物验收：调用方必须同时校验 input_digest、
status=success 和真实产物，三者都满足才允许跳过。
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import threading
import uuid
import wave


STATE_FILENAME = "audio-producer-resume.json"
SCHEMA_VERSION = 1
_thread_lock = threading.RLock()


def stable_digest(*values):
    digest = hashlib.sha256()
    for value in values:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def file_digest(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file_obj:
        for block in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def valid_json(path, predicate=None):
    try:
        if not os.path.isfile(path) or os.path.getsize(path) <= 0:
            return False
        with open(path, "r", encoding="utf-8") as file_obj:
            payload = json.load(file_obj)
    except (OSError, ValueError, TypeError):
        return False
    return bool(predicate(payload) if predicate else payload is not None)


def valid_text(path):
    try:
        return os.path.isfile(path) and bool(Path(path).read_text("utf-8").strip())
    except OSError:
        return False


def valid_wav(path):
    try:
        if not os.path.isfile(path) or os.path.getsize(path) <= 44:
            return False
        with wave.open(str(path), "rb") as audio:
            return (
                audio.getnchannels() > 0
                and audio.getframerate() > 0
                and audio.getnframes() > 0
                and audio.getsampwidth() > 0
            )
    except (OSError, EOFError, wave.Error):
        return False


def atomic_write_json(path, payload):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f"{destination.name}.{os.getpid()}.{threading.get_ident()}."
        f"{uuid.uuid4().hex}.part"
    )
    try:
        with temporary.open("w", encoding="utf-8") as file_obj:
            json.dump(payload, file_obj, ensure_ascii=False, indent=2)
            file_obj.flush()
            os.fsync(file_obj.fileno())
        if not valid_json(temporary):
            raise RuntimeError(f"JSON 临时产物校验失败：{temporary}")
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_write_text(path, content):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f"{destination.name}.{os.getpid()}.{threading.get_ident()}."
        f"{uuid.uuid4().hex}.part"
    )
    try:
        with temporary.open("w", encoding="utf-8") as file_obj:
            file_obj.write(content)
            file_obj.flush()
            os.fsync(file_obj.fileno())
        if not valid_text(temporary):
            raise RuntimeError(f"文本临时产物校验失败：{temporary}")
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class ResumeState:
    def __init__(self, task_root):
        self.task_root = Path(task_root)
        self.path = self.task_root / STATE_FILENAME
        self.lock_path = self.task_root / f"{STATE_FILENAME}.lock"

    @contextmanager
    def _locked(self):
        self.task_root.mkdir(parents=True, exist_ok=True)
        with _thread_lock:
            with self.lock_path.open("a+b") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _load_unlocked(self):
        try:
            with self.path.open("r", encoding="utf-8") as file_obj:
                state = json.load(file_obj)
        except (OSError, ValueError, TypeError):
            state = {}
        if not isinstance(state, dict) or state.get("schema_version") != SCHEMA_VERSION:
            state = {"schema_version": SCHEMA_VERSION, "steps": {}}
        state.setdefault("steps", {})
        return state

    def _save_unlocked(self, state):
        atomic_write_json(self.path, state)

    def get_item(self, step, item_key):
        with self._locked():
            state = self._load_unlocked()
            return dict(
                state.get("steps", {}).get(step, {}).get("items", {}).get(item_key, {})
            )

    def list_items(self, step):
        with self._locked():
            state = self._load_unlocked()
            items = state.get("steps", {}).get(step, {}).get("items", {})
            return {key: dict(value) for key, value in items.items()}

    def get_step(self, step):
        with self._locked():
            state = self._load_unlocked()
            return dict(state.get("steps", {}).get(step, {}))

    def step_matches(self, step, input_digest):
        step_state = self.get_step(step)
        return (
            step_state.get("status") == "success"
            and step_state.get("input_digest") == input_digest
        )

    def item_matches(self, step, item_key, input_digest):
        item = self.get_item(step, item_key)
        return item.get("status") == "success" and item.get("input_digest") == input_digest

    def mark_item(self, step, item_key, input_digest, *, status, artifacts=None, error=None):
        with self._locked():
            state = self._load_unlocked()
            step_state = state["steps"].setdefault(step, {"items": {}})
            step_state.setdefault("items", {})[item_key] = {
                "status": status,
                "input_digest": input_digest,
                "artifacts": list(artifacts or []),
                "error": str(error) if error else None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            self._save_unlocked(state)

    def mark_step(self, step, input_digest, *, status, artifacts=None, error=None):
        with self._locked():
            state = self._load_unlocked()
            previous_items = state["steps"].get(step, {}).get("items", {})
            state["steps"][step] = {
                "status": status,
                "input_digest": input_digest,
                "artifacts": list(artifacts or []),
                "error": str(error) if error else None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "execution_id": os.getenv("AUDIO_PRODUCER_EXECUTION_ID"),
                "attempt_id": os.getenv("AUDIO_PRODUCER_ATTEMPT_ID"),
                "items": previous_items,
            }
            self._save_unlocked(state)


class StepIncompleteError(RuntimeError):
    """有限重试后仍有缺失/失败资产；必须让模块最终状态变为失败。"""
