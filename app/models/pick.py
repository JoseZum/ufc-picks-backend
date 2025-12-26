from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class Pick(BaseModel):
    """Elección de un usuario para una pelea"""
    
    id: str  # user_id:bout_id

    user_id: str
    event_id: int
    bout_id: int

    picked_corner: str  # red | blue

    is_correct: Optional[bool] = None
    points_awarded: int = 0

    created_at: datetime
    locked_at: datetime

    class Config:
        populate_by_name = True
