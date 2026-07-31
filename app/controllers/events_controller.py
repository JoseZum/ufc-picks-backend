"""
Controlador de eventos - Endpoints relacionados con eventos
"""

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from datetime import date, datetime

from app.core.dependencies import Database
from app.services.event_service import EventService, EventNotFoundError


router = APIRouter(prefix="/events", tags=["events"])


class LocationResponse(BaseModel):
    """Ubicación de una pelea."""
    venue: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None


class EventResponse(BaseModel):
    """Datos básicos del evento."""
    id: int
    name: str
    subtitle: Optional[str] = None
    date: date
    start_time_et: Optional[str] = None
    timezone: Optional[str] = None
    location: Optional[dict] = None
    status: str
    total_bouts: int
    poster_image_url: Optional[str] = None
    hero_image_url: Optional[str] = None
    event_art_url: Optional[str] = None
    picks_locked: bool = False
    picks_lock_override: Optional[str] = None
    card_start_time_utc: Optional[datetime] = None
    picks_lock_time_utc: Optional[datetime] = None
    section_start_times_utc: dict[str, datetime] = Field(default_factory=dict)
    section_lock_times_utc: dict[str, datetime] = Field(default_factory=dict)
    timing_source: Optional[str] = None
    is_title_fight: bool = False  # True si la pelea principal es por título
    is_bmf_title_fight: bool = False  # True si la pelea principal es por el cinturón BMF


class EventDetailResponse(EventResponse):
    """Detalles completos del evento."""
    promotion: str
    url: str


@router.get("", response_model=list[EventResponse])
async def get_events(
    db: Database,
    status: Optional[str] = Query(None, description="Filtrar por: scheduled, completed"),
    limit: int = Query(20, ge=1, le=50)
):
    """Obtiene lista de eventos próximos y recientes."""
    event_service = EventService(db)
    events = await event_service.get_events_by_status(status, limit)
    # Get event IDs to check for event_art
    event_ids = [e.id for e in events]
    
    # Check which events have event_art in MongoDB
    events_with_art = set()
    if event_ids:
        cursor = db["events"].find(
            {"id": {"$in": event_ids}, "event_art": {"$exists": True, "$ne": None}},
            {"id": 1}
        )
        async for doc in cursor:
            events_with_art.add(doc["id"])

    # Determinar qué eventos tienen pelea principal por título (dorado) o BMF (plateado)
    title_event_ids = await _get_main_event_flag_ids(db, event_ids, "is_title_fight")
    bmf_event_ids = await _get_main_event_flag_ids(db, event_ids, "is_bmf_title_fight")

    # Procesar cada evento y obtener su poster URL
    result = []
    for e in events:
        poster_url = _get_poster_url(
            getattr(e, 'poster_image_url', None),
            getattr(e, 'poster_image_source', None),
        )
        event_art_url = f"/events/{e.id}/event-art" if e.id in events_with_art else None
        result.append(
            EventResponse(
                id=e.id,
                name=e.name,
                subtitle=e.subtitle,
                date=e.date,
                start_time_et=getattr(e, 'start_time_et', None),
                timezone=getattr(e, 'timezone', None),
                location=e.location,
                status=e.status,
                total_bouts=e.total_bouts,
                poster_image_url=poster_url,
                hero_image_url=_get_hero_url(
                    getattr(e, 'hero_image_url', None),
                    getattr(e, 'hero_image_source', None),
                ),
                event_art_url=event_art_url,
                picks_locked=getattr(e, 'picks_locked', False),
                picks_lock_override=getattr(e, 'picks_lock_override', None),
                card_start_time_utc=getattr(e, 'card_start_time_utc', None),
                picks_lock_time_utc=getattr(e, 'picks_lock_time_utc', None),
                section_start_times_utc=getattr(
                    e, 'section_start_times_utc', {}
                ),
                section_lock_times_utc=getattr(
                    e, 'section_lock_times_utc', {}
                ),
                timing_source=getattr(e, 'timing_source', None),
                is_title_fight=e.id in title_event_ids,
                is_bmf_title_fight=e.id in bmf_event_ids
            )
        )

    return result


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

    poster_url = _get_poster_url(
        getattr(event, 'poster_image_url', None),
        getattr(event, 'poster_image_source', None),
    )
    
    # Check if event has event_art
    event_doc = await db["events"].find_one(
        {"id": event_id, "event_art": {"$exists": True, "$ne": None}},
        {"_id": 1}
    )
    event_art_url = f"/events/{event_id}/event-art" if event_doc else None

    return EventDetailResponse(
        id=event.id,
        name=event.name,
        subtitle=event.subtitle,
        date=event.date,
        start_time_et=getattr(event, 'start_time_et', None),
        timezone=getattr(event, 'timezone', None),
        location=event.location,
        status=event.status,
        total_bouts=event.total_bouts,
        poster_image_url=poster_url,
        hero_image_url=_get_hero_url(
            getattr(event, 'hero_image_url', None),
            getattr(event, 'hero_image_source', None),
        ),
        event_art_url=event_art_url,
        promotion=event.promotion,
        url=event.url,
        picks_locked=getattr(event, 'picks_locked', False),
        picks_lock_override=getattr(event, 'picks_lock_override', None),
        card_start_time_utc=getattr(event, 'card_start_time_utc', None),
        picks_lock_time_utc=getattr(event, 'picks_lock_time_utc', None),
        section_start_times_utc=getattr(
            event, 'section_start_times_utc', {}
        ),
        section_lock_times_utc=getattr(
            event, 'section_lock_times_utc', {}
        ),
        timing_source=getattr(event, 'timing_source', None),
        is_title_fight=bool(await _get_main_event_flag_ids(db, [event_id], "is_title_fight")),
        is_bmf_title_fight=bool(await _get_main_event_flag_ids(db, [event_id], "is_bmf_title_fight"))
    )


