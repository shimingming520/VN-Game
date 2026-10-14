"""创作 API:创作引擎代理转发到中台(game-platform)的 /api/v1/creation。

为什么再套一层代理:创作服务(novel-game-factory api_gateway)自身没有用户鉴权,
中台已经在它前面挡了一道。创作引擎自己的前端只认引擎签发的 JWT,所以这里
先用引擎的 get_current_user 校验用户,再把请求转给中台,浏览器永远不直连创作服务。

对中台的鉴权走服务令牌 + 显式用户归属(见 _upstream_auth):引擎和中台各自签 JWT、
密钥不同,转发用户 token 会被中台 401;拉平密钥又会让引擎 token 自动获得中台权限。
所以用 X-Service-Token 证明调用方是引擎,X-Platform-User-Uid 声明任务归属。
令牌不回写给浏览器:响应只透传状态码、Content-Type 和 body。
"""
import re
from typing import List, Optional
from urllib.parse import quote

import requests
from fastapi import (
    APIRouter, Depends, File, Form, Header, HTTPException, Request, Response, UploadFile,
)
from starlette.concurrency import run_in_threadpool

import uuid

from sqlmodel import Session, select

from auth.deps import get_current_user
from auth.token_service import get_valid_access_token
from common.config import (
    GAME_BASE_URL, PLATFORM_API_URL, PLATFORM_SERVICE_TOKEN,
)
from common.models import NovelTask, PlatformUser, engine
from common.local_storage import local_asset_url, local_path_for_key


def _owned_task(task_id: str, user: PlatformUser) -> NovelTask:
    """归属校验:换个 task_id 不能读到别人作品的发布信息。

    与 gateway/projects.py 的 _owned 同一口径 —— 越权按 404 而非 403,
    否则等于白送一个「这个 task_id 存在」的探测接口。
    """
    with Session(engine) as session:
        task = session.exec(select(NovelTask).where(NovelTask.task_id == task_id)).first()
    if (
        task is None
        or task.user_id != user.main_site_uid
        or task.is_deleted
        or getattr(task, "is_purged", False)
    ):
        raise HTTPException(404, "作品不存在")
    return task

router = APIRouter(prefix="/api/v1/creation", tags=["creation"])

# 中台创作代理的基地址,本文件所有转发都落在这个前缀下
_UPSTREAM_BASE = f"{PLATFORM_API_URL}/api/v1/creation"

# 一次批量最多 50 本,与中台/创作服务两侧限制保持一致
_MAX_NOVELS = 50
# 只等「提交」返回,不等生成跑完。30 本大文件还要经过中台并完成批量建单，
# 与两层 Nginx/Ingress 的上传超时保持一致，避免外层仍在等、这一层先报 502。
_UPLOAD_TIMEOUT_SEC = 300
_DEFAULT_TIMEOUT_SEC = 60
_INTERACTIVE_WRITE_TIMEOUT_SEC = 180
# CMS 选项查询/新增在中台内还会继续请求 CMS，内层最长等待 120 秒。
# 代理层必须留出响应转发余量，不能沿用默认 60 秒。
_CMS_OPTIONS_PROXY_TIMEOUT_SEC = 150

