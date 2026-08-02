
from pydantic import BaseModel


class CardStructure(BaseModel):
    has_main_card: bool
    has_prelims: bool
    has_early_prelims: bool
