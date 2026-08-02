"""Admin HTTP boundary for the monthly mission programme.

Admin picks one of the 18 reviewed templates for a month and fixes its
parameters before the month starts. Every mutation writes an audit row, because
these decisions change what every user is asked to do.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, status

from app.core.dependencies import CurrentAdmin, Database
from app.modules.missions.application.card_control import (
    CardControlError,
    CardControlService,
)
from app.modules.missions.application.monthly_config import MonthlyConfigService
from app.modules.missions.application.monthly_progress import MonthlyProgressService
from app.modules.missions.application.reconciliation import (
    MissionReconciliationService,
    ReconciliationError,
)
from app.modules.missions.catalog import load_monthly_catalog
from app.modules.missions.contracts import (
    CardControlActionRequest,
    CardControlView,
    MonthlyConfigView,
    MonthlyTemplateView,
    ReconciliationApplyRequest,
    ReconciliationPreviewView,
    UpsertMonthlyConfigRequest,
)
from app.modules.missions.domain.enums import MissionTransitionReason
from app.modules.missions.domain.monthly import (
    MonthlyConfigError,
    MonthlyMissionConfig,
)
from app.modules.missions.domain.reconciliation import (
    ReconciliationInputError,
    ReconciliationScope,
)
from app.modules.missions.domain.state_machines import IllegalMissionTransition

router = APIRouter(prefix="/admin/missions", tags=["admin", "missions"])

_STATUS_BY_CODE = {
    "CONFIG_NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "UNKNOWN_MISSION": status.HTTP_400_BAD_REQUEST,
    "INVALID_MONTH": status.HTTP_400_BAD_REQUEST,
    "INVALID_PARAMETERS": status.HTTP_400_BAD_REQUEST,
    "CONFIG_ALREADY_EXISTS": status.HTTP_409_CONFLICT,
    "CONFIG_FROZEN": status.HTTP_409_CONFLICT,
    "MONTH_ALREADY_STARTED": status.HTTP_409_CONFLICT,
    "MONTH_NOT_FINISHED": status.HTTP_409_CONFLICT,
}


def _fail(error: MonthlyConfigError) -> HTTPException:
    return HTTPException(
        status_code=_STATUS_BY_CODE.get(error.code.value, status.HTTP_400_BAD_REQUEST),
        detail={"code": error.code.value, "message": str(error)},
    )


def _view(
    config: MonthlyMissionConfig,
    catalog,
    *,
    has_progress: bool = False,
) -> MonthlyConfigView:
    definition = catalog.get(config.mission_id)
    return MonthlyConfigView(
        month_key=config.month_key,
        mission_id=config.mission_id,
        name=definition.ui.name if definition else config.mission_id,
        description=definition.ui.description if definition else "",
        state=config.state.value,
        xp=config.xp,
        parameters=config.parameters,
        starts_at=config.starts_at,
        ends_at=config.ends_at,
        activated_at=config.activated_at,
        closed_at=config.closed_at,
        editable=(
            config.state.value == "DRAFT"
            and not has_progress
            and datetime.now(UTC) <= config.ends_at
        ),
    )


async def _audit(db, *, admin_id: str, action: str, month_key: str, payload: dict) -> None:
    await db["mission_admin_audit"].insert_one(
        {
            "actor_id": admin_id,
            "action": action,
            "month_key": month_key,
            "payload": payload,
            "created_at": datetime.now(UTC),
        }
    )


def _services(db):
    catalog = load_monthly_catalog()
    return catalog, MonthlyConfigService(db, catalog=catalog)


@router.get("/monthly/templates", response_model=list[MonthlyTemplateView])
async def list_monthly_templates(_admin: CurrentAdmin) -> list[MonthlyTemplateView]:
    """The 18 reviewed templates, with the bounds Admin may choose inside."""
    catalog = load_monthly_catalog()
    return [
        MonthlyTemplateView(
            mission_id=definition.mission_id,
            name=definition.ui.name,
            description=definition.ui.description,
            xp=definition.xp,
            compatibility=definition.compatibility.value,
            parameters=[
                {
                    "key": parameter.key,
                    "label": parameter.label,
                    "kind": parameter.kind.value,
                    "default": parameter.default,
                    "minimum": parameter.minimum,
                    "maximum": parameter.maximum,
                }
                for parameter in definition.admin_parameters
            ],
        )
        for definition in sorted(catalog.values(), key=lambda item: item.mission_id)
    ]


@router.get("/monthly/{month_key}", response_model=MonthlyConfigView)
async def get_monthly_config(
    month_key: str,
    _admin: CurrentAdmin,
    db: Database,
) -> MonthlyConfigView:
    catalog, service = _services(db)
    try:
        config = await service.require(month_key)
    except MonthlyConfigError as error:
        raise _fail(error) from error
    return _view(config, catalog)


@router.put("/monthly/{month_key}", response_model=MonthlyConfigView)
async def upsert_monthly_config(
    month_key: str,
    request: UpsertMonthlyConfigRequest,
    admin: CurrentAdmin,
    db: Database,
) -> MonthlyConfigView:
    """Create the month's draft, or edit it while it is still editable."""
    catalog, service = _services(db)
    try:
        existing = await service.get(month_key)
        config = (
            await service.update_draft(
                month_key=month_key,
                mission_id=request.mission_id,
                parameters=request.parameters,
            )
            if existing
            else await service.create_draft(
                month_key=month_key,
                mission_id=request.mission_id,
                parameters=request.parameters,
            )
        )
    except MonthlyConfigError as error:
        raise _fail(error) from error

    await _audit(
        db,
        admin_id=str(getattr(admin, "id", "") or getattr(admin, "google_id", "")),
        action="monthly.upsert",
        month_key=month_key,
        payload={"mission_id": config.mission_id, "parameters": config.parameters},
    )
    return _view(config, catalog)


