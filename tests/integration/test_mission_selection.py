"""Transactional mission selection and canonical-pick synchronization."""

from datetime import UTC, datetime

import pytest

from app.modules.missions.application import (
    MissionSelectionError,
    MissionSelectionErrorCode,
    MissionSelectionService,
)
from app.modules.missions.catalog import load_card_catalog
from app.modules.missions.domain import SelectMissionCommand, load_mission_catalog
from app.modules.missions.indexes import apply_mission_indexes

NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)
EVENT_ID = 4242
OFFER_SET_ID = "offer_set_aaaaaaaaaaaaaaaa"


def common_definition(mission_id, difficulty, interaction, **overrides):
    value = {
        "mission_id": mission_id,
        "catalog_version": "2026.08.01",
        "difficulty": difficulty,
        "xp": {"EASY": 1, "MEDIUM": 4, "HARD": 8}[difficulty],
        "ui": {
            "name": mission_id,
            "description": "Choose the required mission target.",
            "progress_template": "{current} / {target}",
            "selection_prompt": "Choose your mission target",
        },
        "evaluation": {
            "metric": "selected_fighter_win",
            "comparator": "GTE",
            "target": 1,
            "evaluate_on": ["AFTER_EACH_RESULT", "CARD_FINALIZED"],
        },
        "eligibility": {
            "min_eligible_bouts": 1,
            "capabilities": ["CANONICAL_CARD"],
        },
        "compatibility": "REQUIRES_UI",
        "overlap_tags": ["selection", mission_id.lower()],
        "interaction": interaction,
    }
    value.update(overrides)
    return value


def mission_catalog():
    return load_mission_catalog(
        [
            common_definition(
                "CARD-V2-M-002",
                "MEDIUM",
                "TARGET_FIGHTER",
                pick_effect="UPSERT_ONE",
                selection={
                    "bound_pick_fields": ["WINNER", "METHOD"],
                    "allowed_methods": ["SUBMISSION"],
                },
            ),
            common_definition(
                "CARD-V2-H-003",
                "HARD",
                "TARGET_FIGHTER",
                pick_effect="UPSERT_ONE",
                selection={
                    "bound_pick_fields": ["WINNER", "METHOD", "ROUND"],
                    "allowed_methods": ["KO_TKO", "SUBMISSION"],
                    "allowed_rounds": [1, 2, 3],
                },
            ),
            common_definition(
                "CARD-V2-E-001",
                "EASY",
                "TARGET_FIGHTER",
                pick_effect="UPSERT_ONE",
                selection={"bound_pick_fields": ["WINNER"]},
            ),
            common_definition(
                "CARD-V2-H-007",
                "HARD",
                "COMBO_BUILDER",
                pick_effect="UPSERT_MANY",
                selection={
                    "leg_count": 2,
                    "legs": [
                        {
                            "key": "ko",
                            "label": "KO/TKO",
                            "target": "FIGHTER",
                            "method": "KO_TKO",
                        },
                        {
                            "key": "dec",
                            "label": "Decision",
                            "target": "FIGHTER",
                            "method": "DECISION",
                        },
                    ],
                },
            ),
        ],
        expected_version="2026.08.01",
    )


def event_document():
    return {
        "_id": "event-document",
        "id": EVENT_ID,
        "name": "Test Card",
        "status": "scheduled",
        "card_data_v1": {
            "card_revision": 7,
            "current_eligibility": {"denominator": 8},
        },
    }


def bout_document(bout_id, red, blue):
    return {
        "_id": f"bout-{bout_id}",
        "id": bout_id,
        "event_id": EVENT_ID,
        "status": "scheduled",
        "rounds_scheduled": 3,
        "fighters": {
            "red": {"fighter_name": red},
            "blue": {"fighter_name": blue},
        },
        "result": None,
    }


def offer_set_document(*offers, owner="jose"):
    return {
        "_id": OFFER_SET_ID,
        "user_id": owner,
        "event_id": EVENT_ID,
        "card_revision": 7,
        "catalog_version": "2026.08.01",
        "slots": [
            {
                "slot": 1,
                "offers": [
                    {"offer_id": offer_id, "mission_id": mission_id}
                    for offer_id, mission_id in offers
                ],
            }
        ],
        "created_at": NOW,
    }


