"""Conformance for the 18 reviewed monthly mission templates."""

import pytest

from app.modules.missions.catalog import load_monthly_catalog
from app.modules.missions.domain import (
    MONTHLY_MISSION_XP,
    MonthlyConfigError,
    MonthlyConfigErrorCode,
    MonthlyMissionDefinition,
)

CATALOG = load_monthly_catalog()

#: Exactly the IDs Jose approved on the Monthly Missions sheet.
APPROVED_IDS = (
    "MONTH-V2-001",
    "MONTH-V2-003",
    "MONTH-V2-004",
    "MONTH-V2-006",
    "MONTH-V2-007",
    "MONTH-V2-008",
    "MONTH-V2-009",
    "MONTH-V2-010",
    "MONTH-V2-013",
    "MONTH-V2-014",
    "MONTH-V2-015",
    "MONTH-V2-019",
    "MONTH-V3-001",
    "MONTH-V3-002",
    "MONTH-V3-003",
    "MONTH-V3-008",
    "MONTH-V3-009",
    "MONTH-V3-010",
)


def test_catalog_is_exactly_the_18_approved_templates():
    assert tuple(sorted(CATALOG)) == tuple(sorted(APPROVED_IDS))
    assert len(CATALOG) == 18


@pytest.mark.parametrize("mission_id", APPROVED_IDS)
def test_every_monthly_mission_is_worth_fifteen_xp(mission_id: str):
    assert CATALOG[mission_id].xp == MONTHLY_MISSION_XP == 15


@pytest.mark.parametrize("mission_id", APPROVED_IDS)
def test_every_monthly_mission_is_automatic_and_settles_at_card_close(mission_id: str):
    definition = CATALOG[mission_id]
    assert definition.ui.selection_prompt is None
    assert "CARD_FINALIZED" in [moment.value for moment in definition.evaluation.evaluate_on]


@pytest.mark.parametrize("mission_id", APPROVED_IDS)
def test_every_monthly_mission_has_usable_admin_defaults(mission_id: str):
    definition = CATALOG[mission_id]
    defaults = definition.default_parameters()

    assert defaults
    assert definition.validate_admin_parameters(defaults) == defaults
    for parameter in definition.admin_parameters:
        assert parameter.minimum <= defaults[parameter.key] <= parameter.maximum


@pytest.mark.parametrize("mission_id", APPROVED_IDS)
def test_progress_copy_only_uses_tokens_the_parameters_can_fill(mission_id: str):
    definition = CATALOG[mission_id]
    template = definition.ui.progress_template
    known = {
        "current",
        "target",
        "correct",
        "resolved",
        "accuracy",
    } | {parameter.key for parameter in definition.admin_parameters} | {
        "ko",
        "sub",
        "decision",
    }
    used = {
        token.split("}")[0]
        for token in template.split("{")[1:]
    }

    assert used <= known, (mission_id, sorted(used - known))
    assert template.strip()


@pytest.mark.parametrize("mission_id", APPROVED_IDS)
def test_parameters_outside_their_bounds_are_rejected(mission_id: str):
    definition: MonthlyMissionDefinition = CATALOG[mission_id]
    for parameter in definition.admin_parameters:
        for bad in (parameter.minimum - 1, parameter.maximum + 1):
            values = definition.default_parameters() | {parameter.key: bad}
            with pytest.raises(MonthlyConfigError) as error:
                definition.validate_admin_parameters(values)
            assert error.value.code == MonthlyConfigErrorCode.INVALID_PARAMETERS


def test_single_target_missions_name_the_parameter_they_compare_against():
    for mission_id, definition in CATALOG.items():
        spec = definition.evaluation
        keys = {parameter.key for parameter in definition.admin_parameters}
        if spec.comparator.value == "ALL":
            assert spec.target_parameter is None, mission_id
            assert len(keys) > 1, mission_id
        else:
            assert spec.target_parameter in keys, mission_id


def test_metric_names_are_monthly_scoped_and_unique_per_contract():
    metrics = [definition.evaluation.metric for definition in CATALOG.values()]

    assert all(metric.startswith("monthly_") for metric in metrics)
    # ROUND COLLECTOR intentionally reuses the perfect-pick metric with a
    # different Admin threshold, so metrics are not globally unique.
    assert metrics.count("monthly_perfect_pick_count") == 2
    assert len(set(metrics)) == 17
