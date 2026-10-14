import os
import re
import json
import asyncio
import hashlib
import random
import sys
import inspect
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from sqlalchemy import create_engine, text
from openai import AsyncOpenAI

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
utils_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "utils")
for p in (project_root, os.path.normpath(utils_dir)):
    if p not in sys.path:
        sys.path.insert(0, p)

from logger_config import setup_logger

logger = setup_logger("Step1_Sound", "step1_sound_extract.log")

from common.artifact_io import PathLike, atomic_write_bytes
from common.config import OUTPUTS_PATH, PLATFORM_DATABASE_URL
from common.db_pool import worker_pool_kwargs
from common.llm_fallback import ModelFallback
from common.model_json import (
    coerce_numeric_fields,
    parse_model_json as _parse_model_json,
)
from common.progress_utils import (
    StageLeaseLostError,
    ensure_stage_lease_owned,
)
from services.info_extractor.sound.bgm_prompt import (
    BGM_EXTRACTION_PROMPT_VERSION,
    build_bgm_system_prompt,
)
from services.info_extractor.sound.bgm_contract import (
    normalize_importance_fields,
    normalize_bgm_proposals,
)

try:
    from common.usage_tracker import tracker as _usage_tracker
    from common.usage_tracker import init_usage_table

    init_usage_table()
except ImportError:
    _usage_tracker = None
    logger.warning("usage tracker 模块不存在，计费追踪已禁用。")
except Exception as e:
    _usage_tracker = None
    logger.warning(f"usage tracker 初始化失败，计费追踪已禁用: {e}")

DB_URL = PLATFORM_DATABASE_URL  # 跨模块共用表已迁平台库(agent_platform)

API_KEY = os.getenv("GPT_IMAGE_API_KEY") or os.getenv("OPENAI_API_KEY")
assert API_KEY, "环境变量 GPT_IMAGE_API_KEY（或 OPENAI_API_KEY）未设置，请检查配置后重试。"

BASE_URL = os.getenv("GPT_IMAGE_BASE_URL", "http://sefutech-api.kube-test.svc:3000").rstrip("/")
if not BASE_URL.endswith("/v1"):
    BASE_URL = f"{BASE_URL}/v1"
MODEL_NAME = "gpt-5.6-sol"
# 主模型偶发不稳定时自动改用备选模型重发同一请求（同中转站、同调用参数）。
# 本步骤主模型已是 gpt-5.6-sol，备选自动落到 qwen3.7-max。
_model_fallback = ModelFallback(MODEL_NAME, name="Sound", logger=logger)

# 每个场景超过此行数时，强制按块切分后送给 SFX 提取，防止超长输入压垮模型
SFX_SCENE_CHUNK_SIZE = 50
CHECKPOINT_SCHEMA_VERSION = "sound_step15_checkpoint_v1"
SOUND_CONTRACT_VERSION = "sound_step15_units_v1"
# SFX 提取规则变更时，仅使 SFX 单元的 checkpoint 失效；BGM/Ambience 可继续复用。
SFX_PROMPT_VERSION = "sfx_prompt_v4_importance_calibration"
SOUND_TRACK_FIELDS = {
    "bgm": "bgm_events",
    "sfx": "sfx_events",
    "ambience": "ambience_events",
}
SoundUnitKey = tuple[str, str, str | None, int | None]
OwnerCheck = Callable[[], None]

# 数值字段：模型偶发把它们写成英文数词（例如 ``"bpm": "fifty"``）。
_NUMERIC_EVENT_FIELDS = ("bpm", "duration", "start_seq", "end_seq")


def coerce_numeric_event_fields(events, *, node_id, audio_type):
    """将模型输出中的数词/数字字符串归一化为数值字段。"""
    return coerce_numeric_fields(
        events,
        _NUMERIC_EVENT_FIELDS,
        context=f"{audio_type}/{node_id}",
        logger=logger,
    )


def parse_model_json(raw_content, *, node_id, audio_type):
    """解析模型 JSON；标准解析失败时使用公共 repair helper。"""
    return _parse_model_json(
        raw_content,
        context=f"{audio_type}/{node_id}",
        logger=logger,
    )


class SoundInputUnavailableError(RuntimeError):
    pass


class SoundUnitValidationError(RuntimeError):
    pass


class SoundUnitInputError(SoundUnitValidationError):
    """当前 SoundUnit 输入或内部映射不可用，不能降级成静音。"""


