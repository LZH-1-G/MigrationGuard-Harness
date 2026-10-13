"""Receivers verify signatures computed by the *old* service: the payload string must not change."""
import json

from webhooks.signing import outgoing, parse_event, sign, signing_payload

BODY = json.dumps({"eventId": "evt_9", "type": "order.created", "occurredAt": "2024-03-01T12:00:00+00:00", "data": {"sku": "A-1", "qty": 2}})


def test_signing_payload_is_byte_identical():
    assert signing_payload(parse_event(BODY)) == (
        '{"eventId": "evt_9", "type": "order.created", "occurredAt": "2024-03-01T12:00:00+00:00", '
        '"version": "2023-10", "data": {"sku": "A-1", "qty": 2}}'
    )


def test_signature_matches_receivers():
    assert sign(parse_event(BODY), "whsec") == "sha256=%s" % __import__("hmac").new(
        b"whsec",
        b'{"eventId": "evt_9", "type": "order.created", "occurredAt": "2024-03-01T12:00:00+00:00", '
        b'"version": "2023-10", "data": {"sku": "A-1", "qty": 2}}',
        "sha256",
    ).hexdigest()


def test_outgoing_timestamps_and_retry():
    data = outgoing(parse_event(BODY))
    assert data["occurredAt"] == "2024-03-01T12:00:00+00:00"
    assert data["delivery"] == {"attempt": 1, "retry_after": 30}
    assert data["type"] == "order.created"
