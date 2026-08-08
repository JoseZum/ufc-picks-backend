"""El resultado lo escribe el scraper y la misión se evalúa igual.

El scraper no llama a `PUT /admin/bouts/{id}/result`: escribe `bouts` directo en
Mongo con su sidecar canónico. Estos tests reproducen ese write exacto y
comprueban que el barrido de reconciliación cierra el hueco, que no paga dos
veces y que no puede alcanzar el histórico.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.modules.missions.application import MissionSelectionService
from app.modules.missions.application.read_models import MissionReadService
from app.modules.missions.application.result_reconciliation import (
    WATERMARK_COLLECTION,
    MissionResultReconciler,
)
from app.modules.missions.catalog import load_card_catalog
from app.modules.missions.domain.selections import SelectMissionCommand
from app.modules.missions.indexes import apply_mission_indexes

EVENT_ID = 56001
BOUT_IDS = (56101, 56102, 56103)
OFFER_SECRET = b"reconcile-tests-offer-secret-000000000000000"


def _bout(index: int, event_id: int = EVENT_ID) -> dict:
    bout_id = BOUT_IDS[index]
    return {
        "_id": f"bout-{bout_id}",
        "id": bout_id,
        "event_id": event_id,
        "status": "scheduled",
        "fighters": {
            "red": {"fighter_name": f"Red {index}"},
            "blue": {"fighter_name": f"Blue {index}"},
        },
        "card_data_v1": {
            "bout_id": bout_id,
            "event_id": event_id,
            "matchup_revision": 1,
            "status": "scheduled",
            "fighters": [
                {
                    "fighter_id": f"fighter-{bout_id}-red",
                    "display_name": f"Red {index}",
                    "corner": "red",
                },
                {
                    "fighter_id": f"fighter-{bout_id}-blue",
                    "display_name": f"Blue {index}",
                    "corner": "blue",
                },
            ],
            "scheduled_rounds": 3,
            "is_title_fight": False,
            "result_revision": 0,
            "result": None,
        },
    }


def _slot(index: int) -> dict:
    bout_id = BOUT_IDS[index]
    return {
        "_id": f"{EVENT_ID}:{bout_id}",
        "event_id": EVENT_ID,
        "bout_id": bout_id,
        "is_current": True,
        "card_section": "main",
        "order_overall": index + 1,
        "order_section": index + 1,
        "role": "main_event" if index == 0 else "co_main" if index == 1 else "regular",
        "structure_revision": 1,
    }


@pytest.fixture
async def card(test_db, sample_event_data):
    await apply_mission_indexes(test_db)
    for collection in (
        "events",
        "bouts",
        "picks",
        "event_card_slots",
        "mission_assignments",
        "mission_offer_sets",
        "mission_xp_ledger",
        "mission_card_finalization_runs",
        "mission_evaluation_runs",
        "mission_celebrations",
        WATERMARK_COLLECTION,
    ):
        await test_db[collection].delete_many({})
    await test_db["events"].insert_one(
        {
            **sample_event_data,
            "id": EVENT_ID,
            "name": "UFC: Reconcile",
            "slug": "ufc-reconcile",
            "status": "scheduled",
            "date": datetime.now(UTC),
        }
    )
    await test_db["bouts"].insert_many([_bout(i) for i in range(3)])
    await test_db["event_card_slots"].insert_many([_slot(i) for i in range(3)])
    return EVENT_ID


async def _select_auto(test_db, user_id: str) -> str:
    reader = MissionReadService(test_db, offer_secret=OFFER_SECRET)
    home = await reader.home(user_id=user_id, event_id=EVENT_ID)
    slot, offer = next(
        (s.slot, o)
        for s in home.slots
        for o in s.options
        if o.interaction.value == "AUTO"
    )
    result = await MissionSelectionService(test_db, load_card_catalog()).select(
        user_id=user_id,
        command=SelectMissionCommand(
            event_id=EVENT_ID,
            slot=slot,
            offer_set_id=home.offer_set_id,
            offer_id=offer.offer_id,
            idempotency_key=f"reconcile-slot-{slot}",
            selection={"kind": "AUTO"},
        ),
    )
    return result.assignment_id


async def scraper_writes_result(test_db, bout_id: int, *, revision: int = 1) -> None:
    """El write del scraper: `canonical_card_writer` sobre `bouts`, sin trigger.

    `card_data_v1.status` pasa a `completed` junto con el resultado: así están
    los 38 bouts con resultado canónico en producción, y el contrato del
    snapshot rechaza un bout con resultado cuyo lifecycle siga en `scheduled`
    (`domain/evaluation.py:102`).
    """
    await test_db["bouts"].update_one(
        {"id": bout_id},
        {
            "$set": {
                "status": "completed",
                "card_data_v1.status": "completed",
                "result": {
                    "winner": "red",
                    "winner_name": "Red",
                    "method": "KO/TKO",
                    "round": 1,
                    "time": "1:23",
                    "source": "card_data_v1",
                },
                "card_data_v1.result": {
                    "outcome": "red_win",
                    "winner_fighter_id": f"fighter-{bout_id}-red",
                    "method_family": "ko_tko",
                    "ending_round": 1,
                    "ending_time_seconds": 83,
                    "revision": revision,
                    "status": "final",
                },
                "card_data_v1.result_revision": revision,
            }
        },
    )


async def test_a_scraper_result_moves_the_mission_after_reconciling(
    test_db, card, sample_user_data
):
    assignment_id = await _select_auto(test_db, sample_user_data["google_id"])
    await scraper_writes_result(test_db, BOUT_IDS[0])

    frozen = await test_db["mission_assignments"].find_one({"_id": assignment_id})
    assert frozen["progress"] == {}, "sin reconciliar no deberia haberse movido"

    report = await MissionResultReconciler(test_db).reconcile(event_id=EVENT_ID)

    assert report.triggered == 1
    assert report.evaluated_assignments == 1
    assert report.errors == []
    moved = await test_db["mission_assignments"].find_one({"_id": assignment_id})
    assert moved["progress"], "el barrido no llego al evaluador"
    assert moved["revision"] > frozen["revision"]


async def test_running_it_twice_pays_nothing_extra(test_db, card, sample_user_data):
    await _select_auto(test_db, sample_user_data["google_id"])
    await scraper_writes_result(test_db, BOUT_IDS[0])

    reconciler = MissionResultReconciler(test_db)
    first = await reconciler.reconcile(event_id=EVENT_ID)
    second = await reconciler.reconcile(event_id=EVENT_ID)

    assert first.triggered == 1
    assert second.scanned == 0, "la marca de agua no evito el reproceso"
    assert second.triggered == 0
    assert await test_db["mission_evaluation_runs"].count_documents({}) == 1
    ledger = await test_db["mission_xp_ledger"].count_documents({})
    assert ledger <= 1


async def test_a_corrected_result_is_picked_up_again(test_db, card, sample_user_data):
    """Una correccion sube la revision, y esa sí es trabajo nuevo."""
    await _select_auto(test_db, sample_user_data["google_id"])
    await scraper_writes_result(test_db, BOUT_IDS[0], revision=1)
    reconciler = MissionResultReconciler(test_db)
    await reconciler.reconcile(event_id=EVENT_ID)

    await scraper_writes_result(test_db, BOUT_IDS[0], revision=2)
    again = await reconciler.reconcile(event_id=EVENT_ID)

    assert again.triggered == 1
    assert await test_db[WATERMARK_COLLECTION].count_documents({}) == 2


async def test_the_whole_card_finalizes_without_anyone_registering_results(
    test_db, card, sample_user_data
):
    await _select_auto(test_db, sample_user_data["google_id"])
    for bout_id in BOUT_IDS:
        await scraper_writes_result(test_db, bout_id)

    report = await MissionResultReconciler(test_db).reconcile(event_id=EVENT_ID)

    assert report.triggered == 3
    # Se cuenta por evento: reproducir una card ya completa devuelve
    # `card_finalized` en los tres bouts, pero solo se finaliza una vez.
    assert report.cards_finalized == 1
    assert await test_db["mission_card_finalization_runs"].count_documents({}) == 1


async def test_the_window_keeps_the_backlog_out_of_reach(test_db, card):
    """La proteccion que importa: 277 resultados historicos sin evaluar.

    Un barrido sin ventana pagaria XP y moveria rachas de hace dos años.
    """
    old_event = 56999
    await test_db["events"].insert_one(
        {
            "id": old_event,
            "name": "UFC: Ancient",
            "slug": "ancient",
            "status": "completed",
            "date": datetime.now(UTC) - timedelta(days=400),
        }
    )
    old_bout = dict(_bout(0, event_id=old_event))
    old_bout["_id"] = "bout-old"
    old_bout["id"] = 56901
    await test_db["bouts"].insert_one(old_bout)
    await scraper_writes_result(test_db, 56901)

    reconciler = MissionResultReconciler(test_db)
    default_scope = await reconciler.pending(window_days=3)
    assert 56901 not in {item.bout_id for item in default_scope}

    widened = await reconciler.pending(window_days=500)
    assert 56901 in {item.bout_id for item in widened}


async def test_dry_run_reports_without_touching_anything(
    test_db, card, sample_user_data
):
    assignment_id = await _select_auto(test_db, sample_user_data["google_id"])
    await scraper_writes_result(test_db, BOUT_IDS[0])

    report = await MissionResultReconciler(test_db).reconcile(
        event_id=EVENT_ID, dry_run=True
    )

    assert report.scanned == 1
    assert report.triggered == 0
    assert await test_db[WATERMARK_COLLECTION].count_documents({}) == 0
    untouched = await test_db["mission_assignments"].find_one({"_id": assignment_id})
    assert untouched["progress"] == {}


async def test_bouts_are_replayed_in_card_order(test_db, card, sample_user_data):
    """Rachas y finalizacion leen lo que dejo la pelea anterior."""
    await _select_auto(test_db, sample_user_data["google_id"])
    for bout_id in reversed(BOUT_IDS):
        await scraper_writes_result(test_db, bout_id)

    pending = await MissionResultReconciler(test_db).pending(event_id=EVENT_ID)

    assert [item.bout_id for item in pending] == list(BOUT_IDS)


async def test_the_endpoint_refuses_without_a_token(client, test_db, card):
    response = await client.post("/admin/missions/evaluation/reconcile")
    assert response.status_code in (401, 503)


async def test_the_endpoint_runs_with_the_service_token(
    client, test_db, card, sample_user_data, monkeypatch
):
    from app.core.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("MISSION_RECONCILE_TOKEN", "s3cr3t-token")
    get_settings.cache_clear()

    await _select_auto(test_db, sample_user_data["google_id"])
    await scraper_writes_result(test_db, BOUT_IDS[0])

    bad = await client.post(
        "/admin/missions/evaluation/reconcile",
        headers={"X-Mission-Reconcile-Token": "wrong"},
        params={"event_id": EVENT_ID},
    )
    assert bad.status_code == 401

    good = await client.post(
        "/admin/missions/evaluation/reconcile",
        headers={"X-Mission-Reconcile-Token": "s3cr3t-token"},
        params={"event_id": EVENT_ID},
    )
    assert good.status_code == 200, good.text
    assert good.json()["triggered"] == 1

    get_settings.cache_clear()
