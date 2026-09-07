"""Bloqueo de picks y cierre de eventos."""

import logging

from fastapi import APIRouter, HTTPException, Request, status

from app.core.dependencies import CurrentAdmin, Database
from app.core.rate_limit import limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/events/{event_id}/lock-picks")
@limiter.limit("30/minute")
async def lock_event_picks(
    request: Request,
    event_id: int,
    admin: CurrentAdmin,
    db: Database
):
    """
    Lockear picks para un evento completo.
    Solo administradores.
    """
    event = await db["events"].find_one({"id": event_id})
    if not event:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Evento {event_id} no encontrado"
        )

    # Update event picks_locked flag
    await db["events"].update_one(
        {"id": event_id},
        {
            "$set": {
                "picks_locked": True,
                "picks_lock_override": "locked",
            }
        }
    )

    # Update all picks for this event to locked: True
    picks_result = await db["picks"].update_many(
        {"event_id": event_id, "locked": False},
        {"$set": {"locked": True}}
    )

    return {
        "success": True,
        "message": f"Picks lockeados para evento {event_id}",
        "event_id": event_id,
        "picks_locked": True,
        "picks_updated": picks_result.modified_count
    }


@router.post("/events/{event_id}/unlock-picks")
@limiter.limit("30/minute")
async def unlock_event_picks(
    request: Request,
    event_id: int,
    admin: CurrentAdmin,
    db: Database
):
    """
    Unlockear picks para un evento completo.
    Solo administradores.
    """
    event = await db["events"].find_one({"id": event_id})
    if not event:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Evento {event_id} no encontrado"
        )

    # Update event picks_locked flag
    await db["events"].update_one(
        {"id": event_id},
        {
            "$set": {
                "picks_locked": False,
                "picks_lock_override": "unlocked",
            }
        }
    )

    # Preserve individually locked bouts when removing the full-event lock.
    individually_locked_bout_ids = await db["bouts"].distinct(
        "id",
        {
            "event_id": event_id,
            "$or": [
                {"picks_lock_override": "locked"},
                {
                    "picks_lock_override": {"$exists": False},
                    "picks_locked": True,
                },
            ],
        },
    )
    unlock_query = {"event_id": event_id, "locked": True}
    if individually_locked_bout_ids:
        unlock_query["bout_id"] = {"$nin": individually_locked_bout_ids}

    picks_result = await db["picks"].update_many(
        unlock_query,
        {"$set": {"locked": False}}
    )

    return {
        "success": True,
        "message": f"Picks desbloqueados para evento {event_id}",
        "event_id": event_id,
        "picks_locked": False,
        "picks_updated": picks_result.modified_count
    }


@router.post("/events/{event_id}/complete")
@limiter.limit("30/minute")
async def complete_event(
    request: Request,
    event_id: int,
    admin: CurrentAdmin,
    db: Database
):
    """
    Marca un evento como completado manualmente.
    Solo administradores.
    """
    event = await db["events"].find_one({"id": event_id})
    if not event:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Evento {event_id} no encontrado"
        )

    await db["events"].update_one(
        {"id": event_id},
        {"$set": {"status": "completed"}}
    )

    return {
        "success": True,
        "message": f"Evento {event_id} marcado como completado",
        "event_id": event_id,
        "status": "completed"
    }


@router.post("/bouts/{bout_id}/lock-picks")
@limiter.limit("30/minute")
async def lock_bout_picks(
    request: Request,
    bout_id: int,
    admin: CurrentAdmin,
    db: Database
):
    """
    Lockear picks para una pelea individual.
    Solo administradores.
    """
    bout = await db["bouts"].find_one({"id": bout_id})
    if not bout:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Bout {bout_id} no encontrado"
        )

    # Update bout picks_locked flag
    await db["bouts"].update_one(
        {"id": bout_id},
        {
            "$set": {
                "picks_locked": True,
                "picks_lock_override": "locked",
            }
        }
    )

    # Update all picks for this bout to locked: True
    picks_result = await db["picks"].update_many(
        {"bout_id": bout_id, "locked": False},
        {"$set": {"locked": True}}
    )

    return {
        "success": True,
        "message": f"Picks lockeados para bout {bout_id}",
        "bout_id": bout_id,
        "picks_locked": True,
        "picks_updated": picks_result.modified_count
    }


@router.post("/bouts/{bout_id}/unlock-picks")
@limiter.limit("30/minute")
async def unlock_bout_picks(
    request: Request,
    bout_id: int,
    admin: CurrentAdmin,
    db: Database
):
    """
    Unlockear picks para una pelea individual.
    Solo administradores.
    """
    bout = await db["bouts"].find_one({"id": bout_id})
    if not bout:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Bout {bout_id} no encontrado"
        )

    # Update bout picks_locked flag
    await db["bouts"].update_one(
        {"id": bout_id},
        {
            "$set": {
                "picks_locked": False,
                "picks_lock_override": "unlocked",
            }
        }
    )

    # Update all picks for this bout to locked: False
    picks_result = await db["picks"].update_many(
        {"bout_id": bout_id, "locked": True},
        {"$set": {"locked": False}}
    )

    return {
        "success": True,
        "message": f"Picks desbloqueados para bout {bout_id}",
        "bout_id": bout_id,
        "picks_locked": False,
        "picks_updated": picks_result.modified_count
    }


# Bout cancellation endpoint
