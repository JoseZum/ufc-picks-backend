"""Proxy de imágenes de Tapology con caché en memoria o S3."""

import hashlib
import time
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Response

from app.core.config import get_settings
from app.services.s3_service import S3NotConfiguredError, S3WriteNotAllowedError, get_s3_service

router = APIRouter(prefix="/proxy", tags=["proxy"])
settings = get_settings()

# Estrategia de caché configurada (MEMORY o S3)
CACHE_STRATEGY: Literal["MEMORY", "S3"] = settings.image_cache_strategy.upper()  # type: ignore

# In-memory cache
# Cache en memoria - solo para desarrollo
# Estructura: {cache_key: (image_bytes, content_type, etag, timestamp)}
_image_cache: dict[str, tuple[bytes, str, str, float]] = {}
CACHE_TTL = 3600 * 24 * 7  # 7 días de vida en memoria
MAX_CACHE_SIZE = 200  # Máximo de imágenes en memoria (evita consumo excesivo de RAM)


def _get_content_type(path: str, response_content_type: str | None) -> str:
    """
    Determina el content-type correcto de una imagen

    Prioriza el content-type de la respuesta HTTP, pero si no está disponible
    o no es válido, infiere desde la extensión del archivo.

    Args:
        path: Path de la imagen (ej: /poster_images/123/profile/image.jpg)
        response_content_type: Content-Type del response HTTP (puede ser None)

    Returns:
        MIME type apropiado (ej: "image/jpeg", "image/png")
    """
    # Si el response tiene un content-type válido de imagen, usarlo
    if response_content_type and response_content_type.startswith("image/"):
        return response_content_type

    # Si no, inferir desde la extensión del archivo
    path_lower = path.lower()
    if path_lower.endswith(".png"):
        return "image/png"
    elif path_lower.endswith(".gif"):
        return "image/gif"
    elif path_lower.endswith(".webp"):
        return "image/webp"
    elif path_lower.endswith(".svg"):
        return "image/svg+xml"

    # Default a JPEG (la mayoría de imágenes de Tapology son JPG)
    return "image/jpeg"


def _build_cache_headers(etag: str, cache_status: str) -> dict:
    """Headers de cache para browser y CDN.

    Las imagenes no cambian, asi que se cachean 7 dias y se siguen sirviendo
    viejas mientras revalidan o si el origen falla.
    """
    return {
        # Cache principal: 7 días, permite servir stale mientras revalida
        "Cache-Control": "public, max-age=604800, stale-while-revalidate=86400, stale-if-error=86400",
        # Vercel Edge: header específico para cache en edge de Vercel
        "CDN-Cache-Control": "public, max-age=604800",
        # Cloudflare: header específico para Cloudflare CDN
        "Cloudflare-CDN-Cache-Control": "public, max-age=604800",
        # ETag: permite al browser hacer requests condicionales (304 Not Modified)
        "ETag": f'"{etag}"',
        # Vary: asegura que el cache considere el header Accept (importante para content negotiation)
        "Vary": "Accept",
        # Headers de debug (útiles para troubleshooting)
        "X-Cache": cache_status,
        "X-Cache-Strategy": CACHE_STRATEGY,
        # CORS: permite que cualquier dominio use estas imágenes
        "Access-Control-Allow-Origin": "*",
    }


def _clean_old_cache():
    """
    Limpia entradas expiradas del cache en memoria

    Esta función hace dos cosas:
    1. Elimina imágenes más viejas que CACHE_TTL (7 días)
    2. Si aún hay demasiadas imágenes, elimina las más antiguas (FIFO)

    Se llama periódicamente antes de agregar nuevas imágenes al cache
    para evitar que crezca indefinidamente y consuma toda la RAM.
    """
    now = time.time()

    # Paso 1: eliminar entradas expiradas
    expired = [k for k, v in _image_cache.items() if now - v[3] > CACHE_TTL]
    for k in expired:
        del _image_cache[k]

    # Paso 2: si aún superamos el límite, eliminar las más viejas
    if len(_image_cache) > MAX_CACHE_SIZE:
        # Ordenar por timestamp (más vieja primero)
        sorted_keys = sorted(_image_cache.keys(), key=lambda k: _image_cache[k][3])
        # Eliminar las más viejas hasta llegar al límite
        to_remove = len(_image_cache) - MAX_CACHE_SIZE
        for k in sorted_keys[:to_remove]:
            del _image_cache[k]


