"""Rebuildable user progression projection over the XP ledger."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from pymongo import ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase

from app.modules.missions.application.celebration_queue import (
    CelebrationQueueError,
    CelebrationQueueService,
)
from app.modules.missions.domain.celebrations import (
    CelebrationKind,
    CelebrationPresentation,
    EnqueueCelebrationCommand,
)
from app.modules.missions.domain.progression import (
    ProgressionProjection,
    project_progression,
    title_for_level,
)

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ProgressionService:
    def __init__(self, db: AsyncDatabase, *, clock: Clock = _utc_now) -> None:
        self.db = db
        self.clock = clock

    async def sync(self, user_id: str) -> ProgressionProjection:
        """Refresh the cache and raise a celebration for any level crossed.

        Level-ups are not events anyone emits — they are a consequence of the
        ledger total moving. This compares the cached level with the recomputed
        one, so a single card that awards enough XP to cross two levels still
        announces the level the user actually landed on, exactly once.
        """
        previous = await self.db["mission_user_progression"].find_one(
            {"user_id": user_id}, {"level": 1, "title": 1}
        )
        projection = await self.rebuild_cache(user_id)

        # Existing users start at level 1 (D-PROD-007), so a user with no cache
        # yet is compared against 1 rather than skipped — otherwise the very
        # first level-up would be the one nobody ever gets told about.
        previous_level = int((previous or {}).get("level", 1))
        # Same reasoning for the title: a user with no cache was at level 1, and
        # level 1 already has a title. Leaving this None made every first
        # level-up claim a new title had been unlocked — the celebration read
        # "NEW TITLE UNLOCKED / BUM" while the title had not moved at all.
        previous_title = (previous or {}).get("title") or title_for_level(
            previous_level
        )[1].value
        if projection.level <= previous_level:
            return projection

        await self._celebrate(
            user_id,
            key=f"level-up:{user_id}:{projection.level}",
            kind=CelebrationKind.LEVEL_UP,
            heading=f"Level {projection.level}",
            message=f"{projection.title.value} · {projection.lifetime_xp} XP",
            metadata={
                "level": projection.level,
                "title": projection.title.value,
                "title_changed": projection.title.value != previous_title,
            },
        )
        if projection.title.value != previous_title:
            await self._celebrate(
                user_id,
                key=f"title-unlocked:{user_id}:{projection.title.value}",
                kind=CelebrationKind.TITLE_UNLOCKED,
                heading=projection.title.value,
                message=f"New title unlocked at level {projection.level}",
                metadata={
                    "level": projection.level,
                    "title": projection.title.value,
                    "title_changed": True,
                },
            )
        return projection

    async def _celebrate(
        self,
        user_id: str,
        *,
        key: str,
        kind: CelebrationKind,
        heading: str,
        message: str,
        metadata: dict,
    ) -> None:
        """Attach the celebration to the award that caused it, if there is one.

        Tying it to the most recent ledger entry is what lets a compensation
        cancel the celebration too, so a corrected result does not leave a
        congratulation on screen for XP the user no longer has.
        """
        latest = (
            await self.db["mission_xp_ledger"]
            .find({"user_id": user_id, "entry_type": "AWARD"})
            .sort([("created_at", -1)])
            .to_list(length=1)
        )
        if not latest:
            return
        try:
            await CelebrationQueueService(self.db, clock=self.clock).enqueue(
                user_id=user_id,
                command=EnqueueCelebrationCommand(
                    idempotency_key=key,
                    xp_entry_id=latest[0]["_id"],
                    kind=kind,
                    presentation=CelebrationPresentation.FULL_SCREEN,
                    heading=heading,
                    message=message,
                    metadata=metadata,
                ),
            )
        except CelebrationQueueError:
            # A replay of the same milestone is not an error worth propagating.
            return

    async def compute(self, user_id: str) -> tuple[ProgressionProjection, int]:
        cursor = await self.db["mission_xp_ledger"].aggregate(
            [
                {"$match": {"user_id": user_id}},
                {
                    "$group": {
                        "_id": None,
                        "lifetime_xp": {"$sum": "$amount"},
                        "ledger_entry_count": {"$sum": 1},
                    }
                },
            ]
        )
        values = await cursor.to_list(length=1)
        lifetime_xp = int(values[0]["lifetime_xp"]) if values else 0
        entry_count = int(values[0]["ledger_entry_count"]) if values else 0
        return project_progression(lifetime_xp), entry_count

    async def rebuild_cache(self, user_id: str) -> ProgressionProjection:
        projection, entry_count = await self.compute(user_id)
        now = self.clock()
        document = await self.db["mission_user_progression"].find_one_and_update(
            {"user_id": user_id},
            {
                "$set": {
                    "user_id": user_id,
                    **projection.model_dump(mode="json"),
                    "ledger_entry_count": entry_count,
                    "computed_at": now,
                },
                "$setOnInsert": {"created_at": now},
                "$inc": {"revision": 1},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return ProgressionProjection.model_validate(
            {
                field: document[field]
                for field in ProgressionProjection.model_fields
            }
        )
