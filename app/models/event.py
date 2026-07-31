from datetime import datetime, date
from typing import Optional
from pydantic import BaseModel, Field


class Event(BaseModel):
    """Un evento UFC (UFC 300, UFC Fight Night, etc)"""
    
    id: int  # ID único del evento
    source: str  # De dónde sacamos los datos (ej: "tapology")
    promotion: str  # "UFC", "Bellator", etc

    name: str  # "UFC 300"
    subtitle: Optional[str] = None  # "Adesanya vs Péreira"
    slug: str  # Para URL: "ufc-300"
    url: str  # Link al sitio de dónde lo sacamos

    date: date  # Cuándo es el evento
    start_time_et: Optional[str] = None
    timezone: Optional[str] = None  # Zona horaria

    location: Optional[dict] = None  # {venue, city, country}

    status: str  # "scheduled" | "completed" | "cancelled"

    total_bouts: int  # Cuántas peleas tiene
    main_event_bout_id: Optional[int] = None  # ID de la pelea principal
    # Vertical card poster resolved through the event's Wikipedia Source/Credit.
    poster_image_url: Optional[str] = None
    poster_image_source: Optional[str] = None
    poster_source_page_url: Optional[str] = None
    wikipedia_article_url: Optional[str] = None
    wikipedia_file_url: Optional[str] = None
    wikipedia_image_url: Optional[str] = None

    # Wide high-resolution art from the official UFC event page.
    hero_image_url: Optional[str] = None
    hero_image_source: Optional[str] = None
    official_url: Optional[str] = None

    # Stable ESPN source mapping used by the ESPN ETL.
    espn_event_id: Optional[str] = None
    espn_url: Optional[str] = None
    # event_art stored as binary in MongoDB, served via /events/{id}/event-art endpoint

    picks_locked: bool = False  # Admin puede lockear picks para este evento
    picks_lock_override: Optional[str] = None  # locked | unlocked | None

    # Canonical UTC schedule. Each card section closes independently.
    card_start_time_utc: Optional[datetime] = None
    picks_lock_time_utc: Optional[datetime] = None
    section_start_times_utc: dict[str, datetime] = Field(default_factory=dict)
    section_lock_times_utc: dict[str, datetime] = Field(default_factory=dict)
    timing_source: Optional[str] = None  # espn | admin

    scraped_at: datetime  # Cuándo lo metimos a la BD
    last_updated: datetime  # Última actualización

    class Config:
        populate_by_name = True


class EventCardSlot(BaseModel):
    """La posición de una pelea dentro de la cartelera de un evento"""
    
    id: str  # ID único: "event_id:bout_id"

    event_id: int
    bout_id: int

    card_section: str  # "main" | "prelim" | "early_prelim"

    order_overall: int  # Posición en toda la cartelera (1, 2, 3...)
    order_section: int  # Posición dentro de su sección (main 1, main 2...)

    is_main_event: bool = False  # La pelea principal
    is_co_main: bool = False  # La pelea co-principal

    class Config:
        populate_by_name = True
