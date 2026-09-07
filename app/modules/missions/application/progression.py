"""Proyección de progresión del usuario, reconstruible desde el ledger de XP."""

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
        """Refresca el cache y dispara una celebración si se cruzó de nivel.

        El level-up no lo emite nadie: se compara el nivel cacheado contra el
        recalculado, así una card que cruza dos niveles avisa una sola vez.
        """
        previous = await self.db["mission_user_progression"].find_one(
            {"user_id": user_id}, {"level": 1, "title": 1}
        )
        projection = await self.rebuild_cache(user_id)

        # Sin cache previo se compara contra nivel 1 (arranque real, D-PROD-007),
        # si no el primer level-up real nunca se anunciaría.
        previous_level = int((previous or {}).get("level", 1))
        # Mismo motivo para el título: dejarlo en None hacía que todo primer
        # level-up pareciera título nuevo aunque no hubiera cambiado.
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
        """Ata la celebración al award que la causó, si existe uno.

        Ligarla a la entrada más reciente permite que una compensación
        también la cancele, sin dejar una felicitación por XP que ya no existe.
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
            # Repetir el mismo hito no es un error que valga la pena propagar.
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
