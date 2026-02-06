"""
Servicio de Puntos - Calcula y asigna puntos por picks correctos

IMPORTANTE: La comparación se hace por NOMBRE del peleador, no por corner.
Esto evita problemas cuando los datos de los bouts se actualizan y los
corners (red/blue) cambian.
"""

from typing import Dict, Any
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
        """Normalizar nombre para comparación"""
        if not name:
            return ""
        # Quitar espacios extra y convertir a minúsculas para comparación
        return " ".join(name.lower().strip().split())

    async def calculate_points(
        self,
        pick: Dict[str, Any],
        winner_name: str,
        result_method: str,
        result_round: int = None
    ) -> int:
        """
        Calcular puntos para un pick basado en el resultado.

        Args:
            pick: Dict con picked_fighter_name, picked_method, picked_round
            winner_name: Nombre del peleador ganador
            result_method: Método de victoria (KO/TKO, SUB, DEC)
            result_round: Round en que terminó (None para DEC)

        Returns:
            Puntos ganados (0-3)
        """
        points = 0

        # Si no hay ganador, nadie gana puntos
        if not winner_name:
            return 0

        picked_name = self.normalize_name(pick.get("picked_fighter_name", ""))
        winner_normalized = self.normalize_name(winner_name)

        # 1 punto por acertar ganador (comparación por nombre)
        if picked_name == winner_normalized:
            points += 1

            # +1 punto por acertar método (solo si acertó ganador)
            pick_method = self.normalize_method(pick.get("picked_method", ""))
            normalized_result_method = self.normalize_method(result_method)

            if pick_method == normalized_result_method:
                points += 1

                # +1 punto por acertar round (solo si acertó ganador y método)
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
        Calcular y asignar puntos a todos los picks de un bout.

        El resultado debe contener:
        - winner_name: Nombre del peleador ganador
        - method: Método de victoria
        - round: Round (opcional)

        NOTA: También soporta el formato legacy con 'winner' (corner) para
        compatibilidad, pero buscará el nombre del fighter en ese caso.
        """
        # Buscar todos los picks para este bout
        picks_cursor = self.db["picks"].find({"bout_id": bout_id})
        picks = await picks_cursor.to_list(length=None)

        if not picks:
            return {
                "picks_processed": 0,
                "points_distributed": 0
            }

        # Obtener el bout para tener acceso a los fighters
        bout = await self.db["bouts"].find_one({"id": bout_id})
        if not bout:
            return {
                "picks_processed": 0,
                "points_distributed": 0,
                "error": "Bout not found"
            }

        # Determinar el nombre del ganador
        winner_name = result.get("winner_name")

        # Compatibilidad con formato legacy (winner = corner)
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
                "error": "Could not determine winner name"
            }

        result_method = result.get("method", "")
        result_round = result.get("round")

        picks_updated = 0
        total_points = 0
        users_affected = set()

        # Procesar cada pick
        for pick in picks:
            # Calcular puntos usando nombre del peleador
            points = await self.calculate_points(
                pick, winner_name, result_method, result_round
            )

            # Determinar si es correcto (comparación por nombre)
            picked_name = self.normalize_name(pick.get("picked_fighter_name", ""))
            winner_normalized = self.normalize_name(winner_name)
            is_correct = picked_name == winner_normalized

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

        # Actualizar estadísticas de usuarios
        for user_id in users_affected:
            await self._update_user_stats(user_id)

        return {
            "picks_processed": picks_updated,
            "points_distributed": total_points,
            "users_affected": len(users_affected),
            "winner_name": winner_name
        }

    async def revert_points(self, bout_id: int):
        """
        Revertir puntos asignados para un bout (si se elimina resultado).
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
        Actualizar las estadísticas del usuario basado en todos sus picks.
        """
        picks_cursor = self.db["picks"].find({"user_id": user_id})
        picks = await picks_cursor.to_list(length=None)

        total_points = 0
        picks_total = len(picks)
        picks_correct = 0
        perfect_picks = 0

        for pick in picks:
            total_points += pick.get("points_awarded", 0)

            if pick.get("is_correct") is True:
                picks_correct += 1

            if pick.get("points_awarded", 0) == 3:
                perfect_picks += 1

        accuracy = (picks_correct / picks_total) if picks_total > 0 else 0.0

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
