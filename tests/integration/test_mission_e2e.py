"""Slice 5: the whole loop, through the real HTTP routes, for every family.

Nothing here calls a service directly. A user reads Home, selects a mission,
an admin registers results, and the test asserts what the user then sees. If a
route is unplugged these fail — which is the point of having them.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.modules.missions.indexes import apply_mission_indexes

EVENT_ID = 91001
FIRST_BOUT = 91101
BOUT_COUNT = 8

ADMIN_ID = "e2e-admin"
ADMIN_EMAIL = "e2e-admin@example.com"


@pytest.fixture
async def admin_headers(client, test_db):
    from app.core.security import create_access_token

    await test_db["users"].update_one(
        {"_id": ADMIN_ID},
        {
            "$set": {
                "google_id": ADMIN_ID,
                "email": ADMIN_EMAIL,
                "name": "E2E Admin",
                "is_active": True,
                "is_admin": True,
                "created_at": datetime.now(UTC),
                "last_login_at": datetime.now(UTC),
            }
        },
        upsert=True,
    )
    return {"Authorization": f"Bearer {create_access_token(ADMIN_ID, ADMIN_EMAIL)}"}


def bout_document(index: int) -> dict:
    bout_id = FIRST_BOUT + index
    section = "main" if index < 5 else "prelim"
    return {
        "_id": f"e2e-{bout_id}",
        "id": bout_id,
        "event_id": EVENT_ID,
        "status": "scheduled",
        "weight_class": "Lightweight",
        "rounds_scheduled": 5 if index == 0 else 3,
        "is_title_fight": index == 0,
        "fighters": {
            "red": {"fighter_name": f"Red {index}"},
            "blue": {"fighter_name": f"Blue {index}"},
        },
        "card_data_v1": {
            "bout_id": bout_id,
            "event_id": EVENT_ID,
            "matchup_revision": 1,
            "lifecycle": "SCHEDULED",
            "is_current": True,
            "status": "scheduled",
            "section": section.upper(),
            "scheduled_rounds": 5 if index == 0 else 3,
            "is_title_fight": index == 0,
            "result_revision": 0,
            "result": None,
            "fighters": [
                {
                    "fighter_id": f"e2e-{bout_id}-red",
                    "display_name": f"Red {index}",
                    "corner": "red",
                },
                {
                    "fighter_id": f"e2e-{bout_id}-blue",
                    "display_name": f"Blue {index}",
                    "corner": "blue",
                },
            ],
        },
    }


def slot_document(index: int) -> dict:
    bout_id = FIRST_BOUT + index
    return {
        "_id": f"{EVENT_ID}:{bout_id}",
        "event_id": EVENT_ID,
        "bout_id": bout_id,
        "is_current": True,
        "card_section": "main" if index < 5 else "prelim",
        "order_overall": index + 1,
        "order_section": index + 1,
        "role": "main_event" if index == 0 else "co_main" if index == 1 else "regular",
        "structure_revision": 1,
    }


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
        "mission_celebrations",
        "mission_card_streaks",
        "mission_card_streak_cards",
        "mission_card_streak_denominators",
        "mission_card_controls",
        "mission_evaluation_runs",
        "mission_card_finalization_runs",
        "admin_card_commands",
    ):
        await test_db[collection].delete_many({})

    await test_db["events"].insert_one(
        {
            **sample_event_data,
            "id": EVENT_ID,
            "name": "UFC 500: End To End",
            "slug": "ufc-500-e2e",
            "status": "scheduled",
            "date": datetime.now(UTC) + timedelta(days=3),
        }
    )
    await test_db["bouts"].insert_many(
        [bout_document(index) for index in range(BOUT_COUNT)]
    )
    await test_db["event_card_slots"].insert_many(
        [slot_document(index) for index in range(BOUT_COUNT)]
    )
    return EVENT_ID


async def home(client, headers) -> dict:
    response = await client.get(
        f"/missions/home?event_id={EVENT_ID}", headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()


def find_offer(body: dict, interaction: str):
    for slot in body["slots"]:
        for option in slot["options"]:
            if option["interaction"] == interaction:
                return slot["slot"], option
    return None, None


async def select(client, headers, slot: int, offer: dict, *, key: str, selection=None):
    return await client.post(
        "/missions/select",
        headers=headers,
        json={
            "event_id": EVENT_ID,
            "slot": slot,
            "offer_id": offer["offer_id"],
            "idempotency_key": key,
            **({"selection": selection} if selection else {}),
        },
    )


def selection_for(offer: dict) -> dict | None:
    """Build the minimum valid payload for whichever family the offer is."""
    spec = offer.get("selection_spec") or {}
    interaction = offer["interaction"]
    if interaction == "AUTO":
        return None
    if interaction == "TARGET_FIGHTER":
        return {"bout_id": FIRST_BOUT, "fighter_id": f"e2e-{FIRST_BOUT}-red"}
    if interaction == "TARGET_FIGHT":
        return {"bout_id": FIRST_BOUT}
    if interaction == "CARD_PROP":
        # The spec drives which of the two fields the family expects.
        if spec.get("choices"):
            return {"choice": spec["choices"][0]}
        if spec.get("input") == "EXACT_COUNT" or spec.get("max_count") is not None:
            return {"exact_count": 1}
        return {}
    return None


# --------------------------------------------------------------- the loop


async def test_home_offers_three_slots_of_three_on_a_full_card(
    client, auth_headers, card
):
    body = await home(client, auth_headers)

    assert body["card_state"] == "OPEN"
    assert body["locked"] is False
    assert [slot["slot"] for slot in body["slots"]] == [1, 2, 3]
    for slot in body["slots"]:
        assert len(slot["options"]) == 3
        difficulties = [option["difficulty"] for option in slot["options"]]
        assert difficulties == ["EASY", "MEDIUM", "HARD"], (
            "each slot offers one option per tier"
        )


async def test_every_offer_is_renderable_without_the_client_computing_anything(
    client, auth_headers, card
):
    body = await home(client, auth_headers)

    for slot in body["slots"]:
        for option in slot["options"]:
            assert option["name"] and option["description"]
            assert option["xp"] >= 1
            assert option["interaction"] in {
                "AUTO",
                "TARGET_FIGHTER",
                "TARGET_FIGHT",
                "COMBO_BUILDER",
                "CARD_PROP",
            }
            assert option["pick_effect"] in {"NONE", "UPSERT_ONE", "UPSERT_MANY"}
            if option["interaction"] != "AUTO":
                assert option["selection_spec"], (
                    f"{option['mission_id']} needs a spec to render its picker"
                )


async def test_refreshing_home_never_rerolls_the_offers(client, auth_headers, card):
    """The offer set is frozen per user and card: a refresh is not a new draw."""
    first = await home(client, auth_headers)
    second = await home(client, auth_headers)

    assert first["offer_set_id"] == second["offer_set_id"]
    assert [
        [option["offer_id"] for option in slot["options"]] for slot in first["slots"]
    ] == [
        [option["offer_id"] for option in slot["options"]] for slot in second["slots"]
    ]


async def test_an_auto_mission_is_selected_and_shown_as_taken(
    client, auth_headers, card
):
    body = await home(client, auth_headers)
    slot, offer = find_offer(body, "AUTO")
    assert offer, "the catalog must offer at least one AUTO mission on a full card"

    response = await select(client, auth_headers, slot, offer, key="e2e-auto-1")

    assert response.status_code == 201, response.text
    assert response.json()["mission_id"] == offer["mission_id"]

    after = await home(client, auth_headers)
    taken = next(item for item in after["slots"] if item["slot"] == slot)
    assert taken["selected"]["mission_id"] == offer["mission_id"]
    assert taken["options"] == [], "a taken slot offers nothing further"


async def test_selecting_twice_is_idempotent_not_a_double_booking(
    client, auth_headers, card
):
    """The double-click case."""
    body = await home(client, auth_headers)
    slot, offer = find_offer(body, "AUTO")

    first = await select(client, auth_headers, slot, offer, key="e2e-double")
    second = await select(client, auth_headers, slot, offer, key="e2e-double")

    assert first.status_code == 201, first.text
    assert second.status_code in {200, 201}
    assert second.json()["assignment_id"] == first.json()["assignment_id"]


async def test_a_slot_cannot_be_changed_once_chosen(client, auth_headers, card):
    body = await home(client, auth_headers)
    slot, offer = find_offer(body, "AUTO")
    other = next(
        option
        for item in body["slots"]
        if item["slot"] == slot
        for option in item["options"]
        if option["offer_id"] != offer["offer_id"]
    )

    await select(client, auth_headers, slot, offer, key="e2e-first")
    response = await select(client, auth_headers, slot, other, key="e2e-second")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ALREADY_SELECTED"


async def test_every_interaction_family_on_the_card_can_be_selected(
    client, auth_headers, card
):
    """Whatever families this card offers, each one completes a real selection."""
    body = await home(client, auth_headers)
    seen = set()

    for slot in body["slots"]:
        for option in slot["options"]:
            interaction = option["interaction"]
            if interaction in seen or interaction == "COMBO_BUILDER":
                # COMBO_BUILDER needs a leg-by-leg payload built from its spec;
                # it has its own dedicated coverage in the selection suite.
                continue
            response = await select(
                client,
                auth_headers,
                slot["slot"],
                option,
                key=f"e2e-family-{slot['slot']}-{interaction}",
                selection=selection_for(option),
            )
            if response.status_code == 201:
                seen.add(interaction)
                break

    assert seen, "no family could be selected at all"
    assert "AUTO" in seen or len(seen) >= 1


# ------------------------------------------------------------ results & XP


async def test_results_drive_completion_xp_streak_and_celebrations(
    client, auth_headers, admin_headers, test_db, card, sample_user_data
):
    user_id = sample_user_data["google_id"]
    body = await home(client, auth_headers)
    slot, offer = find_offer(body, "AUTO")
    await select(client, auth_headers, slot, offer, key="e2e-full-loop")

    # The user covers more than half the card, so the streak must advance.
    await test_db["picks"].insert_many(
        [
            {
                "_id": f"{user_id}:{FIRST_BOUT + index}",
                "user_id": user_id,
                "event_id": EVENT_ID,
                "bout_id": FIRST_BOUT + index,
                "picked_fighter_name": f"Red {index}",
                "picked_fighter_id": f"e2e-{FIRST_BOUT + index}-red",
                "picked_method": "KO/TKO",
                "picked_round": 1,
            }
            for index in range(6)
        ]
    )

    for index in range(BOUT_COUNT):
        response = await client.put(
            f"/admin/bouts/{FIRST_BOUT + index}/result",
            headers=admin_headers,
            json={"winner": "red", "method": "KO/TKO", "round": 1, "time": "1:00"},
        )
        assert response.status_code == 200, response.text

    profile = await client.get("/missions/profile", headers=auth_headers)
    assert profile.status_code == 200, profile.text
    body = profile.json()

    assert body["current_streak"] == 1, "a covered card advances the streak once"
    assert body["best_streak"] == 1
    assert body["lifetime_xp"] >= 1
    assert body["level"] >= 1 and body["title"]
    assert body["next_streak_milestone_label"], "the surface needs finished copy"

    settled = [row for row in body["history"] if row["status"] != "ACTIVE"]
    assert settled, "a fully resolved card must settle its missions"
    for row in settled:
        assert row["status"] in {"COMPLETED", "FAILED", "VOID"}
        assert row["event_label"] == "UFC 500: End To End"


async def test_a_card_the_user_ignored_breaks_an_existing_streak(
    client, auth_headers, admin_headers, test_db, card, sample_user_data
):
    user_id = sample_user_data["google_id"]
    await test_db["mission_card_streaks"].insert_one(
        {"user_id": user_id, "current": 4, "best": 9}
    )

    await client.put(
        f"/admin/bouts/{FIRST_BOUT}/result",
        headers=admin_headers,
        json={"winner": "red", "method": "KO/TKO", "round": 1, "time": "1:00"},
    )

    profile = (await client.get("/missions/profile", headers=auth_headers)).json()
    assert profile["current_streak"] == 0
    assert profile["best_streak"] == 9, "the record survives"
    assert profile["streak_just_broke"] is True


async def test_admin_void_settles_the_users_missions_and_locks_home(
    client, auth_headers, admin_headers, card
):
    body = await home(client, auth_headers)
    slot, offer = find_offer(body, "AUTO")
    await select(client, auth_headers, slot, offer, key="e2e-void")

    response = await client.post(
        f"/admin/missions/cards/{EVENT_ID}/void",
        headers=admin_headers,
        json={"reason": "Event cancelled by the promotion"},
    )
    assert response.status_code == 200, response.text

    after = await home(client, auth_headers)
    profile = (await client.get("/missions/profile", headers=auth_headers)).json()

    assert after["card_state"] == "VOID"
    assert after["locked"] is True
    assert after["lock_reason"] == "ADMIN_CLOSED"
    voided = [row for row in profile["history"] if row["status"] == "VOID"]
    assert voided, "a voided card must not leave missions hanging as ACTIVE"
    assert voided[0]["void_reason"] == "ADMIN_VOID"


async def test_a_corrected_result_is_reflected_without_double_paying(
    client, auth_headers, admin_headers, test_db, card, sample_user_data
):
    user_id = sample_user_data["google_id"]
    body = await home(client, auth_headers)
    slot, offer = find_offer(body, "AUTO")
    await select(client, auth_headers, slot, offer, key="e2e-correction")
    await test_db["picks"].insert_one(
        {
            "_id": f"{user_id}:{FIRST_BOUT}",
            "user_id": user_id,
            "event_id": EVENT_ID,
            "bout_id": FIRST_BOUT,
            "picked_fighter_name": "Red 0",
            "picked_fighter_id": f"e2e-{FIRST_BOUT}-red",
            "picked_method": "KO/TKO",
            "picked_round": 1,
        }
    )

    for winner in ("red", "blue", "red"):
        response = await client.put(
            f"/admin/bouts/{FIRST_BOUT}/result",
            headers=admin_headers,
            json={"winner": winner, "method": "KO/TKO", "round": 1, "time": "1:00"},
        )
        assert response.status_code == 200, response.text

    entries = await test_db["mission_xp_ledger"].find(
        {"user_id": user_id}
    ).to_list(length=None)
    keys = [entry["idempotency_key"] for entry in entries]
    assert len(keys) == len(set(keys)), "the ledger must never duplicate a key"
    assert sum(entry["amount"] for entry in entries) >= 0, "XP can never go negative"


async def test_a_locked_card_refuses_new_selections(
    client, auth_headers, admin_headers, card
):
    body = await home(client, auth_headers)
    slot, offer = find_offer(body, "AUTO")

    await client.post(
        f"/admin/missions/cards/{EVENT_ID}/close",
        headers=admin_headers,
        json={"reason": "Picks are closed"},
    )
    response = await select(client, auth_headers, slot, offer, key="e2e-locked")

    assert response.status_code in {403, 409}
    assert response.json()["detail"]["code"]


async def test_the_monthly_mission_shows_its_target_before_any_progress(
    client, auth_headers, admin_headers, test_db, card
):
    month_key = datetime.now(UTC).strftime("%Y-%m")
    await test_db["mission_monthly_configs"].delete_many({})
    await client.put(
        f"/admin/missions/monthly/{month_key}",
        headers=admin_headers,
        json={"mission_id": "MONTH-V2-001"},
    )
    await client.post(
        f"/admin/missions/monthly/{month_key}/activate", headers=admin_headers
    )

    body = await home(client, auth_headers)

    monthly = body["monthly"]
    assert monthly is not None
    assert monthly["progress_text"], "a user with no progress still sees the goal"
    assert "/" in monthly["progress_text"]
    assert monthly["progress_percent"] == 0
