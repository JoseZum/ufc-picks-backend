"""
Controlador de Admin - Endpoints exclusivos para administradores
"""

import logging
import os
from datetime import UTC, datetime
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, File, HTTPException, Request, UploadFile, status
from pydantic import BaseModel

from app.core.dependencies import CurrentAdmin, Database
from app.core.rate_limit import limiter
from app.modules.missions.application.orchestration import (
    MissionTriggerService,
    project_admin_result_to_canonical,
)
from app.services.admin_card_commands import (
    forget_admin_command,
    lifecycle_values,
    record_admin_command,
    result_values,
    structure_values,
    title_values,
)
from app.services.canonical_authority import record_admin_field_overrides
from app.services.points_service import PointsService
from app.services.s3_service import S3ServiceError, S3WriteNotAllowedError, get_s3_service

logger = logging.getLogger(__name__)


async def _run_mission_triggers(
    db,
    *,
    event_id: int,
    bout_id: int,
    canonical_fields: dict | None,
) -> dict:
    """Drive mission evaluation from a registered result, and never break it.

    Points and result registration are the product's core loop; missions are an
    additive layer on top. Any failure here is logged and reported in the
    response, but it must never turn a successful result write into a 500.
    """
    if not canonical_fields:
        return {
            "triggered": False,
            "reason": "bout has no canonical CardData projection",
        }
    try:
        outcome = await MissionTriggerService(db).on_bout_result(
            event_id=event_id,
            bout_id=bout_id,
            result_revision=int(canonical_fields["card_data_v1.result_revision"]),
        )
    except Exception:  # noqa: BLE001 - missions must not break result writing
        logger.exception(
            "Mission evaluation failed for bout %s on event %s", bout_id, event_id
        )
        return {"triggered": False, "reason": "mission evaluation raised"}
    return {
        "triggered": True,
        "evaluated_assignments": outcome.evaluated_assignments,
        "card_finalized": outcome.card_finalized,
        "monthly_updates": outcome.monthly_updates,
        "errors": list(outcome.errors),
    }


router = APIRouter(prefix="/admin", tags=["admin"])


# Request schemas

class UpdateEventTimingRequest(BaseModel):
    """Datos para actualizar fecha/hora de un evento."""
    card_start_time_utc: Optional[datetime] = None
    picks_lock_time_utc: Optional[datetime] = None
    # Legacy aliases kept while deployed frontends roll over.
    event_date: Optional[datetime] = None
    picks_lock_date: Optional[datetime] = None


class UpdateBoutTimingRequest(BaseModel):
    """Datos para actualizar timing de una pelea individual."""
    bout_start_time: Optional[datetime] = None
    picks_lock_time: Optional[datetime] = None


class UpdateBoutResultRequest(BaseModel):
    """Datos para registrar el resultado de una pelea."""
    winner: str  # "red" | "blue" | "draw" | "nc"
    method: str  # "KO/TKO" | "SUB" | "DEC" | "DQ" | "OTHER"
    round: Optional[int] = None
    time: Optional[str] = None


class UpdateBoutDetailsRequest(BaseModel):
    """Datos editables de una pelea y su posición en la cartelera."""
    # Campos del bout
    rounds_scheduled: Optional[int] = None  # 3 o 5
    weight_class: Optional[str] = None
    is_title_fight: Optional[bool] = None
    is_bmf_title_fight: Optional[bool] = None
    # Campos del event_card_slot
    card_section: Optional[str] = None  # "main" | "prelim" | "early_prelim"
    order_overall: Optional[int] = None
    order_section: Optional[int] = None
    is_main_event: Optional[bool] = None
    is_co_main: Optional[bool] = None


# Event art endpoints

@router.post("/events/{event_id}/event-art")
@limiter.limit("30/minute")
async def upload_event_art(
    request: Request,
    event_id: int,
    admin: CurrentAdmin,
    db: Database,
    file: UploadFile = File(...)
):
    """Sube una imagen personalizada para un evento."""
    # Verificar que el evento existe
    event = await db["events"].find_one({"id": event_id})
    if not event:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Evento {event_id} no encontrado"
        )

    # Validar que sea una imagen soportada
    valid_extensions = ['.avif', '.png', '.jpg', '.jpeg', '.webp']
    if not file.filename or not any(file.filename.lower().endswith(ext) for ext in valid_extensions):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Tipos válidos: {', '.join(valid_extensions)}"
        )

    # Mapear extensión a content type
    content_type = file.content_type or 'application/octet-stream'
    if file.filename:
        ext = file.filename.lower().split('.')[-1]
        content_type_map = {
            'avif': 'image/avif',
            'png': 'image/png',
            'jpg': 'image/jpeg',
            'jpeg': 'image/jpeg',
            'webp': 'image/webp'
        }
        content_type = content_type_map.get(ext, content_type)

    try:
        # Leer y guardar la imagen
        image_data = await file.read()

        await db["events"].update_one(
            {"id": event_id},
            {"$set": {
                "event_art": image_data,
                "event_art_content_type": content_type
            }}
        )

        return {
            "success": True,
            "message": f"Imagen subida para evento {event_id}",
            "size_bytes": len(image_data),
            "content_type": content_type
        }

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error al subir: {str(e)}"
        )


