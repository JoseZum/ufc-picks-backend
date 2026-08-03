from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, field_validator


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

    # ESPN enrichment
    espn_id: Optional[str] = None
    espn_url: Optional[str] = None
    espn_headshot_url: Optional[str] = None
    date_of_birth: Optional[date] = None
    stance: Optional[str] = None
    weight_class: Optional[str] = None
    career_stats: Optional[dict] = None
    image_source: Optional[str] = None

    # Tapology data for images
    tapology_id: Optional[str] = None
    tapology_url: Optional[str] = None
    profile_image_url: Optional[str] = None  # /proxy/tapology/... path for nginx
    image_key: Optional[str] = None  # S3 key for fighter image (e.g., "fighters/12345.jpg")

    class Config:
        populate_by_name = True


class Bout(BaseModel):
    """Pelea individual"""

    id: int
    event_id: int

    # These fields are Optional to handle legacy documents that may not have them
    source: Optional[str] = None
    url: Optional[str] = None
    slug: Optional[str] = None
    espn_competition_id: Optional[str] = None
    espn_match_number: Optional[int] = None
    espn_card_segment: Optional[str] = None

    weight_class: Optional[str] = None
    gender: Optional[str] = "male"

    rounds_scheduled: Optional[int] = 3
    is_title_fight: bool = False
    is_bmf_title_fight: bool = False  # Pelea por el cinturón BMF (tratamiento plateado)
    is_main_event: bool = False  # La pelea principal del evento (5 rounds)
    is_co_main_event: bool = False
    card_section: Optional[str] = None
    card_order: Optional[int] = None
    order_overall: Optional[int] = None
    order_section: Optional[int] = None

    status: str = "scheduled"  # scheduled | completed

    fighters: dict[str, FighterSnapshot] = {}  # {"red": ..., "blue": ...}

    result: Optional[dict] = None

    picks_locked: bool = False  # Admin puede lockear picks para esta pelea
    picks_lock_override: Optional[str] = None  # locked | unlocked | None
    automatic_lock_time_utc: Optional[datetime] = None

    scraped_at: Optional[datetime] = None
    last_updated: Optional[datetime] = None

    @field_validator(
        "is_title_fight",
        "is_bmf_title_fight",
        "is_main_event",
        "is_co_main_event",
        "picks_locked",
        "status",
        mode="before",
    )
    @classmethod
    def _null_reads_as_default(cls, value, info):
        """A stored `null` reads as the field's default.

        A Pydantic default only applies when the KEY IS ABSENT. When the
        scraper leaves the key present with `null`, validation runs and
        raises — and because the whole card is parsed in one list
        comprehension, one null flag on one fight answered the entire
        `/events/{id}/bouts` request with a 500.

        Defaulting is the honest reading, not a paper-over: "we do not know
        whether this is a title fight" and "it is not one" already mean the
        same thing to every consumer, all of which type the field as a plain
        bool.
        """
        return cls.model_fields[info.field_name].default if value is None else value

    class Config:
        populate_by_name = True
