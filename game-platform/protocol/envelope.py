"""MQ Envelope 协议模型(设计文档第六章的可执行版本)。

这份文件就是"契约":中台和协议 SDK 共用,算法组照此收发。
"""
import uuid
from datetime import datetime, timezone
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid() -> str:
    return str(uuid.uuid4())


# ── Job Request(中台 → 服务) ────────────────────────────────


class ArtifactRef(BaseModel):
    role: str
    oss_key: str
    material_id: Optional[str] = None
    node_id: Optional[str] = None
    meta: dict = Field(default_factory=dict)


class JobInput(BaseModel):
    params: dict = Field(default_factory=dict)
    artifacts: List[ArtifactRef] = Field(default_factory=list)
    context: dict = Field(default_factory=dict)


class JobOutput(BaseModel):
    oss_bucket: str
    oss_prefix: str
    manifest_required: bool = True


class JobCallbackPolicy(BaseModel):
    progress_expected: bool = True
    min_progress_interval_sec: int = 15


class JobRequest(BaseModel):
    schema_version: str = SCHEMA_VERSION
    msg_id: str = Field(default_factory=_uuid)
    msg_type: Literal["job.request"] = "job.request"
    emitted_at: str = Field(default_factory=_now)

    task_id: str
    step_id: str
    trace_id: str = Field(default_factory=_uuid)
    attempt: int = 1
    idempotency_key: str

    capability: str
    impl_id: str
    group_name: str

    priority: int = 5
    deadline: Optional[str] = None
    reply_to: str = "game.callbacks"
    reply_routing_key: str

    input: JobInput = Field(default_factory=JobInput)
    output: JobOutput
    callback: JobCallbackPolicy = Field(default_factory=JobCallbackPolicy)


# ── Callback(服务 → 中台) ────────────────────────────────


class UsageItem(BaseModel):
    api_type: Literal["llm", "image", "tts", "music"]
    model: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    image_count: int = 0
    tts_chars: int = 0
    requests: int = 0


class _CallbackBase(BaseModel):
    schema_version: str = SCHEMA_VERSION
    msg_id: str = Field(default_factory=_uuid)
    emitted_at: str = Field(default_factory=_now)
    task_id: str
    step_id: str
    trace_id: Optional[str] = None
    capability: str
    impl_id: str
    group_name: str
    idempotency_key: str


class CallbackProgress(_CallbackBase):
    msg_type: Literal["callback.progress"] = "callback.progress"
    attempt: int = 1
    progress: int = Field(ge=0, le=100)
    stage_detail: Optional[str] = None
    metrics: dict = Field(default_factory=dict)


class CallbackOutput(BaseModel):
    artifacts: List[ArtifactRef] = Field(default_factory=list)
    summary: dict = Field(default_factory=dict)


class CallbackSucceeded(_CallbackBase):
    msg_type: Literal["callback.succeeded"] = "callback.succeeded"
    attempt: int = 1
    status: Literal["succeeded"] = "succeeded"
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_ms: Optional[int] = None
    output: CallbackOutput = Field(default_factory=CallbackOutput)
    usage: List[UsageItem] = Field(default_factory=list)
    cost_hint: Optional[dict] = None


class CallbackError(BaseModel):
    code: str  # INVALID_INPUT / UPSTREAM_TIMEOUT / OOM / OSS_WRITE_FAIL / INTERNAL
    retryable: bool = True
    message: str = ""
    detail: Optional[str] = None


class CallbackFailed(_CallbackBase):
    msg_type: Literal["callback.failed"] = "callback.failed"
    attempt: int = 1
    status: Literal["failed"] = "failed"
    error: CallbackError
    usage: List[UsageItem] = Field(default_factory=list)


def parse_callback(payload: dict):
    """按 msg_type 解析回调;未知类型抛 ValueError"""
    msg_type = payload.get("msg_type")
    if msg_type == "callback.progress":
        return CallbackProgress(**payload)
    if msg_type == "callback.succeeded":
        return CallbackSucceeded(**payload)
    if msg_type == "callback.failed":
        return CallbackFailed(**payload)
    raise ValueError(f"unknown msg_type: {msg_type}")