# ── 白名单 ──
# 代理是「把浏览器请求原样带到内网服务」,不设白名单等于把中台整个 API 面暴露出去。
# 这里按 taskCreation 前端实际调用的路径逐条放行,路径参数只允许 task_id 这类安全字符,
# 不允许 .. 或斜杠穿越到别的前缀。
_SEG = r"[A-Za-z0-9_.\-]+"
_ALLOWED = [
    # 剧本上传与四步信息确认
    ("POST", rf"^story/upload$"),
    ("POST", rf"^story/upload-url$"),
    ("GET", rf"^story/similar-config$"),
    ("GET", rf"^story/{_SEG}/raw-novel$"),
    ("GET", rf"^story/{_SEG}/summary$"),
    ("POST", rf"^story/{_SEG}/summary/regenerate$"),
    ("POST", rf"^story/{_SEG}/config$"),
    ("POST", rf"^story/{_SEG}/adaptation$"),
    ("GET", rf"^story/{_SEG}/outline$"),
    ("POST", rf"^story/{_SEG}/generate-name$"),
    ("POST", rf"^story/{_SEG}/confirm$"),
    ("GET", rf"^story/{_SEG}/tree$"),
    # 任务状态 / 构建 / 导出
    ("GET", rf"^tasks/status-counts$"),
    ("GET", rf"^tasks/{_SEG}/status$"),
    # entry 是「该跳到第几步」的唯一权威来源,创作页刷新恢复现场就靠它
    ("GET", rf"^tasks/{_SEG}/entry$"),
    ("GET", rf"^tasks/{_SEG}/generation-params$"),
    ("POST", rf"^tasks/{_SEG}/build$"),
    ("GET", rf"^tasks/{_SEG}/build/{_SEG}/status$"),
    ("POST", rf"^tasks/{_SEG}/export$"),
    ("DELETE", rf"^tasks/{_SEG}$"),
    # 人物场景确认 / 演绎微调
    ("POST", rf"^tasks/{_SEG}/confirm-material$"),
    ("POST", rf"^tasks/{_SEG}/confirm-performance$"),
    ("GET", rf"^tasks/{_SEG}/performance/scenes$"),
    ("GET", rf"^tasks/{_SEG}/performance/beat$"),
    ("PATCH", rf"^tasks/{_SEG}/performance/scene-shot$"),
    # 角色 / 场景素材清单(第 2 步的缩略图墙,清单以 OSS 运行时路径为准)
    ("GET", rf"^tasks/{_SEG}/characters$"),
    ("GET", rf"^tasks/{_SEG}/scenes$"),
    # 素材重生成与版本管理
    ("GET", rf"^materials/{_SEG}/regen/history$"),
    ("POST", rf"^materials/{_SEG}/regen/activate$"),
    ("POST", rf"^materials/{_SEG}/regen/rollback$"),
    ("POST", rf"^materials/char_designer/regenerate$"),
    ("POST", rf"^materials/scene_painter/regenerate$"),
    ("GET", rf"^materials/audio_producer/voice/recommend$"),
    ("POST", rf"^materials/audio_producer/voice/audition$"),
    ("POST", rf"^materials/audio_producer/voice/regenerate$"),
    ("GET", rf"^materials/regenerate/{_SEG}/status$"),
    # 可选模型清单 / 立绘热更新
    ("GET", rf"^models/options$"),
    ("GET", rf"^models/image-options$"),
    ("PATCH", rf"^game/cast$"),
]
_ALLOWED_COMPILED = [(m, re.compile(p)) for m, p in _ALLOWED]


def _check_allowed(method: str, subpath: str):
    if ".." in subpath:
        raise HTTPException(400, "非法路径")
    for allowed_method, pattern in _ALLOWED_COMPILED:
        if allowed_method == method and pattern.match(subpath):
            return
    raise HTTPException(404, f"创作接口未开放: {method} {subpath}")


def _upstream_auth(authorization: Optional[str], user: PlatformUser) -> dict:
    """转发用的鉴权头:服务令牌 + 显式用户归属。

    不转发用户自己的 Authorization —— 引擎和中台各自签 JWT、密钥不同,中台解不开
    引擎的 token 会直接 401;把两边密钥拉平也不行,那等于引擎签发的 token 自动获得
    中台权限。所以这里用服务令牌证明「调用方是引擎」,再用归属头声明「这活算谁的」,
    中台按 uid 把任务记到人头上(见中台 auth/deps.py 的 get_task_owner)。

    用户身份已由本文件各端点的 get_current_user 校验过,authorization 只用来判断
    请求确实带了登录态。
    """
    if not user.main_site_uid:
        raise HTTPException(400, "当前账号缺少主站 uid,无法创建作品")
    if not PLATFORM_SERVICE_TOKEN:
        raise HTTPException(503, "未配置 PLATFORM_SERVICE_TOKEN,无法调用中台创作接口")
    return {
        "X-Service-Token": PLATFORM_SERVICE_TOKEN,
        "X-Platform-User-Uid": user.main_site_uid,
        "X-Platform-User-Name": quote(user.username or "", safe=""),
    }


def _detail_of(resp) -> str:
    """把下游错误原样带出来,不然前端只看到一个干巴巴的 502。"""
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:500]
    if isinstance(body, dict):
        return str(body.get("detail") or body)
    return str(body)


def _relay(resp) -> Response:
    """透传下游响应:只带状态码、Content-Type 和 body。

    响应头整体不转发 —— 里面可能有 Set-Cookie / WWW-Authenticate 之类的凭证痕迹,
    转给浏览器等于把内网服务的鉴权状态漏出去。
    """
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, _detail_of(resp))
    passthrough_headers = {}
    if resp.headers.get("Content-Disposition"):
        passthrough_headers["Content-Disposition"] = resp.headers["Content-Disposition"]
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("Content-Type", "application/json"),
        headers=passthrough_headers,
    )


