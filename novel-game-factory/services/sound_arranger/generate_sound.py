import os
import sys
import time
import random
import json
import shutil
import glob
import io
import threading
import tempfile
import wave
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
import psycopg2
import psycopg2.extras
import requests
import numpy as np
from concurrent.futures import ThreadPoolExecutor, as_completed

os.environ["TOKENIZERS_PARALLELISM"] = "false"

from sentence_transformers import SentenceTransformer
from logger_config import setup_logger

# 引入项目配置
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from common.config import OUTPUTS_PATH, STORAGE_PATH, DATABASE_URL
from common.artifact_io import (
    ArtifactValidationError,
    atomic_output_path,
    atomic_write_bytes,
)
from services.sound_arranger.cache_policy import (
    GenerationMetadata,
    build_asset_id,
    build_generation_metadata,
    normalize_duration_seconds,
    normalize_generation_metadata,
    resolve_effective_cache_mode,
)
from services.sound_arranger.sfx_prompt_rewriter import (
    REWRITE_MODEL,
    rewrite_sfx_prompt_once,
)
from services.sound_arranger.generation_quota import SoundGenerationQuota
from services.sound_arranger.sound_scheduler import (
    AudioCluster,
    CoveragePhaseResult,
    ReusableAudioSource,
    SoundScheduleMetrics,
    ScheduledAudioTask,
    assign_direct_sources,
    cluster_candidates,
    compatibility_key,
    deduplicate_canonical_targets,
    is_valid_wav,
    normalize_scheduler_importance,
    order_competition_clusters,
    select_bgm_coverage_wave,
    resolve_node_order,
    semantic_similarity_threshold,
)

try:
    from common.usage_tracker import tracker as _usage_tracker
    from common.usage_tracker import init_usage_table

    init_usage_table()
except Exception:
    _usage_tracker = None

# 读取环境变量（NewAPI 网关，OpenAI 兼容端点）
# NEWAPI_BASE_URL = os.getenv("NEWAPI_BASE_URL", "http://172.16.30.31:32691").rstrip("/")
NEWAPI_API_KEY = os.getenv("STABILITY_API_KEY")

if not NEWAPI_API_KEY:
    raise ValueError("❌ 错误: 未设置 STABILITY_API_KEY 环境变量\n")

logger = setup_logger("SoundGen", "step2_sound_gen.log")

INPUT_ROOT = OUTPUTS_PATH
OUTPUT_ROOT = OUTPUTS_PATH
# 定义一个独立于任何剧本任务的“全局素材库”目录 (现职能: 下载暂存区 & 上传失败隔离区)
ASSETS_ROOT = os.path.join(STORAGE_PATH, "global_audio_assets")

# 核心并发与配置参数
MAX_WORKERS = 2
FORCE_OVERWRITE = False
# NewAPI 网关的 OpenAI 兼容音频端点
# STABLE_AUDIO_API_URL = "http://172.16.30.31:32691/v1/audio/speech"
# STABLE_AUDIO_API_URL = "https://ai.farmmx.com/v1/audio/speech"
STABLE_AUDIO_API_URL = os.getenv("STABLE_AUDIO_API_URL", "http://172.16.30.31:32691/v1/audio/speech")
AUDIO_MODEL = "stable-audio-3"
AUDIO_CACHE_NAMESPACE = os.getenv(
    "AUDIO_CACHE_NAMESPACE",
    "company-gateway/stable-audio-3/revision-1",
)
STABLE_AUDIO_STEPS = int(os.getenv("STABLE_AUDIO_STEPS", "8"))

# 进程内余额熔断信号：仅在响应正文明确表示余额/额度不足时置位。
# 每次新的 Sound 主任务开始前必须清除，避免污染同一 Celery 子进程的后续任务。
INSUFFICIENT_BALANCE = threading.Event()

AUDIO_FORMAT = "wav"
AUDIO_EXT = f".{AUDIO_FORMAT}"
PROVIDER_SUCCESS_UNCOMMITTED = "PROVIDER_SUCCESS_UNCOMMITTED"

SOUND_TRACK_KEYS = (
    ("bgm_events", "bgm_id"),
    ("sfx_events", "sfx_id"),
    ("ambience_events", "ambience_id"),
)


class SoundDataUnavailableError(RuntimeError):
    """数据库和共享 fallback 都无法提供合法 Sound 输入。"""


def _is_windows_fsync_error(exc):
    """识别 Windows 上对只读句柄调用 fsync 的兼容性错误。"""
    return isinstance(exc, OSError) and getattr(exc, "errno", None) == 9


@contextmanager
def _sound_atomic_output_path(final_path, *, validator=None):
    """Use the shared atomic helper, with a narrow Windows fsync fallback.

    The shared helper is the canonical implementation.  Some Windows Python
    builds reject ``fsync`` on a read-only handle (errno 9); in that case the
    same temp/validate/replace contract is performed locally so Sound remains
    usable without changing the shared artifact module in this task.
    """
    if os.name != "nt":
        with atomic_output_path(final_path, validator=validator) as temporary:
            yield temporary
        return

    final = Path(final_path)
    final.parent.mkdir(parents=True, exist_ok=True)
    temporary = final.with_name(
        f".{final.name}.{os.getpid()}.{threading.get_ident()}.sound.part"
    )
    try:
        yield temporary
        if not temporary.is_file() or temporary.stat().st_size <= 0:
            raise ArtifactValidationError(f"empty artifact: {temporary}")
        with temporary.open("rb+") as file_obj:
            os.fsync(file_obj.fileno())
        if validator is not None and not validator(temporary):
            raise ArtifactValidationError(
                f"artifact validation failed: {temporary}"
            )
        os.replace(temporary, final)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _sound_atomic_write_bytes(final_path, data, *, validator=None):
    try:
        return atomic_write_bytes(final_path, data, validator=validator)
    except OSError as exc:
        if os.name != "nt" or not _is_windows_fsync_error(exc):
            raise
    # Use the same atomic contract with a writable handle on Windows.
    with _sound_atomic_output_path(final_path, validator=validator) as temporary:
        with Path(temporary).open("wb") as file_obj:
            file_obj.write(data)
            file_obj.flush()
            os.fsync(file_obj.fileno())


@dataclass
class Sfx403RecoveryContext:
    terminal_events: set[tuple[str, str, str]] = field(default_factory=set)
    covered_bgm_node_ids: set[str] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @staticmethod
    def _key(task_id, node_id, event_id):
        return str(task_id), str(node_id), str(event_id)

    def mark_terminal(self, task_id, node_id, event_id):
        with self.lock:
            self.terminal_events.add(self._key(task_id, node_id, event_id))

    def is_terminal(self, task_id, node_id, event_id):
        with self.lock:
            return self._key(task_id, node_id, event_id) in self.terminal_events

    def mark_bgm_covered(self, node_id):
        with self.lock:
            self.covered_bgm_node_ids.add(str(node_id))

    def bgm_covered_nodes(self):
        with self.lock:
            return set(self.covered_bgm_node_ids)


bucket = None
logger.info("本地存储模式已启用，音效不会上传 OSS。")

encode_lock = threading.Lock()

MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sentence-transformers", "all-MiniLM-L6-v2")
logger.info("⏳ 正在加载本地向量模型 (all-MiniLM-L6-v2)...")
try:
    embedding_model = SentenceTransformer(MODEL_PATH)
    logger.info("✅ 向量模型加载完毕，素材库智能拦截已就绪！")
except Exception as e:
    logger.error(f"❌ 向量模型加载失败，将自动回退到纯 API 生成模式: {e}")
    embedding_model = None


def get_db_connection():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL 未配置，请检查 common.config")
    return psycopg2.connect(DATABASE_URL)


def get_platform_db_connection():
    """sound 表已迁平台库(agent_platform);sound_assets 素材缓存仍在业务库"""
    from common.config import PLATFORM_DATABASE_URL
    return psycopg2.connect(PLATFORM_DATABASE_URL)


def unpack_audio_task(raw_task):
    if len(raw_task) == 7:
        return (*raw_task, {})
    if len(raw_task) == 8:
        return raw_task
    raise ValueError(f"unsupported audio task tuple length: {len(raw_task)}")


