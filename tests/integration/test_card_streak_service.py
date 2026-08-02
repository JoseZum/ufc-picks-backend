"""Slice 3: the single Card Streak settles once per card and pays exactly once."""

from datetime import UTC, datetime

import pytest

from app.modules.missions.application.card_streak import CardStreakService
from app.modules.missions.domain.streak import CardStreakOutcome
from app.modules.missions.indexes import apply_mission_indexes

EVENT_ID = 55001
USER = "streak-user"


@pytest.fixture
async def db(test_db):
    await apply_mission_indexes(test_db)
    for collection in (
        "mission_card_streaks",
        "mission_card_streak_cards",
        "mission_card_streak_denominators",
        "mission_xp_ledger",
        "mission_celebrations",
        "bouts",
        "picks",
    ):
        await test_db[collection].delete_many({})
    return test_db


@pytest.fixture
def service(db):
    return CardStreakService(db, clock=lambda: datetime(2026, 8, 15, tzinfo=UTC))


async def card(db, *, event_id: int = EVENT_ID, active: int = 10, cancelled: int = 0):
    documents = [
        {
            "id": event_id * 100 + index,
            "event_id": event_id,
            "status": "scheduled",
            "card_data_v1": {"lifecycle": "SCHEDULED", "is_current": True},
        }
        for index in range(active)
    ]
    documents += [
        {
            "id": event_id * 100 + 90 + index,
            "event_id": event_id,
            "status": "cancelled",
            "card_data_v1": {"lifecycle": "CANCELLED", "is_current": False},
        }
        for index in range(cancelled)
    ]
    await db["bouts"].insert_many(documents)
    return [document["id"] for document in documents[:active]]


async def pick(db, user_id: str, bout_ids, *, event_id: int = EVENT_ID):
    if not bout_ids:
        return
    await db["picks"].insert_many(
        [
            {
                "_id": f"{user_id}:{bout_id}",
                "user_id": user_id,
                "event_id": event_id,
                "bout_id": bout_id,
                "picked_fighter_name": "Someone",
            }
            for bout_id in bout_ids
        ]
    )


# ----------------------------------------------------------------- denominator


async def test_the_denominator_counts_only_active_bouts(service, db):
    await card(db, active=8, cancelled=3)

    denominator, bout_ids = await service.capture_denominator(EVENT_ID)

    assert denominator == 8
    assert len(bout_ids) == 8


async def test_a_cancellation_after_the_freeze_does_not_rewrite_it(service, db):
    """D-DATA-003: the denominator is snapshotted, not recomputed."""
    bout_ids = await card(db, active=10)
    first, _ = await service.capture_denominator(EVENT_ID)

    await db["bouts"].update_many(
        {"id": {"$in": bout_ids[:4]}},
        {"$set": {"card_data_v1.lifecycle": "CANCELLED", "card_data_v1.is_current": False}},
    )
    second, _ = await service.capture_denominator(EVENT_ID)

    assert (first, second) == (10, 10)


async def test_a_bout_without_canonical_data_still_counts(service, db):
    await db["bouts"].insert_many(
        [
            {"id": 700 + index, "event_id": EVENT_ID, "status": "scheduled"}
            for index in range(5)
        ]
    )

    denominator, _ = await service.capture_denominator(EVENT_ID)

    assert denominator == 5


# ---------------------------------------------------------------------- settle


async def test_covering_more_than_half_advances_and_pays_one_xp(service, db):
    bout_ids = await card(db, active=10)
    await pick(db, USER, bout_ids[:6])

    settlement = await service.settle_card(EVENT_ID)

    assert (settlement.advanced, settlement.broken) == (1, 0)
    assert settlement.xp_awarded == 1
    state = await service.state_for(USER)
    assert (state.current, state.best) == (1, 1)
    assert await db["mission_xp_ledger"].count_documents({"user_id": USER}) == 1


