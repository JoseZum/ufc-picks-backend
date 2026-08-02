"""
Pytest fixtures and configuration for all tests.
"""

import pytest
import asyncio
import os
from typing import AsyncGenerator, Generator
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import ServerSelectionTimeoutError
from datetime import datetime, timedelta, timezone

# MongoDB test database
TEST_DB_URI = "mongodb://localhost:27017"
TEST_DB_NAME = "ufc_picks_test"

# Safe defaults required while integration fixtures import the application.
# ``setdefault`` preserves explicit CI or developer overrides and ensures tests
# never need to read production credentials from a local .env file.
TEST_ENV_DEFAULTS = {
    "MONGODB_URI": TEST_DB_URI,
    "MONGODB_DB_NAME": TEST_DB_NAME,
    "JWT_SECRET": "local-test-secret-only-not-for-production",
    "GOOGLE_CLIENT_ID": "local-test-google-client-id",
    "APP_ENV": "test",
    "DEBUG": "false",
    "IMAGE_CACHE_STRATEGY": "MEMORY",
    "IMAGE_SOURCE_MODE": "cache",
    # The launch flag ships OFF (CAL-004). The suite exercises the feature,
    # so it turns it on with no allowlist — the gate itself has dedicated
    # coverage in tests/integration/test_mission_access.py.
    "MISSIONS_ENABLED": "true",
    "MISSIONS_ALLOWLIST": "",
}
for variable, value in TEST_ENV_DEFAULTS.items():
    os.environ.setdefault(variable, value)


@pytest.fixture(scope="session")
def worker_id(request):
    """
    Return the worker ID when using pytest-xdist, otherwise 'master'.
    This allows each worker to use its own test database.
    """
    if hasattr(request.config, 'workerinput'):
        return request.config.workerinput['workerid']
    return 'master'


@pytest.fixture(scope="session")
def event_loop() -> Generator:
    """Create event loop for async tests."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
async def test_mongo_client() -> AsyncGenerator[AsyncMongoClient, None]:
    """Provide one verified local Mongo client per pytest worker."""
    client = AsyncMongoClient(TEST_DB_URI, serverSelectionTimeoutMS=2_000)
    try:
        await client.admin.command("ping")
    except ServerSelectionTimeoutError:
        pytest.fail(
            "Local MongoDB is unavailable at 127.0.0.1:27017. "
            "Start it with: docker compose -f compose.test.yml up -d --wait",
            pytrace=False,
        )

    hello = await client.admin.command("hello")
    if hello.get("setName") != "rs0":
        pytest.fail(
            "Local MongoDB must run as replica set rs0 for mission tests. "
            "Recreate compose.test.yml and run: "
            ".venv\\Scripts\\python.exe scripts/ensure_test_mongo.py",
            pytrace=False,
        )

    yield client
    await client.close()


@pytest.fixture(scope="function")
async def test_db(
    worker_id,
    test_mongo_client: AsyncMongoClient,
) -> AsyncGenerator[AsyncDatabase, None]:
    """
    Provide a clean test database for each test.

    Uses a separate database per worker when running with pytest-xdist.
    Automatically cleans up after each test.
    """
    # Use different database per worker to avoid conflicts in parallel execution
    db_name = f"{TEST_DB_NAME}_{worker_id}" if worker_id != "master" else TEST_DB_NAME
    db = test_mongo_client[db_name]

    yield db

    # Cleanup: drop all collections after test
    collection_names = await db.list_collection_names()
    for collection_name in collection_names:
        await db[collection_name].drop()


@pytest.fixture
def sample_user_data():
    """Sample user data for testing."""
    return {
        "google_id": "test_google_id_123",
        "email": "test@example.com",
        "name": "Test User",
        "profile_picture": "https://example.com/avatar.jpg"
    }


@pytest.fixture
def sample_event_data():
    """Sample event data for testing."""
    future_event_day = (datetime.now(timezone.utc) + timedelta(days=14)).date()
    future_event_date = datetime.combine(
        future_event_day,
        datetime.min.time(),
        tzinfo=timezone.utc,
    )

    return {
        "id": 12345,
        "source": "tapology",
        "promotion": "UFC",
        "name": "UFC 300: Test Event",
        "subtitle": None,
        "slug": "ufc-300-test-event",
        "url": "https://tapology.com/event/12345",
        "date": future_event_date,
        "timezone": "America/New_York",
        "location": {
            "venue": "T-Mobile Arena",
            "city": "Las Vegas",
            "state": "Nevada",
            "country": "United States"
        },
        "status": "scheduled",
        "total_bouts": 12,
        "main_event_bout_id": 67890,
        "poster_image_url": "https://pbs.twimg.com/media/poster.jpg:large",
        "poster_image_source": "wikipedia_source",
        "hero_image_url": "https://ufc.com/images/styles/background_image_xl_2x/s3/event-art.jpg",
        "hero_image_source": "ufc_official_xl_2x",
        "scraped_at": datetime.now(timezone.utc),
        "last_updated": datetime.now(timezone.utc)
    }


@pytest.fixture
def sample_bout_data():
    """Sample bout data for testing."""
    return {
        "id": 67890,
        "event_id": 12345,
        "source": "tapology",
        "url": "https://tapology.com/bout/67890",
        "slug": "test-fighter-1-vs-test-fighter-2",
        "weight_class": "Lightweight",
        "gender": "male",
        "rounds_scheduled": 5,
        "is_title_fight": True,
        "status": "scheduled",
        "fighters": {
            "red": {
                "fighter_name": "Test Fighter 1",
                "corner": "red",
                "ranking": {"division_rank": 1},
                "record_at_fight": {"wins": 25, "losses": 1, "draws": 0},
                "last_fights": [],
                "nationality": "USA",
                "fighting_out_of": "Las Vegas",
                "age_at_fight_years": 30,
                "height_cm": 180,
                "reach_cm": 185
            },
            "blue": {
                "fighter_name": "Test Fighter 2",
                "corner": "blue",
                "ranking": {"division_rank": 2},
                "record_at_fight": {"wins": 23, "losses": 2, "draws": 0},
                "last_fights": [],
                "nationality": "Brazil",
                "fighting_out_of": "Rio de Janeiro",
                "age_at_fight_years": 28,
                "height_cm": 178,
                "reach_cm": 183
            }
        },
        "result": None,
        "scraped_at": datetime.now(timezone.utc),
        "last_updated": datetime.now(timezone.utc)
    }


@pytest.fixture
def sample_pick_data():
    """Sample pick data for testing."""
    return {
        "event_id": 12345,
        "bout_id": 67890,
        "picked_fighter_name": "Test Fighter 1",
        "picked_method": "KO/TKO",
        "picked_round": 2
    }


@pytest.fixture
def sample_result_data():
    """Sample bout result data for testing."""
    return {
        "winner_name": "Test Fighter 1",
        "method": "KO/TKO",
        "round": 2,
        "time": "3:45",
        "details": "Knockout"
    }
