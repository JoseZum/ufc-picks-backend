"""
🎯 PickRepository - CRUD para picks de usuarios

Repository para manejar las predicciones de usuarios.
IDs compuestos: user_id:bout_id
"""

from datetime import datetime
from typing import Optional
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError

from app.models.pick import Pick


class PickRepository:
    def __init__(self, db: AsyncIOMotorDatabase):
        self.db = db
        self.collection = db["picks"]

    # ============================================
    # 📌 CREATE
    # ============================================

    async def create(self, pick: Pick) -> Pick:
        """
        Crea un pick
        
        ID compuesto: f"{user_id}:{bout_id}"
        """
        pick_dict = pick.model_dump(by_alias=True)
        
        try:
            await self.collection.insert_one(pick_dict)
            return pick
        except DuplicateKeyError:
            raise ValueError(f"Pick {pick.id} already exists")

    # ============================================
    # 📌 READ
    # ============================================

    async def get_by_id(self, pick_id: str) -> Optional[Pick]:
        """Obtiene un pick por ID compuesto"""
        doc = await self.collection.find_one({"id": pick_id})
        return Pick(**doc) if doc else None

    async def get_user_pick_for_bout(
        self, 
        user_id: str, 
        bout_id: int
    ) -> Optional[Pick]:
        """Obtiene el pick de un usuario para una pelea específica"""
        doc = await self.collection.find_one({
            "user_id": user_id,
            "bout_id": bout_id
        })
        return Pick(**doc) if doc else None

    async def get_user_picks_for_event(
        self, 
        user_id: str, 
        event_id: int
    ) -> list[Pick]:
        """Obtiene todos los picks de un usuario para un evento"""
        cursor = self.collection.find({
            "user_id": user_id,
            "event_id": event_id
        }).sort("created_at", 1)

        docs = await cursor.to_list(length=None)
        return [Pick(**doc) for doc in docs]

    async def get_picks_for_bout(self, bout_id: int) -> list[Pick]:
        """
        🔥 Obtiene TODOS los picks para una pelea
        Útil para calcular estadísticas de la comunidad
        """
        cursor = self.collection.find({"bout_id": bout_id})
        docs = await cursor.to_list(length=None)
        return [Pick(**doc) for doc in docs]

    async def get_user_all_picks(
        self, 
        user_id: str,
        limit: int = 100,
        skip: int = 0
    ) -> list[Pick]:
        """Obtiene todos los picks de un usuario (paginado)"""
        cursor = self.collection.find(
            {"user_id": user_id}
        ).sort("created_at", -1).skip(skip).limit(limit)

        docs = await cursor.to_list(length=limit)
        return [Pick(**doc) for doc in docs]

    # ============================================
    # 📌 UPDATE
    # ============================================

    async def update_result(
        self, 
        pick_id: str, 
        is_correct: bool, 
        points_awarded: int
    ) -> Optional[Pick]:
        """
        Actualiza el resultado de un pick después de la pelea
        
        Llamado por el worker que procesa resultados
        """
        result = await self.collection.find_one_and_update(
            {"id": pick_id},
            {
                "$set": {
                    "is_correct": is_correct,
                    "points_awarded": points_awarded
                }
            },
            return_document=True
        )

        return Pick(**result) if result else None

    async def update_picks_for_bout(
        self, 
        bout_id: int, 
        winner_corner: str,
        points: int = 1
    ) -> int:
        """
        🔥 Actualiza TODOS los picks de una pelea en batch
        
        Marca como correctos los que eligieron el corner ganador
        """
        # Picks correctos
        correct_result = await self.collection.update_many(
            {
                "bout_id": bout_id,
                "picked_corner": winner_corner
            },
            {
                "$set": {
                    "is_correct": True,
                    "points_awarded": points
                }
            }
        )

        # Picks incorrectos
        incorrect_result = await self.collection.update_many(
            {
                "bout_id": bout_id,
                "picked_corner": {"$ne": winner_corner}
            },
            {
                "$set": {
                    "is_correct": False,
                    "points_awarded": 0
                }
            }
        )

        return correct_result.modified_count + incorrect_result.modified_count

    # ============================================
    # 📌 DELETE
    # ============================================

    async def delete(self, pick_id: str) -> bool:
        """Elimina un pick (solo antes del lockeo)"""
        result = await self.collection.delete_one({"id": pick_id})
        return result.deleted_count > 0

    # ============================================
    # 📌 STATS & AGGREGATIONS
    # ============================================

    async def get_user_stats(self, user_id: str) -> dict:
        """
        🔥 Estadísticas de un usuario
        
        Retorna: {
            "total_picks": 100,
            "correct_picks": 65,
            "accuracy": 0.65,
            "total_points": 65
        }
        """
        pipeline = [
            {"$match": {"user_id": user_id, "is_correct": {"$ne": None}}},
            {
                "$group": {
                    "_id": None,
                    "total_picks": {"$sum": 1},
                    "correct_picks": {
                        "$sum": {"$cond": ["$is_correct", 1, 0]}
                    },
                    "total_points": {"$sum": "$points_awarded"}
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "total_picks": 1,
                    "correct_picks": 1,
                    "total_points": 1,
                    "accuracy": {
                        "$cond": [
                            {"$eq": ["$total_picks", 0]},
                            0,
                            {"$divide": ["$correct_picks", "$total_picks"]}
                        ]
                    }
                }
            }
        ]

        cursor = self.collection.aggregate(pipeline)
        results = await cursor.to_list(length=1)

        if not results:
            return {
                "total_picks": 0,
                "correct_picks": 0,
                "accuracy": 0.0,
                "total_points": 0
            }

        return results[0]

    async def get_bout_distribution(self, bout_id: int) -> dict:
        """
        🔥 Distribución de picks para una pelea
        
        Retorna: {
            "red": 245,  # 245 usuarios eligieron red
            "blue": 180,  # 180 usuarios eligieron blue
            "total": 425
        }
        """
        pipeline = [
            {"$match": {"bout_id": bout_id}},
            {
                "$group": {
                    "_id": "$picked_corner",
                    "count": {"$sum": 1}
                }
            }
        ]

        cursor = self.collection.aggregate(pipeline)
        results = await cursor.to_list(length=None)

        distribution = {"red": 0, "blue": 0, "total": 0}
        
        for item in results:
            corner = item["_id"]
            count = item["count"]
            distribution[corner] = count
            distribution["total"] += count

        return distribution

    async def count_user_picks_for_event(
        self, 
        user_id: str, 
        event_id: int
    ) -> int:
        """Cuenta cuántos picks hizo un usuario en un evento"""
        return await self.collection.count_documents({
            "user_id": user_id,
            "event_id": event_id
        })

    async def exists(self, user_id: str, bout_id: int) -> bool:
        """Verifica si un usuario ya hizo pick para una pelea"""
        count = await self.collection.count_documents(
            {"user_id": user_id, "bout_id": bout_id},
            limit=1
        )
        return count > 0


# ============================================
# 🎯 EJEMPLO DE USO
# ============================================

"""
pick_repo = PickRepository(db)

# Crear pick
pick = Pick(
    id="user123:bout456",
    user_id="user123",
    event_id=10,
    bout_id=456,
    picked_corner="red",
    created_at=datetime.utcnow(),
    locked_at=datetime.utcnow()
)
await pick_repo.create(pick)

# Obtener picks de usuario
user_picks = await pick_repo.get_user_picks_for_event("user123", event_id=10)

# Ver distribución
distribution = await pick_repo.get_bout_distribution(bout_id=456)
# {"red": 120, "blue": 80, "total": 200}

# Actualizar resultados después de pelea
await pick_repo.update_picks_for_bout(
    bout_id=456,
    winner_corner="red",
    points=1
)

# Stats del usuario
stats = await pick_repo.get_user_stats("user123")
# {"total_picks": 50, "correct_picks": 32, "accuracy": 0.64, "total_points": 32}
"""
