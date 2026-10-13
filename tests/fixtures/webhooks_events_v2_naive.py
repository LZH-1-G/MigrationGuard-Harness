from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator


class EventType(str, Enum):
    ORDER_CREATED = "order.created"
    ORDER_PAID = "order.paid"
    REFUND_ISSUED = "refund.issued"


class Delivery(BaseModel):
    attempt: int = 1
    retry_after: timedelta = timedelta(seconds=30)

    @field_validator("attempt", mode="before")
    @classmethod
    def _truncate(cls, value):
        return int(value) if isinstance(value, float) else value  # v1 truncated floats

    @field_serializer("retry_after", when_used="json")
    def _retry_seconds(self, value: timedelta) -> int:
        return int(value.total_seconds())


class Event(BaseModel):
    id: str = Field(..., alias="eventId")
    type: EventType
    occurred_at: datetime = Field(..., alias="occurredAt")
    version: Literal["2023-10"] = "2023-10"
    data: Dict[str, Any] = {}
    delivery: Delivery = Delivery()

    model_config = ConfigDict(populate_by_name=True, use_enum_values=True, coerce_numbers_to_str=True)

    @field_serializer("occurred_at", when_used="json")
    def _iso(self, value: datetime) -> str:
        return value.isoformat()  # keep v1's "+00:00" (v2 would write "Z")
