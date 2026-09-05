"""Another user's mission standing, as the profile card reads it.

The interesting assertions here are the negative ones. This endpoint exists so
one player can look at another, which makes every field a deliberate decision
about what is public. Celebrations and in-flight missions are not.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.security import create_access_token
from app.modules.missions.indexes import apply_mission_indexes

OTHER = "public-other-user"


@pytest.fixture
async def other_user(test_db):
    await apply_mission_indexes(test_db)
    now = datetime.now(UTC)
    await test_db["users"].update_one(
        {"_id": OTHER},
        {"$set": {
            "google_id": OTHER, "email": f"{OTHER}@example.com", "name": "Other",
            "is_active": True, "is_admin": False,
            "created_at": now, "last_login_at": now,
        }},
        upsert=True,
    )
    await test_db["mission_assignments"].delete_many({"user_id": OTHER})
    await test_db["mission_celebrations"].delete_many({"user_id": OTHER})
    await test_db["mission_xp_ledger"].delete_many({"user_id": OTHER})

    common = {
        "user_id": OTHER, "event_id": 91234, "mission_id": "CARD-V2-E-012",
        "name": "THREE FINISHES", "description": "", "difficulty": "EASY",
        "xp": 1, "interaction": "CARD_PROP", "revision": 1, "created_at": now,
    }
    await test_db["mission_assignments"].insert_many([
        {**common, "_id": "pub-done", "slot": 1, "status": "COMPLETED", "xp_earned": 1},
        {**common, "_id": "pub-failed", "slot": 2, "status": "FAILED", "xp_earned": 0},
        {**common, "_id": "pub-active", "slot": 3, "status": "ACTIVE", "xp_earned": 0,
         "name": "SECRET IN-FLIGHT PICK"},
    ])
    await test_db["mission_celebrations"].insert_one({
        "_id": "pub-celebration", "user_id": OTHER, "kind": "LEVEL_UP",
        "presentation": "FULL_SCREEN", "heading": "Level 2",
        "message": "BUM", "metadata": {}, "acknowledged_at": None,
        "created_at": now, "xp_entry_id": "whatever",
    })
    return OTHER


async def fetch(client, auth_headers, user_id):
    return await client.get(f"/missions/users/{user_id}", headers=auth_headers)


@pytest.mark.asyncio
async def test_it_reports_the_public_standing(client, auth_headers, other_user):
    response = await fetch(client, auth_headers, OTHER)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user_id"] == OTHER
    assert body["level"] >= 1
    assert body["title"]
    assert body["missions_completed"] == 1
    assert body["missions_settled"] == 2, "settled means COMPLETED or FAILED"


@pytest.mark.asyncio
async def test_an_in_flight_mission_is_not_visible_to_anyone_else(
    client, auth_headers, other_user
):
    """An active selection is a bet. Nobody else reads it before it settles."""
    body = (await fetch(client, auth_headers, OTHER)).json()

    assert "SECRET IN-FLIGHT PICK" not in response_text(body)
    assert all(row["status"] == "COMPLETED" for row in body["recent"])
    assert {row["assignment_id"] for row in body["history"]} == {"pub-done", "pub-failed"}


@pytest.mark.asyncio
async def test_history_includes_all_settled_statuses_and_more_than_eight_rows(
    client, auth_headers, other_user, test_db
):
    original = await test_db["mission_assignments"].find_one({"_id": "pub-done"})
    now = datetime.now(UTC)
    await test_db["events"].insert_one({"id": 91234, "name": "UFC Test Card"})
    await test_db["mission_assignments"].insert_many([
        {**original, "_id": f"pub-extra-{i}", "event_id": 91300 + i,
         "created_at": now + timedelta(seconds=i)}
        for i in range(9)
    ] + [{**original, "_id": "pub-void", "event_id": 91400, "status": "VOID",
          "created_at": now + timedelta(seconds=10)}])

    response = await fetch(client, auth_headers, OTHER)
    assert response.status_code == 200
    body = response.json()
    assert len(body["history"]) == 12
    assert len(body["recent"]) == 8
    assert body["history"][0]["assignment_id"] == "pub-void"
    assert {row["status"] for row in body["history"]} == {"COMPLETED", "FAILED", "VOID"}
    completed = next(row for row in body["history"] if row["assignment_id"] == "pub-done")
    assert completed["description"]
    assert completed["event_label"] == "UFC Test Card"
    assert all(row["xp_earned"] == 0 for row in body["history"] if row["status"] != "COMPLETED")
    assert "pub-active" not in response_text(body)


def response_text(body) -> str:
    import json

    return json.dumps(body)


@pytest.mark.asyncio
async def test_celebrations_never_leak(client, auth_headers, other_user):
    """They are unacknowledged notifications addressed to their owner."""
    body = (await fetch(client, auth_headers, OTHER)).json()

    assert "celebrations" not in body
    assert "Level 2" not in response_text(body)


@pytest.mark.asyncio
async def test_an_unknown_user_is_a_404_not_an_empty_record(client, auth_headers):
    """An empty record would turn this into a way to enumerate accounts."""
    response = await fetch(client, auth_headers, "nobody-at-all")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "USER_NOT_FOUND"


@pytest.mark.asyncio
async def test_it_requires_a_session(client, other_user):
    response = await client.get(f"/missions/users/{OTHER}")

    assert response.status_code in (401, 403)
