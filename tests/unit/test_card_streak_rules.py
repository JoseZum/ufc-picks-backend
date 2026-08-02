"""STREAK-001 as the reviewed sheet states it, with nothing added."""

import pytest

from app.modules.missions.domain.streak import (
    CARD_STREAK_XP,
    CardStreakOutcome,
    covers_card,
    decide_card_streak,
    milestone_bonus,
)


@pytest.mark.parametrize(
    ("picked", "denominator", "expected"),
    [
        (6, 10, True),
        (5, 10, False),  # exactly half is not "more than half"
        (4, 10, False),
        (6, 11, True),
        (5, 11, False),
        (1, 1, True),
        (0, 1, False),
        (2, 3, True),
        (1, 3, False),
    ],
)
def test_a_card_is_covered_only_above_half(picked, denominator, expected):
    assert covers_card(picked=picked, denominator=denominator) is expected


@pytest.mark.parametrize(
    ("streak", "bonus"),
    [
        (1, None),
        (2, None),
        (3, 2),
        (4, None),
        (5, 3),
        (6, None),
        (9, None),
        (10, 5),
        (11, None),
        (15, 3),
        (20, 3),
        (25, 3),
        (100, 3),
    ],
)
def test_the_reviewed_milestone_curve(streak, bonus):
    assert milestone_bonus(streak) == bonus


def test_covering_a_card_advances_the_streak_and_pays_one_xp():
    decision = decide_card_streak(current=1, best=4, picked=7, denominator=12)

    assert decision.outcome == CardStreakOutcome.ADVANCED
    assert decision.current_after == 2
    assert decision.best_after == 4
    assert decision.card_xp == CARD_STREAK_XP
    assert decision.milestone is None
    assert decision.total_xp == 1
    assert decision.coverage_percent == 58


def test_reaching_a_milestone_pays_the_bonus_on_top():
    decision = decide_card_streak(current=2, best=2, picked=8, denominator=10)

    assert decision.current_after == 3
    assert decision.milestone == 3
    assert decision.milestone_xp == 2
    assert decision.total_xp == 3


def test_a_new_high_raises_the_best_streak():
    decision = decide_card_streak(current=4, best=4, picked=9, denominator=10)

    assert decision.current_after == 5
    assert decision.best_after == 5


def test_missing_the_card_breaks_the_streak_but_keeps_the_best():
    decision = decide_card_streak(current=7, best=9, picked=5, denominator=10)

    assert decision.outcome == CardStreakOutcome.BROKEN
    assert decision.current_after == 0
    assert decision.best_after == 9
    assert decision.total_xp == 0


def test_a_break_can_still_publish_a_new_best():
    """The card that breaks a record streak must not lose the record."""
    decision = decide_card_streak(current=11, best=3, picked=0, denominator=8)

    assert decision.outcome == CardStreakOutcome.BROKEN
    assert decision.current_after == 0
    assert decision.best_after == 11


def test_missing_a_card_with_no_streak_changes_nothing():
    decision = decide_card_streak(current=0, best=2, picked=1, denominator=10)

    assert decision.outcome == CardStreakOutcome.UNCHANGED
    assert decision.current_after == 0
    assert decision.best_after == 2
    assert decision.total_xp == 0


def test_a_card_with_no_active_bouts_neither_advances_nor_breaks():
    decision = decide_card_streak(current=6, best=6, picked=0, denominator=0)

    assert decision.outcome == CardStreakOutcome.NOT_ELIGIBLE
    assert decision.current_after == 6
    assert decision.total_xp == 0


def test_more_picks_than_bouts_cannot_inflate_coverage():
    decision = decide_card_streak(current=0, best=0, picked=99, denominator=10)

    assert decision.picked == 10
    assert decision.coverage_percent == 100


def test_there_is_no_grace_no_freeze_and_no_comeback():
    """Two consecutive misses stay at zero — nothing restores the streak."""
    first = decide_card_streak(current=5, best=5, picked=2, denominator=10)
    second = decide_card_streak(
        current=first.current_after, best=first.best_after, picked=2, denominator=10
    )

    assert (first.current_after, second.current_after) == (0, 0)
    assert second.best_after == 5