class SoundUnitOutputError(SoundUnitValidationError):
    """模型响应、事件字段或事件归属合同错误，可在可选音轨内降级。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.attempts: int | None = None


class SoundProviderRequestError(RuntimeError):
    """Provider 请求失败，在单元逻辑预算内耗尽后可降级。"""

    def __init__(
        self,
        message: str,
        original: Exception | None = None,
        *,
        attempts: int | None = None,
    ) -> None:
        super().__init__(message)
        self.original = original
        self.attempts = attempts


class SoundExtractionIncompleteError(RuntimeError):
    pass


class SoundCheckpointPersistenceError(RuntimeError):
    pass


class SoundPersistenceError(RuntimeError):
    pass


MAX_BGM_LOGICAL_ATTEMPTS = 3


@dataclass(frozen=True)
class BgmResponseContext:
    """有限诊断上下文，不保存或打印完整模型响应。"""

    used_model: str | None
    # 已经过 strip().lower()；None 表示网关没有提供，__unknown__ 表示
    # 空字符串或非字符串等无法可靠识别的值。
    finish_reason: str | None
    raw_content_length: int | None


@dataclass(frozen=True)
class BgmParsedResponse:
    events: list[object]
    repaired: bool


class BgmResponseContractError(SoundUnitOutputError):
    """模型已经响应，但响应结构不能满足 BGM JSON 合同。"""

    def __init__(
        self,
        message: str,
        details: list[dict] | None = None,
        *,
        context: BgmResponseContext | None = None,
        repaired: bool | None = None,
    ):
        super().__init__(message)
        self.details = details or []
        self.context = context
        self.repaired = repaired
        self.attempts: int | None = None


class BgmProviderRequestError(SoundProviderRequestError):
    """Provider 没有返回可诊断的 response，保留原始异常供最终传播。"""

    def __init__(
        self,
        message: str,
        original: Exception,
        *,
        attempts: int | None = None,
    ):
        super().__init__(message, original, attempts=attempts)


@dataclass(frozen=True)
class SoundUnit:
    key: SoundUnitKey
    audio_type: str
    node_id: str
    scene_id: str | None
    chunk_index: int | None
    request_scene_id: str | None
    mapped_beats: tuple[dict, ...]
    real_beat_ids: tuple[str, ...]
    input_digest: str


def _natural_key(value):
    return [
        int(part) if part.isdigit() else part
        for part in re.split(r"(\d+)", str(value))
    ]


def derive_request_scene_id(
    audio_type: str,
    scene_id: str | None,
    chunk_index: int | None,
) -> str | None:
    if audio_type == "bgm":
        if scene_id is not None or chunk_index is not None:
            raise ValueError("BGM 不应带 scene_id 或 chunk_index")
        return None
    if audio_type == "ambience":
        if not str(scene_id or "").strip() or chunk_index is not None:
            raise ValueError("Ambience 必须带 scene_id 且不能带 chunk_index")
        return None
    if audio_type != "sfx" or not str(scene_id or "").strip():
        raise ValueError("SFX 必须带非空逻辑 scene_id")
    if chunk_index is None:
        return str(scene_id)
    if (
        isinstance(chunk_index, bool)
        or not isinstance(chunk_index, int)
        or chunk_index < 0
    ):
        raise ValueError("SFX chunk_index 必须是非负整数")
    return f"{scene_id}_chunk{chunk_index}"


def serialize_unit_key(key: SoundUnitKey) -> str:
    return json.dumps(list(key), ensure_ascii=False, separators=(",", ":"))


def _parse_unit_key(value: str) -> SoundUnitKey:
    parsed = json.loads(value)
    if not isinstance(parsed, list) or len(parsed) != 4:
        raise ValueError("invalid sound unit key")
    audio_type, node_id, scene_id, chunk_index = parsed
    return audio_type, node_id, scene_id, chunk_index


def _unit_digest(
    *,
    audio_type: str,
    node_id: str,
    scene_id: str | None,
    chunk_index: int | None,
    request_scene_id: str | None,
    mapped_beats: list[dict],
    real_beat_ids: list[str],
) -> str:
    payload = {
        "contract_version": SOUND_CONTRACT_VERSION,
        "model": MODEL_NAME,
        "audio_type": audio_type,
        "node_id": node_id,
        "scene_id": scene_id,
        "chunk_index": chunk_index,
        "request_scene_id": request_scene_id,
        "mapped_beats": mapped_beats,
        "real_beat_ids": real_beat_ids,
    }
    if audio_type == "bgm":
        payload["prompt_version"] = BGM_EXTRACTION_PROMPT_VERSION
    elif audio_type == "sfx":
        payload["prompt_version"] = SFX_PROMPT_VERSION
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _make_sound_unit(
    audio_type: str,
    node_id: str,
    scene_id: str | None,
    chunk_index: int | None,
    beats: list[tuple[dict, str]],
) -> SoundUnit:
    request_scene_id = derive_request_scene_id(
        audio_type,
        scene_id,
        chunk_index,
    )
    mapped_beats = [dict(mapped) for mapped, _real_id in beats]
    real_beat_ids = [str(real_id) for _mapped, real_id in beats]
    key = (audio_type, str(node_id), scene_id, chunk_index)
    return SoundUnit(
        key=key,
        audio_type=audio_type,
        node_id=str(node_id),
        scene_id=scene_id,
        chunk_index=chunk_index,
        request_scene_id=request_scene_id,
        mapped_beats=tuple(mapped_beats),
        real_beat_ids=tuple(real_beat_ids),
        input_digest=_unit_digest(
            audio_type=audio_type,
            node_id=str(node_id),
            scene_id=scene_id,
            chunk_index=chunk_index,
            request_scene_id=request_scene_id,
            mapped_beats=mapped_beats,
            real_beat_ids=real_beat_ids,
        ),
    )


def _map_plots_data(plots_data: dict) -> dict[str, list[tuple[dict, str]]]:
    reason = _validate_plots_data(plots_data)
    if reason:
        raise SoundInputUnavailableError(reason)
    mapped: dict[str, list[tuple[dict, str]]] = {}
    for raw_node_id, raw_beats in plots_data.items():
        node_id = str(raw_node_id)
        valid: list[tuple[dict, str]] = []
        seq_id = 1
        for beat in raw_beats:
            real_beat_id = str(beat["beat_id"]).strip()
            if beat.get("beat_type") == "variable_update" or "-" in real_beat_id:
                continue
            beat_copy = dict(beat)
            beat_copy["beat_id"] = seq_id
            valid.append((beat_copy, real_beat_id))
            seq_id += 1
        mapped[node_id] = valid
    return mapped


def build_sound_units(
    plots_data: dict,
    ordered_node_ids: list[str],
) -> list[SoundUnit]:
    mapped_by_node = _map_plots_data(plots_data)
    ordered = [str(node_id) for node_id in ordered_node_ids]
    if set(ordered) != set(mapped_by_node) or len(ordered) != len(mapped_by_node):
        raise SoundInputUnavailableError(
            "ordered_node_ids 必须完整覆盖当前 plots Node 集合"
        )
    units: list[SoundUnit] = []
    for node_id in ordered:
        node_beats = mapped_by_node.get(str(node_id), [])
        if not node_beats:
            continue
        units.append(_make_sound_unit("bgm", str(node_id), None, None, node_beats))

        scene_groups: dict[str, list[tuple[dict, str]]] = {}
        for mapped_beat, real_id in node_beats:
            raw_scene_id = mapped_beat.get("scene_id")
            scene_text = str(raw_scene_id).strip() if raw_scene_id is not None else ""
            scene_id = scene_text or "default"
            scene_groups.setdefault(scene_id, []).append((mapped_beat, real_id))

        ordered_scenes = sorted(
            scene_groups.items(),
            key=lambda item: (
                int(item[1][0][0]["beat_id"]),
                item[0],
            ),
        )
        for scene_id, scene_beats in ordered_scenes:
            if len(scene_beats) > SFX_SCENE_CHUNK_SIZE:
                for offset in range(0, len(scene_beats), SFX_SCENE_CHUNK_SIZE):
                    chunk_index = offset // SFX_SCENE_CHUNK_SIZE
                    units.append(
                        _make_sound_unit(
                            "sfx",
                            str(node_id),
                            scene_id,
                            chunk_index,
                            scene_beats[offset: offset + SFX_SCENE_CHUNK_SIZE],
                        )
                    )
            else:
                units.append(
                    _make_sound_unit(
                        "sfx",
                        str(node_id),
                        scene_id,
                        None,
                        scene_beats,
                    )
                )

        ambience_groups: dict[str, list[tuple[dict, str]]] = {}
        for mapped_beat, real_id in node_beats:
            raw_scene_id = mapped_beat.get("scene_id")
            scene_text = str(raw_scene_id).strip() if raw_scene_id is not None else ""
            if not scene_text or scene_text.startswith("cg"):
                continue
            ambience_groups.setdefault(scene_text, []).append(
                (mapped_beat, real_id)
            )
        for scene_id, scene_beats in sorted(
            ambience_groups.items(),
            key=lambda item: (int(item[1][0][0]["beat_id"]), item[0]),
        ):
            units.append(
                _make_sound_unit(
                    "ambience",
                    str(node_id),
                    scene_id,
                    None,
                    scene_beats,
                )
            )
    return units


def resolve_node_order(
    current_node_ids,
    previous_sound: object = None,
    force: bool = False,
    warnings: Optional[list[str]] = None,
) -> list[str]:
    current = [str(node_id) for node_id in current_node_ids]
    if not force:
        previous_order = None
        if isinstance(previous_sound, dict):
            meta = previous_sound.get("_meta")
            previous_order = meta.get("node_order") if isinstance(meta, dict) else None
        if (
            isinstance(previous_order, list)
            and all(
                isinstance(item, str) and item.strip()
                for item in previous_order
            )
            and len(previous_order) == len(set(previous_order))
            and set(previous_order) == set(current)
        ):
            return list(previous_order)
        warning_code = "missing_or_invalid_sound_node_order"
        if warnings is not None:
            warnings.append(warning_code)
        logger.warning("%s: using natural node order", warning_code)
    return sorted(current, key=_natural_key)


def require_declared_events(parsed: object, field: str) -> list[object]:
    """Require an explicit track list in the provider JSON response."""
    if not isinstance(parsed, dict):
        raise SoundUnitValidationError("模型返回的 JSON 顶层不是对象")
    if field not in parsed or not isinstance(parsed[field], list):
        raise SoundUnitValidationError(f"缺少或非法轨道字段: {field}")
    return parsed[field]


def log_sound_gather_exception(*, audio_type, node_id, scene_id, error):
    """Keep the legacy logging helper for callers and diagnostics."""
    logger.error(
        "sound_extract_task_failed: audio_type=%s node_id=%s scene_id=%s error=%s",
        audio_type,
        node_id,
        scene_id,
        error,
    )


def _unit_local_map(unit: SoundUnit) -> dict[int, str]:
    if len(unit.mapped_beats) != len(unit.real_beat_ids):
        raise SoundUnitInputError("mapped beats 与真实 beat ID 数量不一致")
    mapping: dict[int, str] = {}
    for beat, real_id in zip(unit.mapped_beats, unit.real_beat_ids):
        try:
            seq = int(beat.get("beat_id"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise SoundUnitInputError("单元 beat 序号不可解析") from exc
        if seq in mapping or not str(real_id).strip():
            raise SoundUnitInputError("单元 beat 映射重复或真实 ID 为空")
        mapping[seq] = str(real_id)
    return mapping


def _parse_sfx_sequence(sfx_id: object) -> int:
    if not isinstance(sfx_id, str) or "_" not in sfx_id:
        raise SoundUnitValidationError("SFX ID 格式无效")
    suffix = sfx_id.rsplit("_", 1)[-1]
    if not suffix.isdigit():
        raise SoundUnitValidationError("SFX ID 未包含模型序号")
    return int(suffix)


def _parse_ambience_range(ambience_id: object) -> tuple[str, int, int]:
    if not isinstance(ambience_id, str):
        raise SoundUnitValidationError("Ambience ID 格式无效")
    match = re.match(r"^(.+)_(\d+)-(\d+)$", ambience_id)
    if not match:
        raise SoundUnitValidationError("Ambience ID 未包含起止序号")
    start, end = int(match.group(2)), int(match.group(3))
    if start > end:
        raise SoundUnitValidationError("Ambience 起止序号无效")
    return match.group(1), start, end


def _event_id_field(audio_type: str) -> str:
    return f"{audio_type}_id"


def _range_belongs_to_unit(start: int, end: int, valid_sequences: set[int]) -> bool:
    """Require every local sequence in a declared range to belong to the unit."""
    return (
        start <= end
        and start in valid_sequences
        and end in valid_sequences
        and set(range(start, end + 1)).issubset(valid_sequences)
    )


def _event_name(
    event: dict,
    event_id: object,
    field: str,
    suffix: str,
    *,
    default: str | None = None,
) -> str:
    value = event.get(field)
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(event_id, str):
        value = (
            event_id[: -len(suffix)]
            if suffix and event_id.endswith(suffix)
            else event_id.rsplit("_", 1)[0]
        )
        if value:
            return value
    if default:
        return default
    raise SoundUnitValidationError(f"{field} 名称为空")


def _validate_and_materialize_event(
    event: object,
    unit: SoundUnit,
) -> dict:
    if not isinstance(event, dict):
        raise SoundUnitValidationError("事件不是对象")
    local_map = _unit_local_map(unit)
    valid_sequences = set(local_map)
    materialized = dict(event)

    if unit.audio_type == "bgm":
        try:
            start = int(event.get("start_seq"))
            end = int(event.get("end_seq"))
        except (TypeError, ValueError):
            # Normalized BGM proposals retain start_seq/end_seq in their model
            # object; an already materialized event uses start_order/coverage.
            try:
                start = int(event["start_order"])
                end = start + int(event["beat_coverage"]) - 1
            except (KeyError, TypeError, ValueError) as second_exc:
                raise SoundUnitValidationError("BGM 起止序号无效") from second_exc
        if not _range_belongs_to_unit(start, end, valid_sequences):
            raise SoundUnitValidationError("BGM 范围超出当前 Node")
        materialized["start_order"] = start
        materialized["beat_coverage"] = end - start + 1
        materialized["start_beat_id"] = local_map[start]
        materialized["end_beat_id"] = local_map[end]
        materialized.pop("start_seq", None)
        materialized.pop("end_seq", None)
        event_id = event.get("bgm_id")
        suffix = f"_{local_map[start]}-{local_map[end]}"
        name = _event_name(
            materialized,
            event_id,
            "bgm_name",
            suffix,
            default=f"bgm_moment_{start}",
        )
        materialized["bgm_name"] = name
        materialized["bgm_id"] = f"{name}_{local_map[start]}-{local_map[end]}"
        return materialized

    if unit.audio_type == "sfx":
        event_id_key = _event_id_field(unit.audio_type)
        event_id = event.get(event_id_key)
        if not isinstance(event_id, str) or not event_id.strip():
            raise SoundUnitValidationError(f"事件缺少非空 {event_id_key}")
        has_materialized_beat = event.get("start_beat_id") is not None
        sequence = event.get("start_order") if has_materialized_beat else None
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            sequence = _parse_sfx_sequence(event_id)
        if sequence not in valid_sequences:
            raise SoundUnitValidationError("SFX beat 不属于当前 scene/chunk")
        if event.get("start_beat_id") is not None and str(
            event["start_beat_id"]
        ) != local_map[sequence]:
            raise SoundUnitValidationError("SFX 起始 beat 映射不一致")
        # Raw provider IDs end in the unit-local numeric sequence.  Once an
        # event has been materialized, its suffix is the real beat ID, which
        # may itself end in digits (for example ``real_1``); in that case the
        # explicit start_beat_id is the authoritative mapping proof.
        suffix = str(event_id).rsplit("_", 1)[-1]
        if has_materialized_beat:
            if not event_id.endswith(f"_{local_map[sequence]}"):
                raise SoundUnitValidationError("SFX ID 与当前 beat 映射不一致")
        elif not suffix.isdigit() or int(suffix) != sequence:
            raise SoundUnitValidationError("SFX ID 无法证明当前 beat 归属")
        name = _event_name(
            event,
            event_id,
            "sfx_name",
            f"_{local_map[sequence]}",
        )
        materialized["sfx_id"] = f"{name}_{local_map[sequence]}"
        materialized["start_order"] = sequence
        materialized["beat_coverage"] = 1
        materialized["start_beat_id"] = local_map[sequence]
        return materialized

    event_id_key = _event_id_field(unit.audio_type)
    event_id = event.get(event_id_key)
    if not isinstance(event_id, str) or not event_id.strip():
        raise SoundUnitValidationError(f"事件缺少非空 {event_id_key}")
    has_materialized_beats = (
        event.get("start_beat_id") is not None
        or event.get("end_beat_id") is not None
    )
    has_schedule = (
        has_materialized_beats
        and isinstance(event.get("start_order"), int)
        and not isinstance(event.get("start_order"), bool)
        and isinstance(event.get("beat_coverage"), int)
        and not isinstance(event.get("beat_coverage"), bool)
        and event["beat_coverage"] > 0
    )
    if has_schedule:
        start = int(event["start_order"])
        end = start + int(event["beat_coverage"]) - 1
        if not _range_belongs_to_unit(start, end, valid_sequences):
            raise SoundUnitValidationError("Ambience 范围不属于当前 scene")
        expected_suffix = f"_{local_map[start]}-{local_map[end]}"
        id_proves_range = event_id.endswith(expected_suffix)
        parsed_name = parsed_start = parsed_end = None
        try:
            parsed_name, parsed_start, parsed_end = _parse_ambience_range(event_id)
        except SoundUnitValidationError:
            if not id_proves_range:
                raise SoundUnitValidationError("Ambience ID 无法证明当前范围")
        else:
            if not id_proves_range and (parsed_start != start or parsed_end != end):
                raise SoundUnitValidationError(
                    "Ambience ID 范围与 start_order/beat_coverage 不一致"
                )
        name = _event_name(
            event,
            event_id,
            "ambience_name",
            expected_suffix,
            default=parsed_name,
        )
    else:
        name, start, end = _parse_ambience_range(event_id)
        expected_suffix = f"_{local_map.get(start, '')}-{local_map.get(end, '')}"
        if not name:
            name = _event_name(event, event_id, "ambience_name", expected_suffix)
    if not _range_belongs_to_unit(start, end, valid_sequences):
        raise SoundUnitValidationError("Ambience 范围不属于当前 scene")
    if event.get("start_beat_id") is not None and str(event["start_beat_id"]) != local_map[start]:
        raise SoundUnitValidationError("Ambience 起始 beat 映射不一致")
    if event.get("end_beat_id") is not None and str(event["end_beat_id"]) != local_map[end]:
        raise SoundUnitValidationError("Ambience 结束 beat 映射不一致")
    materialized["ambience_id"] = f"{name}_{local_map[start]}-{local_map[end]}"
    materialized["start_order"] = start
    materialized["beat_coverage"] = end - start + 1
    materialized["start_beat_id"] = local_map[start]
    materialized["end_beat_id"] = local_map[end]
    return materialized


def validate_unit_events(events: object, unit: SoundUnit) -> list[dict]:
    """Validate and materialize all events for one unit, without silent drops."""
    if not isinstance(events, list):
        raise SoundUnitValidationError("事件列表不是数组")
    if not events:
        return []

    if unit.audio_type == "bgm":
        # BGM generator 已经完成一次、且仅一次 proposal 合同归一化。
        # 这里只做当前 Node 的本地范围映射和最终物化，避免再次丢弃或
        # 重复记录 proposal 级诊断。
        return [
            _validate_and_materialize_event(event, unit)
            for event in events
        ]

    if unit.audio_type in {"sfx", "ambience"}:
        valid = filter_sound_event_dicts(
            events,
            node_id=unit.node_id,
            scene_id=unit.scene_id,
            audio_type=unit.audio_type,
        )
        if not valid:
            raise SoundUnitValidationError("非空列表没有合法事件对象")
    else:
        valid = events
    materialized = [_validate_and_materialize_event(event, unit) for event in valid]
    if unit.audio_type == "sfx":
        materialized = filter_human_voice_sfx(unit.scene_id, materialized)
        materialized = normalize_event_importance_list(
            materialized,
            node_id=unit.node_id,
            audio_type=unit.audio_type,
        )
        materialized.sort(key=lambda item: int(item.get("start_order", 0)))
        deduped: list[dict] = []
        seen_orders: set[int] = set()
        for event in materialized:
            start_order = int(event["start_order"])
            if start_order in seen_orders:
                logger.warning(
                    "duplicate_sfx_beat: node_id=%s scene_id=%s beat_id=%s",
                    unit.node_id,
                    unit.scene_id,
                    event.get("start_beat_id"),
                )
                continue
            seen_orders.add(start_order)
            deduped.append(event)
        return deduped
    if unit.audio_type == "ambience":
        return normalize_event_importance_list(
            materialized,
            node_id=unit.node_id,
            audio_type=unit.audio_type,
        )
    raise SoundUnitInputError(f"未知音轨类型: {unit.audio_type}")

# SFX 轨绝不合成人声（人声由 TTS 配音轨负责）。模型偶发违规时用这层做确定性兜底：
# 命中即整条丢弃，等价于"该 beat 直接跳过"。
# 只收录几乎不可能出现在非人声音效里的词——像 hum / groan / roar / screech 这类
# 同时用于木料、风、金属、野兽的词故意不收，避免误杀正常音效。
HUMAN_VOICE_KEYWORDS = (
    "voice", "voices", "vocal", "vocals", "vocalization", "vocalisation", "vocalizing",
    "speech", "speak", "speaks", "speaking", "spoken", "talking", "saying",
    "shout", "shouts", "shouting", "shouted", "yell", "yells", "yelling",
    "scream", "screams", "screaming", "screamed",
    "chant", "chants", "chanting", "chanted", "incantation", "incantations", "mantra",
    "syllable", "syllables", "utterance", "utterances",
    "whisper", "whispers", "whispering", "murmur", "murmurs", "murmuring",
    "mutter", "mutters", "muttering", "chatter", "chattering",
    "sing", "sings", "singing", "sung", "song", "choir", "chorus", "cappella", "lyrics",
    "laugh", "laughs", "laughing", "laughter", "giggle", "chuckle",
    "sob", "sobs", "sobbing", "weep", "weeping", "cry", "cries", "crying",
    "gasp", "gasps", "gasping", "pant", "panting", "breath", "breathing", "breathe",
    "sigh", "sighs", "sighing", "cough", "coughing", "sneeze",
    "crowd", "crowds", "walla", "cheer", "cheers", "cheering", "chorused",
    "dialogue", "narration", "verbal",
)
HUMAN_VOICE_PATTERN = re.compile(
    r"\b(?:%s)\b" % "|".join(HUMAN_VOICE_KEYWORDS), re.IGNORECASE
)


def filter_human_voice_sfx(scene_id, sfx_events):
    """丢弃任何涉及人声的 SFX：人声归 TTS 轨，SFX 轨合成人声只会得到诡异叠音。"""
    kept = []
    for sfx in sfx_events:
        haystack = " ".join(
            str(sfx.get(k, "") or "") for k in ("sfx_name", "event", "prompt")
        )
        hit = HUMAN_VOICE_PATTERN.search(haystack)
        if hit:
            logger.warning(
                f"🔇 场景 {scene_id} 命中人声词 `{hit.group(0)}`，已跳过该 beat 的 SFX: "
                f"{sfx.get('sfx_id')} | {sfx.get('event')}"
            )
            continue
        kept.append(sfx)
    return kept


def filter_sound_event_dicts(
    events,
    *,
    node_id,
    scene_id,
    audio_type,
):
    """只保留模型事件数组中的 JSON 对象，避免异常项进入字段后处理。"""
    if not isinstance(events, list):
        logger.warning(
            "invalid_sound_event_list: audio_type=%s node_id=%s "
            "scene_id=%s item_type=%s",
            audio_type,
            node_id,
            scene_id,
            type(events).__name__,
        )
        return []

    filtered = []
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            logger.warning(
                "invalid_sound_event_item: audio_type=%s node_id=%s "
                "scene_id=%s index=%s item_type=%s",
                audio_type,
                node_id,
                scene_id,
                index,
                type(event).__name__,
            )
            continue
        filtered.append(event)
    return filtered


def normalize_event_importance_list(events, *, node_id, audio_type):
    normalized_events = []
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict):
            normalized_events.append(event)
            continue
        normalized, warned = normalize_importance_fields(event)
        if warned:
            logger.warning(
                "invalid_sound_importance: node_id=%s audio_type=%s event_id=%s",
                node_id,
                audio_type,
                event.get(f"{audio_type}_id", ""),
            )
        normalized_events.append(normalized)
    return normalized_events


def materialize_sfx_event(event, seq_to_real_map):
    """Legacy standalone materializer retained for existing Info tests/callers."""
    materialized = dict(event)
    sfx_id = str(materialized.get("sfx_id", ""))
    if "_" not in sfx_id:
        return materialized
    name, suffix = sfx_id.rsplit("_", 1)
    if not suffix.isdigit():
        raise ValueError("SFX ID 末段不是顺序数字")
    sequence = int(suffix)
    real_id = seq_to_real_map.get(sequence)
    if real_id is None:
        raise KeyError(sequence)
    materialized["sfx_id"] = f"{name}_{real_id}"
    materialized["start_order"] = sequence
    materialized["beat_coverage"] = 1
    return materialized


def attach_sound_metadata(sound_data, node_order, warnings):
    ordered_node_ids = [str(node_id) for node_id in node_order]
    sound_data["_meta"] = {
        "schema_version": "sound_v2",
        "prompt_version": "bgm_prompt_v3_three_mode",
        "node_order": ordered_node_ids,
        "warnings": [
            warning.model_dump()
            if hasattr(warning, "model_dump")
            else warning
            for warning in warnings
        ],
    }
    return sound_data


def _decode_json_value(value: object) -> object:
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        return json.loads(value)
    return value


def _validate_plots_data(data: object) -> Optional[str]:
    if not isinstance(data, dict) or not data:
        return "plots 顶层必须是非空对象"
    for node_id, beats in data.items():
        if not isinstance(node_id, str) or not node_id.strip():
            return "plots 包含空 Node ID"
        if node_id == "_meta":
            return "plots 顶层包含保留元数据键 _meta"
        if not isinstance(beats, list):
            return f"Node {node_id} 的 beats 不是列表"
        for beat in beats:
            if not isinstance(beat, dict):
                return f"Node {node_id} 包含非对象 beat"
            raw_beat_id = beat.get("beat_id")
            if raw_beat_id is None or (
                isinstance(raw_beat_id, str) and not raw_beat_id.strip()
            ):
                return f"Node {node_id} 包含空 beat_id"
    return None


def validate_public_sound_data(
    data: object,
    expected_node_ids: Optional[set[str]] = None,
) -> Optional[str]:
    """Return a reason when public sound data is not a complete valid payload."""
    if not isinstance(data, dict):
        return "sound 顶层不是对象"
    meta = data.get("_meta")
    if not isinstance(meta, dict):
        return "sound._meta 必须是对象"
    node_ids = [key for key in data if key != "_meta"]
    if not node_ids:
        return "sound 缺少 Node"
    node_order = meta.get("node_order")
    if node_order is not None:
        if (
            not isinstance(node_order, list)
            or not node_order
            or any(not isinstance(node_id, str) or not node_id.strip() for node_id in node_order)
            or len(node_order) != len(set(node_order))
            or set(node_order) != set(node_ids)
        ):
            return "sound._meta.node_order 与 Node 集合不一致"
    if expected_node_ids is not None and set(node_ids) != set(expected_node_ids):
        return "sound Node 集合与当前 plots 不一致"
    for node_id in node_ids:
        node = data[node_id]
        if not isinstance(node, dict):
            return f"Node {node_id} 不是对象"
        for field in ("bgm_events", "sfx_events", "ambience_events"):
            events = node.get(field)
            if not isinstance(events, list):
                return f"Node {node_id} 缺少或非法 {field}"
            event_id_field = field[:-7] + "_id"
            for event in events:
                if not isinstance(event, dict):
                    return f"Node {node_id} 的 {field} 包含非对象事件"
                if not isinstance(event.get(event_id_field), str) or not event.get(event_id_field).strip():
                    return f"Node {node_id} 的 {field} 包含空 {event_id_field}"
    return None


def _checkpoint_unit_payload(
    unit: SoundUnit,
    status: str,
    events: list[dict],
    *,
    attempts: int = 0,
    failure_kind: str | None = None,
    last_error: str | None = None,
) -> dict:
    payload = {
        "audio_type": unit.audio_type,
        "node_id": unit.node_id,
        "scene_id": unit.scene_id,
        "chunk_index": unit.chunk_index,
        "request_scene_id": unit.request_scene_id,
        "input_digest": unit.input_digest,
        "status": status,
        "events": events if status == "complete" else [],
    }
    if status == "failed":
        try:
            normalized_attempts = int(attempts)
        except (TypeError, ValueError):
            normalized_attempts = 0
        payload["attempts"] = max(0, normalized_attempts)
        payload["failure_kind"] = failure_kind or "retry_required"
        if last_error:
            payload["last_error"] = str(last_error)[:500]
    return payload


def _checkpoint_attempts(record: object) -> int:
    if not isinstance(record, dict):
        return 0
    value = record.get("attempts", 0)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


def empty_checkpoint(task_id: str) -> dict:
    return {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "contract_version": SOUND_CONTRACT_VERSION,
        "task_id": str(task_id),
        "status": "in_progress",
        "units": {},
    }


def _validate_checkpoint_unit_shape(key: SoundUnitKey, record: object) -> bool:
    if not isinstance(record, dict):
        return False
    if not {
        "audio_type", "node_id", "scene_id", "chunk_index",
        "request_scene_id", "input_digest", "status", "events",
    }.issubset(record):
        return False
    try:
        audio_type, node_id, scene_id, chunk_index = key
        if (
            audio_type not in SOUND_TRACK_FIELDS
            or not isinstance(node_id, str)
            or not node_id
            or (
                record["audio_type"],
                record["node_id"],
                record["scene_id"],
                record["chunk_index"],
            ) != key
            or record["request_scene_id"]
            != derive_request_scene_id(audio_type, scene_id, chunk_index)
        ):
            return False
        if not isinstance(record["input_digest"], str) or not record["input_digest"]:
            return False
        status = record["status"]
        events = record["events"]
        if status not in {"complete", "failed"} or not isinstance(events, list):
            return False
        if "attempts" in record:
            attempts = record["attempts"]
            if (
                isinstance(attempts, bool)
                or not isinstance(attempts, int)
                or attempts < 0
            ):
                return False
        if "failure_kind" in record:
            failure_kind = record["failure_kind"]
            if (
                not isinstance(failure_kind, str)
                or not failure_kind.strip()
                or len(failure_kind) > 120
            ):
                return False
        if "last_error" in record:
            last_error = record["last_error"]
            if not isinstance(last_error, str) or len(last_error) > 500:
                return False
        if status == "failed" and events:
            return False
        if status == "complete":
            event_id_field = _event_id_field(audio_type)
            if any(
                not isinstance(event, dict)
                or not isinstance(event.get(event_id_field), str)
                or not event[event_id_field].strip()
                for event in events
            ):
                return False
        return True
    except (TypeError, ValueError, json.JSONDecodeError):
        return False


def _validate_checkpoint_shape(data: object, task_id: str) -> bool:
    if not isinstance(data, dict):
        return False
    if (
        data.get("schema_version") != CHECKPOINT_SCHEMA_VERSION
        or data.get("contract_version") != SOUND_CONTRACT_VERSION
        or str(data.get("task_id")) != str(task_id)
        or data.get("status") not in {"in_progress", "completed"}
        or not isinstance(data.get("units"), dict)
    ):
        return False
    for key_text, record in data["units"].items():
        try:
            key = _parse_unit_key(key_text)
        except (ValueError, TypeError, json.JSONDecodeError):
            return False
        if not _validate_checkpoint_unit_shape(key, record):
            return False
    return True


def _validate_checkpoint_root_shape(data: object, task_id: str) -> bool:
    """Validate only the checkpoint envelope, leaving individual units recoverable."""
    return (
        isinstance(data, dict)
        and data.get("schema_version") == CHECKPOINT_SCHEMA_VERSION
        and data.get("contract_version") == SOUND_CONTRACT_VERSION
        and str(data.get("task_id")) == str(task_id)
        and data.get("status") in {"in_progress", "completed"}
        and isinstance(data.get("units"), dict)
    )


def _event_belongs_to_unit(event: dict, unit: SoundUnit) -> bool:
    if not isinstance(event, dict):
        return False
    try:
        materialized = _validate_and_materialize_event(event, unit)
        fields = [
            _event_id_field(unit.audio_type),
            "start_order",
            "beat_coverage",
            "start_beat_id",
        ]
        if unit.audio_type != "sfx":
            fields.append("end_beat_id")
        return all(event.get(field) == materialized.get(field) for field in fields)
    except (KeyError, TypeError, ValueError, SoundUnitValidationError):
        return False


def is_checkpoint_unit_reusable(record: object, unit: SoundUnit) -> bool:
    if not _validate_checkpoint_unit_shape(unit.key, record):
        return False
    if record["status"] != "complete" or record["input_digest"] != unit.input_digest:
        return False
    return all(_event_belongs_to_unit(event, unit) for event in record["events"])


def _checkpoint_path(task_id: str) -> Path:
    return Path(OUTPUTS_PATH) / str(task_id) / "sound.step15.checkpoint.json"


def load_sound_checkpoint(task_id: str) -> dict:
    path = _checkpoint_path(task_id)
    if not path.is_file():
        return empty_checkpoint(task_id)
    try:
        data = _decode_json_value(path.read_bytes())
        if not _validate_checkpoint_root_shape(data, task_id):
            raise ValueError("checkpoint 根结构不兼容")
        valid_units: dict[str, dict] = {}
        invalid_count = 0
        for key_text, record in data["units"].items():
            try:
                key = _parse_unit_key(key_text)
            except (ValueError, TypeError, json.JSONDecodeError):
                invalid_count += 1
                logger.warning(
                    "invalid_sound_checkpoint_unit_key: task_id=%s key=%s",
                    task_id,
                    str(key_text)[:300],
                )
                continue
            if not _validate_checkpoint_unit_shape(key, record):
                invalid_count += 1
                logger.warning(
                    "invalid_sound_checkpoint_unit: task_id=%s key=%s",
                    task_id,
                    key_text,
                )
                continue
            valid_units[key_text] = record
        recovered = dict(data)
        recovered["units"] = valid_units
        if invalid_count:
            recovered["status"] = "in_progress"
            logger.warning(
                "sound_checkpoint_partial_recovery: task_id=%s invalid_units=%s",
                task_id,
                invalid_count,
            )
        return recovered
    except Exception as exc:
        logger.warning(
            "invalid_sound_checkpoint: task_id=%s path=%s error=%s",
            task_id,
            path,
            exc,
        )
        return empty_checkpoint(task_id)


def save_sound_checkpoint(
    task_id: str,
    checkpoint: dict,
    owner_check: Optional[OwnerCheck] = None,
) -> None:
    if not _validate_checkpoint_shape(checkpoint, task_id):
        raise SoundCheckpointPersistenceError("checkpoint 结构无效")
    if owner_check is not None:
        owner_check()
    try:
        payload = json.dumps(
            checkpoint,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
    except Exception as exc:
        raise SoundCheckpointPersistenceError(
            f"checkpoint 序列化失败 task_id={task_id}: {exc}"
        ) from exc

    def validator(path: PathLike) -> bool:
        try:
            parsed = _decode_json_value(Path(path).read_bytes())
            valid = _validate_checkpoint_shape(parsed, task_id)
        except Exception:
            valid = False
        if valid and owner_check is not None:
            owner_check()
        return valid

    try:
        atomic_write_bytes(_checkpoint_path(task_id), payload, validator=validator)
    except StageLeaseLostError:
        raise
    except Exception as exc:
        raise SoundCheckpointPersistenceError(
            f"checkpoint 保存失败 task_id={task_id}: {exc}"
        ) from exc

# 客户端不在导入时创建：AsyncOpenAI 会绑定创建时的事件循环。
# 在 Celery worker 中每个任务用独立的 asyncio.run() 新建循环，
# 复用绑在旧（已关闭）循环上的 client 会触发 "Event loop is closed"。
# 因此改为每次 process_task 内重建并在结束时关闭。
client = None
engine = create_engine(
    DB_URL,
    pool_pre_ping=True,
    **worker_pool_kwargs(default_pool_size=5, default_max_overflow=10),
)


def get_plots_from_db(task_id):
    query = text("SELECT data FROM plots WHERE task_id = :task_id")
    try:
        with engine.connect() as conn:
            result = conn.execute(query, {"task_id": task_id}).fetchone()
            return result[0] if result and result[0] else None
    except Exception as exc:
        logger.warning("read_plots_db_failed: task_id=%s error=%s", task_id, exc)
        return None


def load_plots_data(task_id: str) -> dict:
    try:
        db_value = get_plots_from_db(task_id)
        db_value = _decode_json_value(db_value) if db_value is not None else None
        if _validate_plots_data(db_value) is None:
            return db_value
    except Exception as exc:
        logger.warning("invalid_plots_db_json: task_id=%s error=%s", task_id, exc)

    fallback_path = Path(OUTPUTS_PATH) / str(task_id) / "plots.json"
    try:
        if fallback_path.is_file():
            fallback_value = _decode_json_value(fallback_path.read_bytes())
            if _validate_plots_data(fallback_value) is None:
                logger.warning(
                    "using_shared_plots_fallback: task_id=%s path=%s",
                    task_id,
                    fallback_path,
                )
                return fallback_value
            logger.warning("invalid_plots_fallback: task_id=%s path=%s", task_id, fallback_path)
    except Exception as exc:
        logger.warning("read_plots_fallback_failed: task_id=%s path=%s error=%s", task_id, fallback_path, exc)
    raise SoundInputUnavailableError(
        f"plots 不可用 task_id={task_id}（数据库和共享 plots.json 均无有效数据）"
    )


def load_previous_sound_for_order(task_id: str) -> Optional[dict]:
    query = text("SELECT data FROM sound WHERE task_id = :task_id")

    def _has_basic_order(value: object) -> bool:
        if not isinstance(value, dict) or not isinstance(value.get("_meta"), dict):
            return False
        node_order = value["_meta"].get("node_order")
        return (
            isinstance(node_order, list)
            and bool(node_order)
            and all(isinstance(node_id, str) and node_id.strip() for node_id in node_order)
        )

    try:
        with engine.connect() as conn:
            result = conn.execute(query, {"task_id": task_id}).fetchone()
            if result and result[0] is not None:
                value = _decode_json_value(result[0])
                if _has_basic_order(value):
                    return value
    except Exception as exc:
        logger.warning("read_sound_order_db_failed: task_id=%s error=%s", task_id, exc)

    path = Path(OUTPUTS_PATH) / str(task_id) / "sound.json"
    try:
        if path.is_file():
            value = _decode_json_value(path.read_bytes())
            if _has_basic_order(value):
                return value
    except Exception as exc:
        logger.warning("read_sound_order_fallback_failed: task_id=%s error=%s", task_id, exc)
    return None


def _public_file_validator(path: PathLike, expected_node_ids: set[str]) -> bool:
    return validate_public_sound_file(path, expected_node_ids) is None


def validate_public_sound_file(
    path: PathLike,
    expected_node_ids: set[str],
) -> Optional[str]:
    """Validate a serialized public sound payload and return a reason on failure."""
    try:
        data = _decode_json_value(Path(path).read_bytes())
    except Exception as exc:
        return f"sound 文件读取或 JSON 解析失败: {exc}"
    return validate_public_sound_data(data, expected_node_ids)


def write_sound_file(
    task_id: str,
    sound_data: dict,
    expected_node_ids: set[str],
    owner_check: Optional[OwnerCheck] = None,
) -> None:
    reason = validate_public_sound_data(sound_data, expected_node_ids)
    if reason:
        raise SoundPersistenceError(f"公开 sound 结构无效: {reason}")
    if owner_check is not None:
        owner_check()
    path = Path(OUTPUTS_PATH) / str(task_id) / "sound.json"
    try:
        payload = json.dumps(sound_data, ensure_ascii=False, indent=2).encode("utf-8")
    except Exception as exc:
        raise SoundPersistenceError(
            f"共享 sound.json 序列化失败 task_id={task_id}: {exc}"
        ) from exc

    def validator(temp_path: PathLike) -> bool:
        valid = _public_file_validator(temp_path, expected_node_ids)
        if valid and owner_check is not None:
            owner_check()
        return valid

    try:
        atomic_write_bytes(path, payload, validator=validator)
    except StageLeaseLostError:
        raise
    except Exception as exc:
        raise SoundPersistenceError(
            f"共享 sound.json 写入失败 task_id={task_id}: {exc}"
        ) from exc


def _snapshot_file(path: Path) -> Optional[bytes]:
    """Capture an existing publication before a multi-file force commit."""
    if not path.exists():
        return None
    if not path.is_file():
        raise SoundPersistenceError(f"发布路径不是文件: {path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise SoundPersistenceError(f"发布前读取旧文件失败: {path}: {exc}") from exc


def _restore_file_snapshot(path: Path, snapshot: Optional[bytes]) -> None:
    """Best-effort atomic rollback that never hides the original exception."""
    try:
        if snapshot is None:
            if path.exists():
                path.unlink()
            return
        # Preserve the previous publication byte-for-byte.  It can be a legacy
        # payload, so applying the current public validator during rollback
        # would incorrectly prevent restoration.
        atomic_write_bytes(path, snapshot)
    except Exception as exc:
        logger.error(
            "sound_publication_rollback_failed: path=%s error=%s",
            path,
            exc,
        )


def write_sound_row_to_db(
    task_id: str,
    sound_data: dict,
    owner_check: Optional[OwnerCheck] = None,
) -> bool:
    if owner_check is not None:
        owner_check()
    # Serialize before entering the degradable DB-error branch.  A malformed
    # in-memory payload is a programming/data-contract error, not a transient
    # database outage and must not be reported as a successful fallback.
    encoded_data = json.dumps(sound_data, ensure_ascii=False)
    query = text("""
        INSERT INTO sound (task_id, data, create_time, update_time)
        VALUES (:task_id, :data, CURRENT_TIMESTAMP(0), CURRENT_TIMESTAMP(0))
        ON CONFLICT (task_id) DO UPDATE SET
            data = EXCLUDED.data,
            update_time = CURRENT_TIMESTAMP(0)
    """)
    try:
        with engine.begin() as conn:
            conn.execute(query, {
                "task_id": task_id,
                "data": encoded_data,
            })
        logger.info("sound_db_upsert_ok: task_id=%s", task_id)
        return True
    except StageLeaseLostError:
        raise
    except Exception as exc:
        logger.warning(
            "sound_db_upsert_failed_using_shared_file: task_id=%s error=%s",
            task_id,
            exc,
        )
        return False


def save_to_sound_table(
    task_id,
    sound_data,
    *,
    expected_node_ids: Optional[set[str]] = None,
    owner_check: Optional[OwnerCheck] = None,
):
    expected = expected_node_ids
    if expected is None:
        expected = {str(key) for key in sound_data if key != "_meta"}
    write_sound_file(task_id, sound_data, expected, owner_check)
    return write_sound_row_to_db(task_id, sound_data, owner_check)


def _normalize_bgm_finish_reason(value: object) -> str | None:
    """将网关结束原因归一化后再参与所有 BGM 分支判断。"""
    if isinstance(value, str):
        normalized = value.strip().lower()
        return normalized or "__unknown__"
    if value is None:
        return None
    return "__unknown__"


def _response_field(value: object, field: str, default: object = None) -> object:
    if isinstance(value, dict):
        return value.get(field, default)
    return getattr(value, field, default)


def _safe_response_field(value: object, field: str, default: object = None) -> object:
    """Read response metadata without leaking attribute errors as unit errors."""
    try:
        return _response_field(value, field, default)
    except Exception:
        return default


def _bgm_context(
    *,
    used_model: object,
    finish_reason: object = None,
    raw_content_length: int | None = None,
) -> BgmResponseContext:
    return BgmResponseContext(
        used_model=str(used_model) if used_model is not None else None,
        finish_reason=_normalize_bgm_finish_reason(finish_reason),
        raw_content_length=raw_content_length,
    )


async def _request_bgm_once(
    *,
    messages: list[dict],
) -> tuple[BgmResponseContext, str]:
    """执行一次底层 BGM 请求并返回有限上下文与原始 content。"""
    try:
        response, used_model = await _model_fallback.achat(
            client,
            messages=messages,
            temperature=0.5,
            response_format={"type": "json_object"},
        )
    except (
        StageLeaseLostError,
        SoundInputUnavailableError,
        SoundCheckpointPersistenceError,
        SoundPersistenceError,
        SoundUnitInputError,
    ):
        raise
    except Exception as exc:
        raise BgmProviderRequestError("BGM Provider 请求失败", exc) from exc

    # 先记 usage，再检查响应结构；usage 记录异常必须原样传播，不能被
    # 重新归类为 Provider 错误或触发模型合同重试。
    # Deliberately do not use _safe_response_field here: a usage property that
    # raises is a metering/persistence error and must remain visible to caller.
    usage = _response_field(response, "usage")
    if _usage_tracker is not None and usage is not None:
        _usage_tracker.add_llm_usage(
            usage,
            used_model,
            provider="openai_compatible",
        )

    choices = _safe_response_field(response, "choices")
    if not isinstance(choices, (list, tuple)) or not choices:
        raise BgmResponseContractError(
            "BGM 响应缺少 choices",
            [{"loc": ["choices"], "type": "missing", "msg": "choices 为空或不是数组"}],
            context=_bgm_context(used_model=used_model),
        )

    choice = choices[0]
    finish_reason = _safe_response_field(choice, "finish_reason")
    normalized_finish_reason = _normalize_bgm_finish_reason(finish_reason)
    message = _safe_response_field(choice, "message")
    context = BgmResponseContext(
        used_model=str(used_model) if used_model is not None else None,
        finish_reason=normalized_finish_reason,
        raw_content_length=None,
    )
    if message is None:
        raise BgmResponseContractError(
            "BGM 响应缺少 message",
            [{"loc": ["choices", 0, "message"], "type": "missing", "msg": "message 缺失"}],
            context=context,
        )

    content = _safe_response_field(message, "content")
    if not isinstance(content, str):
        raise BgmResponseContractError(
            "BGM 响应 content 不是字符串",
            [{"loc": ["choices", 0, "message", "content"], "type": "string_type", "msg": "content 必须是字符串"}],
            context=context,
        )
    if not content.strip():
        context = BgmResponseContext(
            used_model=context.used_model,
            finish_reason=context.finish_reason,
            raw_content_length=0,
        )
        raise BgmResponseContractError(
            "BGM 响应 content 为空",
            [{"loc": ["choices", 0, "message", "content"], "type": "empty", "msg": "content 不能为空"}],
            context=context,
        )

    context = BgmResponseContext(
        used_model=context.used_model,
        finish_reason=context.finish_reason,
        raw_content_length=len(content),
    )
    return context, content


def _parse_bgm_response(
    raw_content: str,
    *,
    node_id: str,
    response_context: BgmResponseContext,
) -> BgmParsedResponse:
    """解析顶层 BGM JSON，同时保留是否使用 repair 的状态。"""
    try:
        parsed = json.loads(raw_content)
        repaired = False
    except json.JSONDecodeError as json_exc:
        try:
            parsed = parse_model_json(
                raw_content,
                node_id=node_id,
                audio_type="bgm",
            )
        except json.JSONDecodeError as parse_exc:
            raise BgmResponseContractError(
                "BGM JSON 无法解析",
                [{
                    "loc": [],
                    "type": "json_invalid",
                    "msg": str(parse_exc)[:300],
                }],
                context=response_context,
                repaired=False,
            ) from parse_exc
        except (TypeError, ValueError) as parse_exc:
            raise BgmResponseContractError(
                "BGM JSON 解析结果无效",
                [{
                    "loc": [],
                    "type": type(parse_exc).__name__.lower(),
                    "msg": str(parse_exc)[:300],
                }],
                context=response_context,
                repaired=False,
            ) from parse_exc
        else:
            repaired = True

    try:
        events = require_declared_events(parsed, "bgm_events")
    except SoundUnitValidationError as exc:
        raise BgmResponseContractError(
            "BGM 顶层结构无效",
            [{
                "loc": ["bgm_events"],
                "type": "structure_invalid",
                "msg": str(exc)[:300],
            }],
            context=response_context,
            repaired=repaired,
        ) from exc
    return BgmParsedResponse(events=events, repaired=repaired)


def build_bgm_correction_text(details: list[dict] | None) -> str:
    """构造有界的合同纠正指令，不把模型原文带回请求或日志。"""
    projected: list[dict] = []
    for detail in (details or [])[:8]:
        if not isinstance(detail, dict):
            continue
        nested = detail.get("validation_errors")
        errors = nested if isinstance(nested, list) and nested else [detail]
        for item in errors[:8]:
            if not isinstance(item, dict):
                continue
            candidate = {
                "proposal_index": detail.get("proposal_index"),
                "loc": item.get("loc", []),
                "type": str(item.get("type", ""))[:80],
                "msg": str(item.get("msg", ""))[:300],
            }
            candidate_json = json.dumps(
                projected + [candidate],
                ensure_ascii=False,
            )
            if len(candidate_json) > 900:
                break
            projected.append(candidate)
            if len(projected) >= 8:
                break
        if len(projected) >= 8:
            break

    return (
        "上一次 BGM 输出未通过 JSON/字段合同校验。"
        "请只修正结构并重新输出完整 JSON。错误摘要："
        f"{json.dumps(projected, ensure_ascii=False)}"
        "\n以下六个核心字段必须包含：start_seq、end_seq、"
        "primary_instrument、bpm、prompt、duration；"
        "字段不得省略、不得为 null、不得为空字符串。"
        "没有 BGM 时只能返回 {\"bgm_events\": []}。"
        "只输出 JSON，不要 Markdown、注释、尾逗号或未闭合字符串。"
    )


def _build_bgm_messages(
    system_prompt: str,
    base_user_message: str,
    correction_text: str | None = None,
) -> list[dict]:
    user_message = base_user_message
    if correction_text:
        user_message += "\n\n【上次结构纠正】\n" + correction_text
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]


def _log_bgm_response_error(
    *,
    task_id: str | None,
    node_id: str,
    error: BgmResponseContractError,
) -> None:
    context = error.context
    error_type = (
        error.details[0].get("type", "contract_error")
        if error.details and isinstance(error.details[0], dict)
        else "contract_error"
    )
    logger.warning(
        "invalid_bgm_response: task_id=%s node_id=%s used_model=%s "
        "finish_reason=%s raw_content_length=%s repaired=%s "
        "error_type=%s details=%s",
        task_id,
        node_id,
        getattr(context, "used_model", None),
        getattr(context, "finish_reason", None),
        getattr(context, "raw_content_length", None),
        getattr(error, "repaired", False),
        error_type,
        error.details[:8],
    )


def _bgm_backoff_seconds(attempt: int) -> float:
    upper = min(15.0, 2.0 ** max(1, attempt))
    return random.uniform(2.0, upper)


def _bgm_finish_correction_reason(finish_reason: str | None) -> str | None:
    if finish_reason == "length":
        return "finish_length"
    if finish_reason not in (None, "stop"):
        return "finish_unknown"
    return None


def _bgm_correction_text_for(
    reason: str,
    details: list[dict] | None = None,
) -> str:
    if reason == "json_repair_empty":
        return (
            "上一次 JSON 经过修复且没有明确的 BGM 数组结果。"
            "请重新输出未经过修复的完整 JSON；没有 BGM 时只能返回 "
            "{\"bgm_events\": []}。"
        )
    text = build_bgm_correction_text(details)
    if reason == "finish_length" and not details:
        return text + "\n上次响应可能被截断，请重新输出完整 JSON。"
    if reason == "finish_unknown" and not details:
        return text + "\n请使用正常结束并输出完整 JSON。"
    return text


def _log_bgm_correction(
    *,
    task_id: str | None,
    node_id: str,
    logical_attempt: int,
    reason: str,
) -> None:
    logger.warning(
        "bgm_contract_correction: task_id=%s node_id=%s "
        "logical_attempt=%s correction_reason=%s",
        task_id,
        node_id,
        logical_attempt,
        reason,
    )


async def generate_bgm_for_node(
    node_name,
    all_beats,
    *,
    task_id: str | None = None,
):
    """一次性读取整个节点，只负责提取宏观 BGM。"""
    if not isinstance(all_beats, list) or not all_beats:
        raise SoundUnitInputError("BGM Node 没有可用 beat")

    # 保留原有完整 Node payload；这里的 beat_id 仅用于提示词展示，
    # proposal 的范围合同仍以当前 Node 的位置数量为准。
    min_beat = all_beats[0]["beat_id"]
    max_beat = all_beats[-1]["beat_id"]
    system_prompt = build_bgm_system_prompt(
        node_name=node_name,
        min_beat=min_beat,
        max_beat=max_beat,
    )
    compact_beats = [{
        "beat_id": b["beat_id"],
        "content": b["content"],
        "emotion": b.get("emotion"),
        "beat_type": b.get("beat_type"),
        "scene_id": b.get("scene_id"),
    } for b in all_beats]
    user_content = (
        "请通读以下完整节点，以 scene_id 变化和情绪/情境实质切换为断点，"
        "保留静音留白；为每条 BGM 根据剧情自行选择 modern、ancient 或 "
        "unknown 指导，并严格按输出 schema 返回 JSON：\n"
        f"{json.dumps(compact_beats, ensure_ascii=False)}"
    )
    messages = _build_bgm_messages(system_prompt, user_content)
    logical_attempt = 0
    correction_reason: str | None = None
    correction_text: str | None = None

    while logical_attempt < MAX_BGM_LOGICAL_ATTEMPTS:
        logical_attempt += 1
        try:
            context, raw_content = await _request_bgm_once(messages=messages)
            parsed_response = _parse_bgm_response(
                raw_content,
                node_id=node_name,
                response_context=context,
            )
            raw_events = parsed_response.events
        except BgmProviderRequestError as exc:
            if logical_attempt >= MAX_BGM_LOGICAL_ATTEMPTS:
                exc.attempts = logical_attempt
                raise exc
            logger.warning(
                "bgm_provider_retry: task_id=%s node_id=%s logical_attempt=%s",
                task_id,
                node_name,
                logical_attempt,
            )
            await asyncio.sleep(_bgm_backoff_seconds(logical_attempt))
            continue
        except BgmResponseContractError as exc:
            _log_bgm_response_error(
                task_id=task_id,
                node_id=node_name,
                error=exc,
            )
            if correction_reason is not None or logical_attempt >= MAX_BGM_LOGICAL_ATTEMPTS:
                final_error = BgmResponseContractError(
                    f"BGM 响应合同失败: node_id={node_name}",
                    exc.details,
                    context=exc.context,
                    repaired=exc.repaired,
                )
                final_error.attempts = logical_attempt
                raise final_error from exc
            correction_reason = (
                _bgm_finish_correction_reason(
                    exc.context.finish_reason if exc.context else None
                )
                or "response_structure"
            )
            _log_bgm_correction(
                task_id=task_id,
                node_id=node_name,
                logical_attempt=logical_attempt,
                reason=correction_reason,
            )
            correction_text = _bgm_correction_text_for(
                correction_reason,
                exc.details,
            )
            messages = _build_bgm_messages(
                system_prompt,
                user_content,
                correction_text,
            )
            continue

        raw_snapshot = deepcopy(raw_events)
        coerced_events = coerce_numeric_event_fields(
            deepcopy(raw_events),
            node_id=node_name,
            audio_type="bgm",
        )
        normalized = normalize_bgm_proposals(
            coerced_events,
            node_id=node_name,
            node_beat_count=len(all_beats),
            task_id=task_id,
            used_model=context.used_model,
            finish_reason=context.finish_reason,
            raw_content_length=context.raw_content_length,
            repaired=parsed_response.repaired,
            diagnostic_raw_proposals=raw_snapshot,
            diagnostic_logger=logger,
        )

        finish_reason = context.finish_reason
        finish_correction_reason = _bgm_finish_correction_reason(finish_reason)
        if finish_correction_reason is not None:
            if correction_reason is None and logical_attempt < MAX_BGM_LOGICAL_ATTEMPTS:
                correction_reason = finish_correction_reason
                _log_bgm_correction(
                    task_id=task_id,
                    node_id=node_name,
                    logical_attempt=logical_attempt,
                    reason=correction_reason,
                )
                correction_text = _bgm_correction_text_for(
                    correction_reason,
                    normalized.invalid_details,
                )
                messages = _build_bgm_messages(
                    system_prompt,
                    user_content,
                    correction_text,
                )
                continue
            final_error = BgmResponseContractError(
                f"BGM 响应未正常结束: node_id={node_name} "
                f"finish_reason={finish_reason}",
                normalized.invalid_details,
                context=context,
                repaired=parsed_response.repaired,
            )
            final_error.attempts = logical_attempt
            raise final_error

        if normalized.proposals:
            return node_name, [
                proposal.model_dump() for proposal in normalized.proposals
            ]

        if not raw_events:
            if not parsed_response.repaired and correction_reason is None:
                return node_name, []
            if (
                parsed_response.repaired
                and correction_reason is None
                and finish_reason in (None, "stop")
                and logical_attempt < MAX_BGM_LOGICAL_ATTEMPTS
            ):
                correction_reason = "json_repair_empty"
                _log_bgm_correction(
                    task_id=task_id,
                    node_id=node_name,
                    logical_attempt=logical_attempt,
                    reason=correction_reason,
                )
                correction_text = _bgm_correction_text_for(correction_reason)
                messages = _build_bgm_messages(
                    system_prompt,
                    user_content,
                    correction_text,
                )
                continue
            if (
                correction_reason == "json_repair_empty"
                and not parsed_response.repaired
                and finish_reason in (None, "stop")
            ):
                return node_name, []
            final_error = BgmResponseContractError(
                f"BGM 纠正后为空，不能证明 Node 无 BGM: node_id={node_name}",
                normalized.invalid_details,
                context=context,
                repaired=parsed_response.repaired,
            )
            final_error.attempts = logical_attempt
            raise final_error

        if correction_reason is None and logical_attempt < MAX_BGM_LOGICAL_ATTEMPTS:
            correction_reason = "all_proposals_invalid"
            _log_bgm_correction(
                task_id=task_id,
                node_id=node_name,
                logical_attempt=logical_attempt,
                reason=correction_reason,
            )
            correction_text = _bgm_correction_text_for(
                correction_reason,
                normalized.invalid_details,
            )
            messages = _build_bgm_messages(
                system_prompt,
                user_content,
                correction_text,
            )
            continue
        final_error = BgmResponseContractError(
            f"非空 BGM 提案全部无效: node_id={node_name}",
            normalized.invalid_details,
            context=context,
            repaired=parsed_response.repaired,
        )
        final_error.attempts = logical_attempt
        raise final_error

    final_error = BgmResponseContractError(
        f"BGM 逻辑请求超出预算: node_id={node_name}"
    )
    final_error.attempts = MAX_BGM_LOGICAL_ATTEMPTS
    raise final_error


async def generate_sfx_for_scene(
    node_name,
    scene_id,
    scene_beats,
    *,
    correction_text: str | None = None,
):
    """
    【事件音效·逐句触发】按规范 §4 提取物理事件音效，并合并连续/重复动作链。
    每条事件锚定到实际 beat_id；一个场景可有多条 SFX，但【一个 beat 至多一条】。
    只做物理事件音效，不做心理演出音效(§4.2.4 惊吓/耳鸣/心跳)。
    """
    if not isinstance(scene_beats, list) or not scene_beats:
        raise SoundUnitInputError("SFX scene 没有可用 beat")
    system_prompt = f"""
