"""Canonical bout-result mission evaluation, retries and corrections."""

import asyncio
from datetime import UTC, datetime

import pytest

from app.modules.missions.application import (
    BoutEvaluationError,
    BoutEvaluationErrorCode,
    BoutResultMissionEvaluator,
    EvaluateBoutResultCommand,
)
from app.modules.missions.catalog import load_card_catalog
from app.modules.missions.indexes import apply_mission_indexes

NOW = datetime(2026, 8, 1, 18, tzinfo=UTC)
EVENT_ID = 99001


def canonical_bout(bout_id: int, *, role: str = "regular") -> dict:
    return {
        "_id": f"bout-{bout_id}",
        "id": bout_id,
        "event_id": EVENT_ID,
        "status": "scheduled",
        "fighters": {
            "red": {"fighter_name": f"Red {bout_id}"},
            "blue": {"fighter_name": f"Blue {bout_id}"},
        },
        "card_data_v1": {
            "bout_id": bout_id,
            "event_id": EVENT_ID,
            "matchup_revision": 1,
            "status": "scheduled",
            "fighters": [
                {
                    "fighter_id": f"fighter-{bout_id}-red",
                    "display_name": f"Red {bout_id}",
                    "corner": "red",
                },
                {
                    "fighter_id": f"fighter-{bout_id}-blue",
                    "display_name": f"Blue {bout_id}",
                    "corner": "blue",
                },
            ],
            "scheduled_rounds": 3,
            "is_title_fight": False,
            "result_revision": 0,
            "result": None,
        },
        "_test_role": role,
    }


def canonical_slot(bout_id: int, *, role: str = "regular", order: int = 1) -> dict:
    return {
        "_id": f"{EVENT_ID}:{bout_id}",
        "event_id": EVENT_ID,
        "bout_id": bout_id,
        "is_current": True,
        "card_section": "main",
        "order_overall": order,
        "order_section": order,
        "role": role,
        "structure_revision": 1,
    }


def assignment(definition, *, assignment_id: str, selection: dict) -> dict:
    return {
        "_id": assignment_id,
        "user_id": "jose",
        "event_id": EVENT_ID,
        "slot": 1,
        "offer_set_id": "offer-set-test",
        "offer_id": "offer-test",
        "mission_id": definition.mission_id,
        "catalog_version": definition.catalog_version,
        "card_revision": 1,
        "eligibility_snapshot": {
            "eligibility_snapshot_id": "elig-test-1",
            "eligibility_revision": 1,
            "fingerprint": "sha256:test-frozen-eligibility",
            "eligible_targets": [
                {"bout_id": 101, "matchup_revision": 1},
                {"bout_id": 102, "matchup_revision": 1},
            ],
            "denominator": 2,
        },
        "definition_snapshot": definition.model_dump(mode="json"),
        "selection": selection,
        "status": "ACTIVE",
        "xp": definition.xp,
        "progress": {},
        "linked_pick_ids": [],
        "revision": 1,
        "created_at": NOW,
        "updated_at": NOW,
    }


def result(revision: int, winner: str, *, corrected: bool = False) -> dict:
    return {
        "revision": revision,
        "status": "corrected" if corrected else "final",
        "outcome": f"{winner}_win",
        "winner_fighter_id": f"fighter-101-{winner}",
        "method_family": "ko_tko",
        "ending_round": 1,
    }


async def register_result(db, bout_id: int, revision: int, winner: str) -> None:
    await db["bouts"].update_one(
        {"id": bout_id},
        {
            "$set": {
                "status": "completed",
                "card_data_v1.status": "completed",
                "card_data_v1.result_revision": revision,
                "card_data_v1.result": {
                    "revision": revision,
                    "status": "corrected" if revision > 1 else "final",
                    "outcome": f"{winner}_win",
                    "winner_fighter_id": f"fighter-{bout_id}-{winner}",
                    "method_family": "ko_tko",
                    "ending_round": 1,
                },
            }
        },
    )


