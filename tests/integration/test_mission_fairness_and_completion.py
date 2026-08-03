"""Two questions Jose asked out loud, answered by running them.

1. Does everyone get the same three missions? They must not — the draw is
   personalised per user — but it must also be stable for one user, or a
   refresh would reroll what they are about to choose.
2. Do the missions actually complete? Not "does the evaluator unit-test pass",
   but: accept one through the real route, register results that plainly
   satisfy it, and check the user ends up COMPLETED with the XP in the ledger.

One mission per interaction family, driven through `/missions/*` and the real
Admin result route. The all-85 conformance suite proves the catalog's rules;
this proves the wiring around them.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.security import create_access_token
from app.modules.missions.application.orchestration import MissionTriggerService
from app.modules.missions.indexes import apply_mission_indexes

EVENT_ID = 96001
BOUT_BASE = EVENT_ID * 10
# Eight bouts: enough for a full three-slot card and for combo missions.
FIGHTERS = [
    ("Red One", "Blue One"), ("Red Two", "Blue Two"),
    ("Red Three", "Blue Three"), ("Red Four", "Blue Four"),
    ("Red Five", "Blue Five"), ("Red Six", "Blue Six"),
    ("Red Seven", "Blue Seven"), ("Red Eight", "Blue Eight"),
]


def bout_document(index: int) -> dict:
    bout_id = BOUT_BASE + index
    red, blue = FIGHTERS[index]
    section = "main" if index < 5 else "prelim"
    return {
        "_id": bout_id,
        "id": bout_id,
        "event_id": EVENT_ID,
        "status": "scheduled",
        "weight_class": "Lightweight",
        "rounds_scheduled": 5 if index == 0 else 3,
        "is_title_fight": index == 0,
        "card_section": section,
        "card_order": index + 1,
        "is_main_event": index == 0,
        "is_co_main_event": index == 1,
        "result": None,
        "fighters": {
            "red": {"fighter_name": red, "corner": "red"},
            "blue": {"fighter_name": blue, "corner": "blue"},
        },
        "card_data_v1": {
            "bout_id": bout_id,
            "event_id": EVENT_ID,
            "matchup_revision": 1,
            "lifecycle": "SCHEDULED",
            "is_current": True,
            "status": "scheduled",
            "section": section.upper(),
            "role": "MAIN_EVENT" if index == 0 else "STANDARD",
            "scheduled_rounds": 5 if index == 0 else 3,
            "is_title_fight": index == 0,
            "result_revision": 0,
            "result": None,
            "fighters": [
                {"fighter_id": f"f-{bout_id}-red", "display_name": red, "corner": "red"},
                {"fighter_id": f"f-{bout_id}-blue", "display_name": blue, "corner": "blue"},
            ],
        },
    }


def slot_document(index: int) -> dict:
    bout_id = BOUT_BASE + index
    return {
        "_id": f"{EVENT_ID}:{bout_id}",
        "event_id": EVENT_ID,
        "bout_id": bout_id,
        "is_current": True,
        "card_section": "main" if index < 5 else "prelim",
        "order_overall": index + 1,
        "order_section": index + 1,
        "role": "main_event" if index == 0 else "regular",
        "structure_revision": 1,
    }


@pytest.fixture
async def card(test_db, sample_event_data):
    await apply_mission_indexes(test_db)
    for collection in (
        "events", "bouts", "event_card_slots", "picks",
        "mission_assignments", "mission_offer_sets", "mission_xp_ledger",
        "mission_celebrations", "mission_evaluation_runs",
        "mission_card_finalization_runs", "mission_card_controls",
        "mission_user_progression",
    ):
        await test_db[collection].delete_many({})
    await test_db["events"].insert_one({
        **sample_event_data,
        "id": EVENT_ID,
        "name": "UFC 900: Fairness Card",
        "slug": "ufc-900-fairness",
        "status": "scheduled",
        "date": datetime.now(UTC) + timedelta(days=3),
        "card_revision": 1,
        "total_bouts": len(FIGHTERS),
    })
    await test_db["bouts"].insert_many(
        [bout_document(index) for index in range(len(FIGHTERS))]
    )
    await test_db["event_card_slots"].insert_many(
        [slot_document(index) for index in range(len(FIGHTERS))]
    )
    return EVENT_ID


async def headers_for(test_db, user_id: str) -> dict:
    email = f"{user_id}@example.com"
    now = datetime.now(UTC)
    await test_db["users"].update_one(
        {"_id": user_id},
        {"$set": {
            "google_id": user_id, "email": email, "name": user_id,
            "is_active": True, "is_admin": False,
            "created_at": now, "last_login_at": now,
        }},
        upsert=True,
    )
    return {"Authorization": "Bearer " + create_access_token(user_id, email)}


async def home_for(client, headers) -> dict:
    response = await client.get(f"/missions/home?event_id={EVENT_ID}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def offered_missions(home: dict) -> set[str]:
    return {
        option["mission_id"]
        for slot in home["slots"]
        for option in slot["options"]
    }


# ------------------------------------------------------------------ fairness


@pytest.mark.asyncio
async def test_different_users_are_offered_different_missions(
    client, test_db, card
):
    """The draw is personalised. Ten users must not all see one hand."""
    hands = []
    for index in range(10):
        headers = await headers_for(test_db, f"draw-user-{index}")
        hands.append(frozenset(offered_missions(await home_for(client, headers))))

    assert len(set(hands)) > 1, "every user was dealt the identical nine missions"
    # Nine offers each; if the draw were broken they would overlap almost
    # perfectly. Two arbitrary users sharing everything is the failure mode.
    assert hands[0] != hands[1]


@pytest.mark.asyncio
async def test_one_user_always_sees_the_same_offers(client, test_db, card):
    """Personalised, but not random per request: refreshing must not reroll."""
    headers = await headers_for(test_db, "stable-user")

    first = await home_for(client, headers)
    second = await home_for(client, headers)

    assert first["offer_set_id"] == second["offer_set_id"]
    assert offered_missions(first) == offered_missions(second)


@pytest.mark.asyncio
async def test_every_slot_offers_one_of_each_difficulty(client, test_db, card):
    headers = await headers_for(test_db, "tier-user")
    home = await home_for(client, headers)

    assert len(home["slots"]) == 3
    for slot in home["slots"]:
        tiers = sorted(option["difficulty"] for option in slot["options"])
        assert tiers == ["EASY", "HARD", "MEDIUM"], slot["slot"]


@pytest.mark.asyncio
async def test_a_mission_is_never_offered_twice_in_one_hand(
    client, test_db, card
):
    """Nine offers, nine distinct missions: a duplicate wastes a slot."""
    for index in range(5):
        headers = await headers_for(test_db, f"dup-user-{index}")
        home = await home_for(client, headers)
        ids = [
            option["mission_id"]
            for slot in home["slots"]
            for option in slot["options"]
        ]
        assert len(ids) == len(set(ids)), ids


# --------------------------------------------------------------- completion


async def register_result(client, admin_headers, bout_id, winner, method, round_=None):
    body = {"winner": winner, "method": method, "time": "2:30"}
    if round_ is not None:
        body["round"] = round_
    response = await client.put(
        f"/admin/bouts/{bout_id}/result", headers=admin_headers, json=body
    )
    assert response.status_code == 200, response.text


@pytest.fixture
async def admin_headers(client, test_db):
    now = datetime.now(UTC)
    await test_db["users"].update_one(
        {"_id": "fair-admin"},
        {"$set": {
            "google_id": "fair-admin", "email": "fair-admin@example.com",
            "name": "Fair Admin", "is_active": True, "is_admin": True,
            "created_at": now, "last_login_at": now,
        }},
        upsert=True,
    )
    return {
        "Authorization": "Bearer "
        + create_access_token("fair-admin", "fair-admin@example.com")
    }


def find_offer(home: dict, mission_id: str):
    for slot in home["slots"]:
        for option in slot["options"]:
            if option["mission_id"] == mission_id:
                return slot["slot"], option
    return None, None


async def force_offer(test_db, user_id: str, mission_id: str, home: dict) -> str:
    """Put one specific mission into slot 1 of this user's existing hand.

    The draw is personalised, so a test cannot ask for a particular family and
    expect to get it. Rewriting the stored offer set is the only way to cover
    every family deterministically; everything after this point is the real
    route doing real work.
    """
    offer_id = f"offer_{abs(hash((user_id, mission_id))):016x}"[:22]
    await test_db["mission_offer_sets"].update_one(
        {"_id": home["offer_set_id"]},
        {"$set": {"slots.0.offers": [{"offer_id": offer_id, "mission_id": mission_id}]}},
    )
    return offer_id


async def settle_card(test_db, user_id: str):
    """Finalize the card so card-level missions resolve."""
    await MissionTriggerService(test_db).on_card_finalized(event_id=EVENT_ID)


@pytest.mark.parametrize(
    "mission_id,expected_family",
    [
        ("CARD-V2-E-008", "AUTO"),          # PRELIM START — 1 prelim winner
        ("CARD-V2-E-012", "CARD_PROP"),     # THREE FINISHES — >=3 finishes
    ],
)
@pytest.mark.asyncio
async def test_a_mission_that_should_pass_actually_completes(
    client, test_db, admin_headers, card, mission_id, expected_family
):
    """Accept it, then make the card do exactly what it asks for."""
    user_id = f"complete-{mission_id}"
    headers = await headers_for(test_db, user_id)
    home = await home_for(client, headers)
    offer_id = await force_offer(test_db, user_id, mission_id, home)

    refreshed = await home_for(client, headers)
    slot, offer = find_offer(refreshed, mission_id)
    assert offer is not None, "the forced offer did not come back on Home"
    assert offer["interaction"] == expected_family

    selection = {"kind": offer["interaction"]}
    response = await client.post(
        "/missions/select",
        headers=headers,
        json={
            "event_id": EVENT_ID,
            "slot": slot,
            "offer_id": offer_id,
            "idempotency_key": f"complete-key-{mission_id}",
            "selection": selection if offer["interaction"] != "AUTO" else None,
        },
    )
    assert response.status_code == 201, response.text

    # PRELIM START scores the user's own picks, so the user has to have made
    # one. Picking red everywhere and then making red win is the plainest
    # possible "this should pass".
    for index in range(len(FIGHTERS)):
        pick = await client.post(
            "/picks",
            headers=headers,
            json={
                "event_id": EVENT_ID,
                "bout_id": BOUT_BASE + index,
                "picked_fighter_name": FIGHTERS[index][0],
                "picked_method": "KO/TKO",
                "picked_round": 1,
            },
        )
        assert pick.status_code in (200, 201), pick.text

    # Every bout finishes: at least one prelim winner, and far more than three
    # finishes, so both missions above are plainly satisfied.
    for index in range(len(FIGHTERS)):
        await register_result(
            client, admin_headers, BOUT_BASE + index, "red", "KO/TKO", 1
        )

    profile = await client.get("/missions/profile", headers=headers)
    assert profile.status_code == 200, profile.text
    settled = [
        row for row in profile.json()["history"] if row["mission_id"] == mission_id
    ]
    assert settled, f"{mission_id} never settled: {profile.json()['history']}"
    assert settled[0]["status"] == "COMPLETED", settled[0]
    assert settled[0]["xp_earned"] == settled[0]["xp"] > 0, settled[0]
    assert profile.json()["lifetime_xp"] >= settled[0]["xp"]


@pytest.mark.asyncio
async def test_a_mission_that_should_fail_is_reported_as_failed(
    client, test_db, admin_headers, card
):
    """The other half of trust: a mission the card did not satisfy must FAIL.

    SUBMISSION TRIO needs three submissions; this card produces none.
    """
    mission_id = "CARD-V2-H-017"
    user_id = "failing-user"
    headers = await headers_for(test_db, user_id)
    home = await home_for(client, headers)
    offer_id = await force_offer(test_db, user_id, mission_id, home)

    refreshed = await home_for(client, headers)
    slot, offer = find_offer(refreshed, mission_id)
    assert offer is not None

    response = await client.post(
        "/missions/select",
        headers=headers,
        json={
            "event_id": EVENT_ID,
            "slot": slot,
            "offer_id": offer_id,
            "idempotency_key": "failing-mission-key",
            "selection": {"kind": offer["interaction"]},
        },
    )
    assert response.status_code == 201, response.text

    for index in range(len(FIGHTERS)):
        await register_result(
            client, admin_headers, BOUT_BASE + index, "red", "Decision"
        )

    profile = await client.get("/missions/profile", headers=headers)
    settled = [
        row for row in profile.json()["history"] if row["mission_id"] == mission_id
    ]
    assert settled, "a mission that cannot pass must still settle, not hang"
    assert settled[0]["status"] == "FAILED", settled[0]
    assert settled[0]["xp_earned"] == 0
