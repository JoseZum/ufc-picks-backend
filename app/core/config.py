"""
Configuración de la app cargada desde variables de entorno (.env)

Todo lo que varía entre desarrollo/producción va aquí
"""

from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # MongoDB
    mongodb_uri: str  # "mongodb+srv://user:pass@cluster.mongodb.net/"
    mongodb_db_name: str = "ufc_picks"  # Nombre de la base de datos

    # JWT - para firmar los tokens de autenticación
    jwt_secret: str  # Una cadena larga y aleatoria
    jwt_algorithm: str = "HS256"  # Algoritmo de encriptación
    jwt_expire_minutes: int = 60 * 24 * 7  # Los tokens expiran en 7 días

    # Google OAuth - para autenticación con Google
    google_client_id: str  # ID de la aplicación en Google Cloud Console
    google_client_secret: str | None = None  # Secret (solo necesario para server-side flows)

    # App
    app_env: str = "development"  # o "production"
    debug: bool = False

    # CORS - de dónde pueden venir los requests
    cors_origins: str = "http://localhost:3000"  # URLs separadas por coma

    class Config:
        env_file = ".env"  # Lee desde el archivo .env
        env_file_encoding = "utf-8"
        extra = "ignore"  # Ignora campos extras del .env que no estén en el modelo


@lru_cache()
def get_settings() -> Settings:
    """Retorna la instancia de configuración (cacheada para no releerla)"""
    return Settings()
