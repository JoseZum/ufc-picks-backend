"""Durable record of who decided a canonical field, and who may overwrite it.

D-DATA-002 fixes the precedence: ``Admin override > explicit ESPN metadata >
scraper inference > quarantined fallback``. D-DATA-010 makes Admin the sole
authority for the title fields, with **both** ``true`` and ``false`` durable —
removing a title designation is a decision, not an absence of one.

That only holds if the decision is written down. A bare ``is_title_fight: false``
on a bout is indistinguishable from a scraper default, so the next Tapology run
overwrites it and the removal silently reverts. This module writes the evidence
entry that makes the difference visible, in the shape the canonical card writer
already reads (``evidence.<field>.source_kind == "admin_override"``).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pymongo.asynchronous.database import AsyncDatabase

#: Bout fields Admin owns outright (D-DATA-010).
ADMIN_OWNED_BOUT_FIELDS = frozenset(
    {"is_title_fight", "is_bmf_title_fight", "title_type"}
)

ADMIN_SOURCE_KIND = "admin_override"

_SIDECAR = "card_data_v1"


def evidence_entry(value: Any, *, actor_id: str, now: datetime | None = None) -> dict:
    return {
        "source_kind": ADMIN_SOURCE_KIND,
        "value": value,
        "actor_id": actor_id,
        "recorded_at": (now or datetime.now(UTC)),
    }


async def record_admin_field_overrides(
    db: AsyncDatabase,
    *,
    bout_id: int,
    fields: Mapping[str, Any],
    actor_id: str,
) -> list[str]:
    """Stamp Admin authority beside the legacy field write.

    Returns the fields actually stamped. Fields outside Admin's ownership are
    ignored rather than rejected: this records authority, it does not police
    which columns an endpoint may edit.
    """
    owned = {
        key: value
        for key, value in fields.items()
        if key in ADMIN_OWNED_BOUT_FIELDS and value is not None
    }
    if not owned:
        return []

    now = datetime.now(UTC)
    await db["bouts"].update_one(
        {"id": bout_id},
        {
            "$set": {
                f"{_SIDECAR}.evidence.{field}": evidence_entry(
                    value, actor_id=actor_id, now=now
                )
                for field, value in owned.items()
            }
        },
    )
    return sorted(owned)


def admin_owned_fields(bout: Mapping[str, Any] | None) -> set[str]:
    """The canonical fields on this bout that Admin has explicitly decided.

    This is the read every non-Admin writer must perform before touching a
    canonical field. A bout that has never been through Admin returns an empty
    set, so legacy ingestion of a brand new card is unaffected.
    """
    if not bout:
        return set()
    evidence = (bout.get(_SIDECAR) or {}).get("evidence") or {}
    if not isinstance(evidence, Mapping):
        return set()
    return {
        str(field)
        for field, entry in evidence.items()
        if str(field) in ADMIN_OWNED_BOUT_FIELDS
        and isinstance(entry, Mapping)
        and entry.get("source_kind") == ADMIN_SOURCE_KIND
    }


def strip_admin_owned(
    update: Mapping[str, Any], bout: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Drop the fields Admin has decided from a lower-authority update.

    Advisory signals stay advisory: ESPN and Tapology keep writing everything
    else, and only the specific fields Admin claimed are held back.
    """
    protected = admin_owned_fields(bout)
    if not protected:
        return dict(update)
    return {key: value for key, value in update.items() if key not in protected}


__all__ = [
    "ADMIN_OWNED_BOUT_FIELDS",
    "ADMIN_SOURCE_KIND",
    "admin_owned_fields",
    "evidence_entry",
    "record_admin_field_overrides",
    "strip_admin_owned",
]
