from pydantic import BaseModel
from typing import Optional

class Location(BaseModel):
    venue: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None

