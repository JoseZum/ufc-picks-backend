"""Acceso a datos para la colección de peleas."""

from datetime import datetime
from typing import Optional

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
            raise ValueError(f"Bout with id {bout.id} already exists")

    async def create_many(self, bouts: list[Bout]) -> int:
        """Inserta múltiples peleas (bulk insert desde scraper)"""
        if not bouts:
            return 0

        bouts_dict = [b.model_dump(by_alias=True) for b in bouts]
        result = await self.collection.insert_many(bouts_dict, ordered=False)
        return len(result.inserted_ids)

    # Read

    async def get_by_id(self, bout_id: int) -> Optional[Bout]:
        """Obtiene una pelea por su ID"""
        doc = await self.collection.find_one({"id": bout_id})
        return Bout(**doc) if doc else None

    async def get_by_event(
        self,
        event_id: int,
        status: Optional[str] = None
    ) -> list[Bout]:
        """
        Obtiene todas las peleas de un evento ordenadas correctamente:
        1. Main event primero
        2. Co-main event segundo
        3. Resto de main card por card_order
        4. Prelims por card_order

        Ejemplo: bouts = await repo.get_by_event(event_id=123, status="scheduled")
        """
        query = {"event_id": event_id}
        if status:
            query["status"] = status
        else:
            # Removed/cancelled matchups must not remain on the public card.
            query["status"] = {"$ne": "cancelled"}

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

    async def get_main_event(self, event_id: int) -> Optional[Bout]:
        """
        Obtiene la pelea principal de un evento
        Asume que tienes un campo is_main_event en EventCardSlot
        """
        # Opción 1: Si guardás is_main_event en Bout
        doc = await self.collection.find_one({
            "event_id": event_id,
            "is_main_event": True
        })

        # Opción 2: Si lo tenés en Event.main_event_bout_id
        # Lo manejás desde EventRepository y luego llamás get_by_id()

        return Bout(**doc) if doc else None

    async def search_by_fighter(self, fighter_name: str) -> list[Bout]:
        """Busca peleas por nombre de peleador."""
        query = {
            "$or": [
                {"fighters.red.fighter_name": {"$regex": fighter_name, "$options": "i"}},
                {"fighters.blue.fighter_name": {"$regex": fighter_name, "$options": "i"}},
            ]
        }

        cursor = self.collection.find(query).sort("scraped_at", -1).limit(50)
        docs = await cursor.to_list(length=50)
        return [Bout(**doc) for doc in docs]

    async def get_by_weight_class(
        self,
        weight_class: str,
        gender: str = "male",
        limit: int = 20
    ) -> list[Bout]:
        """Filtra peleas por categoría de peso"""
        query = {
            "weight_class": weight_class,
            "gender": gender,
            "status": "completed"  # Solo peleas finalizadas
        }

        cursor = self.collection.find(query).sort("scraped_at", -1).limit(limit)
        docs = await cursor.to_list(length=limit)
        return [Bout(**doc) for doc in docs]

    async def get_title_fights(
        self,
        event_id: Optional[int] = None
    ) -> list[Bout]:
        """Obtiene peleas por el título"""
        query = {"is_title_fight": True}
        if event_id:
            query["event_id"] = event_id

        cursor = self.collection.find(query).sort("event_id", -1)
        docs = await cursor.to_list(length=None)
        return [Bout(**doc) for doc in docs]

    # Update

    async def update(self, bout_id: int, updates: dict) -> Optional[Bout]:
        """Actualiza campos específicos de una pelea"""
        updates["last_updated"] = datetime.utcnow()

        result = await self.collection.find_one_and_update(
            {"id": bout_id},
            {"$set": updates},
            return_document=True
        )

        return Bout(**result) if result else None

    async def set_result(
        self,
        bout_id: int,
        result: dict
    ) -> Optional[Bout]:
        """
        Actualiza el resultado de una pelea

        result ejemplo:
        {
            "winner": "red",
            "method": "KO/TKO",
            "round": 2,
            "time": "3:42"
        }
        """
        return await self.update(bout_id, {
            "result": result,
            "status": "completed"
        })

    async def update_status(self, bout_id: int, status: str) -> Optional[Bout]:
        """Cambia el estado de una pelea"""
        return await self.update(bout_id, {"status": status})

    # Delete

    async def delete(self, bout_id: int) -> bool:
        """Elimina una pelea"""
        result = await self.collection.delete_one({"id": bout_id})
        return result.deleted_count > 0

    # Aggregations

    async def get_stats_by_weight_class(self) -> list[dict]:
        """Obtiene estadísticas agrupadas por categoría de peso."""
        pipeline = [
            {
                "$group": {
                    "_id": "$weight_class",
                    "total_bouts": {"$sum": 1},
                    "title_fights": {
                        "$sum": {"$cond": ["$is_title_fight", 1, 0]}
                    }
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "weight_class": "$_id",
                    "total_bouts": 1,
                    "title_fights": 1
                }
            },
            {"$sort": {"total_bouts": -1}}
        ]

        cursor = self.collection.aggregate(pipeline)
        return await cursor.to_list(length=None)

    async def get_fighter_record(self, fighter_name: str) -> dict:
        """Calcula el récord de un peleador a partir de resultados guardados."""
        pipeline = [
            {
                "$match": {
                    "$or": [
                        {"fighters.red.fighter_name": fighter_name},
                        {"fighters.blue.fighter_name": fighter_name}
                    ],
                    "status": "completed",
                    "result": {"$exists": True}
                }
            },
            {
                "$project": {
                    "won": {
                        "$cond": [
                            {
                                "$or": [
                                    {
                                        "$and": [
                                            {"$eq": ["$fighters.red.fighter_name", fighter_name]},
                                            {"$eq": ["$result.winner", "red"]}
                                        ]
                                    },
                                    {
                                        "$and": [
                                            {"$eq": ["$fighters.blue.fighter_name", fighter_name]},
                                            {"$eq": ["$result.winner", "blue"]}
                                        ]
                                    }
                                ]
                            },
                            1,
                            0
                        ]
                    }
                }
            },
            {
                "$group": {
                    "_id": None,
                    "total_fights": {"$sum": 1},
                    "wins": {"$sum": "$won"},
                    "losses": {
                        "$sum": {"$cond": [{"$eq": ["$won", 0]}, 1, 0]}
                    }
                }
            }
        ]

        cursor = self.collection.aggregate(pipeline)
        results = await cursor.to_list(length=1)

        if not results:
            return {"total_fights": 0, "wins": 0, "losses": 0}

        return results[0]

    # Utility

    async def count_by_event(self, event_id: int) -> int:
        """Cuenta cuántas peleas tiene un evento"""
        return await self.collection.count_documents({"event_id": event_id})

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

