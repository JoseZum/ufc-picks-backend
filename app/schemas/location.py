
from pydantic import BaseModel


class Location(BaseModel):
    venue: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None

