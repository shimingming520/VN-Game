import base64
import array
import binascii
from contextlib import contextmanager
import fcntl
import hashlib
import io
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from dotenv import load_dotenv
from openai import OpenAI

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common.audio_languages import AUDIO_LANGUAGE_BOOST_MAP
from common.config import OUTPUTS_PATH, REDIS_URL
from common.llm_fallback import ModelFallback
from generate_json_from_txt import (
    EMOTION_STYLES as MINIMAX_EMOTION_STYLES,
    MINIMAX_EMOTION_MAP,
    has_pronounceable_content,
)
from logger_config import setup_logger
from resume_state import (
    ResumeState,
    atomic_write_json,
    file_digest,
    stable_digest,
    valid_wav,
)

try:
    from common.usage_tracker import tracker as _usage_tracker
except ImportError:
    _usage_tracker = None


load_dotenv()

logger = setup_logger(
    "ElevenLabs_Dialogue_Audio_Relay",
    "elevenlabs_dialogue_audio_relay.log",
)

# 中转站走 OpenAI 风格的 Bearer 鉴权，端点路径与 ElevenLabs 官方保持一致。
ELEVENLABS_RELAY_BASE_URL = os.getenv(
    "AUDIO_BASE_URL", "http://sefutech-api.kube-test.svc:3000"
).rstrip("/")
ELEVENLABS_API_KEY = (
    os.getenv("ELEVENLABS_RELAY_API_KEY")
    or os.getenv("DASHSCOPE_API_KEY")
)
ELEVENLABS_URL = (
    f"{ELEVENLABS_RELAY_BASE_URL}/v1/text-to-dialogue/with-timestamps"
)
ELEVENLABS_FORCED_ALIGNMENT_URL = (
    f"{ELEVENLABS_RELAY_BASE_URL}/v1/forced-alignment"
)
# ElevenLabs 没有儿童/少年音色，这些角色用 MiniMax 音色走单句合成。
# 该端点只接受单个 voice，也不返回时间戳，所以不参与切分。
MINIMAX_SPEECH_URL = f"{ELEVENLABS_RELAY_BASE_URL}/v1/audio/speech"
MINIMAX_SPEECH_MODEL = os.getenv("MINIMAX_SPEECH_MODEL", "speech-2.8-hd")
# MiniMax 输出上限是 44.1 kHz，取满后再由 FFmpeg 重采样到全链统一的 48 kHz。
MINIMAX_SAMPLE_RATE = int(os.getenv("MINIMAX_SAMPLE_RATE", "44100"))
PROVIDER_MINIMAX = "minimax"
# 情绪映射和语言增强直接复用 MiniMax 链的既有配置，避免两套参数漂移。
MINIMAX_LANGUAGE_BOOST_MAP = AUDIO_LANGUAGE_BOOST_MAP
MODEL_ID = "eleven_v3"
# Dialogue 原音和切分音统一使用 48 kHz PCM WAV，避免有损 MP3 再解码切分。
OUTPUT_FORMAT = "wav_48000"
# 与官方脚本分开暂存，避免两个脚本互相续跑对方未完成的批次。
# 使用可见目录，方便直接检查失败批次和诊断产物。
STAGE_DIR_PREFIX = "elevenlabs-relay-stage-"
LEGACY_STAGE_DIR_PREFIX = ".elevenlabs-relay-stage-"
# 切分后的成品直接落到主流程契约目录 audios/{locale}-audio/，
# 与 MiniMax 链产物同构，供 game_builder 直接消费。
FINAL_AUDIO_DIR_SUFFIX = "-audio"
# 长音频和时间戳是中间产物，放在 audios/ 外面，避免被当成配音成品消费。
# 成功切分后会逐个清空，正常结束时该目录应为空。
DIALOGUE_DIR_SUFFIX = "-elevenlabs-dialogue"
# 成功批次 JSON 现在在 WAV 发布后直接删除，不再搬进暂存目录。这两个名称只用于
# 清理旧版执行遗留的缓存，以及判断暂存目录是否值得续跑。
CONSUMED_JSON_DIR_NAME = "consumed-dialogue-json"
LEGACY_CONSUMED_JSON_DIR_NAME = ".consumed-dialogue-json"
FAILED_BATCH_DIR_NAME = "failed-batches"
_moderation_report_lock = threading.Lock()
_publish_clips_lock = threading.Lock()
_stage_lock_local = threading.local()


def _task_owner_check(task_info=None):
    """返回批次所属执行实例的 owner 检查回调。

    第六字段以前的测试/旧调用仍可使用六元 task_info；正式主流程传入第七
    字段，线程池中的每个批次都能在 API 前后检查自己的阶段租约。
    """
    callback = task_info[6] if task_info is not None and len(task_info) > 6 else None
    if callback is not None:
        callback()


def _task_owner_callback(task_info=None):
    return task_info[6] if task_info is not None and len(task_info) > 6 else None


def _is_stage_lease_lost(exc):
    """识别来自 tasks.py/common.progress_utils 的租约丢失异常。

    relay 模块不能直接依赖 Celery 任务入口，但必须把租约丢失当作控制流，
    不能像普通批次失败一样写入 failed-batches 并继续消费共享文件。
    """
    seen = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ == "StageLeaseLostError":
            return True
        current = current.__cause__ or current.__context__
    return False
# 排查切分问题时置 1，保留长音频和时间戳供 resplit 工具复算边界。
KEEP_DIALOGUE_ARTIFACTS = os.getenv(
    "ELEVENLABS_KEEP_DIALOGUE", ""
).strip().lower() in {"1", "true", "yes", "on"}
# 只负责英语；日语仍由 MiniMax 链生成。
TARGET_LOCALES = tuple(
    item.strip()
    for item in os.getenv("ELEVENLABS_RELAY_LOCALES", "en-US").split(",")
    if item.strip()
)
OUTPUT_ROOT = OUTPUTS_PATH
# 中转站实测并发上限为 3（超过固定只放 3 个过，其余全部 429），而这个配额是
# 全站共享的：dialogue、forced-alignment 和 MiniMax 单句都在抢同一份。
# 上限按 Pod 数 × 本值 计算，所以单 Pod 只开 1 路，靠 replicas 控制总并发。
MAX_WORKERS = int(os.getenv("ELEVENLABS_DIALOGUE_WORKERS", "2"))
# 中转站全局能承受的并发上限，仅用于启动时告警，不参与调度。
RELAY_GLOBAL_CONCURRENCY_LIMIT = int(
    os.getenv("ELEVENLABS_RELAY_CONCURRENCY_LIMIT", "3")
)
MAX_RETRIES = 5
MAX_QUALITY_RETRIES = max(
    1, int(os.getenv("ELEVENLABS_DIALOGUE_QUALITY_RETRIES", "5"))
)
MODERATION_REPAIR_MAX_ATTEMPTS = int(
    os.getenv("ELEVENLABS_MODERATION_REPAIR_ATTEMPTS", "3")
)
MODERATION_REPAIR_MODEL = "qwen3.7-max"
MODERATION_REPAIR_BASE_URL = os.getenv(
    "AUDIO_TEXT_REPAIR_BASE_URL", "https://ai.farmmx.com/v1"
)
MODERATION_REPAIR_API_KEY = os.getenv("DEEPSEEK_API_KEY")
MODERATION_REPAIR_TIMEOUT_SECONDS = max(
    60.0, float(os.getenv("AUDIO_TEXT_REPAIR_TIMEOUT_SECONDS", "180"))
)
# 主模型偶发不稳定时自动改用备选模型重发同一请求（同中转站、同调用参数）。
_model_fallback = ModelFallback(
    MODERATION_REPAIR_MODEL, name="TextRepair", logger=logger
)
MODERATION_REPORT_FILENAME = "audio_content_moderation.jsonl"
SEGMENT_FADE_SECONDS = float(os.getenv("ELEVENLABS_SEGMENT_FADE_SECONDS", "0"))
MIN_SEGMENT_SECONDS = 0.01
TIMING_METADATA_VERSION = 2
MIN_OUTPUT_RMS = int(os.getenv("ELEVENLABS_MIN_OUTPUT_RMS", "256"))
MIN_OUTPUT_PEAK = int(os.getenv("ELEVENLABS_MIN_OUTPUT_PEAK", "512"))
MAX_FORCED_ALIGNMENT_OVERLAP_SECONDS = float(
    os.getenv("ELEVENLABS_FORCED_ALIGNMENT_MAX_OVERLAP", "0.03")
)
# 实测 3358 条成品单句的最快语速是 17.8 字符/秒（p99 为 15.2），塌缩时会出现
# 12 字符 / 0.04 秒 ≈ 300 字符/秒 这种物理上不可能的值。阈值取 40 留足余量，
# 只用来识别塌缩，不参与正常语速判断。
MAX_PLAUSIBLE_CHARS_PER_SECOND = float(
    os.getenv("ELEVENLABS_MAX_CHARS_PER_SECOND", "40")
)
# Forced Alignment 的句末时间常略微越过实际收声点，个别句子还会整体偏移，
# 所以最终切点不直接取相邻声学范围的中点，而是在中点附近的真实静音里选。
BOUNDARY_SEARCH_PADDING_SECONDS = float(
    os.getenv("ELEVENLABS_BOUNDARY_SEARCH_PADDING", "0.3")
)
# 语速快的相邻台词之间只有一两帧能量低谷，阈值放宽到 20ms 才能命中。
MIN_SILENCE_RUN_SECONDS = float(
    os.getenv("ELEVENLABS_MIN_SILENCE_SECONDS", "0.02")
)
ENVELOPE_HOP_SECONDS = 0.01
ENVELOPE_WINDOW_HOPS = 3
SILENCE_PEAK_RATIO = 0.015
SILENCE_FLOOR_RATIO = 3.0
SILENCE_ABSOLUTE_FLOOR = 40.0
# 只有这些固定词才是 Eleven v3 情绪标签。任意方括号文本都可能是正文
#（例如 [Link Share: ...]）或 UI 占位（例如 [Image]），不能一律拦截。
EMOTION_TAG_WORDS = frozenset({
    "sad", "cheerfully", "excited", "angry", "fearful",
    "surprised", "disgusted", "tired", "neutral", "calm", "happy",
})
# Step 4 只会把情绪标签放在台词开头；锚定开头可避免正文提到
# "[happy]" 或链接/代码片段时被误判。
EMOTION_TAG_RE = re.compile(
    r"^\s*(?:\[(?:" + "|".join(sorted(EMOTION_TAG_WORDS)) + r")\]\s*)+",
    re.IGNORECASE,
)
BOUNDARY_POLICY = (
    "silence_run_center_nearest_forced_alignment_midpoint_with_acoustic_fallback"
)


class ElevenLabsAuthenticationError(RuntimeError):
    pass


class ElevenLabsRequestRejectedError(RuntimeError):
    """请求被上游拒绝，但响应没有明确表明是 API Key 鉴权失败。"""

    def __init__(self, message, batch_payload=None, response_text=""):
        super().__init__(message)
        self.batch_payload = batch_payload
        self.response_text = response_text


class ElevenLabsServiceUnavailableError(RuntimeError):
    """ElevenLabs/中转链在有限重试后仍不可用，可触发整线灾备。"""


class StageLeaseLostError(RuntimeError):
    """兼容独立运行/测试；实际流水线 owner_check 的异常会原样向上传播。"""


class ElevenLabsForcedAlignmentRequestError(RuntimeError):
    pass


class ForcedAlignmentCollapseError(RuntimeError):
    """Forced Alignment 把多句台词的边界堆到了同一处，整批切分不可信。

    连续重复的台词（例如四个角色接连说 'Vote Xie Wuyi.'）会让声学模型无法
    区分第几遍，返回的边界随机塌缩：某句时长压到几十毫秒、后一句起点等于前
    一句终点。此时音频本身是好的，重试同一批只会换个塌缩位置，所以要和普通
    质量问题区分开，直接走逐句重合成。
    """


class BatchJsonConsumptionError(RuntimeError):
    pass


# 旧版"暂存目录整体提交"会先把现有产物搬进备份目录再替换。该机制已移除，改为
# 逐个 WAV 发布，但历史任务目录里可能还留着中断后的备份残留：那种暂存目录的产物
# 处于半替换状态，不能拿来续跑，所以续跑判据仍要识别这两个名称。
OUTPUT_BACKUP_DIR_NAME = "previous-output-backup"
LEGACY_OUTPUT_BACKUP_DIR_NAME = ".previous-output-backup"


class StageBusyError(RuntimeError):
    """同一任务的另一执行实例仍在操作 ElevenLabs 暂存目录。"""


STAGE_LOCK_FILENAME = "elevenlabs-relay-stage.lock"
STAGE_LOCK_WAIT_SECONDS = max(
    30.0, float(os.getenv("ELEVENLABS_STAGE_LOCK_WAIT_SECONDS", "900"))
)
STAGE_LOCK_POLL_SECONDS = max(
    0.5, float(os.getenv("ELEVENLABS_STAGE_LOCK_POLL_SECONDS", "2"))
)
STAGE_LOCK_BACKEND = os.getenv("ELEVENLABS_STAGE_LOCK_BACKEND", "auto").strip().lower()
STAGE_REDIS_LOCK_RENEW_INTERVAL = max(
    5.0,
    float(os.getenv("ELEVENLABS_STAGE_REDIS_LOCK_RENEW_INTERVAL", "60")),
)
# 锁 TTL 只需覆盖"心跳中断到判定失效"这段窗口，不需要覆盖整个任务时长——
# 持有期由心跳每 RENEW_INTERVAL 秒续期维持。设成任务上限（曾是 48 小时）会让
# 被 kill 的 Worker 把锁留到两天后才过期，重启的新 Worker 只能干等。
# 下限取 6 倍续期间隔：容忍连续几次心跳失败或网络抖动，又能在进程死亡后
# 十分钟内自动释放。
STAGE_REDIS_LOCK_TIMEOUT = max(
    STAGE_REDIS_LOCK_RENEW_INTERVAL * 6,
    float(os.getenv("ELEVENLABS_STAGE_REDIS_LOCK_TIMEOUT", "600")),
)


class _StageLockBackendUnavailable(RuntimeError):
    """Redis stage lock cannot be contacted; allows explicit local fallback only."""


def _execution_label():
    return (
        os.getenv("AUDIO_PRODUCER_EXECUTION_ID")
        or os.getenv("NOVEL_GAME_EXECUTION_ID")
        or f"pid={os.getpid()}"
    )


