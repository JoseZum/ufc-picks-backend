"""Cancelar un bout tiene que sacarlo tambien de la card canonica.

El endpoint escribia solo el `status` legacy y el comando `bout_lifecycle`. El
motor de misiones no lee ese campo: `bout_evaluation._bout_snapshot` prefiere
`card_data_v1.status`, y la pertenencia a la card la decide el slot. Con la
pelea cancelada solo por fuera, seguia contando como `surviving`, asi que
`_finalize_if_complete` veia un bout sin resultado y la card no finalizaba
nunca -- las misiones se quedaban sin pagar XP.

Ocurrio en produccion el 2026-08-29 con el bout 401887538 (Ce Liu vs Junior
Tafa, evento 137846), que ESPN ya 404eaba.
"""

from datetime import UTC, datetime

import pytest

BOUT_ID = 67101
LEGACY_BOUT_ID = 67102
EVENT_ID = 67001

ADMIN_ID = "cancel-admin-user"
ADMIN_EMAIL = "cancel-admin@example.com"


@pytest.fixture
async def admin_headers(client, test_db):
    from app.core.security import create_access_token

    await test_db["users"].update_one(
        {"_id": ADMIN_ID},
        {
            "$set": {
                "google_id": ADMIN_ID,
                "email": ADMIN_EMAIL,
                "name": "Cancel Admin",
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
async def canonical_bout(test_db):
    """Un bout que ya cruzo la frontera CardData: tiene sidecar y slot."""
    await test_db["bouts"].delete_many({"id": BOUT_ID})
    await test_db["event_card_slots"].delete_many({"bout_id": BOUT_ID})
    await test_db["bouts"].insert_one(
        {
            "id": BOUT_ID,
            "event_id": EVENT_ID,
            "status": "scheduled",
            "fighters": {
                "red": {"fighter_name": "Red"},
                "blue": {"fighter_name": "Blue"},
            },
            "card_data_v1": {
                "bout_id": BOUT_ID,
                "event_id": EVENT_ID,
                "status": "scheduled",
                "scheduled_rounds": 3,
                "matchup_revision": 1,
                "fighters": [
                    {"fighter_id": "espn:1", "display_name": "Red", "corner": "red"},
                    {"fighter_id": "espn:2", "display_name": "Blue", "corner": "blue"},
                ],
            },
        }
    )
    await test_db["event_card_slots"].insert_one(
        {
            "event_id": EVENT_ID,
            "bout_id": BOUT_ID,
            "card_section": "main",
            "role": "regular",
            "is_current": True,
            "structure_revision": 1,
        }
    )


@pytest.fixture
async def legacy_bout(test_db):
    """Un bout viejo que nunca cruzo la frontera: no tiene sidecar."""
    await test_db["bouts"].delete_many({"id": LEGACY_BOUT_ID})
    await test_db["bouts"].insert_one(
        {
            "id": LEGACY_BOUT_ID,
            "event_id": EVENT_ID,
            "status": "scheduled",
            "fighters": {
                "red": {"fighter_name": "Old Red"},
                "blue": {"fighter_name": "Old Blue"},
            },
        }
    )


async def test_cancelling_removes_the_bout_from_the_canonical_card(
    client, admin_headers, test_db, canonical_bout
):
    response = await client.post(
        f"/admin/bouts/{BOUT_ID}/cancel", headers=admin_headers
    )
    assert response.status_code == 200

    bout = await test_db["bouts"].find_one({"id": BOUT_ID})
    assert bout["status"] == "cancelled"
    # Lo que lee el motor de misiones.
    assert bout["card_data_v1"]["status"] == "cancelled"

    slot = await test_db["event_card_slots"].find_one({"bout_id": BOUT_ID})
    assert slot["is_current"] is False


async def test_cancelling_records_both_commands_so_the_scraper_cannot_revive_it(
    client, admin_headers, test_db, canonical_bout
):
    await client.post(f"/admin/bouts/{BOUT_ID}/cancel", headers=admin_headers)

    commands = await test_db["admin_card_commands"].find(
        {"bout_id": BOUT_ID}
    ).to_list(length=None)
    by_kind = {command["kind"]: command for command in commands}

    # Sin el lifecycle, la proxima pasada de ESPN recalcula `status=scheduled`.
    assert by_kind["bout_lifecycle"]["values"] == {"status": "cancelled"}
    # Sin el structure, el reconciliador es dueno del slot y lo vuelve a marcar
    # `is_current`, con lo que el bout reaparece en la card.
    assert by_kind["bout_structure"]["values"] == {"is_current": False}


async def test_cancelling_a_legacy_bout_does_not_invent_a_sidecar(
    client, admin_headers, test_db, legacy_bout
):
    """Crear `card_data_v1` aqui haria que un bout legacy pase a pertenecer a la
    card (`_belongs_to_the_card`) con una proyeccion sin peleadores canonicos, y
    eso hace fallar la evaluacion de la card entera."""
    response = await client.post(
        f"/admin/bouts/{LEGACY_BOUT_ID}/cancel", headers=admin_headers
    )
    assert response.status_code == 200

    bout = await test_db["bouts"].find_one({"id": LEGACY_BOUT_ID})
    assert bout["status"] == "cancelled"
    assert "card_data_v1" not in bout
