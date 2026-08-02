import pytest

from app.modules.missions.domain import (
    CARD_METRIC_NAMES,
    BoutEvaluationSnapshot,
    BoutLifecycle,
    BoutOutcome,
    BoutResultSnapshot,
    BoutRole,
    CardSection,
    FighterMetricLeg,
    FightMetricLeg,
    LeaderboardEvaluationSnapshot,
    MetricRegistry,
    MetricRegistryError,
    MetricRegistryErrorCode,
    MetricSelectionSnapshot,
    MetricVoidReason,
    MissionEvaluationContext,
    PickEvaluationSnapshot,
    ResultMethodFamily,
    build_card_metric_registry,
)


def completed_bout(
    bout_id,
    section,
    *,
    role=BoutRole.STANDARD,
    winner="red",
    outcome=None,
    method=ResultMethodFamily.KO_TKO,
    round_=1,
    title=False,
):
    fighter_ids = (f"red-{bout_id}", f"blue-{bout_id}")
    if outcome is None:
        outcome = BoutOutcome.RED_WIN if winner == "red" else BoutOutcome.BLUE_WIN
    winner_id = (
        None
        if outcome in {BoutOutcome.DRAW, BoutOutcome.NO_CONTEST}
        else (fighter_ids[0] if winner == "red" else fighter_ids[1])
    )
    return BoutEvaluationSnapshot(
        bout_id=bout_id,
        fighter_ids=fighter_ids,
        section=section,
        role=role,
        is_title_fight=title,
        scheduled_rounds=5 if role == BoutRole.MAIN_EVENT else 3,
        lifecycle=BoutLifecycle.COMPLETED,
        matchup_revision=1,
        result=BoutResultSnapshot(
            revision=1,
            outcome=outcome,
            winner_fighter_id=winner_id,
            method=method,
            round=round_,
        ),
    )


def pick(bout_id, fighter, method, round_, points):
    return PickEvaluationSnapshot(
        bout_id=bout_id,
        winner_fighter_id=f"{fighter}-{bout_id}",
        method=method,
        round=round_,
        points_awarded=points,
        score_revision=1,
    )


def card_context(**overrides):
    bouts = (
        completed_bout(
            1,
            CardSection.MAIN,
            role=BoutRole.MAIN_EVENT,
            method=ResultMethodFamily.KO_TKO,
            round_=1,
            title=True,
        ),
        completed_bout(
            2,
            CardSection.MAIN,
            role=BoutRole.CO_MAIN,
            winner="blue",
            method=ResultMethodFamily.DECISION,
            round_=3,
            title=True,
        ),
        completed_bout(
            3,
            CardSection.MAIN,
            winner="blue",
            method=ResultMethodFamily.SUBMISSION,
            round_=2,
        ),
        completed_bout(
            4,
            CardSection.MAIN,
            method=ResultMethodFamily.DECISION,
            round_=3,
        ),
        completed_bout(
            5,
            CardSection.PRELIM,
            method=ResultMethodFamily.SUBMISSION,
            round_=1,
        ),
        completed_bout(
            6,
            CardSection.EARLY_PRELIM,
            outcome=BoutOutcome.DRAW,
            method=ResultMethodFamily.DECISION,
            round_=3,
        ),
    )
    picks = (
        pick(1, "red", "KO_TKO", 1, 3),
        pick(2, "blue", "DECISION", None, 2),
        pick(3, "red", "KO_TKO", 1, 0),
        pick(4, "red", "DECISION", None, 2),
        pick(5, "red", "SUBMISSION", 1, 3),
        pick(6, "blue", "DECISION", None, 0),
    )
    values = {
        "event_id": 4242,
        "user_id": "jose",
        "card_revision": 4,
        "eligibility_revision": 2,
        "eligibility_fingerprint": "eligibility:card-4242",
        "frozen_eligible_bout_ids": tuple(range(1, 7)),
        "card_finalized": True,
        "bouts": bouts,
        "picks": picks,
        "leaderboard": LeaderboardEvaluationSnapshot(
            rank=1,
            tied_for_first=True,
            active_user_count=3,
            finalized=True,
        ),
    }
    values.update(overrides)
    return MissionEvaluationContext(**values)


def target_context(bout, selection, *, finalized=True):
    return MissionEvaluationContext(
        event_id=5151,
        user_id="jose",
        card_revision=1,
        eligibility_revision=1,
        eligibility_fingerprint="eligibility:target",
        frozen_eligible_bout_ids=(bout.bout_id,),
        card_finalized=finalized,
        bouts=(bout,),
        selection=selection,
    )


