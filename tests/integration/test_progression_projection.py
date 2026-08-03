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


@pytest.mark.asyncio
async def test_a_first_level_up_does_not_claim_a_new_title(
    progression_services, test_db
):
    """Level 2 is still BUM. Only levels 5/10/15/20/30/50 change the title.

    A user with no cached progression looked like they had no title at all, so
    the first level-up compared BUM against None and reported a change. The
    surface renders that flag as "NEW TITLE UNLOCKED", which announced a title
    the user already had.
    """
    ledger, progression = progression_services
    # 5 XP buys level 2 (D-PROD-007); the title boundary is level 5.
    await ledger.award(user_id="climber", command=award("level-two-key", "assignment-1", 6))

    projection = await progression.sync("climber")

    assert projection.level == 2
    assert projection.title == ProgressionTitle.BUM

    celebrations = await test_db["mission_celebrations"].find(
        {"user_id": "climber"}
    ).to_list(length=10)
    kinds = {c["kind"] for c in celebrations}
    assert "LEVEL_UP" in kinds
    assert "TITLE_UNLOCKED" not in kinds, "no title boundary was crossed"

    level_up = next(c for c in celebrations if c["kind"] == "LEVEL_UP")
    assert level_up["metadata"]["title_changed"] is False


@pytest.mark.asyncio
async def test_crossing_a_title_boundary_does_announce_the_new_title(
    progression_services, test_db
):
    ledger, progression = progression_services
    # Level 5 is PROSPECT: 5+7+9+11 = 32 XP to leave level 1 behind.
    await ledger.award(user_id="riser", command=award("level-five-key", "assignment-2", 40))

    projection = await progression.sync("riser")

    assert projection.level >= 5
    assert projection.title == ProgressionTitle.PROSPECT

    celebrations = await test_db["mission_celebrations"].find(
        {"user_id": "riser"}
    ).to_list(length=10)
    kinds = {c["kind"] for c in celebrations}
    assert kinds == {"LEVEL_UP", "TITLE_UNLOCKED"}

    level_up = next(c for c in celebrations if c["kind"] == "LEVEL_UP")
    assert level_up["metadata"]["title_changed"] is True
