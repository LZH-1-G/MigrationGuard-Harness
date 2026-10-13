from decimal import Decimal

from billing.api import from_crm, parse_invoice, summary, to_accounting

CRM = {"id": 9001, "customer": 1042, "date": "2024-05-05", "lines": [["licence", 3, "19.99"], ["support", 1, "100"]]}


def test_crm_numeric_ids_are_accepted_as_strings():
    invoice = from_crm(CRM)
    assert invoice.invoice_id == "9001" and invoice.customer_id == "1042"
    assert summary(invoice)["total"] == "159.97"


def test_accounting_receives_numbers():
    data = to_accounting(from_crm(CRM))
    assert data["lines"][0]["unit_price"] == 19.99
    assert isinstance(data["lines"][1]["unit_price"], (int, float))
    assert data["issued"] == "2024-05-05"


def test_string_ids_still_work():
    inv = parse_invoice({"invoice_id": "A", "customer_id": "B", "issued": "2024-01-01", "lines": [{"description": "x", "unit_price": 1}]})
    assert inv.total == Decimal("1.00")
