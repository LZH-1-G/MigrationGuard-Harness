import json

import pytest
from pydantic import ValidationError

from webhooks.signing import next_attempt, outgoing, parse_event, sign, verify

BODY = json.dumps({"eventId": "evt_1", "type": "order.paid", "occurredAt": "2024-03-01T12:00:00Z", "data": {"amount": 1200}})


def test_parse_event():
    event = parse_event(BODY)
    assert event.id == "evt_1"
    assert event.type == "order.paid"
    assert event.version == "2023-10"
    assert event.delivery.attempt == 1


def test_population_by_field_name():
    event = parse_event(json.dumps({"id": "e", "type": "refund.issued", "occurred_at": "2024-01-01T00:00:00"}))
    assert event.id == "e"


def test_unknown_type_rejected():
    with pytest.raises(ValidationError):
        parse_event(json.dumps({"eventId": "e", "type": "nope", "occurredAt": "2024-01-01T00:00:00"}))


def test_version_is_constant():
    with pytest.raises(ValidationError):
        parse_event(json.dumps({"eventId": "e", "type": "order.paid", "occurredAt": "2024-01-01T00:00:00", "version": "2024-01"}))


def test_signature_roundtrip():
    event = parse_event(BODY)
    signature = sign(event, "s3cret")
    assert signature.startswith("sha256=") and verify(event, "s3cret", signature)
    assert not verify(event, "other", signature)


def test_outgoing_shape():
    data = outgoing(parse_event(BODY))
    assert data["eventId"] == "evt_1" and data["data"] == {"amount": 1200}
    assert data["delivery"]["retry_after"] == 30


def test_next_attempt():
    event = next_attempt(parse_event(BODY))
    assert event.delivery.attempt == 2