def init_sound_assets_table():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                        CREATE TABLE IF NOT EXISTS sound_assets
                        (
                            asset_id
                            VARCHAR
                        (
                            100
                        ) PRIMARY KEY,
                            audio_type VARCHAR
                        (
                            20
                        ) NOT NULL,
                            original_prompt TEXT NOT NULL,
                            prompt_vector JSONB NOT NULL,
                            file_path VARCHAR
                        (
                            500
                        ) NOT NULL,
                            usage_count INTEGER DEFAULT 1,
                            create_time TIMESTAMP
                        (
                            0
                        ) DEFAULT CURRENT_TIMESTAMP,
                            update_time TIMESTAMP
                        (
                            0
                        ) DEFAULT CURRENT_TIMESTAMP
                            );
                        """)
            cur.execute("""
                        ALTER TABLE sound_assets
                        ADD COLUMN IF NOT EXISTS generation_signature VARCHAR(64);
                        """)
            cur.execute("""
                        ALTER TABLE sound_assets
                        ADD COLUMN IF NOT EXISTS generation_metadata JSONB;
                        """)
            cur.execute("""
                        CREATE INDEX IF NOT EXISTS idx_sound_assets_type
                            ON sound_assets(audio_type);
                        """)
            cur.execute("""
                        CREATE INDEX IF NOT EXISTS idx_sound_assets_type_signature
                            ON sound_assets(audio_type, generation_signature);
                        """)
            conn.commit()
    except Exception as e:
        logger.error(f"⚠️ 初始化 sound_assets 表失败: {e}")
    finally:
        conn.close()


def cosine_similarity(vec_a, vec_b):
    return np.dot(vec_a, vec_b) / (np.linalg.norm(vec_a) * np.linalg.norm(vec_b))


def search_local_asset(
    audio_type,
    prompt_text,
    generation_metadata,
    similarity_threshold=None,
    query_vector=None,
):
    if not isinstance(generation_metadata, GenerationMetadata):
        raise TypeError("generation_metadata must be GenerationMetadata")
    if similarity_threshold is None:
        similarity_threshold = semantic_similarity_threshold(audio_type)
        if similarity_threshold is None:
            logger.warning(
                "sound_cache_unknown_audio_type: audio_type=%s",
                audio_type,
            )
            return None
    if not embedding_model:
        return None

    if query_vector is None:
        # 多线程排队进入模型计算，防止底层模型并发崩溃。
        with encode_lock:
            query_vector = embedding_model.encode(prompt_text)
    query_vector = np.asarray(query_vector, dtype=float)

    conn = None
    best_match = None
    max_score = -1

    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            if audio_type == "bgm":
                normalized_primary = (
                    generation_metadata.primary_instrument.strip().lower()
                )
                cur.execute(
                    """
                    SELECT asset_id, original_prompt, file_path, prompt_vector
                    FROM sound_assets
                    WHERE audio_type = %s
                      AND LOWER(BTRIM(generation_metadata ->> 'primary_instrument')) = %s
                    """,
                    ("bgm", normalized_primary),
                )
            else:
                cur.execute(
                    """
                    SELECT asset_id, original_prompt, file_path, prompt_vector
                    FROM sound_assets
                    WHERE audio_type = %s AND generation_signature = %s
                    """,
                    (audio_type, generation_metadata.generation_signature),
                )
            rows = cur.fetchall()

            for asset_id, original_prompt, file_path, vector_json in rows:
                db_vector = np.array(vector_json)
                score = cosine_similarity(query_vector, db_vector)

                if score > max_score:
                    max_score = score
                    best_match = {
                        "asset_id": asset_id,
                        "path": file_path,
                        "score": score,
                        "prompt_vector": db_vector,
                    }

            if max_score >= similarity_threshold and best_match:
                cur.execute("UPDATE sound_assets SET usage_count = usage_count + 1 WHERE asset_id = %s",
                            (best_match['asset_id'],))
                conn.commit()
                return best_match
    except Exception as e:
        logger.error(f"检索本地素材库异常: {e}")
    finally:
        if conn is not None:
            conn.close()
    return None


def save_asset_to_db(
    asset_id,
    audio_type,
    prompt_text,
    file_path,
    generation_metadata,
):
    if not isinstance(generation_metadata, GenerationMetadata):
        raise TypeError("generation_metadata must be GenerationMetadata")
    if not embedding_model:
        logger.info(
            "sound_global_cache_write: component=db status=skipped "
            "reason=embedding_model_unavailable asset_id=%s audio_type=%s",
            asset_id,
            audio_type,
        )
        return "skipped"

    conn = None
    try:
        # 【核心修复】多线程排队进入模型计算
        with encode_lock:
            vector_list = embedding_model.encode(prompt_text).tolist()

        conn = get_db_connection()
        with conn.cursor() as cur:
            sql = """
                  INSERT INTO sound_assets (
                      asset_id,
                      audio_type,
                      original_prompt,
                      prompt_vector,
                      file_path,
                      generation_signature,
                      generation_metadata
                  )
                  VALUES (%s, %s, %s, %s, %s, %s, %s)
                  ON CONFLICT (asset_id) DO NOTHING;
                  """
            cur.execute(
                sql,
                (
                    asset_id,
                    audio_type,
                    prompt_text,
                    json.dumps(vector_list),
                    file_path,
                    generation_metadata.generation_signature,
                    json.dumps(generation_metadata.model_dump()),
                ),
            )
            inserted = getattr(cur, "rowcount", 1) != 0
            conn.commit()
        status = "success" if inserted else "already_exists"
        logger.info(
            "sound_global_cache_write: component=db status=%s asset_id=%s "
            "audio_type=%s path=%s",
            status,
            asset_id,
            audio_type,
            file_path,
        )
        return status
    except Exception as e:
        logger.error(
            "sound_global_cache_write: component=db status=failure "
            "asset_id=%s audio_type=%s path=%s error=%s",
            asset_id,
            audio_type,
            file_path,
            e,
        )
        return "failure"
    finally:
        if conn is not None:
            conn.close()


def get_latest_task_id():
    conn = None
    try:
        conn = get_platform_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT task_id FROM sound ORDER BY id DESC LIMIT 1;")
            row = cur.fetchone()
            if row:
                return row[0]
    except Exception as e:
        logger.error(f"❌ 获取最新 task_id 失败: {e}")
    finally:
        if conn:
            conn.close()
    return None


def is_insufficient_balance(code, err_msg):
    """仅根据响应正文中的明确语义判断余额/额度不足。

    402/403 在不同网关中还可能表示上游错误、审核或权限问题，不能仅凭
    HTTP 状态码熔断整批任务。
    """
    # 429 的 quota/rate-limit 文案表示限流，必须走等待重试而不是余额熔断。
    if code == 429:
        return False
    if isinstance(err_msg, (dict, list, tuple)):
        text = json.dumps(err_msg, ensure_ascii=False).casefold()
    else:
        text = str(err_msg).casefold()

    explicit_markers = (
        "insufficient_balance",
        "insufficient balance",
        "balance insufficient",
        "balance is insufficient",
        "not enough balance",
        "insufficient credit",
        "insufficient credits",
        "insufficient_quota",
        "quota exceeded",
        "exceeded your current quota",
        "payment required",
        "billing hard limit",
        "余额不足",
        "余额已用尽",
        "额度不足",
        "额度已用尽",
        "配额不足",
        "配额已用尽",
        "欠费",
        "积分不足",
    )
    return any(marker in text for marker in explicit_markers)


def _is_valid_wav_bytes(body):
    try:
        if not isinstance(body, (bytes, bytearray)) or len(body) <= 44:
            return False
        with wave.open(io.BytesIO(body), "rb") as wav_file:
            return (
                wav_file.getnframes() > 0
                and wav_file.getframerate() > 0
                and wav_file.getnchannels() > 0
                and wav_file.getsampwidth() > 0
            )
    except (OSError, EOFError, wave.Error, TypeError, ValueError):
        return False


def execute_stable_audio_request(
    prompt,
    duration,
    dst_path,
    attempt_idx=1,
    batch_name="Batch",
    on_provider_success=None,
):
    headers = {
        "Authorization": f"Bearer {NEWAPI_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "audio/*"
    }

    safe_duration = normalize_duration_seconds(duration)
    # NewAPI 走 OpenAI 兼容端点：prompt->input，output_format->response_format
    payload = {
        "model": AUDIO_MODEL,
        "input": prompt,
        "response_format": AUDIO_FORMAT,
        "duration": safe_duration,
        "steps": STABLE_AUDIO_STEPS
    }

    try:
        response = requests.post(STABLE_AUDIO_API_URL, headers=headers, json=payload, timeout=120)

        if response.status_code == 200:
            body = response.content
            content_type = response.headers.get("Content-Type", "")
            if not _is_valid_wav_bytes(body):
                preview = body[:500].decode("utf-8", "replace")
                return (
                    False,
                    response.status_code,
                    "未返回基础有效 WAV "
                    f"Content-Type={content_type!r} body={preview!r}",
                )
            # A valid 200 is already a provider success.  Any failure after
            # this point (metering or local persistence) is terminal for this
            # asset and must never cause another paid provider request.
            try:
                provider_success_recorded = True
                if on_provider_success is not None:
                    provider_success_recorded = on_provider_success() is not False
                if _usage_tracker:
                    _usage_tracker.add_music(
                        1,
                        AUDIO_MODEL,
                        provider="stability",
                        duration_seconds=safe_duration,
                    )
                # ``dst_path`` is the generation path.  In reuse mode it is a
                # task-level temporary asset; in bypass mode it is the
                # canonical path.
                _sound_atomic_write_bytes(
                    dst_path,
                    bytes(body),
                    validator=is_valid_wav,
                )
                if not provider_success_recorded:
                    return (
                        False,
                        PROVIDER_SUCCESS_UNCOMMITTED,
                        "quota consume failed after provider success",
                    )
            except Exception as exc:
                logger.error(
                    "provider_success_persistence_failed: path=%s error=%s",
                    dst_path,
                    exc,
                )
                return False, PROVIDER_SUCCESS_UNCOMMITTED, str(exc)
            return True, "OK", ""
        elif response.status_code == 429:
            return False, 429, "Rate Limit Exceeded"
        else:
            try:
                err_msg = response.json()
            except Exception:
                err_msg = response.text
            return False, response.status_code, str(err_msg)
    except requests.exceptions.Timeout:
        return False, "TIMEOUT", "请求超时"
    except Exception as e:
        return False, "REQUEST_ERROR", str(e)


def _cleanup_same_event_legacy_files(
    target_dir,
    base_name,
    timeline_suffix,
    keep_filename=None,
):
    """Remove only legacy hashed files belonging to the current event.

    A bare ``*_<suffix>.wav`` glob is unsafe because two independent events
    may cover the same timeline.  The extra ``base_name`` component narrows
    cleanup to the old naming family for this event.
    """
    normalized_base = str(base_name).strip()
    normalized_suffix = str(timeline_suffix).strip()
    if (
        not normalized_base
        or not normalized_suffix
        or Path(normalized_base).name != normalized_base
        or "/" in normalized_base
        or "\\" in normalized_base
        or "/" in normalized_suffix
        or "\\" in normalized_suffix
    ):
        logger.warning(
            "canonical_legacy_cleanup_skipped: dir=%s base_name=%s suffix=%s",
            target_dir,
            base_name,
            timeline_suffix,
        )
        return

    pattern = os.path.join(
        target_dir,
        f"{glob.escape(normalized_base)}_*_"
        f"{glob.escape(normalized_suffix)}{AUDIO_EXT}",
    )
    normalized_keep = (
        os.path.basename(str(keep_filename))
        if keep_filename is not None
        else None
    )
    try:
        old_paths = glob.glob(pattern)
    except OSError as exc:
        logger.warning(
            "canonical_legacy_cleanup_failed: pattern=%s error=%s",
            pattern,
            exc,
        )
        return
    for old in old_paths:
        if normalized_keep and os.path.basename(old) == normalized_keep:
            continue
        try:
            os.remove(old)
        except OSError as exc:
            logger.warning(
                "canonical_legacy_cleanup_failed: path=%s error=%s",
                old,
                exc,
            )


def _atomic_publish_canonical_wav(
    target_path,
    timeline_suffix,
    write_to_temporary,
    *,
    base_name,
):
    """Publish a validated WAV atomically, then clean same-event legacy files."""
    final_path = Path(target_path)
    with _sound_atomic_output_path(
        final_path,
        validator=is_valid_wav,
    ) as temporary_path:
        write_to_temporary(temporary_path)

    _cleanup_same_event_legacy_files(
        str(final_path.parent),
        base_name,
        timeline_suffix,
        keep_filename=final_path.name,
    )
    return final_path


def _remove_invalid_audio_target(path, *, log_context):
    try:
        Path(path).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(
            "%s: 无效 WAV 清理失败 path=%s error=%s",
            log_context,
            path,
            exc,
        )


def write_sfx_prompt_sidecar(
    *,
    target_dir,
    event_id,
    original_prompt,
    effective_prompt,
    rewrite_model,
):
    final_path = Path(target_dir) / f"{event_id}.prompt.json"
    temp_path = None
    payload = {
        "sfx_id": event_id,
        "original_prompt": original_prompt,
        "effective_prompt": effective_prompt,
        "rewrite_reason": "ambiguous_403",
        "rewrite_model": rewrite_model,
    }

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{event_id}.prompt.",
            suffix=".tmp",
            dir=target_dir,
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_path, final_path)
        return str(final_path)
    except Exception as exc:
        logger.warning(
            "SFX 改写 sidecar 写入失败: event_id=%s path=%s error=%s",
            event_id,
            final_path,
            exc,
        )
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass
        return None


def _materialize_cache_hit(
    hit_result,
    *,
    base_name,
    target_dir,
    timeline_suffix,
    final_file_name,
    batch_name,
    index,
    total,
    audio_type,
):
    if not isinstance(hit_result, dict):
        return None

    hit_asset_id = str(hit_result.get("asset_id", ""))
    if not hit_asset_id.strip():
        logger.warning(
            "cache_hit_missing_asset_id: target_dir=%s file=%s",
            target_dir,
            final_file_name,
        )
        return None
    source_path = hit_result.get("path")
    target_path = Path(target_dir) / final_file_name

    def publish(writer, source_label):
        _atomic_publish_canonical_wav(
            target_path,
            timeline_suffix,
            writer,
            base_name=base_name,
        )
        return (
            f"🎯 [{batch_name}][{index}/{total}] 缓存命中 "
            f"({source_label},源={hit_asset_id}): 成功导出为 {final_file_name}"
        )

    try:
        local_source = Path(source_path) if source_path else None
    except (TypeError, ValueError) as exc:
        local_source = None
        logger.warning(
            "cache_local_source_invalid: asset_id=%s source=%r error=%s",
            hit_asset_id,
            source_path,
            exc,
        )

    if local_source is not None and local_source.is_file():
        try:
            return publish(
                lambda temporary: shutil.copyfile(
                    local_source,
                    temporary,
                ),
                "本地隔离区直拷",
            )
        except Exception as exc:
            logger.warning(
                "cache_local_materialization_failed: asset_id=%s "
                "target=%s error=%s",
                hit_asset_id,
                target_path,
                exc,
            )

    if bucket:
        oss_object_key = f"sound/{audio_type}/{hit_asset_id}{AUDIO_EXT}"
        try:
            return publish(
                lambda temporary: bucket.get_object_to_file(
                    oss_object_key,
                    str(temporary),
                ),
                "OSS云端下载",
            )
        except Exception as exc:
            logger.warning(
                "cache_oss_materialization_failed: asset_id=%s "
                "target=%s error=%s",
                hit_asset_id,
                target_path,
                exc,
            )
            return None

    logger.warning(
        "cache_source_unavailable: asset_id=%s target=%s",
        hit_asset_id,
        target_path,
    )
    return None


def _format_similarity(value):
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return "unknown"


def _record_global_cache_write(
    metrics,
    *,
    task_id,
    node_id,
    audio_type,
    event_id,
    asset_id,
    db_status,
    oss_status,
    canonical_path,
):
    """Record one aggregate cache-write result without changing generation flow."""
    normalized_db = str(db_status or "skipped").strip().lower()
    normalized_oss = str(oss_status or "skipped").strip().lower()
    db_ok = normalized_db in {"success", "already_exists"}
    oss_ok = normalized_oss in {"success", "skipped"}
    if normalized_db == "skipped":
        aggregate = "skipped"
    else:
        aggregate = "success" if db_ok and oss_ok else "failure"

    logger.info(
        "sound_global_cache_write: component=aggregate status=%s "
        "task_id=%s node_id=%s audio_type=%s event_id=%s asset_id=%s "
        "db_status=%s oss_status=%s canonical=%s",
        aggregate,
        task_id,
        node_id,
        audio_type,
        event_id,
        asset_id,
        normalized_db,
        normalized_oss,
        canonical_path,
    )
    if metrics is not None and aggregate in {"success", "failure"}:
        metrics.record_cache_write(aggregate)


def _log_schedule_metrics(metrics, *, task_id, phase="final"):
    if metrics is None:
        return
    values = metrics.snapshot()
    logger.info(
        "sound_schedule_resolution_stats: phase=%s task_id=%s "
        "existing_canonical=%s global_cache_hit=%s direct_reuse=%s "
        "cluster_representative_generated=%s cluster_member_copied=%s "
        "global_cache_write_success=%s global_cache_write_failure=%s",
        phase,
        task_id,
        values["existing_canonical"],
        values["global_cache_hit"],
        values["direct_reuse"],
        values["cluster_representative_generated"],
        values["cluster_member_copied"],
        values["global_cache_write_success"],
        values["global_cache_write_failure"],
    )


def _log_cache_hit(*, task_id, node_id, audio_type, event_id, hit_result,
                   canonical_path, prompt_variant="original", metrics=None):
    if not isinstance(hit_result, dict):
        return
    logger.info(
        "sound_cache_hit: source=global_cache_hit task_id=%s node_id=%s audio_type=%s "
        "event_id=%s asset_id=%s similarity=%s prompt_variant=%s canonical=%s",
        task_id,
        node_id,
        audio_type,
        event_id,
        hit_result.get("asset_id"),
        _format_similarity(hit_result.get("score")),
        prompt_variant,
        canonical_path,
    )
    if metrics is not None:
        metrics.record_resolution("global_cache_hit")


def _record_cluster_representative_generated(
    metrics,
    *,
    task_id,
    node_id,
    audio_type,
    event_id,
    canonical_path,
):
    if metrics is None:
        return
    metrics.record_resolution("cluster_representative_generated")
    logger.info(
        "sound_resolved: source=cluster_representative_generated "
        "task_id=%s node_id=%s audio_type=%s event_id=%s canonical=%s",
        task_id,
        node_id,
        audio_type,
        event_id,
        canonical_path,
    )


def _publish_generated_audio(
    *,
    base_name,
    generation_path,
    dst_path,
    final_file_name,
    target_dir,
    timeline_suffix,
    effective_cache_mode,
    asset_id,
    audio_type,
    prompt,
    generation_metadata,
    batch_name,
    index,
    total,
    task_id=None,
    node_id=None,
    event_id=None,
    metrics=None,
    generation_origin=None,
):
    if effective_cache_mode == "bypass":
        if Path(generation_path) != Path(dst_path):
            raise ValueError(
                "bypass generation_path must equal canonical dst_path"
            )
        if not is_valid_wav(generation_path):
            raise ArtifactValidationError(
                f"provider generation path is not a valid WAV: {generation_path}"
            )
        _cleanup_same_event_legacy_files(
            target_dir,
            base_name,
            timeline_suffix,
            keep_filename=final_file_name,
        )
        logger.info(
            "sound_global_cache_write: component=aggregate status=skipped "
            "reason=bypass task_id=%s node_id=%s audio_type=%s event_id=%s "
            "asset_id=%s canonical=%s",
            task_id,
            node_id,
            audio_type,
            event_id,
            asset_id,
            dst_path,
        )
        if metrics is not None and generation_origin == "cluster_representative":
            _record_cluster_representative_generated(
                metrics,
                task_id=task_id,
                node_id=node_id,
                audio_type=audio_type,
                event_id=event_id,
                canonical_path=dst_path,
            )
        return (
            f"✅ [{batch_name}][{index}/{total}] "
            f"API生成(绕过全局缓存) 应用为({final_file_name})"
        )

    try:
        cache_db_status = save_asset_to_db(
            asset_id,
            audio_type,
            prompt,
            generation_path,
            generation_metadata,
        )
    except Exception as exc:
        cache_db_status = "failure"
        logger.error(
            "sound_global_cache_write: component=db status=failure "
            "task_id=%s node_id=%s audio_type=%s event_id=%s asset_id=%s error=%s",
            task_id,
            node_id,
            audio_type,
            event_id,
            asset_id,
            exc,
        )
    _atomic_publish_canonical_wav(
        dst_path,
        timeline_suffix,
        lambda temporary: shutil.copyfile(
            generation_path,
            temporary,
        ),
        base_name=base_name,
    )

    oss_status = "skipped"
    oss_display_status = "本地保存"
    logger.info(
        "sound_global_cache_write: component=local status=success "
        "task_id=%s node_id=%s audio_type=%s event_id=%s asset_id=%s path=%s",
        task_id,
        node_id,
        audio_type,
        event_id,
        asset_id,
        generation_path,
    )

    _record_global_cache_write(
        metrics,
        task_id=task_id,
        node_id=node_id,
        audio_type=audio_type,
        event_id=event_id,
        asset_id=asset_id,
        db_status=cache_db_status,
        oss_status=oss_status,
        canonical_path=dst_path,
    )
    if metrics is not None and generation_origin == "cluster_representative":
        _record_cluster_representative_generated(
            metrics,
            task_id=task_id,
            node_id=node_id,
            audio_type=audio_type,
            event_id=event_id,
            canonical_path=dst_path,
        )

    return (
        f"✅ [{batch_name}][{index}/{total}] API生成入库"
        f"({asset_id}{AUDIO_EXT})[{oss_display_status}] 应用为({final_file_name})"
    )


def process_one_audio_robust(
    task_info,
    recovery_context=None,
    quota_lease=None,
    metrics=None,
    generation_origin=None,
):
    try:
        return _process_one_audio_robust_inner(
            task_info,
            recovery_context,
            quota_lease,
            metrics,
            generation_origin,
        )
    finally:
        if quota_lease is not None:
            quota_lease.release_if_pending()


def _process_one_audio_robust_inner(
    task_info,
    recovery_context,
    quota_lease,
    metrics,
    generation_origin,
):
    # event_id 例: bgm_cinematic_tension_heavy_15-17
    # base_name 例: bgm_cinematic_tension_heavy
    (
        index,
        total,
        task_id,
        node_id,
        audio_type,
        prompt,
        duration,
        base_name,
        event_id,
        batch_name,
        effective_cache_mode,
        raw_generation_metadata,
    ) = task_info

    generation_metadata = None
    if effective_cache_mode == "reuse":
        generation_metadata = normalize_generation_metadata(
            raw_generation_metadata
        )
        if generation_metadata is None:
            effective_cache_mode = "bypass"

    # 【动态解析】剥离出时间轴序号 (例如 "15-17")
    timeline_suffix = event_id[len(base_name) + 1:] if event_id.startswith(base_name + "_") else event_id.split('_')[-1]

    target_dir = os.path.join(OUTPUT_ROOT, str(task_id), node_id, "audios", audio_type)

    try:
        os.makedirs(target_dir, exist_ok=True)
    except (PermissionError, OSError) as e:
        return f"❌ [{batch_name}] 创建目录失败: {node_id}"

    # ==========================================
    # 1. 智能防重跑拦截
    # 只有当目标文件就是 {event_id}{AUDIO_EXT}（新命名规则）且非空时才算已存在；
    # 旧格式文件（如 bgm_xxx_<hash>_15-17 的其他扩展名）会在后续生成时被清理迁移
    # ==========================================
    expected_filename = f"{event_id}{AUDIO_EXT}"
    expected_path = os.path.join(target_dir, expected_filename)

    if not FORCE_OVERWRITE and is_valid_wav(expected_path):
        logger.info(
            "sound_resolved: source=existing_canonical task_id=%s "
            "node_id=%s audio_type=%s event_id=%s canonical=%s",
            task_id,
            node_id,
            audio_type,
            event_id,
            expected_path,
        )
        if metrics is not None:
            metrics.record_resolution("existing_canonical")
        return f"⏭️ [{batch_name}][{index}/{total}] 跳过(已存在): {expected_filename}"

    if not prompt:
        return f"⚠️ [{batch_name}][{index}/{total}] 提示词为空，跳过生成。"

    # ==========================================
    # 2. 方案A：本地容错拦截 -> OSS云端按需拉取
    # ==========================================
    hit_result = None
    if effective_cache_mode == "reuse":
        hit_result = search_local_asset(
            audio_type,
            prompt,
            generation_metadata,
        )
    cache_result = _materialize_cache_hit(
        hit_result,
        base_name=base_name,
        target_dir=target_dir,
        timeline_suffix=timeline_suffix,
        final_file_name=f"{event_id}{AUDIO_EXT}",
        batch_name=batch_name,
        index=index,
        total=total,
        audio_type=audio_type,
    )
    if cache_result:
        _log_cache_hit(
            task_id=task_id,
            node_id=node_id,
            audio_type=audio_type,
            event_id=event_id,
            hit_result=hit_result,
            canonical_path=expected_path,
            metrics=metrics,
        )
        return cache_result

    time.sleep(random.uniform(0.1, 1.5))

    final_file_name = f"{event_id}{AUDIO_EXT}"
    dst_path = os.path.join(target_dir, final_file_name)

    asset_id = None
    if effective_cache_mode == "reuse":
        asset_id = build_asset_id(
            base_name,
            prompt,
            generation_metadata.generation_signature,
        )
        library_dir = os.path.join(ASSETS_ROOT, audio_type, str(task_id))
        os.makedirs(library_dir, exist_ok=True)
        generation_path = os.path.join(
            library_dir,
            f"{asset_id}{AUDIO_EXT}",
        )
    else:
        generation_path = dst_path

    # 4. 执行 API 生成与重试
    max_retries = 4
    awaiting_second_initial_403 = False
    for attempt in range(1, max_retries + 1):
        if quota_lease is not None and quota_lease.state != "pending":
            return (
                f"❌ [{batch_name}][{index}/{total}] Provider 已成功但后续处理失败，"
                f"不再重试: {event_id}"
            )
        # 熔断已触发（其他线程遇到余额不足），不再发起新请求
        if INSUFFICIENT_BALANCE.is_set():
            return f"🛑 [{batch_name}][{index}/{total}] 余额不足已熔断，跳过: {event_id}"

        success, code, err = execute_stable_audio_request(
            prompt,
            duration,
            generation_path,
            attempt,
            batch_name,
            on_provider_success=(
                quota_lease.consume if quota_lease is not None else None
            ),
        )

        if code == PROVIDER_SUCCESS_UNCOMMITTED:
            if audio_type == "sfx" and recovery_context is not None:
                recovery_context.mark_terminal(task_id, node_id, event_id)
            return (
                f"❌ [{batch_name}][{index}/{total}] Provider 已成功但本地落盘失败，"
                f"当前音频终止且不再重试: {event_id}"
            )

        if success:
            return _publish_generated_audio(
                base_name=base_name,
                generation_path=generation_path,
                dst_path=dst_path,
                final_file_name=final_file_name,
                target_dir=target_dir,
                timeline_suffix=timeline_suffix,
                effective_cache_mode=effective_cache_mode,
                asset_id=asset_id,
                audio_type=audio_type,
                prompt=prompt,
                generation_metadata=generation_metadata,
                batch_name=batch_name,
                index=index,
                total=total,
                task_id=task_id,
                node_id=node_id,
                event_id=event_id,
                metrics=metrics,
                generation_origin=generation_origin,
            )

        if quota_lease is not None and quota_lease.state != "pending":
            return (
                f"❌ [{batch_name}][{index}/{total}] Provider 已成功但后续处理失败，"
                f"不再重试: {event_id}"
            )

        # 只有响应正文明确表示余额/额度不足时才触发整批熔断。
        if is_insufficient_balance(code, err):
            INSUFFICIENT_BALANCE.set()
            logger.error(f"🛑 [{batch_name}][{index}/{total}] 判定为余额不足(code={code})，触发全局熔断: {err[:150]}")
            return f"🛑 [{batch_name}][{index}/{total}] 余额不足中止: {event_id}"

        rewrite_enabled = recovery_context is not None and audio_type == "sfx"
        if rewrite_enabled and attempt == 1 and code == 403:
            awaiting_second_initial_403 = True
        elif rewrite_enabled and attempt == 2 and awaiting_second_initial_403:
            if code == 403:
                try:
                    effective_prompt = rewrite_sfx_prompt_once(prompt)
                except Exception as exc:
                    logger.warning(
                        "SFX 中性化改写异常: task_id=%s node_id=%s event_id=%s error=%s",
                        task_id,
                        node_id,
                        event_id,
                        exc,
                    )
                    effective_prompt = None

                if (
                    not isinstance(effective_prompt, str)
                    or not effective_prompt.strip()
                    or effective_prompt.strip() == prompt.strip()
                ):
                    recovery_context.mark_terminal(task_id, node_id, event_id)
                    return (
                        f"❌ [{batch_name}][{index}/{total}] SFX改写失败，"
                        f"当前音频终止: {event_id}"
                    )

                effective_prompt = effective_prompt.strip()
                sidecar_path = write_sfx_prompt_sidecar(
                    target_dir=target_dir,
                    event_id=event_id,
                    original_prompt=prompt,
                    effective_prompt=effective_prompt,
                    rewrite_model=REWRITE_MODEL,
                )
                logger.info(
                    "SFX 模糊403中性化改写完成: task_id=%s node_id=%s "
                    "event_id=%s sidecar=%s",
                    task_id,
                    node_id,
                    event_id,
                    sidecar_path,
                )

                effective_hit = None
                if effective_cache_mode == "reuse":
                    effective_hit = search_local_asset(
                        audio_type,
                        effective_prompt,
                        generation_metadata,
                    )
                effective_cache_result = _materialize_cache_hit(
                    effective_hit,
                    base_name=base_name,
                    target_dir=target_dir,
                    timeline_suffix=timeline_suffix,
                    final_file_name=final_file_name,
                    batch_name=batch_name,
                    index=index,
                    total=total,
                    audio_type=audio_type,
                )
                if effective_cache_result:
                    _log_cache_hit(
                        task_id=task_id,
                        node_id=node_id,
                        audio_type=audio_type,
                        event_id=event_id,
                        hit_result=effective_hit,
                        canonical_path=dst_path,
                        prompt_variant="effective",
                        metrics=metrics,
                    )
                    return effective_cache_result

                effective_asset_id = None
                if effective_cache_mode == "reuse":
                    effective_asset_id = build_asset_id(
                        base_name,
                        effective_prompt,
                        generation_metadata.generation_signature,
                    )
                    effective_generation_path = os.path.join(
                        ASSETS_ROOT,
                        audio_type,
                        str(task_id),
                        f"{effective_asset_id}{AUDIO_EXT}",
                    )
                    os.makedirs(
                        os.path.dirname(effective_generation_path),
                        exist_ok=True,
                    )
                else:
                    effective_generation_path = dst_path

                final_success, final_code, final_error = execute_stable_audio_request(
                    effective_prompt,
                    duration,
                    effective_generation_path,
                    1,
                    batch_name,
                    on_provider_success=(
                        quota_lease.consume
                        if quota_lease is not None
                        else None
                    ),
                )
                if final_code == PROVIDER_SUCCESS_UNCOMMITTED:
                    recovery_context.mark_terminal(task_id, node_id, event_id)
                    logger.warning(
                        "SFX effective provider success but persistence failed: "
                        "task_id=%s node_id=%s event_id=%s error=%s",
                        task_id,
                        node_id,
                        event_id,
                        str(final_error)[:150],
                    )
                    return (
                        f"❌ [{batch_name}][{index}/{total}] SFX改写后本地落盘失败，"
                        f"当前音频终止且不再重试: {event_id}"
                    )
                if final_success:
                    return _publish_generated_audio(
                        base_name=base_name,
                        generation_path=effective_generation_path,
                        dst_path=dst_path,
                        final_file_name=final_file_name,
                        target_dir=target_dir,
                        timeline_suffix=timeline_suffix,
                        effective_cache_mode=effective_cache_mode,
                        asset_id=effective_asset_id,
                        audio_type=audio_type,
                        prompt=effective_prompt,
                        generation_metadata=generation_metadata,
                        batch_name=batch_name,
                        index=index,
                        total=total,
                        task_id=task_id,
                        node_id=node_id,
                        event_id=event_id,
                        metrics=metrics,
                        generation_origin=generation_origin,
                    )

                if is_insufficient_balance(final_code, final_error):
                    INSUFFICIENT_BALANCE.set()
                recovery_context.mark_terminal(task_id, node_id, event_id)
                logger.warning(
                    "SFX effective prompt 最终请求失败: task_id=%s node_id=%s "
                    "event_id=%s code=%s error=%s",
                    task_id,
                    node_id,
                    event_id,
                    final_code,
                    str(final_error)[:150],
                )
                return (
                    f"❌ [{batch_name}][{index}/{total}] SFX改写后生成失败，"
                    f"当前音频终止: {event_id}"
                )

            awaiting_second_initial_403 = False

        # 只有 429 使用 65 秒限流避让；普通 402/403 走下方通用短间隔重试。
        # 重试耗尽只失败当前音频，不设置余额熔断，也不取消本作品的其他音频。
        if code == 429:
            if attempt >= max_retries:
                logger.warning(
                    f"⚠️ [{batch_name}][{index}/{total}] "
                    f"HTTP {code} 重试已耗尽: {err[:100]}..."
                )
                break
            wait = 65
            logger.warning(
                f"⏳ [{batch_name}][{index}/{total}] 触发 HTTP {code}，"
                f"避让 {wait}s 后重试..."
            )
            time.sleep(wait)
            continue

        if code == "TIMEOUT":
            logger.warning(f"⏳ [{batch_name}][{index}/{total}] 接口超时，准备重试...")
            time.sleep(2)
            continue

        logger.warning(f"⚠️ [{batch_name}][{index}/{total}] 尝试{attempt}失败 [{code}]: {err[:100]}...")
        time.sleep(2)

    return f"❌ [{batch_name}][{index}/{total}] 失败保留(重试耗尽): {event_id}"


def _decode_sound_payload(raw_payload):
    """Normalize DB/file representations to a Python JSON value."""
    if isinstance(raw_payload, dict):
        return raw_payload, None
    if isinstance(raw_payload, memoryview):
        raw_payload = raw_payload.tobytes()
    if isinstance(raw_payload, (bytes, bytearray)):
        try:
            raw_payload = bytes(raw_payload).decode("utf-8")
        except UnicodeDecodeError:
            return None, "json_not_utf8"
    if isinstance(raw_payload, str):
        if not raw_payload.strip():
            return None, "empty"
        try:
            return json.loads(raw_payload), None
        except json.JSONDecodeError:
            return None, "json_parse_error"
    if raw_payload is None:
        return None, "missing"
    return None, f"unsupported_type:{type(raw_payload).__name__}"


def _sound_payload_error(payload):
    if not isinstance(payload, dict) or not payload:
        return "root_not_non_empty_object"
    if "_meta" in payload and not isinstance(payload.get("_meta"), dict):
        return "meta_not_object"

    nodes = [
        (str(node_id), node)
        for node_id, node in payload.items()
        if node_id != "_meta"
    ]
    if not nodes:
        return "no_sound_nodes"

    for node_id, node in nodes:
        if not isinstance(node, dict):
            return f"node_not_object:{node_id}"
        tracks = [spec for spec in SOUND_TRACK_KEYS if spec[0] in node]
        if not tracks:
            return f"node_without_track_lists:{node_id}"
        for track, id_key in tracks:
            events = node[track]
            if not isinstance(events, list):
                return f"track_not_list:{node_id}:{track}"
            for event_index, event in enumerate(events):
                if not isinstance(event, dict):
                    return f"event_not_object:{node_id}:{track}:{event_index}"
                event_id = event.get(id_key)
                if not isinstance(event_id, str) or not event_id.strip():
                    return (
                        f"event_id_invalid:{node_id}:{track}:"
                        f"{event_index}:{id_key}"
                    )
    return None


def load_sound_data(target_task_id):
    """Read validated Sound JSON from DB, then the shared output fallback."""
    conn = None
    database_reason = "not_found"
    try:
        conn = get_platform_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT data FROM sound WHERE task_id = %s;",
                (target_task_id,),
            )
            row = cur.fetchone()
        raw_candidate = row["data"] if row else None
        candidate, database_reason = _decode_sound_payload(raw_candidate)
        if database_reason is None:
            database_reason = _sound_payload_error(candidate)
        if database_reason is None:
            logger.info(
                "sound_input_loaded: task_id=%s source=database",
                target_task_id,
            )
            return candidate
        logger.warning(
            "sound_input_database_unusable: task_id=%s reason=%s",
            target_task_id,
            database_reason,
        )
    except Exception as exc:
        database_reason = f"read_error:{type(exc).__name__}"
        logger.warning(
            "sound_input_database_unusable: task_id=%s error=%s",
            target_task_id,
            exc,
        )
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception as exc:
                logger.warning(
                    "sound_input_database_close_failed: task_id=%s error=%s",
                    target_task_id,
                    exc,
                )

    fallback_path = Path(OUTPUTS_PATH) / str(target_task_id) / "sound.json"
    fallback_reason = "missing"
    try:
        if fallback_path.is_file():
            candidate, fallback_reason = _decode_sound_payload(
                fallback_path.read_bytes()
            )
            if fallback_reason is None:
                fallback_reason = _sound_payload_error(candidate)
            if fallback_reason is None:
                logger.info(
                    "sound_input_loaded: task_id=%s source=shared_fallback path=%s",
                    target_task_id,
                    fallback_path,
                )
                return candidate
    except Exception as exc:
        fallback_reason = f"read_error:{type(exc).__name__}"
        logger.warning(
            "sound_input_fallback_unusable: task_id=%s path=%s error=%s",
            target_task_id,
            fallback_path,
            exc,
        )

    message = (
        f"Sound input unavailable for task_id={target_task_id}: "
        f"database={database_reason}, shared_fallback={fallback_reason}, "
        f"path={fallback_path}"
    )
    logger.error("sound_input_unavailable: %s", message)
    raise SoundDataUnavailableError(message)


def _event_identity(event, id_key, name_key, *, node_id, track, event_index):
    if not isinstance(event, dict):
        raise SoundDataUnavailableError(
            f"event_not_object:{node_id}:{track}:{event_index}"
        )
    raw_id = event.get(id_key)
    if not isinstance(raw_id, str) or not raw_id.strip():
        raise SoundDataUnavailableError(
            f"event_id_invalid:{node_id}:{track}:{event_index}:{id_key}"
        )
    event_id = raw_id.strip()
    raw_name = event.get(name_key)
    if isinstance(raw_name, str) and raw_name.strip():
        base_name = raw_name.strip()
    else:
        base_name = event_id.rsplit("_", 1)[0] or event_id
    return event_id, base_name


def _build_event_metadata(audio_type, event, bgm_prompt_version):
    is_bgm = audio_type == "bgm"
    try:
        metadata = build_generation_metadata(
            cache_namespace=AUDIO_CACHE_NAMESPACE,
            model_alias=AUDIO_MODEL,
            audio_type=audio_type,
            prompt_version=(
                bgm_prompt_version if is_bgm else f"{audio_type}_prompt_v1"
            ),
            primary_instrument=(
                event.get("primary_instrument", "") if is_bgm else ""
            ),
            supporting_instruments=(
                event.get("supporting_instruments", []) if is_bgm else ()
            ),
            bpm=event.get("bpm", 0) if is_bgm else 0,
            raw_duration=event.get("duration", 0),
            audio_format=AUDIO_FORMAT,
            steps=STABLE_AUDIO_STEPS,
            music_style=event.get("music_style", "") if is_bgm else "",
            emotion_target=(
                event.get("emotion_target", "") if is_bgm else ""
            ),
            provider_url=STABLE_AUDIO_API_URL,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        logger.warning(
            "⚠️ 音频任务 metadata 无法构造，将使用 bypass: "
            "audio_type=%s event_id=%s error=%s",
            audio_type,
            event.get(f"{audio_type}_id", ""),
            exc,
        )
        return {}
    return metadata.model_dump()


def fetch_tasks_from_db(target_task_id, node_ids=None, sound_type="all", material_ids=None):
    """
    从数据库读取音频生成任务

    Args:
        target_task_id: 任务 ID
        node_ids: 章节 ID 列表，None=所有章节
        sound_type: "bgm"|"sfx"|"ambience"|"all"，默认 "all"
        material_ids: 指定的 bgm_id 或 sfx_id 列表，None=所有

    Returns:
        list of tuples: (task_id, node_id, audio_type, prompt, duration,
                         base_name, event_id, generation_metadata)
    """
    tasks = []
    json_data = load_sound_data(target_task_id)
    if not isinstance(json_data, dict):
        return tasks

    metadata_root = json_data.get("_meta", {})
    bgm_prompt_version = (
        metadata_root.get("prompt_version", "")
        if isinstance(metadata_root, dict)
        else ""
    )

    legacy_specs = (
        ("bgm", "bgm_events", "bgm_name", "bgm_id"),
        ("sfx", "sfx_events", "sfx_name", "sfx_id"),
        ("ambience", "ambience_events", "ambience_name", "ambience_id"),
    )
    for node_id, node_content in json_data.items():
        if node_id == "_meta":
            continue
        if node_ids is not None and node_id not in node_ids:
            continue
        for audio_type, list_key, name_key, id_key in legacy_specs:
            if sound_type not in (audio_type, "all"):
                continue
            for event_index, event in enumerate(
                node_content.get(list_key, []),
                start=1,
            ):
                event_id, base_name = _event_identity(
                    event,
                    id_key,
                    name_key,
                    node_id=node_id,
                    track=audio_type,
                    event_index=event_index,
                )
                if material_ids is not None and event_id not in material_ids:
                    continue
                tasks.append(
                    (
                        target_task_id,
                        node_id,
                        audio_type,
                        event.get("prompt", ""),
                        event.get("duration", 0),
                        base_name,
                        event_id,
                        _build_event_metadata(
                            audio_type,
                            event,
                            bgm_prompt_version,
                        ),
                    )
                )

    return tasks


def _schedule_int(event, field_name, fallback, *, minimum):
    value = event.get(field_name)
    if isinstance(value, bool):
        return fallback, True
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError):
        return fallback, True
    if normalized < minimum:
        return fallback, True
    return normalized, False


def fetch_scheduled_tasks(target_task_id):
    """读取一个作品的全部三轨事件并建立调度候选。"""
    json_data = load_sound_data(target_task_id)
    if not isinstance(json_data, dict):
        return []

    metadata_root = json_data.get("_meta", {})
    bgm_prompt_version = (
        metadata_root.get("prompt_version", "")
        if isinstance(metadata_root, dict)
        else ""
    )
    node_orders = resolve_node_order(json_data)
    candidates = []
    track_specs = (
        ("bgm", "bgm_events", "bgm_name", "bgm_id"),
        ("sfx", "sfx_events", "sfx_name", "sfx_id"),
        (
            "ambience",
            "ambience_events",
            "ambience_name",
            "ambience_id",
        ),
    )

    for node_id, node_content in json_data.items():
        if node_id == "_meta":
            continue
        if not isinstance(node_content, dict):
            raise SoundDataUnavailableError(
                f"node_not_object:{node_id}"
            )
        for audio_type, list_key, name_key, id_key in track_specs:
            events = node_content.get(list_key, [])
            if not isinstance(events, list):
                raise SoundDataUnavailableError(
                    f"track_not_list:{node_id}:{list_key}"
                )
            for event_index, event in enumerate(events, start=1):
                event_id, base_name = _event_identity(
                    event,
                    id_key,
                    name_key,
                    node_id=node_id,
                    track=audio_type,
                    event_index=event_index,
                )
                raw_importance = event.get("importance")
                importance = normalize_scheduler_importance(raw_importance)
                importance_is_valid = (
                    isinstance(raw_importance, str)
                    and raw_importance.strip().lower()
                    in {"critical", "important", "normal"}
                )
                if "importance" in event and not importance_is_valid:
                    logger.warning(
                        "invalid_sound_importance: node_id=%s audio_type=%s "
                        "event_id=%s value=%r",
                        node_id,
                        audio_type,
                        event_id,
                        raw_importance,
                    )
                importance_reason = event.get("importance_reason", "")
                if not isinstance(importance_reason, str):
                    importance_reason = ""
                start_order, bad_start = _schedule_int(
                    event,
                    "start_order",
                    event_index,
                    minimum=0,
                )
                beat_coverage, bad_coverage = _schedule_int(
                    event,
                    "beat_coverage",
                    1,
                    minimum=1,
                )
                if bad_start or bad_coverage:
                    logger.warning(
                        "missing_sound_schedule_order: node_id=%s "
                        "audio_type=%s event_id=%s",
                        node_id,
                        audio_type,
                        event_id,
                    )
                canonical_path = os.path.join(
                    OUTPUT_ROOT,
                    str(target_task_id),
                    str(node_id),
                    "audios",
                    audio_type,
                    f"{event_id}{AUDIO_EXT}",
                )
                candidates.append(
                    ScheduledAudioTask(
                        task_id=str(target_task_id),
                        node_id=str(node_id),
                        audio_type=audio_type,
                        prompt=(
                            event.get("prompt", "").strip()
                            if isinstance(event.get("prompt"), str)
                            else ""
                        ),
                        duration=event.get("duration", 0),
                        base_name=base_name,
                        event_id=event_id,
                        generation_metadata=_build_event_metadata(
                            audio_type,
                            event,
                            bgm_prompt_version,
                        ),
                        importance=importance,
                        importance_reason=importance_reason.strip(),
                        node_order=node_orders.get(str(node_id), 0),
                        start_order=start_order,
                        beat_coverage=beat_coverage,
                        canonical_path=canonical_path,
                    )
                )

    deduplicated, duplicate_keys = deduplicate_canonical_targets(candidates)
    for duplicate_key in duplicate_keys:
        logger.warning("duplicate_sound_event_id: key=%s", duplicate_key)
    return deduplicated


def _candidate_key(candidate):
    return (
        str(candidate.task_id),
        str(candidate.node_id),
        str(candidate.audio_type),
        str(candidate.event_id),
    )


def _timeline_suffix(base_name, event_id):
    if event_id.startswith(base_name + "_"):
        return event_id[len(base_name) + 1:]
    return event_id.split("_")[-1]


def _prepare_scheduled_clusters(
    candidates,
    recovery_context=None,
    metrics=None,
):
    """完成 canonical/cache 预扫描、冻结 source 复用并建立剩余聚类。"""
    if metrics is None and recovery_context is not None:
        metrics = getattr(recovery_context, "_sound_schedule_metrics", None)
    stats = {"canonical": 0, "cache": 0, "direct_reuse": 0}
    unfinished = []
    for candidate in candidates:
        if not candidate.canonical_path:
            candidate.canonical_path = os.path.join(
                OUTPUT_ROOT,
                str(candidate.task_id),
                str(candidate.node_id),
                "audios",
                candidate.audio_type,
                f"{candidate.event_id}{AUDIO_EXT}",
            )
        if is_valid_wav(candidate.canonical_path):
            stats["canonical"] += 1
            if metrics is not None:
                metrics.record_resolution("existing_canonical")
                logger.info(
                    "sound_resolved: source=existing_canonical task_id=%s "
                    "node_id=%s audio_type=%s event_id=%s canonical=%s",
                    candidate.task_id,
                    candidate.node_id,
                    candidate.audio_type,
                    candidate.event_id,
                    candidate.canonical_path,
                )
            if (
                recovery_context is not None
                and candidate.audio_type == "bgm"
            ):
                recovery_context.mark_bgm_covered(candidate.node_id)
            continue
        if Path(candidate.canonical_path).exists():
            _remove_invalid_audio_target(
                candidate.canonical_path,
                log_context="prescan_invalid_canonical",
            )
        unfinished.append(candidate)

    reusable_candidates = [
        candidate
        for candidate in unfinished
        if resolve_effective_cache_mode(
            "reuse",
            candidate.generation_metadata,
        ) == "reuse"
        and bool(candidate.prompt)
    ]
    if embedding_model is not None and reusable_candidates:
        prompts = [candidate.prompt for candidate in reusable_candidates]
        try:
            with encode_lock:
                encoded = np.asarray(embedding_model.encode(prompts), dtype=float)
            if encoded.ndim == 1 and len(reusable_candidates) == 1:
                encoded = encoded.reshape(1, -1)
            if len(encoded) != len(reusable_candidates):
                raise ValueError("embedding result count mismatch")
            for candidate, vector in zip(reusable_candidates, encoded):
                candidate.prompt_vector = np.asarray(vector, dtype=float)
        except Exception as exc:
            logger.warning(
                "sound_schedule_embedding_failed: task_id=%s error=%s",
                candidates[0].task_id if candidates else "",
                exc,
            )

    remaining_by_key = {
        _candidate_key(candidate): candidate
        for candidate in unfinished
    }
    reusable_sources = []
    total = len(unfinished)
    for index, candidate in enumerate(unfinished, start=1):
        if candidate.prompt_vector is None:
            continue
        metadata = normalize_generation_metadata(
            candidate.generation_metadata
        )
        if metadata is None:
            continue
        try:
            hit_result = search_local_asset(
                candidate.audio_type,
                candidate.prompt,
                metadata,
                query_vector=candidate.prompt_vector,
            )
        except Exception as exc:
            logger.warning(
                "sound_schedule_cache_query_failed: task_id=%s node_id=%s "
                "audio_type=%s event_id=%s error=%s",
                candidate.task_id,
                candidate.node_id,
                candidate.audio_type,
                candidate.event_id,
                exc,
            )
            hit_result = None
        target_path = Path(candidate.canonical_path)
        cache_result = _materialize_cache_hit(
            hit_result,
            base_name=candidate.base_name,
            target_dir=str(target_path.parent),
            timeline_suffix=_timeline_suffix(
                candidate.base_name,
                candidate.event_id,
            ),
            final_file_name=target_path.name,
            batch_name="PreScan-Cache",
            index=index,
            total=total,
            audio_type=candidate.audio_type,
        )
        if not cache_result or not is_valid_wav(target_path):
            continue
        try:
            similarity = f"{float(hit_result.get('score')):.4f}"
        except (TypeError, ValueError):
            similarity = "unknown"
        logger.info(
            "sound_schedule_cache_hit: source=global_cache_hit task_id=%s node_id=%s "
            "audio_type=%s event_id=%s asset_id=%s similarity=%s canonical=%s",
            candidate.task_id,
            candidate.node_id,
            candidate.audio_type,
            candidate.event_id,
            hit_result.get("asset_id"),
            similarity,
            target_path,
        )
        if metrics is not None:
            metrics.record_resolution("global_cache_hit")
        remaining_by_key.pop(_candidate_key(candidate), None)
        stats["cache"] += 1
        if (
            recovery_context is not None
            and candidate.audio_type == "bgm"
        ):
            recovery_context.mark_bgm_covered(candidate.node_id)
        source_vector = hit_result.get("prompt_vector")
        if source_vector is None:
            continue
        reusable_sources.append(
            ReusableAudioSource(
                asset_id=str(hit_result["asset_id"]),
                source_path=str(target_path),
                compatibility_key=compatibility_key(candidate),
                prompt_vector=np.asarray(source_vector, dtype=float),
            )
        )

    remaining_candidates = list(remaining_by_key.values())
    for source, target in assign_direct_sources(
        reusable_sources,
        remaining_candidates,
    ):
        target_key = _candidate_key(target)
        target_path = Path(target.canonical_path)
        copy_succeeded = False
        try:
            _atomic_publish_canonical_wav(
                target_path,
                _timeline_suffix(target.base_name, target.event_id),
                lambda temporary: shutil.copyfile(
                    source.source_path,
                    temporary,
                ),
                base_name=target.base_name,
            )
            copy_succeeded = True
        except (OSError, shutil.Error, ArtifactValidationError) as exc:
            logger.warning(
                "direct_source_copy_failed: asset_id=%s target=%s error=%s",
                source.asset_id,
                target_key,
                exc,
            )
        if copy_succeeded and is_valid_wav(target_path):
            logger.info(
                "sound_schedule_direct_reuse: source=direct_reuse task_id=%s "
                "source_asset_id=%s target_node_id=%s audio_type=%s "
                "target_event_id=%s similarity=%.4f target_canonical=%s canonical=%s",
                target.task_id,
                source.asset_id,
                target.node_id,
                target.audio_type,
                target.event_id,
                cosine_similarity(source.prompt_vector, target.prompt_vector),
                target_path,
                target_path,
            )
            remaining_by_key.pop(target_key, None)
            stats["direct_reuse"] += 1
            if metrics is not None:
                metrics.record_resolution("direct_reuse")
            if (
                recovery_context is not None
                and target.audio_type == "bgm"
            ):
                recovery_context.mark_bgm_covered(target.node_id)
            continue
        # Failed direct reuse remains in ``remaining_by_key`` and can enter
        # the normal clustering/provider path below.

    clusters = cluster_candidates(list(remaining_by_key.values()))
    clusters_by_type = {"bgm": [], "sfx": [], "ambience": []}
    for cluster in clusters:
        clusters_by_type[cluster.representative.audio_type].append(cluster)
    return clusters_by_type, stats


def run_scheduled_representative(
    cluster,
    recovery_context,
    quota_lease,
    metrics=None,
):
    if metrics is None and recovery_context is not None:
        metrics = getattr(recovery_context, "_sound_schedule_metrics", None)
    representative = cluster.representative
    effective_cache_mode = resolve_effective_cache_mode(
        "reuse",
        representative.generation_metadata,
    )
    task_info = (
        1,
        1,
        representative.task_id,
        representative.node_id,
        representative.audio_type,
        representative.prompt,
        representative.duration,
        representative.base_name,
        representative.event_id,
        "Scheduled-StableAudio",
        effective_cache_mode,
        representative.generation_metadata,
    )
    return process_one_audio_robust(
        task_info,
        recovery_context,
        quota_lease,
        metrics,
        "cluster_representative",
    )


def _materialize_cluster_members(cluster, *, metrics=None):
    representative = cluster.representative
    source_path = Path(representative.canonical_path)
    valid_members = []
    if not is_valid_wav(source_path):
        return valid_members

    for member in cluster.members:
        target_path = Path(member.canonical_path)
        if member is representative or target_path == source_path:
            valid_members.append(member)
            continue
        copy_succeeded = False
        try:
            _atomic_publish_canonical_wav(
                target_path,
                _timeline_suffix(member.base_name, member.event_id),
                lambda temporary: shutil.copyfile(source_path, temporary),
                base_name=member.base_name,
            )
            copy_succeeded = True
        except (OSError, shutil.Error, ArtifactValidationError) as exc:
            logger.warning(
                "cluster_member_copy_failed: source=%s target=%s error=%s",
                representative.member_key,
                member.member_key,
                exc,
            )
        if copy_succeeded and is_valid_wav(target_path):
            valid_members.append(member)
            if metrics is not None:
                metrics.record_resolution("cluster_member_copied")
                logger.info(
                    "sound_resolved: source=cluster_member_copied "
                    "task_id=%s source_node_id=%s source_event_id=%s "
                    "target_node_id=%s audio_type=%s target_event_id=%s "
                    "canonical=%s",
                    representative.task_id,
                    representative.node_id,
                    representative.event_id,
                    member.node_id,
                    member.audio_type,
                    member.event_id,
                    target_path,
                )
    return valid_members


def run_bgm_coverage_phase(
    clusters,
    quota,
    recovery_context,
    *,
    metrics=None,
):
    if metrics is None:
        metrics = getattr(recovery_context, "_sound_schedule_metrics", None)
    remaining_bgm_clusters = list(clusters)
    all_candidate_nodes = {
        node_id
        for cluster in remaining_bgm_clusters
        for node_id in cluster.covered_node_ids
    }
    covered_node_ids = recovery_context.bgm_covered_nodes()
    uncovered_nodes = all_candidate_nodes.difference(covered_node_ids)

    while (
        remaining_bgm_clusters
        and uncovered_nodes
        and not INSUFFICIENT_BALANCE.is_set()
    ):
        snapshot = quota.snapshot()
        if snapshot is None or snapshot.remaining <= 0:
            break
        selected = select_bgm_coverage_wave(
            remaining_bgm_clusters,
            uncovered_nodes,
            min(MAX_WORKERS, snapshot.remaining),
        )
        if not selected:
            break

        submissions = []
        for cluster in selected:
            representative = cluster.representative
            lease = quota.try_reserve(
                audio_type="bgm",
                event_id=representative.event_id,
            )
            if lease is None:
                break
            remaining_bgm_clusters.remove(cluster)
            submissions.append((cluster, lease))

        if not submissions:
            latest_snapshot = quota.snapshot()
            if latest_snapshot is not None and latest_snapshot.remaining > 0:
                logger.error(
                    "sound_generation_quota_reserve_unavailable: "
                    "phase=bgm_coverage remaining=%s",
                    latest_snapshot.remaining,
                )
            break

        with ThreadPoolExecutor(max_workers=len(submissions)) as executor:
            def submit_representative(cluster, lease):
                return executor.submit(
                    run_scheduled_representative,
                    cluster,
                    recovery_context,
                    lease,
                )

            future_map = {
                submit_representative(cluster, lease): (cluster, lease)
                for cluster, lease in submissions
            }
            for future in as_completed(future_map):
                cluster, lease = future_map[future]
                try:
                    result = future.result()
                    logger.info(result)
                except Exception as exc:
                    logger.error(
                        "scheduled_bgm_worker_failed: event_id=%s error=%s",
                        cluster.representative.event_id,
                        exc,
                    )
                finally:
                    lease.release_if_pending()

                if is_valid_wav(cluster.representative.canonical_path):
                    valid_members = _materialize_cluster_members(
                        cluster,
                        metrics=metrics,
                    )
                    newly_covered = {
                        str(member.node_id) for member in valid_members
                    }
                    covered_node_ids.update(newly_covered)
                    uncovered_nodes.difference_update(newly_covered)
                    for node_id in newly_covered:
                        recovery_context.mark_bgm_covered(node_id)

    return CoveragePhaseResult(
        covered_node_ids=covered_node_ids,
        remaining_bgm_clusters=remaining_bgm_clusters,
    )


def run_competition_phase(
    clusters,
    quota,
    recovery_context,
    *,
    metrics=None,
):
    if metrics is None:
        metrics = getattr(recovery_context, "_sound_schedule_metrics", None)
    pending_clusters = list(clusters)
    while (
        pending_clusters
        and not INSUFFICIENT_BALANCE.is_set()
    ):
        snapshot = quota.snapshot()
        if snapshot is None or snapshot.remaining <= 0:
            break
        ordered = order_competition_clusters(
            pending_clusters,
            snapshot.success_counts,
        )
        selected = ordered[:min(MAX_WORKERS, snapshot.remaining)]
        submissions = []
        for cluster in selected:
            representative = cluster.representative
            lease = quota.try_reserve(
                audio_type=representative.audio_type,
                event_id=representative.event_id,
            )
            if lease is None:
                break
            pending_clusters.remove(cluster)
            submissions.append((cluster, lease))

        if not submissions:
            latest_snapshot = quota.snapshot()
            if latest_snapshot is not None and latest_snapshot.remaining > 0:
                logger.error(
                    "sound_generation_quota_reserve_unavailable: "
                    "phase=competition remaining=%s",
                    latest_snapshot.remaining,
                )
            break

        with ThreadPoolExecutor(max_workers=len(submissions)) as executor:
            def submit_representative(cluster, lease):
                return executor.submit(
                    run_scheduled_representative,
                    cluster,
                    recovery_context,
                    lease,
                )

            future_map = {
                submit_representative(cluster, lease): (cluster, lease)
                for cluster, lease in submissions
            }
            for future in as_completed(future_map):
                cluster, lease = future_map[future]
                try:
                    result = future.result()
                    logger.info(result)
                except Exception as exc:
                    logger.error(
                        "scheduled_competition_worker_failed: event_id=%s "
                        "error=%s",
                        cluster.representative.event_id,
                        exc,
                    )
                finally:
                    lease.release_if_pending()
                if is_valid_wav(cluster.representative.canonical_path):
                    _materialize_cluster_members(cluster, metrics=metrics)


def run_batch_execution(
    file_list,
    batch_name,
    cache_mode="reuse",
    recovery_context=None,
):
    total = len(file_list)
    if total == 0:
        return 0

    logger.info(f"\n🚀 启动批次: {batch_name} | 任务数: {total}")

    current_workers = MAX_WORKERS
    if "Retry" in batch_name:
        current_workers = max(2, int(MAX_WORKERS / 2))

    start_time = time.time()

    # ==========================================
    # 【新增】细化的分类统计计数器
    # ==========================================
    success_count = 0
    api_gen_count = 0
    cache_hit_count = 0
    skip_count = 0
    fail_count = 0

    task_args = []
    has_reuse_task = False
    for i, raw_task in enumerate(file_list, 1):
        (
            tid,
            node,
            audio_type,
            prompt,
            duration,
            base_name,
            event_id,
            generation_metadata,
        ) = unpack_audio_task(raw_task)
        effective_cache_mode = resolve_effective_cache_mode(
            cache_mode,
            generation_metadata,
        )
        if effective_cache_mode == "reuse":
            has_reuse_task = True
        task_args.append(
            (
                i,
                total,
                tid,
                node,
                audio_type,
                prompt,
                duration,
                base_name,
                event_id,
                batch_name,
                effective_cache_mode,
                generation_metadata,
            )
        )

    if has_reuse_task:
        init_sound_assets_table()

    with ThreadPoolExecutor(max_workers=current_workers) as executor:
        future_to_task = {
            executor.submit(process_one_audio_robust, arg, recovery_context): arg
            for arg in task_args
        }
        for future in as_completed(future_to_task):
            try:
                res = future.result()
                logger.info(res)

                # ==========================================
                # 【修改】通过识别返回日志中的 Emoji 来分类计分
                # ==========================================
                if "✅" in res:
                    success_count += 1
                    api_gen_count += 1
                elif "🎯" in res:  # 这一句同时兼容了本地命中 "🎯" 和 OSS 命中 "☁️🎯"
                    success_count += 1
                    cache_hit_count += 1
                elif "⏭️" in res:
                    success_count += 1
                    skip_count += 1
                else:
                    fail_count += 1

            except Exception as e:
                logger.error(f"线程执行发生严重异常: {e}")
                fail_count += 1

            # 余额不足熔断：取消尚未开始的任务，尽快退出批次
            if INSUFFICIENT_BALANCE.is_set():
                for f in future_to_task:
                    f.cancel()
                logger.error(f"🛑 批次 {batch_name} 因余额不足提前中止，已取消未开始的任务。")
                break

    # ==========================================
    # 【修改】批次结束时，打印一份极其清晰的数据大盘
    # ==========================================
    logger.info(f"⏱️  批次 {batch_name} 结束，总成功率 {success_count}/{total}，耗时: {time.time() - start_time:.2f}s")
    logger.info(
        f"📊 【本次执行盘点】 🌟 API 新生成: {api_gen_count} 个 | 🎯 缓存命中: {cache_hit_count} 个 | ⏭️ 跳过已存在: {skip_count} 个 | ❌ 失败: {fail_count} 个\n")

    return success_count


def check_missing_tasks(tasks, recovery_context=None):
    """验证文件落盘情况：只有基础有效的 canonical WAV 才算完成。
    旧格式（带哈希）文件不算完成，会在 run_batch_execution 时被清理并替换为新格式。
    """
    missing = []
    for task in tasks:
        tid, node, audio_type, prompt, duration, base_name, event_id, _ = (
            unpack_audio_task(task)
        )

        if (
            recovery_context is not None
            and audio_type == "sfx"
            and recovery_context.is_terminal(tid, node, event_id)
        ):
            continue

        target_dir = os.path.join(OUTPUT_ROOT, str(tid), node, "audios", audio_type)
        expected_path = os.path.join(target_dir, f"{event_id}{AUDIO_EXT}")

        if not is_valid_wav(expected_path):
            missing.append(task)

    return missing


def main(target_task_id=None):
    # Celery 子进程会复用处理多个任务；上一任务的余额熔断不能污染新作品。
    INSUFFICIENT_BALANCE.clear()

    if not target_task_id:
        logger.info("🔍 未指定 task_id，查找 sound 表中的最新任务...")
        target_task_id = get_latest_task_id()
        if not target_task_id:
            logger.error("❌ 错误: sound 表为空，未找到任何可处理的任务。")
            if _usage_tracker:
                _usage_tracker.flush()
            return

    if _usage_tracker:
        _usage_tracker.set_context(
            target_task_id, "sound_arranger", force_new_run=True
        )

    logger.info(f"🚀 锁定任务 Task ID: {target_task_id}")
    logger.info(f"🔍 [Init] 正在从数据库获取 Sound JSON 数据...")

    schedule_metrics = SoundScheduleMetrics()

    scheduled_tasks = fetch_scheduled_tasks(target_task_id)
    if not scheduled_tasks:
        logger.info("🎉 [Init] 未提取到任何音频任务，程序退出。")
        if _usage_tracker:
            _usage_tracker.flush()
        return

    logger.info("📋 共解析出 %s 个音频生成请求。", len(scheduled_tasks))

    recovery_context = Sfx403RecoveryContext()
    recovery_context._sound_schedule_metrics = schedule_metrics
    has_reusable_candidates = embedding_model is not None and any(
        resolve_effective_cache_mode(
            "reuse",
            candidate.generation_metadata,
        ) == "reuse"
        for candidate in scheduled_tasks
    )
    if has_reusable_candidates:
        try:
            init_sound_assets_table()
        except Exception as exc:
            logger.error(
                "sound_assets_init_failed_before_schedule: task_id=%s error=%s",
                target_task_id,
                exc,
            )

    clusters_by_type, prescan_stats = _prepare_scheduled_clusters(
        scheduled_tasks,
        recovery_context=recovery_context,
    )
    all_clusters = [
        *clusters_by_type["bgm"],
        *clusters_by_type["sfx"],
        *clusters_by_type["ambience"],
    ]
    if not all_clusters:
        logger.info(
            "🎉 [Init] 全部音频已由 canonical、全局缓存或作品内复用完成。"
        )
        _log_schedule_metrics(schedule_metrics, task_id=target_task_id)
        if _usage_tracker:
            _usage_tracker.flush()
        return

    quota = SoundGenerationQuota(str(target_task_id))
    initial_snapshot = quota.snapshot()
    if initial_snapshot is None:
        logger.error(
            "sound_generation_quota_unavailable: task_id=%s，"
            "本次停止新的 Stable Audio 请求，保留预扫描结果。",
            target_task_id,
        )
        _log_schedule_metrics(schedule_metrics, task_id=target_task_id)
        if _usage_tracker:
            _usage_tracker.flush()
        return

    coverage = run_bgm_coverage_phase(
        clusters_by_type["bgm"],
        quota,
        recovery_context,
    )
    competition_clusters = [
        *coverage.remaining_bgm_clusters,
        *clusters_by_type["sfx"],
        *clusters_by_type["ambience"],
    ]
    run_competition_phase(
        competition_clusters,
        quota,
        recovery_context,
    )

    final_snapshot = quota.snapshot()
    unresolved = [
        candidate
        for candidate in scheduled_tasks
        if not is_valid_wav(candidate.canonical_path)
    ]
    unresolved_by_type = {
        audio_type: sum(
            1
            for candidate in unresolved
            if candidate.audio_type == audio_type
        )
        for audio_type in ("bgm", "sfx", "ambience")
    }
    bgm_node_ids = {
        str(candidate.node_id)
        for candidate in scheduled_tasks
        if candidate.audio_type == "bgm"
    }
    covered_bgm_node_ids = {
        str(candidate.node_id)
        for candidate in scheduled_tasks
        if candidate.audio_type == "bgm"
        and is_valid_wav(candidate.canonical_path)
    }
    uncovered_bgm_nodes = bgm_node_ids.difference(covered_bgm_node_ids)
    metrics_values = schedule_metrics.snapshot()
    logger.info("\n" + "=" * 40)
    logger.info(
        "📊 [Sound 调度盘点] existing_canonical=%s | "
        "global_cache_hit=%s | direct_reuse=%s | "
        "cluster_representative_generated=%s | cluster_member_copied=%s | "
        "global_cache_write_success=%s | global_cache_write_failure=%s | "
        "unresolved=%s (BGM=%s SFX=%s Ambience=%s) | uncovered_bgm_nodes=%s",
        metrics_values["existing_canonical"],
        metrics_values["global_cache_hit"],
        metrics_values["direct_reuse"],
        metrics_values["cluster_representative_generated"],
        metrics_values["cluster_member_copied"],
        metrics_values["global_cache_write_success"],
        metrics_values["global_cache_write_failure"],
        len(unresolved),
        unresolved_by_type["bgm"],
        unresolved_by_type["sfx"],
        unresolved_by_type["ambience"],
        len(uncovered_bgm_nodes),
    )
    _log_schedule_metrics(schedule_metrics, task_id=target_task_id)
    if final_snapshot is not None:
        logger.info(
            "📊 [%s额度] BGM=%s | SFX=%s | Ambience=%s | "
            "预占=%s | 剩余=%s",
            getattr(final_snapshot, "quota_limit", 500),
            final_snapshot.bgm_success_count,
            final_snapshot.sfx_success_count,
            final_snapshot.ambience_success_count,
            final_snapshot.reserved_count,
            final_snapshot.remaining,
        )
    if unresolved:
        logger.warning(
            "⚠️ [Stable Audio] %s 个低优先或失败事件没有音频，"
            "Sound 按降级合同正常结束。",
            len(unresolved),
        )
    else:
        logger.info("🎉 [Stable Audio] 所有三轨音频事件均已拥有有效 WAV。")
    logger.info("=" * 40)

    if _usage_tracker:
        _usage_tracker.flush()


if __name__ == "__main__":
    main(target_task_id="")
