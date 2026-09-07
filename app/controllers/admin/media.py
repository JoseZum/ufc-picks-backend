"""Subida y borrado de imágenes de evento y peleador."""

import logging
import os

from fastapi import APIRouter, File, HTTPException, Request, UploadFile, status

from app.controllers.admin.shared import get_event_or_404
from app.core.dependencies import CurrentAdmin, Database
from app.core.rate_limit import limiter
from app.services.s3_service import S3ServiceError, S3WriteNotAllowedError, get_s3_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/events/{event_id}/event-art")
@limiter.limit("30/minute")
async def upload_event_art(
    request: Request,
    event_id: int,
    admin: CurrentAdmin,
    db: Database,
    file: UploadFile = File(...)
):
    """Sube una imagen personalizada para un evento."""
    # Verificar que el evento existe
    await get_event_or_404(db, event_id)

    # Validar que sea una imagen soportada
    valid_extensions = ['.avif', '.png', '.jpg', '.jpeg', '.webp']
    if not file.filename or not any(file.filename.lower().endswith(ext) for ext in valid_extensions):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Tipos válidos: {', '.join(valid_extensions)}"
        )

    # Mapear extensión a content type
    content_type = file.content_type or 'application/octet-stream'
    if file.filename:
        ext = file.filename.lower().split('.')[-1]
        content_type_map = {
            'avif': 'image/avif',
            'png': 'image/png',
            'jpg': 'image/jpeg',
            'jpeg': 'image/jpeg',
            'webp': 'image/webp'
        }
        content_type = content_type_map.get(ext, content_type)

    try:
        # Leer y guardar la imagen
        image_data = await file.read()

        await db["events"].update_one(
            {"id": event_id},
            {"$set": {
                "event_art": image_data,
                "event_art_content_type": content_type
            }}
        )

        return {
            "success": True,
            "message": f"Imagen subida para evento {event_id}",
            "size_bytes": len(image_data),
            "content_type": content_type
        }

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error al subir: {str(e)}"
        ) from e


@router.delete("/events/{event_id}/event-art")
@limiter.limit("30/minute")
async def delete_event_art(
    request: Request,
    event_id: int,
    admin: CurrentAdmin,
    db: Database
):
    """Elimina la imagen personalizada de un evento."""
    # Verificar que el evento existe
    await get_event_or_404(db, event_id)

    # Eliminar imagen de MongoDB
    await db["events"].update_one(
        {"id": event_id},
        {"$unset": {"event_art": "", "event_art_content_type": ""}}
    )

    return {
        "success": True,
        "message": f"Imagen eliminada para evento {event_id}"
    }


# Event timing endpoints

@router.post("/fighters/photo")
@limiter.limit("30/minute")
async def upload_fighter_photo(
    request: Request,
    admin: CurrentAdmin,
    file: UploadFile = File(...)
):
    """Sube una foto de peleador a S3."""
    # Validar que sea un formato de imagen soportado por el frontend
    valid_extensions = ['.png', '.jpg', '.jpeg', '.webp', '.avif']
    if not file.filename or not any(file.filename.lower().endswith(ext) for ext in valid_extensions):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Solo se aceptan archivos: {', '.join(valid_extensions)}"
        )

    # Sanitizar filename para evitar path traversal
    filename = os.path.basename(file.filename)
    if not filename or filename.startswith('.'):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Nombre de archivo inválido"
        )

    # Determinar content type según extensión
    ext = filename.lower().split('.')[-1]
    content_type_map = {
        'png': 'image/png',
        'jpg': 'image/jpeg',
        'jpeg': 'image/jpeg',
        'webp': 'image/webp',
        'avif': 'image/avif',
    }
    content_type = content_type_map[ext]

    # Construir key S3: fighters/{filename}
    s3_key = f"fighters/{filename}"

    try:
        image_data = await file.read()
        s3_service = get_s3_service()
        await s3_service.upload_image(s3_key, image_data, content_type=content_type)

        # Generar URL de CloudFront
        cloudfront_url = s3_service.get_cloudfront_url(s3_key)

        return {
            "success": True,
            "message": f"Foto subida: {filename}",
            "s3_key": s3_key,
            "cloudfront_url": cloudfront_url,
            "size_bytes": len(image_data),
            "content_type": content_type
        }

    except S3WriteNotAllowedError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Escritura en S3 no permitida (modo cache activo)"
        ) from None
    except S3ServiceError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error de S3: {str(e)}"
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error al subir foto: {str(e)}"
        ) from e


# Bout deletion endpoint
