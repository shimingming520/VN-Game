"""CMS 发布对接:解析 story_tree_json 预填表单 + 调 CMS /business/game/set 发布游戏。

流程:
  前端任务列表点"发布" → GET  /api/v1/admin/tasks/{task_id}/publish-info 拿预填数据
  用户在弹窗里改名称/描述/价格/分类/标签 → POST /api/v1/admin/tasks/{task_id}/publish-cms
  → 本服务组装 GameSetReq 调 CMS POST /business/game/set(带 Bearer token)

数据来源:
  - story_tree_json 表(平台库,novel-game-factory 写入):book_title/book_summary/nodes
  - 封面: {ASSET_BASE_URL}/{task_id}/novel_cover/assets/images/novel_cover.png
    (scene_painter 上传 OSS 的固定 key,见 novel-game-factory regenerate._oss_key_images)
  - 游戏链接: {GAME_BASE_URL}/{task_id}/(仅供 publish-info 预览;不写入发往 CMS 的 payload)
  - story_id = task_id

CMS token:用当前登录用户的 OAuth2 access_token(登录时落库,game.publish scope);
过期由 token_service 抛 401 引导重新登录。也支持请求体临时传 cms_token 覆盖(联调用)。
"""
import json
import logging
import re
from typing import Any, Optional

import requests
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text as sql_text
from sqlmodel import Session

from common.config import ASSET_BASE_URL, CMS_API_BASE, GAME_BASE_URL
from common.models import PlatformUser, engine
from auth.deps import get_current_user_optional, get_user_or_service
from auth.token_service import get_valid_access_token

router = APIRouter(prefix="/api/v1", tags=["cms-publish"])
logger = logging.getLogger(__name__)

_CMS_READ_TIMEOUT_SEC = 60
_CMS_WRITE_TIMEOUT_SEC = 120


# ── 工具 ──

def _mark_tasks_published(task_ids: list[str]) -> None:
    """CMS 确认成功后落本地发布状态，供创作入口和服务端写锁统一读取。"""
    unique_ids = list(dict.fromkeys(task_id.strip() for task_id in task_ids if task_id.strip()))
    if not unique_ids:
        return
    with engine.begin() as conn:
        for task_id in unique_ids:
            conn.execute(
                sql_text(
                    "UPDATE public.novel_tasks "
                    "SET is_published = TRUE, info_confirm_stage = 'publish', update_time = NOW() "
                    "WHERE task_id = :task_id"
                ),
                {"task_id": task_id},
            )


def _is_task_published(task_id: str) -> bool:
    with engine.connect() as conn:
        row = conn.execute(
            sql_text(
                "SELECT is_published FROM public.novel_tasks WHERE task_id = :task_id"
            ),
            {"task_id": task_id},
        ).fetchone()
    return bool(row and row[0])

def _clean_title(raw: str) -> str:
    """去掉 LLM 生成标题里的 markdown 加粗/引号等噪音(如 **Ghost Peak Reborn**)。"""
    return re.sub(r"^[\s*#\"'「【]+|[\s*\"'」】]+$", "", raw or "").strip()


def _resolve_cms_token(
    user: Optional[PlatformUser], override: Optional[str] = None
) -> str:
    """取 CMS token(请求覆盖 > 当前用户 OAuth token),兼容带 'Bearer ' 前缀的填法。

    user 为 None = 内部服务调用(创作引擎带 X-Service-Token 转发)。这种情况必须显式传
    cms_token:引擎用户的 OAuth token 是用引擎自己的 TOKEN_ENCRYPT_KEY 加密的,
    中台的密钥解不开,只能由引擎解密后带过来。
    """
    if override and override.strip():
        token = override.strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        return token
    if user is None:
        raise HTTPException(400, "内部服务调用必须传 cms_token")
    with Session(engine) as session:
        return get_valid_access_token(session, user)


def _cms_get(path: str, params: dict, token: str) -> dict:
    """带 token 调 CMS GET 接口,统一错误处理,返回解析后的 JSON。"""
    return _cms_request("GET", path, token, params=params)


