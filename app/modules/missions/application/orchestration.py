"""La costura que convierte un resultado canónico en progreso de misiones.

Nada más llama a los evaluadores: un writer llama `on_bout_result`, y
cuando la card se queda sin bouts sin resolver, la misma llamada la
finaliza y pliega cada resumen en su mes. Cada paso tiene key propia para
que un retry o replay converjan en un solo resultado.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pymongo.asynchronous.database import AsyncDatabase

from app.modules.missions.application.bout_evaluation import (
    BoutEvaluationError,
    BoutResultMissionEvaluator,
    EvaluateBoutResultCommand,
    MissionEvaluationContextBuilder,
)
from app.modules.missions.application.card_finalization import (
    CardFinalizationError,
    CardFinalizationErrorCode,
    CardMissionFinalizer,
    FinalizeCardMissionsCommand,
)
from app.modules.missions.application.card_streak import (
    CardStreakService,
    CardStreakSettlement,
)
from app.modules.missions.application.monthly_progress import MonthlyProgressService
from app.modules.missions.application.progression import ProgressionService
from app.modules.missions.catalog import load_card_catalog, load_monthly_catalog
from app.modules.missions.domain.evaluation import (
    BoutOutcome,
    ResultMethodFamily,
)
from app.modules.missions.domain.monthly_metrics import MonthlyEventSummary

Clock = Callable[[], datetime]

_TERMINAL_LIFECYCLES = {"CANCELLED", "POSTPONED", "REPLACED"}
_FINISH_METHODS = {ResultMethodFamily.KO_TKO, ResultMethodFamily.SUBMISSION}


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class MissionTriggerOutcome:
    event_id: int
    bout_id: int | None
    evaluated_assignments: int = 0
    card_finalized: bool = False
    finalization_revision: int | None = None
    monthly_updates: int = 0
    streak_advanced: int = 0
    streak_broken: int = 0
    levelled_up: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)


class MissionTriggerService:
    """El único punto de entrada que llama cualquier writer, ESPN o Admin."""

    def __init__(
        self,
        db: AsyncDatabase,
        *,
        clock: Clock = _utc_now,
    ) -> None:
        self.db = db
        self.clock = clock
        self.catalog = load_card_catalog()
        self.evaluator = BoutResultMissionEvaluator(db)
        self.finalizer = CardMissionFinalizer(db)
        self.monthly = MonthlyProgressService(db, catalog=load_monthly_catalog(), clock=clock)
        self.streak = CardStreakService(db, clock=clock)
        self.progression = ProgressionService(db, clock=clock)

    async def on_bout_result(
        self,
        *,
        event_id: int,
        bout_id: int,
        result_revision: int,
    ) -> MissionTriggerOutcome:
        """Evalúa incrementalmente y finaliza la card si ya está completa."""
        errors: list[str] = []
        evaluated = 0
        try:
            result = await self.evaluator.evaluate(
                EvaluateBoutResultCommand(
                    event_id=event_id,
                    bout_id=bout_id,
                    result_revision=result_revision,
                )
            )
            evaluated = len(result.assignments)
            errors.extend(
                f"{failure.assignment_id}: {failure.message}"
                for failure in result.failures
            )
        except BoutEvaluationError as exc:
            errors.append(f"bout-evaluation: {exc}")

        streak = await self._settle_streak(event_id)
        finalized = await self._finalize_if_complete(event_id)
        levelled = await self._sync_progression(event_id)
        return MissionTriggerOutcome(
            event_id=event_id,
            bout_id=bout_id,
            evaluated_assignments=evaluated,
            card_finalized=finalized.card_finalized,
            finalization_revision=finalized.finalization_revision,
            monthly_updates=finalized.monthly_updates,
            streak_advanced=streak.advanced,
            streak_broken=streak.broken,
            levelled_up=levelled,
            errors=tuple(errors) + streak.errors + finalized.errors,
        )

    async def _sync_progression(self, event_id: int) -> int:
        """Refresca nivel y título de todos a quienes esta card acaba de pagar.

        El level-up es consecuencia de que el ledger se movió, no un evento
        que alguien emite, así que se detecta aquí una vez por card.
        """
        # Derivado del propio estado de la card, no de matchear el formato de
        # los source ids de XP, que cambia según la fuente.
        touched = set(
            await self.db["mission_assignments"].distinct(
                "user_id", {"event_id": event_id}
            )
        ) | set(
            await self.db["mission_card_streak_cards"].distinct(
                "user_id", {"event_id": event_id}
            )
        )
        levelled = 0
        for user_id in touched:
            try:
                before = await self.db["mission_user_progression"].find_one(
                    {"user_id": user_id}, {"level": 1}
                )
                projection = await self.progression.sync(user_id)
            except Exception:  # noqa: BLE001 - progresión no debe romper un resultado
                continue
            if projection.level > int((before or {}).get("level", 1)):
                levelled += 1
        return levelled

    async def _settle_streak(self, event_id: int) -> CardStreakSettlement:
        """Liquida el Card Streak una vez, con el primer resultado de la card.

        Idempotente por (usuario, card); la card solo queda marcada liquidada
        si todos tuvieron éxito, así una falla parcial se reintenta después.
        """
        if await self.streak.is_settled(event_id):
            return CardStreakSettlement(event_id=event_id, denominator=0)
        try:
            return await self.streak.settle_card(event_id)
        except Exception as exc:  # noqa: BLE001 - nunca romper la escritura de un resultado
            return CardStreakSettlement(
                event_id=event_id,
                denominator=0,
                errors=(f"card-streak: {exc}",),
            )

    async def _finalize_if_complete(self, event_id: int) -> MissionTriggerOutcome:
        """Finaliza exactamente una vez por cada set de inputs canónicos."""
        bouts, slots = await self._card(event_id)
        snapshots = self._snapshots(bouts, slots)
        unresolved = [
            snapshot.bout_id
            for snapshot in snapshots.values()
            if snapshot.is_current
            and snapshot.lifecycle.value not in _TERMINAL_LIFECYCLES
            and snapshot.result is None
        ]
        if unresolved or not snapshots:
            return MissionTriggerOutcome(event_id=event_id, bout_id=None)

        # La revisión avanza solo si cambian los inputs canónicos: un trigger
        # repetido repite la finalización congelada en vez de acuñar otra.
        revision = await self._finalization_revision(event_id)
        errors: list[str] = []
        try:
            result = await self.finalizer.finalize(
                FinalizeCardMissionsCommand(
                    event_id=event_id,
                    finalization_revision=revision,
                )
            )
            errors.extend(
                f"{failure.assignment_id}: {failure.message}"
                for failure in result.failures
            )
        except CardFinalizationError as exc:
            if exc.code == CardFinalizationErrorCode.UNRESOLVED_BOUTS:
                return MissionTriggerOutcome(event_id=event_id, bout_id=None)
            return MissionTriggerOutcome(
                event_id=event_id,
                bout_id=None,
                errors=(f"card-finalization: {exc}",),
            )

        monthly_updates = await self._fold_month(event_id, snapshots)
        return MissionTriggerOutcome(
            event_id=event_id,
            bout_id=None,
            card_finalized=True,
            finalization_revision=revision,
            monthly_updates=monthly_updates,
            errors=tuple(errors),
        )

    async def _finalization_revision(self, event_id: int) -> int:
        """Una revisión por cada set distinto de inputs canónicos de este evento."""
        existing = (
            await self.db["mission_card_finalization_runs"]
            .find({"event_id": event_id})
            .sort([("finalization_revision", -1)])
            .to_list(length=1)
        )
        return int(existing[0]["finalization_revision"]) if existing else 1

    # --------------------------------------------------------------- mensual

    async def _fold_month(self, event_id: int, snapshots: Mapping) -> int:
        month_key = await self.monthly.month_key_for_event(event_id)
        if month_key is None:
            return 0
        summaries = await self._event_summaries(event_id, month_key, snapshots)
        updated = 0
        for user_id, summary in summaries.items():
            if await self.monthly.record_event_summary(
                user_id=user_id, summary=summary
            ):
                updated += 1
        return updated

    async def _event_summaries(
        self,
        event_id: int,
        month_key: str,
        snapshots: Mapping,
    ) -> dict[str, MonthlyEventSummary]:
        bouts = await self.db["bouts"].find({"event_id": event_id}).to_list(length=None)
        canonical_names = {
            int(bout["id"]): CardMissionFinalizer._fighter_name_map(bout)
            for bout in bouts
        }
        picks = await self.db["picks"].find({"event_id": event_id}).to_list(length=None)
        assignments = (
            await self.db["mission_assignments"]
            .find({"event_id": event_id, "status": "COMPLETED"})
            .to_list(length=None)
        )

        decided = {
            snapshot.bout_id: snapshot
            for snapshot in snapshots.values()
            if snapshot.is_current
            and snapshot.lifecycle.value not in _TERMINAL_LIFECYCLES
            and snapshot.result is not None
        }
        main_card_ids = {
            snapshot.bout_id
            for snapshot in decided.values()
            if snapshot.section.value == "MAIN"
        }
        main_event_id = next(
            (
                snapshot.bout_id
                for snapshot in decided.values()
                if snapshot.role.value == "MAIN_EVENT"
            ),
            None,
        )
        co_main_id = next(
            (
                snapshot.bout_id
                for snapshot in decided.values()
                if snapshot.role.value == "CO_MAIN"
            ),
            None,
        )

        tally: dict[str, dict] = defaultdict(
            lambda: {
                "resolved_picks": 0,
                "correct": 0,
                "wrong": 0,
                "points": 0,
                "perfect": 0,
                "two_plus": 0,
                "finish_methods": 0,
                "ko": 0,
                "sub": 0,
                "dec": 0,
                "main_event": False,
                "co_main": False,
                "main_card_correct": 0,
            }
        )

        for pick in picks:
            user_id = pick.get("user_id")
            bout_id = pick.get("bout_id")
            if not isinstance(user_id, str) or bout_id not in decided:
                continue
            try:
                snapshot = MissionEvaluationContextBuilder._pick_snapshot(
                    pick, snapshots, canonical_names
                )
            except BoutEvaluationError:
                # Un pick legacy mal formado no debe envenenar el mes entero.
                continue
            result = decided[bout_id].result
            if result.outcome not in {BoutOutcome.RED_WIN, BoutOutcome.BLUE_WIN}:
                continue

            bucket = tally[user_id]
            bucket["resolved_picks"] += 1
            points = int(snapshot.points_awarded or 0)
            bucket["points"] += points
            if points >= 3:
                bucket["perfect"] += 1
            if points >= 2:
                bucket["two_plus"] += 1

            correct = snapshot.winner_fighter_id == result.winner_fighter_id
            if not correct:
                bucket["wrong"] += 1
                continue
            bucket["correct"] += 1
            if bout_id in main_card_ids:
                bucket["main_card_correct"] += 1
            if bout_id == main_event_id:
                bucket["main_event"] = True
            if bout_id == co_main_id:
                bucket["co_main"] = True

            method_matches = snapshot.method is not None and result.method.value == snapshot.method.value
            if method_matches:
                if result.method in _FINISH_METHODS:
                    bucket["finish_methods"] += 1
                if result.method == ResultMethodFamily.KO_TKO:
                    bucket["ko"] += 1
                elif result.method == ResultMethodFamily.SUBMISSION:
                    bucket["sub"] += 1
                elif result.method == ResultMethodFamily.DECISION:
                    bucket["dec"] += 1

        missions_by_user: dict[str, list[dict]] = defaultdict(list)
        for assignment in assignments:
            missions_by_user[assignment["user_id"]].append(assignment)

        summaries: dict[str, MonthlyEventSummary] = {}
        for user_id in set(tally) | set(missions_by_user):
            bucket = tally[user_id]
            completed = missions_by_user.get(user_id, [])
            hard = sum(
                1
                for assignment in completed
                if self.catalog.get(assignment["mission_id"]).difficulty.value == "HARD"
            )
            summaries[user_id] = MonthlyEventSummary(
                event_id=event_id,
                month_key=month_key,
                # La revisión es la identidad propia del input: un resultado
                # corregido produce un resumen distinto para el mismo evento.
                summary_revision=max(
                    1,
                    max(
                        (snapshot.result.revision for snapshot in decided.values()),
                        default=1,
                    ),
                ),
                resolved_bouts=len(decided),
                resolved_picks=bucket["resolved_picks"],
                correct_winners=bucket["correct"],
                wrong_winners=bucket["wrong"],
                pick_points=bucket["points"],
                perfect_picks=bucket["perfect"],
                two_plus_point_picks=bucket["two_plus"],
                correct_finish_methods=bucket["finish_methods"],
                correct_ko_wins=bucket["ko"],
                correct_submission_wins=bucket["sub"],
                correct_decision_wins=bucket["dec"],
                main_event_correct=bucket["main_event"],
                co_main_correct=bucket["co_main"],
                main_card_bouts=len(main_card_ids),
                main_card_correct=bucket["main_card_correct"],
                completed_card_missions=len(completed),
                completed_hard_card_missions=hard,
            )
        return summaries

    # ---------------------------------------------------------------- común

    async def _card(self, event_id: int) -> tuple[list[dict], list[dict]]:
        bouts = await self.db["bouts"].find({"event_id": event_id}).to_list(length=None)
        slots = (
            await self.db["event_card_slots"]
            .find({"event_id": event_id})
            .to_list(length=None)
        )
        return bouts, slots

    @staticmethod
    def _snapshots(bouts: list[dict], slots: list[dict]) -> dict[int, object]:
        slots_by_bout: dict[int, Mapping] = {}
        for slot in sorted(
            slots,
            key=lambda value: (
                bool(value.get("is_current")),
                int(value.get("structure_revision") or 0),
            ),
        ):
            if isinstance(slot.get("bout_id"), int):
                slots_by_bout[slot["bout_id"]] = slot
        snapshots = {}
        for bout in bouts:
            # Igual que en la evaluación y la finalización: lo que no pertenece
            # a la card canónica no entra ni la rompe.
            if not MissionEvaluationContextBuilder._belongs_to_the_card(
                bout, slots_by_bout
            ):
                continue
            snapshot = MissionEvaluationContextBuilder._bout_snapshot(
                bout, slots_by_bout.get(bout.get("id"))
            )
            snapshots[snapshot.bout_id] = snapshot
        return snapshots


__all__ = [
    "MissionTriggerOutcome",
    "MissionTriggerService",
    "project_admin_result_to_canonical",
]


_ADMIN_METHOD_FAMILY = {
    "KO": "ko_tko",
    "TKO": "ko_tko",
    "KO/TKO": "ko_tko",
    "SUB": "submission",
    "SUBMISSION": "submission",
    "DEC": "decision",
    "DECISION": "decision",
    "DQ": "dq",
}


def project_admin_result_to_canonical(
    bout: Mapping,
    result_data: Mapping,
) -> dict | None:
    """Traduce un resultado Admin al `card_data_v1.result` canónico.

    None si el bout nunca pasó por CardData: el write de Admin igual debe
    tener éxito, solo que no dispara evaluación de misiones (D-ARCH-018).
    """
    sidecar = bout.get("card_data_v1")
    if not isinstance(sidecar, Mapping):
        return None
    fighters = sidecar.get("fighters") or []
    by_corner = {
        str(fighter.get("corner")): fighter.get("fighter_id")
        for fighter in fighters
        if isinstance(fighter, Mapping)
    }

    outcome = str(result_data.get("outcome") or "").lower()
    winner_corner = result_data.get("winner")
    if outcome in {"red", "blue"} or winner_corner in {"red", "blue"}:
        corner = winner_corner if winner_corner in {"red", "blue"} else outcome
        winner_fighter_id = by_corner.get(corner)
        if not winner_fighter_id:
            return None
        canonical_outcome = f"{corner}_win"
    elif outcome == "draw":
        winner_fighter_id, canonical_outcome = None, "draw"
    elif outcome in {"nc", "no_contest"}:
        winner_fighter_id, canonical_outcome = None, "no_contest"
    else:
        return None

    method_family = _ADMIN_METHOD_FAMILY.get(
        str(result_data.get("method") or "").upper(), "other"
    )
    ending_round = result_data.get("round")
    if not isinstance(ending_round, int) or not 1 <= ending_round <= 5:
        ending_round = int(sidecar.get("scheduled_rounds") or 3)

    revision = int(sidecar.get("result_revision") or 0) + 1
    return {
        "card_data_v1.status": "completed",
        "card_data_v1.result_revision": revision,
        "card_data_v1.result": {
            "revision": revision,
            "status": "corrected" if revision > 1 else "final",
            "outcome": canonical_outcome,
            "winner_fighter_id": winner_fighter_id,
            "method_family": method_family,
            "ending_round": ending_round,
        },
    }