def command(offer_id, selection, *, key="select-command-001", patches=()):
    return SelectMissionCommand(
        event_id=EVENT_ID,
        slot=1,
        offer_set_id=OFFER_SET_ID,
        offer_id=offer_id,
        idempotency_key=key,
        selection=selection,
        pick_patches=patches,
    )


@pytest.fixture
async def mission_db(test_db):
    await apply_mission_indexes(test_db)
    await test_db["events"].insert_one(event_document())
    await test_db["bouts"].insert_many(
        [
            bout_document(101, "Red One", "Blue One"),
            bout_document(102, "Red Two", "Blue Two"),
            bout_document(103, "Red Three", "Blue Three"),
        ]
    )
    return test_db


def service(db):
    return MissionSelectionService(db, mission_catalog(), clock=lambda: NOW)


def reviewed_catalog_service(db):
    return MissionSelectionService(db, load_card_catalog(), clock=lambda: NOW)


@pytest.mark.asyncio
async def test_target_fighter_selection_replaces_existing_pick_atomically(mission_db):
    offer_id = "offer_bbbbbbbbbbbbbbbb"
    await mission_db["mission_offer_sets"].insert_one(
        offer_set_document((offer_id, "CARD-V2-M-002"))
    )
    await mission_db["picks"].insert_one(
        {
            "_id": "jose:101",
            "user_id": "jose",
            "event_id": EVENT_ID,
            "bout_id": 101,
            "picked_fighter_name": "Red One",
            "picked_method": "DEC",
            "picked_round": None,
            "is_correct": None,
            "points_awarded": 0,
            "locked": False,
            "created_at": NOW,
        }
    )

    result = await service(mission_db).select(
        user_id="jose",
        command=command(
            offer_id,
            {"kind": "TARGET_FIGHTER", "bout_id": 101, "corner": "blue"},
            patches=({"bout_id": 101, "round": 2},),
        ),
    )

    pick = await mission_db["picks"].find_one({"_id": "jose:101"})
    assignment = await mission_db["mission_assignments"].find_one({"_id": result.assignment_id})
    assert pick["picked_fighter_name"] == "Blue One"
    assert pick["picked_method"] == "SUB"
    assert pick["picked_round"] == 2
    assert pick["revision"] == 1
    assert pick["mission_assignment_ids"] == [result.assignment_id]
    assert pick["mission_field_locks"] == {
        "winner": [result.assignment_id],
        "method": [result.assignment_id],
    }
    assert assignment["linked_pick_ids"] == ["jose:101"]
    assert assignment["selection"]["selected_fighter_name"] == "Blue One"


@pytest.mark.asyncio
async def test_mission_pick_name_and_id_come_from_the_same_carddata_fighter(mission_db):
    offer_id = "offer_caddada1caddada1"
    await mission_db["mission_offer_sets"].insert_one(
        offer_set_document((offer_id, "CARD-V2-E-001"))
    )
    await mission_db["bouts"].update_one(
        {"id": 101},
        {
            "$set": {
                "card_data_v1": {
                    "bout_id": 101,
                    "matchup_revision": 1,
                    "scheduled_rounds": 3,
                    "fighters": [
                        {
                            "fighter_id": "fighter-101-red",
                            "display_name": "Canonical Red",
                            "corner": "red",
                        },
                        {
                            "fighter_id": "fighter-101-blue",
                            "display_name": "Canonical Blue",
                            "corner": "blue",
                        },
                    ],
                }
            }
        },
    )

    result = await service(mission_db).select(
        user_id="jose",
        command=command(
            offer_id,
            {"kind": "TARGET_FIGHTER", "bout_id": 101, "corner": "blue"},
            patches=({"bout_id": 101, "method": "DECISION"},),
        ),
    )

    pick = await mission_db["picks"].find_one({"_id": "jose:101"})
    stored = await mission_db["mission_assignments"].find_one(
        {"_id": result.assignment_id}
    )
    assert pick["picked_fighter_name"] == "Canonical Blue"
    assert pick["picked_fighter_id"] == "fighter-101-blue"
    assert stored["selection"]["selected_fighter_name"] == "Canonical Blue"
    assert stored["selection"]["selected_fighter_id"] == "fighter-101-blue"


