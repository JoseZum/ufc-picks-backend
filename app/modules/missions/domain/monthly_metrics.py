"""Month-scoped metrics over per-event summaries.

A month is evaluated from one immutable summary per event that finished inside it.
Keeping the summary as the unit means a correction re-summarizes a single event and
the month recomputes deterministically, instead of replaying every pick again.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from string import Formatter

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.modules.missions.domain.definitions import (
    MetricComparator,
    MissionEvaluationSpec,
)
from app.modules.missions.domain.metrics import (
    MetricObservation,
    MetricRegistryError,
    MetricRegistryErrorCode,
)
from app.modules.missions.domain.monthly import (
    MonthlyMissionDefinition,
    MonthlyParameterKind,
)
from app.modules.missions.domain.resolution import (
    COMPARATOR_REGISTRY,
    MetricResolution,
    MetricResolutionReason,
    MetricResolutionStatus,
    ProgressSnapshot,
    ResolutionError,
    ResolutionErrorCode,
)


class MonthlyMetricModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MonthlyEventSummary(MonthlyMetricModel):
    """What one finished event contributed to one user's month."""

    event_id: int = Field(gt=0)
    month_key: str = Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")
    #: Bumped whenever the event is re-summarized after a result correction.
    summary_revision: int = Field(default=1, ge=1)

    resolved_bouts: int = Field(default=0, ge=0)
    resolved_picks: int = Field(default=0, ge=0)
    correct_winners: int = Field(default=0, ge=0)
    wrong_winners: int = Field(default=0, ge=0)
    pick_points: int = Field(default=0, ge=0)

    perfect_picks: int = Field(default=0, ge=0)
    two_plus_point_picks: int = Field(default=0, ge=0)
    correct_finish_methods: int = Field(default=0, ge=0)
    correct_ko_wins: int = Field(default=0, ge=0)
    correct_submission_wins: int = Field(default=0, ge=0)
    correct_decision_wins: int = Field(default=0, ge=0)

    main_event_correct: bool = False
    co_main_correct: bool = False
    main_card_bouts: int = Field(default=0, ge=0)
    main_card_correct: int = Field(default=0, ge=0)

    completed_card_missions: int = Field(default=0, ge=0)
    completed_hard_card_missions: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_counts(self):
        if self.correct_winners + self.wrong_winners > self.resolved_picks:
            raise ValueError("correct and wrong winners cannot exceed resolved picks")
        if self.main_card_correct > self.main_card_bouts:
            raise ValueError("main-card correct picks cannot exceed main-card bouts")
        if self.completed_hard_card_missions > self.completed_card_missions:
            raise ValueError("hard missions are a subset of completed card missions")
        return self


class MonthlyEvaluationContext(MonthlyMetricModel):
    month_key: str = Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")
    user_id: str = Field(min_length=1)
    parameters: dict[str, int]
    events: tuple[MonthlyEventSummary, ...] = ()
    #: A month only settles when its configuration closes.
    month_closed: bool = False

    @model_validator(mode="after")
    def validate_events(self):
        if any(event.month_key != self.month_key for event in self.events):
            raise ValueError("every event summary must belong to this month")
        ids = [event.event_id for event in self.events]
        if len(ids) != len(set(ids)):
            raise ValueError("an event may only be summarized once per month")
        return self

    def total(self, field: str) -> int:
        return sum(getattr(event, field) for event in self.events)

    def count_events(self, predicate: Callable[[MonthlyEventSummary], bool]) -> int:
        return sum(1 for event in self.events if predicate(event))


MonthlyMetricProvider = Callable[[MonthlyEvaluationContext], MetricObservation]


def _observation(
    metric: str,
    context: MonthlyEvaluationContext,
    *,
    value: int | float,
    numerator: int | None = None,
    denominator: int | None = None,
    details: dict[str, int | float | bool | str] | None = None,
) -> MetricObservation:
    return MetricObservation(
        metric=metric,
        value=value,
        numerator=numerator,
        denominator=denominator,
        sample_size=context.total("resolved_picks"),
        resolved_count=context.total("resolved_picks"),
        total_count=context.total("resolved_bouts"),
        terminal=context.month_closed,
        details=details or {},
    )


def _sum_provider(metric: str, field: str) -> MonthlyMetricProvider:
    def provider(context: MonthlyEvaluationContext) -> MetricObservation:
        return _observation(metric, context, value=context.total(field))

    return provider


