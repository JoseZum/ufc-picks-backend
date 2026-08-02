"""Retry-safe terminal evaluation when a canonical UFC card is finalized."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass

from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from app.modules.missions.application.bout_evaluation import (
    AssignmentEvaluationFailure,
    AssignmentEvaluationResult,
    AssignmentEvaluationTrigger,
    BoutEvaluationError,
    BoutResultMissionEvaluator,
    MissionEvaluationContextBuilder,
)
from app.modules.missions.domain.enums import MissionAssignmentStatus, StringEnum
from app.modules.missions.domain.evaluation import LeaderboardEvaluationSnapshot


class CardFinalizationErrorCode(StringEnum):
    EVENT_NOT_FOUND = "EVENT_NOT_FOUND"
    UNRESOLVED_BOUTS = "UNRESOLVED_BOUTS"
    INVALID_LEADERBOARD_PICK = "INVALID_LEADERBOARD_PICK"
    FINALIZATION_REVISION_CONFLICT = "FINALIZATION_REVISION_CONFLICT"


class CardFinalizationError(ValueError):
    def __init__(self, code: CardFinalizationErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class FinalizeCardMissionsCommand:
    event_id: int
    finalization_revision: int

    def __post_init__(self) -> None:
        if self.event_id < 1 or self.finalization_revision < 1:
            raise ValueError("event and finalization revision must be positive")


@dataclass(frozen=True)
class CardFinalizationResult:
    event_id: int
    finalization_revision: int
    active_user_count: int
    assignments: tuple[AssignmentEvaluationResult, ...]
    failures: tuple[AssignmentEvaluationFailure, ...]

    @property
    def replayed_count(self) -> int:
        return sum(item.replayed for item in self.assignments)


class CardMissionFinalizer:
    def __init__(self, db: AsyncDatabase) -> None:
        self.db = db
        self.assignment_evaluator = BoutResultMissionEvaluator(db)

    async def finalize(
        self,
        command: FinalizeCardMissionsCommand,
    ) -> CardFinalizationResult:
        calculated, fingerprint = await self._final_leaderboard(command.event_id)
        leaderboard = await self._freeze_finalization(
            command,
            leaderboard=calculated,
            input_fingerprint=fingerprint,
        )
        trigger = AssignmentEvaluationTrigger(
            event_id=command.event_id,
            trigger_type="CARD_FINALIZED",
            trigger_id=command.event_id,
            trigger_revision=command.finalization_revision,
            card_finalized=True,
        )
        cursor = self.db["mission_assignments"].find(
            {
                "event_id": command.event_id,
                "status": {"$ne": MissionAssignmentStatus.VOID.value},
            }
        )
        assignment_ids = [item["_id"] for item in await cursor.to_list(length=None)]
        results = []
        failures = []
        for assignment_id in assignment_ids:
            assignment = await self.db["mission_assignments"].find_one(
                {"_id": assignment_id}, {"user_id": 1}
            )
            if not assignment:
                continue
            try:
                results.append(
                    await self.assignment_evaluator.evaluate_assignment(
                        assignment_id,
                        trigger=trigger,
                        leaderboard=leaderboard.get(assignment["user_id"]),
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
        return CardFinalizationResult(
            event_id=command.event_id,
            finalization_revision=command.finalization_revision,
            active_user_count=len(leaderboard),
            assignments=tuple(results),
            failures=tuple(failures),
        )

    async def _final_leaderboard(
        self,
        event_id: int,
    ) -> tuple[dict[str, LeaderboardEvaluationSnapshot], str]:
        event = await self.db["events"].find_one({"id": event_id})
        if not event:
            raise CardFinalizationError(
                CardFinalizationErrorCode.EVENT_NOT_FOUND,
                f"Event {event_id} was not found",
            )
        bouts = await self.db["bouts"].find({"event_id": event_id}).to_list(
            length=None
        )
        slots = await self.db["event_card_slots"].find(
            {"event_id": event_id}
        ).to_list(length=None)
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
        unresolved = []
        for bout in bouts:
            bout_id = bout.get("id")
            slot = slots_by_bout.get(bout_id)
            snapshot = MissionEvaluationContextBuilder._bout_snapshot(bout, slot)
            snapshots[snapshot.bout_id] = snapshot
            if snapshot.is_current and snapshot.lifecycle.value not in {
                "CANCELLED",
                "POSTPONED",
                "REPLACED",
            } and snapshot.result is None:
                unresolved.append(snapshot.bout_id)
        if unresolved:
            raise CardFinalizationError(
                CardFinalizationErrorCode.UNRESOLVED_BOUTS,
                "Card cannot finalize while canonical bouts are unresolved: "
                + ", ".join(map(str, sorted(unresolved))),
            )

        canonical_names = {
            int(bout["id"]): self._fighter_name_map(bout)
            for bout in bouts
        }
        picks = await self.db["picks"].find({"event_id": event_id}).to_list(
            length=None
        )
        points_by_user: defaultdict[str, int] = defaultdict(int)
        pick_count_by_user: defaultdict[str, int] = defaultdict(int)
        for pick in picks:
            user_id = pick.get("user_id")
            if not isinstance(user_id, str) or pick.get("bout_id") not in snapshots:
                continue
            try:
                snapshot = MissionEvaluationContextBuilder._pick_snapshot(
                    pick,
                    snapshots,
                    canonical_names,
                )
            except BoutEvaluationError as exc:
                raise CardFinalizationError(
                    CardFinalizationErrorCode.INVALID_LEADERBOARD_PICK,
                    f"Cannot rank canonical pick {pick.get('_id')}: {exc}",
                ) from exc
            points_by_user[user_id] += int(snapshot.points_awarded or 0)
            pick_count_by_user[user_id] += 1

        fingerprint = self._input_fingerprint(event, bouts, slots, picks)
        if not pick_count_by_user:
            return {}, fingerprint
        maximum = max(points_by_user.values(), default=0)
        leaders = sum(
            points == maximum
            for user_id, points in points_by_user.items()
            if pick_count_by_user[user_id]
        )
        active_count = len(pick_count_by_user)
        leaderboard = {
            user_id: LeaderboardEvaluationSnapshot(
                rank=1
                + sum(
                    other_points > points
                    for other_id, other_points in points_by_user.items()
                    if pick_count_by_user[other_id]
                ),
                tied_for_first=points == maximum and leaders > 1,
                active_user_count=active_count,
                finalized=True,
            )
            for user_id, points in points_by_user.items()
            if pick_count_by_user[user_id]
        }
        return leaderboard, fingerprint

    async def _freeze_finalization(
        self,
        command: FinalizeCardMissionsCommand,
        *,
        leaderboard: Mapping[str, LeaderboardEvaluationSnapshot],
        input_fingerprint: str,
    ) -> dict[str, LeaderboardEvaluationSnapshot]:
        document_id = f"card-finalization:{command.event_id}:{command.finalization_revision}"
        stored_leaderboard = {
            user_id: snapshot.model_dump(mode="json")
            for user_id, snapshot in leaderboard.items()
        }
        document = {
            "_id": document_id,
            "event_id": command.event_id,
            "finalization_revision": command.finalization_revision,
            "input_fingerprint": input_fingerprint,
            "leaderboard": stored_leaderboard,
        }
        existing = await self.db["mission_card_finalization_runs"].find_one(
            {"_id": document_id}
        )
        if not existing:
            try:
                await self.db["mission_card_finalization_runs"].insert_one(document)
                existing = document
            except DuplicateKeyError:
                existing = await self.db["mission_card_finalization_runs"].find_one(
                    {"_id": document_id}
                )
        if not existing or existing.get("input_fingerprint") != input_fingerprint:
            raise CardFinalizationError(
                CardFinalizationErrorCode.FINALIZATION_REVISION_CONFLICT,
                "Finalization revision was already used for different canonical inputs",
            )
        return {
            user_id: LeaderboardEvaluationSnapshot.model_validate(snapshot)
            for user_id, snapshot in (existing.get("leaderboard") or {}).items()
        }

    @staticmethod
    def _input_fingerprint(
        event: Mapping,
        bouts: list[Mapping],
        slots: list[Mapping],
        picks: list[Mapping],
    ) -> str:
        event_sidecar = event.get("card_data_v1") or {}
        payload = {
            "event": {
                "id": event.get("id"),
                "status": event.get("status"),
                "structure_revision": event_sidecar.get("structure_revision"),
            },
            "bouts": [
                {
                    "id": bout.get("id"),
                    "sidecar": bout.get("card_data_v1"),
                }
                for bout in sorted(bouts, key=lambda item: item.get("id", 0))
            ],
            "slots": [
                {
                    key: slot.get(key)
                    for key in (
                        "bout_id",
                        "is_current",
                        "card_section",
                        "order_overall",
                        "order_section",
                        "role",
                        "structure_revision",
                    )
                }
                for slot in sorted(
                    slots,
                    key=lambda item: (item.get("bout_id", 0), str(item.get("_id"))),
                )
            ],
            "picks": [
                {
                    key: pick.get(key)
                    for key in (
                        "_id",
                        "user_id",
                        "bout_id",
                        "picked_fighter_id",
                        "picked_fighter_name",
                        "picked_method",
                        "picked_round",
                        "revision",
                    )
                }
                for pick in sorted(picks, key=lambda item: str(item.get("_id")))
            ],
        }
        serialized = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return f"sha256:{hashlib.sha256(serialized.encode()).hexdigest()}"

    @staticmethod
    def _fighter_name_map(bout: Mapping) -> dict[str, str]:
        sidecar = bout.get("card_data_v1") or {}
        by_name = {}
        for fighter in sidecar.get("fighters") or ():
            if not isinstance(fighter, Mapping):
                continue
            fighter_id = fighter.get("fighter_id")
            name = fighter.get("display_name")
            if isinstance(fighter_id, str) and fighter_id.strip():
                normalized = " ".join(str(name or "").lower().strip().split())
                by_name[normalized] = fighter_id
        return by_name


__all__ = [
    "CardFinalizationError",
    "CardFinalizationErrorCode",
    "CardFinalizationResult",
    "CardMissionFinalizer",
    "FinalizeCardMissionsCommand",
]
