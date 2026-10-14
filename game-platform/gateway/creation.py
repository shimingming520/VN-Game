"""创作 API:中台代理转发到 novel-game-factory 的 api_gateway。

为什么代理而不让前端直连:api_gateway 自身没有用户鉴权(端点全裸),直接暴露给浏览器
等于谁都能发起创作。这里先确认调用方身份和任务归属,再带 X-Service-Token 转发,
用户身份由中台保证,api_gateway 只信任来自中台的调用。

调用方有两类(都由 get_task_owner 统一处理,见 auth/deps.py):
- 浏览器直连:带平台 JWT,归属取自 JWT 里的用户。
- creation-engine 代理:引擎有独立的 JWT 密钥(中台解不开也不该解),所以它作为
  可信内部服务带 X-Service-Token,并用 X-Platform-User-Uid 显式声明归属人。
无论哪类,请求里自带的 user_id/user_name 都会被剥掉,归属只认鉴权结果。

两类入口:
1. 简单入口 `/novels`:批量提交,提交后一路跑到底,无人工确认卡点。
2. 单文件人工确认流程:upload → config → confirm → 素材/演绎 → 打包,
   浏览器 → creation-engine → 本文件 → NOVEL_API_URL。
   这一段端点多且几乎全是转发,所以除了要塞用户身份的 upload 之外,
   统一走下面的通用转发,但只放行白名单里的 (method, path) 组合 ——
   裸 catch-all 等于把 api_gateway 整个暴露出去,包括删除、清理这类破坏性端点。
"""
import json
import re
from typing import List, Optional

import requests
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from auth.deps import TaskOwner, get_task_owner
from common.config import NOVEL_API_URL, PLATFORM_SERVICE_TOKEN

router = APIRouter(prefix="/api/v1/creation", tags=["creation"])

# 一次批量最多 50 本,和 api_gateway 侧限制保持一致
_MAX_NOVELS = 50
# 生成是长任务,但这里只等「提交」返回,不等跑完。批量上传还要经过
# api_gateway 的整批校验、原文持久化和建单，和外层 Nginx/Ingress 统一留 300 秒。
_SUBMIT_TIMEOUT_SEC = 300

# 前端不得自己声明身份:这几个字段一律由中台按 JWT 覆盖(见 _identity_of)
_IDENTITY_FIELDS = {"user_id", "user_name"}
# 转发时只带这几个响应头回去:raw-novel 是文件下载,少了 disposition 前端存不出文件名
_PASSTHROUGH_RESPONSE_HEADERS = ("content-disposition",)

# 路径里的 ID 段(task_id 形如 拼音_8位uuid,build_id/regenerate_id 是 uuid)
_SEG = r"[A-Za-z0-9_.\-]+"

# 白名单:下游 api_gateway 的路径模板,{} 处匹配单个 ID 段。
# 只列人工确认流程真正用到的端点;删除/清理/软删恢复这类破坏性操作不放行,
# 需要时走中台自己的管理端点(带审计),不从创作代理透出。
_ALLOWED_PATHS: dict[str, tuple[str, ...]] = {
    "GET": (
        "models/options",
        "models/image-options",
        "tasks",
        "tasks/new",
        "tasks/published",
        "tasks/unpublished",
        "tasks/participated",
        "tasks/status-counts",
        "tasks/{}/status",
        "tasks/{}/entry",
        "tasks/{}/generation-params",
        "tasks/{}/build/{}/status",
        "tasks/{}/performance/beat",
        "tasks/{}/performance/scenes",
        "tasks/{}/characters",
        "tasks/{}/scenes",
        "story/similar-config",
        "story/{}/summary",
        "story/{}/outline",
        "story/{}/adaptation",
        "story/{}/tree",
        "story/{}/raw-novel",
        "story/{}/script-preview",
        "story/{}/plots-status",
        "materials/audio_producer/voice/recommend",
        "materials/audio_producer/voice/audition",
        "materials/regenerate/{}/status",
        "materials/{}/regen/history",
    ),
    "POST": (
        "story/{}/config",
        "story/{}/adaptation",
        "story/{}/confirm",
        "story/{}/summary/regenerate",
        "story/{}/generate-name",
        "tasks/{}/confirm-material",
        "tasks/{}/confirm-performance",
        "tasks/{}/build",
        "tasks/{}/export",
        "materials/char_designer/regenerate",
        "materials/scene_painter/regenerate",
        "materials/audio_producer/regenerate",
        "materials/audio_producer/voice/regenerate",
        "materials/sound_arranger/regenerate",
        "materials/{}/regen/activate",
        "materials/{}/regen/rollback",
    ),
    "PATCH": (
        "tasks/{}/performance/scene-shot",
        "game/cast",
    ),
}

