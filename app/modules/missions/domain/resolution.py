"""Comparator resolution and safe UI progress formatting."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from string import Formatter
from typing import TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from app.modules.missions.domain.definitions import (
    EvaluationTargetSource,
    MetricComparator,
    MissionEvaluationSpec,
    ScalarParameter,
)
from app.modules.missions.domain.enums import StringEnum
from app.modules.missions.domain.metrics import MetricObservation, MetricScalar


class ResolutionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MetricResolutionStatus(StringEnum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    VOID = "VOID"


class MetricResolutionReason(StringEnum):
    PENDING_RESULTS = "PENDING_RESULTS"
    THRESHOLD_REACHED = "THRESHOLD_REACHED"
    TERMINAL_MATCH = "TERMINAL_MATCH"
    TERMINAL_MISS = "TERMINAL_MISS"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"
    INSUFFICIENT_TARGETS = "INSUFFICIENT_TARGETS"
    OBSERVATION_VOID = "OBSERVATION_VOID"


class ComparatorAssessment(ResolutionModel):
    matched: bool
    requires_terminal_success: bool
    irreversible_failure: bool
    effective_target: MetricScalar | None = None
    required_items: frozenset[str] = Field(default_factory=frozenset)


class ProgressSnapshot(ResolutionModel):
    text: str
    percent: int = Field(ge=0, le=100)
    values: dict[str, str | int | float]


class MetricResolution(ResolutionModel):
    status: MetricResolutionStatus
    reason: MetricResolutionReason
    satisfied: bool
    assessment: ComparatorAssessment
    observation: MetricObservation
    progress: ProgressSnapshot


class ResolutionErrorCode(StringEnum):
    DUPLICATE_COMPARATOR = "DUPLICATE_COMPARATOR"
    INVALID_COMPARATOR_INPUT = "INVALID_COMPARATOR_INPUT"
    INVALID_PARAMETERS = "INVALID_PARAMETERS"
    UNKNOWN_PROGRESS_PLACEHOLDER = "UNKNOWN_PROGRESS_PLACEHOLDER"


class ResolutionError(ValueError):
    def __init__(self, code: ResolutionErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


ComparatorProvider: TypeAlias = Callable[
    [MetricObservation, MetricScalar | None, Mapping[str, ScalarParameter]],
    ComparatorAssessment,
]
ProgressProvider: TypeAlias = Callable[
    [MetricObservation, ComparatorAssessment],
    float,
]


class ComparatorRegistry:
    def __init__(self) -> None:
        self._providers: dict[MetricComparator, ComparatorProvider] = {}

    def register(
        self,
        comparator: MetricComparator,
        provider: ComparatorProvider,
    ) -> None:
        if comparator in self._providers:
            raise ResolutionError(
                ResolutionErrorCode.DUPLICATE_COMPARATOR,
                f"Comparator already registered: {comparator.value}",
            )
        self._providers[comparator] = provider

    @property
    def comparators(self) -> tuple[MetricComparator, ...]:
        return tuple(sorted(self._providers, key=lambda item: item.value))

    def assess(
        self,
        spec: MissionEvaluationSpec,
        observation: MetricObservation,
    ) -> ComparatorAssessment:
        provider = self._providers[spec.comparator]
        target = observation.target_override
        if (
            spec.target_source == EvaluationTargetSource.OBSERVATION_OVERRIDE
            and target is None
        ):
            raise ResolutionError(
                ResolutionErrorCode.INVALID_COMPARATOR_INPUT,
                "Evaluation requires a frozen selection target override",
            )
        if target is None:
            target = spec.target
        return provider(observation, target, spec.parameters)


class ProgressFormatterRegistry:
    def __init__(self) -> None:
        self._providers: dict[MetricComparator, ProgressProvider] = {}

    def register(
        self,
        comparator: MetricComparator,
        provider: ProgressProvider,
    ) -> None:
        if comparator in self._providers:
            raise ResolutionError(
                ResolutionErrorCode.DUPLICATE_COMPARATOR,
                f"Progress formatter already registered: {comparator.value}",
            )
        self._providers[comparator] = provider

    def format(
        self,
        *,
        comparator: MetricComparator,
        template: str,
        observation: MetricObservation,
        assessment: ComparatorAssessment,
        status: MetricResolutionStatus,
    ) -> ProgressSnapshot:
        values = _progress_values(observation, assessment, status)
        fields = {
            field_name
            for _literal, field_name, _format_spec, _conversion in Formatter().parse(template)
            if field_name is not None
        }
        unknown = sorted(fields - values.keys())
        if unknown:
            raise ResolutionError(
                ResolutionErrorCode.UNKNOWN_PROGRESS_PLACEHOLDER,
                f"Unknown progress placeholders: {', '.join(unknown)}",
            )
        try:
            text = template.format_map(values)
        except (KeyError, ValueError) as exc:
            raise ResolutionError(
                ResolutionErrorCode.UNKNOWN_PROGRESS_PLACEHOLDER,
                "Progress template could not be rendered",
            ) from exc
        fraction = self._providers[comparator](observation, assessment)
        if status != MetricResolutionStatus.PENDING:
            fraction = 1
        return ProgressSnapshot(
            text=text,
            percent=round(max(0, min(1, fraction)) * 100),
            values=values,
        )


def _numeric_target(
    target: MetricScalar | None,
    comparator: MetricComparator,
) -> MetricScalar:
    if target is None or isinstance(target, bool):
        raise ResolutionError(
            ResolutionErrorCode.INVALID_COMPARATOR_INPUT,
            f"{comparator.value} requires a numeric target",
        )
    return target


def _numeric_assessment(
    comparator: MetricComparator,
    observation: MetricObservation,
    target: MetricScalar | None,
    _parameters: Mapping[str, ScalarParameter],
) -> ComparatorAssessment:
    expected = _numeric_target(target, comparator)
    if comparator == MetricComparator.EQ:
        matched = observation.value == expected
        irreversible = observation.value > expected
        requires_terminal = True
    elif comparator == MetricComparator.GTE:
        matched = observation.value >= expected
        irreversible = False
        requires_terminal = False
    elif comparator == MetricComparator.LTE:
        matched = observation.value <= expected
        irreversible = observation.value > expected
        requires_terminal = True
    else:
        matched = observation.value >= expected
        irreversible = False
        requires_terminal = True
    return ComparatorAssessment(
        matched=matched,
        requires_terminal_success=requires_terminal,
        irreversible_failure=irreversible,
        effective_target=expected,
    )


def _other_assessment(
    comparator: MetricComparator,
    observation: MetricObservation,
    _target: MetricScalar | None,
    _parameters: Mapping[str, ScalarParameter],
) -> ComparatorAssessment:
    if observation.other_value is None:
        raise ResolutionError(
            ResolutionErrorCode.INVALID_COMPARATOR_INPUT,
            f"{comparator.value} requires other_value",
        )
    matched = (
        observation.value > observation.other_value
        if comparator == MetricComparator.GT_OTHER
        else observation.value >= observation.other_value
    )
    return ComparatorAssessment(
        matched=matched,
        requires_terminal_success=True,
        irreversible_failure=False,
    )


def _all_assessment(
    observation: MetricObservation,
    _target: MetricScalar | None,
    _parameters: Mapping[str, ScalarParameter],
) -> ComparatorAssessment:
    if observation.numerator is None or observation.denominator is None:
        raise ResolutionError(
            ResolutionErrorCode.INVALID_COMPARATOR_INPUT,
            "ALL requires numerator and denominator",
        )
    return ComparatorAssessment(
        matched=(
            observation.denominator > 0
            and observation.numerator == observation.denominator
        ),
        requires_terminal_success=True,
        irreversible_failure=False,
    )


def _required_items(parameters: Mapping[str, ScalarParameter]) -> frozenset[str]:
    raw = parameters.get("required_items")
    if not isinstance(raw, str):
        raise ResolutionError(
            ResolutionErrorCode.INVALID_PARAMETERS,
            "CONTAINS_ALL requires comma-separated required_items",
        )
    values = frozenset(item.strip() for item in raw.split(",") if item.strip())
    if not values:
        raise ResolutionError(
            ResolutionErrorCode.INVALID_PARAMETERS,
            "required_items cannot be empty",
        )
    return values


def _contains_all_assessment(
    observation: MetricObservation,
    _target: MetricScalar | None,
    parameters: Mapping[str, ScalarParameter],
) -> ComparatorAssessment:
    required = _required_items(parameters)
    return ComparatorAssessment(
        matched=required <= observation.matched_items,
        requires_terminal_success=False,
        irreversible_failure=False,
        required_items=required,
    )


def _register_comparators() -> ComparatorRegistry:
    registry = ComparatorRegistry()
    registry.register(
        MetricComparator.EQ,
        lambda observation, target, parameters: _numeric_assessment(
            MetricComparator.EQ, observation, target, parameters
        ),
    )
    registry.register(
        MetricComparator.GTE,
        lambda observation, target, parameters: _numeric_assessment(
            MetricComparator.GTE, observation, target, parameters
        ),
    )
    registry.register(
        MetricComparator.LTE,
        lambda observation, target, parameters: _numeric_assessment(
            MetricComparator.LTE, observation, target, parameters
        ),
    )
    registry.register(
        MetricComparator.RATIO_GTE,
        lambda observation, target, parameters: _numeric_assessment(
            MetricComparator.RATIO_GTE, observation, target, parameters
        ),
    )
    registry.register(
        MetricComparator.GT_OTHER,
        lambda observation, target, parameters: _other_assessment(
            MetricComparator.GT_OTHER, observation, target, parameters
        ),
    )
    registry.register(
        MetricComparator.GTE_OTHER,
        lambda observation, target, parameters: _other_assessment(
            MetricComparator.GTE_OTHER, observation, target, parameters
        ),
    )
    registry.register(MetricComparator.ALL, _all_assessment)
    registry.register(MetricComparator.CONTAINS_ALL, _contains_all_assessment)
    return registry


def _ratio_progress(
    observation: MetricObservation,
    assessment: ComparatorAssessment,
) -> float:
    target = assessment.effective_target
    if target is None or target <= 0:
        return _resolved_progress(observation, assessment)
    return float(observation.value) / float(target)


def _resolved_progress(
    observation: MetricObservation,
    _assessment: ComparatorAssessment,
) -> float:
    return (
        observation.resolved_count / observation.total_count
        if observation.total_count
        else 0
    )


def _all_progress(
    observation: MetricObservation,
    _assessment: ComparatorAssessment,
) -> float:
    return (
        observation.numerator / observation.denominator
        if observation.denominator
        else 0
    )


def _contains_progress(
    observation: MetricObservation,
    assessment: ComparatorAssessment,
) -> float:
    return (
        len(observation.matched_items & assessment.required_items)
        / len(assessment.required_items)
        if assessment.required_items
        else 0
    )


def _register_progress_formatters() -> ProgressFormatterRegistry:
    registry = ProgressFormatterRegistry()
    for comparator in {
        MetricComparator.EQ,
        MetricComparator.GTE,
        MetricComparator.RATIO_GTE,
    }:
        registry.register(comparator, _ratio_progress)
    for comparator in {
        MetricComparator.LTE,
        MetricComparator.GT_OTHER,
        MetricComparator.GTE_OTHER,
    }:
        registry.register(comparator, _resolved_progress)
    registry.register(MetricComparator.ALL, _all_progress)
    registry.register(MetricComparator.CONTAINS_ALL, _contains_progress)
    return registry


def _integer_parameter(
    parameters: Mapping[str, ScalarParameter],
    name: str,
    *,
    default: int = 0,
) -> int:
    value = parameters.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ResolutionError(
            ResolutionErrorCode.INVALID_PARAMETERS,
            f"{name} must be a non-negative integer",
        )
    return value


def _boolean_parameter(
    parameters: Mapping[str, ScalarParameter],
    name: str,
    *,
    default: bool = False,
) -> bool:
    value = parameters.get(name, default)
    if not isinstance(value, bool):
        raise ResolutionError(
            ResolutionErrorCode.INVALID_PARAMETERS,
            f"{name} must be boolean",
        )
    return value


def _insufficient_status(
    parameters: Mapping[str, ScalarParameter],
    name: str,
    *,
    default: str,
) -> MetricResolutionStatus:
    value = parameters.get(name, default)
    try:
        status = MetricResolutionStatus(str(value))
    except ValueError as exc:
        raise ResolutionError(
            ResolutionErrorCode.INVALID_PARAMETERS,
            f"{name} must be FAILED or VOID",
        ) from exc
    if status not in {MetricResolutionStatus.FAILED, MetricResolutionStatus.VOID}:
        raise ResolutionError(
            ResolutionErrorCode.INVALID_PARAMETERS,
            f"{name} must be FAILED or VOID",
        )
    return status


def _display_number(value: MetricScalar | None) -> str | int | float:
    if value is None:
        return "—"
    if isinstance(value, float):
        return round(value, 2)
    return value


def _progress_values(
    observation: MetricObservation,
    assessment: ComparatorAssessment,
    status: MetricResolutionStatus,
) -> dict[str, str | int | float]:
    target = assessment.effective_target
    details = observation.details
    finishes = details.get(
        "finishes",
        observation.value if "finish" in observation.metric else 0,
    )
    decisions = details.get(
        "decisions",
        observation.value if "decision_count" in observation.metric else 0,
    )
    correct = observation.numerator
    if correct is None and observation.metric == "wrong_winner_count":
        correct = max(0, observation.total_count - int(observation.value))
    if correct is None:
        correct = int(observation.value) if float(observation.value).is_integer() else 0
    result_label = {
        MetricResolutionStatus.PENDING: "PENDING",
        MetricResolutionStatus.COMPLETED: "SUCCESS",
        MetricResolutionStatus.FAILED: "MISS",
        MetricResolutionStatus.VOID: "VOID",
    }[status]
    percentage = round(float(observation.value) * 100)
    return {
        "current": _display_number(observation.value),
        "target": _display_number(target),
        "displayed_target": _display_number(target),
        "correct": correct,
        "eligible_card_bouts": observation.total_count,
        "main_card_bouts": observation.total_count,
        "prelim_bouts": observation.total_count,
        "resolved": observation.sample_size,
        "accuracy": percentage,
        "finishes": _display_number(finishes),
        "decisions": _display_number(decisions),
        "eligible": (
            observation.denominator
            if observation.denominator is not None
            else observation.total_count
        ),
        "rate": percentage,
        "matched": (
            observation.numerator
            if observation.numerator is not None
            else int(observation.value)
        ),
        "methods": len(observation.matched_items),
        "result": result_label,
        "selected_count": _display_number(observation.value),
        "other_count": _display_number(observation.other_value),
        "wrong": _display_number(observation.value),
        "rank": _display_number(details.get("rank")),
        "actual": _display_number(details.get("actual_round")),
    }


COMPARATOR_REGISTRY = _register_comparators()
PROGRESS_FORMATTER_REGISTRY = _register_progress_formatters()


def resolve_metric_observation(
    *,
    spec: MissionEvaluationSpec,
    observation: MetricObservation,
    progress_template: str,
    comparator_registry: ComparatorRegistry = COMPARATOR_REGISTRY,
    progress_registry: ProgressFormatterRegistry = PROGRESS_FORMATTER_REGISTRY,
) -> MetricResolution:
    assessment = comparator_registry.assess(spec, observation)

    if observation.void_reason is not None:
        status = MetricResolutionStatus.VOID
        reason = MetricResolutionReason.OBSERVATION_VOID
    else:
        min_sample = _integer_parameter(spec.parameters, "min_sample_size")
        min_targets = _integer_parameter(spec.parameters, "min_total_count")
        insufficient_reason = None
        insufficient_status_parameter = "insufficient_sample_status"
        if observation.total_count < min_targets:
            insufficient_reason = MetricResolutionReason.INSUFFICIENT_TARGETS
            insufficient_status_parameter = "insufficient_targets_status"
        elif observation.sample_size < min_sample:
            insufficient_reason = MetricResolutionReason.INSUFFICIENT_SAMPLE

        if insufficient_reason is not None:
            if observation.terminal:
                status = _insufficient_status(
                    spec.parameters,
                    insufficient_status_parameter,
                    default=(
                        "VOID"
                        if insufficient_status_parameter
                        == "insufficient_targets_status"
                        else "FAILED"
                    ),
                )
                reason = insufficient_reason
            else:
                status = MetricResolutionStatus.PENDING
                reason = MetricResolutionReason.PENDING_RESULTS
        else:
            wait_for_terminal = assessment.requires_terminal_success or _boolean_parameter(
                spec.parameters,
                "resolve_only_when_terminal",
            )
            if assessment.matched and (observation.terminal or not wait_for_terminal):
                status = MetricResolutionStatus.COMPLETED
                reason = (
                    MetricResolutionReason.TERMINAL_MATCH
                    if observation.terminal
                    else MetricResolutionReason.THRESHOLD_REACHED
                )
            elif assessment.irreversible_failure:
                status = MetricResolutionStatus.FAILED
                reason = MetricResolutionReason.LIMIT_EXCEEDED
            elif observation.terminal:
                status = MetricResolutionStatus.FAILED
                reason = MetricResolutionReason.TERMINAL_MISS
            else:
                status = MetricResolutionStatus.PENDING
                reason = MetricResolutionReason.PENDING_RESULTS

    progress = progress_registry.format(
        comparator=spec.comparator,
        template=progress_template,
        observation=observation,
        assessment=assessment,
        status=status,
    )
    return MetricResolution(
        status=status,
        reason=reason,
        satisfied=assessment.matched,
        assessment=assessment,
        observation=observation,
        progress=progress,
    )
