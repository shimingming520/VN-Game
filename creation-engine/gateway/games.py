"""游戏库 API:展示中台分发过来的创作推广游戏"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session, select

from common.models import PromoGame, PromoGameDistribution, PlatformUser, engine
from auth.deps import get_current_user

router = APIRouter(prefix="/api/v1", tags=["library"])


class LibraryGame(BaseModel):
    id: int
    name: str
    url: str
    cover: str
    description: str


class GameListOut(BaseModel):
    items: list[LibraryGame]
    total: int


@router.get("/library/games", response_model=GameListOut)
def list_library_games(user: PlatformUser = Depends(get_current_user)):
    """游戏库列表:仅展示已被中台分发的推广游戏,按分发时间倒序。"""
    with Session(engine) as session:
        rows = session.exec(
            select(PromoGame)
            .join(PromoGameDistribution, PromoGameDistribution.promo_game_id == PromoGame.id)
            .order_by(PromoGameDistribution.distributed_at.desc())
        ).all()
    items = [
        LibraryGame(
            id=g.id, name=g.name, url=g.url, cover=g.cover, description=g.description
        )
        for g in rows
    ]
    return {"items": items, "total": len(items)}
