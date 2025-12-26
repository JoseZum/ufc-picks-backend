from typing import Optional
from pydantic import BaseModel


class LeaderboardEntry(BaseModel):
    """Entrada en una tabla de clasificación (resultado agregado)"""
    
    user_id: str
    username: str
    avatar_url: Optional[str] = None

    total_points: int
    accuracy: float

    picks_total: int
    picks_correct: int

    category: str  # global | main_events | prelims | early_prelims
    scope: str     # all_time | year | event

    class Config:
        populate_by_name = True
