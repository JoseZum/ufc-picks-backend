"""
Controlador de picks - Endpoints para gestionar picks de usuarios
"""

from fastapi import APIRouter, HTTPException, Query, status

from app.core.dependencies import Database, CurrentUser
from app.services.pick_service import (
    PickService,
    PickLockedError,
    EventNotFoundError,
    BoutNotFoundError,
    InvalidPickError
)
from app.models.pick import PickCreate, PickResponse


router = APIRouter(prefix="/picks", tags=["picks"])


@router.post("", response_model=PickResponse, status_code=status.HTTP_201_CREATED)
async def create_pick(
    pick_data: PickCreate,
    user: CurrentUser,
    db: Database
):
    """
    Crear o actualizar un pick.

    El usuario puede modificar sus picks hasta que comience el evento;
    tras el inicio, los picks quedan bloqueados.
    """
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
        picked_corner=pick.picked_corner,
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
    event_id: int = Query(..., description="Event ID to get picks for")
):
    """
    Obtener los picks del usuario actual para un evento.
    """
    pick_service = PickService(db)
    picks = await pick_service.get_user_picks_for_event(user.id, event_id)

    return [
        PickResponse(
            id=p.id,
            bout_id=p.bout_id,
            event_id=p.event_id,
            picked_corner=p.picked_corner,
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
    limit: int = Query(100, ge=1, le=500, description="Maximum number of picks to return")
):
    """
    Obtener todos los picks del usuario actual en todos los eventos.
    """
    pick_service = PickService(db)
    picks = await pick_service.get_all_user_picks(user.id, limit)

    return [
        PickResponse(
            id=p.id,
            bout_id=p.bout_id,
            event_id=p.event_id,
            picked_corner=p.picked_corner,
            picked_method=p.picked_method,
            picked_round=p.picked_round,
            is_correct=p.is_correct,
            points_awarded=p.points_awarded,
            locked=p.locked,
            created_at=p.created_at
        )
        for p in picks
    ]
