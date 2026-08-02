"""CAL-004: the launch switch and the canary allowlist.

The suite runs with the feature on, so these tests turn it off and on again
themselves. They are the only place the gate is exercised, which is why they
assert the exact status code: a user outside the canary must not be able to
tell that missions exist.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import get_settings
from app.modules.missions import access
from app.modules.missions.indexes import apply_mission_indexes

EVENT_ID = 92001


@pytest.fixture
def flags(monkeypatch):
    """Set the launch flags for one test and restore the cache afterwards."""

    def configure(*, enabled: bool, allowlist: str = "") -> None:
        settings = get_settings()
        monkeypatch.setattr(settings, "missions_enabled", enabled, raising=False)
        monkeypatch.setattr(settings, "missions_allowlist", allowlist, raising=False)
        access.reset_cache()

    yield configure
    access.reset_cache()


@pytest.fixture
async def card(test_db, sample_event_data):
    await apply_mission_indexes(test_db)
    await test_db["events"].delete_many({"id": EVENT_ID})
    await test_db["events"].insert_one(
        {
            **sample_event_data,
            "id": EVENT_ID,
            "name": "UFC 501: Canary",
            "slug": "ufc-501-canary",
            "status": "scheduled",
            "date": datetime.now(UTC) + timedelta(days=4),
        }
    )
    return EVENT_ID


ROUTES = ("/missions/capabilities", "/missions/profile")


# ------------------------------------------------------------- the off switch


@pytest.mark.parametrize("route", ROUTES)
async def test_the_feature_is_invisible_while_disabled(
    client, auth_headers, flags, card, route
):
    flags(enabled=False)

    response = await client.get(route, headers=auth_headers)

    assert response.status_code == 404, "a dark feature must not announce itself"
    assert response.json()["detail"]["code"] == "MISSIONS_UNAVAILABLE"


async def test_home_is_unavailable_while_disabled(client, auth_headers, flags, card):
    flags(enabled=False)

    response = await client.get(
        f"/missions/home?event_id={EVENT_ID}", headers=auth_headers
    )

    assert response.status_code == 404


async def test_selection_is_refused_while_disabled(client, auth_headers, flags, card):
    flags(enabled=False)

    response = await client.post(
        "/missions/select",
        headers=auth_headers,
        json={
            "event_id": EVENT_ID,
            "slot": 1,
            "offer_id": "whatever",
            "idempotency_key": "gate-check-key",
        },
    )

    assert response.status_code == 404, "the off switch must stop writes, not just reads"


async def test_acknowledging_a_celebration_is_refused_while_disabled(
    client, auth_headers, flags
):
    flags(enabled=False)

    response = await client.post(
        "/missions/celebrations/anything/ack", headers=auth_headers
    )

    assert response.status_code == 404


# ------------------------------------------------------------------- canary


async def test_only_allowlisted_users_see_the_feature(
    client, auth_headers, flags, sample_user_data
):
    flags(enabled=True, allowlist="someone-else@example.com")

    response = await client.get("/missions/capabilities", headers=auth_headers)

    assert response.status_code == 404, "an allowlist excludes everyone not on it"


async def test_a_user_on_the_allowlist_by_email_gets_in(
    client, auth_headers, flags, sample_user_data
):
    flags(enabled=True, allowlist=f"nobody@example.com, {sample_user_data['email']}")

    response = await client.get("/missions/capabilities", headers=auth_headers)

    assert response.status_code == 200


async def test_a_user_on_the_allowlist_by_id_gets_in(
    client, auth_headers, flags, sample_user_data
):
    flags(enabled=True, allowlist=sample_user_data["google_id"])

    response = await client.get("/missions/capabilities", headers=auth_headers)

    assert response.status_code == 200


async def test_an_empty_allowlist_means_general_availability(
    client, auth_headers, flags
):
    """Going GA is emptying one variable, not shipping a release."""
    flags(enabled=True, allowlist="")

    response = await client.get("/missions/capabilities", headers=auth_headers)

    assert response.status_code == 200


async def test_the_off_switch_beats_the_allowlist(
    client, auth_headers, flags, sample_user_data
):
    """The 2am case: one variable stops it for everyone, canary included."""
    flags(enabled=False, allowlist=sample_user_data["email"])

    response = await client.get("/missions/capabilities", headers=auth_headers)

    assert response.status_code == 404


# ------------------------------------------------------------------ matching


@pytest.mark.parametrize(
    "allowlist",
    [
        "  Jose@Example.com  ",
        "jose@example.com;other@example.com",
        "other@example.com,jose@example.com",
    ],
)
def test_the_allowlist_tolerates_how_a_human_writes_one(flags, allowlist):
    """Spaces, semicolons and casing are how real allowlists arrive."""
    flags(enabled=True, allowlist=allowlist)

    assert access.user_can_see_missions("some-id", "jose@example.com") is True
    assert access.user_can_see_missions("some-id", "stranger@example.com") is False


def test_a_disabled_feature_is_closed_for_everyone(flags):
    flags(enabled=False, allowlist="jose@example.com")

    assert access.missions_enabled() is False
    assert access.user_can_see_missions("jose@example.com", "jose@example.com") is False


def test_canary_only_reports_whether_an_allowlist_is_active(flags):
    flags(enabled=True, allowlist="jose@example.com")
    assert access.canary_only() is True

    flags(enabled=True, allowlist="")
    assert access.canary_only() is False


async def test_admin_configuration_stays_reachable_while_the_feature_is_dark(
    client, test_db, flags
):
    """An operator must be able to configure the month before opening the gate."""
    from app.core.security import create_access_token

    await test_db["users"].update_one(
        {"_id": "gate-admin"},
        {"$set": {
            "google_id": "gate-admin", "email": "gate-admin@example.com",
            "name": "Gate Admin", "is_active": True, "is_admin": True,
            "created_at": datetime.now(UTC), "last_login_at": datetime.now(UTC),
        }},
        upsert=True,
    )
    headers = {
        "Authorization": "Bearer "
        + create_access_token("gate-admin", "gate-admin@example.com")
    }
    flags(enabled=False)

    response = await client.get(
        "/admin/missions/monthly/templates", headers=headers
    )

    assert response.status_code == 200
    assert len(response.json()) == 18
