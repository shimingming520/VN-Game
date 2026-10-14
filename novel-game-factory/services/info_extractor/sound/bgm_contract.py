"""BGM 提案的最小运行安全校验与最终事件物化合同。"""

import logging

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    field_validator,
)


logger = logging.getLogger(__name__)

VALID_IMPORTANCE = frozenset({"critical", "important", "normal"})


def normalize_importance(value: object) -> tuple[str, bool]:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in VALID_IMPORTANCE:
            return normalized, False
    return "normal", True


def normalize_importance_reason(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def normalize_importance_fields(raw_event: dict) -> tuple[dict, bool]:
    normalized = dict(raw_event)
    if "importance" not in raw_event:
        importance, warned = "normal", False
    else:
        importance, warned = normalize_importance(raw_event.get("importance"))
    normalized["importance"] = importance
    normalized["importance_reason"] = normalize_importance_reason(
        raw_event.get("importance_reason")
    )
    return normalized, warned


class SoundWarningRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    node_id: str
    start_seq: int | None = None
    related_start_seq: int | None = None


class BgmProposal(BaseModel):
    model_config = ConfigDict(extra="ignore")

    start_seq: int = Field(ge=1)
    end_seq: int = Field(ge=1)
    scene_type: str = "unknown"
    emotion_target: str = ""
    music_style: str = ""
    primary_instrument: str
    supporting_instruments: list[str] = Field(default_factory=list)
    bpm: int = Field(ge=1)
    prompt: str
    duration: int = Field(ge=1, le=190)
    importance: str = "normal"
    importance_reason: str = ""

    @field_validator("start_seq", "end_seq", "bpm", "duration", mode="before")
    @classmethod
    def reject_boolean_numbers(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("boolean is not a valid numeric field")
        return value

    @field_validator("scene_type", mode="before")
    @classmethod
    def normalize_scene_type(cls, value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            return "unknown"
        return value.strip()

    @field_validator("emotion_target", "music_style", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: object) -> str:
        return value.strip() if isinstance(value, str) else ""

    @field_validator("supporting_instruments", mode="before")
    @classmethod
    def normalize_supporting(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [
            item.strip()
            for item in value
            if isinstance(item, str) and item.strip()
        ]

    @field_validator("primary_instrument")
    @classmethod
    def normalize_primary_instrument(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("primary_instrument must not be blank")
        return normalized

    @field_validator("prompt")
    @classmethod
    def reject_blank_prompt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prompt must not be blank")
        return value


class BgmNormalizationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposals: list[BgmProposal] = Field(default_factory=list)
    warnings: list[SoundWarningRecord] = Field(default_factory=list)

    _had_invalid_member: bool = PrivateAttr(default=False)
    _invalid_details: list[dict] = PrivateAttr(default_factory=list)

    @property
    def had_invalid_member(self) -> bool:
        return self._had_invalid_member

    @property
    def invalid_details(self) -> list[dict]:
        return list(self._invalid_details)


def _warning(
    code: str,
    node_id: str,
    start_seq: int | None,
    related_start_seq: int | None = None,
) -> SoundWarningRecord:
    return SoundWarningRecord(
        code=code,
        node_id=node_id,
        start_seq=start_seq,
        related_start_seq=related_start_seq,
    )


def _raw_start_seq(raw: object) -> int | None:
    if not isinstance(raw, dict):
        return None
    value = raw.get("start_seq")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _bounded_raw_value(value: object) -> object:
    """Keep diagnostic scalar values bounded and safe to log."""
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, str):
        return value[:100]
    return str(value)[:100]


def _raw_field(raw: object, field: str) -> object:
    if not isinstance(raw, dict):
        return None
    return _bounded_raw_value(raw.get(field))


def _raw_keys(raw: object) -> list[str]:
    if not isinstance(raw, dict):
        return []
    return sorted(str(key) for key in raw.keys())


def _project_validation_errors(exc: Exception) -> list[dict]:
    if isinstance(exc, ValidationError):
        projected: list[dict] = []
        for item in exc.errors()[:8]:
            loc = item.get("loc", ())
            projected.append(
                {
                    "loc": list(loc) if isinstance(loc, tuple) else loc,
                    "type": str(item.get("type", "validation_error"))[:80],
                    "msg": str(item.get("msg", ""))[:300],
                }
            )
        return projected
    return [{
        "loc": [],
        "type": type(exc).__name__.lower(),
        "msg": str(exc)[:300],
    }]


def _result(
    proposals: list[BgmProposal],
    warnings: list[SoundWarningRecord],
    *,
    had_invalid_member: bool,
    invalid_details: list[dict],
) -> BgmNormalizationResult:
    result = BgmNormalizationResult(
        proposals=proposals,
        warnings=warnings,
    )
    result._had_invalid_member = had_invalid_member
    result._invalid_details = list(invalid_details)
    return result


def _log_warning(log, record: SoundWarningRecord) -> None:
    log.warning(
        "%s: node_id=%s start_seq=%s related_start_seq=%s",
        record.code,
        record.node_id,
        record.start_seq,
        record.related_start_seq,
    )


def _log_invalid_member(
    log,
    *,
    task_id: str | None,
    node_id: str,
    proposal_index: int,
    detail: dict,
    used_model: str | None,
    finish_reason: str | None,
    raw_content_length: int | None,
    repaired: bool | None,
    code: str = "invalid_bgm_proposal",
) -> None:
    log.warning(
        "%s: task_id=%s node_id=%s proposal_index=%s "
        "validation_errors=%s raw_keys=%s start_seq=%r end_seq=%r "
        "used_model=%s finish_reason=%s raw_content_length=%s repaired=%s",
        code,
        task_id,
        node_id,
        proposal_index,
        detail.get("validation_errors", []),
        detail.get("raw_keys", []),
        detail.get("start_seq"),
        detail.get("end_seq"),
        used_model,
        finish_reason,
        raw_content_length,
        repaired,
    )


def normalize_bgm_proposals(
    raw_proposals: object,
    *,
    node_id: str,
    node_beat_count: int,
    task_id: str | None = None,
    used_model: str | None = None,
    finish_reason: str | None = None,
    raw_content_length: int | None = None,
    repaired: bool | None = None,
    diagnostic_raw_proposals: object | None = None,
    diagnostic_logger: logging.Logger | None = None,
) -> BgmNormalizationResult:
    """只丢弃无法安全物化或无法形成 Provider 请求的 BGM 提案。"""

    log = diagnostic_logger or logger
    warnings: list[SoundWarningRecord] = []
    invalid_details: list[dict] = []
    had_invalid_member = False
    if not isinstance(raw_proposals, list):
        detail = {
            "proposal_index": 0,
            "validation_errors": [{
                "loc": [],
                "type": "list_type",
                "msg": "BGM 提案必须是数组",
            }],
            "raw_keys": _raw_keys(raw_proposals),
            "start_seq": _raw_field(raw_proposals, "start_seq"),
            "end_seq": _raw_field(raw_proposals, "end_seq"),
        }
        invalid_details.append(detail)
        had_invalid_member = True
        record = _warning("invalid_bgm_proposal", node_id, None)
        warnings.append(record)
        _log_invalid_member(
            log,
            task_id=task_id,
            node_id=node_id,
            proposal_index=0,
            detail=detail,
            used_model=used_model,
            finish_reason=finish_reason,
            raw_content_length=raw_content_length,
            repaired=repaired,
        )
        return _result(
            [],
            warnings,
            had_invalid_member=had_invalid_member,
            invalid_details=invalid_details,
        )

    structurally_valid: list[BgmProposal] = []
    for proposal_index, raw in enumerate(raw_proposals):
        normalized_raw = raw
        if isinstance(raw, dict):
            normalized_raw, warned = normalize_importance_fields(raw)
            if warned:
                record = _warning(
                    "invalid_sound_importance",
                    node_id,
                    _raw_start_seq(raw),
                )
                warnings.append(record)
                _log_warning(log, record)
        try:
            proposal = BgmProposal.model_validate(normalized_raw)
        except (ValidationError, TypeError, ValueError) as exc:
            detail = {
                "proposal_index": proposal_index,
                "validation_errors": _project_validation_errors(exc),
                "raw_keys": _raw_keys(raw),
                "start_seq": _raw_field(raw, "start_seq"),
                "end_seq": _raw_field(raw, "end_seq"),
            }
            invalid_details.append(detail)
            had_invalid_member = True
            record = _warning(
                "invalid_bgm_proposal",
                node_id,
                _raw_start_seq(raw),
            )
            warnings.append(record)
            diagnostic_source = raw
            if isinstance(diagnostic_raw_proposals, list) and proposal_index < len(
                diagnostic_raw_proposals
            ):
                diagnostic_source = diagnostic_raw_proposals[proposal_index]
                detail["raw_keys"] = _raw_keys(diagnostic_source)
                detail["start_seq"] = _raw_field(diagnostic_source, "start_seq")
                detail["end_seq"] = _raw_field(diagnostic_source, "end_seq")
            _log_invalid_member(
                log,
                task_id=task_id,
                node_id=node_id,
                proposal_index=proposal_index,
                detail=detail,
                used_model=used_model,
                finish_reason=finish_reason,
                raw_content_length=raw_content_length,
                repaired=repaired,
            )
            continue

        if not (
            1
            <= proposal.start_seq
            <= proposal.end_seq
            <= node_beat_count
        ):
            detail = {
                "proposal_index": proposal_index,
                "validation_errors": [{
                    "loc": ["start_seq", "end_seq"],
                    "type": "range_invalid",
                    "msg": (
                        f"range {proposal.start_seq}-{proposal.end_seq} is "
                        f"outside the allowed Node range 1-{node_beat_count}"
                    ),
                }],
                "raw_keys": _raw_keys(raw),
                "start_seq": _raw_field(raw, "start_seq"),
                "end_seq": _raw_field(raw, "end_seq"),
            }
            if isinstance(diagnostic_raw_proposals, list) and proposal_index < len(
                diagnostic_raw_proposals
            ):
                diagnostic_source = diagnostic_raw_proposals[proposal_index]
                detail["raw_keys"] = _raw_keys(diagnostic_source)
                detail["start_seq"] = _raw_field(diagnostic_source, "start_seq")
                detail["end_seq"] = _raw_field(diagnostic_source, "end_seq")
            invalid_details.append(detail)
            had_invalid_member = True
            record = _warning("invalid_bgm_range", node_id, proposal.start_seq)
            warnings.append(record)
            _log_invalid_member(
                log,
                task_id=task_id,
                node_id=node_id,
                proposal_index=proposal_index,
                detail=detail,
                used_model=used_model,
                finish_reason=finish_reason,
                raw_content_length=raw_content_length,
                repaired=repaired,
                code="invalid_bgm_range",
            )
            continue

        structurally_valid.append(proposal)

    proposals = sorted(
        structurally_valid,
        key=lambda item: (item.start_seq, item.end_seq),
    )
    active_end = 0
    active_start: int | None = None
    for proposal in proposals:
        if proposal.start_seq <= active_end:
            record = _warning(
                "overlapping_bgm_range",
                node_id,
                proposal.start_seq,
                active_start,
            )
            warnings.append(record)
            _log_warning(log, record)
        if proposal.end_seq > active_end:
            active_end = proposal.end_seq
            active_start = proposal.start_seq

    return _result(
        proposals,
        warnings,
        had_invalid_member=had_invalid_member,
        invalid_details=invalid_details,
    )


def build_bgm_identifiers(
    *,
    start_seq: int,
    real_start_beat_id: str,
    real_end_beat_id: str,
) -> tuple[str, str]:
    bgm_name = f"bgm_moment_{start_seq}"
    bgm_id = f"{bgm_name}_{real_start_beat_id}-{real_end_beat_id}"
    return bgm_name, bgm_id


def materialize_bgm_event(
    proposal: BgmProposal,
    seq_to_real_map: dict[int, str],
) -> dict:
    real_start_beat_id = str(seq_to_real_map[proposal.start_seq])
    real_end_beat_id = str(seq_to_real_map[proposal.end_seq])
    bgm_name, bgm_id = build_bgm_identifiers(
        start_seq=proposal.start_seq,
        real_start_beat_id=real_start_beat_id,
        real_end_beat_id=real_end_beat_id,
    )
    event = proposal.model_dump(exclude={"start_seq", "end_seq"})
    event.update(
        {
            "bgm_name": bgm_name,
            "bgm_id": bgm_id,
            "start_beat_id": real_start_beat_id,
            "end_beat_id": real_end_beat_id,
            "start_order": proposal.start_seq,
            "beat_coverage": proposal.end_seq - proposal.start_seq + 1,
        }
    )
    return event
