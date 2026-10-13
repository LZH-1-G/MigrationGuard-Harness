from typing import Dict, Generic, List, Optional, TypeVar

from pydantic import BaseModel, Field, root_validator, validator
from pydantic.generics import GenericModel

T = TypeVar("T")


class Price(BaseModel):
    amount_cents: int = Field(..., ge=0, alias="amountCents")
    currency: str = "EUR"

    class Config:
        allow_population_by_field_name = True
        allow_mutation = False


class Product(BaseModel):
    product_id: str = Field(..., alias="productId")
    title: str
    price: Price
    tags: List[str] = []
    vat_rate: float = 0.2
    gross_cents: int = 0

    class Config:
        allow_population_by_field_name = True

    @validator("title")
    def title_not_blank(cls, value):
        if not value.strip():
            raise ValueError("title must not be blank")
        return value.strip()

    @root_validator
    def compute_gross(cls, values):
        price = values.get("price")
        if price is not None:
            values["gross_cents"] = round(price.amount_cents * (1 + values.get("vat_rate", 0)))
        return values


class TagList(BaseModel):
    __root__: List[str]

    def normalised(self) -> List[str]:
        return sorted({t.lower() for t in self.__root__})


class Page(GenericModel, Generic[T]):
    items: List[T]
    total: int
    next_cursor: Optional[str]


def field_aliases(model) -> Dict[str, str]:
    return {name: field.alias for name, field in model.__fields__.items()}
