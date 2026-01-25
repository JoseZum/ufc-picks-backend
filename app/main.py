"""
Entry point de la API
"""

import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import get_settings
from app.database import Database

from app.controllers.auth_controller import router as auth_router
from app.controllers.events_controller import router as events_router
from app.controllers.bouts_controller import router as bouts_router
from app.controllers.picks_controller import router as picks_router
from app.controllers.leaderboard_controller import router as leaderboard_router
from app.controllers.health_controller import router as health_router
from app.controllers.proxy_controller import router as proxy_router

settings = get_settings()

# Parse CORS origins
CORS_ORIGINS = [origin.strip() for origin in settings.cors_origins.split(",")]
CORS_ORIGIN_REGEX = re.compile(r"https://.*\.vercel\.app") if settings.app_env == "production" else None


def is_allowed_origin(origin: str) -> bool:
    """Check if origin is allowed by explicit list or regex pattern."""
    if not origin:
        return False
    if origin in CORS_ORIGINS:
        return True
    if CORS_ORIGIN_REGEX and CORS_ORIGIN_REGEX.match(origin):
        return True
    return False


class CORSMiddleware(BaseHTTPMiddleware):
    """
    Custom CORS middleware that handles OPTIONS preflight BEFORE routing.

    This fixes the issue where FastAPI's query parameter validation
    causes OPTIONS requests to fail with 400.
    """

    async def dispatch(self, request: Request, call_next):
        origin = request.headers.get("origin", "")

        # Handle preflight OPTIONS request IMMEDIATELY
        if request.method == "OPTIONS":
            if is_allowed_origin(origin):
                return Response(
                    status_code=200,
                    headers={
                        "Access-Control-Allow-Origin": origin,
                        "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS, PATCH",
                        "Access-Control-Allow-Headers": "Authorization, Content-Type, Accept, Origin, X-Requested-With",
                        "Access-Control-Allow-Credentials": "true",
                        "Access-Control-Max-Age": "86400",  # Cache preflight for 24 hours
                    }
                )
            else:
                # Origin not allowed
                return Response(status_code=403, content="Origin not allowed")

        # For non-OPTIONS requests, proceed normally and add CORS headers to response
        response = await call_next(request)

        if is_allowed_origin(origin):
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"

        return response


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

# Add custom CORS middleware (handles OPTIONS before routing)
app.add_middleware(CORSMiddleware)

# Agrego todos los routers de los controllers al app
app.include_router(health_router)
app.include_router(auth_router)
app.include_router(events_router)
app.include_router(bouts_router)
app.include_router(picks_router)
app.include_router(leaderboard_router)
app.include_router(proxy_router)


@app.get("/")
async def root():
    # Endpoint raíz, sirve para verificar que la API está levantada
    return {
        "name": "UFC Picks API",
        "version": "1.0.0",
        "docs": "/docs"  # Link a la documentación interactiva de Swagger
    }