def test_registry_exactly_covers_the_58_approved_card_metric_names():
    registry = build_card_metric_registry()

    assert len(CARD_METRIC_NAMES) == 58
    assert registry.names == tuple(sorted(CARD_METRIC_NAMES))


def test_registry_rejects_unknown_and_duplicate_metrics():
    registry = MetricRegistry()

    def provider(request, context):
        return None

    registry.register("known_metric", provider)

    with pytest.raises(MetricRegistryError) as duplicate:
        registry.register("known_metric", provider)
    assert duplicate.value.code == MetricRegistryErrorCode.DUPLICATE_METRIC

    with pytest.raises(MetricRegistryError) as unknown:
        registry.evaluate("missing_metric", card_context())
    assert unknown.value.code == MetricRegistryErrorCode.UNKNOWN_METRIC


def test_card_aggregate_metrics_use_result_families_and_frozen_survivors():
    registry = build_card_metric_registry()
    context = card_context()

    decisions = registry.evaluate("card_decision_count", context)
    finishes = registry.evaluate("card_finish_count", context)
    rate = registry.evaluate("card_finish_rate", context)
    presence = registry.evaluate("card_method_presence", context)
    round_one = registry.evaluate(
        "card_round_finish_count",
        context,
        parameters={"round": 1},
    )

    assert decisions.value == 3
    assert finishes.value == 3
    assert (rate.numerator, rate.denominator, rate.value) == (3, 6, 0.5)
    assert presence.matched_items == {"KO_TKO", "SUBMISSION", "DECISION"}
    assert round_one.value == 2
    assert registry.evaluate("card_submission_count", context).value == 2
    assert registry.evaluate("main_card_decision_count", context).value == 2
    assert registry.evaluate("main_card_finish_count", context).value == 2
    assert registry.evaluate("prelim_finish_count", context).total_count == 2


def test_dynamic_and_user_selected_card_prop_targets_are_frozen_in_observation():
    registry = build_card_metric_registry()
    dynamic = registry.evaluate(
        "card_finish_count",
        card_context(selection=MetricSelectionSnapshot(card_prop_frozen_target=3)),
    )
    exact = registry.evaluate(
        "card_decision_count",
        card_context(selection=MetricSelectionSnapshot(card_prop_exact_count=3)),
    )
    lane = registry.evaluate(
        "selected_result_family_vs_other",
        card_context(selection=MetricSelectionSnapshot(card_prop_choice="FINISHES")),
    )

    assert dynamic.target_override == 3
    assert exact.target_override == 3
    assert (lane.value, lane.other_value) == (3, 3)


def test_mixed_results_groups_ko_and_submission_into_one_finish_family():
    result = build_card_metric_registry().evaluate(
        "card_method_presence",
        card_context(),
        parameters={"presence_mode": "FINISH_DECISION"},
    )

    assert result.matched_items == {"FINISH", "DECISION"}
    assert result.value == 2


def test_disqualification_and_other_results_do_not_count_as_finishes():
    bouts = (
        completed_bout(
            20,
            CardSection.MAIN,
            method=ResultMethodFamily.DQ,
        ),
        completed_bout(
            21,
            CardSection.MAIN,
            method=ResultMethodFamily.OTHER,
        ),
    )
    context = card_context(
        bouts=bouts,
        picks=(),
        frozen_eligible_bout_ids=(20, 21),
        leaderboard=None,
    )

    result = build_card_metric_registry().evaluate("card_finish_count", context)

    assert result.value == 0


def test_auto_pick_metrics_derive_correctness_from_ids_and_current_revision():
    registry = build_card_metric_registry()
    context = card_context()

    assert registry.evaluate("correct_winner_count", context).value == 4
    accuracy = registry.evaluate("winner_accuracy", context)
    assert (accuracy.numerator, accuracy.denominator, accuracy.value) == (4, 5, 0.8)
    assert registry.evaluate("wrong_winner_count", context).value == 1
    assert registry.evaluate("event_pick_points", context).value == 10
    assert registry.evaluate("perfect_pick_count", context).value == 2
    assert registry.evaluate("two_plus_point_pick_count", context).value == 4
    assert registry.evaluate("main_event_correct_winner", context).value == 1
    assert registry.evaluate("co_main_correct_winner", context).value == 1
    assert registry.evaluate("main_and_co_main_correct_winners", context).value == 2
    assert registry.evaluate("main_event_pick_points", context).value == 3


