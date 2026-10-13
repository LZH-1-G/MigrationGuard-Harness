from datetime import date
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, Field, validator


class LineItem(BaseModel):
    description: str
    quantity: int = Field(1, ge=1)
    unit_price: Decimal = Field(..., ge=0)

    @property
    def total(self) -> Decimal:
        return (self.unit_price * self.quantity).quantize(Decimal("0.01"))


class Invoice(BaseModel):
    invoice_id: str
    customer_id: str
    issued: date
    currency: str = Field("USD", regex=r"^[A-Z]{3}$")
    lines: List[LineItem]
    notes: Optional[str]

    class Config:
        anystr_strip_whitespace = True

    @validator("lines")
    def at_least_one_line(cls, lines):
        if not lines:
            raise ValueError("an invoice needs at least one line")
        return lines

    @property
    def total(self) -> Decimal:
        return sum((line.total for line in self.lines), Decimal("0.00"))
