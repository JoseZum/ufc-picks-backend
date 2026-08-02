"""B-010/B-011: an Admin title decision is recorded, durable and scraper-proof.

The endpoint used to write `is_title_fight` as a bare field. A bare `false` is
indistinguishable from the scraper's default, so the next Tapology run undid the
removal. These tests assert the evidence entry that makes the decision visible,
through the real route, and then replay the scraper guard against it.
"""

from datetime import UTC, datetime

import pytest

from app.services.canonical_authority import admin_owned_fields, strip_admin_owned

BOUT_ID = 66101
EVENT_ID = 66001

ADMIN_ID = "mission-admin-user"
ADMIN_EMAIL = "mission-admin@example.com"


@pytest.fixture
async def admin_headers(client, test_db):
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
    return {"Authorization": f"Bearer {create_access_token(ADMIN_ID, ADMIN_EMAIL)}"}


@pytest.fixture
async def bout(test_db):
    await test_db["bouts"].delete_many({"id": BOUT_ID})
    await test_db["bouts"].insert_one(
        {
            "id": BOUT_ID,
            "event_id": EVENT_ID,
            "status": "scheduled",
            "weight_class": "Lightweight",
            "rounds_scheduled": 3,
            # What a Tapology run leaves behind: a default nobody decided.
            "is_title_fight": True,
            "fighters": {
                "red": {"fighter_name": "Red"},
                "blue": {"fighter_name": "Blue"},
            },
        }
    )
    return BOUT_ID


async def test_setting_a_title_records_admin_as_the_authority(
    client, admin_headers, test_db, bout
):
    response = await client.put(
        f"/admin/bouts/{BOUT_ID}/details",
        headers=admin_headers,
        json={"is_title_fight": True},
    )

    assert response.status_code == 200, response.text
    stored = await test_db["bouts"].find_one({"id": BOUT_ID})
    evidence = stored["card_data_v1"]["evidence"]["is_title_fight"]
    assert evidence["source_kind"] == "admin_override"
    assert evidence["value"] is True
    assert evidence["actor_id"] == ADMIN_ID


async def test_removing_a_title_is_recorded_just_as_durably(
    client, admin_headers, test_db, bout
):
    """The regression behind B-010: `false` must be a decision, not an absence."""
    response = await client.put(
        f"/admin/bouts/{BOUT_ID}/details",
        headers=admin_headers,
        json={"is_title_fight": False},
    )

    assert response.status_code == 200, response.text
    stored = await test_db["bouts"].find_one({"id": BOUT_ID})
    assert stored["is_title_fight"] is False
    assert admin_owned_fields(stored) == {"is_title_fight"}


async def test_the_scraper_guard_refuses_to_undo_that_removal(
    client, admin_headers, test_db, bout
):
    """End to end: Admin removes the title, then a Tapology run claims it back."""
    await client.put(
        f"/admin/bouts/{BOUT_ID}/details",
        headers=admin_headers,
        json={"is_title_fight": False},
    )
    stored = await test_db["bouts"].find_one({"id": BOUT_ID})

    scraped = {"is_title_fight": True, "weight_class": "Welterweight"}
    writable = strip_admin_owned(scraped, stored)

    assert "is_title_fight" not in writable
    assert writable["weight_class"] == "Welterweight", "advisory fields still flow"


async def test_editing_an_unrelated_field_claims_no_title_authority(
    client, admin_headers, test_db, bout
):
    """Authority is claimed by deciding, not by opening the edit form."""
    response = await client.put(
        f"/admin/bouts/{BOUT_ID}/details",
        headers=admin_headers,
        json={"weight_class": "Welterweight"},
    )

    assert response.status_code == 200, response.text
    stored = await test_db["bouts"].find_one({"id": BOUT_ID})
    assert admin_owned_fields(stored) == set()
    assert strip_admin_owned({"is_title_fight": False}, stored) == {
        "is_title_fight": False
    }


