"""Cierra el hueco entre quien escribe un resultado y quien evalúa misiones.

`on_bout_result` es el único disparador; el scraper nunca lo llama, así que
sin este barrido las misiones quedan congeladas. Reusa el trigger, ya
idempotente, en vez de duplicar lógica. Protege con ventana temporal y
marca de agua propia por bout y revisión.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from app.modules.missions.application.orchestration import MissionTriggerService

WATERMARK_COLLECTION = "mission_result_reconciliation"

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class PendingResult:
    event_id: int
    bout_id: int
    result_revision: int

    @property
    def watermark_id(self) -> str:
        return f"{self.bout_id}:{self.result_revision}"


@dataclass
class ReconciliationReport:
    """Lo que hizo la pasada, en términos útiles para un log de CI."""

    scanned: int = 0
    triggered: int = 0
    skipped: int = 0
    evaluated_assignments: int = 0
    cards_finalized: int = 0
    errors: list[str] = field(default_factory=list)
    bouts: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "scanned": self.scanned,
            "triggered": self.triggered,
            "skipped": self.skipped,
            "evaluated_assignments": self.evaluated_assignments,
            "cards_finalized": self.cards_finalized,
            "errors": list(self.errors),
            "bouts": list(self.bouts),
        }


class MissionResultReconciler:
    def __init__(
        self,
        db: AsyncDatabase,
        *,
        clock: Clock = _utc_now,
        trigger_factory: Callable[[AsyncDatabase], MissionTriggerService] | None = None,
    ) -> None:
        self.db = db
        self.clock = clock
        self._trigger_factory = trigger_factory or MissionTriggerService

    async def pending(
        self,
        *,
        event_id: int | None = None,
        window_days: int = 3,
    ) -> list[PendingResult]:
        """Resultados canónicos finales que aún no pasaron por el trigger."""
        event_ids = await self._events_in_scope(event_id, window_days)
        if not event_ids:
            return []

        cursor = self.db["bouts"].find(
            {
                "event_id": {"$in": event_ids},
                "card_data_v1.result_revision": {"$gte": 1},
                "card_data_v1.result.status": "final",
            },
            {"id": 1, "event_id": 1, "card_data_v1.result_revision": 1},
        )
        candidates = [
            PendingResult(
                event_id=int(document["event_id"]),
                bout_id=int(document["id"]),
                result_revision=int(document["card_data_v1"]["result_revision"]),
            )
            for document in await cursor.to_list(length=None)
        ]
        if not candidates:
            return []

        done = set(
            await self.db[WATERMARK_COLLECTION].distinct(
                "_id", {"_id": {"$in": [item.watermark_id for item in candidates]}}
            )
        )
        pending = [item for item in candidates if item.watermark_id not in done]
        # Orden de la card: una racha o finalización lee el estado que dejó la
        # pelea anterior, reproducir fuera de orden daría otro resultado.
        pending.sort(key=lambda item: (item.event_id, item.bout_id))
        return pending

    async def reconcile(
        self,
        *,
        event_id: int | None = None,
        window_days: int = 3,
        dry_run: bool = False,
    ) -> ReconciliationReport:
        report = ReconciliationReport()
        pending = await self.pending(event_id=event_id, window_days=window_days)
        report.scanned = len(pending)
        if dry_run:
            report.skipped = len(pending)
            report.bouts = [
                {
                    "event_id": item.event_id,
                    "bout_id": item.bout_id,
                    "result_revision": item.result_revision,
                    "action": "would_trigger",
                }
                for item in pending
            ]
            return report

        trigger = self._trigger_factory(self.db)
        finalized_events: set[int] = set()
        for item in pending:
            try:
                outcome = await trigger.on_bout_result(
                    event_id=item.event_id,
                    bout_id=item.bout_id,
                    result_revision=item.result_revision,
                )
            except Exception as exc:  # noqa: BLE001
                # Sin marca de agua la siguiente pasada reintenta; un bout que
                # falla no puede bloquear a los que siguen.
                report.errors.append(f"bout {item.bout_id}: {exc!r}")
                report.bouts.append(
                    {
                        "event_id": item.event_id,
                        "bout_id": item.bout_id,
                        "result_revision": item.result_revision,
                        "action": "raised",
                    }
                )
                continue

            if outcome.errors:
                report.errors.extend(
                    f"bout {item.bout_id}: {message}" for message in outcome.errors
                )
            report.triggered += 1
            report.evaluated_assignments += outcome.evaluated_assignments
            if outcome.card_finalized:
                # No significa "yo la finalicé": es idempotente (un único run),
                # por eso se cuenta por evento y no por bout.
                finalized_events.add(item.event_id)
            report.bouts.append(
                {
                    "event_id": item.event_id,
                    "bout_id": item.bout_id,
                    "result_revision": item.result_revision,
                    "action": "triggered",
                    "evaluated_assignments": outcome.evaluated_assignments,
                    "card_finalized": outcome.card_finalized,
                }
            )
            await self._mark(item, outcome.evaluated_assignments)
        report.cards_finalized = len(finalized_events)
        return report

    async def _events_in_scope(
        self, event_id: int | None, window_days: int
    ) -> list[int]:
        if event_id is not None:
            return [int(event_id)]
        if window_days <= 0:
            return []
        since = self.clock() - timedelta(days=int(window_days))
        cursor = self.db["events"].find({"date": {"$gte": since}}, {"id": 1})
        return [
            int(document["id"])
            for document in await cursor.to_list(length=None)
            if document.get("id") is not None
        ]

    async def _mark(self, item: PendingResult, evaluated: int) -> None:
        try:
            await self.db[WATERMARK_COLLECTION].insert_one(
                {
                    "_id": item.watermark_id,
                    "event_id": item.event_id,
                    "bout_id": item.bout_id,
                    "result_revision": item.result_revision,
                    "evaluated_assignments": evaluated,
                    "reconciled_at": self.clock(),
                }
            )
        except DuplicateKeyError:
            # Dos pasadas a la vez: `on_bout_result` ya es idempotente, la
            # segunda no paga nada y la marca ya existe.
            pass


__all__ = [
    "WATERMARK_COLLECTION",
    "MissionResultReconciler",
    "PendingResult",
    "ReconciliationReport",
]
