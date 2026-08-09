"""Stable transport contracts for the mission-system boundary.

Every field is presentation-ready. The client renders these strings and numbers
as-is; it never recomputes progress, eligibility, XP or lock rules.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.modules.missions.domain.enums import MissionInteractionType

MISSION_API_VERSION = "1"
MISSION_CATALOG_VERSION = "2026.08.01"

class MissionCapabilitiesResponse(BaseModel):
    """Renderer capabilities shared by the backend and frontend gateway."""

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
    """One selectable option inside a slot."""

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

    Se manda partida porque la UI estiliza cada rol por separado: el peleador
    destaca y el método lo acompaña discreto. Mientras solo viajaba la frase
    plana, la superficie tenía que partirla por puntuación o mostrarlo todo con
    el mismo peso, y el método salía además con la ortografía interna
    (`KO_TKO` en vez de `KO/TKO`).
    """

    label: str | None = None
    value: str
    detail: str | None = None


class SelectedMissionView(MissionTransport):
    """An irreversible selection and its resolved progress."""

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
    #: The typed payload the surface renders (level, title, streak, XP). Carried
    #: through so the client never has to parse `heading`/`message` back apart.
    metadata: dict[str, Any] = Field(default_factory=dict)


class StreakCardView(MissionTransport):
    """What one settled card did to the streak."""

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
    #: Finished copy for the next milestone, e.g. "5 → +3 XP".
    next_streak_milestone_label: str = ""
    #: True when the most recently settled card broke the streak.
    streak_just_broke: bool = False
    monthly: MonthlyMissionView | None = None
    active: tuple[SelectedMissionView, ...] = ()
    history: tuple[SelectedMissionView, ...] = ()
    streak_history: tuple[StreakCardView, ...] = ()
    celebrations: tuple[CelebrationView, ...] = ()


class PublicMissionProfileResponse(MissionTransport):
    """What one user may see about ANOTHER user's mission record.

    Deliberately a subset of `ProfileMissionsResponse`: no celebrations (they
    are unacknowledged notifications addressed to their owner) and no active
    missions (an in-flight selection is a bet nobody else has a right to read
    before the card settles). Everything here is already-public standing —
    level, title, XP and the missions the user finished.
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
    #: Most recently finished missions, newest first.
    recent: tuple[SelectedMissionView, ...] = ()


class SelectMissionRequest(MissionTransport):
    event_id: int = Field(gt=0)
    slot: Literal[1, 2, 3]
    offer_id: str
    idempotency_key: str = Field(min_length=8, max_length=128)
    selection: dict[str, Any] | None = None
    # A mission that binds a winner still has to write a COMPLETE canonical
    # pick, and several missions leave the method or the round to the user.
    # On a bout the user never picked there is nothing to inherit them from,
    # so the client sends the missing fields here. Shapes are validated by
    # `CanonicalPickPatch` in the domain, not restated.
    pick_patches: list[dict[str, Any]] = Field(default_factory=list, max_length=6)


class MissionErrorResponse(MissionTransport):
    code: str
    message: str


class MonthlyTemplateView(MissionTransport):
    """One of the 18 reviewed templates, with the bounds Admin may pick inside."""

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
    #: False once the month starts or leaves DRAFT — the UI disables editing.
    editable: bool


class UpsertMonthlyConfigRequest(MissionTransport):
    mission_id: str
    #: Omit to take the reviewed defaults for that template.
    parameters: dict[str, int] | None = None


class CardControlView(MissionTransport):
    """The mission window Admin controls on one card."""

    event_id: int
    state: Literal["OPEN", "CLOSED", "VOID"]
    reason: str | None = None
    actor_id: str | None = None
    updated_at: datetime | None = None
    #: How many ACTIVE assignments a VOID settled. 0 for close/reopen.
    voided_assignments: int = 0
    #: Missions users have chosen on this card. An operator needs this BEFORE
    #: pressing VOID, because VOID settles every one of them.
    selected_assignments: int = 0
    revision: int = 0


class CardControlActionRequest(MissionTransport):
    #: Mandatory: an Admin action on live user state must say why.
    reason: str = Field(min_length=3, max_length=240)


class ReconciliationPreviewView(MissionTransport):
    """A no-write repair plan. `plan_id` is what `apply` must echo back."""

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
    #: The plan the operator actually reviewed. A stale id is a 409.
    plan_id: str
    reason: str = Field(min_length=3, max_length=240)
    event_id: int | None = None
    user_id: str | None = None
    assignment_id: str | None = None
