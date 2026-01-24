from datetime import datetime, date
from typing import Optional
from pydantic import BaseModel


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
    timezone: Optional[str] = None  # Zona horaria

    location: Optional[dict] = None  # {venue, city, country}

    status: str  # "scheduled" | "completed" | "cancelled"

    total_bouts: int  # Cuántas peleas tiene
    main_event_bout_id: Optional[int] = None  # ID de la pelea principal
    poster_image_url: Optional[str] = None  # /proxy/tapology/poster_images/... path for nginx

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