async def test_a_normal_user_cannot_decide_a_title(client, auth_headers, test_db, bout):
    response = await client.put(
        f"/admin/bouts/{BOUT_ID}/details",
        headers=auth_headers,
        json={"is_title_fight": False},
    )

    assert response.status_code == 403
    stored = await test_db["bouts"].find_one({"id": BOUT_ID})
    assert admin_owned_fields(stored) == set()


# ------------------------------------------------------- the durable command


async def test_a_title_decision_persists_a_command_for_the_boundary(
    client, admin_headers, test_db, bout
):
    """The evidence stamp protects the legacy route; the command is what makes
    the decision outlast an ESPN reconciliation pass."""
    await test_db["admin_card_commands"].delete_many({})

    response = await client.put(
        f"/admin/bouts/{BOUT_ID}/details",
        headers=admin_headers,
        json={"is_title_fight": False},
    )

    assert response.status_code == 200, response.text
    command = await test_db["admin_card_commands"].find_one({"bout_id": BOUT_ID})
    assert command is not None, "no command means the next ESPN pass reverts this"
    assert command["kind"] == "title"
    assert command["event_id"] == EVENT_ID
    assert command["values"] == {"is_title_fight": False}
    assert command["actor_id"] == ADMIN_ID
    assert command["reason"]
    assert command["observed_at"].endswith("Z")


async def test_re_deciding_replaces_the_command_instead_of_stacking_them(
    client, admin_headers, test_db, bout
):
    """Two conflicting overrides replayed together would be a coin flip."""
    await test_db["admin_card_commands"].delete_many({})

    for value in (True, False, True):
        await client.put(
            f"/admin/bouts/{BOUT_ID}/details",
            headers=admin_headers,
            json={"is_title_fight": value},
        )

    commands = await test_db["admin_card_commands"].find(
        {"bout_id": BOUT_ID}
    ).to_list(length=None)

    assert len(commands) == 1, "one standing decision per field, per actor"
    assert commands[0]["values"]["is_title_fight"] is True, "the latest one wins"


async def test_editing_a_non_title_field_records_no_command(
    client, admin_headers, test_db, bout
):
    await test_db["admin_card_commands"].delete_many({})

    await client.put(
        f"/admin/bouts/{BOUT_ID}/details",
        headers=admin_headers,
        json={"weight_class": "Welterweight"},
    )

    assert await test_db["admin_card_commands"].count_documents({}) == 0


async def test_a_bmf_title_travels_with_the_decision(
    client, admin_headers, test_db, bout
):
    await test_db["admin_card_commands"].delete_many({})

    await client.put(
        f"/admin/bouts/{BOUT_ID}/details",
        headers=admin_headers,
        json={"is_title_fight": True, "is_bmf_title_fight": True},
    )

    command = await test_db["admin_card_commands"].find_one({"bout_id": BOUT_ID})
    assert command["values"] == {
        "is_title_fight": True,
        "is_bmf_title_fight": True,
    }


async def test_an_unsupported_command_kind_is_a_programming_error(test_db):
    from app.services.admin_card_commands import AdminCommandError, record_admin_command

    with pytest.raises(AdminCommandError):
        await record_admin_command(
            test_db, kind="teleport", event_id=1, reason="why", actor_id="a", bout_id=1
        )
    with pytest.raises(AdminCommandError):
        await record_admin_command(
            test_db, kind="title", event_id=1, reason="", actor_id="a", bout_id=1
        )
    with pytest.raises(AdminCommandError):
        await record_admin_command(
            test_db, kind="title", event_id=1, reason="why", actor_id="a"
        )


# ------------------------------ B-011: the other canonical writes, made durable


