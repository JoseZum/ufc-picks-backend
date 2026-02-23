"""
Controlador de Admin - Endpoints exclusivos para administradores
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, status, UploadFile, File
from pydantic import BaseModel

import os

from app.core.dependencies import CurrentAdmin, Database
from app.core.rate_limit import limiter
from app.services.points_service import PointsService
from app.services.s3_service import get_s3_service, S3WriteNotAllowedError, S3ServiceError


router = APIRouter(prefix="/admin", tags=["admin"])


# ============================================
# REQUEST SCHEMAS
# ============================================

class UpdateEventTimingRequest(BaseModel):
    """Datos para actualizar fecha/hora de un evento."""
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
    # Campos del event_card_slot
    card_section: Optional[str] = None  # "main" | "prelim" | "early_prelim"
    order_overall: Optional[int] = None
    order_section: Optional[int] = None
    is_main_event: Optional[bool] = None
    is_co_main: Optional[bool] = None


# ============================================
# EVENT ART UPLOAD ENDPOINT
# ============================================

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


# ============================================
# EVENT TIMING ENDPOINTS
# ============================================

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

    # Construir update
    update_data = {}
    if body.event_date:
        update_data["date"] = body.event_date
    if body.picks_lock_date:
        update_data["picks_lock_date"] = body.picks_lock_date

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


# ============================================
# RESULT ENDPOINTS
# ============================================

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
        "method": body.method,
        "round": body.round,
        "time": body.time
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

    return {
        "success": True,
        "message": f"Resultado del bout {bout_id} eliminado y puntos revertidos"
    }


# ============================================
# STATS RECALCULATION ENDPOINT
# ============================================

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


# ============================================
# PICKS LOCK ENDPOINTS
# ============================================

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
        {"$set": {"picks_locked": True}}
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
        {"$set": {"picks_locked": False}}
    )

    # Update all picks for this event to locked: False
    picks_result = await db["picks"].update_many(
        {"event_id": event_id, "locked": True},
        {"$set": {"locked": False}}
    )

    return {
        "success": True,
        "message": f"Picks desbloqueados para evento {event_id}",
        "event_id": event_id,
        "picks_locked": False,
        "picks_updated": picks_result.modified_count
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
        {"$set": {"picks_locked": True}}
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
        {"$set": {"picks_locked": False}}
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


# ============================================
# BOUT CANCELLATION ENDPOINT
# ============================================

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


# ============================================
# FIGHTER PHOTO UPLOAD ENDPOINT
# ============================================

@router.post("/fighters/photo")
@limiter.limit("30/minute")
async def upload_fighter_photo(
    request: Request,
    admin: CurrentAdmin,
    file: UploadFile = File(...)
):
    """Sube una foto de peleador a S3."""
    # Validar que sea PNG o JPG
    valid_extensions = ['.png', '.jpg', '.jpeg']
    if not file.filename or not any(file.filename.lower().endswith(ext) for ext in valid_extensions):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Solo se aceptan archivos PNG y JPG"
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
        'jpeg': 'image/jpeg'
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


# ============================================
# BOUT DELETION ENDPOINT
# ============================================

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


# ============================================
# BOUT DETAILS EDIT ENDPOINT
# ============================================

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

    # Actualizar campos del card_slot
    if slot_update:
        result = await db["event_card_slots"].update_one(
            {"bout_id": bout_id},
            {"$set": slot_update}
        )
        if result.modified_count > 0:
            updated_fields.extend(list(slot_update.keys()))

    return {
        "success": True,
        "message": f"Bout {bout_id} actualizado correctamente",
        "updated_fields": updated_fields
    }
