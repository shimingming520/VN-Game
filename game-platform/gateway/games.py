"""游戏管理 API:
  - 视觉小说:代理 CMS POST /business/game/list(用当前登录用户的 OAuth token)
  - 创作推广:平台库 PromoGame 表 CRUD + 封面上传 OSS(game--oss bucket)
  - 分发:PromoGameDistribution 记录表,分发=插记录/撤销=删记录,创作引擎游戏库据此展示
"""
import uuid
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlmodel import Session, select

from common.models import PromoGame, PromoGameDistribution, PlatformUser, engine
from common.local_storage import local_asset_url, local_path_for_key
from auth.deps import get_current_user
from gateway.cms_publish import _resolve_cms_token, _cms_request, CMS_OK_CODES

router = APIRouter(prefix="/api/v1", tags=["games"])


# ── 视觉小说:CMS 游戏列表代理 ──

@router.get("/admin/games/cms-list")
def list_cms_games(
    page: int = 1, page_size: int = 20, status: int = 0,
    user: PlatformUser = Depends(get_current_user),
):
    """拉 CMS 游戏列表,归一化成 {items:[{id,name,cover,link,...}], total}。

    status 为游戏状态过滤(CMS 约定):0=全部/不过滤 1=已下架 2=审核中 3=已发布。
    """
    token = _resolve_cms_token(user)
    data = _cms_request(
        "POST", "/business/game/list", token,
        json_body={"page": page, "pageSize": page_size, "status": status},
    )
    if data.get("code") not in CMS_OK_CODES:
        raise HTTPException(502, f"CMS 游戏列表返回异常: code={data.get('code')} msg={data.get('msg')}")
    block = data.get("data") or {}
    rows = block.get("data") or []
    items = [
        {
            "id": g.get("id"),
            "name": g.get("name", ""),
            "cover": g.get("cover", ""),
            "link": g.get("link", ""),
            "description": g.get("description", ""),
            "status": g.get("status"),
            "status_name": g.get("status_name", ""),
        }
        for g in rows
    ]
    return {"items": items, "total": block.get("total", len(items))}


# ── 创作推广:平台自维护 ──

class PromoGameOut(BaseModel):
    id: int
    name: str
    url: str
    cover: str
    description: str
    created_at: datetime


class PromoGameIn(BaseModel):
    name: str
    url: str = ""
    cover: str = ""
    description: str = ""


@router.get("/admin/promo-games")
def list_promo_games(
    page: int = 1, page_size: int = 20,
    user: PlatformUser = Depends(get_current_user),
):
    """创作推广游戏列表,倒序 created_at,SQL 分页。"""
    with Session(engine) as session:
        total = len(session.exec(select(PromoGame.id)).all())
        rows = session.exec(
            select(PromoGame)
            .order_by(PromoGame.created_at.desc(), PromoGame.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
    items = [
        PromoGameOut(
            id=g.id, name=g.name, url=g.url, cover=g.cover,
            description=g.description, created_at=g.created_at,
        )
        for g in rows
    ]
    return {"items": items, "total": total}


@router.post("/admin/promo-games/cover")
async def upload_promo_cover(
    file: UploadFile = File(...),
    user: PlatformUser = Depends(get_current_user),
):
    """上传封面到本地 storage，返回本地素材 URL 供建卡时填 cover。"""
    ext = ""
    if file.filename and "." in file.filename:
        ext = "." + file.filename.rsplit(".", 1)[-1].lower()
    key = f"promo_games/{uuid.uuid4().hex}/cover{ext}"
    content = await file.read()
    try:
        path = local_path_for_key(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    except Exception as e:
        raise HTTPException(500, f"本地封面保存失败: {e}")
    return {"cover_url": local_asset_url(key)}


@router.post("/admin/promo-games", response_model=PromoGameOut)
def create_promo_game(
    body: PromoGameIn,
    user: PlatformUser = Depends(get_current_user),
):
    if not body.name.strip():
        raise HTTPException(400, "游戏名不能为空")
    game = PromoGame(
        name=body.name.strip(), url=body.url.strip(),
        cover=body.cover.strip(), description=body.description.strip(),
    )
    with Session(engine) as session:
        session.add(game)
        session.commit()
        session.refresh(game)
    return PromoGameOut(
        id=game.id, name=game.name, url=game.url, cover=game.cover,
        description=game.description, created_at=game.created_at,
    )


@router.delete("/admin/promo-games/{game_id}")
def delete_promo_game(
    game_id: int,
    user: PlatformUser = Depends(get_current_user),
):
    """删库(同时清理分发记录;OSS 封面暂不删,避免误删共享资源)。"""
    with Session(engine) as session:
        game = session.get(PromoGame, game_id)
        if game is None:
            raise HTTPException(404, f"promo_game not found: {game_id}")
        session.delete(game)
        for rec in session.exec(
            select(PromoGameDistribution).where(PromoGameDistribution.promo_game_id == game_id)
        ).all():
            session.delete(rec)
        session.commit()
    return {"ok": True}


# ── 分发到创作引擎游戏库 ──

class DistributeIn(BaseModel):
    game_ids: list[int]


@router.get("/admin/promo-games/distributed")
def list_distributed_ids(user: PlatformUser = Depends(get_current_user)):
    """已分发的 promo_game_id 列表,供前端回显选中态。"""
    with Session(engine) as session:
        ids = session.exec(select(PromoGameDistribution.promo_game_id)).all()
    return {"ids": list(ids)}


@router.post("/admin/promo-games/distribute")
def distribute_promo_games(
    body: DistributeIn,
    user: PlatformUser = Depends(get_current_user),
):
    """批量分发(幂等:已分发的跳过,只插新记录)。"""
    if not body.game_ids:
        raise HTTPException(400, "game_ids 不能为空")
    with Session(engine) as session:
        exist_games = set(
            session.exec(select(PromoGame.id).where(PromoGame.id.in_(body.game_ids))).all()
        )
        missing = [gid for gid in body.game_ids if gid not in exist_games]
        if missing:
            raise HTTPException(404, f"promo_game 不存在: {missing}")
        already = set(
            session.exec(
                select(PromoGameDistribution.promo_game_id).where(
                    PromoGameDistribution.promo_game_id.in_(body.game_ids)
                )
            ).all()
        )
        added = 0
        for gid in body.game_ids:
            if gid in already:
                continue
            session.add(PromoGameDistribution(promo_game_id=gid, distributed_by=user.main_site_uid))
            added += 1
        session.commit()
    return {"ok": True, "distributed": added, "skipped": len(body.game_ids) - added}


@router.post("/admin/promo-games/recall")
def recall_promo_games(
    body: DistributeIn,
    user: PlatformUser = Depends(get_current_user),
):
    """批量撤销分发(删记录,不存在的忽略)。"""
    if not body.game_ids:
        raise HTTPException(400, "game_ids 不能为空")
    with Session(engine) as session:
        recs = session.exec(
            select(PromoGameDistribution).where(
                PromoGameDistribution.promo_game_id.in_(body.game_ids)
            )
        ).all()
        for rec in recs:
            session.delete(rec)
        session.commit()
    return {"ok": True, "recalled": len(recs)}
