"""
FastAPI入口服务 - 批量处理小说任务
作为独立的Docker服务运行
"""
import asyncio
import hashlib
import os
import sys
import logging
import logging.handlers
import uuid
from pathlib import Path
from typing import List, Optional, Union
import json
import re
import mimetypes
from datetime import date, datetime, time, timedelta

from fastapi import FastAPI, UploadFile, File, HTTPException, Form, Request
from fastapi.responses import FileResponse
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_, func, or_, text, update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

# 添加项目根目录到路径
project_root = Path(__file__).resolve().parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# 初始化日志（控制台 + 文件）
_LOG_DIR = Path(__file__).resolve().parent / "log"
_LOG_DIR.mkdir(exist_ok=True)
_LOG_FORMAT = "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
_log_formatter = logging.Formatter(_LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")

# 模块级别无条件创建 handler（startup 事件里会用到）
_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(_log_formatter)

_file_handler = logging.handlers.RotatingFileHandler(
    _LOG_DIR / "api_gateway.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_file_handler.setFormatter(_log_formatter)

logger = logging.getLogger(__name__)

from common.celery_app import celery_app
from common.artifact_io import atomic_write_bytes
from common.config import RAW_NOVELS_PATH, OUTPUTS_PATH, BUILT_GAMES_PATH
from common.oss_utils import local_path_for_key, oss_read, oss_write
from common.local_runtime import local_pipeline_enabled, submit_local_visual_pipeline
from common.output_mirror import fetch_output_file
from common.story_tree_locale import select_story_tree_for_consumer
from common.style_anchor_generation import (
    AnchorGenerationError,
    normalize_uploaded_anchor,
)
from common.visual_style import (
    ANCHOR_RELATIVE_PATHS,
    ANCHOR_STATUS_FAILED,
    ANCHOR_STATUS_CONFIRMED,
    ANCHOR_STATUS_GENERATING,
    ANCHOR_STATUS_MISSING,
    ANCHOR_STATUS_READY,
    ANCHOR_STATUS_STALE,
    ASSET_CHARACTER,
    ASSET_SCENE,
    REMOVE_CHARACTER_BACKGROUND_KEY,
    STYLE_MODE_CUSTOM,
    STYLE_MODE_LLM,
    VALID_ASSET_KINDS,
    VALID_STYLE_MODES,
    VisualStyleConfigError,
    file_sha256,
    load_visual_style_config,
    persist_visual_style_bundle_for_run,
    save_visual_style_config,
    style_anchor_path,
    validate_visual_style_for_run,
    visual_style_timestamp,
)
from common.task_models import (
    NovelTask, TaskStatus, ArtStyle, StoryMode, InfoConfirmStage,
    TextModel, ImageModel, AudioModel, Resolution, engine as business_engine,
)
from common.platform_db import platform_engine as engine  # novel_tasks 已迁平台库
from common.platform_client import resolve_pipeline_name, submit_platform_task
from common.task_editors import (
    edited_task_ids_for_user_name,
    ensure_task_editors_schema,
    get_task_editors,
    record_task_editor,
)
from common.task_remakes import (
    append_remake_task_id,
    ensure_task_remakes_schema,
    normalize_remake_task_ids,
)
from common.task_schema import ensure_novel_tasks_schema
from services.story_planner.core.world_analyst import WorldAnalyst
from services.api_gateway.core.cast_updater import (
    CastArtifactError,
    CastNotFoundError,
    CastValidationError,
    normalize_beat_id,
    normalize_character_position,
    normalize_resource_id,
    read_cast as read_cast_from_package,
    update_cast as apply_cast_update,
)
from services.api_gateway.core.scene_shot_updater import (
    SceneShotUpdateError,
    SceneShotUploadError,
    apply_scene_shot_image,
    extract_variant_scene_ids,
    resolve_original_scene_id,
)
from services.api_gateway.request_limits import RequestBodyLimitMiddleware


_DEFAULT_MAX_BATCH_REQUEST_BYTES = 72 * 1024 * 1024

# 在线故事树接口只接受运行时实际支持的两个 locale。未传或非法值
# 统一回退英语，避免把 zh-CN、短码或任意 BCP-47 变体带入在线包。
_ONLINE_STORY_TREE_LANGUAGES = {"en-US", "ja-JP"}


def _story_tree_request_language(lang: Optional[str]) -> str:
    # Match the online game's exact locale contract; do not accept whitespace,
    # aliases, or other BCP-47 variants as a valid language selector.
    value = str(lang or "")
    return value if value in _ONLINE_STORY_TREE_LANGUAGES else "en-US"

app = FastAPI(
    title="Novel Game Factory API Gateway",
    description="批量处理小说，生成视觉小说游戏的入口服务",
    version="1.0.0"
)
app.add_middleware(
    RequestBodyLimitMiddleware,
    path="/api/v1/novels/batch",
    env_name="GATEWAY_MAX_BATCH_REQUEST_BYTES",
    default_limit=_DEFAULT_MAX_BATCH_REQUEST_BYTES,
)

# 配置CORS。后添加的 CORS 位于自定义 body guard 外层，因此 middleware
# 直接返回的 400/413 也会带上浏览器需要的跨域响应头。
# 注意：当 allow_credentials=True 时，浏览器会拒绝 Allow-Origin: *。
# 用 allow_origin_regex 让 starlette 把请求的 Origin 反射回去，规避非法组合。
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=".*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(request: Request, exc: RequestValidationError):
    logger.warning(
        f"[422] {request.method} {request.url.path} content-type={request.headers.get('content-type')} "
        f"errors={exc.errors()}"
    )
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=422, content=jsonable_encoder({"detail": exc.errors()}))


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request, exc):
    logger.exception(f"[500] {request.method} {request.url.path} — {exc}")
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=500, content={"detail": str(exc)})


@app.on_event("startup")
async def _reconfigure_logging():
    """uvicorn 启动后重新配置日志，确保我们的 handler/formatter 生效。"""
    root = logging.getLogger()
    # 移除 uvicorn 注入的所有 handler，换成我们自己的
    root.handlers.clear()
    root.addHandler(_console_handler)
    root.addHandler(_file_handler)
    root.setLevel(logging.INFO)
    for _noisy in ("httpx", "httpcore", "urllib3", "multipart", "uvicorn.access"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)
    logger.info("[api_gateway] 日志系统已初始化")


@app.on_event("startup")
async def _ensure_local_pipeline_schema():
    """Create the local visual-pipeline tables on first startup."""
    if not local_pipeline_enabled():
        return
    try:
        from services.story_planner.table.models import init_db
        from common.local_pipeline_schema import ensure_local_pipeline_schema

        init_db()
        ensure_local_pipeline_schema()
        logger.info("[api_gateway] 本地 Pipeline 数据库结构已就绪")
    except Exception:
        logger.exception("[api_gateway] 本地 Pipeline 数据库初始化失败")
        raise


@app.on_event("startup")
async def _fix_novel_tasks_sequence():
    """对齐 novel_tasks 主键序列，避免历史显式 id 导入导致序列落后引发的
    duplicate key (novel_tasks_pkey) 插入冲突。"""
    from sqlalchemy import text
    try:
        with engine.begin() as conn:
            new_val = conn.execute(text(
                "SELECT setval("
                "pg_get_serial_sequence('public.novel_tasks', 'id'), "
                "(SELECT COALESCE(MAX(id), 1) FROM public.novel_tasks), true)"
            )).scalar()
        logger.info(f"[api_gateway] novel_tasks 序列已对齐到 {new_val}")
    except Exception as e:
        logger.error(f"[api_gateway] 对齐 novel_tasks 序列失败: {e}")


@app.on_event("startup")
async def _ensure_novel_tasks_schema():
    """补齐共享任务表的历史缺列,避免新接口查询旧表直接 500。"""
    try:
        ensure_novel_tasks_schema()
        logger.info("[api_gateway] 双库 novel_tasks 共享字段已就绪")
    except Exception:
        logger.exception("[api_gateway] 初始化 novel_tasks 共享字段失败")
        raise


@app.on_event("startup")
async def _ensure_task_editors_column():
    """确保 agent_platform/extreme 两个库的作品编辑者名单字段存在。"""
    try:
        ensure_task_editors_schema()
        logger.info("[api_gateway] 双库 novel_tasks.editors 字段已就绪")
    except Exception:
        logger.exception("[api_gateway] 初始化 novel_tasks.editors 字段失败")
        raise


@app.on_event("startup")
async def _ensure_task_remakes_column():
    """确保 agent_platform/extreme 两个库的复刻任务列表字段存在。"""
    try:
        ensure_task_remakes_schema()
        logger.info("[api_gateway] 双库 novel_tasks.remake_task_ids 字段已就绪")
    except Exception:
        logger.exception("[api_gateway] 初始化 novel_tasks.remake_task_ids 字段失败")
        raise


@app.on_event("startup")
async def _backfill_task_editors_from_regen_history():
    """把已有的成功素材重生成操作人回填到尚无编辑者的旧作品。"""
    try:
        with business_engine.connect() as connection:
            rows = connection.execute(text("""
                SELECT task_id, user_id, user_name, created_at
                FROM (
                    SELECT DISTINCT ON (task_id, user_id)
                           task_id, user_id, user_name, created_at
                    FROM public.material_regen_sessions
                    WHERE user_id IS NOT NULL
                      AND user_id <> ''
                      AND (status IS NULL OR status = 'success')
                    ORDER BY task_id, user_id, created_at DESC
                ) AS latest_editor
                ORDER BY task_id, created_at
            """)).fetchall()

        if rows:
            editors_by_task = get_task_editors({row[0] for row in rows})
            empty_task_ids = {
                task_id for task_id, editors in editors_by_task.items() if not editors
            }
            backfilled = 0
            for task_id, user_id, user_name, _created_at in rows:
                if task_id in empty_task_ids and record_task_editor(task_id, user_id, user_name):
                    backfilled += 1
            if backfilled:
                logger.info("[api_gateway] 已从素材重生成记录回填 %s 条作品编辑者", backfilled)
    except Exception as exc:
        # 老环境可能尚未给 material_regen_sessions 增加 user/status 字段；
        # 回填失败不阻塞新编辑者名单功能，后续成功操作仍会正常记录。
        logger.warning("[api_gateway] 旧编辑者记录回填跳过: %s", exc)


# ==================== 响应模型 ====================

class TaskInfo(BaseModel):
    """任务信息"""
    task_id: str
    project_name: str
    novel_filename: str
    output_language: str
    art_style: str
    story_mode: str
    celery_chain_id: Optional[str] = None
    status: str = "queued"
    error: Optional[str] = None


class BatchProcessResponse(BaseModel):
    """批量处理响应"""
    success: bool
    total: int
    tasks: List[TaskInfo]
    message: str
    batch_id: Optional[str] = None


class TaskStatusResponse(BaseModel):
    """任务状态响应"""
    task_id: str
    project_name: str
    novel_filename: str
    output_language: str
    art_style: str
    story_mode: str
    gameplay: Optional[str] = None
    upload_type: Optional[str] = None
    overall_status: str
    progress_percentage: int
    stages: dict
    error_message: Optional[str]
    create_time: datetime
    update_time: datetime
    completed_at: Optional[datetime]
    info_confirm_stage: Optional[str] = None
    editors: List[dict] = Field(default_factory=list)
    remake_count: int = 0
    remake_task_ids: List[str] = Field(default_factory=list)


class TaskListResponse(BaseModel):
    """任务列表响应"""
    total: int
    tasks: List[dict]


class TaskStatusCountsResponse(BaseModel):
    """作品状态筛选数量响应。"""
    new: int
    published: int
    unpublished: int
    participated: int


# 进入接口返回的阶段枚举(与前端约定)。由单字段 info_confirm_stage 派生。
class EntryStage:
    CREATE = "create"            # ① 创建/上传
    CONFIG = "config"            # ② 输出约束
    CONFIRM = "confirm"          # ② 信息确认（含摘要生成中）
    MATERIAL = "material"        # ③ 人物场景
    PERFORMANCE = "performance"  # ④ 演绎微调
    PUBLISH = "publish"          # ⑤ 发布


class TaskEntryResponse(BaseModel):
    """作品进入接口响应:stage 为算好的主结论,前端直接据此跳转。"""
    task_id: str
    stage: str
    overall_status_code: int
    progress_percentage: int
    info_confirm_stage: Optional[str] = None
    is_published: bool
    error_message: Optional[str] = None


class GenerationParamsResponse(BaseModel):
    """任务的生成参数：3 个模型 + 分辨率 + 视觉风格。

    展示某 task_id 生成时选定的模型/分辨率/画面风格。
    """
    task_id: str
    text_model: Optional[str] = None
    image_model: Optional[str] = None
    audio_model: Optional[str] = None
    resolution: Optional[str] = None
    art_style: str


class ImageModelOptionsResponse(BaseModel):
    """重新生图可选模型响应。"""
    default: str
    options: List[str]


class VisualStyleUpdateRequest(BaseModel):
    """Task-level visual-style settings edited before full generation starts."""
    mode: str = STYLE_MODE_LLM
    character_global_style: str = ""
    scene_global_style: str = ""
    remove_character_background: bool = True


class StyleAnchorGenerateRequest(BaseModel):
    """Optional model override; omitted means the task's selected image model."""
    image_model: Optional[str] = None


class StyleAnchorJobResponse(BaseModel):
    task_id: str
    asset_kind: str
    job_id: str
    status: str
    celery_state: Optional[str] = None
    error: Optional[str] = None
    preview_url: Optional[str] = None
    prompt: Optional[str] = None
    model: Optional[str] = None
    sha256: Optional[str] = None


# ==================== 素材重生成 Request/Response 模型 ====================

class RegenerateItem(BaseModel):
    """单个重生成项"""
    material_id: str
    material_subtype: str
    status: str  # success | skipped | failed
    session_no: Optional[int] = None
    node_id: Optional[str] = None        # 角色级换音色批量时标记所属节点
    voice_id: Optional[str] = None       # 本次使用的音色 ID（audio_producer）
    path: Optional[str] = None  # OSS URL
    error: Optional[str] = None


class ActivateVersionRequest(BaseModel):
    """激活指定版本请求"""
    task_id: str
    target_material_version: int       # 要激活的版本号（0=原始版本）
    node_id: str                       # OSS 格式 node_id
    material_id: str                   # 文件名（如 LiYu_1_calm.png / scene_alley_day_frost_01.png）
    subdir: Optional[str] = None       # 可不传，后端从 DB 的 oss_key 中自动解析
    language: str = ""                 # 语言维度：audio_producer 用 zh-CN/en-US/ja-JP；其它服务留空


class RollbackVersionRequest(BaseModel):
    """回滚到上一个版本请求"""
    task_id: str
    node_id: str                        # OSS 格式 node_id
    material_id: str                    # 素材 ID（不含后缀）
    current_material_version: int       # 当前版本号，回滚到 version-1
    language: str = ""                  # 语言维度：audio_producer 用 zh-CN/en-US/ja-JP；其它服务留空


class CharDesignerRegenerateRequest(BaseModel):
    """角色素材重生成请求（单张图片）。emotion=calm 走文生图并联动重生成同 combination 的所有 emotion；emotion!=calm 以同 combination 的 calm 图为基图走图生图。"""
    task_id: str = Field(..., description="任务 ID")
    node_id: str = Field(..., description="OSS 格式章节 ID")
    material_id: str = Field(
        ...,
        description="格式 {speaker_id}_{p-b-c}_{emotion}[_{景别}]，不含后缀。景别可选 CS/MS/LS（默认 LS）。新旧两种格式后端都能解析：旧 Liang_1-1-1_calm、ShiXing_1-1-1_angry；新 Liang_1-1-1_calm_LS",
    )
    user_id: str = Field(..., description="操作人 ID")
    user_name: str = Field(..., description="操作人名称")
    new_prompt: Optional[str] = Field(None, description="新 positive_prompt（只更新该 combination 的提示词）")
    model_name: Optional[str] = Field(
        None,
        description="图片模型：gpt-image-2 / qwen-image-plus / wan2.5-t2i-preview；不传=系统默认（gpt）。Wan/Qwen 的图生图统一走 Qwen-Image-Edit",
    )
    force: bool = Field(True, description="强制重生成，默认 true")
    session_no: Optional[int] = Field(None, description="操作序号，null=自动分配")


class ScenePainterRegenerateRequest(BaseModel):
    """场景素材重生成请求（单个素材）。场景提示词读写自 scene_prompts 表，封面提示词在 backgrounds 表。"""
    task_id: str = Field(..., description="任务 ID")
    user_id: str = Field(..., description="操作人 ID")
    user_name: str = Field(..., description="操作人名称")
    material_type: str = Field("scene", description="scene 或 background（暂不支持 all）")
    scene_ids: Optional[List[str]] = Field(None, description="material_type=scene 时必填，只能包含 1 个")
    background_node_ids: Optional[List[str]] = Field(
        None,
        description="material_type=background 时必填，只能包含 1 个；支持 OSS 格式 node_id 或 \"novel_cover\"",
    )
    new_prompts: Optional[dict] = Field(None, description="{material_id: new_prompt}")
    model_name: Optional[str] = Field(
        None,
        description="图片模型：gpt-image-2 / wan2.5-t2i-preview / qwen-image-plus；不传=环境默认。系统按模型名自动路由",
    )
    force: bool = Field(True, description="强制重生成，默认 true")
    session_no: Optional[int] = Field(None, description="操作序号，null=自动分配")
    # 以下批量参数暂不开放
    # material_type: str = "all"                 # all=全量重生成（批量）
    # scene_ids: None                            # None=全部场景（批量）
    # background_node_ids: None                  # None=全部封面（批量）


class AudioProducerRegenerateRequest(BaseModel):
    """音频素材重生成请求（单条音频）。不改音色，始终用该角色在 voice_mappings[language] 中当前的音色；换音色请用角色级换音色接口。"""
    task_id: str = Field(..., description="任务 ID")
    node_id: str = Field(..., description="OSS 格式章节 ID")
    material_id: str = Field(..., description="格式 {scene_id}-{beat_id}，不含后缀，如 scene_forest_day_dappled_sunlight_01-14")
    user_id: str = Field(..., description="操作人 ID")
    user_name: str = Field(..., description="操作人名称")
    new_content: Optional[str] = Field(None, description="新台词内容（同时更新 plots 表和字幕文件；仅 zh-CN 会回写 plots 源文本）")
    language: str = Field("zh-CN", description="语言 zh-CN/en-US/ja-JP，默认 zh-CN")
    force: bool = Field(True, description="强制重生成，默认 true")
    session_no: Optional[int] = Field(None, description="操作序号，null=自动分配")
    # 以下批量参数暂不开放
    # material_id: Optional[str] = None          # None=该节点所有音频（批量）


class SoundArrangerRegenerateRequest(BaseModel):
    """音效素材重生成请求（单个音效）"""
    task_id: str = Field(..., description="任务 ID")
    node_id: str = Field(..., description="OSS 格式章节 ID")
    material_id: str = Field(..., description="bgm_id 或 sfx_id，不含扩展名，如 bgm_melancholic_wolf_ambience_1-27")
    user_id: str = Field(..., description="操作人 ID")
    user_name: str = Field(..., description="操作人名称")
    sound_type: Optional[str] = Field("all", description="bgm / sfx / all，默认 all")
    new_prompt: Optional[str] = Field(None, description="新提示词")
    force: bool = Field(True, description="强制重生成，默认 true")
    session_no: Optional[int] = Field(None, description="操作序号，null=自动分配")
    # 以下批量参数暂不开放
    # material_id: Optional[str] = None          # None=该节点所有音效（批量）


class CharacterVoiceRegenerateRequest(BaseModel):
    """角色级换音色请求：更新 voice_mappings 并联动重生成该角色全部台词；字幕前缀不完整时由标准 master JSON 识别 speaker。"""
    task_id: str = Field(..., description="任务 ID")
    speaker_id: str = Field(..., description="角色 speaker_id（与 master/characters/name_mappings 一致）")
    voice_id: str = Field(..., description="新音色 ID")
    user_id: str = Field(..., description="操作人 ID")
    user_name: str = Field(..., description="操作人名称")
    language: str = Field("zh-CN", description="语言，默认 zh-CN（只改中文，不影响日英）")


class MaterialRegenerateResponse(BaseModel):
    """素材重生成响应"""
    regenerate_id: str
    task_id: str
    service_type: str
    status: str  # success | partial | failed | processing
    total: int = 0
    success: int = 0
    failed: int = 0
    items: List[RegenerateItem] = []
    errors: List[str] = []
    started_at: datetime
    completed_at: Optional[datetime] = None
    emotion_pending: bool = Field(
        False,
        description="角色 calm 图重生成场景：calm 已完成并返回 success，但联动的情绪变体仍在后台生成。"
                    "为 true 时 items 只含 calm；如需变体结果可继续轮询直到该字段为 false。"
    )


_MAX_NOVEL_FILE_BYTES = 50 * 1024 * 1024
_DEFAULT_MAX_NOVEL_CHARS = 1_500_000
_DEFAULT_MAX_BATCH_FILE_BYTES = 64 * 1024 * 1024
_DEFAULT_MAX_BATCH_TOTAL_CHARS = 50_000_000
_DEFAULT_BATCH_PERSIST_CONCURRENCY = 10
_DEFAULT_BATCH_SUBMIT_CONCURRENCY = 10
_DEFAULT_PLATFORM_SUBMIT_ATTEMPTS = 2
_DEFAULT_BATCH_RECONCILE_INTERVAL_SECONDS = 60
_DEFAULT_BATCH_RECONCILE_LIMIT = 100
_ADMISSION_FAILURE_PREFIX = "提交中台编排失败:"
_ADMISSION_PERMANENT_FAILURE_PREFIX = "提交中台编排永久失败:"


class _PermanentPlatformSubmitError(RuntimeError):
    """平台返回明确业务 4xx；后台 reconciler 不应无限重试。"""


