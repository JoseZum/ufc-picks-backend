"""The single Card Streak (STREAK-001).

One streak per user, Duolingo-style: a card advances it when the user picked a
winner in *more than* half of the card's active bouts before picks closed. There
is no Freeze, grace period, comeback or alternate streak — a covered card
advances, an uncovered one breaks it, and that is the whole rule.

The reviewed reward curve is +1 XP per completed card plus a milestone bonus at
3, 5, 10 and every 5 thereafter.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.modules.missions.domain.enums import StringEnum

#: XP every completed card is worth, independent of milestones.
CARD_STREAK_XP = 1


class CardStreakOutcome(StringEnum):
    ADVANCED = "ADVANCED"
    BROKEN = "BROKEN"
    #: Covered too little, but there was no streak to break.
    UNCHANGED = "UNCHANGED"
    #: The card had no active bouts at all, so it neither advances nor breaks.
    NOT_ELIGIBLE = "NOT_ELIGIBLE"


def milestone_bonus(streak_length: int) -> int | None:
    """The reviewed milestone curve: 3 (+2), 5 (+3), 10 (+5), then every 5 (+3)."""
    if streak_length == 3:
        return 2
    if streak_length == 5:
        return 3
    if streak_length == 10:
        return 5
    if streak_length > 10 and streak_length % 5 == 0:
        return 3
    return None


def next_milestone(current: int) -> tuple[int, int]:
    """The next streak length that pays a bonus, and what it pays.

    Resolved here rather than in React so the surface renders a finished string
    and never re-derives the reward curve (D-ARCH-011).
    """
    candidate = current + 1
    while True:
        bonus = milestone_bonus(candidate)
        if bonus is not None:
            return candidate, bonus
        candidate += 1


def covers_card(*, picked: int, denominator: int) -> bool:
    """More than half — an exact 50% split does not complete the card."""
    if denominator <= 0:
        return False
    return picked * 2 > denominator


class CardStreakDecision(BaseModel):
    """What one card did to one user's streak, and what it is worth."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: CardStreakOutcome
    denominator: int = Field(ge=0)
    picked: int = Field(ge=0)
    coverage_percent: int = Field(ge=0, le=100)
    current_before: int = Field(ge=0)
    current_after: int = Field(ge=0)
    best_before: int = Field(ge=0)
    best_after: int = Field(ge=0)
    card_xp: int = Field(ge=0)
    milestone: int | None = Field(default=None, ge=1)
    milestone_xp: int = Field(ge=0)

    @property
    def total_xp(self) -> int:
        return self.card_xp + self.milestone_xp

    @model_validator(mode="after")
    def rewards_only_follow_an_advance(self):
        if self.outcome != CardStreakOutcome.ADVANCED and self.total_xp:
            raise ValueError("only an advanced card is worth XP")
        if (self.milestone is None) != (self.milestone_xp == 0):
            raise ValueError("a milestone and its bonus travel together")
        if self.best_after < self.best_before:
            raise ValueError("the best streak can never decrease")
        if self.current_after > self.best_after:
            raise ValueError("the current streak cannot exceed the best one")
        return self


def decide_card_streak(
    *,
    current: int,
    best: int,
    picked: int,
    denominator: int,
) -> CardStreakDecision:
    """Apply STREAK-001 to one user on one card.

    `denominator` is the frozen count of active bouts at pick close; `picked` is
    how many of those the user actually picked. Both are inputs — this function
    reads no clock and no database, so the same card always decides the same way.
    """
    if denominator <= 0:
        # A card with nothing to pick is not a card the user can fail.
        return CardStreakDecision(
            outcome=CardStreakOutcome.NOT_ELIGIBLE,
            denominator=0,
            picked=0,
            coverage_percent=0,
            current_before=current,
            current_after=current,
            best_before=best,
            best_after=max(best, current),
            card_xp=0,
            milestone=None,
            milestone_xp=0,
        )

    picked = min(picked, denominator)
    coverage = round(picked / denominator * 100)

    if not covers_card(picked=picked, denominator=denominator):
        return CardStreakDecision(
            outcome=(
                CardStreakOutcome.BROKEN if current > 0 else CardStreakOutcome.UNCHANGED
            ),
            denominator=denominator,
            picked=picked,
            coverage_percent=coverage,
            current_before=current,
            current_after=0,
            best_before=best,
            best_after=max(best, current),
            card_xp=0,
            milestone=None,
            milestone_xp=0,
        )

    advanced = current + 1
    bonus = milestone_bonus(advanced)
    return CardStreakDecision(
        outcome=CardStreakOutcome.ADVANCED,
        denominator=denominator,
        picked=picked,
        coverage_percent=coverage,
        current_before=current,
        current_after=advanced,
        best_before=best,
        best_after=max(best, advanced),
        card_xp=CARD_STREAK_XP,
        milestone=advanced if bonus else None,
        milestone_xp=bonus or 0,
    )


__all__ = [
    "CARD_STREAK_XP",
    "CardStreakDecision",
    "CardStreakOutcome",
    "covers_card",
    "decide_card_streak",
    "milestone_bonus",
    "next_milestone",
]
