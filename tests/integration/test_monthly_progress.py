from datetime import UTC, datetime

import pytest

from app.modules.missions.application import (
    MonthlyConfigService,
    MonthlyProgressService,
)
from app.modules.missions.domain import (
    MonthlyEventSummary,
    MonthlyProgressStatus,
    validate_monthly_definition,
)
from app.modules.missions.indexes import apply_mission_indexes

BEFORE = datetime(2026, 7, 20, 12, tzinfo=UTC)
INSIDE = datetime(2026, 8, 14, 12, tzinfo=UTC)
AFTER = datetime(2026, 9, 2, 12, tzinfo=UTC)
USER = "jose"


def win_target(target: int = 4):
    return validate_monthly_definition(
        {
            "mission_id": "MONTH-V2-001",
            "catalog_version": "2026.08.01",
            "xp": 15,
            "ui": {
                "name": "WIN TARGET",
                "description": "Correctly predict at least N fight winners this month.",
                "progress_template": "{current} / {target} winners",
            },
            "evaluation": {
                "metric": "monthly_correct_winner_count",
                "comparator": "GTE",
                "target_parameter": "winner_target",
                "evaluate_on": ["AFTER_EACH_RESULT", "CARD_FINALIZED"],
            },
            "eligibility": {"min_resolved_picks": 1, "min_events": 1},
            "admin_parameters": [
                {
                    "key": "winner_target",
                    "label": "Winners required",
                    "kind": "COUNT",
                    "default": target,
                    "minimum": 1,
                    "maximum": 60,
                }
            ],
            "compatibility": "V1_READY",
            "overlap_tags": ["monthly", "winners"],
        }
    )


CATALOG = {"MONTH-V2-001": win_target()}


def summary(event_id: int, correct: int, *, month="2026-08", revision=1, picks=5):
    return MonthlyEventSummary(
        event_id=event_id,
        month_key=month,
        summary_revision=revision,
        resolved_bouts=picks,
        resolved_picks=picks,
        correct_winners=correct,
        wrong_winners=picks - correct,
        pick_points=correct * 2,
    )


@pytest.fixture
async def stack(test_db):
    await apply_mission_indexes(test_db)

    def build(now: datetime = INSIDE):
        return (
            MonthlyConfigService(test_db, catalog=CATALOG, clock=lambda: now),
            MonthlyProgressService(test_db, catalog=CATALOG, clock=lambda: now),
        )

    return build


@pytest.fixture
async def active_month(stack):
    config, _ = stack(BEFORE)
    await config.create_draft(month_key="2026-08", mission_id="MONTH-V2-001")
    config_now, _ = stack(INSIDE)
    await config_now.activate(month_key="2026-08")
    return stack


async def test_progress_accumulates_across_events(active_month):
    _, progress = active_month(INSIDE)

    first = await progress.record_event_summary(user_id=USER, summary=summary(101, 2))
    second = await progress.record_event_summary(user_id=USER, summary=summary(102, 1))

    assert first.status == MonthlyProgressStatus.ACTIVE
    assert first.resolution.observation.value == 2
    assert second.resolution.observation.value == 3
    assert second.resolution.progress.text == "3 / 4 winners"


async def test_reaching_the_target_awards_exactly_fifteen_xp_once(active_month, test_db):
    _, progress = active_month(INSIDE)

    await progress.record_event_summary(user_id=USER, summary=summary(101, 2))
    completed = await progress.record_event_summary(user_id=USER, summary=summary(102, 2))
    again = await progress.record_event_summary(user_id=USER, summary=summary(103, 3))

    assert completed.status == MonthlyProgressStatus.COMPLETED
    assert completed.xp_delta == 15
    assert again.xp_delta == 0

    awards = await test_db["mission_xp_ledger"].count_documents(
        {"user_id": USER, "entry_type": "AWARD", "source_type": "MONTHLY_MISSION"}
    )
    assert awards == 1


async def test_replaying_the_same_event_summary_changes_nothing(active_month, test_db):
    _, progress = active_month(INSIDE)
    await progress.record_event_summary(user_id=USER, summary=summary(101, 4))

    replay = await progress.record_event_summary(user_id=USER, summary=summary(101, 4))

    assert replay.replayed is True
    assert replay.status == MonthlyProgressStatus.COMPLETED
    assert replay.xp_delta == 0
    assert (
        await test_db["mission_xp_ledger"].count_documents(
            {"user_id": USER, "source_type": "MONTHLY_MISSION"}
        )
        == 1
    )


