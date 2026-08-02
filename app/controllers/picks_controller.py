"""
Controlador de picks - Endpoints para gestionar picks de usuarios
"""

from fastapi import APIRouter, HTTPException, Query, status

from app.core.dependencies import CurrentUser, Database
from app.models.pick import PickCreate, PickResponse
from app.services.pick_service import (
    BoutNotFoundError,
    EventNotFoundError,
    InvalidPickError,
    PickLockedError,
    PickService,
)

router = APIRouter(prefix="/picks", tags=["picks"])


@router.post("", response_model=PickResponse, status_code=status.HTTP_201_CREATED)
async def create_pick(
    pick_data: PickCreate,
    user: CurrentUser,
    db: Database
):
    """Crea o actualiza un pick con validaciones completas."""
    pick_service = PickService(db)

    try:
        pick = await pick_service.create_or_update_pick(user.id, pick_data)
    except PickLockedError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e)
        )
    except EventNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except BoutNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except InvalidPickError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )

    return PickResponse(
        id=pick.id,
        bout_id=pick.bout_id,
        event_id=pick.event_id,
        picked_fighter_name=pick.picked_fighter_name,
        picked_method=pick.picked_method,
        picked_round=pick.picked_round,
        is_correct=pick.is_correct,
        points_awarded=pick.points_awarded,
        locked=pick.locked,
        created_at=pick.created_at
    )


@router.get("/me", response_model=list[PickResponse])
async def get_my_picks(
    user: CurrentUser,
    db: Database,
    event_id: int = Query(..., description="ID del evento")
):
    """Obtiene los picks del usuario en un evento específico."""
    pick_service = PickService(db)
    picks = await pick_service.get_user_picks_for_event(user.id, event_id)

    return [
        PickResponse(
            id=p.id,
            bout_id=p.bout_id,
            event_id=p.event_id,
            picked_fighter_name=p.picked_fighter_name,
            picked_method=p.picked_method,
            picked_round=p.picked_round,
            is_correct=p.is_correct,
            points_awarded=p.points_awarded,
            locked=p.locked,
            created_at=p.created_at
        )
        for p in picks
    ]


@router.get("/me/all", response_model=list[PickResponse])
async def get_all_my_picks(
    user: CurrentUser,
    db: Database,
    limit: int = Query(100, ge=1, le=500, description="Máximo número de picks")
):
    """Obtiene todos los picks del usuario en todos los eventos."""
    pick_service = PickService(db)
    picks = await pick_service.get_all_user_picks(user.id, limit)

    return [
        PickResponse(
            id=p.id,
            bout_id=p.bout_id,
            event_id=p.event_id,
            picked_fighter_name=p.picked_fighter_name,
            picked_method=p.picked_method,
            picked_round=p.picked_round,
            is_correct=p.is_correct,
            points_awarded=p.points_awarded,
            locked=p.locked,
            created_at=p.created_at
        )
        for p in picks
    ]

@router.post("/me/cleanup")
async def cleanup_pending_picks(
    user: CurrentUser,
    db: Database
):
    """Elimina picks pending en eventos ya completados (peleas canceladas o sin resultado)."""
    pick_service = PickService(db)
    deleted = await pick_service.cleanup_pending_picks(user.id)
    return {"deleted": deleted}


@router.get("/me/detailed", response_model=list[dict])
async def get_all_my_picks_detailed(
    user: CurrentUser,
    db: Database,
    limit: int = Query(100, ge=1, le=500, description="Máximo número de picks")
):
    """Obtiene picks con detalles completos de peleas y eventos."""
    pick_service = PickService(db)
    picks = await pick_service.get_all_user_picks(user.id, limit)

    if not picks:
        return []

    # Get event and bout IDs
    event_ids = list(set(p.event_id for p in picks))
    bout_ids = list(set(p.bout_id for p in picks))

    # Fetch events
    events_cursor = db["events"].find({"id": {"$in": event_ids}})
    events = await events_cursor.to_list(length=None)
    events_map = {e["id"]: e for e in events}

    # Fetch bouts
    bouts_cursor = db["bouts"].find({"id": {"$in": bout_ids}})
    bouts = await bouts_cursor.to_list(length=None)
    bouts_map = {b["id"]: b for b in bouts}

    # Build detailed response
    result = []
    for p in picks:
        event = events_map.get(p.event_id, {})
        bout = bouts_map.get(p.bout_id, {})
        fighters = bout.get("fighters", {})

        result.append({
            "id": p.id,
            "bout_id": p.bout_id,
            "event_id": p.event_id,
            "event_name": event.get("name"),
            "event_date": event.get("event_date"),
            "picked_fighter_name": p.picked_fighter_name,
            "picked_method": p.picked_method,
            "picked_round": p.picked_round,
            "is_correct": p.is_correct,
            "points_awarded": p.points_awarded,
            "locked": p.locked,
            "created_at": p.created_at,
            "fighter_red": fighters.get("red", {}).get("fighter_name"),
            "fighter_blue": fighters.get("blue", {}).get("fighter_name"),
            "weight_class": bout.get("weight_class"),
            "result": bout.get("result")
        })

    return result
