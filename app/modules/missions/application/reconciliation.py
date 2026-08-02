"""Preview and apply reconciliation of the mission projections.

Reconciliation is deliberately narrow. It only repairs state that has a
*derivable* truth: the progression cache is a fold of the append-only XP ledger,
and the streak counters are a fold of the append-only settled-card rows. When
one of those caches drifts — a crashed write, a partial settlement — the correct
value can be recomputed with no judgement involved.

It does not touch assignments. A mission's outcome is decided by the evaluator
against a frozen card, so "what this assignment should say" is not derivable
here; inventing it would be a second, unaudited evaluation.

Preview never writes. Apply writes under compare-and-set on the same revision
the preview observed, so a plan built against stale state fails instead of
overwriting someone else's change.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from pymongo.asynchronous.database import AsyncDatabase

from app.modules.missions.application.progression import ProgressionService
from app.modules.missions.domain.enums import StringEnum
from app.modules.missions.domain.reconciliation import (
    MissionReconciliationPreview,
    ReconciliationAction,
    ReconciliationCandidate,
    ReconciliationEntityType,
    ReconciliationImpact,
    ReconciliationScope,
    build_reconciliation_preview,
)
from app.modules.missions.domain.streak import CardStreakOutcome

Clock = Callable[[], datetime]

_PROGRESSION_FIELDS = ("lifetime_xp", "level", "title")
_STREAK_FIELDS = ("current", "best")


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ReconciliationErrorCode(StringEnum):
    PLAN_STALE = "PLAN_STALE"
    PLAN_BLOCKED = "PLAN_BLOCKED"
    REASON_REQUIRED = "REASON_REQUIRED"


class ReconciliationError(ValueError):
    def __init__(self, code: ReconciliationErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ReconciliationOutcome:
    plan_id: str
    applied: int
    skipped: int
    converged: bool


class MissionReconciliationService:
    def __init__(self, db: AsyncDatabase, *, clock: Clock = _utc_now) -> None:
        self.db = db
        self.clock = clock
        self.progression = ProgressionService(db, clock=clock)

    # ---------------------------------------------------------------- preview

    async def preview(self, scope: ReconciliationScope) -> MissionReconciliationPreview:
        """Build the plan without writing anything."""
        candidates: list[ReconciliationCandidate] = []
        for user_id in await self._users_in_scope(scope):
            candidates.extend(await self._progression_candidate(user_id))
            candidates.extend(await self._streak_candidate(user_id))
        return build_reconciliation_preview(scope=scope, candidates=candidates)

    async def _users_in_scope(self, scope: ReconciliationScope) -> list[str]:
        if scope.user_id:
            return [scope.user_id]
        if scope.assignment_id:
            assignment = await self.db["mission_assignments"].find_one(
                {"_id": scope.assignment_id}, {"user_id": 1}
            )
            return [assignment["user_id"]] if assignment else []
        # An event scope means everyone that card could have touched.
        users = set(
            await self.db["mission_assignments"].distinct(
                "user_id", {"event_id": scope.event_id}
            )
        ) | set(
            await self.db["mission_card_streak_cards"].distinct(
                "user_id", {"event_id": scope.event_id}
            )
        )
        return sorted(users)

    async def _progression_candidate(
        self, user_id: str
    ) -> list[ReconciliationCandidate]:
        projection, _ = await self.progression.compute(user_id)
        desired = {
            "lifetime_xp": projection.lifetime_xp,
            "level": projection.level,
            "title": projection.title.value,
        }
        cached = await self.db["mission_user_progression"].find_one({"user_id": user_id})
        current = (
            {field: cached.get(field) for field in _PROGRESSION_FIELDS}
            if cached
            else None
        )
        return [
            ReconciliationCandidate(
                entity_type=ReconciliationEntityType.USER_PROGRESSION,
                entity_id=user_id,
                impact=ReconciliationImpact.PROGRESSION_REBUILD,
                owned_fields=_PROGRESSION_FIELDS,
                desired=desired,
                current=current,
                expected_revision=int(cached["revision"]) if cached else None,
                current_revision=int(cached["revision"]) if cached else None,
            )
        ]

    async def _streak_candidate(self, user_id: str) -> list[ReconciliationCandidate]:
        """Fold the settled cards back into the counters, in settlement order."""
        rows = (
            await self.db["mission_card_streak_cards"]
            .find({"user_id": user_id})
            .sort([("settled_at", 1)])
            .to_list(length=None)
        )
        current = 0
        best = 0
        for row in rows:
            if row.get("outcome") == CardStreakOutcome.ADVANCED.value:
                current += 1
                best = max(best, current)
            else:
                best = max(best, current)
                current = 0

        cached = await self.db["mission_card_streaks"].find_one({"user_id": user_id})
        if cached is None and not rows:
            return []
        return [
            ReconciliationCandidate(
                entity_type=ReconciliationEntityType.CARD_STREAK,
                entity_id=user_id,
                impact=ReconciliationImpact.STREAK_STATE,
                owned_fields=_STREAK_FIELDS,
                desired={"current": current, "best": best},
                current=(
                    {field: int(cached.get(field, 0)) for field in _STREAK_FIELDS}
                    if cached
                    else None
                ),
                expected_revision=int(cached["revision"]) if cached else None,
                current_revision=int(cached["revision"]) if cached else None,
            )
        ]

    # ------------------------------------------------------------------ apply

    async def apply(
        self,
        scope: ReconciliationScope,
        *,
        plan_id: str,
        actor_id: str,
        reason: str,
    ) -> ReconciliationOutcome:
        """Re-preview, verify the plan still describes reality, then write."""
        if not (reason or "").strip():
            raise ReconciliationError(
                ReconciliationErrorCode.REASON_REQUIRED,
                "A reconciliation must record why it was applied",
            )
        preview = await self.preview(scope)
        if preview.plan_id != plan_id:
            # The state moved between preview and apply. Refusing is the whole
            # point: the operator approved a different set of changes.
            raise ReconciliationError(
                ReconciliationErrorCode.PLAN_STALE,
                "The state changed since this plan was previewed; preview again",
            )
        if not preview.safe_to_apply:
            raise ReconciliationError(
                ReconciliationErrorCode.PLAN_BLOCKED,
                "This plan has blocking findings and cannot be applied",
            )

        applied = 0
        skipped = 0
        for operation in preview.operations:
            if await self._write(operation):
                applied += 1
            else:
                skipped += 1

        await self.db["mission_admin_audit"].insert_one(
            {
                "actor_id": actor_id,
                "action": "reconciliation.apply",
                "plan_id": plan_id,
                "reason": reason,
                "scope": {
                    "event_id": scope.event_id,
                    "user_id": scope.user_id,
                    "assignment_id": scope.assignment_id,
                },
                "operations": [
                    {
                        "operation_id": operation.operation_id,
                        "entity_type": operation.entity_type.value,
                        "entity_id": operation.entity_id,
                        "action": operation.action.value,
                        "changed_fields": list(operation.changed_fields),
                        "before": dict(operation.before or {}),
                        "after": dict(operation.after),
                    }
                    for operation in preview.operations
                ],
                "applied": applied,
                "skipped": skipped,
                "created_at": self.clock(),
            }
        )
        return ReconciliationOutcome(
            plan_id=plan_id,
            applied=applied,
            skipped=skipped,
            converged=applied > 0 and skipped == 0,
        )

    async def _write(self, operation) -> bool:
        """Compare-and-set on the revision the preview observed."""
        collection, key = (
            ("mission_user_progression", "user_id")
            if operation.entity_type == ReconciliationEntityType.USER_PROGRESSION
            else ("mission_card_streaks", "user_id")
        )
        now = self.clock()
        payload = {
            **{field: operation.after[field] for field in operation.changed_fields},
            "reconciled_at": now,
            "updated_at": now,
        }

        if operation.action == ReconciliationAction.INSERT:
            result = await self.db[collection].update_one(
                {key: operation.entity_id, "revision": {"$exists": False}},
                {
                    "$set": {key: operation.entity_id, **payload},
                    "$setOnInsert": {"created_at": now},
                    "$inc": {"revision": 1},
                },
                upsert=True,
            )
            return bool(result.upserted_id or result.modified_count)

        result = await self.db[collection].update_one(
            {key: operation.entity_id, "revision": operation.expected_revision},
            {"$set": payload, "$inc": {"revision": 1}},
        )
        return bool(result.modified_count)


__all__ = [
    "MissionReconciliationService",
    "ReconciliationError",
    "ReconciliationErrorCode",
    "ReconciliationOutcome",
]