@contextmanager
def _file_stage_lock(task_root, owner_check):
    """在共享 PVC 上串行化同一 task 的 relay stage 生命周期。

    Redis 阶段租约负责决定谁有资格提交终态，但不能终止旧 Worker 已经发出的
    API 请求。这个 POSIX 锁保证接管 Worker 不会在旧 Worker 仍读写 stage 时
    扫描、删除或消费同一批次文件。锁文件放在 task 根目录，跨 Pod 共享卷时
    对所有进程可见；进程异常退出时由内核自动释放。
    """
    task_root = Path(task_root)
    task_root.mkdir(parents=True, exist_ok=True)
    lock_path = task_root / STAGE_LOCK_FILENAME
    deadline = time.monotonic() + STAGE_LOCK_WAIT_SECONDS
    with lock_path.open("a+b") as lock_file:
        while True:
            owner_check()
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise StageBusyError(
                        f"task={task_root.name} 的 ElevenLabs 暂存锁等待超时；"
                        f"当前执行={_execution_label()}"
                    )
                logger.warning(
                    "⏳ ElevenLabs stage 正被其他 Worker 使用，等待释放："
                    f"task={task_root.name}, current_execution={_execution_label()}"
                )
                time.sleep(STAGE_LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _redis_lock_holder(client, lock_name):
    """读锁的剩余 TTL，用于判断它是活锁还是被 kill 的进程留下的残留。

    redis-py 的锁值是随机 token，认不出是哪个 Pod，所以这里只报 TTL：TTL 在
    持续变大说明有心跳在续期（活锁），停在某个值不动就是残留。
    """
    try:
        ttl = client.ttl(lock_name)
    except Exception as exc:
        return f"TTL 未知（{exc}）"
    if ttl is None or ttl < 0:
        return "锁已消失或无 TTL"
    return f"剩余 TTL {ttl}s（TTL 不再变大即为残留锁）"


@contextmanager
def _redis_stage_lock(task_root, owner_check):
    """跨 Pod 互斥锁；Redis 是共享 PVC 不支持可靠 flock 时的主锁。"""
    try:
        import redis
        client = redis.Redis.from_url(
            REDIS_URL, decode_responses=True,
            socket_connect_timeout=5, socket_timeout=5,
        )
        lock_name = f"novel-game:elevenlabs-stage-lock:{Path(task_root).name}"
        lock = client.lock(
            lock_name,
            timeout=STAGE_REDIS_LOCK_TIMEOUT,
            blocking=False,
        )
    except Exception as exc:
        raise _StageLockBackendUnavailable(f"Redis stage lock 初始化失败：{exc}") from exc

    deadline = time.monotonic() + STAGE_LOCK_WAIT_SECONDS
    acquired = False
    heartbeat_stop = threading.Event()
    heartbeat_lost = threading.Event()
    heartbeat_thread = None

    def guarded_owner_check():
        owner_check()
        if heartbeat_lost.is_set():
            raise StageLeaseLostError(
                f"task={Path(task_root).name} Redis ElevenLabs 暂存锁已丢失"
            )

    try:
        while not acquired:
            guarded_owner_check()
            try:
                acquired = bool(lock.acquire(blocking=False))
            except Exception as exc:
                raise _StageLockBackendUnavailable(
                    f"Redis stage lock 获取失败：{exc}"
                ) from exc
            if acquired:
                break
            holder = _redis_lock_holder(client, lock_name)
            if time.monotonic() >= deadline:
                raise StageBusyError(
                    f"task={Path(task_root).name} 的 Redis ElevenLabs 暂存锁等待超时；"
                    f"持有者={holder}，本执行={_execution_label()}；"
                    f"若持有者进程已不存在，可删除 Redis key {lock_name}"
                )
            logger.warning(
                "⏳ ElevenLabs Redis stage 锁被占用，继续等待："
                f"task={Path(task_root).name}, 持有者={holder}, "
                f"本执行={_execution_label()}"
            )
            time.sleep(STAGE_LOCK_POLL_SECONDS)

        def renew_lock():
            while not heartbeat_stop.wait(STAGE_REDIS_LOCK_RENEW_INTERVAL):
                try:
                    if not lock.extend(STAGE_REDIS_LOCK_TIMEOUT, replace_ttl=True):
                        heartbeat_lost.set()
                        return
                except Exception as exc:
                    logger.error("❌ Redis ElevenLabs stage 锁续租失败：%s", exc)
                    heartbeat_lost.set()
                    return

        heartbeat_thread = threading.Thread(
            target=renew_lock,
            name=f"elevenlabs-stage-lock-{Path(task_root).name}",
            daemon=True,
        )
        heartbeat_thread.start()
        yield
    finally:
        heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=2)
        if acquired:
            try:
                lock.release()
            except Exception as exc:
                logger.error("❌ Redis ElevenLabs stage 锁释放失败：%s", exc)


@contextmanager
def _task_stage_lock(task_root, owner_check):
    """同一 task 的 stage 互斥：生产多 Pod 以 Redis 为准，本地才用文件锁。"""
    lock_key = str(Path(task_root).resolve())
    held = getattr(_stage_lock_local, "held", set())
    if lock_key in held:
        # Step 3-5 的外层 relay 锁与 Step 5 公共入口可能嵌套调用；同一线程
        # 已持有锁时直接复用，避免 Redis Lock 非可重入导致自锁死。
        yield
        return
    held = set(held)
    held.add(lock_key)
    _stage_lock_local.held = held
    backend = STAGE_LOCK_BACKEND
    if backend not in {"auto", "redis", "local"}:
        _stage_lock_local.held.discard(lock_key)
        raise RuntimeError(
            "ELEVENLABS_STAGE_LOCK_BACKEND 必须是 auto、redis 或 local，"
            f"当前为 {backend!r}"
        )
    # Kubernetes/多 Pod 默认必须使用 Redis；只有显式 local 才只用文件锁。
    require_shared_lock = backend == "redis" or (
        backend == "auto" and bool(os.getenv("KUBERNETES_SERVICE_HOST"))
    )
    try:
        if backend != "local":
            try:
                with _redis_stage_lock(task_root, owner_check):
                    # Redis 锁已经提供跨 Pod 的唯一性；不再强制叠加 NFS
                    # flock，避免某些 RWX PVC 不支持 POSIX 锁时把任务误杀。
                    yield
                return
            except _StageLockBackendUnavailable:
                if require_shared_lock:
                    raise
                logger.warning(
                    "⚠️ Redis stage 锁不可用，当前非 Kubernetes 环境回退到文件锁；"
                    "生产多 Pod 请设置 ELEVENLABS_STAGE_LOCK_BACKEND=redis"
                )
        with _file_stage_lock(task_root, owner_check):
            yield
    finally:
        held = set(getattr(_stage_lock_local, "held", set()))
        held.discard(lock_key)
        _stage_lock_local.held = held


def _idempotency_key(task_id, module, batch_id, operation, quality_attempt):
    """同一次业务尝试的网络重试共用 key；质量重做使用新 key。"""
    return hashlib.sha256(
        "\x00".join(map(str, (
            task_id, module, batch_id, operation, quality_attempt,
        ))).encode("utf-8")
    ).hexdigest()


def _is_auth_error(status_code, message=""):
    status_code = int(status_code or 0)
    # 成功响应可能包含用户原始台词；绝不能在 2xx 正文里搜索 unauthorized 等词，
    # 否则剧情文本会被误判成鉴权错误并触发整批回滚。
    if 200 <= status_code < 300:
        return False
    # 401 可以直接视为鉴权失败；403 还可能是内容审核、模型权限或音色权限拒绝，
    # 只有响应正文明确提到鉴权时才归类为 Key 问题。
    if status_code == 401:
        return True
    raw_message = str(message or "")
    try:
        payload = json.loads(raw_message)
    except (ValueError, TypeError):
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            raw_message = " ".join(
                str(error.get(key) or "")
                for key in ("message", "type", "code")
            )
        elif isinstance(payload.get("base_resp"), dict):
            raw_message = str(payload["base_resp"].get("status_msg") or "")
    normalized = raw_message.lower()
    return any(key in normalized for key in (
        "invalid api key", "invalid_api_key", "unauthorized",
        "invalid token",
        "authentication failed", "authorization failed",
    ))


def _dialogue_rejection_diagnostics(batch_payload, response):
    """输出被拒批次的完整输入，便于从审核失败日志定位候选台词。"""
    batch_id = batch_payload.get("batch_id") or "unknown-batch"
    context = (
        f"node_id={batch_payload.get('node_id') or 'unknown'}, "
        f"scene_id={batch_payload.get('scene_id') or 'unknown'}, "
        f"batch_id={batch_id}, "
        f"language={batch_payload.get('language_code') or batch_payload.get('language') or 'unknown'}"
    )
    lines = []
    for index, item in enumerate(batch_payload.get("inputs") or []):
        text_value = str(item.get("text") or "")
        # JSON 编码可把换行和控制字符转义，避免一条台词伪装成多条日志。
        encoded_text = json.dumps(text_value, ensure_ascii=False)
        lines.append(
            f"  [{index}] material_id={item.get('material_id') or 'unknown'}, "
            f"source_filename={item.get('source_filename') or 'unknown'}, "
            f"speaker_id={item.get('speaker_id') or 'unknown'}, "
            f"voice_id={item.get('voice_id') or 'unknown'}, "
            f"chars={len(text_value)}, text={encoded_text}"
        )
    response_text = str(getattr(response, "text", "") or "")
    return (
        "ElevenLabs Dialogue 请求被 HTTP 403 拒绝；响应未明确表示 API Key "
        "无效，可能是输入内容审核、模型权限或音色权限问题。\n"
        f"批次定位: {context}\n"
        f"批次内候选台词（共 {len(lines)} 条，审核接口未返回具体命中项）:\n"
        + ("\n".join(lines) if lines else "  <没有 inputs>")
        + f"\n上游响应: {response_text}"
    )


def _call_dialogue_api(batch_payload):
    if not ELEVENLABS_API_KEY:
        raise ElevenLabsAuthenticationError(
            "ELEVENLABS_RELAY_API_KEY 未设置"
        )

    api_inputs = [
        {"text": item["text"], "voice_id": item["voice_id"]}
        for item in batch_payload["inputs"]
    ]
    request_payload = {
        "inputs": api_inputs,
        "model_id": batch_payload.get("model_id", MODEL_ID),
        "language_code": batch_payload.get("language_code"),
        "apply_text_normalization": "auto",
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {ELEVENLABS_API_KEY}",
        "Idempotency-Key": batch_payload.get("_idempotency_key") or _idempotency_key(
            batch_payload.get("task_id") or batch_payload.get("node_id"),
            "audio_producer",
            batch_payload.get("batch_id"),
            "dialogue",
            batch_payload.get("_quality_attempt", 1),
        ),
    }

    try:
        response = requests.post(
            ELEVENLABS_URL,
            params={
                "output_format": OUTPUT_FORMAT,
                "enable_logging": "false",
            },
            headers=headers,
            json=request_payload,
            timeout=180,
        )
    except requests.RequestException as exc:
        # 返回给统一请求重试器，保证网络超时也按“每轮最多 5 次”执行，
        # 而不是直接跳到下一轮质量重试。
        return None, f"SERVICE_UNAVAILABLE: ElevenLabs Dialogue 请求失败：{exc}"
    if _is_auth_error(response.status_code, response.text):
        raise ElevenLabsAuthenticationError(
            f"ElevenLabs API Key 鉴权失败：HTTP {response.status_code}: {response.text}"
        )
    if response.status_code == 403:
        raise ElevenLabsRequestRejectedError(
            _dialogue_rejection_diagnostics(batch_payload, response),
            batch_payload=batch_payload,
            response_text=response.text,
        )
    if response.status_code in (429, 503):
        return None, f"RATE_LIMIT: HTTP {response.status_code}"
    if 500 <= response.status_code < 600:
        return None, f"SERVICE_UNAVAILABLE: HTTP {response.status_code}: {response.text}"
    if response.status_code != 200:
        return None, f"HTTP {response.status_code}: {response.text}"

    try:
        response_data = response.json()
    except Exception as exc:
        return None, f"响应 JSON 解析失败：{exc}"
    return response_data, ""


def _call_minimax_speech_api(batch_payload):
    """单句 TTS：返回原始音频字节，没有时间戳也不需要切分。"""
    if not ELEVENLABS_API_KEY:
        raise ElevenLabsAuthenticationError(
            "ELEVENLABS_RELAY_API_KEY 未设置"
        )
    item = batch_payload["inputs"][0]
    voice_id = item["voice_id"]
    emotion_label = str(item.get("emotion") or "").lower()
    emotion = MINIMAX_EMOTION_MAP.get(
        emotion_label, MINIMAX_EMOTION_MAP["default"]
    )
    style = MINIMAX_EMOTION_STYLES.get(
        emotion_label, MINIMAX_EMOTION_STYLES["default"]
    )
    # 与 generate_audio_from_speech.py 保持同一套 MiniMax 约定：原生参数放在
    # metadata 里由中转站透传，顶层只留 OpenAI 兼容字段。
    metadata = {
        "voice_setting": {
            "voice_id": voice_id,
            "speed": style["speed"],
            "vol": style["vol"],
            "pitch": 0,
            "emotion": emotion,
            "text_normalization": True,
        },
        # MiniMax 上限 44.1 kHz，且只认 audio_setting 里的 sample_rate。
        "audio_setting": {
            "sample_rate": MINIMAX_SAMPLE_RATE,
            "format": "wav",
            "channel": 1,
        },
        "subtitle_enable": False,
    }
    boost = MINIMAX_LANGUAGE_BOOST_MAP.get(batch_payload.get("language"))
    if boost:
        metadata["language_boost"] = boost

    # MiniMax 只认 emotion 参数，文本里的 [angry] 会被逐字念出来。step4 已按
    # provider 区分，这里对原始文本兜底核查，防止上游回归后无声地念出标签。
    raw_text = str(item["text"] or "")
    if EMOTION_TAG_RE.search(raw_text):
        return None, (
            "MiniMax 台词文本含情绪标签，情绪只能走 voice_setting.emotion："
            f"{raw_text!r}"
        )
    spoken = _spoken_text(raw_text)
    # 非空不等于能发音："..." / "？？？" 这类纯标点会拿回空音频或极短噪声。
    if not _has_alignable_characters(raw_text):
        return None, f"MiniMax 台词没有可发音文本：{raw_text!r}"

    request_payload = {
        "model": batch_payload.get("model_id", MINIMAX_SPEECH_MODEL),
        "voice": voice_id,
        "input": spoken,
        "response_format": "wav",
        "speed": style["speed"],
        "metadata": metadata,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {ELEVENLABS_API_KEY}",
    }
    try:
        response = requests.post(
            MINIMAX_SPEECH_URL,
            headers=headers,
            json=request_payload,
            timeout=180,
        )
    except requests.RequestException as exc:
        return None, f"SERVICE_UNAVAILABLE: MiniMax 单句合成请求失败：{exc}"

    content_type = response.headers.get("Content-Type", "").lower()
    message = "" if content_type.startswith("audio/") else response.text
    if _is_auth_error(response.status_code, message):
        raise ElevenLabsAuthenticationError(
            f"中转站鉴权失败：HTTP {response.status_code}: {message}"
        )
    if response.status_code in (429, 503):
        return None, f"RATE_LIMIT: HTTP {response.status_code}"
    if 500 <= response.status_code < 600:
        return None, f"SERVICE_UNAVAILABLE: HTTP {response.status_code}: {message}"
    if response.status_code != 200:
        return None, f"HTTP {response.status_code}: {message}"
    if not content_type.startswith("audio/") or not response.content:
        return None, f"MiniMax 未返回音频：{message[:200]}"
    return response.content, ""


def _write_minimax_clip(audio_bytes, destination_path):
    """把 MiniMax 音频统一转成 48 kHz 单声道 16-bit WAV，与 EL 产物同构。"""
    temp_source = destination_path + ".src"
    temp_output = destination_path + ".tmp.wav"
    with open(temp_source, "wb") as file_obj:
        file_obj.write(audio_bytes)
    command = [
        "ffmpeg", "-y", "-loglevel", "error", "-i", temp_source,
        "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", temp_output,
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"MiniMax 音频转码失败：{result.stderr.strip()}"
            )
        duration = _wav_duration(temp_output)
        if not duration or duration <= 0:
            raise RuntimeError("MiniMax 转码结果时长非法")
        _validate_split_audio(temp_output, duration)
        os.replace(temp_output, destination_path)
    finally:
        for path in (temp_source, temp_output):
            if os.path.exists(path):
                os.remove(path)


