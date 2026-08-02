"""Settles the single Card Streak once per card, per user.

Three collections carry the whole feature:

``mission_card_streak_denominators``
    One frozen row per event. The count of active bouts is captured the first
    time the card is settled and never recomputed, so a bout cancelled after
    picks closed cannot retroactively rewrite whether a user covered the card
    (D-DATA-003).

``mission_card_streak_cards``
    One row per (user, event). It is the idempotency token: a card can advance
    a streak at most once, however many times a writer replays the trigger.

``mission_card_streaks``
    The user's current and best streak. A projection of the rows above, but
    kept live because every Home and Profile read needs it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pymongo import ReturnDocument
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
from app.modules.missions.domain.streak import (
    CardStreakDecision,
    CardStreakOutcome,
    decide_card_streak,
)
from app.modules.missions.domain.xp import AwardXpCommand, XpSourceType

Clock = Callable[[], datetime]

_TERMINAL_LIFECYCLES = {"CANCELLED", "POSTPONED", "REPLACED"}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _card_id(user_id: str, event_id: int) -> str:
    return f"streak:{user_id}:{event_id}"


@dataclass(frozen=True)
class CardStreakState:
    user_id: str
    current: int = 0
    best: int = 0
    last_event_id: int | None = None
    last_outcome: str | None = None
    last_settled_at: datetime | None = None


@dataclass(frozen=True)
class CardStreakSettlement:
    event_id: int
    denominator: int
    advanced: int = 0
    broken: int = 0
    unchanged: int = 0
    skipped: int = 0
    xp_awarded: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)


class CardStreakService:
    """STREAK-001 end to end: freeze the denominator, settle, pay, celebrate."""

    def __init__(self, db: AsyncDatabase, *, clock: Clock = _utc_now) -> None:
        self.db = db
        self.clock = clock
        self.streaks = db["mission_card_streaks"]
        self.cards = db["mission_card_streak_cards"]
        self.denominators = db["mission_card_streak_denominators"]
        self.xp = XpLedgerService(db, clock=clock)
        self.celebrations = CelebrationQueueService(db, clock=clock)

    # ------------------------------------------------------------------ reads

    async def state_for(self, user_id: str) -> CardStreakState:
        document = await self.streaks.find_one({"user_id": user_id})
        if document is None:
            return CardStreakState(user_id=user_id)
        return CardStreakState(
            user_id=user_id,
            current=int(document.get("current", 0)),
            best=int(document.get("best", 0)),
            last_event_id=document.get("last_event_id"),
            last_outcome=document.get("last_outcome"),
            last_settled_at=document.get("last_settled_at"),
        )

    async def history_for(
        self, user_id: str, *, limit: int = 20
    ) -> list[dict]:
        """The user's most recent settled cards, newest first."""
        return (
            await self.cards.find({"user_id": user_id})
            .sort([("settled_at", -1)])
            .to_list(length=limit)
        )

    # ------------------------------------------------------- denominator

    async def capture_denominator(self, event_id: int) -> tuple[int, list[int]]:
        """Freeze the card's active bouts, or return the already-frozen set.

        Ideally this runs exactly at pick close. In practice the first observable
        moment after picks close is the first registered result, and that is what
        drives it today — a bout cancelled in that short window is therefore
        excluded. Once written the row is never rewritten, which is the property
        D-DATA-003 actually asks for.
        """
        frozen = await self.denominators.find_one({"_id": event_id})
        if frozen is not None:
            return int(frozen["denominator"]), list(frozen["bout_ids"])

        bout_ids = await self._active_bout_ids(event_id)
        now = self.clock()
        try:
            await self.denominators.insert_one(
                {
                    "_id": event_id,
                    "event_id": event_id,
                    "bout_ids": bout_ids,
                    "denominator": len(bout_ids),
                    "captured_at": now,
                }
            )
        except DuplicateKeyError:
            # Another writer froze it first; theirs is the authority.
            frozen = await self.denominators.find_one({"_id": event_id})
            return int(frozen["denominator"]), list(frozen["bout_ids"])
        return len(bout_ids), bout_ids

    async def _active_bout_ids(self, event_id: int) -> list[int]:
        bouts = await self.db["bouts"].find({"event_id": event_id}).to_list(length=None)
        active: list[int] = []
        for bout in bouts:
            canonical = bout.get("card_data_v1") or {}
            lifecycle = str(canonical.get("lifecycle") or "SCHEDULED").upper()
            is_current = canonical.get("is_current")
            if is_current is None:
                is_current = True
            if not is_current or lifecycle in _TERMINAL_LIFECYCLES:
                continue
            if str(bout.get("status") or "").lower() in {"cancelled", "canceled"}:
                continue
            active.append(int(bout["id"]))
        return sorted(active)

    # ----------------------------------------------------------------- settle

    async def settle_card(self, event_id: int) -> CardStreakSettlement:
        """Settle every user this card can touch. Safe to call repeatedly."""
        denominator, bout_ids = await self.capture_denominator(event_id)
        if denominator <= 0:
            return CardStreakSettlement(event_id=event_id, denominator=0)

        picks_by_user = await self._picks_by_user(event_id, bout_ids)
        # A user with a live streak must be settled even if they ignored the
        # card entirely — that is exactly how a streak breaks.
        candidates = set(picks_by_user) | {
            document["user_id"]
            async for document in self.streaks.find(
                {"current": {"$gt": 0}}, {"user_id": 1}
            )
        }

        settlement = CardStreakSettlement(event_id=event_id, denominator=denominator)
        counters = {"advanced": 0, "broken": 0, "unchanged": 0, "skipped": 0, "xp": 0}
        errors: list[str] = []
        for user_id in sorted(candidates):
            try:
                decision = await self.settle_user(
                    user_id=user_id,
                    event_id=event_id,
                    denominator=denominator,
                    picked=len(picks_by_user.get(user_id, ())),
                )
            except Exception as exc:  # noqa: BLE001 - one user must not stop the card
                errors.append(f"{user_id}: {exc}")
                continue
            if decision is None:
                counters["skipped"] += 1
                continue
            counters["xp"] += decision.total_xp
            if decision.outcome == CardStreakOutcome.ADVANCED:
                counters["advanced"] += 1
            elif decision.outcome == CardStreakOutcome.BROKEN:
                counters["broken"] += 1
            else:
                counters["unchanged"] += 1

        if not errors:
            # Only a clean sweep closes the card, so a run that failed for some
            # users is retried by the next trigger instead of being lost.
            await self.denominators.update_one(
                {"_id": event_id},
                {"$set": {"settled_at": self.clock()}},
            )

        return CardStreakSettlement(
            event_id=settlement.event_id,
            denominator=denominator,
            advanced=counters["advanced"],
            broken=counters["broken"],
            unchanged=counters["unchanged"],
            skipped=counters["skipped"],
            xp_awarded=counters["xp"],
            errors=tuple(errors),
        )

    async def is_settled(self, event_id: int) -> bool:
        """Whether this card already settled every user cleanly."""
        row = await self.denominators.find_one(
            {"_id": event_id}, {"settled_at": 1}
        )
        return bool(row and row.get("settled_at"))

    async def settle_user(
        self,
        *,
        user_id: str,
        event_id: int,
        denominator: int,
        picked: int,
    ) -> CardStreakDecision | None:
        """Settle one user on one card. Returns ``None`` if already settled."""
        if await self.cards.find_one({"_id": _card_id(user_id, event_id)}):
            return None

        state = await self.state_for(user_id)
        decision = decide_card_streak(
            current=state.current,
            best=state.best,
            picked=picked,
            denominator=denominator,
        )
        if decision.outcome == CardStreakOutcome.NOT_ELIGIBLE:
            return None

        async def callback(session: AsyncClientSession) -> CardStreakDecision:
            await self._record_card(
                session, user_id=user_id, event_id=event_id, decision=decision
            )
            await self._apply_state(
                session, user_id=user_id, event_id=event_id, decision=decision
            )
            await self._pay(session, user_id=user_id, event_id=event_id, decision=decision)
            return decision

        try:
            async with self.db.client.start_session() as session:
                return await session.with_transaction(callback)
        except DuplicateKeyError:
            # Another writer settled this card for this user first.
            return None

    # ---------------------------------------------------------------- writers

    async def _record_card(
        self,
        session: AsyncClientSession,
        *,
        user_id: str,
        event_id: int,
        decision: CardStreakDecision,
    ) -> None:
        await self.cards.insert_one(
            {
                "_id": _card_id(user_id, event_id),
                "user_id": user_id,
                "event_id": event_id,
                "outcome": decision.outcome.value,
                "denominator": decision.denominator,
                "picked": decision.picked,
                "coverage_percent": decision.coverage_percent,
                "current_before": decision.current_before,
                "current_after": decision.current_after,
                "best_after": decision.best_after,
                "milestone": decision.milestone,
                "xp_awarded": decision.total_xp,
                "settled_at": self.clock(),
            },
            session=session,
        )

    async def _apply_state(
        self,
        session: AsyncClientSession,
        *,
        user_id: str,
        event_id: int,
        decision: CardStreakDecision,
    ) -> None:
        now = self.clock()
        await self.streaks.find_one_and_update(
            {"user_id": user_id},
            {
                "$set": {
                    "user_id": user_id,
                    "current": decision.current_after,
                    "best": decision.best_after,
                    "last_event_id": event_id,
                    "last_outcome": decision.outcome.value,
                    "last_settled_at": now,
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now},
                "$inc": {"revision": 1},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
            session=session,
        )

    async def _pay(
        self,
        session: AsyncClientSession,
        *,
        user_id: str,
        event_id: int,
        decision: CardStreakDecision,
    ) -> None:
        if decision.outcome != CardStreakOutcome.ADVANCED:
            return

        await self.xp.award(
            user_id=user_id,
            command=AwardXpCommand(
                idempotency_key=f"card-streak:{user_id}:{event_id}",
                source_type=XpSourceType.CARD_STREAK,
                source_id=str(event_id),
                amount=decision.card_xp,
                reason=f"Card streak continued: {decision.current_after} cards",
                metadata={
                    "event_id": event_id,
                    "streak": decision.current_after,
                    "coverage_percent": decision.coverage_percent,
                },
            ),
            session=session,
        )
        if decision.milestone is None:
            return

        milestone_entry = await self.xp.award(
            user_id=user_id,
            command=AwardXpCommand(
                idempotency_key=f"card-streak-milestone:{user_id}:{event_id}",
                source_type=XpSourceType.STREAK_MILESTONE,
                source_id=f"{event_id}:{decision.milestone}",
                amount=decision.milestone_xp,
                reason=f"Card streak milestone: {decision.milestone} cards",
                metadata={"event_id": event_id, "milestone": decision.milestone},
            ),
            session=session,
        )
        await self.celebrations.enqueue(
            user_id=user_id,
            command=EnqueueCelebrationCommand(
                idempotency_key=f"streak-milestone:{user_id}:{event_id}",
                xp_entry_id=milestone_entry.id,
                kind=CelebrationKind.STREAK_MILESTONE,
                presentation=CelebrationPresentation.FULL_SCREEN,
                heading=f"{decision.milestone} card streak",
                message=f"Streak milestone reached · +{decision.milestone_xp} XP",
                metadata={
                    "event_id": event_id,
                    "streak": decision.milestone,
                    "bonus_xp": decision.milestone_xp,
                },
            ),
            session=session,
        )

    # ---------------------------------------------------------------- helpers

    async def _picks_by_user(
        self, event_id: int, bout_ids: list[int]
    ) -> dict[str, set[int]]:
        """Winner picks, counted only against the frozen denominator."""
        eligible = set(bout_ids)
        picks_by_user: dict[str, set[int]] = {}
        cursor = self.db["picks"].find(
            {"event_id": event_id, "bout_id": {"$in": bout_ids}},
            {"user_id": 1, "bout_id": 1, "picked_fighter_name": 1, "picked_fighter_id": 1},
        )
        async for pick in cursor:
            user_id = pick.get("user_id")
            bout_id = pick.get("bout_id")
            if not isinstance(user_id, str) or bout_id not in eligible:
                continue
            if not (pick.get("picked_fighter_name") or pick.get("picked_fighter_id")):
                continue
            picks_by_user.setdefault(user_id, set()).add(int(bout_id))
        return picks_by_user


__all__ = [
    "CardStreakService",
    "CardStreakSettlement",
    "CardStreakState",
]
