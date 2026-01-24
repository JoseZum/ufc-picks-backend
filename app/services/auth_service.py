"""
AuthService - Google OAuth authentication logic.
"""

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.security import verify_google_token, create_access_token, GoogleAuthError
from app.repositories.user_repository import UserRepository
from app.models.user import User, UserCreate


class AuthServiceError(Exception):
    """Base exception for auth service errors."""
    pass


class AuthService:
    def __init__(self, db: AsyncIOMotorDatabase):
        self.user_repo = UserRepository(db)

    async def authenticate_with_google(self, google_id_token: str) -> tuple[User, str]:
        """
        Authenticate user with Google id_token.

        1. Verifies the Google token
        2. Creates or finds user in database
        3. Returns user and JWT access token

        Returns: (user, jwt_token)
        Raises: AuthServiceError on failure
        """
        try:
            google_data = await verify_google_token(google_id_token)
        except GoogleAuthError as e:
            raise AuthServiceError(str(e))

        google_id = google_data["sub"]
        email = google_data["email"]
        name = google_data.get("name", email.split("@")[0])
        picture = google_data.get("picture")

        # Find or create user
        user = await self.user_repo.get_by_google_id(google_id)

        if user is None:
            user_data = UserCreate(
                google_id=google_id,
                email=email,
                name=name,
                profile_picture=picture
            )
            user = await self.user_repo.create(user_data)
        else:
            await self.user_repo.update_last_login(user.id)

        # Generate JWT
        access_token = create_access_token(user.id, user.email)

        return user, access_token
