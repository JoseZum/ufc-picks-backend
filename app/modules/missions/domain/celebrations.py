"""Durable celebration commands and queue records."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.modules.missions.domain.enums import CelebrationStatus, StringEnum


class CelebrationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CelebrationKind(StringEnum):
    MISSION_COMPLETED = "MISSION_COMPLETED"
    LEVEL_UP = "LEVEL_UP"
    TITLE_UNLOCKED = "TITLE_UNLOCKED"
    STREAK_MILESTONE = "STREAK_MILESTONE"


class CelebrationPresentation(StringEnum):
    TOAST = "TOAST"
    FULL_SCREEN = "FULL_SCREEN"


FULL_SCREEN_KINDS = frozenset(
    {
        CelebrationKind.LEVEL_UP,
        CelebrationKind.TITLE_UNLOCKED,
        CelebrationKind.STREAK_MILESTONE,
    }
)


class EnqueueCelebrationCommand(CelebrationModel):
    idempotency_key: str = Field(
        min_length=8,
        max_length=160,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    xp_entry_id: str = Field(pattern=r"^xp_[a-f0-9]{24}$")
    kind: CelebrationKind
    presentation: CelebrationPresentation
    heading: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=240)
    metadata: dict[str, str | int | float | bool] = Field(default_factory=dict)

    @model_validator(mode="after")
    def presentation_matches_kind(self):
        expected = (
            CelebrationPresentation.FULL_SCREEN
            if self.kind in FULL_SCREEN_KINDS
            else CelebrationPresentation.TOAST
        )
        if self.presentation != expected:
            raise ValueError(f"{self.kind.value} must use {expected.value}")
        return self


class Celebration(CelebrationModel):
    id: str = Field(alias="_id", pattern=r"^celebration_[a-f0-9]{24}$")
    user_id: str = Field(min_length=1)
    idempotency_key: str
    xp_entry_id: str
    kind: CelebrationKind
    presentation: CelebrationPresentation
    status: CelebrationStatus
    heading: str
    message: str
    metadata: dict[str, str | int | float | bool] = Field(default_factory=dict)
    payload_hash: str
    created_at: datetime
    acknowledged_at: datetime | None = None
    cancelled_at: datetime | None = None

    @field_validator("created_at", "acknowledged_at", "cancelled_at")
    @classmethod
    def normalize_datetime(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def acknowledgement_matches_status(self):
        if self.status == CelebrationStatus.PENDING and self.acknowledged_at is not None:
            raise ValueError("pending celebrations cannot have acknowledged_at")
        if (
            self.status == CelebrationStatus.ACKNOWLEDGED
            and self.acknowledged_at is None
        ):
            raise ValueError("acknowledged celebrations require acknowledged_at")
        if self.status == CelebrationStatus.CANCELLED and self.acknowledged_at is not None:
            raise ValueError("cancelled celebrations cannot be acknowledged")
        if (self.status == CelebrationStatus.CANCELLED) != (self.cancelled_at is not None):
            raise ValueError("only cancelled celebrations require cancelled_at")
        return self