@pytest.mark.asyncio
async def test_reviewed_fade_lock_selects_loser_but_writes_opponent_as_pick_winner(
    mission_db,
):
    offer_id = "offer_fafafafafafafafa"
    await mission_db["mission_offer_sets"].insert_one(
        offer_set_document((offer_id, "CARD-V2-E-002"))
    )
    await mission_db["picks"].insert_one(
        {
            "_id": "jose:101",
            "user_id": "jose",
            "event_id": EVENT_ID,
            "bout_id": 101,
            "picked_fighter_name": "Red One",
            "picked_method": "KO/TKO",
            "picked_round": 2,
            "is_correct": None,
            "points_awarded": 0,
            "locked": False,
            "created_at": NOW,
        }
    )

    result = await reviewed_catalog_service(mission_db).select(
        user_id="jose",
        command=command(
            offer_id,
            {"kind": "TARGET_FIGHTER", "bout_id": 101, "corner": "red"},
            patches=({"bout_id": 101, "method": "DECISION"},),
        ),
    )

    pick = await mission_db["picks"].find_one({"_id": "jose:101"})
    assignment = await mission_db["mission_assignments"].find_one({"_id": result.assignment_id})
    assert assignment["selection"]["selected_fighter_name"] == "Red One"
    assert assignment["selection"]["canonical_winner_name"] == "Blue One"
    assert pick["picked_fighter_name"] == "Blue One"
    assert pick["picked_method"] == "DEC"
    assert pick["picked_round"] is None


@pytest.mark.asyncio
async def test_explicit_canonical_non_title_cannot_be_overridden_by_legacy_true(
    mission_db,
):
    offer_id = "offer_cdcdcdcdcdcdcdcd"
    await mission_db["mission_offer_sets"].insert_one(
        offer_set_document((offer_id, "CARD-V2-H-015"))
    )
    await mission_db["bouts"].update_one(
        {"id": 101},
        {
            "$set": {
                "is_title_fight": True,
                "card_data_v1": {
                    "is_title_fight": False,
                    "scheduled_rounds": 3,
                },
            }
        },
    )

    with pytest.raises(MissionSelectionError) as exc_info:
        await reviewed_catalog_service(mission_db).select(
            user_id="jose",
            command=command(
                offer_id,
                {
                    "kind": "TARGET_FIGHTER",
                    "bout_id": 101,
                    "corner": "red",
                    "method": "SUBMISSION",
                },
            ),
        )

    assert exc_info.value.code == MissionSelectionErrorCode.INVALID_SELECTION
    assert await mission_db["picks"].count_documents({}) == 0
    assert await mission_db["mission_assignments"].count_documents({}) == 0


@pytest.mark.asyncio
async def test_reviewed_target_fight_selection_does_not_touch_existing_pick(
    mission_db,
):
    offer_id = "offer_efefefefefefefef"
    await mission_db["mission_offer_sets"].insert_one(
        offer_set_document((offer_id, "CARD-V2-M-001"))
    )
    existing_pick = {
        "_id": "jose:101",
        "user_id": "jose",
        "event_id": EVENT_ID,
        "bout_id": 101,
        "picked_fighter_name": "Red One",
        "picked_method": "KO/TKO",
        "picked_round": 2,
        "is_correct": None,
        "points_awarded": 0,
        "locked": False,
        "created_at": NOW,
    }
    await mission_db["picks"].insert_one(existing_pick)

    result = await reviewed_catalog_service(mission_db).select(
        user_id="jose",
        command=command(
            offer_id,
            {"kind": "TARGET_FIGHT", "bout_id": 101},
        ),
    )

    assignment = await mission_db["mission_assignments"].find_one({"_id": result.assignment_id})
    persisted_pick = await mission_db["picks"].find_one({"_id": "jose:101"})
    assert result.linked_pick_ids == ()
    assert assignment["selection"]["bout_ids"] == [101]
    assert set(persisted_pick) == set(existing_pick)
    for field in {
        "picked_fighter_name",
        "picked_method",
        "picked_round",
        "points_awarded",
        "locked",
    }:
        assert persisted_pick[field] == existing_pick[field]


