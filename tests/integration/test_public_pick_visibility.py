from datetime import datetime, timezone

import pytest


@pytest.mark.asyncio
async def test_locked_pick_stays_private_until_result(
    client,
    test_db,
    sample_event_data,
    sample_bout_data,
):
    user_id = "public-user"
    await test_db["users"].insert_one(
        {
            "_id": user_id,
            "name": "Public User",
            "created_at": datetime.now(timezone.utc),
        }
    )
    await test_db["events"].insert_one(sample_event_data)
    await test_db["bouts"].insert_one(sample_bout_data)
    await test_db["picks"].insert_one(
        {
            "_id": f"{user_id}:{sample_bout_data['id']}",
            "user_id": user_id,
            "event_id": sample_event_data["id"],
            "bout_id": sample_bout_data["id"],
            "picked_fighter_name": "Test Fighter 1",
            "picked_method": "DEC",
            "picked_round": None,
            "is_correct": None,
            "points_awarded": 0,
            "locked": True,
            "created_at": datetime.now(timezone.utc),
        }
    )

    private_response = await client.get(f"/users/{user_id}/picks")
    assert private_response.status_code == 200
    assert private_response.json() == []

    await test_db["bouts"].update_one(
        {"id": sample_bout_data["id"]},
        {
            "$set": {
                "status": "completed",
                "result": {
                    "winner": "red",
                    "method": "DEC",
                    "round": 3,
                },
            }
        },
    )

    public_response = await client.get(f"/users/{user_id}/picks")
    assert public_response.status_code == 200
    assert len(public_response.json()) == 1
    assert public_response.json()[0]["locked"] is True
    assert public_response.json()[0]["result"]["winner"] == "red"
