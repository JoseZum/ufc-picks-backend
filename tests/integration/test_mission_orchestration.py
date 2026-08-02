"""A canonical result must move mission progress through one real entry point.

These tests call only `MissionTriggerService.on_bout_result` — the same thing a
result writer calls. Nothing here reaches into the evaluator, the finalizer or
the monthly service directly, because the gap this closes is precisely that
nobody was calling them.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.modules.missions.application import (
    MissionSelectionService,
    MissionTriggerService,
    MonthlyConfigService,
)
from app.modules.missions.application.read_models import MissionReadService
from app.modules.missions.catalog import load_card_catalog, load_monthly_catalog
from app.modules.missions.domain.selections import SelectMissionCommand
from app.modules.missions.indexes import apply_mission_indexes

EVENT_ID = 88001
USER = "orchestration-user"
OFFER_SECRET = b"orchestration-tests-offer-secret-000000000000"
EVENT_DATE = datetime(2026, 8, 12, 23, tzinfo=UTC)


def bout(index: int):
    bout_id = 88100 + index
    return {
        "_id": f"bout-{bout_id}",
        "id": bout_id,
        "event_id": EVENT_ID,
        "status": "scheduled",
        "fighters": {
            "red": {"fighter_name": f"Red {index}"},
            "blue": {"fighter_name": f"Blue {index}"},
        },
        "card_data_v1": {
            "bout_id": bout_id,
            "event_id": EVENT_ID,
            "matchup_revision": 1,
            "status": "scheduled",
            "fighters": [
                {
                    "fighter_id": f"fighter-{bout_id}-red",
                    "display_name": f"Red {index}",
                    "corner": "red",
                },
                {
                    "fighter_id": f"fighter-{bout_id}-blue",
                    "display_name": f"Blue {index}",
                    "corner": "blue",
                },
            ],
            "scheduled_rounds": 3,
            "is_title_fight": False,
            "result_revision": 0,
            "result": None,
        },
    }


@pytest.fixture
async def card(test_db):
    await apply_mission_indexes(test_db)
    for collection in (
        "events",
        "bouts",
        "picks",
        "mission_assignments",
        "mission_offer_sets",
        "mission_xp_ledger",
        "mission_monthly_progress",
        "mission_monthly_configs",
        "mission_card_finalization_runs",
        "mission_evaluation_runs",
        "mission_celebrations",
        "event_card_slots",
    ):
        await test_db[collection].delete_many({})

    await test_db["events"].insert_one(
        {
            "id": EVENT_ID,
            "name": "UFC 401: Orchestration",
            "status": "scheduled",
            "date": EVENT_DATE,
            "card_revision": 1,
        }
    )
    await test_db["bouts"].insert_many([bout(index) for index in range(6)])
    await test_db["event_card_slots"].insert_many(
        [
            {
                "_id": f"{EVENT_ID}:{88100 + index}",
                "event_id": EVENT_ID,
                "bout_id": 88100 + index,
                "is_current": True,
                "card_section": "main" if index < 3 else "prelim",
                "order_overall": index + 1,
                "order_section": (index if index < 3 else index - 3) + 1,
                "role": "main_event"
                if index == 0
                else "co_main"
                if index == 1
                else "regular",
                "structure_revision": 1,
            }
            for index in range(6)
        ]
    )
    return EVENT_ID


async def resolve(test_db, index: int, *, winner: str = "red", revision: int = 1):
    bout_id = 88100 + index
    await test_db["bouts"].update_one(
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


async def pick_everything(test_db, *, correct: bool = True):
    await test_db["picks"].insert_many(
        [
            {
                "_id": f"{USER}:{88100 + index}",
                "user_id": USER,
                "event_id": EVENT_ID,
                "bout_id": 88100 + index,
                "picked_fighter_name": (
                    f"Red {index}" if correct else f"Blue {index}"
                ),
                "picked_fighter_id": (
                    f"fighter-{88100 + index}-red"
                    if correct
                    else f"fighter-{88100 + index}-blue"
                ),
                "picked_method": "KO/TKO",
                "picked_round": 1,
                "points_awarded": 0,
                "is_correct": None,
            }
            for index in range(6)
        ]
    )


async def select_auto_mission(test_db, slot_hint: str = "auto") -> str:
    reader = MissionReadService(test_db, offer_secret=OFFER_SECRET)
    home = await reader.home(user_id=USER, event_id=EVENT_ID)
    slot, offer = next(
        (slot.slot, option)
        for slot in home.slots
        for option in slot.options
        if option.interaction.value == "AUTO"
    )
    service = MissionSelectionService(test_db, load_card_catalog())
    result = await service.select(
        user_id=USER,
        command=SelectMissionCommand(
            event_id=EVENT_ID,
            slot=slot,
            offer_set_id=home.offer_set_id,
            offer_id=offer.offer_id,
            idempotency_key=f"orchestration-{slot_hint}-slot{slot}",
            selection={"kind": "AUTO"},
        ),
    )
    return result.assignment_id


async def test_a_result_moves_mission_progress_without_calling_the_evaluator(
    test_db, card
):
    await pick_everything(test_db)
    assignment_id = await select_auto_mission(test_db)
    await resolve(test_db, 0)

    trigger = MissionTriggerService(test_db)
    outcome = await trigger.on_bout_result(
        event_id=EVENT_ID, bout_id=88100, result_revision=1
    )

    assert outcome.errors == ()
    assert outcome.evaluated_assignments >= 1
    assignment = await test_db["mission_assignments"].find_one({"_id": assignment_id})
    assert assignment["progress"], "the evaluator never ran"
    assert assignment["revision"] > 1


async def test_the_card_does_not_finalize_while_bouts_are_unresolved(test_db, card):
    await pick_everything(test_db)
    await select_auto_mission(test_db)
    await resolve(test_db, 0)

    trigger = MissionTriggerService(test_db)
    outcome = await trigger.on_bout_result(
        event_id=EVENT_ID, bout_id=88100, result_revision=1
    )

    assert outcome.card_finalized is False
    assert (
        await test_db["mission_card_finalization_runs"].count_documents({}) == 0
    )


async def test_the_last_result_finalizes_the_card_exactly_once(test_db, card):
    await pick_everything(test_db)
    await select_auto_mission(test_db)
    trigger = MissionTriggerService(test_db)

    for index in range(6):
        await resolve(test_db, index)
        outcome = await trigger.on_bout_result(
            event_id=EVENT_ID, bout_id=88100 + index, result_revision=1
        )

    assert outcome.card_finalized is True
    assert outcome.errors == ()
    assert (
        await test_db["mission_card_finalization_runs"].count_documents({}) == 1
    )


async def test_replaying_the_final_trigger_does_not_duplicate_anything(test_db, card):
    await pick_everything(test_db)
    await select_auto_mission(test_db)
    trigger = MissionTriggerService(test_db)
    for index in range(6):
        await resolve(test_db, index)
        await trigger.on_bout_result(
            event_id=EVENT_ID, bout_id=88100 + index, result_revision=1
        )

    xp_before = await test_db["mission_xp_ledger"].count_documents({"user_id": USER})
    await trigger.on_bout_result(
        event_id=EVENT_ID, bout_id=88105, result_revision=1
    )
    xp_after = await test_db["mission_xp_ledger"].count_documents({"user_id": USER})

    assert xp_after == xp_before
    assert (
        await test_db["mission_card_finalization_runs"].count_documents({}) == 1
    )


async def test_finalization_folds_the_event_into_an_active_month(test_db, card):
    monthly_catalog = load_monthly_catalog()
    config = MonthlyConfigService(
        test_db,
        catalog=monthly_catalog,
        clock=lambda: EVENT_DATE - timedelta(days=20),
    )
    await config.create_draft(month_key="2026-08", mission_id="MONTH-V2-001")
    await MonthlyConfigService(
        test_db, catalog=monthly_catalog, clock=lambda: EVENT_DATE
    ).activate(month_key="2026-08")

    await pick_everything(test_db)
    await select_auto_mission(test_db)
    trigger = MissionTriggerService(test_db, clock=lambda: EVENT_DATE)
    for index in range(6):
        await resolve(test_db, index)
        outcome = await trigger.on_bout_result(
            event_id=EVENT_ID, bout_id=88100 + index, result_revision=1
        )

    assert outcome.card_finalized is True
    assert outcome.monthly_updates >= 1

    progress = await test_db["mission_monthly_progress"].find_one({"user_id": USER})
    assert progress is not None
    assert progress["month_key"] == "2026-08"
    summary = progress["event_summaries"][str(EVENT_ID)]
    assert summary["correct_winners"] == 6
    assert summary["wrong_winners"] == 0
    assert summary["resolved_bouts"] == 6


async def test_a_month_that_is_not_active_absorbs_nothing(test_db, card):
    await pick_everything(test_db)
    await select_auto_mission(test_db)
    trigger = MissionTriggerService(test_db, clock=lambda: EVENT_DATE)
    for index in range(6):
        await resolve(test_db, index)
        outcome = await trigger.on_bout_result(
            event_id=EVENT_ID, bout_id=88100 + index, result_revision=1
        )

    assert outcome.card_finalized is True
    assert outcome.monthly_updates == 0
    assert await test_db["mission_monthly_progress"].count_documents({}) == 0


async def test_wrong_picks_are_summarized_as_misses(test_db, card):
    monthly_catalog = load_monthly_catalog()
    await MonthlyConfigService(
        test_db, catalog=monthly_catalog, clock=lambda: EVENT_DATE - timedelta(days=20)
    ).create_draft(month_key="2026-08", mission_id="MONTH-V2-001")
    await MonthlyConfigService(
        test_db, catalog=monthly_catalog, clock=lambda: EVENT_DATE
    ).activate(month_key="2026-08")

    await pick_everything(test_db, correct=False)
    await select_auto_mission(test_db)
    trigger = MissionTriggerService(test_db, clock=lambda: EVENT_DATE)
    for index in range(6):
        await resolve(test_db, index)
        await trigger.on_bout_result(
            event_id=EVENT_ID, bout_id=88100 + index, result_revision=1
        )

    progress = await test_db["mission_monthly_progress"].find_one({"user_id": USER})
    summary = progress["event_summaries"][str(EVENT_ID)]
    assert summary["correct_winners"] == 0
    assert summary["wrong_winners"] == 6


async def test_an_unknown_bout_is_reported_not_raised(test_db, card):
    trigger = MissionTriggerService(test_db)

    outcome = await trigger.on_bout_result(
        event_id=EVENT_ID, bout_id=999999, result_revision=1
    )

    assert outcome.errors
    assert outcome.card_finalized is False
