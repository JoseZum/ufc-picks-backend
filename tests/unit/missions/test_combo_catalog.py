from collections import Counter

from app.modules.missions.catalog import load_card_catalog
from app.modules.missions.domain import (
    CARD_METRIC_REGISTRY,
    CardCapability,
    ComboLegTarget,
    MetricComparator,
    MetricObservation,
    MissionDifficulty,
    MissionInteractionType,
    PickEffect,
    WinMethod,
    resolve_metric_observation,
)

APPROVED_COMBO_ROWS = {
    "CARD-V2-M-011": (
        "MEDIUM",
        3,
        "WINNER DOUBLE",
        "combo_correct_winner_count",
        "{current} / 2 legs",
    ),
    "CARD-V2-M-012": (
        "MEDIUM",
        4,
        "FINISH DOUBLE",
        "combo_bout_finish_count",
        "{current} / 2 finish legs",
    ),
    "CARD-V2-M-013": (
        "MEDIUM",
        4,
        "METHOD PAIR",
        "combo_distinct_correct_methods",
        "{current} / 2 method legs",
    ),
    "CARD-V3-M-006": ("MEDIUM", 4, "KO / DEC SPLIT", "combo_ko_dec_correct", "{current} / 2 legs"),
    "CARD-V3-M-007": (
        "MEDIUM",
        3,
        "FINISH / DECISION DOUBLE",
        "combo_finish_decision_bouts",
        "{current} / 2 legs",
    ),
    "CARD-V2-H-007": (
        "HARD",
        8,
        "METHOD CYCLE",
        "combo_correct_method_cycle",
        "{current} / 3 cycle legs",
    ),
    "CARD-V2-H-008": ("HARD", 8, "KO HAT TRICK", "combo_correct_ko_wins", "{current} / 3 KO legs"),
    "CARD-V2-H-009": (
        "HARD",
        6,
        "DECISION HAT TRICK",
        "combo_correct_decision_wins",
        "{current} / 3 DEC legs",
    ),
    "CARD-V2-H-010": (
        "HARD",
        14,
        "ROUND LADDER",
        "combo_correct_round_ladder",
        "{current} / 3 round legs",
    ),
    "CARD-V3-H-002": (
        "HARD",
        15,
        "SUBMISSION HAT TRICK",
        "combo_correct_submission_wins",
        "{current} / 3 SUB legs",
    ),
    "CARD-V3-H-005": ("HARD", 6, "KO / SUB DOUBLE", "combo_ko_sub_correct", "{current} / 2 legs"),
    "CARD-V3-H-015": (
        "HARD",
        12,
        "DOUBLE GOLD SCRIPT",
        "title_bout_winner_method_accuracy",
        "{matched} / 4 title conditions",
    ),
}


def combo_definitions():
    return load_card_catalog().by_interaction(MissionInteractionType.COMBO_BUILDER)


def test_reviewed_combo_family_is_complete_and_matches_reviewed_core_copy():
    definitions = combo_definitions()

    assert {definition.mission_id for definition in definitions} == set(APPROVED_COMBO_ROWS)
    assert Counter(definition.difficulty for definition in definitions) == {
        MissionDifficulty.MEDIUM: 5,
        MissionDifficulty.HARD: 7,
    }
    for definition in definitions:
        difficulty, xp, name, metric, progress = APPROVED_COMBO_ROWS[definition.mission_id]
        assert definition.difficulty.value == difficulty
        assert definition.xp == xp
        assert definition.ui.name == name
        assert definition.evaluation.metric == metric
        assert definition.ui.progress_template == progress


def test_combo_pick_effect_follows_leg_target_and_every_metric_is_registered():
    for definition in combo_definitions():
        target = definition.selection.legs[0].target
        expected_effect = (
            PickEffect.UPSERT_MANY if target == ComboLegTarget.FIGHTER else PickEffect.NONE
        )
        assert definition.pick_effect == expected_effect
        assert definition.evaluation.metric in CARD_METRIC_REGISTRY.names
        assert definition.evaluation.comparator == MetricComparator.ALL
        assert definition.ui.selection_prompt


def test_dynamic_method_round_ladder_and_double_gold_contracts_are_frozen():
    catalog = load_card_catalog()
    method_pair = catalog.get("CARD-V2-M-013")
    assert method_pair.selection.distinct_methods is True
    assert all(
        leg.allowed_methods
        == frozenset({WinMethod.KO_TKO, WinMethod.SUBMISSION, WinMethod.DECISION})
        for leg in method_pair.selection.legs
    )

    round_ladder = catalog.get("CARD-V2-H-010")
    assert [leg.round for leg in round_ladder.selection.legs] == [1, 2, 3]
    assert all(
        leg.method is None and not leg.allowed_methods for leg in round_ladder.selection.legs
    )
    assert CardCapability.RESULT_ROUND in round_ladder.eligibility.capabilities

    double_gold = catalog.get("CARD-V3-H-015")
    assert double_gold.selection.title_bouts_only is True
    assert double_gold.eligibility.min_title_bouts == 2
    assert CardCapability.TITLE_BOUTS in double_gold.eligibility.capabilities


def test_every_combo_progress_template_renders():
    for definition in combo_definitions():
        denominator = (
            4 if definition.mission_id == "CARD-V3-H-015" else definition.selection.leg_count
        )
        result = resolve_metric_observation(
            spec=definition.evaluation,
            observation=MetricObservation(
                metric=definition.evaluation.metric,
                value=0,
                numerator=0,
                denominator=denominator,
                total_count=denominator,
                terminal=False,
            ),
            progress_template=definition.ui.progress_template,
        )

        assert result.progress.text
        assert result.progress.percent == 0
