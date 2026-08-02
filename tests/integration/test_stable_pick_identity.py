"""B-009: ordinary picks must carry a stable fighter id, not just a display name.

Names drift across corrections and fighter replacements; ids do not. These tests
pin the two halves of the contract: new writes resolve and persist the canonical
id, and scoring trusts the id over the name whenever both sides have one.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.models.pick import PickCreate
from app.services.pick_service import PickService
from app.services.points_service import PointsService

EVENT_ID = 66001
BOUT_ID = 66101
USER = "identity-user"


@pytest.fixture
async def card(test_db, sample_event_data, sample_bout_data):
    for collection in ("events", "bouts", "picks"):
        await test_db[collection].delete_many({})
    await test_db["events"].insert_one(
        {
            **sample_event_data,
            "id": EVENT_ID,
            "name": "UFC 402: Identity",
            "slug": "ufc-402-identity",
            "status": "scheduled",
            "date": datetime.now(UTC) + timedelta(days=5),
        }
    )
    await test_db["bouts"].insert_one(
        {
            **sample_bout_data,
            "_id": f"bout-{BOUT_ID}",
            "id": BOUT_ID,
            "event_id": EVENT_ID,
            "status": "scheduled",
            "fighters": {
                "red": {"fighter_name": "Alex Pereira"},
                "blue": {"fighter_name": "Jiri Prochazka"},
            },
            "card_data_v1": {
                "bout_id": BOUT_ID,
                "event_id": EVENT_ID,
                "matchup_revision": 1,
                "status": "scheduled",
                "fighters": [
                    {
                        "fighter_id": "fighter-pereira",
                        "display_name": "Alex Pereira",
                        "corner": "red",
                    },
                    {
                        "fighter_id": "fighter-prochazka",
                        "display_name": "Jiri Prochazka",
                        "corner": "blue",
                    },
                ],
                "scheduled_rounds": 5,
                "is_title_fight": True,
                "result_revision": 0,
                "result": None,
            },
        }
    )
    return test_db


def create(name: str = "Alex Pereira", method: str = "KO/TKO", round_: int | None = 2):
    return PickCreate(
        event_id=EVENT_ID,
        bout_id=BOUT_ID,
        picked_fighter_name=name,
        picked_method=method,
        picked_round=round_,
    )


async def test_a_new_pick_persists_the_canonical_fighter_id(card):
    service = PickService(card)

    pick = await service.create_or_update_pick(USER, create())

    assert pick.picked_fighter_name == "Alex Pereira"
    assert pick.picked_fighter_id == "fighter-pereira"

    stored = await card["picks"].find_one({"_id": f"{USER}:{BOUT_ID}"})
    assert stored["picked_fighter_id"] == "fighter-pereira"


async def test_changing_the_pick_moves_the_id_with_the_name(card):
    service = PickService(card)
    await service.create_or_update_pick(USER, create())

    updated = await service.create_or_update_pick(
        USER, create(name="Jiri Prochazka")
    )

    assert updated.picked_fighter_name == "Jiri Prochazka"
    assert updated.picked_fighter_id == "fighter-prochazka"


async def test_a_bout_without_canonical_data_stores_a_null_id_not_a_guess(card):
    await card["bouts"].update_one({"id": BOUT_ID}, {"$unset": {"card_data_v1": ""}})
    service = PickService(card)

    pick = await service.create_or_update_pick(USER, create())

    assert pick.picked_fighter_name == "Alex Pereira"
    assert pick.picked_fighter_id is None


async def test_an_ambiguous_name_refuses_to_resolve_an_id(card):
    await card["bouts"].update_one(
        {"id": BOUT_ID},
        {
            "$set": {
                "card_data_v1.fighters": [
                    {
                        "fighter_id": "fighter-a",
                        "display_name": "Alex Pereira",
                        "corner": "red",
                    },
                    {
                        "fighter_id": "fighter-b",
                        "display_name": "Alex Pereira",
                        "corner": "blue",
                    },
                ]
            }
        },
    )
    service = PickService(card)

    pick = await service.create_or_update_pick(USER, create())

    assert pick.picked_fighter_id is None, "a wrong id is worse than no id"


async def test_scoring_prefers_the_id_when_the_display_name_drifted(card):
    """A correction renamed the fighter; the id still identifies the same person."""
    points = PointsService(card)
    pick = {
        "picked_fighter_id": "fighter-pereira",
        "picked_fighter_name": "A. Pereira",
        "picked_method": "KO/TKO",
        "picked_round": 2,
    }

    assert points.winner_matches(
        pick, "Alex Pereira Jr.", "fighter-pereira"
    ) is True
    assert (
        await points.calculate_points(
            pick,
            "Alex Pereira Jr.",
            "KO/TKO",
            2,
            winner_fighter_id="fighter-pereira",
        )
        == 3
    )


async def test_scoring_rejects_a_name_match_when_the_ids_disagree(card):
    """Same name, different person — the id is authoritative and fails closed."""
    points = PointsService(card)
    pick = {
        "picked_fighter_id": "fighter-a",
        "picked_fighter_name": "Alex Pereira",
        "picked_method": "KO/TKO",
    }

    assert points.winner_matches(pick, "Alex Pereira", "fighter-b") is False
    assert (
        await points.calculate_points(
            pick, "Alex Pereira", "KO/TKO", None, winner_fighter_id="fighter-b"
        )
        == 0
    )


async def test_legacy_picks_without_an_id_still_score_by_name(card):
    points = PointsService(card)
    legacy = {"picked_fighter_name": "Alex Pereira", "picked_method": "KO/TKO"}

    assert points.winner_matches(legacy, "Alex Pereira", "fighter-pereira") is True
    assert (
        await points.calculate_points(
            legacy,
            "Alex Pereira",
            "KO/TKO",
            None,
            winner_fighter_id="fighter-pereira",
        )
        == 2
    )


async def test_a_result_without_a_canonical_id_still_scores_by_name(card):
    points = PointsService(card)
    pick = {
        "picked_fighter_id": "fighter-pereira",
        "picked_fighter_name": "Alex Pereira",
        "picked_method": "DEC",
    }

    assert points.winner_matches(pick, "Alex Pereira", None) is True
