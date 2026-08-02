from datetime import UTC, datetime

import pytest

from app.modules.missions.application import MonthlyConfigService
from app.modules.missions.domain import (
    IllegalMissionTransition,
    MissionTransitionReason,
    MonthlyConfigError,
    MonthlyConfigErrorCode,
    MonthlyConfigState,
    MonthlyMissionDefinition,
    validate_monthly_definition,
)
from app.modules.missions.indexes import apply_mission_indexes

BEFORE_AUGUST = datetime(2026, 7, 20, 12, tzinfo=UTC)
INSIDE_AUGUST = datetime(2026, 8, 14, 12, tzinfo=UTC)
AFTER_AUGUST = datetime(2026, 9, 2, 12, tzinfo=UTC)


def definition(
    mission_id: str = "MONTH-V2-001",
    *,
    key: str = "winner_target",
    default: int = 15,
    minimum: int = 5,
    maximum: int = 40,
) -> MonthlyMissionDefinition:
    return validate_monthly_definition(
        {
            "mission_id": mission_id,
            "catalog_version": "2026.08.01",
            "xp": 15,
            "ui": {
                "name": "WIN TARGET",
                "description": "Correctly predict at least N fight winners this month.",
                "progress_template": "{current} / {target} winners",
            },
            "evaluation": {
                "metric": "monthly_correct_winner_count",
                "comparator": "GTE",
                "target_parameter": key,
                "evaluate_on": ["AFTER_EACH_RESULT", "CARD_FINALIZED"],
            },
            "eligibility": {"min_resolved_picks": 1, "min_events": 1},
            "admin_parameters": [
                {
                    "key": key,
                    "label": "Winners required",
                    "kind": "COUNT",
                    "default": default,
                    "minimum": minimum,
                    "maximum": maximum,
                }
            ],
            "compatibility": "V1_READY",
            "overlap_tags": ["monthly", "winners"],
        }
    )


CATALOG = {
    "MONTH-V2-001": definition(),
    "MONTH-V3-008": definition(
        "MONTH-V3-008", key="point_target", default=40, minimum=10, maximum=200
    ),
}


@pytest.fixture
async def service(test_db):
    await apply_mission_indexes(test_db)

    def build(now: datetime = BEFORE_AUGUST) -> MonthlyConfigService:
        return MonthlyConfigService(test_db, catalog=CATALOG, clock=lambda: now)

    return build


async def test_draft_uses_reviewed_defaults_and_freezes_the_month_window(service):
    config = await service().create_draft(
        month_key="2026-08", mission_id="MONTH-V2-001"
    )

    assert config.state == MonthlyConfigState.DRAFT
    assert config.parameters == {"winner_target": 15}
    assert config.xp == 15
    assert config.starts_at == datetime(2026, 8, 1, tzinfo=UTC)
    assert config.ends_at.day == 31
    assert config.activated_at is None


async def test_a_month_accepts_exactly_one_configuration(service):
    await service().create_draft(month_key="2026-08", mission_id="MONTH-V2-001")

    with pytest.raises(MonthlyConfigError) as error:
        await service().create_draft(month_key="2026-08", mission_id="MONTH-V3-008")

    assert error.value.code == MonthlyConfigErrorCode.CONFIG_ALREADY_EXISTS


@pytest.mark.parametrize("value", [4, 41])
async def test_parameters_outside_the_reviewed_bounds_are_rejected(service, value):
    with pytest.raises(MonthlyConfigError) as error:
        await service().create_draft(
            month_key="2026-08",
            mission_id="MONTH-V2-001",
            parameters={"winner_target": value},
        )

    assert error.value.code == MonthlyConfigErrorCode.INVALID_PARAMETERS


async def test_unknown_or_missing_parameters_are_rejected(service):
    with pytest.raises(MonthlyConfigError) as unknown:
        await service().create_draft(
            month_key="2026-08",
            mission_id="MONTH-V2-001",
            parameters={"winner_target": 15, "bonus": 2},
        )
    with pytest.raises(MonthlyConfigError) as missing:
        await service().create_draft(
            month_key="2026-09", mission_id="MONTH-V2-001", parameters={}
        )

    assert unknown.value.code == MonthlyConfigErrorCode.INVALID_PARAMETERS
    assert missing.value.code == MonthlyConfigErrorCode.INVALID_PARAMETERS