@router.delete("/events/{event_id}/event-art")
@limiter.limit("30/minute")
async def delete_event_art(
    request: Request,
    event_id: int,
    admin: CurrentAdmin,
    db: Database
):
    """Elimina la imagen personalizada de un evento."""
    # Verificar que el evento existe
    event = await db["events"].find_one({"id": event_id})
    if not event:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Evento {event_id} no encontrado"
        )

    # Eliminar imagen de MongoDB
    await db["events"].update_one(
        {"id": event_id},
        {"$unset": {"event_art": "", "event_art_content_type": ""}}
    )

    return {
        "success": True,
        "message": f"Imagen eliminada para evento {event_id}"
    }


# Event timing endpoints


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
    card_start_time_utc: Optional[datetime],
    picks_lock_time_utc: Optional[datetime],
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
    # Verificar que el evento existe
    event = await db["events"].find_one({"id": event_id})
    if not event:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Evento {event_id} no encontrado"
        )

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
    bout = await db["bouts"].find_one({"id": bout_id})
    if not bout:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Bout {bout_id} no encontrado"
        )

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
    bout = await db["bouts"].find_one({"id": bout_id})
    if not bout:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Bout {bout_id} no encontrado"
        )

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

@router.post("/bouts/{bout_id}/cancel")
@limiter.limit("30/minute")
async def cancel_bout(
    request: Request,
    bout_id: int,
    admin: CurrentAdmin,
    db: Database
):
    """
    Cancel a bout and delete all associated picks.

    This:
    1. Marks the bout as cancelled
    2. Reverts any points already assigned
    3. Deletes all picks for this bout
    4. Recalculates stats for affected users

    Solo administradores.
    """
    # Verify bout exists
    bout = await db["bouts"].find_one({"id": bout_id})
    if not bout:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Bout {bout_id} no encontrado"
        )

    # Get affected users before deleting picks
    picks_cursor = db["picks"].find({"bout_id": bout_id})
    picks = await picks_cursor.to_list(length=None)
    users_affected = set(pick["user_id"] for pick in picks)
    picks_count = len(picks)

    # If bout had a result, revert points first
    if bout.get("result"):
        points_service = PointsService(db)
        await points_service.revert_points(bout_id)

    # Delete all picks for this bout
    delete_result = await db["picks"].delete_many({"bout_id": bout_id})

    # Mark bout as cancelled
    await db["bouts"].update_one(
        {"id": bout_id},
        {"$set": {"status": "cancelled"}}
    )
    # El motor de misiones lee `card_data_v1.status`, no el `status` legacy
    # (`bout_evaluation._bout_snapshot` prefiere el sidecar). Sin esto el bout
    # sigue contando como `surviving` y `_finalize_if_complete` ve una pelea sin
    # resultado, asi que la card no finaliza y las misiones nunca pagan XP hasta
    # que el reconciliador converja en la siguiente pasada.
    #
    # El filtro por `$exists` no es opcional: crear el sidecar aqui haria que un
    # bout legacy pase a "pertenecer a la card" (`_belongs_to_the_card`) con una
    # proyeccion invalida, y eso revienta la evaluacion de la card entera.
    await db["bouts"].update_one(
        {"id": bout_id, "card_data_v1": {"$exists": True}},
        {"$set": {"card_data_v1.status": "cancelled"}}
    )
    await db["event_card_slots"].update_one(
        {"bout_id": bout_id},
        {"$set": {"is_current": False}}
    )
    # B-011: sin comando, la siguiente pasada de ESPN vuelve a listar el bout y
    # el `status` canonico se recalcula como programado.
    actor_id = str(getattr(admin, "id", "") or getattr(admin, "google_id", ""))
    await record_admin_command(
        db,
        kind="bout_lifecycle",
        event_id=int(bout["event_id"]),
        bout_id=bout_id,
        actor_id=actor_id,
        reason="Admin cancelled the bout",
        values=lifecycle_values("cancelled"),
    )
    # El slot es propiedad exclusiva del reconciliador: sin comando, la proxima
    # pasada lo vuelve a marcar `is_current` y el bout reaparece en la card.
    await record_admin_command(
        db,
        kind="bout_structure",
        event_id=int(bout["event_id"]),
        bout_id=bout_id,
        actor_id=actor_id,
        reason="Admin cancelled the bout",
        values=structure_values(is_current=False),
    )

    # Recalculate stats for all affected users
    points_service = PointsService(db)
    for user_id in users_affected:
        await points_service._update_user_stats(user_id)

    return {
        "success": True,
        "message": f"Bout {bout_id} cancelled and {picks_count} picks deleted",
        "bout_id": bout_id,
        "picks_deleted": delete_result.deleted_count,
        "users_affected": len(users_affected)
    }


