import pytest

from app.modules.missions.domain import (
    CardCapability,
    EligibilityFailureCode,
    FrozenCardFacts,
    MissionOverlapPolicy,
    eligible_definitions,
    evaluate_definition_eligibility,
    validate_mission_definition,
)


def definition(
    mission_id="CARD-V2-E-004",
    *,
    tags=None,
    eligibility=None,
):
    return validate_mission_definition(
        {
            "mission_id": mission_id,
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
            "eligibility": eligibility
            or {
                "min_eligible_bouts": 1,
                "capabilities": ["CANONICAL_CARD", "MAIN_EVENT"],
            },
            "compatibility": "V1_READY",
            "overlap_tags": tags or ["performance", "winner", "main-event"],
            "interaction": "AUTO",
            "pick_effect": "NONE",
            "selection": None,
        }
    )


def card(**overrides):
    values = {
        "event_id": 142997,
        "card_revision": 7,
        "eligible_bouts": 12,
        "main_card_bouts": 6,
        "prelim_bouts": 6,
        "title_bouts": 0,
        "capabilities": frozenset(
            {
                CardCapability.CANONICAL_CARD,
                CardCapability.MAIN_EVENT,
                CardCapability.CO_MAIN,
                CardCapability.SECTION_ORDER,
                CardCapability.RESULT_METHOD,
                CardCapability.RESULT_ROUND,
                CardCapability.PICK_POINTS,
                CardCapability.LEADERBOARD,
            }
        ),
    }
    values.update(overrides)
    return FrozenCardFacts(**values)


def test_eligible_definition_records_frozen_card_revision():
    decision = evaluate_definition_eligibility(definition(), card())

    assert decision.eligible is True
    assert decision.card_revision == 7
    assert decision.failures == ()


def test_all_missing_capabilities_and_counts_are_explained():
    mission = definition(
        "CARD-V2-E-010",
        eligibility={
            "min_eligible_bouts": 8,
            "min_main_card_bouts": 4,
            "min_prelim_bouts": 5,
            "min_title_bouts": 2,
            "capabilities": [
                "CANONICAL_CARD",
                "SECTION_ORDER",
                "TITLE_BOUTS",
                "RESULT_METHOD",
            ],
        },
    )
    decision = evaluate_definition_eligibility(
        mission,
        card(
            eligible_bouts=6,
            main_card_bouts=3,
            prelim_bouts=3,
            title_bouts=0,
            capabilities=frozenset({CardCapability.CANONICAL_CARD}),
        ),
    )

    assert decision.eligible is False
    assert {failure.code for failure in decision.failures} == {
        EligibilityFailureCode.MISSING_CAPABILITY,
        EligibilityFailureCode.INSUFFICIENT_ELIGIBLE_BOUTS,
        EligibilityFailureCode.INSUFFICIENT_MAIN_CARD_BOUTS,
        EligibilityFailureCode.INSUFFICIENT_PRELIM_BOUTS,
        EligibilityFailureCode.INSUFFICIENT_TITLE_BOUTS,
    }
    missing = [
        failure.required
        for failure in decision.failures
        if failure.code == EligibilityFailureCode.MISSING_CAPABILITY
    ]
    assert missing == ["RESULT_METHOD", "SECTION_ORDER", "TITLE_BOUTS"]


def test_eligible_filter_preserves_catalog_order():
    headliner = definition()
    title = definition(
        "CARD-V2-E-009",
        eligibility={
            "min_eligible_bouts": 1,
            "min_title_bouts": 1,
            "capabilities": ["CANONICAL_CARD", "TITLE_BOUTS"],
        },
    )

    assert eligible_definitions((headliner, title), card()) == (headliner,)


def test_overlap_budget_blocks_similar_but_not_merely_related_missions():
    policy = MissionOverlapPolicy(max_shared_tags=1)
    main = definition(tags=["performance", "winner", "main-event"])
    co_main = definition(
        "CARD-V2-E-005",
        tags=["performance", "winner", "co-main"],
    )
    banker = definition(
        "CARD-V3-E-004",
        tags=["confidence", "winner", "single-fighter"],
    )

    similar = policy.compare(main, co_main)
    related = policy.compare(main, banker)
    same = policy.compare(main, main)

    assert similar.conflicts is True
    assert similar.shared_tags == frozenset({"performance", "winner"})
    assert related.conflicts is False
    assert related.shared_tags == frozenset({"winner"})
    assert same.conflicts is True


def test_frozen_card_fact_invariants_reject_impossible_counts():
    with pytest.raises(ValueError, match="main-card bouts"):
        card(eligible_bouts=4, main_card_bouts=5)
    with pytest.raises(ValueError, match="positive event id"):
        card(event_id=0)