def _event_count_provider(
    metric: str,
    predicate: Callable[[MonthlyEventSummary], bool],
) -> MonthlyMetricProvider:
    def provider(context: MonthlyEvaluationContext) -> MetricObservation:
        matched = context.count_events(predicate)
        return _observation(
            metric,
            context,
            value=matched,
            numerator=matched,
            denominator=len(context.events),
        )

    return provider


def _winner_accuracy(context: MonthlyEvaluationContext) -> MetricObservation:
    correct = context.total("correct_winners")
    resolved = context.total("resolved_picks")
    return _observation(
        "monthly_winner_accuracy",
        context,
        value=correct / resolved if resolved else 0,
        numerator=correct,
        denominator=resolved,
    )


def _one_blemish(context: MonthlyEvaluationContext) -> MetricObservation:
    max_wrong = context.parameters.get("max_wrong", 1)
    matched = context.count_events(
        lambda event: event.resolved_picks > 0
        and event.resolved_picks == event.resolved_bouts
        and event.wrong_winners <= max_wrong
    )
    return _observation(
        "monthly_event_one_blemish_count",
        context,
        value=matched,
        numerator=matched,
        denominator=len(context.events),
        details={"max_wrong": max_wrong},
    )


def _method_triathlon(context: MonthlyEvaluationContext) -> MetricObservation:
    goals = (
        ("ko", "correct_ko_wins", "ko_target"),
        ("sub", "correct_submission_wins", "sub_target"),
        ("decision", "correct_decision_wins", "decision_target"),
    )
    details: dict[str, int | float | bool | str] = {}
    met = 0
    for label, field, parameter in goals:
        actual = context.total(field)
        required = context.parameters.get(parameter, 0)
        details[label] = actual
        details[parameter] = required
        met += int(actual >= required)
    return _observation(
        "monthly_method_triathlon",
        context,
        value=met,
        numerator=met,
        denominator=len(goals),
        details=details,
    )


MONTHLY_METRIC_PROVIDERS: Mapping[str, MonthlyMetricProvider] = {
    "monthly_correct_winner_count": _sum_provider(
        "monthly_correct_winner_count", "correct_winners"
    ),
    "monthly_perfect_pick_count": _sum_provider(
        "monthly_perfect_pick_count", "perfect_picks"
    ),
    "monthly_winner_accuracy": _winner_accuracy,
    "monthly_main_event_correct_count": _event_count_provider(
        "monthly_main_event_correct_count", lambda event: event.main_event_correct
    ),
    "monthly_top_two_event_count": _event_count_provider(
        "monthly_top_two_event_count",
        lambda event: event.main_event_correct and event.co_main_correct,
    ),
    "monthly_two_plus_point_pick_count": _sum_provider(
        "monthly_two_plus_point_pick_count", "two_plus_point_picks"
    ),
    "monthly_correct_finish_method_count": _sum_provider(
        "monthly_correct_finish_method_count", "correct_finish_methods"
    ),
    "monthly_events_with_perfect_pick": _event_count_provider(
        "monthly_events_with_perfect_pick", lambda event: event.perfect_picks >= 1
    ),
    "monthly_card_mission_completion_count": _sum_provider(
        "monthly_card_mission_completion_count", "completed_card_missions"
    ),
    "monthly_hard_mission_completion_count": _sum_provider(
        "monthly_hard_mission_completion_count", "completed_hard_card_missions"
    ),
    "monthly_event_one_blemish_count": _one_blemish,
    "monthly_correct_submission_win_count": _sum_provider(
        "monthly_correct_submission_win_count", "correct_submission_wins"
    ),
    "monthly_correct_ko_win_count": _sum_provider(
        "monthly_correct_ko_win_count", "correct_ko_wins"
    ),
    "monthly_correct_decision_win_count": _sum_provider(
        "monthly_correct_decision_win_count", "correct_decision_wins"
    ),
    "monthly_pick_points": _sum_provider("monthly_pick_points", "pick_points"),
    "monthly_main_card_sweep_count": _event_count_provider(
        "monthly_main_card_sweep_count",
        lambda event: event.main_card_bouts > 0
        and event.main_card_correct == event.main_card_bouts,
    ),
    "monthly_method_triathlon": _method_triathlon,
}

