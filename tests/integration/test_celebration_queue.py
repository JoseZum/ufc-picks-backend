import asyncio
from datetime import UTC, datetime, timedelta
from itertools import count

import pytest

from app.modules.missions.application import (
    CelebrationQueueError,
    CelebrationQueueErrorCode,
    CelebrationQueueService,
    XpLedgerService,
)
from app.modules.missions.domain import (
    AwardXpCommand,
    CelebrationKind,
    CelebrationPresentation,
    CompensateXpCommand,
    EnqueueCelebrationCommand,
)
from app.modules.missions.indexes import apply_mission_indexes

NOW = datetime(2026, 8, 1, 16, tzinfo=UTC)


@pytest.fixture
async def queue_services(test_db):
    await apply_mission_indexes(test_db)
    return (
        XpLedgerService(test_db, clock=lambda: NOW),
        CelebrationQueueService(test_db, clock=lambda: NOW),
    )


def award(key: str, source_id: str, amount: int = 6) -> AwardXpCommand:
    return AwardXpCommand(
        idempotency_key=key,
        source_type="CARD_MISSION",
        source_id=source_id,
        amount=amount,
        reason="Mission completed",
    )


def celebration(xp_entry_id: str, key: str, *, heading="Mission complete"):
    return EnqueueCelebrationCommand(
        idempotency_key=key,
        xp_entry_id=xp_entry_id,
        kind=CelebrationKind.MISSION_COMPLETED,
        presentation=CelebrationPresentation.TOAST,
        heading=heading,
        message="XP added to your profile.",
    )


@pytest.mark.asyncio
async def test_enqueue_is_retry_and_concurrency_idempotent(queue_services, test_db):
    ledger, queue = queue_services
    xp = await ledger.award(
        user_id="jose",
        command=award("mission:assignment-1", "assignment-1"),
    )
    command = celebration(xp.id, "celebration:assignment-1")

    values = await asyncio.gather(
        *(queue.enqueue(user_id="jose", command=command) for _ in range(5))
    )
    retry = await queue.enqueue(user_id="jose", command=command)

    assert len({value.id for value in values}) == 1
    assert retry == values[0]
    assert await test_db["mission_celebrations"].count_documents({}) == 1


@pytest.mark.asyncio
async def test_idempotency_key_cannot_change_payload(queue_services):
    ledger, queue = queue_services
    xp = await ledger.award(
        user_id="jose",
        command=award("mission:assignment-2", "assignment-2"),
    )
    await queue.enqueue(
        user_id="jose",
        command=celebration(xp.id, "celebration:assignment-2"),
    )

    with pytest.raises(CelebrationQueueError) as raised:
        await queue.enqueue(
            user_id="jose",
            command=celebration(
                xp.id,
                "celebration:assignment-2",
                heading="Changed heading",
            ),
        )

    assert raised.value.code == CelebrationQueueErrorCode.IDEMPOTENCY_CONFLICT


@pytest.mark.asyncio
async def test_celebration_requires_owners_positive_award(queue_services):
    ledger, queue = queue_services
    xp = await ledger.award(
        user_id="jose",
        command=award("mission:assignment-3", "assignment-3"),
    )
    compensation = await ledger.compensate(
        user_id="jose",
        command=CompensateXpCommand(
            idempotency_key="compensation:assignment-3",
            original_entry_id=xp.id,
            reason="Result correction",
        ),
    )

    for user_id, entry_id, key in (
        ("chris", xp.id, "celebration:wrong-owner"),
        ("jose", compensation.id, "celebration:compensation"),
    ):
        with pytest.raises(CelebrationQueueError) as raised:
            await queue.enqueue(
                user_id=user_id,
                command=celebration(entry_id, key),
            )
        assert raised.value.code == CelebrationQueueErrorCode.XP_AWARD_NOT_FOUND


@pytest.mark.asyncio
async def test_pending_queue_is_user_scoped_fifo_and_ack_is_idempotent(
    queue_services,
    test_db,
):
    ledger, _ = queue_services
    seconds = count()
    queue = CelebrationQueueService(
        test_db,
        clock=lambda: NOW + timedelta(seconds=next(seconds)),
    )
    jose_first_xp = await ledger.award(
        user_id="jose", command=award("mission:jose-1", "jose-1", 1)
    )
    jose_second_xp = await ledger.award(
        user_id="jose", command=award("mission:jose-2", "jose-2", 3)
    )
    chris_xp = await ledger.award(
        user_id="chris", command=award("mission:chris-1", "chris-1", 1)
    )
    first = await queue.enqueue(
        user_id="jose",
        command=celebration(jose_first_xp.id, "celebration:jose-1"),
    )
    second = await queue.enqueue(
        user_id="jose",
        command=celebration(jose_second_xp.id, "celebration:jose-2"),
    )
    await queue.enqueue(
        user_id="chris",
        command=celebration(chris_xp.id, "celebration:chris-1"),
    )

    assert [item.id for item in await queue.pending_for_user("jose")] == [
        first.id,
        second.id,
    ]
    acknowledged = await queue.acknowledge(user_id="jose", celebration_id=first.id)
    retry = await queue.acknowledge(user_id="jose", celebration_id=first.id)

    assert acknowledged.newly_acknowledged is True
    assert retry.newly_acknowledged is False
    assert retry.celebration.acknowledged_at == acknowledged.celebration.acknowledged_at
    assert [item.id for item in await queue.pending_for_user("jose")] == [second.id]
    assert len(await queue.pending_for_user("chris")) == 1

    with pytest.raises(CelebrationQueueError) as raised:
        await queue.acknowledge(user_id="chris", celebration_id=second.id)
    assert raised.value.code == CelebrationQueueErrorCode.CELEBRATION_NOT_FOUND


@pytest.mark.asyncio
async def test_pending_limit_is_bounded(queue_services):
    _, queue = queue_services
    with pytest.raises(ValueError, match="between 1 and 100"):
        await queue.pending_for_user("jose", limit=0)