def _cms_request(method: str, path: str, token: str, params: Optional[dict] = None, json_body: Optional[dict] = None) -> dict:
    url = f"{CMS_API_BASE}{path}"
    timeout = _CMS_READ_TIMEOUT_SEC if method.upper() == "GET" else _CMS_WRITE_TIMEOUT_SEC
    try:
        resp = requests.request(
            method,
            url,
            params=params,
            json=json_body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
    except requests.RequestException as e:
        raise HTTPException(502, f"请求 CMS 失败: {e}")
    if resp.status_code == 401:
        raise HTTPException(401, "CMS 发布授权已过期，请重新登录")
    if resp.status_code == 403:
        raise HTTPException(403, f"CMS 当前账号没有操作权限: {resp.text[:500]}")
    if resp.status_code != 200:
        raise HTTPException(502, f"CMS 返回 HTTP {resp.status_code}: {resp.text[:500]}")
    try:
        return resp.json()
    except ValueError:
        raise HTTPException(502, f"CMS 返回非 JSON: {resp.text[:500]}")


def _fetch_story_tree(task_id: str) -> dict:
    """读取并展开 story_tree_json，发布默认使用英语 locale。

    story_planner 新版产物是完整语言树的 bundle，例如::

        {"en-US": {"nodes": ...}, "ja-JP": {"nodes": ...},
         "publication": {...}}

    发布接口需要的是单棵树。历史数据仍可能是顶层直接包含 ``nodes`` 的
    扁平结构，因此先兼容扁平格式，再按 ``en-US`` → 首个可用 locale 回退。
    """
    with Session(engine) as session:
        row = session.exec(
            sql_text("SELECT data FROM public.story_tree_json WHERE task_id = :tid")
            .bindparams(tid=task_id)
        ).first()
    if row is None or not row[0]:
        raise HTTPException(404, f"story_tree_json 中无 task_id={task_id} 的数据(任务可能还没跑完 story_planner)")
    payload = row[0]
    if isinstance(payload, (str, bytes, bytearray)):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError) as exc:
            raise HTTPException(502, f"task_id={task_id} 的 story_tree_json 不是有效 JSON: {exc}")
    if not isinstance(payload, dict):
        raise HTTPException(502, f"task_id={task_id} 的 story_tree_json 不是 JSON 对象")

    # 旧版扁平树：直接沿用。
    if isinstance(payload.get("nodes"), dict):
        return payload

    # 新版语言 bundle：发布默认读英语；英语缺失时回退到第一个有效语言树。
    candidates = [
        (str(locale), tree)
        for locale, tree in payload.items()
        if locale != "publication"
        and isinstance(tree, dict)
        and isinstance(tree.get("nodes"), dict)
    ]
    if not candidates:
        raise HTTPException(502, f"task_id={task_id} 的 story_tree_json 没有可用的语言树")
    selected_locale, selected_tree = next(
        (
            (locale, tree)
            for locale, tree in candidates
            if locale.strip().replace("_", "-").lower() == "en-us"
        ),
        candidates[0],
    )
    # Keep the selected locale available to the chapter fallback path even if
    # an old producer omitted ``player_language`` inside the locale snapshot.
    selected_tree = dict(selected_tree)
    selected_tree.setdefault("player_language", selected_locale)
    logger.info(
        "发布读取 story_tree locale: task_id=%s requested=en-US actual=%s nodes=%s",
        task_id,
        selected_locale,
        len(selected_tree.get("nodes") or {}),
    )
    return selected_tree


def _cover_url(task_id: str) -> str:
    # 封面 OSS key 约定: storage/outputs/{task_id}/images/backgrounds/novel_cover.png
    # (与 novel-game-factory 本地 storage/outputs 目录结构一致,上传规则由生成侧保证)
    return f"{ASSET_BASE_URL}/storage/outputs/{task_id}/images/backgrounds/novel_cover.png"


def _game_link(task_id: str) -> str:
    # 直达第一章(根节点 chapter_1 → 文件夹 chapter_1_chapter_1)
    return f"{GAME_BASE_URL}/{task_id}/chapter_1_chapter_1/"


def _chapter_title(node: dict) -> str:
    """Return a player-facing chapter title without the internal node suffix.

    Some historical story trees stored titles such as ``"Dawn chapter_1"``
    or ``"... node_abc123"``.  Those suffixes are routing identifiers and
    must not leak into the CMS chapter name.  Newer localized trees already
    contain clean titles, so this helper leaves normal punctuation/content
    untouched.
    """
    if not isinstance(node, dict):
        return ""
    title = node.get("title") or node.get("name") or node.get("chapter_title") or ""
    if isinstance(title, dict):
        # Be defensive if a producer writes a localized title object instead
        # of relying on the outer story-tree language bundle.
        title = title.get("en-US") or title.get("en") or next(
            (value for value in title.values() if isinstance(value, str)), ""
        )
    title = str(title).strip()
    if not title:
        return ""

    node_id = str(node.get("id") or "").strip()
    if node_id and title.casefold() == node_id.casefold():
        return ""
    if node_id:
        title = re.sub(
            rf"(?:\s+|[-–—:：]){re.escape(node_id)}\s*$",
            "",
            title,
            flags=re.IGNORECASE,
        ).strip()
    # Older generators used a generic chapter/node suffix even when the node
    # id was absent from the serialized object.
    title = re.sub(
        r"\s+(?:chapter_\d+|node_[A-Za-z0-9_-]+)\s*$",
        "",
        title,
        flags=re.IGNORECASE,
    ).strip()
    return _clean_title(title)


