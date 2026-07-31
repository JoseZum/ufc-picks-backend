from datetime import datetime, timezone

import pytest

from app.core.security import create_access_token


async def admin_headers(test_db) -> dict[str, str]:
    user_id = "admin-user"
    email = "admin@example.com"
    await test_db["users"].insert_one(
        {
            "_id": user_id,
            "google_id": user_id,
            "email": email,
            "name": "Admin",
            "created_at": datetime.now(timezone.utc),
            "last_login_at": datetime.now(timezone.utc),
            "is_active": True,
            "is_admin": True,
        }
    )
    token = create_access_token(user_id, email)
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_admin_timing_shifts_staged_sections(
    client,
    test_db,
    sample_event_data,
):
    headers = await admin_headers(test_db)
    sample_event_data.update(
        {
            "card_start_time_utc": datetime(2026, 8, 15, 21),
            "picks_lock_time_utc": datetime(2026, 8, 15, 21),
            "section_start_times_utc": {
                "early_prelim": datetime(2026, 8, 15, 21),
                "prelim": datetime(2026, 8, 15, 23),
                "main": datetime(2026, 8, 16, 1),
            },
            "section_lock_times_utc": {
                "early_prelim": datetime(2026, 8, 15, 21),
                "prelim": datetime(2026, 8, 15, 23),
                "main": datetime(2026, 8, 16, 1),
            },
        }
    )
    await test_db["events"].insert_one(sample_event_data)

    response = await client.put(
        f"/admin/events/{sample_event_data['id']}/timing",
        headers=headers,
        json={
            "card_start_time_utc": "2026-08-15T22:00:00Z",
            "picks_lock_time_utc": "2026-08-15T21:30:00Z",
        },
    )

    assert response.status_code == 200
    stored = await test_db["events"].find_one(
        {"id": sample_event_data["id"]}
    )
    assert stored["section_start_times_utc"]["main"] == datetime(
        2026, 8, 16, 2
    )
    assert stored["section_lock_times_utc"]["main"] == datetime(
        2026, 8, 16, 1, 30
    )
    assert stored["timing_source"] == "admin"


@pytest.mark.asyncio
async def test_full_event_unlock_preserves_individual_bout_lock(
    client,
    test_db,
    sample_event_data,
    sample_bout_data,
):
    headers = await admin_headers(test_db)
    await test_db["events"].insert_one(sample_event_data)
    sample_bout_data.update(
        {
            "picks_locked": True,
            "picks_lock_override": "locked",
        }
    )
    await test_db["bouts"].insert_one(sample_bout_data)
    await test_db["picks"].insert_one(
        {
            "_id": f"user:{sample_bout_data['id']}",
            "user_id": "user",
            "event_id": sample_event_data["id"],
            "bout_id": sample_bout_data["id"],
            "picked_fighter_name": "Test Fighter 1",
            "picked_method": "DEC",
            "locked": True,
            "created_at": datetime.now(timezone.utc),
        }
    )

    lock_response = await client.post(
        f"/admin/events/{sample_event_data['id']}/lock-picks",
        headers=headers,
    )
    assert lock_response.status_code == 200

    unlock_response = await client.post(
        f"/admin/events/{sample_event_data['id']}/unlock-picks",
        headers=headers,
    )
    assert unlock_response.status_code == 200

    event = await test_db["events"].find_one(
        {"id": sample_event_data["id"]}
    )
    bout = await test_db["bouts"].find_one(
        {"id": sample_bout_data["id"]}
    )
    pick = await test_db["picks"].find_one(
        {"_id": f"user:{sample_bout_data['id']}"}
    )
    assert event["picks_lock_override"] == "unlocked"
    assert bout["picks_lock_override"] == "locked"
    assert pick["locked"] is True


@pytest.mark.asyncio
async def test_non_admin_cannot_change_timing(
    client,
    auth_headers,
    test_db,
    sample_event_data,
):
    await test_db["events"].insert_one(sample_event_data)
    response = await client.put(
        f"/admin/events/{sample_event_data['id']}/timing",
        headers=auth_headers,
        json={"card_start_time_utc": "2026-08-15T22:00:00Z"},
    )
    assert response.status_code == 403
