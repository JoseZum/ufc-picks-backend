"""
Controlador de proxy de imágenes - Proxy para imágenes de Tapology

En producción no hay nginx, así que el backend hace de proxy para cachear
las imágenes de Tapology y evitar hotlinking.
"""

import httpx
from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import StreamingResponse
from functools import lru_cache
import hashlib
import time

router = APIRouter(prefix="/proxy", tags=["proxy"])

# Simple in-memory cache for images (in production, use Redis)
_image_cache: dict[str, tuple[bytes, str, float]] = {}
CACHE_TTL = 3600 * 24  # 24 hours
MAX_CACHE_SIZE = 100  # Max images to cache in memory


def _clean_old_cache():
    """Remove expired entries from cache."""
    now = time.time()
    expired = [k for k, v in _image_cache.items() if now - v[2] > CACHE_TTL]
    for k in expired:
        del _image_cache[k]

    # If still too large, remove oldest
    if len(_image_cache) > MAX_CACHE_SIZE:
        sorted_keys = sorted(_image_cache.keys(), key=lambda k: _image_cache[k][2])
        for k in sorted_keys[:len(_image_cache) - MAX_CACHE_SIZE]:
            del _image_cache[k]


@router.get("/tapology/{path:path}")
async def proxy_tapology_image(path: str):
    """
    Proxy images from Tapology's CDN.

    Path format: /proxy/tapology/poster_images/135755/profile/xxx.jpg
    Proxies to: https://images.tapology.com/poster_images/135755/profile/xxx.jpg
    """
    # Build the Tapology URL
    tapology_url = f"https://images.tapology.com/{path}"

    # Create cache key
    cache_key = hashlib.md5(tapology_url.encode()).hexdigest()

    # Check cache
    if cache_key in _image_cache:
        content, content_type, cached_at = _image_cache[cache_key]
        if time.time() - cached_at < CACHE_TTL:
            return Response(
                content=content,
                media_type=content_type,
                headers={
                    "Cache-Control": "public, max-age=86400",
                    "X-Cache": "HIT"
                }
            )

    # Clean old cache entries
    _clean_old_cache()

    # Fetch from Tapology
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                tapology_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer": "https://www.tapology.com/",
                    "Accept": "image/*,*/*;q=0.8",
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
            content_type = response.headers.get("content-type", "image/jpeg")

            # Cache the image
            _image_cache[cache_key] = (content, content_type, time.time())

            return Response(
                content=content,
                media_type=content_type,
                headers={
                    "Cache-Control": "public, max-age=86400",
                    "X-Cache": "MISS"
                }
            )

    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Timeout fetching image")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Error fetching image: {str(e)}")