MONTHLY_METRIC_NAMES: tuple[str, ...] = tuple(sorted(MONTHLY_METRIC_PROVIDERS))


def evaluate_monthly_metric(
    metric: str,
    context: MonthlyEvaluationContext,
) -> MetricObservation:
    provider = MONTHLY_METRIC_PROVIDERS.get(metric)
    if provider is None:
        raise MetricRegistryError(
            MetricRegistryErrorCode.UNKNOWN_METRIC,
            f"Unknown monthly mission metric: {metric}",
        )
    return provider(context)


def effective_target(
    definition: MonthlyMissionDefinition,
    parameters: Mapping[str, int],
) -> float | None:
    """The numeric threshold the comparator uses, in the comparator's own units."""
    key = definition.evaluation.target_parameter
    if key is None:
        return None
    value = parameters[key]
    if definition.parameter(key).kind == MonthlyParameterKind.PERCENT:
        # RATIO_GTE compares a 0..1 ratio; the Admin types a percentage.
        return value / 100
    return value


def _progress_values(
    definition: MonthlyMissionDefinition,
    observation: MetricObservation,
    target: float | None,
    parameters: Mapping[str, int],
) -> dict[str, str | int | float]:
    resolved = observation.denominator if observation.denominator is not None else 0
    accuracy = round(float(observation.value) * 100) if resolved else 0
    values: dict[str, str | int | float] = {
        "current": round(observation.value, 2)
        if isinstance(observation.value, float)
        else observation.value,
        "target": target if target is not None else "—",
        "correct": observation.numerator if observation.numerator is not None else 0,
        "resolved": resolved,
        "accuracy": accuracy,
    }
    values.update(parameters)
    for key, detail in observation.details.items():
        values[key] = detail
    return values


def resolve_monthly_observation(
    *,
    definition: MonthlyMissionDefinition,
    observation: MetricObservation,
    parameters: Mapping[str, int],
) -> MetricResolution:
    """Assess a monthly observation and render its reviewed progress copy."""
    target = effective_target(definition, parameters)
    spec = MissionEvaluationSpec(
        metric=definition.evaluation.metric,
        comparator=definition.evaluation.comparator,
        target=target,
        evaluate_on=definition.evaluation.evaluate_on,
    )
    assessment = COMPARATOR_REGISTRY.assess(spec, observation)

    if assessment.matched and observation.terminal:
        status = MetricResolutionStatus.COMPLETED
        reason = MetricResolutionReason.TERMINAL_MATCH
    elif assessment.matched and definition.evaluation.comparator == MetricComparator.GTE:
        # A cumulative count can never fall back below its threshold on its own,
        # so a reached target settles immediately instead of waiting for month end.
        status = MetricResolutionStatus.COMPLETED
        reason = MetricResolutionReason.THRESHOLD_REACHED
    elif observation.terminal:
        status = MetricResolutionStatus.FAILED
        reason = MetricResolutionReason.TERMINAL_MISS
    else:
        status = MetricResolutionStatus.PENDING
        reason = MetricResolutionReason.PENDING_RESULTS

    values = _progress_values(definition, observation, target, parameters)
    template = definition.ui.progress_template
    unknown = sorted(
        {
            name
            for _literal, name, _spec, _conv in Formatter().parse(template)
            if name is not None
        }
        - values.keys()
    )
    if unknown:
        raise ResolutionError(
            ResolutionErrorCode.UNKNOWN_PROGRESS_PLACEHOLDER,
            f"Unknown monthly progress placeholders: {', '.join(unknown)}",
        )
    fraction = (
        1.0
        if status != MetricResolutionStatus.PENDING
        else (float(observation.value) / float(target) if target else 0.0)
    )
    return MetricResolution(
        status=status,
        reason=reason,
        satisfied=assessment.matched,
        assessment=assessment,
        observation=observation,
        progress=ProgressSnapshot(
            text=template.format_map(values),
            percent=round(max(0.0, min(1.0, fraction)) * 100),
            values=values,
        ),
    )


__all__ = [
    "MONTHLY_METRIC_NAMES",
    "MONTHLY_METRIC_PROVIDERS",
    "MonthlyEvaluationContext",
    "MonthlyEventSummary",
    "effective_target",
    "evaluate_monthly_metric",
    "resolve_monthly_observation",
]
