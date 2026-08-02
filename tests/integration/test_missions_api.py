"""End-to-end contract tests for the user-facing mission endpoints."""

from datetime import UTC, datetime, timedelta

import pytest

from app.modules.missions.indexes import apply_mission_indexes

EVENT_ID = 77001


@pytest.fixture
async def card(test_db):
    await apply_mission_indexes(test_db)
    await test_db["events"].delete_many({"id": EVENT_ID})
    await test_db["bouts"].delete_many({"event_id": EVENT_ID})
    await test_db["events"].insert_one(
        {
            "id": EVENT_ID,
            "name": "UFC 400: Local Test",
            "status": "scheduled",
            "date": datetime.now(UTC) + timedelta(days=3),
            "card_revision": 1,
        }
    )
    await test_db["bouts"].insert_many(
        [
            {
                "id": 90000 + index,
                "event_id": EVENT_ID,
                "status": "scheduled",
                "section": "MAIN" if index < 5 else "PRELIM",
                "is_title_fight": index == 0,
                "fighters": {
                    "red": {"name": f"Red {index}"},
                    "blue": {"name": f"Blue {index}"},
                },
            }
            for index in range(10)
        ]
    )
    return EVENT_ID


async def test_home_requires_authentication(client, card):
    response = await client.get(f"/missions/home?event_id={card}")

    assert response.status_code == 403


