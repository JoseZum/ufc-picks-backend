import pytest

from app.modules.missions.catalog import load_card_catalog
from app.modules.missions.domain import (
    ExceptionalEvaluatorError,
    ExceptionalEvaluatorErrorCode,
    ExceptionalEvaluatorRegistry,
    MetricObservation,
    MissionEvaluationContext,
    build_card_metric_registry,
    load_mission_catalog,
    validate_catalog_evaluator_coverage,
)
from tests.unit.missions.test_definitions import base_definition


def exceptional_definition(*, mission_id="CARD-V2-E-004", key="special_rule"):
    value = base_definition(
        mission_id=mission_id,
        evaluation={
            "metric": "special_metric",
            "custom_evaluator_key": key,
            "comparator": "GTE",
            "target": 1,
            "evaluate_on": ["AFTER_EACH_RESULT", "CARD_FINALIZED"],
        },
    )
    return load_mission_catalog([value], expected_version="2026.08.01")[0]


def test_reviewed_catalog_is_fully_declarative_and_needs_zero_exceptions():
    catalog = load_card_catalog()
    report = validate_catalog_evaluator_coverage(catalog)

    assert report.total_count == 85
    assert len(report.declarative_mission_ids) == 85
    assert report.exceptional_mission_ids == ()


def test_exception_requires_explicit_scope_and_nonempty_justification():
    registry = ExceptionalEvaluatorRegistry()

    with pytest.raises(ExceptionalEvaluatorError) as raised:
        registry.register(
            "special_rule",
            mission_ids=frozenset(),
            justification="",
            provider=lambda _spec, _context: None,
        )

    assert raised.value.code == ExceptionalEvaluatorErrorCode.INVALID_REGISTRATION


def test_exceptional_coverage_accepts_only_allowlisted_definition():
    definition = exceptional_definition()
    catalog = load_mission_catalog(
        [definition.model_dump(mode="json")],
        expected_version="2026.08.01",
    )
    registry = ExceptionalEvaluatorRegistry()
    registry.register(
        "special_rule",
        mission_ids=frozenset({definition.mission_id}),
        justification="Fixture proves the explicit exception boundary.",
        provider=lambda spec, _context: MetricObservation(
            metric=spec.metric,
            value=1,
            sample_size=1,
            resolved_count=1,
            total_count=1,
            terminal=True,
        ),
    )

    report = validate_catalog_evaluator_coverage(
        catalog,
        metric_registry=build_card_metric_registry(),
        exceptional_registry=registry,
    )

    assert report.declarative_mission_ids == ()
    assert report.exceptional_mission_ids == (definition.mission_id,)


def test_unknown_metric_without_explicit_exception_is_rejected():
    value = base_definition(
        evaluation={
            "metric": "unknown_metric",
            "comparator": "GTE",
            "target": 1,
            "evaluate_on": ["AFTER_EACH_RESULT", "CARD_FINALIZED"],
        }
    )
    catalog = load_mission_catalog([value], expected_version="2026.08.01")

    with pytest.raises(ExceptionalEvaluatorError) as raised:
        validate_catalog_evaluator_coverage(catalog)

    assert raised.value.code == ExceptionalEvaluatorErrorCode.UNREGISTERED_METRIC


def test_exception_cannot_be_reused_by_an_unlisted_mission():
    definition = exceptional_definition(mission_id="CARD-V2-E-005")
    registry = ExceptionalEvaluatorRegistry()
    registry.register(
        "special_rule",
        mission_ids=frozenset({"CARD-V2-E-004"}),
        justification="Only the named mission owns this rule.",
        provider=lambda _spec, _context: None,
    )

    with pytest.raises(ExceptionalEvaluatorError) as raised:
        registry.require_definition(definition)

    assert raised.value.code == ExceptionalEvaluatorErrorCode.MISSION_NOT_ALLOWED
