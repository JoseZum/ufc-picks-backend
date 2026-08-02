"""The launch switch for the mission system (CAL-004).

Two independent controls, deliberately:

``MISSIONS_ENABLED``
    The off switch. When false nobody sees missions, regardless of allowlist.
    This is what gets flipped if something goes wrong at 2am — one variable, no
    deploy, no code change.

``MISSIONS_ALLOWLIST``
    The canary. When it is non-empty, only those users get missions even though
    the feature is "on". Emptying it opens the feature to everyone, which makes
    "go to general availability" a config change rather than a release.

Failing closed matters more than convenience here: an unset allowlist with the
feature enabled means everyone, so the canary phase must set both.
"""

from __future__ import annotations

from functools import lru_cache

from app.core.config import get_settings


def _split(raw: str | None) -> frozenset[str]:
    if not raw:
        return frozenset()
    return frozenset(
        part.strip().lower() for part in str(raw).replace(";", ",").split(",")
        if part.strip()
    )


@lru_cache(maxsize=1)
def _allowlist() -> frozenset[str]:
    return _split(getattr(get_settings(), "missions_allowlist", ""))


def missions_enabled() -> bool:
    return bool(getattr(get_settings(), "missions_enabled", False))


def canary_only() -> bool:
    """True while an allowlist is set, i.e. the feature is still a canary."""
    return bool(_allowlist())


def user_can_see_missions(user_id: str | None, email: str | None = None) -> bool:
    """Whether this user is inside the launch.

    Matching accepts either the account id or the email, because an allowlist
    written by a human is going to contain emails.
    """
    if not missions_enabled():
        return False
    allowed = _allowlist()
    if not allowed:
        return True
    candidates = {
        value.strip().lower()
        for value in (user_id, email)
        if value and value.strip()
    }
    return bool(candidates & allowed)


def reset_cache() -> None:
    """Tests and config reloads only."""
    _allowlist.cache_clear()


__all__ = [
    "canary_only",
    "missions_enabled",
    "reset_cache",
    "user_can_see_missions",
]
