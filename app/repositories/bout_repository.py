"""Acceso a datos para la colección de peleas."""

from datetime import datetime
from typing import Any

from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from app.models.bout import Bout


class BoutRepository:
    def __init__(self, db: AsyncDatabase):
        self.db = db
        self.collection = db["bouts"]

    # Create

    async def create(self, bout: Bout) -> Bout:
        """Inserta una nueva pelea"""
        bout_dict = bout.model_dump(by_alias=True)

        try:
            result = await self.collection.insert_one(bout_dict)
            bout_dict["_id"] = result.inserted_id
            return Bout(**bout_dict)
        except DuplicateKeyError:
            raise ValueError(f"Bout with id {bout.id} already exists") from None

    # Read

    async def get_by_id(self, bout_id: int) -> Bout | None:
        """Obtiene una pelea por su ID"""
        doc = await self.collection.find_one({"id": bout_id})
        return Bout(**doc) if doc else None

    async def get_by_event(
        self,
        event_id: int,
        status: str | None = None
    ) -> list[Bout]:
        """
        Obtiene todas las peleas de un evento ordenadas correctamente:
        1. Main event primero
        2. Co-main event segundo
        3. Resto de main card por card_order
        4. Prelims por card_order

        Ejemplo: bouts = await repo.get_by_event(event_id=123, status="scheduled")
        """
        query: dict[str, Any] = {"event_id": event_id}
        if status:
            query["status"] = status
        else:
            # Terminal/non-current matchups remain in canonical history, but
            # must not remain on the public fight card.
            query["status"] = {"$nin": ["cancelled", "postponed", "replaced"]}

        # Orden correcto: main event -> co-main -> main card -> prelims
        # Usamos múltiples criterios de ordenamiento
        cursor = self.collection.find(query).sort([
            ("is_main_event", -1),      # Main event primero (True = -1)
            ("is_co_main_event", -1),   # Co-main segundo
            ("card_section", 1),        # "main" antes que "prelim" alfabéticamente
            ("card_order", 1)           # Orden dentro de cada sección
        ])
        docs = await cursor.to_list(length=None)
        return [Bout(**doc) for doc in docs]

    # Update

    async def update(self, bout_id: int, updates: dict) -> Bout | None:
        """Actualiza campos específicos de una pelea"""
        updates["last_updated"] = datetime.utcnow()

        result = await self.collection.find_one_and_update(
            {"id": bout_id},
            {"$set": updates},
            return_document=True
        )

        return Bout(**result) if result else None

    # Delete

    async def delete(self, bout_id: int) -> bool:
        """Elimina una pelea"""
        result = await self.collection.delete_one({"id": bout_id})
        return result.deleted_count > 0

    # Aggregations

    # Utility

    async def exists(self, bout_id: int) -> bool:
        """Verifica si una pelea existe"""
        count = await self.collection.count_documents({"id": bout_id}, limit=1)
        return count > 0

    async def get_recent_completed(self, limit: int = 10) -> list[Bout]:
        """Obtiene las peleas completadas más recientes"""
        cursor = self.collection.find(
            {"status": "completed"}
        ).sort("last_updated", -1).limit(limit)

        docs = await cursor.to_list(length=limit)
        return [Bout(**doc) for doc in docs]

