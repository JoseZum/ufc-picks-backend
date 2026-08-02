"""Pure metric registry over normalized mission-evaluation snapshots."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.modules.missions.domain.definitions import (
    ScalarParameter,
    TargetFightOutcome,
    WinMethod,
)
from app.modules.missions.domain.enums import StringEnum
from app.modules.missions.domain.evaluation import (
    BoutEvaluationSnapshot,
    BoutLifecycle,
    BoutOutcome,
    BoutResultSnapshot,
    BoutRole,
    CardSection,
    FighterMetricLeg,
    FightMetricLeg,
    MetricVoidReason,
    MissionEvaluationContext,
    PickEvaluationSnapshot,
    ResultMethodFamily,
)

MetricScalar: TypeAlias = int | float


class MetricModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MetricRequest(MetricModel):
    metric: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    parameters: dict[str, ScalarParameter] = Field(default_factory=dict)


class MetricObservation(MetricModel):
    metric: str
    value: MetricScalar
    other_value: MetricScalar | None = None
    numerator: int | None = Field(default=None, ge=0)
    denominator: int | None = Field(default=None, ge=0)
    sample_size: int = Field(default=0, ge=0)
    resolved_count: int = Field(default=0, ge=0)
    total_count: int = Field(default=0, ge=0)
    matched_items: frozenset[str] = Field(default_factory=frozenset)
    target_override: MetricScalar | None = None
    terminal: bool
    void_reason: MetricVoidReason | None = None
    details: dict[str, ScalarParameter] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_counts(self):
        if (self.numerator is None) != (self.denominator is None):
            raise ValueError("numerator and denominator must appear together")
        if self.numerator is not None and self.numerator > self.denominator:
            raise ValueError("numerator cannot exceed denominator")
        if self.resolved_count > self.total_count:
            raise ValueError("resolved_count cannot exceed total_count")
        if self.void_reason is not None and not self.terminal:
            raise ValueError("VOID observations must be terminal")
        return self


class MetricRegistryErrorCode(StringEnum):
    UNKNOWN_METRIC = "UNKNOWN_METRIC"
    DUPLICATE_METRIC = "DUPLICATE_METRIC"
    INVALID_PARAMETERS = "INVALID_PARAMETERS"
    INVALID_SELECTION = "INVALID_SELECTION"


class MetricRegistryError(ValueError):
    def __init__(self, code: MetricRegistryErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


MetricProvider: TypeAlias = Callable[
    [MetricRequest, MissionEvaluationContext],
    MetricObservation,
]


class MetricRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, MetricProvider] = {}

    def register(self, names: str | tuple[str, ...], provider: MetricProvider) -> None:
        values = (names,) if isinstance(names, str) else names
        if len(values) != len(set(values)):
            raise MetricRegistryError(
                MetricRegistryErrorCode.DUPLICATE_METRIC,
                "One registration contains duplicate metric names",
            )
        duplicates = sorted(set(values) & self._providers.keys())
        if duplicates:
            raise MetricRegistryError(
                MetricRegistryErrorCode.DUPLICATE_METRIC,
                f"Metrics already registered: {', '.join(duplicates)}",
            )
        self._providers.update({name: provider for name in values})

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    def evaluate(
        self,
        metric: str,
        context: MissionEvaluationContext,
        *,
        parameters: Mapping[str, ScalarParameter] | None = None,
    ) -> MetricObservation:
        provider = self._providers.get(metric)
        if provider is None:
            raise MetricRegistryError(
                MetricRegistryErrorCode.UNKNOWN_METRIC,
                f"Unknown mission metric: {metric}",
            )
        request = MetricRequest(metric=metric, parameters=dict(parameters or {}))
        return provider(request, context)


CARD_METRIC_NAMES: tuple[str, ...] = (
    "card_decision_count",
    "card_decision_vs_finish",
    "card_finish_count",
    "card_finish_rate",
    "card_finish_vs_decision",
    "card_method_presence",
    "card_round_finish_count",
    "card_submission_count",
    "co_main_correct_winner",
    "co_main_winner_and_finish",
    "combo_bout_finish_count",
    "combo_correct_decision_wins",
    "combo_correct_ko_wins",
    "combo_correct_method_cycle",
    "combo_correct_round_ladder",
    "combo_correct_submission_wins",
    "combo_correct_winner_count",
    "combo_distinct_correct_methods",
    "combo_finish_decision_bouts",
    "combo_ko_dec_correct",
    "combo_ko_sub_correct",
    "correct_winner_count",
    "event_leaderboard_rank",
    "event_pick_points",
    "main_and_co_main_correct_winners",
    "main_card_correct_winner_count",
    "main_card_decision_count",
    "main_card_finish_count",
    "main_card_winner_accuracy",
    "main_card_winner_method_accuracy",
    "main_co_main_perfect_pick_count",
    "main_event_correct_winner",
    "main_event_pick_points",
    "main_event_winner_and_finish",
    "main_or_co_main_correct_count",
    "perfect_pick_count",
    "prelim_correct_winner_count",
    "prelim_finish_count",
    "prelim_winner_accuracy",
    "selected_bout_finish_round",
    "selected_bout_is_decision",
    "selected_bout_is_finish",
    "selected_exact_pick",
    "selected_fighter_decision_win",
    "selected_fighter_ko_round_one_win",
    "selected_fighter_ko_win",
    "selected_fighter_loss",
    "selected_fighter_submission_round_one_win",
    "selected_fighter_submission_win",
    "selected_fighter_win",
    "selected_result_family_vs_other",
    "selected_title_bout_winner_method",
    "title_bout_correct_winner_count",
    "title_bout_winner_accuracy",
    "title_bout_winner_method_accuracy",
    "two_plus_point_pick_count",
    "winner_accuracy",
    "wrong_winner_count",
)

_VOID_LIFECYCLES = {
    BoutLifecycle.CANCELLED: MetricVoidReason.TARGET_CANCELLED,
    BoutLifecycle.POSTPONED: MetricVoidReason.TARGET_POSTPONED,
    BoutLifecycle.REPLACED: MetricVoidReason.TARGET_REPLACED,
}
_RESULT_METHOD_BY_PICK = {
    WinMethod.KO_TKO: ResultMethodFamily.KO_TKO,
    WinMethod.SUBMISSION: ResultMethodFamily.SUBMISSION,
    WinMethod.DECISION: ResultMethodFamily.DECISION,
}


def _observation(
    request: MetricRequest,
    context: MissionEvaluationContext,
    *,
    value: MetricScalar,
    other_value: MetricScalar | None = None,
    numerator: int | None = None,
    denominator: int | None = None,
    sample_size: int = 0,
    resolved_count: int = 0,
    total_count: int = 0,
    matched_items: frozenset[str] = frozenset(),
    target_override: MetricScalar | None = None,
    terminal: bool | None = None,
    void_reason: MetricVoidReason | None = None,
    details: dict[str, ScalarParameter] | None = None,
) -> MetricObservation:
    return MetricObservation(
        metric=request.metric,
        value=value,
        other_value=other_value,
        numerator=numerator,
        denominator=denominator,
        sample_size=sample_size,
        resolved_count=resolved_count,
        total_count=total_count,
        matched_items=matched_items,
        target_override=target_override,
        terminal=context.card_finalized if terminal is None else terminal,
        void_reason=void_reason,
        details=details or {},
    )


def _int_parameter(
    request: MetricRequest,
    name: str,
    *,
    default: int | None = None,
    minimum: int = 0,
) -> int:
    value = request.parameters.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise MetricRegistryError(
            MetricRegistryErrorCode.INVALID_PARAMETERS,
            f"Metric {request.metric} requires integer parameter {name} >= {minimum}",
        )
    return value


def _float_parameter(
    request: MetricRequest,
    name: str,
    *,
    default: float | None = None,
    minimum: float = 0,
    maximum: float = 1,
) -> float:
    value = request.parameters.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise MetricRegistryError(
            MetricRegistryErrorCode.INVALID_PARAMETERS,
            f"Metric {request.metric} requires numeric parameter {name}",
        )
    number = float(value)
    if not minimum <= number <= maximum:
        raise MetricRegistryError(
            MetricRegistryErrorCode.INVALID_PARAMETERS,
            f"Metric {request.metric} parameter {name} is outside its range",
        )
    return number


def _bool_parameter(request: MetricRequest, name: str, *, default: bool = False) -> bool:
    value = request.parameters.get(name, default)
    if not isinstance(value, bool):
        raise MetricRegistryError(
            MetricRegistryErrorCode.INVALID_PARAMETERS,
            f"Metric {request.metric} requires boolean parameter {name}",
        )
    return value


def _surviving_bouts(
    context: MissionEvaluationContext,
    *,
    section: CardSection | None = None,
    roles: frozenset[BoutRole] | None = None,
    title_only: bool = False,
) -> tuple[BoutEvaluationSnapshot, ...]:
    return tuple(
        bout
        for bout in context.surviving_eligible_bouts()
        if (section is None or bout.section == section)
        and (roles is None or bout.role in roles)
        and (not title_only or bout.is_title_fight)
    )


def _resolved(bouts: tuple[BoutEvaluationSnapshot, ...]) -> tuple[BoutEvaluationSnapshot, ...]:
    return tuple(bout for bout in bouts if bout.result is not None)


def _is_decision(result: BoutResultSnapshot) -> bool:
    return (
        result.outcome != BoutOutcome.NO_CONTEST
        and result.method == ResultMethodFamily.DECISION
    )


def _is_finish(result: BoutResultSnapshot) -> bool:
    return (
        result.outcome not in {BoutOutcome.DRAW, BoutOutcome.NO_CONTEST}
        and result.method
        in {ResultMethodFamily.KO_TKO, ResultMethodFamily.SUBMISSION}
    )


def _common_method(result: BoutResultSnapshot) -> str | None:
    if result.method in {
        ResultMethodFamily.KO_TKO,
        ResultMethodFamily.SUBMISSION,
        ResultMethodFamily.DECISION,
    }:
        return result.method.value
    return None


def _count_observation(
    request: MetricRequest,
    context: MissionEvaluationContext,
    bouts: tuple[BoutEvaluationSnapshot, ...],
    predicate: Callable[[BoutResultSnapshot], bool],
) -> MetricObservation:
    resolved = _resolved(bouts)
    count = sum(predicate(bout.result) for bout in resolved)
    target_override: int | None = None
    if context.selection.card_prop_exact_count is not None:
        target_override = context.selection.card_prop_exact_count
    elif context.selection.card_prop_frozen_target is not None:
        target_override = context.selection.card_prop_frozen_target
    return _observation(
        request,
        context,
        value=count,
        sample_size=len(resolved),
        resolved_count=len(resolved),
        total_count=len(bouts),
        target_override=target_override,
    )


def _card_decision_count(request, context):
    return _count_observation(
        request,
        context,
        _surviving_bouts(context),
        _is_decision,
    )


def _card_finish_count(request, context):
    return _count_observation(
        request,
        context,
        _surviving_bouts(context),
        _is_finish,
    )


def _card_submission_count(request, context):
    return _count_observation(
        request,
        context,
        _surviving_bouts(context),
        lambda result: result.outcome != BoutOutcome.NO_CONTEST
        and result.method == ResultMethodFamily.SUBMISSION,
    )


def _card_finish_rate(request, context):
    bouts = _surviving_bouts(context)
    resolved = _resolved(bouts)
    denominator = sum(
        _is_finish(bout.result) or _is_decision(bout.result) for bout in resolved
    )
    numerator = sum(_is_finish(bout.result) for bout in resolved)
    decisions = sum(_is_decision(bout.result) for bout in resolved)
    value = numerator / denominator if denominator else 0
    return _observation(
        request,
        context,
        value=value,
        numerator=numerator,
        denominator=denominator,
        sample_size=denominator,
        resolved_count=len(resolved),
        total_count=len(bouts),
        details={"finishes": numerator, "decisions": decisions},
    )


def _finish_vs_decision(request, context):
    bouts = _surviving_bouts(context)
    resolved = _resolved(bouts)
    finishes = sum(_is_finish(bout.result) for bout in resolved)
    decisions = sum(_is_decision(bout.result) for bout in resolved)
    reverse = request.metric == "card_decision_vs_finish"
    return _observation(
        request,
        context,
        value=decisions if reverse else finishes,
        other_value=finishes if reverse else decisions,
        sample_size=len(resolved),
        resolved_count=len(resolved),
        total_count=len(bouts),
        details={"finishes": finishes, "decisions": decisions},
    )


def _card_method_presence(request, context):
    bouts = _surviving_bouts(context)
    resolved = _resolved(bouts)
    presence_mode = request.parameters.get("presence_mode")
    if presence_mode in {None, "METHODS"}:
        methods = frozenset(
            method
            for bout in resolved
            if (method := _common_method(bout.result)) is not None
        )
    elif presence_mode == "FINISH_DECISION":
        methods = frozenset(
            family
            for family, present in (
                ("FINISH", any(_is_finish(bout.result) for bout in resolved)),
                ("DECISION", any(_is_decision(bout.result) for bout in resolved)),
            )
            if present
        )
    else:
        raise MetricRegistryError(
            MetricRegistryErrorCode.INVALID_PARAMETERS,
            f"Unsupported card method presence mode: {presence_mode}",
        )
    return _observation(
        request,
        context,
        value=len(methods),
        sample_size=len(resolved),
        resolved_count=len(resolved),
        total_count=len(bouts),
        matched_items=methods,
    )


def _card_round_finish_count(request, context):
    round_ = _int_parameter(request, "round", minimum=1)
    return _count_observation(
        request,
        context,
        _surviving_bouts(context),
        lambda result: _is_finish(result) and result.round == round_,
    )


def _section_count_provider(
    section: CardSection,
    predicate: Callable[[BoutResultSnapshot], bool],
) -> MetricProvider:
    def provider(request, context):
        return _count_observation(
            request,
            context,
            _surviving_bouts(context, section=section),
            predicate,
        )

    return provider


def _prelim_bouts(context: MissionEvaluationContext) -> tuple[BoutEvaluationSnapshot, ...]:
    return tuple(
        bout
        for bout in _surviving_bouts(context)
        if bout.section in {CardSection.PRELIM, CardSection.EARLY_PRELIM}
    )


def _prelim_finish_count(request, context):
    return _count_observation(request, context, _prelim_bouts(context), _is_finish)


def _selection_target_bout(
    context: MissionEvaluationContext,
    *,
    fighter: bool,
) -> tuple[BoutEvaluationSnapshot | None, FighterMetricLeg | FightMetricLeg | None]:
    if len(context.selection.legs) != 1:
        return None, None
    leg = context.selection.legs[0]
    if fighter != isinstance(leg, FighterMetricLeg):
        return None, None
    return context.bouts_by_id().get(leg.bout_id), leg


def _target_void_reason(
    bout: BoutEvaluationSnapshot | None,
    *,
    winner_or_method: bool,
) -> MetricVoidReason | None:
    if bout is None:
        return MetricVoidReason.TARGET_NOT_FOUND
    if bout.lifecycle in _VOID_LIFECYCLES:
        return _VOID_LIFECYCLES[bout.lifecycle]
    if (
        winner_or_method
        and bout.result is not None
        and bout.result.outcome in {BoutOutcome.DRAW, BoutOutcome.NO_CONTEST}
    ):
        return MetricVoidReason.TARGET_DRAW_OR_NO_CONTEST
    if bout.result is not None and bout.result.outcome == BoutOutcome.NO_CONTEST:
        return MetricVoidReason.TARGET_DRAW_OR_NO_CONTEST
    return None


def _terminal_void(
    request: MetricRequest,
    context: MissionEvaluationContext,
    reason: MetricVoidReason,
    *,
    total_count: int = 1,
) -> MetricObservation:
    return _observation(
        request,
        context,
        value=0,
        numerator=0,
        denominator=total_count,
        resolved_count=0,
        total_count=total_count,
        terminal=True,
        void_reason=reason,
    )


def _fighter_target_provider(
    *,
    expected_method: WinMethod | None = None,
    expected_round: int | None = None,
    loss: bool = False,
    exact_from_leg: bool = False,
    title_only: bool = False,
    require_method: bool = False,
) -> MetricProvider:
    def provider(request, context):
        bout, raw_leg = _selection_target_bout(context, fighter=True)
        if not isinstance(raw_leg, FighterMetricLeg):
            raise MetricRegistryError(
                MetricRegistryErrorCode.INVALID_SELECTION,
                f"Metric {request.metric} requires one fighter target",
            )
        reason = _target_void_reason(bout, winner_or_method=True)
        if reason:
            return _terminal_void(request, context, reason)
        assert bout is not None
        if title_only and not bout.is_title_fight:
            return _terminal_void(
                request,
                context,
                MetricVoidReason.TARGET_NOT_FOUND,
            )
        if require_method and raw_leg.method is None:
            raise MetricRegistryError(
                MetricRegistryErrorCode.INVALID_SELECTION,
                f"Metric {request.metric} requires a selected method",
            )
        if bout.result is None:
            component_count = 1 + int(
                (raw_leg.method if exact_from_leg else expected_method) is not None
            ) + int((raw_leg.round if exact_from_leg else expected_round) is not None)
            return _observation(
                request,
                context,
                value=0,
                numerator=0,
                denominator=component_count,
                total_count=component_count,
                terminal=False,
            )
        result = bout.result
        winner_match = result.winner_fighter_id == raw_leg.fighter_id
        if loss:
            checks = [not winner_match]
        else:
            method = raw_leg.method if exact_from_leg else expected_method
            round_ = raw_leg.round if exact_from_leg else expected_round
            checks = [winner_match]
            if method is not None:
                checks.append(result.method == _RESULT_METHOD_BY_PICK[method])
            if round_ is not None:
                checks.append(result.round == round_)
        matched = sum(checks)
        return _observation(
            request,
            context,
            value=matched,
            numerator=matched,
            denominator=len(checks),
            sample_size=1,
            resolved_count=len(checks),
            total_count=len(checks),
            terminal=True,
            details={"actual_round": result.round},
        )

    return provider


def _selected_exact_pick(request, context):
    bout, raw_leg = _selection_target_bout(context, fighter=True)
    if not isinstance(raw_leg, FighterMetricLeg):
        raise MetricRegistryError(
            MetricRegistryErrorCode.INVALID_SELECTION,
            "selected_exact_pick requires one fighter target",
        )
    if raw_leg.method is None or raw_leg.round is None:
        raise MetricRegistryError(
            MetricRegistryErrorCode.INVALID_SELECTION,
            "selected_exact_pick requires fighter, method and round",
        )
    reason = _target_void_reason(bout, winner_or_method=True)
    if reason:
        return _terminal_void(request, context, reason, total_count=3)
    assert bout is not None
    if bout.result is None:
        return _observation(
            request,
            context,
            value=0,
            numerator=0,
            denominator=3,
            total_count=3,
            terminal=False,
        )
    checks = (
        bout.result.winner_fighter_id == raw_leg.fighter_id,
        bout.result.method == _RESULT_METHOD_BY_PICK[raw_leg.method],
        bout.result.round == raw_leg.round,
    )
    matched = sum(checks)
    return _observation(
        request,
        context,
        value=matched,
        numerator=matched,
        denominator=3,
        sample_size=1,
        resolved_count=3,
        total_count=3,
        terminal=True,
    )


def _fight_target_provider(
    *,
    expected: str,
) -> MetricProvider:
    def provider(request, context):
        bout, raw_leg = _selection_target_bout(context, fighter=False)
        if not isinstance(raw_leg, FightMetricLeg):
            raise MetricRegistryError(
                MetricRegistryErrorCode.INVALID_SELECTION,
                f"Metric {request.metric} requires one fight target",
            )
        reason = _target_void_reason(bout, winner_or_method=False)
        if reason:
            return _terminal_void(request, context, reason)
        assert bout is not None
        if bout.result is None:
            return _observation(
                request,
                context,
                value=0,
                numerator=0,
                denominator=1,
                total_count=1,
                terminal=False,
            )
        if expected == "FINISH":
            success = _is_finish(bout.result)
        elif expected == "DECISION":
            success = _is_decision(bout.result)
        else:
            round_ = raw_leg.round or _int_parameter(request, "round", minimum=1)
            success = _is_finish(bout.result) and bout.result.round == round_
        return _observation(
            request,
            context,
            value=int(success),
            numerator=int(success),
            denominator=1,
            sample_size=1,
            resolved_count=1,
            total_count=1,
            terminal=True,
            details={"actual_round": bout.result.round},
        )

    return provider


def _validate_fighter_combo_contract(
    request: MetricRequest,
    context: MissionEvaluationContext,
    legs: tuple[FighterMetricLeg, ...],
) -> bool:
    methods = [leg.method for leg in legs]
    rounds = [leg.round for leg in legs]
    expected_method_sets = {
        "combo_correct_method_cycle": {
            WinMethod.KO_TKO,
            WinMethod.SUBMISSION,
            WinMethod.DECISION,
        },
        "combo_ko_dec_correct": {WinMethod.KO_TKO, WinMethod.DECISION},
        "combo_ko_sub_correct": {WinMethod.KO_TKO, WinMethod.SUBMISSION},
    }
    if request.metric == "combo_correct_winner_count":
        return False
    if request.metric == "combo_correct_round_ladder":
        if set(rounds) == {1, 2, 3} and all(method is None for method in methods):
            return False
        raise MetricRegistryError(
            MetricRegistryErrorCode.INVALID_SELECTION,
            f"Selection legs do not satisfy {request.metric}",
        )
    if request.metric == "combo_correct_decision_wins" and set(methods) != {
        WinMethod.DECISION
    }:
        pass
    elif request.metric == "combo_correct_ko_wins" and set(methods) != {
        WinMethod.KO_TKO
    }:
        pass
    elif request.metric == "combo_correct_submission_wins" and set(methods) != {
        WinMethod.SUBMISSION
    }:
        pass
    elif request.metric in expected_method_sets and set(methods) != expected_method_sets[
        request.metric
    ]:
        pass
    elif request.metric == "combo_distinct_correct_methods" and (
        None in methods or len(set(methods)) != len(methods)
    ):
        pass
    elif request.metric == "title_bout_winner_method_accuracy" and (
        None in methods
        or any(
            not context.bouts_by_id()[leg.bout_id].is_title_fight for leg in legs
        )
    ):
        pass
    else:
        return True
    raise MetricRegistryError(
        MetricRegistryErrorCode.INVALID_SELECTION,
        f"Selection legs do not satisfy {request.metric}",
    )


def _evaluate_fighter_combo(request, context):
    legs = context.selection.legs
    if len(legs) not in {2, 3} or not all(isinstance(leg, FighterMetricLeg) for leg in legs):
        raise MetricRegistryError(
            MetricRegistryErrorCode.INVALID_SELECTION,
            f"Metric {request.metric} requires two or three fighter legs",
        )
    typed_legs = tuple(leg for leg in legs if isinstance(leg, FighterMetricLeg))
    check_method = _validate_fighter_combo_contract(request, context, typed_legs)
    bouts = context.bouts_by_id()
    correct = 0
    resolved_count = 0
    any_failure = False
    for raw_leg in legs:
        assert isinstance(raw_leg, FighterMetricLeg)
        bout = bouts.get(raw_leg.bout_id)
        reason = _target_void_reason(bout, winner_or_method=True)
        if reason:
            return _terminal_void(request, context, reason, total_count=len(legs))
        assert bout is not None
        if bout.result is None:
            continue
        resolved_count += 1
        winner_success = bout.result.winner_fighter_id == raw_leg.fighter_id
        method_success = bool(
            raw_leg.method is not None
            and bout.result.method == _RESULT_METHOD_BY_PICK[raw_leg.method]
        )
        round_success = bool(
            raw_leg.round is not None and bout.result.round == raw_leg.round
        )
        if request.metric == "title_bout_winner_method_accuracy":
            leg_correct = int(winner_success) + int(method_success)
            correct += leg_correct
            any_failure = any_failure or leg_correct != 2
            continue
        success = winner_success
        if check_method and raw_leg.method is not None:
            success = success and method_success
        if raw_leg.round is not None:
            success = success and round_success
        correct += int(success)
        any_failure = any_failure or not success
    denominator = (
        len(legs) * 2
        if request.metric == "title_bout_winner_method_accuracy"
        else len(legs)
    )
    return _observation(
        request,
        context,
        value=correct,
        numerator=correct,
        denominator=denominator,
        sample_size=resolved_count,
        resolved_count=resolved_count,
        total_count=denominator,
        terminal=any_failure or resolved_count == len(legs),
    )


def _evaluate_fight_combo(request, context):
    legs = context.selection.legs
    if len(legs) != 2 or not all(isinstance(leg, FightMetricLeg) for leg in legs):
        raise MetricRegistryError(
            MetricRegistryErrorCode.INVALID_SELECTION,
            f"Metric {request.metric} requires two fight legs",
        )
    typed_legs = tuple(leg for leg in legs if isinstance(leg, FightMetricLeg))
    outcomes = {leg.outcome for leg in typed_legs}
    valid = (
        request.metric == "combo_bout_finish_count"
        and outcomes == {TargetFightOutcome.FINISH}
    ) or (
        request.metric == "combo_finish_decision_bouts"
        and outcomes == {TargetFightOutcome.FINISH, TargetFightOutcome.DECISION}
    )
    if not valid:
        raise MetricRegistryError(
            MetricRegistryErrorCode.INVALID_SELECTION,
            f"Selection legs do not satisfy {request.metric}",
        )
    bouts = context.bouts_by_id()
    correct = 0
    resolved_count = 0
    any_failure = False
    for raw_leg in legs:
        assert isinstance(raw_leg, FightMetricLeg)
        bout = bouts.get(raw_leg.bout_id)
        reason = _target_void_reason(bout, winner_or_method=False)
        if reason:
            return _terminal_void(request, context, reason, total_count=len(legs))
        assert bout is not None
        if bout.result is None:
            continue
        resolved_count += 1
        if raw_leg.outcome.value == "FINISH":
            success = _is_finish(bout.result)
        elif raw_leg.outcome.value == "DECISION":
            success = _is_decision(bout.result)
        else:
            success = _is_finish(bout.result) and bout.result.round == raw_leg.round
        correct += int(success)
        any_failure = any_failure or not success
    return _observation(
        request,
        context,
        value=correct,
        numerator=correct,
        denominator=len(legs),
        sample_size=resolved_count,
        resolved_count=resolved_count,
        total_count=len(legs),
        terminal=any_failure or resolved_count == len(legs),
    )


def _pick_pairs(
    context: MissionEvaluationContext,
    bouts: tuple[BoutEvaluationSnapshot, ...],
) -> tuple[tuple[BoutEvaluationSnapshot, PickEvaluationSnapshot], ...]:
    picks = context.picks_by_bout_id()
    return tuple(
        (bout, pick)
        for bout in bouts
        if bout.result is not None
        and bout.result.outcome in {BoutOutcome.RED_WIN, BoutOutcome.BLUE_WIN}
        and (pick := picks.get(bout.bout_id)) is not None
    )


def _winner_correct(bout: BoutEvaluationSnapshot, pick: PickEvaluationSnapshot) -> bool:
    return bool(bout.result and bout.result.winner_fighter_id == pick.winner_fighter_id)


def _method_correct(bout: BoutEvaluationSnapshot, pick: PickEvaluationSnapshot) -> bool:
    return bool(
        bout.result
        and _winner_correct(bout, pick)
        and bout.result.method == _RESULT_METHOD_BY_PICK[pick.method]
    )


def _role_bouts(
    context: MissionEvaluationContext,
    roles: frozenset[BoutRole],
) -> tuple[BoutEvaluationSnapshot, ...]:
    return _surviving_bouts(context, roles=roles)


def _correct_winner_count_for(
    request: MetricRequest,
    context: MissionEvaluationContext,
    bouts: tuple[BoutEvaluationSnapshot, ...],
) -> MetricObservation:
    pairs = _pick_pairs(context, bouts)
    correct = sum(_winner_correct(bout, pick) for bout, pick in pairs)
    resolved = _resolved(bouts)
    return _observation(
        request,
        context,
        value=correct,
        numerator=correct,
        denominator=len(pairs),
        sample_size=len(pairs),
        resolved_count=len(resolved),
        total_count=len(bouts),
        terminal=bool(bouts) and len(resolved) == len(bouts),
    )


def _correct_winner_count(request, context):
    return _correct_winner_count_for(request, context, _surviving_bouts(context))


def _section_correct_provider(section: CardSection) -> MetricProvider:
    def provider(request, context):
        return _correct_winner_count_for(
            request,
            context,
            _surviving_bouts(context, section=section),
        )

    return provider


def _role_correct_provider(roles: frozenset[BoutRole]) -> MetricProvider:
    def provider(request, context):
        return _correct_winner_count_for(request, context, _role_bouts(context, roles))

    return provider


def _title_correct_winner_count(request, context):
    return _correct_winner_count_for(
        request,
        context,
        _surviving_bouts(context, title_only=True),
    )


def _winner_and_finish_provider(role: BoutRole) -> MetricProvider:
    def provider(request, context):
        bouts = _role_bouts(context, frozenset({role}))
        pairs = _pick_pairs(context, bouts)
        checks = (
            (
                _winner_correct(*pairs[0]),
                _is_finish(pairs[0][0].result),
            )
            if pairs
            else (False, False)
        )
        matched = sum(checks)
        resolved = _resolved(bouts)
        return _observation(
            request,
            context,
            value=matched,
            numerator=matched,
            denominator=2,
            sample_size=len(pairs),
            resolved_count=len(resolved),
            total_count=2,
            terminal=bool(bouts) and len(resolved) == len(bouts),
            details={
                "winner_matched": checks[0],
                "finish_matched": checks[1],
            },
        )

    return provider


def _event_pick_points(request, context):
    eligible = {bout.bout_id for bout in _surviving_bouts(context)}
    scored = [
        pick
        for pick in context.picks
        if pick.bout_id in eligible and pick.points_awarded is not None
    ]
    return _observation(
        request,
        context,
        value=sum(pick.points_awarded for pick in scored),
        sample_size=len(scored),
        resolved_count=len(scored),
        total_count=len(_surviving_bouts(context)),
    )


def _main_event_pick_points(request, context):
    bout_ids = {bout.bout_id for bout in _role_bouts(context, frozenset({BoutRole.MAIN_EVENT}))}
    scored = [
        pick
        for pick in context.picks
        if pick.bout_id in bout_ids and pick.points_awarded is not None
    ]
    return _observation(
        request,
        context,
        value=sum(pick.points_awarded for pick in scored),
        sample_size=len(scored),
        resolved_count=len(scored),
        total_count=len(bout_ids),
    )


def _point_pick_count_provider(minimum: int) -> MetricProvider:
    def provider(request, context):
        eligible = {bout.bout_id for bout in _surviving_bouts(context)}
        scored = [
            pick
            for pick in context.picks
            if pick.bout_id in eligible and pick.points_awarded is not None
        ]
        count = sum(pick.points_awarded >= minimum for pick in scored)
        return _observation(
            request,
            context,
            value=count,
            sample_size=len(scored),
            resolved_count=len(scored),
            total_count=len(_surviving_bouts(context)),
        )

    return provider


def _winner_accuracy_for(
    request: MetricRequest,
    context: MissionEvaluationContext,
    bouts: tuple[BoutEvaluationSnapshot, ...],
    *,
    require_every_target: bool = False,
    method_too: bool = False,
) -> MetricObservation:
    pairs = _pick_pairs(context, bouts)
    predicate = _method_correct if method_too else _winner_correct
    correct = sum(predicate(bout, pick) for bout, pick in pairs)
    denominator = len(bouts) if require_every_target else len(pairs)
    value = correct / denominator if denominator else 0
    return _observation(
        request,
        context,
        value=value,
        numerator=correct,
        denominator=denominator,
        sample_size=denominator,
        resolved_count=len(_resolved(bouts)),
        total_count=len(bouts),
    )


def _winner_accuracy(request, context):
    return _winner_accuracy_for(request, context, _surviving_bouts(context))


def _main_card_winner_accuracy(request, context):
    return _winner_accuracy_for(
        request,
        context,
        _surviving_bouts(context, section=CardSection.MAIN),
        require_every_target=True,
    )


def _prelim_winner_accuracy(request, context):
    return _winner_accuracy_for(
        request,
        context,
        _prelim_bouts(context),
        require_every_target=True,
    )


def _title_winner_accuracy(request, context):
    return _winner_accuracy_for(
        request,
        context,
        _surviving_bouts(context, title_only=True),
        require_every_target=True,
    )


def _main_card_winner_method_accuracy(request, context):
    return _winner_accuracy_for(
        request,
        context,
        _surviving_bouts(context, section=CardSection.MAIN),
        require_every_target=True,
        method_too=True,
    )


def _title_winner_method_accuracy(request, context):
    return _evaluate_fighter_combo(request, context)


def _wrong_winner_count(request, context):
    bouts = _surviving_bouts(context)
    pairs = _pick_pairs(context, bouts)
    wrong = sum(not _winner_correct(bout, pick) for bout, pick in pairs)
    if _bool_parameter(request, "require_full_card", default=False):
        # A bout the user never picked counts as a miss, but only once it has
        # actually produced a winner. Counting still-scheduled bouts here would
        # make a full-card mission look irreversibly failed before the card runs.
        picked = {pick.bout_id for _, pick in pairs}
        decided = {
            bout.bout_id
            for bout in bouts
            if bout.result is not None
            and bout.result.outcome in {BoutOutcome.RED_WIN, BoutOutcome.BLUE_WIN}
        }
        wrong += len(decided - picked)
        sample_size = len(bouts)
    else:
        sample_size = len(pairs)
    return _observation(
        request,
        context,
        value=wrong,
        sample_size=sample_size,
        resolved_count=len(_resolved(bouts)),
        total_count=len(bouts),
    )


def _main_co_main_perfect_count(request, context):
    ids = {
        bout.bout_id
        for bout in _role_bouts(
            context,
            frozenset({BoutRole.MAIN_EVENT, BoutRole.CO_MAIN}),
        )
    }
    scored = [
        pick
        for pick in context.picks
        if pick.bout_id in ids and pick.points_awarded is not None
    ]
    count = sum(pick.points_awarded == 3 for pick in scored)
    return _observation(
        request,
        context,
        value=count,
        numerator=count,
        denominator=len(ids),
        sample_size=len(scored),
        resolved_count=len(scored),
        total_count=len(ids),
        terminal=context.card_finalized or (bool(ids) and len(scored) == len(ids)),
    )


def _selected_result_family(request, context):
    choice = (context.selection.card_prop_choice or "").upper()
    if choice not in {"FINISHES", "DECISIONS"}:
        raise MetricRegistryError(
            MetricRegistryErrorCode.INVALID_SELECTION,
            "selected_result_family_vs_other requires FINISHES or DECISIONS",
        )
    bouts = _surviving_bouts(context)
    resolved = _resolved(bouts)
    finishes = sum(_is_finish(bout.result) for bout in resolved)
    decisions = sum(_is_decision(bout.result) for bout in resolved)
    chosen, other = (
        (finishes, decisions) if choice == "FINISHES" else (decisions, finishes)
    )
    return _observation(
        request,
        context,
        value=chosen,
        other_value=other,
        sample_size=len(resolved),
        resolved_count=len(resolved),
        total_count=len(bouts),
        details={
            "selected_family": choice,
            "finishes": finishes,
            "decisions": decisions,
        },
    )


def _leaderboard_rank(request, context):
    leaderboard = context.leaderboard
    if leaderboard is None:
        if context.card_finalized:
            return _terminal_void(
                request,
                context,
                MetricVoidReason.LEADERBOARD_UNAVAILABLE,
            )
        return _observation(
            request,
            context,
            value=0,
            terminal=False,
        )
    first = leaderboard.rank == 1 or leaderboard.tied_for_first
    return _observation(
        request,
        context,
        value=int(first),
        sample_size=leaderboard.active_user_count,
        resolved_count=int(leaderboard.finalized),
        total_count=1,
        terminal=leaderboard.finalized,
        details={
            "rank": leaderboard.rank,
            "tied_for_first": leaderboard.tied_for_first,
        },
    )


def build_card_metric_registry() -> MetricRegistry:
    registry = MetricRegistry()
    registry.register("card_decision_count", _card_decision_count)
    registry.register("card_finish_count", _card_finish_count)
    registry.register("card_submission_count", _card_submission_count)
    registry.register("card_finish_rate", _card_finish_rate)
    registry.register(
        ("card_finish_vs_decision", "card_decision_vs_finish"),
        _finish_vs_decision,
    )
    registry.register("card_method_presence", _card_method_presence)
    registry.register("card_round_finish_count", _card_round_finish_count)
    registry.register(
        "main_card_decision_count",
        _section_count_provider(CardSection.MAIN, _is_decision),
    )
    registry.register(
        "main_card_finish_count",
        _section_count_provider(CardSection.MAIN, _is_finish),
    )
    registry.register(
        "prelim_finish_count",
        _prelim_finish_count,
    )

    registry.register("selected_fighter_win", _fighter_target_provider())
    registry.register("selected_fighter_loss", _fighter_target_provider(loss=True))
    registry.register(
        "selected_fighter_decision_win",
        _fighter_target_provider(expected_method=WinMethod.DECISION),
    )
    registry.register(
        "selected_fighter_ko_win",
        _fighter_target_provider(expected_method=WinMethod.KO_TKO),
    )
    registry.register(
        "selected_fighter_submission_win",
        _fighter_target_provider(expected_method=WinMethod.SUBMISSION),
    )
    registry.register(
        "selected_fighter_ko_round_one_win",
        _fighter_target_provider(
            expected_method=WinMethod.KO_TKO,
            expected_round=1,
        ),
    )
    registry.register(
        "selected_fighter_submission_round_one_win",
        _fighter_target_provider(
            expected_method=WinMethod.SUBMISSION,
            expected_round=1,
        ),
    )
    registry.register("selected_exact_pick", _selected_exact_pick)
    registry.register(
        "selected_title_bout_winner_method",
        _fighter_target_provider(
            exact_from_leg=True,
            title_only=True,
            require_method=True,
        ),
    )
    registry.register(
        "selected_bout_is_finish",
        _fight_target_provider(expected="FINISH"),
    )
    registry.register(
        "selected_bout_is_decision",
        _fight_target_provider(expected="DECISION"),
    )
    registry.register(
        "selected_bout_finish_round",
        _fight_target_provider(expected="FINISH_ROUND"),
    )

    registry.register(
        (
            "combo_correct_winner_count",
            "combo_correct_decision_wins",
            "combo_correct_ko_wins",
            "combo_correct_method_cycle",
            "combo_correct_round_ladder",
            "combo_correct_submission_wins",
            "combo_distinct_correct_methods",
            "combo_ko_dec_correct",
            "combo_ko_sub_correct",
        ),
        _evaluate_fighter_combo,
    )
    registry.register(
        ("combo_bout_finish_count", "combo_finish_decision_bouts"),
        _evaluate_fight_combo,
    )

    registry.register("correct_winner_count", _correct_winner_count)
    registry.register(
        "main_card_correct_winner_count",
        _section_correct_provider(CardSection.MAIN),
    )
    registry.register(
        "prelim_correct_winner_count",
        lambda request, context: _correct_winner_count_for(
            request,
            context,
            _prelim_bouts(context),
        ),
    )
    registry.register(
        "main_event_correct_winner",
        _role_correct_provider(frozenset({BoutRole.MAIN_EVENT})),
    )
    registry.register(
        "co_main_correct_winner",
        _role_correct_provider(frozenset({BoutRole.CO_MAIN})),
    )
    registry.register(
        ("main_or_co_main_correct_count", "main_and_co_main_correct_winners"),
        _role_correct_provider(frozenset({BoutRole.MAIN_EVENT, BoutRole.CO_MAIN})),
    )
    registry.register("title_bout_correct_winner_count", _title_correct_winner_count)
    registry.register(
        "main_event_winner_and_finish",
        _winner_and_finish_provider(BoutRole.MAIN_EVENT),
    )
    registry.register(
        "co_main_winner_and_finish",
        _winner_and_finish_provider(BoutRole.CO_MAIN),
    )
    registry.register("event_pick_points", _event_pick_points)
    registry.register("main_event_pick_points", _main_event_pick_points)
    registry.register("perfect_pick_count", _point_pick_count_provider(3))
    registry.register("two_plus_point_pick_count", _point_pick_count_provider(2))
    registry.register("winner_accuracy", _winner_accuracy)
    registry.register("wrong_winner_count", _wrong_winner_count)
    registry.register("main_card_winner_accuracy", _main_card_winner_accuracy)
    registry.register("prelim_winner_accuracy", _prelim_winner_accuracy)
    registry.register("title_bout_winner_accuracy", _title_winner_accuracy)
    registry.register(
        "main_card_winner_method_accuracy",
        _main_card_winner_method_accuracy,
    )
    registry.register(
        "title_bout_winner_method_accuracy",
        _title_winner_method_accuracy,
    )
    registry.register("main_co_main_perfect_pick_count", _main_co_main_perfect_count)
    registry.register("selected_result_family_vs_other", _selected_result_family)
    registry.register("event_leaderboard_rank", _leaderboard_rank)

    if registry.names != tuple(sorted(CARD_METRIC_NAMES)):
        missing = sorted(set(CARD_METRIC_NAMES) - set(registry.names))
        extra = sorted(set(registry.names) - set(CARD_METRIC_NAMES))
        raise RuntimeError(f"Metric registry mismatch; missing={missing}, extra={extra}")
    return registry


CARD_METRIC_REGISTRY = build_card_metric_registry()
