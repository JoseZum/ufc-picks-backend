import itertools

import pytest

from app.modules.missions.domain import (
    CardMissionState,
    CelebrationStatus,
    IllegalMissionTransition,
    MissionAssignmentStatus,
    MissionDifficulty,
    MissionInteractionType,
    MissionTransitionReason,
    MonthlyConfigState,
    MonthlyProgressStatus,
    PickEffect,
    ensure_assignment_transition,
    ensure_card_transition,
    ensure_celebration_transition,
    ensure_monthly_config_transition,
    ensure_monthly_progress_transition,
)


def test_persisted_enum_values_are_stable():
    assert [item.value for item in MissionDifficulty] == ["EASY", "MEDIUM", "HARD"]
    assert [item.value for item in MissionInteractionType] == [
        "AUTO",
        "TARGET_FIGHTER",
        "TARGET_FIGHT",
        "COMBO_BUILDER",
        "CARD_PROP",
    ]
    assert [item.value for item in PickEffect] == [
        "NONE",
        "UPSERT_ONE",
        "UPSERT_MANY",
    ]


@pytest.mark.parametrize(
    ("current", "target", "reason"),
    [
        (MissionAssignmentStatus.ACTIVE, MissionAssignmentStatus.COMPLETED, MissionTransitionReason.EVALUATION),
        (MissionAssignmentStatus.ACTIVE, MissionAssignmentStatus.FAILED, MissionTransitionReason.EVALUATION),
        (MissionAssignmentStatus.ACTIVE, MissionAssignmentStatus.VOID, MissionTransitionReason.ADMIN_VOID),
        (MissionAssignmentStatus.COMPLETED, MissionAssignmentStatus.FAILED, MissionTransitionReason.RESULT_CORRECTION),
        (MissionAssignmentStatus.FAILED, MissionAssignmentStatus.COMPLETED, MissionTransitionReason.RESULT_CORRECTION),
        (MissionAssignmentStatus.COMPLETED, MissionAssignmentStatus.ACTIVE, MissionTransitionReason.RESULT_CORRECTION),
        (MissionAssignmentStatus.FAILED, MissionAssignmentStatus.ACTIVE, MissionTransitionReason.RESULT_CORRECTION),
        (MissionAssignmentStatus.COMPLETED, MissionAssignmentStatus.VOID, MissionTransitionReason.RESULT_CORRECTION),
    ],
)
def test_legal_assignment_transitions(current, target, reason):
    ensure_assignment_transition(current, target, reason)


@pytest.mark.parametrize(
    ("current", "target", "reason"),
    [
        (MissionAssignmentStatus.VOID, MissionAssignmentStatus.ACTIVE, MissionTransitionReason.RESULT_CORRECTION),
        (MissionAssignmentStatus.COMPLETED, MissionAssignmentStatus.FAILED, MissionTransitionReason.EVALUATION),
        (MissionAssignmentStatus.FAILED, MissionAssignmentStatus.ACTIVE, MissionTransitionReason.EVALUATION),
    ],
)
def test_illegal_assignment_transitions_are_descriptive(current, target, reason):
    with pytest.raises(IllegalMissionTransition) as raised:
        ensure_assignment_transition(current, target, reason)
    assert raised.value.current == current
    assert raised.value.target == target
    assert raised.value.reason == reason


@pytest.mark.parametrize(
    ("current", "target", "reason"),
    [
        (CardMissionState.OPEN, CardMissionState.CLOSED, MissionTransitionReason.ADMIN_CLOSE),
        (CardMissionState.OPEN, CardMissionState.CLOSED, MissionTransitionReason.PICKS_LOCKED),
        (CardMissionState.OPEN, CardMissionState.CLOSED, MissionTransitionReason.RESULTS_STARTED),
        (CardMissionState.CLOSED, CardMissionState.OPEN, MissionTransitionReason.ADMIN_REOPEN),
        (CardMissionState.CLOSED, CardMissionState.VOID, MissionTransitionReason.ADMIN_VOID),
    ],
)
def test_legal_card_transitions(current, target, reason):
    ensure_card_transition(current, target, reason)


