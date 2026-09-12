"""Contratos de transporte estables para la frontera del sistema de misiones.

Cada campo llega listo para pintar. El cliente renderiza estos strings y
números tal cual, nunca recalcula progreso, elegibilidad, XP ni locks.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.modules.missions.domain.enums import MissionInteractionType

MISSION_API_VERSION = "1"
MISSION_CATALOG_VERSION = "2026.08.01"

class MissionCapabilitiesResponse(BaseModel):
    """Capacidades del renderer, compartidas entre backend y gateway del frontend."""

    model_config = ConfigDict(frozen=True)

    api_version: Literal["1"] = MISSION_API_VERSION
    catalog_version: str = MISSION_CATALOG_VERSION
    interaction_types: tuple[MissionInteractionType, ...] = (
        MissionInteractionType.AUTO,
        MissionInteractionType.TARGET_FIGHTER,
        MissionInteractionType.TARGET_FIGHT,
        MissionInteractionType.COMBO_BUILDER,
        MissionInteractionType.CARD_PROP,
    )


class MissionTransport(BaseModel):
    model_config = ConfigDict(frozen=True)


class MissionOfferView(MissionTransport):
    """Una opción seleccionable dentro de un slot."""

    offer_id: str
    mission_id: str
    name: str
    description: str
    difficulty: Literal["EASY", "MEDIUM", "HARD"]
    xp: int
    interaction: MissionInteractionType
    pick_effect: Literal["NONE", "UPSERT_ONE", "UPSERT_MANY"]
    selection_prompt: str | None = None
    selection_spec: dict[str, Any] | None = None


class SelectionPartView(MissionTransport):
    """Una pieza de lo que el usuario eligió, ya lista para pintar.

    Va partida porque la UI estiliza cada rol aparte, y así el método sale
    ya traducido (`KO/TKO`, no `KO_TKO`).
    """

    label: str | None = None
    value: str
    detail: str | None = None


class SelectedMissionView(MissionTransport):
    """Una selección irreversible y su progreso resuelto."""

    assignment_id: str
    event_id: int
    event_label: str | None = None
    slot: int
    offer_id: str | None = None
    mission_id: str
    name: str
    description: str
    difficulty: Literal["EASY", "MEDIUM", "HARD"]
    xp: int
    xp_earned: int = 0
    interaction: MissionInteractionType
    status: Literal["ACTIVE", "COMPLETED", "FAILED", "VOID"]
    progress_text: str = ""
    progress_percent: int = Field(default=0, ge=0, le=100)
    selection_summary: str | None = None
    selection_parts: tuple["SelectionPartView", ...] = ()
    selection: dict[str, Any] | None = None
    void_reason: str | None = None


class MissionSlotView(MissionTransport):
    slot: int = Field(ge=1, le=3)
    selected: SelectedMissionView | None = None
    options: tuple[MissionOfferView, ...] = ()


class MonthlyMissionView(MissionTransport):
    month_key: str
    mission_id: str
    name: str
    description: str
    xp: int
    status: Literal["ACTIVE", "COMPLETED", "FAILED", "VOID"]
    progress_text: str = ""
    progress_percent: int = Field(default=0, ge=0, le=100)


class CelebrationView(MissionTransport):
    id: str
    kind: str
    presentation: str
    heading: str
    message: str
    #: Payload tipado (nivel, título, streak, XP) para que el cliente no
    #: tenga que parsear `heading`/`message`.
    metadata: dict[str, Any] = Field(default_factory=dict)


class StreakCardView(MissionTransport):
    """Qué le hizo al streak una card ya liquidada."""

    event_id: int
    event_label: str | None = None
    outcome: Literal["ADVANCED", "BROKEN", "UNCHANGED"]
    picked: int
    denominator: int
    coverage_percent: int = Field(ge=0, le=100)
    streak_after: int
    milestone: int | None = None
    xp_earned: int = 0


class HomeMissionsResponse(MissionTransport):
    event_id: int
    card_state: Literal["OPEN", "CLOSED", "VOID"]
    offer_set_id: str | None = None
    card_revision: int | None = None
    monthly: MonthlyMissionView | None = None
    slots: tuple[MissionSlotView, ...] = ()
    current_streak: int = 0
    best_streak: int = 0
    locked: bool = False
    lock_reason: str | None = None


class ProfileMissionsResponse(MissionTransport):
    lifetime_xp: int
    level: int
    title: str
    xp_into_level: int
    xp_for_next_level: int
    level_progress_pct: int
    next_title: str | None = None
    next_title_level: int | None = None
    current_streak: int
    best_streak: int
    #: Texto final del próximo hito, ej. "5 → +3 XP".
    next_streak_milestone_label: str = ""
    #: True cuando la última card liquidada rompió el streak.
    streak_just_broke: bool = False
    monthly: MonthlyMissionView | None = None
    active: tuple[SelectedMissionView, ...] = ()
    history: tuple[SelectedMissionView, ...] = ()
    streak_history: tuple[StreakCardView, ...] = ()
    celebrations: tuple[CelebrationView, ...] = ()


class PublicMissionProfileResponse(MissionTransport):
    """Lo que un usuario puede ver del historial de misiones de OTRO.

    Subconjunto de `ProfileMissionsResponse`: sin celebraciones (del dueño)
    ni misiones activas (nadie más puede leer una apuesta en curso).
    """

    user_id: str
    lifetime_xp: int
    level: int
    title: str
    xp_into_level: int
    xp_for_next_level: int
    level_progress_pct: int
    current_streak: int
    best_streak: int
    missions_completed: int
    missions_settled: int
    #: Misiones liquidadas, más nuevas primero, mismo límite que Profile.
    history: tuple[SelectedMissionView, ...] = ()
    #: Últimas ocho completadas, se mantiene por clientes viejos.
    recent: tuple[SelectedMissionView, ...] = ()


class SelectMissionRequest(MissionTransport):
    event_id: int = Field(gt=0)
    slot: Literal[1, 2, 3]
    offer_id: str
    idempotency_key: str = Field(min_length=8, max_length=128)
    selection: dict[str, Any] | None = None
    # El pick debe quedar COMPLETE; sin pick previo no hay método/round que
    # heredar, así que el cliente los manda aquí (forma validada en el dominio).
    pick_patches: list[dict[str, Any]] = Field(default_factory=list, max_length=6)


class MonthlyTemplateView(MissionTransport):
    """Una de las 18 plantillas revisadas, con los límites que Admin puede elegir."""

    mission_id: str
    name: str
    description: str
    xp: int
    compatibility: str
    parameters: list[dict[str, Any]]


class MonthlyConfigView(MissionTransport):
    month_key: str
    mission_id: str
    name: str
    description: str
    state: Literal["DRAFT", "ACTIVE", "CLOSED"]
    xp: int
    parameters: dict[str, int]
    starts_at: datetime
    ends_at: datetime
    activated_at: datetime | None = None
    closed_at: datetime | None = None
    #: False en cuanto el mes arranca o deja DRAFT; la UI bloquea edición.
    editable: bool


class UpsertMonthlyConfigRequest(MissionTransport):
    mission_id: str
    #: Se omite para usar los defaults revisados de esa plantilla.
    parameters: dict[str, int] | None = None


class CardControlView(MissionTransport):
    """La ventana de misiones que Admin controla sobre una card."""

    event_id: int
    state: Literal["OPEN", "CLOSED", "VOID"]
    reason: str | None = None
    actor_id: str | None = None
    updated_at: datetime | None = None
    #: Cuántas asignaciones ACTIVE liquidó un VOID. 0 en close/reopen.
    voided_assignments: int = 0
    #: Misiones ya elegidas en esta card. El operador lo necesita ANTES de
    #: pulsar VOID porque liquida todas.
    selected_assignments: int = 0
    revision: int = 0


class CardControlActionRequest(MissionTransport):
    #: Obligatorio: una acción Admin sobre estado en vivo debe decir por qué.
    reason: str = Field(min_length=3, max_length=240)


class ReconciliationPreviewView(MissionTransport):
    """Un plan de reparación que no escribe. `apply` debe repetir `plan_id`."""

    preview_version: str
    plan_id: str
    current_digest: str
    desired_digest: str
    converged: bool
    safe_to_apply: bool
    operations: list[dict[str, Any]] = Field(default_factory=list)
    unchanged_entities: list[str] = Field(default_factory=list)
    blockers: list[dict[str, Any]] = Field(default_factory=list)


class ReconciliationApplyRequest(MissionTransport):
    #: El plan que el operador realmente revisó. Un id viejo da 409.
    plan_id: str
    reason: str = Field(min_length=3, max_length=240)
    event_id: int | None = None
    user_id: str | None = None
    assignment_id: str | None = None