@router.post("/monthly/{month_key}/activate", response_model=MonthlyConfigView)
async def activate_monthly_config(
    month_key: str,
    admin: CurrentAdmin,
    db: Database,
) -> MonthlyConfigView:
    catalog, service = _services(db)
    try:
        config = await service.activate(month_key=month_key)
    except MonthlyConfigError as error:
        raise _fail(error) from error
    except IllegalMissionTransition as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "ILLEGAL_TRANSITION", "message": str(error)},
        ) from error

    await _audit(
        db,
        admin_id=str(getattr(admin, "id", "") or getattr(admin, "google_id", "")),
        action="monthly.activate",
        month_key=month_key,
        payload={"mission_id": config.mission_id},
    )
    return _view(config, catalog)


@router.post("/monthly/{month_key}/close", response_model=MonthlyConfigView)
async def close_monthly_config(
    month_key: str,
    admin: CurrentAdmin,
    db: Database,
    force: bool = False,
) -> MonthlyConfigView:
    """Close the month and settle everyone still short of the target.

    `force=true` is the explicit Admin close for a month that has not ended yet;
    without it a running month refuses to close.
    """
    catalog, service = _services(db)
    try:
        config = await service.close(
            month_key=month_key,
            reason=(
                MissionTransitionReason.ADMIN_CLOSE
                if force
                else MissionTransitionReason.MONTH_CLOSE
            ),
        )
        settled = await MonthlyProgressService(
            db, catalog=catalog
        ).close_month(month_key=month_key)
    except MonthlyConfigError as error:
        raise _fail(error) from error
    except IllegalMissionTransition as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "ILLEGAL_TRANSITION", "message": str(error)},
        ) from error

    await _audit(
        db,
        admin_id=str(getattr(admin, "id", "") or getattr(admin, "google_id", "")),
        action="monthly.close",
        month_key=month_key,
        payload={"forced": force, "settled": len(settled)},
    )
    return _view(config, catalog)


# ---------------------------------------------------------------------- cards


_CARD_STATUS_BY_CODE = {
    "CARD_NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "ALREADY_VOID": status.HTTP_409_CONFLICT,
    "REASON_REQUIRED": status.HTTP_400_BAD_REQUEST,
}


def _actor(admin) -> str:
    return str(getattr(admin, "id", "") or getattr(admin, "google_id", ""))


def _card_view(state) -> CardControlView:
    return CardControlView(
        event_id=state.event_id,
        state=state.state.value,
        reason=state.reason,
        actor_id=state.actor_id,
        updated_at=state.updated_at,
        voided_assignments=state.voided_assignments,
        revision=state.revision,
    )


def _card_fail(error: CardControlError) -> HTTPException:
    return HTTPException(
        status_code=_CARD_STATUS_BY_CODE.get(
            error.code.value, status.HTTP_400_BAD_REQUEST
        ),
        detail={"code": error.code.value, "message": str(error)},
    )