async def test_months_before_august_2026_cannot_be_configured(service):
    with pytest.raises(MonthlyConfigError) as error:
        await service().create_draft(month_key="2026-07", mission_id="MONTH-V2-001")

    assert error.value.code == MonthlyConfigErrorCode.INVALID_MONTH


async def test_switching_mission_swaps_to_the_new_parameter_contract(service):
    await service().create_draft(month_key="2026-08", mission_id="MONTH-V2-001")

    updated = await service().update_draft(
        month_key="2026-08", mission_id="MONTH-V3-008"
    )

    assert updated.mission_id == "MONTH-V3-008"
    assert updated.parameters == {"point_target": 40}


async def test_a_draft_stays_editable_after_the_month_technically_began(service):
    """Otherwise August 2026 — the launch month — could never be configured."""
    await service().create_draft(month_key="2026-08", mission_id="MONTH-V2-001")

    updated = await service(INSIDE_AUGUST).update_draft(
        month_key="2026-08", parameters={"winner_target": 5}
    )

    assert updated.parameters == {"winner_target": 5}


async def test_recorded_progress_freezes_the_draft_immediately(service, test_db):
    await service().create_draft(month_key="2026-08", mission_id="MONTH-V2-001")
    await test_db["mission_monthly_progress"].insert_one(
        {"_id": "someone", "user_id": "someone", "month_key": "2026-08"}
    )

    with pytest.raises(MonthlyConfigError) as error:
        await service(INSIDE_AUGUST).update_draft(
            month_key="2026-08", parameters={"winner_target": 5}
        )

    assert error.value.code == MonthlyConfigErrorCode.CONFIG_FROZEN


async def test_activation_is_idempotent_and_freezes_parameters(service):
    await service().create_draft(month_key="2026-08", mission_id="MONTH-V2-001")

    first = await service(INSIDE_AUGUST).activate(month_key="2026-08")
    second = await service(INSIDE_AUGUST).activate(month_key="2026-08")

    assert first.state == MonthlyConfigState.ACTIVE
    assert first.activated_at == INSIDE_AUGUST
    assert second.activated_at == first.activated_at

    with pytest.raises(MonthlyConfigError) as error:
        await service(INSIDE_AUGUST).update_draft(
            month_key="2026-08", parameters={"winner_target": 6}
        )
    assert error.value.code == MonthlyConfigErrorCode.CONFIG_FROZEN


async def test_active_lookup_only_answers_inside_its_own_month(service):
    await service().create_draft(month_key="2026-08", mission_id="MONTH-V2-001")
    await service(INSIDE_AUGUST).activate(month_key="2026-08")

    inside = await service().active_for(INSIDE_AUGUST)
    outside = await service().active_for(AFTER_AUGUST)

    assert inside is not None and inside.month_key == "2026-08"
    assert outside is None


async def test_a_month_cannot_close_before_it_ends_but_admin_may_force_it(service):
    await service().create_draft(month_key="2026-08", mission_id="MONTH-V2-001")
    await service(INSIDE_AUGUST).activate(month_key="2026-08")

    with pytest.raises(MonthlyConfigError) as error:
        await service(INSIDE_AUGUST).close(month_key="2026-08")
    assert error.value.code == MonthlyConfigErrorCode.MONTH_NOT_FINISHED

    forced = await service(INSIDE_AUGUST).close(
        month_key="2026-08", reason=MissionTransitionReason.ADMIN_CLOSE
    )
    assert forced.state == MonthlyConfigState.CLOSED
    assert forced.closed_at == INSIDE_AUGUST


async def test_closing_after_the_month_ends_is_idempotent_and_terminal(service):
    await service().create_draft(month_key="2026-08", mission_id="MONTH-V2-001")
    await service(INSIDE_AUGUST).activate(month_key="2026-08")

    closed = await service(AFTER_AUGUST).close(month_key="2026-08")
    again = await service(AFTER_AUGUST).close(month_key="2026-08")

    assert closed.state == MonthlyConfigState.CLOSED
    assert again.closed_at == closed.closed_at

    with pytest.raises(IllegalMissionTransition):
        await service(AFTER_AUGUST).activate(month_key="2026-08")


async def test_missing_configuration_is_reported_explicitly(service):
    assert await service().get("2026-08") is None

    with pytest.raises(MonthlyConfigError) as error:
        await service().require("2026-08")

    assert error.value.code == MonthlyConfigErrorCode.CONFIG_NOT_FOUND