def test_sweep_metrics_require_a_pick_for_every_target_bout():
    registry = build_card_metric_registry()
    context = card_context()

    main = registry.evaluate("main_card_winner_accuracy", context)
    prelim = registry.evaluate("prelim_winner_accuracy", context)
    title = registry.evaluate("title_bout_winner_accuracy", context)
    clean_sweep_wrong = registry.evaluate(
        "wrong_winner_count",
        context.model_copy(update={"picks": context.picks[:-2]}),
        parameters={"require_full_card": True},
    )

    assert (main.numerator, main.denominator, main.value) == (3, 4, 0.75)
    assert (prelim.numerator, prelim.denominator, prelim.value) == (1, 2, 0.5)
    assert (title.numerator, title.denominator, title.value) == (2, 2, 1)
    # Bout 3 is a wrong pick and bout 5 was decided but never picked. Bout 6 is a
    # draw: it produced no winner, so like every other accuracy metric it is not
    # held against the user.
    assert clean_sweep_wrong.value == 2


def test_full_card_sweep_ignores_bouts_that_have_not_been_decided_yet():
    registry = build_card_metric_registry()
    context = card_context()
    running_card = context.model_copy(
        update={
            "card_finalized": False,
            "bouts": tuple(
                bout
                if bout.bout_id == 1
                else bout.model_copy(
                    update={
                        "lifecycle": BoutLifecycle.SCHEDULED,
                        "is_current": True,
                        "result": None,
                    }
                )
                for bout in context.bouts
            ),
        }
    )

    observation = registry.evaluate(
        "wrong_winner_count",
        running_card,
        parameters={"require_full_card": True},
    )

    assert observation.value == 0
    assert observation.terminal is False


def test_double_feature_is_terminal_at_card_close_even_with_a_missing_pick():
    registry = build_card_metric_registry()
    context = card_context()
    only_main_event_pick = tuple(pick for pick in context.picks if pick.bout_id == 1)

    result = registry.evaluate(
        "main_co_main_perfect_pick_count",
        context.model_copy(update={"picks": only_main_event_pick}),
    )

    assert result.value == 1
    assert result.terminal is True


def test_selected_fighter_metrics_complete_or_void_from_the_target_result():
    registry = build_card_metric_registry()
    bout = completed_bout(
        10,
        CardSection.MAIN,
        method=ResultMethodFamily.SUBMISSION,
        round_=1,
    )
    red = FighterMetricLeg(
        key="target",
        bout_id=10,
        fighter_id="red-10",
        method="SUBMISSION",
        round=1,
    )
    blue = FighterMetricLeg(
        key="target",
        bout_id=10,
        fighter_id="blue-10",
    )

    red_context = target_context(bout, MetricSelectionSnapshot(legs=(red,)))
    blue_context = target_context(bout, MetricSelectionSnapshot(legs=(blue,)))

    assert registry.evaluate("selected_fighter_win", red_context).value == 1
    submission = registry.evaluate("selected_fighter_submission_win", red_context)
    assert (submission.numerator, submission.denominator) == (2, 2)
    assert (
        registry.evaluate(
            "selected_fighter_submission_round_one_win",
            red_context,
        ).value
        == 3
    )
    assert registry.evaluate("selected_fighter_ko_win", red_context).value == 1
    assert registry.evaluate("selected_exact_pick", red_context).value == 3
    assert registry.evaluate("selected_fighter_loss", blue_context).value == 1

    draw = completed_bout(
        10,
        CardSection.MAIN,
        outcome=BoutOutcome.DRAW,
        method=ResultMethodFamily.DECISION,
        round_=3,
    )
    voided = registry.evaluate(
        "selected_fighter_win",
        target_context(draw, MetricSelectionSnapshot(legs=(red,))),
    )
    assert voided.void_reason == MetricVoidReason.TARGET_DRAW_OR_NO_CONTEST


def test_target_fight_draw_decision_succeeds_but_no_contest_voids():
    registry = build_card_metric_registry()
    decision_leg = FightMetricLeg(
        key="target",
        bout_id=20,
        outcome="DECISION",
    )
    draw = completed_bout(
        20,
        CardSection.MAIN,
        outcome=BoutOutcome.DRAW,
        method=ResultMethodFamily.DECISION,
        round_=3,
    )
    no_contest = completed_bout(
        20,
        CardSection.MAIN,
        outcome=BoutOutcome.NO_CONTEST,
        method=ResultMethodFamily.OTHER,
        round_=1,
    )

    success = registry.evaluate(
        "selected_bout_is_decision",
        target_context(draw, MetricSelectionSnapshot(legs=(decision_leg,))),
    )
    voided = registry.evaluate(
        "selected_bout_is_decision",
        target_context(no_contest, MetricSelectionSnapshot(legs=(decision_leg,))),
    )

    assert success.value == 1
    assert voided.void_reason == MetricVoidReason.TARGET_DRAW_OR_NO_CONTEST


