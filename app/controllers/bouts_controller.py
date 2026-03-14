"""
Controlador de peleas - Endpoints relacionados con bouts
"""

from typing import Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.core.dependencies import Database
from app.services.event_service import EventService, EventNotFoundError
from app.services.s3_service import get_s3_service, S3NotConfiguredError


router = APIRouter(tags=["bouts"])

# Cache global para resolución de image_key (se resetea al reiniciar el servidor)
_image_key_cache: dict[str, str] = {}
_image_extension_priority = ("png", "jpg", "jpeg", "webp", "avif", "gif")


def _resolve_image_key(image_key: str) -> str:
    """Buscar la mejor versión disponible de la imagen en extensiones conocidas."""
    if image_key in _image_key_cache:
        return _image_key_cache[image_key]

    base, ext = image_key.rsplit('.', 1) if '.' in image_key else (image_key, '')
    candidate_keys = [image_key]
    for candidate_ext in _image_extension_priority:
        candidate_key = f"{base}.{candidate_ext}"
        if candidate_key not in candidate_keys:
            candidate_keys.append(candidate_key)

    try:
        s3 = get_s3_service()
        for candidate_key in candidate_keys:
            try:
                s3.s3_client.head_object(Bucket=s3.settings.aws_s3_bucket, Key=candidate_key)
                _image_key_cache[image_key] = candidate_key
                return candidate_key
            except Exception:
                continue
    except Exception:
        pass

    # Si no podemos verificar en S3 o ninguna variante existe, usar la key almacenada.
    fallback_key = image_key if ext else f"{base}.jpg"
    _image_key_cache[image_key] = fallback_key
    return fallback_key


def _process_fighters(fighters: dict) -> dict:
    """
    Procesa el diccionario de peleadores, normalizando campos y generando profile_image_url.

    Normalización de campos:
    - ufc_ranking -> ranking (frontend espera 'ranking')
    - Asegura que height, reach estén en formato correcto

    Estrategia de imagen:
    1. Si image_key existe (ya subido a S3 por el spider) -> usar CloudFront
    2. Si no hay image_key -> el frontend mostrará placeholder

    Nota: El proxy on-demand no funciona porque Tapology bloquea requests directos
    (bot protection). Las imágenes se obtienen via el spider fighter_images que
    corre periódicamente y sube a S3.
    """
    if not fighters:
        return fighters

    processed = {}

    for corner in ["red", "blue"]:
        fighter = fighters.get(corner, {})
        if not fighter:
            processed[corner] = fighter
            continue

        # Copy fighter data
        processed_fighter = dict(fighter)

        # === FIELD NORMALIZATION FOR FRONTEND ===

        # bout_details historically stores `name`; frontend/backend expect `fighter_name`
        if not processed_fighter.get("fighter_name"):
            processed_fighter["fighter_name"] = processed_fighter.get("name") or "TBD"
        if not processed_fighter.get("name") and processed_fighter.get("fighter_name"):
            processed_fighter["name"] = processed_fighter["fighter_name"]

        # Map ufc_ranking to ranking (frontend expects 'ranking')
        if not processed_fighter.get("ranking") and processed_fighter.get("ufc_ranking"):
            processed_fighter["ranking"] = processed_fighter["ufc_ranking"]

        # Keep both last_fights and last_5_fights compatible
        if not processed_fighter.get("last_5_fights") and processed_fighter.get("last_fights"):
            processed_fighter["last_5_fights"] = processed_fighter["last_fights"]
        if not processed_fighter.get("last_fights") and processed_fighter.get("last_5_fights"):
            processed_fighter["last_fights"] = processed_fighter["last_5_fights"]

        # Fill convenience scalar fields from structured stats when missing
        if not processed_fighter.get("age_at_fight_years") and isinstance(processed_fighter.get("age_at_fight"), dict):
            processed_fighter["age_at_fight_years"] = processed_fighter["age_at_fight"].get("years")
        if not processed_fighter.get("height_cm") and isinstance(processed_fighter.get("height"), dict):
            processed_fighter["height_cm"] = processed_fighter["height"].get("cm")
        if not processed_fighter.get("reach_cm") and isinstance(processed_fighter.get("reach"), dict):
            processed_fighter["reach_cm"] = processed_fighter["reach"].get("cm")
        
        # Ensure corner field is set
        if not processed_fighter.get("corner"):
            processed_fighter["corner"] = corner

        # === IMAGE URL PROCESSING ===

        # Always prioritize image_key (CloudFront) over old profile_image_url (proxy URLs)
        image_key = fighter.get("image_key")
        if image_key:
            resolved_key = _resolve_image_key(image_key)
            try:
                s3_service = get_s3_service()
                cloudfront_url = s3_service.get_cloudfront_url(resolved_key)
                if cloudfront_url:
                    processed_fighter["profile_image_url"] = cloudfront_url
                else:
                    # Fallback to proxy for S3 images if CloudFront not configured
                    processed_fighter["profile_image_url"] = f"/proxy/tapology/{image_key}"
            except S3NotConfiguredError:
                processed_fighter["profile_image_url"] = f"/proxy/tapology/{image_key}"
        # If no image_key but has old profile_image_url, keep it (backward compatibility)
        # If neither exists, frontend will show placeholder

        processed[corner] = processed_fighter

    return processed


