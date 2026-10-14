"""素材库 API:代理列出 OSS 或本地 storage 目录。

分类 → OSS prefix 映射(bucket=game--oss,与生成侧上传路径约定一致):
  character → image-backups/character/   scene → image-backups/sence/
  bgm → sound-backups/bgm/               sfx → sound-backups/sfx/
OSS list 只按 key 字典序,无法按时间增量拉,所以全量 list(几千条规模)后
内存过滤/排序/分页,加 60s TTL 缓存兜住频繁翻页。
URL 用 ASSET_BASE_URL 拼接(CDN 或 OSS 公网),不用签名 URL(会绕过 CDN 且有过期问题)。
"""
import time
from datetime import datetime
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from common.config import ASSET_BASE_URL, ASSET_OSS_BUCKET, OSS_ACCESS_KEY, OSS_ENDPOINT, OSS_SECRET_KEY
from common.local_storage import (
    local_asset_record,
    local_asset_url,
    local_media_type,
    local_path_for_key,
    local_output_prefix_for_asset,
    oss_disabled,
    storage_root,
)

router = APIRouter(prefix="/api/v1", tags=["assets"])

CATEGORY_PREFIX = {
    "character": "image-backups/character/",
    "scene": "image-backups/sence/",
    "bgm": "sound-backups/bgm/",
    "sfx": "sound-backups/sfx/",
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
        output_suffix = local_output_prefix_for_asset(prefix)
        legacy_root = local_path_for_key(prefix)
        roots = [legacy_root] if legacy_root.exists() else []
        roots.extend(
            task_root / output_suffix
            for task_root in (storage_root() / "outputs").glob("*")
            if (task_root / output_suffix).is_dir()
        )
        items = [
            local_asset_record(
                path.relative_to(storage_root()).as_posix(),
                path,
            )
            for root in roots
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
            if obj.key.endswith("/"):  # 目录占位对象
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


class AssetPageOut(BaseModel):
    total: int
    page: int
    page_size: int
    items: list


@router.get("/assets", response_model=AssetPageOut)
def list_assets(
    category: str,
    name: Optional[str] = None,             # 文件名模糊筛选,可选
    start_time: Optional[datetime] = None,  # last_modified 范围,可选
    end_time: Optional[datetime] = None,
    page: int = 1,
    page_size: int = 24,
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

    total = len(items)
    offset = (page - 1) * page_size
    return AssetPageOut(
        total=total, page=page, page_size=page_size,
        items=items[offset:offset + page_size],
    )


@router.get("/assets/download")
def download_asset(key: str):
    """代理下载素材(本地模式从 storage 读取，远程模式从 OSS 读取)。"""
    prefix_ok = any(key.startswith(p) for p in CATEGORY_PREFIX.values())
    local_key_ok = key.startswith(("storage/outputs/", "outputs/"))
    if (not prefix_ok and not (oss_disabled() and local_key_ok)) or ".." in key:
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


class AssetCleanIn(BaseModel):
    category: str
    start_time: Optional[datetime] = None  # last_modified 范围,可选(都不传=该分类全部)
    end_time: Optional[datetime] = None
    dry_run: bool = True  # 默认只预览,确认后传 false 真删


@router.post("/assets/clean")
def clean_assets(body: AssetCleanIn):
    """按分类+时间范围批量删除本地素材或 OSS 素材。"""
    prefix = CATEGORY_PREFIX.get(body.category)
    if prefix is None:
        raise HTTPException(400, f"未知素材分类: {body.category}(可选 {', '.join(CATEGORY_PREFIX)})")

    items = _list_all(prefix)
    if body.start_time:
        items = [i for i in items if datetime.fromisoformat(i["last_modified"]) >= body.start_time]
    if body.end_time:
        items = [i for i in items if datetime.fromisoformat(i["last_modified"]) <= body.end_time]

    keys = [i["key"] for i in items]
    if body.dry_run:
        return {"dry_run": True, "count": len(keys), "keys": keys[:50]}

    if oss_disabled():
        deleted = 0
        for key in keys:
            try:
                path = local_path_for_key(key)
                if path.is_file():
                    path.unlink()
                    deleted += 1
            except (OSError, ValueError):
                continue
        _cache.pop(prefix, None)
        return {"dry_run": False, "count": deleted}

    import oss2
    bucket = _get_bucket()
    deleted = 0
    try:
        for batch_start in range(0, len(keys), 1000):  # batch_delete 单次上限 1000
            batch = keys[batch_start:batch_start + 1000]
            if batch:
                bucket.batch_delete_objects(batch)
                deleted += len(batch)
    except oss2.exceptions.OssError as e:
        _cache.pop(prefix, None)
        raise HTTPException(502, f"OSS 删除失败(已删 {deleted} 个): {e}")

    _cache.pop(prefix, None)
    return {"dry_run": False, "count": deleted}
