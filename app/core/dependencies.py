"""
Dependencies de FastAPI para autenticacion e inyeccion de BD
"""

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.security import decode_access_token
from app.database import get_database
from app.repositories.user_repository import UserRepository
from app.models.user import User

# Esquema de seguridad: espera un header "Authorization: Bearer <token>"
security = HTTPBearer()


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
    db: Annotated[AsyncIOMotorDatabase, Depends(get_database)]
) -> User:
    """
    Dependency que valida el JWT del usuario.

    Se usa en los endpoints que requieren autenticacion.
    Retorna el usuario si el token es valido.
    """
    token = credentials.credentials
    payload = decode_access_token(token)

    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token invalido o expirado",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Payload del token invalido",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Busco el usuario en la BD
    user_repo = UserRepository(db)
    user = await user_repo.get_by_id(user_id)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuario no encontrado",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cuenta de usuario deshabilitada",
        )

    return user


# Alias de tipos para que se vea mas limpio en los endpoints
CurrentUser = Annotated[User, Depends(get_current_user)]
Database = Annotated[AsyncIOMotorDatabase, Depends(get_database)]