# 角色定义 (Role)
你是一个专用于视觉小说音频管线的【事件音效(SFX)】结构化提取模型，当前处理节点 `{node_name}` 的场景 `{scene_id}`。

# 任务目标 (Objective)
从输入文本中，精准识别【玩家需要明确感知"某个动作或事件发生了"】的物理声音事件，转化为结构化 JSON。
以下规则是内部网关提示词启发式规则，等待第三期真实试听验证。
本阶段【只做物理事件音效】：不做环境底噪、不做背景音乐、不做心理演出音效(惊吓/耳鸣/心跳等)、**不做任何人声**。

# 【最高优先级·人声一律不做】(No Human Voice — Hard Gate)
本管线的人声全部由独立的 TTS 配音轨负责，SFX 轨【绝对不允许】出现任何人类发声。
凡是声源为"人的嘴/喉/肺"的，**该 beat 直接跳过，不输出任何 SFX**，也不要退而求其次改写成别的音效。
包括但不限于：
- 说话、念白、念口诀/咒语/经文、诵读、报数、下令、呼喝
- 喊叫、呼救、怒吼、惨叫、喊杀声、口号、齐声呐喊、欢呼、起哄
- 哭泣、抽泣、哀嚎、呻吟、惨呼
- 笑声、冷笑、嗤笑、窃笑
- 低语、耳语、议论、嘈杂人声、人群喧哗、交头接耳
- 唱歌、吟唱、合唱、哼唱
- 呼吸、喘息、叹气、咳嗽、吸气、咬牙出声
判据很简单：**如果这个声音必须由人开口才能发出，就跳过。**
反例（这些不是人声，但仍必须按后续硬闸门、独立价值和密度规则判断，不能因此全部提取）：脚步、拍桌、兵刃相击、法术引发的轰鸣/风声/静电爆裂、野兽吼叫、器物碎裂。
注意区分：`柳拂衣飞速念了一串口诀` 是【人在念】→ 跳过；若同句还写了口诀引发的灵力爆发，则只提取那个非人声的爆发，绝不描写念诵本身。
`窗外传来阵阵喊杀声` 是【人群在喊】→ 跳过（不要写成 crowd shouting，也不要写成 crowd murmur/walla）。
`prompt` 里同样【严禁】出现 voice / vocal / speech / shouting / chanting / incantation / syllables / whisper / murmur / crowd walla / singing / laughing / sobbing / breathing 等任何指向人类发声的词——写了就会被下游合成出诡异人声。

