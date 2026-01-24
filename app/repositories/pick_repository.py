"""
PickRepository - MongoDB access for picks collection.
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

    # CREATE

    async def create(self, pick: Pick) -> Pick:
        """Create a new pick."""
        pick_dict = pick.model_dump(by_alias=True)

        try:
            await self.collection.insert_one(pick_dict)
            return pick
        except DuplicateKeyError:
            raise ValueError(f"Pick {pick.id} already exists")

    # READ

    async def get_by_id(self, pick_id: str) -> Optional[Pick]:
        """Get pick by composite ID (user_id:bout_id)."""
        doc = await self.collection.find_one({"_id": pick_id})
        return Pick(**doc) if doc else None

    async def get_user_pick_for_bout(
        self,
        user_id: str,
        bout_id: int
    ) -> Optional[Pick]:
        """Get user's pick for a specific bout."""
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
        """Get all picks for a user in an event."""
        cursor = self.collection.find({
            "user_id": user_id,
            "event_id": event_id
        }).sort("created_at", 1)

        docs = await cursor.to_list(length=None)
        return [Pick(**doc) for doc in docs]

    async def get_picks_for_bout(self, bout_id: int) -> list[Pick]:
        """Get all picks for a bout (community stats)."""
        cursor = self.collection.find({"bout_id": bout_id})
        docs = await cursor.to_list(length=None)
        return [Pick(**doc) for doc in docs]

    async def get_user_all_picks(
        self,
        user_id: str,
        limit: int = 100,
        skip: int = 0
    ) -> list[Pick]:
        """Get all picks for a user (paginated)."""
        cursor = self.collection.find(
            {"user_id": user_id}
        ).sort("created_at", -1).skip(skip).limit(limit)

        docs = await cursor.to_list(length=limit)
        return [Pick(**doc) for doc in docs]

    # UPDATE

    async def update_pick(
        self,
        pick_id: str,
        picked_corner: str,
        picked_method: str,
        picked_round: Optional[int],
        updated_at: datetime
    ) -> Optional[Pick]:
        """Update a pick's prediction."""
        result = await self.collection.find_one_and_update(
            {"_id": pick_id, "locked": False},
            {
                "$set": {
                    "picked_corner": picked_corner,
                    "picked_method": picked_method,
                    "picked_round": picked_round,
                    "updated_at": updated_at
                }
            },
            return_document=True
        )
        return Pick(**result) if result else None

    async def update_result(
        self,
        pick_id: str,
        is_correct: bool,
        points_awarded: int
    ) -> Optional[Pick]:
        """Update pick result after bout completion."""
        result = await self.collection.find_one_and_update(
            {"_id": pick_id},
            {
                "$set": {
                    "is_correct": is_correct,
                    "points_awarded": points_awarded
                }
            },
            return_document=True
        )
        return Pick(**result) if result else None

    async def lock_picks_for_event(self, event_id: int) -> int:
        """Lock all picks for an event."""
        result = await self.collection.update_many(
            {"event_id": event_id, "locked": False},
            {"$set": {"locked": True}}
        )
        return result.modified_count

    async def update_picks_for_bout(
        self,
        bout_id: int,
        winner_corner: str,
        result_method: str,
        result_round: Optional[int]
    ) -> int:
        """
        Batch update all picks for a bout after result.

        Calculates scores based on scoring rules:
        - Wrong fighter: 0 points
        - Correct fighter only: 1 point
        - Correct fighter + method: 2 points
        - Correct fighter + method + round (non-DEC): 3 points
        """
        updated = 0

        cursor = self.collection.find({"bout_id": bout_id})
        async for doc in cursor:
            pick = Pick(**doc)

            is_correct = pick.picked_corner == winner_corner

            if not is_correct:
                points = 0
            else:
                method_match = self._methods_match(pick.picked_method, result_method)

                if pick.picked_method == "DEC":
                    points = 2 if method_match else 1
                else:
                    if method_match and pick.picked_round == result_round:
                        points = 3
                    elif method_match:
                        points = 2
                    else:
                        points = 1

            await self.collection.update_one(
                {"_id": pick.id},
                {"$set": {"is_correct": is_correct, "points_awarded": points}}
            )
            updated += 1

        return updated

    def _methods_match(self, picked: str, actual: str) -> bool:
        """Check if picked method matches actual result method."""
        if not actual:
            return picked == "DEC"

        actual_upper = actual.upper()

        if "KO" in actual_upper or "TKO" in actual_upper:
            return picked == "KO/TKO"
        elif "SUB" in actual_upper:
            return picked == "SUB"
        else:
            return picked == "DEC"

    # DELETE

    async def delete(self, pick_id: str) -> bool:
        """Delete a pick (only if not locked)."""
        result = await self.collection.delete_one({
            "_id": pick_id,
            "locked": False
        })
        return result.deleted_count > 0

    # STATS

    async def get_user_stats(self, user_id: str) -> dict:
        """Get user statistics."""
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
        """Get pick distribution for a bout."""
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

    async def exists(self, user_id: str, bout_id: int) -> bool:
        """Check if user has a pick for a bout."""
        count = await self.collection.count_documents(
            {"user_id": user_id, "bout_id": bout_id},
            limit=1
        )
        return count > 0