def test_card_reopen_requires_explicit_admin_reason():
    with pytest.raises(IllegalMissionTransition):
        ensure_card_transition(
            CardMissionState.CLOSED,
            CardMissionState.OPEN,
            MissionTransitionReason.EVALUATION,
        )


def test_monthly_config_is_one_way():
    ensure_monthly_config_transition(
        MonthlyConfigState.DRAFT,
        MonthlyConfigState.ACTIVE,
        MissionTransitionReason.MONTH_START,
    )
    ensure_monthly_config_transition(
        MonthlyConfigState.ACTIVE,
        MonthlyConfigState.CLOSED,
        MissionTransitionReason.ADMIN_CLOSE,
    )
    with pytest.raises(IllegalMissionTransition):
        ensure_monthly_config_transition(
            MonthlyConfigState.CLOSED,
            MonthlyConfigState.ACTIVE,
            MissionTransitionReason.ADMIN_REOPEN,
        )


def test_monthly_progress_supports_result_correction_but_void_is_terminal():
    ensure_monthly_progress_transition(
        MonthlyProgressStatus.COMPLETED,
        MonthlyProgressStatus.FAILED,
        MissionTransitionReason.RESULT_CORRECTION,
    )
    # A mid-month correction returns the user to ACTIVE so they can earn it back.
    ensure_monthly_progress_transition(
        MonthlyProgressStatus.COMPLETED,
        MonthlyProgressStatus.ACTIVE,
        MissionTransitionReason.RESULT_CORRECTION,
    )
    with pytest.raises(IllegalMissionTransition):
        ensure_monthly_progress_transition(
            MonthlyProgressStatus.COMPLETED,
            MonthlyProgressStatus.ACTIVE,
            MissionTransitionReason.EVALUATION,
        )
    with pytest.raises(IllegalMissionTransition):
        ensure_monthly_progress_transition(
            MonthlyProgressStatus.VOID,
            MonthlyProgressStatus.ACTIVE,
            MissionTransitionReason.RESULT_CORRECTION,
        )


def test_celebration_can_only_be_acknowledged_once():
    ensure_celebration_transition(
        CelebrationStatus.PENDING,
        CelebrationStatus.ACKNOWLEDGED,
        MissionTransitionReason.ACKNOWLEDGEMENT,
    )
    with pytest.raises(IllegalMissionTransition):
        ensure_celebration_transition(
            CelebrationStatus.ACKNOWLEDGED,
            CelebrationStatus.PENDING,
            MissionTransitionReason.ACKNOWLEDGEMENT,
        )


@pytest.mark.parametrize(
    ("status_type", "ensure"),
    [
        (MissionAssignmentStatus, ensure_assignment_transition),
        (CardMissionState, ensure_card_transition),
        (MonthlyConfigState, ensure_monthly_config_transition),
        (MonthlyProgressStatus, ensure_monthly_progress_transition),
        (CelebrationStatus, ensure_celebration_transition),
    ],
)
def test_every_self_transition_is_idempotent(status_type, ensure):
    for status, reason in itertools.product(status_type, MissionTransitionReason):
        ensure(status, status, reason)