async def test_home_returns_three_slots_with_easy_medium_hard(
    client, auth_headers, card
):
    response = await client.get(f"/missions/home?event_id={card}", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["event_id"] == card
    assert body["card_state"] == "OPEN"
    assert body["locked"] is False
    assert len(body["slots"]) == 3
    for slot in body["slots"]:
        assert slot["selected"] is None
        difficulties = [option["difficulty"] for option in slot["options"]]
        assert difficulties == ["EASY", "MEDIUM", "HARD"]


async def test_refresh_never_rerolls_the_offer_set(client, auth_headers, card):
    first = await client.get(f"/missions/home?event_id={card}", headers=auth_headers)
    second = await client.get(f"/missions/home?event_id={card}", headers=auth_headers)

    assert first.json()["offer_set_id"] == second.json()["offer_set_id"]
    assert first.json()["slots"] == second.json()["slots"]


async def test_every_offer_is_presentation_ready(client, auth_headers, card):
    body = (
        await client.get(f"/missions/home?event_id={card}", headers=auth_headers)
    ).json()

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
            if option["interaction"] == "AUTO":
                assert option["selection_prompt"] is None
            else:
                assert option["selection_prompt"]


async def test_missing_event_reports_a_locked_void_card(client, auth_headers):
    response = await client.get("/missions/home?event_id=999999", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["card_state"] == "VOID"
    assert body["locked"] is True
    assert body["lock_reason"] == "CARD_NOT_FOUND"
    assert body["slots"] == []


async def test_a_completed_card_is_locked(client, auth_headers, card, test_db):
    await test_db["events"].update_one(
        {"id": card}, {"$set": {"status": "completed"}}
    )

    body = (
        await client.get(f"/missions/home?event_id={card}", headers=auth_headers)
    ).json()

    assert body["locked"] is True
    assert body["lock_reason"] == "RESULTS_STARTED"


async def test_selecting_an_auto_mission_activates_it(client, auth_headers, card):
    home = (
        await client.get(f"/missions/home?event_id={card}", headers=auth_headers)
    ).json()
    auto = next(
        (slot["slot"], option)
        for slot in home["slots"]
        for option in slot["options"]
        if option["interaction"] == "AUTO"
    )
    slot, option = auto

    response = await client.post(
        "/missions/select",
        headers=auth_headers,
        json={
            "event_id": card,
            "slot": slot,
            "offer_id": option["offer_id"],
            "idempotency_key": f"select-{card}-{slot}",
        },
    )

    assert response.status_code == 201, response.text
    selected = response.json()
    assert selected["mission_id"] == option["mission_id"]
    assert selected["status"] == "ACTIVE"

    after = (
        await client.get(f"/missions/home?event_id={card}", headers=auth_headers)
    ).json()
    chosen = next(item for item in after["slots"] if item["slot"] == slot)
    assert chosen["selected"]["mission_id"] == option["mission_id"]
    assert chosen["options"] == []


async def test_selecting_on_a_locked_card_is_rejected(
    client, auth_headers, card, test_db
):
    home = (
        await client.get(f"/missions/home?event_id={card}", headers=auth_headers)
    ).json()
    slot = home["slots"][0]
    await test_db["events"].update_one({"id": card}, {"$set": {"status": "completed"}})

    response = await client.post(
        "/missions/select",
        headers=auth_headers,
        json={
            "event_id": card,
            "slot": slot["slot"],
            "offer_id": slot["options"][0]["offer_id"],
            "idempotency_key": f"locked-{card}",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "SLOT_LOCKED"


async def test_profile_reports_progression_even_with_no_missions(
    client, auth_headers
):
    response = await client.get("/missions/profile", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["level"] >= 1
    assert body["title"]
    assert body["lifetime_xp"] >= 0
    assert body["current_streak"] >= 0
    assert isinstance(body["active"], list)
    assert isinstance(body["celebrations"], list)


async def test_profile_lists_an_active_selection(client, auth_headers, card):
    home = (
        await client.get(f"/missions/home?event_id={card}", headers=auth_headers)
    ).json()
    auto = next(
        (slot["slot"], option)
        for slot in home["slots"]
        for option in slot["options"]
        if option["interaction"] == "AUTO"
    )
    slot, option = auto
    await client.post(
        "/missions/select",
        headers=auth_headers,
        json={
            "event_id": card,
            "slot": slot,
            "offer_id": option["offer_id"],
            "idempotency_key": f"profile-{card}-{slot}",
        },
    )

    body = (await client.get("/missions/profile", headers=auth_headers)).json()

    assert any(item["mission_id"] == option["mission_id"] for item in body["active"])


async def test_selected_view_renders_persisted_evaluator_progress(
    client, auth_headers, card, test_db
):
    """Progress lives at progress.progress.{text,percent}, not at the top level."""
    home = (
        await client.get(f"/missions/home?event_id={card}", headers=auth_headers)
    ).json()
    slot, option = next(
        (item["slot"], option)
        for item in home["slots"]
        for option in item["options"]
        if option["interaction"] == "AUTO"
    )
    created = await client.post(
        "/missions/select",
        headers=auth_headers,
        json={
            "event_id": card,
            "slot": slot,
            "offer_id": option["offer_id"],
            "idempotency_key": f"progress-{card}-{slot}",
        },
    )
    assignment_id = created.json()["assignment_id"]

    await test_db["mission_assignments"].update_one(
        {"_id": assignment_id},
        {
            "$set": {
                "status": "COMPLETED",
                "progress": {
                    "status": "COMPLETED",
                    "reason": "TERMINAL_MATCH",
                    "progress": {"text": "2 / 2 winners", "percent": 100},
                    "observation": {"void_reason": None},
                },
            }
        },
    )

    body = (await client.get("/missions/profile", headers=auth_headers)).json()
    entry = next(item for item in body["history"] if item["assignment_id"] == assignment_id)

    assert entry["progress_text"] == "2 / 2 winners"
    assert entry["progress_percent"] == 100
    assert entry["xp_earned"] == entry["xp"]


async def test_void_reason_reaches_the_client(client, auth_headers, card, test_db):
    home = (
        await client.get(f"/missions/home?event_id={card}", headers=auth_headers)
    ).json()
    slot, option = next(
        (item["slot"], option)
        for item in home["slots"]
        for option in item["options"]
        if option["interaction"] == "AUTO"
    )
    created = await client.post(
        "/missions/select",
        headers=auth_headers,
        json={
            "event_id": card,
            "slot": slot,
            "offer_id": option["offer_id"],
            "idempotency_key": f"void-{card}-{slot}",
        },
    )
    await test_db["mission_assignments"].update_one(
        {"_id": created.json()["assignment_id"]},
        {
            "$set": {
                "status": "VOID",
                "progress": {
                    "progress": {"text": "VOID", "percent": 100},
                    "observation": {"void_reason": "TARGET_CANCELLED"},
                },
            }
        },
    )

    body = (await client.get("/missions/profile", headers=auth_headers)).json()
    entry = next(
        item
        for item in body["history"]
        if item["assignment_id"] == created.json()["assignment_id"]
    )

    assert entry["status"] == "VOID"
    assert entry["void_reason"] == "TARGET_CANCELLED"
    assert entry["xp_earned"] == 0


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ({"picks_locked": True}, "PICKS_CLOSED"),
        ({"status": "live"}, "RESULTS_STARTED"),
    ],
)
async def test_lock_reason_distinguishes_why_selection_closed(
    client, auth_headers, card, test_db, mutation, expected
):
    await test_db["events"].update_one({"id": card}, {"$set": mutation})

    body = (
        await client.get(f"/missions/home?event_id={card}", headers=auth_headers)
    ).json()

    assert body["locked"] is True
    assert body["lock_reason"] == expected


async def test_admin_close_is_distinguishable_from_picks_close(
    client, auth_headers, card, test_db
):
    await test_db["mission_card_controls"].update_one(
        {"event_id": card},
        {"$set": {"event_id": card, "state": "CLOSED"}},
        upsert=True,
    )

    body = (
        await client.get(f"/missions/home?event_id={card}", headers=auth_headers)
    ).json()

    assert body["lock_reason"] == "ADMIN_CLOSED"


async def test_results_started_locks_selection(client, auth_headers, card, test_db):
    await test_db["bouts"].update_one(
        {"event_id": card, "id": 90000},
        {"$set": {"result": {"outcome": "RED_WIN", "method": "KO_TKO", "round": 1}}},
    )

    body = (
        await client.get(f"/missions/home?event_id={card}", headers=auth_headers)
    ).json()

    assert body["locked"] is True
    assert body["lock_reason"] == "RESULTS_STARTED"


async def test_profile_carries_the_monthly_mission(client, auth_headers):
    body = (await client.get("/missions/profile", headers=auth_headers)).json()

    assert "monthly" in body


async def test_retrying_a_selection_duplicates_nothing(
    client, auth_headers, card, test_db
):
    home = (
        await client.get(f"/missions/home?event_id={card}", headers=auth_headers)
    ).json()
    slot, option = next(
        (item["slot"], option)
        for item in home["slots"]
        for option in item["options"]
        if option["interaction"] == "AUTO"
    )
    payload = {
        "event_id": card,
        "slot": slot,
        "offer_id": option["offer_id"],
        "idempotency_key": f"retry-{card}-{slot}",
    }

    first = await client.post("/missions/select", headers=auth_headers, json=payload)
    second = await client.post("/missions/select", headers=auth_headers, json=payload)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["assignment_id"] == second.json()["assignment_id"]
    assert (
        await test_db["mission_assignments"].count_documents(
            {"event_id": card, "slot": slot}
        )
        == 1
    )


async def test_every_interaction_type_ships_a_usable_selection_spec(
    client, auth_headers, card
):
    body = (
        await client.get(f"/missions/home?event_id={card}", headers=auth_headers)
    ).json()

    for slot in body["slots"]:
        for option in slot["options"]:
            if option["interaction"] == "AUTO":
                assert option["selection_spec"] is None
            else:
                assert isinstance(option["selection_spec"], dict)
                assert option["selection_spec"], option["mission_id"]
