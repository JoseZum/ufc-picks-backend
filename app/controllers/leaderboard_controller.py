"""
Controlador de leaderboards - Endpoints de clasificación

Las tablas de clasificación se generan de forma previa por procesos offline.
Este controlador sirve los datos en caché.
"""

from typing import Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.core.dependencies import Database, CurrentUser
from app.services.leaderboard_service import LeaderboardService


router = APIRouter(prefix="/leaderboard", tags=["leaderboard"])


class LeaderboardEntryResponse(BaseModel):
    """Entrada del leaderboard (usuario y estadísticas)."""
    rank: int
    user_id: str
    username: str
    avatar_url: Optional[str] = None
    total_points: int
    accuracy: float
    picks_total: int
    picks_correct: int


class LeaderboardResponse(BaseModel):
    """Leaderboard con las entradas y la posición del usuario (opcional)."""
    entries: list[LeaderboardEntryResponse]
    user_position: Optional[LeaderboardEntryResponse] = None


@router.get("/global", response_model=LeaderboardResponse)
async def get_global_leaderboard(
    db: Database,
    year: Optional[int] = Query(None, description="Filter by year"),
    limit: int = Query(100, ge=1, le=500)
):
    """
    Obtener el leaderboard global (todos los eventos).
    """
    leaderboard_service = LeaderboardService(db)
    entries = await leaderboard_service.get_global_leaderboard(limit, year)

    return LeaderboardResponse(
        entries=[
            LeaderboardEntryResponse(
                rank=idx + 1,
                user_id=e.user_id,
                username=e.username,
                avatar_url=e.avatar_url,
                total_points=e.total_points,
                accuracy=e.accuracy,
                picks_total=e.picks_total,
                picks_correct=e.picks_correct
            )
            for idx, e in enumerate(entries)
        ]
    )


@router.get("/event/{event_id}", response_model=LeaderboardResponse)
async def get_event_leaderboard(
    event_id: int,
    db: Database,
    limit: int = Query(100, ge=1, le=500)
):
    """
    Obtener el leaderboard de un evento específico.
    """
    leaderboard_service = LeaderboardService(db)
    entries = await leaderboard_service.get_event_leaderboard(event_id, limit)

    return LeaderboardResponse(
        entries=[
            LeaderboardEntryResponse(
                rank=idx + 1,
                user_id=e.user_id,
                username=e.username,
                avatar_url=e.avatar_url,
                total_points=e.total_points,
                accuracy=e.accuracy,
                picks_total=e.picks_total,
                picks_correct=e.picks_correct
            )
            for idx, e in enumerate(entries)
        ]
    )


@router.get("/category/{category}", response_model=LeaderboardResponse)
async def get_category_leaderboard(
    category: str,
    db: Database,
    year: Optional[int] = Query(None, description="Filter by year"),
    limit: int = Query(100, ge=1, le=500)
):
    """
    Obtener leaderboard por categoría.

    Categorías: global, main_events, main_card, prelims, early_prelims
    """
    leaderboard_service = LeaderboardService(db)
    entries = await leaderboard_service.get_category_leaderboard(category, limit, year)

    return LeaderboardResponse(
        entries=[
            LeaderboardEntryResponse(
                rank=idx + 1,
                user_id=e.user_id,
                username=e.username,
                avatar_url=e.avatar_url,
                total_points=e.total_points,
                accuracy=e.accuracy,
                picks_total=e.picks_total,
                picks_correct=e.picks_correct
            )
            for idx, e in enumerate(entries)
        ]
    )


@router.get("/me", response_model=dict)
async def get_my_leaderboard_position(
    user: CurrentUser,
    db: Database,
    category: str = Query("global", description="Leaderboard category")
):
    """
    Obtener la posición del usuario actual en el leaderboard.
    """
    leaderboard_service = LeaderboardService(db)
    result = await leaderboard_service.get_user_rank(user.id, category)

    if not result:
        return {"rank": None, "entry": None}

    entry = result["entry"]

    return {
        "rank": result["rank"],
        "entry": LeaderboardEntryResponse(
            rank=result["rank"],
            user_id=entry.user_id,
            username=entry.username,
            avatar_url=entry.avatar_url,
            total_points=entry.total_points,
            accuracy=entry.accuracy,
            picks_total=entry.picks_total,
            picks_correct=entry.picks_correct
        )
    }
