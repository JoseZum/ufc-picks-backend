"""Registro y borrado de resultados."""

import logging

from fastapi import APIRouter, HTTPException, Request, status

from app.controllers.admin.schemas import (
    UpdateBoutResultRequest,
)
from app.controllers.admin.shared import (
    _run_mission_triggers,
    get_bout_or_404,
)
from app.core.dependencies import CurrentAdmin, Database
from app.core.rate_limit import limiter
from app.modules.missions.application.orchestration import (
    project_admin_result_to_canonical,
)
from app.services.admin_card_commands import (
    forget_admin_command,
    record_admin_command,
    result_values,
)
from app.services.points_service import PointsService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


async def _complete_event_when_all_results_exist(db, event_id: int) -> bool:
    """Synchronize event status immediately after the final result is stored."""
    event = await db["events"].find_one(
        {"id": event_id},
        {"total_bouts": 1},
    )
    if not event:
        return False

    bouts = await db["bouts"].find({"event_id": event_id}).to_list(length=None)
    active_bouts = [
        bout for bout in bouts if bout.get("status") != "cancelled"
    ]
    result_bouts = [
        bout
        for bout in active_bouts
        if isinstance(bout.get("result"), dict) and bool(bout["result"])
    ]
    expected_total = int(event.get("total_bouts") or 0)
    is_complete = (
        len(result_bouts) >= expected_total
        if expected_total > 0
        else bool(active_bouts) and len(result_bouts) == len(active_bouts)
    )
    if not is_complete:
        return False

    result_ids = [bout["id"] for bout in result_bouts]
    stale_ids = [
        bout["id"]
        for bout in active_bouts
        if not isinstance(bout.get("result"), dict) or not bout["result"]
    ]
    if result_ids:
        await db["bouts"].update_many(
            {"id": {"$in": result_ids}},
            {"$set": {"status": "completed"}},
        )
    if stale_ids:
        await db["bouts"].update_many(
            {"id": {"$in": stale_ids}},
            {"$set": {"status": "cancelled"}},
        )
    await db["events"].update_one(
        {"id": event_id},
        {"$set": {"status": "completed"}},
    )
    return True


@router.put("/bouts/{bout_id}/result")
@limiter.limit("30/minute")
async def update_bout_result(
    request: Request,
    bout_id: int,
    body: UpdateBoutResultRequest,
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
    bout = await get_bout_or_404(db, bout_id)

    # Validar winner
    if body.winner not in ["red", "blue", "draw", "nc"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Winner debe ser 'red', 'blue', 'draw' o 'nc'"
        )

    # Construir resultado
    result_data = {
        "winner": body.winner if body.winner not in ["draw", "nc"] else None,
        "outcome": body.winner,
        "method": body.method,
        "round": body.round,
        "time": body.time
    }

    # Actualizar bout. La proyeccion canonica se escribe junto al campo legacy:
    # el sidecar `card_data_v1` es lo que el motor de misiones lee, mientras el
    # `result` de nivel superior sigue sirviendo a la API/UI actual sin cambios.
    canonical_fields = project_admin_result_to_canonical(bout, result_data)
    update_result = await db["bouts"].update_one(
        {"id": bout_id},
        {
            "$set": {
                "result": result_data,
                "status": "completed",
                **(canonical_fields or {}),
            }
        }
    )

    if update_result.modified_count == 0:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No se pudo actualizar el resultado del bout"
        )

    # B-011: la proyeccion canonica de arriba la recalcula el reconciler en la
    # siguiente pasada. El comando persistido es lo que hace que el resultado
    # registrado por Admin sea la autoridad de forma duradera (D-DATA-002).
    actor_id = str(getattr(admin, "id", "") or getattr(admin, "google_id", ""))
    canonical_result = (canonical_fields or {}).get("card_data_v1.result") or {}
    if canonical_result.get("outcome"):
        await record_admin_command(
            db,
            kind="result",
            event_id=int(bout.get("event_id") or 0),
            bout_id=bout_id,
            actor_id=actor_id,
            reason="Admin registered the bout result",
            values=result_values(
                outcome=canonical_result["outcome"],
                winner_fighter_id=canonical_result.get("winner_fighter_id"),
                method_detail=result_data.get("method"),
                ending_round=result_data.get("round"),
            ),
        )

    # Calcular y asignar puntos
    points_service = PointsService(db)
    points_result = await points_service.calculate_and_assign_points(bout_id, result_data)
    event_completed = await _complete_event_when_all_results_exist(
        db,
        int(bout["event_id"]),
    )
    missions_result = await _run_mission_triggers(
        db,
        event_id=int(bout["event_id"]),
        bout_id=bout_id,
        canonical_fields=canonical_fields,
    )

    return {
        "success": True,
        "message": f"Resultado del bout {bout_id} registrado correctamente",
        "result": result_data,
        "points_assigned": points_result,
        "event_completed": event_completed,
        "missions": missions_result,
    }


@router.delete("/bouts/{bout_id}/result")
@limiter.limit("30/minute")
async def delete_bout_result(
    request: Request,
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
    bout = await get_bout_or_404(db, bout_id)

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
    await db["events"].update_one(
        {"id": int(bout["event_id"]), "status": "completed"},
        {"$set": {"status": "scheduled"}},
    )

    # B-011: retirar el comando permanente. Si no, la frontera seguiria
    # reaplicando para siempre un resultado que Admin acaba de borrar.
    actor_id = str(getattr(admin, "id", "") or getattr(admin, "google_id", ""))
    await forget_admin_command(
        db,
        kind="result",
        event_id=int(bout["event_id"]),
        bout_id=bout_id,
        actor_id=actor_id,
    )
    await record_admin_command(
        db,
        kind="clear_result",
        event_id=int(bout["event_id"]),
        bout_id=bout_id,
        actor_id=actor_id,
        reason="Admin deleted the bout result",
    )

    return {
        "success": True,
        "message": f"Resultado del bout {bout_id} eliminado y puntos revertidos"
    }


# Stats recalculation endpoint

@router.post("/recalculate-all-stats")
@limiter.limit("30/minute")
async def recalculate_all_user_stats(
    request: Request,
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


# Pick lock endpoints
