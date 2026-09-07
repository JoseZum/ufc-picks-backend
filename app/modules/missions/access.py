"""Interruptor de lanzamiento del sistema de misiones.

Dos controles independientes: ``MISSIONS_ENABLED`` apaga todo sin importar
el allowlist, y ``MISSIONS_ALLOWLIST`` es el canary (si tiene usuarios, solo
ellos ven misiones). Falla cerrado: un allowlist vacío con la feature activa
significa "todos", así que el canary debe fijar ambas variables.
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
    """True mientras haya un allowlist activo, o sea la feature sigue en canary."""
    return bool(_allowlist())


def user_can_see_missions(user_id: str | None, email: str | None = None) -> bool:
    """Si este usuario está dentro del lanzamiento.

    Acepta id de cuenta o email porque un allowlist escrito a mano suele
    tener emails.
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
    """Solo para tests y recargas de config."""
    _allowlist.cache_clear()


__all__ = [
    "canary_only",
    "missions_enabled",
    "reset_cache",
    "user_can_see_missions",
]
