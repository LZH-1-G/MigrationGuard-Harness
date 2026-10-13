from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict

from pydantic import BaseModel, Field


class EventType(str, Enum):
    ORDER_CREATED = "order.created"
    ORDER_PAID = "order.paid"
    REFUND_ISSUED = "refund.issued"


class Delivery(BaseModel):
    attempt: int = 1
    retry_after: timedelta = timedelta(seconds=30)

    class Config:
        json_encoders = {timedelta: lambda v: int(v.total_seconds())}


class Event(BaseModel):
    id: str = Field(..., alias="eventId")
    type: EventType
    occurred_at: datetime = Field(..., alias="occurredAt")
    version: str = Field("2023-10", const=True)
    data: Dict[str, Any] = {}
    delivery: Delivery = Delivery()

    class Config:
        allow_population_by_field_name = True
        use_enum_values = True
        json_encoders = {timedelta: lambda v: int(v.total_seconds())}
