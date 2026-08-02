"""Append-only XP ledger command and entry values."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.modules.missions.domain.enums import StringEnum


class XpModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class XpEntryType(StringEnum):
    AWARD = "AWARD"
    COMPENSATION = "COMPENSATION"


class XpSourceType(StringEnum):
    CARD_MISSION = "CARD_MISSION"
    MONTHLY_MISSION = "MONTHLY_MISSION"
    CARD_STREAK = "CARD_STREAK"
    STREAK_MILESTONE = "STREAK_MILESTONE"


class AwardXpCommand(XpModel):
    idempotency_key: str = Field(
        min_length=8,
        max_length=160,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    source_type: XpSourceType
    source_id: str = Field(min_length=1, max_length=160)
    amount: int = Field(gt=0, le=100)
    reason: str = Field(min_length=1, max_length=240)
    metadata: dict[str, str | int | float | bool] = Field(default_factory=dict)


class CompensateXpCommand(XpModel):
    idempotency_key: str = Field(
        min_length=8,
        max_length=160,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    original_entry_id: str = Field(pattern=r"^xp_[a-f0-9]{24}$")
    reason: str = Field(min_length=1, max_length=240)


class XpLedgerEntry(XpModel):
    id: str = Field(alias="_id")
    user_id: str
    idempotency_key: str
    entry_type: XpEntryType
    source_type: XpSourceType
    source_id: str
    amount: int
    reason: str
    payload_hash: str
    compensates_entry_id: str | None = None
    metadata: dict[str, str | int | float | bool] = Field(default_factory=dict)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_sign_and_link(self):
        if self.entry_type == XpEntryType.AWARD:
            if self.amount <= 0 or self.compensates_entry_id is not None:
                raise ValueError("XP awards must be positive and uncompensated")
        elif self.amount >= 0 or self.compensates_entry_id is None:
            raise ValueError("XP compensations must be negative and link an award")
        return self