def _positive_env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, ""))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _validate_uploaded_novel_payload(
    filename: str,
    content: bytes,
) -> dict:
    """在持久化/排队前做 Gateway 自己可以负责的文本校验。

    翻译模型、分章和内容预检属于 Story Planner，不在 Gateway 导入或判定，
    避免平台入口依赖另一个业务服务的私有实现。
    """
    if len(content) > _MAX_NOVEL_FILE_BYTES:
        raise ValueError("文件大小不能超过 50MB")

    if content.startswith((b"\x00\x00\xfe\xff", b"\xff\xfe\x00\x00")):
        encoding = "utf-32"
    elif content.startswith((b"\xfe\xff", b"\xff\xfe")):
        encoding = "utf-16"
    elif content.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
    else:
        if content and content.count(b"\x00") / len(content) > 0.001:
            raise ValueError("文件包含空字节且没有文本 BOM，疑似二进制文件")
        try:
            content.decode("utf-8", errors="strict")
            encoding = "utf-8"
        except UnicodeDecodeError:
            encoding = "gb18030"

    try:
        text_value = content.decode(encoding, errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError(f"小说文本编码无效: {exc}") from exc
    if not text_value.strip():
        raise ValueError("小说缺少有效正文")

    suspicious_controls = sum(
        1
        for char in text_value
        if (ord(char) < 32 and char not in {"\t", "\n", "\r", "\f"})
        or 0x7F <= ord(char) <= 0x9F
    )
    if text_value and suspicious_controls / len(text_value) > 0.01:
        raise ValueError("文件包含过多控制字符，疑似二进制文件")

    max_chars = _positive_env_int(
        "GATEWAY_MAX_NOVEL_CHARS",
        _DEFAULT_MAX_NOVEL_CHARS,
    )
    char_count = len(text_value)
    if char_count > max_chars:
        raise ValueError(f"小说字符数 {char_count} 超过上限 {max_chars}")

    return {
        "filename": filename,
        "content": content,
        "char_count": char_count,
    }


def _make_batch_task_id(
    batch_id: str,
    position: int,
    filename: str,
    content: bytes,
    config_fingerprint: str,
) -> str:
    """同一幂等批次稳定复用 ID；同名/同秒/同内容的不同位置仍不碰撞。"""
    content_digest = hashlib.sha256(content).hexdigest()
    seed = "\0".join(
        (batch_id, str(position), filename, content_digest, config_fingerprint)
    )
    return uuid.uuid5(uuid.NAMESPACE_URL, seed).hex[:16]


def _lookup_platform_task(task_id: str) -> Optional[dict]:
    """按 task_id 查询平台；404 表示确实尚未创建。"""
    import requests
    from common.config import PLATFORM_API_URL, PLATFORM_SERVICE_TOKEN

    response = requests.get(
        f"{PLATFORM_API_URL}/api/v1/tasks/{task_id}",
        headers={"X-Service-Token": PLATFORM_SERVICE_TOKEN},
        timeout=5,
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def _submit_platform_task_idempotent(
    task_id: str,
    pipeline_name: Optional[str],
) -> dict:
    """提交平台任务，并收敛 POST ACK 丢失/超时/5xx 的不确定窗口。"""
    attempts = min(
        _positive_env_int(
            "GATEWAY_PLATFORM_SUBMIT_ATTEMPTS",
            _DEFAULT_PLATFORM_SUBMIT_ATTEMPTS,
        ),
        3,
    )
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return submit_platform_task(task_id, pipeline_name=pipeline_name)
        except Exception as exc:
            last_error = exc
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)
            response_text = str(getattr(response, "text", "") or "")
            if status_code == 400 and "task already exists" in response_text.lower():
                logger.warning("[platform] 幂等恢复已存在任务: task_id=%s", task_id)
                try:
                    existing = _lookup_platform_task(task_id)
                except Exception as lookup_error:
                    raise RuntimeError(
                        f"平台报告任务已存在，但回查失败: {task_id}"
                    ) from lookup_error
                if existing is None:
                    raise RuntimeError(
                        f"平台报告任务已存在，但按 task_id 回查为 404: {task_id}"
                    )
                return {**existing, "idempotent_replay": True}

            # 明确业务 4xx 不交给常驻 reconciler 无限重打；408/429 仍按暂态处理。
            # 无 response 通常是 connect/read timeout；5xx 也可能发生在平台已
            # commit、响应未送达之后，两者都先按 ID 回查。
            if (
                status_code is not None
                and 400 <= status_code < 500
                and status_code not in {408, 429}
            ):
                raise _PermanentPlatformSubmitError(
                    f"平台拒绝任务提交 HTTP {status_code}: {response_text[:500]}"
                ) from exc
            try:
                existing = _lookup_platform_task(task_id)
            except Exception as lookup_error:
                logger.warning(
                    "[platform] 提交失败后的回查也失败: task_id=%s attempt=%s/%s error=%s",
                    task_id,
                    attempt,
                    attempts,
                    lookup_error,
                )
            else:
                if existing is not None:
                    logger.warning(
                        "[platform] POST ACK 丢失但任务已创建，按成功恢复: task_id=%s",
                        task_id,
                    )
                    return {**existing, "idempotent_replay": True}

            if attempt < attempts:
                logger.warning(
                    "[platform] 任务尚未创建，执行有界重试: task_id=%s attempt=%s/%s",
                    task_id,
                    attempt,
                    attempts,
                )
                continue
            raise

    # range 至少执行一次；此分支仅供类型/防御性完整性。
    raise RuntimeError(f"平台任务提交失败: {task_id}") from last_error


def _batch_submission_candidates(limit: int) -> list[dict]:
    """读取已完整落库、但尚未确认进入中台编排的批量任务。"""
    # 保留完整字面量，便于离线合同/运维检索直接确认 reconciler 的恢复范围。
    retry_prefix = "提交中台编排失败:%"
    with Session(engine) as session:
        rows = session.exec(
            select(NovelTask)
            .where(NovelTask.upload_type == "batch")
            .where(or_(
                NovelTask.overall_status == TaskStatus.PENDING,
                and_(
                    NovelTask.overall_status == TaskStatus.FAILED,
                    NovelTask.error_message.like(retry_prefix),
                ),
            ))
            .order_by(NovelTask.id)
            .limit(max(1, limit))
        ).all()
        return [
            {
                "task_id": row.task_id,
                "pipeline_name": row.pipeline_name,
            }
            for row in rows
        ]


def _conditionally_apply_batch_submission_outcome(
    session: Session,
    task_id: str,
    *,
    platform_result: Optional[dict] = None,
    error: Optional[str] = None,
    permanent_error: bool = False,
) -> bool:
    """CAS-style admission writeback that cannot regress algorithm-owned state."""
    platform_status = str((platform_result or {}).get("status") or "").lower()
    values = {}
    if platform_status in {"failed", "cancelled"}:
        values = {
            "overall_status": TaskStatus.FAILED,
            "error_message": f"中台编排任务已进入终态: {platform_status}",
        }
    elif error:
        prefix = (
            _ADMISSION_PERMANENT_FAILURE_PREFIX
            if permanent_error
            else _ADMISSION_FAILURE_PREFIX
        )
        values = {
            "overall_status": TaskStatus.FAILED,
            "error_message": f"{prefix} {error}"[:2000],
        }
    else:
        values = {
            "overall_status": TaskStatus.PROCESSING,
            "progress_percentage": func.greatest(
                func.coalesce(NovelTask.progress_percentage, 0),
                5,
            ),
            "error_message": None,
        }

    # Worker 可以在平台 POST 返回与 Gateway 回写之间迅速把任务推进为
    # PROCESSING/COMPLETED/业务 FAILED。只有尚未 admission 的 PENDING，或由
    # admission 自己留下的可恢复失败，允许被本次结果更新。
    result = session.exec(
        sa_update(NovelTask)
        .where(NovelTask.task_id == task_id)
        .where(NovelTask.upload_type == "batch")
        .where(or_(
            NovelTask.overall_status == TaskStatus.PENDING,
            and_(
                NovelTask.overall_status == TaskStatus.FAILED,
                NovelTask.error_message.like(f"{_ADMISSION_FAILURE_PREFIX}%"),
            ),
        ))
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    return bool(result.rowcount)


def _persist_batch_submission_outcome(
    task_id: str,
    *,
    platform_result: Optional[dict] = None,
    error: Optional[str] = None,
    permanent_error: bool = False,
) -> None:
    """幂等写回 admission 结果，不覆盖已由算法 Worker 推进的终态。"""
    with Session(engine) as session:
        _conditionally_apply_batch_submission_outcome(
            session,
            task_id,
            platform_result=platform_result,
            error=error,
            permanent_error=permanent_error,
        )
        session.commit()


async def _reconcile_batch_submissions_once() -> tuple[int, int]:
    """补齐 Gateway 在 DB commit 与平台 POST 之间退出留下的 admission 空窗。"""
    limit = _positive_env_int(
        "GATEWAY_BATCH_RECONCILE_LIMIT",
        _DEFAULT_BATCH_RECONCILE_LIMIT,
    )
    candidates = await run_in_threadpool(_batch_submission_candidates, limit)
    if not candidates:
        return 0, 0

    concurrency = _positive_env_int(
        "GATEWAY_BATCH_SUBMIT_CONCURRENCY",
        _DEFAULT_BATCH_SUBMIT_CONCURRENCY,
    )
    semaphore = asyncio.Semaphore(concurrency)

    async def submit(candidate: dict) -> bool:
        async with semaphore:
            try:
                result = await run_in_threadpool(
                    _submit_platform_task_idempotent,
                    candidate["task_id"],
                    candidate.get("pipeline_name"),
                )
            except _PermanentPlatformSubmitError as exc:
                logger.error(
                    "[batch-reconciler] admission 永久失败 task_id=%s: %s",
                    candidate["task_id"],
                    exc,
                )
                await run_in_threadpool(
                    _persist_batch_submission_outcome,
                    candidate["task_id"],
                    error=str(exc),
                    permanent_error=True,
                )
                return False
            except Exception as exc:
                logger.warning(
                    "[batch-reconciler] admission 仍失败 task_id=%s: %s",
                    candidate["task_id"],
                    exc,
                )
                await run_in_threadpool(
                    _persist_batch_submission_outcome,
                    candidate["task_id"],
                    error=str(exc),
                )
                return False
            await run_in_threadpool(
                _persist_batch_submission_outcome,
                candidate["task_id"],
                platform_result=result,
            )
            return True

    outcomes = await asyncio.gather(*(submit(item) for item in candidates))
    succeeded = sum(outcomes)
    return succeeded, len(outcomes) - succeeded


async def _batch_submission_reconciler_loop() -> None:
    interval = _positive_env_int(
        "GATEWAY_BATCH_RECONCILE_INTERVAL_SECONDS",
        _DEFAULT_BATCH_RECONCILE_INTERVAL_SECONDS,
    )
    while True:
        try:
            succeeded, failed = await _reconcile_batch_submissions_once()
            if succeeded or failed:
                logger.info(
                    "[batch-reconciler] admission recovered=%s pending_failures=%s",
                    succeeded,
                    failed,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[batch-reconciler] 本轮扫描失败")
        await asyncio.sleep(interval)


@app.on_event("startup")
async def _start_batch_submission_reconciler():
    # 先完成本模块前面的 schema startup handlers，再启动常驻补偿循环。
    app.state.batch_submission_reconciler = asyncio.create_task(
        _batch_submission_reconciler_loop(),
        name="batch-submission-reconciler",
    )


@app.on_event("shutdown")
async def _stop_batch_submission_reconciler():
    task = getattr(app.state, "batch_submission_reconciler", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ==================== API路由 ====================

@app.get("/")
async def root():
    """根路径"""
    return {
        "service": "Novel Game Factory API Gateway",
        "version": "1.0.0",
        "status": "running"
    }


@app.get("/api/v1/models/options")
async def get_model_options():
    """
    返回可选模型清单，供前端渲染文本/生图/音频模型下拉框。
    default 为各类型默认值，options 为全部可选值。
    """
    return {
        "text_model": {"default": TextModel.DEFAULT, "options": TextModel.VALID_MODELS},
        "image_model": {"default": ImageModel.DEFAULT, "options": ImageModel.VALID_MODELS},
        "audio_model": {"default": AudioModel.DEFAULT, "options": AudioModel.VALID_MODELS},
        "resolution": {"default": Resolution.DEFAULT, "options": Resolution.VALID_RESOLUTIONS},
    }


@app.get("/api/v1/models/image-options", response_model=ImageModelOptionsResponse)
async def get_image_model_options():
    """返回重新生图可选择的全部图像模型。"""
    return ImageModelOptionsResponse(
        default=ImageModel.DEFAULT,
        options=list(ImageModel.VALID_MODELS),
    )


@app.post("/api/v1/novels/batch", response_model=BatchProcessResponse)
async def batch_process_novels(
    request: Request,
    novels: List[UploadFile] = File(..., description="小说文件列表（最多50本）"),
    output_language: str = Form(default="zh-CN", description="输出语言（zh-CN, en-US, ja-JP等）"),
    art_style: str = Form(default=ArtStyle.INK_CHINESE, description="画面风格：ink_chinese/cyberpunk/japanese/dark_european/modern_realism"),
    story_mode: str = Form(default=StoryMode.ORIGINAL, description="剧情模式：original=原文适应（默认），multi=多剧情改写"),
    gameplay: str = Form(default="interactive_story", description="游戏玩法：interactive_story=互动故事（默认），free_growth=自由成长"),
    text_model: str = Form(default=TextModel.DEFAULT, description="文本模型，见 GET /api/v1/models/options"),
    image_model: str = Form(default=ImageModel.DEFAULT, description="生图模型，见 GET /api/v1/models/options"),
    audio_model: str = Form(default=AudioModel.DEFAULT, description="音频模型，见 GET /api/v1/models/options"),
    resolution: str = Form(default=Resolution.DEFAULT, description="输出分辨率：1080p / 2k / 4k"),
    user_id: Optional[str] = Form(default=None, description="用户ID"),
    user_name: Optional[str] = Form(default=None, description="用户名"),
    mode: Optional[str] = Form(default=None, description="创作模式：efficiency/balanced/quality，由中台配置映射到管线"),
    pipeline_name: Optional[str] = Form(default=None, description="直接指定管线名，优先于 mode；两者都不传则各步自动选实现"),
    source_task_id: Optional[str] = Form(default=None, description="创建相似作品时的源任务 ID；本批成功启动的每个新任务都会记录到该源作品"),
    batch_id: Optional[str] = Form(default=None, description="批次幂等键；重试同一批时复用，亦可使用 Idempotency-Key 请求头"),
):
    """
    批量处理小说（最多50本）

    Args:
        novels: 小说文件列表（.txt格式）
        output_language: 输出语言代码
        art_style: 画面风格
        story_mode: 剧情模式，original=原文适应（默认），multi=多剧情改写
        gameplay: 游戏玩法，interactive_story=互动故事（默认），free_growth=自由成长
        source_task_id: 创建相似作品时的共同源任务 ID

    Returns:
        任务ID列表和状态
    """
    logger.info(
        f"[batch] 收到批量处理请求: 文件数={len(novels)} output_language={output_language} "
        f"art_style={art_style} story_mode={story_mode} gameplay={gameplay} "
        f"text_model={text_model} image_model={image_model} audio_model={audio_model} "
        f"resolution={resolution} user_id={user_id} user_name={user_name} "
        f"source_task_id={source_task_id}"
    )
    # 验证 story_mode
    if story_mode not in StoryMode.VALID_MODES:
        raise HTTPException(status_code=400, detail=f"story_mode 只支持: {', '.join(StoryMode.VALID_MODES)}")

    # 验证 art_style
    if art_style not in ArtStyle.VALID_STYLES:
        raise HTTPException(status_code=400, detail=f"art_style 只支持: {', '.join(ArtStyle.VALID_STYLES)}")

    # 验证 gameplay
    valid_gameplay = ["interactive_story", "free_growth"]
    if gameplay not in valid_gameplay:
        raise HTTPException(status_code=400, detail=f"gameplay 只支持: {', '.join(valid_gameplay)}")

    # 验证模型
    if text_model not in TextModel.VALID_MODELS:
        raise HTTPException(status_code=400, detail=f"text_model 只支持: {', '.join(TextModel.VALID_MODELS)}")
    if image_model not in ImageModel.VALID_MODELS:
        raise HTTPException(status_code=400, detail=f"image_model 只支持: {', '.join(ImageModel.VALID_MODELS)}")
    if audio_model not in AudioModel.VALID_MODELS:
        raise HTTPException(status_code=400, detail=f"audio_model 只支持: {', '.join(AudioModel.VALID_MODELS)}")
    if resolution not in Resolution.VALID_RESOLUTIONS:
        raise HTTPException(status_code=400, detail=f"resolution 只支持: {', '.join(Resolution.VALID_RESOLUTIONS)}")

    supported_languages = ["zh-CN", "en-US"]
    if output_language not in supported_languages:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的语言代码: {output_language}。支持的语言: {', '.join(supported_languages)}",
        )

    # 验证文件数量
    if len(novels) > 50:
        raise HTTPException(status_code=400, detail="单次最多处理50本小说")

    if len(novels) == 0:
        raise HTTPException(status_code=400, detail="至少需要上传1本小说")

    # 必须先把整批文件读完并验证，再做任何本地/OSS/DB 写入或平台提交。
    # 这样第 N 本格式、编码或字符数不合法时，前 N-1 本不会已经进入队列。
    validated_novels = []
    total_file_bytes = 0
    total_chars = 0
    for position, novel_file in enumerate(novels):
        filename = novel_file.filename or ""
        if not filename.lower().endswith(".txt"):
            raise HTTPException(
                status_code=400,
                detail=f"文件格式错误: {filename or '<空文件名>'}。仅支持.txt格式",
            )
        content = await novel_file.read()
        try:
            validated = _validate_uploaded_novel_payload(
                filename,
                content,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"文件校验失败: {filename}: {exc}",
            ) from exc
        validated["position"] = position
        validated["project_name"] = Path(filename).stem
        validated_novels.append(validated)
        total_file_bytes += len(content)
        total_chars += validated["char_count"]

    max_batch_file_bytes = _positive_env_int(
        "GATEWAY_MAX_BATCH_FILE_BYTES",
        _DEFAULT_MAX_BATCH_FILE_BYTES,
    )
    if total_file_bytes > max_batch_file_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"批量文件总大小 {total_file_bytes} 超过上限 {max_batch_file_bytes} bytes",
        )
    max_batch_total_chars = _positive_env_int(
        "GATEWAY_MAX_BATCH_TOTAL_CHARS",
        _DEFAULT_MAX_BATCH_TOTAL_CHARS,
    )
    if total_chars > max_batch_total_chars:
        raise HTTPException(
            status_code=400,
            detail=f"批量小说总字符数 {total_chars} 超过上限 {max_batch_total_chars}",
        )

    requested_batch_id = (
        request.headers.get("Idempotency-Key") or batch_id or ""
    ).strip()
    effective_batch_id = requested_batch_id or uuid.uuid4().hex
    if len(effective_batch_id) > 128:
        raise HTTPException(status_code=400, detail="batch_id/Idempotency-Key 最长128字符")

    # 定管线：整批共用一条，解析一次即可（模式→管线的映射在中台，随时可改，所以现查）
    try:
        resolved_pipeline = resolve_pipeline_name(mode, pipeline_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("[batch] 查询创作模式映射失败")
        raise HTTPException(status_code=502, detail=f"无法从中台获取创作模式配置: {e}")

    normalized_source_task_id = (source_task_id or "").strip() or None
    if normalized_source_task_id:
        with Session(engine) as source_session:
            source_task = source_session.exec(
                select(NovelTask).where(NovelTask.task_id == normalized_source_task_id)
            ).first()
            if not source_task:
                raise HTTPException(
                    status_code=404,
                    detail=f"源任务不存在: {normalized_source_task_id}",
                )

    config_fingerprint = json.dumps(
        {
            "output_language": output_language,
            "art_style": art_style,
            "story_mode": story_mode,
            "gameplay": gameplay,
            "text_model": text_model,
            "image_model": image_model,
            "audio_model": audio_model,
            "resolution": resolution,
            "pipeline_name": resolved_pipeline,
            "user_id": user_id,
            "source_task_id": normalized_source_task_id,
        },
        ensure_ascii=False,
        sort_keys=True,
    )

    specs = []
    for item in validated_novels:
        task_id = _make_batch_task_id(
            effective_batch_id,
            item["position"],
            item["filename"],
            item["content"],
            config_fingerprint,
        )
        specs.append({**item, "task_id": task_id, "is_new": True})

    storage_dir = Path(RAW_NOVELS_PATH)
    storage_dir.mkdir(parents=True, exist_ok=True)

    # 幂等重试时，已存在且已开始的任务直接复用；缺失项才继续持久化。
    with Session(engine) as session:
        for spec in specs:
            existing_task = session.exec(
                select(NovelTask).where(NovelTask.task_id == spec["task_id"])
            ).first()
            if existing_task:
                spec["is_new"] = False
                spec["existing_status"] = existing_task.overall_status
                spec["existing_error"] = existing_task.error_message

    new_specs = [spec for spec in specs if spec["is_new"]]
    persist_limit = _positive_env_int(
        "GATEWAY_BATCH_PERSIST_CONCURRENCY",
        _DEFAULT_BATCH_PERSIST_CONCURRENCY,
    )
    persist_semaphore = asyncio.Semaphore(persist_limit)

    async def _persist_raw_novel(spec: dict) -> tuple[str, Optional[str]]:
        novel_path = storage_dir / f"{spec['task_id']}.txt"
        async with persist_semaphore:
            try:
                # Gateway 进程退出或 PVC 写入失败时，不能留下一个顶着最终文件名的
                # 截断小说；下次相同 Idempotency-Key 请求会安全地重新发布完整正文。
                await run_in_threadpool(
                    atomic_write_bytes,
                    novel_path,
                    spec["content"],
                )
                raw_novel_key = f"storage/raw_novels/{spec['task_id']}.txt"
                stored = await run_in_threadpool(
                    oss_write,
                    raw_novel_key,
                    spec["content"],
                    "text/plain; charset=utf-8",
                )
                if not stored:
                    return spec["task_id"], "本地文件持久化返回失败"
                return spec["task_id"], None
            except Exception as exc:
                return spec["task_id"], str(exc)

    raw_results = await asyncio.gather(
        *(_persist_raw_novel(spec) for spec in new_specs)
    )
    raw_failures = [
        f"{task_id}: {error}" for task_id, error in raw_results if error
    ]
    if raw_failures:
        # 已成功的原子 raw 可能成为不可见孤儿；不做删除回滚，避免并发幂等
        # 请求间误删。相同 batch_id 重试会使用同一 task_id 并安全覆盖。
        logger.error("[batch] raw 持久化部分失败: %s", raw_failures)
        raise HTTPException(
            status_code=502,
            detail="批量小说持久化失败，尚未创建或提交任何任务，"
            "可使用相同 batch_id 安全重试: " + "; ".join(raw_failures),
        )

    if new_specs:
        with Session(engine) as session:
            for spec in new_specs:
                novel_task = NovelTask(
                    task_id=spec["task_id"],
                    novel_filename=spec["filename"],
                    project_name=spec["project_name"],
                    output_language=output_language,
                    art_style=art_style,
                    story_mode=story_mode,
                    gameplay=gameplay,
                    text_model=text_model,
                    image_model=image_model,
                    audio_model=audio_model,
                    resolution=resolution,
                    upload_type="batch",
                    user_id=user_id,
                    user_name=user_name,
                    overall_status=TaskStatus.PENDING,
                    progress_percentage=0,
                )
                novel_task.pipeline_name = resolved_pipeline
                session.add(novel_task)
            try:
                session.commit()
            except IntegrityError:
                # 两个相同 Idempotency-Key 请求并发到达时，唯一键只允许一个创建；
                # 另一个回查整批并继续走幂等提交即可。
                session.rollback()
                existing_ids = set(
                    session.exec(
                        select(NovelTask.task_id).where(
                            NovelTask.task_id.in_([
                                spec["task_id"] for spec in new_specs
                            ])
                        )
                    ).all()
                )
                missing_ids = [
                    spec["task_id"]
                    for spec in new_specs
                    if spec["task_id"] not in existing_ids
                ]
                if missing_ids:
                    raise HTTPException(
                        status_code=500,
                        detail=f"批次任务落库冲突且回查缺失: {missing_ids}",
                    )
                for spec in new_specs:
                    spec["is_new"] = False

    # 平台调用只等待“任务已入编排队列”的短 ACK，不等待任何算法任务执行。
    # 最多并发 10 个提交，30 本是三批短请求，不会把 Uvicorn event loop 串行堵住。
    submit_limit = _positive_env_int(
        "GATEWAY_BATCH_SUBMIT_CONCURRENCY",
        _DEFAULT_BATCH_SUBMIT_CONCURRENCY,
    )
    semaphore = asyncio.Semaphore(submit_limit)

    async def _submit_one(spec: dict) -> tuple[str, dict]:
        existing_status = spec.get("existing_status")
        existing_error = str(spec.get("existing_error") or "")
        if existing_status in (TaskStatus.PROCESSING, TaskStatus.COMPLETED):
            return spec["task_id"], {"error": None, "platform_result": None}
        if (
            existing_status == TaskStatus.FAILED
            and not existing_error.startswith("提交中台编排失败")
        ):
            spec["preserve_existing_failure"] = True
            return spec["task_id"], {
                "error": existing_error or "任务此前已失败",
                "platform_result": None,
            }
        async with semaphore:
            try:
                platform_result = await run_in_threadpool(
                    _submit_platform_task_idempotent,
                    spec["task_id"],
                    resolved_pipeline,
                )
                platform_status = str(
                    (platform_result or {}).get("status") or ""
                ).lower()
                if platform_status in {"failed", "cancelled"}:
                    return spec["task_id"], {
                        "error": f"中台编排任务已进入终态: {platform_status}",
                        "platform_result": platform_result,
                    }
                return spec["task_id"], {
                    "error": None,
                    "platform_result": platform_result,
                }
            except _PermanentPlatformSubmitError as exc:
                logger.error(
                    "[batch] 提交中台编排永久失败 task_id=%s: %s",
                    spec["task_id"],
                    exc,
                )
                return spec["task_id"], {
                    "error": str(exc),
                    "platform_result": None,
                    "permanent_error": True,
                }
            except Exception as exc:
                logger.exception(
                    "[batch] 提交中台编排失败 task_id=%s",
                    spec["task_id"],
                )
                return spec["task_id"], {
                    "error": str(exc),
                    "platform_result": None,
                }

    submit_results = dict(await asyncio.gather(*(_submit_one(spec) for spec in specs)))

    missing_task_ids = set()
    with Session(engine) as session:
        for spec in specs:
            task_exists = session.exec(
                select(NovelTask.task_id).where(NovelTask.task_id == spec["task_id"])
            ).first()
            if not task_exists:
                missing_task_ids.add(spec["task_id"])
                continue
            outcome = submit_results.get(spec["task_id"]) or {}
            error = outcome.get("error")
            if not spec.get("preserve_existing_failure"):
                _conditionally_apply_batch_submission_outcome(
                    session,
                    spec["task_id"],
                    platform_result=outcome.get("platform_result"),
                    error=str(error) if error else None,
                    permanent_error=bool(outcome.get("permanent_error")),
                )
            if not error and normalized_source_task_id:
                append_remake_task_id(
                    session,
                    normalized_source_task_id,
                    spec["task_id"],
                )
        session.commit()

    # 以条件更新后的真实状态生成响应。若 Worker 已经抢先写入业务 FAILED，
    # 这里必须报告那个失败，不能把“平台 POST 成功”包装成 queued 假成功。
    with Session(engine) as session:
        rows = session.exec(
            select(NovelTask).where(
                NovelTask.task_id.in_([spec["task_id"] for spec in specs])
            )
        ).all()
        tasks_by_id = {task.task_id: task for task in rows}

    for spec in specs:
        task = tasks_by_id.get(spec["task_id"])
        if spec["task_id"] in missing_task_ids or task is None:
            spec["result_status"] = "failed"
            spec["result_error"] = "任务记录不存在"
        elif task.overall_status == TaskStatus.FAILED:
            spec["result_status"] = "failed"
            spec["result_error"] = task.error_message or "任务已失败"
        else:
            spec["result_status"] = "queued"
            spec["result_error"] = None

    # 起名是独立的非关键队列任务，只对本次新建且已成功入队的作品投递一次。
    for spec in specs:
        if not spec["is_new"] or spec.get("result_status") != "queued":
            continue
        try:
            celery_app.signature(
                'services.story_planner.tasks.generate_project_name_only',
                args=[spec["task_id"]],
                queue='story_queue',
            ).apply_async()
        except Exception as exc:
            logger.warning(
                "[batch] Celery 起名任务触发失败 task_id=%s: %s",
                spec["task_id"],
                exc,
            )

    task_infos = [
        TaskInfo(
            task_id=spec["task_id"],
            project_name=spec["project_name"],
            novel_filename=spec["filename"],
            output_language=output_language,
            art_style=art_style,
            story_mode=story_mode,
            status=spec.get("result_status", "failed"),
            error=spec.get("result_error"),
        )
        for spec in specs
    ]
    queued_count = sum(info.status == "queued" for info in task_infos)
    failed_count = len(task_infos) - queued_count
    logger.info(
        "[batch] 批量处理完成: batch_id=%s queued=%s failed=%s",
        effective_batch_id,
        queued_count,
        failed_count,
    )
    return BatchProcessResponse(
        success=failed_count == 0,
        total=len(task_infos),
        tasks=task_infos,
        message=f"已入队{queued_count}个任务，失败{failed_count}个",
        batch_id=effective_batch_id,
    )


@app.get("/api/v1/tasks/{task_id}/status", response_model=TaskStatusResponse)
async def get_task_status(task_id: str):
    """
    查询任务状态

    Args:
        task_id: 任务ID

    Returns:
        任务详细状态
    """
    with Session(engine) as session:
        task = session.exec(
            select(NovelTask).where(
                NovelTask.task_id == task_id,
                NovelTask.is_deleted == False,
                NovelTask.is_purged == False,
            )
        ).first()

        if not task:
            raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")

        editors = get_task_editors([task_id]).get(task_id, [])
        remake_task_ids = normalize_remake_task_ids(task.remake_task_ids)
        return TaskStatusResponse(
            task_id=task.task_id,
            project_name=task.project_name,
            novel_filename=task.novel_filename,
            output_language=task.output_language,
            art_style=task.art_style,
            story_mode=task.story_mode,
            gameplay=task.gameplay,
            upload_type=task.upload_type,
            overall_status=TaskStatus.get_description(task.overall_status),
            progress_percentage=task.progress_percentage,
            stages={
                "story_planner": TaskStatus.get_description(task.story_planner_status),
                "info_extractor": TaskStatus.get_description(task.info_extractor_status),
                "sound_arranger": (
                    "已跳过（本地美术实验）"
                    if local_pipeline_enabled()
                    else TaskStatus.get_description(task.sound_arranger_status)
                ),
                "char_designer": TaskStatus.get_description(task.char_designer_status),
                "audio_producer": (
                    "已跳过（本地美术实验）"
                    if local_pipeline_enabled()
                    else TaskStatus.get_description(task.audio_producer_status)
                ),
                "scene_painter": TaskStatus.get_description(task.scene_painter_status),
                "game_builder": TaskStatus.get_description(task.game_builder_status),
            },
            error_message=task.error_message,
            create_time=task.create_time,
            update_time=task.update_time,
            completed_at=task.completed_at,
            info_confirm_stage=task.info_confirm_stage,
            editors=editors,
            remake_count=len(remake_task_ids),
            remake_task_ids=remake_task_ids,
        )


# info_confirm_stage 值 → 前端跳转阶段(EntryStage)。
# 语义:info_confirm_stage 记录“用户刚完成的那一步”，entry 跳到“下一个该去的地方”。
_STAGE_TO_ENTRY = {
    InfoConfirmStage.UPLOAD: EntryStage.CONFIG,
    InfoConfirmStage.CONFIG: EntryStage.CONFIRM,
    "adaptation": EntryStage.CONFIRM,  # 兼容历史数据；当前流程不再返回 adaptation
    InfoConfirmStage.CONFIRMED: EntryStage.MATERIAL,      # 生成已提交 → 落②角色场景
    InfoConfirmStage.MATERIAL: EntryStage.PERFORMANCE,    # ②已确认完成 → 直接去③，不回跳②
    # performance 表示③已完成；前端结合 info_confirm_stage 展示④游戏界面。
    InfoConfirmStage.PERFORMANCE: EntryStage.PUBLISH,
    InfoConfirmStage.PUBLISH: EntryStage.PUBLISH,
}


def _resolve_entry_stage(task: NovelTask) -> str:
    """根据已完成节点返回下一个精确页面。

    输出约束完成后直接进入信息确认页。该页会继续轮询摘要，
    所以摘要尚在生成时也能正确恢复。
    """
    if task.info_confirm_stage in _STAGE_TO_ENTRY:
        return _STAGE_TO_ENTRY[task.info_confirm_stage]
    return EntryStage.CREATE if task.overall_status == TaskStatus.PENDING else EntryStage.MATERIAL


@app.get("/api/v1/tasks/{task_id}/entry", response_model=TaskEntryResponse)
async def get_task_entry(task_id: str):
    """作品「进入接口」：根据单字段 info_confirm_stage 算出应跳转的阶段，前端据此跳回上次离开处。

    stage 取值：create(①上传) / config(②输出约束)
             / confirm(②信息确认/摘要生成中)
             / material(③人物场景) / performance(④演绎微调) / publish(⑤发布)

    语义:info_confirm_stage 记录“用户刚完成的那一步”，entry 跳到“下一个该去的地方”。
    判定：
      1. 已发布(is_published 列为真) → publish（CMS 发布成功后置位，覆盖一切）
      2. 否则按 info_confirm_stage 映射(见 _STAGE_TO_ENTRY)：
         upload→config，config→confirm，confirmed→material，
         material→performance，performance→publish，publish→publish。
      3. 批量流程 info_confirm_stage 为空：待处理→create，已进任务链/完成→material。
    失败(overall_status=3)不改变 stage 归属，仅透传 error_message。
    """
    with Session(engine) as session:
        task = session.exec(
            select(NovelTask).where(
                NovelTask.task_id == task_id,
                NovelTask.is_deleted == False,
                NovelTask.is_purged == False,
            )
        ).first()
        if not task:
            raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")

        is_published = _task_is_published(session, task_id)

    if is_published:
        stage = EntryStage.PUBLISH
    else:
        # info_confirm_stage 为空时，保留批量流程的 overall_status 兜底。
        stage = _resolve_entry_stage(task)

    return TaskEntryResponse(
        task_id=task.task_id,
        stage=stage,
        overall_status_code=task.overall_status,
        progress_percentage=task.progress_percentage,
        info_confirm_stage=task.info_confirm_stage,
        is_published=is_published,
        error_message=task.error_message,
    )


@app.get("/api/v1/tasks/{task_id}/generation-params", response_model=GenerationParamsResponse)
async def get_generation_params(task_id: str):
    """
    查询某任务的生成参数：3 个模型（文本/生图/音频）+ 分辨率 + 视觉风格。

    Args:
        task_id: 任务 ID

    Returns:
        GenerationParamsResponse: 该任务选定的模型/分辨率/画面风格
    """
    with Session(engine) as session:
        task = session.exec(
            select(NovelTask).where(NovelTask.task_id == task_id)
        ).first()
        if not task:
            raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")

        return GenerationParamsResponse(
            task_id=task.task_id,
            text_model=task.text_model,
            image_model=task.image_model,
            audio_model=task.audio_model,
            resolution=task.resolution,
            art_style=task.art_style,
        )


def _successful_publish_exists():
    """返回“作品已发布”的过滤条件（读 novel_tasks.is_published 列）。

    发布状态以本地 is_published 列为准（CMS 发布成功后由中台置位），
    不再查询外部 publish_record 表。
    """
    return NovelTask.is_published == True  # noqa: E712


def _task_is_published(session: Session, task_id: str) -> bool:
    """该作品是否已发布(读 novel_tasks.is_published 列)。"""
    return bool(session.scalar(
        select(NovelTask.is_published).where(NovelTask.task_id == task_id)
    ))


def _ensure_task_mutable(session: Session, task_id: str) -> NovelTask:
    """返回可编辑任务；已发布作品的所有创作写接口都必须在服务端拒绝。

    前端 disabled 只负责交互提示，不能作为数据一致性的边界。统一在这里检查，
    避免遗漏某个重生成、版本激活或演绎热更新接口后被直接构造请求绕过。
    """
    task = session.exec(
        select(NovelTask).where(NovelTask.task_id == task_id)
    ).first()
    if not task:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    if bool(task.is_published):
        raise HTTPException(status_code=400, detail="作品已发布，不可修改")
    return task


def _advance_stage(task: NovelTask, target_stage: str) -> str:
    """把 info_confirm_stage 沿 ORDER 链单向推进到 target_stage(只前进不后退),返回推进后的值。
    调用方负责 session.add/commit。target_stage 必须已校验合法。"""
    order = InfoConfirmStage.ORDER
    current_idx = order.index(task.info_confirm_stage) if task.info_confirm_stage in order else -1
    target_idx = order.index(target_stage)
    if target_idx > current_idx:
        task.info_confirm_stage = target_stage
    return task.info_confirm_stage


def _task_generation_started(task: NovelTask) -> bool:
    """任务是否已经成功进入过完整生成流程。

    celery_chain_id 只对本地 Celery chain 时代的存量任务有意义（现在提交走中台编排，
    这一列不再写），新任务靠 info_confirm_stage / overall_status 判定：
    提交成功才会写 CONFIRMED + PROCESSING，提交失败只落 error_message，因此可重试。
    """
    started_stages = {
        InfoConfirmStage.CONFIRMED,
        InfoConfirmStage.MATERIAL,
        InfoConfirmStage.PERFORMANCE,
        InfoConfirmStage.PUBLISH,
    }
    return bool(
        task.celery_chain_id
        or task.info_confirm_stage in started_stages
        or task.overall_status in (TaskStatus.PROCESSING, TaskStatus.COMPLETED)
    )


def _style_anchor_kind(asset_kind: str) -> str:
    kind = str(asset_kind or "").strip().lower()
    if kind not in VALID_ASSET_KINDS:
        raise HTTPException(status_code=400, detail="asset_kind 只支持 character / scene")
    return kind


def _locked_visual_style_task(session: Session, task_id: str) -> NovelTask:
    """Lock one task and reject all style mutations after generation starts."""
    task = session.exec(
        select(NovelTask)
        .where(NovelTask.task_id == task_id)
        .with_for_update()
    ).first()
    if not task:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    if bool(task.is_published):
        raise HTTPException(status_code=409, detail="作品已发布，不可修改美术风格")
    if _task_generation_started(task):
        raise HTTPException(status_code=409, detail="任务已提交生成，不可再修改美术风格")
    return task


def _style_anchor_preview_url(task_id: str, kind: str, sha256: str = "") -> str:
    suffix = f"?v={sha256[:12]}" if sha256 else ""
    return f"/api/v1/tasks/{task_id}/style-anchors/{kind}/image{suffix}"


def _public_visual_style_config(
    task: NovelTask,
    config: dict,
    *,
    fetch_remote: bool = True,
) -> dict:
    public = {
        "task_id": task.task_id,
        "mode": config.get("mode", STYLE_MODE_LLM),
        "character_global_style": str(
            (config.get(ASSET_CHARACTER) or {}).get("global_style") or ""
        ),
        "scene_global_style": str(
            (config.get(ASSET_SCENE) or {}).get("global_style") or ""
        ),
        "remove_character_background": bool(
            config.get(REMOVE_CHARACTER_BACKGROUND_KEY, True)
        ),
        "locked": bool(task.is_published or _task_generation_started(task)),
    }

    for kind in (ASSET_CHARACTER, ASSET_SCENE):
        section = dict(config.get(kind) or {})
        target = style_anchor_path(task.task_id, kind)
        if fetch_remote and (not target.exists() or target.stat().st_size <= 0):
            fetched = fetch_output_file(
                task.task_id,
                section.get("anchor_file") or ANCHOR_RELATIVE_PATHS[kind],
            )
            if fetched is not None:
                target = fetched
        exists = target.exists() and target.is_file() and target.stat().st_size > 0
        section["exists"] = exists
        section["preview_url"] = (
            _style_anchor_preview_url(task.task_id, kind, section.get("sha256", ""))
            if exists
            else None
        )
        if section.get("confirmed") and not exists:
            section["status"] = ANCHOR_STATUS_MISSING
        public[kind] = section

    try:
        validate_visual_style_for_run(
            task.task_id,
            fetch_remote=fetch_remote,
        )
        public["ready_to_run"] = True
        public["validation_error"] = None
    except VisualStyleConfigError as exc:
        public["ready_to_run"] = False
        public["validation_error"] = str(exc)
    return public


# ==================== 任务级自定义美术风格 / Anchor ====================


@app.get("/api/v1/tasks/{task_id}/visual-style")
async def get_visual_style(task_id: str):
    with Session(engine) as session:
        task = session.exec(
            select(NovelTask).where(NovelTask.task_id == task_id)
        ).first()
        if not task:
            raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
        try:
            config = load_visual_style_config(task_id, fetch_remote=True)
            return _public_visual_style_config(task, config, fetch_remote=True)
        except VisualStyleConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/v1/tasks/{task_id}/visual-style")
async def update_visual_style(task_id: str, req: VisualStyleUpdateRequest):
    mode = str(req.mode or "").strip().lower()
    if mode not in VALID_STYLE_MODES:
        raise HTTPException(status_code=400, detail="mode 只支持 llm / custom")

    with Session(engine) as session:
        task = _locked_visual_style_task(session, task_id)
        try:
            config = load_visual_style_config(task_id, fetch_remote=True)
            config["mode"] = mode
            config[REMOVE_CHARACTER_BACKGROUND_KEY] = bool(
                req.remove_character_background
            )
            incoming_styles = {
                ASSET_CHARACTER: req.character_global_style.strip(),
                ASSET_SCENE: req.scene_global_style.strip(),
            }
            for kind, style in incoming_styles.items():
                section = config[kind]
                old_style = str(section.get("global_style") or "").strip()
                if style != old_style:
                    section["global_style"] = style
                    section["confirmed"] = False
                    section["job_id"] = ""
                    section["error"] = ""
                    section["updated_at"] = visual_style_timestamp()
                    if section.get("source") == "generated" and section.get("sha256"):
                        section["stale"] = True
                        section["status"] = ANCHOR_STATUS_STALE
                    else:
                        section["stale"] = False
                        section["status"] = (
                            ANCHOR_STATUS_READY
                            if section.get("source") and section.get("sha256")
                            else ANCHOR_STATUS_MISSING
                        )

                if mode == STYLE_MODE_LLM and section.get("status") == ANCHOR_STATUS_GENERATING:
                    section["job_id"] = ""
                    section["error"] = ""
                    section["status"] = (
                        ANCHOR_STATUS_READY
                        if section.get("source") and section.get("sha256")
                        else ANCHOR_STATUS_MISSING
                    )

            save_visual_style_config(task_id, config, mirror=False)
            session.commit()
            return _public_visual_style_config(task, config, fetch_remote=False)
        except VisualStyleConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/api/v1/tasks/{task_id}/style-anchors/{asset_kind}/generate",
    response_model=StyleAnchorJobResponse,
)
async def generate_style_anchor(
    task_id: str,
    asset_kind: str,
    req: Optional[StyleAnchorGenerateRequest] = None,
):
    kind = _style_anchor_kind(asset_kind)
    job_id = uuid.uuid4().hex

    with Session(engine) as session:
        task = _locked_visual_style_task(session, task_id)
        try:
            config = load_visual_style_config(task_id, fetch_remote=True)
        except VisualStyleConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if config.get("mode") != STYLE_MODE_CUSTOM:
            raise HTTPException(status_code=409, detail="请先切换到自定义风格模式")

        section = config[kind]
        global_style = str(section.get("global_style") or "").strip()
        if not global_style:
            raise HTTPException(status_code=400, detail="生成 Anchor 前请填写对应的 global style")

        requested_model = req.image_model if req is not None else None
        image_model = str(requested_model or task.image_model or ImageModel.DEFAULT).strip()
        if image_model not in ImageModel.VALID_MODELS:
            raise HTTPException(
                status_code=400,
                detail=f"image_model 只支持: {', '.join(ImageModel.VALID_MODELS)}",
            )

        section.update(
            confirmed=False,
            stale=False,
            status=ANCHOR_STATUS_GENERATING,
            job_id=job_id,
            error="",
            updated_at=visual_style_timestamp(),
        )
        save_visual_style_config(task_id, config, mirror=False)

        try:
            celery_app.signature(
                "services.scene_painter.tasks.generate_style_anchor",
                args=[task_id, kind, global_style, image_model],
                queue="scene_queue",
            ).apply_async(task_id=job_id)
        except Exception as exc:
            section.update(
                status=ANCHOR_STATUS_FAILED,
                error=f"Anchor 生成任务提交失败: {exc}",
                updated_at=visual_style_timestamp(),
            )
            save_visual_style_config(task_id, config, mirror=False)
            session.commit()
            logger.exception(
                "[style-anchor] Celery 提交失败: task_id=%s kind=%s job_id=%s",
                task_id,
                kind,
                job_id,
            )
            raise HTTPException(status_code=503, detail="Anchor 生成任务提交失败，请稍后重试")

        session.commit()
        logger.info(
            "[style-anchor] 已提交生成: task_id=%s kind=%s job_id=%s model=%s",
            task_id,
            kind,
            job_id,
            image_model,
        )
        return StyleAnchorJobResponse(
            task_id=task_id,
            asset_kind=kind,
            job_id=job_id,
            status=ANCHOR_STATUS_GENERATING,
            celery_state="PENDING",
        )


@app.get(
    "/api/v1/tasks/{task_id}/style-anchor-jobs/{job_id}",
    response_model=StyleAnchorJobResponse,
)
async def get_style_anchor_job(task_id: str, job_id: str):
    from celery.result import AsyncResult

    with Session(engine) as session:
        task = session.exec(
            select(NovelTask).where(NovelTask.task_id == task_id)
        ).first()
        if not task:
            raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
        config = load_visual_style_config(task_id, fetch_remote=True)

    kind = next(
        (
            candidate
            for candidate in (ASSET_CHARACTER, ASSET_SCENE)
            if (config.get(candidate) or {}).get("job_id") == job_id
        ),
        None,
    )
    if kind is None:
        raise HTTPException(status_code=404, detail=f"Anchor job 不存在: {job_id}")

    section = config[kind]
    status = str(section.get("status") or ANCHOR_STATUS_GENERATING)
    error = str(section.get("error") or "") or None
    celery_state = "UNKNOWN"
    try:
        result = AsyncResult(job_id, app=celery_app)
        celery_state = result.state
        if result.failed():
            status = ANCHOR_STATUS_FAILED
            error = str(result.result or error or "Anchor 生成失败")
        elif result.successful() and isinstance(result.result, dict):
            status = str(result.result.get("status") or status)
            error = str(result.result.get("error") or error or "") or None
        elif (
            status == ANCHOR_STATUS_GENERATING
            and celery_state in {"PENDING", "STARTED", "RETRY", "PROGRESS"}
        ):
            # Redis 的 PENDING 同时表示“尚未开始”和“结果已过期”。配置文件中的
            # ready/failed 是 Worker 持锁写下的权威状态，不能被 PENDING 覆盖。
            status = ANCHOR_STATUS_GENERATING
    except Exception as exc:
        logger.warning(
            "[style-anchor] 查询 Celery 状态失败，回退到配置状态: "
            "task_id=%s job_id=%s error=%s",
            task_id,
            job_id,
            exc,
        )

    target = style_anchor_path(task_id, kind)
    preview_url = (
        _style_anchor_preview_url(task_id, kind, section.get("sha256", ""))
        if target.exists() and target.stat().st_size > 0 and status == ANCHOR_STATUS_READY
        else None
    )
    return StyleAnchorJobResponse(
        task_id=task_id,
        asset_kind=kind,
        job_id=job_id,
        status=status,
        celery_state=celery_state,
        error=error,
        preview_url=preview_url,
        prompt=section.get("prompt") or None,
        model=section.get("model") or None,
        sha256=section.get("sha256") or None,
    )


@app.post("/api/v1/tasks/{task_id}/style-anchors/{asset_kind}/upload")
async def upload_style_anchor(
    task_id: str,
    asset_kind: str,
    image: UploadFile = File(...),
):
    kind = _style_anchor_kind(asset_kind)
    content = await image.read()
    final_target = style_anchor_path(task_id, kind)
    upload_target = final_target.parent / ".uploads" / f"{uuid.uuid4().hex}.png"

    try:
        metadata = normalize_uploaded_anchor(content, upload_target)
        with Session(engine) as session:
            task = _locked_visual_style_task(session, task_id)
            config = load_visual_style_config(task_id, fetch_remote=True)
            if config.get("mode") != STYLE_MODE_CUSTOM:
                raise HTTPException(status_code=409, detail="请先切换到自定义风格模式")

            final_target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(upload_target, final_target)
            section = config[kind]
            section.update(
                source="uploaded",
                confirmed=False,
                stale=False,
                sha256=metadata["sha256"],
                model="uploaded",
                prompt="",
                status=ANCHOR_STATUS_READY,
                job_id="",
                error="",
                updated_at=visual_style_timestamp(),
            )
            save_visual_style_config(task_id, config, mirror=False)
            session.commit()
            return {
                "success": True,
                "asset_kind": kind,
                "preview_url": _style_anchor_preview_url(
                    task_id,
                    kind,
                    metadata["sha256"],
                ),
                **metadata,
            }
    except HTTPException:
        raise
    except (AnchorGenerationError, VisualStyleConfigError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        upload_target.unlink(missing_ok=True)


@app.post("/api/v1/tasks/{task_id}/style-anchors/{asset_kind}/confirm")
async def confirm_style_anchor(task_id: str, asset_kind: str):
    kind = _style_anchor_kind(asset_kind)
    with Session(engine) as session:
        task = _locked_visual_style_task(session, task_id)
        try:
            config = load_visual_style_config(task_id, fetch_remote=True)
        except VisualStyleConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if config.get("mode") != STYLE_MODE_CUSTOM:
            raise HTTPException(status_code=409, detail="当前不是自定义风格模式")

        section = config[kind]
        if section.get("status") == ANCHOR_STATUS_GENERATING:
            raise HTTPException(status_code=409, detail="Anchor 尚在生成中")
        if section.get("stale"):
            raise HTTPException(status_code=409, detail="风格文本已变化，请重新生成 Anchor")
        target = style_anchor_path(task_id, kind)
        if not target.exists() or target.stat().st_size <= 0:
            raise HTTPException(status_code=404, detail="Anchor 图片不存在")
        if section.get("source") not in {"generated", "uploaded"}:
            raise HTTPException(status_code=409, detail="Anchor 来源无效，请重新生成或上传")

        section.update(
            sha256=file_sha256(target),
            confirmed=True,
            stale=False,
            status=ANCHOR_STATUS_CONFIRMED,
            job_id="",
            error="",
            updated_at=visual_style_timestamp(),
        )
        save_visual_style_config(task_id, config, mirror=False)
        session.commit()
        return _public_visual_style_config(task, config, fetch_remote=False)


@app.get("/api/v1/tasks/{task_id}/style-anchors/{asset_kind}/image")
async def get_style_anchor_image(task_id: str, asset_kind: str):
    from fastapi.responses import FileResponse

    kind = _style_anchor_kind(asset_kind)
    with Session(engine) as session:
        task = session.exec(
            select(NovelTask).where(NovelTask.task_id == task_id)
        ).first()
        if not task:
            raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    config = load_visual_style_config(task_id, fetch_remote=True)
    section = config[kind]
    target = style_anchor_path(task_id, kind)
    if not target.exists() or target.stat().st_size <= 0:
        fetched = fetch_output_file(
            task_id,
            section.get("anchor_file") or ANCHOR_RELATIVE_PATHS[kind],
        )
        if fetched is not None:
            target = fetched
    if not target.exists() or target.stat().st_size <= 0:
        raise HTTPException(status_code=404, detail="Anchor 图片不存在")
    return FileResponse(
        target,
        media_type="image/png",
        filename=target.name,
        content_disposition_type="inline",
        headers={"Cache-Control": "private, no-store"},
    )


def _task_list_item(task: NovelTask, editors: Optional[List[dict]] = None) -> dict:
    """将 NovelTask 转换为作品列表统一响应结构。"""
    remake_task_ids = normalize_remake_task_ids(
        getattr(task, "remake_task_ids", None)
    )
    return {
        "task_id": task.task_id,
        "project_name": task.project_name,
        "novel_filename": task.novel_filename,
        "user_id": task.user_id,
        "user_name": task.user_name,
        "output_language": task.output_language,
        "art_style": task.art_style,
        "story_mode": task.story_mode,
        "gameplay": task.gameplay,
        "upload_type": task.upload_type,
        "overall_status": TaskStatus.get_description(task.overall_status),
        "overall_status_code": task.overall_status,
        "progress_percentage": task.progress_percentage,
        "create_time": task.create_time.isoformat(),
        "update_time": task.update_time.isoformat(),
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "info_confirm_stage": task.info_confirm_stage,
        "editors": editors or [],
        "remake_count": len(remake_task_ids),
    }


def _active_task_filters() -> List:
    """正常作品列表的基础条件：排除已软删除（回收站）和已彻底删除的作品。"""
    return [
        NovelTask.is_deleted == False,
        NovelTask.is_purged == False,
    ]


def _time_range_filters(
    start_time: Optional[Union[date, datetime]],
    end_time: Optional[Union[date, datetime]],
) -> List:
    """按创建时间筛选；纯日期的结束值覆盖该日全天。"""
    start_bound = (
        datetime.combine(start_time, time.min)
        if start_time and not isinstance(start_time, datetime)
        else start_time
    )
    end_is_date = bool(end_time and not isinstance(end_time, datetime))
    end_bound = (
        datetime.combine(end_time + timedelta(days=1), time.min)
        if end_is_date
        else end_time
    )

    if start_bound and end_bound and start_bound > end_bound:
        raise HTTPException(status_code=400, detail="start_time 不能晚于 end_time")

    filters = []
    if start_bound:
        filters.append(NovelTask.create_time >= start_bound)
    if end_bound:
        if end_is_date:
            filters.append(NovelTask.create_time < end_bound)
        else:
            filters.append(NovelTask.create_time <= end_bound)
    return filters


def _query_active_tasks(
    *,
    extra_filters: Optional[List] = None,
    task_id: Optional[str] = None,
    novel_filename: Optional[str] = None,
    user_name: Optional[str] = None,
    keyword: Optional[str] = None,
    start_time: Optional[Union[date, datetime]] = None,
    end_time: Optional[Union[date, datetime]] = None,
    limit: int = 50,
    offset: int = 0,
) -> TaskListResponse:
    """复用正常作品列表的搜索、计数和分页逻辑。"""
    with Session(engine) as session:
        count_query = select(func.count(NovelTask.id))
        query = select(NovelTask)

        filters = _active_task_filters()
        filters.extend(extra_filters or [])
        filters.extend(_time_range_filters(start_time, end_time))

        if task_id is not None:
            filters.append(NovelTask.task_id.contains(task_id, autoescape=True))
        if novel_filename is not None:
            filters.append(NovelTask.novel_filename.contains(novel_filename, autoescape=True))
        if user_name is not None:
            filters.append(NovelTask.user_name.contains(user_name, autoescape=True))
        if keyword:
            kw = keyword.strip()
            if kw:
                filters.append(or_(
                    NovelTask.task_id.contains(kw, autoescape=True),
                    NovelTask.novel_filename.contains(kw, autoescape=True),
                    NovelTask.user_name.contains(kw, autoescape=True),
                ))

        for condition in filters:
            count_query = count_query.where(condition)
            query = query.where(condition)

        total = session.scalar(count_query) or 0
        query = query.order_by(NovelTask.create_time.desc()).offset(offset).limit(limit)
        tasks = session.exec(query).all()
        editors_by_task = get_task_editors(task.task_id for task in tasks)

        return TaskListResponse(
            total=total,
            tasks=[
                _task_list_item(task, editors_by_task.get(task.task_id, []))
                for task in tasks
            ],
        )


def _edited_task_ids_for_user(user_name: str) -> List[str]:
    """查询用户成功编辑过的作品 ID。"""
    task_ids = edited_task_ids_for_user_name(user_name)
    seen = set(task_ids)
    try:
        with business_engine.connect() as connection:
            rows = connection.execute(
                text("""
                    SELECT DISTINCT task_id
                    FROM public.material_regen_sessions
                    WHERE user_name = :user_name
                      AND (status IS NULL OR status = 'success')
                """),
                {"user_name": user_name},
            ).fetchall()
        for row in rows:
            if row[0] not in seen:
                task_ids.append(row[0])
                seen.add(row[0])
    except Exception as exc:
        logger.warning("[tasks] 查询旧素材编辑记录失败，已使用 editors 名单: %s", exc)
    return task_ids


def _participated_task_filter(user_name: str):
    """构造用户创建或编辑过作品的筛选条件。"""
    conditions = [NovelTask.user_name == user_name]
    edited_task_ids = _edited_task_ids_for_user(user_name)
    if edited_task_ids:
        conditions.append(NovelTask.task_id.in_(edited_task_ids))
    return or_(*conditions)


@app.get("/api/v1/tasks/status-counts", response_model=TaskStatusCountsResponse)
async def get_task_status_counts(user_name: str):
    """查询新建、已发布、未发布和当前用户参与作品的数量。

    user_name 由前端传入，采用精确匹配。参与作品包括该用户创建的作品，以及在
    novel_tasks.editors 中记录为该用户成功编辑过的作品。
    """
    normalized_user_name = user_name.strip()
    if not normalized_user_name:
        raise HTTPException(status_code=400, detail="user_name 不能为空")

    base_filters = _active_task_filters()
    published_exists = _successful_publish_exists()
    participated_filter = _participated_task_filter(normalized_user_name)

    with Session(engine) as session:
        def count_tasks(*extra_filters) -> int:
            statement = select(func.count(NovelTask.id)).where(
                *base_filters,
                *extra_filters,
            )
            return session.scalar(statement) or 0

        return TaskStatusCountsResponse(
            new=count_tasks(NovelTask.overall_status != TaskStatus.COMPLETED),
            published=count_tasks(published_exists),
            unpublished=count_tasks(
                NovelTask.overall_status == TaskStatus.COMPLETED,
                ~published_exists,
            ),
            participated=count_tasks(participated_filter),
        )


@app.get("/api/v1/tasks", response_model=TaskListResponse)
async def list_tasks(
    status: Optional[int] = None,
    task_id: Optional[str] = None,
    novel_filename: Optional[str] = None,
    user_name: Optional[str] = None,
    keyword: Optional[str] = None,
    start_time: Optional[Union[date, datetime]] = None,
    end_time: Optional[Union[date, datetime]] = None,
    limit: int = 50,
    offset: int = 0
):
    """
    查询任务列表，支持多条件模糊查询（作品ID、作品名称、创建者姓名）。

    Args:
        status: 过滤状态（0-待处理, 1-处理中, 2-已完成, 3-失败）
        task_id: 按任务ID模糊查询
        novel_filename: 按作品名称模糊查询
        user_name: 按创建者姓名模糊查询
        keyword: 综合搜索词，命中 task_id / novel_filename / user_name 任一字段（OR 模糊匹配）
        limit: 返回数量限制
        offset: 偏移量
    """
    status_filters = []
    if status is not None:
        status_filters.append(NovelTask.overall_status == status)
    return _query_active_tasks(
        extra_filters=status_filters,
        task_id=task_id,
        novel_filename=novel_filename,
        user_name=user_name,
        keyword=keyword,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
        offset=offset,
    )


@app.get("/api/v1/tasks/new", response_model=TaskListResponse)
async def list_new_tasks(
    task_id: Optional[str] = None,
    novel_filename: Optional[str] = None,
    user_name: Optional[str] = None,
    keyword: Optional[str] = None,
    start_time: Optional[Union[date, datetime]] = None,
    end_time: Optional[Union[date, datetime]] = None,
    limit: int = 50,
    offset: int = 0,
):
    """查询新建作品：所有未完成且未被软删除的作品。"""
    return _query_active_tasks(
        extra_filters=[NovelTask.overall_status != TaskStatus.COMPLETED],
        task_id=task_id,
        novel_filename=novel_filename,
        user_name=user_name,
        keyword=keyword,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
        offset=offset,
    )


@app.get("/api/v1/tasks/published", response_model=TaskListResponse)
async def list_published_tasks(
    task_id: Optional[str] = None,
    novel_filename: Optional[str] = None,
    user_name: Optional[str] = None,
    keyword: Optional[str] = None,
    start_time: Optional[Union[date, datetime]] = None,
    end_time: Optional[Union[date, datetime]] = None,
    limit: int = 50,
    offset: int = 0,
):
    """查询已发布作品：存在 status=published 发布记录的未删除作品。"""
    return _query_active_tasks(
        extra_filters=[_successful_publish_exists()],
        task_id=task_id,
        novel_filename=novel_filename,
        user_name=user_name,
        keyword=keyword,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
        offset=offset,
    )


@app.get("/api/v1/tasks/unpublished", response_model=TaskListResponse)
async def list_unpublished_tasks(
    task_id: Optional[str] = None,
    novel_filename: Optional[str] = None,
    user_name: Optional[str] = None,
    keyword: Optional[str] = None,
    start_time: Optional[Union[date, datetime]] = None,
    end_time: Optional[Union[date, datetime]] = None,
    limit: int = 50,
    offset: int = 0,
):
    """查询未发布作品：已完成但没有成功发布记录的未删除作品。"""
    return _query_active_tasks(
        extra_filters=[
            NovelTask.overall_status == TaskStatus.COMPLETED,
            ~_successful_publish_exists(),
        ],
        task_id=task_id,
        novel_filename=novel_filename,
        user_name=user_name,
        keyword=keyword,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
        offset=offset,
    )


@app.get("/api/v1/tasks/participated", response_model=TaskListResponse)
async def list_participated_tasks(
    user_name: str,
    task_id: Optional[str] = None,
    novel_filename: Optional[str] = None,
    keyword: Optional[str] = None,
    start_time: Optional[Union[date, datetime]] = None,
    end_time: Optional[Union[date, datetime]] = None,
    limit: int = 50,
    offset: int = 0,
):
    """查询当前用户创建或编辑过的未删除作品。"""
    normalized_user_name = user_name.strip()
    if not normalized_user_name:
        raise HTTPException(status_code=400, detail="user_name 不能为空")

    return _query_active_tasks(
        extra_filters=[_participated_task_filter(normalized_user_name)],
        task_id=task_id,
        novel_filename=novel_filename,
        keyword=keyword,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
        offset=offset,
    )


@app.delete("/api/v1/tasks/{task_id}")
async def delete_task(task_id: str):
    """
    软删除任务（仅将 novel_tasks 主表记录标记为已删除，不级联清理关联表数据与素材文件）

    Args:
        task_id: 任务ID

    Returns:
        删除结果
    """
    with Session(engine) as session:
        task = session.exec(
            select(NovelTask).where(NovelTask.task_id == task_id)
        ).first()

        if not task or task.is_purged:
            raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")

        if task.is_deleted:
            raise HTTPException(status_code=404, detail=f"任务已删除: {task_id}")

        task.is_deleted = True
        session.add(task)
        session.commit()

        logger.info(f"[delete] 任务已软删除: task_id={task_id}")
        return {"task_id": task_id, "message": "任务删除成功"}


@app.post("/api/v1/tasks/{task_id}/restore")
async def restore_task(task_id: str):
    """
    恢复被软删除的任务（将 is_deleted 置回 false）

    Args:
        task_id: 任务ID

    Returns:
        恢复结果
    """
    with Session(engine) as session:
        task = session.exec(
            select(NovelTask).where(NovelTask.task_id == task_id)
        ).first()

        if not task or task.is_purged:
            raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")

        if not task.is_deleted:
            raise HTTPException(status_code=400, detail=f"任务未被删除，无需恢复: {task_id}")

        task.is_deleted = False
        session.add(task)
        session.commit()

        logger.info(f"[restore] 任务已恢复: task_id={task_id}")
        return {"task_id": task_id, "message": "任务恢复成功"}


@app.delete("/api/v1/tasks/{task_id}/purge")
async def purge_task(task_id: str):
    """
    彻底删除任务（二级软删除：将 is_purged 置为 true）。

    置位后正常列表和回收站列表均不再返回该作品，前端彻底查不到。
    数据行与关联表数据、OSS 素材文件仍保留，仅对前端隐藏；如需回收存储由后台清理任务另行处理。

    前置条件：任务必须已在回收站中（is_deleted == true）才允许彻底删除。

    Args:
        task_id: 任务ID

    Returns:
        彻底删除结果
    """
    with Session(engine) as session:
        task = session.exec(
            select(NovelTask).where(NovelTask.task_id == task_id)
        ).first()

        if not task or task.is_purged:
            raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")

        if not task.is_deleted:
            raise HTTPException(status_code=400, detail=f"任务未在回收站中，需先删除后再彻底删除: {task_id}")

        task.is_purged = True
        session.add(task)
        session.commit()

        logger.info(f"[purge] 任务已彻底删除（前端不可见）: task_id={task_id}")
        return {"task_id": task_id, "message": "任务彻底删除成功"}


@app.get("/api/v1/tasks/deleted", response_model=TaskListResponse)
async def list_deleted_tasks(
    task_id: Optional[str] = None,
    novel_filename: Optional[str] = None,
    user_name: Optional[str] = None,
    keyword: Optional[str] = None,
    start_time: Optional[Union[date, datetime]] = None,
    end_time: Optional[Union[date, datetime]] = None,
    limit: int = 50,
    offset: int = 0
):
    """
    查询已软删除的任务列表，支持多条件模糊查询（作品ID、作品名称、创建者姓名）。

    Args:
        task_id: 按任务ID模糊查询
        novel_filename: 按作品名称模糊查询
        user_name: 按创建者姓名模糊查询
        keyword: 综合搜索词，命中 task_id / novel_filename / user_name 任一字段（OR 模糊匹配）
        limit: 返回数量限制
        offset: 偏移量
    """
    with Session(engine) as session:
        count_query = select(func.count(NovelTask.id))
        query = select(NovelTask)

        filters = [
            NovelTask.is_deleted == True,
            NovelTask.is_purged == False,
        ]
        filters.extend(_time_range_filters(start_time, end_time))
        if task_id is not None:
            filters.append(NovelTask.task_id.contains(task_id, autoescape=True))
        if novel_filename is not None:
            filters.append(NovelTask.novel_filename.contains(novel_filename, autoescape=True))
        if user_name is not None:
            filters.append(NovelTask.user_name.contains(user_name, autoescape=True))
        if keyword:
            kw = keyword.strip()
            if kw:
                filters.append(or_(
                    NovelTask.task_id.contains(kw, autoescape=True),
                    NovelTask.novel_filename.contains(kw, autoescape=True),
                    NovelTask.user_name.contains(kw, autoescape=True),
                ))

        for f in filters:
            count_query = count_query.where(f)
            query = query.where(f)

        total = session.scalar(count_query) or 0
        query = query.order_by(NovelTask.update_time.desc()).offset(offset).limit(limit)
        tasks = session.exec(query).all()
        editors_by_task = get_task_editors(task.task_id for task in tasks)

        return TaskListResponse(
            total=total,
            tasks=[
                _task_list_item(task, editors_by_task.get(task.task_id, []))
                for task in tasks
            ]
        )


# ==================== 素材重生成接口 ====================

VALID_SERVICE_TYPES = ["char_designer", "scene_painter", "audio_producer", "sound_arranger"]
VALID_MATERIAL_TYPES = ["scene", "background", "all"]
VALID_SOUND_TYPES = ["bgm", "sfx", "all"]

# 内存存储 regenerate_id -> AsyncResult（生产环境建议用 Redis）
_regenerate_results: dict = {}


@app.post("/api/v1/materials/char_designer/regenerate", response_model=MaterialRegenerateResponse)
async def regenerate_char_designer(req: CharDesignerRegenerateRequest):
    """角色素材重生成接口（单张图片）"""
    logger.info(
        f"[regen] char_designer 收到请求: task_id={req.task_id} node_id={req.node_id} "
        f"material_id={req.material_id} user_id={req.user_id} user_name={req.user_name} "
        f"model_name={req.model_name} new_prompt={'有' if req.new_prompt else '无'} "
        f"force={req.force} session_no={req.session_no}"
    )
    with Session(engine) as session:
        _ensure_task_mutable(session, req.task_id)

    started_at = datetime.now()
    sig = celery_app.signature(
        'services.char_designer.tasks.regenerate_characters',
        args=[req.task_id],
        kwargs={
            "node_id": req.node_id,
            "material_id": req.material_id,
            "new_prompt": req.new_prompt,
            "force": req.force,
            "session_no": req.session_no,
            "user_id": req.user_id,
            "user_name": req.user_name,
            "model_name": req.model_name,
        },
        queue='char_queue'
    )
    result = sig.apply_async()
    _regenerate_results[result.id] = {
        "task_id": req.task_id,
        "service_type": "char_designer",
        "started_at": started_at,
    }
    logger.info(f"[regen] char_designer 已提交 celery: regenerate_id={result.id}")

    return MaterialRegenerateResponse(
        regenerate_id=result.id,
        task_id=req.task_id,
        service_type="char_designer",
        status="processing",
        total=0, success=0, failed=0,
        items=[], errors=[],
        started_at=started_at,
        completed_at=None
    )


@app.post("/api/v1/materials/scene_painter/regenerate", response_model=MaterialRegenerateResponse)
async def regenerate_scene_painter(req: ScenePainterRegenerateRequest):
    """场景素材重生成接口（单个素材）"""
    logger.info(
        f"[regen] scene_painter 收到请求: task_id={req.task_id} material_type={req.material_type} "
        f"scene_ids={req.scene_ids} background_node_ids={req.background_node_ids} "
        f"user_id={req.user_id} user_name={req.user_name} model_name={req.model_name} "
        f"new_prompts={'有' if req.new_prompts else '无'} force={req.force} session_no={req.session_no}"
    )
    if req.material_type not in ("scene", "background"):
        raise HTTPException(
            status_code=400,
            detail="material_type 只支持 scene 或 background，暂不支持 all"
        )
    if req.material_type == "scene" and (not req.scene_ids or len(req.scene_ids) != 1):
        raise HTTPException(status_code=400, detail="scene 模式下 scene_ids 必须提供且只能包含 1 个")
    if req.material_type == "background" and (not req.background_node_ids or len(req.background_node_ids) != 1):
        raise HTTPException(status_code=400, detail="background 模式下 background_node_ids 必须提供且只能包含 1 个")

    with Session(engine) as session:
        _ensure_task_mutable(session, req.task_id)

    started_at = datetime.now()
    sig = celery_app.signature(
        'services.scene_painter.tasks.regenerate_scene_materials',
        args=[req.task_id],
        kwargs={
            "material_type": req.material_type,
            "scene_ids": req.scene_ids,
            "background_node_ids": req.background_node_ids,
            "new_prompts": req.new_prompts,
            "force": req.force,
            "session_no": req.session_no,
            "user_id": req.user_id,
            "user_name": req.user_name,
            "model_name": req.model_name,
        },
        queue='scene_queue'
    )
    result = sig.apply_async()
    _regenerate_results[result.id] = {
        "task_id": req.task_id,
        "service_type": "scene_painter",
        "started_at": started_at,
    }
    logger.info(f"[regen] scene_painter 已提交 celery: regenerate_id={result.id}")

    return MaterialRegenerateResponse(
        regenerate_id=result.id,
        task_id=req.task_id,
        service_type="scene_painter",
        status="processing",
        total=0, success=0, failed=0,
        items=[], errors=[],
        started_at=started_at,
        completed_at=None
    )


@app.post("/api/v1/materials/audio_producer/regenerate", response_model=MaterialRegenerateResponse)
async def regenerate_audio_producer(req: AudioProducerRegenerateRequest):
    """音频素材重生成接口"""
    logger.info(
        f"[regen] audio_producer 收到请求: task_id={req.task_id} node_id={req.node_id} "
        f"material_id={req.material_id} language={req.language} user_id={req.user_id} user_name={req.user_name} "
        f"new_content={'有' if req.new_content else '无'} force={req.force} session_no={req.session_no}"
    )
    with Session(engine) as session:
        _ensure_task_mutable(session, req.task_id)

    started_at = datetime.now()
    sig = celery_app.signature(
        'services.audio_producer.tasks.regenerate_audio',
        args=[req.task_id, req.node_id, req.material_id],
        kwargs={
            "new_content": req.new_content,
            "force": req.force,
            "session_no": req.session_no,
            "user_id": req.user_id,
            "user_name": req.user_name,
            "language": req.language,
        },
        queue='audio_queue'
    )
    result = sig.apply_async()
    _regenerate_results[result.id] = {
        "task_id": req.task_id,
        "service_type": "audio_producer",
        "started_at": started_at,
    }
    logger.info(f"[regen] audio_producer 已提交 celery: regenerate_id={result.id}")

    return MaterialRegenerateResponse(
        regenerate_id=result.id,
        task_id=req.task_id,
        service_type="audio_producer",
        status="processing",
        total=0, success=0, failed=0,
        items=[], errors=[],
        started_at=started_at,
        completed_at=None
    )


@app.post("/api/v1/materials/audio_producer/voice/regenerate", response_model=MaterialRegenerateResponse)
async def regenerate_character_voice(req: CharacterVoiceRegenerateRequest):
    """
    角色级换音色：更新 voice_mappings 中该角色的 voice_id，并联动重生成该角色在该语言下的全部台词音频。
    异步执行，返回 regenerate_id 供轮询状态。
    """
    logger.info(
        f"[regen] character_voice 收到请求: task_id={req.task_id} speaker_id={req.speaker_id} "
        f"voice_id={req.voice_id} language={req.language} user_id={req.user_id} user_name={req.user_name}"
    )
    with Session(engine) as session:
        _ensure_task_mutable(session, req.task_id)

    started_at = datetime.now()
    sig = celery_app.signature(
        'services.audio_producer.tasks.regenerate_character_voice',
        args=[req.task_id, req.speaker_id, req.voice_id],
        kwargs={
            "user_id": req.user_id,
            "user_name": req.user_name,
            "language": req.language,
        },
        queue='audio_queue'
    )
    result = sig.apply_async()
    _regenerate_results[result.id] = {
        "task_id": req.task_id,
        "service_type": "audio_producer",
        "started_at": started_at,
    }
    logger.info(f"[regen] character_voice 已提交 celery: regenerate_id={result.id}")

    return MaterialRegenerateResponse(
        regenerate_id=result.id,
        task_id=req.task_id,
        service_type="audio_producer",
        status="processing",
        total=0, success=0, failed=0,
        items=[], errors=[],
        started_at=started_at,
        completed_at=None
    )


@app.get("/api/v1/materials/audio_producer/voice/recommend")
async def recommend_character_voices(
    task_id: str,
    speaker_id: str,
    language: str = "zh-CN",
):
    """
    为指定角色推荐可试听的音色列表（按角色性别+年龄段筛选，排除当前在用音色）。

    Args:
        task_id:    任务 ID
        speaker_id: 角色 speaker_id（与字幕前缀、换音色接口一致）
        language:   语言（默认 zh-CN，当前仅中文有试听音频）

    Returns:
        {task_id, speaker_id, gender, age_category, current_voice_id, total, voices: [...], errors}
    """
    logger.info(f"[voice] recommend 收到请求: task_id={task_id} speaker_id={speaker_id} language={language}")
    with Session(engine) as session:
        task = session.exec(select(NovelTask).where(NovelTask.task_id == task_id)).first()
        if not task:
            raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")

    try:
        # 用 common/ 下的独立实现（api_gateway 容器只 COPY 了 common/，无 audio_producer 代码）
        from common.voice_audition import recommend_voices
        return recommend_voices(task_id=task_id, speaker_id=speaker_id, language=language)
    except Exception as e:
        logger.error(f"[voice] recommend 失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取推荐音色失败: {e}")


class VoiceAuditionRequest(BaseModel):
    voice_id: str = Field(..., min_length=1, max_length=180)
    gender: str = Field("", max_length=20)
    age_category: str = Field("", max_length=40)


@app.post("/api/v1/materials/audio_producer/voice/audition")
async def generate_voice_audition(req: VoiceAuditionRequest):
    """按需生成试听样例并保存到本地 storage。"""
    try:
        from common.voice_preview import generate_local_audition

        url = await run_in_threadpool(
            generate_local_audition,
            voice_id=req.voice_id,
            gender=req.gender,
            age_category=req.age_category,
        )
        return {"voice_id": req.voice_id, "audition_url": url}
    except Exception as exc:
        logger.warning("[voice] audition 生成失败: voice_id=%s error=%s", req.voice_id, exc)
        raise HTTPException(status_code=502, detail=f"生成试听音频失败: {exc}") from exc


@app.post("/api/v1/materials/sound_arranger/regenerate", response_model=MaterialRegenerateResponse)
async def regenerate_sound_arranger(req: SoundArrangerRegenerateRequest):
    """音效素材重生成接口"""
    logger.info(
        f"[regen] sound_arranger 收到请求: task_id={req.task_id} node_id={req.node_id} "
        f"material_id={req.material_id} sound_type={req.sound_type} "
        f"user_id={req.user_id} user_name={req.user_name} "
        f"new_prompt={'有' if req.new_prompt else '无'} force={req.force} session_no={req.session_no}"
    )
    if req.sound_type not in VALID_SOUND_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"sound_type 只支持: {', '.join(VALID_SOUND_TYPES)}"
        )

    with Session(engine) as session:
        _ensure_task_mutable(session, req.task_id)

    started_at = datetime.now()
    sig = celery_app.signature(
        'services.sound_arranger.tasks.regenerate_sound',
        args=[req.task_id, req.node_id],
        kwargs={
            "material_id": req.material_id,
            "new_prompt": req.new_prompt,
            "sound_type": req.sound_type,
            "force": req.force,
            "session_no": req.session_no,
            "user_id": req.user_id,
            "user_name": req.user_name,
        },
        queue='sound_queue'
    )
    result = sig.apply_async()
    _regenerate_results[result.id] = {
        "task_id": req.task_id,
        "service_type": "sound_arranger",
        "started_at": started_at,
    }
    logger.info(f"[regen] sound_arranger 已提交 celery: regenerate_id={result.id}")

    return MaterialRegenerateResponse(
        regenerate_id=result.id,
        task_id=req.task_id,
        service_type="sound_arranger",
        status="processing",
        total=0, success=0, failed=0,
        items=[], errors=[],
        started_at=started_at,
        completed_at=None
    )


@app.get("/api/v1/materials/regenerate/{regenerate_id}/status", response_model=MaterialRegenerateResponse)
async def get_regenerate_status(regenerate_id: str):
    """
    查询素材重生成任务状态。

    Args:
        regenerate_id: 重生成任务 ID（由 POST 接口返回）

    Returns:
        重生成详细结果
    """
    from celery.result import AsyncResult

    if regenerate_id not in _regenerate_results:
        raise HTTPException(status_code=404, detail=f"regenerate_id 不存在: {regenerate_id}")

    meta = _regenerate_results[regenerate_id]
    task_id = meta["task_id"]
    service_type = meta["service_type"]

    async_result = AsyncResult(regenerate_id, app=celery_app)

    if async_result.ready():
        # 任务已完成（成功或失败）
        # FAILURE 状态：autoretry 耗尽后抛出的异常 → async_result.get() 会重新 raise，需捕获
        if async_result.failed():
            err = str(async_result.result) if async_result.result else "任务失败"
            return MaterialRegenerateResponse(
                regenerate_id=regenerate_id,
                task_id=task_id,
                service_type=service_type,
                status="failed",
                items=[],
                errors=[f"任务执行失败（重试耗尽）: {err}"],
                started_at=meta["started_at"],
                completed_at=datetime.now()
            )
        try:
            raw = async_result.get(timeout=5)
        except Exception as e:
            return MaterialRegenerateResponse(
                regenerate_id=regenerate_id,
                task_id=task_id,
                service_type=service_type,
                status="failed",
                items=[],
                errors=[f"获取任务结果失败: {e}"],
                started_at=meta["started_at"],
                completed_at=datetime.now()
            )
        if isinstance(raw, dict):
            items = [RegenerateItem(**it) for it in raw.get("items", [])]
            status = "success" if raw.get("failed", 0) == 0 else (
                "partial" if raw.get("success", 0) > 0 else "failed"
            )
            return MaterialRegenerateResponse(
                regenerate_id=regenerate_id,
                task_id=task_id,
                service_type=service_type,
                status=status,
                total=raw.get("total", 0),
                success=raw.get("success", 0),
                failed=raw.get("failed", 0),
                items=items,
                errors=raw.get("errors", []),
                started_at=meta["started_at"],
                completed_at=datetime.now()
            )
        else:
            return MaterialRegenerateResponse(
                regenerate_id=regenerate_id,
                task_id=task_id,
                service_type=service_type,
                status="failed",
                items=[],
                errors=[str(raw)],
                started_at=meta["started_at"],
                completed_at=datetime.now()
            )
    else:
        # 任务进行中（含 PENDING / STARTED / PROGRESS / RETRY）
        celery_state = async_result.state
        info_text = f"任务执行中（celery 状态: {celery_state}）"
        if celery_state == "RETRY":
            info_text = "任务上次执行失败，Celery 正在自动重试"

        # PROGRESS：角色 calm 图已生成完，emotion 联动仍在后台跑。
        # 此时直接按 calm 自身的结果返回终态（success/failed），并置 emotion_pending=true，
        # 让前端不必等所有情绪变体跑完就能拿到 calm 的消息。
        # 需要变体结果的前端可继续轮询，直到 emotion_pending 变为 false。
        if celery_state == "PROGRESS" and isinstance(async_result.info, dict):
            progress = async_result.info
            progress_items: List[RegenerateItem] = []
            for it in progress.get("items", []):
                try:
                    progress_items.append(RegenerateItem(**it))
                except Exception as e:
                    logger.warning(f"[regen] 中间态 item 解析失败，已跳过: {e}")
            if progress_items:
                main_failed = progress.get("failed", 0)
                return MaterialRegenerateResponse(
                    regenerate_id=regenerate_id,
                    task_id=task_id,
                    service_type=service_type,
                    status="success" if main_failed == 0 else "failed",
                    total=progress.get("total", 0),
                    success=progress.get("success", 0),
                    failed=main_failed,
                    items=progress_items,
                    errors=progress.get("errors", []),
                    started_at=meta["started_at"],
                    completed_at=datetime.now(),
                    emotion_pending=bool(progress.get("emotion_pending")),
                )

        return MaterialRegenerateResponse(
            regenerate_id=regenerate_id,
            task_id=task_id,
            service_type=service_type,
            status="processing",
            items=[],
            errors=[info_text],
            started_at=meta["started_at"],
            completed_at=None
        )


# ==================== 游戏包构建接口 ====================

UI_PRESET_MAP = {
    1: ArtStyle.INK_CHINESE,
    2: ArtStyle.CYBERPUNK,
    3: ArtStyle.JAPANESE,
    4: ArtStyle.DARK_EUROPEAN,
    5: ArtStyle.MODERN_REALISM,
}

# 构建统一投递到 build_queue 由 game-builder worker 执行：
# converter/converter_steam 和 12M 游戏模板只在 game_builder 镜像里，api_gateway 镜像没有
# （services/api_gateway/Dockerfile 不 COPY 那个目录），进程内 import 会 ModuleNotFoundError。
# 两个 Deployment 挂同一个 storage PVC，所以 worker 读写的是同一份 outputs/built_games。
_BUILD_ONLINE_TASK = "services.game_builder.tasks.rebuild_online_package"
_BUILD_EXPORT_TASK = "services.game_builder.tasks.export_offline_package"


class BuildOnlineRequest(BaseModel):
    ui_preset: int    # 1-5


class BuildResponse(BaseModel):
    build_id: str
    task_id: str
    status: str       # processing | success | failed
    output_dir: Optional[str] = None
    error: Optional[str] = None
    started_at: datetime
    completed_at: Optional[datetime] = None


def _prepare_build(task_id: str) -> Path:
    """构建/导出共用的前置校验：任务可编辑、已完成且输出目录存在。

    构建不等于发布，不能在这里写 is_published；只有 CMS 确认成功后才置位。
    游戏界面构建成功前也不能推进到 publish，否则刷新会越过第 4 步。
    """
    with Session(engine) as session:
        task = _ensure_task_mutable(session, task_id)
        if task.overall_status != TaskStatus.COMPLETED:
            raise HTTPException(
                status_code=400,
                detail=f"任务尚未完成，当前状态: {TaskStatus.get_description(task.overall_status)}"
            )

    source_dir = Path(OUTPUTS_PATH) / task_id
    if not source_dir.exists():
        raise HTTPException(status_code=404, detail=f"任务输出目录不存在: {source_dir}")
    return source_dir


def _dispatch_build(task_name: str, task_id: str, **kwargs) -> str:
    """投递构建任务到 build_queue，返回 celery task id 作为 build_id。"""
    try:
        async_result = celery_app.signature(
            task_name,
            kwargs={"task_id": task_id, **kwargs},
            queue="build_queue",
        ).apply_async()
    except Exception as e:
        logger.exception(f"[build] 投递失败: task={task_name} task_id={task_id}")
        raise HTTPException(status_code=503, detail=f"构建任务投递失败: {e}")
    return async_result.id


def _confirm_workspace_stage(task_id: str, target_stage: str, log_tag: str) -> dict:
    """把 info_confirm_stage 单向推进到 target_stage(只前进不后退)并落库。
    回退或重复推进为幂等操作,直接返回当前值不报错,避免打断前端流程。
    供 confirm-material / confirm-performance 两个语义化确认接口共用。"""
    with Session(engine) as session:
        task = _ensure_task_mutable(session, task_id)
        new_stage = _advance_stage(task, target_stage)
        session.add(task)
        session.commit()

    logger.info(f"[{log_tag}] task_id={task_id} target={target_stage} -> {new_stage}")
    return {"task_id": task_id, "info_confirm_stage": new_stage}


@app.post("/api/v1/tasks/{task_id}/confirm-material")
async def confirm_material(task_id: str):
    """② 人物场景确认完成：前端「角色场景确认」按钮按下时调用，推进到 material。

    无请求体。单向推进：若已在 performance/publish 则幂等忽略，返回当前值不报错。
    确认后进入接口(entry)会把该任务跳到 ③ 演绎微调(performance)。
    """
    return _confirm_workspace_stage(task_id, InfoConfirmStage.MATERIAL, "confirm-material")


@app.post("/api/v1/tasks/{task_id}/confirm-performance")
async def confirm_performance(task_id: str):
    """③ 演绎微调完成：前端「完成演绎」按钮按下时调用，推进到 performance。

    无请求体。单向推进：若已在 publish 则幂等忽略，返回当前值不报错。
    确认后前端进入第 ④ 步游戏界面；在线包构建成功后再推进到 publish。
    """
    return _confirm_workspace_stage(task_id, InfoConfirmStage.PERFORMANCE, "confirm-performance")


@app.post("/api/v1/tasks/{task_id}/build", response_model=BuildResponse)
async def build_online(task_id: str, req: BuildOnlineRequest):
    """选择 UI 预设生成在线包：投递到 build_queue，由 game-builder worker 改写
    story_tree.json 的 art_style 后重跑 converter。返回的 build_id 是 celery task id。"""
    if req.ui_preset not in UI_PRESET_MAP:
        raise HTTPException(status_code=400, detail="ui_preset 只支持 1-5")

    _prepare_build(task_id)

    art_style = UI_PRESET_MAP[req.ui_preset]
    started_at = datetime.now()
    build_id = _dispatch_build(_BUILD_ONLINE_TASK, task_id, art_style=art_style)

    logger.info(f"[build] 已投递在线包构建: task_id={task_id} ui_preset={req.ui_preset} art_style={art_style} build_id={build_id}")

    return BuildResponse(
        build_id=build_id,
        task_id=task_id,
        status="processing",
        started_at=started_at,
    )


@app.post("/api/v1/tasks/{task_id}/export", response_model=BuildResponse)
async def export_offline(task_id: str):
    """
    第二步：导出单机包（写死 offline）。
    直接读取当前 story_tree.json 的 art_style，跑 converter_steam.py。
    """
    _prepare_build(task_id)

    started_at = datetime.now()
    build_id = _dispatch_build(_BUILD_EXPORT_TASK, task_id)

    logger.info(f"[export] 已投递单机包导出: task_id={task_id} build_id={build_id}")

    return BuildResponse(
        build_id=build_id,
        task_id=task_id,
        status="processing",
        started_at=started_at,
    )


@app.get("/api/v1/tasks/{task_id}/build/{build_id}/status", response_model=BuildResponse)
async def get_build_status(task_id: str, build_id: str):
    """查询构建/导出任务状态（build 和 export 共用）。

    状态取自 celery result backend,所以 api_gateway 重启后仍能查到 —— 早先存在
    进程内 dict 里,一次滚动更新就让前端轮询全部 404。
    """
    from celery.result import AsyncResult
    result = AsyncResult(build_id, app=celery_app)

    output_dir = None
    error = None
    completed_at = None
    if result.successful():
        status = "success"
        payload = result.result if isinstance(result.result, dict) else {}
        output_dir = payload.get("output_dir")
        completed_at = result.date_done
        # 第 4 步的在线界面包真正构建成功后，才允许刷新恢复到第 5 步。
        # 离线导出返回值没有 domain，不参与创作步骤推进。
        if "domain" in payload:
            with Session(engine) as session:
                task = session.exec(
                    select(NovelTask).where(NovelTask.task_id == task_id)
                ).first()
                if task and not bool(task.is_published):
                    _advance_stage(task, InfoConfirmStage.PUBLISH)
                    session.add(task)
                    session.commit()
    elif result.failed():
        status = "failed"
        error = str(result.result)
        completed_at = result.date_done
    else:
        status = "processing"

    return BuildResponse(
        build_id=build_id,
        task_id=task_id,
        status=status,
        output_dir=output_dir,
        error=error,
        started_at=result.date_done or datetime.now(),
        completed_at=completed_at,
    )


@app.get("/api/v1/materials/{task_id}/regen/history")
async def get_regen_history(
    task_id: str,
    node_id: Optional[str] = None,
    material_id: Optional[str] = None,
    language: Optional[str] = None,
):
    """
    查询素材重生成历史记录。

    Args:
        task_id: 任务 ID
        node_id: 可选，过滤指定章节（OSS 格式）
        material_id: 可选，过滤指定素材文件名
        language: 可选，过滤指定语言（audio_producer：zh-CN/en-US/ja-JP）

    Returns:
        历史记录列表，按 session_no ASC 排序
    """
    from common import regen_version
    records = regen_version.get_history(task_id, node_id=node_id, material_id=material_id, language=language)
    return {
        "task_id": task_id,
        "total": len(records),
        "records": records,
    }


def _restore_metadata(result: dict, task_id: str, node_id: str, material_id: str, language: str = "") -> dict:
    """根据 activate_version / rollback_version 的结果恢复服务特定元数据，返回 metadata_restore dict。

    元数据恢复走 common/regen_metadata_restore（纯 DB/OSS 操作，自包含）——api_gateway 容器
    只 COPY 了 common/，没有 audio_producer / char_designer / scene_painter / sound_arranger 代码，
    直接 import 这些 service 会报 No module named。
    """
    service_type = result.get("service_type")
    saved_prompt = result.get("saved_prompt")

    if not saved_prompt:
        return None

    from common import regen_metadata_restore as _restore

    if service_type == "audio_producer":
        try:
            return _restore.restore_audio_metadata(
                task_id=task_id,
                node_id=node_id,
                material_id=material_id,
                original_subtitle=saved_prompt,
                language=language or "zh-CN",
            )
        except Exception as e:
            return {"status": "failed", "errors": [str(e)]}

    if service_type == "sound_arranger":
        try:
            return _restore.restore_sound_prompt(
                task_id=task_id,
                node_id=node_id,
                material_id=material_id,
                original_prompt=saved_prompt,
            )
        except Exception as e:
            return {"status": "failed", "errors": [str(e)]}

    if service_type == "char_designer":
        try:
            import re as _re
            # 只取 speaker_id + combination_num；后段 emotion / 景别(_CS|_MS|_LS) 都不影响 prompt 恢复，
            # 用前缀匹配天然兼容旧(Name_1-1-1_calm)与新(Name_1-1-1_calm_LS)两种格式
            m = _re.match(r"^(.+?)_(\d+-\d+-\d+)_", material_id)
            if m:
                return _restore.restore_char_prompt(
                    task_id=task_id,
                    speaker_id=m.group(1),
                    combination_num=m.group(2),
                    original_prompt=saved_prompt,
                )
            return {"status": "failed", "errors": [f"无法从 material_id 解析 speaker_id/combination: {material_id}"]}
        except Exception as e:
            return {"status": "failed", "errors": [str(e)]}

    if service_type == "scene_painter":
        try:
            return _restore.restore_scene_prompt(
                task_id=task_id,
                material_id=material_id,
                original_prompt=saved_prompt,
            )
        except Exception as e:
            return {"status": "failed", "errors": [str(e)]}

    return None


@app.post("/api/v1/materials/{task_id}/regen/activate")
async def activate_regen_version(task_id: str, req: ActivateVersionRequest):
    """
    激活指定 material_version 的版本：将文件写到本地并更新 DB is_active。
    同步恢复服务特定元数据（字幕/prompt）。
    """
    logger.info(
        f"[regen-activate] 收到激活版本请求: task_id={task_id} node_id={req.node_id} "
        f"material_id={req.material_id} target_material_version={req.target_material_version} "
        f"subdir={req.subdir} language={req.language}"
    )
    if req.task_id != task_id:
        raise HTTPException(status_code=400, detail="路径中的 task_id 与请求体不一致")
    with Session(engine) as session:
        _ensure_task_mutable(session, task_id)

    from common import regen_version
    session_no = regen_version.get_session_no_by_version(
        task_id, req.node_id, req.material_id, req.target_material_version, language=req.language
    )
    if session_no is None:
        raise HTTPException(status_code=400,
                            detail=f"找不到 material_version={req.target_material_version} 的版本记录")

    result = regen_version.activate_version(
        task_id=task_id,
        session_no=session_no,
        node_id=req.node_id,
        material_id=req.material_id,
        subdir=req.subdir,
        language=req.language,
    )
    if result["status"] == "failed":
        logger.error(f"[regen-activate] 激活失败: task_id={task_id} material_id={req.material_id} error={result.get('error')}")
        raise HTTPException(status_code=400, detail=result.get("error", "激活失败"))

    result["metadata_restore"] = _restore_metadata(result, task_id, req.node_id, req.material_id, language=req.language)
    logger.info(f"[regen-activate] 版本激活完成: task_id={task_id} material_id={req.material_id} target_material_version={req.target_material_version}")
    return result


@app.post("/api/v1/materials/{task_id}/regen/rollback")
async def rollback_regen_version(task_id: str, req: RollbackVersionRequest):
    """
    回滚到上一个版本：自动找 current_material_version - 1 对应的记录并激活。
    同步恢复服务特定元数据（字幕/prompt）。

    例：当前版本 1.2（material_version=2），点击回滚 → 激活 1.1（material_version=1）。
        当前版本 1.1（material_version=1），点击回滚 → 激活原始版本（session_no=0）。
    """
    logger.info(
        f"[regen-rollback] 收到回滚版本请求: task_id={task_id} node_id={req.node_id} "
        f"material_id={req.material_id} current_material_version={req.current_material_version} "
        f"language={req.language}"
    )
    if req.task_id != task_id:
        raise HTTPException(status_code=400, detail="路径中的 task_id 与请求体不一致")
    with Session(engine) as session:
        _ensure_task_mutable(session, task_id)

    from common import regen_version
    result = regen_version.rollback_version(
        task_id=task_id,
        node_id=req.node_id,
        material_id=req.material_id,
        current_material_version=req.current_material_version,
        language=req.language,
    )
    if result["status"] == "failed":
        logger.error(f"[regen-rollback] 回滚失败: task_id={task_id} material_id={req.material_id} error={result.get('error')}")
        raise HTTPException(status_code=400, detail=result.get("error", "回滚失败"))

    result["metadata_restore"] = _restore_metadata(result, task_id, req.node_id, req.material_id, language=req.language)
    logger.info(f"[regen-rollback] 版本回滚完成: task_id={task_id} material_id={req.material_id} current_material_version={req.current_material_version}")
    return result


# ==================== 角色 / 场景素材清单（重生成页用） ====================
#
# 清单以 OSS 运行时路径为准({task_id}/{node_id}/assets/{characters|scenes|images}/),
# 不从 JSONB 反推 —— JSONB 里有的组合可能生成失败、也可能被景别裁剪派生出额外文件,
# 只有 OSS 上真实存在的才是玩家看到的那张图,也才是重生成能覆盖的那张。
# JSONB(character_prompts / scene_prompts / backgrounds)只用来给这些文件补提示词。

_SHOT_SIZES = ("CS", "MS", "LS")


def _oss_asset_url(key: str) -> str:
    """OSS/CDN 公网 URL,与 common.oss_utils.oss_public_url 同一套规则。"""
    from common.oss_utils import oss_public_url
    return oss_public_url(key)


@app.get("/api/v1/local-assets/{asset_path:path}")
async def get_local_asset(asset_path: str):
    """Serve a local storage asset without exposing the filesystem."""
    normalized = asset_path.replace("\\", "/").lstrip("/")
    if not normalized.startswith("storage/"):
        raise HTTPException(status_code=404, detail="本地资源不存在")

    try:
        target = local_path_for_key(normalized)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="非法本地资源路径") from exc

    if not target.is_file():
        raise HTTPException(status_code=404, detail="本地资源不存在")

    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media_type)


