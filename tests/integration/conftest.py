"""
Fixtures for integration tests
"""

from datetime import UTC

import pytest
from httpx import AsyncClient

from app.database import Database
from app.main import app


@pytest.fixture
async def client(test_db):
    """
    HTTP client for testing API endpoints.

    Overrides the database dependency with test database.
    """
    # Override the database dependency
    async def override_get_db():
        return test_db

    # Store original db connection
    original_db = Database.db
    Database.db = test_db

    async with AsyncClient(app=app, base_url="http://test") as ac:
        yield ac

    # Restore original db
    Database.db = original_db


@pytest.fixture
async def auth_headers(client, sample_user_data, test_db):
    """
    Provides authentication headers for protected endpoints.

    Creates a test user and returns valid JWT token headers.
    """
    from datetime import datetime, timezone

    from app.core.security import create_access_token

    # Create or update test user (upsert to avoid DuplicateKeyError in parallel tests)
    await test_db["users"].update_one(
        {"_id": sample_user_data["google_id"]},
        {"$set": {
            "google_id": sample_user_data["google_id"],
            "email": sample_user_data["email"],
            "name": sample_user_data["name"],
            "profile_picture": sample_user_data["profile_picture"],
            "created_at": datetime.now(UTC),
            "last_login_at": datetime.now(UTC),
            "is_active": True,
            "is_admin": False
        }},
        upsert=True
    )

    # Create JWT token
    token = create_access_token(
        sample_user_data["google_id"],
        sample_user_data["email"]
    )

    return {"Authorization": f"Bearer {token}"}
