"""
PickService - Business logic for picks.

Handles validation, locking rules, and scoring.

IMPORTANTE: Usamos picked_fighter_name (nombre del peleador) en lugar de
picked_corner para evitar problemas cuando los corners cambian.
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
        self.db = db
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
        - Fighter name is valid (belongs to the bout)
        - Event has not started (picks not locked)
        - Round is only set for non-DEC methods
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

        # Validate fighter name belongs to the bout
        fighters = bout.fighters or {}
        red_fighter = fighters.get("red", {})
        blue_fighter = fighters.get("blue", {})

        red_name = red_fighter.get("fighter_name", "") if isinstance(red_fighter, dict) else getattr(red_fighter, "fighter_name", "")
        blue_name = blue_fighter.get("fighter_name", "") if isinstance(blue_fighter, dict) else getattr(blue_fighter, "fighter_name", "")

        valid_fighters = [
            self._normalize_name(red_name),
            self._normalize_name(blue_name)
        ]

        picked_normalized = self._normalize_name(pick_data.picked_fighter_name)
        if picked_normalized not in valid_fighters:
            raise InvalidPickError(
                f"Fighter '{pick_data.picked_fighter_name}' is not in this bout. "
                f"Valid fighters: {red_name}, {blue_name}"
            )

        # Check if picks are locked (event has started or completed)
        if event.status in ("completed", "cancelled"):
            raise PickLockedError("Cannot modify picks for completed or cancelled events")

        # Check if event or bout are locked by admin
        if getattr(event, "picks_locked", False):
            raise PickLockedError("Picks are locked for this event by admin")

        if getattr(bout, "picks_locked", False):
            raise PickLockedError("Picks are locked for this bout by admin")

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
                picked_fighter_name=pick_data.picked_fighter_name,
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
                picked_fighter_name=pick_data.picked_fighter_name,
                picked_method=pick_data.picked_method,
                picked_round=pick_data.picked_round,
                is_correct=None,
                points_awarded=0,
                locked=False,
                created_at=now,
                updated_at=None
            )
            return await self.pick_repo.create(pick)

    def _normalize_name(self, name: str) -> str:
        """Normalize name for comparison."""
        if not name:
            return ""
        return " ".join(name.lower().strip().split())

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
        """Lock all picks for an event."""
        return await self.pick_repo.lock_picks_for_event(event_id)
