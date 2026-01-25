"""
Controlador de proxy de imágenes - Proxy para imágenes de Tapology

En producción no hay nginx, así que el backend hace de proxy para cachear
las imágenes de Tapology y evitar hotlinking.

Headers de cache optimizados para:
- Browser cache
- Vercel Edge cache
- Cloudflare CDN cache
"""

import httpx
from fastapi import APIRouter, HTTPException, Response
import hashlib
import time

router = APIRouter(prefix="/proxy", tags=["proxy"])

# Simple in-memory cache for images (in production, use Redis)
_image_cache: dict[str, tuple[bytes, str, str, float]] = {}  # key -> (content, content_type, etag, timestamp)
CACHE_TTL = 3600 * 24 * 7  # 7 days in memory
MAX_CACHE_SIZE = 200  # Max images to cache in memory


def _get_content_type(path: str, response_content_type: str | None) -> str:
    """Determine correct content type from path or response."""
    if response_content_type and response_content_type.startswith("image/"):
        return response_content_type

    path_lower = path.lower()
    if path_lower.endswith(".png"):
        return "image/png"
    elif path_lower.endswith(".gif"):
        return "image/gif"
    elif path_lower.endswith(".webp"):
        return "image/webp"
    elif path_lower.endswith(".svg"):
        return "image/svg+xml"
    return "image/jpeg"


def _build_cache_headers(etag: str, cache_status: str) -> dict:
    """Build optimized cache headers for browsers and CDNs."""
    return {
        # Browser + CDN caching: 7 days, serve stale while revalidating
        "Cache-Control": "public, max-age=604800, stale-while-revalidate=86400, stale-if-error=86400",
        # Vercel Edge specific
        "CDN-Cache-Control": "public, max-age=604800",
        # Cloudflare specific
        "Cloudflare-CDN-Cache-Control": "public, max-age=604800",
        # ETag for conditional requests
        "ETag": f'"{etag}"',
        # Vary header for proper cache key
        "Vary": "Accept",
        # Debug header
        "X-Cache": cache_status,
        # Allow CORS for images
        "Access-Control-Allow-Origin": "*",
    }


def _clean_old_cache():
    """Remove expired entries from cache."""
    now = time.time()
    expired = [k for k, v in _image_cache.items() if now - v[3] > CACHE_TTL]
    for k in expired:
        del _image_cache[k]

    # If still too large, remove oldest
    if len(_image_cache) > MAX_CACHE_SIZE:
        sorted_keys = sorted(_image_cache.keys(), key=lambda k: _image_cache[k][3])
        for k in sorted_keys[:len(_image_cache) - MAX_CACHE_SIZE]:
            del _image_cache[k]


@router.get("/tapology/{path:path}")
async def proxy_tapology_image(path: str):
    """
    Proxy images from Tapology's CDN with aggressive caching.

    Path format: /proxy/tapology/poster_images/135755/profile/xxx.jpg
    Proxies to: https://images.tapology.com/poster_images/135755/profile/xxx.jpg

    Cache layers:
    1. In-memory cache (backend) - 7 days
    2. Browser cache - 7 days
    3. Vercel Edge cache - 7 days
    4. Cloudflare CDN - 7 days (if configured)
    """
    # Remove query params for cache key (Tapology adds timestamps)
    clean_path = path.split("?")[0]

    # Build the Tapology URL
    tapology_url = f"https://images.tapology.com/{path}"

    # Create cache key from clean path
    cache_key = hashlib.md5(clean_path.encode()).hexdigest()

    # Check in-memory cache
    if cache_key in _image_cache:
        content, content_type, etag, cached_at = _image_cache[cache_key]
        if time.time() - cached_at < CACHE_TTL:
            return Response(
                content=content,
                media_type=content_type,
                headers=_build_cache_headers(etag, "HIT")
            )

    # Clean old cache entries periodically
    _clean_old_cache()

    # Fetch from Tapology
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                tapology_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer": "https://www.tapology.com/",
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

            # Generate ETag from content hash
            etag = hashlib.md5(content).hexdigest()[:16]

            # Cache the image
            _image_cache[cache_key] = (content, content_type, etag, time.time())

            return Response(
                content=content,
                media_type=content_type,
                headers=_build_cache_headers(etag, "MISS")
            )

    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Timeout fetching image")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Error fetching image: {str(e)}")
