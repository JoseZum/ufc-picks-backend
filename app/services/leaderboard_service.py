"""
LeaderboardService - Calculates and serves leaderboard data in real-time.

This service calculates leaderboards on-the-fly from picks data.
For better performance in production, consider pre-computing these values.
"""

from typing import Optional
from collections import defaultdict

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.models.leaderboard import LeaderboardEntry


class LeaderboardServiceError(Exception):
    """Base exception for leaderboard service errors."""
    pass


class LeaderboardNotFoundError(LeaderboardServiceError):
    """Raised when leaderboard data is not found."""
    pass


class LeaderboardService:
    def __init__(self, db: AsyncIOMotorDatabase):
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
        Get stats for a single user.

        NOTA: Ahora usa los campos pre-calculados del User model.
        Solo calcula en tiempo real si hay filtros (event, year).
        """

        # Get user info
        user = await self.users_collection.find_one({"_id": user_id})
        if not user:
            return None

        # Si NO hay filtros, usar stats pre-calculadas del User (RÁPIDO)
        if not event_filter and not year:
            # Usar campos del User model que se actualizan automáticamente
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

        # Si HAY filtros, calcular en tiempo real (event o year específico)
        picks_query = {"user_id": user_id}

        if event_filter:
            picks_query.update(event_filter)

        picks = await self.picks_collection.find(picks_query).to_list(length=None)

        if not picks:
            return None

        # Filter by year if needed
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

        # Calculate stats en tiempo real para este subset de picks
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
        Get global leaderboard (all events).

        Si NO hay filtro de año, usa los campos pre-calculados del User (RÁPIDO).
        Si HAY filtro de año, calcula en tiempo real.
        """
        # Si NO hay filtro de año, usar stats pre-calculadas (OPTIMIZADO)
        if not year:
            # Obtener todos los usuarios que tienen picks (picks_total > 0)
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

            # Sort by total points (descending)
            entries.sort(key=lambda x: x.total_points, reverse=True)

            return entries[:limit]

        # Si HAY filtro de año, calcular en tiempo real
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

        # Sort by total points (descending)
        entries.sort(key=lambda x: x.total_points, reverse=True)

        return entries[:limit]

    async def get_event_leaderboard(
        self,
        event_id: int,
        limit: int = 100
    ) -> list[LeaderboardEntry]:
        """Get leaderboard for a specific event."""
        
        # Get all unique user IDs who have picks for this event
        user_ids = await self.picks_collection.distinct("user_id", {"event_id": event_id})
        
        # Calculate stats for each user
        entries = []
        for user_id in user_ids:
            stats = await self._calculate_user_stats(user_id, event_filter={"event_id": event_id})
            if stats and stats["picks_total"] > 0:
                entries.append(LeaderboardEntry(
                    category="event",
                    scope=str(event_id),
                    **stats
                ))
        
        # Sort by total points (descending)
        entries.sort(key=lambda x: x.total_points, reverse=True)
        
        return entries[:limit]

    async def get_category_leaderboard(
        self,
        category: str,
        limit: int = 100,
        year: Optional[int] = None
    ) -> list[LeaderboardEntry]:
        """
        Get leaderboard by category.
        
        Categories: global, main_events, main_card, prelims, early_prelims
        
        For now, returns global leaderboard. 
        TODO: Filter by bout card_position when implemented.
        """
        # For simplicity, return global leaderboard
        # In the future, filter picks by bout's card_position
        return await self.get_global_leaderboard(limit, year)

    async def get_user_rank(
        self,
        user_id: str,
        category: str = "global"
    ) -> Optional[dict]:
        """
        Get user's rank in a specific leaderboard category.
        
        Returns dict with rank and entry data, or None if not found.
        """
        # Get full leaderboard
        leaderboard = await self.get_global_leaderboard(limit=1000)
        
        # Find user's position
        for idx, entry in enumerate(leaderboard):
            if entry.user_id == user_id:
                return {
                    "rank": idx + 1,
                    "entry": entry
                }
        
        # User not found in leaderboard
        # Try to get their stats anyway
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
