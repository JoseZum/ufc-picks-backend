"""
Seguridad: Manejo de JWT y verificación de tokens de Google OAuth
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from jose import JWTError, jwt

from app.core.config import get_settings

settings = get_settings()

# URL del endpoint de Google para verificar tokens
GOOGLE_TOKEN_INFO_URL = "https://oauth2.googleapis.com/tokeninfo"


class GoogleAuthError(Exception):
    """Se lanza cuando falla la verificación del token de Google"""
    pass


class JWTError(Exception):
    """Se lanza cuando falla algo con los JWT"""
    pass


async def verify_google_token(id_token: str) -> dict:
    """
    Verifica que el id_token de Google sea válido y retorna info del usuario
    
    Lo que retorna: {sub, email, name, picture}
    
    Lanza GoogleAuthError si algo está mal
    """
    import logging
    logger = logging.getLogger(__name__)
    
    logger.info(f"🔍 Verificando Google token...")
    logger.info(f"🔑 GOOGLE_CLIENT_ID configurado: {settings.google_client_id}")
    
    async with httpx.AsyncClient() as client:
        response = await client.get(
            GOOGLE_TOKEN_INFO_URL,
            params={"id_token": id_token}
        )

        if response.status_code != 200:
            error_msg = f"Token de Google inválido. Status: {response.status_code}, Response: {response.text}"
            logger.error(f"❌ {error_msg}")
            raise GoogleAuthError(error_msg)

        data = response.json()
        logger.info(f"✅ Token verificado por Google. aud={data.get('aud')}, email={data.get('email')}")

        # Verifico que el token fue emitido para nuestra aplicación (no para otra)
        token_aud = data.get("aud")
        if token_aud != settings.google_client_id:
            error_msg = f"Token no fue emitido para esta aplicación. Expected: {settings.google_client_id}, Got: {token_aud}"
            logger.error(f"❌ {error_msg}")
            raise GoogleAuthError(error_msg)

        # Verifico que el token no haya expirado
        exp = int(data.get("exp", 0))
        if datetime.now(timezone.utc).timestamp() > exp:
            error_msg = "Token expirado"
            logger.error(f"❌ {error_msg}")
            raise GoogleAuthError(error_msg)

        logger.info(f"✅ Token válido para user: {data.get('email')}")
        return {
            "sub": data.get("sub"),  # ID único del usuario en Google
            "email": data.get("email"),
            "name": data.get("name"),
            "picture": data.get("picture"),
        }


def create_access_token(user_id: str, email: str) -> str:
    """
    Crea un JWT para que el usuario pueda hacer requests autenticados
    
    El JWT contiene el user_id y expira en 7 días
    """
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)

    payload = {
        "sub": user_id,      # Subject: el usuario
        "email": email,
        "exp": expire,       # Expiración
        "iat": datetime.now(timezone.utc),  # Issued at (cuándo se creó)
    }

    # Firmo el token con nuestra clave secreta
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> Optional[dict]:
    """
    Decodifica y valida un JWT
    
    Retorna el payload si es válido, None si está expirado o corrupto
    """
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm]
        )
        return payload
    except Exception:
        # Token inválido, expirado, o corrupto
        return None
