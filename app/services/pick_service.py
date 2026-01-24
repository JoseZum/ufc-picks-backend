"""
PickService - Business logic for picks.

Handles validation, locking rules, and scoring.
"""

from datetime import datetime, timezone
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.repositories.pick_repository import PickRepository
from app.repositories.event_repository import EventRepository
from app.repositories.bout_repository import BoutRepository
from app.models.pick import Pick, PickCreate


class PickServiceError(Exception):
    """Base exception for pick service errors."""
    pass


class PickLockedError(PickServiceError):
    """Raised when trying to modify a locked pick."""
    pass


class EventNotFoundError(PickServiceError):
    """Raised when event is not found."""
    pass


class BoutNotFoundError(PickServiceError):
    """Raised when bout is not found."""
    pass


class InvalidPickError(PickServiceError):
    """Raised when pick data is invalid."""
    pass


class PickService:
    def __init__(self, db: AsyncIOMotorDatabase):
        self.pick_repo = PickRepository(db)
        self.event_repo = EventRepository(db)
        self.bout_repo = BoutRepository(db)

    async def create_or_update_pick(
        self,
        user_id: str,
        pick_data: PickCreate
    ) -> Pick:
        """
        Create or update a pick.

        Validates:
        - Event exists
        - Bout exists and belongs to event
        - Event has not started (picks not locked)
        - Round is only set for non-DEC methods

        Returns the created/updated pick.
        """
        # Validate event exists
        event = await self.event_repo.get_by_id(pick_data.event_id)
        if not event:
            raise EventNotFoundError(f"Event {pick_data.event_id} not found")

        # Validate bout exists and belongs to event
        bout = await self.bout_repo.get_by_id(pick_data.bout_id)
        if not bout:
            raise BoutNotFoundError(f"Bout {pick_data.bout_id} not found")

        if bout.event_id != pick_data.event_id:
            raise InvalidPickError("Bout does not belong to the specified event")

        # Check if picks are locked (event has started or completed)
        if event.status in ("completed", "cancelled"):
            raise PickLockedError("Cannot modify picks for completed or cancelled events")

        # Check if this specific pick is already locked
        existing_pick = await self.pick_repo.get_user_pick_for_bout(user_id, pick_data.bout_id)
        if existing_pick and existing_pick.locked:
            raise PickLockedError("This pick has been locked")

        # Validate round only for non-DEC methods
        if pick_data.picked_method == "DEC" and pick_data.picked_round is not None:
            raise InvalidPickError("Round cannot be specified for DEC method")

        now = datetime.now(timezone.utc)
        pick_id = f"{user_id}:{pick_data.bout_id}"

        if existing_pick:
            # Update existing pick
            return await self.pick_repo.update_pick(
                pick_id=pick_id,
                picked_corner=pick_data.picked_corner,
                picked_method=pick_data.picked_method,
                picked_round=pick_data.picked_round,
                updated_at=now
            )
        else:
            # Create new pick
            pick = Pick(
                _id=pick_id,
                user_id=user_id,
                event_id=pick_data.event_id,
                bout_id=pick_data.bout_id,
                picked_corner=pick_data.picked_corner,
                picked_method=pick_data.picked_method,
                picked_round=pick_data.picked_round,
                is_correct=None,
                points_awarded=0,
                locked=False,
                created_at=now,
                updated_at=None
            )
            return await self.pick_repo.create(pick)

    async def get_user_picks_for_event(
        self,
        user_id: str,
        event_id: int
    ) -> list[Pick]:
        """Get all picks for a user in an event."""
        return await self.pick_repo.get_user_picks_for_event(user_id, event_id)

    async def get_all_user_picks(
        self,
        user_id: str,
        limit: int = 100
    ) -> list[Pick]:
        """Get all picks for a user across all events."""
        return await self.pick_repo.get_user_all_picks(user_id, limit)

    async def get_user_pick_for_bout(
        self,
        user_id: str,
        bout_id: int
    ) -> Optional[Pick]:
        """Get a specific pick."""
        return await self.pick_repo.get_user_pick_for_bout(user_id, bout_id)

    async def lock_picks_for_event(self, event_id: int) -> int:
        """
        Lock all picks for an event.

        Called when event starts.
        Returns number of picks locked.
        """
        return await self.pick_repo.lock_picks_for_event(event_id)

    async def calculate_score(
        self,
        picked_corner: str,
        picked_method: str,
        picked_round: Optional[int],
        result: dict
    ) -> tuple[bool, int]:
        """
        Calculate score for a pick based on result.

        Scoring:
        - Wrong fighter: 0 points
        - Correct fighter only: 1 point
        - Correct fighter + method: 2 points
        - Correct fighter + method + round (non-DEC): 3 points

        Returns: (is_correct, points)
        """
        winner = result.get("winner")
        method = result.get("method")
        result_round = result.get("round")

        is_correct = picked_corner == winner

        if not is_correct:
            return False, 0

        # Normalize method for comparison
        actual_method = self._normalize_method(method)
        method_correct = picked_method == actual_method

        if picked_method == "DEC":
            # DEC picks: max 2 points
            return True, 2 if method_correct else 1
        else:
            # KO/TKO or SUB: can get up to 3 points
            if method_correct and picked_round == result_round:
                return True, 3
            elif method_correct:
                return True, 2
            else:
                return True, 1

    def _normalize_method(self, method: str) -> str:
        """Normalize result method to match pick method format."""
        if not method:
            return "DEC"

        method_upper = method.upper()

        if "KO" in method_upper or "TKO" in method_upper:
            return "KO/TKO"
        elif "SUB" in method_upper:
            return "SUB"
        else:
            return "DEC"
