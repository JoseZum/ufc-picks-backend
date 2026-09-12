"""Acceso a datos para la colección de picks."""

from datetime import datetime

from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from app.models.pick import Pick


class PickRepository:
    def __init__(self, db: AsyncDatabase):
        self.db = db
        self.collection = db["picks"]

    # Create

    async def create(self, pick: Pick) -> Pick:
        """Create a new pick."""
        pick_dict = pick.model_dump(by_alias=True)

        try:
            await self.collection.insert_one(pick_dict)
            return pick
        except DuplicateKeyError:
            raise ValueError(f"Pick {pick.id} already exists") from None

    # Read

    async def get_by_id(self, pick_id: str) -> Pick | None:
        """Get pick by composite ID (user_id:bout_id)."""
        doc = await self.collection.find_one({"_id": pick_id})
        return Pick(**doc) if doc else None

    async def get_user_pick_for_bout(
        self,
        user_id: str,
        bout_id: int
    ) -> Pick | None:
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

    # Update

    async def update_pick(
        self,
        pick_id: str,
        picked_fighter_name: str,
        picked_method: str,
        picked_round: int | None,
        updated_at: datetime,
        picked_fighter_id: str | None = None,
    ) -> Pick | None:
        """Update a pick's prediction.

        `picked_fighter_id` is written alongside the display name so the pair
        always describes the same fighter. A legacy pick that cannot be resolved
        keeps a null id rather than an id that might belong to the other corner.
        """
        result = await self.collection.find_one_and_update(
            {"_id": pick_id},
            {
                "$set": {
                    "picked_fighter_name": picked_fighter_name,
                    "picked_fighter_id": picked_fighter_id,
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
    ) -> Pick | None:
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

    def _normalize_name(self, name: str) -> str:
        """Normalize name for comparison."""
        if not name:
            return ""
        return " ".join(name.lower().strip().split())

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

    # Delete

    async def delete(self, pick_id: str) -> bool:
        """Delete a pick (only if not locked)."""
        result = await self.collection.delete_one({
            "_id": pick_id,
            "locked": False
        })
        return result.deleted_count > 0

    # Stats

    async def exists(self, user_id: str, bout_id: int) -> bool:
        """Check if user has a pick for a bout."""
        count = await self.collection.count_documents(
            {"user_id": user_id, "bout_id": bout_id},
            limit=1
        )
        return count > 0