@pytest.mark.asyncio
async def test_exact_script_creates_complete_pick_without_separate_write(mission_db):
    offer_id = "offer_cccccccccccccccc"
    await mission_db["mission_offer_sets"].insert_one(
        offer_set_document((offer_id, "CARD-V2-H-003"))
    )

    result = await service(mission_db).select(
        user_id="jose",
        command=command(
            offer_id,
            {
                "kind": "TARGET_FIGHTER",
                "bout_id": 101,
                "corner": "red",
                "method": "KO_TKO",
                "round": 2,
            },
        ),
    )

    pick = await mission_db["picks"].find_one({"_id": "jose:101"})
    assert result.linked_pick_ids == ("jose:101",)
    assert pick["picked_fighter_name"] == "Red One"
    assert pick["picked_method"] == "KO/TKO"
    assert pick["picked_round"] == 2


@pytest.mark.asyncio
async def test_exact_script_rejects_round_beyond_bout_schedule(mission_db):
    offer_id = "offer_abababababababab"
    await mission_db["mission_offer_sets"].insert_one(
        offer_set_document((offer_id, "CARD-V2-H-003"))
    )

    with pytest.raises(MissionSelectionError) as exc_info:
        await service(mission_db).select(
            user_id="jose",
            command=command(
                offer_id,
                {
                    "kind": "TARGET_FIGHTER",
                    "bout_id": 101,
                    "corner": "red",
                    "method": "KO_TKO",
                    "round": 5,
                },
            ),
        )

    assert exc_info.value.code == MissionSelectionErrorCode.INVALID_SELECTION
    assert await mission_db["picks"].count_documents({}) == 0
    assert await mission_db["mission_assignments"].count_documents({}) == 0


@pytest.mark.asyncio
async def test_unbound_pick_fields_can_be_completed_in_same_command(mission_db):
    offer_id = "offer_dddddddddddddddd"
    await mission_db["mission_offer_sets"].insert_one(
        offer_set_document((offer_id, "CARD-V2-E-001"))
    )

    await service(mission_db).select(
        user_id="jose",
        command=command(
            offer_id,
            {"kind": "TARGET_FIGHTER", "bout_id": 101, "corner": "blue"},
            patches=({"bout_id": 101, "method": "DECISION"},),
        ),
    )

    pick = await mission_db["picks"].find_one({"_id": "jose:101"})
    assert pick["picked_fighter_name"] == "Blue One"
    assert pick["picked_method"] == "DEC"


@pytest.mark.asyncio
async def test_conflicting_pick_patch_rolls_back_assignment_and_pick(mission_db):
    offer_id = "offer_eeeeeeeeeeeeeeee"
    await mission_db["mission_offer_sets"].insert_one(
        offer_set_document((offer_id, "CARD-V2-M-002"))
    )
    original = {
        "_id": "jose:101",
        "user_id": "jose",
        "event_id": EVENT_ID,
        "bout_id": 101,
        "picked_fighter_name": "Red One",
        "picked_method": "DEC",
        "picked_round": None,
        "locked": False,
        "created_at": NOW,
    }
    await mission_db["picks"].insert_one(original)

    with pytest.raises(MissionSelectionError) as raised:
        await service(mission_db).select(
            user_id="jose",
            command=command(
                offer_id,
                {"kind": "TARGET_FIGHTER", "bout_id": 101, "corner": "blue"},
                patches=({"bout_id": 101, "winner_corner": "red"},),
            ),
        )

    assert raised.value.code == MissionSelectionErrorCode.INVALID_SELECTION
    assert await mission_db["mission_assignments"].count_documents({}) == 0
    stored = await mission_db["picks"].find_one({"_id": "jose:101"})
    assert stored["picked_fighter_name"] == "Red One"
    assert "revision" not in stored


@pytest.mark.asyncio
async def test_first_result_locks_all_mission_selection(mission_db):
    offer_id = "offer_ffffffffffffffff"
    await mission_db["mission_offer_sets"].insert_one(
        offer_set_document((offer_id, "CARD-V2-H-003"))
    )
    await mission_db["bouts"].update_one(
        {"id": 102},
        {"$set": {"result": {"winner_name": "Red Two"}}},
    )

    with pytest.raises(MissionSelectionError) as raised:
        await service(mission_db).select(
            user_id="jose",
            command=command(
                offer_id,
                {
                    "kind": "TARGET_FIGHTER",
                    "bout_id": 101,
                    "corner": "red",
                    "method": "KO_TKO",
                    "round": 1,
                },
            ),
        )

    assert raised.value.code == MissionSelectionErrorCode.CARD_LOCKED
    assert await mission_db["mission_assignments"].count_documents({}) == 0
    assert await mission_db["picks"].count_documents({}) == 0