@router.get("/cards/{event_id}", response_model=CardControlView)
async def get_card_control(
    event_id: int,
    _admin: CurrentAdmin,
    db: Database,
) -> CardControlView:
    return _card_view(await CardControlService(db).state_for(event_id))


@router.post("/cards/{event_id}/{action}", response_model=CardControlView)
async def act_on_card(
    event_id: int,
    action: str,
    request: CardControlActionRequest,
    admin: CurrentAdmin,
    db: Database,
) -> CardControlView:
    """Close, reopen or VOID a card's mission window.

    VOID is irreversible and settles every ACTIVE assignment on the card, so the
    reason is mandatory and the whole transition is audited.
    """
    service = CardControlService(db)
    handler = {
        "close": service.close,
        "reopen": service.reopen,
        "void": service.void,
    }.get(action)
    if handler is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "UNKNOWN_ACTION", "message": f"Unknown card action {action!r}"},
        )

    actor_id = _actor(admin)
    try:
        state = await handler(event_id=event_id, actor_id=actor_id, reason=request.reason)
    except CardControlError as error:
        raise _card_fail(error) from error
    except IllegalMissionTransition as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "ILLEGAL_TRANSITION", "message": str(error)},
        ) from error

    await _audit(
        db,
        admin_id=actor_id,
        action=f"card.{action}",
        month_key="",
        payload={
            "event_id": event_id,
            "reason": request.reason,
            "state": state.state.value,
            "voided_assignments": state.voided_assignments,
        },
    )
    return _card_view(state)


# ------------------------------------------------------------- reconciliation


def _scope(
    event_id: int | None, user_id: str | None, assignment_id: str | None
) -> ReconciliationScope:
    try:
        return ReconciliationScope(
            event_id=event_id, user_id=user_id, assignment_id=assignment_id
        )
    except ReconciliationInputError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_SCOPE", "message": str(error)},
        ) from error


@router.get("/reconciliation/preview", response_model=ReconciliationPreviewView)
async def preview_reconciliation(
    _admin: CurrentAdmin,
    db: Database,
    event_id: int | None = None,
    user_id: str | None = None,
    assignment_id: str | None = None,
) -> ReconciliationPreviewView:
    """Build the repair plan. Writes nothing, ever."""
    preview = await MissionReconciliationService(db).preview(
        _scope(event_id, user_id, assignment_id)
    )
    return ReconciliationPreviewView(
        preview_version=preview.preview_version,
        plan_id=preview.plan_id,
        current_digest=preview.current_digest,
        desired_digest=preview.desired_digest,
        converged=preview.converged,
        safe_to_apply=preview.safe_to_apply,
        operations=[
            {
                "operation_id": operation.operation_id,
                "action": operation.action.value,
                "entity_type": operation.entity_type.value,
                "entity_id": operation.entity_id,
                "impact": operation.impact.value,
                "changed_fields": list(operation.changed_fields),
                "before": dict(operation.before or {}),
                "after": dict(operation.after),
            }
            for operation in preview.operations
        ],
        unchanged_entities=list(preview.unchanged_entities),
        blockers=[
            {
                "code": blocker.code,
                "message": blocker.message,
                "blocking": blocker.blocking,
            }
            for blocker in preview.blockers
        ],
    )


@router.post("/reconciliation/apply")
async def apply_reconciliation(
    request: ReconciliationApplyRequest,
    admin: CurrentAdmin,
    db: Database,
) -> dict:
    """Apply a previously previewed plan, under compare-and-set."""
    scope = _scope(request.event_id, request.user_id, request.assignment_id)
    try:
        outcome = await MissionReconciliationService(db).apply(
            scope,
            plan_id=request.plan_id,
            actor_id=_actor(admin),
            reason=request.reason,
        )
    except ReconciliationError as error:
        raise HTTPException(
            status_code=(
                status.HTTP_409_CONFLICT
                if error.code.value == "PLAN_STALE"
                else status.HTTP_400_BAD_REQUEST
            ),
            detail={"code": error.code.value, "message": str(error)},
        ) from error

    return {
        "plan_id": outcome.plan_id,
        "applied": outcome.applied,
        "skipped": outcome.skipped,
        "converged": outcome.converged,
    }