# 【加入时机与密度规则】
每条 SFX 必须仍然绑定到事件实际发生的 beat_id；以下规则用于决定是否新增事件：
1. **先执行硬闸门**：人声、音乐、心理演出、纯环境底噪，以及无法还原为具体物理声源的抽象剧情概念，不生成 SFX。能够还原为真实物理声学成分的超凡事件仍可提取。
2. **不要把“原文中能听见”自动等同于“需要独立 SFX”**。只有具有明确物理声源，并且对剧情、情绪、角色或世界观有作用的事件才提取。
3. **同一连续或重复动作链，只选择一个最有代表性的 beat 作为 SFX 锚点**。连续脚步、连续敲击、连续碰撞、反复拿放物品、普通衣物或器物动作，如果没有剧情节点、材质变化、明显力度变化或动作结果变化，应合并为一个代表性事件或直接留白。
4. **只有当声源、材质、力度、动作结果或剧情功能发生明显变化，且后续事件具有独立的声学或叙事价值时，才在后续 beat 新增 SFX**。
   例如：开锁声和开门声是不同事件；挥击声和命中声是不同事件；不能因为它们属于同一动作链就强行合并。
5. **同一个 beat 至多输出一条 SFX**。同一句有多个动作时，只保留声学上最主要、叙事权重最高的一条。
6. **先决定是否保留独立 SFX，再进行 importance 分级**：只有事件已经通过 SFX 硬闸门、并且决定保留为独立 SFX 后，才进行重要度判断；如果事件没有独立的声学或叙事价值，则不输出 SFX。具体分级见字段规范第 9 条。不得为了逃避密度目标，把普通事件标记为 critical 或 important。
7. **普通连续段落中，normal SFX 通常每 20～30 个有效 beat 至多保留约 1 条**；没有代表性事件时可以为 0。
8. **单个 50-beat 分块通常控制在 2～4 条非 critical SFX**。这是软目标，不是最低数量，也不是硬上限。符合硬闸门的 critical，或彼此独立且具有明确声学/叙事价值的 important 事件，可以超过该目标；不得为了凑数量新增 normal SFX。
9. **50-beat 分块只是请求长度限制，不代表新的场景或剧情边界**；不要因为进入新的技术分块就自动新增 SFX。

