import pytest

from app.modules.missions.domain import (
    CardCapability,
    FrozenCardFacts,
    MissionDifficulty,
    MissionOverlapPolicy,
    OfferGenerationError,
    generate_mission_offers,
    load_mission_catalog,
)

SECRET = b"offer-generation-test-secret-32-bytes-minimum"


def raw_definition(difficulty: MissionDifficulty, number: int, *, title_only=False):
    letter = {
        MissionDifficulty.EASY: "E",
        MissionDifficulty.MEDIUM: "M",
        MissionDifficulty.HARD: "H",
    }[difficulty]
    mission_id = f"CARD-V9-{letter}-{number:03d}"
    capabilities = ["CANONICAL_CARD"]
    min_title_bouts = 0
    if title_only:
        capabilities.append("TITLE_BOUTS")
        min_title_bouts = 1
    return {
        "mission_id": mission_id,
        "catalog_version": "2026.08.01",
        "difficulty": difficulty.value,
        "xp": {"EASY": 1, "MEDIUM": 3, "HARD": 6}[difficulty.value],
        "ui": {
            "name": f"{difficulty.value} {number}",
            "description": "Correctly predict the required card outcome.",
            "progress_template": "{current} / {target}",
        },
        "evaluation": {
            "metric": "correct_winner_count",
            "comparator": "GTE",
            "target": number,
            "evaluate_on": ["AFTER_EACH_RESULT", "CARD_FINALIZED"],
        },
        "eligibility": {
            "min_eligible_bouts": 1,
            "min_title_bouts": min_title_bouts,
            "capabilities": capabilities,
        },
        "compatibility": "V1_READY",
        "overlap_tags": [difficulty.value.lower(), f"mission-{number}"],
        "interaction": "AUTO",
        "pick_effect": "NONE",
        "selection": None,
    }


def catalog(per_difficulty=6, *, include_ineligible=False):
    values = [
        raw_definition(difficulty, number)
        for difficulty in MissionDifficulty
        for number in range(1, per_difficulty + 1)
    ]
    if include_ineligible:
        values.extend(
            raw_definition(difficulty, 99, title_only=True)
            for difficulty in MissionDifficulty
        )
    return load_mission_catalog(values, expected_version="2026.08.01")


def card(revision=7, eligible_bouts=12):
    return FrozenCardFacts(
        event_id=142997,
        card_revision=revision,
        eligible_bouts=eligible_bouts,
        main_card_bouts=6,
        prelim_bouts=6,
        title_bouts=0,
        capabilities=frozenset({CardCapability.CANONICAL_CARD}),
    )


def mission_ids(offer_set):
    return tuple(
        offer.definition.mission_id
        for slot in offer_set.slots
        for offer in slot.offers
    )


def test_offer_generation_is_retry_stable_and_structurally_complete():
    first = generate_mission_offers(
        catalog=catalog(),
        card=card(),
        user_id="jose",
        secret=SECRET,
    )
    retry = generate_mission_offers(
        catalog=catalog(),
        card=card(),
        user_id="jose",
        secret=SECRET,
    )

    assert retry == first
    assert [slot.slot for slot in first.slots] == [1, 2, 3]
    assert len(set(mission_ids(first))) == 9
    for slot in first.slots:
        assert [offer.definition.difficulty for offer in slot.offers] == [
            MissionDifficulty.EASY,
            MissionDifficulty.MEDIUM,
            MissionDifficulty.HARD,
        ]


def test_offer_identity_is_personalized_and_eligibility_bound():
    """A reorder must not redraw; a genuinely different card must.

    `card_revision` advances on any structural edit, including two prelims
    swapping places, so binding the draw to it rerolled nine missions for
    changes that leave every eligibility input untouched.
    """
    jose = generate_mission_offers(
        catalog=catalog(), card=card(), user_id="jose", secret=SECRET
    )
    chris = generate_mission_offers(
        catalog=catalog(), card=card(), user_id="chris", secret=SECRET
    )
    reordered = generate_mission_offers(
        catalog=catalog(), card=card(revision=8), user_id="jose", secret=SECRET
    )
    resized = generate_mission_offers(
        catalog=catalog(),
        card=card(revision=8, eligible_bouts=8),
        user_id="jose",
        secret=SECRET,
    )

    assert jose.offer_set_id != chris.offer_set_id
    assert jose.slots != chris.slots

    assert reordered.offer_set_id == jose.offer_set_id
    assert reordered.slots == jose.slots
    assert reordered.facts_fingerprint == jose.facts_fingerprint

    assert resized.offer_set_id != jose.offer_set_id
    assert resized.facts_fingerprint != jose.facts_fingerprint


def test_generated_alternatives_respect_overlap_policy():
    policy = MissionOverlapPolicy(max_shared_tags=1)
    result = generate_mission_offers(
        catalog=catalog(),
        card=card(),
        user_id="jose",
        secret=SECRET,
        overlap_policy=policy,
    )

    for slot in result.slots:
        for left_index, left in enumerate(slot.offers):
            for right in slot.offers[left_index + 1 :]:
                assert policy.compare(left.definition, right.definition).conflicts is False


def test_ineligible_definitions_never_enter_offer_set():
    result = generate_mission_offers(
        catalog=catalog(include_ineligible=True),
        card=card(),
        user_id="jose",
        secret=SECRET,
    )

    assert all(not mission_id.endswith("-099") for mission_id in mission_ids(result))


def test_insufficient_pool_and_weak_secret_fail_before_generation():
    with pytest.raises(OfferGenerationError, match="only 2 eligible EASY"):
        generate_mission_offers(
            catalog=catalog(per_difficulty=2),
            card=card(),
            user_id="jose",
            secret=SECRET,
        )
    with pytest.raises(OfferGenerationError, match="at least 32 bytes"):
        generate_mission_offers(
            catalog=catalog(),
            card=card(),
            user_id="jose",
            secret=b"weak",
        )
