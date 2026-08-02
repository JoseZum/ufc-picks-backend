"""Slice 1 acceptance: a result registered through the REAL route moves missions.

Nothing here calls the evaluator, the finalizer or the monthly service. The test
hits `PUT /admin/bouts/{id}/result` — the endpoint an admin actually uses — and
then asserts that mission state moved. If the trigger is ever unplugged, these
fail.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.modules.missions.application import MissionSelectionService
from app.modules.missions.application.read_models import MissionReadService
from app.modules.missions.catalog import load_card_catalog
from app.modules.missions.domain.selections import SelectMissionCommand
from app.modules.missions.indexes import apply_mission_indexes

EVENT_ID = 55001
FIRST_BOUT = 55101
OFFER_SECRET = b"result-trigger-tests-offer-secret-0000000000"


def canonical_bout(index: int, event_id: int = EVENT_ID) -> dict:
    bout_id = 55101 + index
    return {
        "_id": f"bout-{bout_id}",
        "id": bout_id,
        "event_id": event_id,
        "status": "scheduled",
        "fighters": {
            "red": {"fighter_name": f"Red {index}"},
            "blue": {"fighter_name": f"Blue {index}"},
        },
        "card_data_v1": {
            "bout_id": bout_id,
            "event_id": event_id,
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


def canonical_slot(index: int) -> dict:
    bout_id = 55101 + index
    return {
        "_id": f"{EVENT_ID}:{bout_id}",
        "event_id": EVENT_ID,
        "bout_id": bout_id,
        "is_current": True,
        "card_section": "main",
        "order_overall": index + 1,
        "order_section": index + 1,
        "role": "main_event" if index == 0 else "co_main" if index == 1 else "regular",
        "structure_revision": 1,
    }


ADMIN_ID = "mission-admin-user"
ADMIN_EMAIL = "mission-admin@example.com"


@pytest.fixture
async def admin_headers(client, test_db):
    """A dedicated admin.

    Deliberately NOT the `sample_user_data` account: `auth_headers` upserts that
    same user with `is_admin: False`, so sharing it makes admin calls 403
    depending on fixture resolution order.
    """
    from app.core.security import create_access_token

    await test_db["users"].update_one(
        {"_id": ADMIN_ID},
        {
            "$set": {
                "google_id": ADMIN_ID,
                "email": ADMIN_EMAIL,
                "name": "Mission Admin",
                "is_active": True,
                "is_admin": True,
                "created_at": datetime.now(UTC),
                "last_login_at": datetime.now(UTC),
            }
        },
        upsert=True,
    )
    token = create_access_token(ADMIN_ID, ADMIN_EMAIL)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def card(test_db, sample_event_data):
    await apply_mission_indexes(test_db)
    for collection in (
        "events",
        "bouts",
        "picks",
        "event_card_slots",
        "mission_assignments",
        "mission_offer_sets",
        "mission_xp_ledger",
        "mission_card_finalization_runs",
        "mission_evaluation_runs",
        "mission_celebrations",
    ):
        await test_db[collection].delete_many({})

    await test_db["events"].insert_one(
        {
            **sample_event_data,
            "id": EVENT_ID,
            "name": "UFC 403: Trigger",
            "slug": "ufc-403-trigger",
            "status": "scheduled",
            "date": datetime.now(UTC) + timedelta(days=2),
        }
    )
    await test_db["bouts"].insert_many([canonical_bout(index) for index in range(3)])
    await test_db["event_card_slots"].insert_many(
        [canonical_slot(index) for index in range(3)]
    )
    return EVENT_ID


async def select_auto(test_db, user_id: str) -> str:
    reader = MissionReadService(test_db, offer_secret=OFFER_SECRET)
    home = await reader.home(user_id=user_id, event_id=EVENT_ID)
    slot, offer = next(
        (slot.slot, option)
        for slot in home.slots
        for option in slot.options
        if option.interaction.value == "AUTO"
    )
    result = await MissionSelectionService(test_db, load_card_catalog()).select(
        user_id=user_id,
        command=SelectMissionCommand(
            event_id=EVENT_ID,
            slot=slot,
            offer_set_id=home.offer_set_id,
            offer_id=offer.offer_id,
            idempotency_key=f"trigger-test-slot-{slot}",
            selection={"kind": "AUTO"},
        ),
    )
    return result.assignment_id


def result_body(round_: int = 1) -> dict:
    return {"winner": "red", "method": "KO/TKO", "round": round_, "time": "1:23"}


async def test_registering_a_result_writes_the_canonical_projection(
    client, admin_headers, test_db, card
):
    response = await client.put(
        f"/admin/bouts/{FIRST_BOUT}/result",
        headers=admin_headers,
        json=result_body(),
    )

    assert response.status_code == 200, response.text
    bout = await test_db["bouts"].find_one({"id": FIRST_BOUT})
    # The legacy field the current API/UI reads is untouched in shape.
    assert bout["result"]["method"] == "KO/TKO"
    assert bout["status"] == "completed"
    # And the canonical projection the mission engine reads now exists.
    canonical = bout["card_data_v1"]["result"]
    assert canonical["revision"] == 1
    assert canonical["outcome"] == "red_win"
    assert canonical["winner_fighter_id"] == f"fighter-{FIRST_BOUT}-red"
    assert canonical["method_family"] == "ko_tko"


async def test_a_result_through_the_real_route_moves_mission_progress(
    client, admin_headers, test_db, card, sample_user_data
):
    user_id = sample_user_data["google_id"]
    assignment_id = await select_auto(test_db, user_id)

    response = await client.put(
        f"/admin/bouts/{FIRST_BOUT}/result",
        headers=admin_headers,
        json=result_body(),
    )

    assert response.status_code == 200, response.text
    assert response.json()["missions"]["triggered"] is True

    assignment = await test_db["mission_assignments"].find_one({"_id": assignment_id})
    assert assignment["progress"], "the trigger never reached the evaluator"
    assert assignment["revision"] > 1


async def test_resolving_the_last_bout_finalizes_the_card_through_the_route(
    client, admin_headers, test_db, card, sample_user_data
):
    await select_auto(test_db, sample_user_data["google_id"])

    for index in range(3):
        response = await client.put(
            f"/admin/bouts/{55101 + index}/result",
            headers=admin_headers,
            json=result_body(),
        )
        assert response.status_code == 200, response.text

    assert response.json()["missions"]["card_finalized"] is True
    assert await test_db["mission_card_finalization_runs"].count_documents({}) == 1


async def test_a_correction_bumps_the_canonical_revision(
    client, admin_headers, test_db, card
):
    await client.put(
        f"/admin/bouts/{FIRST_BOUT}/result", headers=admin_headers, json=result_body()
    )
    await client.put(
        f"/admin/bouts/{FIRST_BOUT}/result",
        headers=admin_headers,
        json={"winner": "blue", "method": "SUB", "round": 2, "time": "3:00"},
    )

    bout = await test_db["bouts"].find_one({"id": FIRST_BOUT})
    canonical = bout["card_data_v1"]["result"]

    assert canonical["revision"] == 2
    assert canonical["status"] == "corrected"
    assert canonical["outcome"] == "blue_win"
    assert canonical["winner_fighter_id"] == f"fighter-{FIRST_BOUT}-blue"
    assert canonical["method_family"] == "submission"


async def test_a_legacy_bout_without_canonical_data_still_registers_its_result(
    client, admin_headers, test_db, card
):
    """Missions are additive: a pre-boundary bout must not fail an Admin write."""
    await test_db["bouts"].update_one(
        {"id": FIRST_BOUT}, {"$unset": {"card_data_v1": ""}}
    )

    response = await client.put(
        f"/admin/bouts/{FIRST_BOUT}/result",
        headers=admin_headers,
        json=result_body(),
    )

    assert response.status_code == 200, response.text
    assert response.json()["missions"]["triggered"] is False
    bout = await test_db["bouts"].find_one({"id": FIRST_BOUT})
    assert bout["result"]["method"] == "KO/TKO"
    assert bout["status"] == "completed"


async def test_a_draw_projects_without_a_winner(client, admin_headers, test_db, card):
    response = await client.put(
        f"/admin/bouts/{FIRST_BOUT}/result",
        headers=admin_headers,
        json={"winner": "draw", "method": "DEC", "round": 3, "time": "5:00"},
    )

    assert response.status_code == 200, response.text
    canonical = (await test_db["bouts"].find_one({"id": FIRST_BOUT}))["card_data_v1"][
        "result"
    ]
    assert canonical["outcome"] == "draw"
    assert canonical["winner_fighter_id"] is None


# ---------------------------------------------------------------- card streak


async def _pick(test_db, user_id: str, bout_indexes) -> None:
    if not bout_indexes:
        return
    await test_db["picks"].insert_many(
        [
            {
                "_id": f"{user_id}:{55101 + index}",
                "user_id": user_id,
                "event_id": EVENT_ID,
                "bout_id": 55101 + index,
                "picked_fighter_name": f"Red {index}",
            }
            for index in bout_indexes
        ]
    )


async def test_the_first_result_settles_the_card_streak(
    client, admin_headers, test_db, card
):
    """A registered result means picks closed — that is when STREAK-001 runs."""
    await _pick(test_db, "covered", [0, 1])  # 2 of 3 is more than half

    response = await client.put(
        f"/admin/bouts/{FIRST_BOUT}/result", headers=admin_headers, json=result_body()
    )

    assert response.status_code == 200, response.text
    streak = await test_db["mission_card_streaks"].find_one({"user_id": "covered"})
    assert streak["current"] == 1
    assert streak["best"] == 1
    assert (
        await test_db["mission_xp_ledger"].count_documents(
            {"user_id": "covered", "source_type": "CARD_STREAK"}
        )
        == 1
    )


async def test_a_thin_card_breaks_the_streak_of_an_absent_user(
    client, admin_headers, test_db, card
):
    await test_db["mission_card_streaks"].insert_one(
        {"user_id": "absent", "current": 4, "best": 9}
    )

    await client.put(
        f"/admin/bouts/{FIRST_BOUT}/result", headers=admin_headers, json=result_body()
    )

    streak = await test_db["mission_card_streaks"].find_one({"user_id": "absent"})
    assert streak["current"] == 0
    assert streak["best"] == 9


async def test_every_further_result_leaves_the_streak_untouched(
    client, admin_headers, test_db, card
):
    """Three results on one card must still be a single advance."""
    await _pick(test_db, "covered", [0, 1, 2])

    for index in range(3):
        response = await client.put(
            f"/admin/bouts/{55101 + index}/result",
            headers=admin_headers,
            json=result_body(),
        )
        assert response.status_code == 200, response.text

    streak = await test_db["mission_card_streaks"].find_one({"user_id": "covered"})
    assert streak["current"] == 1
    assert (
        await test_db["mission_xp_ledger"].count_documents(
            {"user_id": "covered", "source_type": "CARD_STREAK"}
        )
        == 1
    )


async def test_the_denominator_ignores_a_bout_cancelled_before_the_freeze(
    client, admin_headers, test_db, card
):
    """One pick out of two surviving bouts is not more than half."""
    await test_db["bouts"].update_one(
        {"id": 55103},
        {"$set": {"card_data_v1.lifecycle": "CANCELLED", "card_data_v1.is_current": False}},
    )
    await _pick(test_db, "partial", [0])

    await client.put(
        f"/admin/bouts/{FIRST_BOUT}/result", headers=admin_headers, json=result_body()
    )

    frozen = await test_db["mission_card_streak_denominators"].find_one({"_id": EVENT_ID})
    assert frozen["denominator"] == 2
    assert await test_db["mission_card_streaks"].count_documents(
        {"user_id": "partial", "current": {"$gt": 0}}
    ) == 0


async def test_the_streak_reaches_the_profile_endpoint(
    client, admin_headers, auth_headers, test_db, card, sample_user_data
):
    user_id = sample_user_data["google_id"]
    await _pick(test_db, user_id, [0, 1, 2])
    await client.put(
        f"/admin/bouts/{FIRST_BOUT}/result", headers=admin_headers, json=result_body()
    )

    profile = await client.get("/missions/profile", headers=auth_headers)

    assert profile.status_code == 200, profile.text
    body = profile.json()
    assert body["current_streak"] == 1
    assert body["best_streak"] == 1
    assert body["lifetime_xp"] >= 1
    row = next(row for row in body["streak_history"] if row["event_id"] == EVENT_ID)
    assert row["outcome"] == "ADVANCED"
    assert row["event_label"] == "UFC 403: Trigger"
    assert row["denominator"] == 3 and row["picked"] == 3
