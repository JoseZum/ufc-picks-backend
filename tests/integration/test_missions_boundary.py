"""Contract tests for the mission module HTTP boundary."""

import pytest

from app.modules.missions.contracts import (
    MISSION_API_VERSION,
    MISSION_CATALOG_VERSION,
)


@pytest.mark.asyncio
async def test_mission_capabilities_require_authentication(client):
    response = await client.get("/missions/capabilities")

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_mission_capabilities_publish_renderer_contract(
    client,
    auth_headers,
):
    response = await client.get(
        "/missions/capabilities",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json() == {
        "api_version": MISSION_API_VERSION,
        "catalog_version": MISSION_CATALOG_VERSION,
        "interaction_types": [
            "AUTO",
            "TARGET_FIGHTER",
            "TARGET_FIGHT",
            "COMBO_BUILDER",
            "CARD_PROP",
        ],
    }


@pytest.mark.asyncio
async def test_local_mongo_supports_mission_transactions(test_db):
    marker = {"_id": "mission-transaction-probe"}

    async with test_db.client.start_session() as session:
        async with await session.start_transaction():
            await test_db["mission_transaction_probes"].insert_one(
                marker,
                session=session,
            )

    assert await test_db["mission_transaction_probes"].find_one(marker)