async def test_exactly_half_does_not_advance(service, db):
    bout_ids = await card(db, active=10)
    await pick(db, USER, bout_ids[:5])

    await service.settle_card(EVENT_ID)

    state = await service.state_for(USER)
    assert state.current == 0
    assert await db["mission_xp_ledger"].count_documents({"user_id": USER}) == 0


async def test_a_user_who_ignored_the_card_loses_their_streak(service, db):
    await db["mission_card_streaks"].insert_one(
        {"user_id": USER, "current": 6, "best": 6}
    )
    await card(db, active=10)

    settlement = await service.settle_card(EVENT_ID)

    state = await service.state_for(USER)
    assert settlement.broken == 1
    assert state.current == 0
    assert state.best == 6, "the record survives the break"


async def test_a_user_with_no_streak_and_no_picks_is_left_alone(service, db):
    await card(db, active=10)

    settlement = await service.settle_card(EVENT_ID)

    assert (settlement.advanced, settlement.broken, settlement.unchanged) == (0, 0, 0)
    assert await db["mission_card_streaks"].count_documents({}) == 0


async def test_a_card_with_no_active_bouts_settles_nobody(service, db):
    await db["mission_card_streaks"].insert_one(
        {"user_id": USER, "current": 4, "best": 4}
    )
    await card(db, active=0, cancelled=5)

    settlement = await service.settle_card(EVENT_ID)

    assert settlement.denominator == 0
    assert (await service.state_for(USER)).current == 4


# ----------------------------------------------------------------- idempotency


async def test_settling_the_same_card_twice_advances_it_once(service, db):
    bout_ids = await card(db, active=10)
    await pick(db, USER, bout_ids[:8])

    await service.settle_card(EVENT_ID)
    second = await service.settle_card(EVENT_ID)

    state = await service.state_for(USER)
    assert state.current == 1
    assert second.advanced == 0
    assert second.skipped == 1
    assert await db["mission_xp_ledger"].count_documents({"user_id": USER}) == 1


async def test_editing_picks_after_settlement_cannot_re_advance(service, db):
    bout_ids = await card(db, active=10)
    await pick(db, USER, bout_ids[:6])
    await service.settle_card(EVENT_ID)

    await pick(db, USER, bout_ids[6:], event_id=EVENT_ID)
    await service.settle_card(EVENT_ID)

    assert (await service.state_for(USER)).current == 1


async def test_a_clean_settlement_marks_the_card_closed(service, db):
    bout_ids = await card(db, active=10)
    await pick(db, USER, bout_ids[:9])

    assert await service.is_settled(EVENT_ID) is False
    await service.settle_card(EVENT_ID)
    assert await service.is_settled(EVENT_ID) is True


# ------------------------------------------------------------------ milestones


async def test_the_third_card_pays_the_milestone_and_a_full_screen_celebration(
    service, db
):
    await db["mission_card_streaks"].insert_one(
        {"user_id": USER, "current": 2, "best": 2}
    )
    bout_ids = await card(db, active=10)
    await pick(db, USER, bout_ids[:7])

    settlement = await service.settle_card(EVENT_ID)

    assert settlement.xp_awarded == 3, "+1 card, +2 milestone"
    assert (await service.state_for(USER)).current == 3

    entries = await db["mission_xp_ledger"].find({"user_id": USER}).to_list(length=None)
    assert sorted(entry["amount"] for entry in entries) == [1, 2]
    assert {entry["source_type"] for entry in entries} == {
        "CARD_STREAK",
        "STREAK_MILESTONE",
    }

    celebration = await db["mission_celebrations"].find_one({"user_id": USER})
    assert celebration["kind"] == "STREAK_MILESTONE"
    assert celebration["presentation"] == "FULL_SCREEN"


async def test_a_non_milestone_card_raises_no_celebration(service, db):
    await db["mission_card_streaks"].insert_one(
        {"user_id": USER, "current": 3, "best": 3}
    )
    bout_ids = await card(db, active=10)
    await pick(db, USER, bout_ids[:7])

    await service.settle_card(EVENT_ID)

    assert await db["mission_celebrations"].count_documents({"user_id": USER}) == 0


