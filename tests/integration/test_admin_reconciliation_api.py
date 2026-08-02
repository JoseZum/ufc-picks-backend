"""Slice 4: reconciliation previews without writing, and applies under CAS."""

from datetime import UTC, datetime, timedelta

import pytest

from app.modules.missions.indexes import apply_mission_indexes

USER = "drifted-user"
EVENT_ID = 78001
ADMIN_ID = "mission-admin-user"
ADMIN_EMAIL = "mission-admin@example.com"

REASON = "Repairing a cache that drifted after a crashed write"


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
async def drifted(test_db):
    """A user whose caches disagree with the append-only ledgers."""
    await apply_mission_indexes(test_db)
    for collection in (
        "mission_xp_ledger",
        "mission_user_progression",
        "mission_card_streaks",
        "mission_card_streak_cards",
        "mission_admin_audit",
    ):
        await test_db[collection].delete_many({})

    from app.modules.missions.application.xp_ledger import XpLedgerService
    from app.modules.missions.domain.xp import AwardXpCommand, XpSourceType

    ledger = XpLedgerService(test_db)
    for index in range(6):
        await ledger.award(
            user_id=USER,
            command=AwardXpCommand(
                idempotency_key=f"reconcile-seed-{index}",
                source_type=XpSourceType.CARD_MISSION,
                source_id=str(EVENT_ID),
                amount=1,
                reason="seed",
            ),
        )

    # The cache says level 1 / 0 XP; the ledger says 6 XP, which is level 2.
    await test_db["mission_user_progression"].insert_one(
        {
            "user_id": USER,
            "lifetime_xp": 0,
            "level": 1,
            "title": "BUM",
            "revision": 3,
        }
    )
    # Two advanced cards on record, but the counter says zero.
    now = datetime.now(UTC)
    await test_db["mission_card_streak_cards"].insert_many(
        [
            {
                "_id": f"streak:{USER}:{EVENT_ID + index}",
                "user_id": USER,
                "event_id": EVENT_ID + index,
                "outcome": "ADVANCED",
                "denominator": 10,
                "picked": 8,
                "coverage_percent": 80,
                "current_before": index,
                "current_after": index + 1,
                "best_after": index + 1,
                "xp_awarded": 1,
                "settled_at": now + timedelta(minutes=index),
            }
            for index in range(2)
        ]
    )
    await test_db["mission_card_streaks"].insert_one(
        {"user_id": USER, "current": 0, "best": 0, "revision": 1}
    )
    return test_db


async def preview(client, headers):
    return await client.get(
        f"/admin/missions/reconciliation/preview?user_id={USER}", headers=headers
    )


# ----------------------------------------------------------------- preview


async def test_preview_reports_the_drift_without_writing_anything(
    client, admin_headers, drifted
):
    response = await preview(client, admin_headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["converged"] is False
    assert body["safe_to_apply"] is True
    assert len(body["operations"]) == 2

    cache = await drifted["mission_user_progression"].find_one({"user_id": USER})
    assert cache["lifetime_xp"] == 0, "a preview must never write"
    assert cache["revision"] == 3


async def test_preview_recomputes_both_projections_from_their_ledgers(
    client, admin_headers, drifted
):
    body = (await preview(client, admin_headers)).json()

    by_type = {row["entity_type"]: row for row in body["operations"]}
    assert by_type["USER_PROGRESSION"]["after"]["lifetime_xp"] == 6
    assert by_type["USER_PROGRESSION"]["after"]["level"] == 2
    assert by_type["CARD_STREAK"]["after"] == {"current": 2, "best": 2}


async def test_a_converged_user_produces_no_operations(client, admin_headers, drifted):
    plan = (await preview(client, admin_headers)).json()
    await client.post(
        "/admin/missions/reconciliation/apply",
        headers=admin_headers,
        json={"plan_id": plan["plan_id"], "reason": REASON, "user_id": USER},
    )

    after = (await preview(client, admin_headers)).json()

    assert after["converged"] is True
    assert after["operations"] == []


async def test_a_scope_naming_nothing_is_rejected(client, admin_headers, drifted):
    response = await client.get(
        "/admin/missions/reconciliation/preview", headers=admin_headers
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "INVALID_SCOPE"


async def test_a_normal_user_cannot_preview_or_apply(client, auth_headers, drifted):
    read = await preview(client, auth_headers)
    write = await client.post(
        "/admin/missions/reconciliation/apply",
        headers=auth_headers,
        json={"plan_id": "whatever", "reason": REASON, "user_id": USER},
    )

    assert read.status_code == 403
    assert write.status_code == 403


# ------------------------------------------------------------------- apply


async def test_applying_the_reviewed_plan_repairs_both_caches(
    client, admin_headers, drifted
):
    plan = (await preview(client, admin_headers)).json()

    response = await client.post(
        "/admin/missions/reconciliation/apply",
        headers=admin_headers,
        json={"plan_id": plan["plan_id"], "reason": REASON, "user_id": USER},
    )

    assert response.status_code == 200, response.text
    assert response.json()["applied"] == 2
    assert response.json()["skipped"] == 0

    progression = await drifted["mission_user_progression"].find_one({"user_id": USER})
    streak = await drifted["mission_card_streaks"].find_one({"user_id": USER})
    assert (progression["lifetime_xp"], progression["level"]) == (6, 2)
    assert progression["revision"] == 4, "CAS advances the revision exactly once"
    assert (streak["current"], streak["best"]) == (2, 2)


async def test_a_plan_built_against_stale_state_is_refused(
    client, admin_headers, drifted
):
    """The operator approved a different set of changes; applying anyway is wrong."""
    plan = (await preview(client, admin_headers)).json()

    from app.modules.missions.application.xp_ledger import XpLedgerService
    from app.modules.missions.domain.xp import AwardXpCommand, XpSourceType

    await XpLedgerService(drifted).award(
        user_id=USER,
        command=AwardXpCommand(
            idempotency_key="reconcile-late-award",
            source_type=XpSourceType.CARD_MISSION,
            source_id=str(EVENT_ID),
            amount=5,
            reason="an award that landed after the preview",
        ),
    )

    response = await client.post(
        "/admin/missions/reconciliation/apply",
        headers=admin_headers,
        json={"plan_id": plan["plan_id"], "reason": REASON, "user_id": USER},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PLAN_STALE"
    cache = await drifted["mission_user_progression"].find_one({"user_id": USER})
    assert cache["lifetime_xp"] == 0, "a refused plan must not half-apply"


async def test_applying_without_a_reason_is_rejected(client, admin_headers, drifted):
    plan = (await preview(client, admin_headers)).json()

    response = await client.post(
        "/admin/missions/reconciliation/apply",
        headers=admin_headers,
        json={"plan_id": plan["plan_id"], "reason": "", "user_id": USER},
    )

    assert response.status_code == 422


async def test_an_apply_records_actor_reason_and_every_operation(
    client, admin_headers, drifted
):
    plan = (await preview(client, admin_headers)).json()
    await client.post(
        "/admin/missions/reconciliation/apply",
        headers=admin_headers,
        json={"plan_id": plan["plan_id"], "reason": REASON, "user_id": USER},
    )

    row = await drifted["mission_admin_audit"].find_one(
        {"action": "reconciliation.apply"}
    )

    assert row is not None
    assert row["actor_id"] == ADMIN_ID
    assert row["reason"] == REASON
    assert row["plan_id"] == plan["plan_id"]
    assert row["scope"]["user_id"] == USER
    assert len(row["operations"]) == 2
    assert all("before" in op and "after" in op for op in row["operations"])
