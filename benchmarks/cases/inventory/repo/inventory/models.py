from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, root_validator, validator


class Category(str, Enum):
    TOOLS = "tools"
    GARDEN = "garden"
    KITCHEN = "kitchen"


class Supplier(BaseModel):
    name: str
    country: str = "US"
    contact_email: Optional[str]

    class Config:
        orm_mode = True
        anystr_strip_whitespace = True


class Item(BaseModel):
    sku: str = Field(..., regex=r"^[A-Z]{3}-\d{4}$")
    name: str = Field(..., min_length=1, max_length=80)
    category: Category
    unit_price: float = Field(..., ge=0)
    quantity: int = 0
    tags: List[str] = []
    supplier: Optional[Supplier]
    discontinued_at: Optional[datetime]

    class Config:
        orm_mode = True
        allow_population_by_field_name = True

    @validator("tags", pre=True)
    def split_tags(cls, value):
        if isinstance(value, str):
            return [t.strip() for t in value.split(",") if t.strip()]
        return value

    @validator("name")
    def normalise_name(cls, value):
        return " ".join(value.split()).title()

    @root_validator(skip_on_failure=True)
    def discontinued_items_have_no_stock(cls, values):
        if values.get("discontinued_at") is not None and values.get("quantity", 0) > 0:
            raise ValueError("discontinued items cannot have stock")
        return values

    @property
    def stock_value(self) -> float:
        return round(self.unit_price * self.quantity, 2)


class Inventory(BaseModel):
    items: List[Item] = []

    def total_value(self) -> float:
        return round(sum(item.stock_value for item in self.items), 2)

    def by_category(self) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for item in self.items:
            out.setdefault(item.category.value, []).append(item.sku)
        return out

    def find(self, sku: str) -> Optional[Item]:
        for item in self.items:
            if item.sku == sku:
                return item
        return None
