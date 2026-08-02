"""
PickService - Lógica de negocio para picks.

Maneja validaciones, reglas de bloqueo y puntuación.

IMPORTANTE: Usamos picked_fighter_name (nombre del peleador) en lugar de
picked_corner para evitar problemas cuando los corners cambian.
"""

from datetime import UTC, datetime
from typing import Optional

from pymongo.asynchronous.database import AsyncDatabase

from app.models.pick import Pick, PickCreate
from app.repositories.bout_repository import BoutRepository
from app.repositories.event_repository import EventRepository
from app.repositories.pick_repository import PickRepository
from app.services.pick_lock_service import evaluate_bout_pick_lock


class PickServiceError(Exception):
    """Error base del servicio de picks."""
    pass


class PickLockedError(PickServiceError):
    """Cuando intenta modificar un pick bloqueado."""
    pass


class EventNotFoundError(PickServiceError):
    """Evento no encontrado."""
    pass


class BoutNotFoundError(PickServiceError):
    """Pelea no encontrada."""
    pass


class InvalidPickError(PickServiceError):
    """Datos del pick inválidos."""
    pass


class PickService:
    def __init__(self, db: AsyncDatabase):
        self.db = db
        self.pick_repo = PickRepository(db)
        self.event_repo = EventRepository(db)
        self.bout_repo = BoutRepository(db)

    async def create_or_update_pick(
        self,
        user_id: str,
        pick_data: PickCreate
    ) -> Pick:
        """Crea o actualiza un pick con validaciones completas."""
        # Verificar que el evento exista
        event = await self.event_repo.get_by_id(pick_data.event_id)
        if not event:
            raise EventNotFoundError(f"Evento {pick_data.event_id} no encontrado")

        # Verificar que la pelea existe y pertenece al evento
        bout = await self.bout_repo.get_by_id(pick_data.bout_id)
        if not bout:
            raise BoutNotFoundError(f"Pelea {pick_data.bout_id} no encontrada")

        if bout.event_id != pick_data.event_id:
            raise InvalidPickError("La pelea no pertenece a este evento")

        # Si la pelea ya cerró o tiene resultado, no se pueden crear ni editar picks
        lock_state = evaluate_bout_pick_lock(event, bout)
        if lock_state.locked:
            messages = {
                "result": (
                    "No se pueden editar picks de peleas terminadas, "
                    "canceladas o con resultado"
                ),
                "admin_event": (
                    "Los picks están bloqueados por el admin en este evento"
                ),
                "admin_bout": (
                    "Los picks están bloqueados por el admin en esta pelea"
                ),
                "section_time": (
                    "Los picks cerraron al comenzar esta sección de la cartelera"
                ),
            }
            raise PickLockedError(
                messages.get(lock_state.reason, "Los picks están bloqueados")
            )

        # Verificar que el nombre del peleador sea válido
        fighters = bout.fighters or {}
        red_fighter = fighters.get("red", {})
        blue_fighter = fighters.get("blue", {})

        red_name = red_fighter.get("fighter_name", "") if isinstance(red_fighter, dict) else getattr(red_fighter, "fighter_name", "")
        blue_name = blue_fighter.get("fighter_name", "") if isinstance(blue_fighter, dict) else getattr(blue_fighter, "fighter_name", "")

        valid_fighters = [
            self._normalize_name(red_name),
            self._normalize_name(blue_name)
        ]

        picked_normalized = self._normalize_name(pick_data.picked_fighter_name)
        picked_fighter_id = await self._resolve_fighter_id(
            pick_data.bout_id, picked_normalized
        )
        if picked_normalized not in valid_fighters:
            raise InvalidPickError(
                f"Peleador '{pick_data.picked_fighter_name}' no está en esta pelea. "
                f"Válidos: {red_name}, {blue_name}"
            )

        existing_pick = await self.pick_repo.get_user_pick_for_bout(user_id, pick_data.bout_id)

        # Para DEC no se puede especificar round
        if pick_data.picked_method == "DEC" and pick_data.picked_round is not None:
            raise InvalidPickError("No se puede especificar round para decisión")

        if existing_pick:
            self._ensure_mission_bound_fields_unchanged(existing_pick, pick_data)

        now = datetime.now(UTC)
        pick_id = f"{user_id}:{pick_data.bout_id}"

        if existing_pick:
            # Actualizar pick existente
            return await self.pick_repo.update_pick(
                pick_id=pick_id,
                picked_fighter_name=pick_data.picked_fighter_name,
                picked_fighter_id=picked_fighter_id,
                picked_method=pick_data.picked_method,
                picked_round=pick_data.picked_round,
                updated_at=now
            )
        else:
            # Crear nuevo pick
            pick = Pick(
                _id=pick_id,
                user_id=user_id,
                event_id=pick_data.event_id,
                bout_id=pick_data.bout_id,
                picked_fighter_name=pick_data.picked_fighter_name,
                picked_fighter_id=picked_fighter_id,
                picked_method=pick_data.picked_method,
                picked_round=pick_data.picked_round,
                is_correct=None,
                points_awarded=0,
                locked=False,
                created_at=now,
                updated_at=None
            )
            return await self.pick_repo.create(pick)

    def _ensure_mission_bound_fields_unchanged(
        self,
        existing_pick: Pick,
        pick_data: PickCreate,
    ) -> None:
        locks = existing_pick.mission_field_locks
        changed_fields = []
        if locks.get("winner") and self._normalize_name(
            existing_pick.picked_fighter_name
        ) != self._normalize_name(pick_data.picked_fighter_name):
            changed_fields.append("winner")
        if locks.get("method") and existing_pick.picked_method != pick_data.picked_method:
            changed_fields.append("method")
        if locks.get("round") and existing_pick.picked_round != pick_data.picked_round:
            changed_fields.append("round")
        if changed_fields:
            raise PickLockedError(
                "Mission-bound pick fields cannot be changed: "
                + ", ".join(changed_fields)
            )

    async def _resolve_fighter_id(
        self,
        bout_id: int,
        picked_normalized: str,
    ) -> Optional[str]:
        """Resolve the stable fighter id for a pick (B-009).

        Reads the raw bout document because `card_data_v1` is a canonical sidecar
        that the `Bout` model deliberately does not expose. Returns None when the
        bout has not been through the CardData boundary yet, or when the name is
        ambiguous: a wrong id is far worse than no id, because scoring trusts the
        id ahead of the name.
        """
        document = await self.db["bouts"].find_one(
            {"id": bout_id}, {"card_data_v1.fighters": 1}
        )
        fighters = ((document or {}).get("card_data_v1") or {}).get("fighters") or []
        resolved = [
            fighter.get("fighter_id")
            for fighter in fighters
            if isinstance(fighter, dict)
            and fighter.get("fighter_id")
            and self._normalize_name(fighter.get("display_name", "")) == picked_normalized
        ]
        return resolved[0] if len(resolved) == 1 else None

    def _normalize_name(self, name: str) -> str:
        """Normaliza nombres para comparación (lowercase, sin espacios extras)."""
        if not name:
            return ""
        return " ".join(name.lower().strip().split())

    async def get_user_picks_for_event(
        self,
        user_id: str,
        event_id: int
    ) -> list[Pick]:
        """Obtiene todos los picks del usuario en un evento específico."""
        return await self.pick_repo.get_user_picks_for_event(user_id, event_id)

    async def get_all_user_picks(
        self,
        user_id: str,
        limit: int = 100
    ) -> list[Pick]:
        """Obtiene todos los picks del usuario en todos los eventos."""
        return await self.pick_repo.get_user_all_picks(user_id, limit)

    async def get_user_pick_for_bout(
        self,
        user_id: str,
        bout_id: int
    ) -> Optional[Pick]:
        """Obtiene un pick específico de una pelea."""
        return await self.pick_repo.get_user_pick_for_bout(user_id, bout_id)

    async def cleanup_pending_picks(self, user_id: str) -> int:
        """
        Elimina picks sin resultado en eventos ya completados.
        Si el evento terminó y el pick sigue pending, la pelea se canceló o no se puntuó.
        """
        # Buscar eventos completados
        completed_events = self.db["events"].find({"status": "completed"}, {"id": 1})
        completed_ids = [e["id"] async for e in completed_events]

        if not completed_ids:
            return 0

        # Borrar picks del usuario que estén pending en eventos completados
        result = await self.db["picks"].delete_many({
            "user_id": user_id,
            "event_id": {"$in": completed_ids},
            "is_correct": None
        })

        # Recalcular stats si se borró algo
        if result.deleted_count > 0:
            from app.services.points_service import PointsService
            points_service = PointsService(self.db)
            await points_service._update_user_stats(user_id)

        return result.deleted_count

    async def lock_picks_for_event(self, event_id: int) -> int:
        """Bloquea todos los picks de un evento."""
        return await self.pick_repo.lock_picks_for_event(event_id)