@router.get("/{event_id}/event-art")
async def get_event_art(
    event_id: int,
    db: Database
):
    """
    Obtener el event art (imagen) de un evento.
    
    Devuelve los bytes de la imagen directamente desde MongoDB.
    El frontend puede usar esta URL como src de imagen.
    """
    # Buscar evento con el campo event_art
    event = await db["events"].find_one(
        {"id": event_id},
        {"event_art": 1, "event_art_content_type": 1}
    )
    
    if not event:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Event {event_id} not found"
        )
    
    if "event_art" not in event or event["event_art"] is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Event {event_id} has no event art"
        )
    
    content_type = event.get("event_art_content_type", "image/avif")
    
    return Response(
        content=event["event_art"],
        media_type=content_type,
        headers={
            "Cache-Control": "public, max-age=86400",  # Cache 24 hours
            "Content-Disposition": f"inline; filename=event-{event_id}.{content_type.split('/')[-1]}"
        }
    )


async def _get_main_event_flag_ids(db, event_ids: list[int], flag_field: str) -> set[int]:
    """
    Devuelve el set de event_ids cuya pelea principal tiene `flag_field`=True.

    Hace una sola consulta para todos los eventos pedidos (eficiente). Se usa para
    detectar eventos con main event por título (is_title_fight) o por el cinturón
    BMF (is_bmf_title_fight).
    """
    if not event_ids:
        return set()

    matched: set[int] = set()
    cursor = db["bouts"].find(
        {"event_id": {"$in": event_ids}, "is_main_event": True, flag_field: True},
        {"event_id": 1}
    )
    async for doc in cursor:
        matched.add(doc["event_id"])
    return matched


def _get_poster_url(
    source_url: Optional[str],
    source_kind: Optional[str],
) -> Optional[str]:
    """Prefer Wikipedia posters and allow the explicit official UFC fallback."""
    if source_kind not in {
        "wikipedia_source",
        "wikipedia_file",
        "ufc_official_fallback",
    }:
        return None
    if not source_url or not source_url.startswith(("https://", "http://")):
        return None
    return source_url


def _get_hero_url(
    source_url: Optional[str],
    source_kind: Optional[str],
) -> Optional[str]:
    """Return only official UFC XL 2x hero art."""
    if source_kind != "ufc_official_xl_2x":
        return None
    if not source_url or not source_url.startswith(("https://", "http://")):
        return None
    return source_url
