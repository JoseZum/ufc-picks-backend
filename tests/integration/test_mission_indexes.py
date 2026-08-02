"""Integration coverage for the mission index migration manifest."""

import pytest

from app.modules.missions.indexes import (
    MISSION_INDEXES,
    MissionIndexMigrationError,
    apply_mission_indexes,
    inspect_mission_indexes,
    rollback_mission_indexes,
)


@pytest.mark.asyncio
async def test_index_manifest_applies_idempotently_and_rolls_back_safely(test_db):
    await test_db["mission_assignments"].insert_one(
        {"_id": "keep-me", "user_id": "user", "event_id": 1, "slot": 1}
    )
    await test_db["mission_assignments"].create_index(
        [("external_marker", 1)],
        name="unmanaged_external_index",
    )

    dry_run = await inspect_mission_indexes(test_db)
    assert dry_run.can_apply is True
    assert len(dry_run.missing) == len(MISSION_INDEXES)

    applied = await apply_mission_indexes(test_db)
    assert applied.missing == ()
    assert len(applied.matching) == len(MISSION_INDEXES)

    reapplied = await apply_mission_indexes(test_db)
    assert reapplied == applied

    rolled_back = await rollback_mission_indexes(test_db)
    assert len(rolled_back.missing) == len(MISSION_INDEXES)
    assert await test_db["mission_assignments"].find_one({"_id": "keep-me"})
    names = {
        item["name"]
        for item in await (
            await test_db["mission_assignments"].list_indexes()
        ).to_list(length=None)
    }
    assert "unmanaged_external_index" in names


@pytest.mark.asyncio
async def test_unique_index_duplicate_blocker_prevents_partial_apply(test_db):
    await test_db["mission_card_controls"].insert_many(
        [
            {"_id": "first", "event_id": 77},
            {"_id": "second", "event_id": 77},
        ]
    )

    report = await inspect_mission_indexes(test_db)
    assert any(
        blocker.index_name == "mission_v1_card_control_event_uq"
        and blocker.duplicate_groups == 1
        for blocker in report.duplicate_blockers
    )

    with pytest.raises(MissionIndexMigrationError):
        await apply_mission_indexes(test_db)

    assert await test_db["mission_offer_sets"].index_information() == {}
