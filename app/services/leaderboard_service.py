"""
LeaderboardService - Calcula y sirve datos de clasificación en tiempo real.

Este servicio calcula rankings al vuelo a partir de datos de picks.
Para mejor rendimiento en producción, considera precomputar estos valores.
"""

from typing import Optional

from pymongo.asynchronous.database import AsyncDatabase

from app.models.leaderboard import LeaderboardEntry


class LeaderboardServiceError(Exception):
    """Excepción base para errores del servicio de clasificación."""
    pass


class LeaderboardNotFoundError(LeaderboardServiceError):
    """Se lanza cuando no se encuentran datos de clasificación."""
    pass


class LeaderboardService:
    def __init__(self, db: AsyncDatabase):
        self.db = db
        self.picks_collection = db["picks"]
        self.users_collection = db["users"]
        self.events_collection = db["events"]

    async def _calculate_user_stats(
        self,
        user_id: str,
        event_filter: Optional[dict] = None,
        year: Optional[int] = None
    ) -> Optional[dict]:
        """
        Obtiene estadísticas de un solo usuario.

        NOTA: Ahora usa los campos pre-calculados del User model.
        Solo calcula en tiempo real si hay filtros (event, year).
        """

        # Obtener información del usuario
        user = await self.users_collection.find_one({"_id": user_id})
        if not user:
            return None

        # Si no hay filtros, usar estadísticas precalculadas del usuario.
        if not event_filter and not year:
            # Usar campos del modelo User que se actualizan automáticamente.
            return {
                "user_id": user_id,
                "username": user.get("name", "Unknown"),
                "avatar_url": user.get("profile_picture"),
                "total_points": user.get("total_points", 0),
                "accuracy": user.get("accuracy", 0.0),
                "picks_total": user.get("picks_total", 0),
                "picks_correct": user.get("picks_correct", 0),
                "perfect_picks": user.get("perfect_picks", 0),
            }

        # Si hay filtros, calcular en tiempo real.
        picks_query = {"user_id": user_id}

        if event_filter:
            picks_query.update(event_filter)

        picks = await self.picks_collection.find(picks_query).to_list(length=None)

        if not picks:
            return None

        # Filtrar por año si hace falta
        if year:
            event_ids = [p["event_id"] for p in picks]
            events = await self.events_collection.find({
                "id": {"$in": event_ids},
                "date": {"$regex": f"^{year}"}
            }).to_list(length=None)

            valid_event_ids = {e["id"] for e in events}
            picks = [p for p in picks if p["event_id"] in valid_event_ids]

        if not picks:
            return None

        # Calcular estadísticas para este subconjunto de picks.
        total_points = sum(p.get("points_awarded", 0) for p in picks)
        picks_total = len(picks)

        evaluated_picks = [p for p in picks if p.get("is_correct") is not None]
        picks_correct = sum(1 for p in evaluated_picks if p.get("is_correct"))
        perfect_picks = sum(1 for p in evaluated_picks if p.get("points_awarded") == 3)

        accuracy = picks_correct / len(evaluated_picks) if evaluated_picks else 0.0

        return {
            "user_id": user_id,
            "username": user.get("name", "Unknown"),
            "avatar_url": user.get("profile_picture"),
            "total_points": total_points,
            "accuracy": accuracy,
            "picks_total": picks_total,
            "picks_correct": picks_correct,
            "perfect_picks": perfect_picks,
        }

    async def get_global_leaderboard(
        self,
        limit: int = 100,
        year: Optional[int] = None
    ) -> list[LeaderboardEntry]:
        """
        Obtiene el ranking global (todos los eventos).

        Si no hay filtro de año, usa los campos precalculados del usuario.
        Si hay filtro de año, calcula en tiempo real.
        """
        # Si no hay filtro de año, usar estadísticas precalculadas.
        if not year:
            # Obtener todos los usuarios que tienen picks.
            users = await self.users_collection.find({
                "picks_total": {"$gt": 0}
            }).to_list(length=None)

            entries = []
            for user in users:
                entries.append(LeaderboardEntry(
                    category="global",
                    scope="all_time",
                    user_id=user["_id"],
                    username=user.get("name", "Unknown"),
                    avatar_url=user.get("profile_picture"),
                    total_points=user.get("total_points", 0),
                    accuracy=user.get("accuracy", 0.0),
                    picks_total=user.get("picks_total", 0),
                    picks_correct=user.get("picks_correct", 0),
                    perfect_picks=user.get("perfect_picks", 0),
                ))

            # Ordenar por puntos totales (descendente)
            entries.sort(key=lambda x: x.total_points, reverse=True)

            return entries[:limit]

        # Si hay filtro de año, calcular en tiempo real.
        user_ids = await self.picks_collection.distinct("user_id")

        entries = []
        for user_id in user_ids:
            stats = await self._calculate_user_stats(user_id, year=year)
            if stats and stats["picks_total"] > 0:
                entries.append(LeaderboardEntry(
                    category="global",
                    scope=str(year),
                    **stats
                ))

        # Ordenar por puntos totales (descendente)
        entries.sort(key=lambda x: x.total_points, reverse=True)

        return entries[:limit]

    async def get_event_leaderboard(
        self,
        event_id: int,
        limit: int = 100
    ) -> list[LeaderboardEntry]:
        """Obtiene el ranking para un evento específico."""

        # Obtener todos los IDs únicos de usuarios con picks en este evento
        user_ids = await self.picks_collection.distinct("user_id", {"event_id": event_id})

        # Calcular estadísticas para cada usuario
        entries = []
        for user_id in user_ids:
            stats = await self._calculate_user_stats(user_id, event_filter={"event_id": event_id})
            if stats and stats["picks_total"] > 0:
                entries.append(LeaderboardEntry(
                    category="event",
                    scope=str(event_id),
                    **stats
                ))

        # Ordenar por puntos totales (descendente)
        entries.sort(key=lambda x: x.total_points, reverse=True)

        return entries[:limit]

    async def get_category_leaderboard(
        self,
        category: str,
        limit: int = 100,
        year: Optional[int] = None
    ) -> list[LeaderboardEntry]:
        """
        Obtiene el ranking por categoría.

        Categorías: global, main_events, main_card, prelims, early_prelims

        Por ahora, devuelve el ranking global.
        TODO: Filtrar por card_position de la pelea cuando se implemente.
        """
        # Por simplicidad, devolver ranking global
        # En el futuro, filtrar picks por card_position de la pelea
        return await self.get_global_leaderboard(limit, year)

    async def get_user_rank(
        self,
        user_id: str,
        category: str = "global"
    ) -> Optional[dict]:
        """
        Obtiene la posición del usuario en una categoría específica.

        Devuelve un dict con la posición y los datos de entrada, o None si no existe.
        """
        # Obtener ranking completo
        leaderboard = await self.get_global_leaderboard(limit=1000)

        # Encontrar la posición del usuario
        for idx, entry in enumerate(leaderboard):
            if entry.user_id == user_id:
                return {
                    "rank": idx + 1,
                    "entry": entry
                }

        # Usuario no encontrado en el ranking
        # Intentar obtener sus estadísticas de todas formas
        stats = await self._calculate_user_stats(user_id)
        if stats:
            return {
                "rank": None,
                "entry": LeaderboardEntry(
                    category=category,
                    scope="all_time",
                    **stats
                )
            }

        return None
