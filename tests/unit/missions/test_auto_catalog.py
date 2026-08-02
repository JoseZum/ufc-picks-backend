from collections import Counter

from app.modules.missions.catalog import (
    CARD_CATALOG_PATH,
    CARD_CATALOG_VERSION,
    load_card_catalog,
)
from app.modules.missions.domain import (
    CARD_METRIC_REGISTRY,
    MetricComparator,
    MetricObservation,
    MissionDifficulty,
    MissionInteractionType,
    PickEffect,
    resolve_metric_observation,
)

APPROVED_AUTO_IDS = {
    "CARD-V2-E-004",
    "CARD-V2-E-005",
    "CARD-V2-E-006",
    "CARD-V2-E-007",
    "CARD-V2-E-008",
    "CARD-V2-E-009",
    "CARD-V2-E-010",
    "CARD-V2-E-011",
    "CARD-V2-E-020",
    "CARD-V3-E-001",
    "CARD-V3-E-002",
    "CARD-V3-E-003",
    "CARD-V3-E-004",
    "CARD-V3-E-007",
    "CARD-V2-M-005",
    "CARD-V2-M-006",
    "CARD-V2-M-007",
    "CARD-V2-M-008",
    "CARD-V2-M-009",
    "CARD-V2-M-010",
    "CARD-V2-M-016",
    "CARD-V2-M-020",
    "CARD-V3-M-003",
    "CARD-V3-M-004",
    "CARD-V3-M-005",
    "CARD-V3-M-010",
    "CARD-V2-H-004",
    "CARD-V2-H-005",
    "CARD-V2-H-006",
    "CARD-V2-H-012",
    "CARD-V2-H-013",
    "CARD-V2-H-014",
    "CARD-V2-H-016",
    "CARD-V3-H-008",
    "CARD-V3-H-009",
    "CARD-V3-H-010",
    "CARD-V3-H-011",
    "CARD-V3-H-012",
    "CARD-V3-H-014",
    "CARD-V3-H-016",
}


def auto_definitions():
    return load_card_catalog().by_interaction(MissionInteractionType.AUTO)


def test_reviewed_auto_family_is_complete_and_uniquely_versioned():
    definitions = auto_definitions()

    assert CARD_CATALOG_PATH.is_file()
    assert {definition.mission_id for definition in definitions} == APPROVED_AUTO_IDS
    assert len(definitions) == 40
    assert {definition.catalog_version for definition in definitions} == {
        CARD_CATALOG_VERSION
    }
    assert Counter(definition.difficulty for definition in definitions) == {
        MissionDifficulty.EASY: 14,
        MissionDifficulty.MEDIUM: 12,
        MissionDifficulty.HARD: 14,
    }


def test_auto_family_uses_only_registered_metrics_and_no_pick_mutation():
    for definition in auto_definitions():
        assert definition.evaluation.metric in CARD_METRIC_REGISTRY.names
        assert definition.pick_effect == PickEffect.NONE
        assert definition.selection is None
        assert definition.ui.selection_prompt is None


def test_auto_rewards_follow_reviewed_tier_ranges_and_clean_sweep_contract():
    xp_ranges = {
        MissionDifficulty.EASY: range(1, 3),
        MissionDifficulty.MEDIUM: range(3, 6),
        MissionDifficulty.HARD: range(6, 16),
    }
    definitions = auto_definitions()

    for definition in definitions:
        assert definition.xp in xp_ranges[definition.difficulty]

    clean_sweep = load_card_catalog().get("CARD-V2-H-006")
    assert clean_sweep.xp == 15
    assert clean_sweep.evaluation.metric == "wrong_winner_count"
    assert clean_sweep.evaluation.comparator == MetricComparator.LTE
    assert clean_sweep.evaluation.target == 0
    assert clean_sweep.evaluation.parameters["require_full_card"] is True
    assert clean_sweep.eligibility.min_eligible_bouts == 8


def test_every_auto_progress_template_renders_with_safe_placeholder_values():
    for definition in auto_definitions():
        comparator = definition.evaluation.comparator
        total = max(
            1,
            definition.evaluation.parameters.get("min_total_count", 1),
        )
        kwargs = {
            "metric": definition.evaluation.metric,
            "value": 0,
            "sample_size": 0,
            "resolved_count": 0,
            "total_count": total,
            "terminal": False,
            "details": {"rank": 3},
        }
        if comparator == MetricComparator.ALL:
            kwargs.update(numerator=0, denominator=total)

        result = resolve_metric_observation(
            spec=definition.evaluation,
            observation=MetricObservation(**kwargs),
            progress_template=definition.ui.progress_template,
        )

        assert result.progress.text
        assert 0 <= result.progress.percent <= 100