@pytest.mark.asyncio
async def test_selection_is_idempotent_but_key_reuse_with_new_payload_fails(mission_db):
    offer_id = "offer_1111111111111111"
    await mission_db["mission_offer_sets"].insert_one(
        offer_set_document((offer_id, "CARD-V2-H-003"))
    )
    selected = command(
        offer_id,
        {
            "kind": "TARGET_FIGHTER",
            "bout_id": 101,
            "corner": "red",
            "method": "KO_TKO",
            "round": 1,
        },
    )

    first = await service(mission_db).select(user_id="jose", command=selected)
    retry = await service(mission_db).select(user_id="jose", command=selected)

    assert retry == first
    assert await mission_db["mission_assignments"].count_documents({}) == 1
    assert (await mission_db["picks"].find_one({"_id": "jose:101"}))["revision"] == 1

    changed = command(
        offer_id,
        {
            "kind": "TARGET_FIGHTER",
            "bout_id": 101,
            "corner": "blue",
            "method": "KO_TKO",
            "round": 1,
        },
    )
    with pytest.raises(MissionSelectionError) as raised:
        await service(mission_db).select(user_id="jose", command=changed)
    assert raised.value.code == MissionSelectionErrorCode.IDEMPOTENCY_CONFLICT


@pytest.mark.asyncio
async def test_combo_upserts_every_bound_pick_in_one_transaction(mission_db):
    offer_id = "offer_2222222222222222"
    await mission_db["mission_offer_sets"].insert_one(
        offer_set_document((offer_id, "CARD-V2-H-007"))
    )

    result = await service(mission_db).select(
        user_id="jose",
        command=command(
            offer_id,
            {
                "kind": "COMBO_BUILDER",
                "legs": [
                    {"key": "ko", "bout_id": 101, "corner": "red"},
                    {"key": "dec", "bout_id": 102, "corner": "blue"},
                ],
            },
            patches=({"bout_id": 101, "round": 1},),
        ),
    )

    picks = await mission_db["picks"].find({"user_id": "jose"}).to_list(length=None)
    assert result.linked_pick_ids == ("jose:101", "jose:102")
    assert {(pick["bout_id"], pick["picked_method"]) for pick in picks} == {
        (101, "KO/TKO"),
        (102, "DEC"),
    }


@pytest.mark.asyncio
async def test_method_pair_persists_distinct_selected_methods_and_locks_them(
    mission_db,
):
    offer_id = "offer_2424242424242424"
    await mission_db["mission_offer_sets"].insert_one(
        offer_set_document((offer_id, "CARD-V2-M-013"))
    )

    result = await reviewed_catalog_service(mission_db).select(
        user_id="jose",
        command=command(
            offer_id,
            {
                "kind": "COMBO_BUILDER",
                "legs": [
                    {
                        "key": "method_one",
                        "bout_id": 101,
                        "corner": "red",
                        "method": "KO_TKO",
                    },
                    {
                        "key": "method_two",
                        "bout_id": 102,
                        "corner": "blue",
                        "method": "DECISION",
                    },
                ],
            },
            patches=({"bout_id": 101, "round": 2},),
        ),
    )

    assignment = await mission_db["mission_assignments"].find_one({"_id": result.assignment_id})
    picks = await mission_db["picks"].find({"user_id": "jose"}).to_list(length=None)
    assert [leg["method"] for leg in assignment["selection"]["legs"]] == [
        "KO_TKO",
        "DECISION",
    ]
    assert all("method" in pick["mission_field_locks"] for pick in picks)


@pytest.mark.asyncio
async def test_method_pair_rejects_duplicate_methods_before_any_write(mission_db):
    offer_id = "offer_2525252525252525"
    await mission_db["mission_offer_sets"].insert_one(
        offer_set_document((offer_id, "CARD-V2-M-013"))
    )

    with pytest.raises(MissionSelectionError) as raised:
        await reviewed_catalog_service(mission_db).select(
            user_id="jose",
            command=command(
                offer_id,
                {
                    "kind": "COMBO_BUILDER",
                    "legs": [
                        {
                            "key": "method_one",
                            "bout_id": 101,
                            "corner": "red",
                            "method": "DECISION",
                        },
                        {
                            "key": "method_two",
                            "bout_id": 102,
                            "corner": "blue",
                            "method": "DECISION",
                        },
                    ],
                },
            ),
        )

    assert raised.value.code == MissionSelectionErrorCode.INVALID_SELECTION
    assert await mission_db["mission_assignments"].count_documents({}) == 0
    assert await mission_db["picks"].count_documents({}) == 0


