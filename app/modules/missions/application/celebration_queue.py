"""Durable pending-until-acknowledged celebration queue."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from pymongo import ReturnDocument
from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from app.modules.missions.domain.celebrations import (
    Celebration,
    EnqueueCelebrationCommand,
)
from app.modules.missions.domain.enums import (
    CelebrationStatus,
    MissionTransitionReason,
    StringEnum,
)
from app.modules.missions.domain.state_machines import ensure_celebration_transition
from app.modules.missions.domain.xp import XpEntryType, XpLedgerEntry

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class CelebrationQueueErrorCode(StringEnum):
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    XP_AWARD_NOT_FOUND = "XP_AWARD_NOT_FOUND"
    CELEBRATION_NOT_FOUND = "CELEBRATION_NOT_FOUND"


class CelebrationQueueError(ValueError):
    def __init__(self, code: CelebrationQueueErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class CelebrationAcknowledgement:
    celebration: Celebration
    newly_acknowledged: bool


def _payload_hash(user_id: str, command: EnqueueCelebrationCommand) -> str:
    payload = {"user_id": user_id, **command.model_dump(mode="json")}
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def _celebration_id(user_id: str, idempotency_key: str) -> str:
    value = f"{user_id}\x1f{idempotency_key}"
    return f"celebration_{hashlib.sha256(value.encode()).hexdigest()[:24]}"


class CelebrationQueueService:
    def __init__(self, db: AsyncDatabase, *, clock: Clock = _utc_now) -> None:
        self.db = db
        self.collection = db["mission_celebrations"]
        self.clock = clock

    async def enqueue(
        self,
        *,
        user_id: str,
        command: EnqueueCelebrationCommand,
        session: AsyncClientSession | None = None,
    ) -> Celebration:
        payload_hash = _payload_hash(user_id, command)
        existing = await self.collection.find_one(
            {"idempotency_key": command.idempotency_key},
            session=session,
        )
        if existing:
            return self._same_request_or_conflict(existing, payload_hash)

        xp_document = await self.db["mission_xp_ledger"].find_one(
            {
                "_id": command.xp_entry_id,
                "user_id": user_id,
                "entry_type": XpEntryType.AWARD.value,
            },
            session=session,
        )
        if not xp_document:
            raise CelebrationQueueError(
                CelebrationQueueErrorCode.XP_AWARD_NOT_FOUND,
                "A positive XP award was not found for this user",
            )
        XpLedgerEntry.model_validate(xp_document)

        celebration = Celebration(
            _id=_celebration_id(user_id, command.idempotency_key),
            user_id=user_id,
            idempotency_key=command.idempotency_key,
            xp_entry_id=command.xp_entry_id,
            kind=command.kind,
            presentation=command.presentation,
            status=CelebrationStatus.PENDING,
            heading=command.heading,
            message=command.message,
            metadata=command.metadata,
            payload_hash=payload_hash,
            created_at=self.clock(),
        )
        try:
            await self.collection.insert_one(
                celebration.model_dump(by_alias=True, mode="python"),
                session=session,
            )
            return celebration
        except DuplicateKeyError:
            concurrent = await self.collection.find_one(
                {"idempotency_key": command.idempotency_key},
                session=session,
            )
            if concurrent:
                return self._same_request_or_conflict(concurrent, payload_hash)
            raise

    async def pending_for_user(
        self,
        user_id: str,
        *,
        limit: int = 20,
    ) -> tuple[Celebration, ...]:
        if limit not in range(1, 101):
            raise ValueError("limit must be between 1 and 100")
        cursor = (
            self.collection.find(
                {
                    "user_id": user_id,
                    "status": CelebrationStatus.PENDING.value,
                    "acknowledged_at": None,
                }
            )
            .sort([("created_at", 1), ("_id", 1)])
            .limit(limit)
        )
        values = await cursor.to_list(length=limit)
        return tuple(Celebration.model_validate(value) for value in values)

    async def cancel_for_xp_award(
        self,
        *,
        user_id: str,
        xp_entry_id: str,
        session: AsyncClientSession | None = None,
    ) -> int:
        ensure_celebration_transition(
            CelebrationStatus.PENDING,
            CelebrationStatus.CANCELLED,
            MissionTransitionReason.RESULT_CORRECTION,
        )
        result = await self.collection.update_many(
            {
                "user_id": user_id,
                "xp_entry_id": xp_entry_id,
                "status": CelebrationStatus.PENDING.value,
            },
            {
                "$set": {
                    "status": CelebrationStatus.CANCELLED.value,
                    "cancelled_at": self.clock(),
                }
            },
            session=session,
        )
        return result.modified_count

    async def acknowledge(
        self,
        *,
        user_id: str,
        celebration_id: str,
    ) -> CelebrationAcknowledgement:
        now = self.clock()
        document = await self.collection.find_one_and_update(
            {
                "_id": celebration_id,
                "user_id": user_id,
                "status": CelebrationStatus.PENDING.value,
            },
            {
                "$set": {
                    "status": CelebrationStatus.ACKNOWLEDGED.value,
                    "acknowledged_at": now,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if document:
            ensure_celebration_transition(
                CelebrationStatus.PENDING,
                CelebrationStatus.ACKNOWLEDGED,
                MissionTransitionReason.ACKNOWLEDGEMENT,
            )
            return CelebrationAcknowledgement(
                celebration=Celebration.model_validate(document),
                newly_acknowledged=True,
            )

        existing = await self.collection.find_one(
            {"_id": celebration_id, "user_id": user_id}
        )
        if existing:
            return CelebrationAcknowledgement(
                celebration=Celebration.model_validate(existing),
                newly_acknowledged=False,
            )
        raise CelebrationQueueError(
            CelebrationQueueErrorCode.CELEBRATION_NOT_FOUND,
            "Celebration was not found for this user",
        )

    @staticmethod
    def _same_request_or_conflict(
        document: dict,
        payload_hash: str,
    ) -> Celebration:
        if document.get("payload_hash") != payload_hash:
            raise CelebrationQueueError(
                CelebrationQueueErrorCode.IDEMPOTENCY_CONFLICT,
                "Celebration idempotency key was already used with another request",
            )
        return Celebration.model_validate(document)
