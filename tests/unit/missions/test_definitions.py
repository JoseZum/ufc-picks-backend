import pytest
from pydantic import ValidationError

from app.modules.missions.domain import (
    AutoMissionDefinition,
    CardPropMissionDefinition,
    ComboBuilderMissionDefinition,
    TargetFighterMissionDefinition,
    TargetFightMissionDefinition,
    validate_mission_definition,
)


def base_definition(**overrides):
    value = {
        "mission_id": "CARD-V2-E-004",
        "catalog_version": "2026.08.01",
        "difficulty": "EASY",
        "xp": 1,
        "ui": {
            "name": "HEADLINER READ",
            "description": "Correctly predict the main-event winner.",
            "progress_template": "{current} / 1",
        },
        "evaluation": {
            "metric": "main_event_correct_winner",
            "comparator": "GTE",
            "target": 1,
            "evaluate_on": ["AFTER_EACH_RESULT", "CARD_FINALIZED"],
        },
        "eligibility": {
            "min_eligible_bouts": 1,
            "capabilities": ["CANONICAL_CARD", "MAIN_EVENT"],
        },
        "compatibility": "V1_READY",
        "overlap_tags": ["performance", "winner", "main-event"],
        "interaction": "AUTO",
        "pick_effect": "NONE",
        "selection": None,
    }
    value.update(overrides)
    return value


def interactive_ui(name="MISSION"):
    return {
        "name": name,
        "description": "Make the required mission selection.",
        "progress_template": "{current} / {target}",
        "selection_prompt": "Choose your mission target",
    }


def test_auto_definition_validates_and_is_frozen():
    definition = validate_mission_definition(base_definition())

    assert isinstance(definition, AutoMissionDefinition)
    assert definition.interaction.value == "AUTO"
    with pytest.raises(ValidationError):
        definition.xp = 99


@pytest.mark.parametrize(
    ("mission_id", "difficulty", "xp", "interaction", "pick_effect", "selection"),
    [
        (
            "CARD-V3-M-011",
            "EASY",
            1,
            "CARD_PROP",
            "NONE",
            {"input": "ACCEPT"},
        ),
        (
            "CARD-V2-H-001",
            "MEDIUM",
            3,
            "TARGET_FIGHTER",
            "UPSERT_ONE",
            {
                "bound_pick_fields": ["WINNER", "METHOD"],
                "allowed_methods": ["SUBMISSION"],
            },
        ),
        (
            "CARD-V3-M-002",
            "HARD",
            6,
            "TARGET_FIGHT",
            "NONE",
            {"outcome": "FINISH_ROUND", "required_round": 2},
        ),
    ],
)
def test_historical_id_segment_does_not_override_reviewed_difficulty(
    mission_id,
    difficulty,
    xp,
    interaction,
    pick_effect,
    selection,
):
    definition = validate_mission_definition(
        base_definition(
            mission_id=mission_id,
            difficulty=difficulty,
            xp=xp,
            interaction=interaction,
            pick_effect=pick_effect,
            selection=selection,
            ui=interactive_ui(),
        )
    )

    assert definition.mission_id == mission_id
    assert definition.difficulty.value == difficulty


def test_target_fighter_definition_binds_one_canonical_pick():
    definition = validate_mission_definition(
        base_definition(
            mission_id="CARD-V2-M-002",
            difficulty="MEDIUM",
            xp=3,
            interaction="TARGET_FIGHTER",
            pick_effect="UPSERT_ONE",
            selection={
                "bound_pick_fields": ["WINNER", "METHOD"],
                "winner_binding": "SELECTED_FIGHTER",
                "allowed_methods": ["KO_TKO"],
            },
            ui={
                "name": "KO LOCK",
                "description": "Choose one fighter to win by KO/TKO.",
                "progress_template": "{result} / KO win",
                "selection_prompt": "Choose one KO/TKO winner",
            },
        )
    )

    assert isinstance(definition, TargetFighterMissionDefinition)
    assert definition.pick_effect.value == "UPSERT_ONE"


def test_target_fight_definition_never_claims_canonical_pick_fields():
    definition = validate_mission_definition(
        base_definition(
            mission_id="CARD-V2-M-004",
            difficulty="MEDIUM",
            xp=3,
            interaction="TARGET_FIGHT",
            pick_effect="NONE",
            selection={"outcome": "FINISH_ROUND", "required_round": 1},
            ui=interactive_ui("FIRST-ROUND FIRE"),
        )
    )

    assert isinstance(definition, TargetFightMissionDefinition)


