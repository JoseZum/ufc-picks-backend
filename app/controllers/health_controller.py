"""
Controlador de salud - Endpoint de comprobación del servicio
"""

from fastapi import APIRouter
from pydantic import BaseModel

from app.database import Database


router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """Respuesta del chequeo de estado."""
    status: str
    database: str


@router.get("/health", response_model=HealthResponse)
@router.head("/health")
async def health_check():
    """
    Endpoint de verificación de estado.

    Comprueba que la API esté en funcionamiento y que la base de datos esté conectada.
    Acepta tanto GET como HEAD para compatibilidad con monitores como UptimeRobot.
    """
    db_status = "connected" if Database.db is not None else "disconnected"

    return HealthResponse(
        status="ok",
        database=db_status
    )