def _list_task_asset_keys(task_id: str, subdir: str) -> dict:
    """列出 {task_id}/*/assets/{subdir}/ 下的文件,返回 {oss_node_id: [(filename, key, size)]}。

    一次 list {task_id}/ 前缀,内存按 subdir 分组 —— 逐个节点 list 会放大 OSS 请求数,
    而节点数在分支故事里能到几十个。
    """
    from common.oss_utils import oss_list_keys

    grouped: dict = {}

    def append_asset(node_id: str, filename: str, key: str) -> None:
        """同节点同文件名优先保留先发现的运行时素材，标准 Artifact 只补缺。"""
        files = grouped.setdefault(node_id, [])
        if any(existing_filename == filename for existing_filename, _ in files):
            return
        files.append((filename, key))

    prefix = f"{task_id}/"
    marker = f"/assets/{subdir}/"
    for key in oss_list_keys(prefix):
        if marker not in key or key.endswith("/"):
            continue
        rest = key[len(prefix):]
        node_id, _, tail = rest.partition(marker)
        # tail 再含 / 说明是更深一层(如 voices/{language}/),图片目录没有这种情况
        if not node_id or "/" in tail or not tail:
            continue
        append_asset(node_id, tail, key)

    # 外部组契约只强制交付 storage/outputs，不强制复制一组运行时 assets key。
    # 标准产物始终用于补缺：否则首次重生成产生一个运行时文件后，会把其余标准素材遮掉。
    from common.node_utils import db_to_oss_node_id

    if subdir == "characters":
        stem_nodes = {}
        try:
            master, _source = _load_performance_data(task_id)
            for db_node_id, beats in master.items():
                if not isinstance(beats, list):
                    continue
                for beat in beats:
                    cp = beat.get("character_position") if isinstance(beat, dict) else None
                    if not cp:
                        continue
                    body = str(cp).strip().strip("[]")
                    for entry in _split_char_entries(body):
                        match = re.match(r"^(.+?)\(", entry.strip())
                        if match:
                            stem_nodes.setdefault(match.group(1).strip(), set()).add(db_node_id)
        except Exception as e:
            logger.info(f"[assets] 无法从 master 推导角色节点: task={task_id}: {e}")

        standard_prefix = f"storage/outputs/{task_id}/images/characters/"
        for key in oss_list_keys(standard_prefix):
            if key.endswith("/") or not key.lower().endswith(".png"):
                continue
            filename = key.rsplit("/", 1)[-1]
            stem = filename[:-4]
            nodes = stem_nodes.get(stem) or []
            if not nodes:
                continue
            for db_node_id in sorted(nodes):
                try:
                    oss_node_id = db_to_oss_node_id(db_node_id, task_id)
                except Exception:
                    continue
                append_asset(oss_node_id, filename, key)
        return grouped

    if subdir == "scenes":
        try:
            scene_prompts = _fetch_json_data(task_id, "scene_prompts", True)
        except Exception:
            scene_prompts = {}
        standard_prefix = f"storage/outputs/{task_id}/images/scenes/"
        for key in oss_list_keys(standard_prefix):
            if key.endswith("/") or not key.lower().endswith(".png"):
                continue
            filename = key.rsplit("/", 1)[-1]
            scene_id = filename[:-4]
            prompt_key = scene_id if scene_id in scene_prompts else re.sub(r"_(\d+)_\d+$", r"_\1", scene_id)
            info = scene_prompts.get(prompt_key, {})
            source_node = info.get("source_node") if isinstance(info, dict) else None
            if not source_node:
                continue
            try:
                oss_node_id = db_to_oss_node_id(source_node, task_id)
            except Exception:
                continue
            append_asset(oss_node_id, filename, key)
        return grouped

    if subdir == "images":
        standard_prefix = f"storage/outputs/{task_id}/images/backgrounds/"
        for key in oss_list_keys(standard_prefix):
            if key.endswith("/") or not key.lower().endswith(".png"):
                continue
            stored_filename = key.rsplit("/", 1)[-1]
            stem = stored_filename[:-4]
            if stem == "novel_cover":
                append_asset("novel_cover", "novel_cover.png", key)
                continue
            try:
                oss_node_id = db_to_oss_node_id(stem, task_id)
            except Exception:
                continue
            append_asset(oss_node_id, "beijing.png", key)
        return grouped

    return grouped


