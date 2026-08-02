import pytest

from app.modules.missions.domain import (
    COMPARATOR_REGISTRY,
    ComparatorRegistry,
    EvaluationTargetSource,
    MetricComparator,
    MetricObservation,
    MetricResolutionReason,
    MetricResolutionStatus,
    MetricVoidReason,
    MissionEvaluationSpec,
    ResolutionError,
    ResolutionErrorCode,
    resolve_metric_observation,
)


def spec(comparator, *, target=None, target_source="STATIC", parameters=None):
    return MissionEvaluationSpec(
        metric="fixture_metric",
        comparator=comparator,
        target=target,
        target_source=target_source,
        parameters=parameters or {},
        evaluate_on=("AFTER_EACH_RESULT", "CARD_FINALIZED"),
    )


def observation(
    *,
    value=0,
    other=None,
    numerator=None,
    denominator=None,
    sample_size=0,
    resolved=0,
    total=10,
    terminal=False,
    matched_items=frozenset(),
    target_override=None,
    void_reason=None,
    details=None,
):
    return MetricObservation(
        metric="fixture_metric",
        value=value,
        other_value=other,
        numerator=numerator,
        denominator=denominator,
        sample_size=sample_size,
        resolved_count=resolved,
        total_count=total,
        terminal=terminal,
        matched_items=matched_items,
        target_override=target_override,
        void_reason=void_reason,
        details=details or {},
    )


def resolve(spec_value, observation_value, template="{current} / {target}"):
    return resolve_metric_observation(
        spec=spec_value,
        observation=observation_value,
        progress_template=template,
    )


def test_comparator_registry_covers_every_persisted_comparator():
    assert set(COMPARATOR_REGISTRY.comparators) == set(MetricComparator)


def test_gte_threshold_completes_early_and_uses_target_override():
    result = resolve(
        spec(
            MetricComparator.GTE,
            target_source=EvaluationTargetSource.OBSERVATION_OVERRIDE,
        ),
        observation(value=3, resolved=3, target_override=3),
    )

    assert result.status == MetricResolutionStatus.COMPLETED
    assert result.reason == MetricResolutionReason.THRESHOLD_REACHED
    assert result.assessment.effective_target == 3
    assert result.progress.text == "3 / 3"
    assert result.progress.percent == 100


def test_required_frozen_target_never_falls_back_to_a_static_number():
    with pytest.raises(ResolutionError) as raised:
        resolve(
            spec(
                MetricComparator.EQ,
                target_source=EvaluationTargetSource.OBSERVATION_OVERRIDE,
            ),
            observation(value=2, resolved=4, total=8),
        )

    assert raised.value.code == ResolutionErrorCode.INVALID_COMPARATOR_INPUT


def test_exact_equality_waits_at_target_but_fails_early_after_overshoot():
    waiting = resolve(
        spec(MetricComparator.EQ, target=3),
        observation(value=3, resolved=6, total=10),
    )
    exceeded = resolve(
        spec(MetricComparator.EQ, target=3),
        observation(value=4, resolved=7, total=10),
    )
    completed = resolve(
        spec(MetricComparator.EQ, target=3),
        observation(value=3, resolved=10, total=10, terminal=True),
    )

    assert waiting.status == MetricResolutionStatus.PENDING
    assert waiting.satisfied is True
    assert exceeded.status == MetricResolutionStatus.FAILED
    assert exceeded.reason == MetricResolutionReason.LIMIT_EXCEEDED
    assert completed.status == MetricResolutionStatus.COMPLETED


def test_at_most_limit_waits_when_safe_and_fails_as_soon_as_exceeded():
    safe = resolve(
        spec(MetricComparator.LTE, target=1),
        observation(value=1, resolved=5, total=8),
        "{wrong} wrong / max {target}",
    )
    exceeded = resolve(
        spec(MetricComparator.LTE, target=1),
        observation(value=2, resolved=6, total=8),
    )
    final_safe = resolve(
        spec(MetricComparator.LTE, target=1),
        observation(value=1, resolved=8, total=8, terminal=True),
    )

    assert safe.status == MetricResolutionStatus.PENDING
    assert safe.progress.text == "1 wrong / max 1"
    assert exceeded.status == MetricResolutionStatus.FAILED
    assert final_safe.status == MetricResolutionStatus.COMPLETED


@pytest.mark.parametrize(
    ("comparator", "value", "other", "expected"),
    [
        (MetricComparator.GT_OTHER, 4, 3, True),
        (MetricComparator.GT_OTHER, 3, 3, False),
        (MetricComparator.GTE_OTHER, 3, 3, True),
    ],
)
def test_versus_comparators_only_resolve_at_terminal(
    comparator,
    value,
    other,
    expected,
):
    pending = resolve(
        spec(comparator),
        observation(value=value, other=other, resolved=4, total=8),
    )
    final = resolve(
        spec(comparator),
        observation(
            value=value,
            other=other,
            resolved=8,
            total=8,
            terminal=True,
        ),
    )

    assert pending.status == MetricResolutionStatus.PENDING
    assert final.satisfied is expected
    assert final.status == (
        MetricResolutionStatus.COMPLETED if expected else MetricResolutionStatus.FAILED
    )


