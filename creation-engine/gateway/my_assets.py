"""我的资产 API:查询用户生产历史，扫描 OSS 或本地 storage。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from typing import Optional

from common.models import PlatformUser, NovelTask, UserFolder, SavedMaterial, engine
from auth.deps import get_current_user
from common.config import OSS_ACCESS_KEY, OSS_SECRET_KEY, OSS_ENDPOINT, ASSET_OSS_BUCKET, ASSET_BASE_URL
from common.local_storage import (
    local_asset_record,
    local_asset_url,
    local_path_for_key,
    oss_disabled,
    storage_root,
)

router = APIRouter(prefix="/api/v1", tags=["my_assets"])

_bucket = None


def _get_bucket():
    global _bucket
    if oss_disabled():
        return None
    if _bucket is None:
        import oss2
        if not OSS_ACCESS_KEY or not OSS_SECRET_KEY:
            raise HTTPException(500, "OSS 凭证未配置")
        auth = oss2.Auth(OSS_ACCESS_KEY, OSS_SECRET_KEY)
        _bucket = oss2.Bucket(auth, f"https://{OSS_ENDPOINT}", ASSET_OSS_BUCKET)
    return _bucket


class MyAssetTask(BaseModel):
    task_id: str
    project_name: str
    created_at: str
    files: list[dict]


class MyAssetsOut(BaseModel):
    items: list[MyAssetTask]
    total: int


@router.get("/my-assets", response_model=MyAssetsOut)
def get_my_assets(user: PlatformUser = Depends(get_current_user)):
    """查询当前用户的所有生产任务，按 task_id 扫描 OSS 或本地产物。"""
    cms_uid = user.main_site_uid
    with Session(engine) as session:
        stmt = (
            select(NovelTask)
            .where(NovelTask.user_id == cms_uid, NovelTask.is_deleted == False)
            .order_by(NovelTask.create_time.desc())
        )
        tasks = session.exec(stmt).all()

    result = []
    for task in tasks:
        files = []
        if oss_disabled():
            local_root = storage_root() / "outputs" / task.task_id
            if local_root.is_dir():
                for path in local_root.rglob("*"):
                    if path.is_file():
                        key = f"storage/{path.relative_to(storage_root()).as_posix()}"
                        files.append(local_asset_record(key, path))
        else:
            import oss2
            bucket = _get_bucket()
            prefix = f"{task.task_id}/"
            try:
                for obj in oss2.ObjectIterator(bucket, prefix=prefix, max_keys=1000):
                    if obj.key.endswith("/"):
                        continue
                    files.append({
                        "key": obj.key,
                        "name": obj.key.rsplit("/", 1)[-1],
                        "size": obj.size,
                        "last_modified": obj.last_modified,
                        "url": f"{ASSET_BASE_URL}/{obj.key}" if ASSET_BASE_URL else obj.key,
                    })
            except oss2.exceptions.OssError:
                # OSS 扫描失败不阻断整体列表,该任务记录 0 文件
                pass
        files.sort(key=lambda item: item.get("last_modified", 0), reverse=True)

        result.append(MyAssetTask(
            task_id=task.task_id,
            project_name=task.project_name or task.generated_project_name or task.novel_filename,
            created_at=task.created_at.isoformat() if task.created_at else "",
            files=files,
        ))

    return {"items": result, "total": len(result)}


# ── 用户文件夹(资产页“新建文件夹”持久化) ──
class FolderIn(BaseModel):
    name: str


class FolderOut(BaseModel):
    id: int
    name: str
    created_at: str
    material_count: int = 0


def _folder_owned(session: Session, folder_id: int, user_uid: str) -> UserFolder:
    folder = session.get(UserFolder, folder_id)
    if folder is None or folder.user_id != user_uid:
        raise HTTPException(404, "文件夹不存在")
    return folder


@router.get("/folders", response_model=list[FolderOut])
def list_folders(user: PlatformUser = Depends(get_current_user)):
    with Session(engine) as session:
        rows = session.exec(
            select(UserFolder)
            .where(UserFolder.user_id == user.main_site_uid)
            .order_by(UserFolder.created_at.desc())
        ).all()
        counts = {}
        for m in session.exec(
            select(SavedMaterial).where(SavedMaterial.user_id == user.main_site_uid)
        ).all():
            counts[m.folder_id] = counts.get(m.folder_id, 0) + 1
    return [
        FolderOut(
            id=f.id,
            name=f.name,
            created_at=f.created_at.isoformat() if f.created_at else "",
            material_count=counts.get(f.id, 0),
        )
        for f in rows
    ]


@router.post("/folders", response_model=FolderOut)
def create_folder(body: FolderIn, user: PlatformUser = Depends(get_current_user)):
    name = (body.name or "").strip() or "未命名文件夹"
    with Session(engine) as session:
        folder = UserFolder(user_id=user.main_site_uid, name=name)
        session.add(folder)
        session.commit()
        session.refresh(folder)
    return FolderOut(id=folder.id, name=folder.name, created_at=folder.created_at.isoformat(), material_count=0)


@router.delete("/folders/{folder_id}")
def delete_folder(folder_id: int, user: PlatformUser = Depends(get_current_user)):
    """删除文件夹,同时清掉夹内已保存素材记录(不动 OSS)"""
    with Session(engine) as session:
        _folder_owned(session, folder_id, user.main_site_uid)
        for m in session.exec(
            select(SavedMaterial).where(
                SavedMaterial.user_id == user.main_site_uid,
                SavedMaterial.folder_id == folder_id,
            )
        ).all():
            session.delete(m)
        folder = session.get(UserFolder, folder_id)
        session.delete(folder)
        session.commit()
    return {"ok": True}


# ── 已保存到我的资产的素材 ──
class SavedMaterialOut(BaseModel):
    id: int
    folder_id: Optional[int]
    key: str
    name: str
    url: str


class MoveIn(BaseModel):
    target_folder_id: Optional[int] = None


class CopyIn(BaseModel):
    target_folder_id: Optional[int] = None


@router.get("/saved-materials", response_model=list[SavedMaterialOut])
def list_saved_materials(
    folder_id: Optional[int] = None,
    user: PlatformUser = Depends(get_current_user),
):
    """列出已保存素材;传 folder_id 只看某文件夹,不传看全部"""
    with Session(engine) as session:
        stmt = select(SavedMaterial).where(SavedMaterial.user_id == user.main_site_uid)
        if folder_id is not None:
            stmt = stmt.where(SavedMaterial.folder_id == folder_id)
        stmt = stmt.order_by(SavedMaterial.created_at.desc())
        rows = session.exec(stmt).all()
    return [
        SavedMaterialOut(
            id=r.id,
            folder_id=r.folder_id,
            key=r.asset_key,
            name=r.name or r.asset_key.rsplit("/", 1)[-1],
            url=r.url or (
                local_asset_url(r.asset_key)
                if oss_disabled()
                else (f"{ASSET_BASE_URL}/{r.asset_key}" if ASSET_BASE_URL else r.asset_key)
            ),
        )
        for r in rows
    ]


@router.post("/saved-materials/{material_id}/move")
def move_saved_material(material_id: int, body: MoveIn, user: PlatformUser = Depends(get_current_user)):
    """把已保存素材移动到另一个文件夹(target 为空=未分类)"""
    with Session(engine) as session:
        m = session.get(SavedMaterial, material_id)
        if m is None or m.user_id != user.main_site_uid:
            raise HTTPException(404, "素材不存在")
        if body.target_folder_id is not None:
            _folder_owned(session, body.target_folder_id, user.main_site_uid)
        m.folder_id = body.target_folder_id
        session.add(m)
        session.commit()
    return {"ok": True}


@router.post("/saved-materials/{material_id}/copy")
def copy_saved_material(material_id: int, body: CopyIn, user: PlatformUser = Depends(get_current_user)):
    """把已保存素材复制到另一个文件夹(新建一条记录)"""
    with Session(engine) as session:
        m = session.get(SavedMaterial, material_id)
        if m is None or m.user_id != user.main_site_uid:
            raise HTTPException(404, "素材不存在")
        if body.target_folder_id is not None:
            _folder_owned(session, body.target_folder_id, user.main_site_uid)
        exists = session.exec(
            select(SavedMaterial).where(
                SavedMaterial.user_id == user.main_site_uid,
                SavedMaterial.asset_key == m.asset_key,
                SavedMaterial.folder_id == body.target_folder_id,
            )
        ).first()
        if exists is None:
            session.add(SavedMaterial(
                user_id=user.main_site_uid,
                folder_id=body.target_folder_id,
                asset_key=m.asset_key,
                name=m.name,
                url=m.url,
            ))
            session.commit()
    return {"ok": True}


@router.delete("/saved-materials/{material_id}")
def delete_saved_material(material_id: int, user: PlatformUser = Depends(get_current_user)):
    """从我的资产里移除一条已保存素材(不动 OSS)"""
    with Session(engine) as session:
        m = session.get(SavedMaterial, material_id)
        if m is None or m.user_id != user.main_site_uid:
            raise HTTPException(404, "素材不存在")
        session.delete(m)
        session.commit()
    return {"ok": True}
