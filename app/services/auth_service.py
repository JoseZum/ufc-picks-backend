"""
AuthService - Lógica de autenticación con Google OAuth.
"""

from pymongo.asynchronous.database import AsyncDatabase

from app.core.security import (
    GoogleAuthError,
    create_access_token,
    verify_google_access_token,
    verify_google_token,
)
from app.models.user import User, UserCreate
from app.repositories.user_repository import UserRepository


class AuthServiceError(Exception):
    """Excepción base para errores del servicio de autenticación."""
    pass


class AuthService:
    def __init__(self, db: AsyncDatabase):
        self.user_repo = UserRepository(db)

    async def authenticate_with_google(self, google_id_token: str) -> tuple[User, str]:
        """
        Autentica un usuario con un id_token de Google.

        1. Verifica el token de Google
        2. Crea o busca al usuario en base de datos
        3. Devuelve el usuario y el token de acceso JWT

        Retorna: (usuario, jwt_token)
        Lanza: AuthServiceError en caso de fallo
        """
        try:
            google_data = await verify_google_token(google_id_token)
        except GoogleAuthError as e:
            raise AuthServiceError(str(e))

        google_id = google_data["sub"]
        email = google_data["email"]
        name = google_data.get("name", email.split("@")[0])
        picture = google_data.get("picture")

        # Buscar o crear usuario
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

        # Generar JWT
        access_token = create_access_token(user.id, user.email)

        return user, access_token

    async def authenticate_with_google_access_token(self, google_access_token: str) -> tuple[User, str]:
        """
        Autentica un usuario con un access_token de Google (flujo de botón personalizado).

        1. Verifica el access_token de Google vía endpoint userinfo
        2. Crea o busca al usuario en base de datos
        3. Devuelve el usuario y el token de acceso JWT

        Retorna: (usuario, jwt_token)
        Lanza: AuthServiceError en caso de fallo
        """
        try:
            google_data = await verify_google_access_token(google_access_token)
        except GoogleAuthError as e:
            raise AuthServiceError(str(e))

        google_id = google_data["sub"]
        email = google_data["email"]
        name = google_data.get("name", email.split("@")[0])
        picture = google_data.get("picture")

        # Buscar o crear usuario
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

        # Generar JWT
        access_token = create_access_token(user.id, user.email)

        return user, access_token