def _cms_chapter_node(node: dict) -> dict:
    """Keep the historical nested-node payload shape sent to CMS."""
    return {
        "id": node.get("id", ""),
        "title": node.get("title", ""),
        "summary": node.get("summary", ""),
        "depth": node.get("depth", 1),
        "generated_at": node.get("generated_at", ""),
        "cover": node.get("cover", ""),
        "link": node.get("link", ""),
    }


def _chapter_fallback_title(tree: dict, depth: int) -> str:
    """Use a language-aware numbered title only when the source has no title."""
    language = str(
        (tree or {}).get("player_language")
        or (tree or {}).get("book_language")
        or ""
    ).strip().lower().replace("_", "-")
    if language.startswith("en"):
        return f"Chapter {depth}"
    return f"第{depth}章"


def _is_original_node(node: dict) -> bool:
    """Accept boolean and JSON/string forms used by historical exports."""
    value = node.get("is_original") if isinstance(node, dict) else None
    return value is True or str(value or "").strip().lower() in {"true", "1", "yes"}


def _build_chapters(task_id: str, tree: dict) -> list[dict]:
    """按 depth 把同级 nodes 分组成 chapters(CMS ChapterInfo 结构)。"""
    nodes = tree.get("nodes", {}) or {}
    if not isinstance(nodes, dict):
        return []
    by_depth: dict[int, list[dict]] = {}
    primary_summaries: dict[int, str] = {}
    fallback_summaries: dict[int, str] = {}
    for node in nodes.values():
        if not isinstance(node, dict):
            continue
        try:
            depth = int(node.get("depth", 1))
        except (TypeError, ValueError):
            depth = 1
        summary = (
            node.get("summary")
            or node.get("chapter_summary")
            or node.get("description")
            or ""
        )
        if not isinstance(summary, str):
            summary = str(summary)
        if summary and depth not in fallback_summaries:
            fallback_summaries[depth] = summary
        if _is_original_node(node) and depth not in primary_summaries:
            primary_summaries[depth] = summary
        by_depth.setdefault(depth, []).append({
            "id": node.get("id", ""),
            "title": node.get("title", ""),
            "name": node.get("name", ""),
            "chapter_title": node.get("chapter_title", ""),
            "is_original": node.get("is_original"),
            "summary": summary,
            "depth": depth,
            "generated_at": node.get("generated_at", ""),
            "cover": "",  # 章节封面 beijing.png 按节点存放,CMS 侧暂不需要则留空
            "link": f"{GAME_BASE_URL}/{task_id}/{node.get('id', '')}/",
        })
    chapters = []
    for depth in sorted(by_depth):
        chapter_nodes = sorted(by_depth[depth], key=lambda n: n["id"])
        # 章节简介取该章主节点（通常 is_original=true）的摘要；分支节点的
        # 摘要仍保留在 nodes 中，避免把多个分支简介拼成一段不可读文本。
        chapter_description = primary_summaries.get(depth) or fallback_summaries.get(depth, "")
        # CMS 的章节顶层名称不能再用固定的“第 N 章”。优先使用该深度的
        # 原著主节点标题，原著节点缺失时取第一个有标题的节点；这样发布
        # 端会保留 story_tree 当前语言包(en-US 等)里的真实章节名。
        title_candidates = [
            node for node in chapter_nodes if _is_original_node(node)
        ]
        # A few old trees omit is_original but retain the conventional root
        # id.  Prefer that root before selecting an arbitrary branch title.
        root_node = next(
            (node for node in chapter_nodes if str(node.get("id")) == f"chapter_{depth}"),
            None,
        )
        if root_node is not None and root_node not in title_candidates:
            title_candidates.append(root_node)
        title_candidates.extend(
            node for node in chapter_nodes if node not in title_candidates
        )
        chapter_title = next(
            (_chapter_title(node) for node in title_candidates if _chapter_title(node)),
            _chapter_fallback_title(tree, depth),
        )
        # 字段集合与 CMS ChapterInfo 示例完全对齐,缺字段可能触发参数校验失败(code=3)
        chapters.append({
            "id": 0,
            "game_id": 0,
            "number": depth,
            "title": chapter_title,
            "name": chapter_title,
            "base_name": "",
            "coins": 0,
            "is_free": True,
            "cover": "",
            "description": chapter_description,
            "link": "",
            "site_id": 0,
            "status": 1,
            "status_name": "",
            "published_time": "",
            "nodes": [_cms_chapter_node(node) for node in chapter_nodes],
        })
    return chapters


# ── CMS 选项(标签/分类)代理:token 不下发前端,由后端带 token 转发 ──

