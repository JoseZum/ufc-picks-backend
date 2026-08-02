"""Mission-selection command payloads."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.missions.domain.definitions import WinMethod
from app.modules.missions.domain.enums import MissionInteractionType, StringEnum


class SelectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FighterCorner(StringEnum):
    RED = "red"
    BLUE = "blue"


class AutoMissionSelection(SelectionModel):
    kind: Literal[MissionInteractionType.AUTO]


class TargetFighterMissionSelection(SelectionModel):
    kind: Literal[MissionInteractionType.TARGET_FIGHTER]
    bout_id: int = Field(gt=0)
    corner: FighterCorner
    method: WinMethod | None = None
    round: int | None = Field(default=None, ge=1, le=5)


class TargetFightMissionSelection(SelectionModel):
    kind: Literal[MissionInteractionType.TARGET_FIGHT]
    bout_id: int = Field(gt=0)


class ComboLegSelection(SelectionModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=40)
    bout_id: int = Field(gt=0)
    corner: FighterCorner | None = None
    method: WinMethod | None = None


class ComboBuilderMissionSelection(SelectionModel):
    kind: Literal[MissionInteractionType.COMBO_BUILDER]
    legs: tuple[ComboLegSelection, ...]


class CardPropMissionSelection(SelectionModel):
    kind: Literal[MissionInteractionType.CARD_PROP]
    choice: str | None = Field(default=None, min_length=1, max_length=60)
    exact_count: int | None = Field(default=None, ge=0, le=30)


MissionSelection: TypeAlias = Annotated[
    AutoMissionSelection
    | TargetFighterMissionSelection
    | TargetFightMissionSelection
    | ComboBuilderMissionSelection
    | CardPropMissionSelection,
    Field(discriminator="kind"),
]


class CanonicalPickPatch(SelectionModel):
    bout_id: int = Field(gt=0)
    winner_corner: FighterCorner | None = None
    method: WinMethod | None = None
    round: int | None = Field(default=None, ge=1, le=5)


class SelectMissionCommand(SelectionModel):
    event_id: int = Field(gt=0)
    slot: Literal[1, 2, 3]
    offer_set_id: str = Field(pattern=r"^offer_set_[a-f0-9]{8,64}$")
    offer_id: str = Field(pattern=r"^offer_[a-f0-9]{8,64}$")
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    selection: MissionSelection
    pick_patches: tuple[CanonicalPickPatch, ...] = ()

    @field_validator("pick_patches")
    @classmethod
    def unique_pick_patches(
        cls,
        value: tuple[CanonicalPickPatch, ...],
    ) -> tuple[CanonicalPickPatch, ...]:
        bout_ids = [patch.bout_id for patch in value]
        if len(bout_ids) != len(set(bout_ids)):
            raise ValueError("pick patches must use unique bout ids")
        return value