# Fighter photo upload endpoint

@router.post("/fighters/photo")
@limiter.limit("30/minute")
async def upload_fighter_photo(
    request: Request,
    admin: CurrentAdmin,
    file: UploadFile = File(...)
):
    """Sube una foto de peleador a S3."""
    # Validar que sea un formato de imagen soportado por el frontend
    valid_extensions = ['.png', '.jpg', '.jpeg', '.webp', '.avif']
    if not file.filename or not any(file.filename.lower().endswith(ext) for ext in valid_extensions):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Solo se aceptan archivos: {', '.join(valid_extensions)}"
        )

    # Sanitizar filename para evitar path traversal
    filename = os.path.basename(file.filename)
    if not filename or filename.startswith('.'):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Nombre de archivo inválido"
        )

    # Determinar content type según extensión
    ext = filename.lower().split('.')[-1]
    content_type_map = {
        'png': 'image/png',
        'jpg': 'image/jpeg',
        'jpeg': 'image/jpeg',
        'webp': 'image/webp',
        'avif': 'image/avif',
    }
    content_type = content_type_map[ext]

    # Construir key S3: fighters/{filename}
    s3_key = f"fighters/{filename}"

    try:
        image_data = await file.read()
        s3_service = get_s3_service()
        await s3_service.upload_image(s3_key, image_data, content_type=content_type)

        # Generar URL de CloudFront
        cloudfront_url = s3_service.get_cloudfront_url(s3_key)

        return {
            "success": True,
            "message": f"Foto subida: {filename}",
            "s3_key": s3_key,
            "cloudfront_url": cloudfront_url,
            "size_bytes": len(image_data),
            "content_type": content_type
        }

    except S3WriteNotAllowedError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Escritura en S3 no permitida (modo cache activo)"
        )
    except S3ServiceError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error de S3: {str(e)}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error al subir foto: {str(e)}"
        )


# Bout deletion endpoint

@router.delete("/bouts/{bout_id}")
@limiter.limit("30/minute")
async def delete_bout(
    request: Request,
    bout_id: int,
    admin: CurrentAdmin,
    db: Database
):
    """
    Eliminar una pelea por completo de la base de datos.

    A diferencia de cancel, esto:
    1. Revierte puntos si la pelea tenía resultado
    2. Elimina todos los picks asociados
    3. Elimina el event_card_slot correspondiente
    4. Elimina el bout de la colección
    5. Actualiza el total_bouts del evento
    6. Recalcula stats de usuarios afectados

    Solo administradores.
    """
    # Verificar que el bout existe
    bout = await db["bouts"].find_one({"id": bout_id})
    if not bout:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Bout {bout_id} no encontrado"
        )

    event_id = bout.get("event_id")

    # Recopilar usuarios afectados antes de eliminar picks
    picks_cursor = db["picks"].find({"bout_id": bout_id})
    picks = await picks_cursor.to_list(length=None)
    users_affected = set(pick["user_id"] for pick in picks)
    picks_count = len(picks)

    # Si tenía resultado, revertir puntos primero
    if bout.get("result"):
        points_service = PointsService(db)
        await points_service.revert_points(bout_id)

    # Eliminar todos los picks de esta pelea
    await db["picks"].delete_many({"bout_id": bout_id})

    # Eliminar el event_card_slot
    await db["event_card_slots"].delete_one({"bout_id": bout_id})

    # Eliminar el bout
    await db["bouts"].delete_one({"id": bout_id})

    # Actualizar total_bouts del evento
    if event_id:
        remaining_bouts = await db["bouts"].count_documents({"event_id": event_id})
        await db["events"].update_one(
            {"id": event_id},
            {"$set": {"total_bouts": remaining_bouts}}
        )

    # Recalcular stats de usuarios afectados
    points_service = PointsService(db)
    for user_id in users_affected:
        await points_service._update_user_stats(user_id)

    return {
        "success": True,
        "message": f"Bout {bout_id} eliminado completamente",
        "bout_id": bout_id,
        "event_id": event_id,
        "picks_deleted": picks_count,
        "users_affected": len(users_affected)
    }


# Bout details endpoint

