"""Endpoints that aggregate, which the PyMongo Async migration left broken.

`collection.aggregate()` returns a coroutine on PyMongo Async; on Motor it
returned the cursor directly. Five call sites kept the Motor shape and raised
`'coroutine' object has no attribute 'to_list'` on the next line — a 500 that
no test noticed because nothing exercised those routes.
"""

from datetime import UTC, datetime

import pytest

from app.core.security import create_access_token

USER = "aggregate-user"


@pytest.fixture
async def user_with_picks(test_db, sample_event_data):
    now = datetime.now(UTC)
    await test_db["users"].update_one(
        {"_id": USER},
        {"$set": {
            "google_id": USER, "email": f"{USER}@example.com", "name": "Aggregate",
            "is_active": True, "is_admin": False,
            "created_at": now, "last_login_at": now,
            "total_points": 4, "picks_total": 2, "picks_correct": 1,
        }},
        upsert=True,
    )
    # The pipeline joins picks to their bout and drops anything unmatched, so a
    # pick without its bout is invisible — the fixture needs both.
    await test_db["bouts"].delete_many({"event_id": 98001})
    await test_db["bouts"].insert_many([
        {
            "_id": 98101 + index, "id": 98101 + index, "event_id": 98001,
            "status": "completed", "weight_class": "Lightweight",
            "rounds_scheduled": 3, "card_section": "main", "card_order": index + 1,
            "fighters": {
                "red": {"fighter_name": "Someone", "corner": "red"},
                "blue": {"fighter_name": "Other", "corner": "blue"},
            },
            "result": {"winner": "red", "method": "KO/TKO", "round": 1},
        }
        for index in range(2)
    ])
    await test_db["picks"].delete_many({"user_id": USER})
    await test_db["picks"].insert_many([
        {
            "_id": f"{USER}:1", "user_id": USER, "event_id": 98001, "bout_id": 98101,
            "picked_fighter_name": "Someone", "picked_method": "KO/TKO",
            "picked_round": 1, "is_correct": True, "points_awarded": 3,
            "locked": False, "created_at": now,
        },
        {
            "_id": f"{USER}:2", "user_id": USER, "event_id": 98001, "bout_id": 98102,
            "picked_fighter_name": "Other", "picked_method": "DEC",
            "is_correct": False, "points_awarded": 0,
            "locked": False, "created_at": now,
        },
    ])
    return USER


@pytest.mark.asyncio
async def test_user_pick_stats_does_not_500(client, auth_headers, user_with_picks):
    response = await client.get(
        f"/users/{USER}/picks/stats", headers=auth_headers
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_picks"] == 2
    assert body["correct_picks"] == 1


@pytest.mark.asyncio
async def test_the_stats_payload_is_shaped_for_the_profile_page(
    client, auth_headers, user_with_picks
):
    """The page reads `accuracy` with `toFixed(0)`, so it must be a percentage."""
    body = (
        await client.get(f"/users/{USER}/picks/stats", headers=auth_headers)
    ).json()

    assert 0 <= body["accuracy"] <= 100
    assert body["accuracy"] == pytest.approx(50, abs=1), body