_ALLOWED_PATTERNS: dict[str, tuple[re.Pattern, ...]] = {
    method: tuple(
        re.compile("^" + re.escape(tpl).replace(r"\{\}", _SEG) + "$")
        for tpl in templates
    )
    for method, templates in _ALLOWED_PATHS.items()
}


def _identity_of(owner: TaskOwner) -> dict:
    """归属字段转成下游要的表单/查询参数。归属只有一个来源:get_task_owner
    (平台 JWT 或可信内部服务的显式声明),前端自带的同名字段一律先被剥掉。"""
    return {"user_id": owner.user_id, "user_name": owner.user_name}


def _is_path_allowed(method: str, path: str) -> bool:
    # ".." 段单独挡掉:它能匹配 ID 段的字符集,拼出去后被 requests 规范化成另一条路径。
    # 目前推演不出能借此打到破坏性端点(那些要么是 DELETE、要么后缀对不上),
    # 但白名单以「拼出的 URL 就是模板本身」为前提,这个前提得守住。
    if ".." in path.split("/"):
        return False
    return any(p.match(path) for p in _ALLOWED_PATTERNS.get(method, ()))


def _forward(
    method: str,
    path: str,
    *,
    params: Optional[list] = None,
    json_body=None,
    data=None,
    files=None,
    headers: Optional[dict] = None,
) -> Response:
    """阻塞版转发。调用方必须放线程池(见 _forward_async 的说明)。"""
    # 先按大小写不敏感的 HTTP 语义剥掉调用方提供的服务令牌，再由中台注入。
    # 普通 dict 区分大小写，不能只覆盖精确拼写，否则将来传入
    # ``x-service-token`` 时会同时保留两个同名 HTTP 头的字典键。
    forward_headers = {
        key: value
        for key, value in (headers or {}).items()
        if key.lower() != "x-service-token"
    }
    forward_headers["X-Service-Token"] = PLATFORM_SERVICE_TOKEN
    try:
        resp = requests.request(
            method,
            f"{NOVEL_API_URL}/api/v1/{path}",
            params=params,
            json=json_body,
            data=data,
            files=files,
            headers=forward_headers,
            timeout=_SUBMIT_TIMEOUT_SEC,
        )
    except requests.RequestException as e:
        raise HTTPException(502, f"创作服务不可达: {e}")

    if resp.status_code >= 400:
        # 把下游的状态码和 detail 原样带出来,不然前端只能看到一个 502
        raise HTTPException(resp.status_code, _detail_of(resp))

    out_headers = {
        k: v
        for k, v in resp.headers.items()
        if k.lower() in _PASSTHROUGH_RESPONSE_HEADERS
    }
    # 原样回传字节:JSON 端点前端照常解析,raw-novel 这种文件下载也不会被破坏
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("Content-Type"),
        headers=out_headers,
    )


async def _forward_async(method: str, path: str, **kwargs) -> Response:
    # 必须放线程池:requests 是阻塞的,直接在 async 端点里调会占死事件循环。
    # 而这条链路是环形的 —— api_gateway 收到请求后要回头调中台解析管线、提交编排,
    # 循环被占住就 accept 不了那些回调,双方一起等到超时。
    return await run_in_threadpool(lambda: _forward(method, path, **kwargs))


@router.post("/novels")
async def create_novels(
    request: Request,
    novels: List[UploadFile] = File(..., description="小说 .txt 文件,最多 50 本"),
    mode: Optional[str] = Form(default=None, description="创作模式 efficiency/balanced/quality"),
    pipeline_name: Optional[str] = Form(default=None, description="直接指定管线,优先于 mode"),
    output_language: str = Form(default="zh-CN"),
    art_style: str = Form(default="ink_chinese"),
    story_mode: str = Form(default="original"),
    gameplay: str = Form(default="interactive_story"),
    text_model: Optional[str] = Form(default=None),
    image_model: Optional[str] = Form(default=None),
    audio_model: Optional[str] = Form(default=None),
    resolution: Optional[str] = Form(default=None),
    batch_id: Optional[str] = Form(default=None, description="批次幂等键"),
    owner: TaskOwner = Depends(get_task_owner),
):
    """提交创作(简单入口):转发给 api_gateway 的批量端点,任务按管线跑到底。"""
    if not novels:
        raise HTTPException(400, "至少上传 1 本小说")
    if len(novels) > _MAX_NOVELS:
        raise HTTPException(400, f"单次最多 {_MAX_NOVELS} 本")

    data = {
        "output_language": output_language,
        "art_style": art_style,
        "story_mode": story_mode,
        "gameplay": gameplay,
        # 用户身份由统一鉴权依赖决定,不接受请求体自己传,避免冒用
        **_identity_of(owner),
    }
    for key, value in (
        ("mode", mode),
        ("pipeline_name", pipeline_name),
        ("text_model", text_model),
        ("image_model", image_model),
        ("audio_model", audio_model),
        ("resolution", resolution),
        ("batch_id", batch_id),
    ):
        if value:
            data[key] = value

    files = [
        ("novels", (f.filename, await f.read(), f.content_type or "text/plain"))
        for f in novels
    ]
    idempotency_key = (request.headers.get("Idempotency-Key") or "").strip()
    headers = (
        {"Idempotency-Key": idempotency_key}
        if idempotency_key
        else None
    )
    return await _forward_async(
        "POST",
        "novels/batch",
        data=data,
        files=files,
        headers=headers,
    )


