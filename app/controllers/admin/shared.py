"""Helpers compartidos por los routers de admin."""

import logging
from typing import Any

from fastapi import HTTPException, status

from app.modules.missions.application.orchestration import (
    MissionTriggerService,
)

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


async def get_bout_or_404(db, bout_id: int) -> dict[str, Any]:
    """Documento crudo de la pelea. Admin trabaja sobre el documento entero."""
    bout = await db["bouts"].find_one({"id": bout_id})
    if not bout:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Bout {bout_id} no encontrado",
        )
    return bout


async def get_event_or_404(db, event_id: int) -> dict[str, Any]:
    """Documento crudo del evento."""
    event = await db["events"].find_one({"id": event_id})
    if not event:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Evento {event_id} no encontrado",
        )
    return event
