"""Legal mission lifecycle transitions.

Service-layer guards decide *whether* a requested reopen/correction is timely.
These functions decide whether that kind of transition exists at all.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from app.modules.missions.domain.enums import (
    CardMissionState,
    CelebrationStatus,
    MissionAssignmentStatus,
    MissionTransitionReason,
    MonthlyConfigState,
    MonthlyProgressStatus,
)


@dataclass(frozen=True)
class TransitionRule:
    target: Enum
    reasons: frozenset[MissionTransitionReason]


class IllegalMissionTransition(ValueError):
    def __init__(
        self,
        entity: str,
        current: Enum,
        target: Enum,
        reason: MissionTransitionReason,
    ) -> None:
        self.entity = entity
        self.current = current
        self.target = target
        self.reason = reason
        super().__init__(
            f"Illegal {entity} transition: {current.value} -> {target.value} "
            f"for {reason.value}"
        )


ASSIGNMENT_RULES: Mapping[
    MissionAssignmentStatus,
    tuple[TransitionRule, ...],
] = {
    MissionAssignmentStatus.ACTIVE: (
        TransitionRule(
            MissionAssignmentStatus.COMPLETED,
            frozenset({MissionTransitionReason.EVALUATION}),
        ),
        TransitionRule(
            MissionAssignmentStatus.FAILED,
            frozenset({MissionTransitionReason.EVALUATION}),
        ),
        TransitionRule(
            MissionAssignmentStatus.VOID,
            frozenset(
                {
                    MissionTransitionReason.EVALUATION,
                    MissionTransitionReason.ADMIN_VOID,
                }
            ),
        ),
    ),
    MissionAssignmentStatus.COMPLETED: (
        TransitionRule(
            MissionAssignmentStatus.ACTIVE,
            frozenset({MissionTransitionReason.RESULT_CORRECTION}),
        ),
        TransitionRule(
            MissionAssignmentStatus.FAILED,
            frozenset({MissionTransitionReason.RESULT_CORRECTION}),
        ),
        TransitionRule(
            MissionAssignmentStatus.VOID,
            frozenset(
                {
                    MissionTransitionReason.RESULT_CORRECTION,
                    MissionTransitionReason.ADMIN_VOID,
                }
            ),
        ),
    ),
    MissionAssignmentStatus.FAILED: (
        TransitionRule(
            MissionAssignmentStatus.ACTIVE,
            frozenset({MissionTransitionReason.RESULT_CORRECTION}),
        ),
        TransitionRule(
            MissionAssignmentStatus.COMPLETED,
            frozenset({MissionTransitionReason.RESULT_CORRECTION}),
        ),
        TransitionRule(
            MissionAssignmentStatus.VOID,
            frozenset(
                {
                    MissionTransitionReason.RESULT_CORRECTION,
                    MissionTransitionReason.ADMIN_VOID,
                }
            ),
        ),
    ),
    MissionAssignmentStatus.VOID: (),
}

CARD_RULES: Mapping[CardMissionState, tuple[TransitionRule, ...]] = {
    CardMissionState.OPEN: (
        TransitionRule(
            CardMissionState.CLOSED,
            frozenset(
                {
                    MissionTransitionReason.ADMIN_CLOSE,
                    MissionTransitionReason.PICKS_LOCKED,
                    MissionTransitionReason.RESULTS_STARTED,
                }
            ),
        ),
        TransitionRule(
            CardMissionState.VOID,
            frozenset({MissionTransitionReason.ADMIN_VOID}),
        ),
    ),
    CardMissionState.CLOSED: (
        TransitionRule(
            CardMissionState.OPEN,
            frozenset({MissionTransitionReason.ADMIN_REOPEN}),
        ),
        TransitionRule(
            CardMissionState.VOID,
            frozenset({MissionTransitionReason.ADMIN_VOID}),
        ),
    ),
    CardMissionState.VOID: (),
}

MONTHLY_CONFIG_RULES: Mapping[MonthlyConfigState, tuple[TransitionRule, ...]] = {
    MonthlyConfigState.DRAFT: (
        TransitionRule(
            MonthlyConfigState.ACTIVE,
            frozenset({MissionTransitionReason.MONTH_START}),
        ),
    ),
    MonthlyConfigState.ACTIVE: (
        TransitionRule(
            MonthlyConfigState.CLOSED,
            frozenset(
                {
                    MissionTransitionReason.MONTH_CLOSE,
                    MissionTransitionReason.ADMIN_CLOSE,
                }
            ),
        ),
    ),
    MonthlyConfigState.CLOSED: (),
}

MONTHLY_PROGRESS_RULES: Mapping[
    MonthlyProgressStatus,
    tuple[TransitionRule, ...],
] = {
    MonthlyProgressStatus.ACTIVE: (
        TransitionRule(
            MonthlyProgressStatus.COMPLETED,
            frozenset({MissionTransitionReason.EVALUATION}),
        ),
        TransitionRule(
            MonthlyProgressStatus.FAILED,
            frozenset({MissionTransitionReason.MONTH_CLOSE}),
        ),
        TransitionRule(
            MonthlyProgressStatus.VOID,
            frozenset({MissionTransitionReason.ADMIN_VOID}),
        ),
    ),
    MonthlyProgressStatus.COMPLETED: (
        TransitionRule(
            # A correction inside a running month takes the user back below the
            # threshold, not out of the running: the month can still be earned.
            MonthlyProgressStatus.ACTIVE,
            frozenset({MissionTransitionReason.RESULT_CORRECTION}),
        ),
        TransitionRule(
            MonthlyProgressStatus.FAILED,
            frozenset({MissionTransitionReason.RESULT_CORRECTION}),
        ),
        TransitionRule(
            MonthlyProgressStatus.VOID,
            frozenset({MissionTransitionReason.ADMIN_VOID}),
        ),
    ),
    MonthlyProgressStatus.FAILED: (
        TransitionRule(
            MonthlyProgressStatus.COMPLETED,
            frozenset({MissionTransitionReason.RESULT_CORRECTION}),
        ),
        TransitionRule(
            MonthlyProgressStatus.VOID,
            frozenset({MissionTransitionReason.ADMIN_VOID}),
        ),
    ),
    MonthlyProgressStatus.VOID: (),
}

CELEBRATION_RULES: Mapping[CelebrationStatus, tuple[TransitionRule, ...]] = {
    CelebrationStatus.PENDING: (
        TransitionRule(
            CelebrationStatus.ACKNOWLEDGED,
            frozenset({MissionTransitionReason.ACKNOWLEDGEMENT}),
        ),
        TransitionRule(
            CelebrationStatus.CANCELLED,
            frozenset({MissionTransitionReason.RESULT_CORRECTION}),
        ),
    ),
    CelebrationStatus.ACKNOWLEDGED: (),
    CelebrationStatus.CANCELLED: (),
}


def _ensure_transition(
    entity: str,
    current: Enum,
    target: Enum,
    reason: MissionTransitionReason,
    rules: Mapping[Enum, tuple[TransitionRule, ...]],
) -> None:
    if current == target:
        return
    if any(
        rule.target == target and reason in rule.reasons
        for rule in rules[current]
    ):
        return
    raise IllegalMissionTransition(entity, current, target, reason)


def ensure_assignment_transition(
    current: MissionAssignmentStatus,
    target: MissionAssignmentStatus,
    reason: MissionTransitionReason,
) -> None:
    _ensure_transition("assignment", current, target, reason, ASSIGNMENT_RULES)


def ensure_card_transition(
    current: CardMissionState,
    target: CardMissionState,
    reason: MissionTransitionReason,
) -> None:
    _ensure_transition("card", current, target, reason, CARD_RULES)


def ensure_monthly_config_transition(
    current: MonthlyConfigState,
    target: MonthlyConfigState,
    reason: MissionTransitionReason,
) -> None:
    _ensure_transition(
        "monthly_config",
        current,
        target,
        reason,
        MONTHLY_CONFIG_RULES,
    )


def ensure_monthly_progress_transition(
    current: MonthlyProgressStatus,
    target: MonthlyProgressStatus,
    reason: MissionTransitionReason,
) -> None:
    _ensure_transition(
        "monthly_progress",
        current,
        target,
        reason,
        MONTHLY_PROGRESS_RULES,
    )


def ensure_celebration_transition(
    current: CelebrationStatus,
    target: CelebrationStatus,
    reason: MissionTransitionReason,
) -> None:
    _ensure_transition(
        "celebration",
        current,
        target,
        reason,
        CELEBRATION_RULES,
    )