# 提取范围 (§4.1 事件音效)
## 可提取候选——仍需通过上述硬闸门、独立价值和密度规则：
- 敲门、拍桌、脚步(尤其是逼近/奔跑/沉重的脚步)、推倒重物
- 挥剑、格挡、兵刃相击、弓弦、拳脚重击
- 爆炸、玻璃/陶瓷/玉器碎裂、厚重木石断裂或坍塌
- 机关启动/石门开合、锁链、重物坠地
- 怪物吼叫、灵力/法术爆发等超凡体系的声音事件
- **正在发生的事件类动物声**：如"一只狗朝你扑来狂吠"(§4.2.2，这是事件，要提取)

## 不提取——排除项：
- **一切人声**：见上文【人声一律不做】硬闸门，命中即跳过该 beat。
- **微观拟音/底噪**：衣物摩擦、呼吸、心跳、脚步的极轻响动、翻书页。
- **纯视觉/心理描写**："无声"、"寂静"、"安静地流泪"、内心活动。
- **环境点缀类声源**：如"阡陌交通，鸡犬相闻"这类【背景环境描写里的声源】——它属于环境音的点缀层，不在本阶段提取(§4.2.2)。判据：是【正在发生的具体事件】才提取，是【背景氛围的一部分】则跳过。
- **心理演出音效**：惊吓、耳鸣、倒带、精神冲击——本阶段不做。

# 定制生成闸门 (§4.3.1)
公共音效库当前【不存在】，因此全部走 AI 定制生成。仅当同时满足：①原文明确出现该声音或触发它的动作；②该声音对剧情/情绪/角色/世界观有叙事作用。二者缺一不提取。

# 字段规范 (Data Formatting)
每条对象包含以下字段：
1. `sfx_name`: 全小写英文+下划线，以 `sfx_` 开头(例：`sfx_wooden_door_knock`)。
2. `sfx_id`: 格式 `{{sfx_name}}_{{beat_id}}`(beat_id 为该事件所在句)。
3. `event`: 事件/声源的英文简述(如 `door knock`, `sword parry`, `jade shatter`)。
4. `material`: 声源材质，原文明确时必须填(如 `wood`, `iron`, `stone`, `jade`, `ceramic`, `glass`)；原文未提材质则填 `null`。(§4.2.1)
5. `intensity`: 动作力度/声音强弱，取 `faint`/`weak`/`medium`/`strong`/`extreme` 之一，依原文语义判断(如"重重砸下"=strong，"轻叩"=weak)。(§4.2.1)
6. `spatial`: 空间渲染对象(§5)。**仅当原文明确交代了距离或遮挡关系时才填**，否则填 `null`：
   - `distance`: `near`/`mid`/`far` 之一(如"远处传来的枪声"=far)。
   - `occlusion`: `none`/`through_door`/`through_window`/`through_wall` 之一(如"墙外的呼喊"=through_wall)。
   - 例：`{{"distance": "far", "occlusion": "none"}}`；无空间信息则整个 `spatial` 为 `null`。