async def test_re_summarizing_an_event_replaces_it_instead_of_double_counting(
    active_month,
):
    _, progress = active_month(INSIDE)
    await progress.record_event_summary(user_id=USER, summary=summary(101, 3))

    corrected = await progress.record_event_summary(
        user_id=USER, summary=summary(101, 1, revision=2)
    )

    assert corrected.resolution.observation.value == 1


async def test_a_correction_below_the_target_compensates_the_award(
    active_month, test_db
):
    _, progress = active_month(INSIDE)
    completed = await progress.record_event_summary(user_id=USER, summary=summary(101, 4))
    assert completed.status == MonthlyProgressStatus.COMPLETED

    reversed_ = await progress.record_event_summary(
        user_id=USER, summary=summary(101, 1, revision=2)
    )

    assert reversed_.status == MonthlyProgressStatus.ACTIVE
    assert reversed_.xp_delta == -15
    entries = await test_db["mission_xp_ledger"].find({"user_id": USER}).to_list(None)
    assert len(entries) == 2
    assert sum(entry["amount"] for entry in entries) == 0

    cancelled = await test_db["mission_celebrations"].count_documents(
        {"user_id": USER, "status": "CANCELLED"}
    )
    assert cancelled == 1


async def test_a_completed_month_can_be_re_earned_after_a_second_correction(
    active_month, test_db
):
    _, progress = active_month(INSIDE)
    await progress.record_event_summary(user_id=USER, summary=summary(101, 4))
    await progress.record_event_summary(user_id=USER, summary=summary(101, 1, revision=2))

    re_earned = await progress.record_event_summary(
        user_id=USER, summary=summary(101, 4, revision=3)
    )

    assert re_earned.status == MonthlyProgressStatus.COMPLETED
    assert re_earned.xp_delta == 15
    entries = await test_db["mission_xp_ledger"].find({"user_id": USER}).to_list(None)
    assert sum(entry["amount"] for entry in entries) == 15


async def test_a_draft_month_contributes_nothing(stack):
    config, progress = stack(BEFORE)
    await config.create_draft(month_key="2026-08", mission_id="MONTH-V2-001")

    result = await progress.record_event_summary(user_id=USER, summary=summary(101, 4))

    assert result is None


async def test_an_unconfigured_month_contributes_nothing(stack):
    _, progress = stack(INSIDE)

    result = await progress.record_event_summary(user_id=USER, summary=summary(101, 4))

    assert result is None


async def test_closing_the_month_fails_everyone_still_short(active_month, stack):
    _, progress = active_month(INSIDE)
    await progress.record_event_summary(user_id=USER, summary=summary(101, 1))

    config_after, progress_after = stack(AFTER)
    await config_after.close(month_key="2026-08")
    settled = await progress_after.close_month(month_key="2026-08")

    assert len(settled) == 1
    assert settled[0].status == MonthlyProgressStatus.FAILED
    assert settled[0].xp_delta == 0


async def test_closing_the_month_leaves_completed_participants_alone(
    active_month, stack, test_db
):
    _, progress = active_month(INSIDE)
    await progress.record_event_summary(user_id=USER, summary=summary(101, 4))

    config_after, progress_after = stack(AFTER)
    await config_after.close(month_key="2026-08")
    settled = await progress_after.close_month(month_key="2026-08")

    assert settled == ()
    stored = await progress_after.get(user_id=USER, month_key="2026-08")
    assert stored["status"] == MonthlyProgressStatus.COMPLETED.value
    assert (
        await test_db["mission_xp_ledger"].count_documents({"user_id": USER}) == 1
    )


async def test_parameters_are_snapshotted_against_later_catalog_edits(
    active_month, test_db
):
    _, progress = active_month(INSIDE)
    await progress.record_event_summary(user_id=USER, summary=summary(101, 2))

    stored = await progress.get(user_id=USER, month_key="2026-08")

    assert stored["parameters"] == {"winner_target": 4}
    assert stored["catalog_version"] == "2026.08.01"


