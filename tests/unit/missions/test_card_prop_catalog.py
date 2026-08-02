from collections import Counter

from app.modules.missions.catalog import load_card_catalog
from app.modules.missions.domain import (
    CARD_METRIC_REGISTRY,
    CardCapability,
    CardPropInput,
    CardPropTargetSource,
    EvaluationTargetSource,
    MetricComparator,
    MetricObservation,
    MissionDifficulty,
    MissionInteractionType,
    PickEffect,
    resolve_metric_observation,
)

APPROVED_CARD_PROP_ROWS = {
    "CARD-V2-E-012": ("EASY", 1, "THREE FINISHES", "card_finish_count", "{current} / 3 finishes"),
    "CARD-V2-E-013": (
        "EASY",
        1,
        "THREE DECISIONS",
        "card_decision_count",
        "{current} / 3 decisions",
    ),
    "CARD-V2-E-016": (
        "EASY",
        1,
        "ROUND ONE EXISTS",
        "card_round_finish_count",
        "{current} / 1 R1 finish",
    ),
    "CARD-V2-E-017": (
        "EASY",
        1,
        "MAIN CARD ACTION",
        "main_card_finish_count",
        "{current} / 1 main-card finish",
    ),
    "CARD-V2-E-018": (
        "EASY",
        1,
        "PRELIM ACTION",
        "prelim_finish_count",
        "{current} / 1 prelim finish",
    ),
    "CARD-V2-E-019": (
        "EASY",
        1,
        "MIXED RESULTS",
        "card_method_presence",
        "{methods} / 2 result types",
    ),
    "CARD-V3-E-005": (
        "EASY",
        2,
        "SECOND-ROUND SIGHTING",
        "card_round_finish_count",
        "{current} / 1 R2 finish",
    ),
    "CARD-V3-E-008": (
        "EASY",
        2,
        "PICK A LANE",
        "selected_result_family_vs_other",
        "{selected_count} vs {other_count}",
    ),
    "CARD-V3-M-011": (
        "EASY",
        2,
        "RESULT TRIANGLE",
        "card_method_presence",
        "{methods} / 3 result types",
    ),
    "CARD-V2-M-017": (
        "MEDIUM",
        3,
        "DISPLAYED FINISH LINE",
        "card_finish_count",
        "{current} / {displayed_target} finishes",
    ),
    "CARD-V2-M-018": (
        "MEDIUM",
        3,
        "DISPLAYED DECISION LINE",
        "card_decision_count",
        "{current} / {displayed_target} decisions",
    ),
    "CARD-V2-M-019": (
        "MEDIUM",
        4,
        "VIOLENCE NIGHT",
        "card_finish_vs_decision",
        "{finishes} F / {decisions} D",
    ),
    "CARD-V3-M-008": (
        "MEDIUM",
        4,
        "HALF VIOLENCE",
        "card_finish_rate",
        "{finishes} / {eligible} · {rate}%",
    ),
    "CARD-V3-M-009": (
        "MEDIUM",
        5,
        "JUDGES' NIGHT",
        "card_decision_vs_finish",
        "{decisions} D / {finishes} F",
    ),
    "CARD-V2-H-017": (
        "HARD",
        6,
        "SUBMISSION TRIO ON THE CARD",
        "card_submission_count",
        "{current} / 3 submissions",
    ),
    "CARD-V2-H-018": (
        "HARD",
        8,
        "NO JUDGES MAIN CARD",
        "main_card_decision_count",
        "{decisions} decisions / target 0",
    ),
    "CARD-V2-H-019": (
        "HARD",
        10,
        "EXACT FINISH COUNT",
        "card_finish_count",
        "{current} finishes / target {target}",
    ),
    "CARD-V3-H-013": (
        "HARD",
        10,
        "EXACT DECISION COUNT",
        "card_decision_count",
        "{current} decisions / target {target}",
    ),
    "CARD-V3-H-017": (
        "HARD",
        15,
        "JUDGES TOOK THE NIGHT OFF",
        "card_decision_count",
        "{decisions} decisions / target 0",
    ),
}


def card_prop_definitions():
    return load_card_catalog().by_interaction(MissionInteractionType.CARD_PROP)


