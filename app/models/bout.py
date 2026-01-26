from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class FighterSnapshot(BaseModel):
    """Snapshot histórico del estado del peleador en una pelea específica"""

    fighter_name: str
    corner: Optional[str] = None  # red | blue

    # Rankings
    ranking: Optional[dict] = None
    ufc_ranking: Optional[dict] = None  # {"position": 1, "division": "Featherweight"}

    # Records
    record_at_fight: Optional[dict] = None  # wins / losses / draws
    last_fights: list[str] = []
    last_5_fights: Optional[list[str]] = None  # ["W", "L", "W", "W", "W"]

    # Betting information
    betting_odds: Optional[dict] = None  # {"line": "-160", "description": "Slight Favorite"}
    title_status: Optional[str] = None  # "Champion" | "Challenger"

    # Personal information
    nationality: Optional[str] = None
    fighting_out_of: Optional[str] = None
    nickname: Optional[str] = None

    # Physical stats
    age_at_fight_years: Optional[int] = None
    age_at_fight: Optional[dict] = None  # {"years": 37, "months": 4, "days": 2}
    height_cm: Optional[int] = None
    height: Optional[dict] = None  # {"feet": 5, "inches": 6, "cm": 168}
    reach_cm: Optional[int] = None
    reach: Optional[dict] = None  # {"inches": 71.5, "cm": 182}
    latest_weight: Optional[dict] = None  # {"lbs": 145.0, "kgs": 65.8}

    # Training
    gym: Optional[dict] = None  # {"primary": "Tiger Muay Thai", "other": ["Freestyle Fighting Gym"]}

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