@pytest.mark.asyncio
async def test_round_ladder_collects_methods_but_only_locks_winner_and_round(
    mission_db,
):
    offer_id = "offer_2626262626262626"
    await mission_db["mission_offer_sets"].insert_one(
        offer_set_document((offer_id, "CARD-V2-H-010"))
    )

    result = await reviewed_catalog_service(mission_db).select(
        user_id="jose",
        command=command(
            offer_id,
            {
                "kind": "COMBO_BUILDER",
                "legs": [
                    {"key": "round_one", "bout_id": 101, "corner": "red"},
                    {"key": "round_two", "bout_id": 102, "corner": "blue"},
                    {"key": "round_three", "bout_id": 103, "corner": "red"},
                ],
            },
            patches=(
                {"bout_id": 101, "method": "KO_TKO"},
                {"bout_id": 102, "method": "SUBMISSION"},
                {"bout_id": 103, "method": "KO_TKO"},
            ),
        ),
    )

    picks = await mission_db["picks"].find({"user_id": "jose"}).to_list(length=None)
    assert result.linked_pick_ids == ("jose:101", "jose:102", "jose:103")
    assert {pick["picked_round"] for pick in picks} == {1, 2, 3}
    assert all(set(pick["mission_field_locks"]) == {"winner", "round"} for pick in picks)


@pytest.mark.asyncio
async def test_double_gold_rejects_non_title_legs_before_pick_writes(mission_db):
    offer_id = "offer_2727272727272727"
    await mission_db["mission_offer_sets"].insert_one(
        offer_set_document((offer_id, "CARD-V3-H-015"))
    )
    await mission_db["bouts"].update_one(
        {"id": 101},
        {"$set": {"card_data_v1": {"is_title_fight": True, "scheduled_rounds": 3}}},
    )

    with pytest.raises(MissionSelectionError) as raised:
        await reviewed_catalog_service(mission_db).select(
            user_id="jose",
            command=command(
                offer_id,
                {
                    "kind": "COMBO_BUILDER",
                    "legs": [
                        {
                            "key": "title_one",
                            "bout_id": 101,
                            "corner": "red",
                            "method": "DECISION",
                        },
                        {
                            "key": "title_two",
                            "bout_id": 102,
                            "corner": "blue",
                            "method": "SUBMISSION",
                        },
                    ],
                },
                patches=({"bout_id": 102, "round": 2},),
            ),
        )

    assert raised.value.code == MissionSelectionErrorCode.INVALID_SELECTION
    assert await mission_db["mission_assignments"].count_documents({}) == 0
    assert await mission_db["picks"].count_documents({}) == 0


@pytest.mark.asyncio
async def test_winner_double_does_not_lock_unbound_method_or_round(mission_db):
    offer_id = "offer_2828282828282828"
    await mission_db["mission_offer_sets"].insert_one(
        offer_set_document((offer_id, "CARD-V2-M-011"))
    )

    await reviewed_catalog_service(mission_db).select(
        user_id="jose",
        command=command(
            offer_id,
            {
                "kind": "COMBO_BUILDER",
                "legs": [
                    {"key": "winner_one", "bout_id": 101, "corner": "red"},
                    {"key": "winner_two", "bout_id": 102, "corner": "blue"},
                ],
            },
            patches=(
                {"bout_id": 101, "method": "DECISION"},
                {"bout_id": 102, "method": "KO_TKO", "round": 2},
            ),
        ),
    )

    picks = await mission_db["picks"].find({"user_id": "jose"}).to_list(length=None)
    assert all(set(pick["mission_field_locks"]) == {"winner"} for pick in picks)