@pytest.fixture
async def evaluation_db(test_db):
    await apply_mission_indexes(test_db)
    bouts = [canonical_bout(101, role="main_event"), canonical_bout(102)]
    await test_db["events"].insert_one(
        {
            "_id": "event-test",
            "id": EVENT_ID,
            "status": "scheduled",
            "card_data_v1": {
                "structure_revision": 1,
                "current_eligibility": {
                    "card_snapshot_revision": 1,
                    "eligible_targets": [
                        {"bout_id": 101, "matchup_revision": 1},
                        {"bout_id": 102, "matchup_revision": 1},
                    ],
                    "denominator": 2,
                    "fingerprint": "sha256:test-current-eligibility",
                },
            },
        }
    )
    await test_db["bouts"].insert_many(bouts)
    await test_db["event_card_slots"].insert_many(
        [
            canonical_slot(101, role="main_event", order=1),
            canonical_slot(102, order=2),
        ]
    )
    return test_db


@pytest.mark.asyncio
async def test_completion_retry_and_result_corrections_are_ledger_safe(evaluation_db):
    definition = load_card_catalog().get("CARD-V2-E-001")
    await evaluation_db["mission_assignments"].insert_one(
        assignment(
            definition,
            assignment_id="assignment-correction-test",
            selection={
                "kind": "TARGET_FIGHTER",
                "bout_id": 101,
                "corner": "red",
                "selected_fighter_id": "fighter-101-red",
                "selected_fighter_name": "Red 101",
                "canonical_winner_id": "fighter-101-red",
                "canonical_winner_name": "Red 101",
                "method": None,
                "round": None,
                "bout_ids": [101],
            },
        )
    )
    evaluator = BoutResultMissionEvaluator(evaluation_db, clock=lambda: NOW)

    await register_result(evaluation_db, 101, 1, "red")
    first = await evaluator.evaluate(EvaluateBoutResultCommand(EVENT_ID, 101, 1))
    assert first.assignments[0].status.value == "COMPLETED"
    assert first.assignments[0].xp_delta == 1
    assert await evaluation_db["mission_xp_ledger"].count_documents({}) == 1
    assert await evaluation_db["mission_celebrations"].count_documents(
        {"status": "PENDING"}
    ) == 1

    replay = await evaluator.evaluate(EvaluateBoutResultCommand(EVENT_ID, 101, 1))
    assert replay.assignments[0].replayed is True
    assert await evaluation_db["mission_xp_ledger"].count_documents({}) == 1
    stored = await evaluation_db["mission_assignments"].find_one(
        {"_id": "assignment-correction-test"}
    )
    assert stored["revision"] == 2

    await register_result(evaluation_db, 101, 2, "blue")
    corrected = await evaluator.evaluate(EvaluateBoutResultCommand(EVENT_ID, 101, 2))
    assert corrected.assignments[0].status.value == "FAILED"
    assert corrected.assignments[0].xp_delta == -1
    assert await evaluator.xp.total_for_user("jose") == 0
    assert await evaluation_db["mission_celebrations"].count_documents(
        {"status": "CANCELLED"}
    ) == 1
    cancelled = await evaluation_db["mission_celebrations"].find_one(
        {"status": "CANCELLED"}
    )
    acknowledgement = await evaluator.celebrations.acknowledge(
        user_id="jose",
        celebration_id=cancelled["_id"],
    )
    assert acknowledgement.newly_acknowledged is False
    assert acknowledgement.celebration.status.value == "CANCELLED"

    await register_result(evaluation_db, 101, 3, "red")
    restored = await evaluator.evaluate(EvaluateBoutResultCommand(EVENT_ID, 101, 3))
    assert restored.assignments[0].status.value == "COMPLETED"
    assert restored.assignments[0].xp_delta == 1
    assert await evaluator.xp.total_for_user("jose") == 1
    assert await evaluation_db["mission_xp_ledger"].count_documents({}) == 3
    assert await evaluation_db["mission_celebrations"].count_documents(
        {"status": "PENDING"}
    ) == 1