def _character_combination_suffix(value: object) -> str:
    """提取角色组合编号，兼容 `角色名-1-2-3`、`1-2-3` 和下划线格式。"""
    match = re.search(r"(?:^|[-_])(\d+)[-_](\d+)[-_](\d+)$", str(value or "").strip())
    return "-".join(match.groups()) if match else ""


def _fetch_character_prompts(task_id: str) -> dict:
    """character_prompts.data 是 list,转成 {(speaker_id, combination_num): record}。"""
    from common.platform_db import platform_engine

    with platform_engine.connect() as conn:
        row = conn.execute(
            text("SELECT data FROM character_prompts WHERE task_id = :tid"),
            {"tid": task_id},
        ).fetchone()
    if not row or row[0] is None:
        return {}
    raw = row[0]
    items = raw if isinstance(raw, list) else json.loads(raw)
    if not isinstance(items, list):
        return {}

    indexed = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        speaker_id = (
            item.get("speaker_id")
            or item.get("logical_character_id")
            or item.get("character_id")
        )
        combo_num = _character_combination_suffix(
            item.get("combination_num")
            or item.get("combination")
            or item.get("material_id")
        )
        if speaker_id and combo_num:
            indexed[(speaker_id, combo_num)] = item
    return indexed


def _fetch_json_data(task_id: str, table: str, order_desc: bool = False) -> dict:
    """读平台库单表的 data 字段(scene_prompts / backgrounds),无记录返回 {}。"""
    from common.platform_db import platform_engine

    order = " ORDER BY create_time DESC" if order_desc else ""
    with platform_engine.connect() as conn:
        row = conn.execute(
            text(f"SELECT data FROM {table} WHERE task_id = :tid{order} LIMIT 1"),
            {"tid": task_id},
        ).fetchone()
    if not row or row[0] is None:
        return {}
    raw = row[0]
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) or {}
    except (TypeError, ValueError):
        return {}


