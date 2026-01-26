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
