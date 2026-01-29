"""
Unit tests for AuthService
"""

import pytest
from unittest.mock import AsyncMock, patch

from app.services.auth_service import AuthService, AuthServiceError
from app.models.user import User
from app.core.security import GoogleAuthError


class TestAuthService:
    """Test suite for AuthService authentication logic."""
    
    @pytest.mark.asyncio
    async def test_authenticate_new_user(self, test_db, sample_user_data):
        """Test authenticating a new user creates account."""
        service = AuthService(test_db)
        
        # Mock Google token verification
        with patch('app.services.auth_service.verify_google_token') as mock_verify:
            mock_verify.return_value = {
                "sub": sample_user_data["google_id"],
                "email": sample_user_data["email"],
                "name": sample_user_data["name"],
                "picture": sample_user_data["profile_picture"]
            }
            
            # Mock JWT creation
            with patch('app.services.auth_service.create_access_token') as mock_jwt:
                mock_jwt.return_value = "fake_jwt_token"
                
                # Act
                user, token = await service.authenticate_with_google("fake_google_token")
        
        # Assert
        assert user.google_id == sample_user_data["google_id"]
        assert user.email == sample_user_data["email"]
        assert user.name == sample_user_data["name"]
        assert token == "fake_jwt_token"
        
        # Verify user was saved to database
        saved_user = await test_db["users"].find_one({"_id": sample_user_data["google_id"]})
        assert saved_user is not None
        assert saved_user["email"] == sample_user_data["email"]
    
    @pytest.mark.asyncio
    async def test_authenticate_existing_user(self, test_db, sample_user_data):
        """Test authenticating existing user updates last login."""
        # Setup: Create existing user
        await test_db["users"].insert_one({
            "_id": sample_user_data["google_id"],
            "google_id": sample_user_data["google_id"],
            "email": sample_user_data["email"],
            "name": sample_user_data["name"],
            "profile_picture": sample_user_data["profile_picture"],
            "created_at": "2025-01-01T00:00:00Z",
            "last_login_at": "2025-01-01T00:00:00Z",
            "is_active": True,
            "is_admin": False
        })
        
        service = AuthService(test_db)
        
        # Mock Google token verification
        with patch('app.services.auth_service.verify_google_token') as mock_verify:
            mock_verify.return_value = {
                "sub": sample_user_data["google_id"],
                "email": sample_user_data["email"],
                "name": sample_user_data["name"],
                "picture": sample_user_data["profile_picture"]
            }
            
            # Mock JWT creation
            with patch('app.services.auth_service.create_access_token') as mock_jwt:
                mock_jwt.return_value = "fake_jwt_token"
                
                # Act
                user, token = await service.authenticate_with_google("fake_google_token")
        
        # Assert
        assert user.google_id == sample_user_data["google_id"]
        assert token == "fake_jwt_token"
        
        # Verify last_login_at was updated
        updated_user = await test_db["users"].find_one({"_id": sample_user_data["google_id"]})
        assert updated_user["last_login_at"] != "2025-01-01T00:00:00Z"
    
    @pytest.mark.asyncio
    async def test_authenticate_invalid_token(self, test_db):
        """Test authentication with invalid Google token."""
        service = AuthService(test_db)
        
        # Mock Google token verification to raise error
        with patch('app.services.auth_service.verify_google_token') as mock_verify:
            mock_verify.side_effect = GoogleAuthError("Invalid token")
            
            # Act & Assert
            with pytest.raises(AuthServiceError):
                await service.authenticate_with_google("invalid_token")
    
    @pytest.mark.asyncio
    async def test_authenticate_creates_user_with_email_name_fallback(self, test_db):
        """Test that name falls back to email prefix if not provided."""
        service = AuthService(test_db)
        
        # Mock Google token with no name
        with patch('app.services.auth_service.verify_google_token') as mock_verify:
            mock_verify.return_value = {
                "sub": "google123",
                "email": "test@example.com",
                # No "name" field
            }
            
            with patch('app.services.auth_service.create_access_token') as mock_jwt:
                mock_jwt.return_value = "fake_jwt_token"
                
                # Act
                user, token = await service.authenticate_with_google("fake_google_token")
        
        # Assert: name should be email prefix
        assert user.name == "test"  # From "test@example.com"