# CMS 在不同环境/版本里出现过 0、1、200 以及字符串形式。
# 统一在服务端归一化，避免 CMS 已经落库成功，创作平台却把 ``msg=成功``
# 误判成失败并展示红色叉号。
CMS_OK_CODES = (0, 1, 200, "0", "1", "200")


def _cms_succeeded(data: dict) -> bool:
    message_text = str(data.get("msg") or data.get("message") or "").strip().lower()
    return bool(
        data.get("code") in CMS_OK_CODES
        or data.get("ok") is True
        or data.get("success") is True
        or message_text in {"ok", "success", "succeeded", "成功", "操作成功", "发布成功"}
    )


def _cms_auth_failed(data: dict) -> bool:
    """识别 CMS 以 HTTP 200 包装的 token 失效业务响应。"""
    code = str(data.get("code") or "").strip().lower()
    message_text = str(data.get("msg") or data.get("message") or "").lower()
    return (
        code in {"401", "4010", "unauthorized", "token_expired"}
        or any(
            keyword in message_text
            for keyword in ("token", "unauthorized", "未登录", "登录失效", "授权失效", "授权过期")
        )
    )


def _raise_cms_business_error(data: dict, action: str) -> None:
    if _cms_auth_failed(data):
        raise HTTPException(401, "CMS 发布授权已过期，请重新登录")
    raise HTTPException(
        502,
        f"{action}: code={data.get('code')} msg={data.get('msg') or data.get('message')}",
    )


def _cms_list_items(data: dict) -> list:
    """兼容 CMS 列表的 data、data.data、items/rows/records 等历史响应。"""
    list_keys = ("data", "items", "list", "rows", "records")
    queue = [data]
    visited: set[int] = set()
    while queue:
        payload = queue.pop(0)
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict) or id(payload) in visited:
            continue
        visited.add(id(payload))
        for key in list_keys:
            nested = payload.get(key)
            if isinstance(nested, list):
                return nested
            if isinstance(nested, dict):
                queue.append(nested)
    return []


@router.get("/admin/cms/tags")
def list_cms_tags(
    cms_token: Optional[str] = None,
    user: Optional[PlatformUser] = Depends(get_current_user_optional),
):
    """拉 CMS 启用中的标签列表,返回前端 a-select 可直接用的 {value,label} 数组。

    cms_token 是给内部服务调用留的口子(创作引擎带 X-Service-Token 转发时 user 为 None,
    引擎用自己的密钥解密出 token 再带过来)。
    """
    token = _resolve_cms_token(user, cms_token)
    data = _cms_get("/business/tag/list", {"page": 1, "pageSize": 500, "status": 1}, token)
    if not _cms_succeeded(data):
        _raise_cms_business_error(data, "CMS 标签列表返回异常")
    items = _cms_list_items(data)
    return {
        "items": [
            {"value": t["id"], "label": t.get("name", str(t["id"]))}
            for t in items
        ]
    }


@router.get("/admin/cms/classes")
def list_cms_classes(
    cms_token: Optional[str] = None,
    user: Optional[PlatformUser] = Depends(get_current_user_optional),
):
    """拉 CMS 启用中的游戏分类列表,children 递归转成 cascader options({value,label,children})。"""
    token = _resolve_cms_token(user, cms_token)
    data = _cms_request(
        "POST", "/business/game/class/list", token,
        json_body={"id": None, "name": None, "site_id": 0, "status": 1},
    )
    if not _cms_succeeded(data):
        _raise_cms_business_error(data, "CMS 分类列表返回异常")
    items = _cms_list_items(data)

    def to_option(node: dict) -> dict:
        node_id = node.get("id", node.get("value"))
        opt = {"value": node_id, "label": node.get("name") or node.get("label") or str(node_id)}
        children_data = (
            node.get("children")
            or node.get("child")
            or node.get("child_list")
            or node.get("childList")
            or node.get("childs")
            or []
        )
        children = [
            to_option(c)
            for c in children_data
            if isinstance(c, dict) and c.get("id", c.get("value")) is not None
        ]
        if children:
            opt["children"] = children
        return opt

    return {
        "items": [
            to_option(n)
            for n in items
            if isinstance(n, dict) and n.get("id", n.get("value")) is not None
        ]
    }


class CmsTagIn(BaseModel):
    name: str
    status: int = 1
    cms_token: Optional[str] = None


@router.post("/admin/cms/tags")
def create_cms_tag(
    body: CmsTagIn,
    cms_token: Optional[str] = None,
    user: Optional[PlatformUser] = Depends(get_current_user_optional),
):
    """新建 CMS 标签,返回 {id, name} 供发布表单立即选中。"""
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "标签名不能为空")
    # 创作引擎通过 service token 调用时把 CMS token 放在 query 中；中台前端
    # 直连时仍兼容请求体中的 cms_token。
    token = _resolve_cms_token(user, body.cms_token or cms_token)
    data = _cms_request(
        "POST", "/business/tag/set", token,
        json_body={"id": 0, "name": name, "status": body.status},
    )
    if not _cms_succeeded(data):
        _raise_cms_business_error(data, "CMS 新建标签失败")
    created = data.get("data") or {}
    return {"id": created.get("id"), "name": created.get("name", name)}


