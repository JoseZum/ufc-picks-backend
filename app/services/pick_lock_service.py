"""Authoritative event/bout pick-lock evaluation.

Locks are evaluated at request time, so closing a section does not depend on a
background scheduler being awake. Manual admin overrides take precedence over
automatic section times, while completed/cancelled/resulted fights are always
immutable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Optional


@dataclass(frozen=True)
class PickLockState:
    locked: bool
    reason: Optional[str] = None
    automatic_lock_time_utc: Optional[datetime] = None


def _value(document: Any, field: str, default=None):
    if isinstance(document, dict):
        return document.get(field, default)
    return getattr(document, field, default)


def as_utc_datetime(value: Any) -> Optional[datetime]:
    """Normalize Mongo datetimes and ISO strings to aware UTC datetimes."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def get_bout_automatic_lock_time(event: Any, bout: Any) -> Optional[datetime]:
    """Resolve a bout's section lock with compatibility fallbacks."""
    direct = as_utc_datetime(_value(bout, "automatic_lock_time_utc"))
    if direct:
        return direct

    section = _value(bout, "card_section")
    section_locks = _value(event, "section_lock_times_utc", {}) or {}
    if section and section_locks.get(section):
        return as_utc_datetime(section_locks[section])

    section_starts = _value(event, "section_start_times_utc", {}) or {}
    if section and section_starts.get(section):
        return as_utc_datetime(section_starts[section])

    return as_utc_datetime(
        _value(event, "picks_lock_time_utc")
        or _value(event, "card_start_time_utc")
        or _value(event, "picks_lock_date")
    )


def evaluate_bout_pick_lock(
    event: Any,
    bout: Any,
    now: Optional[datetime] = None,
) -> PickLockState:
    """Return the effective lock and the exact reason shown by the UI."""
    automatic_lock_time = get_bout_automatic_lock_time(event, bout)
    current_time = as_utc_datetime(now or datetime.now(UTC))

    if (
        _value(bout, "status") in {"completed", "cancelled"}
        or bool(_value(bout, "result"))
        or _value(event, "status") in {"completed", "cancelled"}
    ):
        return PickLockState(True, "result", automatic_lock_time)

    event_override = _value(event, "picks_lock_override")
    bout_override = _value(bout, "picks_lock_override")

    if event_override == "locked" or (
        event_override is None and bool(_value(event, "picks_locked", False))
    ):
        return PickLockState(True, "admin_event", automatic_lock_time)
    if bout_override == "locked" or (
        bout_override is None and bool(_value(bout, "picks_locked", False))
    ):
        return PickLockState(True, "admin_bout", automatic_lock_time)

    # Full-event unlock opens every non-individually-locked bout. This is an
    # intentional admin override of already-passed section times.
    if event_override == "unlocked":
        return PickLockState(False, None, automatic_lock_time)
    if bout_override == "unlocked":
        return PickLockState(False, None, automatic_lock_time)

    if (
        automatic_lock_time is not None
        and current_time is not None
        and current_time >= automatic_lock_time
    ):
        return PickLockState(True, "section_time", automatic_lock_time)
    return PickLockState(False, None, automatic_lock_time)
