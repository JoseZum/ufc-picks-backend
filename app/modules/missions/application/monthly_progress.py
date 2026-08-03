"""Monthly mission progress, finalization and its single 15 XP reward.

A month accumulates one immutable summary per finished event. Re-recording an
event replaces its summary and recomputes the month, so a late result correction
converges instead of double-counting. The 15 XP award is keyed by user+month, so
retries, replays and concurrent writers can only ever produce one award.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.asynchronous.database import AsyncDatabase

from app.modules.missions.application.celebration_queue import CelebrationQueueService
from app.modules.missions.application.monthly_config import MonthlyConfigService
from app.modules.missions.application.xp_ledger import XpLedgerService
from app.modules.missions.domain.celebrations import (
    CelebrationKind,
    CelebrationPresentation,
    EnqueueCelebrationCommand,
)
from app.modules.missions.domain.enums import (
    MissionTransitionReason,
    MonthlyConfigState,
    MonthlyProgressStatus,
    StringEnum,
)
from app.modules.missions.domain.monthly import (
    MONTHLY_MISSION_XP,
    MonthlyConfigError,
    MonthlyConfigErrorCode,
    MonthlyMissionConfig,
    MonthlyMissionDefinition,
    month_key_for,
)
from app.modules.missions.domain.monthly_metrics import (
    MonthlyEvaluationContext,
    MonthlyEventSummary,
    evaluate_monthly_metric,
    resolve_monthly_observation,
)
from app.modules.missions.domain.resolution import (
    MetricResolution,
    MetricResolutionStatus,
)
from app.modules.missions.domain.state_machines import (
    ensure_monthly_progress_transition,
)
from app.modules.missions.domain.xp import (
    AwardXpCommand,
    CompensateXpCommand,
    XpSourceType,
)

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class MonthlyProgressErrorCode(StringEnum):
    MONTH_NOT_ACTIVE = "MONTH_NOT_ACTIVE"
    UNKNOWN_MISSION = "UNKNOWN_MISSION"
    EVENT_OUTSIDE_MONTH = "EVENT_OUTSIDE_MONTH"


class MonthlyProgressError(ValueError):
    def __init__(self, code: MonthlyProgressErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class MonthlyProgressResult:
    user_id: str
    month_key: str
    mission_id: str
    status: MonthlyProgressStatus
    resolution: MetricResolution
    xp_delta: int
    replayed: bool


def _progress_id(user_id: str, month_key: str) -> str:
    value = f"{user_id}\x1f{month_key}"
    return f"monthprog_{hashlib.sha256(value.encode()).hexdigest()[:24]}"


def _award_key(user_id: str, month_key: str, cycle: int) -> str:
    """Idempotency key for one award attempt.

    The cycle only advances when a correction compensated the previous award, so
    a retry or replay reuses the same key while a genuine re-earn gets a new one.
    """
    return f"monthly-mission:{user_id}:{month_key}:{cycle}"


class MonthlyProgressService:
    def __init__(
        self,
        db: AsyncDatabase,
        *,
        catalog: Mapping[str, MonthlyMissionDefinition],
        clock: Clock = _utc_now,
    ) -> None:
        self.db = db
        self.collection = db["mission_monthly_progress"]
        self.catalog = catalog
        self.clock = clock
        self.config_service = MonthlyConfigService(db, catalog=catalog, clock=clock)
        self.xp = XpLedgerService(db, clock=clock)
        self.celebrations = CelebrationQueueService(db, clock=clock)

    # ------------------------------------------------------------------ month

    async def month_key_for_event(self, event_id: int) -> str | None:
        """The month an event belongs to, taken from its official CardData date.

        Falls back to the legacy event date so a card that has not been through
        the CardData boundary yet still lands in a month instead of vanishing.
        """
        event = await self.db["events"].find_one(
            {"id": event_id},
            {"card_data_v1": 1, "date": 1, "event_date": 1},
        )
        if not event:
            return None
        official = (event.get("card_data_v1") or {}).get("official_date")
        moment = official or event.get("date") or event.get("event_date")
        if moment is None:
            return None
        if isinstance(moment, str):
            moment = datetime.fromisoformat(moment.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return month_key_for(moment)

    async def event_moment(self, event_id: int) -> datetime | None:
        """When an event happened, by the same rule that assigns it a month."""
        event = await self.db["events"].find_one(
            {"id": event_id},
            {"card_data_v1": 1, "date": 1, "event_date": 1},
        )
        if not event:
            return None
        official = (event.get("card_data_v1") or {}).get("official_date")
        moment = official or event.get("date") or event.get("event_date")
        if moment is None:
            return None
        if isinstance(moment, str):
            moment = datetime.fromisoformat(moment.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment

    # --------------------------------------------------------------- progress

    async def record_event_summary(
        self,
        *,
        user_id: str,
        summary: MonthlyEventSummary,
        session: AsyncClientSession | None = None,
    ) -> MonthlyProgressResult | None:
        """Fold one finished event into the user's month and re-resolve it.

        Returns ``None`` when the month has no ACTIVE configuration, so a card
        outside the programme simply contributes nothing.
        """
        config = await self.config_service.get(summary.month_key)
        if config is None or config.state == MonthlyConfigState.DRAFT:
            return None

        # A month activated part-way through only counts what happens after it
        # opens. Without this, activating August late would retroactively fold
        # in cards that ran while nobody had been told the month existed — and
        # a result correction on one of those old cards would quietly do the
        # same thing months later.
        if not await self._is_within_activation(config, summary.event_id):
            return None

        definition = self._definition(config)
        document = await self._load_or_create(user_id, config, session=session)

        summaries = {
            int(key): MonthlyEventSummary.model_validate(value)
            for key, value in (document.get("event_summaries") or {}).items()
        }
        previous = summaries.get(summary.event_id)
        if previous is not None and previous == summary:
            # Exact replay of an event we already folded in: nothing changes.
            return await self._resolve_and_persist(
                user_id,
                config,
                definition,
                document,
                summaries,
                session=session,
                replayed=True,
            )
        summaries[summary.event_id] = summary
        return await self._resolve_and_persist(
            user_id,
            config,
            definition,
            document,
            summaries,
            session=session,
            replayed=False,
        )

    async def _is_within_activation(self, config, event_id: int) -> bool:
        """Whether this event happened once the month was already open.

        An event with no resolvable date is counted rather than dropped: losing
        a user's month over missing card metadata is the worse failure.
        """
        activated_at = getattr(config, "activated_at", None)
        if activated_at is None:
            return True
        moment = await self.event_moment(event_id)
        if moment is None:
            return True
        return moment >= activated_at

    async def close_month(
        self,
        *,
        month_key: str,
        session: AsyncClientSession | None = None,
    ) -> tuple[MonthlyProgressResult, ...]:
        """Settle every still-ACTIVE participant once the month is CLOSED."""
        config = await self.config_service.require(month_key)
        if config.state != MonthlyConfigState.CLOSED:
            raise MonthlyConfigError(
                MonthlyConfigErrorCode.MONTH_NOT_FINISHED,
                f"{month_key} must be CLOSED before its progress can settle",
            )
        definition = self._definition(config)
        cursor = self.collection.find(
            {"month_key": month_key, "status": MonthlyProgressStatus.ACTIVE.value},
            session=session,
        )
        results = []
        for document in await cursor.to_list(length=None):
            summaries = {
                int(key): MonthlyEventSummary.model_validate(value)
                for key, value in (document.get("event_summaries") or {}).items()
            }
            results.append(
                await self._resolve_and_persist(
                    document["user_id"],
                    config,
                    definition,
                    document,
                    summaries,
                    session=session,
                    replayed=False,
                )
            )
        return tuple(results)

    async def get(self, *, user_id: str, month_key: str) -> dict | None:
        return await self.collection.find_one(
            {"_id": _progress_id(user_id, month_key)}
        )

    # ---------------------------------------------------------------- helpers

    def _definition(self, config: MonthlyMissionConfig) -> MonthlyMissionDefinition:
        definition = self.catalog.get(config.mission_id)
        if definition is None:
            raise MonthlyProgressError(
                MonthlyProgressErrorCode.UNKNOWN_MISSION,
                f"Configured monthly mission {config.mission_id!r} is not in the catalog",
            )
        return definition

    async def _load_or_create(
        self,
        user_id: str,
        config: MonthlyMissionConfig,
        *,
        session: AsyncClientSession | None,
    ) -> dict:
        document_id = _progress_id(user_id, config.month_key)
        existing = await self.collection.find_one({"_id": document_id}, session=session)
        if existing:
            return existing
        now = self.clock()
        document = {
            "_id": document_id,
            "user_id": user_id,
            "month_key": config.month_key,
            "mission_id": config.mission_id,
            "catalog_version": config.catalog_version,
            # The parameters are snapshotted so a later catalog edit cannot
            # retroactively change what this user was asked to do.
            "parameters": dict(config.parameters),
            "status": MonthlyProgressStatus.ACTIVE.value,
            "event_summaries": {},
            "xp_award_entry_id": None,
            "xp_award_cycle": 0,
            "revision": 1,
            "created_at": now,
            "updated_at": now,
        }
        await self.collection.insert_one(document, session=session)
        return document

    async def _resolve_and_persist(
        self,
        user_id: str,
        config: MonthlyMissionConfig,
        definition: MonthlyMissionDefinition,
        document: dict,
        summaries: dict[int, MonthlyEventSummary],
        *,
        session: AsyncClientSession | None,
        replayed: bool,
    ) -> MonthlyProgressResult:
        parameters = dict(document.get("parameters") or config.parameters)
        context = MonthlyEvaluationContext(
            month_key=config.month_key,
            user_id=user_id,
            parameters=parameters,
            events=tuple(summaries[key] for key in sorted(summaries)),
            month_closed=config.state == MonthlyConfigState.CLOSED,
        )
        observation = evaluate_monthly_metric(definition.evaluation.metric, context)
        resolution = resolve_monthly_observation(
            definition=definition,
            observation=observation,
            parameters=parameters,
        )

        previous_status = MonthlyProgressStatus(document["status"])
        target_status = self._target_status(resolution.status)
        if target_status != previous_status:
            ensure_monthly_progress_transition(
                previous_status,
                target_status,
                self._transition_reason(previous_status, target_status, context),
            )

        xp_delta = 0
        award_entry_id = document.get("xp_award_entry_id")
        award_cycle = int(document.get("xp_award_cycle", 0))

        if (
            target_status == MonthlyProgressStatus.COMPLETED
            and award_entry_id is None
        ):
            entry = await self.xp.award(
                user_id=user_id,
                command=AwardXpCommand(
                    idempotency_key=_award_key(user_id, config.month_key, award_cycle),
                    source_type=XpSourceType.MONTHLY_MISSION,
                    source_id=f"{config.month_key}:{config.mission_id}",
                    amount=MONTHLY_MISSION_XP,
                    reason=f"Monthly mission complete: {definition.ui.name}",
                    metadata={
                        "month_key": config.month_key,
                        "mission_id": config.mission_id,
                    },
                ),
                session=session,
            )
            award_entry_id = entry.id
            xp_delta = MONTHLY_MISSION_XP
            await self.celebrations.enqueue(
                user_id=user_id,
                command=EnqueueCelebrationCommand(
                    idempotency_key=(
                        f"monthly-celebration:{user_id}:{config.month_key}:{award_cycle}"
                    ),
                    xp_entry_id=entry.id,
                    kind=CelebrationKind.MISSION_COMPLETED,
                    presentation=CelebrationPresentation.TOAST,
                    heading="Monthly mission complete",
                    message=f"{definition.ui.name} · +{MONTHLY_MISSION_XP} XP",
                    metadata={
                        "month_key": config.month_key,
                        "name": definition.ui.name,
                        "xp": MONTHLY_MISSION_XP,
                    },
                ),
                session=session,
            )
        elif (
            previous_status == MonthlyProgressStatus.COMPLETED
            and target_status != MonthlyProgressStatus.COMPLETED
            and award_entry_id
        ):
            # A correction took the month back below its threshold. XP is
            # append-only, so reverse it with an exact linked compensation.
            await self.xp.compensate(
                user_id=user_id,
                command=CompensateXpCommand(
                    idempotency_key=(
                        f"monthly-compensate:{user_id}:{config.month_key}:{award_cycle}"
                    ),
                    original_entry_id=award_entry_id,
                    reason="Monthly mission reversed after a result correction",
                ),
                session=session,
            )
            await self.celebrations.cancel_for_xp_award(
                user_id=user_id,
                xp_entry_id=award_entry_id,
                session=session,
            )
            xp_delta = -MONTHLY_MISSION_XP
            award_entry_id = None
            # Only a real reversal advances the cycle, so the next completion is
            # a new award instead of colliding with the compensated one.
            award_cycle += 1

        now = self.clock()
        await self.collection.update_one(
            {"_id": document["_id"]},
            {
                "$set": {
                    "status": target_status.value,
                    "event_summaries": {
                        str(event_id): summary.model_dump(mode="python")
                        for event_id, summary in summaries.items()
                    },
                    "progress_text": resolution.progress.text,
                    "progress_percent": resolution.progress.percent,
                    "observed_value": resolution.observation.value,
                    "xp_award_entry_id": award_entry_id,
                    "xp_award_cycle": award_cycle,
                    "updated_at": now,
                },
                "$inc": {"revision": 1},
            },
            session=session,
        )
        return MonthlyProgressResult(
            user_id=user_id,
            month_key=config.month_key,
            mission_id=config.mission_id,
            status=target_status,
            resolution=resolution,
            xp_delta=xp_delta,
            replayed=replayed,
        )

    @staticmethod
    def _target_status(status: MetricResolutionStatus) -> MonthlyProgressStatus:
        if status == MetricResolutionStatus.PENDING:
            return MonthlyProgressStatus.ACTIVE
        return MonthlyProgressStatus(status.value)

    @staticmethod
    def _transition_reason(
        previous: MonthlyProgressStatus,
        target: MonthlyProgressStatus,
        context: MonthlyEvaluationContext,
    ) -> MissionTransitionReason:
        if previous != MonthlyProgressStatus.ACTIVE:
            return MissionTransitionReason.RESULT_CORRECTION
        if target == MonthlyProgressStatus.FAILED:
            return MissionTransitionReason.MONTH_CLOSE
        return MissionTransitionReason.EVALUATION


__all__ = [
    "MonthlyProgressError",
    "MonthlyProgressErrorCode",
    "MonthlyProgressResult",
    "MonthlyProgressService",
]