# ── 预填数据 ──

class PublishInfoOut(BaseModel):
    task_id: str
    story_id: str
    name: str
    description: str
    cover: str
    link: str
    chapters: list
    gameplay: str = "interactive_story"
    story_mode: str = "original"
    genre: str = ""
    suggested_categories: list[str] = []
    suggested_tags: list[str] = []


def _fetch_task_publish_profile(task_id: str) -> dict:
    """发布表单需要的作品名与游戏类型。

    用原始 SQL 是因为 game-platform 不持有 novel_tasks 的完整 ORM 模型，
    但该表和 story_tree_json 都在同一个平台库中。
    """
    try:
        with engine.connect() as conn:
            row = conn.execute(
                sql_text(
                    "SELECT project_name, generated_project_name, novel_filename, "
                    "gameplay, story_mode, art_style "
                    "FROM public.novel_tasks WHERE task_id = :task_id"
                ),
                {"task_id": task_id},
            ).mappings().first()
    except Exception as exc:
        # 兼容尚未执行最新 schema repair 的存量环境：基础发布信息
        # 仍然要能回填，只是暂时少了游戏类型的自动推荐。
        logger.warning("读取发布类型字段失败，回退基础字段: %s", exc)
        with engine.connect() as conn:
            row = conn.execute(
                sql_text(
                    "SELECT project_name, generated_project_name, novel_filename "
                    "FROM public.novel_tasks WHERE task_id = :task_id"
                ),
                {"task_id": task_id},
            ).mappings().first()
    return dict(row) if row else {}


def _detect_publish_genre(title: str, summary: str, tree: dict) -> str:
    """从故事树显式字段或标题/简介关键词得出可编辑的预设题材。"""
    explicit = (
        tree.get("genre")
        or (tree.get("hot_elements") or {}).get("genre")
        or (tree.get("metadata") or {}).get("genre")
        or ""
    )
    text = f"{explicit} {title} {summary}".lower()
    rules = (
        ("恋爱", ("恋爱", "言情", "爱情", "romance", "love", "心动", "婚约")),
        ("悬疑", ("悬疑", "推理", "谜案", "mystery", "detective", "真相")),
        ("惊悚", ("恐怖", "惊悚", "诡异", "horror", "thriller", "鬼", "诡")),
        ("奇幻", ("奇幻", "玄幻", "仙侠", "fantasy", "魔法", "吸血鬼", "狼人")),
        ("科幻", ("科幻", "赛博", "cyberpunk", "sci-fi", "science fiction", "未来")),
        ("武侠", ("武侠", "江湖", "江山", "朝堂", "权谋", "剑")),
        ("都市", ("都市", "现代", "校园", "urban", "modern", "职场")),
    )
    for genre, keywords in rules:
        if any(keyword in text for keyword in keywords):
            return genre
    return str(explicit).strip()


def _publish_presets(gameplay: str, story_mode: str, genre: str) -> tuple[list[str], list[str]]:
    """返回按优先级排序的 CMS 分类/标签名候选，前端再匹配实际 ID。"""
    gameplay = (gameplay or "interactive_story").strip().lower()
    story_mode = (story_mode or "original").strip().lower()
    if gameplay == "free_growth":
        categories = ["模拟养成", "养成游戏", "养成", "模拟经营", "角色扮演", "互动游戏"]
        tags = ["自由成长", "养成", "角色扮演", "多结局"]
    elif story_mode in {"multi", "chapter_branch", "branch"}:
        categories = ["互动小说", "视觉小说", "文字冒险", "文字剧情", "剧情互动", "角色扮演"]
        tags = ["互动剧情", "多分支", "多结局", "视觉小说"]
    else:
        categories = ["视觉小说", "互动小说", "文字冒险", "文字剧情", "剧情互动", "角色扮演"]
        tags = ["剧情", "沉浸式", "视觉小说"]
    if genre:
        categories = [genre, *categories]
        tags = [genre, *tags]
    return list(dict.fromkeys(categories)), list(dict.fromkeys(tags))