@pytest.mark.asyncio
async def test_each_bout_revision_updates_incremental_progress(evaluation_db):
    definition = load_card_catalog().get("CARD-V2-E-006")
    await evaluation_db["mission_assignments"].insert_one(
        assignment(
            definition,
            assignment_id="assignment-incremental-test",
            selection={"kind": "AUTO"},
        )
    )
    await evaluation_db["picks"].insert_many(
        [
            {
                "_id": f"jose:{bout_id}",
                "user_id": "jose",
                "event_id": EVENT_ID,
                "bout_id": bout_id,
                "picked_fighter_name": f"Red {bout_id}",
                "picked_method": "KO/TKO",
                "picked_round": 1,
                "points_awarded": 0,
                "is_correct": None,
            }
            for bout_id in (101, 102)
        ]
    )
    await evaluation_db["picks"].update_one(
        {"_id": "jose:102"},
        {"$set": {"picked_round": None}},
    )
    evaluator = BoutResultMissionEvaluator(evaluation_db, clock=lambda: NOW)

    await register_result(evaluation_db, 101, 1, "red")
    first = await evaluator.evaluate(EvaluateBoutResultCommand(EVENT_ID, 101, 1))
    assert first.assignments[0].status.value == "ACTIVE"
    stored = await evaluation_db["mission_assignments"].find_one(
        {"_id": "assignment-incremental-test"}
    )
    assert stored["progress"]["observation"]["value"] == 1
    assert stored["progress"]["progress"]["percent"] == 50

    await register_result(evaluation_db, 102, 1, "red")
    second = await evaluator.evaluate(EvaluateBoutResultCommand(EVENT_ID, 102, 1))
    assert second.assignments[0].status.value == "COMPLETED"
    assert second.assignments[0].xp_delta == definition.xp
    assert await evaluation_db["mission_evaluation_runs"].count_documents({}) == 2
    assert await evaluator.xp.total_for_user("jose") == definition.xp


@pytest.mark.asyncio
async def test_invalid_assignment_does_not_block_valid_assignment(evaluation_db):
    definition = load_card_catalog().get("CARD-V2-E-001")
    valid = assignment(
        definition,
        assignment_id="assignment-valid-isolation",
        selection={
            "kind": "TARGET_FIGHTER",
            "bout_id": 101,
            "corner": "red",
            "selected_fighter_id": "fighter-101-red",
            "method": None,
            "round": None,
            "bout_ids": [101],
        },
    )
    invalid = assignment(
        definition,
        assignment_id="assignment-invalid-isolation",
        selection={"kind": "TARGET_FIGHTER", "corner": "red"},
    )
    invalid["slot"] = 2
    await evaluation_db["mission_assignments"].insert_many([invalid, valid])
    await register_result(evaluation_db, 101, 1, "red")

    evaluated = await BoutResultMissionEvaluator(
        evaluation_db, clock=lambda: NOW
    ).evaluate(EvaluateBoutResultCommand(EVENT_ID, 101, 1))

    assert [item.assignment_id for item in evaluated.assignments] == [
        "assignment-valid-isolation"
    ]
    assert len(evaluated.failures) == 1
    assert evaluated.failures[0].assignment_id == "assignment-invalid-isolation"
    stored_invalid = await evaluation_db["mission_assignments"].find_one(
        {"_id": "assignment-invalid-isolation"}
    )
    assert stored_invalid["status"] == "ACTIVE"
    assert stored_invalid["revision"] == 1


@pytest.mark.asyncio
async def test_draw_voids_a_direct_fighter_target(evaluation_db):
    definition = load_card_catalog().get("CARD-V2-E-001")
    await evaluation_db["mission_assignments"].insert_one(
        assignment(
            definition,
            assignment_id="assignment-draw-void",
            selection={
                "kind": "TARGET_FIGHTER",
                "bout_id": 101,
                "corner": "red",
                "selected_fighter_id": "fighter-101-red",
                "selected_fighter_name": "Red 101",
                "method": None,
                "round": None,
                "bout_ids": [101],
            },
        )
    )
    await evaluation_db["bouts"].update_one(
        {"id": 101},
        {
            "$set": {
                "status": "completed",
                "card_data_v1.status": "completed",
                "card_data_v1.result_revision": 1,
                "card_data_v1.result": {
                    "revision": 1,
                    "status": "final",
                    "outcome": "draw",
                    "winner_fighter_id": None,
                    "method_family": "decision",
                    "ending_round": 3,
                },
            }
        },
    )

    evaluated = await BoutResultMissionEvaluator(evaluation_db).evaluate(
        EvaluateBoutResultCommand(EVENT_ID, 101, 1)
    )

    assert evaluated.assignments[0].status.value == "VOID"
    assert evaluated.assignments[0].xp_delta == 0
    assert await evaluation_db["mission_xp_ledger"].count_documents({}) == 0


