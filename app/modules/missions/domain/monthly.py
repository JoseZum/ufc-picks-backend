"""Monthly mission definitions and the Admin-owned monthly configuration.

A month has exactly one global mission (D-PROD-010). The definition is versioned
content; the configuration is the Admin's parameter choice for one concrete month
and freezes as soon as that month starts.
"""

from __future__ import annotations

import calendar
import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    TypeAdapter,
    field_validator,
    model_validator,
)

from app.modules.missions.domain.definitions import (
    EvaluationMoment,
    MetricComparator,
    MissionCompatibility,
    MissionUiCopy,
)
from app.modules.missions.domain.enums import MonthlyConfigState, StringEnum

MONTH_KEY_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"

#: D-PROD-010 — the monthly programme starts in August 2026. Earlier months are
#: not configurable, so a mistyped month cannot silently create back-dated state.
FIRST_MONTHLY_MONTH_KEY = "2026-08"

#: D-PROD-010 — every monthly mission is worth exactly 15 XP.
MONTHLY_MISSION_XP = 15


class MonthlyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MonthlyParameterKind(StringEnum):
    COUNT = "COUNT"
    PERCENT = "PERCENT"


class MonthlyAdminParameter(MonthlyModel):
    """One value the Admin fixes before the month starts."""

    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=40)
    label: str = Field(min_length=1, max_length=60)
    kind: MonthlyParameterKind
    default: StrictInt
    minimum: StrictInt
    maximum: StrictInt

    @model_validator(mode="after")
    def validate_bounds(self):
        if self.minimum < 1:
            raise ValueError("monthly parameters must require at least 1")
        if self.maximum < self.minimum:
            raise ValueError("maximum cannot be below minimum")
        if not self.minimum <= self.default <= self.maximum:
            raise ValueError("default must sit inside its own bounds")
        if self.kind == MonthlyParameterKind.PERCENT and self.maximum > 100:
            raise ValueError("percent parameters cannot exceed 100")
        return self


class MonthlyEvaluationSpec(MonthlyModel):
    metric: str = Field(pattern=r"^monthly_[a-z][a-z0-9_]*$")
    comparator: MetricComparator
    #: Which admin parameter supplies the threshold this comparator comes up against.
    #: ``ALL`` missions compare several sub-goals at once and read every parameter,
    #: so they intentionally name none.
    target_parameter: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_]*$", max_length=40
    )
    evaluate_on: tuple[EvaluationMoment, ...]

    @model_validator(mode="after")
    def validate_target_parameter(self):
        multi_goal = self.comparator in {
            MetricComparator.ALL,
            MetricComparator.CONTAINS_ALL,
        }
        if multi_goal and self.target_parameter is not None:
            raise ValueError(
                f"{self.comparator.value} monthly missions compare every parameter "
                "and cannot name a single target"
            )
        if not multi_goal and self.target_parameter is None:
            raise ValueError(
                f"{self.comparator.value} monthly missions require a target parameter"
            )
        return self

    @field_validator("evaluate_on")
    @classmethod
    def require_unique_moments(
        cls,
        value: tuple[EvaluationMoment, ...],
    ) -> tuple[EvaluationMoment, ...]:
        if not value:
            raise ValueError("at least one evaluation moment is required")
        if len(value) != len(set(value)):
            raise ValueError("evaluation moments must be unique")
        if EvaluationMoment.CARD_FINALIZED not in value:
            raise ValueError("monthly missions must settle when a card finalizes")
        return value


class MonthlyEligibilitySpec(MonthlyModel):
    min_resolved_picks: int = Field(default=1, ge=1, le=200)
    min_events: int = Field(default=1, ge=1, le=20)


class MonthlyMissionDefinition(MonthlyModel):
    mission_id: str = Field(pattern=r"^MONTH-V[1-9][0-9]*-[0-9]{3}$")
    catalog_version: str = Field(pattern=r"^[0-9]{4}\.[0-9]{2}\.[0-9]{2}$")
    xp: Literal[15] = MONTHLY_MISSION_XP
    ui: MissionUiCopy
    evaluation: MonthlyEvaluationSpec
    eligibility: MonthlyEligibilitySpec
    admin_parameters: tuple[MonthlyAdminParameter, ...] = Field(min_length=1)
    compatibility: MissionCompatibility
    overlap_tags: frozenset[str] = Field(min_length=1)

    @field_validator("overlap_tags")
    @classmethod
    def validate_overlap_tags(cls, value: frozenset[str]) -> frozenset[str]:
        if any(not tag or tag.lower() != tag or " " in tag for tag in value):
            raise ValueError("overlap tags must be non-empty lowercase tokens")
        return value

    @model_validator(mode="after")
    def validate_parameters(self):
        keys = [parameter.key for parameter in self.admin_parameters]
        if len(keys) != len(set(keys)):
            raise ValueError("admin parameter keys must be unique")
        target = self.evaluation.target_parameter
        if target is not None and target not in keys:
            raise ValueError(
                f"{self.mission_id} compares against unknown parameter {target}"
            )
        if self.ui.selection_prompt:
            raise ValueError("monthly missions are automatic and take no user selection")
        return self

    def parameter(self, key: str) -> MonthlyAdminParameter:
        for parameter in self.admin_parameters:
            if parameter.key == key:
                return parameter
        raise KeyError(key)

    def default_parameters(self) -> dict[str, int]:
        return {
            parameter.key: parameter.default for parameter in self.admin_parameters
        }

    def validate_admin_parameters(self, values: dict[str, int]) -> dict[str, int]:
        """Return the exact, bounded parameter set for this definition."""
        expected = {parameter.key for parameter in self.admin_parameters}
        provided = set(values)
        if missing := sorted(expected - provided):
            raise MonthlyConfigError(
                MonthlyConfigErrorCode.INVALID_PARAMETERS,
                f"Missing monthly parameters: {', '.join(missing)}",
            )
        if unknown := sorted(provided - expected):
            raise MonthlyConfigError(
                MonthlyConfigErrorCode.INVALID_PARAMETERS,
                f"Unknown monthly parameters: {', '.join(unknown)}",
            )
        resolved: dict[str, int] = {}
        for parameter in self.admin_parameters:
            value = values[parameter.key]
            if isinstance(value, bool) or not isinstance(value, int):
                raise MonthlyConfigError(
                    MonthlyConfigErrorCode.INVALID_PARAMETERS,
                    f"{parameter.key} must be an integer",
                )
            if not parameter.minimum <= value <= parameter.maximum:
                raise MonthlyConfigError(
                    MonthlyConfigErrorCode.INVALID_PARAMETERS,
                    f"{parameter.key} must be between {parameter.minimum} "
                    f"and {parameter.maximum}",
                )
            resolved[parameter.key] = value
        return resolved


