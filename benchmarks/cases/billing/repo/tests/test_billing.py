import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from billing.api import invoice_to_json, parse_invoice, roundtrip, summary

PAYLOAD = {
    "invoice_id": "INV-1",
    "customer_id": "C-42",
    "issued": "2024-03-01",
    "lines": [{"description": "consulting", "quantity": 2, "unit_price": "150.00"}, {"description": "travel", "unit_price": "80.5"}],
}


def test_parse_and_total():
    invoice = parse_invoice(PAYLOAD)
    assert invoice.total == Decimal("380.50")
    assert invoice.currency == "USD"
    assert invoice.notes is None


def test_requires_lines():
    with pytest.raises(ValidationError):
        parse_invoice(dict(PAYLOAD, lines=[]))


def test_currency_must_be_iso():
    with pytest.raises(ValidationError):
        parse_invoice(dict(PAYLOAD, currency="usd"))


def test_strips_whitespace():
    assert parse_invoice(dict(PAYLOAD, customer_id="  C-42 ")).customer_id == "C-42"


def test_summary():
    assert summary(parse_invoice(PAYLOAD)) == {"invoice_id": "INV-1", "customer_id": "C-42", "lines": 2, "total": "380.50", "currency": "USD"}


def test_json_contains_fields():
    data = json.loads(invoice_to_json(parse_invoice(PAYLOAD)))
    assert data["invoice_id"] == "INV-1"
    assert len(data["lines"]) == 2


def test_roundtrip():
    invoice = parse_invoice(PAYLOAD)
    assert roundtrip(invoice) == invoice
