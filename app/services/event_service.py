from typing import Optional
from datetime import date

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.repositories.event_repository import EventRepository
from app.repositories.bout_repository import BoutRepository
from app.models.event import Event, EventCardSlot
from app.models.bout import Bout


class EventServiceError(Exception):
    pass


class EventNotFoundError(EventServiceError):
    pass


class EventService:
    def __init__(self, db: AsyncIOMotorDatabase):
        self.event_repo = EventRepository(db)
        self.bout_repo = BoutRepository(db)

    async def get_event(self, event_id: int) -> Event:
        event = await self.event_repo.get_by_id(event_id)
        if not event:
            raise EventNotFoundError(f"Event {event_id} not found")
        return event

    async def get_upcoming_events(self, limit: int = 5) -> list[Event]:
        """Get upcoming scheduled events."""
        return await self.event_repo.get_upcoming(limit)

    async def get_recent_completed(self, limit: int = 5) -> list[Event]:
        """Get recently completed events."""
        return await self.event_repo.get_recent_completed(limit)

    async def get_events_by_status(
        self,
        status: Optional[str] = None,
        limit: int = 20
    ) -> list[Event]:
        """Get events filtered by status."""
        if status == "scheduled":
            return await self.event_repo.get_upcoming(limit)
        elif status == "completed":
            return await self.event_repo.get_recent_completed(limit)
        else:
            # Get both upcoming and recent
            upcoming = await self.event_repo.get_upcoming(limit)
            completed = await self.event_repo.get_recent_completed(limit)
            return upcoming + completed

    async def get_event_bouts(self, event_id: int) -> list[Bout]:
        """Get all bouts for an event."""
        event = await self.event_repo.get_by_id(event_id)
        if not event:
            raise EventNotFoundError(f"Event {event_id} not found")

        return await self.bout_repo.get_by_event(event_id)

    async def get_event_card_structure(self, event_id: int) -> list[EventCardSlot]:
        """Get the card structure (bout order) for an event."""
        event = await self.event_repo.get_by_id(event_id)
        if not event:
            raise EventNotFoundError(f"Event {event_id} not found")

        return await self.event_repo.get_card_structure(event_id)