async def test_month_is_taken_from_the_official_card_data_date(active_month, test_db):
    _, progress = active_month(INSIDE)
    await test_db["events"].insert_one(
        {
            "id": 5001,
            "date": datetime(2026, 7, 31, 20, tzinfo=UTC),
            "card_data_v1": {"official_date": datetime(2026, 8, 1, 2, tzinfo=UTC)},
        }
    )
    await test_db["events"].insert_one({"id": 5002, "date": datetime(2026, 9, 3, tzinfo=UTC)})

    assert await progress.month_key_for_event(5001) == "2026-08"
    assert await progress.month_key_for_event(5002) == "2026-09"
    assert await progress.month_key_for_event(9999) is None


async def test_two_users_progress_independently(active_month, test_db):
    _, progress = active_month(INSIDE)

    await progress.record_event_summary(user_id=USER, summary=summary(101, 4))
    other = await progress.record_event_summary(user_id="chris", summary=summary(101, 1))

    assert other.status == MonthlyProgressStatus.ACTIVE
    assert (
        await test_db["mission_xp_ledger"].count_documents({"user_id": "chris"}) == 0
    )


# --------------------------------------------- activating a month part-way


async def test_a_card_that_ran_before_activation_does_not_count(
    active_month, test_db
):
    """Activating August on the 14th must not retroactively score the 2nd.

    Jose's case exactly: the month was opened late, and the cards that already
    ran that month were played by people who had never been told the month
    existed. Counting them would hand out progress nobody competed for.
    """
    _, progress = active_month(INSIDE)
    early_event = 70801
    await test_db["events"].delete_many({"id": early_event})
    await test_db["events"].insert_one({
        "id": early_event,
        "name": "UFC Fight Night: Before The Month Opened",
        # Two days into August, twelve days before activation.
        "date": datetime(2026, 8, 2, 23, tzinfo=UTC),
    })

    result = await progress.record_event_summary(
        user_id=USER, summary=summary(early_event, 4)
    )

    assert result is None, "an event from before activation contributes nothing"
    assert await progress.get(user_id=USER, month_key="2026-08") is None


async def test_a_card_after_activation_counts_normally(active_month, test_db):
    _, progress = active_month(INSIDE)
    late_event = 70802
    await test_db["events"].delete_many({"id": late_event})
    await test_db["events"].insert_one({
        "id": late_event,
        "name": "UFC Fight Night: After The Month Opened",
        "date": datetime(2026, 8, 20, 23, tzinfo=UTC),
    })

    result = await progress.record_event_summary(
        user_id=USER, summary=summary(late_event, 3)
    )

    assert result is not None
    assert result.status == MonthlyProgressStatus.ACTIVE
    stored = await progress.get(user_id=USER, month_key="2026-08")
    assert str(late_event) in stored["event_summaries"]


async def test_correcting_an_old_result_cannot_smuggle_the_card_back_in(
    active_month, test_db
):
    """The failure this really guards against.

    A card from before activation is ignored today because it settled while the
    month was still DRAFT. But an Admin correcting one of its results months
    later re-fires the trigger with the month now ACTIVE — without an explicit
    rule, that stale card would fold in at that moment.
    """
    _, progress = active_month(INSIDE)
    old_event = 70803
    await test_db["events"].delete_many({"id": old_event})
    await test_db["events"].insert_one({
        "id": old_event,
        "name": "UFC Fight Night: Corrected Later",
        "date": datetime(2026, 8, 1, 23, tzinfo=UTC),
    })

    first = await progress.record_event_summary(
        user_id=USER, summary=summary(old_event, 2)
    )
    corrected = await progress.record_event_summary(
        user_id=USER, summary=summary(old_event, 5, revision=2)
    )

    assert first is None
    assert corrected is None, "a correction must not open a door the card lost"


async def test_an_event_with_no_resolvable_date_still_counts(
    active_month, test_db
):
    """Losing someone's month over missing card metadata is the worse failure."""
    _, progress = active_month(INSIDE)
    undated = 70804
    await test_db["events"].delete_many({"id": undated})
    await test_db["events"].insert_one({"id": undated, "name": "No Date"})

    result = await progress.record_event_summary(
        user_id=USER, summary=summary(undated, 2)
    )

    assert result is not None
