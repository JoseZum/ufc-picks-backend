"""
Unit tests for UserRepository
"""

import pytest
from datetime import datetime, timezone

from app.repositories.user_repository import UserRepository
from app.models.user import UserCreate


class TestUserRepository:
    """Test suite for UserRepository database operations."""
    
    @pytest.mark.asyncio
    async def test_create_user(self, test_db, sample_user_data):
        """Test creating a new user."""
        repo = UserRepository(test_db)
        user_create = UserCreate(**sample_user_data)
        
        # Act
        user = await repo.create(user_create)
        
        # Assert
        assert user.google_id == sample_user_data["google_id"]
        assert user.email == sample_user_data["email"]
        assert user.name == sample_user_data["name"]
        assert user.profile_picture == sample_user_data["profile_picture"]
        assert user.is_active is True
        assert user.is_admin is False
        assert user.created_at is not None
    
    @pytest.mark.asyncio
    async def test_get_by_id(self, test_db, sample_user_data):
        """Test retrieving user by ID."""
        repo = UserRepository(test_db)
        user_create = UserCreate(**sample_user_data)
        created_user = await repo.create(user_create)
        
        # Act
        user = await repo.get_by_id(created_user.id)
        
        # Assert
        assert user is not None
        assert user.id == created_user.id
        assert user.email == sample_user_data["email"]
    
    @pytest.mark.asyncio
    async def test_get_by_id_not_found(self, test_db):
        """Test retrieving non-existent user returns None."""
        repo = UserRepository(test_db)
        
        # Act
        user = await repo.get_by_id("non_existent_id")
        
        # Assert
        assert user is None
    
    @pytest.mark.asyncio
    async def test_get_by_google_id(self, test_db, sample_user_data):
        """Test retrieving user by Google ID."""
        repo = UserRepository(test_db)
        user_create = UserCreate(**sample_user_data)
        await repo.create(user_create)
        
        # Act
        user = await repo.get_by_google_id(sample_user_data["google_id"])
        
        # Assert
        assert user is not None
        assert user.google_id == sample_user_data["google_id"]
    
    @pytest.mark.asyncio
    async def test_get_by_email(self, test_db, sample_user_data):
        """Test retrieving user by email."""
        repo = UserRepository(test_db)
        user_create = UserCreate(**sample_user_data)
        await repo.create(user_create)
        
        # Act
        user = await repo.get_by_email(sample_user_data["email"])
        
        # Assert
        assert user is not None
        assert user.email == sample_user_data["email"]
    
    @pytest.mark.asyncio
    async def test_update_last_login(self, test_db, sample_user_data):
        """Test updating user's last login timestamp."""
        repo = UserRepository(test_db)
        user_create = UserCreate(**sample_user_data)
        user = await repo.create(user_create)
        
        original_last_login = user.last_login_at
        
        # Wait a tiny bit to ensure timestamp difference
        import asyncio
        await asyncio.sleep(0.01)
        
        # Act
        updated_user = await repo.update_last_login(user.id)
        
        # Assert
        assert updated_user is not None
        assert updated_user.last_login_at > original_last_login
    
    @pytest.mark.asyncio
    async def test_update_profile(self, test_db, sample_user_data):
        """Test updating user profile."""
        repo = UserRepository(test_db)
        user_create = UserCreate(**sample_user_data)
        user = await repo.create(user_create)
        
        # Act
        updated_user = await repo.update_profile(
            user.id,
            name="New Name",
            profile_picture="https://example.com/new-avatar.jpg"
        )
        
        # Assert
        assert updated_user is not None
        assert updated_user.name == "New Name"
        assert updated_user.profile_picture == "https://example.com/new-avatar.jpg"
        assert updated_user.email == sample_user_data["email"]  # Unchanged
    
    @pytest.mark.asyncio
    async def test_update_profile_partial(self, test_db, sample_user_data):
        """Test updating only some profile fields."""
        repo = UserRepository(test_db)
        user_create = UserCreate(**sample_user_data)
        user = await repo.create(user_create)
        
        # Act: Update only name
        updated_user = await repo.update_profile(user.id, name="Only New Name")
        
        # Assert
        assert updated_user.name == "Only New Name"
        assert updated_user.profile_picture == sample_user_data["profile_picture"]  # Unchanged
    
    @pytest.mark.asyncio
    async def test_exists(self, test_db, sample_user_data):
        """Test checking if user exists."""
        repo = UserRepository(test_db)
        user_create = UserCreate(**sample_user_data)
        user = await repo.create(user_create)
        
        # Act & Assert
        assert await repo.exists(user.id) is True
        assert await repo.exists("non_existent_id") is False
