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
    PickField,
    WinnerBinding,
    resolve_metric_observation,
)

APPROVED_TARGET_FIGHTER_IDS = {
    "CARD-V2-E-001",
    "CARD-V2-E-002",
    "CARD-V2-H-001",
    "CARD-V2-M-002",
    "CARD-V3-M-001",
    "CARD-V2-H-003",
    "CARD-V2-H-015",
    "CARD-V3-H-006",
    "CARD-V3-H-007",
}


def target_fighter_definitions():
    return load_card_catalog().by_interaction(MissionInteractionType.TARGET_FIGHTER)


def test_reviewed_target_fighter_family_is_complete():
    definitions = target_fighter_definitions()

    assert {definition.mission_id for definition in definitions} == (
        APPROVED_TARGET_FIGHTER_IDS
    )
    assert Counter(definition.difficulty for definition in definitions) == {
        MissionDifficulty.EASY: 2,
        MissionDifficulty.MEDIUM: 3,
        MissionDifficulty.HARD: 4,
    }


def test_every_target_fighter_mission_binds_a_canonical_winner_pick():
    for definition in target_fighter_definitions():
        assert definition.evaluation.metric in CARD_METRIC_REGISTRY.names
        assert definition.evaluation.comparator == MetricComparator.ALL
        assert definition.pick_effect == PickEffect.UPSERT_ONE
        assert PickField.WINNER in definition.selection.bound_pick_fields
        assert definition.ui.selection_prompt

    fade = load_card_catalog().get("CARD-V2-E-002")
    assert fade.selection.winner_binding == WinnerBinding.OPPONENT_OF_SELECTED_FIGHTER
    assert fade.selection.bound_pick_fields == frozenset({PickField.WINNER})


def test_method_round_and_title_bindings_match_the_reviewed_contract():
    catalog = load_card_catalog()

    exact = catalog.get("CARD-V2-H-003")
    assert exact.selection.bound_pick_fields == frozenset(
        {PickField.WINNER, PickField.METHOD, PickField.ROUND}
    )
    assert exact.selection.allowed_rounds == (1, 2, 3, 4, 5)

    title = catalog.get("CARD-V2-H-015")
    assert title.selection.title_bouts_only is True
    assert title.eligibility.min_title_bouts == 1
    assert CardCapability.TITLE_BOUTS in title.eligibility.capabilities

    first_round_ids = {"CARD-V3-H-006", "CARD-V3-H-007"}
    for mission_id in first_round_ids:
        definition = catalog.get(mission_id)
        assert definition.selection.allowed_rounds == (1,)
        assert PickField.ROUND in definition.selection.bound_pick_fields


def test_every_target_fighter_progress_template_renders():
    for definition in target_fighter_definitions():
        component_count = len(definition.selection.bound_pick_fields)
        result = resolve_metric_observation(
            spec=definition.evaluation,
            observation=MetricObservation(
                metric=definition.evaluation.metric,
                value=0,
                numerator=0,
                denominator=component_count,
                total_count=component_count,
                terminal=False,
            ),
            progress_template=definition.ui.progress_template,
        )

        assert result.progress.text
        assert result.progress.percent == 0