def _merge_detail_fighters(detail_fighters: dict, bout_fighters: dict) -> dict:
    """Merge canonical bout fighter fields into bout_details before API normalization."""
    if not detail_fighters:
        return bout_fighters or {}

    merged = {}
    for corner in ["red", "blue"]:
        detail_fighter = dict(detail_fighters.get(corner, {}) or {})
        bout_fighter = dict(bout_fighters.get(corner, {}) or {})

        if not detail_fighter:
            merged[corner] = bout_fighter
            continue

        for field in (
            "fighter_name",
            "name",
            "corner",
            "tapology_id",
            "tapology_url",
            "image_key",
            "profile_image_url",
            "last_fights",
            "last_5_fights",
            "ranking",
            "ufc_ranking",
        ):
            if bout_fighter.get(field) and not detail_fighter.get(field):
                detail_fighter[field] = bout_fighter[field]

        if bout_fighter.get("fighter_name") and not detail_fighter.get("fighter_name"):
            detail_fighter["fighter_name"] = bout_fighter["fighter_name"]
        if detail_fighter.get("name") and not detail_fighter.get("fighter_name"):
            detail_fighter["fighter_name"] = detail_fighter["name"]

        merged[corner] = detail_fighter

    return merged


class FighterResponse(BaseModel):
    """Datos del peleador en el momento de la pelea."""
    fighter_name: str
    corner: str
    nationality: str
    record_at_fight: dict
    ranking: Optional[dict] = None
    age_at_fight_years: int
    height_cm: Optional[int] = None
    reach_cm: Optional[int] = None
    fighting_out_of: Optional[str] = None
    tapology_id: Optional[str] = None
    tapology_url: Optional[str] = None
    profile_image_url: Optional[str] = None


class BoutResultResponse(BaseModel):
    """Resultado de la pelea."""
    winner: str
    method: str
    round: Optional[int] = None
    time: Optional[str] = None


class BoutResponse(BaseModel):
    """Datos de la pelea devueltos por la API."""
    id: int
    event_id: int
    weight_class: str
    gender: str
    rounds_scheduled: int
    is_title_fight: bool
    status: str
    fighters: dict
    result: Optional[dict] = None
    picks_locked: bool = False