@router.get("/modes")
async def list_creation_modes(
    authorization: Optional[str] = Header(default=None),
    user: PlatformUser = Depends(get_current_user),
):
    """创作模式清单(效率/均衡/高质量),给上传页的模式选择用。

    中台把模式配置放在管线模块(/api/v1/pipelines/creation-modes),这里只把
    对外要用的 mode/label 挑出来:模式绑哪条管线是运维口径,创作用户不需要看见。
    """
    headers = _upstream_auth(authorization, user)
    try:
        resp = await run_in_threadpool(
            lambda: requests.get(
                f"{PLATFORM_API_URL}/api/v1/pipelines/creation-modes",
                headers=headers,
                timeout=_DEFAULT_TIMEOUT_SEC,
            )
        )
    except requests.RequestException as e:
        raise HTTPException(502, f"中台不可达: {e}")

    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, _detail_of(resp))
    try:
        rows = resp.json()
    except ValueError:
        raise HTTPException(502, "中台返回了非 JSON 响应")
    if not isinstance(rows, list):
        return rows
    return [
        {
            "mode": row.get("mode"),
            "label": row.get("label"),
            "pipeline_name": row.get("pipeline_name"),
            "available": bool(row.get("pipeline_exists")),
        }
        for row in rows
        if isinstance(row, dict)
    ]


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
    authorization: Optional[str] = Header(default=None),
    user: PlatformUser = Depends(get_current_user),
):
    """提交创作(简单入口):转给中台的批量端点,任务按管线一路跑到底。"""
    if not novels:
        raise HTTPException(400, "至少上传 1 本小说")
    if len(novels) > _MAX_NOVELS:
        raise HTTPException(400, f"单次最多 {_MAX_NOVELS} 本")

    data = {
        "output_language": output_language,
        "art_style": art_style,
        "story_mode": story_mode,
        "gameplay": gameplay,
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

    headers = _upstream_auth(authorization, user)
    # 只透传明确允许的幂等头；服务令牌和用户归属仍完全由 _upstream_auth 生成，
    # 调用方不能借转发覆盖内部鉴权。相同批次重试必须沿整条链复用同一 key。
    idempotency_key = (request.headers.get("Idempotency-Key") or "").strip()
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    if "X-Service-Token" in headers:
        # 服务令牌模式下中台没有用户上下文,归属必须由我们显式给出。
        # 值取自本地校验过的 JWT,不接受前端自己传,避免冒用他人身份。
        data["user_id"] = user.main_site_uid
        data["user_name"] = user.username

    files = [
        ("novels", (f.filename, await f.read(), f.content_type or "text/plain"))
        for f in novels
    ]

    # 必须放线程池:requests 是阻塞的,直接在 async 端点里调会占死事件循环。
    # 这条链路是环形的(中台收到请求后会回头调创作服务、创作服务再回调中台),
    # 循环被占住就 accept 不了回调,双方一起等到超时。
    try:
        resp = await run_in_threadpool(
            lambda: requests.post(
                f"{_UPSTREAM_BASE}/novels",
                data=data,
                files=files,
                headers=headers,
                timeout=_UPLOAD_TIMEOUT_SEC,
            )
        )
    except requests.RequestException as e:
        raise HTTPException(502, f"中台不可达: {e}")
    return _relay(resp)


@router.post("/story/upload")
async def upload_single_story(
    novel: UploadFile = File(..., description="小说 .txt 文件,最大 5MB"),
    mode: Optional[str] = Form(default=None, description="创作模式 efficiency/balanced/quality"),
    pipeline_name: Optional[str] = Form(default=None, description="直接指定管线,优先于 mode"),
    output_language: str = Form(default="zh-CN"),
    art_style: Optional[str] = Form(default=None),
    story_mode: str = Form(default="original"),
    gameplay: Optional[str] = Form(default=None),
    text_model: Optional[str] = Form(default=None),
    image_model: Optional[str] = Form(default=None),
    audio_model: Optional[str] = Form(default=None),
    resolution: Optional[str] = Form(default=None),
    authorization: Optional[str] = Header(default=None),
    user: PlatformUser = Depends(get_current_user),
):
    """上传单个剧本(四步流程第一步):只落库 + 触发摘要/起名,不启动生成。

    必须显式实现、不能落到下面的通用转发:通用转发按原始字节透传 body,而中台的
    创作代理对 multipart 一律拒收(它没法在不重编码的前提下剥掉前端夹带的 user_id),
    结果就是 500/415。这里把表单拆开重建,并按本地校验过的 JWT 补上任务归属。
    """
    data = {}
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

    headers = _upstream_auth(authorization, user)
    # 归属只认本地校验结果,不接受前端自己传,避免冒用他人身份
    data["user_id"] = user.main_site_uid
    data["user_name"] = user.username

    files = [("novel", (novel.filename, await novel.read(), novel.content_type or "text/plain"))]

    try:
        resp = await run_in_threadpool(
            lambda: requests.post(
                f"{_UPSTREAM_BASE}/story/upload",
                data=data,
                files=files,
                headers=headers,
                timeout=_UPLOAD_TIMEOUT_SEC,
            )
        )
    except requests.RequestException as e:
        raise HTTPException(502, f"中台不可达: {e}")
    return _relay(resp)


@router.get("/runtime-config")
def get_runtime_config(user: PlatformUser = Depends(get_current_user)):
    """前端需要的运行时配置。

    章节 iframe 地址由前端按 game_base_url 拼,不能写死在前端包里 ——
    测试和生产的游戏域名不同,写死会让其中一套环境全是坏链。
    """
    return {"game_base_url": GAME_BASE_URL}


# ── 发布相关的中台代理 ──
#
# 这三个 GET 与封面上传都是发布表单要用的。它们不在创作服务(api_gateway)上,
# 而在中台的 /admin 下,所以不能走上面那套 _UPSTREAM_BASE 白名单转发。


def _cms_token_of(user: PlatformUser) -> str:
    """用户的 CMS token。

    中台的 CMS 代理端点在 service-token 调用下拿不到用户 OAuth token
    (那份 token 是用引擎自己的 TOKEN_ENCRYPT_KEY 加密的,中台密钥解不开),
    所以由引擎解密后显式带过去 —— 与 gateway/projects.py 同一套做法。
    """
    with Session(engine) as session:
        return get_valid_access_token(session, user)


def _admin_request(
    method: str, path: str, user: PlatformUser,
    params: Optional[dict] = None, json_body: Optional[dict] = None,
    with_cms_token: bool = False,
    timeout_sec: int = _DEFAULT_TIMEOUT_SEC,
) -> Response:
    if not PLATFORM_SERVICE_TOKEN:
        raise HTTPException(503, "未配置 PLATFORM_SERVICE_TOKEN,无法调用中台接口")
    headers = {"X-Service-Token": PLATFORM_SERVICE_TOKEN}
    if user.main_site_uid:
        headers["X-Platform-User-Uid"] = user.main_site_uid
    merged_params = dict(params or {})
    if with_cms_token:
        merged_params["cms_token"] = _cms_token_of(user)
    try:
        resp = requests.request(
            method, f"{PLATFORM_API_URL}{path}", headers=headers,
            params=merged_params or None, json=json_body,
            timeout=timeout_sec,
        )
    except requests.RequestException as e:
        raise HTTPException(502, f"中台不可达: {e}")
    return _relay(resp)


@router.get("/publish-info/{task_id}")
async def get_publish_info(
    task_id: str,
    user: PlatformUser = Depends(get_current_user),
):
    """发布表单预填数据(作品名/自动封面/章节结构/游戏链接)。"""
    _owned_task(task_id, user)
    return await run_in_threadpool(
        lambda: _admin_request("GET", f"/api/v1/admin/tasks/{task_id}/publish-info", user)
    )


@router.get("/cms/tags")
async def list_cms_tags(user: PlatformUser = Depends(get_current_user)):
    return await run_in_threadpool(
        lambda: _admin_request(
            "GET", "/api/v1/admin/cms/tags", user,
            with_cms_token=True,
            timeout_sec=_CMS_OPTIONS_PROXY_TIMEOUT_SEC,
        )
    )


@router.get("/cms/classes")
async def list_cms_classes(user: PlatformUser = Depends(get_current_user)):
    return await run_in_threadpool(
        lambda: _admin_request(
            "GET", "/api/v1/admin/cms/classes", user,
            with_cms_token=True,
            # 中台内部还要调 CMS 的 POST 查询，允许 120 秒；外层必须更长，
            # 否则中台仍在等待时创作引擎会先报“分类加载失败”。
            timeout_sec=_CMS_OPTIONS_PROXY_TIMEOUT_SEC,
        )
    )


@router.post("/cms/tags")
async def create_cms_tag(
    body: dict,
    user: PlatformUser = Depends(get_current_user),
):
    """发布表单里内联新增标签。"""
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "标签名不能为空")
    return await run_in_threadpool(
        lambda: _admin_request(
            "POST", "/api/v1/admin/cms/tags", user,
            json_body={"id": 0, "name": name, "status": 1},
            with_cms_token=True,
            timeout_sec=_CMS_OPTIONS_PROXY_TIMEOUT_SEC,
        )
    )


