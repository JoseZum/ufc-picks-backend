import pytest
from pydantic import ValidationError

from app.modules.missions.domain import (
    BoutEvaluationSnapshot,
    BoutLifecycle,
    BoutOutcome,
    BoutResultSnapshot,
    BoutRole,
    CardSection,
    FighterMetricLeg,
    LeaderboardEvaluationSnapshot,
    MetricSelectionSnapshot,
    MissionEvaluationContext,
    PickEvaluationSnapshot,
    ResultMethodFamily,
)

DEFAULT_RESULT = object()


def result(
    winner="fighter-red-1",
    *,
    outcome=BoutOutcome.RED_WIN,
    method=ResultMethodFamily.KO_TKO,
    round_=1,
    revision=1,
):
    return BoutResultSnapshot(
        revision=revision,
        outcome=outcome,
        winner_fighter_id=winner,
        method=method,
        round=round_,
    )


def bout(
    bout_id=1,
    *,
    lifecycle=BoutLifecycle.COMPLETED,
    result_value=DEFAULT_RESULT,
    section=CardSection.MAIN,
    role=BoutRole.MAIN_EVENT,
    current=True,
):
    return BoutEvaluationSnapshot(
        bout_id=bout_id,
        fighter_ids=(f"fighter-red-{bout_id}", f"fighter-blue-{bout_id}"),
        section=section,
        role=role,
        scheduled_rounds=5 if role == BoutRole.MAIN_EVENT else 3,
        lifecycle=lifecycle,
        is_current=current,
        matchup_revision=1,
        result=(
            result(winner=f"fighter-red-{bout_id}")
            if result_value is DEFAULT_RESULT
            else result_value
        ),
    )


def context(**overrides):
    values = {
        "event_id": 4242,
        "user_id": "jose",
        "card_revision": 3,
        "eligibility_revision": 2,
        "eligibility_fingerprint": "eligibility:fixture",
        "frozen_eligible_bout_ids": (1,),
        "card_finalized": True,
        "bouts": (bout(),),
        "picks": (
            PickEvaluationSnapshot(
                bout_id=1,
                winner_fighter_id="fighter-red-1",
                method="KO_TKO",
                round=1,
                points_awarded=3,
                score_revision=1,
            ),
        ),
        "leaderboard": LeaderboardEvaluationSnapshot(
            rank=1,
            tied_for_first=True,
            active_user_count=3,
            finalized=True,
        ),
    }
    values.update(overrides)
    return MissionEvaluationContext(**values)


def test_context_exposes_frozen_and_surviving_sets_by_stable_id():
    value = context()

    assert [item.bout_id for item in value.frozen_bouts()] == [1]
    assert [item.bout_id for item in value.surviving_eligible_bouts()] == [1]
    assert value.picks_by_bout_id()[1].winner_fighter_id == "fighter-red-1"


def test_cancelled_frozen_bout_remains_auditable_but_not_surviving():
    cancelled = bout(
        lifecycle=BoutLifecycle.CANCELLED,
        result_value=None,
        current=False,
    )
    value = context(
        card_finalized=True,
        bouts=(cancelled,),
        picks=(),
        leaderboard=None,
    )

    assert value.frozen_bouts() == (cancelled,)
    assert value.surviving_eligible_bouts() == ()


def test_result_winner_must_match_outcome_corner():
    with pytest.raises(ValidationError, match="red fighter"):
        bout(result_value=result(winner="fighter-blue-1"))


def test_draw_and_no_contest_require_null_winner():
    for outcome in (BoutOutcome.DRAW, BoutOutcome.NO_CONTEST):
        assert result(
            winner=None,
            outcome=outcome,
            method=ResultMethodFamily.DECISION,
            round_=3,
        ).winner_fighter_id is None
        with pytest.raises(ValidationError, match="draw/no contest"):
            result(
                winner="fighter-red-1",
                outcome=outcome,
                method=ResultMethodFamily.DECISION,
                round_=3,
            )


def test_scored_pick_must_match_current_result_revision():
    stale_pick = PickEvaluationSnapshot(
        bout_id=1,
        winner_fighter_id="fighter-red-1",
        method="KO_TKO",
        round=1,
        points_awarded=3,
        score_revision=1,
    )
    corrected_bout = bout(result_value=result(revision=2))

    with pytest.raises(ValidationError, match="current result revision"):
        context(bouts=(corrected_bout,), picks=(stale_pick,))


def test_finalized_card_rejects_unresolved_surviving_bout():
    scheduled = bout(
        lifecycle=BoutLifecycle.SCHEDULED,
        result_value=None,
    )
    with pytest.raises(ValidationError, match="unresolved surviving"):
        context(bouts=(scheduled,), picks=(), leaderboard=None)


def test_selection_fighter_must_belong_to_target_matchup():
    selection = MetricSelectionSnapshot(
        legs=(
            FighterMetricLeg(
                key="target",
                bout_id=1,
                fighter_id="fighter-from-another-bout",
            ),
        )
    )
    with pytest.raises(ValidationError, match="selected fighter"):
        context(selection=selection)


def test_duplicate_bouts_picks_and_roles_are_rejected():
    with pytest.raises(ValidationError, match="bout IDs must be unique"):
        context(bouts=(bout(), bout()), frozen_eligible_bout_ids=(1,))

    duplicate_pick = context().picks[0]
    with pytest.raises(ValidationError, match="picks must be unique"):
        context(picks=(duplicate_pick, duplicate_pick))

    second_main = bout(
        2,
        result_value=result(winner="fighter-red-2"),
    )
    with pytest.raises(ValidationError, match="at most one main event"):
        context(
            bouts=(bout(), second_main),
            frozen_eligible_bout_ids=(1, 2),
            picks=(),
        )
