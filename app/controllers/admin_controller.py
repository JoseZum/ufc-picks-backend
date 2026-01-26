"""
Controlador de Admin - Endpoints exclusivos para administradores
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.core.dependencies import CurrentAdmin, Database
from app.services.points_service import PointsService


router = APIRouter(prefix="/admin", tags=["admin"])


# ============================================
# REQUEST SCHEMAS
# ============================================

class UpdateEventTimingRequest(BaseModel):
    """Request para actualizar timing de evento"""
    event_date: Optional[datetime] = None  # Fecha/hora del evento
    picks_lock_date: Optional[datetime] = None  # Cuando se cierran las picks


class UpdateBoutTimingRequest(BaseModel):
    """Request para actualizar timing de bout individual"""
    bout_start_time: Optional[datetime] = None  # Hora de inicio de la pelea
    picks_lock_time: Optional[datetime] = None  # Cuando se cierran picks para esta pelea


class UpdateBoutResultRequest(BaseModel):
    """Request para registrar resultado de pelea"""
    winner: str  # "red" | "blue" | "draw" | "nc" (no contest)
    method: str  # "KO/TKO" | "SUB" | "DEC" | "DQ" | "OTHER"
    round: Optional[int] = None  # Round en que termino
    time: Optional[str] = None  # Tiempo en el round (ej: "4:32")


# ============================================
# EVENT TIMING ENDPOINTS
# ============================================

@router.put("/events/{event_id}/timing")
async def update_event_timing(
    event_id: int,
    request: UpdateEventTimingRequest,
    admin: CurrentAdmin,
    db: Database
):
    """
    Actualizar fecha/hora de evento y lock de picks.
    Solo administradores.
    """
    # Verificar que el evento existe
    event = await db["events"].find_one({"id": event_id})
    if not event:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Evento {event_id} no encontrado"
        )

    # Construir update
    update_data = {}
    if request.event_date:
        update_data["date"] = request.event_date
    if request.picks_lock_date:
        update_data["picks_lock_date"] = request.picks_lock_date

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

    if result.modified_count == 0:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No se pudo actualizar el evento"
        )

    return {
        "success": True,
        "message": f"Evento {event_id} actualizado correctamente",
        "updated_fields": list(update_data.keys())
    }


@router.put("/bouts/{bout_id}/timing")
async def update_bout_timing(
    bout_id: int,
    request: UpdateBoutTimingRequest,
    admin: CurrentAdmin,
    db: Database
):
    """
    Actualizar timing de pelea individual (hora inicio, lock picks).
    Solo administradores.
    """
    # Verificar que el bout existe
    bout = await db["bouts"].find_one({"id": bout_id})
    if not bout:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Bout {bout_id} no encontrado"
        )

    # Construir update
    update_data = {}
    if request.bout_start_time:
        update_data["bout_start_time"] = request.bout_start_time
    if request.picks_lock_time:
        update_data["picks_lock_time"] = request.picks_lock_time

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


# ============================================
# RESULT ENDPOINTS
# ============================================

@router.put("/bouts/{bout_id}/result")
async def update_bout_result(
    bout_id: int,
    request: UpdateBoutResultRequest,
    admin: CurrentAdmin,
    db: Database
):
    """
    Registrar resultado de pelea y calcular puntos automáticamente.
    Solo administradores.

    Esto:
    1. Actualiza el resultado del bout
    2. Marca el bout como completado
    3. Calcula y asigna puntos a todos los usuarios con picks
    4. Actualiza leaderboards automáticamente
    """
    # Verificar que el bout existe
    bout = await db["bouts"].find_one({"id": bout_id})
    if not bout:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Bout {bout_id} no encontrado"
        )

    # Validar winner
    if request.winner not in ["red", "blue", "draw", "nc"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Winner debe ser 'red', 'blue', 'draw' o 'nc'"
        )

    # Construir resultado
    result_data = {
        "winner": request.winner if request.winner not in ["draw", "nc"] else None,
        "method": request.method,
        "round": request.round,
        "time": request.time
    }

    # Actualizar bout
    update_result = await db["bouts"].update_one(
        {"id": bout_id},
        {
            "$set": {
                "result": result_data,
                "status": "completed"
            }
        }
    )

    if update_result.modified_count == 0:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No se pudo actualizar el resultado del bout"
        )

    # Calcular y asignar puntos
    points_service = PointsService(db)
    points_result = await points_service.calculate_and_assign_points(bout_id, result_data)

    return {
        "success": True,
        "message": f"Resultado del bout {bout_id} registrado correctamente",
        "result": result_data,
        "points_assigned": points_result
    }


@router.delete("/bouts/{bout_id}/result")
async def delete_bout_result(
    bout_id: int,
    admin: CurrentAdmin,
    db: Database
):
    """
    Eliminar resultado de pelea (por si se registró incorrectamente).
    Revierte puntos asignados.
    Solo administradores.
    """
    # Verificar que el bout existe
    bout = await db["bouts"].find_one({"id": bout_id})
    if not bout:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Bout {bout_id} no encontrado"
        )

    # Verificar que tiene resultado
    if "result" not in bout or bout["result"] is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Bout {bout_id} no tiene resultado registrado"
        )

    # Revertir puntos
    points_service = PointsService(db)
    await points_service.revert_points(bout_id)

    # Eliminar resultado
    await db["bouts"].update_one(
        {"id": bout_id},
        {
            "$set": {
                "result": None,
                "status": "scheduled"
            }
        }
    )

    return {
        "success": True,
        "message": f"Resultado del bout {bout_id} eliminado y puntos revertidos"
    }


# ============================================
# STATS RECALCULATION ENDPOINT
# ============================================

@router.post("/recalculate-all-stats")
async def recalculate_all_user_stats(
    admin: CurrentAdmin,
    db: Database
):
    """
    Recalcular las estadísticas de TODOS los usuarios.
    Útil para migración inicial o cuando se detectan inconsistencias.

    ADVERTENCIA: Este endpoint puede tardar en ejecutarse si hay muchos usuarios.
    Solo administradores.
    """
    # Obtener todos los usuarios
    users_cursor = db["users"].find({})
    users = await users_cursor.to_list(length=None)

    if not users:
        return {
            "success": True,
            "message": "No hay usuarios para procesar",
            "users_processed": 0
        }

    # Recalcular stats para cada usuario
    points_service = PointsService(db)
    users_processed = 0

    for user in users:
        user_id = user.get("_id")
        if user_id:
            await points_service._update_user_stats(user_id)
            users_processed += 1

    return {
        "success": True,
        "message": f"Estadísticas recalculadas para {users_processed} usuarios",
        "users_processed": users_processed
    }


# ============================================
# PICKS LOCK ENDPOINTS
# ============================================

@router.post("/events/{event_id}/lock-picks")
async def lock_event_picks(
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

    await db["events"].update_one(
        {"id": event_id},
        {"$set": {"picks_locked": True}}
    )

    return {
        "success": True,
        "message": f"Picks lockeados para evento {event_id}",
        "event_id": event_id,
        "picks_locked": True
    }


@router.post("/events/{event_id}/unlock-picks")
async def unlock_event_picks(
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

    await db["events"].update_one(
        {"id": event_id},
        {"$set": {"picks_locked": False}}
    )

    return {
        "success": True,
        "message": f"Picks desbloqueados para evento {event_id}",
        "event_id": event_id,
        "picks_locked": False
    }


@router.post("/bouts/{bout_id}/lock-picks")
async def lock_bout_picks(
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

    await db["bouts"].update_one(
        {"id": bout_id},
        {"$set": {"picks_locked": True}}
    )

    return {
        "success": True,
        "message": f"Picks lockeados para bout {bout_id}",
        "bout_id": bout_id,
        "picks_locked": True
    }


@router.post("/bouts/{bout_id}/unlock-picks")
async def unlock_bout_picks(
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

    await db["bouts"].update_one(
        {"id": bout_id},
        {"$set": {"picks_locked": False}}
    )

    return {
        "success": True,
        "message": f"Picks desbloqueados para bout {bout_id}",
        "bout_id": bout_id,
        "picks_locked": False
    }