_COVER_MAX_BYTES = 5 * 1024 * 1024
_COVER_ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp"}


def _save_publish_cover_local(user_uid: str, content: bytes, ext: str) -> str:
    key = f"publish_covers/{user_uid or 'anon'}/{uuid.uuid4().hex}{ext}"
    path = local_path_for_key(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return local_asset_url(key)


@router.post("/publish-cover")
async def upload_publish_cover(
    file: UploadFile = File(...),
    user: PlatformUser = Depends(get_current_user),
):
    """发布封面保存到本地 storage，返回本地素材 URL 填进发布表单的 cover 字段。

    文件名不参与本地 key(用 uuid)，避免用户传的名字带路径分隔符写到别的前缀下。
    """
    ext = ""
    if file.filename and "." in file.filename:
        ext = "." + file.filename.rsplit(".", 1)[-1].lower()
    if ext not in _COVER_ALLOWED_EXT:
        raise HTTPException(400, "封面只支持 png / jpg / jpeg / webp")

    content = await file.read()
    if not content:
        raise HTTPException(400, "封面文件为空")
    if len(content) > _COVER_MAX_BYTES:
        raise HTTPException(400, "封面不能超过 5MB")

    try:
        url = await run_in_threadpool(
            _save_publish_cover_local,
            user.main_site_uid,
            content,
            ext,
        )
    except Exception as e:
        raise HTTPException(500, f"本地封面保存失败: {e}")
    return {"url": url}


@router.api_route("/{subpath:path}", methods=["GET", "POST", "PATCH", "DELETE"])
async def proxy_creation(
    subpath: str,
    request: Request,
    authorization: Optional[str] = Header(default=None),
    user: PlatformUser = Depends(get_current_user),
):
    """通用转发(严格白名单):覆盖 taskCreation 那套四步创作/素材重生成/构建导出接口。

    body 按原始字节转发并沿用原 Content-Type,所以 JSON 和 multipart 都不用分别处理。
    """
    method = request.method.upper()
    _check_allowed(method, subpath)

    # 这两个端点读取的是源作品本身，不能只靠“请求已登录”就放行：
    # 换一个 task_id / source_task_id 不应拿到别人的配置或原始小说。
    if method == "GET" and subpath == "story/similar-config":
        source_task_id = (request.query_params.get("source_task_id") or "").strip()
        if not source_task_id:
            raise HTTPException(400, "缺少 source_task_id")
        _owned_task(source_task_id, user)
    else:
        raw_novel_match = re.fullmatch(rf"story/({_SEG})/raw-novel", subpath)
        if method == "GET" and raw_novel_match:
            _owned_task(raw_novel_match.group(1), user)

    headers = _upstream_auth(authorization, user)
    if "X-Service-Token" in headers:
        headers["X-Platform-User-Uid"] = user.main_site_uid
    body = await request.body()
    content_type = request.headers.get("Content-Type")
    if body and content_type:
        headers["Content-Type"] = content_type
    accept = request.headers.get("Accept")
    if accept:
        headers["Accept"] = accept

    if body and len(body) > 1024 * 1024:
        timeout = _UPLOAD_TIMEOUT_SEC
    elif method in {"POST", "PATCH"}:
        # 重生成提交、景别调整和立绘热更新可能需要下游同步写文件/刷新游戏包。
        # 前端已允许等待 180 秒，这一层不能仍在 60 秒时提前断开并伪装成 502。
        timeout = _INTERACTIVE_WRITE_TIMEOUT_SEC
    else:
        timeout = _DEFAULT_TIMEOUT_SEC
    url = f"{_UPSTREAM_BASE}/{subpath}"
    params = list(request.query_params.multi_items())

    try:
        resp = await run_in_threadpool(
            lambda: requests.request(
                method,
                url,
                params=params,
                data=body if body else None,
                headers=headers,
                timeout=timeout,
            )
        )
    except requests.RequestException as e:
        raise HTTPException(502, f"中台不可达: {e}")
    return _relay(resp)


# ── 部署前置条件 ──
# 1. PLATFORM_SERVICE_TOKEN 必须与中台 game-platform 的同名值完全一致,且两边都非空;
#    引擎侧 k8s secret 默认留空,不注入则本模块所有端点返回 503。
# 2. 用户须有 main_site_uid(CMS uid)。任务归属按它落 novel_tasks.user_id,
#    「我的项目」也按它过滤,所以缺 uid 的账号不允许创建作品。
