"""Slice 2: Admin picks a future monthly mission and it shows up for users."""

from datetime import UTC, datetime, timedelta

import pytest

from app.modules.missions.indexes import apply_mission_indexes

NEXT_MONTH_START = (
    datetime.now(UTC).replace(day=28, hour=0, minute=0, second=0, microsecond=0)
    + timedelta(days=4)
).replace(day=1)
MONTH = NEXT_MONTH_START.strftime("%Y-%m")


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
async def clean(test_db):
    await apply_mission_indexes(test_db)
    for collection in (
        "mission_monthly_configs",
        "mission_monthly_progress",
        "mission_admin_audit",
    ):
        await test_db[collection].delete_many({})
    return test_db


async def test_templates_require_admin(client, auth_headers, clean):
    response = await client.get("/admin/missions/monthly/templates", headers=auth_headers)

    assert response.status_code == 403


async def test_admin_sees_the_eighteen_reviewed_templates(
    client, admin_headers, clean
):
    response = await client.get(
        "/admin/missions/monthly/templates", headers=admin_headers
    )

    assert response.status_code == 200
    templates = response.json()
    assert len(templates) == 18
    assert all(template["xp"] == 15 for template in templates)
    for template in templates:
        assert template["parameters"]
        for parameter in template["parameters"]:
            assert parameter["minimum"] <= parameter["default"] <= parameter["maximum"]


async def test_admin_configures_a_future_month_with_reviewed_defaults(
    client, admin_headers, clean
):
    response = await client.put(
        f"/admin/missions/monthly/{MONTH}",
        headers=admin_headers,
        json={"mission_id": "MONTH-V2-001"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["month_key"] == MONTH
    assert body["state"] == "DRAFT"
    assert body["xp"] == 15
    assert body["parameters"] == {"winner_target": 15}
    assert body["editable"] is True


async def test_admin_can_change_the_choice_while_it_is_a_draft(
    client, admin_headers, clean
):
    await client.put(
        f"/admin/missions/monthly/{MONTH}",
        headers=admin_headers,
        json={"mission_id": "MONTH-V2-001"},
    )

    response = await client.put(
        f"/admin/missions/monthly/{MONTH}",
        headers=admin_headers,
        json={"mission_id": "MONTH-V3-008", "parameters": {"point_target": 60}},
    )

    assert response.status_code == 200, response.text
    assert response.json()["mission_id"] == "MONTH-V3-008"
    assert response.json()["parameters"] == {"point_target": 60}


async def test_a_parameter_outside_its_bounds_is_rejected(
    client, admin_headers, clean
):
    response = await client.put(
        f"/admin/missions/monthly/{MONTH}",
        headers=admin_headers,
        json={"mission_id": "MONTH-V2-001", "parameters": {"winner_target": 999}},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "INVALID_PARAMETERS"


async def test_an_unknown_template_is_rejected(client, admin_headers, clean):
    response = await client.put(
        f"/admin/missions/monthly/{MONTH}",
        headers=admin_headers,
        json={"mission_id": "MONTH-V9-999"},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "UNKNOWN_MISSION"


async def test_activating_freezes_the_configuration(client, admin_headers, clean):
    await client.put(
        f"/admin/missions/monthly/{MONTH}",
        headers=admin_headers,
        json={"mission_id": "MONTH-V2-001"},
    )

    activated = await client.post(
        f"/admin/missions/monthly/{MONTH}/activate", headers=admin_headers
    )
    assert activated.status_code == 200, activated.text
    assert activated.json()["state"] == "ACTIVE"
    assert activated.json()["editable"] is False

    blocked = await client.put(
        f"/admin/missions/monthly/{MONTH}",
        headers=admin_headers,
        json={"mission_id": "MONTH-V2-001", "parameters": {"winner_target": 5}},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "CONFIG_FROZEN"


async def test_activation_is_idempotent(client, admin_headers, clean):
    await client.put(
        f"/admin/missions/monthly/{MONTH}",
        headers=admin_headers,
        json={"mission_id": "MONTH-V2-001"},
    )

    first = await client.post(
        f"/admin/missions/monthly/{MONTH}/activate", headers=admin_headers
    )
    second = await client.post(
        f"/admin/missions/monthly/{MONTH}/activate", headers=admin_headers
    )

    assert first.json()["activated_at"] == second.json()["activated_at"]


async def test_a_running_month_refuses_to_close_without_force(
    client, admin_headers, clean
):
    await client.put(
        f"/admin/missions/monthly/{MONTH}",
        headers=admin_headers,
        json={"mission_id": "MONTH-V2-001"},
    )
    await client.post(f"/admin/missions/monthly/{MONTH}/activate", headers=admin_headers)

    refused = await client.post(
        f"/admin/missions/monthly/{MONTH}/close", headers=admin_headers
    )
    forced = await client.post(
        f"/admin/missions/monthly/{MONTH}/close?force=true", headers=admin_headers
    )

    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "MONTH_NOT_FINISHED"
    assert forced.status_code == 200
    assert forced.json()["state"] == "CLOSED"


async def test_a_missing_month_is_a_404(client, admin_headers, clean):
    response = await client.get(
        "/admin/missions/monthly/2026-09", headers=admin_headers
    )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "CONFIG_NOT_FOUND"


async def test_every_mutation_is_audited(client, admin_headers, clean, test_db):
    await client.put(
        f"/admin/missions/monthly/{MONTH}",
        headers=admin_headers,
        json={"mission_id": "MONTH-V2-001"},
    )
    await client.post(f"/admin/missions/monthly/{MONTH}/activate", headers=admin_headers)
    await client.post(
        f"/admin/missions/monthly/{MONTH}/close?force=true", headers=admin_headers
    )

    actions = [
        row["action"]
        async for row in test_db["mission_admin_audit"].find({"month_key": MONTH})
    ]

    assert sorted(actions) == ["monthly.activate", "monthly.close", "monthly.upsert"]


async def test_an_active_month_reaches_the_user_home(
    client, admin_headers, auth_headers, clean, test_db, sample_event_data
):
    await test_db["events"].delete_many({"id": 44001})
    await test_db["events"].insert_one(
        {
            **sample_event_data,
            "id": 44001,
            "name": "UFC 404: Monthly",
            "slug": "ufc-404-monthly",
            "status": "scheduled",
                "date": NEXT_MONTH_START.replace(day=20),
        }
    )
    await client.put(
        f"/admin/missions/monthly/{MONTH}",
        headers=admin_headers,
        json={"mission_id": "MONTH-V2-001"},
    )

    before = await client.get("/missions/home?event_id=44001", headers=auth_headers)
    assert before.json()["monthly"] is None, "a DRAFT month must not be advertised"

    activated = await client.post(
        f"/admin/missions/monthly/{MONTH}/activate", headers=admin_headers
    )
    assert activated.status_code == 200, activated.text
    assert activated.json()["state"] == "ACTIVE", activated.text
    after = await client.get("/missions/home?event_id=44001", headers=auth_headers)

    monthly = after.json()["monthly"]
    assert monthly is not None, after.text
    assert monthly is not None
    assert monthly["mission_id"] == "MONTH-V2-001"
    assert monthly["name"] == "WIN TARGET"
    assert monthly["xp"] == 15
    assert monthly["month_key"] == MONTH
