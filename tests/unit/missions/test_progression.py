import pytest

from app.modules.missions.domain import (
    ProgressionTitle,
    level_for_lifetime_xp,
    project_progression,
    title_for_level,
    xp_cost_for_next_level,
    xp_required_for_level,
)


@pytest.mark.parametrize(
    ("level", "lifetime_xp", "next_level_cost"),
    [
        (1, 0, 5),
        (2, 5, 7),
        (3, 12, 9),
        (4, 21, 11),
        (5, 32, 13),
        (10, 117, 23),
        (50, 2597, 103),
    ],
)
def test_selected_curve_thresholds(level, lifetime_xp, next_level_cost):
    assert xp_required_for_level(level) == lifetime_xp
    assert xp_cost_for_next_level(level) == next_level_cost
    assert level_for_lifetime_xp(lifetime_xp) == level
    if level > 1:
        assert level_for_lifetime_xp(lifetime_xp - 1) == level - 1


@pytest.mark.parametrize(
    ("level", "title", "next_title_level"),
    [
        (1, ProgressionTitle.BUM, 5),
        (4, ProgressionTitle.BUM, 5),
        (5, ProgressionTitle.PROSPECT, 10),
        (10, ProgressionTitle.RANKED, 15),
        (15, ProgressionTitle.TITLE_CHALLENGER, 20),
        (20, ProgressionTitle.CHAMPION, 30),
        (30, ProgressionTitle.HALL_OF_FAMER, 50),
        (50, ProgressionTitle.GOAT, None),
        (75, ProgressionTitle.GOAT, None),
    ],
)
def test_visual_title_boundaries(level, title, next_title_level):
    _, actual_title, actual_next_level, _ = title_for_level(level)

    assert actual_title == title
    assert actual_next_level == next_title_level


def test_projection_exposes_level_and_title_progress():
    projection = project_progression(69)

    assert projection.level == 7
    assert projection.title == ProgressionTitle.PROSPECT
    assert projection.title_started_at_level == 5
    assert projection.next_title == ProgressionTitle.RANKED
    assert projection.next_title_level == 10
    assert projection.level_start_lifetime_xp == 60
    assert projection.xp_into_level == 9
    assert projection.xp_for_next_level == 17
    assert projection.xp_remaining_to_next_level == 8
    assert projection.level_progress_pct == 53


def test_new_user_starts_at_level_one_without_xp():
    projection = project_progression(0)

    assert projection.level == 1
    assert projection.title == ProgressionTitle.BUM
    assert projection.level_progress_pct == 0


def test_negative_xp_and_invalid_levels_are_rejected():
    with pytest.raises(ValueError, match="cannot be negative"):
        project_progression(-1)
    with pytest.raises(ValueError, match="at least 1"):
        xp_required_for_level(0)
    with pytest.raises(ValueError, match="at least 1"):
        title_for_level(0)
