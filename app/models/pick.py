"""
Pick model - user predictions for bouts.
"""

from datetime import datetime
from typing import Optional, Literal

from pydantic import BaseModel, Field


VictoryMethod = Literal["DEC", "KO/TKO", "SUB"]
Corner = Literal["red", "blue"]


class Pick(BaseModel):
    """User prediction for a bout."""

    id: str = Field(..., alias="_id")  # user_id:bout_id

    user_id: str
    event_id: int
    bout_id: int

    picked_corner: Corner
    picked_method: VictoryMethod
    picked_round: Optional[int] = None  # 1-5, only if method != DEC

    is_correct: Optional[bool] = None
    points_awarded: int = 0

    locked: bool = False

    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        populate_by_name = True


class PickCreate(BaseModel):
    """Data to create or update a pick."""

    event_id: int
    bout_id: int
    picked_corner: Corner
    picked_method: VictoryMethod
    picked_round: Optional[int] = Field(None, ge=1, le=5)


class PickResponse(BaseModel):
    """Pick data returned by API."""

    id: str
    bout_id: int
    event_id: int
    picked_corner: Corner
    picked_method: VictoryMethod
    picked_round: Optional[int] = None
    is_correct: Optional[bool] = None
    points_awarded: int = 0
    locked: bool = False
    created_at: datetime
