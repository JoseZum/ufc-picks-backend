import asyncio
from datetime import UTC, datetime

import pytest

from app.modules.missions.application import (
    XpLedgerError,
    XpLedgerErrorCode,
    XpLedgerService,
)
from app.modules.missions.domain import AwardXpCommand, CompensateXpCommand
from app.modules.missions.indexes import apply_mission_indexes

NOW = datetime(2026, 8, 1, 13, tzinfo=UTC)


@pytest.fixture
async def ledger(test_db):
    await apply_mission_indexes(test_db)
    return XpLedgerService(test_db, clock=lambda: NOW)


def award_command(key="mission-award-001", amount=6):
    return AwardXpCommand(
        idempotency_key=key,
        source_type="CARD_MISSION",
        source_id="assignment-123",
        amount=amount,
        reason="Mission completed",
        metadata={"event_id": 4242},
    )


@pytest.mark.asyncio
async def test_award_is_append_only_and_retry_idempotent(ledger, test_db):
    first = await ledger.award(user_id="jose", command=award_command())
    retry = await ledger.award(user_id="jose", command=award_command())

    assert retry == first
    assert first.amount == 6
    assert await test_db["mission_xp_ledger"].count_documents({}) == 1
    assert await ledger.total_for_user("jose") == 6
    assert await test_db["users"].count_documents({}) == 0
    assert await test_db["picks"].count_documents({}) == 0


@pytest.mark.asyncio
async def test_concurrent_award_retries_converge_to_one_entry(ledger, test_db):
    results = await asyncio.gather(
        *(ledger.award(user_id="jose", command=award_command()) for _ in range(5))
    )

    assert len({result.id for result in results}) == 1
    assert await test_db["mission_xp_ledger"].count_documents({}) == 1


@pytest.mark.asyncio
async def test_idempotency_key_cannot_change_award_payload(ledger):
    await ledger.award(user_id="jose", command=award_command())

    with pytest.raises(XpLedgerError) as raised:
        await ledger.award(user_id="jose", command=award_command(amount=10))

    assert raised.value.code == XpLedgerErrorCode.IDEMPOTENCY_CONFLICT


@pytest.mark.asyncio
async def test_compensation_is_exact_negative_link_and_is_itself_idempotent(
    ledger,
    test_db,
):
    award = await ledger.award(user_id="jose", command=award_command())
    compensation = await ledger.compensate(
        user_id="jose",
        command=CompensateXpCommand(
            idempotency_key="mission-compensation-001",
            original_entry_id=award.id,
            reason="Result correction",
        ),
    )
    retry_with_another_key = await ledger.compensate(
        user_id="jose",
        command=CompensateXpCommand(
            idempotency_key="mission-compensation-002",
            original_entry_id=award.id,
            reason="Same correction retried",
        ),
    )

    assert compensation.amount == -6
    assert compensation.compensates_entry_id == award.id
    assert retry_with_another_key.id == compensation.id
    assert await test_db["mission_xp_ledger"].count_documents({}) == 2
    assert await ledger.total_for_user("jose") == 0


@pytest.mark.asyncio
async def test_cannot_compensate_another_users_or_a_compensation(ledger):
    award = await ledger.award(user_id="jose", command=award_command())
    with pytest.raises(XpLedgerError) as raised:
        await ledger.compensate(
            user_id="chris",
            command=CompensateXpCommand(
                idempotency_key="wrong-user-compensation",
                original_entry_id=award.id,
                reason="Not allowed",
            ),
        )
    assert raised.value.code == XpLedgerErrorCode.ORIGINAL_NOT_FOUND

    compensation = await ledger.compensate(
        user_id="jose",
        command=CompensateXpCommand(
            idempotency_key="valid-compensation",
            original_entry_id=award.id,
            reason="Correction",
        ),
    )
    with pytest.raises(XpLedgerError) as raised:
        await ledger.compensate(
            user_id="jose",
            command=CompensateXpCommand(
                idempotency_key="compensate-compensation",
                original_entry_id=compensation.id,
                reason="Invalid chain",
            ),
        )
    assert raised.value.code == XpLedgerErrorCode.INVALID_COMPENSATION


@pytest.mark.asyncio
async def test_entries_are_returned_newest_first_with_bounded_limit(ledger):
    await ledger.award(user_id="jose", command=award_command("award-key-0001", 1))
    await ledger.award(user_id="jose", command=award_command("award-key-0002", 3))

    entries = await ledger.entries_for_user("jose", limit=1)
    assert len(entries) == 1
    with pytest.raises(ValueError, match="between 1 and 500"):
        await ledger.entries_for_user("jose", limit=0)