def test_fighter_combo_requires_multi_pick_effect():
    definition = validate_mission_definition(
        base_definition(
            mission_id="CARD-V2-H-007",
            difficulty="HARD",
            xp=8,
            interaction="COMBO_BUILDER",
            pick_effect="UPSERT_MANY",
            selection={
                "leg_count": 3,
                "legs": [
                    {"key": "ko", "label": "KO/TKO", "target": "FIGHTER", "method": "KO_TKO"},
                    {
                        "key": "sub",
                        "label": "Submission",
                        "target": "FIGHTER",
                        "method": "SUBMISSION",
                    },
                    {"key": "dec", "label": "Decision", "target": "FIGHTER", "method": "DECISION"},
                ],
            },
            ui=interactive_ui("METHOD CYCLE"),
        )
    )

    assert isinstance(definition, ComboBuilderMissionDefinition)
    assert len(definition.selection.legs) == 3


def test_combo_dynamic_method_and_round_only_contracts_are_explicit():
    method_pair = validate_mission_definition(
        base_definition(
            mission_id="CARD-V2-M-013",
            difficulty="MEDIUM",
            xp=4,
            interaction="COMBO_BUILDER",
            pick_effect="UPSERT_MANY",
            selection={
                "leg_count": 2,
                "distinct_methods": True,
                "legs": [
                    {
                        "key": "one",
                        "label": "One",
                        "target": "FIGHTER",
                        "allowed_methods": ["KO_TKO", "SUBMISSION", "DECISION"],
                    },
                    {
                        "key": "two",
                        "label": "Two",
                        "target": "FIGHTER",
                        "allowed_methods": ["KO_TKO", "SUBMISSION", "DECISION"],
                    },
                ],
            },
            ui=interactive_ui("METHOD PAIR"),
        )
    )
    round_ladder = validate_mission_definition(
        base_definition(
            mission_id="CARD-V2-H-010",
            difficulty="HARD",
            xp=14,
            interaction="COMBO_BUILDER",
            pick_effect="UPSERT_MANY",
            selection={
                "leg_count": 3,
                "legs": [
                    {"key": "r1", "label": "R1", "target": "FIGHTER", "round": 1},
                    {"key": "r2", "label": "R2", "target": "FIGHTER", "round": 2},
                    {"key": "r3", "label": "R3", "target": "FIGHTER", "round": 3},
                ],
            },
            ui=interactive_ui("ROUND LADDER"),
        )
    )

    assert method_pair.selection.distinct_methods is True
    assert all(leg.allowed_methods for leg in method_pair.selection.legs)
    assert [leg.round for leg in round_ladder.selection.legs] == [1, 2, 3]
    assert all(leg.method is None for leg in round_ladder.selection.legs)


@pytest.mark.parametrize(
    "selection",
    [
        {
            "leg_count": 2,
            "legs": [
                {
                    "key": "one",
                    "label": "One",
                    "target": "FIGHTER",
                    "method": "KO_TKO",
                    "allowed_methods": ["KO_TKO"],
                },
                {"key": "two", "label": "Two", "target": "FIGHTER"},
            ],
        },
        {
            "leg_count": 2,
            "distinct_methods": True,
            "legs": [
                {"key": "one", "label": "One", "target": "FIGHTER"},
                {"key": "two", "label": "Two", "target": "FIGHTER"},
            ],
        },
    ],
)
def test_invalid_combo_method_contracts_are_rejected(selection):
    with pytest.raises(ValidationError):
        validate_mission_definition(
            base_definition(
                mission_id="CARD-V2-M-013",
                difficulty="MEDIUM",
                xp=4,
                interaction="COMBO_BUILDER",
                pick_effect="UPSERT_MANY",
                selection=selection,
                ui=interactive_ui("METHOD PAIR"),
            )
        )


def test_card_prop_choice_requires_unique_choices():
    definition = validate_mission_definition(
        base_definition(
            mission_id="CARD-V3-E-008",
            interaction="CARD_PROP",
            pick_effect="NONE",
            selection={
                "input": "CHOICE",
                "choices": ["FINISHES", "DECISIONS"],
            },
            ui=interactive_ui("PICK A LANE"),
        )
    )
    assert isinstance(definition, CardPropMissionDefinition)

    invalid = base_definition(
        mission_id="CARD-V3-E-008",
        interaction="CARD_PROP",
        pick_effect="NONE",
        selection={"input": "CHOICE", "choices": ["FINISHES", "FINISHES"]},
        ui=interactive_ui("PICK A LANE"),
    )
    with pytest.raises(ValidationError, match="unique choices"):
        validate_mission_definition(invalid)


