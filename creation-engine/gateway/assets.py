"""素材库 API:代理列出 OSS 或本地 storage 目录。"""
import os
import time
from datetime import datetime
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from common.config import ASSET_BASE_URL, ASSET_OSS_BUCKET, OSS_ACCESS_KEY, OSS_ENDPOINT, OSS_SECRET_KEY
from common.models import (
    NovelTask,
    PlatformUser,
    SavedMaterial,
    UserFolder,
    UserMaterialFlag,
    engine,
)
from auth.deps import get_current_user
from common.local_storage import (
    local_asset_record,
    local_asset_url,
    local_media_type,
    local_path_for_key,
    oss_disabled,
    storage_root,
)

router = APIRouter(prefix="/api/v1", tags=["assets"])

CATEGORY_PREFIX = {
    "character": "image-backups/character/",
    "scene": "image-backups/sence/",
    "music": "sound-backups/bgm/",
    "sound": "sound-backups/sfx/",
}

_CACHE_TTL_SEC = 60
_cache: dict = {}  # prefix -> (fetched_at, [obj dict])

_bucket = None


def _get_bucket():
    global _bucket
    if oss_disabled():
        return None
    if _bucket is None:
        import oss2
        if not OSS_ACCESS_KEY or not OSS_SECRET_KEY:
            raise HTTPException(500, "OSS 凭证未配置(OSS_ACCESS_KEY/OSS_SECRET_KEY)")
        auth = oss2.Auth(OSS_ACCESS_KEY, OSS_SECRET_KEY)
        _bucket = oss2.Bucket(auth, f"https://{OSS_ENDPOINT}", ASSET_OSS_BUCKET)
    return _bucket


def _list_all(prefix: str) -> list[dict]:
    now = time.time()
    cached = _cache.get(prefix)
    if cached and now - cached[0] < _CACHE_TTL_SEC:
        return cached[1]

    if oss_disabled():
        root = local_path_for_key(prefix)
        if not root.exists():
            return []
        items = [
            local_asset_record(path.relative_to(storage_root()).as_posix(), path)
            for path in root.rglob("*")
            if path.is_file()
        ]
        items.sort(key=lambda x: x["last_modified"], reverse=True)
        _cache[prefix] = (now, items)
        return items

    import oss2
    bucket = _get_bucket()
    items = []
    try:
        for obj in oss2.ObjectIterator(bucket, prefix=prefix):
            if obj.key.endswith("/"):
                continue
            items.append({
                "key": obj.key,
                "name": obj.key.rsplit("/", 1)[-1],
                "url": f"{ASSET_BASE_URL}/{obj.key}",
                "size": obj.size,
                "last_modified": datetime.fromtimestamp(obj.last_modified).isoformat(),
            })
    except oss2.exceptions.OssError as e:
        raise HTTPException(502, f"OSS 列举失败: {e}")

    items.sort(key=lambda x: x["last_modified"], reverse=True)
    _cache[prefix] = (now, items)
    return items


def _hidden_keys(user_uid: str) -> set:
    with Session(engine) as session:
        rows = session.exec(
            select(UserMaterialFlag.asset_key).where(
                UserMaterialFlag.user_id == user_uid,
                UserMaterialFlag.hidden == True,  # noqa: E712
            )
        ).all()
    return set(rows)


def _safe_asset_key(key: str) -> bool:
    """OSS key 的通用安全检查，避免把路径穿越或 URL 当作对象 key 入库。"""
    if not key or key != key.strip() or len(key) > 1024:
        return False
    if key.startswith(("/", "\\")) or "\\" in key or "://" in key or "\x00" in key:
        return False
    return all(part not in {"", ".", ".."} for part in key.split("/"))


def _history_asset_task_id(key: str) -> Optional[str]:
    """识别生成历史中的安全素材 key，并返回所属 task_id。

    一组旧产物使用 ``{task_id}/{node_id}/assets/{category}/...``，热插拔契约和
    新镜像使用 ``storage/outputs/{task_id}/...``。这里只接受人物、场景、BGM、
    音效四类真实产物，配音、工程文件等仍不能借此入口保存。
    """
    if not _safe_asset_key(key):
        return None
    parts = key.split("/")
    if parts[:2] == ["storage", "outputs"]:
        if len(parts) < 5:
            return None
        task_id = parts[2]
        asset_path = "/".join(parts[3:]).lower()
        allowed = (
            asset_path.startswith("images/characters/"),
            asset_path.startswith("images/scenes/"),
            asset_path.startswith("images/backgrounds/"),
            "/assets/characters/" in f"/{asset_path}",
            "/assets/scenes/" in f"/{asset_path}",
            "/assets/music/" in f"/{asset_path}",
            "/assets/sounds/" in f"/{asset_path}",
        )
    else:
        if len(parts) < 5:
            return None
        task_id = parts[0]
        asset_path = "/".join(parts[1:]).lower()
        allowed = (
            "/assets/characters/" in f"/{asset_path}",
            "/assets/scenes/" in f"/{asset_path}",
            "/assets/music/" in f"/{asset_path}",
            "/assets/sounds/" in f"/{asset_path}",
        )
    return task_id if task_id and any(allowed) else None


def _valid_asset_key(key: str) -> bool:
    """公共素材库中的 key。生成历史 key 需要在 save_asset 内额外校验归属。"""
    return _safe_asset_key(key) and any(key.startswith(p) for p in CATEGORY_PREFIX.values())


class AssetKeyIn(BaseModel):
    key: str


class AssetSaveIn(BaseModel):
    key: str
    folder_id: Optional[int] = None