7. `prompt`: **纯英文**，遵循当前内部网关的 SFX 启发式规则：
   - 用自然语言散文描述，**不要加 `TrackType:`/`Genre:` 等元数据标签**。
   - 写清【声源 + 动作/触发方式 + 材质 + 时长感/衰减 + 制作质感】，客观物理描述。(§4.3/§4.2)
   - 例：`a heavy iron-studded wooden door being knocked three times with a firm fist, solid low-mid thud with a short decay, close and dry`。
   - 严禁血腥暴力词(flesh/blood/kill)，致命伤害抽象为客观材质破坏声(如骨断=`heavy dull snap`)。
   - **【概念词过滤】** 本模型基于真实世界音频训练，看不懂剧情设定概念。严禁把 `system`, `data`, `digital`, `topology`, `tribulation`, `spatial distortion`, `mind-control` 等设定词直接写进 prompt。必须翻译成真实可听的物理声：`data lightning tribulation` → `crackling high-voltage electrical arcing with a deep bass hum`；`system notification` → `a crisp synthetic bell chime`（用真实电子音描述，不写 system）。
   - **【超凡/魔法声必须拆成物理成分】** 本模型没有"magic"这种音，只写 `magical` 会得到不可控结果。任何法术/灵力/超凡声都必须拆解为【真实可合成的物理声组合】：嗡鸣 `low resonant hum`、风 `swirling wind whoosh`、静电 `crackling static`、碎裂 `crystalline shatter`、火 `roaring flames`、金属 `metallic ring` 等。例：`spiritual energy burst` → `a deep low-frequency rumble mixed with swirling wind and crackling static`。可保留一个 `ethereal/mystical` 作点缀，但主体必须是物理声。
   - **【否定词禁令】** 本模型无 negative prompt，`no XXX` 会反向召唤该元素。严禁写 `no music`/`no voices`/`no reverb` 等——想避开就【绝口不提】那个词。
   - **【干声原则·严格】** `prompt` 只描述【近距离、干净的音源本身】。**严禁在 prompt 里写 `distant`, `from afar`, `long reverb tail`, `echoing`, `through the wall` 等空间效果**——远近/遮挡/混响一律由下游 DSP 按 `spatial` 字段叠加。若事件确实发生在远处或墙后，在 `spatial` 字段标注 `distance`/`occlusion`，而 prompt 仍写近距离干声。收尾统一用 `close and dry`。
8. `duration`: 纯数字(秒)。短促脆响 1.0-1.5；沉闷重击/碎裂 1.5-2.5；巨响/坍塌 3.0-5.0。
9. `importance`: 只允许 `critical`/`important`/`normal`。只有事件已经通过 SFX 硬闸门，并且决定保留为独立 SFX 后，才进行重要度分级：
   - 非重大事件默认使用 `normal`；只有已经决定输出该独立 SFX、但无法进一步确定重要度时，才以 `normal` 作为默认值。
   - `critical`：缺少该声音会导致玩家错过或误解关键线索、关键因果或重大状态变化。只保留不可逆转折、关键线索、已经触发并产生关键结果的机关、决定性结果和重大因果事件。
   - `important`：事件已通过 SFX 硬闸门，且玩家需要明确感知该动作或状态反馈；该声音应当是确认当前动作、即时空间变化或状态变化的主要反馈之一。不播放不会妨碍理解剧情，但会明显削弱动作辨识、即时场景压力或交互反馈。仅因为原文写到了动作、声音可听见，或对场景有一点作用，不得标记为 `important`。
   - `normal`：普通表现润色，可以生成，也可以省略；无法确定时使用 `normal`。普通脚步、普通关门、普通放置和一般动物反应，默认使用 `normal`；如果它们没有独立的声学或叙事价值，也可以直接省略。只有当这些事件承担明确剧情功能时，才可升级为 `important` 或 `critical`。
   - 声音更响、更长、更剧烈、材质更特殊，不能单独证明剧情重要性。
   - 不得为了避免密度目标，或为了让事件显得更有价值，把普通事件标记为 `important` 或 `critical`。
   - 例外：普通脚步通常是 `normal`，但追踪线索中的特定脚步可以是 `important`；普通动物叫声通常是 `normal`，但揭示敌人接近或危险来源时可以升级；普通开门通常是 `normal`，但暗门机关开启并显露关键通道时可以是 `critical`；一般物品操作通常是 `normal`，但关键物品损坏或触发机关时可以是 `critical`。
10. `importance_reason`: 用简洁英文说明该声音的剧情作用，仅供日志和人工审查。
- 声音强度、音量、时长和场面大小不等于剧情重要性；无法确定时使用 `normal`，不得为了显得重要而滥用 `critical`。

重要度正例（用于校准分级，不要求照抄文字）：
- `normal`：普通脚步、普通关门、一般动物反应。
  `importance_reason`: "Routine physical detail; omitting it does not affect plot understanding."
- `important`：厚重木门突然关闭，玩家需要明确知道通道已经关闭，但该门不是关键机关。
  `importance_reason`: "The sound clearly confirms the immediate action and spatial feedback, but is not a decisive plot event."
- `critical`：隐藏机关发出卡响，石门解锁并显露秘密通道。
  `importance_reason`: "This is a decisive causal event that reveals a key route or plot clue."

# 输出要求
- 只输出合法 JSON，包含 `sfx_events` 数组。
- 无符合条件的事件 → 返回 {{"sfx_events": []}}。

# 输出示例

示例 1（普通对话与轻微动作，无事件音效）：
输入："他轻轻叹了口气，理了理衣袖，缓缓走到窗边坐下。"
输出：{{"sfx_events": []}}

示例 1b（**人声硬闸门**：声源全是人的嘴，两句都必须整条跳过，绝不能写成 incantation / crowd shouting）：
输入：[
  {{"beat_id": 21, "content": "柳拂衣飞速念了一串口诀。"}},
  {{"beat_id": 22, "content": "窗外火把的光团不住地擦过窗台，外面传来阵阵喊杀声。"}}
]
输出：{{"sfx_events": []}}

示例 1c（人声与非人声同句：只取非人声的那部分）：
输入："他低喝一声，掌心灵力轰然炸开，将石桌掀翻。"
输出：
{{
  "sfx_events": [
    {{
      "sfx_id": "sfx_energy_burst_stone_table_31",
      "sfx_name": "sfx_energy_burst_stone_table",
      "event": "energy burst overturning a stone table",
      "material": "stone",
      "intensity": "strong",
      "spatial": null,
      "prompt": "a deep low-frequency rumble mixed with swirling wind and crackling static bursting outward, followed by a heavy stone slab being flipped and grinding hard against the ground, sharp attack with a rough tail, close and dry",
      "duration": 2.5,
      "importance": "critical",
      "importance_reason": "The triggered burst reveals a decisive supernatural attack."
    }}
  ]
}}

示例 2（一个场景多个事件，各挂各的 beat；含材质/强度/空间渲染）：
输入：[
  {{"beat_id": 12, "content": "远处传来三声沉闷的更鼓，隔着雨幕若有若无。"}},
  {{"beat_id": 13, "content": "他一脚踹开厚重的木门。"}},
  {{"beat_id": 14, "content": "案上的青瓷茶盏应声坠地，摔得粉碎。"}}
]
输出：
{{
  "sfx_events": [
    {{
      "sfx_id": "sfx_distant_watch_drum_12",
      "sfx_name": "sfx_distant_watch_drum",
      "event": "night watch drum",
      "material": "wood",
      "intensity": "faint",
      "spatial": {{"distance": "far", "occlusion": "none"}},
      "prompt": "three dull beats on a wooden night-watch drum, deep and muffled with a long decay",
      "duration": 3.0,
      "importance": "normal",
      "importance_reason": "Routine physical detail; omitting it does not affect plot understanding."
    }},
    {{
      "sfx_id": "sfx_heavy_wooden_door_kick_13",
      "sfx_name": "sfx_heavy_wooden_door_kick",
      "event": "door kicked open",
      "material": "wood",
      "intensity": "strong",
      "spatial": null,
      "prompt": "a heavy wooden door being kicked open forcefully, a powerful low-mid wooden slam with a sharp attack and rattling frame, close and dry",
      "duration": 2.0,
      "importance": "important",
      "importance_reason": "The sound clearly confirms the immediate action and spatial feedback, but is not a decisive plot event."
    }},
    {{
      "sfx_id": "sfx_porcelain_cup_shatter_14",
      "sfx_name": "sfx_porcelain_cup_shatter",
      "event": "porcelain cup shatter",
      "material": "ceramic",
      "intensity": "medium",
      "spatial": null,
      "prompt": "a porcelain tea cup falling and shattering on a hard floor, bright ceramic smash with scattering shards and a short decay",
      "duration": 1.5,
      "importance": "normal",
      "importance_reason": "Routine physical detail; omitting it does not affect plot understanding."
    }}
  ]
}}"""

    compact_beats = [{
        "beat_id": b["beat_id"],
        "speaker": b.get("speaker_name"),
        "content": b["content"],
        "beat_type": b.get("beat_type"),
    } for b in scene_beats]
    user_content = json.dumps(compact_beats, ensure_ascii=False)

    user_instruction = (
        "请按上述加入时机与密度规则逐句判断并提取物理事件音效(SFX)，"
        "记住同一个 beat 至多一条、连续重复动作只保留代表事件，无事件则留白，"
        "必须以 JSON 格式输出：\n"
        f"{user_content}"
    )
    if correction_text:
        user_instruction += "\n\n【上次输出纠正】\n" + correction_text
    try:
        response, used_model = await _model_fallback.achat(
            client,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_instruction},
            ],
            temperature=0.6,
            response_format={"type": "json_object"},
        )
    except (
        StageLeaseLostError,
        SoundInputUnavailableError,
        SoundCheckpointPersistenceError,
        SoundPersistenceError,
        SoundUnitInputError,
    ):
        raise
    except Exception as exc:
        raise SoundProviderRequestError(
            f"SFX Provider 请求失败: node_id={node_name} scene_id={scene_id}",
            exc,
        ) from exc

    # usage 记录必须发生在后续 JSON/合同处理之前；计量异常不归类为模型输出失败。
    usage = _response_field(response, "usage")
    if _usage_tracker is not None and usage is not None:
        _usage_tracker.add_llm_usage(
            usage, used_model, provider="openai_compatible"
        )

    try:
        choices = _response_field(response, "choices")
        if not isinstance(choices, (list, tuple)) or not choices:
            raise ValueError("choices 为空或不是数组")
        message = _response_field(choices[0], "message")
        raw_content = _response_field(message, "content")
        if not isinstance(raw_content, str) or not raw_content.strip():
            raise ValueError("message.content 为空或不是字符串")
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
        raise SoundUnitOutputError(
            f"SFX 响应结构无效: node_id={node_name} scene_id={scene_id}: {exc}"
        ) from exc

    try:
        parsed_json = parse_model_json(
            raw_content, node_id=node_name, audio_type="sfx"
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning(
            "sound_track_json_invalid: audio_type=sfx node_id=%s scene_id=%s error=%s",
            node_name,
            scene_id,
            str(exc)[:300],
        )
        raise SoundUnitOutputError(
            f"SFX JSON 无法解析: node_id={node_name} scene_id={scene_id}"
        ) from exc
    try:
        events = require_declared_events(parsed_json, "sfx_events")
    except SoundUnitValidationError as exc:
        raise SoundUnitOutputError(
            f"SFX 响应缺少合法 sfx_events: node_id={node_name} scene_id={scene_id}"
        ) from exc
    return node_name, coerce_numeric_event_fields(
        events,
        node_id=node_name,
        audio_type="sfx",
    )


async def generate_ambience_for_scene(
    node_name,
    scene_id,
    scene_beats,
    *,
    correction_text: str | None = None,
):
    """
    【环境音·按场景生产】读取单个场景的全部句子，为该场景产出【至多一条】环境音。
    环境音是一个持续的整体声场：场景底噪 / 天气 / 偶发点缀这三类，
    由模型判断后【融合进同一条 prompt】——可结合，也可只用其中一类，
    但一个场景最终只输出一条 ambience 记录、一个 prompt。

    严格遵守规范 §3.5.1 生成闸门：公共库当前不存在，只有当【原文明确出现声音元素
    或角色明确听到声音】且【对剧情/情绪/世界观有明显作用】时才生成。
    否则返回空数组。
    ambience_id 关联场景的 beat_id 区间（首句-末句），环境音覆盖整个场景。
    """
    if not isinstance(scene_beats, list) or not scene_beats:
        raise SoundUnitInputError("Ambience scene 没有可用 beat")
    start_beat_id = scene_beats[0]["beat_id"] if scene_beats else "0"
    end_beat_id = scene_beats[-1]["beat_id"] if scene_beats else "0"

    system_prompt = f"""
# 角色定义 (Role)
你是一个专用于视觉小说音频管线的【环境音】结构化提取模型，当前处理节点 `{node_name}` 的场景 `{scene_id}`。
注意：`scene_id` 命名格式为 `scene_<地点>_<时间>_<天气>_<序号>`，其中【天气段】可直接读取（如 thunderstorm/rainy/heavy_rain/foggy/none）。

# 什么是环境音 (Definition)
环境音用于表达场景背景、空间、天气、自然状态，营造空间感与氛围。它是一个【持续的整体声场】。构成它的声音有三类，你需要判断本场景该有哪些，然后【全部融合进同一条 prompt 描述】：
1. **场景状态底噪**：角色在该场景中自然能持续听到的基础底噪。如室内夜里的屋舍低鸣、庭院的虫鸣风声、殿堂的空旷回响。
2. **天气声**：持续性天气。如雨声、风声。（**雷是瞬态**，若原文有雷，把它作为"偶尔的远雷"融进描述，不要写成持续不断的雷。）
3. **偶发点缀**：离散、低频次的补充声，如远处更鼓、远钟、犬吠、水流声。