# --------------------------------------------------------------------- history


async def test_history_records_what_each_card_did(service, db):
    bout_ids = await card(db, active=10)
    await pick(db, USER, bout_ids[:6])
    await service.settle_card(EVENT_ID)

    await card(db, event_id=EVENT_ID + 1, active=10)
    await service.settle_card(EVENT_ID + 1)

    history = await service.history_for(USER)

    outcomes = {row["event_id"]: row["outcome"] for row in history}
    assert outcomes[EVENT_ID] == CardStreakOutcome.ADVANCED.value
    assert outcomes[EVENT_ID + 1] == CardStreakOutcome.BROKEN.value
    assert next(row for row in history if row["event_id"] == EVENT_ID)[
        "coverage_percent"
    ] == 60


async def test_many_users_settle_independently_on_one_card(service, db):
    bout_ids = await card(db, active=10)
    await pick(db, "winner", bout_ids[:9])
    await pick(db, "loser", bout_ids[:2])
    await db["mission_card_streaks"].insert_one(
        {"user_id": "loser", "current": 4, "best": 4}
    )

    settlement = await service.settle_card(EVENT_ID)

    assert (settlement.advanced, settlement.broken) == (1, 1)
    assert (await service.state_for("winner")).current == 1
    assert (await service.state_for("loser")).current == 0


# ------------------------------------------------------- level and celebration


async def _award(db, amount: int, key: str) -> None:
    """Award through the real ledger so the entry is a valid XpLedgerEntry."""
    from app.modules.missions.application.xp_ledger import XpLedgerService
    from app.modules.missions.domain.xp import AwardXpCommand, XpSourceType

    await XpLedgerService(db, clock=lambda: datetime(2026, 8, 15, tzinfo=UTC)).award(
        user_id=USER,
        command=AwardXpCommand(
            idempotency_key=key,
            source_type=XpSourceType.CARD_STREAK,
            source_id="1",
            amount=amount,
            reason="test award",
        ),
    )


def _progression(db):
    from app.modules.missions.application.progression import ProgressionService

    return ProgressionService(db, clock=lambda: datetime(2026, 8, 15, tzinfo=UTC))


async def test_crossing_a_level_raises_a_level_up_celebration(db):
    # Level 2 costs 5 XP (D-PROD-007).
    await _award(db, 6, "level-test-award-1")

    projection = await _progression(db).sync(USER)

    assert projection.level == 2
    celebration = await db["mission_celebrations"].find_one(
        {"user_id": USER, "kind": "LEVEL_UP"}
    )
    assert celebration is not None
    assert celebration["presentation"] == "FULL_SCREEN"
    assert celebration["metadata"]["level"] == 2


async def test_a_level_up_is_announced_only_once(db):
    await _award(db, 6, "level-test-award-2")

    await _progression(db).sync(USER)
    await _progression(db).sync(USER)

    assert (
        await db["mission_celebrations"].count_documents(
            {"user_id": USER, "kind": "LEVEL_UP"}
        )
        == 1
    )


async def test_staying_on_the_same_level_announces_nothing(db):
    await _award(db, 2, "level-test-award-3")

    projection = await _progression(db).sync(USER)

    assert projection.level == 1
    assert await db["mission_celebrations"].count_documents({"user_id": USER}) == 0


async def test_a_new_title_is_announced_beside_the_level(db):
    """Level 5 is PROSPECT (D-PROD-008); reaching it unlocks a title too."""
    await _progression(db).sync(USER)  # seed the cache at level 1 / BUM
    await _award(db, 40, "level-test-award-4")

    projection = await _progression(db).sync(USER)

    assert projection.level >= 5
    assert projection.title.value == "PROSPECT"
    assert await db["mission_celebrations"].count_documents(
        {"user_id": USER, "kind": "TITLE_UNLOCKED"}
    ) == 1