class AssetPageOut(BaseModel):
    total: int
    page: int
    page_size: int
    items: list


@router.get("/assets", response_model=AssetPageOut)
def list_assets(
    category: str,
    name: Optional[str] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    page: int = 1,
    page_size: int = 24,
    user: PlatformUser = Depends(get_current_user),
):
    prefix = CATEGORY_PREFIX.get(category)
    if prefix is None:
        raise HTTPException(400, f"未知素材分类: {category}(可选 {', '.join(CATEGORY_PREFIX)})")
    page = max(page, 1)
    page_size = min(max(page_size, 1), 200)

    items = _list_all(prefix)
    if name:
        needle = name.lower()
        items = [i for i in items if needle in i["name"].lower()]
    if start_time:
        items = [i for i in items if datetime.fromisoformat(i["last_modified"]) >= start_time]
    if end_time:
        items = [i for i in items if datetime.fromisoformat(i["last_modified"]) <= end_time]

    # 过滤掉该用户软删除的素材(仅对本人隐藏,OSS 不动)
    hidden = _hidden_keys(user.main_site_uid)
    if hidden:
        items = [i for i in items if i["key"] not in hidden]

    total = len(items)
    offset = (page - 1) * page_size
    return AssetPageOut(
        total=total, page=page, page_size=page_size,
        items=items[offset:offset + page_size],
    )


@router.get("/assets/download")
def download_asset(key: str, user: PlatformUser = Depends(get_current_user)):
    """代理下载素材(本地模式从 storage 读取，远程模式从 OSS 读取)。"""
    prefix_ok = any(key.startswith(p) for p in CATEGORY_PREFIX.values())
    if not prefix_ok or ".." in key:
        raise HTTPException(400, "非法 key")
    if oss_disabled():
        return download_local_asset(key, as_attachment=True)
    import oss2
    bucket = _get_bucket()
    try:
        obj = bucket.get_object(key)
    except oss2.exceptions.NoSuchKey:
        raise HTTPException(404, f"对象不存在: {key}")
    except oss2.exceptions.OssError as e:
        raise HTTPException(502, f"OSS 下载失败: {e}")
    filename = key.rsplit("/", 1)[-1]
    return StreamingResponse(
        obj,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
        },
    )


@router.get("/local-assets/{key:path}")
def download_local_asset(key: str, as_attachment: bool = False):
    """Serve a validated local asset without exposing the host filesystem path."""
    if not oss_disabled():
        raise HTTPException(404, "本地素材服务未启用")
    try:
        path = local_path_for_key(key)
    except ValueError as exc:
        raise HTTPException(400, "非法 key") from exc
    if not path.is_file():
        raise HTTPException(404, f"文件不存在: {key}")
    disposition = "attachment" if as_attachment else "inline"
    filename = quote(path.name)
    return FileResponse(
        path,
        media_type=local_media_type(path),
        headers={"Content-Disposition": f"{disposition}; filename*=UTF-8''{filename}"},
    )


def _upsert_flag(session: Session, user_uid: str, key: str, **fields):
    row = session.exec(
        select(UserMaterialFlag).where(
            UserMaterialFlag.user_id == user_uid,
            UserMaterialFlag.asset_key == key,
        )
    ).first()
    if row is None:
        row = UserMaterialFlag(user_id=user_uid, asset_key=key)
    for k, v in fields.items():
        setattr(row, k, v)
    row.updated_at = datetime.now()
    session.add(row)
    session.commit()


@router.post("/assets/hide")
def hide_asset(body: AssetKeyIn, user: PlatformUser = Depends(get_current_user)):
    """软删除:仅在该用户的素材列表里隐藏,不删 OSS 对象"""
    if not _valid_asset_key(body.key):
        raise HTTPException(400, "非法 key")
    with Session(engine) as session:
        _upsert_flag(session, user.main_site_uid, body.key, hidden=True)
    return {"ok": True}


@router.post("/assets/save")
def save_asset(body: AssetSaveIn, user: PlatformUser = Depends(get_current_user)):
    """保存到我的资产:在指定文件夹(folder_id 为空=未分类)新建一条已保存素材"""
    history_task_id = _history_asset_task_id(body.key)
    if not _valid_asset_key(body.key) and history_task_id is None:
        raise HTTPException(400, "非法 key")
    with Session(engine) as session:
        if history_task_id is not None:
            owned_task = session.exec(
                select(NovelTask).where(
                    NovelTask.task_id == history_task_id,
                    NovelTask.user_id == user.main_site_uid,
                    NovelTask.is_deleted == False,  # noqa: E712
                )
            ).first()
            if owned_task is None:
                raise HTTPException(403, "生成历史素材不属于当前用户")
        if body.folder_id is not None:
            folder = session.get(UserFolder, body.folder_id)
            if folder is None or folder.user_id != user.main_site_uid:
                raise HTTPException(404, "文件夹不存在")
        # 同一素材在同一文件夹里去重
        exists = session.exec(
            select(SavedMaterial).where(
                SavedMaterial.user_id == user.main_site_uid,
                SavedMaterial.asset_key == body.key,
                SavedMaterial.folder_id == body.folder_id,
            )
        ).first()
        if exists is None:
            name = body.key.rsplit("/", 1)[-1]
            url = local_asset_url(body.key) if oss_disabled() else (
                f"{ASSET_BASE_URL}/{body.key}" if ASSET_BASE_URL else body.key
            )
            session.add(SavedMaterial(
                user_id=user.main_site_uid,
                folder_id=body.folder_id,
                asset_key=body.key,
                name=name,
                url=url,
            ))
            session.commit()
    return {"ok": True}