@router.post("/story/upload")
async def upload_single_novel(
    novel: UploadFile = File(..., description="小说 .txt 文件,最大 50MB"),
    mode: Optional[str] = Form(default=None, description="创作模式 efficiency/balanced/quality"),
    pipeline_name: Optional[str] = Form(default=None, description="直接指定管线,优先于 mode"),
    output_language: str = Form(default="zh-CN"),
    art_style: Optional[str] = Form(default=None),
    story_mode: str = Form(default="original"),
    gameplay: str = Form(default="interactive_story"),
    text_model: Optional[str] = Form(default=None),
    image_model: Optional[str] = Form(default=None),
    audio_model: Optional[str] = Form(default=None),
    resolution: Optional[str] = Form(default=None),
    owner: TaskOwner = Depends(get_task_owner),
):
    """上传剧本(人工确认流程第一步):只落库 + 触发摘要/起名,不启动生成。

    管线在这一步定下并由下游落库,confirm 时不再传 —— 用户可能隔很久才确认,
    那时中台的「模式→管线」映射未必还是提交时选的那条。
    显式声明而不走通用转发:multipart 里要塞中台侧的用户身份,
    通用转发对 multipart 一律拒绝(见 proxy),免得前端夹带 user_id 冒用他人。
    """
    data = dict(_identity_of(owner))
    for key, value in (
        ("mode", mode),
        ("pipeline_name", pipeline_name),
        ("output_language", output_language),
        ("art_style", art_style),
        ("story_mode", story_mode),
        ("gameplay", gameplay),
        ("text_model", text_model),
        ("image_model", image_model),
        ("audio_model", audio_model),
        ("resolution", resolution),
    ):
        if value:
            data[key] = value

    files = [("novel", (novel.filename, await novel.read(), novel.content_type or "text/plain"))]
    return await _forward_async("POST", "story/upload", data=data, files=files)


@router.api_route("/{path:path}", methods=["GET", "POST", "PATCH"])
async def proxy(
    path: str,
    request: Request,
    owner: TaskOwner = Depends(get_task_owner),
):
    """人工确认流程的通用转发:白名单内的 (method, path) 原样转给 api_gateway。

    身份处理:查询串和 JSON body 里调用方自带的 user_id/user_name 一律丢掉,
    再按鉴权结果重新塞进查询串(下游忽略它不认的查询参数,认的地方正好按本人过滤)。
    """
    if not _is_path_allowed(request.method, path):
        raise HTTPException(404, f"未开放的创作接口: {request.method} /{path}")

    identity = _identity_of(owner)
    params = [
        (k, v)
        for k, v in request.query_params.multi_items()
        if k not in _IDENTITY_FIELDS
    ]
    params.extend(identity.items())

    if request.method == "GET":
        return await _forward_async("GET", path, params=params)

    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type in ("multipart/form-data", "application/x-www-form-urlencoded"):
        # 表单体里的字段没法在不重编码的前提下过滤,而 user_id 就藏在里面 ——
        # 需要传文件的端点单独显式实现(见 upload_single_novel)
        raise HTTPException(415, "该接口只接受 JSON 请求体")

    body = await request.body()
    if not body:
        return await _forward_async(request.method, path, params=params)

    try:
        parsed = json.loads(body)
    except ValueError:
        raise HTTPException(400, "请求体必须是 JSON")
    if isinstance(parsed, dict):
        # 重生成那几个端点把 user_id/user_name 声明成必填 body 字段,只塞查询串会 422。
        # 所以剥掉调用方自带的之后,按鉴权结果重新塞回 body —— 下游模型忽略它不认的
        # 多余字段,认的地方正好拿到可信的操作人。
        parsed = {k: v for k, v in parsed.items() if k not in _IDENTITY_FIELDS}
        parsed.update(identity)
    return await _forward_async(request.method, path, params=params, json_body=parsed)


def _detail_of(resp) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:500]
    if isinstance(body, dict):
        return str(body.get("detail") or body)
    return str(body)
