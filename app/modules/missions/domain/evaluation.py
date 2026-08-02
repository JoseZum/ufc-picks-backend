"""Normalized, immutable snapshots consumed by mission metrics."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.modules.missions.domain.definitions import TargetFightOutcome, WinMethod
from app.modules.missions.domain.enums import StringEnum


class EvaluationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CardSection(StringEnum):
    MAIN = "MAIN"
    PRELIM = "PRELIM"
    EARLY_PRELIM = "EARLY_PRELIM"


class BoutRole(StringEnum):
    MAIN_EVENT = "MAIN_EVENT"
    CO_MAIN = "CO_MAIN"
    STANDARD = "STANDARD"


class BoutLifecycle(StringEnum):
    SCHEDULED = "SCHEDULED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    POSTPONED = "POSTPONED"
    REPLACED = "REPLACED"


class BoutOutcome(StringEnum):
    RED_WIN = "RED_WIN"
    BLUE_WIN = "BLUE_WIN"
    DRAW = "DRAW"
    NO_CONTEST = "NO_CONTEST"


class ResultMethodFamily(StringEnum):
    KO_TKO = "KO_TKO"
    SUBMISSION = "SUBMISSION"
    DECISION = "DECISION"
    DQ = "DQ"
    OTHER = "OTHER"


class MetricVoidReason(StringEnum):
    TARGET_CANCELLED = "TARGET_CANCELLED"
    TARGET_POSTPONED = "TARGET_POSTPONED"
    TARGET_REPLACED = "TARGET_REPLACED"
    TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
    TARGET_DRAW_OR_NO_CONTEST = "TARGET_DRAW_OR_NO_CONTEST"
    INSUFFICIENT_SURVIVING_BOUTS = "INSUFFICIENT_SURVIVING_BOUTS"
    LEADERBOARD_UNAVAILABLE = "LEADERBOARD_UNAVAILABLE"


class BoutResultSnapshot(EvaluationModel):
    revision: int = Field(ge=1)
    outcome: BoutOutcome
    winner_fighter_id: str | None = None
    method: ResultMethodFamily
    round: int = Field(ge=1, le=5)

    @model_validator(mode="after")
    def validate_winner(self):
        has_winner = self.outcome in {BoutOutcome.RED_WIN, BoutOutcome.BLUE_WIN}
        if has_winner != (self.winner_fighter_id is not None):
            raise ValueError("wins require a winner; draw/no contest require null")
        return self


class BoutEvaluationSnapshot(EvaluationModel):
    bout_id: int = Field(gt=0)
    fighter_ids: tuple[str, str]
    section: CardSection
    role: BoutRole = BoutRole.STANDARD
    is_title_fight: bool = False
    scheduled_rounds: int = Field(default=3, ge=1, le=5)
    lifecycle: BoutLifecycle
    is_current: bool = True
    matchup_revision: int = Field(ge=1)
    result: BoutResultSnapshot | None = None

    @field_validator("fighter_ids")
    @classmethod
    def validate_fighters(cls, value: tuple[str, str]) -> tuple[str, str]:
        if any(not fighter_id.strip() for fighter_id in value) or len(set(value)) != 2:
            raise ValueError("a bout requires two distinct fighter IDs")
        return value

    @model_validator(mode="after")
    def validate_lifecycle_and_result(self):
        if self.role in {BoutRole.MAIN_EVENT, BoutRole.CO_MAIN} and self.section != CardSection.MAIN:
            raise ValueError("main/co-main roles must belong to the main card")
        if self.lifecycle == BoutLifecycle.COMPLETED and self.result is None:
            raise ValueError("completed bouts require a result")
        if self.lifecycle != BoutLifecycle.COMPLETED and self.result is not None:
            raise ValueError("only completed bouts may expose a result")
        if self.result and self.result.winner_fighter_id not in {*self.fighter_ids, None}:
            raise ValueError("result winner must belong to the bout matchup")
        if self.result and self.result.outcome == BoutOutcome.RED_WIN:
            if self.result.winner_fighter_id != self.fighter_ids[0]:
                raise ValueError("red-win result must name the red fighter")
        if self.result and self.result.outcome == BoutOutcome.BLUE_WIN:
            if self.result.winner_fighter_id != self.fighter_ids[1]:
                raise ValueError("blue-win result must name the blue fighter")
        if self.result and self.result.round > self.scheduled_rounds:
            raise ValueError("result round exceeds scheduled rounds")
        if self.lifecycle in {BoutLifecycle.REPLACED, BoutLifecycle.CANCELLED} and self.is_current:
            raise ValueError("replaced/cancelled bouts cannot remain current")
        return self


class PickEvaluationSnapshot(EvaluationModel):
    bout_id: int = Field(gt=0)
    winner_fighter_id: str = Field(min_length=1)
    method: WinMethod
    round: int | None = Field(default=None, ge=1, le=5)
    points_awarded: int | None = Field(default=None, ge=0, le=3)
    score_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_method_round_and_score(self):
        if self.method == WinMethod.DECISION and self.round is not None:
            raise ValueError("decision picks cannot include a round")
        # Legacy user picks may omit a finish round. Mission-authored picks that
        # bind ROUND are still required to be complete by the selection service.
        if (self.points_awarded is None) != (self.score_revision is None):
            raise ValueError("points and score revision must appear together")
        return self


class FighterMetricLeg(EvaluationModel):
    kind: Literal["FIGHTER"] = "FIGHTER"
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=40)
    bout_id: int = Field(gt=0)
    fighter_id: str = Field(min_length=1)
    method: WinMethod | None = None
    round: int | None = Field(default=None, ge=1, le=5)

    @model_validator(mode="after")
    def validate_method_round(self):
        if self.round is not None and self.method == WinMethod.DECISION:
            raise ValueError("decision legs cannot include a round")
        return self


class FightMetricLeg(EvaluationModel):
    kind: Literal["FIGHT"] = "FIGHT"
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=40)
    bout_id: int = Field(gt=0)
    outcome: TargetFightOutcome
    round: int | None = Field(default=None, ge=1, le=5)

    @model_validator(mode="after")
    def validate_round(self):
        if self.outcome == TargetFightOutcome.FINISH_ROUND and self.round is None:
            raise ValueError("FINISH_ROUND legs require a round")
        if self.outcome != TargetFightOutcome.FINISH_ROUND and self.round is not None:
            raise ValueError("round is only valid for FINISH_ROUND")
        return self


MetricLeg: TypeAlias = Annotated[
    FighterMetricLeg | FightMetricLeg,
    Field(discriminator="kind"),
]


class MetricSelectionSnapshot(EvaluationModel):
    legs: tuple[MetricLeg, ...] = ()
    card_prop_choice: str | None = Field(default=None, min_length=1, max_length=60)
    card_prop_exact_count: int | None = Field(default=None, ge=0, le=30)
    card_prop_frozen_target: int | None = Field(default=None, ge=0, le=30)

    @model_validator(mode="after")
    def validate_shape(self):
        keys = [leg.key for leg in self.legs]
        if len(keys) != len(set(keys)):
            raise ValueError("selection leg keys must be unique")
        inputs = bool(self.legs) + (self.card_prop_choice is not None) + (
            self.card_prop_exact_count is not None
        ) + (self.card_prop_frozen_target is not None)
        if inputs > 1:
            raise ValueError("selection can contain legs, a choice or an exact count")
        return self


class LeaderboardEvaluationSnapshot(EvaluationModel):
    rank: int = Field(ge=1)
    tied_for_first: bool = False
    active_user_count: int = Field(ge=1)
    finalized: bool

    @model_validator(mode="after")
    def validate_tie(self):
        if self.tied_for_first and self.rank != 1:
            raise ValueError("tied_for_first requires rank 1")
        return self


class MissionEvaluationContext(EvaluationModel):
    event_id: int = Field(gt=0)
    user_id: str = Field(min_length=1)
    card_revision: int = Field(ge=1)
    eligibility_revision: int = Field(ge=1)
    eligibility_fingerprint: str = Field(min_length=8)
    frozen_eligible_bout_ids: tuple[int, ...]
    card_finalized: bool = False
    bouts: tuple[BoutEvaluationSnapshot, ...]
    picks: tuple[PickEvaluationSnapshot, ...] = ()
    selection: MetricSelectionSnapshot = Field(default_factory=MetricSelectionSnapshot)
    leaderboard: LeaderboardEvaluationSnapshot | None = None

    @model_validator(mode="after")
    def validate_references_and_revisions(self):
        bout_ids = [bout.bout_id for bout in self.bouts]
        if len(bout_ids) != len(set(bout_ids)):
            raise ValueError("bout IDs must be unique")
        if not self.frozen_eligible_bout_ids:
            raise ValueError("frozen eligibility cannot be empty")
        if len(self.frozen_eligible_bout_ids) != len(set(self.frozen_eligible_bout_ids)):
            raise ValueError("frozen eligible bout IDs must be unique")
        by_id = {bout.bout_id: bout for bout in self.bouts}
        if not set(self.frozen_eligible_bout_ids) <= by_id.keys():
            raise ValueError("frozen eligibility references an unknown bout")
        roles = [bout.role for bout in self.bouts]
        if roles.count(BoutRole.MAIN_EVENT) > 1 or roles.count(BoutRole.CO_MAIN) > 1:
            raise ValueError("a card can have at most one main event and co-main")

        pick_ids = [pick.bout_id for pick in self.picks]
        if len(pick_ids) != len(set(pick_ids)):
            raise ValueError("picks must be unique by bout")
        for pick in self.picks:
            bout = by_id.get(pick.bout_id)
            if bout is None or pick.winner_fighter_id not in bout.fighter_ids:
                raise ValueError("pick target must belong to a known bout")
            if pick.points_awarded is not None:
                if bout.result is None or pick.score_revision != bout.result.revision:
                    raise ValueError("scored picks must match the current result revision")

        for leg in self.selection.legs:
            bout = by_id.get(leg.bout_id)
            if bout is None:
                raise ValueError("selection references an unknown bout")
            if isinstance(leg, FighterMetricLeg) and leg.fighter_id not in bout.fighter_ids:
                raise ValueError("selected fighter must belong to the target bout")

        if self.card_finalized:
            unresolved = [
                bout.bout_id
                for bout in self.surviving_eligible_bouts()
                if bout.result is None
            ]
            if unresolved:
                raise ValueError("a finalized card cannot have unresolved surviving bouts")
            if self.leaderboard is not None and not self.leaderboard.finalized:
                raise ValueError("finalized card cannot carry a provisional leaderboard")
        return self

    def bouts_by_id(self) -> dict[int, BoutEvaluationSnapshot]:
        return {bout.bout_id: bout for bout in self.bouts}

    def picks_by_bout_id(self) -> dict[int, PickEvaluationSnapshot]:
        return {pick.bout_id: pick for pick in self.picks}

    def frozen_bouts(self) -> tuple[BoutEvaluationSnapshot, ...]:
        by_id = self.bouts_by_id()
        return tuple(by_id[bout_id] for bout_id in self.frozen_eligible_bout_ids)

    def surviving_eligible_bouts(self) -> tuple[BoutEvaluationSnapshot, ...]:
        invalid = {
            BoutLifecycle.CANCELLED,
            BoutLifecycle.POSTPONED,
            BoutLifecycle.REPLACED,
        }
        return tuple(
            bout
            for bout in self.frozen_bouts()
            if bout.is_current and bout.lifecycle not in invalid
        )
