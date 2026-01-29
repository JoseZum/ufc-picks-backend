"""
Fixtures for integration tests
"""

import pytest
from httpx import AsyncClient
from motor.motor_asyncio import AsyncIOMotorClient

from app.main import app
from app.database import Database


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
    original_db = Database._db
    Database._db = test_db
    
    async with AsyncClient(app=app, base_url="http://test") as ac:
        yield ac
    
    # Restore original db
    Database._db = original_db


@pytest.fixture
async def auth_headers(client, sample_user_data, test_db):
    """
    Provides authentication headers for protected endpoints.
    
    Creates a test user and returns valid JWT token headers.
    """
    from app.core.security import create_access_token
    
    # Create test user
    await test_db["users"].insert_one({
        "_id": sample_user_data["google_id"],
        "google_id": sample_user_data["google_id"],
        "email": sample_user_data["email"],
        "name": sample_user_data["name"],
        "profile_picture": sample_user_data["profile_picture"],
        "is_active": True,
        "is_admin": False
    })
    
    # Create JWT token
    token = create_access_token(
        sample_user_data["google_id"],
        sample_user_data["email"]
    )
    
    return {"Authorization": f"Bearer {token}"}
