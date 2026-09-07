from datetime import date, datetime

from pydantic import BaseModel, field_validator


class FighterSnapshot(BaseModel):
    """Snapshot histórico del estado del peleador en una pelea específica"""

    fighter_name: str
    corner: str | None = None  # red | blue

    # Rankings
    ranking: dict | None = None
    ufc_ranking: dict | None = None  # {"position": 1, "division": "Featherweight"}

    # Records
    record_at_fight: dict | None = None  # wins / losses / draws
    last_fights: list[str] = []
    last_5_fights: list[str] | None = None  # ["W", "L", "W", "W", "W"]

    # Betting information
    betting_odds: dict | None = None  # {"line": "-160", "description": "Slight Favorite"}
    title_status: str | None = None  # "Champion" | "Challenger"

    # Personal information
    nationality: str | None = None
    fighting_out_of: str | None = None
    nickname: str | None = None

    # Physical stats
    age_at_fight_years: int | None = None
    age_at_fight: dict | None = None  # {"years": 37, "months": 4, "days": 2}
    height_cm: int | None = None
    height: dict | None = None  # {"feet": 5, "inches": 6, "cm": 168}
    reach_cm: int | None = None
    reach: dict | None = None  # {"inches": 71.5, "cm": 182}
    latest_weight: dict | None = None  # {"lbs": 145.0, "kgs": 65.8}

    # Training
    gym: dict | None = None  # {"primary": "Tiger Muay Thai", "other": ["Freestyle Fighting Gym"]}

    # ESPN enrichment
    espn_id: str | None = None
    espn_url: str | None = None
    espn_headshot_url: str | None = None
    date_of_birth: date | None = None
    stance: str | None = None
    weight_class: str | None = None
    career_stats: dict | None = None
    image_source: str | None = None

    # Tapology data for images
    tapology_id: str | None = None
    tapology_url: str | None = None
    profile_image_url: str | None = None  # /proxy/tapology/... path for nginx
    image_key: str | None = None  # S3 key for fighter image (e.g., "fighters/12345.jpg")

    class Config:
        populate_by_name = True


class Bout(BaseModel):
    """Pelea individual"""

    id: int
    event_id: int

    # These fields are Optional to handle legacy documents that may not have them
    source: str | None = None
    url: str | None = None
    slug: str | None = None
    espn_competition_id: str | None = None
    espn_match_number: int | None = None
    espn_card_segment: str | None = None

    weight_class: str | None = None
    gender: str | None = "male"

    rounds_scheduled: int | None = 3
    is_title_fight: bool = False
    is_bmf_title_fight: bool = False  # Pelea por el cinturón BMF (tratamiento plateado)
    is_main_event: bool = False  # La pelea principal del evento (5 rounds)
    is_co_main_event: bool = False
    card_section: str | None = None
    card_order: int | None = None
    order_overall: int | None = None
    order_section: int | None = None

    status: str = "scheduled"  # scheduled | completed

    fighters: dict[str, FighterSnapshot] = {}  # {"red": ..., "blue": ...}

    result: dict | None = None

    picks_locked: bool = False  # Admin puede lockear picks para esta pelea
    picks_lock_override: str | None = None  # locked | unlocked | None
    automatic_lock_time_utc: datetime | None = None

    scraped_at: datetime | None = None
    last_updated: datetime | None = None

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
