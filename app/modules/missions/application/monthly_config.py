"""Admin-owned monthly mission configuration lifecycle (DRAFT/ACTIVE/CLOSED).

One month has exactly one configuration. It stays editable while it is a DRAFT
that nobody is playing yet; activation publishes it and freezes the parameters so
a running month can never move the goalposts underneath a user's progress.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from app.modules.missions.domain.enums import (
    MissionTransitionReason,
    MonthlyConfigState,
)
from app.modules.missions.domain.monthly import (
    MonthlyConfigError,
    MonthlyConfigErrorCode,
    MonthlyMissionConfig,
    MonthlyMissionDefinition,
    month_bounds,
    month_key_for,
)
from app.modules.missions.domain.state_machines import ensure_monthly_config_transition

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _config_id(month_key: str) -> str:
    digest = hashlib.sha256(month_key.encode()).hexdigest()[:16]
    return f"monthly_{month_key}_{digest}"


class MonthlyConfigService:
    """Reads and mutates ``mission_monthly_configs`` under the approved rules."""

    def __init__(
        self,
        db: AsyncDatabase,
        *,
        catalog: Mapping[str, MonthlyMissionDefinition],
        clock: Clock = _utc_now,
    ) -> None:
        self.db = db
        self.collection = db["mission_monthly_configs"]
        self.catalog = catalog
        self.clock = clock

    # ------------------------------------------------------------------ reads

    async def get(self, month_key: str) -> MonthlyMissionConfig | None:
        document = await self.collection.find_one({"month_key": month_key})
        return MonthlyMissionConfig.model_validate(document) if document else None

    async def require(self, month_key: str) -> MonthlyMissionConfig:
        config = await self.get(month_key)
        if config is None:
            raise MonthlyConfigError(
                MonthlyConfigErrorCode.CONFIG_NOT_FOUND,
                f"No monthly mission is configured for {month_key}",
            )
        return config

    async def active_for(self, moment: datetime) -> MonthlyMissionConfig | None:
        """The ACTIVE configuration governing the month a moment falls in."""
        config = await self.get(month_key_for(moment))
        if config is None or config.state != MonthlyConfigState.ACTIVE:
            return None
        return config

    # ----------------------------------------------------------------- writes

    async def create_draft(
        self,
        *,
        month_key: str,
        mission_id: str,
        parameters: Mapping[str, int] | None = None,
        session: AsyncClientSession | None = None,
    ) -> MonthlyMissionConfig:
        definition = self._definition(mission_id)
        starts_at, ends_at = month_bounds(month_key)
        now = self.clock()
        if now > ends_at:
            raise MonthlyConfigError(
                MonthlyConfigErrorCode.MONTH_ALREADY_STARTED,
                f"{month_key} is already over and can no longer be drafted",
            )
        resolved = definition.validate_admin_parameters(
            dict(parameters) if parameters is not None else definition.default_parameters()
        )
        config = MonthlyMissionConfig(
            _id=_config_id(month_key),
            month_key=month_key,
            mission_id=definition.mission_id,
            catalog_version=definition.catalog_version,
            parameters=resolved,
            state=MonthlyConfigState.DRAFT,
            starts_at=starts_at,
            ends_at=ends_at,
            created_at=now,
            updated_at=now,
        )
        try:
            await self.collection.insert_one(
                config.model_dump(by_alias=True, mode="python"),
                session=session,
            )
        except DuplicateKeyError as exc:
            raise MonthlyConfigError(
                MonthlyConfigErrorCode.CONFIG_ALREADY_EXISTS,
                f"{month_key} already has a monthly mission configured",
            ) from exc
        return config

    async def update_draft(
        self,
        *,
        month_key: str,
        mission_id: str | None = None,
        parameters: Mapping[str, int] | None = None,
        session: AsyncClientSession | None = None,
    ) -> MonthlyMissionConfig:
        config = await self.require(month_key)
        await self._require_editable(config)
        definition = self._definition(mission_id or config.mission_id)
        if mission_id is not None and mission_id != config.mission_id and parameters is None:
            # A different mission has a different parameter contract, so keeping
            # the previous values would silently mean something else.
            requested = definition.default_parameters()
        else:
            requested = dict(parameters) if parameters is not None else config.parameters
        resolved = definition.validate_admin_parameters(requested)

        now = self.clock()
        updated = config.model_copy(
            update={
                "mission_id": definition.mission_id,
                "catalog_version": definition.catalog_version,
                "parameters": resolved,
                "updated_at": now,
            }
        )
        await self.collection.update_one(
            {"_id": config.id, "state": MonthlyConfigState.DRAFT.value},
            {
                "$set": {
                    "mission_id": updated.mission_id,
                    "catalog_version": updated.catalog_version,
                    "parameters": updated.parameters,
                    "updated_at": now,
                }
            },
            session=session,
        )
        return updated

    async def activate(
        self,
        *,
        month_key: str,
        session: AsyncClientSession | None = None,
    ) -> MonthlyMissionConfig:
        config = await self.require(month_key)
        if config.state == MonthlyConfigState.ACTIVE:
            return config
        ensure_monthly_config_transition(
            config.state,
            MonthlyConfigState.ACTIVE,
            MissionTransitionReason.MONTH_START,
        )
        now = self.clock()
        if now > config.ends_at:
            raise MonthlyConfigError(
                MonthlyConfigErrorCode.MONTH_NOT_FINISHED,
                f"{month_key} is already over and cannot be activated",
            )
        await self.collection.update_one(
            {"_id": config.id, "state": MonthlyConfigState.DRAFT.value},
            {
                "$set": {
                    "state": MonthlyConfigState.ACTIVE.value,
                    "activated_at": now,
                    "updated_at": now,
                }
            },
            session=session,
        )
        # Always answer from storage: Mongo truncates to milliseconds, so
        # returning the in-memory copy made a retry report a different instant.
        return await self.require(month_key)

    async def close(
        self,
        *,
        month_key: str,
        reason: MissionTransitionReason = MissionTransitionReason.MONTH_CLOSE,
        session: AsyncClientSession | None = None,
    ) -> MonthlyMissionConfig:
        config = await self.require(month_key)
        if config.state == MonthlyConfigState.CLOSED:
            return config
        ensure_monthly_config_transition(config.state, MonthlyConfigState.CLOSED, reason)
        now = self.clock()
        if reason == MissionTransitionReason.MONTH_CLOSE and now <= config.ends_at:
            raise MonthlyConfigError(
                MonthlyConfigErrorCode.MONTH_NOT_FINISHED,
                f"{month_key} is still running; use an explicit Admin close instead",
            )
        result = await self.collection.update_one(
            {"_id": config.id, "state": config.state.value},
            {
                "$set": {
                    "state": MonthlyConfigState.CLOSED.value,
                    "closed_at": now,
                    "updated_at": now,
                }
            },
            session=session,
        )
        if result.modified_count == 0:
            return await self.require(month_key)
        return config.model_copy(
            update={
                "state": MonthlyConfigState.CLOSED,
                "closed_at": now,
                "updated_at": now,
            }
        )

    # ---------------------------------------------------------------- helpers

    def _definition(self, mission_id: str) -> MonthlyMissionDefinition:
        definition = self.catalog.get(mission_id)
        if definition is None:
            raise MonthlyConfigError(
                MonthlyConfigErrorCode.UNKNOWN_MISSION,
                f"Unknown monthly mission {mission_id!r}",
            )
        return definition

    async def _require_editable(self, config: MonthlyMissionConfig) -> None:
        """A month is editable while it is a DRAFT nobody is playing yet.

        The invariant that matters is that a user with progress never sees the
        goalposts move. Activation is what publishes the month, so DRAFT stays
        editable even after the month has technically begun — otherwise the
        August 2026 launch month could never be configured at all. Any recorded
        progress freezes it immediately, whatever the state says.
        """
        if config.state != MonthlyConfigState.DRAFT:
            raise MonthlyConfigError(
                MonthlyConfigErrorCode.CONFIG_FROZEN,
                f"{config.month_key} is {config.state.value} and can no longer be edited",
            )
        if self.clock() > config.ends_at:
            raise MonthlyConfigError(
                MonthlyConfigErrorCode.MONTH_ALREADY_STARTED,
                f"{config.month_key} is over and is frozen",
            )
        if await self.db["mission_monthly_progress"].find_one(
            {"month_key": config.month_key}
        ):
            raise MonthlyConfigError(
                MonthlyConfigErrorCode.CONFIG_FROZEN,
                f"{config.month_key} already has recorded progress and is frozen",
            )


__all__ = ["MonthlyConfigService"]
