"""
Modelo de Usuario - autenticacion con Google OAuth
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class User(BaseModel):
    """Usuario autenticado via Google OAuth"""

    id: str = Field(..., alias="_id")  # Mismo que google_id (el 'sub' de Google)
    google_id: str  # ID unico de Google
    email: str
    name: str
    profile_picture: Optional[str] = None

    created_at: datetime  # Cuando se registro
    last_login_at: Optional[datetime] = None  # Ultima vez que entro

    is_active: bool = True  # Podemos deshabilitar cuentas
    is_admin: bool = False  # Para administradores de la plataforma

    # Estadísticas de picks (se actualizan cuando se asignan puntos)
    total_points: int = 0  # Suma de todos los points_awarded
    picks_total: int = 0  # Total de picks hechas
    picks_correct: int = 0  # Picks donde is_correct = True
    perfect_picks: int = 0  # Picks con 3 puntos (acertó todo)
    accuracy: float = 0.0  # picks_correct / picks_total (porcentaje)

    class Config:
        populate_by_name = True  # Acepta _id y id


class UserCreate(BaseModel):
    """Datos necesarios para crear un usuario desde Google OAuth"""

    google_id: str
    email: str
    name: str
    profile_picture: Optional[str] = None


class UserResponse(BaseModel):
    """Info publica del usuario que retornamos en endpoints"""

    id: str
    email: str
    name: str
    profile_picture: Optional[str] = None
    created_at: datetime
    is_admin: bool = False

    # Estadísticas del usuario
    total_points: int = 0
    picks_total: int = 0
    picks_correct: int = 0
    perfect_picks: int = 0
    accuracy: float = 0.0