@router.get("/admin/tasks/{task_id}/publish-info", response_model=PublishInfoOut)
def get_publish_info(task_id: str):
    """发布弹窗的预填数据:解析 story_tree_json,组装名称/描述/封面/链接/章节。"""
    tree = _fetch_story_tree(task_id)
    profile = _fetch_task_publish_profile(task_id)
    title = (
        profile.get("project_name")
        or profile.get("generated_project_name")
        or _clean_title(tree.get("book_title", ""))
        or profile.get("novel_filename")
        or task_id
    )
    summary = (
        tree.get("book_summary")
        or tree.get("summary")
        or tree.get("one_liner")
        or ""
    ).strip()
    gameplay = profile.get("gameplay") or "interactive_story"
    story_mode = profile.get("story_mode") or tree.get("story_mode") or "original"
    genre = _detect_publish_genre(title, summary, tree)
    suggested_categories, suggested_tags = _publish_presets(gameplay, story_mode, genre)
    return PublishInfoOut(
        task_id=task_id,
        story_id=task_id,
        name=_clean_title(title),
        description=summary,
        cover=_cover_url(task_id),
        link=_game_link(task_id),
        chapters=_build_chapters(task_id, tree),
        gameplay=gameplay,
        story_mode=story_mode,
        genre=genre,
        suggested_categories=suggested_categories,
        suggested_tags=suggested_tags,
    )


# ── 发布 ──

class ChapterPriceIn(BaseModel):
    number: int
    coins: int = 0
    is_free: bool = True


class PublishCmsIn(BaseModel):
    name: str
    description: str = ""
    class_id: int = 0
    tags: list[int] = []
    cover: Optional[str] = None          # 不传则用默认 novel_cover 地址
    chapters: list[ChapterPriceIn] = []  # 用户设置的章节价格,按 number 对齐
    site_id: int = 0
    is_recommend: Optional[bool] = None
    cms_token: Optional[str] = None      # 联调临时 token,不传则用当前用户 OAuth token


class PublishCmsOut(BaseModel):
    code: int
    msg: str
    request_payload: dict  # 回显实际发给 CMS 的 payload,便于排查


def _build_game_payload(
    task_id: str,
    *,
    name: str = "",
    description: str = "",
    class_id: int = 0,
    tags: Optional[list[int]] = None,
    cover: Optional[str] = None,
    chapter_prices: Optional[list["ChapterPriceIn"]] = None,
    site_id: int = 0,
    is_recommend: Optional[bool] = None,
) -> dict:
    """组装一个 CMS GameSetReq。单发布与批量发布共用,避免两套字段集合走偏。

    字段集合必须与 CMS 示例一致:缺字段会触发它的参数校验失败(code=3)。
    游戏直达链接只用于发布信息预览，CMS 端不需要，因此不放进请求体。
    """
    tree = _fetch_story_tree(task_id)
    chapters = _build_chapters(task_id, tree)

    # 用户设置的章节价格按 number 覆盖
    price_map = {c.number: c for c in (chapter_prices or [])}
    for chapter in chapters:
        price = price_map.get(chapter["number"])
        if price:
            chapter["coins"] = price.coins
            chapter["is_free"] = price.is_free

    return {
        "id": 0,  # 0=新建;CMS 侧按 story_id 幂等与否由其内部处理
        "story_id": task_id,
        "name": name or _clean_title(tree.get("book_title", "")) or task_id,
        "description": description or (
            tree.get("book_summary")
            or tree.get("summary")
            or tree.get("one_liner")
            or ""
        ).strip(),
        "cover": cover or _cover_url(task_id),
        # CMS 端不需要游戏直达链接；游戏链接仍可通过 publish-info
        # 提供给创作引擎预览，但不再写入发往 CMS 的 GameSetReq。
        "class_id": class_id,
        "tags": tags or [],
        "coins": None,       # 只读字段,CMS 按章节价格计算;示例里显式传 null,跟随之
        "images": [],
        "videos": [],
        "chapters": chapters,
        "chapters_str": "",
        "site_id": site_id,
        "status": 1,
        "is_recommend": is_recommend,  # null=保持原值
        "creator_id": 1,
    }


