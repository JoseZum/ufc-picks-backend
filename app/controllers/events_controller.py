"""
Controlador de eventos - Endpoints relacionados con eventos
"""

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from datetime import date

from app.core.dependencies import Database
from app.services.event_service import EventService, EventNotFoundError


router = APIRouter(prefix="/events", tags=["events"])


class LocationResponse(BaseModel):
    """Ubicación del evento."""
    venue: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None


class EventResponse(BaseModel):
    """Datos del evento devueltos por la API."""
    id: int
    name: str
    subtitle: Optional[str] = None
    date: date
    location: Optional[dict] = None
    status: str
    total_bouts: int
    poster_image_url: Optional[str] = None


class EventDetailResponse(EventResponse):
    """Respuesta detallada del evento, incluyendo peleas."""
    promotion: str
    url: str


@router.get("", response_model=list[EventResponse])
async def get_events(
    db: Database,
    status: Optional[str] = Query(None, description="Filter by status: scheduled, completed"),
    limit: int = Query(20, ge=1, le=50)
):
    """
    Obtener lista de eventos.

    Devuelve eventos próximos y recientes; opcionalmente filtrados por estado.
    """
    event_service = EventService(db)
    events = await event_service.get_events_by_status(status, limit)

    return [
        EventResponse(
            id=e.id,
            name=e.name,
            subtitle=e.subtitle,
            date=e.date,
            location=e.location,
            status=e.status,
            total_bouts=e.total_bouts,
            poster_image_url=getattr(e, 'poster_image_url', None)
        )
        for e in events
    ]


@router.get("/{event_id}", response_model=EventDetailResponse)
async def get_event(
    event_id: int,
    db: Database
):
    """Obtener un evento por su ID."""
    event_service = EventService(db)

    try:
        event = await event_service.get_event(event_id)
    except EventNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Event {event_id} not found"
        )

    return EventDetailResponse(
        id=event.id,
        name=event.name,
        subtitle=event.subtitle,
        date=event.date,
        location=event.location,
        status=event.status,
        total_bouts=event.total_bouts,
        poster_image_url=getattr(event, 'poster_image_url', None),
        promotion=event.promotion,
        url=event.url
    )
