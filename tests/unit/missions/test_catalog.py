import json

import pytest

from app.modules.missions.domain import (
    MissionCatalogError,
    MissionDifficulty,
    MissionInteractionType,
    load_mission_catalog,
    load_mission_catalog_file,
)


def definition(mission_id="CARD-V2-E-004", **overrides):
    value = {
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


def interactive_ui():
    return {
        "name": "FINISH LOCK",
        "description": "Choose one fight that will end before a decision.",
        "progress_template": "{result} / finish",
        "selection_prompt": "Choose one fight to finish",
    }


def test_catalog_is_ordered_queryable_and_versioned():
    catalog = load_mission_catalog(
        [
            definition(),
            definition(
                "CARD-V2-M-001",
                difficulty="MEDIUM",
                xp=3,
                interaction="TARGET_FIGHT",
                selection={"outcome": "FINISH"},
                ui=interactive_ui(),
            ),
        ],
        expected_version="2026.08.01",
    )

    assert catalog.version == "2026.08.01"
    assert len(catalog) == 2
    assert catalog[0].mission_id == "CARD-V2-E-004"
    assert catalog.get("CARD-V2-M-001").difficulty == MissionDifficulty.MEDIUM
    assert len(catalog.by_difficulty(MissionDifficulty.EASY)) == 1
    assert len(catalog.by_interaction(MissionInteractionType.TARGET_FIGHT)) == 1


def test_duplicate_ids_are_rejected():
    with pytest.raises(MissionCatalogError, match="Duplicate mission id"):
        load_mission_catalog(
            [definition(), definition()],
            expected_version="2026.08.01",
        )


def test_mixed_catalog_versions_are_rejected():
    with pytest.raises(MissionCatalogError, match="expected 2026.08.01"):
        load_mission_catalog(
            [definition(catalog_version="2026.08.02")],
            expected_version="2026.08.01",
        )


@pytest.mark.parametrize(
    "values",
    [
        [],
        {"definitions": []},
        [definition(interaction="UNKNOWN")],
        [definition(extra_field="forbidden")],
    ],
)
def test_empty_unknown_or_invalid_catalogs_are_rejected(values):
    with pytest.raises(MissionCatalogError):
        load_mission_catalog(values, expected_version="2026.08.01")


def test_unknown_id_is_not_silently_ignored():
    catalog = load_mission_catalog(
        [definition()],
        expected_version="2026.08.01",
    )
    with pytest.raises(MissionCatalogError, match="Unknown mission id"):
        catalog.get("CARD-V2-E-999")


def test_json_file_loader_wraps_io_and_json_errors(tmp_path):
    valid_path = tmp_path / "catalog.json"
    valid_path.write_text(json.dumps([definition()]), encoding="utf-8")
    assert len(
        load_mission_catalog_file(
            valid_path,
            expected_version="2026.08.01",
        )
    ) == 1

    invalid_path = tmp_path / "broken.json"
    invalid_path.write_text("{", encoding="utf-8")
    with pytest.raises(MissionCatalogError, match="broken.json"):
        load_mission_catalog_file(
            invalid_path,
            expected_version="2026.08.01",
        )