# 【核心·一个场景只输出一条环境音】
- 无论本场景该有一类还是多类声音，最终【只产出一条 ambience 记录、一个 prompt】。
- 若有多类，就在【同一条 prompt】里把它们融合成一个连贯的声场描述（如"室内屋舍底噪 + 屋檐雨声 + 偶尔远处更鼓"写成一段）。
- 绝不为同一场景输出多条记录。

# 生成闸门 (Gate —— 极其重要，宁缺毋滥)
公共音频库当前【不存在】。只有同时满足以下条件才生成，否则返回空数组：
1. 原文场景描写中【明确存在声音元素】，或角色【明确听到】某声音；
2. 该声音对【剧情发展 / 情绪氛围 / 世界观塑造】有明显作用。
- 仅凭"这是个房间/庭院"这类纯视觉描写，若原文没有任何声音线索，**可以不生成**（返回空数组）。
- 若 scene_id 天气段非 none（如 rainy/thunderstorm），天气声通常应纳入声场。
- 不要凭空臆造原文没有依据的声音（如没提到鸟就不要加鸟鸣）。

# 声音元素约束 (§3.5.3)
- 必须体现的信息：输入正文明确提供的世界观、场景、时间、情绪基调；不得预设地域或文化风格。
- 应包含的声音：该场景客观上必然存在的 + 原文明确描写的（融合后总数控制在 3-4 种以内，避免声场杂乱）。
- **禁止包含**：人声台词、音乐旋律、突发的一次性巨响(那属于 SFX)、该场景客观上不可能存在的声音。

# 提示词规范 (prompt 生成)
- 以下规则是内部网关提示词启发式规则，等待第三期真实试听验证。
- prompt 必须为【纯英文】自然语言散文，**不要加 `TrackType:`/`Genre:` 等元数据标签**。
- 客观物理描述：写清各声源 + 场景 + 持续性质感，不要写主观风格词或旋律。
- 环境音是持续 loop 声床：强调持续、无缝、平稳，用【正向词】如 `a continuous seamless ambient bed`, `steady and even`, `smooth and constant`（**不要写 `no sudden transients` 这类否定句**）。
- 多类融合时用连接词自然串起，如 `..., with steady rain on the tiled eaves, and an occasional low distant drum`。
- **【否定词禁令·环境音最易犯】** 本模型无 negative prompt，`no XXX`/`without XXX` 会【反向召唤】该元素。写 `no thunder` 反而可能加雷，写 `no voices` 反而可能招人声，写 `no wind gusts` 反而可能加阵风。因此**严禁一切否定句**：
  · 想要"平稳的雨、不要雷"→ 只写 `steady even rainfall`（绝口不提 thunder），而不是 `rain with no thunder`。
  · 想要"人群嘈杂但听不清词"→ 用专业术语 `crowd murmur` 或 `distant crowd walla`（**绝不写 voice/voices**，提了就会招来清晰人声）。
  · 想要"平稳无突变"→ 写 `steady and even, smooth and constant`，不写 `no sudden transients`。
  · 原则：**要什么写什么，不要什么就绝口不提那个词。**
- **【概念词过滤】** 本模型看不懂剧情设定概念。严禁 `spatial hum`, `spatial distortion`, `system`, `data` 等设定/抽象词。要表达"诡异的低频空间感"就写真实物理声：`a deep low-frequency rumble` + `a hollow reverberant space`。
- 本作超凡氛围（如"灵气水流""诡异气流"）同样拆成真实声：水流 `gentle trickling water`、气流 `swirling airflow and low wind`，可加一个 `eerie/mystical` 作点缀，但主体必须是真实可合成的声音。
- 空旷感用正向词：`distant, sparse, hollow reverberant space`。

# 数据格式 (Data Formatting)
输出对象字段（一个场景至多一个对象）：
- `ambience_name`: 全小写英文+下划线，以 `amb_` 开头，概括整个声场（如 `amb_rainy_bride_chamber_night`、`amb_quiet_manor_interior`）。
- `ambience_id`: 格式为 `{{ambience_name}}_{start_beat_id}-{end_beat_id}`（拼接本场景的 beat_id 区间：首句 `{start_beat_id}` 到末句 `{end_beat_id}`，环境音覆盖整个场景）。
- `prompt`: 纯英文散文，遵循上文规范，融合本场景所有该有的声音。
- `duration`: 纯数字（秒），持续声床取 45-90。
- `intensity`: 整条声场的整体强弱，取 `faint`/`weak`/`medium`/`strong` 之一（供下游做响度映射，环境音通常偏弱不应盖过对白与 BGM，默认 faint 或 weak）。
- `importance`: 只允许 `critical`/`important`/`normal`：
  - `critical`：环境声音本身就是线索、危险条件或核心世界规则；缺少会让玩家错过或误解重大剧情。
  - `important`：明确塑造特殊地点、天气、时代或持续剧情压力，但缺少后仍能理解剧情。
  - `normal`：一般雨声、风声、室内底噪等背景润色。
- `importance_reason`: 用简洁英文说明该声场的剧情作用，仅供日志和人工审查。
- 声音强度、音量、时长和声场规模不等于剧情重要性；无法确定时使用 `normal`，不得为了显得重要而滥用 `critical`。

# 输出要求
- 只输出合法 JSON，包含一个 `ambience_events` 数组，数组内【至多一个】对象。
- 若整个场景无任何符合闸门的声音 → 返回 {{"ambience_events": []}}。

# 输出示例

示例 A（室内夜雨场景，scene_id 天气段=rainy，原文写了雨打屋檐与远处更鼓——三类融合成一条）：
{{
  "ambience_events": [
    {{
      "ambience_id": "amb_rainy_manor_interior_night_{start_beat_id}-{end_beat_id}",
      "ambience_name": "amb_rainy_manor_interior_night",
      "prompt": "a continuous seamless indoor ambient bed of a quiet old Chinese manor at night, faint creaks of wooden beams in a hollow reverberant interior, with steady even rain falling on the tiled eaves outside and an occasional low dull night-watch drum from the town, smooth and constant, sparse and enveloping",
      "duration": 75,
      "intensity": "weak",
      "importance": "important",
      "importance_reason": "The storm sustains pressure throughout the scene."
    }}
  ]
}}

