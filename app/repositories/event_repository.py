"""
📅 EventRepository - CRUD para eventos UFC

Repository para manejar eventos (cards completos)
"""

from datetime import datetime, date
from typing import Optional
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError

from app.models.event import Event, EventCardSlot


class EventRepository:
    def __init__(self, db: AsyncIOMotorDatabase):
        self.db = db
        self.collection = db["events"]
        self.card_slots = db["event_card_slots"]

    # ============================================
    # 📌 CREATE
    # ============================================

    async def create(self, event: Event) -> Event:
        """Crea un evento"""
        event_dict = event.model_dump(by_alias=True)
        
        try:
            await self.collection.insert_one(event_dict)
            return event
        except DuplicateKeyError:
            raise ValueError(f"Event with id {event.id} already exists")

    async def create_card_slot(self, slot: EventCardSlot) -> EventCardSlot:
        """Asigna una pelea a un slot de la cartelera"""
        slot_dict = slot.model_dump(by_alias=True)
        
        try:
            await self.card_slots.insert_one(slot_dict)
            return slot
        except DuplicateKeyError:
            raise ValueError(f"Card slot {slot.id} already exists")

    # ============================================
    # 📌 READ
    # ============================================

    async def get_by_id(self, event_id: int) -> Optional[Event]:
        """Obtiene un evento por ID"""
        doc = await self.collection.find_one({"id": event_id})
        return Event(**doc) if doc else None

    async def get_upcoming(self, limit: int = 5) -> list[Event]:
        """
        Obtiene próximos eventos programados

        Ordenados por fecha ascendente
        """
        # MongoDB necesita datetime, no date
        today = datetime.combine(date.today(), datetime.min.time())

        cursor = self.collection.find({
            "status": "scheduled",
            "date": {"$gte": today}
        }).sort("date", 1).limit(limit)

        docs = await cursor.to_list(length=limit)
        return [Event(**doc) for doc in docs]

    async def get_recent_completed(self, limit: int = 5) -> list[Event]:
        """Obtiene eventos recientes completados"""
        cursor = self.collection.find({
            "status": "completed"
        }).sort("date", -1).limit(limit)

        docs = await cursor.to_list(length=limit)
        return [Event(**doc) for doc in docs]

    async def get_by_date_range(
        self,
        start_date: date,
        end_date: date
    ) -> list[Event]:
        """Obtiene eventos en un rango de fechas"""
        # MongoDB necesita datetime, no date
        start_dt = datetime.combine(start_date, datetime.min.time())
        end_dt = datetime.combine(end_date, datetime.max.time())

        cursor = self.collection.find({
            "date": {
                "$gte": start_dt,
                "$lte": end_dt
            }
        }).sort("date", 1)

        docs = await cursor.to_list(length=None)
        return [Event(**doc) for doc in docs]

    async def get_card_structure(self, event_id: int) -> list[EventCardSlot]:
        """
        🔥 Obtiene la estructura de la cartelera ordenada
        
        Retorna las peleas en orden: main -> co-main -> prelims -> early
        """
        cursor = self.card_slots.find({
            "event_id": event_id
        }).sort("order_overall", 1)

        docs = await cursor.to_list(length=None)
        return [EventCardSlot(**doc) for doc in docs]

    async def get_main_card_bouts(self, event_id: int) -> list[EventCardSlot]:
        """Obtiene solo las peleas del main card"""
        cursor = self.card_slots.find({
            "event_id": event_id,
            "card_section": "main"
        }).sort("order_section", 1)

        docs = await cursor.to_list(length=None)
        return [EventCardSlot(**doc) for doc in docs]

    # ============================================
    # 📌 UPDATE
    # ============================================

    async def update(self, event_id: int, updates: dict) -> Optional[Event]:
        """Actualiza campos de un evento"""
        updates["last_updated"] = datetime.utcnow()

        result = await self.collection.find_one_and_update(
            {"id": event_id},
            {"$set": updates},
            return_document=True
        )

        return Event(**result) if result else None

    async def update_status(self, event_id: int, status: str) -> Optional[Event]:
        """Cambia el estado de un evento"""
        return await self.update(event_id, {"status": status})

    async def update_bout_count(self, event_id: int, total_bouts: int) -> Optional[Event]:
        """Actualiza el conteo de peleas"""
        return await self.update(event_id, {"total_bouts": total_bouts})

    # ============================================
    # 📌 DELETE
    # ============================================

    async def delete(self, event_id: int) -> bool:
        """Elimina un evento"""
        result = await self.collection.delete_one({"id": event_id})
        return result.deleted_count > 0

    async def delete_card_slots(self, event_id: int) -> int:
        """Elimina todos los slots de cartelera de un evento"""
        result = await self.card_slots.delete_many({"event_id": event_id})
        return result.deleted_count

    # ============================================
    # 📌 UTILITY
    # ============================================

    async def exists(self, event_id: int) -> bool:
        """Verifica si existe un evento"""
        count = await self.collection.count_documents({"id": event_id}, limit=1)
        return count > 0

    async def count_upcoming(self) -> int:
        """Cuenta eventos programados próximos"""
        today = datetime.combine(date.today(), datetime.min.time())
        return await self.collection.count_documents({
            "status": "scheduled",
            "date": {"$gte": today}
        })


# ============================================
# 🎯 EJEMPLO DE USO
# ============================================

"""
event_repo = EventRepository(db)

# Obtener próximos eventos
upcoming = await event_repo.get_upcoming(limit=3)

# Obtener estructura de cartelera
card = await event_repo.get_card_structure(event_id=100)

# Crear slot de cartelera
slot = EventCardSlot(
    id="100:12345",
    event_id=100,
    bout_id=12345,
    card_section="main",
    order_overall=1,
    order_section=1,
    is_main_event=True
)
await event_repo.create_card_slot(slot)

# Actualizar estado
await event_repo.update_status(event_id=100, status="completed")
"""
