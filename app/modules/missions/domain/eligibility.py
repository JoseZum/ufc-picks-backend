"""Frozen-card eligibility and offer-overlap policies."""

from __future__ import annotations

from dataclasses import dataclass

from app.modules.missions.domain.definitions import (
    CardCapability,
    MissionDefinition,
)
from app.modules.missions.domain.enums import StringEnum


class EligibilityFailureCode(StringEnum):
    MISSING_CAPABILITY = "MISSING_CAPABILITY"
    INSUFFICIENT_ELIGIBLE_BOUTS = "INSUFFICIENT_ELIGIBLE_BOUTS"
    INSUFFICIENT_MAIN_CARD_BOUTS = "INSUFFICIENT_MAIN_CARD_BOUTS"
    INSUFFICIENT_PRELIM_BOUTS = "INSUFFICIENT_PRELIM_BOUTS"
    INSUFFICIENT_TITLE_BOUTS = "INSUFFICIENT_TITLE_BOUTS"


@dataclass(frozen=True)
class FrozenCardFacts:
    event_id: int
    card_revision: int
    eligible_bouts: int
    main_card_bouts: int
    prelim_bouts: int
    title_bouts: int
    capabilities: frozenset[CardCapability]

    def __post_init__(self) -> None:
        counts = (
            self.card_revision,
            self.eligible_bouts,
            self.main_card_bouts,
            self.prelim_bouts,
            self.title_bouts,
        )
        if self.event_id <= 0 or any(value < 0 for value in counts):
            raise ValueError("card facts require a positive event id and non-negative counts")
        if self.main_card_bouts > self.eligible_bouts:
            raise ValueError("main-card bouts cannot exceed eligible bouts")
        if self.prelim_bouts > self.eligible_bouts:
            raise ValueError("prelim bouts cannot exceed eligible bouts")
        if self.title_bouts > self.eligible_bouts:
            raise ValueError("title bouts cannot exceed eligible bouts")


@dataclass(frozen=True)
class EligibilityFailure:
    code: EligibilityFailureCode
    field: str
    required: int | str
    actual: int | str


@dataclass(frozen=True)
class EligibilityDecision:
    mission_id: str
    card_revision: int
    failures: tuple[EligibilityFailure, ...]

    @property
    def eligible(self) -> bool:
        return not self.failures


@dataclass(frozen=True)
class OverlapDecision:
    left_mission_id: str
    right_mission_id: str
    shared_tags: frozenset[str]
    conflicts: bool


@dataclass(frozen=True)
class MissionOverlapPolicy:
    """Two offers conflict when they share more than the allowed tag budget."""

    max_shared_tags: int = 1

    def __post_init__(self) -> None:
        if self.max_shared_tags < 0:
            raise ValueError("max_shared_tags cannot be negative")

    def compare(
        self,
        left: MissionDefinition,
        right: MissionDefinition,
    ) -> OverlapDecision:
        shared = left.overlap_tags & right.overlap_tags
        return OverlapDecision(
            left_mission_id=left.mission_id,
            right_mission_id=right.mission_id,
            shared_tags=shared,
            conflicts=(
                left.mission_id == right.mission_id
                or len(shared) > self.max_shared_tags
            ),
        )


def evaluate_definition_eligibility(
    definition: MissionDefinition,
    card: FrozenCardFacts,
) -> EligibilityDecision:
    requirement = definition.eligibility
    failures: list[EligibilityFailure] = []

    for capability in sorted(
        requirement.capabilities - card.capabilities,
        key=lambda value: value.value,
    ):
        failures.append(
            EligibilityFailure(
                code=EligibilityFailureCode.MISSING_CAPABILITY,
                field="capabilities",
                required=capability.value,
                actual="MISSING",
            )
        )

    count_rules = (
        (
            "eligible_bouts",
            requirement.min_eligible_bouts,
            card.eligible_bouts,
            EligibilityFailureCode.INSUFFICIENT_ELIGIBLE_BOUTS,
        ),
        (
            "main_card_bouts",
            requirement.min_main_card_bouts,
            card.main_card_bouts,
            EligibilityFailureCode.INSUFFICIENT_MAIN_CARD_BOUTS,
        ),
        (
            "prelim_bouts",
            requirement.min_prelim_bouts,
            card.prelim_bouts,
            EligibilityFailureCode.INSUFFICIENT_PRELIM_BOUTS,
        ),
        (
            "title_bouts",
            requirement.min_title_bouts,
            card.title_bouts,
            EligibilityFailureCode.INSUFFICIENT_TITLE_BOUTS,
        ),
    )
    for field, required, actual, code in count_rules:
        if actual < required:
            failures.append(
                EligibilityFailure(
                    code=code,
                    field=field,
                    required=required,
                    actual=actual,
                )
            )

    return EligibilityDecision(
        mission_id=definition.mission_id,
        card_revision=card.card_revision,
        failures=tuple(failures),
    )


def eligible_definitions(
    definitions: tuple[MissionDefinition, ...],
    card: FrozenCardFacts,
) -> tuple[MissionDefinition, ...]:
    return tuple(
        definition
        for definition in definitions
        if evaluate_definition_eligibility(definition, card).eligible
    )
