"""Hot-update character casts directly inside a built game package."""

from __future__ import annotations

import json
import os
import re
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from common.story_tree_locale import select_story_tree_for_consumer


VALID_POSITIONS = {"left", "center", "right", "left_center", "right_center"}
SLOT_LEFT_PCT = {
    "left": 22,
    "left_center": 30,
    "center": 50,
    "right_center": 70,
    "right": 78,
}


class CastUpdateError(Exception):
    """Base class for expected cast-update failures."""


class CastValidationError(CastUpdateError, ValueError):
    """The request value does not follow the documented contract."""


class CastNotFoundError(CastUpdateError, FileNotFoundError):
    """The requested task, node, beat, or generated file does not exist."""


class CastArtifactError(CastUpdateError):
    """The built package exists but cannot be patched safely."""


@dataclass(frozen=True)
class CastUpdateResult:
    task_id: str
    requested_node_id: str
    source_node_id: str
    built_node_id: str
    beat_id: str
    character_position: str
    script_path: Path
    index_path: Optional[Path]
    service_worker_path: Optional[Path]


@dataclass(frozen=True)
class _FileChange:
    path: Path
    before: str
    after: str


_UPDATE_LOCK = threading.RLock()
_TOP_LEVEL_LABEL_RE = re.compile(r"(?m)^    ['\"](?P<label>[^'\"]+)['\"]\s*:\s*\[\s*$")
_CAST_CALL_RE = re.compile(r"StageManager\.setCast\(\[[^\n]*?\]\)")
_SW_VERSION_RE = re.compile(r"const\s+version\s*=\s*(['\"])[^'\"]*\1\s*;")
_INDEX_SCRIPT_RE = re.compile(
    r"(?P<prefix><script\b[^>]*?\bsrc\s*=\s*)(?P<quote>['\"])(?:\./)?js/script\.js"
    r"(?:\?[^'\"]*)?(?P=quote)",
    flags=re.IGNORECASE,
)
_MAIN_NODE_ID_RE = re.compile(r"node_id\s*:\s*(['\"])(?P<node_id>[^'\"]+)\1")


def normalize_resource_id(value: str, field_name: str) -> str:
    """Strip an identifier and reject values that could escape a storage root."""
    text = str(value).strip()
    if not text:
        raise CastValidationError(f"{field_name} 不能为空")
    if (
        text in {".", ".."}
        or "\x00" in text
        or "/" in text
        or "\\" in text
        or any(ch in text for ch in "*?[]")
    ):
        raise CastValidationError(f"{field_name} 包含非法路径字符")
    return text


def normalize_beat_id(value: str) -> str:
    text = normalize_resource_id(value, "beat_id")
    if not re.fullmatch(r"\d+(?:-[AB]\d+|-C)?", text):
        raise CastValidationError(f"beat_id 格式非法: {text}")
    return text


def _split_character_entries(body: str) -> List[str]:
    if body.rstrip().endswith(","):
        raise CastValidationError("character_position 存在空角色项")
    items: List[str] = []
    current: List[str] = []
    depth = 0
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                raise CastValidationError("character_position 括号不匹配")
        elif ch == "," and depth == 0:
            item = "".join(current).strip()
            if not item:
                raise CastValidationError("character_position 存在空角色项")
            items.append(item)
            current = []
            continue
        current.append(ch)
    if depth != 0:
        raise CastValidationError("character_position 括号不匹配")
    item = "".join(current).strip()
    if item:
        items.append(item)
    return items


