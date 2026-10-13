from datetime import date
from decimal import Decimal
from typing import List, Optional

from pydantic import ConfigDict, field_serializer, field_validator, BaseModel, Field


class LineItem(BaseModel):
    description: str
    quantity: int = Field(1, ge=1)
    unit_price: Decimal = Field(..., ge=0)

    model_config = ConfigDict(coerce_numbers_to_str=True)

    @field_validator("quantity", mode="before")
    @classmethod
    def _truncate_float_quantity(cls, v):
        # Pydantic v1 truncated floats for int fields; keep that behaviour.
        if isinstance(v, float) and v == v and v not in (float("inf"), float("-inf")):
            return int(v)
        return v

    @field_serializer("unit_price", when_used="json")
    def _decimal_as_number(self, v: Decimal):
        # Pydantic v1 encoded Decimal as a JSON number.
        return float(v)

    @property
    def total(self) -> Decimal:
        return (self.unit_price * self.quantity).quantize(Decimal("0.01"))


class Invoice(BaseModel):
    invoice_id: str
    customer_id: str
    issued: date
    currency: str = Field("USD", pattern=r"^[A-Z]{3}$")
    lines: List[LineItem]
    notes: Optional[str] = None

    model_config = ConfigDict(str_strip_whitespace=True, coerce_numbers_to_str=True)

    @field_validator("lines")
    @classmethod
    def at_least_one_line(cls, lines):
        if not lines:
            raise ValueError("an invoice needs at least one line")
        return lines

    @property
    def total(self) -> Decimal:
        return sum((line.total for line in self.lines), Decimal("0.00"))
