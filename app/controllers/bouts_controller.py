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


@router.get("/events/{event_id}/fights/{bout_id}", response_model=BoutResponse)
async def get_bout_details(
    event_id: int,
    bout_id: int,
    db: Database
):
    """
    Obtener detalles completos de una pelea específica.

    Devuelve toda la información de la pelea incluyendo:
    - Información detallada de ambos peleadores
    - Rankings, records, odds
    - Físico (altura, reach, peso)
    - Gimnasios, nacionalidades
    - Resultado (si está disponible)
    """
    # Buscar la pelea básica
    bout_data = await db["bouts"].find_one({"id": bout_id, "event_id": event_id})

    if not bout_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Bout {bout_id} not found in event {event_id}"
        )

    # Buscar los detalles de la pelea en la colección bout_details
    bout_details = await db["bout_details"].find_one({"bout_id": bout_id})

    # Merge fighters data - usar bout_details si existe, sino usar bout básico
    fighters = bout_data.get("fighters", {})
    if bout_details and "fighters" in bout_details:
        # Usar los datos detallados que incluyen todos los campos extras
        fighters = bout_details.get("fighters", {})

    # Obtener resultado de bout_details si existe
    result = None
    if bout_details and "result" in bout_details:
        result = bout_details.get("result")
    elif "result" in bout_data:
        result = bout_data.get("result")

    # Mapear los datos a la respuesta
    return BoutResponse(
        id=bout_data.get("id"),
        event_id=bout_data.get("event_id"),
        weight_class=bout_data.get("weight_class", "Unknown"),
        gender=bout_data.get("gender", "M"),
        rounds_scheduled=bout_data.get("scheduled_rounds", 3),
        is_title_fight=bout_data.get("is_title_fight", False),
        status=bout_data.get("status", "scheduled"),
        fighters=fighters,
        result=result
    )