def parse_character_position(value: str) -> List[Dict[str, object]]:
    """Validate and parse the documented ``character_position`` string."""
    if not isinstance(value, str):
        raise CastValidationError("character_position 必须是字符串")
    text = value.strip()
    if not (text.startswith("[") and text.endswith("]")):
        raise CastValidationError("character_position 必须使用 [...] 包裹")

    body = text[1:-1].strip()
    if not body:
        return []

    result: List[Dict[str, object]] = []
    for entry in _split_character_entries(body):
        match = re.fullmatch(r"(.+?)\(([^()]+)\)", entry)
        if not match:
            raise CastValidationError(
                f"character_position 条目格式非法: {entry}，应为 图片stem(站位)"
            )
        stem = match.group(1).strip()
        position = match.group(2).strip().lower()
        if not stem or any(ch in stem for ch in "[](),/\\\x00"):
            raise CastValidationError(f"立绘 stem 非法: {stem or entry}")
        shot_match = re.search(r"_(LS|MS|CS)$", stem, flags=re.IGNORECASE)
        if not shot_match:
            raise CastValidationError(f"立绘 stem 缺少景别后缀 _LS/_MS/_CS: {stem}")
        stem = stem[: shot_match.start(1)] + shot_match.group(1).upper()
        if position not in VALID_POSITIONS:
            valid = ", ".join(sorted(VALID_POSITIONS))
            raise CastValidationError(f"不支持的站位 {position}，只支持: {valid}")
        result.append(
            {
                "stem": stem,
                "slot": position,
                "left_pct": SLOT_LEFT_PCT[position],
                "key": stem.split("_", 1)[0],
            }
        )
    return result


def normalize_character_position(value: str) -> str:
    cast = parse_character_position(value)
    if not cast:
        return "[]"
    entries = [f"{item['stem']}({item['slot']})" for item in cast]
    return f"[{','.join(entries)}]"


def _cast_to_js(cast: Iterable[Dict[str, object]]) -> str:
    items = []
    for item in cast:
        items.append(
            "{stem:%s,slot:%s,leftPct:%d,key:%s}"
            % (
                json.dumps(item["stem"], ensure_ascii=False),
                json.dumps(item["slot"], ensure_ascii=False),
                int(item["left_pct"]),
                json.dumps(item["key"], ensure_ascii=False),
            )
        )
    return "[" + ",".join(items) + "]"


