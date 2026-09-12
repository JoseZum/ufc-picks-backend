"""Cuerpos de request del panel de admin."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

# Request schemas

class UpdateEventTimingRequest(BaseModel):
    """Datos para actualizar fecha/hora de un evento."""
    card_start_time_utc: datetime | None = None
    picks_lock_time_utc: datetime | None = None
    # Legacy aliases kept while deployed frontends roll over.
    event_date: datetime | None = None
    picks_lock_date: datetime | None = None


class UpdateBoutTimingRequest(BaseModel):
    """Datos para actualizar timing de una pelea individual."""
    bout_start_time: datetime | None = None
    picks_lock_time: datetime | None = None


class UpdateBoutResultRequest(BaseModel):
    """Datos para registrar el resultado de una pelea."""
    winner: str  # "red" | "blue" | "draw" | "nc"
    method: str  # "KO/TKO" | "SUB" | "DEC" | "DQ" | "OTHER"
    round: int | None = None
    time: str | None = None


class UpdateBoutDetailsRequest(BaseModel):
    """Datos editables de una pelea y su posición en la cartelera."""
    # Campos del bout
    rounds_scheduled: Literal[3, 5] | None = None
    weight_class: str | None = None
    is_title_fight: bool | None = None
    is_bmf_title_fight: bool | None = None
    # Campos del event_card_slot
    card_section: Literal["main", "prelim", "early_prelim"] | None = None
    order_overall: int | None = None
    order_section: int | None = None
    is_main_event: bool | None = None
    is_co_main: bool | None = None


# Event art endpoints
