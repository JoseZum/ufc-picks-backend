"""
🎯 EJEMPLO COMPLETO - Route de Bouts usando Repository Pattern

Este archivo demuestra cómo usar los modelos y repositories
en un route real de FastAPI
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase
from typing import Optional

from app.database import get_database
from app.repositories import BoutRepository
from app.models import Bout

router = APIRouter(prefix="/bouts", tags=["bouts"])


# ============================================
# 📌 GET /bouts/{bout_id}
# ============================================

@router.get("/{bout_id}", response_model=Bout)
async def get_bout(
    bout_id: int,
    db: AsyncIOMotorDatabase = Depends(get_database)
):
    """Obtiene una pelea por ID"""
    repo = BoutRepository(db)
    bout = await repo.get_by_id(bout_id)
    
    if not bout:
        raise HTTPException(status_code=404, detail="Bout not found")
    
    return bout


# ============================================
# 📌 GET /bouts/event/{event_id}
# ============================================

@router.get("/event/{event_id}", response_model=list[Bout])
async def get_event_bouts(
    event_id: int,
    status: Optional[str] = Query(None, regex="^(scheduled|completed)$"),
    db: AsyncIOMotorDatabase = Depends(get_database)
):
    """
    Obtiene todas las peleas de un evento
    
    Query params:
    - status: 'scheduled' o 'completed' (opcional)
    """
    repo = BoutRepository(db)
    bouts = await repo.get_by_event(event_id, status)
    return bouts


# ============================================
# 📌 GET /bouts/search/fighter
# ============================================

@router.get("/search/fighter", response_model=list[Bout])
async def search_by_fighter(
    name: str = Query(..., min_length=3, description="Nombre del peleador"),
    db: AsyncIOMotorDatabase = Depends(get_database)
):
    """
    Busca peleas por nombre de peleador
    
    Busca en ambos corners (red y blue)
    """
    repo = BoutRepository(db)
    bouts = await repo.search_by_fighter(name)
    return bouts


# ============================================
# 📌 GET /bouts/stats/weight-classes
# ============================================

@router.get("/stats/weight-classes")
async def get_weight_class_stats(
    db: AsyncIOMotorDatabase = Depends(get_database)
):
    """
    Estadísticas por categoría de peso
    
    Retorna: [
        {"weight_class": "Lightweight", "total_bouts": 150, "title_fights": 12},
        ...
    ]
    """
    repo = BoutRepository(db)
    stats = await repo.get_stats_by_weight_class()
    return {"data": stats}


# ============================================
# 📌 GET /bouts/fighter/{fighter_name}/record
# ============================================

@router.get("/fighter/{fighter_name}/record")
async def get_fighter_record(
    fighter_name: str,
    db: AsyncIOMotorDatabase = Depends(get_database)
):
    """
    Récord histórico de un peleador
    
    Retorna: {
        "fighter": "Conor McGregor",
        "total_fights": 28,
        "wins": 22,
        "losses": 6
    }
    """
    repo = BoutRepository(db)
    record = await repo.get_fighter_record(fighter_name)
    
    return {
        "fighter": fighter_name,
        **record
    }


# ============================================
# 📌 PATCH /bouts/{bout_id}/result
# ============================================

from pydantic import BaseModel

class BoutResultUpdate(BaseModel):
    winner: str  # "red" | "blue"
    method: str  # "KO/TKO" | "Submission" | "Decision"
    round: int
    time: str

@router.patch("/{bout_id}/result", response_model=Bout)
async def update_bout_result(
    bout_id: int,
    result: BoutResultUpdate,
    db: AsyncIOMotorDatabase = Depends(get_database)
):
    """
    Actualiza el resultado de una pelea
    
    Solo para admins (agregar auth después)
    """
    repo = BoutRepository(db)
    
    # Verificar que existe
    bout = await repo.get_by_id(bout_id)
    if not bout:
        raise HTTPException(status_code=404, detail="Bout not found")
    
    # Actualizar resultado
    updated = await repo.set_result(
        bout_id=bout_id,
        result=result.model_dump()
    )
    
    return updated


# ============================================
# 🎯 Cómo registrar en main.py:
# ============================================

"""
# backend/app/main.py

from fastapi import FastAPI
from app.api.routes.bouts_example import router as bouts_router

app = FastAPI(title="UFC Picks API")

app.include_router(bouts_router)

# Ahora tenés disponibles:
# GET  /bouts/{bout_id}
# GET  /bouts/event/{event_id}
# GET  /bouts/search/fighter?name=McGregor
# GET  /bouts/stats/weight-classes
# GET  /bouts/fighter/Conor McGregor/record
# PATCH /bouts/{bout_id}/result
"""
