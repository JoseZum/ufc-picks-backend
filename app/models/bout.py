from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class FighterSnapshot(BaseModel):
    """Snapshot histórico del estado del peleador en una pelea específica"""

    fighter_name: str
    corner: str  # red | blue

    ranking: Optional[dict] = None

    record_at_fight: dict  # wins / losses / draws
    last_fights: list[str] = []

    nationality: str
    fighting_out_of: Optional[str] = None

    age_at_fight_years: int

    height_cm: Optional[int] = None
    reach_cm: Optional[int] = None

    # Tapology data for images
    tapology_id: Optional[str] = None
    tapology_url: Optional[str] = None
    profile_image_url: Optional[str] = None  # /proxy/tapology/... path for nginx

    class Config:
        populate_by_name = True


class Bout(BaseModel):
    """Pelea individual"""
    
    id: int
    event_id: int

    source: str
    url: str
    slug: str

    weight_class: str
    gender: str

    rounds_scheduled: int
    is_title_fight: bool

    status: str  # scheduled | completed

    fighters: dict[str, FighterSnapshot]  # {"red": ..., "blue": ...}

    result: Optional[dict] = None

    scraped_at: datetime
    last_updated: datetime

    class Config:
        populate_by_name = True
