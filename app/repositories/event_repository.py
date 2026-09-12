"""Acceso a datos para eventos y slots de cartelera."""

from datetime import datetime

from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from app.models.event import Event, EventCardSlot


class EventRepository:
    def __init__(self, db: AsyncDatabase):
        self.db = db
        self.collection = db["events"]
        self.card_slots = db["event_card_slots"]

    @staticmethod
    def _normalize_document(doc: dict) -> dict:
        """Normalize legacy Mongo values before Pydantic validation."""
        if isinstance(doc.get("date"), datetime):
            doc["date"] = doc["date"].date()
        if "main_event_bout_id" in doc and not isinstance(
            doc["main_event_bout_id"],
            int,
        ):
            doc.pop("main_event_bout_id", None)
        return doc

    # Create

    async def create(self, event: Event) -> Event:
        """Crea un evento"""
        event_dict = event.model_dump(by_alias=True)

        try:
            await self.collection.insert_one(event_dict)
            return event
        except DuplicateKeyError:
            raise ValueError(f"Event with id {event.id} already exists") from None

    # Read

    async def get_by_id(self, event_id: int) -> Event | None:
        """Obtiene un evento por ID"""
        doc = await self.collection.find_one({"id": event_id})
        if doc:
            return Event(**self._normalize_document(doc))
        return None

    async def get_upcoming(self, limit: int = 5) -> list[Event]:
        """
        Obtiene eventos programados (incluyendo los que ya pasaron su datetime pero no fueron completados)

        Ordenados por fecha ascendente
        """
        cursor = self.collection.find({
            "status": "scheduled",
        }).sort("date", 1).limit(limit)

        docs = await cursor.to_list(length=limit)

        return [Event(**self._normalize_document(doc)) for doc in docs]

    async def get_recent_completed(self, limit: int = 5) -> list[Event]:
        """Obtiene eventos recientes completados"""
        cursor = self.collection.find({
            "status": {"$in": ["completed", "cancelled"]}
        }).sort("date", -1).limit(limit)

        docs = await cursor.to_list(length=limit)

        return [Event(**self._normalize_document(doc)) for doc in docs]

    async def get_card_structure(self, event_id: int) -> list[EventCardSlot]:
        """Obtiene la estructura de cartelera en orden."""
        cursor = self.card_slots.find({
            "event_id": event_id
        }).sort("order_overall", 1)

        docs = await cursor.to_list(length=None)
        return [EventCardSlot(**doc) for doc in docs]

    # Update

    async def update(self, event_id: int, updates: dict) -> Event | None:
        """Actualiza campos de un evento"""
        updates["last_updated"] = datetime.utcnow()

        result = await self.collection.find_one_and_update(
            {"id": event_id},
            {"$set": updates},
            return_document=True
        )

        return Event(**result) if result else None

    # Delete

    async def delete(self, event_id: int) -> bool:
        """Elimina un evento"""
        result = await self.collection.delete_one({"id": event_id})
        return result.deleted_count > 0

    # Utility

    async def exists(self, event_id: int) -> bool:
        """Verifica si existe un evento"""
        count = await self.collection.count_documents({"id": event_id}, limit=1)
        return count > 0

