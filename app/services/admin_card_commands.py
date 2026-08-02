"""The durable channel from an Admin decision to the CardData boundary.

`build_admin_card_observations` already turns an Admin decision into an
`admin_override` observation at the highest source rank — it is written and
tested, but nothing in production ever called it, because the boundary lives in
the scraper and the backend has no dependency on it.

This is the missing link, and it is a queue rather than a direct call on
purpose. Stamping evidence straight onto `bouts.card_data_v1` does not survive:
`rebuild_previous_snapshot` prefers the snapshot persisted on the *event*, so
the next ESPN pass rebuilds evidence without it and the decision quietly
reverts. A persisted command does survive, because every later reconciliation
replays it at rank 500 and it wins again.

The backend still writes the legacy field immediately, so Admin sees the change
at once. This is what makes it last.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pymongo.asynchronous.database import AsyncDatabase

COLLECTION = "admin_card_commands"

#: The kinds `build_admin_card_observations` understands. Anything else is a
#: programming error here, not a user error, so it raises.
COMMAND_KINDS = frozenset(
    {
        "event_timing",
        "event_lifecycle",
        "bout_timing",
        "bout_structure",
        "bout_lifecycle",
        "title",
        "result",
        "clear_result",
    }
)


class AdminCommandError(ValueError):
    pass


def command_id(kind: str, event_id: int, bout_id: int | None, actor_id: str) -> str:
    """Stable per (kind, target, actor).

    Re-deciding the same field replaces the previous command instead of piling
    up conflicting overrides — the latest Admin decision is the only one that
    should ever be replayed.
    """
    seed = f"{kind}\x1f{event_id}\x1f{bout_id or 0}\x1f{actor_id}"
    return f"admincmd_{hashlib.sha256(seed.encode()).hexdigest()[:24]}"


async def record_admin_command(
    db: AsyncDatabase,
    *,
    kind: str,
    event_id: int,
    reason: str,
    actor_id: str,
    bout_id: int | None = None,
    values: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict:
    """Persist one Admin decision for the boundary to replay, forever."""
    if kind not in COMMAND_KINDS:
        raise AdminCommandError(f"Unsupported Admin command kind {kind!r}")
    if not (reason or "").strip():
        raise AdminCommandError("Every Admin command requires a reason")
    if kind not in {"event_timing", "event_lifecycle"} and not bout_id:
        raise AdminCommandError(f"{kind} requires a bout_id")

    observed_at = (now or datetime.now(UTC)).isoformat().replace("+00:00", "Z")
    document = {
        "command_id": command_id(kind, event_id, bout_id, actor_id),
        "kind": kind,
        "event_id": int(event_id),
        "bout_id": int(bout_id) if bout_id else None,
        "observed_at": observed_at,
        "reason": reason,
        "actor_id": actor_id,
        "values": dict(values or {}),
        "updated_at": datetime.now(UTC),
    }
    await db[COLLECTION].update_one(
        {"command_id": document["command_id"]},
        {"$set": document, "$setOnInsert": {"created_at": document["updated_at"]}},
        upsert=True,
    )
    return document


async def commands_for_event(db: AsyncDatabase, event_id: int) -> list[dict]:
    """Every standing Admin decision on this card, oldest first.

    Ordered by `observed_at` so that when two commands touch the same field the
    later one is applied last and wins.
    """
    return (
        await db[COLLECTION]
        .find({"event_id": int(event_id)})
        .sort([("observed_at", 1)])
        .to_list(length=None)
    )


def title_values(
    *, is_title_fight: bool, is_bmf_title_fight: bool | None = None
) -> dict:
    """The `title` command payload the boundary expects.

    `false` is carried explicitly: a removed title is a decision, and it is the
    half of D-DATA-010 that used to revert on the next scrape.
    """
    if not is_title_fight:
        return {"is_title_fight": False}
    return {
        "is_title_fight": True,
        "is_bmf_title_fight": bool(is_bmf_title_fight),
    }


def structure_values(
    *,
    card_section: str | None = None,
    order_overall: int | None = None,
    is_current: bool | None = None,
) -> dict:
    """The `bout_structure` payload: slot section, order and currency.

    `event_card_slots` is reconciler-owned outright, so an Admin edit there is
    guaranteed to be recomputed away on the next pass unless it arrives as a
    command.
    """
    values: dict[str, Any] = {}
    if card_section in {"main", "prelim", "early_prelim"}:
        values["card_section"] = card_section
    if isinstance(order_overall, int) and order_overall > 0:
        values["order_overall"] = order_overall
    if isinstance(is_current, bool):
        values["is_current"] = is_current
    return values


def lifecycle_values(status: str) -> dict:
    """The `bout_lifecycle` / `event_lifecycle` payload."""
    return {"status": status}


def result_values(
    *,
    outcome: str,
    winner_fighter_id: str | None,
    method_family: str | None = None,
    method_detail: str | None = None,
    ending_round: int | None = None,
    ending_time_seconds: int | None = None,
) -> dict:
    """The `result` payload, in canonical vocabulary.

    A draw or no-contest carries no winner; the boundary rejects a decisive
    outcome without one, which is the check that keeps a malformed result from
    becoming a canonical fact.
    """
    values: dict[str, Any] = {
        "outcome": outcome,
        "winner_fighter_id": (
            None if outcome in {"draw", "no_contest"} else winner_fighter_id
        ),
    }
    if method_family:
        values["method_family"] = method_family
    if method_detail:
        values["method_detail"] = method_detail
    if ending_round:
        values["ending_round"] = ending_round
    if ending_time_seconds is not None:
        values["ending_time_seconds"] = ending_time_seconds
    return values


async def forget_admin_command(
    db: AsyncDatabase, *, kind: str, event_id: int, bout_id: int | None, actor_id: str
) -> bool:
    """Withdraw a standing decision so the boundary stops replaying it.

    Used when Admin undoes an action — clearing a result must not leave the old
    result command replaying forever.
    """
    outcome = await db[COLLECTION].delete_one(
        {"command_id": command_id(kind, event_id, bout_id, actor_id)}
    )
    return bool(outcome.deleted_count)


__all__ = [
    "COLLECTION",
    "COMMAND_KINDS",
    "AdminCommandError",
    "command_id",
    "commands_for_event",
    "forget_admin_command",
    "lifecycle_values",
    "record_admin_command",
    "result_values",
    "structure_values",
    "title_values",
]
