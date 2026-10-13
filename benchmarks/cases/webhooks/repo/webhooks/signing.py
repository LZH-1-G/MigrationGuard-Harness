import hashlib
import hmac
import json
from typing import Any, Dict

from webhooks.events import Event


def parse_event(body: str) -> Event:
    return Event.parse_raw(body)


def signing_payload(event: Event) -> str:
    """The exact string receivers recompute the HMAC over."""
    return event.json(by_alias=True, exclude={"delivery"})


def sign(event: Event, secret: str) -> str:
    digest = hmac.new(secret.encode(), signing_payload(event).encode(), hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify(event: Event, secret: str, signature: str) -> bool:
    return hmac.compare_digest(sign(event, secret), signature)


def outgoing(event: Event) -> Dict[str, Any]:
    return json.loads(event.json(by_alias=True))


def next_attempt(event: Event) -> Event:
    delivery = event.delivery.copy(update={"attempt": event.delivery.attempt + 1})
    return event.copy(update={"delivery": delivery})
