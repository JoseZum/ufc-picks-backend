"""El único Card Streak (STREAK-001), uno por usuario, estilo Duolingo.

Una card avanza el streak si el usuario acertó el ganador en más de la mitad
de los bouts activos antes del cierre de picks. No hay Freeze, gracia ni
streak alterno: cubierta avanza, no cubierta rompe. Recompensa: +1 XP por
card más bono de hito en 3, 5, 10 y cada 5 en adelante.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.modules.missions.domain.enums import StringEnum

#: XP que vale cada card completada, aparte de los hitos.
CARD_STREAK_XP = 1


class CardStreakOutcome(StringEnum):
    ADVANCED = "ADVANCED"
    BROKEN = "BROKEN"
    #: Cobertura insuficiente, pero no había streak que romper.
    UNCHANGED = "UNCHANGED"
    #: La card no tenía bouts activos, así que ni avanza ni rompe.
    NOT_ELIGIBLE = "NOT_ELIGIBLE"


def milestone_bonus(streak_length: int) -> int | None:
    """Curva de hitos: 3 (+2), 5 (+3), 10 (+5), luego cada 5 (+3)."""
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
    """Próxima longitud de streak que paga bono, y cuánto paga.

    Se resuelve aquí y no en React para que la UI solo renderice el string
    final, sin re-derivar la curva de recompensa (D-ARCH-011).
    """
    candidate = current + 1
    while True:
        bonus = milestone_bonus(candidate)
        if bonus is not None:
            return candidate, bonus
        candidate += 1


def covers_card(*, picked: int, denominator: int) -> bool:
    """Más de la mitad: un empate exacto al 50% no completa la card."""
    if denominator <= 0:
        return False
    return picked * 2 > denominator


class CardStreakDecision(BaseModel):
    """Qué hizo una card al streak de un usuario, y cuánto vale."""

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
    """Aplica STREAK-001 a un usuario en una card.

    `denominator` es el conteo de bouts activos, congelado al cierre de picks.
    Entrada pura, sin reloj ni DB: la misma card siempre decide igual.
    """
    if denominator <= 0:
        # Una card sin nada que elegir no es una card que el usuario pueda fallar.
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
