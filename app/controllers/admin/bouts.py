"""Cancelación, borrado y edición de peleas."""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from app.controllers.admin.schemas import (
    UpdateBoutDetailsRequest,
)
from app.controllers.admin.shared import get_bout_or_404
from app.core.dependencies import CurrentAdmin, Database
from app.core.rate_limit import limiter
from app.services.admin_card_commands import (
    lifecycle_values,
    record_admin_command,
    structure_values,
    title_values,
)
from app.services.canonical_authority import record_admin_field_overrides
from app.services.points_service import PointsService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/bouts/{bout_id}/cancel")
@limiter.limit("30/minute")
async def cancel_bout(
    request: Request,
    bout_id: int,
    admin: CurrentAdmin,
    db: Database
):
    """Cancela una pelea: revierte puntos, borra sus picks y recalcula stats.

    Solo administradores.
    """
    bout = await get_bout_or_404(db, bout_id)

    # Get affected users before deleting picks
    picks_cursor = db["picks"].find({"bout_id": bout_id})
    picks = await picks_cursor.to_list(length=None)
    users_affected = {pick["user_id"] for pick in picks}
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
    # El motor lee `card_data_v1.status`, no el `status` legacy: sin esto la
    # pelea sigue contando como viva y la card nunca finaliza ni paga XP.
    # El `$exists` es obligatorio: crear el sidecar aqui metería un bout legacy
    # en la card con una proyección inválida y rompería su evaluación entera.
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

@router.delete("/bouts/{bout_id}")
@limiter.limit("30/minute")
async def delete_bout(
    request: Request,
    bout_id: int,
    admin: CurrentAdmin,
    db: Database
):
    """Borra la pelea de verdad, no la cancela: se van también sus picks, su
    slot y su cuenta en el evento, y se recalculan las stats afectadas.

    Solo administradores.
    """
    bout = await get_bout_or_404(db, bout_id)

    event_id = bout.get("event_id")

    # Recopilar usuarios afectados antes de eliminar picks
    picks_cursor = db["picks"].find({"bout_id": bout_id})
    picks = await picks_cursor.to_list(length=None)
    users_affected = {pick["user_id"] for pick in picks}
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
    bout = await get_bout_or_404(db, bout_id)

    # Separar campos del bout y del card_slot
    bout_update: dict[str, Any] = {}
    slot_update: dict[str, Any] = {}

    if body.rounds_scheduled is not None:
        bout_update["rounds_scheduled"] = body.rounds_scheduled

    if body.weight_class is not None:
        bout_update["weight_class"] = body.weight_class

    if body.is_title_fight is not None:
        bout_update["is_title_fight"] = body.is_title_fight

    if body.is_bmf_title_fight is not None:
        bout_update["is_bmf_title_fight"] = body.is_bmf_title_fight

    if body.card_section is not None:
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

        # D-DATA-010: Admin manda sobre los campos de título, y tanto `true`
        # como `false` son decisiones duraderas. Se guardan dos cosas a
        # propósito: la evidencia del sidecar frena a los writers legacy de
        # Tapology, y el comando persistido frena a la frontera canónica, que
        # reconstruye desde observaciones y si no revertiría en la pasada
        # siguiente de ESPN.
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

        # B-011 queda abierto a propósito para la estructura de la card. Este
        # `$set` lo pisa el reconciler, dueño de `event_card_slots`. Emitir un
        # comando `bout_structure` no lo arregla: ESPN manda `card_section`
        # como hecho (no advisory como el título), así el override contradice
        # cada pasada y el plan sale con `safe_to_apply=false` sin converger.
        # Cambiaría un fallo silencioso por uno que bloquea la card entera:
        # antes hay que decidir cómo convergen Admin y ESPN en la frontera.

    return {
        "success": True,
        "message": f"Bout {bout_id} actualizado correctamente",
        "updated_fields": updated_fields
    }
