"""Idempotent mission evaluation after a canonical bout result changes."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import ValidationError
from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from app.modules.missions.application.celebration_queue import CelebrationQueueService
from app.modules.missions.application.xp_ledger import XpLedgerService
from app.modules.missions.domain.celebrations import (
    CelebrationKind,
    CelebrationPresentation,
    EnqueueCelebrationCommand,
)
from app.modules.missions.domain.definitions import (
    CardPropMissionDefinition,
    ComboBuilderMissionDefinition,
    ComboLegTarget,
    EvaluationMoment,
    MissionDefinition,
    TargetFighterMissionDefinition,
    TargetFightMissionDefinition,
    WinMethod,
    validate_mission_definition,
)
from app.modules.missions.domain.enums import (
    MissionAssignmentStatus,
    MissionTransitionReason,
    StringEnum,
)
from app.modules.missions.domain.evaluation import (
    BoutEvaluationSnapshot,
    BoutLifecycle,
    BoutOutcome,
    BoutResultSnapshot,
    BoutRole,
    CardSection,
    FighterMetricLeg,
    FightMetricLeg,
    LeaderboardEvaluationSnapshot,
    MetricSelectionSnapshot,
    MissionEvaluationContext,
    PickEvaluationSnapshot,
    ResultMethodFamily,
)
from app.modules.missions.domain.exceptional import EXCEPTIONAL_EVALUATOR_REGISTRY
from app.modules.missions.domain.metrics import CARD_METRIC_REGISTRY
from app.modules.missions.domain.resolution import (
    MetricResolution,
    MetricResolutionStatus,
    resolve_metric_observation,
)
from app.modules.missions.domain.state_machines import ensure_assignment_transition
from app.modules.missions.domain.xp import (
    AwardXpCommand,
    CompensateXpCommand,
    XpSourceType,
)

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class BoutEvaluationErrorCode(StringEnum):
    EVENT_NOT_FOUND = "EVENT_NOT_FOUND"
    BOUT_NOT_FOUND = "BOUT_NOT_FOUND"
    RESULT_NOT_FINAL = "RESULT_NOT_FINAL"
    RESULT_REVISION_MISMATCH = "RESULT_REVISION_MISMATCH"
    INVALID_CARD_DATA = "INVALID_CARD_DATA"
    STALE_ASSIGNMENT = "STALE_ASSIGNMENT"


class BoutEvaluationError(ValueError):
    def __init__(self, code: BoutEvaluationErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class EvaluateBoutResultCommand:
    event_id: int
    bout_id: int
    result_revision: int

    def __post_init__(self) -> None:
        if self.event_id < 1 or self.bout_id < 1 or self.result_revision < 1:
            raise ValueError("event, bout and result revision must be positive")


@dataclass(frozen=True)
class AssignmentEvaluationTrigger:
    event_id: int
    trigger_type: str
    trigger_id: int
    trigger_revision: int
    card_finalized: bool = False
    bout_id: int | None = None
    result_revision: int | None = None

    def __post_init__(self) -> None:
        if self.event_id < 1 or self.trigger_id < 1 or self.trigger_revision < 1:
            raise ValueError("evaluation trigger identities and revision must be positive")
        if not self.trigger_type.strip():
            raise ValueError("evaluation trigger type cannot be blank")


@dataclass(frozen=True)
class AssignmentEvaluationResult:
    assignment_id: str
    mission_id: str
    previous_status: MissionAssignmentStatus
    status: MissionAssignmentStatus
    assignment_revision: int
    replayed: bool
    xp_delta: int


@dataclass(frozen=True)
class AssignmentEvaluationFailure:
    assignment_id: str
    error_code: str
    message: str


@dataclass(frozen=True)
class BoutEvaluationResult:
    event_id: int
    bout_id: int
    result_revision: int
    assignments: tuple[AssignmentEvaluationResult, ...]
    failures: tuple[AssignmentEvaluationFailure, ...] = ()

    @property
    def replayed_count(self) -> int:
        return sum(item.replayed for item in self.assignments)


_SECTION_MAP = {
    "main": CardSection.MAIN,
    "prelim": CardSection.PRELIM,
    "early_prelim": CardSection.EARLY_PRELIM,
}
_ROLE_MAP = {
    "main_event": BoutRole.MAIN_EVENT,
    "co_main": BoutRole.CO_MAIN,
    "regular": BoutRole.STANDARD,
}
_LIFECYCLE_MAP = {
    "completed": BoutLifecycle.COMPLETED,
    "cancelled": BoutLifecycle.CANCELLED,
    "postponed": BoutLifecycle.POSTPONED,
    "replaced": BoutLifecycle.REPLACED,
}
_OUTCOME_MAP = {
    "red_win": BoutOutcome.RED_WIN,
    "blue_win": BoutOutcome.BLUE_WIN,
    "draw": BoutOutcome.DRAW,
    "no_contest": BoutOutcome.NO_CONTEST,
}
_RESULT_METHOD_MAP = {
    "ko_tko": ResultMethodFamily.KO_TKO,
    "submission": ResultMethodFamily.SUBMISSION,
    "decision": ResultMethodFamily.DECISION,
    "dq": ResultMethodFamily.DQ,
    "other": ResultMethodFamily.OTHER,
}


def _normalize_name(value: object) -> str:
    return " ".join(str(value or "").lower().strip().split())


def _pick_method(value: object) -> WinMethod:
    normalized = str(value or "").upper().replace("-", "_")
    if normalized in {"KO", "TKO", "KO/TKO", "KO_TKO"}:
        return WinMethod.KO_TKO
    if normalized in {"SUB", "SUBMISSION"}:
        return WinMethod.SUBMISSION
    if normalized in {"DEC", "DECISION"}:
        return WinMethod.DECISION
    raise BoutEvaluationError(
        BoutEvaluationErrorCode.INVALID_CARD_DATA,
        f"Unknown canonical pick method {value!r}",
    )


def _canonical_fighters(sidecar: Mapping) -> tuple[tuple[str, str], dict[str, str]]:
    by_corner: dict[str, tuple[str, str]] = {}
    by_name: dict[str, str] = {}
    for fighter in sidecar.get("fighters") or ():
        if not isinstance(fighter, Mapping):
            continue
        corner = str(fighter.get("corner") or "").lower()
        fighter_id = fighter.get("fighter_id")
        name = fighter.get("display_name")
        if corner not in {"red", "blue"} or not isinstance(fighter_id, str):
            continue
        if not fighter_id.strip():
            continue
        by_corner[corner] = (fighter_id, str(name or fighter_id))
        by_name[_normalize_name(name)] = fighter_id
    if set(by_corner) != {"red", "blue"}:
        raise BoutEvaluationError(
            BoutEvaluationErrorCode.INVALID_CARD_DATA,
            f"Bout {sidecar.get('bout_id')} lacks stable red/blue fighter IDs",
        )
    return (
        (by_corner["red"][0], by_corner["blue"][0]),
        by_name,
    )


def _score_pick(pick: PickEvaluationSnapshot, bout: BoutEvaluationSnapshot) -> int:
    result = bout.result
    if result is None or result.winner_fighter_id != pick.winner_fighter_id:
        return 0
    points = 1
    method_matches = (
        (pick.method == WinMethod.KO_TKO and result.method == ResultMethodFamily.KO_TKO)
        or (
            pick.method == WinMethod.SUBMISSION
            and result.method == ResultMethodFamily.SUBMISSION
        )
        or (
            pick.method == WinMethod.DECISION
            and result.method == ResultMethodFamily.DECISION
        )
    )
    if not method_matches:
        return points
    points += 1
    if pick.method != WinMethod.DECISION and pick.round == result.round:
        points += 1
    return points


class MissionEvaluationContextBuilder:
    """Translate persisted CardData V1 and legacy picks into domain snapshots."""

    def __init__(self, db: AsyncDatabase) -> None:
        self.db = db

    async def build(
        self,
        assignment: Mapping,
        *,
        session: AsyncClientSession,
        card_finalized: bool = False,
        leaderboard: LeaderboardEvaluationSnapshot | None = None,
    ) -> MissionEvaluationContext:
        event_id = int(assignment["event_id"])
        event = await self.db["events"].find_one({"id": event_id}, session=session)
        if not event:
            raise BoutEvaluationError(
                BoutEvaluationErrorCode.EVENT_NOT_FOUND,
                f"Event {event_id} was not found",
            )
        bouts = await self.db["bouts"].find(
            {"event_id": event_id}, session=session
        ).to_list(length=None)
        slots = await self.db["event_card_slots"].find(
            {"event_id": event_id}, session=session
        ).to_list(length=None)
        picks = await self.db["picks"].find(
            {"event_id": event_id, "user_id": assignment["user_id"]},
            session=session,
        ).to_list(length=None)

        slots_by_bout: dict[int, Mapping] = {}
        for slot in sorted(
            slots,
            key=lambda value: (
                bool(value.get("is_current")),
                int(value.get("structure_revision") or 0),
            ),
        ):
            bout_id = slot.get("bout_id")
            if isinstance(bout_id, int):
                slots_by_bout[bout_id] = slot

        bout_snapshots = tuple(
            self._bout_snapshot(bout, slots_by_bout.get(bout.get("id")))
            for bout in sorted(bouts, key=lambda value: value.get("id", 0))
            if self._belongs_to_the_card(bout, slots_by_bout)
        )
        by_id = {bout.bout_id: bout for bout in bout_snapshots}
        name_maps = {
            int((bout.get("card_data_v1") or {}).get("bout_id", bout["id"])): (
                _canonical_fighters(bout.get("card_data_v1") or {})[1]
            )
            for bout in bouts
            if self._belongs_to_the_card(bout, slots_by_bout)
        }

        eligibility = assignment.get("eligibility_snapshot") or {}
        targets = eligibility.get("eligible_targets") or ()
        frozen_ids = tuple(
            int(target["bout_id"])
            for target in targets
            if isinstance(target, Mapping) and target.get("bout_id") in by_id
        )
        if not frozen_ids:
            raise BoutEvaluationError(
                BoutEvaluationErrorCode.INVALID_CARD_DATA,
                "Assignment has no frozen canonical eligibility targets",
            )

        pick_snapshots = tuple(
            self._pick_snapshot(pick, by_id, name_maps)
            for pick in picks
            if pick.get("bout_id") in by_id
        )
        selection = self._selection_snapshot(
            validate_mission_definition(assignment["definition_snapshot"]),
            assignment.get("selection") or {},
            by_id,
            name_maps,
        )
        revision = eligibility.get("eligibility_revision")
        if not isinstance(revision, int) or revision < 1:
            revision = int(assignment.get("card_revision") or 1)
        fingerprint = eligibility.get("fingerprint")
        if not isinstance(fingerprint, str) or len(fingerprint) < 8:
            serialized = ",".join(map(str, sorted(frozen_ids)))
            fingerprint = f"derived:{hashlib.sha256(serialized.encode()).hexdigest()}"

        try:
            return MissionEvaluationContext(
                event_id=event_id,
                user_id=str(assignment["user_id"]),
                card_revision=int(assignment["card_revision"]),
                eligibility_revision=revision,
                eligibility_fingerprint=fingerprint,
                frozen_eligible_bout_ids=frozen_ids,
                card_finalized=card_finalized,
                bouts=bout_snapshots,
                picks=pick_snapshots,
                selection=selection,
                leaderboard=leaderboard,
            )
        except ValidationError as exc:
            raise BoutEvaluationError(
                BoutEvaluationErrorCode.INVALID_CARD_DATA,
                f"Invalid evaluation context for {assignment['_id']}: {exc}",
            ) from exc

    @staticmethod
    def _belongs_to_the_card(
        bout: Mapping, slots_by_bout: Mapping[int, Mapping]
    ) -> bool:
        """¿Este bout forma parte de la estructura canónica de la card?

        La estructura la definen los slots, que son propiedad del reconciliador.
        Un bout sin slot Y sin sidecar `card_data_v1` nunca cruzó la frontera
        CardData: es un resto legacy o creado a mano, y no pertenece a la card.
        Incluirlo reventaba la evaluación de TODA la card por un bout que a nadie
        le importa -- ocurrió en producción con el `1143866` (cancelado, sin
        slot y sin sidecar) el día de Gamrot vs Salkilld.

        Un bout que SÍ tiene sidecar pero perdió su slot es otra cosa: eso es
        corrupción de la estructura y debe seguir gritando, no silenciarse.
        """
        bout_id = bout.get("id")
        if isinstance(bout_id, int) and bout_id in slots_by_bout:
            return True
        return bool(bout.get("card_data_v1"))

    @staticmethod
    def _bout_snapshot(bout: Mapping, slot: Mapping | None) -> BoutEvaluationSnapshot:
        sidecar = bout.get("card_data_v1") or {}
        bout_id = sidecar.get("bout_id", bout.get("id"))
        if not isinstance(bout_id, int):
            raise BoutEvaluationError(
                BoutEvaluationErrorCode.INVALID_CARD_DATA,
                "Canonical bout ID is missing",
            )
        if not slot:
            raise BoutEvaluationError(
                BoutEvaluationErrorCode.INVALID_CARD_DATA,
                f"Bout {bout_id} has no canonical card slot",
            )
        fighter_ids, _names = _canonical_fighters(sidecar)
        status = str(sidecar.get("status") or bout.get("status") or "scheduled").lower()
        lifecycle = _LIFECYCLE_MAP.get(status, BoutLifecycle.SCHEDULED)
        result_value = sidecar.get("result")
        result = None
        if isinstance(result_value, Mapping):
            try:
                result = BoutResultSnapshot(
                    revision=int(result_value["revision"]),
                    outcome=_OUTCOME_MAP[str(result_value["outcome"]).lower()],
                    winner_fighter_id=result_value.get("winner_fighter_id"),
                    method=_RESULT_METHOD_MAP[
                        str(result_value["method_family"]).lower()
                    ],
                    round=int(result_value["ending_round"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise BoutEvaluationError(
                    BoutEvaluationErrorCode.INVALID_CARD_DATA,
                    f"Bout {bout_id} has an invalid canonical result",
                ) from exc
        section = _SECTION_MAP.get(str(slot.get("card_section") or "").lower())
        role = _ROLE_MAP.get(str(slot.get("role") or "regular").lower())
        if section is None or role is None:
            raise BoutEvaluationError(
                BoutEvaluationErrorCode.INVALID_CARD_DATA,
                f"Bout {bout_id} has an invalid canonical section or role",
            )
        try:
            return BoutEvaluationSnapshot(
                bout_id=bout_id,
                fighter_ids=fighter_ids,
                section=section,
                role=role,
                is_title_fight=bool(sidecar.get("is_title_fight", False)),
                scheduled_rounds=int(sidecar.get("scheduled_rounds") or 3),
                lifecycle=lifecycle,
                is_current=bool(slot.get("is_current", True)),
                matchup_revision=int(sidecar.get("matchup_revision") or 1),
                result=result,
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise BoutEvaluationError(
                BoutEvaluationErrorCode.INVALID_CARD_DATA,
                f"Bout {bout_id} violates the evaluation snapshot contract",
            ) from exc

    @staticmethod
    def _pick_snapshot(
        pick: Mapping,
        bouts: Mapping[int, BoutEvaluationSnapshot],
        names: Mapping[int, Mapping[str, str]],
    ) -> PickEvaluationSnapshot:
        bout_id = int(pick["bout_id"])
        bout = bouts[bout_id]
        fighter_id = pick.get("picked_fighter_id")
        if fighter_id not in bout.fighter_ids:
            fighter_id = names[bout_id].get(_normalize_name(pick.get("picked_fighter_name")))
        if fighter_id not in bout.fighter_ids:
            raise BoutEvaluationError(
                BoutEvaluationErrorCode.INVALID_CARD_DATA,
                f"Pick {pick.get('_id')} cannot be mapped to a stable fighter ID",
            )
        method = _pick_method(pick.get("picked_method"))
        round_value = pick.get("picked_round") if method != WinMethod.DECISION else None
        base = PickEvaluationSnapshot(
            bout_id=bout_id,
            winner_fighter_id=str(fighter_id),
            method=method,
            round=round_value,
        )
        if bout.result is None:
            return base
        points = _score_pick(base, bout)
        return PickEvaluationSnapshot(
            **base.model_dump(
                mode="python",
                exclude={"points_awarded", "score_revision"},
            ),
            points_awarded=points,
            score_revision=bout.result.revision,
        )

    @staticmethod
    def _selection_snapshot(
        definition: MissionDefinition,
        stored: Mapping,
        bouts: Mapping[int, BoutEvaluationSnapshot],
        names: Mapping[int, Mapping[str, str]],
    ) -> MetricSelectionSnapshot:
        if isinstance(definition, TargetFighterMissionDefinition):
            bout_id = int(stored["bout_id"])
            bout = bouts[bout_id]
            fighter_id = stored.get("selected_fighter_id")
            if fighter_id not in bout.fighter_ids:
                fighter_id = names[bout_id].get(
                    _normalize_name(stored.get("selected_fighter_name"))
                )
            if fighter_id not in bout.fighter_ids:
                raise BoutEvaluationError(
                    BoutEvaluationErrorCode.INVALID_CARD_DATA,
                    "Frozen selected fighter cannot be mapped to CardData V1",
                )
            return MetricSelectionSnapshot(
                legs=(
                    FighterMetricLeg(
                        key="target",
                        bout_id=bout_id,
                        fighter_id=str(fighter_id),
                        method=stored.get("method"),
                        round=stored.get("round"),
                    ),
                )
            )
        if isinstance(definition, TargetFightMissionDefinition):
            return MetricSelectionSnapshot(
                legs=(
                    FightMetricLeg(
                        key="target",
                        bout_id=int(stored["bout_id"]),
                        outcome=definition.selection.outcome,
                        round=definition.selection.required_round,
                    ),
                )
            )
        if isinstance(definition, ComboBuilderMissionDefinition):
            stored_legs = {leg["key"]: leg for leg in stored.get("legs") or ()}
            legs = []
            for spec in definition.selection.legs:
                selected = stored_legs[spec.key]
                bout_id = int(selected["bout_id"])
                if spec.target == ComboLegTarget.FIGHTER:
                    bout = bouts[bout_id]
                    fighter_id = selected.get("fighter_id")
                    if fighter_id not in bout.fighter_ids:
                        fighter_id = names[bout_id].get(
                            _normalize_name(selected.get("fighter_name"))
                        )
                    if fighter_id not in bout.fighter_ids:
                        raise BoutEvaluationError(
                            BoutEvaluationErrorCode.INVALID_CARD_DATA,
                            f"Frozen combo fighter {spec.key} cannot be mapped",
                        )
                    legs.append(
                        FighterMetricLeg(
                            key=spec.key,
                            bout_id=bout_id,
                            fighter_id=str(fighter_id),
                            method=selected.get("method"),
                            round=selected.get("round"),
                        )
                    )
                else:
                    legs.append(
                        FightMetricLeg(
                            key=spec.key,
                            bout_id=bout_id,
                            outcome=spec.fight_outcome,
                        )
                    )
            return MetricSelectionSnapshot(legs=tuple(legs))
        if isinstance(definition, CardPropMissionDefinition):
            return MetricSelectionSnapshot(
                card_prop_choice=stored.get("choice"),
                card_prop_exact_count=stored.get("exact_count"),
                card_prop_frozen_target=stored.get("frozen_target"),
            )
        return MetricSelectionSnapshot()


class BoutResultMissionEvaluator:
    def __init__(self, db: AsyncDatabase, *, clock: Clock = _utc_now) -> None:
        self.db = db
        self.clock = clock
        self.context_builder = MissionEvaluationContextBuilder(db)
        self.xp = XpLedgerService(db, clock=clock)
        self.celebrations = CelebrationQueueService(db, clock=clock)

    async def evaluate(self, command: EvaluateBoutResultCommand) -> BoutEvaluationResult:
        await self._validate_trigger(command)
        trigger = AssignmentEvaluationTrigger(
            event_id=command.event_id,
            trigger_type="BOUT_RESULT",
            trigger_id=command.bout_id,
            trigger_revision=command.result_revision,
            bout_id=command.bout_id,
            result_revision=command.result_revision,
        )
        cursor = self.db["mission_assignments"].find(
            {
                "event_id": command.event_id,
                "status": {"$ne": MissionAssignmentStatus.VOID.value},
            }
        )
        assignment_ids = [value["_id"] for value in await cursor.to_list(length=None)]
        results = []
        failures = []
        for assignment_id in assignment_ids:
            try:
                results.append(
                    await self.evaluate_assignment(
                        assignment_id,
                        trigger=trigger,
                    )
                )
            except (BoutEvaluationError, KeyError, ValueError) as exc:
                error_code = getattr(exc, "code", type(exc).__name__)
                failures.append(
                    AssignmentEvaluationFailure(
                        assignment_id=assignment_id,
                        error_code=str(getattr(error_code, "value", error_code)),
                        message=str(exc),
                    )
                )
        return BoutEvaluationResult(
            event_id=command.event_id,
            bout_id=command.bout_id,
            result_revision=command.result_revision,
            assignments=tuple(results),
            failures=tuple(failures),
        )

    async def _validate_trigger(self, command: EvaluateBoutResultCommand) -> None:
        event = await self.db["events"].find_one({"id": command.event_id})
        if not event:
            raise BoutEvaluationError(
                BoutEvaluationErrorCode.EVENT_NOT_FOUND,
                f"Event {command.event_id} was not found",
            )
        bout = await self.db["bouts"].find_one(
            {"event_id": command.event_id, "id": command.bout_id}
        )
        if not bout:
            raise BoutEvaluationError(
                BoutEvaluationErrorCode.BOUT_NOT_FOUND,
                f"Bout {command.bout_id} was not found on event {command.event_id}",
            )
        sidecar = bout.get("card_data_v1") or {}
        result = sidecar.get("result")
        if not isinstance(result, Mapping):
            raise BoutEvaluationError(
                BoutEvaluationErrorCode.RESULT_NOT_FINAL,
                f"Bout {command.bout_id} has no canonical final result",
            )
        if result.get("revision") != command.result_revision:
            raise BoutEvaluationError(
                BoutEvaluationErrorCode.RESULT_REVISION_MISMATCH,
                "Command revision does not match the canonical bout result",
            )

    async def evaluate_assignment(
        self,
        assignment_id: str,
        *,
        trigger: AssignmentEvaluationTrigger,
        leaderboard: LeaderboardEvaluationSnapshot | None = None,
    ) -> AssignmentEvaluationResult:
        async def callback(session: AsyncClientSession) -> AssignmentEvaluationResult:
            assignment = await self.db["mission_assignments"].find_one(
                {"_id": assignment_id}, session=session
            )
            if not assignment:
                raise BoutEvaluationError(
                    BoutEvaluationErrorCode.STALE_ASSIGNMENT,
                    f"Assignment {assignment_id} disappeared during evaluation",
                )
            previous_status = MissionAssignmentStatus(assignment["status"])
            await self._validate_trigger_in_session(trigger, session)
            definition = validate_mission_definition(assignment["definition_snapshot"])
            evaluation_moment = (
                EvaluationMoment.CARD_FINALIZED
                if trigger.card_finalized
                else EvaluationMoment.AFTER_EACH_RESULT
            )
            if evaluation_moment not in definition.evaluation.evaluate_on:
                return AssignmentEvaluationResult(
                    assignment_id=assignment_id,
                    mission_id=assignment["mission_id"],
                    previous_status=previous_status,
                    status=previous_status,
                    assignment_revision=int(assignment["revision"]),
                    replayed=False,
                    xp_delta=0,
                )
            run_key = {
                "assignment_id": assignment_id,
                "trigger_type": trigger.trigger_type,
                "trigger_id": trigger.trigger_id,
                "trigger_revision": trigger.trigger_revision,
            }
            existing_run = await self.db["mission_evaluation_runs"].find_one(
                run_key, session=session
            )
            if existing_run:
                return AssignmentEvaluationResult(
                    assignment_id=assignment_id,
                    mission_id=assignment["mission_id"],
                    previous_status=MissionAssignmentStatus(
                        existing_run["previous_status"]
                    ),
                    status=MissionAssignmentStatus(existing_run["status"]),
                    assignment_revision=int(existing_run["assignment_revision"]),
                    replayed=True,
                    xp_delta=int(existing_run.get("xp_delta", 0)),
                )

            context = await self.context_builder.build(
                assignment,
                session=session,
                card_finalized=trigger.card_finalized,
                leaderboard=leaderboard,
            )
            resolution = self._resolve(definition, context)
            target_status = self._assignment_status(resolution.status)

            if previous_status == MissionAssignmentStatus.VOID:
                target_status = previous_status
            if target_status != previous_status:
                transition_reason = (
                    MissionTransitionReason.EVALUATION
                    if previous_status == MissionAssignmentStatus.ACTIVE
                    else MissionTransitionReason.RESULT_CORRECTION
                )
                ensure_assignment_transition(
                    previous_status, target_status, transition_reason
                )

            xp_delta = 0
            current_award_id = assignment.get("xp_award_entry_id")
            if (
                previous_status == MissionAssignmentStatus.COMPLETED
                and target_status != MissionAssignmentStatus.COMPLETED
                and current_award_id
            ):
                compensation_key = (
                    f"card-mission:{assignment_id}:compensate:"
                    f"{trigger.trigger_type}:{trigger.trigger_id}:"
                    f"{trigger.trigger_revision}"
                )
                compensation = await self.xp.compensate(
                    user_id=assignment["user_id"],
                    command=CompensateXpCommand(
                        idempotency_key=compensation_key,
                        original_entry_id=current_award_id,
                        reason="Canonical result correction reversed mission completion",
                    ),
                    session=session,
                )
                xp_delta = (
                    compensation.amount
                    if compensation.idempotency_key == compensation_key
                    else 0
                )
                await self.celebrations.cancel_for_xp_award(
                    user_id=assignment["user_id"],
                    xp_entry_id=current_award_id,
                    session=session,
                )
                current_award_id = None
            elif (
                previous_status != MissionAssignmentStatus.COMPLETED
                and target_status == MissionAssignmentStatus.COMPLETED
            ):
                award = await self.xp.award(
                    user_id=assignment["user_id"],
                    command=AwardXpCommand(
                        idempotency_key=(
                            f"card-mission:{assignment_id}:award:"
                            f"{trigger.trigger_type}:{trigger.trigger_id}:"
                            f"{trigger.trigger_revision}"
                        ),
                        source_type=XpSourceType.CARD_MISSION,
                        source_id=assignment_id,
                        amount=int(assignment["xp"]),
                        reason=f"Completed card mission {assignment['mission_id']}",
                        metadata={
                            "event_id": trigger.event_id,
                            "trigger_type": trigger.trigger_type,
                            "trigger_id": trigger.trigger_id,
                            "trigger_revision": trigger.trigger_revision,
                        },
                    ),
                    session=session,
                )
                current_award_id = award.id
                xp_delta = award.amount
                await self.celebrations.enqueue(
                    user_id=assignment["user_id"],
                    command=EnqueueCelebrationCommand(
                        idempotency_key=(
                            f"card-mission:{assignment_id}:celebrate:"
                            f"{trigger.trigger_type}:{trigger.trigger_id}:"
                            f"{trigger.trigger_revision}"
                        ),
                        xp_entry_id=award.id,
                        kind=CelebrationKind.MISSION_COMPLETED,
                        presentation=CelebrationPresentation.TOAST,
                        heading="Mission complete",
                        message=f"You earned {assignment['xp']} XP.",
                        metadata={
                            "mission_id": assignment["mission_id"],
                            "event_id": trigger.event_id,
                            "name": definition.ui.name,
                            "xp": int(assignment["xp"]),
                        },
                    ),
                    session=session,
                )

            now = self.clock()
            next_revision = int(assignment["revision"]) + 1
            progress = resolution.model_dump(mode="json")
            update_result = await self.db["mission_assignments"].update_one(
                {"_id": assignment_id, "revision": assignment["revision"]},
                {
                    "$set": {
                        "status": target_status.value,
                        "progress": progress,
                        "xp_award_entry_id": current_award_id,
                        "last_evaluation_trigger_type": trigger.trigger_type,
                        "last_evaluation_trigger_id": trigger.trigger_id,
                        "last_evaluation_trigger_revision": trigger.trigger_revision,
                        "last_evaluated_bout_id": trigger.bout_id,
                        "last_result_revision": trigger.result_revision,
                        "updated_at": now,
                    },
                    "$inc": {"revision": 1},
                },
                session=session,
            )
            if update_result.modified_count != 1:
                raise BoutEvaluationError(
                    BoutEvaluationErrorCode.STALE_ASSIGNMENT,
                    f"Assignment {assignment_id} revision changed concurrently",
                )
            stored_result = AssignmentEvaluationResult(
                assignment_id=assignment_id,
                mission_id=assignment["mission_id"],
                previous_status=previous_status,
                status=target_status,
                assignment_revision=next_revision,
                replayed=False,
                xp_delta=xp_delta,
            )
            await self.db["mission_evaluation_runs"].insert_one(
                {
                    "_id": self._run_id(assignment_id, trigger),
                    **run_key,
                    "event_id": trigger.event_id,
                    "bout_id": trigger.bout_id,
                    "result_revision": trigger.result_revision,
                    "mission_id": assignment["mission_id"],
                    "previous_status": previous_status.value,
                    "status": target_status.value,
                    "assignment_revision": next_revision,
                    "xp_delta": xp_delta,
                    "created_at": now,
                },
                session=session,
            )
            return stored_result

        try:
            async with self.db.client.start_session() as session:
                return await session.with_transaction(callback)
        except DuplicateKeyError:
            existing = await self.db["mission_evaluation_runs"].find_one(
                {
                    "assignment_id": assignment_id,
                    "trigger_type": trigger.trigger_type,
                    "trigger_id": trigger.trigger_id,
                    "trigger_revision": trigger.trigger_revision,
                }
            )
            if existing:
                return AssignmentEvaluationResult(
                    assignment_id=assignment_id,
                    mission_id=existing["mission_id"],
                    previous_status=MissionAssignmentStatus(existing["previous_status"]),
                    status=MissionAssignmentStatus(existing["status"]),
                    assignment_revision=int(existing["assignment_revision"]),
                    replayed=True,
                    xp_delta=int(existing.get("xp_delta", 0)),
                )
            raise

    async def _validate_trigger_in_session(
        self,
        trigger: AssignmentEvaluationTrigger,
        session: AsyncClientSession,
    ) -> None:
        if trigger.trigger_type != "BOUT_RESULT":
            return
        bout = await self.db["bouts"].find_one(
            {"event_id": trigger.event_id, "id": trigger.trigger_id},
            session=session,
        )
        if not bout:
            raise BoutEvaluationError(
                BoutEvaluationErrorCode.BOUT_NOT_FOUND,
                f"Bout {trigger.trigger_id} was not found during evaluation",
            )
        result = (bout.get("card_data_v1") or {}).get("result")
        if not isinstance(result, Mapping):
            raise BoutEvaluationError(
                BoutEvaluationErrorCode.RESULT_NOT_FINAL,
                f"Bout {trigger.trigger_id} has no canonical final result",
            )
        if result.get("revision") != trigger.trigger_revision:
            raise BoutEvaluationError(
                BoutEvaluationErrorCode.RESULT_REVISION_MISMATCH,
                "Canonical result changed before assignment evaluation",
            )

    @staticmethod
    def _resolve(
        definition: MissionDefinition,
        context: MissionEvaluationContext,
    ) -> MetricResolution:
        if definition.evaluation.custom_evaluator_key:
            observation = EXCEPTIONAL_EVALUATOR_REGISTRY.evaluate(definition, context)
        else:
            observation = CARD_METRIC_REGISTRY.evaluate(
                definition.evaluation.metric,
                context,
                parameters=definition.evaluation.parameters,
            )
        return resolve_metric_observation(
            spec=definition.evaluation,
            observation=observation,
            progress_template=definition.ui.progress_template,
        )

    @staticmethod
    def _assignment_status(status: MetricResolutionStatus) -> MissionAssignmentStatus:
        if status == MetricResolutionStatus.PENDING:
            return MissionAssignmentStatus.ACTIVE
        return MissionAssignmentStatus(status.value)

    @staticmethod
    def _run_id(assignment_id: str, trigger: AssignmentEvaluationTrigger) -> str:
        raw = (
            f"{assignment_id}\x1f{trigger.trigger_type}\x1f{trigger.trigger_id}"
            f"\x1f{trigger.trigger_revision}"
        )
        return f"eval_{hashlib.sha256(raw.encode()).hexdigest()[:24]}"


__all__ = [
    "AssignmentEvaluationTrigger",
    "AssignmentEvaluationFailure",
    "AssignmentEvaluationResult",
    "BoutEvaluationError",
    "BoutEvaluationErrorCode",
    "BoutEvaluationResult",
    "BoutResultMissionEvaluator",
    "EvaluateBoutResultCommand",
    "MissionEvaluationContextBuilder",
]
