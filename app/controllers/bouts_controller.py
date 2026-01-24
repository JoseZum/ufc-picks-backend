"""
Controlador de peleas - Endpoints relacionados con bouts
"""

from typing import Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.core.dependencies import Database
from app.services.event_service import EventService, EventNotFoundError


router = APIRouter(tags=["bouts"])


class FighterResponse(BaseModel):
    """Datos del peleador en el momento de la pelea."""
    fighter_name: str
    corner: str
    nationality: str
    record_at_fight: dict
    ranking: Optional[dict] = None
    age_at_fight_years: int
    height_cm: Optional[int] = None
    reach_cm: Optional[int] = None
    fighting_out_of: Optional[str] = None
    tapology_id: Optional[str] = None
    tapology_url: Optional[str] = None
    profile_image_url: Optional[str] = None


class BoutResultResponse(BaseModel):
    """Resultado de la pelea."""
    winner: str
    method: str
    round: Optional[int] = None
    time: Optional[str] = None


class BoutResponse(BaseModel):
    """Datos de la pelea devueltos por la API."""
    id: int
    event_id: int
    weight_class: str
    gender: str
    rounds_scheduled: int
    is_title_fight: bool
    status: str
    fighters: dict
    result: Optional[dict] = None


@router.get("/events/{event_id}/bouts", response_model=list[BoutResponse])
async def get_event_bouts(
    event_id: int,
    db: Database
):
    """
    Obtener todas las peleas de un evento.

    Devuelve las peleas con la información de los peleadores y el resultado (si está disponible).
    """
    event_service = EventService(db)

    try:
        bouts = await event_service.get_event_bouts(event_id)
    except EventNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Event {event_id} not found"
        )

    return [
        BoutResponse(
            id=b.id,
            event_id=b.event_id,
            weight_class=b.weight_class,
            gender=b.gender,
            rounds_scheduled=b.rounds_scheduled,
            is_title_fight=b.is_title_fight,
            status=b.status,
            fighters=b.fighters,
            result=b.result
        )
        for b in bouts
    ]