def test_fighter_and_fight_combos_are_all_or_nothing_and_fail_early():
    registry = build_card_metric_registry()
    first = completed_bout(
        30,
        CardSection.MAIN,
        method=ResultMethodFamily.KO_TKO,
    )
    second = completed_bout(
        31,
        CardSection.PRELIM,
        method=ResultMethodFamily.DECISION,
        round_=3,
    )
    fighter_selection = MetricSelectionSnapshot(
        legs=(
            FighterMetricLeg(
                key="ko",
                bout_id=30,
                fighter_id="red-30",
                method="KO_TKO",
            ),
            FighterMetricLeg(
                key="dec",
                bout_id=31,
                fighter_id="red-31",
                method="DECISION",
            ),
        )
    )
    fight_selection = MetricSelectionSnapshot(
        legs=(
            FightMetricLeg(key="finish", bout_id=30, outcome="FINISH"),
            FightMetricLeg(key="decision", bout_id=31, outcome="DECISION"),
        )
    )
    base = {
        "event_id": 5252,
        "user_id": "jose",
        "card_revision": 1,
        "eligibility_revision": 1,
        "eligibility_fingerprint": "eligibility:combo",
        "frozen_eligible_bout_ids": (30, 31),
        "card_finalized": True,
        "bouts": (first, second),
    }

    fighter_result = registry.evaluate(
        "combo_ko_dec_correct",
        MissionEvaluationContext(**base, selection=fighter_selection),
    )
    fight_result = registry.evaluate(
        "combo_finish_decision_bouts",
        MissionEvaluationContext(**base, selection=fight_selection),
    )

    assert (fighter_result.numerator, fighter_result.denominator) == (2, 2)
    assert (fight_result.numerator, fight_result.denominator) == (2, 2)
    assert fighter_result.terminal is True
    assert fight_result.terminal is True


def test_round_ladder_scores_winner_and_round_without_scoring_completion_method():
    registry = build_card_metric_registry()
    bouts = tuple(
        completed_bout(
            bout_id,
            CardSection.MAIN,
            method=method,
            round_=round_,
        )
        for bout_id, method, round_ in (
            (40, ResultMethodFamily.SUBMISSION, 1),
            (41, ResultMethodFamily.KO_TKO, 2),
            (42, ResultMethodFamily.SUBMISSION, 3),
        )
    )
    selection = MetricSelectionSnapshot(
        legs=tuple(
            FighterMetricLeg(
                key=f"round_{round_}",
                bout_id=bout.bout_id,
                fighter_id=f"red-{bout.bout_id}",
                round=round_,
            )
            for round_, bout in enumerate(bouts, start=1)
        )
    )
    context = MissionEvaluationContext(
        event_id=5353,
        user_id="jose",
        card_revision=1,
        eligibility_revision=1,
        eligibility_fingerprint="eligibility:round-ladder",
        frozen_eligible_bout_ids=(40, 41, 42),
        card_finalized=True,
        bouts=bouts,
        selection=selection,
    )

    result = registry.evaluate("combo_correct_round_ladder", context)

    assert (result.numerator, result.denominator) == (3, 3)
    assert result.terminal is True


def test_leaderboard_metric_is_pending_before_final_and_void_if_final_missing():
    registry = build_card_metric_registry()
    assert registry.evaluate("event_leaderboard_rank", card_context()).value == 1

    pending = card_context(card_finalized=False, leaderboard=None)
    pending_result = registry.evaluate("event_leaderboard_rank", pending)
    assert pending_result.terminal is False
    assert pending_result.void_reason is None

    final_missing = card_context(leaderboard=None)
    voided = registry.evaluate("event_leaderboard_rank", final_missing)
    assert voided.void_reason == MetricVoidReason.LEADERBOARD_UNAVAILABLE


def test_metric_parameters_are_strictly_typed():
    registry = build_card_metric_registry()
    with pytest.raises(MetricRegistryError) as raised:
        registry.evaluate(
            "card_round_finish_count",
            card_context(),
            parameters={"round": "one"},
        )
    assert raised.value.code == MetricRegistryErrorCode.INVALID_PARAMETERS