async def _fetch_from_tapology(tapology_url: str, path: str) -> tuple[bytes, str]:
    """Descarga una imagen del CDN de Tapology.

    Los headers imitan a un navegador porque Tapology bloquea bots y aplica
    anti-hotlinking. Devuelve (bytes, content_type).
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            tapology_url,
            headers={
                # Simular navegador real
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                # Indicar que venimos de Tapology
                "Referer": "https://www.tapology.com/",
                # Aceptar formatos de imagen modernos
                "Accept": "image/webp,image/avif,image/*,*/*;q=0.8",
            },
            follow_redirects=True
        )

        if response.status_code == 404:
            raise HTTPException(status_code=404, detail="Image not found")

        if response.status_code != 200:
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Failed to fetch image: {response.status_code}"
            )

        content = response.content
        content_type = _get_content_type(path, response.headers.get("content-type"))

        return content, content_type


# In-memory strategy
async def _get_image_memory(clean_path: str, tapology_url: str, path: str) -> Response:
    """Sirve la imagen desde el cache en memoria, descargandola si falta.

    Es la estrategia para local y testing, donde no hay AWS. El cache se
    pierde al reiniciar.
    """
    # Generar key de cache usando hash MD5 del path
    cache_key = hashlib.md5(clean_path.encode()).hexdigest()

    # Verificar si está en cache y no expiró
    if cache_key in _image_cache:
        content, content_type, etag, cached_at = _image_cache[cache_key]
        if time.time() - cached_at < CACHE_TTL:
            # Cache hit - servir desde memoria
            return Response(
                content=content,
                media_type=content_type,
                headers=_build_cache_headers(etag, "HIT")
            )

    # Limpiar cache viejo antes de agregar nueva entrada
    _clean_old_cache()

    # Cache miss - descargar desde Tapology
    try:
        content, content_type = await _fetch_from_tapology(tapology_url, path)

        # Generar ETag desde hash del contenido
        etag = hashlib.md5(content).hexdigest()[:16]

        # Guardar en cache para próximas requests
        _image_cache[cache_key] = (content, content_type, etag, time.time())

        return Response(
            content=content,
            media_type=content_type,
            headers=_build_cache_headers(etag, "MISS")
        )

    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Timeout fetching image") from None
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Error fetching image: {str(e)}") from e


# S3 strategy
async def _get_image_s3(clean_path: str, tapology_url: str, path: str) -> Response:
    """Sirve la imagen desde S3 redirigiendo a CloudFront.

    Con IMAGE_SOURCE_MODE="s3" descarga y sube lo que falte; con "cache" solo
    lee y devuelve 404 si no esta. Se redirige en vez de servir directo porque
    el bucket es privado y CloudFront tiene edge locations.
    """
    s3_service = get_s3_service()

    try:
        # Generar key de S3 usando el servicio centralizado
        s3_key = s3_service.generate_tapology_cache_key(clean_path)

        # Paso 1: verificar si la imagen ya existe en S3
        exists = await s3_service.image_exists(s3_key)

        if exists:
            # Imagen existe en S3 - redirigir a CloudFront
            cloudfront_url = s3_service.get_cloudfront_url(s3_key)

            if cloudfront_url:
                # Redirigir a CloudFront (mejor performance)
                return Response(
                    status_code=302,
                    headers={
                        "Location": cloudfront_url,
                        "Cache-Control": "public, max-age=604800",
                        "X-Cache": "HIT-REDIRECT"
                    }
                )
            else:
                # CloudFront no configurado - servir directo desde S3
                content, content_type = await s3_service.get_image(s3_key)
                etag = hashlib.md5(content).hexdigest()[:16]
                return Response(
                    content=content,
                    media_type=content_type,
                    headers=_build_cache_headers(etag, "HIT")
                )

        # Paso 2: imagen no existe en S3
        # Verificar si estamos en modo solo-lectura
        if s3_service.is_read_only:
            # Modo cache: NO descargar ni subir, solo servir lo que existe
            raise HTTPException(
                status_code=404,
                detail="Image not found in S3 and write is disabled (mode: cache)"
            )

        # Paso 3: modo s3 - descargar desde Tapology y subir a S3
        try:
            content, content_type = await _fetch_from_tapology(tapology_url, path)

            # Subir a S3 con metadata
            await s3_service.upload_image(
                s3_key=s3_key,
                image_data=content,
                content_type=content_type,
                metadata={
                    "source": "tapology",
                    "original-path": clean_path
                }
            )

            # Redirigir a CloudFront
            cloudfront_url = s3_service.get_cloudfront_url(s3_key)

            if cloudfront_url:
                return Response(
                    status_code=302,
                    headers={
                        "Location": cloudfront_url,
                        "Cache-Control": "public, max-age=604800",
                        "X-Cache": "MISS-REDIRECT"
                    }
                )
            else:
                # CloudFront no configurado - servir directo
                etag = hashlib.md5(content).hexdigest()[:16]
                return Response(
                    content=content,
                    media_type=content_type,
                    headers=_build_cache_headers(etag, "MISS")
                )

        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="Timeout fetching image") from None
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Error fetching image: {str(e)}") from e

    except S3NotConfiguredError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    except S3WriteNotAllowedError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"S3 error: {str(e)}") from e


@router.get("/tapology/{path:path}")
async def proxy_tapology_image(path: str):
    """Proxy de imagenes de Tapology.

    `/proxy/tapology/<path>` va a `https://images.tapology.com/<path>`.
    IMAGE_CACHE_STRATEGY elige entre memoria y S3+CloudFront; con S3 devuelve
    un 302 al CDN en vez de la imagen.
    """
    # Limpiar query params del path para generar cache key consistente
    # Tapology agrega timestamps que cambian pero la imagen es la misma
    clean_path = path.split("?")[0]

    # Construir URL completa de Tapology
    tapology_url = f"https://images.tapology.com/{path}"

    # Rutear a la estrategia configurada
    if CACHE_STRATEGY == "S3":
        return await _get_image_s3(clean_path, tapology_url, path)
    else:
        return await _get_image_memory(clean_path, tapology_url, path)
