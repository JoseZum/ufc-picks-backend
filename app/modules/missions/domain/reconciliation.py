"""Pure, content-addressed reconciliation previews for mission state.

This module deliberately has no persistence adapter. Evaluators provide desired
state, the planner compares only explicitly owned fields, and later Admin/API
tasks may decide whether and how a reviewed plan can be applied.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date
from enum import Enum
from typing import Any

from app.modules.missions.domain.enums import StringEnum

RECONCILIATION_PREVIEW_VERSION = "mission-reconciliation-preview/v1"
MISSING_VALUE = {"$mission_missing": True}


class ReconciliationInputError(ValueError):
    """Raised when a preview could silently own or remove the wrong state."""


class ReconciliationEntityType(StringEnum):
    ASSIGNMENT = "ASSIGNMENT"
    XP_LEDGER_ENTRY = "XP_LEDGER_ENTRY"
    USER_PROGRESSION = "USER_PROGRESSION"
    CELEBRATION = "CELEBRATION"
    MONTHLY_PROGRESS = "MONTHLY_PROGRESS"
    CARD_STREAK = "CARD_STREAK"


class ReconciliationAction(StringEnum):
    INSERT = "INSERT"
    UPDATE = "UPDATE"


class ReconciliationImpact(StringEnum):
    ASSIGNMENT_STATE = "ASSIGNMENT_STATE"
    XP_AWARD = "XP_AWARD"
    XP_COMPENSATION = "XP_COMPENSATION"
    PROGRESSION_REBUILD = "PROGRESSION_REBUILD"
    CELEBRATION_QUEUE = "CELEBRATION_QUEUE"
    MONTHLY_PROGRESS = "MONTHLY_PROGRESS"
    STREAK_STATE = "STREAK_STATE"


@dataclass(frozen=True)
class ReconciliationScope:
    event_id: int | None = None
    user_id: str | None = None
    assignment_id: str | None = None

    def __post_init__(self) -> None:
        if self.event_id is None and self.user_id is None and self.assignment_id is None:
            raise ReconciliationInputError(
                "reconciliation scope requires event_id, user_id or assignment_id"
            )
        if self.event_id is not None and self.event_id < 1:
            raise ReconciliationInputError("event_id must be positive")
        if self.user_id is not None and not self.user_id.strip():
            raise ReconciliationInputError("user_id cannot be blank")
        if self.assignment_id is not None and not self.assignment_id.strip():
            raise ReconciliationInputError("assignment_id cannot be blank")


@dataclass(frozen=True)
class ReconciliationCandidate:
    entity_type: ReconciliationEntityType
    entity_id: str
    impact: ReconciliationImpact
    owned_fields: tuple[str, ...]
    desired: Mapping[str, Any]
    current: Mapping[str, Any] | None = None
    expected_revision: int | None = None
    current_revision: int | None = None

    def __post_init__(self) -> None:
        if not self.entity_id.strip():
            raise ReconciliationInputError("entity_id cannot be blank")
        if not self.owned_fields or len(set(self.owned_fields)) != len(self.owned_fields):
            raise ReconciliationInputError("owned_fields must be non-empty and unique")
        if any(not field.strip() for field in self.owned_fields):
            raise ReconciliationInputError("owned field names cannot be blank")
        missing = [field for field in self.owned_fields if field not in self.desired]
        if missing:
            raise ReconciliationInputError(
                "desired state must explicitly contain every owned field: "
                + ", ".join(sorted(missing))
            )
        for revision in (self.expected_revision, self.current_revision):
            if revision is not None and revision < 0:
                raise ReconciliationInputError("revisions cannot be negative")


@dataclass(frozen=True)
class ReconciliationBlocker:
    code: str
    message: str
    entity_type: ReconciliationEntityType | None = None
    entity_id: str | None = None
    blocking: bool = True

    def __post_init__(self) -> None:
        if not self.code.strip() or not self.message.strip():
            raise ReconciliationInputError("blockers require a code and message")


@dataclass(frozen=True)
class ReconciliationOperation:
    operation_id: str
    action: ReconciliationAction
    entity_type: ReconciliationEntityType
    entity_id: str
    impact: ReconciliationImpact
    expected_revision: int | None
    changed_fields: tuple[str, ...]
    before: Mapping[str, Any] | None
    after: Mapping[str, Any]


@dataclass(frozen=True)
class MissionReconciliationPreview:
    preview_version: str
    plan_id: str
    scope: ReconciliationScope
    current_digest: str
    desired_digest: str
    operations: tuple[ReconciliationOperation, ...]
    unchanged_entities: tuple[str, ...]
    blockers: tuple[ReconciliationBlocker, ...]

    @property
    def safe_to_apply(self) -> bool:
        return not any(blocker.blocking for blocker in self.blockers)

    @property
    def converged(self) -> bool:
        return self.safe_to_apply and not self.operations

    def summary(self) -> dict[str, int | bool]:
        return {
            "insert_count": sum(
                operation.action == ReconciliationAction.INSERT
                for operation in self.operations
            ),
            "update_count": sum(
                operation.action == ReconciliationAction.UPDATE
                for operation in self.operations
            ),
            "unchanged_count": len(self.unchanged_entities),
            "blocker_count": len(self.blockers),
            "safe_to_apply": self.safe_to_apply,
            "converged": self.converged,
        }


def _json_default(value: Any) -> str:
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def _hash(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(_canonical(value).encode()).hexdigest()
    return f"{prefix}_{digest[:24]}"


def _entity_key(entity_type: ReconciliationEntityType, entity_id: str) -> str:
    return f"{entity_type.value}:{entity_id}"


def _projection(
    document: Mapping[str, Any] | None,
    owned_fields: Sequence[str],
) -> dict[str, Any] | None:
    if document is None:
        return None
    return {
        field: copy.deepcopy(document.get(field, MISSING_VALUE))
        for field in sorted(owned_fields)
    }


def build_reconciliation_preview(
    *,
    scope: ReconciliationScope,
    candidates: Sequence[ReconciliationCandidate],
    blockers: Sequence[ReconciliationBlocker] = (),
) -> MissionReconciliationPreview:
    """Build a deterministic no-write plan over explicitly owned fields."""

    keys = [
        _entity_key(candidate.entity_type, candidate.entity_id)
        for candidate in candidates
    ]
    if len(keys) != len(set(keys)):
        raise ReconciliationInputError("reconciliation candidate identities must be unique")

    ordered = sorted(
        candidates,
        key=lambda candidate: (candidate.entity_type.value, candidate.entity_id),
    )
    resolved_blockers = list(blockers)
    operations: list[ReconciliationOperation] = []
    unchanged: list[str] = []
    current_values: list[dict[str, Any]] = []
    desired_values: list[dict[str, Any]] = []

    for candidate in ordered:
        key = _entity_key(candidate.entity_type, candidate.entity_id)
        before = _projection(candidate.current, candidate.owned_fields)
        after = _projection(candidate.desired, candidate.owned_fields)
        assert after is not None
        current_values.append(
            {"entity": key, "revision": candidate.current_revision, "value": before}
        )
        desired_values.append({"entity": key, "value": after})

        if (
            candidate.current is not None
            and candidate.expected_revision is not None
            and candidate.current_revision != candidate.expected_revision
        ):
            resolved_blockers.append(
                ReconciliationBlocker(
                    code="REVISION_MISMATCH",
                    message="Current revision differs from the evaluator snapshot",
                    entity_type=candidate.entity_type,
                    entity_id=candidate.entity_id,
                )
            )

        changed_fields = tuple(
            field
            for field in sorted(candidate.owned_fields)
            if before is None or before[field] != after[field]
        )
        if not changed_fields:
            unchanged.append(key)
            continue
        action = (
            ReconciliationAction.INSERT
            if candidate.current is None
            else ReconciliationAction.UPDATE
        )
        operation_payload = {
            "action": action.value,
            "entity": key,
            "impact": candidate.impact.value,
            "expected_revision": candidate.expected_revision,
            "before": before,
            "after": after,
        }
        operations.append(
            ReconciliationOperation(
                operation_id=_hash("reconcile_op", operation_payload),
                action=action,
                entity_type=candidate.entity_type,
                entity_id=candidate.entity_id,
                impact=candidate.impact,
                expected_revision=candidate.expected_revision,
                changed_fields=changed_fields,
                before=before,
                after=after,
            )
        )

    resolved_blockers.sort(
        key=lambda blocker: (
            blocker.code,
            blocker.entity_type.value if blocker.entity_type else "",
            blocker.entity_id or "",
            blocker.message,
        )
    )
    current_digest = _hash("reconcile_current", current_values)
    desired_digest = _hash("reconcile_desired", desired_values)
    plan_payload = {
        "preview_version": RECONCILIATION_PREVIEW_VERSION,
        "scope": asdict(scope),
        "current_digest": current_digest,
        "desired_digest": desired_digest,
        "operation_ids": [operation.operation_id for operation in operations],
        "unchanged": unchanged,
        "blockers": [asdict(blocker) for blocker in resolved_blockers],
    }
    return MissionReconciliationPreview(
        preview_version=RECONCILIATION_PREVIEW_VERSION,
        plan_id=_hash("mission_reconcile", plan_payload),
        scope=scope,
        current_digest=current_digest,
        desired_digest=desired_digest,
        operations=tuple(operations),
        unchanged_entities=tuple(unchanged),
        blockers=tuple(resolved_blockers),
    )