@pytest.mark.asyncio
async def test_trigger_revision_guard_and_concurrent_retry(evaluation_db):
    definition = load_card_catalog().get("CARD-V2-E-001")
    await evaluation_db["mission_assignments"].insert_one(
        assignment(
            definition,
            assignment_id="assignment-concurrent-retry",
            selection={
                "kind": "TARGET_FIGHTER",
                "bout_id": 101,
                "corner": "red",
                "selected_fighter_id": "fighter-101-red",
                "selected_fighter_name": "Red 101",
                "method": None,
                "round": None,
                "bout_ids": [101],
            },
        )
    )
    await register_result(evaluation_db, 101, 1, "red")
    evaluator = BoutResultMissionEvaluator(evaluation_db, clock=lambda: NOW)

    with pytest.raises(BoutEvaluationError) as raised:
        await evaluator.evaluate(EvaluateBoutResultCommand(EVENT_ID, 101, 2))
    assert raised.value.code == BoutEvaluationErrorCode.RESULT_REVISION_MISMATCH

    left, right = await asyncio.gather(
        evaluator.evaluate(EvaluateBoutResultCommand(EVENT_ID, 101, 1)),
        evaluator.evaluate(EvaluateBoutResultCommand(EVENT_ID, 101, 1)),
    )
    assert not left.failures
    assert not right.failures
    assert sorted((left.replayed_count, right.replayed_count)) == [0, 1]
    assert await evaluation_db["mission_evaluation_runs"].count_documents({}) == 1
    assert await evaluation_db["mission_xp_ledger"].count_documents({}) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mission_id", "selection"),
    (
        ("CARD-V2-E-004", {"kind": "AUTO"}),
        (
            "CARD-V2-E-001",
            {
                "kind": "TARGET_FIGHTER",
                "bout_id": 101,
                "selected_fighter_id": "fighter-101-red",
                "selected_fighter_name": "Red 101",
                "method": None,
                "round": None,
            },
        ),
        ("CARD-V2-M-001", {"kind": "TARGET_FIGHT", "bout_id": 101}),
        (
            "CARD-V2-M-011",
            {
                "kind": "COMBO_BUILDER",
                "legs": [
                    {
                        "key": "winner_one",
                        "bout_id": 101,
                        "fighter_id": "fighter-101-red",
                    },
                    {
                        "key": "winner_two",
                        "bout_id": 102,
                        "fighter_id": "fighter-102-red",
                    },
                ],
            },
        ),
        ("CARD-V2-E-012", {"kind": "CARD_PROP"}),
    ),
)
async def test_each_interaction_family_replays_the_same_trigger_once(
    evaluation_db,
    mission_id,
    selection,
):
    definition = load_card_catalog().get(mission_id)
    assignment_id = f"assignment-retry-{definition.interaction.value.lower()}"
    await evaluation_db["mission_assignments"].insert_one(
        assignment(
            definition,
            assignment_id=assignment_id,
            selection=selection,
        )
    )
    await evaluation_db["picks"].insert_one(
        {
            "_id": f"jose:{mission_id}:101",
            "user_id": "jose",
            "event_id": EVENT_ID,
            "bout_id": 101,
            "picked_fighter_name": "Red 101",
            "picked_method": "KO/TKO",
            "picked_round": 1,
            "points_awarded": 0,
            "is_correct": None,
        }
    )
    await register_result(evaluation_db, 101, 1, "red")
    evaluator = BoutResultMissionEvaluator(evaluation_db, clock=lambda: NOW)
    command = EvaluateBoutResultCommand(EVENT_ID, 101, 1)

    first = await evaluator.evaluate(command)
    retry = await evaluator.evaluate(command)

    assert first.failures == ()
    assert retry.failures == ()
    assert first.assignments[0].replayed is False
    assert retry.assignments[0].replayed is True
    assert await evaluation_db["mission_evaluation_runs"].count_documents(
        {"assignment_id": assignment_id}
    ) == 1