def _decode_dialogue_wav(response_data, label):
    audio_base64 = response_data.get("audio_base64")
    if not audio_base64:
        raise RuntimeError(f"ElevenLabs 响应缺少 audio_base64：{label}")
    try:
        audio_bytes = base64.b64decode(audio_base64, validate=True)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise RuntimeError(
            f"ElevenLabs audio_base64 解码失败：{label}"
        ) from exc
    if not audio_bytes.startswith(b"RIFF"):
        raise RuntimeError(
            f"ElevenLabs 未返回预期的 wav_48000 PCM WAV：{label}"
        )
    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as audio:
            params = (
                audio.getframerate(), audio.getnchannels(), audio.getsampwidth()
            )
    except wave.Error as exc:
        raise RuntimeError(f"ElevenLabs WAV 响应解析失败：{label}: {exc}") from exc
    if params != (48000, 1, 2):
        raise RuntimeError(
            "ElevenLabs 音频参数异常（要求 wav_48000 单声道 16-bit）："
            f"{params[0]}Hz/{params[1]}ch/{params[2] * 8}bit"
        )
    return audio_bytes


def _write_dialogue_clip(audio_bytes, destination_path):
    temp_path = (
        f"{destination_path}.{os.getpid()}.{threading.get_ident()}.building"
    )
    try:
        with open(temp_path, "wb") as file_obj:
            file_obj.write(audio_bytes)
        duration = _wav_duration(temp_path)
        if not duration or duration <= 0:
            raise RuntimeError("ElevenLabs 单句音频时长非法")
        _validate_split_audio(temp_path, duration)
        os.replace(temp_path, destination_path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def _write_silent_placeholder(destination_path, duration_seconds=0.35):
    """审核修复耗尽后的跳过占位，保证下游仍能找到合法 WAV 文件。"""
    temp_path = destination_path + ".silent.tmp.wav"
    frame_count = max(1, int(48000 * duration_seconds))
    try:
        with wave.open(temp_path, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(48000)
            audio.writeframes(b"\x00\x00" * frame_count)
        os.replace(temp_path, destination_path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def _single_dialogue_payload(batch_payload, item, text_value):
    single_item = dict(item)
    single_item["text"] = text_value
    single_item["dialogue_input_index"] = 0
    return {
        key: value for key, value in batch_payload.items() if key != "inputs"
    } | {
        "batch_id": (
            f"{batch_payload.get('batch_id') or 'unknown'}__"
            f"{item.get('material_id') or 'line'}"
        ),
        "inputs": [single_item],
    }


def _request_single_dialogue(batch_payload, item, text_value, label):
    response_data = _call_with_retries(
        lambda: _call_dialogue_api(
            _single_dialogue_payload(batch_payload, item, text_value)
        ),
        label,
    )
    audio_bytes = _decode_dialogue_wav(response_data, label)
    if _usage_tracker:
        try:
            _usage_tracker.add_tts(
                len(_spoken_text(text_value)), MODEL_ID,
                provider="elevenlabs-relay",
            )
        except Exception as exc:
            logger.warning(f"⚠️ TTS 用量记录失败，单句音频仍继续：{exc}")
    return audio_bytes


def _extract_json_object(raw_content):
    text_value = str(raw_content or "").strip()
    if text_value.startswith("```"):
        text_value = re.sub(r"^```(?:json)?\s*", "", text_value)
        text_value = re.sub(r"\s*```$", "", text_value)
    try:
        payload = json.loads(text_value)
    except ValueError:
        match = re.search(r"\{.*\}", text_value, flags=re.DOTALL)
        if not match:
            raise ValueError("模型未返回 JSON 对象")
        payload = json.loads(match.group(0))
    if not isinstance(payload, dict):
        raise ValueError("模型响应不是 JSON 对象")
    return payload


def _repair_moderated_text(item, original_text, rejected_versions, attempt):
    if not MODERATION_REPAIR_API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY 未设置，无法调用 qwen3.7-max 修复台词")
    emotion_match = re.match(r"^(\s*\[[^\]\r\n]+\]\s*)", original_text)
    required_prefix = emotion_match.group(1).strip() if emotion_match else ""
    strength = {
        1: "Only replace the smallest likely-sensitive phrase.",
        2: "Use indirect, non-graphic wording while preserving the same event.",
        3: "Rewrite the full sentence safely while preserving only its core plot intent.",
    }.get(attempt, "Rewrite it safely with minimal semantic drift.")
    request_payload = {
        "original_text": original_text,
        "language_code": item.get("language_code") or item.get("language"),
        "speaker_id": item.get("speaker_id"),
        "emotion": item.get("emotion"),
        "required_prefix": required_prefix,
        "previous_rejected_versions": rejected_versions,
        "attempt": attempt,
    }
    client = OpenAI(
        api_key=MODERATION_REPAIR_API_KEY,
        base_url=MODERATION_REPAIR_BASE_URL,
        timeout=MODERATION_REPAIR_TIMEOUT_SECONDS,
        max_retries=0,
    )
    response, used_model = _model_fallback.chat(
        client,
        messages=[
            {
                "role": "system",
                "content": (
                    "You repair a fictional game dialogue line that a text-to-speech "
                    "provider rejected. Preserve the original language, speaker intent, "
                    "plot fact, emotion and approximate length. Remove or soften only "
                    "graphic sexual, violent, self-harm, hateful, abusive or otherwise "
                    "moderation-triggering detail. Do not add explanations, censorship "
                    "symbols, quotation marks or new facts. Preserve required_prefix "
                    "exactly when it is non-empty. The new text must differ from every "
                    "previous rejected version. " + strength + " Return one JSON object "
                    "only: {\"text\":\"repaired dialogue\",\"reason\":\"short reason\"}."
                ),
            },
            {"role": "user", "content": json.dumps(request_payload, ensure_ascii=False)},
        ],
        temperature=min(0.2 + attempt * 0.2, 0.8),
    )
    if _usage_tracker and getattr(response, "usage", None):
        try:
            _usage_tracker.add_llm_usage(
                response.usage, used_model, provider="qwen"
            )
        except Exception as exc:
            logger.warning(f"⚠️ 台词修复模型用量记录失败：{exc}")
    payload = _extract_json_object(response.choices[0].message.content)
    repaired = str(payload.get("text") or "").strip()
    if not repaired:
        raise ValueError("qwen3.7-max 返回了空修复文本")
    if required_prefix and not repaired.startswith(required_prefix):
        repaired = f"{required_prefix} {EMOTION_TAG_RE.sub('', repaired, count=1).strip()}"
    if repaired in {original_text, *rejected_versions}:
        raise ValueError("qwen3.7-max 返回了已被拒绝的相同文本")
    return repaired, str(payload.get("reason") or "").strip()


def _task_root_from_stage(stage_task_root):
    stage_path = Path(stage_task_root)
    if stage_path.name.startswith((STAGE_DIR_PREFIX, LEGACY_STAGE_DIR_PREFIX)):
        return str(stage_path.parent)
    return str(stage_path)


def _prune_empty_stage_parents(path, stage_task_root):
    """删除批次专属空目录，但绝不删除并发批次共用的目录。

    ``{node}/audios/{lang}-audio`` 和 ``{node}/{lang}-elevenlabs-dialogue``
    是多个批次共用的工作目录。若某批次在另一个批次尚未写入切片的窗口
    清理到这些目录，会导致后续 ``os.replace`` 因父目录消失而失败。
    """
    current = Path(path)
    stage_root = Path(stage_task_root)
    while current != stage_root and stage_root in current.parents:
        # 这些目录的生命周期由批次扫描/任务级清理管理，不能在单批次收尾时删除。
        if (
            current.name == "audios"
            or current.name.endswith(FINAL_AUDIO_DIR_SUFFIX)
            or current.name.endswith(DIALOGUE_DIR_SUFFIX)
        ):
            break
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _publish_batch_clips(
    stage_task_root, node_id, language, inputs, staged_output_dir,
    owner_check=None,
):
    """每个批次成功后立即原子同步到主目录，并删除暂存 WAV。"""
    task_root = _task_root_from_stage(stage_task_root)
    destination_dir = os.path.join(
        task_root, node_id, "audios", f"{language}{FINAL_AUDIO_DIR_SUFFIX}"
    )
    published = 0
    # 同一进程内串行化“复制-替换-删除源文件”，避免两个批次同时处理同一
    # 共享语言目录时互相观察到半完成状态。跨进程/副本仍由原子 replace 保证。
    with _publish_clips_lock:
        os.makedirs(destination_dir, exist_ok=True)
        for item in inputs:
            if owner_check:
                owner_check()
            filename = f"{item['material_id']}.wav"
            source_path = os.path.join(staged_output_dir, filename)
            destination_path = os.path.join(destination_dir, filename)
            if os.path.abspath(source_path) == os.path.abspath(destination_path):
                _write_clip_provenance(destination_path, item)
                published += 1
                continue
            if not os.path.isfile(source_path):
                # 发布重试/幂等调用可能已经完成了替换；只有目标通过验收时才
                # 视为成功，避免把真正缺失的切片伪装成已发布。
                if _is_valid_clip(destination_path, expected_input=item):
                    published += 1
                    continue
                raise RuntimeError(f"批次成功但暂存音频不存在：{source_path}")
            # 另一个批次的收尾可能清理过共享目录，逐文件发布前再次创建。
            os.makedirs(os.path.dirname(destination_path), exist_ok=True)
            temp_path = (
                f"{destination_path}.{os.getpid()}.{threading.get_ident()}.publishing"
            )
            try:
                shutil.copy2(source_path, temp_path)
                os.replace(temp_path, destination_path)
            finally:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            try:
                os.remove(source_path)
            except FileNotFoundError:
                # 另一个幂等发布者已消费源文件；目标已原子落盘即可。
                if not _is_valid_clip(destination_path, expected_input=item):
                    raise
            _write_clip_provenance(destination_path, item)
            if owner_check:
                owner_check()
            published += 1
        _prune_empty_stage_parents(staged_output_dir, stage_task_root)
    return published


def _append_moderation_report(stage_task_root, record):
    report_path = os.path.join(
        _task_root_from_stage(stage_task_root), MODERATION_REPORT_FILENAME
    )
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with _moderation_report_lock:
        with open(report_path, "a", encoding="utf-8") as file_obj:
            file_obj.write(json.dumps(record, ensure_ascii=False) + "\n")
    return report_path


def _recover_rejected_dialogue_batch(
    batch_payload, output_dir, stage_task_root, initial_error, owner_check=None
):
    """逐句实测定位 403；每条敏感句用 Qwen 修复并重新送审，耗尽后静音跳过。"""
    inputs = batch_payload.get("inputs") or []
    if not inputs:
        raise RuntimeError("被拒批次没有 inputs，无法定位")
    if owner_check:
        owner_check()

    # 用同一音色的无害短句区分内容审核与账号/模型/音色权限问题。预检也被拒时
    # 不允许批量静音跳过，否则会把系统性故障伪装成生产成功。
    probe_item = inputs[0]
    try:
        _request_single_dialogue(
            batch_payload, probe_item, "Hello, thank you for being here.",
            f"ElevenLabs 403 原因预检 {batch_payload.get('batch_id')}",
        )
    except ElevenLabsRequestRejectedError as exc:
        raise ElevenLabsServiceUnavailableError(
            "ElevenLabs 无害文本预检仍返回 403，判断为 Key、模型或音色权限问题，"
            "不会自动改写或跳过整批台词。"
        ) from exc

    repaired_count = 0
    skipped_count = 0
    original_success_count = 0
    for input_index, item in enumerate(inputs):
        if owner_check:
            owner_check()
        material_id = item["material_id"]
        destination_path = os.path.join(output_dir, f"{material_id}.wav")
        if _is_valid_clip(destination_path, expected_input=item):
            original_success_count += 1
            continue
        original_text = str(item.get("text") or "")
        try:
            audio_bytes = _request_single_dialogue(
                batch_payload, item, original_text,
                f"ElevenLabs 敏感台词定位 {material_id}",
            )
            _write_dialogue_clip(audio_bytes, destination_path)
            original_success_count += 1
            continue
        except ElevenLabsRequestRejectedError as exc:
            logger.error(
                "🚫 精准定位到审核不合格台词："
                f"material_id={material_id}, source_filename={item.get('source_filename')}, "
                f"speaker_id={item.get('speaker_id')}, voice_id={item.get('voice_id')}, "
                f"text={json.dumps(original_text, ensure_ascii=False)}"
            )
            first_rejection = str(exc)

        attempts = []
        rejected_versions = [original_text]
        repaired_successfully = False
        rejected_repair_count = 0
        for repair_attempt in range(1, max(1, MODERATION_REPAIR_MAX_ATTEMPTS) + 1):
            attempt_record = {"attempt": repair_attempt}
            try:
                repaired_text, reason = _repair_moderated_text(
                    item, original_text, rejected_versions, repair_attempt
                )
                attempt_record.update({"text": repaired_text, "reason": reason})
                logger.warning(
                    f"🛠️ 审核台词修复 {repair_attempt}/{MODERATION_REPAIR_MAX_ATTEMPTS}: "
                    f"material_id={material_id}, repaired_text="
                    f"{json.dumps(repaired_text, ensure_ascii=False)}"
                )
                audio_bytes = _request_single_dialogue(
                    batch_payload, item, repaired_text,
                    f"ElevenLabs 修复台词验证 {material_id} attempt={repair_attempt}",
                )
                if owner_check:
                    owner_check()
                _write_dialogue_clip(audio_bytes, destination_path)
                attempt_record["result"] = "accepted"
                attempts.append(attempt_record)
                repaired_count += 1
                repaired_successfully = True
                logger.info(
                    f"✅ 审核台词修复通过：material_id={material_id}, "
                    f"attempt={repair_attempt}"
                )
                break
            except ElevenLabsRequestRejectedError as exc:
                rejected_repair_count += 1
                attempt_record["result"] = "rejected_403"
                attempt_record["error"] = str(exc)
                attempts.append(attempt_record)
                if attempt_record.get("text"):
                    rejected_versions.append(attempt_record["text"])
            except (ElevenLabsAuthenticationError, ElevenLabsServiceUnavailableError):
                raise
            except Exception as exc:
                attempt_record["result"] = "repair_or_request_error"
                attempt_record["error"] = str(exc)
                attempts.append(attempt_record)
                logger.warning(
                    f"⚠️ 审核台词修复尝试失败：material_id={material_id}, "
                    f"attempt={repair_attempt}, error={exc}"
                )

        action = "repaired" if repaired_successfully else "silent_placeholder"
        if not repaired_successfully:
            if rejected_repair_count < max(1, MODERATION_REPAIR_MAX_ATTEMPTS):
                raise RuntimeError(
                    f"敏感台词修复流程未完成 {MODERATION_REPAIR_MAX_ATTEMPTS} 次有效送审；"
                    "存在模型调用、网络或音频校验错误，保留批次等待重跑，"
                    f"不会误静音：material_id={material_id}, attempts={attempts}"
                )
            _write_silent_placeholder(destination_path)
            skipped_count += 1
            logger.error(
                f"⏭️ 台词连续 {MODERATION_REPAIR_MAX_ATTEMPTS} 次修复仍未通过审核，"
                f"已静音跳过：material_id={material_id}"
            )
        report_path = _append_moderation_report(stage_task_root, {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "node_id": batch_payload.get("node_id"),
            "scene_id": batch_payload.get("scene_id"),
            "batch_id": batch_payload.get("batch_id"),
            "dialogue_input_index": input_index,
            "material_id": material_id,
            "source_filename": item.get("source_filename"),
            "speaker_id": item.get("speaker_id"),
            "voice_id": item.get("voice_id"),
            "original_text": original_text,
            "initial_batch_error": str(initial_error),
            "single_line_rejection": first_rejection,
            "repair_model": MODERATION_REPAIR_MODEL,
            "repair_attempts": attempts,
            "action": action,
            "final_text": (
                attempts[-1].get("text") if repaired_successfully and attempts else None
            ),
        })
        logger.info(f"🧾 审核修复记录：{report_path}")

    return {
        "original_success": original_success_count,
        "repaired": repaired_count,
        "skipped": skipped_count,
    }


def _recover_collapsed_dialogue_batch(batch_payload, output_dir, owner_check=None):
    """塌缩批次逐句重合成：单句请求只有一条输入，不切分也就不依赖对齐。

    整批音频本身是好的，坏的只是边界，但没法从塌缩的对齐里还原出可信切点，
    所以退化成一句一请求。代价是额度按句数翻倍，只在塌缩时走这条路。
    失败的句子逐条记账继续，不让一句拖垮整批。
    """
    inputs = batch_payload.get("inputs") or []
    if not inputs:
        raise RuntimeError("塌缩批次没有 inputs，无法逐句重合成")

    reused = 0
    resynthesized = 0
    failures = []
    for item in inputs:
        if owner_check:
            owner_check()
        material_id = item["material_id"]
        destination_path = os.path.join(output_dir, f"{material_id}.wav")
        if _is_valid_clip(destination_path, expected_input=item):
            reused += 1
            continue
        try:
            audio_bytes = _request_single_dialogue(
                batch_payload, item, str(item.get("text") or ""),
                f"ElevenLabs 塌缩批次逐句重合成 {material_id}",
            )
            if owner_check:
                owner_check()
            _write_dialogue_clip(audio_bytes, destination_path)
            resynthesized += 1
        except Exception as exc:
            failures.append(f"{material_id}: {exc}")
            logger.error(
                f"❌ 塌缩批次逐句重合成失败：material_id={material_id}, "
                f"speaker_id={item.get('speaker_id')}, error={exc}"
            )

    return {
        "reused": reused,
        "resynthesized": resynthesized,
        "failures": failures,
    }


def _handle_collapsed_batch(
    collapse_error, batch_payload, inputs, output_dir, combined_dir,
    batch_id, json_path, node_id, language, run_task_root, index, total,
    owner_check=None,
):
    """塌缩批次的统一收尾：复用现有 WAV 和重新生成两条路径都会走到这里。"""
    logger.warning(
        f"🚧 Forced Alignment 边界塌缩，改为逐句重合成："
        f"{node_id}/{batch_id} -> {collapse_error}"
    )
    recovery = _recover_collapsed_dialogue_batch(
        batch_payload, output_dir, owner_check=owner_check
    )
    published_inputs = [
        item for item in inputs
        if _is_valid_clip(
            os.path.join(output_dir, f"{item['material_id']}.wav")
        )
    ]
    _publish_batch_clips(
        run_task_root, node_id, language, published_inputs, output_dir,
        owner_check=owner_check,
    )
    if recovery["failures"]:
        # 已成功的句子留在主目录，批次 JSON 保留等待重跑补齐剩余台词。
        raise RuntimeError(
            f"塌缩批次逐句重合成部分失败：{json_path}；"
            f"已产出 {len(published_inputs)}/{len(inputs)}；"
            + "；".join(recovery["failures"])
        ) from collapse_error
    _cleanup_batch_attempt_artifacts(
        combined_dir, batch_id, owner_check=owner_check
    )
    _retire_finished_batch(
        json_path, combined_dir, batch_id, run_task_root,
        owner_check=owner_check,
    )
    return (
        f"✅ [{index}/{total}] ElevenLabs 塌缩恢复完成："
        f"{node_id}/{batch_id} ({len(inputs)} 条台词，"
        f"复用={recovery['reused']}，"
        f"逐句重合成={recovery['resynthesized']}，"
        "边界=单句无需切分)"
    )


def _clip_provenance_path(path):
    """返回不污染正式音频目录的旁车元数据路径。

    旧版本把 ``*.wav.provenance.json`` 写在 ``en-US-audio`` 内，容易被上传
    脚本当成音频素材。新版本放到节点目录下的 ``en-US-audio-provenance``，
    与 ``en-US-elevenlabs-dialogue-json`` 同级，不属于 OSS 素材目录。
    """
    audio_path = Path(path)
    node_root = audio_path.parent.parent.parent
    metadata_dir = node_root / f"{audio_path.parent.name}-provenance"
    return str(metadata_dir / f"{audio_path.name}.json")


def _legacy_hidden_clip_provenance_path(path):
    audio_path = Path(path)
    metadata_dir = audio_path.parent.parent / ".audio-provenance" / audio_path.parent.name
    return str(metadata_dir / f"{audio_path.name}.json")


def _legacy_clip_provenance_path(path):
    return f"{path}.provenance.json"


def _write_clip_provenance(path, item):
    """将成品 WAV 与本次台词输入绑定，防止旧/错批次音频被复用。"""
    payload = {
        "version": 1,
        "material_id": item.get("material_id"),
        "speaker_id": item.get("speaker_id"),
        "voice_id": item.get("voice_id"),
        "text": item.get("text"),
    }
    metadata_path = _clip_provenance_path(path)
    os.makedirs(os.path.dirname(metadata_path), exist_ok=True)
    temp_path = metadata_path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as file_obj:
        json.dump(payload, file_obj, ensure_ascii=False, sort_keys=True)
    os.replace(temp_path, metadata_path)
    # 若是旧版本留下的同名旁车，成功写入新位置后清理旧文件。
    for legacy_path in (
        _legacy_hidden_clip_provenance_path(path),
        _legacy_clip_provenance_path(path),
    ):
        try:
            os.remove(legacy_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("⚠️ 旧版音频旁车清理失败，不影响音频：%s", exc)


def _clip_matches_input(path, expected_input):
    if not expected_input:
        return True
    payload = None
    for metadata_path in (
        _clip_provenance_path(path),
        _legacy_hidden_clip_provenance_path(path),
        _legacy_clip_provenance_path(path),
    ):
        try:
            with open(metadata_path, "r", encoding="utf-8") as file_obj:
                payload = json.load(file_obj)
            break
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    if not isinstance(payload, dict):
        return False
    return all(
        payload.get(key) == expected_input.get(key)
        for key in ("material_id", "speaker_id", "voice_id", "text")
    )


def _is_valid_clip(path, expected_input=None):
    """只读校验，不会删文件；用于判断已有产物能否直接复用。"""
    if not os.path.isfile(path):
        return False
    try:
        with wave.open(path, "rb") as audio:
            if (
                audio.getframerate(),
                audio.getnchannels(),
                audio.getsampwidth(),
            ) != (48000, 1, 2):
                return False
            frames = audio.getnframes()
            if frames <= 0:
                return False
            raw = audio.readframes(frames)
    except (OSError, EOFError, wave.Error):
        return False
    samples = array.array("h")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    peak = max((abs(value) for value in samples), default=0)
    rms = math.sqrt(
        sum(value * value for value in samples) / max(1, len(samples))
    )
    if peak < MIN_OUTPUT_PEAK or rms < MIN_OUTPUT_RMS:
        return False
    return _clip_matches_input(path, expected_input)


def _process_minimax_batch(task_info):
    index, total, json_path, node_id, language, run_task_root, *_ = task_info
    _task_owner_check(task_info)
    with open(json_path, "r", encoding="utf-8") as file_obj:
        batch_payload = json.load(file_obj)

    inputs = batch_payload["inputs"]
    if len(inputs) != 1:
        raise RuntimeError(
            f"MiniMax 单句任务只能有 1 条台词，实际 {len(inputs)}：{json_path}"
        )
    output_dir = os.path.join(
        run_task_root, node_id, "audios",
        f"{language}{FINAL_AUDIO_DIR_SUFFIX}",
    )
    os.makedirs(output_dir, exist_ok=True)
    material_id = inputs[0]["material_id"]
    destination_path = os.path.join(output_dir, f"{material_id}.wav")

    # 续跑时已合格的单句不重复烧额度。这里不能用 _validate_split_audio，
    # 它在校验失败时会删掉文件。
    if _is_valid_clip(destination_path, expected_input=inputs[0]):
        # 复用也算这一句已产出，同样要把输入 JSON 消费掉。
        _publish_batch_clips(
            run_task_root, node_id, language, inputs, output_dir,
            owner_check=_task_owner_callback(task_info),
        )
        _consume_batch_json(
            json_path, run_task_root, owner_check=_task_owner_callback(task_info)
        )
        return (
            f"✅ [{index}/{total}] MiniMax 单句复用："
            f"{node_id}/{material_id}"
        )

    errors = []
    for attempt in range(1, max(1, MAX_QUALITY_RETRIES) + 1):
        _task_owner_check(task_info)
        try:
            # 每轮质量尝试内部最多请求 5 次；一轮请求全部失败后进入下一轮。
            # 质量轮数由 ELEVENLABS_DIALOGUE_QUALITY_RETRIES 控制，默认 5 轮。
            audio_bytes = _call_with_retries(
                lambda: _call_minimax_speech_api(batch_payload),
                f"MiniMax 单句合成 {os.path.basename(json_path)}",
            )
            _write_minimax_clip(audio_bytes, destination_path)
            _task_owner_check(task_info)
        except (ElevenLabsAuthenticationError, ElevenLabsServiceUnavailableError) as exc:
            # 与 ElevenLabs 批量批次一致：系统级请求错误也限定在当前
            # MiniMax 批次内重试，达到上限后由主调度统一保留并跳过。
            errors.append(str(exc))
            logger.warning(
                f"⚠️ MiniMax 单句请求失败 {attempt}/{MAX_QUALITY_RETRIES}: {exc}"
            )
            if attempt < MAX_QUALITY_RETRIES:
                time.sleep(min(3, attempt))
            continue
        except Exception as exc:
            # owner_check 可能在 API 返回后或文件操作前发现执行权已被接管。
            # 绝不能把它当成质量失败重试，否则旧 Worker 会继续读写共享 stage
            # 并继续消耗额度。
            if _is_stage_lease_lost(exc):
                raise
            errors.append(str(exc))
            logger.warning(
                f"⚠️ MiniMax 单句验收失败 {attempt}/{MAX_QUALITY_RETRIES}: "
                f"{os.path.basename(json_path)} -> {exc}"
            )
            if attempt < MAX_QUALITY_RETRIES:
                time.sleep(min(3, attempt))
            continue
        if _usage_tracker:
            try:
                _usage_tracker.add_tts(
                    len(_spoken_text(inputs[0]["text"])),
                    batch_payload.get("model_id", MINIMAX_SPEECH_MODEL),
                    provider="minimax-relay",
                )
            except Exception as exc:
                logger.warning(f"⚠️ TTS 用量记录失败，音频仍按成功处理：{exc}")
        _publish_batch_clips(
            run_task_root, node_id, language, inputs, output_dir,
            owner_check=_task_owner_callback(task_info),
        )
        _task_owner_check(task_info)
        _consume_batch_json(
            json_path, run_task_root, owner_check=_task_owner_callback(task_info)
        )
        return (
            f"✅ [{index}/{total}] MiniMax 单句完成："
            f"{node_id}/{material_id}"
            f"（{inputs[0].get('speaker_id')}，儿童/少年音色）"
        )

    raise RuntimeError(
        f"MiniMax 单句连续 {MAX_QUALITY_RETRIES} 次未通过："
        f"{json_path}；" + "；".join(errors)
    )


def _call_forced_alignment_api(source_path, transcript):
    if not ELEVENLABS_API_KEY:
        raise ElevenLabsAuthenticationError(
            "ELEVENLABS_RELAY_API_KEY 未设置"
        )
    try:
        with open(source_path, "rb") as audio_file:
            response = requests.post(
                ELEVENLABS_FORCED_ALIGNMENT_URL,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {ELEVENLABS_API_KEY}",
                },
                data={"text": transcript},
                files={
                    "file": (
                        os.path.basename(source_path),
                        audio_file,
                        "audio/wav",
                    )
                },
                timeout=180,
            )
    except (OSError, requests.RequestException) as exc:
        return None, f"SERVICE_UNAVAILABLE: Forced Alignment 请求失败：{exc}"
    if _is_auth_error(response.status_code, response.text):
        raise ElevenLabsAuthenticationError(
            f"ElevenLabs API Key 鉴权失败：HTTP {response.status_code}: "
            f"{response.text}"
        )
    if response.status_code in (429, 503):
        return None, f"RATE_LIMIT: HTTP {response.status_code}"
    if 500 <= response.status_code < 600:
        return None, f"SERVICE_UNAVAILABLE: HTTP {response.status_code}: {response.text}"
    if response.status_code != 200:
        return None, f"HTTP {response.status_code}: {response.text}"
    try:
        response_data = response.json()
    except Exception as exc:
        return None, f"Forced Alignment 响应 JSON 解析失败：{exc}"
    if not isinstance(response_data, dict):
        return None, "Forced Alignment 响应不是 JSON 对象"
    return response_data, ""


def _call_with_retries(callable_obj, label, error_class=RuntimeError):
    response_data = None
    error_message = ""
    for attempt in range(1, MAX_RETRIES + 1):
        response_data, error_message = callable_obj()
        if response_data:
            return response_data
        if "RATE_LIMIT" in error_message:
            wait_seconds = random.uniform(2, 4) * attempt
        else:
            wait_seconds = min(5, attempt)
        logger.warning(
            f"⚠️ {label} 请求重试 {attempt}/{MAX_RETRIES}: {error_message}"
        )
        if attempt < MAX_RETRIES:
            time.sleep(wait_seconds)
    if "RATE_LIMIT" in error_message or "SERVICE_UNAVAILABLE" in error_message:
        raise ElevenLabsServiceUnavailableError(
            f"{label} 请求失败：{error_message}"
        )
    raise error_class(f"{label} 请求失败：{error_message}")


def _wav_duration(source_path):
    try:
        with wave.open(source_path, "rb") as audio:
            return audio.getnframes() / audio.getframerate()
    except (OSError, EOFError, ValueError, ZeroDivisionError):
        return None


def _spoken_text(text):
    return EMOTION_TAG_RE.sub("", str(text or "")).strip()


def _has_alignable_characters(text):
    """剥掉情绪标签后还有没有能被 Forced Alignment 对齐的字符。

    这是 ``_forced_alignment_bounds`` 逐句取声学范围时的同一条判据。两处必须用
    同一个实现：否则批次入口放行、对齐处才报"没有有效发音字符"，重试同一批结果
    完全一样，同批正常台词全被连坐。
    """
    return any(
        unicodedata.category(character)[0] in {"L", "M", "N"}
        for character in _spoken_text(text)
    )


def _forced_alignment_transcript(inputs):
    texts = [_spoken_text(item.get("text")) for item in inputs]
    empty = [
        index for index, item in enumerate(inputs)
        if not _has_alignable_characters(item.get("text"))
    ]
    if empty:
        raise RuntimeError(f"Forced Alignment 存在空台词：{empty}")
    transcript_parts = []
    spans = []
    cursor = 0
    for index, text in enumerate(texts):
        if index:
            transcript_parts.append(" ")
            cursor += 1
        start = cursor
        transcript_parts.append(text)
        cursor += len(text)
        spans.append((start, cursor))
    return "".join(transcript_parts), texts, spans


def _wav_region_metrics(source_path, start, end):
    try:
        with wave.open(source_path, "rb") as audio:
            rate = audio.getframerate()
            channels = audio.getnchannels()
            width = audio.getsampwidth()
            start_frame = max(0, math.floor(start * rate))
            end_frame = min(audio.getnframes(), math.ceil(end * rate))
            if end_frame <= start_frame:
                return 0, 0.0
            audio.setpos(start_frame)
            raw = audio.readframes(end_frame - start_frame)
    except (OSError, EOFError, wave.Error) as exc:
        raise RuntimeError(f"完整对话 WAV 无法读取：{source_path}: {exc}") from exc
    if width != 2:
        raise RuntimeError(f"Forced Alignment 验收只支持 16-bit WAV：{source_path}")
    samples = array.array("h")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    if channels > 1:
        samples = array.array(
            "h",
            (
                int(sum(samples[offset:offset + channels]) / channels)
                for offset in range(0, len(samples), channels)
            ),
        )
    peak = max((abs(value) for value in samples), default=0)
    rms = math.sqrt(
        sum(value * value for value in samples) / max(1, len(samples))
    )
    return peak, rms


def _read_mono_samples(source_path):
    try:
        with wave.open(source_path, "rb") as audio:
            rate = audio.getframerate()
            channels = audio.getnchannels()
            width = audio.getsampwidth()
            raw = audio.readframes(audio.getnframes())
    except (OSError, EOFError, wave.Error) as exc:
        raise RuntimeError(f"完整对话 WAV 无法读取：{source_path}: {exc}") from exc
    if width != 2:
        raise RuntimeError(f"静音检测只支持 16-bit WAV：{source_path}")
    samples = array.array("h")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    if channels > 1:
        samples = array.array(
            "h",
            (
                int(sum(samples[offset:offset + channels]) / channels)
                for offset in range(0, len(samples), channels)
            ),
        )
    return samples, rate


def _rms_envelope(samples, rate):
    hop = max(1, int(rate * ENVELOPE_HOP_SECONDS))
    window = hop * ENVELOPE_WINDOW_HOPS
    envelope = []
    for offset in range(0, max(0, len(samples) - window), hop):
        frame = samples[offset:offset + window]
        envelope.append(
            math.sqrt(sum(value * value for value in frame) / len(frame))
        )
    return envelope


def _silence_threshold(envelope):
    peak = max(envelope)
    ordered = sorted(envelope)
    noise_floor = ordered[len(ordered) // 10]
    return max(
        peak * SILENCE_PEAK_RATIO,
        noise_floor * SILENCE_FLOOR_RATIO,
        SILENCE_ABSOLUTE_FLOOR,
    )


def _silence_runs(source_path):
    samples, rate = _read_mono_samples(source_path)
    envelope = _rms_envelope(samples, rate)
    if not envelope:
        raise RuntimeError(f"完整对话音频过短，无法做静音检测：{source_path}")
    threshold = _silence_threshold(envelope)
    runs = []
    run_start = None
    for index, value in enumerate(envelope):
        if value <= threshold:
            if run_start is None:
                run_start = index
        elif run_start is not None:
            runs.append(
                (run_start * ENVELOPE_HOP_SECONDS, index * ENVELOPE_HOP_SECONDS)
            )
            run_start = None
    if run_start is not None:
        runs.append((
            run_start * ENVELOPE_HOP_SECONDS,
            len(envelope) * ENVELOPE_HOP_SECONDS,
        ))
    return [
        run for run in runs
        if run[1] - run[0] >= MIN_SILENCE_RUN_SECONDS
    ]


def _silence_boundary(silence_runs, previous_end, next_start):
    low = min(previous_end, next_start) - BOUNDARY_SEARCH_PADDING_SECONDS
    high = max(previous_end, next_start) + BOUNDARY_SEARCH_PADDING_SECONDS
    midpoint = (previous_end + next_start) / 2
    candidates = [
        run for run in silence_runs if run[1] > low and run[0] < high
    ]
    if not candidates:
        return None, midpoint
    best = min(
        candidates,
        key=lambda run: abs(
            (max(run[0], low) + min(run[1], high)) / 2 - midpoint
        ),
    )
    clipped_start = max(best[0], low)
    clipped_end = min(best[1], high)
    return (clipped_start + clipped_end) / 2, midpoint


def _fallback_acoustic_boundary(
    midpoint, boundary_index, line_count, previous_boundaries, audio_duration
):
    """没有真实静音时，以相邻声学范围中点作为安全切点。

    连续快速对话不一定有 20ms 静音，但 Forced Alignment 仍能给出可信的句末/
    句首时间。这里保留每段至少 MIN_SEGMENT_SECONDS，并把切点夹在音频范围内，
    防止浮点抖动或极短重叠产生空片段、逆序片段。
    """
    previous_boundary = previous_boundaries[-1] if previous_boundaries else 0.0
    lower = previous_boundary + MIN_SEGMENT_SECONDS
    remaining_segments = line_count - boundary_index - 1
    upper = audio_duration - remaining_segments * MIN_SEGMENT_SECONDS
    if upper < lower:
        raise ForcedAlignmentCollapseError(
            "无静音兜底无法保留最短片段："
            f"index={boundary_index}, lower={lower:.6f}, upper={upper:.6f}, "
            f"audio_duration={audio_duration:.6f}, lines={line_count}"
        )
    return min(max(midpoint, lower), upper)


def _forced_alignment_bounds(source_path, inputs, response_data):
    transcript, spoken_texts, spans = _forced_alignment_transcript(inputs)
    characters = response_data.get("characters")
    if not isinstance(characters, list) or not characters:
        raise RuntimeError("Forced Alignment 响应缺少 characters")
    try:
        response_text = "".join(str(item["text"]) for item in characters)
    except (KeyError, TypeError) as exc:
        raise RuntimeError("Forced Alignment character 格式错误") from exc
    if response_text != transcript or len(characters) != len(transcript):
        raise RuntimeError(
            "Forced Alignment 返回字符与已知台词不一致："
            f"expected={transcript!r}, actual={response_text!r}"
        )

    try:
        global_loss = float(response_data["loss"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Forced Alignment 响应缺少有效 loss") from exc
    if not math.isfinite(global_loss):
        raise RuntimeError(f"Forced Alignment loss 非法：{global_loss}")

    acoustic_ranges = []
    line_validations = []
    for index, ((text_start, text_end), spoken_text) in enumerate(
        zip(spans, spoken_texts)
    ):
        timed_characters = []
        for position in range(text_start, text_end):
            character = transcript[position]
            category = unicodedata.category(character)
            if category[0] not in {"L", "M", "N"}:
                continue
            item = characters[position]
            try:
                start = float(item["start"])
                end = float(item["end"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"Forced Alignment 字符时间错误：index={index}, "
                    f"character={character!r}"
                ) from exc
            # 被邻近音素吞掉的字母会合法地返回 start == end。整句范围取
            # min(start)/max(end)，零宽字符不影响结果，不能据此判整批失败。
            if (
                not math.isfinite(start)
                or not math.isfinite(end)
                or start < 0
                or end < start
            ):
                raise RuntimeError(
                    f"Forced Alignment 字符时间非法：index={index}, "
                    f"character={character!r}, start={start}, end={end}"
                )
            timed_characters.append((start, end))
        if not timed_characters:
            raise RuntimeError(f"Forced Alignment 台词没有有效发音字符：index={index}")

        acoustic_start = min(start for start, _ in timed_characters)
        acoustic_end = max(end for _, end in timed_characters)
        # 塌缩要在能量校验之前判：被压成几十毫秒的句子往往正好落在两句之间的
        # 静音里，会先触发"近静音"报错，把不可重试的塌缩伪装成质量抖动。
        acoustic_duration = acoustic_end - acoustic_start
        if (
            acoustic_duration <= 0
            or len(timed_characters) / acoustic_duration
            > MAX_PLAUSIBLE_CHARS_PER_SECOND
        ):
            raise ForcedAlignmentCollapseError(
                f"Forced Alignment 边界塌缩：index={index}, "
                f"material_id={inputs[index].get('material_id')}, "
                f"chars={len(timed_characters)}, "
                f"duration={acoustic_duration:.6f}, "
                f"range={acoustic_start:.6f}:{acoustic_end:.6f}"
            )
        peak, rms = _wav_region_metrics(
            source_path, acoustic_start, acoustic_end
        )
        if peak < MIN_OUTPUT_PEAK or rms < MIN_OUTPUT_RMS:
            raise RuntimeError(
                f"Forced Alignment 检测到漏句或近静音：index={index}, "
                f"material_id={inputs[index].get('material_id')}, "
                f"range={acoustic_start:.6f}:{acoustic_end:.6f}, "
                f"peak={peak}, rms={rms:.2f}"
            )
        acoustic_ranges.append((acoustic_start, acoustic_end))
        line_validations.append({
            "dialogue_input_index": index,
            "material_id": inputs[index].get("material_id"),
            "spoken_text": spoken_text,
            "acoustic_start_time_seconds": round(acoustic_start, 6),
            "acoustic_end_time_seconds": round(acoustic_end, 6),
            "peak": peak,
            "rms": round(rms, 3),
        })

    audio_duration = _wav_duration(source_path)
    if audio_duration is None or audio_duration <= 0:
        raise RuntimeError(f"无法读取完整对话音频时长：{source_path}")
    boundary_validations = []
    if len(acoustic_ranges) == 1:
        bounds = [(0.0, audio_duration)]
    else:
        silence_runs = _silence_runs(source_path)
        boundaries = []
        for index in range(len(acoustic_ranges) - 1):
            current_end = acoustic_ranges[index][1]
            next_start = acoustic_ranges[index + 1][0]
            overlap = current_end - next_start
            if overlap > MAX_FORCED_ALIGNMENT_OVERLAP_SECONDS:
                raise RuntimeError(
                    f"Forced Alignment 相邻台词声学范围重叠：index={index}, "
                    f"current_end={current_end:.6f}, "
                    f"next_start={next_start:.6f}, overlap={overlap:.6f}"
                )
            boundary, midpoint = _silence_boundary(
                silence_runs, current_end, next_start
            )
            boundary_source = "silence_run"
            if boundary is None:
                boundary = _fallback_acoustic_boundary(
                    midpoint,
                    index,
                    len(acoustic_ranges),
                    boundaries,
                    audio_duration,
                )
                boundary_source = "forced_alignment_acoustic_midpoint"
                logger.warning(
                    "⚠️ 相邻台词没有可用静音，按 Forced Alignment 声学中点切分："
                    "index=%d, current_end=%.6f, next_start=%.6f, boundary=%.6f",
                    index, current_end, next_start, boundary,
                )
            if index and boundary <= boundaries[-1]:
                raise ForcedAlignmentCollapseError(
                    f"切分边界未按台词顺序递增：index={index}, "
                    f"boundary={boundary:.6f}, previous={boundaries[-1]:.6f}"
                )
            boundaries.append(boundary)
            boundary_validations.append({
                "boundary_index": index,
                "previous_acoustic_end_seconds": round(current_end, 6),
                "next_acoustic_start_seconds": round(next_start, 6),
                "forced_alignment_midpoint_seconds": round(midpoint, 6),
                # 保留旧字段名，兼容已有诊断/重切工具；新字段说明实际来源。
                "silence_boundary_seconds": round(boundary, 6),
                "selected_boundary_seconds": round(boundary, 6),
                "boundary_source": boundary_source,
                "shift_seconds": round(boundary - midpoint, 6),
            })
        bounds = [(0.0, boundaries[0])]
        for index in range(1, len(acoustic_ranges) - 1):
            bounds.append((boundaries[index - 1], boundaries[index]))
        bounds.append((boundaries[-1], audio_duration))

    validation = {
        "transcript": transcript,
        "global_loss": global_loss,
        "lines": line_validations,
        "boundaries": boundary_validations,
        "boundary_policy": BOUNDARY_POLICY,
    }
    return bounds, validation


def _alignment_bounds(voice_segments, alignment, expected_texts):
    if not isinstance(alignment, dict):
        raise RuntimeError("ElevenLabs 响应缺少逐字符 alignment")
    characters = alignment.get("characters")
    starts = alignment.get("character_start_times_seconds")
    ends = alignment.get("character_end_times_seconds")
    if not (
        isinstance(characters, list)
        and isinstance(starts, list)
        and isinstance(ends, list)
        and len(characters) == len(starts) == len(ends)
    ):
        raise RuntimeError("ElevenLabs 逐字符 alignment 数组长度不一致")
    expected_full_text = "".join(expected_texts)
    aligned_full_text = "".join(characters)
    if aligned_full_text != expected_full_text:
        raise RuntimeError(
            "ElevenLabs alignment 完整文本与 inputs 拼接结果不一致："
            f"expected={expected_full_text!r}, aligned={aligned_full_text!r}"
        )

    grouped = {index: [] for index in range(len(expected_texts))}
    for segment in voice_segments or []:
        index = segment.get("dialogue_input_index")
        if index not in grouped:
            raise RuntimeError(
                f"ElevenLabs voice_segments 包含非法 dialogue_input_index：{index}"
            )
        grouped[index].append(segment)

    bounds = []
    expected_character_start = 0
    for index, expected_text in enumerate(expected_texts):
        if not expected_text:
            raise RuntimeError(f"ElevenLabs Dialogue 输入存在空台词：index={index}")
        segments = grouped[index]
        if not segments:
            raise RuntimeError(f"ElevenLabs alignment 缺少台词：index={index}")
        try:
            character_ranges = sorted(
                (
                    int(segment["character_start_index"]),
                    int(segment["character_end_index"]),
                )
                for segment in segments
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"ElevenLabs alignment 字符索引错误：index={index}"
            ) from exc
        character_start = character_ranges[0][0]
        character_end = character_ranges[-1][1]
        expected_character_end = expected_character_start + len(expected_text)
        if (
            character_start != expected_character_start
            or character_end != expected_character_end
        ):
            raise RuntimeError(
                f"ElevenLabs alignment 未按 dialogue input 精确覆盖字符："
                f"index={index}, expected_range="
                f"{expected_character_start}:{expected_character_end}, "
                f"actual_range={character_start}:{character_end}"
            )
        cursor = expected_character_start
        for range_start, range_end in character_ranges:
            if range_start != cursor or range_end <= range_start:
                raise RuntimeError(
                    f"ElevenLabs alignment 字符分段存在空洞或重叠："
                    f"index={index}, range={range_start}:{range_end}, "
                    f"expected_start={cursor}"
                )
            cursor = range_end
        if not (0 <= character_start < character_end <= len(characters)):
            raise RuntimeError(
                f"ElevenLabs alignment 字符范围非法：index={index}, "
                f"range={character_start}:{character_end}"
            )
        aligned_text = "".join(characters[character_start:character_end])
        if aligned_text != expected_text:
            raise RuntimeError(
                f"ElevenLabs alignment 文本不一致：index={index}, "
                f"expected={expected_text!r}, aligned={aligned_text!r}"
            )
        try:
            start = float(starts[character_start])
            end = float(ends[character_end - 1])
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"ElevenLabs alignment 字符时间错误：index={index}"
            ) from exc
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise RuntimeError(
                f"ElevenLabs alignment 时间非法：index={index}, "
                f"start={start}, end={end}"
            )
        bounds.append((start, end))
        expected_character_start = expected_character_end
    return bounds


def _segment_bounds(
    voice_segments,
    input_count,
    source_path=None,
    alignment=None,
    expected_texts=None,
    expected_voice_ids=None,
):
    grouped = {index: [] for index in range(input_count)}
    for segment in voice_segments or []:
        index = segment.get("dialogue_input_index")
        if index not in grouped:
            raise RuntimeError(
                f"ElevenLabs voice_segments 包含非法 dialogue_input_index：{index}"
            )
        grouped[index].append(segment)

    missing = [index for index, segments in grouped.items() if not segments]
    if missing:
        raise RuntimeError(f"ElevenLabs 响应缺少台词分段：{missing}")

    # 官方把 voice_segments 明确定义为各个 dialogue input 的音频边界。
    # alignment 只用于验证字符索引和输入文本对应关系，不能反过来替代边界字段。
    raw_bounds = []
    for index in range(input_count):
        segments = grouped[index]
        if expected_voice_ids is not None:
            if len(expected_voice_ids) != input_count:
                raise RuntimeError("ElevenLabs 预期音色数量与 inputs 不一致")
            wrong_voices = sorted({
                str(segment.get("voice_id"))
                for segment in segments
                if segment.get("voice_id") != expected_voice_ids[index]
            })
            if wrong_voices:
                raise RuntimeError(
                    f"ElevenLabs voice segment 音色映射错误：index={index}, "
                    f"expected={expected_voice_ids[index]}, actual={wrong_voices}"
                )
        try:
            start = min(
                float(segment["start_time_seconds"]) for segment in segments
            )
            end = max(
                float(segment["end_time_seconds"]) for segment in segments
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"ElevenLabs 台词时间戳格式错误：dialogue_input_index={index}"
            ) from exc
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise RuntimeError(
                f"ElevenLabs 台词时间戳非法：index={index}, "
                f"start={start}, end={end}"
            )
        raw_bounds.append((start, end))

    if alignment is not None or expected_texts is not None:
        if expected_texts is None or len(expected_texts) != input_count:
            raise RuntimeError("ElevenLabs alignment 预期文本数量不匹配")
        alignment_bounds = _alignment_bounds(
            voice_segments, alignment, expected_texts
        )
        for index, ((segment_start, segment_end), (char_start, char_end)) in enumerate(
            zip(raw_bounds, alignment_bounds)
        ):
            if (
                abs(segment_start - char_start) > 0.005
                or abs(segment_end - char_end) > 0.005
            ):
                raise RuntimeError(
                    f"ElevenLabs voice_segments 与字符 alignment 不一致："
                    f"index={index}, segment={segment_start:.6f}:{segment_end:.6f}, "
                    f"alignment={char_start:.6f}:{char_end:.6f}"
                )

    audio_duration = _wav_duration(source_path)
    duration_limit = audio_duration or raw_bounds[-1][1]
    for index, (start, end) in enumerate(raw_bounds):
        if index:
            previous_end = raw_bounds[index - 1][1]
            if start < previous_end - 0.002:
                raise RuntimeError(
                    f"ElevenLabs 相邻 dialogue input 时间范围重叠：index={index}, "
                    f"start={start:.6f}, previous_end={previous_end:.6f}"
                )
        if audio_duration is not None and end > audio_duration + 0.05:
            raise RuntimeError(
                f"ElevenLabs 台词时间戳超出音频长度：index={index}, "
                f"end={end:.6f}, duration={audio_duration:.6f}"
            )
    if duration_limit < input_count * MIN_SEGMENT_SECONDS:
        raise RuntimeError(
            f"完整对话音频过短，无法切成 {input_count} 条：{duration_limit:.6f}s"
        )
    if input_count == 1:
        # 单句没有串入相邻角色的风险，保留完整响应可避免裁掉模型附带的
        # 句首/句尾气声；多句才必须严格使用 dialogue input 边界。
        return [(0.0, duration_limit)]
    return raw_bounds


def _raw_segment_bounds(voice_segments, input_count):
    grouped = {index: [] for index in range(input_count)}
    for segment in voice_segments or []:
        index = segment.get("dialogue_input_index")
        if index in grouped:
            grouped[index].append(segment)
    result = []
    for index in range(input_count):
        segments = grouped[index]
        if not segments:
            result.append(None)
            continue
        result.append({
            "start_time_seconds": min(
                float(segment["start_time_seconds"]) for segment in segments
            ),
            "end_time_seconds": max(
                float(segment["end_time_seconds"]) for segment in segments
            ),
        })
    return result


def _write_timing_metadata(
    metadata_path,
    batch_payload,
    response_data,
    source_path,
    bounds,
    forced_alignment_response,
    forced_alignment_validation,
):
    inputs = batch_payload["inputs"]
    voice_segments = response_data.get("voice_segments") or []
    raw_bounds = _raw_segment_bounds(voice_segments, len(inputs))
    response_without_audio = {
        key: value for key, value in response_data.items()
        if key != "audio_base64"
    }
    timing_inputs = []
    for index, (input_item, final_bound) in enumerate(zip(inputs, bounds)):
        acoustic_line = forced_alignment_validation["lines"][index]
        timing_inputs.append({
            "dialogue_input_index": index,
            "material_id": input_item["material_id"],
            "speaker_id": input_item.get("speaker_id"),
            "text": input_item.get("text"),
            "raw_timestamp": raw_bounds[index],
            "forced_alignment": {
                "start_time_seconds": acoustic_line[
                    "acoustic_start_time_seconds"
                ],
                "end_time_seconds": acoustic_line[
                    "acoustic_end_time_seconds"
                ],
                "peak": acoustic_line["peak"],
                "rms": acoustic_line["rms"],
            },
            "final_split": {
                "start_time_seconds": round(final_bound[0], 6),
                "end_time_seconds": round(final_bound[1], 6),
            },
        })
    split_policy = {
        "timestamp_source": "forced_alignment_characters",
        "text_validation": "known_text_forced_alignment",
        "ttd_metadata_validation": "alignment_exact_input_character_ranges",
        "voice_validation": "voice_segment_voice_id_matches_input",
        "boundary_policy": BOUNDARY_POLICY,
        "sample_policy": "48khz_floor_start_ceil_end",
        "overlap_between_output_files_seconds": 0,
        "fade_seconds": SEGMENT_FADE_SECONDS,
    }

    metadata = {
        "version": TIMING_METADATA_VERSION,
        "provider": "elevenlabs",
        "model_id": batch_payload.get("model_id", MODEL_ID),
        "output_format": OUTPUT_FORMAT,
        "source_file": os.path.basename(source_path),
        "source_size": os.path.getsize(source_path),
        "audio_duration_seconds": round(_wav_duration(source_path), 6),
        "split_policy": split_policy,
        "inputs": timing_inputs,
        "voice_segments": voice_segments,
        "api_response_without_audio": response_without_audio,
        "forced_alignment_response": forced_alignment_response,
        "forced_alignment_validation": forced_alignment_validation,
    }
    temp_path = metadata_path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as file_obj:
        json.dump(metadata, file_obj, ensure_ascii=False, indent=2)
    os.replace(temp_path, metadata_path)


def _run_ffmpeg_split(source_path, destination_path, start, end):
    try:
        with wave.open(source_path, "rb") as source_audio:
            source_rate = source_audio.getframerate()
            source_frames = source_audio.getnframes()
    except (OSError, EOFError, wave.Error) as exc:
        raise RuntimeError(f"完整对话 WAV 无法读取：{source_path}: {exc}") from exc
    if source_rate != 48000:
        raise RuntimeError(
            f"完整对话 WAV 采样率错误：{source_path} -> {source_rate}Hz"
        )
    start_sample = max(0, math.floor(start * source_rate))
    end_sample = min(source_frames, math.ceil(end * source_rate))
    sample_count = end_sample - start_sample
    if sample_count <= 0:
        raise RuntimeError(f"非法音频分段：start={start}, end={end}")
    duration = sample_count / source_rate

    temp_path = destination_path + ".tmp.wav"
    filters = [
        f"atrim=start_sample={start_sample}:end_sample={end_sample}",
        "asetpts=PTS-STARTPTS",
    ]
    fade_seconds = min(SEGMENT_FADE_SECONDS, duration / 4)
    if fade_seconds > 0:
        fade_out_start = max(0.0, duration - fade_seconds)
        filters.extend([
            f"afade=t=in:st=0:d={fade_seconds:.6f}",
            f"afade=t=out:st={fade_out_start:.6f}:d={fade_seconds:.6f}",
        ])
    audio_filter = ",".join(filters)
    command = [
        "ffmpeg", "-y", "-loglevel", "error", "-i", source_path,
        "-af", audio_filter,
        "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", temp_path,
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise RuntimeError(f"FFmpeg 切割失败：{result.stderr.strip()}")
    os.replace(temp_path, destination_path)
    _validate_split_audio(destination_path, duration)


def _validate_split_audio(path, expected_duration):
    def fail(message):
        try:
            os.remove(path)
        except OSError:
            pass
        raise RuntimeError(message)

    try:
        with wave.open(path, "rb") as audio:
            rate = audio.getframerate()
            channels = audio.getnchannels()
            width = audio.getsampwidth()
            frames = audio.getnframes()
            raw = audio.readframes(frames)
    except (OSError, EOFError, wave.Error) as exc:
        fail(f"切分 WAV 无法读取：{path}: {exc}")
    if (rate, channels, width) != (48000, 1, 2):
        fail(
            f"切分 WAV 格式错误：{path} -> {rate}Hz/{channels}ch/{width * 8}bit"
        )
    actual_duration = frames / rate if rate else 0.0
    if frames <= 0 or abs(actual_duration - expected_duration) > 0.03:
        fail(
            f"切分 WAV 时长错误：{path} -> actual={actual_duration:.6f}, "
            f"expected={expected_duration:.6f}"
        )
    samples = array.array("h")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    peak = max((abs(value) for value in samples), default=0)
    rms = math.sqrt(
        sum(value * value for value in samples) / max(1, len(samples))
    )
    if peak < MIN_OUTPUT_PEAK or rms < MIN_OUTPUT_RMS:
        fail(
            f"切分 WAV 疑似无声：{path} -> peak={peak}, rms={rms:.2f}"
        )


def _load_reusable_batch_attempt(combined_dir, batch_id):
    combined_path = os.path.join(combined_dir, f"{batch_id}.wav")
    timing_path = os.path.join(combined_dir, f"{batch_id}.timestamps.json")
    if os.path.isfile(combined_path) and os.path.isfile(timing_path):
        try:
            with open(timing_path, "r", encoding="utf-8") as file_obj:
                timing_payload = json.load(file_obj)
            response_data = timing_payload.get("api_response_without_audio")
            if (
                isinstance(response_data, dict)
                and timing_payload.get("source_size")
                == os.path.getsize(combined_path)
            ):
                return combined_path, response_data
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass

    # 兼容旧版隐藏文件；新执行不再创建这些名称。
    for source_name, response_name in (
        (f"{batch_id}.attempt.wav", f"{batch_id}.api-response-without-audio.json"),
        (f".{batch_id}.attempt.wav", f".{batch_id}.api-response-without-audio.json"),
    ):
        source_path = os.path.join(combined_dir, source_name)
        response_path = os.path.join(combined_dir, response_name)
        if not (os.path.isfile(source_path) and os.path.isfile(response_path)):
            continue
        try:
            with open(response_path, "r", encoding="utf-8") as file_obj:
                response_data = json.load(file_obj)
            if isinstance(response_data, dict):
                return source_path, response_data
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    return None


def _consume_batch_json(json_path, stage_task_root, owner_check=None):
    """WAV 已原子发布后删除成功批次 JSON；失败批次 JSON 保持原位。"""
    if owner_check:
        owner_check()
    try:
        os.remove(json_path)
    except FileNotFoundError:
        # 接管/重复投递场景下，另一个已验收的执行实例可能已经消费了同一
        # 个 JSON。此时 WAV 已由调用方先完成验收，删除是幂等操作，不应把
        # 正常完成误报成 BatchJsonConsumptionError。
        logger.info(f"♻️ 批次 JSON 已被其他执行实例消费，视为成功：{json_path}")
        return
    except OSError as exc:
        raise BatchJsonConsumptionError(
            f"成功批次 JSON 删除失败：{json_path}: {exc}"
        ) from exc


def _retire_finished_batch(
    json_path, combined_dir, batch_id, stage_task_root, owner_check=None
):
    """批次成功发布后删除长音频、时间戳、暂存 WAV 和批次 JSON。"""
    if owner_check:
        owner_check()
    if not KEEP_DIALOGUE_ARTIFACTS:
        for filename in (f"{batch_id}.wav", f"{batch_id}.timestamps.json"):
            if owner_check:
                owner_check()
            path = os.path.join(combined_dir, filename)
            try:
                os.remove(path)
            except OSError:
                pass
    _consume_batch_json(json_path, stage_task_root, owner_check=owner_check)


def _discard_legacy_consumed_json(stage_task_root):
    """旧版已消费 JSON 对应的 WAV 已发布后，不再恢复，直接清理兼容缓存。"""
    for dirname in (CONSUMED_JSON_DIR_NAME, LEGACY_CONSUMED_JSON_DIR_NAME):
        consumed_dir = os.path.join(stage_task_root, dirname)
        if os.path.isdir(consumed_dir):
            shutil.rmtree(consumed_dir)


def _retain_failed_batch(stage_task_root, task_info, error):
    """将失败批次 JSON 和错误说明复制到可见缓存，源 JSON 仍保留供重跑。"""
    _index, _total, json_path, node_id, language, _run_task_root, *_ = task_info
    failed_dir = os.path.join(
        stage_task_root, FAILED_BATCH_DIR_NAME, node_id, language
    )
    os.makedirs(failed_dir, exist_ok=True)
    batch_name = os.path.basename(json_path)
    cached_json = os.path.join(failed_dir, batch_name)
    if os.path.isfile(json_path):
        shutil.copy2(json_path, cached_json)
    error_path = os.path.join(
        failed_dir, f"{Path(batch_name).stem}.error.txt"
    )
    with open(error_path, "w", encoding="utf-8") as file_obj:
        file_obj.write(str(error))
        file_obj.write("\n")
    return cached_json


def _clear_failed_batch(stage_task_root, task_info):
    """重跑成功后删除该批次旧的失败副本，避免缓存混入成功批次。"""
    _index, _total, json_path, node_id, language, _run_task_root, *_ = task_info
    failed_dir = Path(
        stage_task_root, FAILED_BATCH_DIR_NAME, node_id, language
    )
    batch_name = os.path.basename(json_path)
    for path in (
        failed_dir / batch_name,
        failed_dir / f"{Path(batch_name).stem}.error.txt",
    ):
        try:
            path.unlink()
        except OSError:
            pass
    _prune_empty_stage_parents(failed_dir, stage_task_root)


def _completed_batch_survives_missing_json(task_info):
    """旧/重复执行已消费 JSON 时，依据持久化成功状态安全跳过。"""
    _index, _total, json_path, node_id, language, run_task_root, *_ = task_info
    task_root = _task_root_from_stage(run_task_root)
    item_key = f"{node_id}/{language}/{Path(json_path).stem}"
    item = ResumeState(task_root).get_item("relay_audio", item_key)
    if item.get("status") != "success":
        return False
    artifacts = item.get("artifacts") or []
    if not artifacts or not all(valid_wav(path) for path in artifacts):
        logger.warning(
            "⚠️ 批次 JSON 不存在，但 ResumeState 成功记录的成品不完整，继续重跑：%s",
            json_path,
        )
        return False
    logger.info(
        "♻️ 批次 JSON 已被其他执行实例消费，且持久化成功记录与 WAV 均有效，跳过：%s",
        json_path,
    )
    return True


def _cleanup_batch_attempt_artifacts(combined_dir, batch_id, owner_check=None):
    if owner_check:
        owner_check()
    for pattern in (f"{batch_id}.attempt-*", f".{batch_id}.attempt-*"):
        for path in Path(combined_dir).glob(pattern):
            if owner_check:
                owner_check()
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
    for filename in (
        f"{batch_id}.attempt.wav",
        f"{batch_id}.api-response-without-audio.json",
        f".{batch_id}.attempt.wav",
        f".{batch_id}.api-response-without-audio.json",
    ):
        try:
            os.remove(os.path.join(combined_dir, filename))
        except OSError:
            pass


def _drop_non_verbal_inputs(batch_payload, json_path):
    """送 API 前剔除非语言占位句，避免一句无效内容连坐整批。

    上游 step 4 已经过滤过这类台词，这里是运行时防线：占位句会让 ElevenLabs
    整批返回 400，或让 Forced Alignment 把边界压塌，十几条正常台词跟着一起失败。
    重跑历史批次 JSON 时也会走到这里，所以过滤结果要落盘让下游看到同一份输入。
    """
    inputs = batch_payload.get("inputs") or []
    # 占位判据是启发式的，小说体量大、写法层出不穷，难免有漏网的。但"剥掉情绪
    # 标签后还剩不剩发音字符"是机械可判的，正好是 Forced Alignment 的对齐前提。
    # 两条都在这里兜住，漏判就只丢这一条台词，不会连坐整批。
    verdicts = [
        has_pronounceable_content(item.get("text"))
        and _has_alignable_characters(item.get("text"))
        for item in inputs
    ]
    if all(verdicts):
        return batch_payload
    kept = [item for item, keep in zip(inputs, verdicts) if keep]
    dropped = [
        f"{item.get('material_id')}={item.get('text')!r}"
        for item, keep in zip(inputs, verdicts) if not keep
    ]
    if not kept:
        # 纯 UI/图片占位批次不应连坐失败，也不应请求 TTS。保留一个空 inputs
        # 的已消费批次，由 dispatch 统一记录成功（无音频产物）。
        payload = dict(batch_payload)
        payload["inputs"] = []
        payload.pop("_resume_input_digest", None)
        atomic_write_json(json_path, payload)
        logger.warning(
            "⚠️ 批次全部为非语言占位台词，已安全跳过：%s；%s",
            os.path.basename(json_path), "；".join(dropped),
        )
        return payload
    # dialogue_input_index 是切分边界的对齐依据，剔除后必须重新连续编号。
    filtered = [dict(item) for item in kept]
    for position, item in enumerate(filtered):
        item["dialogue_input_index"] = position
    payload = dict(batch_payload)
    payload["inputs"] = filtered
    # 内容变了，旧摘要不再代表这批输入，交给上层按新 payload 重算。
    payload.pop("_resume_input_digest", None)
    atomic_write_json(json_path, payload)
    logger.warning(
        "⚠️ 批次含 %d 条非语言占位台词，已剔除后继续合成：%s -> %s",
        len(dropped), os.path.basename(json_path), "；".join(dropped),
    )
    return payload


def _dispatch_batch(task_info):
    """按 provider 分流：MiniMax 走单句合成，其余走 Dialogue 批量切分。"""
    _task_owner_check(task_info)
    json_path = task_info[2]
    try:
        with open(json_path, "r", encoding="utf-8") as file_obj:
            batch_payload = json.load(file_obj)
    except FileNotFoundError as exc:
        if _completed_batch_survives_missing_json(task_info):
            return f"✅ 批次已由其他执行实例完成：{os.path.basename(json_path)}"
        raise RuntimeError(f"批次 JSON 无法读取：{json_path}: {exc}") from exc
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"批次 JSON 无法读取：{json_path}: {exc}") from exc
    batch_payload = _drop_non_verbal_inputs(batch_payload, json_path)
    if not batch_payload.get("inputs"):
        run_task_root = task_info[5]
        _consume_batch_json(
            json_path, run_task_root,
            owner_check=_task_owner_callback(task_info),
        )
        result = (
            f"✅ [{task_info[0]}/{task_info[1]}] 非语言占位批次已跳过："
            f"{task_info[3]}/{Path(json_path).stem}"
        )
    else:
        provider = batch_payload.get("provider")
        if provider == PROVIDER_MINIMAX:
            result = _process_minimax_batch(task_info)
        else:
            result = _process_one_batch(task_info)
    _task_owner_check(task_info)

    # 只有所有目标 WAV 已发布到主目录并再次通过验收，才写 success 状态。
    _index, _total, _json_path, node_id, language, run_task_root, *_ = task_info
    task_root = _task_root_from_stage(run_task_root)
    item_key = batch_payload.get("_resume_item_key") or (
        f"{node_id}/{language}/"
        f"{batch_payload.get('batch_id') or Path(json_path).stem}"
    )
    input_digest = batch_payload.get("_resume_input_digest") or stable_digest(
        "elevenlabs-dialogue-json-v2",
        {key: value for key, value in batch_payload.items() if not key.startswith("_resume_")},
        file_digest(
            os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "generate_elevenlabs_dialogue_json.py",
            )
        ),
        file_digest(__file__),
    )
    artifacts = [
        os.path.join(
            task_root, node_id, "audios", f"{language}{FINAL_AUDIO_DIR_SUFFIX}",
            f"{item['material_id']}.wav",
        )
        for item in batch_payload.get("inputs") or []
    ]
    # 审核修复三次仍被拒绝的台词按产品约定发布合法的静音 WAV，因此这里验收
    # WAV 结构；非静音批次的响度/边界已在各处理函数内部完成更严格校验。
    invalid = [path for path in artifacts if not valid_wav(path)]
    if invalid:
        raise RuntimeError(
            "批次返回成功但主目录成品验收失败：" + "，".join(invalid[:5])
        )
    _task_owner_check(task_info)
    ResumeState(task_root).mark_item(
        "relay_audio", item_key, input_digest,
        status="success", artifacts=artifacts,
    )
    return result


def _process_one_batch(task_info):
    index, total, json_path, node_id, language, run_task_root, *_ = task_info
    _task_owner_check(task_info)
    with open(json_path, "r", encoding="utf-8") as file_obj:
        batch_payload = json.load(file_obj)

    combined_dir = os.path.join(
        run_task_root, node_id,
        f"{language}{DIALOGUE_DIR_SUFFIX}",
    )
    output_dir = os.path.join(
        run_task_root, node_id, "audios",
        f"{language}{FINAL_AUDIO_DIR_SUFFIX}",
    )
    os.makedirs(combined_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    batch_id = batch_payload.get("batch_id") or Path(json_path).stem
    combined_path = os.path.join(combined_dir, f"{batch_id}.wav")
    inputs = batch_payload["inputs"]
    timing_path = os.path.join(combined_dir, f"{batch_id}.timestamps.json")
    quality_errors = []
    quality_retry_limit = max(1, MAX_QUALITY_RETRIES)

    # 部分成功发布后，暂存目录仍保留一份已验收单句。重跑时 Step 4 会重新
    # 生成批次 JSON，但这里可直接消费它，避免对已成功批次再次付费生成。
    reusable_clips = [
        os.path.join(output_dir, f"{item['material_id']}.wav")
        for item in inputs
    ]
    if reusable_clips and all(
        _is_valid_clip(path, expected_input=item)
        for path, item in zip(reusable_clips, inputs)
    ):
        _publish_batch_clips(
            run_task_root, node_id, language, inputs, output_dir,
            owner_check=_task_owner_callback(task_info),
        )
        _consume_batch_json(
            json_path, run_task_root, owner_check=_task_owner_callback(task_info)
        )
        return (
            f"✅ [{index}/{total}] ElevenLabs 对话复用已验收单句："
            f"{node_id}/{batch_id} ({len(inputs)} 条台词)"
        )

    reusable_attempt = _load_reusable_batch_attempt(combined_dir, batch_id)
    if reusable_attempt is not None:
        reusable_source, reusable_response = reusable_attempt
        reuse_dir = os.path.join(combined_dir, f"{batch_id}.attempt-0-resume")
        reuse_clips_dir = os.path.join(reuse_dir, "clips")
        if os.path.isdir(reuse_dir):
            shutil.rmtree(reuse_dir)
        os.makedirs(reuse_clips_dir, exist_ok=True)
        try:
            _segment_bounds(
                reusable_response.get("voice_segments"),
                len(inputs),
                reusable_source,
                alignment=reusable_response.get("alignment"),
                expected_texts=[item["text"] for item in inputs],
                expected_voice_ids=[item["voice_id"] for item in inputs],
            )
            transcript, _, _ = _forced_alignment_transcript(inputs)
            forced_alignment_response = _call_with_retries(
                lambda: _call_forced_alignment_api(
                    reusable_source, transcript
                ),
                f"ElevenLabs Forced Alignment {os.path.basename(json_path)}",
                error_class=ElevenLabsForcedAlignmentRequestError,
            )
            with open(
                os.path.join(reuse_dir, "forced-alignment-response.json"),
                "w",
                encoding="utf-8",
            ) as file_obj:
                json.dump(
                    forced_alignment_response,
                    file_obj,
                    ensure_ascii=False,
                    indent=2,
                )
            bounds, forced_alignment_validation = _forced_alignment_bounds(
                reusable_source, inputs, forced_alignment_response
            )
            staged_paths = []
            for input_item, (start, end) in zip(inputs, bounds):
                staged_path = os.path.join(
                    reuse_clips_dir, f"{input_item['material_id']}.wav"
                )
                _run_ffmpeg_split(
                    reusable_source, staged_path, start, end
                )
                staged_paths.append((
                    staged_path,
                    os.path.join(
                        output_dir, f"{input_item['material_id']}.wav"
                    ),
                ))
            if os.path.abspath(reusable_source) != os.path.abspath(combined_path):
                os.replace(reusable_source, combined_path)
            _write_timing_metadata(
                timing_path,
                batch_payload,
                reusable_response,
                combined_path,
                bounds,
                forced_alignment_response,
                forced_alignment_validation,
            )
            os.makedirs(output_dir, exist_ok=True)
            for staged_path, destination_path in staged_paths:
                os.makedirs(os.path.dirname(destination_path), exist_ok=True)
                os.replace(staged_path, destination_path)
            _publish_batch_clips(
                run_task_root, node_id, language, inputs, output_dir,
                owner_check=_task_owner_callback(task_info),
            )
            _cleanup_batch_attempt_artifacts(
                combined_dir, batch_id,
                owner_check=_task_owner_callback(task_info),
            )
            _retire_finished_batch(
                json_path, combined_dir, batch_id, run_task_root,
                owner_check=_task_owner_callback(task_info),
            )
            return (
                f"✅ [{index}/{total}] ElevenLabs 对话完成："
                f"{node_id}/{batch_id} ({len(inputs)} 条台词，"
                "复用现有完整 WAV，边界=Forced Alignment)"
            )
        except ElevenLabsRequestRejectedError:
            raise
        except (ElevenLabsAuthenticationError, ElevenLabsServiceUnavailableError) as exc:
            # 认证/服务错误也属于当前批次的有限重试；达到批次重试上限后由
            # 外层保留失败 JSON 并跳过，不再等待平台下一次执行。
            quality_errors.append(f"复用现有 WAV 请求失败：{exc}")
            logger.warning("⚠️ 复用现有 WAV 的 ElevenLabs 请求失败，将重新生成：%s", exc)
        except ForcedAlignmentCollapseError as exc:
            # 塌缩不是音频问题，重新生成整批同样会塌缩，直接逐句重合成。
            return _handle_collapsed_batch(
                exc, batch_payload, inputs, output_dir, combined_dir,
                batch_id, json_path, node_id, language, run_task_root,
                index, total, owner_check=_task_owner_callback(task_info),
            )
        except Exception as exc:
            quality_errors.append(f"复用现有 WAV 失败：{exc}")
            logger.warning(
                f"⚠️ 现有完整 WAV 未通过 Forced Alignment，"
                f"将重新生成：{os.path.basename(json_path)} -> {exc}"
            )

    for quality_attempt in range(1, quality_retry_limit + 1):
        _task_owner_check(task_info)
        batch_payload["_quality_attempt"] = quality_attempt
        attempt_dir = os.path.join(
            combined_dir, f"{batch_id}.attempt-{quality_attempt}"
        )
        clips_dir = os.path.join(attempt_dir, "clips")
        attempt_path = os.path.join(attempt_dir, "dialogue.wav")
        os.makedirs(clips_dir, exist_ok=True)
        try:
            response_data = _call_with_retries(
                lambda: _call_dialogue_api(batch_payload),
                f"ElevenLabs Dialogue {os.path.basename(json_path)}",
            )
            _task_owner_check(task_info)
            if _usage_tracker:
                try:
                    _usage_tracker.add_tts(
                        sum(len(item.get("text", "")) for item in inputs),
                        MODEL_ID,
                        provider="elevenlabs-relay",
                    )
                except Exception as exc:
                    logger.warning(f"⚠️ TTS 用量记录失败，音频仍继续验收：{exc}")

            response_without_audio = {
                key: value for key, value in response_data.items()
                if key != "audio_base64"
            }
            with open(
                os.path.join(attempt_dir, "text-to-dialogue-response.json"),
                "w",
                encoding="utf-8",
            ) as file_obj:
                json.dump(
                    response_without_audio,
                    file_obj,
                    ensure_ascii=False,
                    indent=2,
                )

            audio_bytes = _decode_dialogue_wav(response_data, json_path)
            with open(attempt_path, "wb") as file_obj:
                file_obj.write(audio_bytes)

            # Text-to-Dialogue 自带 metadata 只验证 input、字符和音色映射，
            # 不再作为最终声学切分边界。
            _segment_bounds(
                response_data.get("voice_segments"),
                len(inputs),
                attempt_path,
                alignment=response_data.get("alignment"),
                expected_texts=[item["text"] for item in inputs],
                expected_voice_ids=[item["voice_id"] for item in inputs],
            )

            transcript, _, _ = _forced_alignment_transcript(inputs)
            forced_alignment_response = _call_with_retries(
                lambda: _call_forced_alignment_api(attempt_path, transcript),
                f"ElevenLabs Forced Alignment {os.path.basename(json_path)}",
                error_class=ElevenLabsForcedAlignmentRequestError,
            )
            _task_owner_check(task_info)
            with open(
                os.path.join(attempt_dir, "forced-alignment-response.json"),
                "w",
                encoding="utf-8",
            ) as file_obj:
                json.dump(
                    forced_alignment_response,
                    file_obj,
                    ensure_ascii=False,
                    indent=2,
                )
            bounds, forced_alignment_validation = _forced_alignment_bounds(
                attempt_path, inputs, forced_alignment_response
            )

            staged_paths = []
            for input_item, (start, end) in zip(inputs, bounds):
                staged_path = os.path.join(
                    clips_dir, f"{input_item['material_id']}.wav"
                )
                _run_ffmpeg_split(attempt_path, staged_path, start, end)
                staged_paths.append((
                    staged_path,
                    os.path.join(
                        output_dir, f"{input_item['material_id']}.wav"
                    ),
                ))

            os.replace(attempt_path, combined_path)
            _write_timing_metadata(
                timing_path,
                batch_payload,
                response_data,
                combined_path,
                bounds,
                forced_alignment_response,
                forced_alignment_validation,
            )
            os.makedirs(output_dir, exist_ok=True)
            for staged_path, destination_path in staged_paths:
                os.makedirs(os.path.dirname(destination_path), exist_ok=True)
                os.replace(staged_path, destination_path)

            _publish_batch_clips(
                run_task_root, node_id, language, inputs, output_dir,
                owner_check=_task_owner_callback(task_info),
            )
            _task_owner_check(task_info)

            _cleanup_batch_attempt_artifacts(
                combined_dir, batch_id,
                owner_check=_task_owner_callback(task_info),
            )
            _retire_finished_batch(
                json_path, combined_dir, batch_id, run_task_root,
                owner_check=_task_owner_callback(task_info),
            )
            return (
                f"✅ [{index}/{total}] ElevenLabs 对话完成："
                f"{node_id}/{batch_id} ({len(inputs)} 条台词，"
                "边界=Forced Alignment，"
                f"质量尝试={quality_attempt})"
            )
        except ForcedAlignmentCollapseError as exc:
            # 重试同一批只会换个塌缩位置，所以不消耗质量尝试次数。
            return _handle_collapsed_batch(
                exc, batch_payload, inputs, output_dir, combined_dir,
                batch_id, json_path, node_id, language, run_task_root,
                index, total, owner_check=_task_owner_callback(task_info),
            )
        except ElevenLabsRequestRejectedError as exc:
            logger.warning(
                f"🚧 Dialogue 批次触发 403，开始逐句精准定位和自动修复："
                f"{node_id}/{batch_id}"
            )
            recovery = _recover_rejected_dialogue_batch(
                batch_payload, output_dir, run_task_root, exc,
                owner_check=_task_owner_callback(task_info),
            )
            _publish_batch_clips(
                run_task_root, node_id, language, inputs, output_dir,
                owner_check=_task_owner_callback(task_info),
            )
            _cleanup_batch_attempt_artifacts(
                combined_dir, batch_id,
                owner_check=_task_owner_callback(task_info),
            )
            _retire_finished_batch(
                json_path, combined_dir, batch_id, run_task_root,
                owner_check=_task_owner_callback(task_info),
            )
            return (
                f"✅ [{index}/{total}] ElevenLabs 审核恢复完成："
                f"{node_id}/{batch_id} ({len(inputs)} 条台词，"
                f"原文通过={recovery['original_success']}，"
                f"修复通过={recovery['repaired']}，"
                f"静音跳过={recovery['skipped']})"
            )
        except (ElevenLabsAuthenticationError, ElevenLabsServiceUnavailableError) as exc:
            quality_errors.append(str(exc))
            logger.warning(
                f"⚠️ ElevenLabs 对话请求失败，继续批次重试 "
                f"{quality_attempt}/{quality_retry_limit}: {exc}"
            )
            if quality_attempt < quality_retry_limit:
                time.sleep(min(3, quality_attempt))
            continue
        except Exception as exc:
            if _is_stage_lease_lost(exc):
                raise
            quality_errors.append(str(exc))
            logger.warning(
                f"⚠️ ElevenLabs 对话质量验收失败 "
                f"{quality_attempt}/{quality_retry_limit}: "
                f"{os.path.basename(json_path)} -> {exc}"
            )
            if quality_attempt < quality_retry_limit:
                time.sleep(min(3, quality_attempt))

    raise RuntimeError(
        f"ElevenLabs 批次连续 {quality_retry_limit} 次未通过验收："
        f"{json_path}；" + "；".join(quality_errors)
    )


def _scan_batches(task_root):
    tasks = []
    for root, _, files in os.walk(task_root):
        dir_name = os.path.basename(root)
        suffix = "-elevenlabs-dialogue-json"
        if not dir_name.endswith(suffix):
            continue
        language = dir_name[:-len(suffix)]
        if language not in TARGET_LOCALES:
            continue
        node_id = Path(root).parent.name
        for filename in sorted(files):
            if filename.endswith(".json"):
                tasks.append((os.path.join(root, filename), node_id, language))
    return tasks


def _latest_resumable_stage(task_root):
    if os.getenv("ELEVENLABS_RESUME_STAGE", "1").strip().lower() in {
        "0", "false", "no", "off",
    }:
        return None
    candidates = []
    stage_paths = [
        *Path(task_root).glob(f"{STAGE_DIR_PREFIX}*"),
        *Path(task_root).glob(f"{LEGACY_STAGE_DIR_PREFIX}*"),
    ]
    for path in stage_paths:
        if not path.is_dir():
            continue
        if (
            (path / OUTPUT_BACKUP_DIR_NAME).exists()
            or (path / LEGACY_OUTPUT_BACKUP_DIR_NAME).exists()
        ):
            continue
        # MiniMax 单句任务只产出最终音频目录，没有 dialogue 中间目录。
        if (
            not (path / FAILED_BATCH_DIR_NAME).exists()
            and not (path / CONSUMED_JSON_DIR_NAME).exists()
            and not (path / LEGACY_CONSUMED_JSON_DIR_NAME).exists()
            and not any(path.glob(f"*/*{DIALOGUE_DIR_SUFFIX}"))
            and not any(path.glob(f"*/audios/*{FINAL_AUDIO_DIR_SUFFIX}"))
        ):
            continue
        candidates.append(path)
    if not candidates:
        return None
    return str(max(candidates, key=lambda item: item.stat().st_mtime))


def _make_stage_visible(stage_task_root):
    """续跑旧版隐藏缓存时原子改名为可见目录。"""
    path = Path(stage_task_root)
    if not path.name.startswith(LEGACY_STAGE_DIR_PREFIX):
        return str(path)
    suffix = path.name[len(LEGACY_STAGE_DIR_PREFIX):]
    destination = path.with_name(f"{STAGE_DIR_PREFIX}{suffix}")
    counter = 1
    while destination.exists():
        destination = path.with_name(f"{STAGE_DIR_PREFIX}{suffix}-{counter}")
        counter += 1
    os.replace(path, destination)
    logger.info(f"👁️ 旧隐藏暂存目录已改为可见目录：{destination}")
    return str(destination)


def _commit_partial_staged_outputs(stage_task_root, task_root, owner_check=None):
    """部分批次失败时只发布已成功生成的单条 WAV。

    不替换整个语言目录：首次生成的失败项保持不存在；重跑时失败项保留旧文件，
    只有本轮真正成功且通过验收的文件才原子覆盖。
    """
    committed = 0
    stage_root = Path(stage_task_root)
    source_paths = sorted(stage_root.glob("*/audios/*-audio/*.wav"))
    source_parents = {source_path.parent for source_path in source_paths}
    for source_path in source_paths:
        if owner_check:
            owner_check()
        relative_path = source_path.relative_to(stage_root)
        destination_path = Path(task_root) / relative_path
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = destination_path.with_name(
            f"{destination_path.name}.{os.getpid()}.partial"
        )
        shutil.copy2(source_path, temp_path)
        os.replace(str(temp_path), str(destination_path))
        if owner_check:
            owner_check()
        source_path.unlink()
        committed += 1
    for parent in sorted(source_parents, key=lambda item: len(item.parts), reverse=True):
        _prune_empty_stage_parents(parent, stage_task_root)
    return committed


def _warn_if_concurrency_exceeds_relay_limit():
    """总并发 = 副本数 × MAX_WORKERS，超过中转站配额会被大量 429。

    副本数只能由部署方通过环境变量告知，这里只做告警：扩容时把约束显式化，
    避免「悄悄变慢」而没人发现。
    """
    replicas = os.getenv("AUDIO_PRODUCER_REPLICAS", "").strip()
    if not replicas.isdigit():
        return
    total = int(replicas) * MAX_WORKERS
    if total > RELAY_GLOBAL_CONCURRENCY_LIMIT:
        logger.warning(
            f"⚠️ 预计总并发 {total} 路（副本 {replicas} × 每副本 {MAX_WORKERS}）"
            f"超过中转站上限 {RELAY_GLOBAL_CONCURRENCY_LIMIT}，"
            "会触发大量 429 重试；请下调副本数或 "
            "ELEVENLABS_DIALOGUE_WORKERS"
        )


def _main_unlocked(target_task_id, owner_check=None):
    owner_check = owner_check or (lambda: None)
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("未找到 ffmpeg，无法生成 ElevenLabs 对话音频")
    _warn_if_concurrency_exceeds_relay_limit()
    if _usage_tracker and target_task_id:
        try:
            _usage_tracker.set_context(target_task_id, "audio_producer")
        except Exception as exc:
            logger.warning(f"⚠️ API 用量上下文设置失败，继续 ElevenLabs Step 5：{exc}")

    task_root = os.path.join(OUTPUT_ROOT, str(target_task_id))
    if not os.path.exists(task_root):
        raise RuntimeError(f"找不到 ElevenLabs Dialogue 任务目录：{task_root}")

    stage_task_root = _latest_resumable_stage(task_root)
    if stage_task_root:
        stage_task_root = _make_stage_visible(stage_task_root)
    tasks = _scan_batches(task_root)
    if not tasks and stage_task_root:
        # 兼容旧版：把尚未发布的暂存 WAV 逐个同步，再清理成功缓存。
        owner_check()
        resumed_published = _commit_partial_staged_outputs(
            stage_task_root, task_root, owner_check=owner_check
        )
        _discard_legacy_consumed_json(stage_task_root)
        if not (Path(stage_task_root) / FAILED_BATCH_DIR_NAME).exists():
            shutil.rmtree(stage_task_root, ignore_errors=True)
        owner_check()
        if resumed_published:
            logger.info(
                f"♻️ 已发布并清理上次中断前完成的 {resumed_published} 个暂存 WAV"
            )
            return {
                "batches": 0, "success": 0, "failed": 0,
                "resumed": True,
            }
    if not tasks:
        logger.info(f"ℹ️ 没有找到 ElevenLabs Dialogue JSON，无需生成：{task_root}")
        return {"batches": 0, "success": 0, "failed": 0}

    if stage_task_root:
        logger.info(f"♻️ 续跑未完成的 ElevenLabs 暂存结果：{stage_task_root}")
        resumed_published = _commit_partial_staged_outputs(
            stage_task_root, task_root, owner_check=owner_check
        )
        if resumed_published:
            logger.info(
                f"📤 已将暂存目录中 {resumed_published} 个现有音频同步到主目录，"
                "后续成功批次也会即时发布"
            )
        _discard_legacy_consumed_json(stage_task_root)
    else:
        stage_task_root = tempfile.mkdtemp(
            prefix=STAGE_DIR_PREFIX, dir=task_root
        )
    total = len(tasks)
    task_args = [
        (
            index, total, path, node_id, language,
            stage_task_root, owner_check,
        )
        for index, (path, node_id, language) in enumerate(tasks, 1)
    ]
    errors = []
    succeeded = 0
    try:
        owner_check()
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_tasks = {
                executor.submit(_dispatch_batch, arg): arg for arg in task_args
            }
            for future in as_completed(future_tasks):
                task_info = future_tasks[future]
                try:
                    logger.info(future.result())
                    _clear_failed_batch(stage_task_root, task_info)
                    succeeded += 1
                except Exception as exc:
                    if _is_stage_lease_lost(exc):
                        # 租约丢失是执行控制流，不是批次业务失败。不能把
                        # 在途文件写入失败缓存，也不能继续清理共享 stage。
                        for pending in future_tasks:
                            pending.cancel()
                        raise
                    errors.append(str(exc))
                    cached_json = _retain_failed_batch(
                        stage_task_root, task_info, exc
                    )
                    logger.error(f"❌ {exc}")
                    logger.error(f"🧾 失败批次已保留：{cached_json}")

        if errors:
            owner_check()
            logger.error(
                f"⚠️ ElevenLabs Dialogue 部分完成：成功批次 {succeeded}/{total}，"
                f"失败批次 {len(errors)}；成功 WAV 已发布并从暂存目录删除，"
                "暂存目录只保留失败批次和诊断，可后续重跑"
            )
            ResumeState(task_root).mark_step(
                "relay_audio", f"remaining:{len(errors)}",
                status="partial", error="；".join(errors[:5]),
            )
            logger.warning(
                "⏭️ 失败批次已达到批次级重试上限，跳过并继续 audio_producer；"
                "失败批次 JSON 和诊断缓存保留，可使用原 task_id 补跑"
            )
            # 返回 failed=0，避免总控把已处理的部分完成误判为异常；partial
            # 状态和失败数量已经写入 ResumeState 与日志，不能伪装成全量成功。
            result = {
                "batches": total,
                "success": succeeded,
                "failed": 0,
                "skipped": len(errors),
                "partial": True,
            }
        else:
            result = {"batches": total, "success": total, "failed": 0}
        owner_check()
        # 失败批次仍需保留缓存；仅在全量成功时删除暂存目录。
        if not errors:
            shutil.rmtree(stage_task_root, ignore_errors=True)
        owner_check()
    except Exception as exc:
        if os.path.isdir(stage_task_root):
            logger.error(
                "❌ 本次生成未完成；已成功批次已发布到主目录并清理暂存副本，"
                "失败批次和诊断产物保留在："
                f"{stage_task_root}"
            )
        if type(exc).__name__ == "StageLeaseLostError":
            raise
        logger.exception(
            "❌ ElevenLabs Dialogue 链异常；保留暂存产物和失败 JSON：%s",
            exc,
        )
        raise
    finally:
        if _usage_tracker:
            try:
                _usage_tracker.flush()
            except Exception as exc:
                logger.warning(f"⚠️ API 用量落库失败，不影响 ElevenLabs Step 5：{exc}")

    if result.get("partial"):
        logger.warning(
            f"⚠️ ElevenLabs Dialogue 部分完成并继续后续流程："
            f"成功 {result.get('success', 0)}/{total}，跳过 {result.get('skipped', 0)} 个批次"
        )
    else:
        logger.info(f"🎉 ElevenLabs Dialogue 全部完成：{total} 个批次")
    item_states = ResumeState(task_root).list_items("relay_audio")
    ResumeState(task_root).mark_step(
        "relay_audio",
        stable_digest(
            "relay-audio-v2",
            sorted(
                (key, value.get("input_digest"), value.get("status"))
                for key, value in item_states.items()
            ),
        ),
        status="partial" if result.get("partial") else "success",
        error=(
            f"跳过 {result.get('skipped', 0)} 个失败批次；失败 JSON 和诊断缓存已保留"
            if result.get("partial") else None
        ),
    )
    return result


def stage_lock(target_task_id, owner_check=None):
    """返回覆盖 ElevenLabs Step 3-5 的任务级互斥上下文。"""
    owner_check = owner_check or (lambda: None)
    task_root = Path(OUTPUT_ROOT) / str(target_task_id)
    if not task_root.is_dir():
        raise RuntimeError(f"找不到 ElevenLabs Dialogue 任务目录：{task_root}")
    return _task_stage_lock(task_root, owner_check)


def main(target_task_id, owner_check=None):
    """运行 ElevenLabs relay，并串行化同一任务的暂存目录操作。

    接管 Worker 不再与旧 Worker 并发消费同一个 stage。旧 Worker 若仍在进行
    API 调用，会在下一次 owner_check 处退出；接管 Worker则等待文件锁释放后
    再扫描和清理 stage，避免把在途文件误判成缺失批次。
    """
    owner_check = owner_check or (lambda: None)
    with stage_lock(target_task_id, owner_check):
        return _main_unlocked(target_task_id, owner_check=owner_check)


if __name__ == "__main__":
    main("")