示例 B（普通室内对话场景，scene_id 天气段=none，原文无任何声音描写）：
{{
  "ambience_events": []
}}"""

    compact_beats = [{
        "beat_id": b["beat_id"],
        "content": b["content"],
        "beat_type": b.get("beat_type"),
    } for b in scene_beats]
    user_content = json.dumps(compact_beats, ensure_ascii=False)

    user_instruction = (
        f"当前场景 scene_id=`{scene_id}`，beat_id 区间=`{start_beat_id}-{end_beat_id}`。"
        "请严格按【生成闸门】判断，只在原文明确有声音且对剧情/情绪/世界观有作用时才生成，"
        "且【整个场景只输出一条环境音、一个融合的 prompt】，必须以 JSON 格式输出：\n"
        f"{user_content}"
    )
    if correction_text:
        user_instruction += "\n\n【上次输出纠正】\n" + correction_text
    try:
        response, used_model = await _model_fallback.achat(
            client,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_instruction},
            ],
            temperature=0.5,
            response_format={"type": "json_object"},
        )
    except (
        StageLeaseLostError,
        SoundInputUnavailableError,
        SoundCheckpointPersistenceError,
        SoundPersistenceError,
        SoundUnitInputError,
    ):
        raise
    except Exception as exc:
        raise SoundProviderRequestError(
            "Ambience Provider 请求失败: "
            f"node_id={node_name} scene_id={scene_id}",
            exc,
        ) from exc

    # usage 记录必须发生在后续 JSON/合同处理之前；计量异常不归类为模型输出失败。
    usage = _response_field(response, "usage")
    if _usage_tracker is not None and usage is not None:
        _usage_tracker.add_llm_usage(
            usage, used_model, provider="openai_compatible"
        )

    try:
        choices = _response_field(response, "choices")
        if not isinstance(choices, (list, tuple)) or not choices:
            raise ValueError("choices 为空或不是数组")
        message = _response_field(choices[0], "message")
        raw_content = _response_field(message, "content")
        if not isinstance(raw_content, str) or not raw_content.strip():
            raise ValueError("message.content 为空或不是字符串")
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
        raise SoundUnitOutputError(
            "Ambience 响应结构无效: "
            f"node_id={node_name} scene_id={scene_id}: {exc}"
        ) from exc

    try:
        parsed_json = parse_model_json(
            raw_content, node_id=node_name, audio_type="ambience"
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning(
            "sound_track_json_invalid: audio_type=ambience node_id=%s scene_id=%s error=%s",
            node_name,
            scene_id,
            str(exc)[:300],
        )
        raise SoundUnitOutputError(
            "Ambience JSON 无法解析: "
            f"node_id={node_name} scene_id={scene_id}"
        ) from exc
    try:
        events = require_declared_events(parsed_json, "ambience_events")
    except SoundUnitValidationError as exc:
        raise SoundUnitOutputError(
            "Ambience 响应缺少合法 ambience_events: "
            f"node_id={node_name} scene_id={scene_id}"
        ) from exc
    return node_name, coerce_numeric_event_fields(
        events,
        node_id=node_name,
        audio_type="ambience",
    )


def make_owner_check(task_id: str) -> OwnerCheck:
    """Build the single owner/fencing check used by Step15 writes.

    ``ensure_stage_lease_owned`` deliberately allows standalone CLI runs that
    have no stage identity.  Any failure while checking a real owner is made
    fatal and normalized to ``StageLeaseLostError`` so it cannot be mistaken
    for an ordinary model-unit failure.
    """

    def _check() -> None:
        try:
            owned = ensure_stage_lease_owned(task_id, "info_extractor")
        except StageLeaseLostError:
            raise
        except Exception as exc:
            raise StageLeaseLostError(
                f"[info_extractor] task_id={task_id} 无法验证阶段执行权"
            ) from exc
        if owned is not True:
            raise StageLeaseLostError(
                f"[info_extractor] task_id={task_id} owner check failed"
            )

    return _check


def _supports_correction_text(generator: Callable[..., object]) -> bool:
    """Keep legacy test/caller fakes compatible with the optional kwarg."""
    try:
        signature = inspect.signature(generator)
    except (TypeError, ValueError):
        return True
    if "correction_text" in signature.parameters:
        return True
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


def _supports_keyword(callable_obj: Callable[..., object], name: str) -> bool:
    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return True
    if name in signature.parameters:
        return True
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


async def _invoke_run_sound_unit(
    unit: SoundUnit,
    *,
    task_id: str | None,
    correction_text: str | None = None,
) -> list[dict]:
    kwargs: dict[str, object] = {}
    if _supports_keyword(run_sound_unit, "task_id"):
        kwargs["task_id"] = task_id
    if correction_text and _supports_keyword(run_sound_unit, "correction_text"):
        kwargs["correction_text"] = correction_text
    return await run_sound_unit(unit, **kwargs)


async def run_sound_unit(
    unit: SoundUnit,
    *,
    task_id: str | None = None,
    correction_text: str | None = None,
) -> list[dict]:
    """Run one extraction unit and perform all deterministic post-processing."""

    mapped_beats = [dict(beat) for beat in unit.mapped_beats]
    if unit.audio_type == "bgm":
        if task_id is None:
            # 保留旧的两参数调用协议，便于独立调用方和既有 fake 使用。
            result = await generate_bgm_for_node(unit.node_id, mapped_beats)
        else:
            result = await generate_bgm_for_node(
                unit.node_id,
                mapped_beats,
                task_id=task_id,
            )
    elif unit.audio_type == "sfx":
        if correction_text and _supports_correction_text(generate_sfx_for_scene):
            result = await generate_sfx_for_scene(
                unit.node_id,
                unit.request_scene_id,
                mapped_beats,
                correction_text=correction_text,
            )
        else:
            result = await generate_sfx_for_scene(
                unit.node_id,
                unit.request_scene_id,
                mapped_beats,
            )
    elif unit.audio_type == "ambience":
        if correction_text and _supports_correction_text(generate_ambience_for_scene):
            result = await generate_ambience_for_scene(
                unit.node_id,
                unit.scene_id,
                mapped_beats,
                correction_text=correction_text,
            )
        else:
            result = await generate_ambience_for_scene(
                unit.node_id,
                unit.scene_id,
                mapped_beats,
            )
    else:  # pragma: no cover - SoundUnit construction already guards this.
        raise SoundUnitInputError(f"未知音轨类型: {unit.audio_type}")

    if not isinstance(result, tuple) or len(result) != 2:
        raise SoundUnitOutputError("生成函数返回结构无效")
    result_node_id, raw_events = result
    if not isinstance(result_node_id, str) or result_node_id != unit.node_id:
        raise SoundUnitOutputError(
            f"生成结果 Node 不匹配: expected={unit.node_id} got={result_node_id}"
        )
    return validate_unit_events(raw_events, unit)


MAX_OPTIONAL_TRACK_LOGICAL_ATTEMPTS = 3


def _optional_track_correction_text(unit: SoundUnit, error: Exception) -> str:
    """Create a bounded correction instruction without echoing model output."""
    summary = str(error).strip().replace("\n", " ")[:500]
    if unit.audio_type == "sfx":
        field = "sfx_events"
    elif unit.audio_type == "ambience":
        field = "ambience_events"
    else:
        field = "bgm_events"
    return (
        f"上一次 {unit.audio_type} 输出未通过 JSON/事件合同校验。"
        f"请修正后只输出完整 JSON；必须包含 {field} 数组，"
        f"没有符合条件的事件时明确返回 {{\"{field}\": []}}。"
        f"错误摘要：{summary}"
    )


def _as_output_error(exc: Exception) -> SoundUnitOutputError:
    if isinstance(exc, SoundUnitOutputError):
        return exc
    wrapped = SoundUnitOutputError(str(exc)[:500])
    wrapped.__cause__ = exc
    return wrapped


async def _run_unit_with_retry(
    unit: SoundUnit,
    *,
    task_id: str | None = None,
) -> tuple[list[dict], int]:
    """Run one unit using exactly one logical budget for its audio track.

    BGM owns its own three-attempt contract loop in ``generate_bgm_for_node``;
    the runner deliberately does not wrap it in another retry loop. SFX and
    ambience use this single three-attempt loop, covering both provider and
    response/event-contract failures.
    """
    if unit.audio_type == "bgm":
        try:
            events = await _invoke_run_sound_unit(unit, task_id=task_id)
            return events, 1
        except (StageLeaseLostError, SoundInputUnavailableError,
                SoundCheckpointPersistenceError, SoundPersistenceError,
                SoundUnitInputError):
            raise
        except SoundProviderRequestError as exc:
            if exc.attempts is None:
                exc.attempts = MAX_BGM_LOGICAL_ATTEMPTS
            raise
        except (SoundUnitOutputError, json.JSONDecodeError,
                SoundUnitValidationError) as exc:
            output_error = _as_output_error(exc)
            if output_error.attempts is None:
                output_error.attempts = getattr(
                    exc, "attempts", MAX_BGM_LOGICAL_ATTEMPTS
                )
            raise output_error from exc

    correction_text: str | None = None
    last_error: Exception | None = None
    for logical_attempt in range(1, MAX_OPTIONAL_TRACK_LOGICAL_ATTEMPTS + 1):
        try:
            if correction_text:
                events = await _invoke_run_sound_unit(
                    unit,
                    task_id=task_id,
                    correction_text=correction_text,
                )
            else:
                events = await _invoke_run_sound_unit(unit, task_id=task_id)
            return events, logical_attempt
        except (StageLeaseLostError, SoundInputUnavailableError,
                SoundCheckpointPersistenceError, SoundPersistenceError,
                SoundUnitInputError):
            raise
        except SoundProviderRequestError as exc:
            last_error = exc
        except (SoundUnitOutputError, json.JSONDecodeError,
                SoundUnitValidationError) as exc:
            last_error = _as_output_error(exc)

        if logical_attempt < MAX_OPTIONAL_TRACK_LOGICAL_ATTEMPTS:
            correction_text = _optional_track_correction_text(unit, last_error)
            logger.warning(
                "sound_unit_retry: task_id=%s audio_type=%s node_id=%s "
                "scene_id=%s chunk_index=%s logical_attempt=%s error=%s",
                task_id,
                unit.audio_type,
                unit.node_id,
                unit.scene_id,
                unit.chunk_index,
                logical_attempt,
                str(last_error)[:500],
            )
            await asyncio.sleep(_bgm_backoff_seconds(logical_attempt))

    exhausted = SoundProviderRequestError(
        f"{unit.audio_type} 单元逻辑重试耗尽: {unit.key}: "
        f"{str(last_error)[:500] if last_error else 'unknown error'}",
        last_error if isinstance(last_error, Exception) else None,
        attempts=MAX_OPTIONAL_TRACK_LOGICAL_ATTEMPTS,
    )
    raise exhausted from last_error


async def run_pending_units(
    units: list[SoundUnit],
    checkpoint: dict,
    *,
    task_id: str,
    owner_check: Optional[OwnerCheck] = None,
    force: bool = False,
    semaphore: Optional[asyncio.Semaphore] = None,
    checkpoint_lock: Optional[asyncio.Lock] = None,
) -> set[SoundUnitKey]:
    """Run all non-reusable units in one track phase.

    Ordinary unit failures are recorded and returned so later phases can still
    run.  Fencing/checkpoint failures are fatal and cancel unfinished workers.
    """

    owner_check = owner_check or make_owner_check(task_id)
    semaphore = semaphore or asyncio.Semaphore(5)
    checkpoint_lock = checkpoint_lock or asyncio.Lock()
    degraded_keys: set[SoundUnitKey] = set()
    pending: list[SoundUnit] = []

    for unit in units:
        record = checkpoint.get("units", {}).get(serialize_unit_key(unit.key))
        if is_checkpoint_unit_reusable(record, unit):
            logger.info(
                "sound_unit_reused: task_id=%s audio_type=%s node_id=%s "
                "scene_id=%s chunk_index=%s",
                task_id,
                unit.audio_type,
                unit.node_id,
                unit.scene_id,
                unit.chunk_index,
            )
            continue
        pending.append(unit)

    if not pending:
        return degraded_keys

    owner_check()

    abort = asyncio.Event()

    async def _worker(unit: SoundUnit) -> None:
        attempts = 0
        failure_kind: str | None = None
        last_error: str | None = None
        try:
            if abort.is_set():
                return
            async with semaphore:
                if abort.is_set():
                    return
                owner_check()
                events, attempts = await _run_unit_with_retry(
                    unit,
                    task_id=task_id,
                )
        except (SoundProviderRequestError, SoundUnitOutputError,
                json.JSONDecodeError) as exc:
            degraded_keys.add(unit.key)
            status = "failed"
            events = []
            failure_kind = "degraded_empty"
            last_error = str(exc)[:500]
            attempts = max(
                1,
                int(getattr(exc, "attempts", None) or (
                    MAX_BGM_LOGICAL_ATTEMPTS
                    if unit.audio_type == "bgm"
                    else MAX_OPTIONAL_TRACK_LOGICAL_ATTEMPTS
                )),
            )
            logger.warning(
                "sound_extract_unit_degraded_empty: task_id=%s audio_type=%s "
                "node_id=%s scene_id=%s chunk_index=%s attempts=%s error=%s",
                task_id,
                unit.audio_type,
                unit.node_id,
                unit.scene_id,
                unit.chunk_index,
                attempts,
                last_error,
            )
        except BaseException:
            # Any unclassified/fatal error aborts the phase. The outer gather
            # cancels sibling workers before propagating the original error.
            abort.set()
            raise
        else:
            status = "complete"

        if abort.is_set():
            return
        async with checkpoint_lock:
            if abort.is_set():
                return
            owner_check()
            previous_record = checkpoint.setdefault("units", {}).get(
                serialize_unit_key(unit.key)
            )
            total_attempts = (
                _checkpoint_attempts(previous_record) + attempts
                if status == "failed"
                else attempts
            )
            checkpoint.setdefault("units", {})[
                serialize_unit_key(unit.key)
            ] = _checkpoint_unit_payload(
                unit,
                status,
                events,
                attempts=total_attempts,
                failure_kind=failure_kind,
                last_error=last_error,
            )
            if not force:
                save_sound_checkpoint(task_id, checkpoint, owner_check)

    tasks = [asyncio.create_task(_worker(unit)) for unit in pending]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        abort.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return degraded_keys


def _assemble_public_sound(
    units: list[SoundUnit],
    checkpoint: dict,
    ordered_node_ids: list[str],
    allow_degraded_keys: Optional[set[SoundUnitKey]] = None,
) -> dict:
    allow_degraded_keys = allow_degraded_keys or set()
    sound_data = {
        str(node_id): {
            "bgm_events": [],
            "sfx_events": [],
            "ambience_events": [],
        }
        for node_id in ordered_node_ids
    }
    for unit in units:
        key = serialize_unit_key(unit.key)
        record = checkpoint.get("units", {}).get(key)
        if is_checkpoint_unit_reusable(record, unit):
            sound_data[unit.node_id][SOUND_TRACK_FIELDS[unit.audio_type]].extend(
                record["events"]
            )
            continue
        if (
            unit.key in allow_degraded_keys
            and isinstance(record, dict)
            and _validate_checkpoint_unit_shape(unit.key, record)
            and record.get("status") == "failed"
            and record.get("failure_kind") == "degraded_empty"
            and record.get("events") == []
            and record.get("input_digest") == unit.input_digest
        ):
            # The current run explicitly authorized this failed unit after
            # exhausting its model/response budget; its public contribution is
            # an empty track, while the failed checkpoint remains retryable.
            continue
        else:
            raise SoundExtractionIncompleteError(
                f"单元尚未完成或 checkpoint 失效: {unit.key}"
            )

    for node in sound_data.values():
        node["sfx_events"].sort(key=lambda event: int(event.get("start_order", 0)))
        node["ambience_events"].sort(
            key=lambda event: int(event.get("start_order", 0))
        )
    attach_sound_metadata(sound_data, ordered_node_ids, [])
    reason = validate_public_sound_data(sound_data, set(ordered_node_ids))
    if reason:
        raise SoundPersistenceError(f"公开 sound 结构无效: {reason}")
    return sound_data


def _mark_checkpoint_in_progress(
    task_id: str,
    checkpoint: dict,
    owner_check: OwnerCheck,
    *,
    force: bool,
    best_effort: bool = False,
) -> None:
    """Persist the retry marker without duplicating recovery branches.

    A normal checkpoint write failure is fatal.  During recovery of an already
    reported publication error, the original error remains authoritative, so a
    secondary checkpoint failure is logged and not substituted.
    """
    if force:
        return
    checkpoint["status"] = "in_progress"
    try:
        save_sound_checkpoint(task_id, checkpoint, owner_check)
    except StageLeaseLostError:
        raise
    except SoundCheckpointPersistenceError as exc:
        if not best_effort:
            raise
        logger.error(
            "sound_checkpoint_recovery_failed: task_id=%s error=%s",
            task_id,
            exc,
            exc_info=True,
        )


async def _process_task_inner(task_id: str, force: bool = False) -> str:
    owner_check = make_owner_check(task_id)
    plots_data = await asyncio.to_thread(load_plots_data, task_id)
    if not isinstance(plots_data, dict):
        raise SoundInputUnavailableError(f"plots 输入不可用: task_id={task_id}")

    current_node_ids = [str(node_id) for node_id in plots_data.keys()]
    previous_sound = None if force else await asyncio.to_thread(
        load_previous_sound_for_order,
        task_id,
    )
    ordered_node_ids = resolve_node_order(
        current_node_ids,
        previous_sound,
        force=force,
    )
    units = build_sound_units(plots_data, ordered_node_ids)
    checkpoint = (
        empty_checkpoint(task_id)
        if force
        else await asyncio.to_thread(load_sound_checkpoint, task_id)
    )
    # Preserve the last known-good public payload while the new version is
    # being assembled and published.
    sound_path = Path(OUTPUTS_PATH) / str(task_id) / "sound.json"
    sound_snapshot = _snapshot_file(sound_path)
    checkpoint_lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(5)
    degraded_units: set[SoundUnitKey] = set()

    # A completed publication is not a skip marker.  If the current plots make
    # any persisted unit stale or invalid, make the checkpoint's in-progress
    # state durable before issuing a new provider request.  This prevents a
    # crash during a refresh from leaving an old ``completed`` status beside a
    # partially refreshed set of unit records.
    if not force and checkpoint.get("status") == "completed":
        has_pending_units = any(
            not is_checkpoint_unit_reusable(
                checkpoint.get("units", {}).get(serialize_unit_key(unit.key)),
                unit,
            )
            for unit in units
        )
        if has_pending_units:
            _mark_checkpoint_in_progress(
                task_id,
                checkpoint,
                owner_check,
                force=force,
            )
    # Recompute these diagnostics for the current run; stale values must not
    # authorize a historical failed unit to enter the public payload.
    checkpoint["degraded_units"] = []
    checkpoint["degraded_count"] = 0

    phases = (
        ("bgm", [unit for unit in units if unit.audio_type == "bgm"]),
        ("sfx", [unit for unit in units if unit.audio_type == "sfx"]),
        ("ambience", [unit for unit in units if unit.audio_type == "ambience"]),
    )
    for audio_type, phase_units in phases:
        owner_check()
        logger.info(
            "sound_extract_phase_start: task_id=%s audio_type=%s units=%s",
            task_id,
            audio_type,
            len(phase_units),
        )
        degraded_units.update(
            await run_pending_units(
                phase_units,
                checkpoint,
                task_id=task_id,
                owner_check=owner_check,
                force=force,
                semaphore=semaphore,
                checkpoint_lock=checkpoint_lock,
            )
        )

    try:
        sound_data = _assemble_public_sound(
            units,
            checkpoint,
            ordered_node_ids,
            allow_degraded_keys=degraded_units,
        )
        owner_check()
        db_saved = save_to_sound_table(
            task_id,
            sound_data,
            expected_node_ids=set(ordered_node_ids),
            owner_check=owner_check,
        )
        if not db_saved:
            logger.warning(
                "sound_step15_degraded_to_shared_file: task_id=%s",
                task_id,
            )

        checkpoint["degraded_units"] = sorted(
            serialize_unit_key(key) for key in degraded_units
        )
        checkpoint["degraded_count"] = len(degraded_units)
        checkpoint["status"] = "completed"
        # For force runs this is the first write to the standard checkpoint.
        # For normal runs it records the completed publication after all unit
        # updates.
        save_sound_checkpoint(task_id, checkpoint, owner_check)
    except StageLeaseLostError:
        # A lease can have moved to a newer execution; do not let an old run
        # roll back files that may already belong to that newer owner.
        raise
    except SoundCheckpointPersistenceError:
        _restore_file_snapshot(sound_path, sound_snapshot)
        raise
    except (SoundExtractionIncompleteError, SoundPersistenceError) as primary_error:
        # A previously completed checkpoint does not mean the current public
        # publication succeeded. Keep unit results, but make the next run's
        # publication work visible as in_progress.
        _restore_file_snapshot(sound_path, sound_snapshot)
        _mark_checkpoint_in_progress(
            task_id,
            checkpoint,
            owner_check,
            force=force,
            best_effort=True,
        )
        raise primary_error
    logger.info("sound_step15_complete: task_id=%s db_saved=%s", task_id, db_saved)
    return task_id


async def process_task(task_id: str, force: bool = False) -> str:
    """Run Step15 and return the same task ID only after complete publication."""

    global client
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("task_id 不能为 None 或空字符串")
    if _usage_tracker:
        _usage_tracker.set_context(task_id, "info_extractor")

    logger.info(
        "========== 开始三轨提取音频数据 (BGM+SFX+环境音): %s ==========" ,
        task_id,
    )
    client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL)
    try:
        result = await _process_task_inner(task_id, force=force)
        if result != task_id:
            raise SoundExtractionIncompleteError(
                f"Step15 返回异常 task_id: {result!r}"
            )
        return result
    finally:
        try:
            await client.close()
        except Exception as exc:
            # Cleanup failure must not hide the extraction/fencing exception
            # that is already propagating, nor turn an otherwise published
            # result into an ambiguous implicit failure.
            logger.warning(
                "sound_client_close_failed: task_id=%s error=%s",
                task_id,
                exc,
            )
        finally:
            client = None


if __name__ == "__main__":
    # if len(sys.argv) < 2:
    #     print("用法: python step1_sound_extract.py <task_id>", file=sys.stderr)
    #     sys.exit(1)
    #
    # task_id_arg = sys.argv[1]

    if sys.platform.startswith('win'):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    import argparse

    parser = argparse.ArgumentParser(description="Info Step15 sound extraction")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    asyncio.run(process_task(task_id=args.task_id, force=args.force))
