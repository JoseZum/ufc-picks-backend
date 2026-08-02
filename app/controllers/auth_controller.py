"""
Controlador de autenticación - Rutas relacionadas con login y usuario
"""

from typing import Optional

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from app.core.dependencies import CurrentUser, Database
from app.core.rate_limit import limiter
from app.models.user import UserResponse
from app.repositories.user_repository import UserRepository
from app.services.auth_service import AuthService, AuthServiceError

router = APIRouter(prefix="/auth", tags=["auth"])

# Cuerpo de la request OAuth
class GoogleAuthRequest(BaseModel):
    id_token: Optional[str] = None
    access_token: Optional[str] = None

# Request para actualizar perfil
class UpdateProfileRequest(BaseModel):
    name: Optional[str] = None
    profile_picture: Optional[str] = None

# Respuesta con JWT
class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


@router.post("/google", response_model=AuthResponse)
@limiter.limit("10/minute")
async def authenticate_google(
    request: Request,
    body: GoogleAuthRequest,
    db: Database
):
    """
    Autentica mediante Google OAuth.

    El frontend envía el `id_token` de Google, el backend lo verifica,
    crea o busca el usuario en la base de datos y devuelve un JWT.
    """
    auth_service = AuthService(db)

    if not body.id_token and not body.access_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either id_token or access_token is required"
        )

    try:
        if body.access_token:
            user, token = await auth_service.authenticate_with_google_access_token(body.access_token)
        else:
            user, token = await auth_service.authenticate_with_google(body.id_token)
    except AuthServiceError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e)
        )

    return AuthResponse(
        access_token=token,
        user=UserResponse(
            id=user.id,
            email=user.email,
            name=user.name,
            profile_picture=user.profile_picture,
            created_at=user.created_at,
            is_admin=user.is_admin,
            total_points=user.total_points,
            picks_total=user.picks_total,
            picks_correct=user.picks_correct,
            perfect_picks=user.perfect_picks,
            accuracy=user.accuracy
        )
    )


@router.get("/me", response_model=UserResponse)
@limiter.limit("30/minute")
async def get_current_user(request: Request, user: CurrentUser):
    """
    Devuelve el usuario actualmente autenticado.

    Requiere un JWT válido en la cabecera `Authorization`.
    """
    return UserResponse(
        id=user.id,
        email=user.email,
        name=user.name,
        profile_picture=user.profile_picture,
        created_at=user.created_at,
        is_admin=user.is_admin,
        total_points=user.total_points,
        picks_total=user.picks_total,
        picks_correct=user.picks_correct,
        perfect_picks=user.perfect_picks,
        accuracy=user.accuracy
    )


@router.put("/me", response_model=UserResponse)
@limiter.limit("30/minute")
async def update_profile(
    request: Request,
    body: UpdateProfileRequest,
    user: CurrentUser,
    db: Database
):
    """
    Actualiza el perfil del usuario autenticado.

    Campos actualizables: name, profile_picture
    """
    user_repo = UserRepository(db)

    updated_user = await user_repo.update_profile(
        user_id=user.id,
        name=body.name,
        profile_picture=body.profile_picture
    )

    if not updated_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    return UserResponse(
        id=updated_user.id,
        email=updated_user.email,
        name=updated_user.name,
        profile_picture=updated_user.profile_picture,
        created_at=updated_user.created_at,
        is_admin=updated_user.is_admin,
        total_points=updated_user.total_points,
        picks_total=updated_user.picks_total,
        picks_correct=updated_user.picks_correct,
        perfect_picks=updated_user.perfect_picks,
        accuracy=updated_user.accuracy
    )