class MonthlyConfigErrorCode(StringEnum):
    UNKNOWN_MISSION = "UNKNOWN_MISSION"
    INVALID_MONTH = "INVALID_MONTH"
    INVALID_PARAMETERS = "INVALID_PARAMETERS"
    CONFIG_NOT_FOUND = "CONFIG_NOT_FOUND"
    CONFIG_ALREADY_EXISTS = "CONFIG_ALREADY_EXISTS"
    CONFIG_FROZEN = "CONFIG_FROZEN"
    MONTH_ALREADY_STARTED = "MONTH_ALREADY_STARTED"
    MONTH_NOT_FINISHED = "MONTH_NOT_FINISHED"


class MonthlyConfigError(ValueError):
    def __init__(self, code: MonthlyConfigErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class MonthlyMissionConfig(BaseModel):
    """One month's Admin decision, persisted in ``mission_monthly_configs``."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str = Field(alias="_id")
    month_key: str = Field(pattern=MONTH_KEY_PATTERN)
    mission_id: str = Field(pattern=r"^MONTH-V[1-9][0-9]*-[0-9]{3}$")
    catalog_version: str = Field(pattern=r"^[0-9]{4}\.[0-9]{2}\.[0-9]{2}$")
    parameters: dict[str, int]
    xp: Literal[15] = MONTHLY_MISSION_XP
    state: MonthlyConfigState
    starts_at: datetime
    ends_at: datetime
    created_at: datetime
    updated_at: datetime
    activated_at: datetime | None = None
    closed_at: datetime | None = None

    @field_validator(
        "starts_at",
        "ends_at",
        "created_at",
        "updated_at",
        "activated_at",
        "closed_at",
    )
    @classmethod
    def normalize_datetime(cls, value: datetime | None) -> datetime | None:
        # Mongo hands these back naive; the domain always reasons in aware UTC.
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_state_timestamps(self):
        if self.ends_at <= self.starts_at:
            raise ValueError("a month must end after it starts")
        if (self.state == MonthlyConfigState.DRAFT) and self.activated_at is not None:
            raise ValueError("a draft month cannot record an activation")
        if self.state != MonthlyConfigState.DRAFT and self.activated_at is None:
            raise ValueError("an activated month must record when it started")
        if (self.state == MonthlyConfigState.CLOSED) != (self.closed_at is not None):
            raise ValueError("closed months, and only closed months, record a close time")
        return self


def month_bounds(month_key: str) -> tuple[datetime, datetime]:
    """Return the half-open UTC window ``[start, end)`` for ``YYYY-MM``."""
    if not re.fullmatch(MONTH_KEY_PATTERN, month_key):
        raise MonthlyConfigError(
            MonthlyConfigErrorCode.INVALID_MONTH,
            f"Month must look like YYYY-MM, got {month_key!r}",
        )
    if month_key < FIRST_MONTHLY_MONTH_KEY:
        raise MonthlyConfigError(
            MonthlyConfigErrorCode.INVALID_MONTH,
            f"Monthly missions start in {FIRST_MONTHLY_MONTH_KEY}",
        )
    year, month = (int(part) for part in month_key.split("-"))
    start = datetime(year, month, 1, tzinfo=UTC)
    last_day = calendar.monthrange(year, month)[1]
    end = datetime(year, month, last_day, 23, 59, 59, 999999, tzinfo=UTC)
    return start, end


def month_key_for(moment: datetime) -> str:
    """The month a moment belongs to, always evaluated in UTC."""
    if moment.tzinfo is None:
        raise ValueError("month resolution requires an aware datetime")
    utc = moment.astimezone(UTC)
    return f"{utc.year:04d}-{utc.month:02d}"


MONTHLY_DEFINITION_ADAPTER = TypeAdapter(MonthlyMissionDefinition)


def validate_monthly_definition(value: object) -> MonthlyMissionDefinition:
    return MONTHLY_DEFINITION_ADAPTER.validate_python(value)


__all__ = [
    "FIRST_MONTHLY_MONTH_KEY",
    "MONTHLY_MISSION_XP",
    "MONTH_KEY_PATTERN",
    "MonthlyAdminParameter",
    "MonthlyConfigError",
    "MonthlyConfigErrorCode",
    "MonthlyEligibilitySpec",
    "MonthlyEvaluationSpec",
    "MonthlyMissionConfig",
    "MonthlyMissionDefinition",
    "MonthlyParameterKind",
    "month_bounds",
    "month_key_for",
    "validate_monthly_definition",
]
