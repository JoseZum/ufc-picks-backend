from datetime import datetime, date
from typing import Optional
from pydantic import BaseModel


class Event(BaseModel):
    id: int
    source: str
    promotion: str

    name: str
    subtitle: Optional[str] = None
    slug: str
    url: str

    date: date
    timezone: Optional[str] = None

    location: Optional[dict] = None

    status: str  # scheduled | completed | cancelled

    total_bouts: int
    main_event_bout_id: Optional[int] = None

    scraped_at: datetime
    last_updated: datetime

    class Config:
        populate_by_name = True


class EventCardSlot(BaseModel):
    """Orden de una pelea dentro de la cartelera de un evento"""
    
    id: str  # event_id:bout_id

    event_id: int
    bout_id: int

    card_section: str  # main | prelim | early_prelim

    order_overall: int
    order_section: int

    is_main_event: bool = False
    is_co_main: bool = False

    class Config:
        populate_by_name = True
