from datetime import UTC, datetime

import pytest

from app.modules.missions.application import ProgressionService, XpLedgerService
from app.modules.missions.domain import (
    AwardXpCommand,
    CompensateXpCommand,
    ProgressionTitle,
)
from app.modules.missions.indexes import apply_mission_indexes

NOW = datetime(2026, 8, 1, 15, tzinfo=UTC)


@pytest.fixture
async def progression_services(test_db):
    await apply_mission_indexes(test_db)
    return (
        XpLedgerService(test_db, clock=lambda: NOW),
        ProgressionService(test_db, clock=lambda: NOW),
    )


def award(key: str, source_id: str, amount: int) -> AwardXpCommand:
    return AwardXpCommand(
        idempotency_key=key,
        source_type="CARD_MISSION",
        source_id=source_id,
        amount=amount,
        reason="Mission completed",
    )


@pytest.mark.asyncio
async def test_existing_user_without_ledger_starts_at_level_one(
    progression_services,
    test_db,
):
    _, progression = progression_services

    computed, entry_count = await progression.compute("existing-user")
    cached = await progression.rebuild_cache("existing-user")
    document = await test_db["mission_user_progression"].find_one(
        {"user_id": "existing-user"}
    )

    assert computed == cached
    assert computed.lifetime_xp == 0
    assert computed.level == 1
    assert computed.title == ProgressionTitle.BUM
    assert entry_count == 0
    assert document["revision"] == 1
    assert document["ledger_entry_count"] == 0


@pytest.mark.asyncio
async def test_cache_rebuild_ignores_stale_values_and_projects_compensations(
    progression_services,
    test_db,
):
    ledger, progression = progression_services
    first = await ledger.award(
        user_id="jose", command=award("monthly-aug", "monthly-aug", 15)
    )
    await ledger.award(
        user_id="jose", command=award("card-hard", "assignment-hard", 6)
    )

    initial = await progression.rebuild_cache("jose")
    assert initial.lifetime_xp == 21
    assert initial.level == 4

    await ledger.compensate(
        user_id="jose",
        command=CompensateXpCommand(
            idempotency_key="void-monthly-aug",
            original_entry_id=first.id,
            reason="Monthly result corrected",
        ),
    )
    await test_db["mission_user_progression"].update_one(
        {"user_id": "jose"},
        {"$set": {"lifetime_xp": 9999, "level": 99}},
    )

    rebuilt = await progression.rebuild_cache("jose")
    document = await test_db["mission_user_progression"].find_one(
        {"user_id": "jose"}
    )

    assert rebuilt.lifetime_xp == 6
    assert rebuilt.level == 2
    assert rebuilt.title == ProgressionTitle.BUM
    assert document["revision"] == 2
    assert document["ledger_entry_count"] == 3
