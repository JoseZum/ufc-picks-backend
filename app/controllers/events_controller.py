"""
Controlador de eventos - Endpoints relacionados con eventos
"""

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import Response
from pydantic import BaseModel
from datetime import date

from app.core.dependencies import Database
from app.services.event_service import EventService, EventNotFoundError
from app.services.s3_service import get_s3_service


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
    event_art_url: Optional[str] = None
    picks_locked: bool = False
    is_title_fight: bool = False  # True si la pelea principal es por título


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
    s3_service = get_s3_service()
    
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

    # Determinar qué eventos tienen pelea principal por título (para destacarlos en dorado)
    title_event_ids = await _get_title_fight_event_ids(db, event_ids)

    # Procesar cada evento y obtener su poster URL
    result = []
    for e in events:
        poster_url = await _get_poster_url(e.id, getattr(e, 'poster_image_url', None), s3_service)
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
                event_art_url=event_art_url,
                picks_locked=getattr(e, 'picks_locked', False),
                is_title_fight=e.id in title_event_ids
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
    s3_service = get_s3_service()

    try:
        event = await event_service.get_event(event_id)
    except EventNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Event {event_id} not found"
        )

    poster_url = await _get_poster_url(event.id, getattr(event, 'poster_image_url', None), s3_service)
    
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
        event_art_url=event_art_url,
        promotion=event.promotion,
        url=event.url,
        picks_locked=getattr(event, 'picks_locked', False),
        is_title_fight=bool(await _get_title_fight_event_ids(db, [event_id]))
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


async def _get_title_fight_event_ids(db, event_ids: list[int]) -> set[int]:
    """
    Devuelve el set de event_ids cuya pelea principal es por título.

    Hace una sola consulta para todos los eventos pedidos (eficiente).
    Un evento es "title fight" si su bout principal (is_main_event=True) tiene
    is_title_fight=True.
    """
    if not event_ids:
        return set()

    title_ids: set[int] = set()
    cursor = db["bouts"].find(
        {"event_id": {"$in": event_ids}, "is_main_event": True, "is_title_fight": True},
        {"event_id": 1}
    )
    async for doc in cursor:
        title_ids.add(doc["event_id"])
    return title_ids


async def _get_poster_url(event_id: int, proxy_url: Optional[str], s3_service) -> Optional[str]:
    """
    Helper para obtener la URL del poster.

    Estrategia:
    1. Verificar si existe poster en S3 (ufc-posters/ufc{id}.jpeg)
    2. Si existe y CloudFront está configurado, devolver la URL de CloudFront
    3. Si no existe, devolver proxy_url de MongoDB

    Args:
        event_id: ID del evento
        proxy_url: URL de proxy desde MongoDB (/proxy/tapology/...)
        s3_service: Servicio S3

    Returns:
        URL de CloudFront si existe en S3, o proxy_url de MongoDB si no
    """
    if not proxy_url:
        return None

    # Verificar si CloudFront está configurado
    if not s3_service.is_cloudfront_configured():
        return proxy_url

    try:
        # Formato en S3: ufc-posters/ufc{numero}.jpeg
        s3_key = f"ufc-posters/ufc{event_id}.jpeg"

        # Verificar si existe en S3
        exists = await s3_service.image_exists(s3_key)

        if exists:
            # Si existe en S3, usar CloudFront
            cloudfront_url = s3_service.get_cloudfront_url(s3_key)
            return cloudfront_url if cloudfront_url else proxy_url
        else:
            # Si no existe en S3, usar el proxy del backend
            return proxy_url

    except Exception:
        # Si hay error verificando S3, usar proxy como fallback
        return proxy_url
