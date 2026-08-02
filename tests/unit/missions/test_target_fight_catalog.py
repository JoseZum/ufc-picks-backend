from collections import Counter

from app.modules.missions.catalog import load_card_catalog
from app.modules.missions.domain import (
    CARD_METRIC_REGISTRY,
    CardCapability,
    MetricComparator,
    MetricObservation,
    MissionDifficulty,
    MissionInteractionType,
    PickEffect,
    TargetFightOutcome,
    resolve_metric_observation,
)

APPROVED_TARGET_FIGHT_IDS = {
    "CARD-V2-M-001",
    "CARD-V2-M-003",
    "CARD-V2-M-004",
    "CARD-V2-H-002",
    "CARD-V3-M-002",
}


def target_fight_definitions():
    return load_card_catalog().by_interaction(MissionInteractionType.TARGET_FIGHT)


def test_reviewed_target_fight_family_is_complete():
    definitions = target_fight_definitions()

    assert {definition.mission_id for definition in definitions} == (
        APPROVED_TARGET_FIGHT_IDS
    )
    assert Counter(definition.difficulty for definition in definitions) == {
        MissionDifficulty.MEDIUM: 3,
        MissionDifficulty.HARD: 2,
    }


def test_target_fight_missions_never_mutate_competitive_picks():
    for definition in target_fight_definitions():
        assert definition.evaluation.metric in CARD_METRIC_REGISTRY.names
        assert definition.evaluation.comparator == MetricComparator.ALL
        assert definition.pick_effect == PickEffect.NONE
        assert definition.ui.selection_prompt


def test_finish_round_definitions_freeze_the_same_round_everywhere():
    for definition in target_fight_definitions():
        if definition.selection.outcome != TargetFightOutcome.FINISH_ROUND:
            assert definition.selection.required_round is None
            continue

        expected_round = definition.selection.required_round
        assert definition.evaluation.parameters["round"] == expected_round
        assert CardCapability.RESULT_ROUND in definition.eligibility.capabilities
        assert CardCapability.RESULT_METHOD in definition.eligibility.capabilities


def test_every_target_fight_progress_template_renders():
    for definition in target_fight_definitions():
        result = resolve_metric_observation(
            spec=definition.evaluation,
            observation=MetricObservation(
                metric=definition.evaluation.metric,
                value=0,
                numerator=0,
                denominator=1,
                total_count=1,
                terminal=False,
            ),
            progress_template=definition.ui.progress_template,
        )

        assert result.progress.text
        assert result.progress.percent == 0