def _load_json(path: Path) -> Dict[str, object]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise CastNotFoundError(f"文件不存在: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CastArtifactError(f"JSON 文件损坏: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise CastArtifactError(f"JSON 顶层必须是对象: {path}")
    return data


def _load_package_story_tree(task_dir: Path) -> Dict[str, object]:
    path = task_dir / "story_tree.json"
    if not path.is_file():
        return {}
    data = _load_json(path)
    # New packages keep one complete story tree below each locale key.  Cast
    # hot updates only need the locale-invariant node/depth mapping, so select
    # the online default layer before reading ``nodes``.  The common selector
    # also preserves historical flat story trees unchanged.
    selected = select_story_tree_for_consumer(data, "en-US")
    return selected if isinstance(selected.get("nodes", {}), dict) else {}


def _built_folder_name(node_id: str, node_data: Dict[str, object]) -> str:
    depth = node_data.get("depth", 1)
    suffix = node_id[5:] if node_id.startswith("node_") else node_id
    return f"chapter_{depth}_{suffix}"


def _story_tree_mappings(story_tree: Dict[str, object]) -> Tuple[Dict[str, str], Dict[str, str]]:
    source_to_built: Dict[str, str] = {}
    built_to_source: Dict[str, str] = {}
    nodes = story_tree.get("nodes", {}) if story_tree else {}
    if not isinstance(nodes, dict):
        return source_to_built, built_to_source
    for source_node_id, raw_node_data in nodes.items():
        if not isinstance(source_node_id, str):
            continue
        node_data = raw_node_data if isinstance(raw_node_data, dict) else {}
        built_node_id = _built_folder_name(source_node_id, node_data)
        source_to_built[source_node_id] = built_node_id
        built_to_source[built_node_id] = source_node_id
    return source_to_built, built_to_source


def _source_node_from_main_js(script_path: Path) -> Optional[str]:
    main_js = script_path.with_name("main.js")
    if not main_js.is_file():
        return None
    try:
        content = main_js.read_text(encoding="utf-8")
    except OSError:
        return None
    match = _MAIN_NODE_ID_RE.search(content)
    return match.group("node_id") if match else None


def _resolve_built_script(task_dir: Path, requested_node_id: str) -> Tuple[Path, str, str]:
    story_tree = _load_package_story_tree(task_dir)
    source_to_built, built_to_source = _story_tree_mappings(story_tree)

    candidate_names: List[str] = []
    mapped_name = source_to_built.get(requested_node_id)
    for name in (requested_node_id, mapped_name):
        if name and name not in candidate_names:
            candidate_names.append(name)

    for built_node_id in candidate_names:
        script_path = task_dir / built_node_id / "js" / "script.js"
        if script_path.is_file():
            source_node_id = (
                built_to_source.get(built_node_id)
                or _source_node_from_main_js(script_path)
                or requested_node_id
            )
            return script_path, built_node_id, source_node_id

    source_node_id = built_to_source.get(requested_node_id, requested_node_id)
    suffix_matches = sorted(task_dir.glob(f"chapter_*_{source_node_id}/js/script.js"))
    if len(suffix_matches) == 1:
        script_path = suffix_matches[0]
        return script_path, script_path.parent.parent.name, source_node_id
    if len(suffix_matches) > 1:
        raise CastArtifactError(f"节点对应多个构建脚本，无法安全更新: {requested_node_id}")

    discovered = []
    for main_js in task_dir.glob("*/js/main.js"):
        try:
            content = main_js.read_text(encoding="utf-8")
        except OSError:
            continue
        if any(
            marker in content
            for marker in (f'node_id: "{source_node_id}"', f"node_id: '{source_node_id}'")
        ):
            script_path = main_js.with_name("script.js")
            if script_path.is_file():
                discovered.append(script_path)
    if len(discovered) == 1:
        script_path = discovered[0]
        return script_path, script_path.parent.parent.name, source_node_id
    if len(discovered) > 1:
        raise CastArtifactError(f"节点对应多个构建脚本，无法安全更新: {requested_node_id}")
    raise CastNotFoundError(f"节点构建脚本不存在: node_id={requested_node_id}")


def _replace_nth_cast_call(block: str, replacement: str, index: int) -> str:
    matches = list(_CAST_CALL_RE.finditer(block))
    if len(matches) <= index:
        raise CastArtifactError("构建脚本中缺少可热更新的 StageManager.setCast 调用")
    match = matches[index]
    return block[: match.start()] + f"StageManager.setCast({replacement})" + block[match.end() :]


def _patch_script(script: str, beat_id: str, cast_js: str) -> str:
    if not re.fullmatch(r"\d+", beat_id):
        raise CastArtifactError(f"当前构建脚本仅支持普通数字 beat 热更新: {beat_id}")

    labels = list(_TOP_LEVEL_LABEL_RE.finditer(script))
    target_label = f"Material_{int(beat_id)}"
    target_index = next(
        (idx for idx, match in enumerate(labels) if match.group("label") == target_label),
        None,
    )
    if target_index is None:
        raise CastNotFoundError(f"构建脚本中找不到 beat: {beat_id}")

    start = labels[target_index].start()
    end = labels[target_index + 1].start() if target_index + 1 < len(labels) else len(script)
    current_block = script[start:end]
    patched_current = _replace_nth_cast_call(current_block, cast_js, 0)
    patched = script[:start] + patched_current + script[end:]

    next_material_label = next(
        (
            match.group("label")
            for match in labels[target_index + 1 :]
            if match.group("label").startswith("Material_")
        ),
        None,
    )
    if next_material_label is None:
        return patched

    patched_labels = list(_TOP_LEVEL_LABEL_RE.finditer(patched))
    next_index = next(
        idx for idx, match in enumerate(patched_labels)
        if match.group("label") == next_material_label
    )
    next_start = patched_labels[next_index].start()
    next_end = (
        patched_labels[next_index + 1].start()
        if next_index + 1 < len(patched_labels)
        else len(patched)
    )
    next_block = patched[next_start:next_end]
    patched_next = _replace_nth_cast_call(next_block, cast_js, 1)
    return patched[:next_start] + patched_next + patched[next_end:]


def _patch_service_worker(content: str, prefix: str = "cast") -> str:
    if not _SW_VERSION_RE.search(content):
        return content
    version = f"{prefix}-{time.time_ns()}"
    return _SW_VERSION_RE.sub(f"const version = '{version}';", content, count=1)


def _patch_index_script_revision(content: str, revision: str) -> str:
    """给 script.js 引用追加版本号，让当前已激活的旧 Service Worker 也缓存未命中。

    仅修改 service-worker.js 不足以让第一次刷新立即生效：旧 worker 会先用旧缓存
    返回 js/script.js，新 worker 要到本次页面加载后才完成安装。index.html 本身通过
    带查询参数的 iframe 地址重新拉取，所以让它引用 ``script.js?v=...`` 后，旧缓存
    也找不到该 URL，只能从网络读取刚写入的新脚本。
    """
    return _INDEX_SCRIPT_RE.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('quote')}"
            f"js/script.js?v={revision}{match.group('quote')}"
        ),
        content,
        count=1,
    )