def _parse_char_filename(filename: str):
    """{speaker_id}_{p-b-c}_{emotion}[_{CS|MS|LS}].png → (speaker_id, combo_num, emotion, shot)。

    解析失败返回 None(OSS 上偶有手工放进去的图,不该让整个清单 500)。
    """
    stem = filename[:-4] if filename.lower().endswith(".png") else filename
    m = re.match(r"^(.+?)_(\d+-\d+-\d+)_([^_]+)_(CS|MS|LS)$", stem)
    if m:
        return m.group(1), m.group(2), m.group(3), m.group(4)
    m = re.match(r"^(.+?)_(\d+-\d+-\d+)_([^_]+)$", stem)
    if m:
        return m.group(1), m.group(2), m.group(3), ""
    return None


def _episode_nos_from_node_id(node_id: object) -> list[int]:
    """从标准 OSS 章节节点提取出现集数，例如 chapter_12_xxx -> [12]。"""
    match = re.match(r"^chapter_(\d+)(?:_|$)", str(node_id or "").strip())
    return [int(match.group(1))] if match else []


@app.get("/api/v1/tasks/{task_id}/characters")
async def list_task_characters(task_id: str, node_id: Optional[str] = None):
    """角色立绘清单。

    按 speaker_id 分组,每张图带上重生成需要的 node_id / material_id 和当前 positive_prompt。
    material_id 不含后缀,可直接传给 /materials/char_designer/regenerate。

    Args:
        task_id: 任务 ID
        node_id: 可选,只看某个章节(OSS 格式 node_id)

    Returns:
        {task_id, status, total, characters:[{speaker_id, name, items:[...]}]}
        素材还没生成时 status=pending、characters=[](不是 404)
    """
    with Session(engine) as session:
        task = session.exec(select(NovelTask).where(NovelTask.task_id == task_id)).first()
    if not task:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")

    grouped = await run_in_threadpool(_list_task_asset_keys, task_id, "characters")
    if node_id:
        grouped = {node_id: grouped.get(node_id, [])}
    prompts = await run_in_threadpool(_fetch_character_prompts, task_id)

    by_speaker: dict = {}
    total = 0
    for oss_node_id, files in grouped.items():
        for filename, key in files:
            parsed = _parse_char_filename(filename)
            if not parsed:
                continue
            speaker_id, combo_num, emotion, shot = parsed
            record = prompts.get((speaker_id, combo_num), {})
            entry = by_speaker.setdefault(speaker_id, {
                "speaker_id": speaker_id,
                "name": record.get("name") or speaker_id,
                "items": [],
            })
            entry["items"].append({
                "node_id": oss_node_id,
                "episode_nos": _episode_nos_from_node_id(oss_node_id),
                "material_id": filename[:-4] if filename.lower().endswith(".png") else filename,
                "filename": filename,
                "combination": record.get("combination", ""),
                "combination_num": combo_num,
                "emotion": emotion,
                "shot": shot or "LS",
                "prompt": (
                    record.get("positive_prompt")
                    or record.get("prompt")
                    or record.get("image_prompt")
                    or ""
                ),
                "url": _oss_asset_url(key),
            })
            total += 1

    characters = sorted(by_speaker.values(), key=lambda c: c["speaker_id"])
    for c in characters:
        # 同一角色内按 章节 → 组合 → 表情 → 景别 排,前端不用再排
        c["items"].sort(key=lambda i: (
            i["node_id"], i["combination_num"], i["emotion"],
            _SHOT_SIZES.index(i["shot"]) if i["shot"] in _SHOT_SIZES else len(_SHOT_SIZES),
        ))

    return {
        "task_id": task_id,
        "status": "ready" if total else "pending",
        "total": total,
        "characters": characters,
    }


