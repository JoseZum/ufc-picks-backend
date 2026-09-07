"""Endpoints de administración, agrupados por dominio."""

from fastapi import APIRouter

from app.controllers.admin import bouts, locks, media, results, timing

router = APIRouter()
for module in (media, timing, results, locks, bouts):
    router.include_router(module.router)
