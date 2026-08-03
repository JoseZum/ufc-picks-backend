"""Authenticated HTTP boundary for the mission system."""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import ValidationError

from app.core.config import get_settings
from app.core.dependencies import CurrentUser, Database
from app.modules.missions.access import user_can_see_missions
from app.modules.missions.application.celebration_queue import (
    CelebrationQueueError,
    CelebrationQueueService,
)
from app.modules.missions.application.read_models import MissionReadService
from app.modules.missions.application.selection import (
    MissionSelectionError,
    MissionSelectionService,
)
from app.modules.missions.catalog import load_card_catalog
from app.modules.missions.contracts import (
    HomeMissionsResponse,
    MissionCapabilitiesResponse,
    ProfileMissionsResponse,
    PublicMissionProfileResponse,
    SelectedMissionView,
    SelectMissionRequest,
)
from app.modules.missions.domain.selections import SelectMissionCommand

router = APIRouter(prefix="/missions", tags=["missions"])

#: Offer draws are HMAC-stable per user+card, so the same secret must produce the
#: same three slots on every refresh. Derived from the app secret, never printed.
def _offer_secret() -> bytes:
    return hashlib.sha256(
        f"mission-offers:{get_settings().jwt_secret}".encode()
    ).digest()


def _user_id(user) -> str:
    return str(getattr(user, "id", None) or getattr(user, "username", ""))


def _require_access(user) -> None:
    """The launch gate (CAL-004).

    404 rather than 403 on purpose: while the feature is dark, a user outside
    the canary should not be able to tell that missions exist at all.
    """
    if user_can_see_missions(
        _user_id(user), getattr(user, "email", None)
    ):
        return
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "MISSIONS_UNAVAILABLE", "message": "Missions are not available"},
    )


@router.get("/capabilities", response_model=MissionCapabilitiesResponse)
async def get_mission_capabilities(
    _current_user: CurrentUser,
) -> MissionCapabilitiesResponse:
    """Return the renderer contract supported by this API version."""

    _require_access(_current_user)

    return MissionCapabilitiesResponse()


@router.get("/home", response_model=HomeMissionsResponse)
async def get_home_missions(
    event_id: int,
    current_user: CurrentUser,
    db: Database,
) -> HomeMissionsResponse:
    """The monthly mission and three card slots for one event."""

    _require_access(current_user)
    service = MissionReadService(db, offer_secret=_offer_secret())
    return await service.home(user_id=_user_id(current_user), event_id=event_id)


@router.get("/profile", response_model=ProfileMissionsResponse)
async def get_profile_missions(
    current_user: CurrentUser,
    db: Database,
) -> ProfileMissionsResponse:
    """XP, level, title, streak, active missions, history and celebrations."""

    _require_access(current_user)

    service = MissionReadService(db, offer_secret=_offer_secret())
    return await service.profile(user_id=_user_id(current_user))


@router.get("/users/{user_id}", response_model=PublicMissionProfileResponse)
async def public_mission_profile(
    user_id: str,
    current_user: CurrentUser,
    db: Database,
) -> PublicMissionProfileResponse:
    """Another user's mission standing, for the profile card.

    Requires a session — this is a logged-in social surface, not an open API —
    and answers 404 for an unknown user rather than an empty record, so the
    endpoint cannot be used to enumerate who exists.
    """

    _require_access(current_user)

    owner = await db["users"].find_one(
        {"$or": [{"_id": user_id}, {"google_id": user_id}]}, {"_id": 1}
    )
    if owner is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "USER_NOT_FOUND", "message": "No such user"},
        )

    service = MissionReadService(db, offer_secret=_offer_secret())
    full = await service.profile(user_id=user_id)

    settled = [row for row in full.history]
    completed = [row for row in settled if row.status == "COMPLETED"]
    return PublicMissionProfileResponse(
        user_id=user_id,
        lifetime_xp=full.lifetime_xp,
        level=full.level,
        title=full.title,
        xp_into_level=full.xp_into_level,
        xp_for_next_level=full.xp_for_next_level,
        level_progress_pct=full.level_progress_pct,
        current_streak=full.current_streak,
        best_streak=full.best_streak,
        missions_completed=len(completed),
        missions_settled=len(settled),
        recent=tuple(completed[:8]),
    )


@router.post(
    "/select",
    response_model=SelectedMissionView,
    status_code=status.HTTP_201_CREATED,
)
async def select_mission(
    request: SelectMissionRequest,
    current_user: CurrentUser,
    db: Database,
) -> SelectedMissionView:
    """Irreversibly activate one offer and upsert any canonical picks it owns."""

    _require_access(current_user)
    user_id = _user_id(current_user)
    read_service = MissionReadService(db, offer_secret=_offer_secret())
    home = await read_service.home(user_id=user_id, event_id=request.event_id)
    if home.offer_set_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "CARD_NOT_FOUND", "message": "Event has no mission offers"},
        )
    if home.locked:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "SLOT_LOCKED",
                "message": home.lock_reason or "Card missions are locked",
            },
        )

    # A retry must be idempotent. Once a slot is taken its options are empty, so
    # resolve an already-selected slot before looking the offer up.
    taken = next(
        (slot.selected for slot in home.slots if slot.slot == request.slot),
        None,
    )
    if taken is not None:
        if taken.offer_id == request.offer_id:
            return taken
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "ALREADY_SELECTED",
                "message": "This slot already holds a different mission",
            },
        )

    # The interaction type is server truth: derive the selection discriminator
    # from the offer instead of trusting a client-supplied `kind`.
    offer = next(
        (
            option
            for slot in home.slots
            if slot.slot == request.slot
            for option in slot.options
            if option.offer_id == request.offer_id
        ),
        None,
    )
    if offer is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "OFFER_NOT_FOUND",
                "message": "That offer is not available in this slot",
            },
        )
    selection = {**(request.selection or {}), "kind": offer.interaction.value}

    selection_service = MissionSelectionService(db, load_card_catalog())
    try:
        result = await selection_service.select(
            user_id=user_id,
            command=SelectMissionCommand(
                event_id=request.event_id,
                slot=request.slot,
                offer_set_id=home.offer_set_id,
                offer_id=request.offer_id,
                idempotency_key=request.idempotency_key,
                selection=selection,
                pick_patches=tuple(request.pick_patches),
            ),
        )
    except ValidationError as error:
        # A malformed patch is the client's mistake, not a 500. The domain
        # models are the only place patch shapes are defined.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "INVALID_SELECTION",
                "message": "Pick completion fields are invalid",
            },
        ) from error
    except MissionSelectionError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": error.code.value, "message": str(error)},
        ) from error

    assignment = await db["mission_assignments"].find_one(
        {"_id": result.assignment_id}
    )
    if assignment is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "ASSIGNMENT_MISSING", "message": "Selection did not persist"},
        )
    return read_service.selected_view(assignment)


@router.post(
    "/celebrations/{celebration_id}/ack",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def acknowledge_celebration(
    celebration_id: str,
    current_user: CurrentUser,
    db: Database,
) -> Response:
    """Acknowledge one pending celebration. Repeats are harmless."""

    _require_access(current_user)

    service = CelebrationQueueService(db)
    try:
        await service.acknowledge(
            user_id=_user_id(current_user),
            celebration_id=celebration_id,
        )
    except CelebrationQueueError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": error.code.value, "message": str(error)},
        ) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)