@app.get("/api/v1/tasks/{task_id}/scenes")
async def list_task_scenes(task_id: str, node_id: Optional[str] = None):
    """场景图 + 章节封面清单。

    两类素材走的是两个不同的重生成入口,所以用 material_type 区分:
      - scene:      material_id = scene_id,传 scene_ids
      - background: material_id = OSS node_id 或 "novel_cover",传 background_node_ids

    Returns:
        {task_id, status, total, scenes:[...], backgrounds:[...]}
        素材还没生成时 status=pending、两个列表都为空(不是 404)
    """
    with Session(engine) as session:
        task = session.exec(select(NovelTask).where(NovelTask.task_id == task_id)).first()
    if not task:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")

    scene_files = await run_in_threadpool(_list_task_asset_keys, task_id, "scenes")
    image_files = await run_in_threadpool(_list_task_asset_keys, task_id, "images")
    if node_id:
        scene_files = {node_id: scene_files.get(node_id, [])}
        image_files = {node_id: image_files.get(node_id, [])}

    scene_prompts = await run_in_threadpool(_fetch_json_data, task_id, "scene_prompts", True)
    bg_data = await run_in_threadpool(_fetch_json_data, task_id, "backgrounds")
    generated_prompts = bg_data.get("generated_prompts", {}) or {}
    novel_cover_meta = bg_data.get("novel_cover_metadata", {}) or {}

    from common.node_utils import parse_oss_node_id

    scenes = []
    for oss_node_id, files in scene_files.items():
        for filename, key in files:
            scene_id = filename[:-4] if filename.lower().endswith(".png") else filename
            # scene_xxx_1_3 这类副本在表里只有源图 scene_xxx_1 的提示词,回退一层去取
            prompt_key = scene_id if scene_id in scene_prompts else re.sub(r"_(\d+)_\d+$", r"_\1", scene_id)
            info = scene_prompts.get(prompt_key, {}) if isinstance(scene_prompts.get(prompt_key), dict) else {}
            scenes.append({
                "node_id": oss_node_id,
                "episode_nos": _episode_nos_from_node_id(oss_node_id),
                "material_type": "scene",
                "material_id": scene_id,
                "filename": filename,
                "prompt": info.get("image_prompt", ""),
                "prompt_key": prompt_key if prompt_key in scene_prompts else "",
                "url": _oss_asset_url(key),
            })

    backgrounds = []
    for oss_node_id, files in image_files.items():
        for filename, key in files:
            if oss_node_id == "novel_cover" or filename == "novel_cover.png":
                backgrounds.append({
                    "node_id": oss_node_id,
                    "episode_nos": _episode_nos_from_node_id(oss_node_id),
                    "material_type": "background",
                    "material_id": "novel_cover",
                    "filename": filename,
                    "prompt": novel_cover_meta.get("prompt", ""),
                    "url": _oss_asset_url(key),
                })
                continue
            if filename != "beijing.png":
                continue  # 章节封面固定叫 beijing.png,其它文件不是可重生成素材
            db_node_id, _ = parse_oss_node_id(oss_node_id)
            info = generated_prompts.get(db_node_id, {})
            backgrounds.append({
                "node_id": oss_node_id,
                "episode_nos": _episode_nos_from_node_id(oss_node_id),
                "material_type": "background",
                # background 重生成传的是 OSS node_id 本身,不是文件名
                "material_id": oss_node_id,
                "filename": filename,
                "prompt": info.get("prompt", "") if isinstance(info, dict) else "",
                "url": _oss_asset_url(key),
            })

    scenes.sort(key=lambda s: (s["node_id"], s["material_id"]))
    backgrounds.sort(key=lambda b: (b["node_id"], b["material_id"]))
    total = len(scenes) + len(backgrounds)

    return {
        "task_id": task_id,
        "status": "ready" if total else "pending",
        "total": total,
        "scenes": scenes,
        "backgrounds": backgrounds,
    }


@app.get("/health")
async def health_check():
    """健康检查"""
    return {"status": "healthy"}


# ==================== 创建作品流程（单文件上传 + 分步配置） ====================

# ---------- 请求/响应模型 ----------

class StoryUploadResponse(BaseModel):
    task_id: str
    project_name: str
    novel_filename: str


class UploadUrlRequest(BaseModel):
    url: str
    user_id: Optional[str] = None
    user_name: Optional[str] = None
    art_style: str = ArtStyle.INK_CHINESE       # 视觉风格
    text_model: str = TextModel.DEFAULT          # 文本模型
    image_model: str = ImageModel.DEFAULT        # 生图模型
    audio_model: str = AudioModel.DEFAULT        # 音频模型
    resolution: str = Resolution.DEFAULT         # 输出分辨率：1080p / 2k / 4k


class SummaryStatusResponse(BaseModel):
    task_id: str
    status: str  # "pending" | "ready" | "failed"
    outline: Optional[str] = None
    background: Optional[str] = None
    one_liner: Optional[str] = None
    error: Optional[str] = None


# ORIGINAL / CHAPTER_BRANCH 的摘要 LLM 单次超时为 90 秒，前端 2 分钟后允许用户重试；
# MULTI 需要先跑世界观分析，给更长的保护窗口。超过窗口仍是 pending 时视为僵尸状态，
# 允许 regenerate 重新投递，避免 Redis/Worker 短暂故障后作品永久卡死。
_SUMMARY_PENDING_STALE_SEC = 3 * 60
_MULTI_SUMMARY_PENDING_STALE_SEC = 15 * 60


def _prepare_story_summary_pending(
    task_id: str,
    *,
    reject_fresh_pending: bool = False,
    stale_after_sec: int = _SUMMARY_PENDING_STALE_SEC,
) -> None:
    """创建或重置摘要 pending 记录。

    上传接口必须在投递 Celery 前先落这条记录，确保轮询能区分“尚未消费”与
    “数据库里根本没有这项任务”。重新提炼时只拒绝仍在合理执行窗口内的 pending；
    超时 pending 会被当作僵尸任务重新投递。
    """
    from services.story_planner.table.models import engine as sp_engine, StorySummary

    def _reset_existing(session: Session, summary: StorySummary) -> None:
        if reject_fresh_pending and summary.status == 0:
            updated_at = summary.update_time or summary.create_time
            age_sec = (
                (
                    datetime.now(tz=updated_at.tzinfo) - updated_at
                    if updated_at.tzinfo is not None
                    else datetime.now() - updated_at
                ).total_seconds()
                if updated_at is not None
                else stale_after_sec
            )
            if age_sec < stale_after_sec:
                raise HTTPException(status_code=409, detail="摘要正在提炼中，请勿重复提交")
            logger.warning(
                "[summary] 检测到超时 pending，允许重新投递: task_id=%s age_sec=%.1f",
                task_id,
                age_sec,
            )
        summary.status = 0
        summary.data = {
            key: value for key, value in dict(summary.data or {}).items() if key != "error"
        }
        session.add(summary)
        session.commit()

    with Session(sp_engine) as session:
        summary = session.exec(
            select(StorySummary).where(StorySummary.task_id == task_id)
        ).first()
        if summary:
            _reset_existing(session, summary)
            return

        session.add(StorySummary(task_id=task_id, data={}, status=0))
        try:
            session.commit()
            return
        except IntegrityError:
            # 两次请求同时补建缺失记录时，唯一键只允许一条。回读赢家并按同一规则处理，
            # 不把可恢复的并发点击暴露成 500。
            session.rollback()
            summary = session.exec(
                select(StorySummary).where(StorySummary.task_id == task_id)
            ).first()
            if not summary:
                raise
            _reset_existing(session, summary)


def _mark_story_summary_failed(task_id: str, error: str = "提炼失败") -> None:
    """投递阶段失败也必须落 failed，前端才能显示可重试状态。"""
    from services.story_planner.table.models import engine as sp_engine, StorySummary

    with Session(sp_engine) as session:
        summary = session.exec(
            select(StorySummary).where(StorySummary.task_id == task_id)
        ).first()
        if summary:
            summary.status = 2
            summary.data = {**dict(summary.data or {}), "error": error}
            session.add(summary)
        else:
            session.add(StorySummary(task_id=task_id, data={"error": error}, status=2))
        session.commit()


class OutlineStatusResponse(BaseModel):
    task_id: str
    status: str  # "pending" | "ready" | "confirmed"
    worldview: Optional[dict] = None
    outline: Optional[dict] = None
    key_nodes: Optional[list] = None


class StoryConfigRequest(BaseModel):
    output_language: str = "zh-CN"   # "zh-CN" | "en-US"
    art_style: str = ArtStyle.INK_CHINESE
    story_mode: str = StoryMode.ORIGINAL
    gameplay: str = "interactive_story"  # "interactive_story" | "free_growth"
    text_model: str = TextModel.DEFAULT      # 文本模型，见 GET /api/v1/models/options
    image_model: str = ImageModel.DEFAULT    # 生图模型
    audio_model: str = AudioModel.DEFAULT    # 音频模型
    resolution: str = Resolution.DEFAULT     # 输出分辨率：1080p / 2k / 4k


class AdaptationRequest(BaseModel):
    target_audience: str                      # "male" | "female"
    narrative_perspective: str                # "first_person" | "third_person"
    genre: Optional[str] = None              # "romance" | "mystery" | "thriller" | "fantasy"
    time_space: Optional[str] = None         # "modern" | "ancient" | "ancient_style" | "near_modern" | "future" | "fictional"
    plot: Optional[List[str]] = None         # 最多2个，如 ["rebirth", "revenge"]


class ConfirmRequest(BaseModel):
    project_name: str
    outline: Optional[dict] = None       # MULTI 模式可传修改后的大纲
    source_task_id: Optional[str] = None  # 创建相似作品时的源任务 ID


class SimilarConfigResponse(BaseModel):
    """创建相似品：从源任务提取的可复用配置，供新任务预填表单。

    改编约束（adaptation）仅 MULTI 模式非空。

    novel_filename 为源小说原始文件名，供前端预填/展示；原文件本身通过
    GET /api/v1/story/{task_id}/raw-novel 下载，前端可据此「像再次上传一次」重走上传流程。
    不复用 project_name、运行时状态、创建者信息。
    """
    source_task_id: str
    novel_filename: str
    pipeline_name: Optional[str] = None
    # 输出约束（对应 StoryConfigRequest）
    output_language: str
    art_style: str
    story_mode: str
    gameplay: Optional[str] = None
    text_model: Optional[str] = None
    image_model: Optional[str] = None
    audio_model: Optional[str] = None
    resolution: Optional[str] = None
    # 改编约束（对应 AdaptationRequest，仅 MULTI 模式）
    adaptation: Optional[dict] = None


def _validate_generation_params(art_style: str, text_model: str, image_model: str,
                                audio_model: str, resolution: str) -> None:
    """校验 5 个生成参数（视觉风格 + 3 模型 + 分辨率），不合法抛 400。"""
    if art_style not in ArtStyle.VALID_STYLES:
        raise HTTPException(status_code=400, detail=f"art_style 只支持: {', '.join(ArtStyle.VALID_STYLES)}")
    if text_model not in TextModel.VALID_MODELS:
        raise HTTPException(status_code=400, detail=f"text_model 只支持: {', '.join(TextModel.VALID_MODELS)}")
    if image_model not in ImageModel.VALID_MODELS:
        raise HTTPException(status_code=400, detail=f"image_model 只支持: {', '.join(ImageModel.VALID_MODELS)}")
    if audio_model not in AudioModel.VALID_MODELS:
        raise HTTPException(status_code=400, detail=f"audio_model 只支持: {', '.join(AudioModel.VALID_MODELS)}")
    if resolution not in Resolution.VALID_RESOLUTIONS:
        raise HTTPException(status_code=400, detail=f"resolution 只支持: {', '.join(Resolution.VALID_RESOLUTIONS)}")


def extract_reusable_config(task: NovelTask) -> SimilarConfigResponse:
    """从源任务提取可复用配置画像，用于「创建相似品发布」。

    复用：8 个输出约束 + 3 个改编约束（MULTI）。
    不复用：project_name、运行时状态、创建者（由新请求提供）。原始小说由下载接口返回。
    """
    import json as _json

    adaptation = None
    if task.story_mode == StoryMode.MULTI:
        hot_elements = {}
        if task.hot_elements:
            try:
                hot_elements = _json.loads(task.hot_elements)
            except Exception:
                pass
        adaptation = {
            "target_audience": task.target_audience,
            "narrative_perspective": task.narrative_perspective,
            "genre": hot_elements.get("genre"),
            "time_space": hot_elements.get("time_space"),
            "plot": hot_elements.get("plot", []),
        }

    return SimilarConfigResponse(
        source_task_id=task.task_id,
        novel_filename=task.novel_filename,
        pipeline_name=task.pipeline_name,
        output_language=task.output_language,
        art_style=task.art_style,
        story_mode=task.story_mode,
        gameplay=task.gameplay,
        text_model=task.text_model,
        image_model=task.image_model,
        audio_model=task.audio_model,
        resolution=task.resolution,
        adaptation=adaptation,
    )


# ---------- 接口实现 ----------

@app.post("/api/v1/story/upload", response_model=StoryUploadResponse)
async def upload_single_novel(
    novel: UploadFile = File(..., description="小说文件（.txt，最大50MB）"),
    user_id: Optional[str] = Form(default=None),
    user_name: Optional[str] = Form(default=None),
    output_language: str = Form(default="zh-CN", description="输出语言（zh-CN / en-US）"),
    art_style: str = Form(default=ArtStyle.INK_CHINESE, description="视觉风格：ink_chinese/cyberpunk/japanese/dark_european/modern_realism"),
    story_mode: str = Form(default=StoryMode.ORIGINAL, description="剧情模式：original/multi/chapter_branch"),
    gameplay: str = Form(default="interactive_story", description="游戏玩法：interactive_story/free_growth"),
    text_model: str = Form(default=TextModel.DEFAULT, description="文本模型，见 GET /api/v1/models/options"),
    image_model: str = Form(default=ImageModel.DEFAULT, description="生图模型，见 GET /api/v1/models/options"),
    audio_model: str = Form(default=AudioModel.DEFAULT, description="音频模型，见 GET /api/v1/models/options"),
    resolution: str = Form(default=Resolution.DEFAULT, description="输出分辨率：1080p / 2k / 4k"),
    mode: Optional[str] = Form(default=None, description="创作模式：efficiency/balanced/quality，由中台配置映射到管线"),
    pipeline_name: Optional[str] = Form(default=None, description="直接指定管线名，优先于 mode；两者都不传则各步自动选实现"),
):
    """
    步骤1：上传剧本文件。
    保存文件和 DB 记录（含 3 模型 + 分辨率 + 视觉风格 + 管线），立即触发摘要生成（Celery），
    不触发完整任务链。前端上传成功后跳转输出约束页，同时轮询 GET /summary 等待摘要就绪。
    这 5 个参数上传时即记录，后续 POST /story/{task_id}/config 仍可覆盖。

    管线在上传时定下来并落库：确认（POST /story/{task_id}/confirm）时直接读 task.pipeline_name
    提交中台编排，不再让前端二次传参。
    """
    logger.info(
        f"[upload] 收到单文件上传请求: filename={novel.filename} user_id={user_id} user_name={user_name} "
        f"output_language={output_language} art_style={art_style} story_mode={story_mode} gameplay={gameplay} "
        f"text_model={text_model} image_model={image_model} "
        f"audio_model={audio_model} resolution={resolution} mode={mode} pipeline_name={pipeline_name}"
    )
    if not novel.filename or not novel.filename.lower().endswith(".txt"):
        raise HTTPException(status_code=400, detail="仅支持 .txt 格式")

    supported_languages = ["zh-CN", "en-US"]
    if output_language not in supported_languages:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的语言代码: {output_language}。支持的语言: {', '.join(supported_languages)}",
        )
    if story_mode not in StoryMode.VALID_MODES:
        raise HTTPException(status_code=400, detail=f"story_mode 只支持: {', '.join(StoryMode.VALID_MODES)}")
    valid_gameplay = ["interactive_story", "free_growth"]
    if gameplay not in valid_gameplay:
        raise HTTPException(status_code=400, detail=f"gameplay 只支持: {', '.join(valid_gameplay)}")
    _validate_generation_params(art_style, text_model, image_model, audio_model, resolution)

    # 本地实验不访问中台；默认生产路径仍按创作模式解析并固化管线。
    if local_pipeline_enabled():
        resolved_pipeline = None
        if mode or pipeline_name:
            logger.info(
                "[upload] LOCAL_PIPELINE_MODE=1，忽略中台管线参数: mode=%s pipeline_name=%s",
                mode,
                pipeline_name,
            )
    else:
        try:
            resolved_pipeline = resolve_pipeline_name(mode, pipeline_name)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.exception("[upload] 查询创作模式映射失败")
            raise HTTPException(status_code=502, detail=f"无法从中台获取创作模式配置: {e}")

    content = await novel.read()
    if len(content) > 50 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="文件大小不能超过 50MB")

    project_name = Path(novel.filename).stem
    task_id = WorldAnalyst.generate_task_id_from_project_name(project_name)

    with Session(engine) as session:
        existing = session.exec(select(NovelTask).where(NovelTask.task_id == task_id)).first()
        if existing:
            task_id = f"{task_id}_{datetime.now().strftime('%H%M%S')}"

        # 本地 storage 是唯一文件持久化位置；oss_write 在本地模式下会
        # 映射回同一挂载目录，保留旧调用方的兼容性。
        storage_dir = Path(RAW_NOVELS_PATH)
        storage_dir.mkdir(parents=True, exist_ok=True)
        (storage_dir / f"{task_id}.txt").write_bytes(content)

        raw_novel_key = f"storage/raw_novels/{task_id}.txt"
        if not oss_write(raw_novel_key, content, "text/plain; charset=utf-8"):
            raise HTTPException(
                status_code=502, detail=f"小说本地持久化失败: {novel.filename}"
            )

        single_task = NovelTask(
            task_id=task_id,
            novel_filename=novel.filename,
            project_name=project_name,
            user_id=user_id,
            user_name=user_name,
            output_language=output_language,
            art_style=art_style,
            story_mode=story_mode,
            gameplay=gameplay,
            text_model=text_model,
            image_model=image_model,
            audio_model=audio_model,
            resolution=resolution,
            upload_type="single",
            overall_status=TaskStatus.PENDING,
            progress_percentage=0,
            info_confirm_stage=InfoConfirmStage.UPLOAD,
        )
        # 管线名写 pipeline_name，别覆盖 group_name（理由同批量入口）
        single_task.pipeline_name = resolved_pipeline
        session.add(single_task)
        session.commit()

    # 先落 pending 再投递 Celery。否则 Worker/Redis 故障时数据库里没有状态行，
    # GET /summary 会永远把“记录不存在”解释成 pending，而 regenerate 又会因缺行返回 409。
    _prepare_story_summary_pending(task_id)

    # 立即触发摘要生成（独立 Celery 任务，不阻塞上传响应）。摘要和起名分开投递，
    # 起名失败不能掩盖摘要已经成功入队，摘要失败也必须落 failed 供前端重试。
    try:
        summary_result = celery_app.signature(
            'services.story_planner.tasks.generate_summary_only',
            args=[task_id],
            queue='story_queue'
        ).apply_async()
        logger.info(
            "[upload] 摘要任务已提交: task_id=%s celery_task_id=%s",
            task_id,
            summary_result.id,
        )
    except Exception:
        _mark_story_summary_failed(task_id, "摘要任务提交失败，请重新提炼")
        logger.exception("[upload] 摘要任务提交失败: task_id=%s", task_id)

    try:
        celery_app.signature(
            'services.story_planner.tasks.generate_project_name_only',
            args=[task_id],
            queue='story_queue'
        ).apply_async()
    except Exception as e:
        logger.warning("[upload] 起名任务提交失败: task_id=%s error=%s", task_id, e)

    logger.info(f"[upload] 单文件上传完成: task_id={task_id} project_name={project_name}")
    return StoryUploadResponse(task_id=task_id, project_name=project_name, novel_filename=novel.filename)


