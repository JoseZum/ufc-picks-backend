"""Acceso a datos para la colección de usuarios."""

from datetime import UTC, datetime
from typing import Any

from pymongo.asynchronous.database import AsyncDatabase

from app.models.user import User, UserCreate


class UserRepository:
    def __init__(self, db: AsyncDatabase):
        self.db = db
        self.collection = db["users"]

    async def get_by_id(self, user_id: str) -> User | None:
        """Get user by ID (google_id)."""
        doc = await self.collection.find_one({"_id": user_id})
        return User(**doc) if doc else None

    async def get_by_google_id(self, google_id: str) -> User | None:
        """Get user by Google ID (alias for get_by_id)."""
        return await self.get_by_id(google_id)

    async def get_by_email(self, email: str) -> User | None:
        """Get user by email."""
        doc = await self.collection.find_one({"email": email})
        return User(**doc) if doc else None

    async def create(self, user_data: UserCreate) -> User:
        """Create a new user."""
        now = datetime.now(UTC)

        user_doc: dict[str, Any] = {
            "_id": user_data.google_id,
            "google_id": user_data.google_id,
            "email": user_data.email,
            "name": user_data.name,
            "profile_picture": user_data.profile_picture,
            "created_at": now,
            "last_login_at": now,
            "is_active": True,
            "is_admin": False,
        }

        await self.collection.insert_one(user_doc)
        return User(**user_doc)

    async def update_last_login(self, user_id: str) -> User | None:
        """Update user's last login timestamp."""
        now = datetime.now(UTC)

        from pymongo import ReturnDocument
        result = await self.collection.find_one_and_update(
            {"_id": user_id},
            {"$set": {"last_login_at": now}},
            return_document=ReturnDocument.AFTER
        )

        return User(**result) if result else None

    async def update_profile(
        self,
        user_id: str,
        name: str | None = None,
        profile_picture: str | None = None
    ) -> User | None:
        """Update user profile fields."""
        updates = {}

        if name is not None:
            updates["name"] = name
        if profile_picture is not None:
            updates["profile_picture"] = profile_picture

        if not updates:
            return await self.get_by_id(user_id)

        result = await self.collection.find_one_and_update(
            {"_id": user_id},
            {"$set": updates},
            return_document=True
        )

        return User(**result) if result else None

    async def exists(self, user_id: str) -> bool:
        """Check if user exists."""
        count = await self.collection.count_documents({"_id": user_id}, limit=1)
        return count > 0