async def test_a_structure_edit_deliberately_records_no_command_yet(
    client, admin_headers, test_db, bout
):
    """B-011 stays open for card structure, on purpose.

    ESPN emits `card_section` as a fact rather than an advisory signal, so an
    Admin override contradicts it every pass: the plan comes back
    `safe_to_apply=false` with quarantines and never converges. Emitting the
    command here would turn a silent revert into a blocked card.
    """
    await test_db["admin_card_commands"].delete_many({})
    await test_db["event_card_slots"].delete_many({"bout_id": BOUT_ID})
    await test_db["event_card_slots"].insert_one(
        {"_id": f"{EVENT_ID}:{BOUT_ID}", "event_id": EVENT_ID, "bout_id": BOUT_ID,
         "card_section": "prelim", "order_overall": 5, "is_current": True}
    )

    response = await client.put(
        f"/admin/bouts/{BOUT_ID}/details",
        headers=admin_headers,
        json={"card_section": "main", "order_overall": 1},
    )

    assert response.status_code == 200, response.text
    slot = await test_db["event_card_slots"].find_one({"bout_id": BOUT_ID})
    assert slot["card_section"] == "main", "the immediate edit still applies"
    assert await test_db["admin_card_commands"].count_documents(
        {"kind": "bout_structure"}
    ) == 0


async def test_registering_a_result_persists_a_canonical_result_command(
    client, admin_headers, test_db, bout
):
    await test_db["admin_card_commands"].delete_many({})
    await test_db["bouts"].update_one(
        {"id": BOUT_ID},
        {"$set": {"card_data_v1": {
            "bout_id": BOUT_ID, "event_id": EVENT_ID, "scheduled_rounds": 3,
            "result_revision": 0, "result": None,
            "fighters": [
                {"fighter_id": "f-red", "display_name": "Red", "corner": "red"},
                {"fighter_id": "f-blue", "display_name": "Blue", "corner": "blue"},
            ],
        }}},
    )

    response = await client.put(
        f"/admin/bouts/{BOUT_ID}/result",
        headers=admin_headers,
        json={"winner": "red", "method": "KO/TKO", "round": 2, "time": "3:15"},
    )

    assert response.status_code == 200, response.text
    command = await test_db["admin_card_commands"].find_one({"kind": "result"})
    assert command is not None, "the boundary would recompute this away otherwise"
    assert command["values"]["outcome"] == "red_win"
    assert command["values"]["winner_fighter_id"] == "f-red"
    assert command["values"]["ending_round"] == 2


async def test_deleting_a_result_withdraws_the_standing_command(
    client, admin_headers, test_db, bout
):
    """Otherwise the boundary replays a result Admin already deleted, forever."""
    await test_db["admin_card_commands"].delete_many({})
    await test_db["bouts"].update_one(
        {"id": BOUT_ID},
        {"$set": {"card_data_v1": {
            "bout_id": BOUT_ID, "event_id": EVENT_ID, "scheduled_rounds": 3,
            "result_revision": 0, "result": None,
            "fighters": [
                {"fighter_id": "f-red", "display_name": "Red", "corner": "red"},
                {"fighter_id": "f-blue", "display_name": "Blue", "corner": "blue"},
            ],
        }}},
    )
    await client.put(
        f"/admin/bouts/{BOUT_ID}/result",
        headers=admin_headers,
        json={"winner": "red", "method": "KO/TKO", "round": 2, "time": "3:15"},
    )
    assert await test_db["admin_card_commands"].count_documents({"kind": "result"}) == 1

    response = await client.delete(
        f"/admin/bouts/{BOUT_ID}/result", headers=admin_headers
    )

    assert response.status_code == 200, response.text
    assert await test_db["admin_card_commands"].count_documents({"kind": "result"}) == 0
    assert await test_db["admin_card_commands"].count_documents(
        {"kind": "clear_result"}
    ) == 1


async def test_cancelling_a_bout_persists_a_lifecycle_command(
    client, admin_headers, test_db, bout
):
    await test_db["admin_card_commands"].delete_many({})

    response = await client.post(
        f"/admin/bouts/{BOUT_ID}/cancel", headers=admin_headers
    )

    assert response.status_code == 200, response.text
    command = await test_db["admin_card_commands"].find_one({"kind": "bout_lifecycle"})
    assert command is not None
    assert command["values"] == {"status": "cancelled"}