@pytest.mark.parametrize(
    ("status_type", "ensure", "legal"),
    [
        (
            MissionAssignmentStatus,
            ensure_assignment_transition,
            {
                (MissionAssignmentStatus.ACTIVE, MissionAssignmentStatus.COMPLETED, MissionTransitionReason.EVALUATION),
                (MissionAssignmentStatus.ACTIVE, MissionAssignmentStatus.FAILED, MissionTransitionReason.EVALUATION),
                (MissionAssignmentStatus.ACTIVE, MissionAssignmentStatus.VOID, MissionTransitionReason.EVALUATION),
                (MissionAssignmentStatus.ACTIVE, MissionAssignmentStatus.VOID, MissionTransitionReason.ADMIN_VOID),
                (MissionAssignmentStatus.COMPLETED, MissionAssignmentStatus.FAILED, MissionTransitionReason.RESULT_CORRECTION),
                (MissionAssignmentStatus.COMPLETED, MissionAssignmentStatus.ACTIVE, MissionTransitionReason.RESULT_CORRECTION),
                (MissionAssignmentStatus.COMPLETED, MissionAssignmentStatus.VOID, MissionTransitionReason.RESULT_CORRECTION),
                (MissionAssignmentStatus.COMPLETED, MissionAssignmentStatus.VOID, MissionTransitionReason.ADMIN_VOID),
                (MissionAssignmentStatus.FAILED, MissionAssignmentStatus.COMPLETED, MissionTransitionReason.RESULT_CORRECTION),
                (MissionAssignmentStatus.FAILED, MissionAssignmentStatus.ACTIVE, MissionTransitionReason.RESULT_CORRECTION),
                (MissionAssignmentStatus.FAILED, MissionAssignmentStatus.VOID, MissionTransitionReason.RESULT_CORRECTION),
                (MissionAssignmentStatus.FAILED, MissionAssignmentStatus.VOID, MissionTransitionReason.ADMIN_VOID),
            },
        ),
        (
            CardMissionState,
            ensure_card_transition,
            {
                (CardMissionState.OPEN, CardMissionState.CLOSED, MissionTransitionReason.ADMIN_CLOSE),
                (CardMissionState.OPEN, CardMissionState.CLOSED, MissionTransitionReason.PICKS_LOCKED),
                (CardMissionState.OPEN, CardMissionState.CLOSED, MissionTransitionReason.RESULTS_STARTED),
                (CardMissionState.OPEN, CardMissionState.VOID, MissionTransitionReason.ADMIN_VOID),
                (CardMissionState.CLOSED, CardMissionState.OPEN, MissionTransitionReason.ADMIN_REOPEN),
                (CardMissionState.CLOSED, CardMissionState.VOID, MissionTransitionReason.ADMIN_VOID),
            },
        ),
        (
            MonthlyConfigState,
            ensure_monthly_config_transition,
            {
                (MonthlyConfigState.DRAFT, MonthlyConfigState.ACTIVE, MissionTransitionReason.MONTH_START),
                (MonthlyConfigState.ACTIVE, MonthlyConfigState.CLOSED, MissionTransitionReason.MONTH_CLOSE),
                (MonthlyConfigState.ACTIVE, MonthlyConfigState.CLOSED, MissionTransitionReason.ADMIN_CLOSE),
            },
        ),
        (
            MonthlyProgressStatus,
            ensure_monthly_progress_transition,
            {
                (MonthlyProgressStatus.ACTIVE, MonthlyProgressStatus.COMPLETED, MissionTransitionReason.EVALUATION),
                (MonthlyProgressStatus.ACTIVE, MonthlyProgressStatus.FAILED, MissionTransitionReason.MONTH_CLOSE),
                (MonthlyProgressStatus.ACTIVE, MonthlyProgressStatus.VOID, MissionTransitionReason.ADMIN_VOID),
                (MonthlyProgressStatus.COMPLETED, MonthlyProgressStatus.ACTIVE, MissionTransitionReason.RESULT_CORRECTION),
                (MonthlyProgressStatus.COMPLETED, MonthlyProgressStatus.FAILED, MissionTransitionReason.RESULT_CORRECTION),
                (MonthlyProgressStatus.COMPLETED, MonthlyProgressStatus.VOID, MissionTransitionReason.ADMIN_VOID),
                (MonthlyProgressStatus.FAILED, MonthlyProgressStatus.COMPLETED, MissionTransitionReason.RESULT_CORRECTION),
                (MonthlyProgressStatus.FAILED, MonthlyProgressStatus.VOID, MissionTransitionReason.ADMIN_VOID),
            },
        ),
        (
                CelebrationStatus,
                ensure_celebration_transition,
                {
                    (CelebrationStatus.PENDING, CelebrationStatus.ACKNOWLEDGED, MissionTransitionReason.ACKNOWLEDGEMENT),
                    (CelebrationStatus.PENDING, CelebrationStatus.CANCELLED, MissionTransitionReason.RESULT_CORRECTION),
                },
        ),
    ],
)
def test_transition_matrix_is_exhaustive(status_type, ensure, legal):
    for current, target, reason in itertools.product(
        status_type,
        status_type,
        MissionTransitionReason,
    ):
        if current == target or (current, target, reason) in legal:
            ensure(current, target, reason)
        else:
            with pytest.raises(IllegalMissionTransition):
                ensure(current, target, reason)
