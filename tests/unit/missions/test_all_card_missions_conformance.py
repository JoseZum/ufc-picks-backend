"""Generated behavioral conformance for every reviewed card mission."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

import pytest

from app.modules.missions.catalog import load_card_catalog
from app.modules.missions.domain.definitions import (
    CardPropMissionDefinition,
    CardPropTargetSource,
    ComboBuilderMissionDefinition,
    ComboLegTarget,
    MissionDefinition,
    TargetFighterMissionDefinition,
    TargetFightMissionDefinition,
    TargetFightOutcome,
    WinMethod,
)
from app.modules.missions.domain.enums import MissionInteractionType
from app.modules.missions.domain.evaluation import (
    BoutEvaluationSnapshot,
    BoutLifecycle,
    BoutOutcome,
    BoutResultSnapshot,
    BoutRole,
    CardSection,
    FighterMetricLeg,
    FightMetricLeg,
    LeaderboardEvaluationSnapshot,
    MetricSelectionSnapshot,
    MissionEvaluationContext,
    PickEvaluationSnapshot,
    ResultMethodFamily,
)
from app.modules.missions.domain.metrics import CARD_METRIC_REGISTRY
from app.modules.missions.domain.resolution import (
    MetricResolution,
    MetricResolutionStatus,
    resolve_metric_observation,
)

CATALOG = load_card_catalog()
IDS = tuple(range(1, 13))


@dataclass(frozen=True)
class Scenario:
    context: MissionEvaluationContext
    resolution: MetricResolution


def _method(value: WinMethod) -> ResultMethodFamily:
    return {
        WinMethod.KO_TKO: ResultMethodFamily.KO_TKO,
        WinMethod.SUBMISSION: ResultMethodFamily.SUBMISSION,
        WinMethod.DECISION: ResultMethodFamily.DECISION,
    }[value]


def _bout(
    bout_id: int,
    method: ResultMethodFamily,
    round_: int,
    *,
    winner: str = "red",
    lifecycle: BoutLifecycle = BoutLifecycle.COMPLETED,
    current: bool = True,
) -> BoutEvaluationSnapshot:
    section = (
        CardSection.MAIN
        if bout_id <= 5
        else CardSection.PRELIM
        if bout_id <= 10
        else CardSection.EARLY_PRELIM
    )
    role = (
        BoutRole.MAIN_EVENT
        if bout_id == 1
        else BoutRole.CO_MAIN
        if bout_id == 2
        else BoutRole.STANDARD
    )
    fighter_ids = (f"red-{bout_id}", f"blue-{bout_id}")
    result = None
    if lifecycle == BoutLifecycle.COMPLETED:
        result = BoutResultSnapshot(
            revision=1,
            outcome=BoutOutcome.RED_WIN if winner == "red" else BoutOutcome.BLUE_WIN,
            winner_fighter_id=f"{winner}-{bout_id}",
            method=method,
            round=round_,
        )
    return BoutEvaluationSnapshot(
        bout_id=bout_id,
        fighter_ids=fighter_ids,
        section=section,
        role=role,
        is_title_fight=bout_id in {1, 2},
        scheduled_rounds=3,
        lifecycle=lifecycle,
        is_current=current,
        matchup_revision=1,
        result=result,
    )


def _default_bouts() -> list[BoutEvaluationSnapshot]:
    methods = {
        1: (ResultMethodFamily.KO_TKO, 1),
        2: (ResultMethodFamily.SUBMISSION, 2),
        3: (ResultMethodFamily.KO_TKO, 3),
        4: (ResultMethodFamily.SUBMISSION, 1),
        5: (ResultMethodFamily.KO_TKO, 2),
        6: (ResultMethodFamily.DECISION, 3),
        7: (ResultMethodFamily.DECISION, 3),
        8: (ResultMethodFamily.DECISION, 3),
        9: (ResultMethodFamily.DECISION, 3),
        10: (ResultMethodFamily.SUBMISSION, 2),
        11: (ResultMethodFamily.DECISION, 3),
        12: (ResultMethodFamily.SUBMISSION, 1),
    }
    return [_bout(bout_id, *methods[bout_id]) for bout_id in IDS]


def _replace_result(
    bouts: list[BoutEvaluationSnapshot],
    bout_id: int,
    *,
    method: ResultMethodFamily,
    round_: int,
    winner: str = "red",
) -> None:
    previous = bouts[bout_id - 1]
    bouts[bout_id - 1] = _bout(
        bout_id,
        method,
        round_,
        winner=winner,
        lifecycle=previous.lifecycle,
        current=previous.is_current,
    )


def _set_all(
    bouts: list[BoutEvaluationSnapshot],
    method: ResultMethodFamily,
    round_: int,
) -> None:
    for bout_id in IDS:
        _replace_result(bouts, bout_id, method=method, round_=round_)


def _perfect_picks(
    bouts: list[BoutEvaluationSnapshot],
    *,
    correct: bool,
) -> tuple[PickEvaluationSnapshot, ...]:
    picks = []
    for bout in bouts:
        if bout.result is None:
            continue
        winner = bout.result.winner_fighter_id if correct else bout.fighter_ids[1]
        if winner == bout.result.winner_fighter_id and not correct:
            winner = bout.fighter_ids[0]
        method = {
            ResultMethodFamily.KO_TKO: WinMethod.KO_TKO,
            ResultMethodFamily.SUBMISSION: WinMethod.SUBMISSION,
            ResultMethodFamily.DECISION: WinMethod.DECISION,
        }.get(bout.result.method, WinMethod.DECISION)
        picks.append(
            PickEvaluationSnapshot(
                bout_id=bout.bout_id,
                winner_fighter_id=winner,
                method=method,
                round=None if method == WinMethod.DECISION else bout.result.round,
                points_awarded=3 if correct and method != WinMethod.DECISION else 2 if correct else 0,
                score_revision=bout.result.revision,
            )
        )
    return tuple(picks)


def _fighter_target(
    definition: TargetFighterMissionDefinition,
    bouts: list[BoutEvaluationSnapshot],
    success: bool,
) -> MetricSelectionSnapshot:
    metric = definition.evaluation.metric
    bout_id = 1
    method = None
    round_ = None
    selected_corner = "blue" if metric == "selected_fighter_loss" else "red"
    selected_id = f"{selected_corner}-{bout_id}"
    winner = "red" if metric == "selected_fighter_loss" and success else selected_corner
    if not success:
        winner = selected_corner if metric == "selected_fighter_loss" else "blue"

    if "submission" in metric:
        method = WinMethod.SUBMISSION
    elif "ko" in metric:
        method = WinMethod.KO_TKO
    elif "decision" in metric:
        method = WinMethod.DECISION
    elif metric in {"selected_exact_pick", "selected_title_bout_winner_method"}:
        method = WinMethod.KO_TKO
    if "round_one" in metric:
        round_ = 1
    elif metric == "selected_exact_pick":
        round_ = 2

    result_method = _method(method or WinMethod.KO_TKO)
    result_round = 3 if method == WinMethod.DECISION else round_ or 2
    _replace_result(
        bouts,
        bout_id,
        method=result_method,
        round_=result_round,
        winner=winner,
    )
    return MetricSelectionSnapshot(
        legs=(
            FighterMetricLeg(
                key="target",
                bout_id=bout_id,
                fighter_id=selected_id,
                method=method,
                round=round_,
            ),
        )
    )


def _fight_target(
    definition: TargetFightMissionDefinition,
    bouts: list[BoutEvaluationSnapshot],
    success: bool,
) -> MetricSelectionSnapshot:
    outcome = definition.selection.outcome
    round_ = definition.selection.required_round
    if outcome == TargetFightOutcome.DECISION:
        method = ResultMethodFamily.DECISION if success else ResultMethodFamily.KO_TKO
        actual_round = 3 if success else 1
    elif outcome == TargetFightOutcome.FINISH:
        method = ResultMethodFamily.KO_TKO if success else ResultMethodFamily.DECISION
        actual_round = 1 if success else 3
    else:
        method = ResultMethodFamily.KO_TKO
        actual_round = round_ if success else (round_ % 3) + 1
    _replace_result(bouts, 1, method=method, round_=actual_round)
    return MetricSelectionSnapshot(
        legs=(
            FightMetricLeg(
                key="target",
                bout_id=1,
                outcome=outcome,
                round=round_,
            ),
        )
    )


def _combo(
    definition: ComboBuilderMissionDefinition,
    bouts: list[BoutEvaluationSnapshot],
    success: bool,
) -> MetricSelectionSnapshot:
    legs = []
    selectable_methods = iter(
        (WinMethod.KO_TKO, WinMethod.SUBMISSION, WinMethod.DECISION)
    )
    for index, spec in enumerate(definition.selection.legs, start=1):
        bout_id = index
        if spec.target == ComboLegTarget.FIGHTER:
            method = spec.method
            if method is None and spec.allowed_methods:
                method = next(
                    candidate
                    for candidate in selectable_methods
                    if candidate in spec.allowed_methods
                )
            result_method = _method(method or WinMethod.KO_TKO)
            result_round = 3 if method == WinMethod.DECISION else spec.round or index
            winner = "red" if success or index > 1 else "blue"
            _replace_result(
                bouts,
                bout_id,
                method=result_method,
                round_=result_round,
                winner=winner,
            )
            legs.append(
                FighterMetricLeg(
                    key=spec.key,
                    bout_id=bout_id,
                    fighter_id=f"red-{bout_id}",
                    method=method,
                    round=spec.round,
                )
            )
        else:
            outcome = spec.fight_outcome
            desired_finish = outcome == TargetFightOutcome.FINISH
            actual_finish = desired_finish if success or index > 1 else not desired_finish
            _replace_result(
                bouts,
                bout_id,
                method=(
                    ResultMethodFamily.KO_TKO
                    if actual_finish
                    else ResultMethodFamily.DECISION
                ),
                round_=1 if actual_finish else 3,
            )
            legs.append(
                FightMetricLeg(
                    key=spec.key,
                    bout_id=bout_id,
                    outcome=outcome,
                )
            )
    return MetricSelectionSnapshot(legs=tuple(legs))


def _card_prop(
    definition: CardPropMissionDefinition,
    bouts: list[BoutEvaluationSnapshot],
    success: bool,
) -> MetricSelectionSnapshot:
    metric = definition.evaluation.metric
    if not success:
        if metric in {
            "card_finish_count",
            "card_finish_rate",
            "card_finish_vs_decision",
            "main_card_finish_count",
            "prelim_finish_count",
        }:
            _set_all(bouts, ResultMethodFamily.DECISION, 3)
        elif metric in {
            "card_decision_count",
            "card_decision_vs_finish",
            "main_card_decision_count",
        }:
            _set_all(
                bouts,
                (
                    ResultMethodFamily.DECISION
                    if definition.evaluation.comparator.value == "LTE"
                    else ResultMethodFamily.KO_TKO
                ),
                3,
            )
        elif metric == "card_submission_count":
            _set_all(bouts, ResultMethodFamily.KO_TKO, 3)
        elif metric == "card_round_finish_count":
            _set_all(bouts, ResultMethodFamily.KO_TKO, 3)
        elif metric in {"card_method_presence", "selected_result_family_vs_other"}:
            _set_all(bouts, ResultMethodFamily.DECISION, 3)
    elif metric == "card_decision_vs_finish":
        _set_all(bouts, ResultMethodFamily.DECISION, 3)
    elif metric == "card_decision_count" and definition.evaluation.comparator.value == "LTE":
        _set_all(bouts, ResultMethodFamily.KO_TKO, 1)

    choice = "FINISHES" if definition.selection.input.value == "CHOICE" else None
    finish_count = sum(
        bout.result is not None
        and bout.result.method
        in {ResultMethodFamily.KO_TKO, ResultMethodFamily.SUBMISSION}
        for bout in bouts
    )
    decision_count = sum(
        bout.result is not None and bout.result.method == ResultMethodFamily.DECISION
        for bout in bouts
    )
    exact = None
    frozen = None
    if definition.selection.target_source == CardPropTargetSource.SELECTED_EXACT_COUNT:
        actual = finish_count if metric == "card_finish_count" else decision_count
        exact = actual if success else min(30, actual + 1)
    elif definition.selection.target_source == CardPropTargetSource.FROZEN_ELIGIBLE_RATIO:
        frozen = 5
    return MetricSelectionSnapshot(
        card_prop_choice=choice,
        card_prop_exact_count=exact,
        card_prop_frozen_target=frozen,
    )


def _scenario(definition: MissionDefinition, *, success: bool) -> Scenario:
    bouts = _default_bouts()
    selection = MetricSelectionSnapshot()
    if isinstance(definition, TargetFighterMissionDefinition):
        selection = _fighter_target(definition, bouts, success)
    elif isinstance(definition, TargetFightMissionDefinition):
        selection = _fight_target(definition, bouts, success)
    elif isinstance(definition, ComboBuilderMissionDefinition):
        selection = _combo(definition, bouts, success)
    elif isinstance(definition, CardPropMissionDefinition):
        selection = _card_prop(definition, bouts, success)

    picks = _perfect_picks(bouts, correct=success)
    leaderboard = LeaderboardEvaluationSnapshot(
        rank=1 if success else 2,
        tied_for_first=False,
        active_user_count=3,
        finalized=True,
    )
    context = MissionEvaluationContext(
        event_id=9001,
        user_id="conformance-user",
        card_revision=1,
        eligibility_revision=1,
        eligibility_fingerprint="sha256:all-85-conformance",
        frozen_eligible_bout_ids=IDS,
        card_finalized=True,
        bouts=tuple(bouts),
        picks=picks,
        selection=selection,
        leaderboard=leaderboard,
    )
    observation = CARD_METRIC_REGISTRY.evaluate(
        definition.evaluation.metric,
        context,
        parameters=definition.evaluation.parameters,
    )
    resolution = resolve_metric_observation(
        spec=definition.evaluation,
        observation=observation,
        progress_template=definition.ui.progress_template,
    )
    return Scenario(context=context, resolution=resolution)


def _resolve(definition: MissionDefinition, context: MissionEvaluationContext):
    observation = CARD_METRIC_REGISTRY.evaluate(
        definition.evaluation.metric,
        context,
        parameters=definition.evaluation.parameters,
    )
    return resolve_metric_observation(
        spec=definition.evaluation,
        observation=observation,
        progress_template=definition.ui.progress_template,
    )


@pytest.mark.parametrize("definition", CATALOG, ids=lambda item: item.mission_id)
def test_every_reviewed_mission_has_a_completed_case(definition: MissionDefinition):
    scenario = _scenario(definition, success=True)
    assert scenario.resolution.status == MetricResolutionStatus.COMPLETED, (
        definition.mission_id,
        scenario.resolution.model_dump(mode="json"),
    )
    assert scenario.resolution.progress.text
    assert "{" not in scenario.resolution.progress.text


@pytest.mark.parametrize("definition", CATALOG, ids=lambda item: item.mission_id)
def test_every_reviewed_mission_has_a_terminal_miss(definition: MissionDefinition):
    scenario = _scenario(definition, success=False)
    assert scenario.resolution.status == MetricResolutionStatus.FAILED, (
        definition.mission_id,
        scenario.resolution.model_dump(mode="json"),
    )


@pytest.mark.parametrize(
    "definition",
    tuple(
        definition
        for definition in CATALOG
        if isinstance(
            definition,
            TargetFighterMissionDefinition
            | TargetFightMissionDefinition
            | ComboBuilderMissionDefinition,
        )
    ),
    ids=lambda item: item.mission_id,
)
def test_direct_and_combo_targets_void_when_the_first_target_is_cancelled(
    definition: MissionDefinition,
):
    scenario = _scenario(definition, success=True)
    bouts = list(scenario.context.bouts)
    target_id = scenario.context.selection.legs[0].bout_id
    target = bouts[target_id - 1]
    bouts[target_id - 1] = target.model_copy(
        update={
            "lifecycle": BoutLifecycle.CANCELLED,
            "is_current": False,
            "result": None,
        }
    )
    context = scenario.context.model_copy(update={"bouts": tuple(bouts)})
    resolution = _resolve(definition, context)
    assert resolution.status == MetricResolutionStatus.VOID, definition.mission_id


@pytest.mark.parametrize(
    "definition",
    tuple(
        definition
        for definition in CATALOG
        if definition.evaluation.parameters.get("min_total_count", 0) > 1
    ),
    ids=lambda item: item.mission_id,
)
def test_minimum_card_contracts_void_if_too_few_targets_survive(
    definition: MissionDefinition,
):
    scenario = _scenario(definition, success=True)
    bouts = tuple(
        bout
        if bout.bout_id == 1
        else bout.model_copy(
            update={
                "lifecycle": BoutLifecycle.CANCELLED,
                "is_current": False,
                "result": None,
            }
        )
        for bout in scenario.context.bouts
    )
    context = scenario.context.model_copy(
        update={
            "bouts": bouts,
            "picks": tuple(
                pick for pick in scenario.context.picks if pick.bout_id == 1
            ),
        }
    )
    resolution = _resolve(definition, context)
    assert resolution.status == MetricResolutionStatus.VOID, definition.mission_id


def test_leaderboard_contract_voids_when_final_rank_is_unavailable():
    definition = CATALOG.get("CARD-V2-M-020")
    scenario = _scenario(definition, success=True)
    resolution = _resolve(
        definition,
        scenario.context.model_copy(update={"leaderboard": None}),
    )
    assert resolution.status == MetricResolutionStatus.VOID


def _pre_result_context(
    context: MissionEvaluationContext,
) -> MissionEvaluationContext:
    """The best state a user can reach alone: every pick in, no fight decided."""
    return context.model_copy(
        update={
            "bouts": tuple(
                bout.model_copy(
                    update={
                        "lifecycle": BoutLifecycle.SCHEDULED,
                        "is_current": True,
                        "result": None,
                    }
                )
                for bout in context.bouts
            ),
            "picks": tuple(
                pick.model_copy(
                    update={"points_awarded": None, "score_revision": None}
                )
                for pick in context.picks
            ),
            "card_finalized": False,
            "leaderboard": None,
        }
    )


@pytest.mark.parametrize("definition", CATALOG, ids=lambda item: item.mission_id)
def test_no_mission_resolves_before_a_single_fight_is_decided(
    definition: MissionDefinition,
):
    """No reviewed mission may pay out, or die, on submitted picks alone.

    Completing here would make the mission a participation reward; failing here
    would make it unwinnable from the first evaluation the card triggers.
    """
    scenario = _scenario(definition, success=True)
    resolution = _resolve(definition, _pre_result_context(scenario.context))
    assert resolution.status == MetricResolutionStatus.PENDING, (
        definition.mission_id,
        resolution.status.value,
        resolution.reason.value,
    )


def test_clean_sweep_stays_active_while_a_perfect_card_is_running():
    definition = CATALOG.get("CARD-V2-H-006")
    scenario = _scenario(definition, success=True)
    decided = list(scenario.context.bouts)

    for resolved_count in range(len(decided)):
        bouts = tuple(
            bout
            if index < resolved_count
            else bout.model_copy(
                update={
                    "lifecycle": BoutLifecycle.SCHEDULED,
                    "is_current": True,
                    "result": None,
                }
            )
            for index, bout in enumerate(decided)
        )
        resolution = _resolve(
            definition,
            scenario.context.model_copy(
                update={
                    "bouts": bouts,
                    "card_finalized": False,
                    "leaderboard": None,
                }
            ),
        )
        assert resolution.status == MetricResolutionStatus.PENDING, (
            resolved_count,
            resolution.reason.value,
        )

    assert scenario.resolution.status == MetricResolutionStatus.COMPLETED


def test_clean_sweep_fails_when_a_decided_bout_was_never_picked():
    definition = CATALOG.get("CARD-V2-H-006")
    scenario = _scenario(definition, success=True)
    context = scenario.context.model_copy(
        update={
            "picks": tuple(
                pick for pick in scenario.context.picks if pick.bout_id != 3
            )
        }
    )
    resolution = _resolve(definition, context)
    assert resolution.status == MetricResolutionStatus.FAILED
    assert resolution.observation.value == 1


def test_catalog_conformance_matrix_covers_all_85_unique_ids():
    assert len(CATALOG) == 85
    assert len({definition.mission_id for definition in CATALOG}) == 85
    assert Counter(definition.interaction for definition in CATALOG) == {
        MissionInteractionType.AUTO: 40,
        MissionInteractionType.TARGET_FIGHTER: 9,
        MissionInteractionType.TARGET_FIGHT: 5,
        MissionInteractionType.COMBO_BUILDER: 12,
        MissionInteractionType.CARD_PROP: 19,
    }
