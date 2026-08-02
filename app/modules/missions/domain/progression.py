"""Selected XP curve and visual title projection."""

from __future__ import annotations

from math import isqrt

from pydantic import BaseModel, ConfigDict, Field

from app.modules.missions.domain.enums import StringEnum


class ProgressionTitle(StringEnum):
    BUM = "BUM"
    PROSPECT = "PROSPECT"
    RANKED = "RANKED"
    TITLE_CHALLENGER = "TITLE CHALLENGER"
    CHAMPION = "CHAMPION"
    HALL_OF_FAMER = "HALL OF FAMER"
    GOAT = "GOAT"


TITLE_THRESHOLDS: tuple[tuple[int, ProgressionTitle], ...] = (
    (1, ProgressionTitle.BUM),
    (5, ProgressionTitle.PROSPECT),
    (10, ProgressionTitle.RANKED),
    (15, ProgressionTitle.TITLE_CHALLENGER),
    (20, ProgressionTitle.CHAMPION),
    (30, ProgressionTitle.HALL_OF_FAMER),
    (50, ProgressionTitle.GOAT),
)


class ProgressionProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lifetime_xp: int = Field(ge=0)
    level: int = Field(ge=1)
    title: ProgressionTitle
    title_started_at_level: int = Field(ge=1)
    next_title: ProgressionTitle | None
    next_title_level: int | None = Field(default=None, ge=1)
    is_max_title: bool
    level_start_lifetime_xp: int = Field(ge=0)
    xp_into_level: int = Field(ge=0)
    xp_for_next_level: int = Field(ge=5)
    xp_remaining_to_next_level: int = Field(ge=1)
    level_progress_pct: int = Field(ge=0, le=100)


def xp_required_for_level(level: int) -> int:
    if level < 1:
        raise ValueError("level must be at least 1")
    return (level - 1) * (level + 3)


def xp_cost_for_next_level(level: int) -> int:
    if level < 1:
        raise ValueError("level must be at least 1")
    return 2 * level + 3


def level_for_lifetime_xp(lifetime_xp: int) -> int:
    if lifetime_xp < 0:
        raise ValueError("lifetime XP cannot be negative")
    return max(1, isqrt(lifetime_xp + 4) - 1)


def title_for_level(
    level: int,
) -> tuple[int, ProgressionTitle, int | None, ProgressionTitle | None]:
    if level < 1:
        raise ValueError("level must be at least 1")
    current_level, current_title = TITLE_THRESHOLDS[0]
    next_level = None
    next_title = None
    for threshold_level, title in TITLE_THRESHOLDS:
        if threshold_level <= level:
            current_level, current_title = threshold_level, title
            continue
        next_level, next_title = threshold_level, title
        break
    return current_level, current_title, next_level, next_title


def project_progression(lifetime_xp: int) -> ProgressionProjection:
    level = level_for_lifetime_xp(lifetime_xp)
    level_start = xp_required_for_level(level)
    into = lifetime_xp - level_start
    cost = xp_cost_for_next_level(level)
    title_level, title, next_title_level, next_title = title_for_level(level)
    return ProgressionProjection(
        lifetime_xp=lifetime_xp,
        level=level,
        title=title,
        title_started_at_level=title_level,
        next_title=next_title,
        next_title_level=next_title_level,
        is_max_title=next_title is None,
        level_start_lifetime_xp=level_start,
        xp_into_level=into,
        xp_for_next_level=cost,
        xp_remaining_to_next_level=cost - into,
        level_progress_pct=round(into / cost * 100),
    )