@pytest.mark.asyncio
async def test_displayed_card_prop_target_is_frozen_from_canonical_card_size(
    mission_db,
):
    offer_id = "offer_2929292929292929"
    await mission_db["mission_offer_sets"].insert_one(
        offer_set_document((offer_id, "CARD-V2-M-017"))
    )

    result = await reviewed_catalog_service(mission_db).select(
        user_id="jose",
        command=command(offer_id, {"kind": "CARD_PROP"}),
    )

    assignment = await mission_db["mission_assignments"].find_one({"_id": result.assignment_id})
    assert assignment["selection"]["eligible_bout_count"] == 8
    assert assignment["selection"]["frozen_target"] == 4
    assert assignment["selection"]["frozen_max_count"] is None
    assert result.linked_pick_ids == ()


@pytest.mark.asyncio
async def test_exact_card_prop_rejects_count_above_frozen_eligible_max(mission_db):
    offer_id = "offer_3030303030303030"
    await mission_db["mission_offer_sets"].insert_one(
        offer_set_document((offer_id, "CARD-V2-H-019"))
    )

    with pytest.raises(MissionSelectionError) as raised:
        await reviewed_catalog_service(mission_db).select(
            user_id="jose",
            command=command(
                offer_id,
                {"kind": "CARD_PROP", "exact_count": 9},
            ),
        )

    assert raised.value.code == MissionSelectionErrorCode.INVALID_SELECTION
    assert await mission_db["mission_assignments"].count_documents({}) == 0


@pytest.mark.asyncio
async def test_exact_card_prop_persists_selected_target_and_frozen_max(mission_db):
    offer_id = "offer_3131313131313131"
    await mission_db["mission_offer_sets"].insert_one(
        offer_set_document((offer_id, "CARD-V3-H-013"))
    )

    result = await reviewed_catalog_service(mission_db).select(
        user_id="jose",
        command=command(
            offer_id,
            {"kind": "CARD_PROP", "exact_count": 3},
        ),
    )

    assignment = await mission_db["mission_assignments"].find_one({"_id": result.assignment_id})
    assert assignment["selection"]["exact_count"] == 3
    assert assignment["selection"]["frozen_max_count"] == 8
    assert assignment["selection"]["frozen_target"] is None


@pytest.mark.asyncio
async def test_locked_combo_leg_aborts_other_pick_write(mission_db):
    offer_id = "offer_3333333333333333"
    await mission_db["mission_offer_sets"].insert_one(
        offer_set_document((offer_id, "CARD-V2-H-007"))
    )
    await mission_db["bouts"].update_one(
        {"id": 102},
        {"$set": {"picks_lock_override": "locked"}},
    )

    with pytest.raises(MissionSelectionError) as raised:
        await service(mission_db).select(
            user_id="jose",
            command=command(
                offer_id,
                {
                    "kind": "COMBO_BUILDER",
                    "legs": [
                        {"key": "ko", "bout_id": 101, "corner": "red"},
                        {"key": "dec", "bout_id": 102, "corner": "blue"},
                    ],
                },
            ),
        )

    assert raised.value.code == MissionSelectionErrorCode.PICK_LOCKED
    assert await mission_db["picks"].find_one({"_id": "jose:101"}) is None
    assert await mission_db["mission_assignments"].count_documents({}) == 0


@pytest.mark.asyncio
async def test_offer_ownership_and_one_selection_per_slot_are_enforced(mission_db):
    offer_id = "offer_4444444444444444"
    await mission_db["mission_offer_sets"].insert_one(
        offer_set_document((offer_id, "CARD-V2-E-001"), owner="chris")
    )
    selected = command(
        offer_id,
        {"kind": "TARGET_FIGHTER", "bout_id": 101, "corner": "red"},
        patches=({"bout_id": 101, "method": "DECISION"},),
    )
    with pytest.raises(MissionSelectionError) as raised:
        await service(mission_db).select(user_id="jose", command=selected)
    assert raised.value.code == MissionSelectionErrorCode.OFFER_NOT_FOUND

    await mission_db["mission_offer_sets"].update_one(
        {"_id": OFFER_SET_ID},
        {"$set": {"user_id": "jose"}},
    )
    await service(mission_db).select(user_id="jose", command=selected)
    with pytest.raises(MissionSelectionError) as raised:
        await service(mission_db).select(
            user_id="jose",
            command=command(
                offer_id,
                {"kind": "TARGET_FIGHTER", "bout_id": 101, "corner": "red"},
                key="select-command-002",
                patches=({"bout_id": 101, "method": "DECISION"},),
            ),
        )
    assert raised.value.code == MissionSelectionErrorCode.ALREADY_SELECTED