@router.post("/admin/tasks/{task_id}/publish-cms", response_model=PublishCmsOut)
def publish_to_cms(
    task_id: str, body: PublishCmsIn,
    user: Optional[PlatformUser] = Depends(get_user_or_service),
):
    """组装 GameSetReq 调 CMS POST /business/game/set。"""
    if _is_task_published(task_id):
        raise HTTPException(400, "作品已发布，请勿重复发布")
    token = _resolve_cms_token(user, body.cms_token)

    payload: dict[str, Any] = _build_game_payload(
        task_id,
        name=body.name,
        description=body.description,
        class_id=body.class_id,
        tags=body.tags,
        cover=body.cover,
        chapter_prices=body.chapters,
        site_id=body.site_id,
        is_recommend=body.is_recommend,
    )

    url = f"{CMS_API_BASE}/business/game/set"
    try:
        resp = requests.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=_CMS_WRITE_TIMEOUT_SEC,
        )
    except requests.RequestException as e:
        raise HTTPException(502, f"请求 CMS 失败: {e}")

    if resp.status_code == 401:
        raise HTTPException(401, "CMS 发布授权已过期，请重新登录")
    if resp.status_code == 403:
        raise HTTPException(403, f"CMS 当前账号没有发布权限: {resp.text[:500]}")
    if resp.status_code != 200:
        raise HTTPException(502, f"CMS 返回 HTTP {resp.status_code}: {resp.text[:500]}")

    try:
        data = resp.json()
    except ValueError:
        raise HTTPException(502, f"CMS 返回非 JSON: {resp.text[:500]}")

    succeeded = _cms_succeeded(data)
    if not succeeded and _cms_auth_failed(data):
        raise HTTPException(401, "CMS 发布授权已过期，请重新登录")
    if succeeded:
        _mark_tasks_published([task_id])

    raw_code = data.get("code")
    try:
        normalized_code = int(raw_code)
    except (TypeError, ValueError):
        normalized_code = 0 if succeeded else -1

    return PublishCmsOut(
        code=normalized_code,
        msg=data.get("msg", ""),
        request_payload=payload,
    )


# ── 批量发布 ──

# 一次批量的上限。CMS 侧逐项执行,条数越多单请求越久;超时后前端无法判断哪些已生效,
# 所以在这里就挡住,让前端分批发。
_MAX_BATCH_ITEMS = 50
# 批量是 N 个游戏顺序落库,比单发布慢得多
_BATCH_TIMEOUT_SEC = 300


class BatchPublishItemIn(BaseModel):
    task_id: str
    name: str = ""
    description: str = ""
    class_id: int = 0
    tags: list[int] = []
    cover: Optional[str] = None
    chapters: list[ChapterPriceIn] = []
    site_id: int = 0
    is_recommend: Optional[bool] = None


class BatchPublishIn(BaseModel):
    items: list[BatchPublishItemIn]
    cms_token: Optional[str] = None


class BatchPublishItemOut(BaseModel):
    task_id: str
    success: bool
    reason: str = ""


class BatchPublishOut(BaseModel):
    code: int
    msg: str
    total_count: int
    success_count: int
    failed_count: int
    results: list[BatchPublishItemOut]
    # 组装阶段就失败的任务(如 story_tree_json 还没生成),没进 CMS 请求
    skipped: list[BatchPublishItemOut] = []


def _batch_item_succeeded(row: dict, overall_success: bool) -> bool:
    """兼容 CMS 批量结果中 bool/code/status/message 并存的历史格式。"""
    if not isinstance(row, dict):
        return overall_success

    if row.get("code") in CMS_OK_CODES or row.get("ok") is True:
        return True

    explicit_failure = False
    raw_success = row.get("success")
    if isinstance(raw_success, bool):
        if raw_success:
            return True
        explicit_failure = True
    elif raw_success is not None:
        normalized = str(raw_success).strip().lower()
        if normalized in {"1", "true", "yes", "ok", "success", "succeeded"}:
            return True
        if normalized in {"0", "false", "no", "fail", "failed", "error"}:
            explicit_failure = True

    status = str(row.get("status") or "").strip().lower()
    if status in {"success", "succeeded", "ok", "done", "completed"}:
        return True
    if status in {"fail", "failed", "error"}:
        return False

    message_text = str(
        row.get("reason") or row.get("msg") or row.get("message") or ""
    ).strip().lower()
    if message_text and any(word in message_text for word in ("失败", "错误", "error", "fail")):
        return False
    if message_text and any(word in message_text for word in ("成功", "success", "succeeded")):
        return True

    if explicit_failure:
        return False

    # CMS 顶层已确认成功、单项又没有明确失败信号时，按成功处理；不能因为缺少
    # success 字段就把实际已经落库的作品展示成失败。
    return overall_success


def _batch_result_task_id(row: dict, ordered_task_ids: list[str]) -> str:
    direct_id = str(row.get("task_id") or row.get("story_id") or "").strip()
    if direct_id in ordered_task_ids:
        return direct_id
    idx = row.get("index")
    try:
        numeric_index = int(idx)
    except (TypeError, ValueError):
        return ""
    # 兼容 CMS 实际使用过的 1 起算，以及少数环境的 0 起算。
    for candidate in (numeric_index - 1, numeric_index):
        if 0 <= candidate < len(ordered_task_ids):
            return ordered_task_ids[candidate]
    return ""