@app.post(
    "/api/v1/story/upload-url",
    response_model=StoryUploadResponse,
    deprecated=True,
    responses={503: {"description": "链接上传功能暂时停用"}},
)
async def upload_novel_by_url(req: UploadUrlRequest):
    """链接上传端点暂时停用；爬虫实现已从本仓库移除。"""
    logger.warning(
        "[upload-url] 已停用的链接上传端点被调用: url=%s user_id=%s",
        req.url,
        req.user_id,
    )
    raise HTTPException(
        status_code=503,
        detail="链接上传功能暂时停用，请改用文件上传接口 /api/v1/story/upload",
    )




@app.post("/api/v1/story/{task_id}/config")
async def save_story_config(task_id: str, req: StoryConfigRequest):
    """
    步骤2：保存输出约束配置（输出语言、画面风格、剧情模式、游戏玩法）。
    当前流程保存后统一进入信息确认页，不再跳转独立改编约束页。
    """
    logger.info(
        f"[config] 收到输出约束配置: task_id={task_id} output_language={req.output_language} "
        f"art_style={req.art_style} story_mode={req.story_mode} gameplay={req.gameplay} "
        f"text_model={req.text_model} image_model={req.image_model} audio_model={req.audio_model} "
        f"resolution={req.resolution}"
    )
    supported_languages = ["zh-CN", "en-US"]
    if req.output_language not in supported_languages:
        raise HTTPException(status_code=400, detail=f"不支持的语言: {req.output_language}")
    if req.art_style not in ArtStyle.VALID_STYLES:
        raise HTTPException(status_code=400, detail=f"art_style 只支持: {', '.join(ArtStyle.VALID_STYLES)}")
    if req.story_mode not in StoryMode.VALID_MODES:
        raise HTTPException(status_code=400, detail=f"story_mode 只支持: {', '.join(StoryMode.VALID_MODES)}")
    if req.text_model not in TextModel.VALID_MODELS:
        raise HTTPException(status_code=400, detail=f"text_model 只支持: {', '.join(TextModel.VALID_MODELS)}")
    if req.image_model not in ImageModel.VALID_MODELS:
        raise HTTPException(status_code=400, detail=f"image_model 只支持: {', '.join(ImageModel.VALID_MODELS)}")
    if req.audio_model not in AudioModel.VALID_MODELS:
        raise HTTPException(status_code=400, detail=f"audio_model 只支持: {', '.join(AudioModel.VALID_MODELS)}")
    if req.resolution not in Resolution.VALID_RESOLUTIONS:
        raise HTTPException(status_code=400, detail=f"resolution 只支持: {', '.join(Resolution.VALID_RESOLUTIONS)}")

    with Session(engine) as session:
        task = _ensure_task_mutable(session, task_id)
        task.output_language = req.output_language
        task.art_style = req.art_style
        task.story_mode = req.story_mode
        task.gameplay = req.gameplay
        task.text_model = req.text_model
        task.image_model = req.image_model
        task.audio_model = req.audio_model
        task.resolution = req.resolution
        _advance_stage(task, InfoConfirmStage.CONFIG)
        session.add(task)
        session.commit()

    logger.info(f"[config] 输出约束已保存: task_id={task_id} story_mode={req.story_mode}")
    return {
        "task_id": task_id,
        "story_mode": req.story_mode,
        "need_adaptation": False,
    }


@app.get("/api/v1/story/{task_id}/adaptation")
async def get_adaptation_constraints(task_id: str):
    """
    查询改编约束配置（仅 MULTI 模式）。用于信息确认页回显。
    """
    import json as _json

    with Session(engine) as session:
        task = session.exec(
            select(NovelTask).where(NovelTask.task_id == task_id)
        ).first()
        if not task:
            raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")

    hot_elements = {}
    if task.hot_elements:
        try:
            hot_elements = _json.loads(task.hot_elements)
        except Exception:
            pass

    return {
        "task_id": task_id,
        "target_audience": task.target_audience,
        "narrative_perspective": task.narrative_perspective,
        "genre": hot_elements.get("genre"),
        "time_space": hot_elements.get("time_space"),
        "plot": hot_elements.get("plot", []),
    }


@app.get("/api/v1/story/similar-config", response_model=SimilarConfigResponse)
async def get_similar_config(source_task_id: str):
    """
    创建相似品发布：从已存在的源任务提取可复用配置，供新任务预填表单。

    返回源任务的输出约束（语言/风格/剧情模式/玩法/三类模型/分辨率）和
    改编约束（仅 MULTI 模式）。前端拿到后预填 config/adaptation 页，
    用户可逐项修改后走正常 upload→config→(adaptation)→confirm 流程。

    不复用：作品名、运行时状态、创建者（user_id/user_name 由新请求提供）。
    原始小说由 raw-novel 接口单独返回。

    Args:
        source_task_id: 作为模板的源任务 ID

    Returns:
        SimilarConfigResponse: 可复用配置画像
    """
    with Session(engine) as session:
        task = session.exec(
            select(NovelTask).where(NovelTask.task_id == source_task_id)
        ).first()
        if not task:
            raise HTTPException(status_code=404, detail=f"源任务不存在: {source_task_id}")

    return extract_reusable_config(task)


@app.get("/api/v1/story/{task_id}/raw-novel")
async def download_raw_novel(task_id: str):
    """
    下载源任务的原始小说文件（storage/raw_novels/{task_id}.txt）。

    「创建相似品」时，前端拿到 similar-config 的 novel_filename 后调用本接口取回原文件，
    像用户再次上传一次那样把文件塞进上传表单，重走 upload → config →(adaptation)→ confirm 流程。

    Args:
        task_id: 源任务 ID

    Returns:
        原始 .txt 文件（text/plain），Content-Disposition 使用源小说原始文件名。
    """
    from fastapi.responses import FileResponse, Response as FileBytesResponse
    from urllib.parse import quote

    with Session(engine) as session:
        task = session.exec(
            select(NovelTask).where(NovelTask.task_id == task_id)
        ).first()
        if not task:
            raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")

    novel_path = Path(RAW_NOVELS_PATH) / f"{task_id}.txt"
    filename = task.novel_filename or f"{task_id}.txt"
    if novel_path.exists():
        return FileResponse(
            path=novel_path,
            media_type="text/plain",
            filename=filename,
        )

    # 本地模式下仅允许从当前 storage 读取，不再依赖远程对象存储。
    content = oss_read(f"storage/raw_novels/{task_id}.txt")
    if content is None:
        raise HTTPException(status_code=404, detail="原始小说文件不存在")
    return FileBytesResponse(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@app.post("/api/v1/story/{task_id}/adaptation")
async def save_adaptation_constraints(task_id: str, req: AdaptationRequest):
    """
    步骤3（仅 MULTI 模式）：保存改编约束，触发主流程（当前为假实现）。
    参数：target_audience, narrative_perspective, genre, time_space, plot（最多2个）
    """
    import json as _json

    logger.info(
        f"[adaptation] 收到改编约束: task_id={task_id} target_audience={req.target_audience} "
        f"narrative_perspective={req.narrative_perspective} genre={req.genre} "
        f"time_space={req.time_space} plot={req.plot}"
    )
    if req.plot and len(req.plot) > 2:
        raise HTTPException(status_code=400, detail="热门情节最多选2个")

    hot_elements = {
        "genre": req.genre,
        "time_space": req.time_space,
        "plot": req.plot or [],
    }

    with Session(engine) as session:
        task = _ensure_task_mutable(session, task_id)
        task.target_audience = req.target_audience
        task.narrative_perspective = req.narrative_perspective
        task.hot_elements = _json.dumps(hot_elements, ensure_ascii=False)
        # 兼容旧调用方：约束数据仍可保存，但当前产品流程已无独立 adaptation 页面。
        # 阶段最多推进到 config，重进时统一落信息确认页。
        _advance_stage(task, InfoConfirmStage.CONFIG)
        session.add(task)
        session.commit()

    # 触发主流程消息队列（当前为假实现，实际触发逻辑待接入）
    pipeline_triggered = False

    logger.info(f"[adaptation] 改编约束已保存: task_id={task_id} pipeline_triggered={pipeline_triggered}")
    return {
        "success": True,
        "task_id": task_id,
        "pipeline_triggered": pipeline_triggered,
    }


@app.post("/api/v1/story/{task_id}/confirm")
async def confirm_and_start(task_id: str, req: ConfirmRequest):
    """
    步骤4：信息确认，提交中台编排。
    - 保存用户修改的作品名
    - MULTI 模式：将 Worldview/Outline 状态改为 CONFIRMED（Celery 轮询自动继续）
    - 按 task.pipeline_name（上传时定下）提交中台编排，7 服务 DAG 的拓扑在平台侧模板里
    - 创建相似作品时，把成功启动的新 task_id 追加到源作品 remake_task_ids
    """
    from services.story_planner.table.models import (
        engine as sp_engine, Worldview, Outline, WorldviewStatus, OutlineStatus,
    )

    normalized_source_task_id = (req.source_task_id or "").strip() or None
    logger.info(
        f"[confirm] 收到信息确认请求: task_id={task_id} project_name={req.project_name} "
        f"source_task_id={normalized_source_task_id}"
    )
    with Session(engine) as session:
        task = _ensure_task_mutable(session, task_id)
        if _task_generation_started(task):
            raise HTTPException(status_code=400, detail="任务已提交生成，请勿重复提交")
        try:
            validate_visual_style_for_run(task_id, fetch_remote=True)
        except VisualStyleConfigError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if normalized_source_task_id:
            if normalized_source_task_id == task_id:
                raise HTTPException(status_code=400, detail="源任务不能是当前任务自身")
            source_task = session.exec(
                select(NovelTask).where(NovelTask.task_id == normalized_source_task_id)
            ).first()
            if not source_task:
                raise HTTPException(
                    status_code=404,
                    detail=f"源任务不存在: {normalized_source_task_id}",
                )

        task.project_name = req.project_name
        task.generated_project_name = req.project_name
        session.add(task)
        session.commit()
        current_story_mode = task.story_mode

    # MULTI 模式：确认世界观和大纲
    if current_story_mode == StoryMode.MULTI:
        with Session(sp_engine) as session:
            worldview = session.exec(select(Worldview).where(Worldview.task_id == task_id)).first()
            outline = session.exec(select(Outline).where(Outline.task_id == task_id)).first()
            if worldview:
                worldview.status = WorldviewStatus.CONFIRMED
                session.add(worldview)
            if outline:
                if req.outline:
                    outline.data = req.outline
                outline.status = OutlineStatus.CONFIRMED
                session.add(outline)
            session.commit()

    # 提交中台编排（与批量入口同一条路径，不再本地拼 Celery chain）
    with Session(engine) as session:
        # 锁行后再判重：确认按钮被连点、或多个标签页同时确认时，
        # 后到的请求会阻塞在这里等锁，拿到锁时已能看到前一个请求写下的 PROCESSING/CONFIRMED，
        # 于是被判为重复提交而不会二次提交给中台。
        task = session.exec(
            select(NovelTask)
            .where(NovelTask.task_id == task_id)
            .with_for_update()
        ).first()
        if not task:
            raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
        if _task_generation_started(task):
            raise HTTPException(status_code=400, detail="任务已提交生成，请勿重复提交")

        try:
            visual_style_config = validate_visual_style_for_run(
                task_id,
                fetch_remote=True,
            )
        except VisualStyleConfigError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        try:
            persist_visual_style_bundle_for_run(
                task_id,
                visual_style_config,
                strict_remote=not local_pipeline_enabled(),
            )
        except Exception as exc:
            logger.exception(
                "[confirm] 视觉风格配置持久化失败 task_id=%s: %s",
                task_id,
                exc,
            )
            task.error_message = f"视觉风格配置持久化失败: {exc}"
            session.add(task)
            session.commit()
            raise HTTPException(
                status_code=503,
                detail="视觉风格配置或 Anchor 持久化失败，请稍后重试",
            ) from exc

        resolved_pipeline = task.pipeline_name  # 上传时已定下并落库
        try:
            if local_pipeline_enabled():
                local_result = submit_local_visual_pipeline(celery_app, task_id)
                logger.info(
                    "[confirm] 已提交本地无音频 Pipeline: task_id=%s celery_task_id=%s",
                    task_id,
                    local_result.id,
                )
            else:
                submit_platform_task(task_id, pipeline_name=resolved_pipeline)
        except Exception as e:
            # 提交失败就是没跑起来：只记错误，不动 overall_status/info_confirm_stage，
            # 用户重试时仍能通过判重（任务还停在未开始的状态）
            target = "本地 Pipeline" if local_pipeline_enabled() else "中台编排"
            logger.exception("[confirm] 提交%s失败 task_id=%s: %s", target, task_id, e)
            task.error_message = f"提交{target}失败: {e}"
            session.add(task)
            session.commit()
            raise HTTPException(status_code=503, detail=f"提交{target}失败，请稍后重试")

        task.overall_status = TaskStatus.PROCESSING
        task.progress_percentage = 5
        task.error_message = None
        _advance_stage(task, InfoConfirmStage.CONFIRMED)
        # 复刻记录：源任务开头已校验存在，这里失败只可能是期间被删，
        # 不能因此回滚——任务已经在中台跑起来了，状态必须落库，否则会被判为未提交而重复提交
        if normalized_source_task_id and not append_remake_task_id(
            session,
            normalized_source_task_id,
            task_id,
        ):
            logger.warning(
                f"[confirm] 源任务已不存在，跳过复刻记录: source_task_id={normalized_source_task_id}"
            )
        session.add(task)
        session.commit()
        if local_pipeline_enabled():
            logger.info("[confirm] 本地无音频 Pipeline 已启动: task_id=%s", task_id)
        else:
            logger.info(
                f"[confirm] 已提交中台编排: task_id={task_id} pipeline={resolved_pipeline or '自动'}"
            )

    logger.info(f"[confirm] 信息确认完成: task_id={task_id}")
    return {"success": True, "task_id": task_id}


@app.get("/api/v1/story/{task_id}/summary", response_model=SummaryStatusResponse)
async def get_story_summary(task_id: str):
    """
    轮询摘要状态。上传后立即可调用，status=pending 时前端展示加载动效。
    - pending: 摘要生成中
    - ready: 摘要已就绪，可展示
    """
    from services.story_planner.table.models import engine as sp_engine, StorySummary

    with Session(sp_engine) as session:
        summary = session.exec(select(StorySummary).where(StorySummary.task_id == task_id)).first()

    if not summary or summary.status == 0:
        return SummaryStatusResponse(task_id=task_id, status="pending")

    if summary.status == 2:
        return SummaryStatusResponse(
            task_id=task_id,
            status="failed",
            error=summary.data.get("error", "提炼失败"),
        )

    return SummaryStatusResponse(
        task_id=task_id,
        status="ready",
        outline=summary.data.get("outline", ""),
        background=summary.data.get("background", ""),
        one_liner=summary.data.get("one_liner", ""),
    )


@app.post("/api/v1/story/{task_id}/summary/regenerate")
async def regenerate_story_summary(task_id: str):
    """重新异步提炼故事梗概、故事背景和一句话故事。"""
    with Session(engine) as session:
        task = _ensure_task_mutable(session, task_id)
        if task.overall_status == TaskStatus.PROCESSING:
            raise HTTPException(status_code=400, detail="任务已进入后续处理，不能重新提炼")

    novel_path = Path(RAW_NOVELS_PATH) / f"{task_id}.txt"
    if not novel_path.exists():
        raise HTTPException(status_code=404, detail="原始剧本文件不存在")

    stale_after_sec = (
        _MULTI_SUMMARY_PENDING_STALE_SEC
        if task.story_mode == StoryMode.MULTI
        else _SUMMARY_PENDING_STALE_SEC
    )
    _prepare_story_summary_pending(
        task_id,
        reject_fresh_pending=True,
        stale_after_sec=stale_after_sec,
    )

    try:
        result = celery_app.signature(
            'services.story_planner.tasks.generate_summary_only',
            args=[task_id],
            queue='story_queue',
        ).apply_async()
    except Exception as exc:
        _mark_story_summary_failed(task_id, "摘要任务提交失败，请重新提炼")
        logger.exception(f"[summary-regenerate] 提交失败: task_id={task_id}")
        raise HTTPException(status_code=500, detail="摘要任务提交失败，请稍后重试") from exc

    logger.info(
        f"[summary-regenerate] 已提交: task_id={task_id} celery_task_id={result.id}"
    )
    return {
        "task_id": task_id,
        "status": "pending",
        "celery_task_id": result.id,
    }


@app.get("/api/v1/story/{task_id}/outline", response_model=OutlineStatusResponse)
async def get_story_outline(task_id: str, lang: Optional[str] = None):
    """
    MULTI 模式专用：查询世界观和大纲状态。
    - pending: 世界观/大纲尚未生成
    - ready: 已生成（DRAFT），等待用户确认
    - confirmed: 已确认，任务链执行中
    """
    from services.story_planner.table.models import (
        engine as sp_engine, Worldview, Outline, WorldviewStatus, OutlineStatus, StoryTreeJson,
    )
    from common.platform_db import platform_engine  # story_tree_json 已迁平台库

    with Session(sp_engine) as session:
        worldview = session.exec(select(Worldview).where(Worldview.task_id == task_id)).first()
        outline = session.exec(select(Outline).where(Outline.task_id == task_id)).first()
    with Session(platform_engine) as session:
        story_json = session.exec(select(StoryTreeJson).where(StoryTreeJson.task_id == task_id)).first()

    if not worldview or not outline:
        return OutlineStatusResponse(task_id=task_id, status="pending")

    status_str = "confirmed" if (
        worldview.status == WorldviewStatus.CONFIRMED and outline.status == OutlineStatus.CONFIRMED
    ) else "ready"

    key_nodes = None
    if story_json and story_json.data:
        selected_tree = select_story_tree_for_consumer(
            story_json.data,
            _story_tree_request_language(lang),
        )
        nodes_map = selected_tree.get("nodes", {})
        key_nodes = [
            {
                "node_id": nid,
                "title": n.get("title", ""),
                "depth": n.get("depth", 0),
                "is_branch": len(n.get("children", [])) > 1,
                "choices": [c.get("choice_text", "") for c in n.get("children", [])] if len(n.get("children", [])) > 1 else [],
            }
            for nid, n in nodes_map.items()
        ]

    return OutlineStatusResponse(
        task_id=task_id, status=status_str,
        worldview=worldview.data, outline=outline.data, key_nodes=key_nodes,
    )


@app.post("/api/v1/story/{task_id}/generate-name")
async def generate_project_name(task_id: str):
    """
    用 LLM 从摘要内容生成作品名，供信息确认页「刷新」图标使用。
    """
    from services.story_planner.table.models import engine as sp_engine, StorySummary
    from services.story_planner.core.llm_client import LLMClient

    logger.info(f"[generate-name] 收到生成作品名请求: task_id={task_id}")
    with Session(engine) as session:
        _ensure_task_mutable(session, task_id)
    with Session(sp_engine) as session:
        summary = session.exec(select(StorySummary).where(StorySummary.task_id == task_id)).first()

    if not summary or summary.status == 0:
        raise HTTPException(status_code=400, detail="摘要尚未生成，请稍后再试")

    data = summary.data
    prompt_text = f"大纲：{data.get('outline', '')}\n背景：{data.get('background', '')}\n一句话故事：{data.get('one_liner', '')}"

    _usage_tracker = None
    usage_run_id = None
    try:
        import os
        try:
            from common.usage_tracker import tracker as _usage_tracker
            usage_run_id = _usage_tracker.set_context(
                task_id, "api_gateway", force_new_run=True
            )
        except Exception:
            _usage_tracker = None
        client = LLMClient(provider=os.getenv("LLM_PROVIDER", "qwen"), temperature=0.8, max_tokens=50)
        name = client.chat_completion(
            system_prompt="你是一个小说命名专家。根据以下故事信息，生成一个简洁有力的中文作品名（4-8个字，不加书名号，只输出名字本身）。",
            user_prompt=prompt_text,
            timeout=30,
        ).strip().strip("《》")
    except Exception as e:
        logger.error(f"[generate-name] 名字生成失败: task_id={task_id} error={e}")
        raise HTTPException(status_code=500, detail=f"名字生成失败: {e}")
    finally:
        if _usage_tracker and usage_run_id:
            _usage_tracker.flush(run_id=usage_run_id)
            _usage_tracker.clear_context(usage_run_id)

    logger.info(f"[generate-name] 作品名已生成: task_id={task_id} project_name={name}")

    with Session(engine) as session:
        task = _ensure_task_mutable(session, task_id)
        task.generated_project_name = name
        session.add(task)
        session.commit()

    return {"project_name": name}


@app.get("/api/v1/story/{task_id}/script-preview")
async def get_script_preview(task_id: str):
    """
    返回旁白型剧本内容，beats 未生成时返回 404。
    格式：每行 [说话人]: 内容，换行分隔。
    """
    from common.platform_db import platform_engine  # plots 已迁平台库
    from sqlalchemy import text

    with platform_engine.connect() as conn:
        row = conn.execute(
            text("SELECT data FROM plots WHERE task_id = :tid LIMIT 1"),
            {"tid": task_id},
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="beats 尚未生成")

    data = row[0]
    lines = []
    for chapter_beats in data.values():
        for beat in chapter_beats:
            speaker = beat.get("speaker_name") or beat.get("speaker_id") or "旁白"
            content = beat.get("content", "").strip()
            if content:
                lines.append(f"[{speaker}]: {content}")

    return {
        "task_id": task_id,
        "total": len(lines),
        "content": "\n".join(lines),
    }


@app.get("/api/v1/story/{task_id}/plots-status")
async def get_plots_status(task_id: str):
    """
    轮询剧情 beats 状态。
    - pending: beats 尚未生成
    - ready: beats 已就绪，可调用 /script-preview
    """
    from common.platform_db import platform_engine  # plots 已迁平台库
    from sqlalchemy import text

    with platform_engine.connect() as conn:
        row = conn.execute(
            text("SELECT id FROM plots WHERE task_id = :tid LIMIT 1"),
            {"tid": task_id},
        ).fetchone()

    status = "ready" if row else "pending"
    return {"task_id": task_id, "status": status}


@app.get("/api/v1/story/{task_id}/tree")
async def get_story_tree(task_id: str, lang: Optional[str] = None):
    """
    查询故事树节点列表。

    从 story_tree_json 表读取故事树，提取每个节点的展示字段：
    node_id / title / depth / parent_ids / is_branch / is_original / summary / children。
    节点按 depth 升序排列（同层保持原顺序），便于前端从根往下渲染树。

    Args:
        task_id: 任务 ID

    Returns:
        {task_id, nodes}，故事树未生成时返回 404
    """
    from services.story_planner.table.models import StoryTreeJson
    from common.platform_db import platform_engine  # story_tree_json 已迁平台库

    with Session(platform_engine) as session:
        story_json = session.exec(
            select(StoryTreeJson).where(StoryTreeJson.task_id == task_id)
        ).first()

    if not story_json or not story_json.data:
        raise HTTPException(status_code=404, detail=f"故事树尚未生成: {task_id}")

    requested_lang = _story_tree_request_language(lang)
    selected_tree = select_story_tree_for_consumer(story_json.data, requested_lang)
    nodes_map = selected_tree.get("nodes", {})

    nodes = [
        {
            "node_id": n.get("id", nid),
            "title": n.get("title", ""),
            "depth": n.get("depth", 0),
            "parent_id": n.get("parent_id"),
            "parent_ids": n.get("parent_ids", []),
            "is_branch": len(n.get("children", [])) > 1,
            "is_original": n.get("is_original", True),
            "summary": n.get("summary", ""),
            "children": [
                {
                    "choice_text": c.get("choice_text", ""),
                    "child_id": c.get("child_id", ""),
                    "branch_variable": c.get("branch_variable"),
                    "branch_threshold": c.get("branch_threshold"),
                }
                for c in n.get("children", [])
            ],
        }
        for nid, n in nodes_map.items()
    ]

    nodes.sort(key=lambda x: x["depth"])

    # children 按其 child_id 对应节点在顶层 nodes 列表中的位置排序，与顶层顺序对齐；
    # 未在列表中的 child_id（异常情况）排到末尾并保持原顺序。
    node_order = {n["node_id"]: i for i, n in enumerate(nodes)}
    for n in nodes:
        n["children"].sort(key=lambda c: node_order.get(c["child_id"], len(node_order)))

    return {
        "task_id": task_id,
        "nodes": nodes,
        # 数值路由信息：结局节点靠 branch_variable/branch_threshold 自动跳转，
        # 前端据此解释每条路由（如 calmness>=65 触发某结局）。
        "language": requested_lang,
        "variable_defs": selected_tree.get("variable_defs", []),
        "player_variables": selected_tree.get("player_variables", {}),
        "branch_thresholds": selected_tree.get("branch_thresholds", {}),
    }


class UpdateCastRequest(BaseModel):
    task_id: str
    node_id: str
    beat_id: str
    character_position: str
    user_id: str = Field(..., description="操作人 ID")
    user_name: str = Field(..., description="操作人名称")

    @field_validator("task_id", "node_id")
    @classmethod
    def _validate_resource_id(cls, value: str, info):
        return normalize_resource_id(value, info.field_name)

    @field_validator("beat_id")
    @classmethod
    def _validate_beat_id(cls, value: str):
        return normalize_beat_id(value)

    @field_validator("character_position")
    @classmethod
    def _validate_character_position(cls, value: str):
        return normalize_character_position(value)

    @field_validator("user_id", "user_name")
    @classmethod
    def _validate_editor(cls, value: str, info):
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{info.field_name} 不能为空")
        return normalized


def _persist_cast_to_ultimate(task_id: str, node_id: str, beat_id: str, character_position: str):
    """把热更新后的 character_position 写回标准 master，并镜像 ultimate。

    以游戏包为主：标准 master 中没有对应 task/node/beat（老任务或产物未生成）时
    静默跳过；一旦找到该 beat，持久化失败会抛出异常，让 cast_updater 回滚游戏包。
    """
    try:
        data, source = _load_performance_data(task_id)
    except HTTPException:
        logger.warning(f"[update-cast] 无标准演出数据，跳过持久化: task_id={task_id}")
        return

    beats = data.get(node_id)
    if not isinstance(beats, list):
        logger.warning(
            f"[update-cast] master 无该节点，跳过持久化: task_id={task_id} node_id={node_id}"
        )
        return
    matches = [beat for beat in beats if str(beat.get("beat_id")) == beat_id]
    if not matches:
        logger.warning(
            f"[update-cast] master 无该 beat，跳过持久化: "
            f"task_id={task_id} node_id={node_id} beat_id={beat_id}"
        )
        return

    for beat in matches:
        beat["character_position"] = character_position
    _persist_performance_data(task_id, data)
    logger.info(
        f"[update-cast] 标准演出数据已同步: task_id={task_id} node_id={node_id} "
        f"beat_id={beat_id} source={source}"
    )


@app.patch(
    "/api/v1/game/cast",
    responses={
        404: {"description": "任务、节点、beat 或构建文件不存在"},
        409: {"description": "构建脚本结构异常，无法安全热更新"},
        422: {"description": "请求字段或 character_position 格式错误"},
    },
)
async def update_cast(req: UpdateCastRequest):
    logger.info(
        f"[update-cast] 收到更新出镜请求: task_id={req.task_id} node_id={req.node_id} "
        f"beat_id={req.beat_id} character_position={req.character_position} "
        f"user_id={req.user_id} user_name={req.user_name}"
    )
    with Session(engine) as session:
        _ensure_task_mutable(session, req.task_id)
    try:
        result = apply_cast_update(
            task_id=req.task_id,
            node_id=req.node_id,
            beat_id=req.beat_id,
            character_position=req.character_position,
            built_games_path=BUILT_GAMES_PATH,
            persist_callback=lambda source_node_id, resolved_beat_id, normalized_position: _persist_cast_to_ultimate(
                req.task_id, source_node_id, resolved_beat_id, normalized_position
            ),
        )
    except CastValidationError as e:
        logger.warning(f"[update-cast] 请求格式错误: {e}")
        raise HTTPException(status_code=422, detail=str(e))
    except CastNotFoundError as e:
        logger.error(f"[update-cast] 更新出镜失败: task_id={req.task_id} node_id={req.node_id} error={e}")
        raise HTTPException(status_code=404, detail=str(e))
    except CastArtifactError as e:
        logger.error(f"[update-cast] 构建产物无法热更新: task_id={req.task_id} node_id={req.node_id} error={e}")
        raise HTTPException(status_code=409, detail=str(e))

    logger.info(
        f"[update-cast] 出镜更新完成: task_id={req.task_id} source_node_id={result.source_node_id} "
        f"built_node_id={result.built_node_id} beat_id={req.beat_id} "
        f"script={result.script_path}"
    )
    try:
        editor_recorded = record_task_editor(req.task_id, req.user_id, req.user_name)
    except Exception:
        editor_recorded = False
        logger.exception(f"[update-cast] 出镜更新成功，但记录编辑者失败: task_id={req.task_id}")
    return {
        "status": "ok",
        "editor_recorded": editor_recorded,
        # 前端据此回读校验，避免接口 200 但游戏包仍展示旧缓存时误报“已更新”。
        "character_position": result.character_position,
        "source_node_id": result.source_node_id,
        "built_node_id": result.built_node_id,
        "cache_busted": bool(result.index_path or result.service_worker_path),
    }


_CHAR_SHOT_SIZES = {"LS", "MS", "CS"}


def _split_char_entries(s: str):
    """按顶层逗号切分 character_position 条目（括号内的逗号不切）。"""
    items, depth, cur = [], 0, []
    for ch in s:
        if ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1)
            cur.append(ch)
        elif ch == "," and depth == 0:
            items.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        items.append("".join(cur))
    return [it.strip() for it in items if it.strip()]


