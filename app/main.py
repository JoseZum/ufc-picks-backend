"""
Entry point de la API 
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.database import Database

from app.controllers.auth_controller import router as auth_router
from app.controllers.events_controller import router as events_router
from app.controllers.bouts_controller import router as bouts_router
from app.controllers.picks_controller import router as picks_router
from app.controllers.leaderboard_controller import router as leaderboard_router
from app.controllers.health_controller import router as health_router

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await Database.connect()
    yield
    await Database.disconnect()

# Creo la app 
app = FastAPI(
    title="UFC Picks API",
    description="Backend de la app para predicciones de UFC",
    version="1.0.0",
    lifespan=lifespan
)

# CORS para que el frontend en localhost:3000 pueda hacer requests
origins = [origin.strip() for origin in settings.cors_origins.split(",")]

# En producción, permitir también dominios de Vercel usando regex
allow_origin_regex = None
if settings.app_env == "production":
    allow_origin_regex = r"https://.*\.vercel\.app"

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_origin_regex=allow_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],  # GET, POST, DELETE, etc
    allow_headers=["*"],   # Authorization, Content-Type, etc
)

# Agrego todos los routers de los controllers al app
app.include_router(health_router)
app.include_router(auth_router)
app.include_router(events_router)
app.include_router(bouts_router)
app.include_router(picks_router)
app.include_router(leaderboard_router)


@app.get("/")
async def root():
    # Endpoint raíz, sirve para verificar que la API está levantada
    return {
        "name": "UFC Picks API",
        "version": "1.0.0",
        "docs": "/docs"  # Link a la documentación interactiva de Swagger
    }
