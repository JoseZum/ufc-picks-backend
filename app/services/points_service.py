"""
Servicio de Puntos - Calcula y asigna puntos por picks correctos.

Sistema de puntos:
- 1 punto por acertar el ganador
- +1 punto por acertar el método (KO/TKO, SUB, DEC)
- +1 punto por acertar el round exacto (máx 3 puntos por pelea)

Nota: Comparamos por NOMBRE del peleador, no por corner, para evitar
problemas cuando los datos cambian en los scrapes.
"""

from typing import Dict, Any
from motor.motor_asyncio import AsyncIOMotorDatabase


class PointsService:
    """Calcula y asigna puntos, actualiza estadísticas de usuarios."""

    def __init__(self, db: AsyncIOMotorDatabase):
        self.db = db

    def normalize_method(self, method: str) -> str:
        """Normaliza los métodos de victoria a formato estándar."""
        if not method:
            return ""
        method_upper = method.upper()
        if method_upper in ["KO", "TKO", "KO/TKO"]:
            return "KO/TKO"
        elif method_upper in ["SUB", "SUBMISSION"]:
            return "SUB"
        elif method_upper in ["DEC", "DECISION"]:
            return "DEC"
        else:
            return method_upper

    def normalize_name(self, name: str) -> str:
        """Normaliza nombres para comparación (lowercase, sin espacios extras)."""
        if not name:
            return ""
        return " ".join(name.lower().strip().split())

    async def calculate_points(
        self,
        pick: Dict[str, Any],
        winner_name: str,
        result_method: str,
        result_round: int = None
    ) -> int:
        """Calcula los puntos para un pick basado en el resultado de la pelea."""
        points = 0

        # Sin ganador, sin puntos
        if not winner_name:
            return 0

        picked_name = self.normalize_name(pick.get("picked_fighter_name", ""))
        winner_normalized = self.normalize_name(winner_name)

        # 1 punto por acertar el ganador
        if picked_name == winner_normalized:
            points += 1

            # +1 punto por acertar el método (si acertó el ganador)
            pick_method = self.normalize_method(pick.get("picked_method", ""))
            normalized_result_method = self.normalize_method(result_method)

            if pick_method == normalized_result_method:
                points += 1

                # +1 punto por acertar el round (si acertó ganador y método)
                if pick.get("picked_round") and result_round:
                    if pick["picked_round"] == result_round:
                        points += 1

        return points

    async def calculate_and_assign_points(
        self,
        bout_id: int,
        result: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Calcula y asigna puntos a todos los picks de una pelea.
        Luego actualiza las stats de usuarios afectados.
        """
        # Obtener todos los picks para esta pelea
        picks_cursor = self.db["picks"].find({"bout_id": bout_id})
        picks = await picks_cursor.to_list(length=None)

        if not picks:
            return {
                "picks_processed": 0,
                "points_distributed": 0
            }

        # Obtener la pelea para extraer información de fighters
        bout = await self.db["bouts"].find_one({"id": bout_id})
        if not bout:
            return {
                "picks_processed": 0,
                "points_distributed": 0,
                "error": "Pelea no encontrada"
            }

        # Extraer el nombre del ganador
        winner_name = result.get("winner_name")

        # Soporte legacy: si viene corner en lugar de nombre
        if not winner_name and result.get("winner"):
            winner_corner = result["winner"]
            fighters = bout.get("fighters", {})
            if winner_corner in ["red", "blue"]:
                winner_data = fighters.get(winner_corner, {})
                winner_name = winner_data.get("fighter_name")

        if not winner_name:
            return {
                "picks_processed": 0,
                "points_distributed": 0,
                "error": "No se pudo determinar el ganador"
            }

        result_method = result.get("method", "")
        result_round = result.get("round")

        picks_updated = 0
        total_points = 0
        users_affected = set()

        # Procesar cada pick y calcular puntos
        for pick in picks:
            points = await self.calculate_points(
                pick, winner_name, result_method, result_round
            )

            # Determinar si el pick fue correcto
            picked_name = self.normalize_name(pick.get("picked_fighter_name", ""))
            winner_normalized = self.normalize_name(winner_name)
            is_correct = picked_name == winner_normalized

            # Guardar puntos en el pick
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

        # Recalcular stats de cada usuario afectado
        for user_id in users_affected:
            await self._update_user_stats(user_id)

        return {
            "picks_processed": picks_updated,
            "points_distributed": total_points,
            "users_affected": len(users_affected),
            "winner_name": winner_name
        }

    async def revert_points(self, bout_id: int):
        """Revierte los puntos de una pelea (se usa cuando se cancela o corrige resultado)."""
        # Recopilar usuarios afectados antes de limpiar
        picks_cursor = self.db["picks"].find({"bout_id": bout_id})
        picks = await picks_cursor.to_list(length=None)
        users_affected = set(pick["user_id"] for pick in picks)

        # Limpiar puntos y resultado de los picks
        await self.db["picks"].update_many(
            {"bout_id": bout_id},
            {
                "$set": {
                    "points_awarded": 0,
                    "is_correct": None
                }
            }
        )

        # Recalcular stats para cada usuario
        for user_id in users_affected:
            await self._update_user_stats(user_id)

    async def _update_user_stats(self, user_id: str):
        """Recalcula todas las estadísticas del usuario basadas en sus picks."""
        picks_cursor = self.db["picks"].find({"user_id": user_id})
        picks = await picks_cursor.to_list(length=None)

        total_points = 0
        picks_total = len(picks)
        picks_correct = 0
        perfect_picks = 0
        picks_evaluated = 0

        for pick in picks:
            total_points += pick.get("points_awarded", 0)

            # Contar solo picks que ya tienen resultado
            if pick.get("is_correct") is not None:
                picks_evaluated += 1
                if pick.get("is_correct") is True:
                    picks_correct += 1

            # Picks perfectos (3 puntos)
            if pick.get("points_awarded", 0) == 3:
                perfect_picks += 1

        # Precisión: picks correctos / picks evaluados (no sobre total)
        accuracy = (picks_correct / picks_evaluated) if picks_evaluated > 0 else 0.0

        await self.db["users"].update_one(
            {"_id": user_id},
            {
                "$set": {
                    "total_points": total_points,
                    "picks_total": picks_total,
                    "picks_correct": picks_correct,
                    "perfect_picks": perfect_picks,
                    "accuracy": round(accuracy, 4)
                }
            }
        )