def test_card_prop_frozen_and_exact_targets_cannot_fall_back_to_static_values():
    frozen = validate_mission_definition(
        base_definition(
            mission_id="CARD-V2-M-017",
            difficulty="MEDIUM",
            xp=3,
            interaction="CARD_PROP",
            pick_effect="NONE",
            selection={
                "input": "ACCEPT",
                "target_source": "FROZEN_ELIGIBLE_RATIO",
                "frozen_ratio": 0.4,
            },
            evaluation={
                "metric": "card_finish_count",
                "comparator": "GTE",
                "target_source": "OBSERVATION_OVERRIDE",
                "evaluate_on": ["AFTER_EACH_RESULT", "CARD_FINALIZED"],
            },
            ui=interactive_ui("DISPLAYED FINISH LINE"),
        )
    )
    exact = validate_mission_definition(
        base_definition(
            mission_id="CARD-V2-H-019",
            difficulty="HARD",
            xp=10,
            interaction="CARD_PROP",
            pick_effect="NONE",
            selection={
                "input": "EXACT_COUNT",
                "target_source": "SELECTED_EXACT_COUNT",
            },
            evaluation={
                "metric": "card_finish_count",
                "comparator": "EQ",
                "target_source": "OBSERVATION_OVERRIDE",
                "evaluate_on": ["AFTER_EACH_RESULT", "CARD_FINALIZED"],
            },
            ui=interactive_ui("EXACT FINISH COUNT"),
        )
    )

    assert frozen.selection.frozen_ratio == 0.4
    assert exact.selection.max_count is None

    with pytest.raises(ValidationError, match="target sources disagree"):
        validate_mission_definition(
            base_definition(
                mission_id="CARD-V2-M-017",
                difficulty="MEDIUM",
                xp=3,
                interaction="CARD_PROP",
                pick_effect="NONE",
                selection={
                    "input": "ACCEPT",
                    "target_source": "FROZEN_ELIGIBLE_RATIO",
                    "frozen_ratio": 0.4,
                },
                evaluation={
                    "metric": "card_finish_count",
                    "comparator": "GTE",
                    "target": 3,
                    "evaluate_on": ["AFTER_EACH_RESULT", "CARD_FINALIZED"],
                },
                ui=interactive_ui("DISPLAYED FINISH LINE"),
            )
        )


@pytest.mark.parametrize(
    "invalid",
    [
        base_definition(pick_effect="UPSERT_ONE"),
        base_definition(difficulty="EASY", xp=3),
        base_definition(overlap_tags=["Has Spaces"]),
        base_definition(
            mission_id="CARD-V2-H-003",
            difficulty="HARD",
            interaction="TARGET_FIGHTER",
            pick_effect="UPSERT_ONE",
            selection={
                "bound_pick_fields": ["WINNER", "METHOD", "ROUND"],
                "allowed_methods": ["DECISION"],
                "allowed_rounds": [1, 2, 3],
            },
            ui=interactive_ui("EXACT SCRIPT"),
        ),
        base_definition(
            mission_id="CARD-V2-M-011",
            difficulty="MEDIUM",
            interaction="COMBO_BUILDER",
            pick_effect="NONE",
            selection={
                "leg_count": 2,
                "legs": [
                    {"key": "one", "label": "One", "target": "FIGHTER"},
                    {"key": "two", "label": "Two", "target": "FIGHTER"},
                ],
            },
            ui=interactive_ui("WINNER DOUBLE"),
        ),
    ],
)
def test_cross_field_contract_violations_are_rejected(invalid):
    with pytest.raises(ValidationError):
        validate_mission_definition(invalid)


def test_evaluation_and_eligibility_metadata_are_not_free_form():
    with pytest.raises(ValidationError):
        validate_mission_definition(
            base_definition(
                evaluation={
                    "metric": "Not Valid",
                    "comparator": "GTE",
                    "target": 1,
                    "evaluate_on": [],
                }
            )
        )

    with pytest.raises(ValidationError, match="SECTION_ORDER"):
        validate_mission_definition(
            base_definition(
                eligibility={
                    "min_eligible_bouts": 5,
                    "min_main_card_bouts": 4,
                    "capabilities": ["CANONICAL_CARD"],
                }
            )
        )
