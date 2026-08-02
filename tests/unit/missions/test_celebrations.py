import pytest
from pydantic import ValidationError

from app.modules.missions.domain import (
    CelebrationKind,
    CelebrationPresentation,
    EnqueueCelebrationCommand,
)

XP_ID = "xp_0123456789abcdef01234567"


def command(kind, presentation):
    return EnqueueCelebrationCommand(
        idempotency_key=f"celebration:{kind.value.lower()}",
        xp_entry_id=XP_ID,
        kind=kind,
        presentation=presentation,
        heading="You did it",
        message="Your reward is ready.",
    )


@pytest.mark.parametrize(
    "kind",
    [
        CelebrationKind.LEVEL_UP,
        CelebrationKind.TITLE_UNLOCKED,
        CelebrationKind.STREAK_MILESTONE,
    ],
)
def test_high_value_celebrations_are_full_screen(kind):
    assert command(kind, CelebrationPresentation.FULL_SCREEN).kind == kind
    with pytest.raises(ValidationError, match="must use FULL_SCREEN"):
        command(kind, CelebrationPresentation.TOAST)


def test_mission_completion_is_a_non_blocking_toast():
    value = command(
        CelebrationKind.MISSION_COMPLETED,
        CelebrationPresentation.TOAST,
    )
    assert value.presentation == CelebrationPresentation.TOAST
    with pytest.raises(ValidationError, match="must use TOAST"):
        command(
            CelebrationKind.MISSION_COMPLETED,
            CelebrationPresentation.FULL_SCREEN,
        )