@router.post("/admin/tasks/publish-cms-batch", response_model=BatchPublishOut)
def publish_to_cms_batch(
    body: BatchPublishIn,
    user: Optional[PlatformUser] = Depends(get_user_or_service),
):
    """批量发布:组装 GameBatchSetReq 调 CMS POST /business/game/batch_set。

    不是「循环调单发布」——CMS 提供了原生批量端点,一次请求逐项执行并回报每项结果;
    循环调的话中途失败会留下一半已发布、一半没发的状态,前端既没法回滚也没法交代。

    组装不出 payload 的任务(通常是还没跑完 story_planner、没有 story_tree_json)
    不会让整批失败,而是单独进 skipped —— 否则一个没跑完的作品会拖垮其他所有作品。
    """
    if not body.items:
        raise HTTPException(400, "请至少选择 1 个作品")
    if len(body.items) > _MAX_BATCH_ITEMS:
        raise HTTPException(400, f"单次最多发布 {_MAX_BATCH_ITEMS} 个作品,请分批操作")

    published_ids = [item.task_id for item in body.items if _is_task_published(item.task_id)]
    if published_ids:
        raise HTTPException(400, f"批量中包含已发布作品，请取消勾选：{', '.join(published_ids)}")

    token = _resolve_cms_token(user, body.cms_token)

    payloads: list[dict] = []
    ordered_task_ids: list[str] = []
    skipped: list[BatchPublishItemOut] = []
    for item in body.items:
        try:
            payloads.append(_build_game_payload(
                item.task_id,
                name=item.name,
                description=item.description,
                class_id=item.class_id,
                tags=item.tags,
                cover=item.cover,
                chapter_prices=item.chapters,
                site_id=item.site_id,
                is_recommend=item.is_recommend,
            ))
            ordered_task_ids.append(item.task_id)
        except HTTPException as e:
            skipped.append(BatchPublishItemOut(
                task_id=item.task_id, success=False, reason=str(e.detail),
            ))

    if not payloads:
        return BatchPublishOut(
            code=-1, msg="所选作品都不具备发布条件",
            total_count=len(body.items), success_count=0,
            failed_count=len(skipped), results=[], skipped=skipped,
        )

    url = f"{CMS_API_BASE}/business/game/batch_set"
    try:
        resp = requests.post(
            url,
            json={"items": payloads},
            headers={"Authorization": f"Bearer {token}"},
            timeout=_BATCH_TIMEOUT_SEC,
        )
    except requests.RequestException as e:
        raise HTTPException(502, f"请求 CMS 失败: {e}")

    if resp.status_code == 401:
        raise HTTPException(401, "CMS 发布授权已过期，请重新登录")
    if resp.status_code == 403:
        raise HTTPException(403, f"CMS 当前账号没有批量发布权限: {resp.text[:500]}")
    if resp.status_code != 200:
        raise HTTPException(502, f"CMS 返回 HTTP {resp.status_code}: {resp.text[:500]}")

    try:
        data = resp.json()
    except ValueError:
        raise HTTPException(502, f"CMS 返回非 JSON: {resp.text[:500]}")

    overall_success = _cms_succeeded(data)
    if not overall_success and _cms_auth_failed(data):
        raise HTTPException(401, "CMS 发布授权已过期，请重新登录")

    body_data = data.get("data") if isinstance(data.get("data"), dict) else {}
    raw_results = body_data.get("results") or data.get("results") or []
    # CMS 回的 results 用 index 标位(按输入顺序,实测 1 起算,做兼容),
    # 换回 task_id 前端才知道是哪张卡片失败的
    results: list[BatchPublishItemOut] = []
    resolved_task_ids: set[str] = set()
    for row in raw_results:
        if not isinstance(row, dict):
            continue
        task_id = _batch_result_task_id(row, ordered_task_ids)
        if not task_id or task_id in resolved_task_ids:
            continue
        resolved_task_ids.add(task_id)
        results.append(BatchPublishItemOut(
            task_id=task_id,
            success=_batch_item_succeeded(row, overall_success),
            reason=row.get("reason") or row.get("msg") or row.get("message") or "",
        ))

    # 有的 CMS 版本只回顶层 code/msg，不回逐项结果；有的虽回 results，却缺
    # success 字段。把未明确失败的项补齐，前端才能展示真实发布结果。
    for task_id in ordered_task_ids:
        if task_id not in resolved_task_ids:
            results.append(BatchPublishItemOut(
                task_id=task_id,
                success=overall_success,
                reason="" if overall_success else (data.get("msg") or "CMS 批量发布失败"),
            ))

    success_count = sum(1 for row in results if row.success)
    failed_count = sum(1 for row in results if not row.success) + len(skipped)
    published_task_ids = [row.task_id for row in results if row.success and row.task_id]
    _mark_tasks_published(published_task_ids)
    raw_code = data.get("code")
    try:
        normalized_code = int(raw_code)
    except (TypeError, ValueError):
        normalized_code = 0 if success_count and not failed_count else -1
    return BatchPublishOut(
        code=normalized_code,
        msg=data.get("msg", ""),
        total_count=len(body.items),
        success_count=success_count,
        failed_count=failed_count,
        results=results,
        skipped=skipped,
    )