@router.put("/bouts/{bout_id}/details")
@limiter.limit("30/minute")
async def update_bout_details(
    request: Request,
    bout_id: int,
    body: UpdateBoutDetailsRequest,
    admin: CurrentAdmin,
    db: Database
):
    """
    Editar campos de una pelea y su posición en la cartelera.
    Solo administradores.
    """
    # Verificar que el bout existe
    bout = await db["bouts"].find_one({"id": bout_id})
    if not bout:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Bout {bout_id} no encontrado"
        )

    # Separar campos del bout y del card_slot
    bout_update = {}
    slot_update = {}

    if body.rounds_scheduled is not None:
        if body.rounds_scheduled not in [3, 5]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="rounds_scheduled debe ser 3 o 5"
            )
        bout_update["rounds_scheduled"] = body.rounds_scheduled

    if body.weight_class is not None:
        bout_update["weight_class"] = body.weight_class

    if body.is_title_fight is not None:
        bout_update["is_title_fight"] = body.is_title_fight

    if body.is_bmf_title_fight is not None:
        bout_update["is_bmf_title_fight"] = body.is_bmf_title_fight

    if body.card_section is not None:
        if body.card_section not in ["main", "prelim", "early_prelim"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="card_section debe ser 'main', 'prelim' o 'early_prelim'"
            )
        slot_update["card_section"] = body.card_section

    if body.order_overall is not None:
        slot_update["order_overall"] = body.order_overall

    if body.order_section is not None:
        slot_update["order_section"] = body.order_section

    if body.is_main_event is not None:
        slot_update["is_main_event"] = body.is_main_event

    if body.is_co_main is not None:
        slot_update["is_co_main"] = body.is_co_main

    if not bout_update and not slot_update:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Debes proporcionar al menos un campo para actualizar"
        )

    updated_fields = []

    # Actualizar campos del bout
    if bout_update:
        result = await db["bouts"].update_one(
            {"id": bout_id},
            {"$set": bout_update}
        )
        if result.modified_count > 0:
            updated_fields.extend(list(bout_update.keys()))

        # D-DATA-010: Admin es la unica autoridad sobre los campos de titulo y
        # tanto `true` como `false` son decisiones duraderas.
        #
        # Se registran dos cosas distintas a proposito. La evidencia en el
        # sidecar protege frente a los writers legacy de Tapology, que leen el
        # documento del bout. El comando persistido protege frente a la frontera
        # canonica, que reconstruye el snapshot desde observaciones y no mira ese
        # sidecar: sin el, la siguiente pasada de ESPN revierte la decision.
        actor_id = str(getattr(admin, "id", "") or getattr(admin, "google_id", ""))
        await record_admin_field_overrides(
            db, bout_id=bout_id, fields=bout_update, actor_id=actor_id
        )
        if "is_title_fight" in bout_update or "is_bmf_title_fight" in bout_update:
            current = await db["bouts"].find_one({"id": bout_id}, {"event_id": 1})
            await record_admin_command(
                db,
                kind="title",
                event_id=int((current or {}).get("event_id") or 0),
                bout_id=bout_id,
                actor_id=actor_id,
                reason="Admin title decision",
                values=title_values(
                    is_title_fight=bool(
                        bout_update.get("is_title_fight", bout.get("is_title_fight"))
                    ),
                    is_bmf_title_fight=bout_update.get(
                        "is_bmf_title_fight", bout.get("is_bmf_title_fight")
                    ),
                ),
            )

    # Actualizar campos del card_slot
    if slot_update:
        result = await db["event_card_slots"].update_one(
            {"bout_id": bout_id},
            {"$set": slot_update}
        )
        if result.modified_count > 0:
            updated_fields.extend(list(slot_update.keys()))

        # B-011 SIGUE ABIERTO para la estructura de la card, deliberadamente.
        #
        # `event_card_slots` es propiedad exclusiva del reconciler, asi que este
        # `$set` se recalcula en la siguiente pasada. Emitir un comando
        # `bout_structure` NO lo arregla hoy: a diferencia de las senales de
        # titulo (que ESPN manda a `title_suggestions` como advisory), ESPN
        # emite `card_section` como hecho, de modo que el override de Admin lo
        # contradice en cada pasada y el plan sale con `safe_to_apply=false` y
        # cuarentenas, sin converger nunca en el replay.
        #
        # Conectar el comando aqui cambiaria un fallo silencioso por uno ruidoso
        # que ademas bloquea la card entera. Requiere una decision sobre la
        # convergencia Admin-vs-ESPN en la frontera antes de habilitarse.

    return {
        "success": True,
        "message": f"Bout {bout_id} actualizado correctamente",
        "updated_fields": updated_fields
    }