@router.get("/events/{event_id}/bouts", response_model=list[BoutResponse])
async def get_event_bouts(
    event_id: int,
    db: Database
):
    """
    Obtener todas las peleas de un evento.

    Devuelve las peleas con la información de los peleadores y el resultado (si está disponible).
    Incluye datos detallados de bout_details si existen.
    """
    event_service = EventService(db)

    try:
        bouts = await event_service.get_event_bouts(event_id)
    except EventNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Event {event_id} not found"
        )

    # Obtener todos los bout_details para este evento de una vez (más eficiente)
    bout_ids = [b.id for b in bouts]
    bout_details_cursor = db["bout_details"].find({"bout_id": {"$in": bout_ids}})
    bout_details_list = await bout_details_cursor.to_list(length=None)
    bout_details_map = {bd["bout_id"]: bd for bd in bout_details_list}

    result = []
    for b in bouts:
        # Convert Pydantic FighterSnapshot models to dicts for easier manipulation
        # b.fighters is dict[str, FighterSnapshot], need to convert to dict[str, dict]
        bout_fighters_dict = {}
        for corner, fighter in (b.fighters or {}).items():
            if hasattr(fighter, 'model_dump'):
                bout_fighters_dict[corner] = fighter.model_dump()
            elif hasattr(fighter, 'dict'):
                bout_fighters_dict[corner] = fighter.dict()
            else:
                bout_fighters_dict[corner] = dict(fighter) if fighter else {}

        fighters = bout_fighters_dict
        bout_result = b.result
        bout_detail = bout_details_map.get(b.id)

        if bout_detail:
            # Usar los datos detallados que incluyen height, reach, record, etc.
            if "fighters" in bout_detail:
                fighters = _merge_detail_fighters(bout_detail["fighters"], bout_fighters_dict)
            if "result" in bout_detail and bout_detail["result"]:
                bout_result = bout_detail["result"]

        result.append(
            BoutResponse(
                id=b.id,
                event_id=b.event_id,
                weight_class=b.weight_class or "Unknown",
                gender=b.gender or "male",
                rounds_scheduled=b.rounds_scheduled or 3,
                is_title_fight=b.is_title_fight,
                status=b.status,
                fighters=_process_fighters(fighters),
                result=bout_result,
                picks_locked=getattr(b, 'picks_locked', False)
            )
        )

    return result


@router.get("/events/{event_id}/fights/{bout_id}", response_model=BoutResponse)
async def get_bout_details(
    event_id: int,
    bout_id: int,
    db: Database
):
    """
    Obtener detalles completos de una pelea específica.

    Devuelve toda la información de la pelea incluyendo:
    - Información detallada de ambos peleadores
    - Rankings, records, odds
    - Físico (altura, reach, peso)
    - Gimnasios, nacionalidades
    - Resultado (si está disponible)
    """
    # Buscar la pelea básica
    bout_data = await db["bouts"].find_one({"id": bout_id, "event_id": event_id})

    if not bout_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Bout {bout_id} not found in event {event_id}"
        )

    # Buscar los detalles de la pelea en la colección bout_details
    bout_details = await db["bout_details"].find_one({"bout_id": bout_id})

    # Merge fighters data - usar bout_details si existe, sino usar bout básico
    bout_fighters = bout_data.get("fighters", {})
    fighters = bout_fighters
    if bout_details and "fighters" in bout_details:
        fighters = _merge_detail_fighters(bout_details.get("fighters", {}), bout_fighters)

    # Obtener resultado de bout_details si existe
    result = None
    if bout_details and "result" in bout_details:
        result = bout_details.get("result")
    elif "result" in bout_data:
        result = bout_data.get("result")

    # Mapear los datos a la respuesta
    return BoutResponse(
        id=bout_data.get("id"),
        event_id=bout_data.get("event_id"),
        weight_class=bout_data.get("weight_class", "Unknown"),
        gender=bout_data.get("gender", "M"),
        rounds_scheduled=bout_data.get("scheduled_rounds", 3),
        is_title_fight=bout_data.get("is_title_fight", False),
        status=bout_data.get("status", "scheduled"),
        fighters=_process_fighters(fighters),
        result=result,
        picks_locked=bout_data.get("picks_locked", False)
    )
