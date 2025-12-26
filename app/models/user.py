from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class User(BaseModel):
    id: str = Field(..., alias="_id")
    email: str
    username: str
    password_hash: Optional[str] = None

    avatar_url: Optional[str] = None

    created_at: datetime
    last_login_at: Optional[datetime] = None

    is_active: bool = True
    is_admin: bool = False

    class Config:
        populate_by_name = True