def parse_character_position(cp: Optional[str]):
    """把 character_position 字符串拆成结构化角色列表。

    输入形如 `[柳拂衣_1-1-1_calm_CS(Left),石兴_1-1-2_calm_MS(Center)]`
    返回 [{key, combo, emotion, shot_size, position}, ...]
    - 括号外 = 立绘 stem（{名}_{combo}_{情绪}[_{景别}]），括号内 = 站位
    - 景别后缀为 LS/MS/CS，可能缺省（缺省时 shot_size=None）
    - 站位原样保留（Left/Center/Right 等），缺省为 Center
    None / 空串 / 无立绘 beat → []
    """
    if not cp:
        return []
    body = str(cp).strip()
    if body.startswith("["):
        body = body[1:]
    if body.endswith("]"):
        body = body[:-1]
    body = body.strip()
    if not body:
        return []

    result = []
    for entry in _split_char_entries(body):
        m = re.match(r"^(.+?)\((.+?)\)\s*$", entry)
        if m:
            stem = m.group(1).strip()
            position = m.group(2).strip()
        else:
            stem = entry.strip()
            position = "Center"
        if not stem:
            continue

        parts = stem.split("_")
        key = parts[0]
        rest = parts[1:]
        shot_size = None
        if rest and rest[-1] in _CHAR_SHOT_SIZES:
            shot_size = rest[-1]
            rest = rest[:-1]
        combo = rest[0] if rest else ""
        emotion = "_".join(rest[1:]) if len(rest) > 1 else ""

        result.append({
            "key": key,
            "combo": combo,
            "emotion": emotion,
            "shot_size": shot_size,
            "position": position,
        })
    return result


def _decode_performance_data(raw) -> Optional[dict]:
    """把 ultimate/master JSON 统一解析为 ``{node_id: [beat, ...]}``。"""
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return None
    return raw if isinstance(raw, dict) else None


def _load_performance_data(task_id: str) -> tuple[dict, str]:
    """读取演出主数据，兼容一组私有表与热插拔标准 Artifact。

    优先级：业务库 ``ultimate`` → 本地 master JSON → OSS 标准
    ``storage/outputs/{task_id}/{task_id}.json``。返回 ``(data, source)``。
    """
    try:
        with business_engine.connect() as conn:
            row = conn.execute(
                text("SELECT data FROM ultimate WHERE task_id = :tid"),
                {"tid": task_id},
            ).fetchone()
        data = _decode_performance_data(row[0] if row else None)
        if data:
            return data, "ultimate"
    except Exception as e:
        logger.warning(f"[performance] ultimate 读取失败，尝试标准 Artifact: task={task_id}: {e}")

    master_path = Path(OUTPUTS_PATH) / task_id / f"{task_id}.json"
    if master_path.is_file():
        try:
            data = _decode_performance_data(master_path.read_text(encoding="utf-8"))
            if data:
                return data, "local-master"
        except Exception as e:
            logger.warning(f"[performance] 本地 master JSON 读取失败: {master_path}: {e}")

    try:
        from common.output_mirror import fetch_output_file

        fetched = fetch_output_file(task_id, f"{task_id}.json")
        if fetched is not None and fetched.is_file():
            data = _decode_performance_data(fetched.read_text(encoding="utf-8"))
            if data:
                return data, "oss-master"
    except Exception as e:
        logger.warning(f"[performance] OSS master JSON 读取失败: task={task_id}: {e}")

    raise HTTPException(
        status_code=404,
        detail="当前管线尚未生成标准演出数据，请确认已上传 master JSON",
    )


def _persist_performance_data(task_id: str, data: dict) -> None:
    """把演出修改写回标准 master JSON，并尽力同步一组 ultimate 镜像。"""
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    master_path = Path(OUTPUTS_PATH) / task_id / f"{task_id}.json"
    master_path.parent.mkdir(parents=True, exist_ok=True)
    previous_local = master_path.read_bytes() if master_path.is_file() else None
    from time import time_ns

    temp_path = master_path.with_name(f".{master_path.name}.{os.getpid()}.{time_ns()}.tmp")
    try:
        temp_path.write_bytes(payload)
        os.replace(temp_path, master_path)
    finally:
        temp_path.unlink(missing_ok=True)

    # 本地模式下标准 Artifact 已由上面的原子写入落盘，不再根据环境变量
    # 尝试远端镜像；即使部署环境残留旧 OSS 凭证，也不能改变存储路径。

    # 一组仍有代码读取 ultimate；把标准数据镜像回去。该镜像失败不影响二组标准 Artifact。
    try:
        with business_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO ultimate (task_id, data, create_time, update_time) "
                    "VALUES (:tid, CAST(:data AS jsonb), NOW(), NOW()) "
                    "ON CONFLICT (task_id) DO UPDATE SET "
                    "data = EXCLUDED.data, update_time = NOW()"
                ),
                {"tid": task_id, "data": payload.decode("utf-8")},
            )
    except Exception as e:
        logger.warning(f"[performance] ultimate 镜像写入失败（标准 Artifact 已保存）: task={task_id}: {e}")


def _fetch_ultimate_data(task_id: str):
    """兼容旧调用名，实际读取 ultimate 或标准 master Artifact。"""
    data, _source = _load_performance_data(task_id)
    return data


def _fetch_ultimate_beat_optional(task_id: str, node_id: str, beat_id: str):
    """尽力从 ultimate 表取指定 beat；表/节点/beat 任一缺失返回 None，不抛异常。

    供演出查询做「游戏包为主、表兜底」时读取 scene_id / speaker_id 等游戏包里
    没有的结构化字段，故不能像 _fetch_ultimate_data 那样缺数据即 404。
    """
    try:
        data, _source = _load_performance_data(task_id)
    except HTTPException:
        return None
    beats = data.get(node_id)
    if not isinstance(beats, list):
        return None
    return next((b for b in beats if str(b.get("beat_id")) == str(beat_id)), None)


@app.get("/api/v1/tasks/{task_id}/performance/beat")
async def get_performance_beat(task_id: str, node_id: str, beat_id: str):
    """演出微调：查询指定 beat 的演出状态（游戏包为主，ultimate 表兜底）。

    立绘（characters）优先解析游戏包 script.js 里该 beat 当前生效的 setCast——
    这与 PATCH 热更新写入的是同一份数据，保证查询回显与游戏画面一致；游戏包不
    存在该 beat（或未构建）时回退用 ultimate 表的 character_position。
    scene_id / scene_shot_size / speaker_id / cg_mode 游戏包脚本里无结构化字段，
    仍从 ultimate 表读取，表缺失时为 None。
    beat_id 跨章节不唯一，故必须配合 node_id 定位（与 PATCH 一致）。
    旁白 / 独白 / CG / 系统面板等 beat 无立绘，characters 为空数组。
    """
    ultimate_beat = _fetch_ultimate_beat_optional(task_id, node_id, beat_id)

    # 立绘：游戏包为主
    character_position = None
    cast_source = "package"
    try:
        character_position = read_cast_from_package(task_id, node_id, beat_id, BUILT_GAMES_PATH)
    except (CastNotFoundError, CastArtifactError) as e:
        # 游戏包没有该 beat / 未构建 / 结构异常 → 回退表
        cast_source = "ultimate"
        if ultimate_beat is not None:
            character_position = ultimate_beat.get("character_position")
        logger.info(f"[performance] 游戏包无立绘，回退 ultimate: task_id={task_id} beat_id={beat_id} ({e})")

    # 游戏包和表都没有该 beat → 404
    if character_position is None and ultimate_beat is None:
        raise HTTPException(
            status_code=404, detail=f"beat 不存在: node_id={node_id} beat_id={beat_id}"
        )

    meta = ultimate_beat or {}
    return {
        "task_id": task_id,
        "node_id": node_id,
        "beat_id": meta.get("beat_id", beat_id),
        "scene_id": meta.get("scene_id"),
        "scene_shot_size": meta.get("scene_shot_size"),
        "speaker_id": meta.get("speaker_id"),
        "cg_mode": meta.get("cg_mode"),
        "characters": parse_character_position(character_position),
        "cast_source": cast_source,
    }


@app.get("/api/v1/tasks/{task_id}/performance/scenes")
async def get_performance_scenes(task_id: str, node_id: str):
    """演出微调：按场景聚合返回某章节下的 beat 分组。

    从 ultimate 表读取 node_id 章节的 beats，按 scene_id 聚合。
    同一 scene_id 若被其他场景打断后再次出现，会拆成多个片段（segment），
    保留原始出场顺序。每个片段的 beats 仅为 beat_id 字符串数组。
    例如场景A=1-3、场景B=4-5、场景A=6-8，返回三个片段：
    {"scene_id":"场景A","beats":["1","2","3"]}、
    {"scene_id":"场景B","beats":["4","5"]}、
    {"scene_id":"场景A","beats":["6","7","8"]}。
    详细演出状态由 /performance/beat 按单个 beat 查询。
    """
    data = _fetch_ultimate_data(task_id)
    beats = data.get(node_id)
    if beats is None:
        raise HTTPException(status_code=404, detail=f"章节节点不存在: {node_id}")

    segments = []
    for beat in beats:
        scene_id = beat.get("scene_id")
        beat_id = beat.get("beat_id")
        if segments and segments[-1]["scene_id"] == scene_id:
            segments[-1]["beats"].append(beat_id)
        else:
            segments.append({"scene_id": scene_id, "beats": [beat_id]})

    return {
        "task_id": task_id,
        "node_id": node_id,
        "segments": segments,
    }


_VALID_SCENE_SHOT_SIZES = {"LS", "MS", "CS"}


class UpdateSceneShotRequest(BaseModel):
    node_id: str
    beat_id: str
    scene_shot_size: str = Field(..., description="场景景别，取值 LS / MS / CS")

    @field_validator("node_id")
    @classmethod
    def _validate_node_id(cls, value: str, info):
        return normalize_resource_id(value, info.field_name)

    @field_validator("beat_id")
    @classmethod
    def _validate_beat_id(cls, value: str):
        return normalize_beat_id(value)

    @field_validator("scene_shot_size")
    @classmethod
    def _validate_shot_size(cls, value: str):
        v = value.strip().upper()
        if v not in _VALID_SCENE_SHOT_SIZES:
            raise ValueError(f"scene_shot_size 只支持 {sorted(_VALID_SCENE_SHOT_SIZES)}")
        return v


@app.patch("/api/v1/tasks/{task_id}/performance/scene-shot")
async def update_scene_shot(task_id: str, req: UpdateSceneShotRequest):
    """演出微调：调整场景景别，更新 ultimate 并刷新对应场景实例图。

    以传入 beat_id 所在的「连续 scene_id 片段」为作用域（与 /performance/scenes
    的分段口径一致）：把该片段内所有 beat 的 scene_shot_size 一起改。被其他场景
    打断后再次出现的同名场景属于另一片段，不受影响。

    图片处理规则：CS 中心裁取原图 70%，MS 中心裁取 85%，LS 使用 100%；裁取后
    统一缩放回原分辨率。裁剪源始终是保留的二级场景原图，输出同名覆盖
    storage/outputs、对应节点 built_games 游戏包与 OSS 主路径，并刷新该节点
    service-worker 缓存版本。
    """
    task_id = normalize_resource_id(task_id, "task_id")
    with Session(engine) as session:
        _ensure_task_mutable(session, task_id)
    data, performance_source = _load_performance_data(task_id)
    original_data = json.loads(json.dumps(data, ensure_ascii=False))
    beats = data.get(req.node_id)
    if not isinstance(beats, list):
        raise HTTPException(status_code=404, detail=f"章节节点不存在: {req.node_id}")

    target_idx = next(
        (i for i, b in enumerate(beats) if str(b.get("beat_id")) == req.beat_id), None
    )
    if target_idx is None:
        raise HTTPException(
            status_code=404,
            detail=f"beat 不存在: node_id={req.node_id} beat_id={req.beat_id}",
        )

    # 向前后扩展到同一 scene_id 的连续片段边界
    scene_id = beats[target_idx].get("scene_id")
    if not isinstance(scene_id, str) or not scene_id.strip():
        raise HTTPException(
            status_code=404,
            detail=f"beat 未绑定场景: node_id={req.node_id} beat_id={req.beat_id}",
        )
    scene_id = scene_id.strip()
    start = target_idx
    while start > 0 and beats[start - 1].get("scene_id") == scene_id:
        start -= 1
    end = target_idx
    while end < len(beats) - 1 and beats[end + 1].get("scene_id") == scene_id:
        end += 1

    updated_beat_ids = []
    for b in beats[start : end + 1]:
        b["scene_shot_size"] = req.scene_shot_size
        updated_beat_ids.append(b.get("beat_id"))

    # scene_prompts 是跨组标准表，可精确识别二级场景原图；一组 scenes 私有表仅作补充。
    variant_scene_ids = set()
    try:
        scene_prompt_data = _fetch_json_data(task_id, "scene_prompts", True)
        variant_scene_ids.update(
            scene_key.strip()
            for scene_key in scene_prompt_data
            if isinstance(scene_key, str) and scene_key.strip()
        )
    except Exception as e:
        logger.info(f"[scene-shot] 标准 scene_prompts 不可用，继续尝试私有数据: task={task_id}: {e}")
    try:
        with business_engine.connect() as conn:
            scenes_row = conn.execute(
                text("SELECT data FROM scenes WHERE task_id = :tid"),
                {"tid": task_id},
            ).fetchone()
        scenes_data = scenes_row[0] if scenes_row else []
        if isinstance(scenes_data, str):
            scenes_data = json.loads(scenes_data)
        variant_scene_ids.update(extract_variant_scene_ids(scenes_data))
    except Exception as e:
        logger.info(f"[scene-shot] 无一组 scenes 私有数据，使用标准 Artifact: task={task_id}: {e}")

    # 外部组不共享 PVC，按需把可能的二级原图从标准 OSS outputs 拉到本地。
    try:
        from common.output_mirror import fetch_output_file

        candidate_ids = []
        resolved = resolve_original_scene_id(scene_id, variant_scene_ids)
        candidate_ids.append(resolved)
        candidate = scene_id
        while "_" in candidate:
            base, suffix = candidate.rsplit("_", 1)
            if not suffix.isdigit():
                break
            candidate = base
            candidate_ids.append(candidate)
        candidate_ids.append(scene_id)

        for candidate in dict.fromkeys(candidate_ids):
            fetched = fetch_output_file(
                task_id,
                Path("images") / "scenes" / f"{candidate}.png",
            )
            if fetched is not None and fetched.exists() and fetched.stat().st_size > 0:
                variant_scene_ids.add(candidate)
                break
    except Exception as e:
        logger.warning(f"[scene-shot] 标准场景原图补齐失败: task={task_id} scene={scene_id}: {e}")

    # 先持久化标准 master；图片热更新失败时把 master 恢复到操作前版本。
    _persist_performance_data(task_id, data)
    try:
        image_result = apply_scene_shot_image(
            task_id=task_id,
            node_id=req.node_id,
            scene_id=scene_id,
            shot_size=req.scene_shot_size,
            known_variant_ids=variant_scene_ids,
            outputs_path=OUTPUTS_PATH,
            built_games_path=BUILT_GAMES_PATH,
        )
    except (SceneShotUploadError, SceneShotUpdateError) as exc:
        try:
            _persist_performance_data(task_id, original_data)
        except Exception:
            logger.exception(f"[scene-shot] 图片失败后恢复 master 数据也失败: task={task_id}")
        status_code = 502 if isinstance(exc, SceneShotUploadError) else 404
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    logger.info(
        f"[scene-shot] 场景景别已更新: task_id={task_id} node_id={req.node_id} "
        f"scene_id={scene_id} original_scene_id={image_result.original_scene_id} "
        f"shot={req.scene_shot_size} ratio={image_result.crop_ratio} "
        f"beats={updated_beat_ids} oss={image_result.oss_key}"
    )
    return {
        "status": "ok",
        "ultimate_updated": True,
        "performance_source": performance_source,
        "task_id": task_id,
        "node_id": req.node_id,
        "built_node_id": image_result.built_node_id,
        "scene_id": scene_id,
        "scene_shot_size": req.scene_shot_size,
        "crop_ratio": image_result.crop_ratio,
        "original_scene_id": image_result.original_scene_id,
        "image_path": image_result.local_path,
        "built_image_path": image_result.built_image_path,
        "service_worker_path": image_result.service_worker_path,
        "service_worker_updated": image_result.service_worker_updated,
        "oss_key": image_result.oss_key,
        "mirror_uploaded": image_result.mirror_uploaded,
        "updated_beat_ids": updated_beat_ids,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