_CAST_ITEM_RE = re.compile(
    r"\{\s*stem\s*:\s*(?P<q1>['\"])(?P<stem>.*?)(?P=q1)\s*,"
    r"\s*slot\s*:\s*(?P<q2>['\"])(?P<slot>.*?)(?P=q2)"
)


def _setcast_js_to_character_position(cast_js: str) -> str:
    """把游戏包脚本里的 setCast JS 字面量还原成 character_position 字符串。

    输入形如 ``[{stem:"Ya_CS",slot:"left",leftPct:22,key:"Ya"}]``，
    输出 ``[Ya_CS(left)]``；空 cast ``[]`` → ``"[]"``，与写入时的规范化格式一致。
    """
    entries = [
        f"{m.group('stem')}({m.group('slot')})"
        for m in _CAST_ITEM_RE.finditer(cast_js)
    ]
    return f"[{','.join(entries)}]"


def read_cast(
    task_id: str,
    node_id: str,
    beat_id: str,
    built_games_path: str,
) -> str:
    """读取游戏包 script.js 中指定 beat 当前的立绘配置（character_position 字符串）。

    与 :func:`update_cast` 走同一套包/beat 定位逻辑，返回值格式与写入一致
    （``[stem(slot),...]`` / 空立绘 ``"[]"``）。找不到任务目录、节点脚本或 beat
    时抛 CastNotFoundError；脚本结构异常抛 CastArtifactError。调用方可据此回退读
    数据库。
    """
    task_id = normalize_resource_id(task_id, "task_id")
    node_id = normalize_resource_id(node_id, "node_id")
    beat_id = normalize_beat_id(beat_id)

    task_dir = Path(built_games_path) / task_id
    if not task_dir.is_dir():
        raise CastNotFoundError(f"已构建游戏目录不存在: {task_dir}")

    with _UPDATE_LOCK:
        script_path, _built_node_id, _source_node_id = _resolve_built_script(task_dir, node_id)
        try:
            script = script_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CastArtifactError(f"无法读取构建脚本: {script_path}: {exc}") from exc

        if not re.fullmatch(r"\d+", beat_id):
            raise CastArtifactError(f"当前构建脚本仅支持普通数字 beat 读取: {beat_id}")

        labels = list(_TOP_LEVEL_LABEL_RE.finditer(script))
        target_label = f"Material_{int(beat_id)}"
        target_index = next(
            (idx for idx, m in enumerate(labels) if m.group("label") == target_label),
            None,
        )
        if target_index is None:
            raise CastNotFoundError(f"构建脚本中找不到 beat: {beat_id}")

        start = labels[target_index].start()
        end = labels[target_index + 1].start() if target_index + 1 < len(labels) else len(script)
        block = script[start:end]
        matches = list(_CAST_CALL_RE.finditer(block))
        if not matches:
            raise CastArtifactError("构建脚本中缺少 StageManager.setCast 调用")
        # 第 0 个是 Apply（当前生效立绘），第 1 个是 Reverse（回退用）
        cast_js = matches[0].group(0)
        cast_js = cast_js[len("StageManager.setCast(") : -1]
        return _setcast_js_to_character_position(cast_js)


