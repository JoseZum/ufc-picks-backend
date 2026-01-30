"""
Controlador de eventos - Endpoints relacionados con eventos
"""

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from datetime import date

from app.core.dependencies import Database
from app.services.event_service import EventService, EventNotFoundError
from app.services.s3_service import get_s3_service


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
    picks_locked: bool = False


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
    s3_service = get_s3_service()

    # Procesar cada evento y obtener su poster URL
    result = []
    for e in events:
        poster_url = await _get_poster_url(e.id, getattr(e, 'poster_image_url', None), s3_service)
        result.append(
            EventResponse(
                id=e.id,
                name=e.name,
                subtitle=e.subtitle,
                date=e.date,
                location=e.location,
                status=e.status,
                total_bouts=e.total_bouts,
                poster_image_url=poster_url,
                picks_locked=getattr(e, 'picks_locked', False)
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

    return EventDetailResponse(
        id=event.id,
        name=event.name,
        subtitle=event.subtitle,
        date=event.date,
        location=event.location,
        status=event.status,
        total_bouts=event.total_bouts,
        poster_image_url=poster_url,
        promotion=event.promotion,
        url=event.url,
        picks_locked=getattr(event, 'picks_locked', False)
    )


async def _get_poster_url(event_id: int, proxy_url: Optional[str], s3_service) -> Optional[str]:
    """
    Helper para obtener la URL del poster.

    Estrategia:
    1. Verificar si existe poster en S3 (ufc-posters/ufc{id}.jpeg)
    2. Si existe Y CloudFront configurado → devolver URL de CloudFront
    3. Si NO existe → devolver proxy_url de MongoDB (se cachea automáticamente cuando se solicita)

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
            # Existe en S3 → usar CloudFront
            cloudfront_url = s3_service.get_cloudfront_url(s3_key)
            return cloudfront_url if cloudfront_url else proxy_url
        else:
            # No existe en S3 → usar proxy del backend (cachea automáticamente)
            return proxy_url

    except Exception:
        # Si hay error verificando S3, usar proxy como fallback
        return proxy_url
