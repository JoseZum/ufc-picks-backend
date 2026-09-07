"""Horarios de evento y de peleas."""

import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request, status

from app.controllers.admin.schemas import (
    UpdateBoutTimingRequest,
    UpdateEventTimingRequest,
)
from app.controllers.admin.shared import get_bout_or_404, get_event_or_404
from app.core.dependencies import CurrentAdmin, Database
from app.core.rate_limit import limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _shift_section_times(
    values: dict | None,
    delta,
) -> dict[str, datetime]:
    return {
        section: _naive_utc(value) + delta
        for section, value in (values or {}).items()
        if isinstance(value, datetime)
    }


def build_event_timing_updates(
    event: dict,
    card_start_time_utc: datetime | None,
    picks_lock_time_utc: datetime | None,
) -> dict:
    """Shift section starts/locks while preserving their ESPN spacing."""
    requested_start = (
        _naive_utc(card_start_time_utc)
        if card_start_time_utc is not None
        else None
    )
    requested_lock = (
        _naive_utc(picks_lock_time_utc)
        if picks_lock_time_utc is not None
        else None
    )
    section_starts = dict(event.get("section_start_times_utc") or {})
    section_locks = dict(
        event.get("section_lock_times_utc")
        or section_starts
    )
    current_start = event.get("card_start_time_utc") or min(
        (
            value
            for value in section_starts.values()
            if isinstance(value, datetime)
        ),
        default=None,
    )
    current_lock = event.get("picks_lock_time_utc") or min(
        (
            value
            for value in section_locks.values()
            if isinstance(value, datetime)
        ),
        default=current_start,
    )
    updates: dict = {}

    if requested_start is not None:
        if isinstance(current_start, datetime):
            start_delta = requested_start - _naive_utc(current_start)
            section_starts = _shift_section_times(
                section_starts,
                start_delta,
            )
            section_locks = _shift_section_times(
                section_locks,
                start_delta,
            )
            if isinstance(current_lock, datetime):
                current_lock = _naive_utc(current_lock) + start_delta
        updates["card_start_time_utc"] = requested_start
        updates["section_start_times_utc"] = section_starts
        updates["section_lock_times_utc"] = section_locks
        if isinstance(current_lock, datetime):
            updates["picks_lock_time_utc"] = current_lock

        et_start = requested_start.replace(
            tzinfo=UTC
        ).astimezone(ZoneInfo("America/New_York"))
        updates["date"] = datetime.combine(
            et_start.date(),
            datetime.min.time(),
        )
        updates["start_time_et"] = et_start.strftime("%H:%M")
        updates["timezone"] = "ET"

    if requested_lock is not None:
        lock_base = (
            updates.get("picks_lock_time_utc")
            or current_lock
            or requested_start
        )
        if isinstance(lock_base, datetime):
            lock_delta = requested_lock - _naive_utc(lock_base)
            section_locks = _shift_section_times(
                updates.get("section_lock_times_utc", section_locks),
                lock_delta,
            )
        updates["picks_lock_time_utc"] = requested_lock
        updates["section_lock_times_utc"] = section_locks

    if updates:
        updates["timing_source"] = "admin"
        updates["timing_updated_at"] = datetime.utcnow()
    return updates

@router.put("/events/{event_id}/timing")
@limiter.limit("30/minute")
async def update_event_timing(
    request: Request,
    event_id: int,
    body: UpdateEventTimingRequest,
    admin: CurrentAdmin,
    db: Database
):
    """
    Actualizar fecha/hora de evento y lock de picks.
    Solo administradores.
    """
    event = await get_event_or_404(db, event_id)

    card_start = body.card_start_time_utc or body.event_date
    picks_lock = body.picks_lock_time_utc or body.picks_lock_date
    update_data = build_event_timing_updates(
        event,
        card_start,
        picks_lock,
    )

    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Debes proporcionar al menos un campo para actualizar"
        )

    # Actualizar evento
    result = await db["events"].update_one(
        {"id": event_id},
        {"$set": update_data}
    )

    if result.matched_count == 0:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No se pudo actualizar el evento"
        )

    for section, lock_time in (
        update_data.get("section_lock_times_utc") or {}
    ).items():
        await db["bouts"].update_many(
            {
                "event_id": event_id,
                "card_section": section,
            },
            {
                "$set": {
                    "automatic_lock_time_utc": lock_time,
                }
            },
        )

    return {
        "success": True,
        "message": f"Evento {event_id} actualizado correctamente",
        "updated_fields": list(update_data.keys())
    }


@router.put("/bouts/{bout_id}/timing")
@limiter.limit("30/minute")
async def update_bout_timing(
    request: Request,
    bout_id: int,
    body: UpdateBoutTimingRequest,
    admin: CurrentAdmin,
    db: Database
):
    """
    Actualizar timing de pelea individual (hora inicio, lock picks).
    Solo administradores.
    """
    # Verificar que el bout existe
    await get_bout_or_404(db, bout_id)

    # Construir update
    update_data = {}
    if body.bout_start_time:
        update_data["bout_start_time"] = body.bout_start_time
    if body.picks_lock_time:
        update_data["picks_lock_time"] = body.picks_lock_time

    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Debes proporcionar al menos un campo para actualizar"
        )

    # Actualizar bout
    result = await db["bouts"].update_one(
        {"id": bout_id},
        {"$set": update_data}
    )

    if result.modified_count == 0:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No se pudo actualizar el bout"
        )

    return {
        "success": True,
        "message": f"Bout {bout_id} actualizado correctamente",
        "updated_fields": list(update_data.keys())
    }


# Result endpoints
