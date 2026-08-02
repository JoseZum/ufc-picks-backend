"""Versioned MongoDB index manifest for the mission system.

The manifest is application-owned infrastructure. Inspection is read-only,
application is explicit, and rollback removes only named mission indexes while
leaving documents and collections intact.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Any

from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.asynchronous.database import AsyncDatabase


@dataclass(frozen=True)
class MissionIndexSpec:
    collection: str
    name: str
    keys: tuple[tuple[str, int], ...]
    unique: bool = False

    def to_index_model(self) -> IndexModel:
        return IndexModel(list(self.keys), name=self.name, unique=self.unique)


@dataclass(frozen=True)
class MissionIndexConflict:
    collection: str
    name: str
    expected_keys: tuple[tuple[str, int], ...]
    expected_unique: bool
    actual_keys: tuple[tuple[str, int], ...]
    actual_unique: bool


@dataclass(frozen=True)
class MissionDuplicateBlocker:
    collection: str
    index_name: str
    duplicate_groups: int


@dataclass(frozen=True)
class MissionIndexReport:
    plan_id: str
    missing: tuple[str, ...]
    matching: tuple[str, ...]
    conflicts: tuple[MissionIndexConflict, ...]
    duplicate_blockers: tuple[MissionDuplicateBlocker, ...]

    @property
    def can_apply(self) -> bool:
        return not self.conflicts and not self.duplicate_blockers

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["can_apply"] = self.can_apply
        return value


class MissionIndexMigrationError(RuntimeError):
    """Raised when a manifest cannot be applied without manual remediation."""


MISSION_INDEXES: tuple[MissionIndexSpec, ...] = (
    MissionIndexSpec(
        "mission_offer_sets",
        "mission_v1_offer_user_event_catalog_uq",
        (("user_id", ASCENDING), ("event_id", ASCENDING), ("catalog_version", ASCENDING)),
        unique=True,
    ),
    MissionIndexSpec(
        "mission_offer_sets",
        "mission_v1_offer_event_user",
        (("event_id", ASCENDING), ("user_id", ASCENDING)),
    ),
    MissionIndexSpec(
        "mission_assignments",
        "mission_v1_assignment_user_event_slot_uq",
        (("user_id", ASCENDING), ("event_id", ASCENDING), ("slot", ASCENDING)),
        unique=True,
    ),
    MissionIndexSpec(
        "mission_assignments",
        "mission_v1_assignment_user_status_updated",
        (("user_id", ASCENDING), ("status", ASCENDING), ("updated_at", DESCENDING)),
    ),
    MissionIndexSpec(
        "mission_assignments",
        "mission_v1_assignment_event_status",
        (("event_id", ASCENDING), ("status", ASCENDING)),
    ),
    MissionIndexSpec(
        "mission_assignments",
        "mission_v1_assignment_bound_bout",
        (("selection.bout_ids", ASCENDING), ("status", ASCENDING)),
    ),
    MissionIndexSpec(
        "mission_card_controls",
        "mission_v1_card_control_event_uq",
        (("event_id", ASCENDING),),
        unique=True,
    ),
    MissionIndexSpec(
        "mission_card_controls",
        "mission_v1_card_control_state_lock",
        (("state", ASCENDING), ("lock_at", ASCENDING)),
    ),
    MissionIndexSpec(
        "mission_monthly_configs",
        "mission_v1_monthly_config_month_uq",
        (("month_key", ASCENDING),),
        unique=True,
    ),
    MissionIndexSpec(
        "mission_monthly_configs",
        "mission_v1_monthly_config_state_start",
        (("state", ASCENDING), ("starts_at", ASCENDING)),
    ),
    MissionIndexSpec(
        "mission_monthly_progress",
        "mission_v1_monthly_progress_user_month_uq",
        (("user_id", ASCENDING), ("month_key", ASCENDING)),
        unique=True,
    ),
    MissionIndexSpec(
        "mission_monthly_progress",
        "mission_v1_monthly_progress_month_status",
        (("month_key", ASCENDING), ("status", ASCENDING)),
    ),
    MissionIndexSpec(
        "mission_xp_ledger",
        "mission_v1_xp_idempotency_uq",
        (("idempotency_key", ASCENDING),),
        unique=True,
    ),
    MissionIndexSpec(
        "mission_xp_ledger",
        "mission_v1_xp_user_created",
        (("user_id", ASCENDING), ("created_at", DESCENDING)),
    ),
    MissionIndexSpec(
        "mission_xp_ledger",
        "mission_v1_xp_source",
        (("source_type", ASCENDING), ("source_id", ASCENDING)),
    ),
    MissionIndexSpec(
        "mission_xp_ledger",
        "mission_v1_xp_compensates",
        (("compensates_entry_id", ASCENDING),),
    ),
    MissionIndexSpec(
        "mission_user_progression",
        "mission_v1_progression_user_uq",
        (("user_id", ASCENDING),),
        unique=True,
    ),
    MissionIndexSpec(
        "mission_celebrations",
        "mission_v1_celebration_idempotency_uq",
        (("idempotency_key", ASCENDING),),
        unique=True,
    ),
    MissionIndexSpec(
        "mission_celebrations",
        "mission_v1_celebration_user_ack_created",
        (("user_id", ASCENDING), ("acknowledged_at", ASCENDING), ("created_at", ASCENDING)),
    ),
    MissionIndexSpec(
        "mission_celebrations",
        "mission_v1_celebration_xp_status",
        (("xp_entry_id", ASCENDING), ("status", ASCENDING)),
    ),
    MissionIndexSpec(
        "mission_card_streaks",
        "mission_v1_streak_user_uq",
        (("user_id", ASCENDING),),
        unique=True,
    ),
    MissionIndexSpec(
        "mission_card_streak_cards",
        "mission_v1_streak_card_uq",
        (("user_id", ASCENDING), ("event_id", ASCENDING)),
        unique=True,
    ),
    MissionIndexSpec(
        "mission_card_streak_cards",
        "mission_v1_streak_card_user_settled",
        (("user_id", ASCENDING), ("settled_at", DESCENDING)),
    ),
    MissionIndexSpec(
        "admin_card_commands",
        "admin_v1_card_command_uq",
        (("command_id", ASCENDING),),
        unique=True,
    ),
    MissionIndexSpec(
        "admin_card_commands",
        "admin_v1_card_command_event_observed",
        (("event_id", ASCENDING), ("observed_at", ASCENDING)),
    ),
    MissionIndexSpec(
        "mission_command_receipts",
        "mission_v1_command_actor_key_uq",
        (("actor_id", ASCENDING), ("idempotency_key", ASCENDING)),
        unique=True,
    ),
    MissionIndexSpec(
        "mission_evaluation_runs",
        "mission_v1_evaluation_assignment_trigger_uq",
        (
            ("assignment_id", ASCENDING),
            ("trigger_type", ASCENDING),
            ("trigger_id", ASCENDING),
            ("trigger_revision", ASCENDING),
        ),
        unique=True,
    ),
    MissionIndexSpec(
        "mission_evaluation_runs",
        "mission_v1_evaluation_event_created",
        (("event_id", ASCENDING), ("created_at", DESCENDING)),
    ),
    MissionIndexSpec(
        "mission_admin_audit",
        "mission_v1_audit_target_created",
        (("target_type", ASCENDING), ("target_id", ASCENDING), ("created_at", DESCENDING)),
    ),
    MissionIndexSpec(
        "mission_admin_audit",
        "mission_v1_audit_actor_created",
        (("actor_id", ASCENDING), ("created_at", DESCENDING)),
    ),
)


def mission_index_plan_id() -> str:
    serialized = json.dumps(
        [asdict(spec) for spec in MISSION_INDEXES],
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"mission_indexes_{sha256(serialized.encode()).hexdigest()[:16]}"


async def _existing_indexes(collection) -> dict[str, dict[str, Any]]:
    if collection.name not in await collection.database.list_collection_names():
        return {}
    cursor = await collection.list_indexes()
    return {item["name"]: item for item in await cursor.to_list(length=None)}


async def _duplicate_group_count(
    db: AsyncDatabase,
    spec: MissionIndexSpec,
) -> int:
    group_id = {
        f"field_{position}": f"${field}"
        for position, (field, _direction) in enumerate(spec.keys)
    }
    cursor = await db[spec.collection].aggregate(
        [
            {"$group": {"_id": group_id, "count": {"$sum": 1}}},
            {"$match": {"count": {"$gt": 1}}},
            {"$count": "duplicate_groups"},
        ]
    )
    rows = await cursor.to_list(length=1)
    return int(rows[0]["duplicate_groups"]) if rows else 0


async def inspect_mission_indexes(db: AsyncDatabase) -> MissionIndexReport:
    missing: list[str] = []
    matching: list[str] = []
    conflicts: list[MissionIndexConflict] = []
    duplicate_blockers: list[MissionDuplicateBlocker] = []
    existing_by_collection: dict[str, dict[str, dict[str, Any]]] = {}

    for spec in MISSION_INDEXES:
        existing = existing_by_collection.get(spec.collection)
        if existing is None:
            existing = await _existing_indexes(db[spec.collection])
            existing_by_collection[spec.collection] = existing
        actual = existing.get(spec.name)
        qualified_name = f"{spec.collection}.{spec.name}"
        if actual is None:
            missing.append(qualified_name)
            if spec.unique:
                duplicate_groups = await _duplicate_group_count(db, spec)
                if duplicate_groups:
                    duplicate_blockers.append(
                        MissionDuplicateBlocker(
                            collection=spec.collection,
                            index_name=spec.name,
                            duplicate_groups=duplicate_groups,
                        )
                    )
            continue

        actual_keys = tuple(actual["key"].items())
        actual_unique = bool(actual.get("unique", False))
        if actual_keys == spec.keys and actual_unique == spec.unique:
            matching.append(qualified_name)
            continue
        conflicts.append(
            MissionIndexConflict(
                collection=spec.collection,
                name=spec.name,
                expected_keys=spec.keys,
                expected_unique=spec.unique,
                actual_keys=actual_keys,
                actual_unique=actual_unique,
            )
        )

    return MissionIndexReport(
        plan_id=mission_index_plan_id(),
        missing=tuple(missing),
        matching=tuple(matching),
        conflicts=tuple(conflicts),
        duplicate_blockers=tuple(duplicate_blockers),
    )


async def apply_mission_indexes(db: AsyncDatabase) -> MissionIndexReport:
    report = await inspect_mission_indexes(db)
    if not report.can_apply:
        raise MissionIndexMigrationError(
            "Mission indexes cannot be applied while conflicts or duplicate "
            "keys exist. Inspect the dry-run report first."
        )

    missing = set(report.missing)
    for spec in MISSION_INDEXES:
        if f"{spec.collection}.{spec.name}" not in missing:
            continue
        await db[spec.collection].create_indexes([spec.to_index_model()])
    return await inspect_mission_indexes(db)


async def rollback_mission_indexes(db: AsyncDatabase) -> MissionIndexReport:
    existing_by_collection: dict[str, dict[str, dict[str, Any]]] = {}
    for spec in reversed(MISSION_INDEXES):
        existing = existing_by_collection.get(spec.collection)
        if existing is None:
            existing = await _existing_indexes(db[spec.collection])
            existing_by_collection[spec.collection] = existing
        if spec.name in existing:
            await db[spec.collection].drop_index(spec.name)
            existing.pop(spec.name, None)
    return await inspect_mission_indexes(db)
