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

    # ==================== Configuración de Imágenes ====================
    # Controla cómo se manejan las imágenes en el sistema
    # - "memory": Cache temporal en memoria del servidor (desarrollo/testing)
    # - "s3": Almacenamiento persistente en AWS S3 (producción)
    # - "cache": Solo lectura desde CDN, nunca escribe (modo solo-cache)
    image_cache_strategy: str = "MEMORY"

    # Modo de origen de imágenes - controla si se permite escritura en S3
    # - "s3": Modo completo - lee de S3, sube si no existe (escritura habilitada)
    # - "cache": Modo solo lectura - solo sirve desde CDN, nunca sube a S3
    # Esto es útil para ambientes de staging que no deben modificar producción
    image_source_mode: str = "s3"

    # ==================== AWS S3 y CloudFront ====================
    # Configuración de almacenamiento en S3 y CDN CloudFront
    # Solo necesario cuando image_cache_strategy="S3"
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_region: str = "us-east-1"
    aws_s3_bucket: str | None = None

    # Dominio de CloudFront - SIEMPRE debe usarse en lugar de URLs directas de S3
    # El backend solo maneja keys (ej: "events/ufc-324.jpg"), nunca URLs completas
    # CloudFront construye: https://{cloudfront_domain}/{key}
    aws_cloudfront_domain: str | None = None

    class Config:
        env_file = ".env"  # Lee desde el archivo .env
        env_file_encoding = "utf-8"
        extra = "ignore"  # Ignora campos extras del .env que no estén en el modelo


@lru_cache()
def get_settings() -> Settings:
    """Retorna la instancia de configuración (cacheada para no releerla)"""
    return Settings()
