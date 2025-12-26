from pydantic import BaseModel
from typing import Optional

class CardStructure(BaseModel):
    has_main_card: bool
    has_prelims: bool
    has_early_prelims: bool
    