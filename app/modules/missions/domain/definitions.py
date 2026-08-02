"""Frozen, discriminated mission-definition schemas.

Definitions are versioned content. They describe UI, eligibility, evaluation
and pick synchronization, but never contain mutable user progress.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    TypeAdapter,
    field_validator,
    model_validator,
)

from app.modules.missions.domain.enums import (
    MissionDifficulty,
    MissionInteractionType,
    PickEffect,
    StringEnum,
)


class MissionDefinitionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MissionCompatibility(StringEnum):
    V1_READY = "V1_READY"
    REQUIRES_UI = "REQUIRES_UI"
    REQUIRES_DATA = "REQUIRES_DATA"


class EvaluationMoment(StringEnum):
    AFTER_EACH_RESULT = "AFTER_EACH_RESULT"
    CARD_FINALIZED = "CARD_FINALIZED"


class MetricComparator(StringEnum):
    EQ = "EQ"
    GTE = "GTE"
    LTE = "LTE"
    GT_OTHER = "GT_OTHER"
    GTE_OTHER = "GTE_OTHER"
    ALL = "ALL"
    CONTAINS_ALL = "CONTAINS_ALL"
    RATIO_GTE = "RATIO_GTE"


class CardCapability(StringEnum):
    CANONICAL_CARD = "CANONICAL_CARD"
    MAIN_EVENT = "MAIN_EVENT"
    CO_MAIN = "CO_MAIN"
    SECTION_ORDER = "SECTION_ORDER"
    TITLE_BOUTS = "TITLE_BOUTS"
    RESULT_METHOD = "RESULT_METHOD"
    RESULT_ROUND = "RESULT_ROUND"
    PICK_POINTS = "PICK_POINTS"
    LEADERBOARD = "LEADERBOARD"


class PickField(StringEnum):
    WINNER = "WINNER"
    METHOD = "METHOD"
    ROUND = "ROUND"


class WinnerBinding(StringEnum):
    SELECTED_FIGHTER = "SELECTED_FIGHTER"
    OPPONENT_OF_SELECTED_FIGHTER = "OPPONENT_OF_SELECTED_FIGHTER"


class WinMethod(StringEnum):
    KO_TKO = "KO_TKO"
    SUBMISSION = "SUBMISSION"
    DECISION = "DECISION"


class TargetFightOutcome(StringEnum):
    FINISH = "FINISH"
    DECISION = "DECISION"
    FINISH_ROUND = "FINISH_ROUND"


class ComboLegTarget(StringEnum):
    FIGHTER = "FIGHTER"
    FIGHT = "FIGHT"


class CardPropInput(StringEnum):
    ACCEPT = "ACCEPT"
    CHOICE = "CHOICE"
    EXACT_COUNT = "EXACT_COUNT"


class EvaluationTargetSource(StringEnum):
    STATIC = "STATIC"
    OBSERVATION_OVERRIDE = "OBSERVATION_OVERRIDE"


class CardPropTargetSource(StringEnum):
    STATIC = "STATIC"
    FROZEN_ELIGIBLE_RATIO = "FROZEN_ELIGIBLE_RATIO"
    SELECTED_EXACT_COUNT = "SELECTED_EXACT_COUNT"


ScalarParameter: TypeAlias = StrictBool | StrictInt | StrictFloat | StrictStr


class MissionUiCopy(MissionDefinitionModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=280)
    progress_template: str = Field(min_length=1, max_length=120)
    selection_prompt: str | None = Field(default=None, max_length=160)


class MissionEvaluationSpec(MissionDefinitionModel):
    metric: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    custom_evaluator_key: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]*$",
        max_length=80,
    )
    comparator: MetricComparator
    target: int | float | None = None
    target_source: EvaluationTargetSource = EvaluationTargetSource.STATIC
    parameters: dict[str, ScalarParameter] = Field(default_factory=dict)
    evaluate_on: tuple[EvaluationMoment, ...]

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
        return value

    @model_validator(mode="after")
    def validate_comparator_target(self):
        numeric_target = {
            MetricComparator.EQ,
            MetricComparator.GTE,
            MetricComparator.LTE,
            MetricComparator.RATIO_GTE,
        }
        if (
            self.comparator in numeric_target
            and self.target_source == EvaluationTargetSource.STATIC
            and self.target is None
        ):
            raise ValueError(f"{self.comparator.value} requires a numeric target")
        if (
            self.target_source == EvaluationTargetSource.OBSERVATION_OVERRIDE
            and self.comparator not in numeric_target
        ):
            raise ValueError("observation target overrides require a numeric comparator")
        if (
            self.target_source == EvaluationTargetSource.OBSERVATION_OVERRIDE
            and self.target is not None
        ):
            raise ValueError("observation target overrides cannot declare a static target")
        if (
            self.comparator == MetricComparator.RATIO_GTE
            and self.target_source == EvaluationTargetSource.STATIC
            and not 0 <= self.target <= 1
        ):
            raise ValueError("RATIO_GTE target must be between 0 and 1")
        return self


class MissionEligibilitySpec(MissionDefinitionModel):
    min_eligible_bouts: int = Field(default=1, ge=1, le=30)
    min_main_card_bouts: int = Field(default=0, ge=0, le=10)
    min_prelim_bouts: int = Field(default=0, ge=0, le=20)
    min_title_bouts: int = Field(default=0, ge=0, le=5)
    capabilities: frozenset[CardCapability] = Field(
        default_factory=lambda: frozenset({CardCapability.CANONICAL_CARD})
    )

    @model_validator(mode="after")
    def validate_capability_requirements(self):
        if self.min_main_card_bouts and CardCapability.SECTION_ORDER not in self.capabilities:
            raise ValueError("main-card requirements need SECTION_ORDER")
        if self.min_prelim_bouts and CardCapability.SECTION_ORDER not in self.capabilities:
            raise ValueError("prelim requirements need SECTION_ORDER")
        if self.min_title_bouts and CardCapability.TITLE_BOUTS not in self.capabilities:
            raise ValueError("title-bout requirements need TITLE_BOUTS")
        return self


class TargetFighterSelectionSpec(MissionDefinitionModel):
    bound_pick_fields: frozenset[PickField]
    winner_binding: WinnerBinding = WinnerBinding.SELECTED_FIGHTER
    allowed_methods: frozenset[WinMethod] = Field(default_factory=frozenset)
    allowed_rounds: tuple[int, ...] = ()
    title_bouts_only: bool = False

    @field_validator("allowed_rounds")
    @classmethod
    def validate_rounds(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if len(value) != len(set(value)) or any(round_ not in range(1, 6) for round_ in value):
            raise ValueError("allowed rounds must be unique UFC rounds 1 through 5")
        return value

    @model_validator(mode="after")
    def validate_bound_fields(self):
        if PickField.WINNER not in self.bound_pick_fields:
            raise ValueError("target-fighter missions must bind the canonical winner")
        if PickField.ROUND in self.bound_pick_fields and PickField.METHOD not in self.bound_pick_fields:
            raise ValueError("a bound round also requires a bound method")
        if PickField.METHOD in self.bound_pick_fields and not self.allowed_methods:
            raise ValueError("a bound method requires allowed_methods")
        if PickField.METHOD not in self.bound_pick_fields and self.allowed_methods:
            raise ValueError("allowed_methods require METHOD in bound_pick_fields")
        if PickField.ROUND in self.bound_pick_fields and not self.allowed_rounds:
            raise ValueError("a bound round requires allowed_rounds")
        if PickField.ROUND not in self.bound_pick_fields and self.allowed_rounds:
            raise ValueError("allowed_rounds require ROUND in bound_pick_fields")
        if PickField.ROUND in self.bound_pick_fields and WinMethod.DECISION in self.allowed_methods:
            raise ValueError("Decision cannot be combined with an exact finish round")
        return self


class TargetFightSelectionSpec(MissionDefinitionModel):
    outcome: TargetFightOutcome
    required_round: int | None = Field(default=None, ge=1, le=5)

    @model_validator(mode="after")
    def validate_required_round(self):
        if self.outcome == TargetFightOutcome.FINISH_ROUND and self.required_round is None:
            raise ValueError("FINISH_ROUND requires required_round")
        if self.outcome != TargetFightOutcome.FINISH_ROUND and self.required_round is not None:
            raise ValueError("required_round is only valid for FINISH_ROUND")
        return self


class ComboLegSpec(MissionDefinitionModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=40)
    label: str = Field(min_length=1, max_length=40)
    target: ComboLegTarget
    method: WinMethod | None = None
    allowed_methods: frozenset[WinMethod] = Field(default_factory=frozenset)
    round: int | None = Field(default=None, ge=1, le=5)
    fight_outcome: TargetFightOutcome | None = None

    @model_validator(mode="after")
    def validate_leg_target(self):
        if self.target == ComboLegTarget.FIGHTER:
            if self.fight_outcome is not None:
                raise ValueError("fighter legs cannot declare fight_outcome")
            if self.method is not None and self.allowed_methods:
                raise ValueError("fighter legs cannot mix fixed and selectable methods")
            if self.round is not None and self.method == WinMethod.DECISION:
                raise ValueError("Decision fighter legs cannot declare a finish round")
            if self.round is not None and WinMethod.DECISION in self.allowed_methods:
                raise ValueError("Decision cannot be selected with an exact finish round")
        else:
            if self.method is not None or self.allowed_methods or self.round is not None:
                raise ValueError("fight legs use fight_outcome instead of method/round")
            if self.fight_outcome is None:
                raise ValueError("fight legs require fight_outcome")
        return self


class ComboSelectionSpec(MissionDefinitionModel):
    leg_count: Literal[2, 3]
    legs: tuple[ComboLegSpec, ...]
    distinct_bouts: bool = True
    distinct_methods: bool = False
    title_bouts_only: bool = False

    @model_validator(mode="after")
    def validate_legs(self):
        if len(self.legs) != self.leg_count:
            raise ValueError("legs length must equal leg_count")
        keys = [leg.key for leg in self.legs]
        if len(keys) != len(set(keys)):
            raise ValueError("combo leg keys must be unique")
        target_types = {leg.target for leg in self.legs}
        if len(target_types) != 1:
            raise ValueError("a combo cannot mix fighter and fight legs")
        if self.distinct_methods and (
            target_types != {ComboLegTarget.FIGHTER}
            or any(not leg.allowed_methods for leg in self.legs)
        ):
            raise ValueError(
                "distinct_methods requires selectable methods on every fighter leg"
            )
        return self


class CardPropSelectionSpec(MissionDefinitionModel):
    input: CardPropInput
    choices: tuple[str, ...] = ()
    max_count: int | None = Field(default=None, ge=0, le=30)
    target_source: CardPropTargetSource = CardPropTargetSource.STATIC
    frozen_ratio: float | None = Field(default=None, gt=0, le=1)

    @model_validator(mode="after")
    def validate_input(self):
        if self.input == CardPropInput.ACCEPT:
            if self.choices or self.max_count is not None:
                raise ValueError("ACCEPT card props take no choices or count")
            if self.target_source == CardPropTargetSource.SELECTED_EXACT_COUNT:
                raise ValueError("ACCEPT card props cannot use a selected exact target")
        elif self.input == CardPropInput.CHOICE:
            if len(self.choices) < 2 or len(self.choices) != len(set(self.choices)):
                raise ValueError("CHOICE card props require at least two unique choices")
            if self.max_count is not None:
                raise ValueError("CHOICE card props do not take max_count")
            if self.target_source != CardPropTargetSource.STATIC:
                raise ValueError("CHOICE card props use a static comparison")
        elif self.target_source != CardPropTargetSource.SELECTED_EXACT_COUNT:
            raise ValueError("EXACT_COUNT card props require a selected exact target")
        if self.target_source == CardPropTargetSource.FROZEN_ELIGIBLE_RATIO:
            if self.frozen_ratio is None:
                raise ValueError("frozen eligible ratios require frozen_ratio")
        elif self.frozen_ratio is not None:
            raise ValueError("frozen_ratio requires FROZEN_ELIGIBLE_RATIO")
        return self


class MissionDefinitionBase(MissionDefinitionModel):
    mission_id: str = Field(pattern=r"^CARD-V[1-9][0-9]*-[EMH]-[0-9]{3}$")
    catalog_version: str = Field(pattern=r"^[0-9]{4}\.[0-9]{2}\.[0-9]{2}$")
    difficulty: MissionDifficulty
    xp: int = Field(ge=1, le=15)
    ui: MissionUiCopy
    evaluation: MissionEvaluationSpec
    eligibility: MissionEligibilitySpec
    compatibility: MissionCompatibility
    overlap_tags: frozenset[str] = Field(min_length=1)

    @field_validator("overlap_tags")
    @classmethod
    def validate_overlap_tags(cls, value: frozenset[str]) -> frozenset[str]:
        if any(not tag or tag.lower() != tag or " " in tag for tag in value):
            raise ValueError("overlap tags must be non-empty lowercase tokens")
        return value

    @model_validator(mode="after")
    def validate_reward_and_interaction(self):
        # Mission IDs are immutable catalog identities. Some reviewed rows were
        # rebalanced after their IDs were assigned, so the historical E/M/H
        # segment must never be interpreted as the current difficulty.
        xp_range = {
            MissionDifficulty.EASY: range(1, 3),
            MissionDifficulty.MEDIUM: range(3, 6),
            MissionDifficulty.HARD: range(6, 16),
        }[self.difficulty]
        if self.xp not in xp_range:
            raise ValueError(
                f"{self.difficulty.value} missions require XP in "
                f"{xp_range.start}..{xp_range.stop - 1}"
            )
        interaction = getattr(self, "interaction", None)
        if interaction == MissionInteractionType.AUTO and self.ui.selection_prompt:
            raise ValueError("AUTO missions cannot expose a selection prompt")
        if interaction not in {None, MissionInteractionType.AUTO} and not self.ui.selection_prompt:
            raise ValueError("interactive missions require a selection prompt")
        return self


class AutoMissionDefinition(MissionDefinitionBase):
    interaction: Literal[MissionInteractionType.AUTO]
    pick_effect: Literal[PickEffect.NONE]
    selection: None = None


class TargetFighterMissionDefinition(MissionDefinitionBase):
    interaction: Literal[MissionInteractionType.TARGET_FIGHTER]
    pick_effect: Literal[PickEffect.UPSERT_ONE]
    selection: TargetFighterSelectionSpec


class TargetFightMissionDefinition(MissionDefinitionBase):
    interaction: Literal[MissionInteractionType.TARGET_FIGHT]
    pick_effect: Literal[PickEffect.NONE]
    selection: TargetFightSelectionSpec


class ComboBuilderMissionDefinition(MissionDefinitionBase):
    interaction: Literal[MissionInteractionType.COMBO_BUILDER]
    pick_effect: PickEffect
    selection: ComboSelectionSpec

    @model_validator(mode="after")
    def validate_pick_effect(self):
        target = self.selection.legs[0].target
        expected = (
            PickEffect.UPSERT_MANY
            if target == ComboLegTarget.FIGHTER
            else PickEffect.NONE
        )
        if self.pick_effect != expected:
            raise ValueError(
                f"{target.value} combo legs require pick_effect {expected.value}"
            )
        return self


class CardPropMissionDefinition(MissionDefinitionBase):
    interaction: Literal[MissionInteractionType.CARD_PROP]
    pick_effect: Literal[PickEffect.NONE]
    selection: CardPropSelectionSpec

    @model_validator(mode="after")
    def validate_target_source(self):
        selection_override = self.selection.target_source in {
            CardPropTargetSource.FROZEN_ELIGIBLE_RATIO,
            CardPropTargetSource.SELECTED_EXACT_COUNT,
        }
        evaluation_override = (
            self.evaluation.target_source
            == EvaluationTargetSource.OBSERVATION_OVERRIDE
        )
        if selection_override != evaluation_override:
            raise ValueError("card-prop selection and evaluation target sources disagree")
        return self


MissionDefinition: TypeAlias = Annotated[
    AutoMissionDefinition
    | TargetFighterMissionDefinition
    | TargetFightMissionDefinition
    | ComboBuilderMissionDefinition
    | CardPropMissionDefinition,
    Field(discriminator="interaction"),
]

MISSION_DEFINITION_ADAPTER = TypeAdapter(MissionDefinition)


def validate_mission_definition(value: object) -> MissionDefinition:
    return MISSION_DEFINITION_ADAPTER.validate_python(value)