def _prepare_temp_file(change: _FileChange) -> Path:
    temp_path = change.path.with_name(f".{change.path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    mode = stat.S_IMODE(change.path.stat().st_mode)
    try:
        with temp_path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(change.after)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, mode)
        return temp_path
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _atomic_apply(
    changes: List[_FileChange],
    persist_callback: Optional[Callable[[], None]] = None,
) -> None:
    """Replace files atomically, then run ``persist_callback`` (the DB write).

    Files are replaced first; if ``persist_callback`` raises, every already
    replaced file is rolled back before the error propagates, so the game
    package and the database stay consistent (both change or neither does).
    """
    prepared: List[Tuple[_FileChange, Path]] = []
    replaced: List[_FileChange] = []
    try:
        prepared = [(change, _prepare_temp_file(change)) for change in changes if change.after != change.before]
        for change, temp_path in prepared:
            os.replace(temp_path, change.path)
            replaced.append(change)
        if persist_callback is not None:
            persist_callback()
    except Exception:
        for change in reversed(replaced):
            try:
                rollback = _FileChange(change.path, change.after, change.before)
                os.replace(_prepare_temp_file(rollback), change.path)
            except Exception:
                pass
        raise
    finally:
        for _, temp_path in prepared:
            temp_path.unlink(missing_ok=True)


def update_cast(
    task_id: str,
    node_id: str,
    beat_id: str,
    character_position: str,
    built_games_path: str,
    persist_callback: Optional[Callable[[str, str, str], None]] = None,
) -> CastUpdateResult:
    """Hot-update the built game package, then optionally mirror to a data store.

    The game package ``script.js`` is always rewritten first. ``persist_callback``
    (if given) is invoked afterwards with ``(source_node_id, beat_id,
    normalized_position)`` to mirror the change elsewhere (e.g. the ``ultimate``
    table). If it raises, all changed files are rolled back so the package and
    the store stay consistent. The callback is expected to no-op silently when
    the store has no matching task/node/beat (old tasks), so the package update
    still succeeds.
    """
    task_id = normalize_resource_id(task_id, "task_id")
    node_id = normalize_resource_id(node_id, "node_id")
    beat_id = normalize_beat_id(beat_id)
    cast = parse_character_position(character_position)
    normalized_position = normalize_character_position(character_position)
    cast_js = _cast_to_js(cast)

    task_dir = Path(built_games_path) / task_id
    if not task_dir.is_dir():
        raise CastNotFoundError(f"已构建游戏目录不存在: {task_dir}")

    with _UPDATE_LOCK:
        script_path, built_node_id, source_node_id = _resolve_built_script(task_dir, node_id)
        try:
            old_script = script_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CastArtifactError(f"无法读取构建脚本: {script_path}: {exc}") from exc
        new_script = _patch_script(old_script, beat_id, cast_js)

        changes = [_FileChange(script_path, old_script, new_script)]
        revision = f"cast-{time.time_ns()}"

        index_path = script_path.parent.parent / "index.html"
        if index_path.is_file():
            old_index = index_path.read_text(encoding="utf-8")
            new_index = _patch_index_script_revision(old_index, revision)
            changes.append(_FileChange(index_path, old_index, new_index))
        else:
            index_path = None

        service_worker_path = script_path.parent.parent / "service-worker.js"
        if service_worker_path.is_file():
            old_sw = service_worker_path.read_text(encoding="utf-8")
            changes.append(_FileChange(service_worker_path, old_sw, _patch_service_worker(old_sw)))
        else:
            service_worker_path = None

        callback = None
        if persist_callback is not None:
            callback = lambda: persist_callback(source_node_id, beat_id, normalized_position)
        _atomic_apply(changes, callback)
        return CastUpdateResult(
            task_id=task_id,
            requested_node_id=node_id,
            source_node_id=source_node_id,
            built_node_id=built_node_id,
            beat_id=beat_id,
            character_position=normalized_position,
            script_path=script_path,
            index_path=index_path,
            service_worker_path=service_worker_path,
        )
