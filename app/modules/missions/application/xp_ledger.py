"""Append-only, idempotent XP ledger operations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime

from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from app.modules.missions.domain.enums import StringEnum
from app.modules.missions.domain.xp import (
    AwardXpCommand,
    CompensateXpCommand,
    XpEntryType,
    XpLedgerEntry,
)


class XpLedgerErrorCode(StringEnum):
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    ORIGINAL_NOT_FOUND = "ORIGINAL_NOT_FOUND"
    INVALID_COMPENSATION = "INVALID_COMPENSATION"


class XpLedgerError(ValueError):
    def __init__(self, code: XpLedgerErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _payload_hash(user_id: str, command) -> str:
    payload = {"user_id": user_id, **command.model_dump(mode="json")}
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def _award_id(user_id: str, idempotency_key: str) -> str:
    value = f"{user_id}\x1f{idempotency_key}"
    return f"xp_{hashlib.sha256(value.encode()).hexdigest()[:24]}"


def _compensation_id(original_entry_id: str) -> str:
    return f"xp_{hashlib.sha256(f'compensate:{original_entry_id}'.encode()).hexdigest()[:24]}"


class XpLedgerService:
    def __init__(self, db: AsyncDatabase, *, clock: Clock = _utc_now) -> None:
        self.db = db
        self.collection = db["mission_xp_ledger"]
        self.clock = clock

    async def award(
        self,
        *,
        user_id: str,
        command: AwardXpCommand,
        session: AsyncClientSession | None = None,
    ) -> XpLedgerEntry:
        payload_hash = _payload_hash(user_id, command)
        entry_id = _award_id(user_id, command.idempotency_key)
        existing = await self.collection.find_one(
            {"user_id": user_id, "idempotency_key": command.idempotency_key},
            session=session,
        )
        if existing:
            return self._same_request_or_conflict(existing, payload_hash)

        entry = XpLedgerEntry(
            _id=entry_id,
            user_id=user_id,
            idempotency_key=command.idempotency_key,
            entry_type=XpEntryType.AWARD,
            source_type=command.source_type,
            source_id=command.source_id,
            amount=command.amount,
            reason=command.reason,
            payload_hash=payload_hash,
            metadata=command.metadata,
            created_at=self.clock(),
        )
        try:
            await self.collection.insert_one(
                entry.model_dump(by_alias=True, mode="python"),
                session=session,
            )
            return entry
        except DuplicateKeyError:
            if session is not None:
                raise
            concurrent = await self.collection.find_one(
                {"user_id": user_id, "idempotency_key": command.idempotency_key},
                session=session,
            )
            if concurrent:
                return self._same_request_or_conflict(concurrent, payload_hash)
            raise

    async def compensate(
        self,
        *,
        user_id: str,
        command: CompensateXpCommand,
        session: AsyncClientSession | None = None,
    ) -> XpLedgerEntry:
        payload_hash = _payload_hash(user_id, command)
        compensation_id = _compensation_id(command.original_entry_id)

        async def callback(session: AsyncClientSession) -> XpLedgerEntry:
            same_key = await self.collection.find_one(
                {"user_id": user_id, "idempotency_key": command.idempotency_key},
                session=session,
            )
            if same_key:
                return self._same_request_or_conflict(same_key, payload_hash)

            existing_compensation = await self.collection.find_one(
                {"_id": compensation_id},
                session=session,
            )
            if existing_compensation:
                return XpLedgerEntry.model_validate(existing_compensation)

            original = await self.collection.find_one(
                {
                    "_id": command.original_entry_id,
                    "user_id": user_id,
                },
                session=session,
            )
            if not original:
                raise XpLedgerError(
                    XpLedgerErrorCode.ORIGINAL_NOT_FOUND,
                    "Original XP award was not found for this user",
                )
            original_entry = XpLedgerEntry.model_validate(original)
            if original_entry.entry_type != XpEntryType.AWARD:
                raise XpLedgerError(
                    XpLedgerErrorCode.INVALID_COMPENSATION,
                    "Only a positive XP award can be compensated",
                )

            compensation = XpLedgerEntry(
                _id=compensation_id,
                user_id=user_id,
                idempotency_key=command.idempotency_key,
                entry_type=XpEntryType.COMPENSATION,
                source_type=original_entry.source_type,
                source_id=original_entry.source_id,
                amount=-original_entry.amount,
                reason=command.reason,
                payload_hash=payload_hash,
                compensates_entry_id=original_entry.id,
                metadata={"original_idempotency_key": original_entry.idempotency_key},
                created_at=self.clock(),
            )
            await self.collection.insert_one(
                compensation.model_dump(by_alias=True, mode="python"),
                session=session,
            )
            return compensation

        try:
            if session is not None:
                return await callback(session)
            async with self.db.client.start_session() as owned_session:
                return await owned_session.with_transaction(callback)
        except DuplicateKeyError:
            if session is not None:
                raise
            existing = await self.collection.find_one({"_id": compensation_id})
            if existing:
                return XpLedgerEntry.model_validate(existing)
            raise

    async def total_for_user(self, user_id: str) -> int:
        cursor = await self.collection.aggregate(
            [
                {"$match": {"user_id": user_id}},
                {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
            ]
        )
        values = await cursor.to_list(length=1)
        return int(values[0]["total"]) if values else 0

    async def entries_for_user(
        self,
        user_id: str,
        *,
        limit: int = 100,
    ) -> tuple[XpLedgerEntry, ...]:
        if limit not in range(1, 501):
            raise ValueError("limit must be between 1 and 500")
        cursor = self.collection.find({"user_id": user_id}).sort("created_at", -1).limit(limit)
        values = await cursor.to_list(length=limit)
        return tuple(XpLedgerEntry.model_validate(value) for value in values)

    @staticmethod
    def _same_request_or_conflict(
        document: dict,
        payload_hash: str,
    ) -> XpLedgerEntry:
        if document.get("payload_hash") != payload_hash:
            raise XpLedgerError(
                XpLedgerErrorCode.IDEMPOTENCY_CONFLICT,
                "XP idempotency key was already used with another request",
            )
        return XpLedgerEntry.model_validate(document)
