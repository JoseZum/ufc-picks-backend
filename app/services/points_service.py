"""
Servicio de Puntos - Calcula y asigna puntos por picks correctos
"""

from typing import Dict, Any, List
from motor.motor_asyncio import AsyncIOMotorDatabase


class PointsService:
    """
    Servicio para calcular y asignar puntos por picks.

    Sistema de puntos:
    - 1 punto: Acertar el ganador
    - +1 punto adicional: Acertar el método (KO/TKO, SUB, DEC)
    - +1 punto adicional: Acertar el round exacto

    Total posible: 3 puntos por pelea
    """

    def __init__(self, db: AsyncIOMotorDatabase):
        self.db = db

    def normalize_method(self, method: str) -> str:
        """Normalizar método a formato estándar"""
        method_upper = method.upper()
        if method_upper in ["KO", "TKO", "KO/TKO"]:
            return "KO/TKO"
        elif method_upper in ["SUB", "SUBMISSION"]:
            return "SUB"
        elif method_upper in ["DEC", "DECISION"]:
            return "DEC"
        else:
            return method_upper

    async def calculate_points(
        self,
        pick: Dict[str, Any],
        result: Dict[str, Any]
    ) -> int:
        """
        Calcular puntos para un pick basado en el resultado.

        Args:
            pick: Dict con picked_corner, picked_method, picked_round
            result: Dict con winner, method, round

        Returns:
            Puntos ganados (0-3)
        """
        points = 0

        # Si el resultado es draw o NC, nadie gana puntos
        if result.get("winner") is None:
            return 0

        # 1 punto por acertar ganador
        if pick["picked_corner"] == result["winner"]:
            points += 1

            # +1 punto por acertar método (solo si acertó ganador)
            pick_method = self.normalize_method(pick["picked_method"])
            result_method = self.normalize_method(result["method"])

            if pick_method == result_method:
                points += 1

                # +1 punto por acertar round (solo si acertó ganador y método)
                if pick.get("picked_round") and result.get("round"):
                    if pick["picked_round"] == result["round"]:
                        points += 1

        return points

    async def calculate_and_assign_points(
        self,
        bout_id: int,
        result: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Calcular y asignar puntos a todos los picks de un bout.

        1. Busca todos los picks para el bout
        2. Calcula puntos para cada pick
        3. Actualiza los picks con puntos y is_correct
        4. Actualiza estadísticas de usuarios
        5. Actualiza leaderboards

        Returns:
            Dict con estadísticas de puntos asignados
        """
        # Buscar todos los picks para este bout
        picks_cursor = self.db["picks"].find({"bout_id": bout_id})
        picks = await picks_cursor.to_list(length=None)

        if not picks:
            return {
                "picks_processed": 0,
                "points_distributed": 0
            }

        picks_updated = 0
        total_points = 0
        users_affected = set()

        # Procesar cada pick
        for pick in picks:
            # Calcular puntos
            points = await self.calculate_points(pick, result)

            # Determinar si es correcto (acertó ganador)
            is_correct = False
            if result.get("winner"):
                is_correct = pick["picked_corner"] == result["winner"]

            # Actualizar pick
            await self.db["picks"].update_one(
                {"_id": pick["_id"]},
                {
                    "$set": {
                        "points_awarded": points,
                        "is_correct": is_correct
                    }
                }
            )

            picks_updated += 1
            total_points += points
            users_affected.add(pick["user_id"])

        # Actualizar estadísticas de usuarios y leaderboards
        for user_id in users_affected:
            await self._update_user_stats(user_id)

        return {
            "picks_processed": picks_updated,
            "points_distributed": total_points,
            "users_affected": len(users_affected)
        }

    async def revert_points(self, bout_id: int):
        """
        Revertir puntos asignados para un bout (si se elimina resultado).

        1. Resetea points_awarded y is_correct en todos los picks
        2. Recalcula estadísticas de usuarios
        """
        # Obtener usuarios afectados antes de resetear
        picks_cursor = self.db["picks"].find({"bout_id": bout_id})
        picks = await picks_cursor.to_list(length=None)
        users_affected = set(pick["user_id"] for pick in picks)

        # Resetear picks
        await self.db["picks"].update_many(
            {"bout_id": bout_id},
            {
                "$set": {
                    "points_awarded": 0,
                    "is_correct": None
                }
            }
        )

        # Recalcular stats de usuarios
        for user_id in users_affected:
            await self._update_user_stats(user_id)

    async def _update_user_stats(self, user_id: str):
        """
        Recalcular y actualizar estadísticas de un usuario en leaderboard.

        Calcula:
        - total_points: Suma de todos los puntos
        - picks_total: Total de picks hechos
        - picks_correct: Picks correctos (acertó ganador)
        - accuracy: Porcentaje de acierto
        """
        # Obtener todos los picks del usuario
        picks_cursor = self.db["picks"].find({"user_id": user_id})
        picks = await picks_cursor.to_list(length=None)

        # Calcular estadísticas
        total_points = sum(pick.get("points_awarded", 0) for pick in picks)
        picks_total = len(picks)
        picks_correct = sum(1 for pick in picks if pick.get("is_correct") is True)
        accuracy = (picks_correct / picks_total * 100) if picks_total > 0 else 0

        # Obtener info del usuario
        user = await self.db["users"].find_one({"_id": user_id})
        if not user:
            return

        # Actualizar en leaderboard global
        await self.db["leaderboard"].update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "username": user["name"],
                    "avatar_url": user.get("profile_picture"),
                    "total_points": total_points,
                    "picks_total": picks_total,
                    "picks_correct": picks_correct,
                    "accuracy": round(accuracy, 2),
                    "updated_at": user.get("last_login_at")
                }
            },
            upsert=True
        )

        # Recalcular rankings (posiciones) en el leaderboard
        await self._recalculate_rankings()

    async def _recalculate_rankings(self):
        """
        Recalcular rankings (posiciones) de todos los usuarios.

        Ordena por:
        1. total_points (descendente)
        2. accuracy (descendente) - como desempate
        3. picks_total (ascendente) - menos picks es mejor si empatan en todo
        """
        # Obtener todos los entries ordenados
        cursor = self.db["leaderboard"].find().sort([
            ("total_points", -1),
            ("accuracy", -1),
            ("picks_total", 1)
        ])
        entries = await cursor.to_list(length=None)

        # Asignar rankings
        for rank, entry in enumerate(entries, start=1):
            await self.db["leaderboard"].update_one(
                {"_id": entry["_id"]},
                {"$set": {"rank": rank}}
            )
