"""Explicit registry for the rare mission rules that metrics cannot express."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from app.modules.missions.domain.catalog import MissionCatalog
from app.modules.missions.domain.definitions import (
    MissionDefinition,
    MissionEvaluationSpec,
)
from app.modules.missions.domain.enums import StringEnum
from app.modules.missions.domain.evaluation import MissionEvaluationContext
from app.modules.missions.domain.metrics import (
    CARD_METRIC_REGISTRY,
    MetricObservation,
    MetricRegistry,
)


class ExceptionalEvaluatorErrorCode(StringEnum):
    DUPLICATE_KEY = "DUPLICATE_KEY"
    INVALID_REGISTRATION = "INVALID_REGISTRATION"
    UNKNOWN_KEY = "UNKNOWN_KEY"
    MISSION_NOT_ALLOWED = "MISSION_NOT_ALLOWED"
    INVALID_OBSERVATION = "INVALID_OBSERVATION"
    UNREGISTERED_METRIC = "UNREGISTERED_METRIC"


class ExceptionalEvaluatorError(ValueError):
    def __init__(
        self,
        code: ExceptionalEvaluatorErrorCode,
        message: str,
    ) -> None:
        self.code = code
        super().__init__(message)


ExceptionalEvaluator = Callable[
    [MissionEvaluationSpec, MissionEvaluationContext],
    MetricObservation,
]


@dataclass(frozen=True)
class ExceptionalEvaluatorEntry:
    key: str
    mission_ids: frozenset[str]
    justification: str
    provider: ExceptionalEvaluator


@dataclass(frozen=True)
class EvaluatorCoverageReport:
    declarative_mission_ids: tuple[str, ...]
    exceptional_mission_ids: tuple[str, ...]

    @property
    def total_count(self) -> int:
        return len(self.declarative_mission_ids) + len(self.exceptional_mission_ids)


class ExceptionalEvaluatorRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, ExceptionalEvaluatorEntry] = {}

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._entries))

    def register(
        self,
        key: str,
        *,
        mission_ids: frozenset[str],
        justification: str,
        provider: ExceptionalEvaluator,
    ) -> None:
        if key in self._entries:
            raise ExceptionalEvaluatorError(
                ExceptionalEvaluatorErrorCode.DUPLICATE_KEY,
                f"Exceptional evaluator already registered: {key}",
            )
        if (
            not re.fullmatch(r"[a-z][a-z0-9_]*", key)
            or not mission_ids
            or not justification.strip()
        ):
            raise ExceptionalEvaluatorError(
                ExceptionalEvaluatorErrorCode.INVALID_REGISTRATION,
                "Exceptional evaluators require a stable key, mission scope and justification",
            )
        self._entries[key] = ExceptionalEvaluatorEntry(
            key=key,
            mission_ids=frozenset(mission_ids),
            justification=justification.strip(),
            provider=provider,
        )

    def get(self, key: str) -> ExceptionalEvaluatorEntry:
        try:
            return self._entries[key]
        except KeyError as exc:
            raise ExceptionalEvaluatorError(
                ExceptionalEvaluatorErrorCode.UNKNOWN_KEY,
                f"Unknown exceptional evaluator: {key}",
            ) from exc

    def require_definition(
        self,
        definition: MissionDefinition,
    ) -> ExceptionalEvaluatorEntry:
        key = definition.evaluation.custom_evaluator_key
        if key is None:
            raise ExceptionalEvaluatorError(
                ExceptionalEvaluatorErrorCode.UNKNOWN_KEY,
                f"Mission {definition.mission_id} does not declare an exceptional evaluator",
            )
        entry = self.get(key)
        if definition.mission_id not in entry.mission_ids:
            raise ExceptionalEvaluatorError(
                ExceptionalEvaluatorErrorCode.MISSION_NOT_ALLOWED,
                f"Mission {definition.mission_id} is outside evaluator {key}'s explicit scope",
            )
        return entry

    def evaluate(
        self,
        definition: MissionDefinition,
        context: MissionEvaluationContext,
    ) -> MetricObservation:
        entry = self.require_definition(definition)
        observation = entry.provider(definition.evaluation, context)
        if observation.metric != definition.evaluation.metric:
            raise ExceptionalEvaluatorError(
                ExceptionalEvaluatorErrorCode.INVALID_OBSERVATION,
                "Exceptional evaluator returned an observation for the wrong metric",
            )
        return observation


EXCEPTIONAL_EVALUATOR_REGISTRY = ExceptionalEvaluatorRegistry()


def validate_catalog_evaluator_coverage(
    catalog: MissionCatalog,
    *,
    metric_registry: MetricRegistry = CARD_METRIC_REGISTRY,
    exceptional_registry: ExceptionalEvaluatorRegistry = (EXCEPTIONAL_EVALUATOR_REGISTRY),
) -> EvaluatorCoverageReport:
    declarative: list[str] = []
    exceptional: list[str] = []
    for definition in catalog:
        if definition.evaluation.custom_evaluator_key is None:
            if definition.evaluation.metric not in metric_registry.names:
                raise ExceptionalEvaluatorError(
                    ExceptionalEvaluatorErrorCode.UNREGISTERED_METRIC,
                    f"Mission {definition.mission_id} uses unknown metric "
                    f"{definition.evaluation.metric}",
                )
            declarative.append(definition.mission_id)
            continue
        exceptional_registry.require_definition(definition)
        exceptional.append(definition.mission_id)
    return EvaluatorCoverageReport(
        declarative_mission_ids=tuple(sorted(declarative)),
        exceptional_mission_ids=tuple(sorted(exceptional)),
    )


__all__ = [
    "EXCEPTIONAL_EVALUATOR_REGISTRY",
    "EvaluatorCoverageReport",
    "ExceptionalEvaluator",
    "ExceptionalEvaluatorEntry",
    "ExceptionalEvaluatorError",
    "ExceptionalEvaluatorErrorCode",
    "ExceptionalEvaluatorRegistry",
    "validate_catalog_evaluator_coverage",
]
