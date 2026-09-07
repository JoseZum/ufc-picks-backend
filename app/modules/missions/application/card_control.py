"""Control admin de la ventana de misiones de una card: close, reopen, VOID.

Único escritor de `mission_card_controls` (antes solo se leía y quedaba en
`OPEN` por defecto). VOID es irreversible: liquida toda asignación porque
una card que nunca ocurrió no puede dejar misiones que ni completan ni fallan.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pymongo.asynchronous.database import AsyncDatabase

from app.modules.missions.domain.enums import (
    CardMissionState,
    MissionAssignmentStatus,
    MissionTransitionReason,
    StringEnum,
)
from app.modules.missions.domain.state_machines import (
    IllegalMissionTransition,
    ensure_assignment_transition,
    ensure_card_transition,
)

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class CardControlErrorCode(StringEnum):
    CARD_NOT_FOUND = "CARD_NOT_FOUND"
    ALREADY_VOID = "ALREADY_VOID"
    REASON_REQUIRED = "REASON_REQUIRED"


class CardControlError(ValueError):
    def __init__(self, code: CardControlErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CardControlState:
    event_id: int
    state: CardMissionState
    reason: str | None = None
    actor_id: str | None = None
    updated_at: datetime | None = None
    voided_assignments: int = 0
    revision: int = 0
    history: tuple[dict, ...] = field(default_factory=tuple)


class CardControlService:
    """Lee y mueve el estado de misiones de una card según las reglas aprobadas."""

    def __init__(self, db: AsyncDatabase, *, clock: Clock = _utc_now) -> None:
        self.db = db
        self.clock = clock
        self.controls = db["mission_card_controls"]

    # ----------------------------------------------------------------- lecturas

    async def state_for(self, event_id: int) -> CardControlState:
        """Una card sin tocar está OPEN: el mismo default que asume Home."""
        document = await self.controls.find_one({"event_id": event_id})
        if document is None:
            return CardControlState(event_id=event_id, state=CardMissionState.OPEN)
        return CardControlState(
            event_id=event_id,
            state=CardMissionState(document.get("state", "OPEN")),
            reason=document.get("reason"),
            actor_id=document.get("actor_id"),
            updated_at=document.get("updated_at"),
            voided_assignments=int(document.get("voided_assignments", 0)),
            revision=int(document.get("revision", 0)),
            history=tuple(document.get("history") or ()),
        )

    # --------------------------------------------------------------- escrituras

    async def close(self, *, event_id: int, actor_id: str, reason: str) -> CardControlState:
        return await self._transition(
            event_id=event_id,
            target=CardMissionState.CLOSED,
            why=MissionTransitionReason.ADMIN_CLOSE,
            actor_id=actor_id,
            reason=reason,
        )

    async def reopen(self, *, event_id: int, actor_id: str, reason: str) -> CardControlState:
        return await self._transition(
            event_id=event_id,
            target=CardMissionState.OPEN,
            why=MissionTransitionReason.ADMIN_REOPEN,
            actor_id=actor_id,
            reason=reason,
        )

    async def void(self, *, event_id: int, actor_id: str, reason: str) -> CardControlState:
        """Pone la card en VOID y liquida todas sus asignaciones. Irreversible."""
        return await self._transition(
            event_id=event_id,
            target=CardMissionState.VOID,
            why=MissionTransitionReason.ADMIN_VOID,
            actor_id=actor_id,
            reason=reason,
        )

    async def _transition(
        self,
        *,
        event_id: int,
        target: CardMissionState,
        why: MissionTransitionReason,
        actor_id: str,
        reason: str,
    ) -> CardControlState:
        if not (reason or "").strip():
            raise CardControlError(
                CardControlErrorCode.REASON_REQUIRED,
                "An Admin card action must record why it was taken",
            )
        if not await self.db["events"].find_one({"id": event_id}, {"_id": 1}):
            raise CardControlError(
                CardControlErrorCode.CARD_NOT_FOUND, f"Event {event_id} does not exist"
            )

        current = await self.state_for(event_id)
        if current.state == target:
            # Repetir la acción no es error: la card ya está en ese estado.
            return current
        if current.state == CardMissionState.VOID:
            raise CardControlError(
                CardControlErrorCode.ALREADY_VOID,
                f"Event {event_id} is VOID and cannot change state again",
            )
        ensure_card_transition(current.state, target, why)

        now = self.clock()
        voided = (
            await self._void_assignments(event_id, actor_id=actor_id, reason=reason)
            if target == CardMissionState.VOID
            else 0
        )
        entry = {
            "from": current.state.value,
            "to": target.value,
            "transition_reason": why.value,
            "reason": reason,
            "actor_id": actor_id,
            "at": now,
        }
        await self.controls.update_one(
            {"event_id": event_id},
            {
                "$set": {
                    "event_id": event_id,
                    "state": target.value,
                    "reason": reason,
                    "actor_id": actor_id,
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now},
                "$inc": {"revision": 1, "voided_assignments": voided},
                "$push": {"history": entry},
            },
            upsert=True,
        )
        return await self.state_for(event_id)

    async def _void_assignments(
        self, event_id: int, *, actor_id: str, reason: str
    ) -> int:
        """Liquida las misiones pendientes de una card que nunca va a ocurrir.

        Las ya liquidadas no se tocan: revertirlas sería una segunda decisión
        no auditada sobre un ledger de XP que es append-only.
        """
        now = self.clock()
        voided = 0
        cursor = self.db["mission_assignments"].find(
            {"event_id": event_id, "status": MissionAssignmentStatus.ACTIVE.value}
        )
        async for assignment in cursor:
            try:
                ensure_assignment_transition(
                    MissionAssignmentStatus(assignment["status"]),
                    MissionAssignmentStatus.VOID,
                    MissionTransitionReason.ADMIN_VOID,
                )
            except IllegalMissionTransition:
                continue
            result = await self.db["mission_assignments"].update_one(
                {"_id": assignment["_id"], "revision": assignment["revision"]},
                {
                    "$set": {
                        "status": MissionAssignmentStatus.VOID.value,
                        "void_reason": "ADMIN_VOID",
                        "voided_at": now,
                        "voided_by": actor_id,
                        "void_note": reason,
                        "updated_at": now,
                    },
                    "$inc": {"revision": 1},
                },
            )
            voided += int(result.modified_count)
        return voided


__all__ = [
    "CardControlError",
    "CardControlErrorCode",
    "CardControlService",
    "CardControlState",
]
