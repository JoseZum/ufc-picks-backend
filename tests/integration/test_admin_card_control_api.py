"""Slice 4: Admin really controls the mission window, and it is audited.

`mission_card_controls` was read by Home and by selection but nothing ever wrote
it. These tests drive the real routes: a card closes, reopens and VOIDs; a
normal user gets 403; every action records an actor and a reason.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.modules.missions.indexes import apply_mission_indexes

EVENT_ID = 77001
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
async def card(test_db, sample_event_data):
    await apply_mission_indexes(test_db)
    for collection in (
        "mission_card_controls",
        "mission_assignments",
        "mission_admin_audit",
    ):
        await test_db[collection].delete_many({})
    await test_db["events"].delete_many({"id": EVENT_ID})
    await test_db["events"].insert_one(
        {
            **sample_event_data,
            "id": EVENT_ID,
            "name": "UFC 405: Control",
            "slug": "ufc-405-control",
            "status": "scheduled",
            "date": datetime.now(UTC) + timedelta(days=3),
        }
    )
    return EVENT_ID


async def assignment(
    test_db, assignment_id: str, status_: str = "ACTIVE", *, slot: int = 1
) -> None:
    await test_db["mission_assignments"].insert_one(
        {
            "_id": assignment_id,
            "user_id": "someone",
            "event_id": EVENT_ID,
            "slot": slot,
            "mission_id": "CARD-V2-E-001",
            "xp": 1,
            "status": status_,
            "revision": 1,
            "created_at": datetime.now(UTC),
        }
    )


REASON = {"reason": "Card cancelled by the promotion"}


# ------------------------------------------------------------------- defaults


async def test_an_untouched_card_is_open(client, admin_headers, card):
    response = await client.get(f"/admin/missions/cards/{EVENT_ID}", headers=admin_headers)

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "OPEN"
    assert response.json()["revision"] == 0


async def test_a_normal_user_cannot_touch_the_card(client, auth_headers, card):
    read = await client.get(f"/admin/missions/cards/{EVENT_ID}", headers=auth_headers)
    write = await client.post(
        f"/admin/missions/cards/{EVENT_ID}/close", headers=auth_headers, json=REASON
    )

    assert read.status_code == 403
    assert write.status_code == 403


# ------------------------------------------------------------ close / reopen


async def test_closing_then_reopening_a_card(client, admin_headers, card):
    closed = await client.post(
        f"/admin/missions/cards/{EVENT_ID}/close", headers=admin_headers, json=REASON
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["state"] == "CLOSED"
    assert closed.json()["actor_id"] == ADMIN_ID
    assert closed.json()["reason"] == REASON["reason"]

    reopened = await client.post(
        f"/admin/missions/cards/{EVENT_ID}/reopen",
        headers=admin_headers,
        json={"reason": "Closed by mistake"},
    )
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["state"] == "OPEN"


async def test_a_closed_card_reaches_the_user_home_as_locked(
    client, admin_headers, auth_headers, card
):
    await client.post(
        f"/admin/missions/cards/{EVENT_ID}/close", headers=admin_headers, json=REASON
    )

    home = await client.get(f"/missions/home?event_id={EVENT_ID}", headers=auth_headers)

    assert home.status_code == 200, home.text
    assert home.json()["card_state"] == "CLOSED"
    assert home.json()["locked"] is True
    assert home.json()["lock_reason"] == "ADMIN_CLOSED"


async def test_repeating_an_action_is_not_an_error(client, admin_headers, card):
    first = await client.post(
        f"/admin/missions/cards/{EVENT_ID}/close", headers=admin_headers, json=REASON
    )
    second = await client.post(
        f"/admin/missions/cards/{EVENT_ID}/close", headers=admin_headers, json=REASON
    )

    assert second.status_code == 200
    assert second.json()["revision"] == first.json()["revision"]


# --------------------------------------------------------------------- void


async def test_voiding_a_card_settles_its_active_missions(
    client, admin_headers, test_db, card
):
    await assignment(test_db, "assignment-active")
    await assignment(test_db, "assignment-done", "COMPLETED", slot=2)

    response = await client.post(
        f"/admin/missions/cards/{EVENT_ID}/void", headers=admin_headers, json=REASON
    )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "VOID"
    assert response.json()["voided_assignments"] == 1

    active = await test_db["mission_assignments"].find_one({"_id": "assignment-active"})
    settled = await test_db["mission_assignments"].find_one({"_id": "assignment-done"})
    assert active["status"] == "VOID"
    assert active["void_reason"] == "ADMIN_VOID"
    assert active["voided_by"] == ADMIN_ID
    assert settled["status"] == "COMPLETED", "a settled mission keeps its outcome"


async def test_void_is_irreversible(client, admin_headers, card):
    await client.post(
        f"/admin/missions/cards/{EVENT_ID}/void", headers=admin_headers, json=REASON
    )

    reopened = await client.post(
        f"/admin/missions/cards/{EVENT_ID}/reopen",
        headers=admin_headers,
        json={"reason": "Changed my mind"},
    )

    assert reopened.status_code == 409
    assert reopened.json()["detail"]["code"] == "ALREADY_VOID"


# ------------------------------------------------------------------- rigour


async def test_an_action_without_a_reason_is_rejected(client, admin_headers, card):
    response = await client.post(
        f"/admin/missions/cards/{EVENT_ID}/close", headers=admin_headers, json={"reason": ""}
    )

    assert response.status_code == 422


async def test_acting_on_a_card_that_does_not_exist_is_a_404(client, admin_headers, card):
    response = await client.post(
        "/admin/missions/cards/99999999/close", headers=admin_headers, json=REASON
    )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "CARD_NOT_FOUND"


async def test_an_unknown_action_is_a_404(client, admin_headers, card):
    response = await client.post(
        f"/admin/missions/cards/{EVENT_ID}/detonate", headers=admin_headers, json=REASON
    )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "UNKNOWN_ACTION"


async def test_every_card_action_is_audited_with_actor_and_reason(
    client, admin_headers, test_db, card
):
    await client.post(
        f"/admin/missions/cards/{EVENT_ID}/close", headers=admin_headers, json=REASON
    )
    await client.post(
        f"/admin/missions/cards/{EVENT_ID}/void",
        headers=admin_headers,
        json={"reason": "Event cancelled"},
    )

    rows = (
        await test_db["mission_admin_audit"]
        .find({"payload.event_id": EVENT_ID})
        .to_list(length=None)
    )

    assert sorted(row["action"] for row in rows) == ["card.close", "card.void"]
    for row in rows:
        assert row["actor_id"] == ADMIN_ID
        assert row["payload"]["reason"]