def test_reviewed_card_prop_family_is_complete_and_matches_reviewed_core_copy():
    definitions = card_prop_definitions()

    assert {definition.mission_id for definition in definitions} == set(APPROVED_CARD_PROP_ROWS)
    assert Counter(definition.difficulty for definition in definitions) == {
        MissionDifficulty.EASY: 9,
        MissionDifficulty.MEDIUM: 5,
        MissionDifficulty.HARD: 5,
    }
    for definition in definitions:
        difficulty, xp, name, metric, progress = APPROVED_CARD_PROP_ROWS[definition.mission_id]
        assert definition.difficulty.value == difficulty
        assert definition.xp == xp
        assert definition.ui.name == name
        assert definition.evaluation.metric == metric
        assert definition.ui.progress_template == progress


def test_card_props_never_modify_picks_and_use_registered_metrics():
    for definition in card_prop_definitions():
        assert definition.pick_effect == PickEffect.NONE
        assert definition.evaluation.metric in CARD_METRIC_REGISTRY.names
        assert definition.ui.selection_prompt


def test_frozen_lines_exact_counts_choice_and_mixed_results_are_declarative():
    catalog = load_card_catalog()
    for mission_id in {"CARD-V2-M-017", "CARD-V2-M-018"}:
        definition = catalog.get(mission_id)
        assert definition.selection.target_source == CardPropTargetSource.FROZEN_ELIGIBLE_RATIO
        assert definition.selection.frozen_ratio == 0.4
        assert definition.evaluation.target_source == EvaluationTargetSource.OBSERVATION_OVERRIDE
        assert definition.evaluation.target is None

    for mission_id in {"CARD-V2-H-019", "CARD-V3-H-013"}:
        definition = catalog.get(mission_id)
        assert definition.selection.input == CardPropInput.EXACT_COUNT
        assert definition.selection.target_source == CardPropTargetSource.SELECTED_EXACT_COUNT
        assert definition.evaluation.comparator == MetricComparator.EQ
        assert definition.evaluation.target_source == EvaluationTargetSource.OBSERVATION_OVERRIDE

    lane = catalog.get("CARD-V3-E-008")
    assert lane.selection.choices == ("FINISHES", "DECISIONS")
    assert lane.evaluation.comparator == MetricComparator.GTE_OTHER

    mixed = catalog.get("CARD-V2-E-019")
    assert mixed.evaluation.parameters["presence_mode"] == "FINISH_DECISION"
    assert mixed.evaluation.parameters["required_items"] == "FINISH,DECISION"


def test_card_prop_minimums_and_capabilities_match_resolution_scope():
    for definition in card_prop_definitions():
        minimum = definition.evaluation.parameters.get("min_total_count")
        assert minimum is not None
        if definition.mission_id in {"CARD-V2-E-017", "CARD-V2-H-018"}:
            assert minimum == definition.eligibility.min_main_card_bouts
            assert CardCapability.SECTION_ORDER in definition.eligibility.capabilities
        elif definition.mission_id == "CARD-V2-E-018":
            assert minimum == definition.eligibility.min_prelim_bouts
            assert CardCapability.SECTION_ORDER in definition.eligibility.capabilities
        else:
            assert minimum == definition.eligibility.min_eligible_bouts


def test_every_card_prop_progress_template_renders():
    for definition in card_prop_definitions():
        total = definition.evaluation.parameters["min_total_count"]
        kwargs = {
            "metric": definition.evaluation.metric,
            "value": 0,
            "sample_size": 0,
            "resolved_count": 0,
            "total_count": total,
            "terminal": False,
            "details": {"finishes": 0, "decisions": 0},
        }
        if definition.evaluation.comparator in {
            MetricComparator.GT_OTHER,
            MetricComparator.GTE_OTHER,
        }:
            kwargs["other_value"] = 0
        if definition.evaluation.comparator == MetricComparator.RATIO_GTE:
            kwargs.update(numerator=0, denominator=total)
        if definition.evaluation.target_source == EvaluationTargetSource.OBSERVATION_OVERRIDE:
            kwargs["target_override"] = 1

        result = resolve_metric_observation(
            spec=definition.evaluation,
            observation=MetricObservation(**kwargs),
            progress_template=definition.ui.progress_template,
        )

        assert result.progress.text
        assert 0 <= result.progress.percent <= 100