def test_all_uses_components_and_respects_early_terminal_combo_failure():
    completed = resolve(
        spec(MetricComparator.ALL),
        observation(
            value=3,
            numerator=3,
            denominator=3,
            resolved=3,
            total=3,
            terminal=True,
        ),
        "{matched} / 3 conditions",
    )
    failed = resolve(
        spec(MetricComparator.ALL),
        observation(
            value=0,
            numerator=0,
            denominator=3,
            resolved=1,
            total=3,
            terminal=True,
        ),
    )

    assert completed.status == MetricResolutionStatus.COMPLETED
    assert completed.progress.text == "3 / 3 conditions"
    assert failed.status == MetricResolutionStatus.FAILED


def test_contains_all_completes_when_required_result_families_are_seen():
    result = resolve(
        spec(
            MetricComparator.CONTAINS_ALL,
            parameters={"required_items": "KO_TKO,SUBMISSION,DECISION"},
        ),
        observation(
            value=3,
            matched_items={"KO_TKO", "SUBMISSION", "DECISION"},
            resolved=5,
        ),
        "{methods} / 3 result types",
    )

    assert result.status == MetricResolutionStatus.COMPLETED
    assert result.progress.text == "3 / 3 result types"


def test_ratio_waits_for_terminal_and_enforces_minimum_sample():
    evaluation_spec = spec(
        MetricComparator.RATIO_GTE,
        target=0.6,
        parameters={"min_sample_size": 5},
    )
    early = resolve(
        evaluation_spec,
        observation(
            value=0.75,
            numerator=3,
            denominator=4,
            sample_size=4,
            resolved=4,
            total=8,
        ),
        "{correct} / {resolved} · {accuracy}%",
    )
    final_short = resolve(
        evaluation_spec,
        observation(
            value=0.75,
            numerator=3,
            denominator=4,
            sample_size=4,
            resolved=8,
            total=8,
            terminal=True,
        ),
    )
    final_enough = resolve(
        evaluation_spec,
        observation(
            value=0.6,
            numerator=3,
            denominator=5,
            sample_size=5,
            resolved=8,
            total=8,
            terminal=True,
        ),
    )

    assert early.status == MetricResolutionStatus.PENDING
    assert early.progress.text == "3 / 4 · 75%"
    assert final_short.status == MetricResolutionStatus.FAILED
    assert final_short.reason == MetricResolutionReason.INSUFFICIENT_SAMPLE
    assert final_enough.status == MetricResolutionStatus.COMPLETED


def test_insufficient_target_population_can_void_at_final():
    evaluation_spec = spec(
        MetricComparator.ALL,
        parameters={
            "min_total_count": 4,
            "insufficient_targets_status": "VOID",
        },
    )
    result = resolve(
        evaluation_spec,
        observation(
            value=2,
            numerator=2,
            denominator=2,
            sample_size=2,
            resolved=2,
            total=2,
            terminal=True,
        ),
    )

    assert result.status == MetricResolutionStatus.VOID
    assert result.reason == MetricResolutionReason.INSUFFICIENT_TARGETS


def test_observation_void_has_priority_over_comparator_match():
    result = resolve(
        spec(MetricComparator.GTE, target=0),
        observation(
            value=0,
            numerator=0,
            denominator=1,
            resolved=0,
            total=1,
            terminal=True,
            void_reason=MetricVoidReason.TARGET_CANCELLED,
        ),
        "{result} / win",
    )

    assert result.status == MetricResolutionStatus.VOID
    assert result.reason == MetricResolutionReason.OBSERVATION_VOID
    assert result.progress.text == "VOID / win"


def test_formatter_supports_every_approved_placeholder_family():
    result = resolve(
        spec(MetricComparator.GTE_OTHER),
        observation(
            value=4,
            other=3,
            numerator=3,
            denominator=5,
            sample_size=5,
            resolved=5,
            total=8,
            terminal=True,
            matched_items={"KO_TKO", "DECISION"},
            details={
                "finishes": 4,
                "decisions": 3,
                "rank": 1,
                "actual_round": 2,
            },
        ),
        (
            "{current}|{target}|{displayed_target}|{correct}|"
            "{eligible_card_bouts}|{main_card_bouts}|{prelim_bouts}|"
            "{resolved}|{accuracy}|{finishes}|{decisions}|{eligible}|"
            "{rate}|{matched}|{methods}|{result}|{selected_count}|"
            "{other_count}|{wrong}|{rank}|{actual}"
        ),
    )

    assert "4|—|—|3|8" in result.progress.text
    assert result.progress.text.endswith("|4|3|4|1|2")


def test_unknown_progress_placeholder_is_rejected():
    with pytest.raises(ResolutionError) as raised:
        resolve(
            spec(MetricComparator.GTE, target=1),
            observation(value=1),
            "{arbitrary_python}",
        )
    assert raised.value.code == ResolutionErrorCode.UNKNOWN_PROGRESS_PLACEHOLDER


def test_duplicate_comparator_registration_is_rejected():
    registry = ComparatorRegistry()

    def provider(observation_value, target, parameters):
        return None

    registry.register(MetricComparator.GTE, provider)
    with pytest.raises(ResolutionError) as raised:
        registry.register(MetricComparator.GTE, provider)
    assert raised.value.code == ResolutionErrorCode.DUPLICATE_COMPARATOR
