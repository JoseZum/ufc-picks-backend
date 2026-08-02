"""Terminal card evaluation and finalized leaderboard behavior."""

from datetime import UTC, datetime

import pytest

from app.modules.missions.application import (
    CardFinalizationError,
    CardFinalizationErrorCode,
    CardMissionFinalizer,
    FinalizeCardMissionsCommand,
)
from app.modules.missions.catalog import load_card_catalog
from app.modules.missions.indexes import apply_mission_indexes

EVENT_ID = 99101
NOW = datetime(2026, 8, 1, 20, tzinfo=UTC)


def event_document() -> dict:
    return {
        "_id": "final-event",
        "id": EVENT_ID,
        "status": "completed",
        "card_data_v1": {
            "structure_revision": 1,
            "current_eligibility": {
                "card_snapshot_revision": 1,
                "eligible_targets": [{"bout_id": 201, "matchup_revision": 1}],
                "denominator": 1,
                "fingerprint": "sha256:final-card-eligibility",
            },
        },
    }


def bout_document(*, completed: bool = True) -> dict:
    canonical_result = (
        {
            "revision": 1,
            "status": "final",
            "outcome": "red_win",
            "winner_fighter_id": "fighter-201-red",
            "method_family": "ko_tko",
            "ending_round": 1,
        }
        if completed
        else None
    )
    return {
        "_id": "final-bout",
        "id": 201,
        "event_id": EVENT_ID,
        "status": "completed" if completed else "scheduled",
        "card_data_v1": {
            "bout_id": 201,
            "event_id": EVENT_ID,
            "matchup_revision": 1,
            "status": "completed" if completed else "scheduled",
            "fighters": [
                {
                    "fighter_id": "fighter-201-red",
                    "display_name": "Red Finalist",
                    "corner": "red",
                },
                {
                    "fighter_id": "fighter-201-blue",
                    "display_name": "Blue Finalist",
                    "corner": "blue",
                },
            ],
            "scheduled_rounds": 3,
            "is_title_fight": False,
            "result_revision": int(completed),
            "result": canonical_result,
        },
    }


def assignment_document() -> dict:
    definition = load_card_catalog().get("CARD-V2-M-020")
    return {
        "_id": "assignment-card-champion",
        "user_id": "jose",
        "event_id": EVENT_ID,
        "slot": 1,
        "offer_set_id": "offer-final",
        "offer_id": "offer-card-champion",
        "mission_id": definition.mission_id,
        "catalog_version": definition.catalog_version,
        "card_revision": 1,
        "eligibility_snapshot": {
            "eligibility_snapshot_id": "elig-final-1",
            "eligibility_revision": 1,
            "fingerprint": "sha256:assignment-final-eligibility",
            "eligible_targets": [{"bout_id": 201, "matchup_revision": 1}],
            "denominator": 1,
        },
        "definition_snapshot": definition.model_dump(mode="json"),
        "selection": {"kind": "AUTO"},
        "status": "ACTIVE",
        "xp": definition.xp,
        "progress": {},
        "linked_pick_ids": [],
        "revision": 1,
        "created_at": NOW,
        "updated_at": NOW,
    }


def pick(user_id: str, fighter: str, round_: int) -> dict:
    return {
        "_id": f"{user_id}:201",
        "user_id": user_id,
        "event_id": EVENT_ID,
        "bout_id": 201,
        "picked_fighter_name": f"{fighter.title()} Finalist",
        "picked_method": "KO/TKO",
        "picked_round": round_,
        "points_awarded": 0,
        "is_correct": None,
    }


@pytest.fixture
async def finalization_db(test_db):
    await apply_mission_indexes(test_db)
    await test_db["events"].insert_one(event_document())
    await test_db["bouts"].insert_one(bout_document())
    await test_db["event_card_slots"].insert_one(
        {
            "_id": f"{EVENT_ID}:201",
            "event_id": EVENT_ID,
            "bout_id": 201,
            "is_current": True,
            "card_section": "main",
            "order_overall": 1,
            "order_section": 1,
            "role": "main_event",
            "structure_revision": 1,
        }
    )
    await test_db["mission_assignments"].insert_one(assignment_document())
    await test_db["picks"].insert_many(
        [
            pick("jose", "red", 1),
            pick("ana", "red", 2),
            pick("bob", "blue", 1),
        ]
    )
    return test_db


@pytest.mark.asyncio
async def test_finalization_freezes_rank_and_replays_without_duplicate_xp(finalization_db):
    finalizer = CardMissionFinalizer(finalization_db)
    command = FinalizeCardMissionsCommand(EVENT_ID, 1)

    first = await finalizer.finalize(command)
    assert first.active_user_count == 3
    assert first.failures == ()
    assert first.assignments[0].status.value == "COMPLETED"
    assert first.assignments[0].xp_delta == 4
    stored = await finalization_db["mission_assignments"].find_one(
        {"_id": "assignment-card-champion"}
    )
    assert stored["progress"]["observation"]["value"] == 1
    assert stored["progress"]["observation"]["details"]["rank"] == 1

    replay = await finalizer.finalize(command)
    assert replay.replayed_count == 1
    assert await finalization_db["mission_xp_ledger"].count_documents({}) == 1
    assert await finalization_db["mission_evaluation_runs"].count_documents({}) == 1
    assert await finalization_db["mission_card_finalization_runs"].count_documents(
        {}
    ) == 1


@pytest.mark.asyncio
async def test_same_finalization_revision_rejects_changed_inputs(finalization_db):
    finalizer = CardMissionFinalizer(finalization_db)
    command = FinalizeCardMissionsCommand(EVENT_ID, 1)
    await finalizer.finalize(command)
    await finalization_db["picks"].update_one(
        {"_id": "ana:201"}, {"$set": {"picked_round": 1}}
    )

    with pytest.raises(CardFinalizationError) as raised:
        await finalizer.finalize(command)

    assert (
        raised.value.code
        == CardFinalizationErrorCode.FINALIZATION_REVISION_CONFLICT
    )


@pytest.mark.asyncio
async def test_finalization_refuses_an_unresolved_current_bout(finalization_db):
    scheduled = bout_document(completed=False)["card_data_v1"]
    await finalization_db["bouts"].update_one(
        {"id": 201},
        {"$set": {"status": "scheduled", "card_data_v1": scheduled}},
    )

    with pytest.raises(CardFinalizationError) as raised:
        await CardMissionFinalizer(finalization_db).finalize(
            FinalizeCardMissionsCommand(EVENT_ID, 1)
        )

    assert raised.value.code == CardFinalizationErrorCode.UNRESOLVED_BOUTS
    stored = await finalization_db["mission_assignments"].find_one(
        {"_id": "assignment-card-champion"}
    )
    assert stored["status"] == "ACTIVE"
    assert await finalization_db["mission_evaluation_runs"].count_documents({}) == 0
