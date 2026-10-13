"""Public API of the billing service."""
import json
from typing import Any, Dict

from billing.models import Invoice


def parse_invoice(payload: Dict[str, Any]) -> Invoice:
    return Invoice.model_validate(payload)


def from_crm(record: Dict[str, Any]) -> Invoice:
    """The CRM export uses integer ids and a flat line list."""
    return Invoice.model_validate(
        {
            "invoice_id": record["id"],
            "customer_id": record["customer"],
            "issued": record["date"],
            "lines": [{"description": d, "quantity": q, "unit_price": p} for d, q, p in record["lines"]],
        }
    )


def invoice_to_json(invoice: Invoice) -> str:
    """Serialised for the accounting system (amounts are JSON numbers)."""
    return invoice.model_dump_json()


def summary(invoice: Invoice) -> Dict[str, Any]:
    return {
        "invoice_id": invoice.invoice_id,
        "customer_id": invoice.customer_id,
        "lines": len(invoice.lines),
        "total": str(invoice.total),
        "currency": invoice.currency,
    }


def roundtrip(invoice: Invoice) -> Invoice:
    return Invoice.model_validate_json(invoice_to_json(invoice))


def to_accounting(invoice: Invoice) -> Dict[str, Any]:
    return json.loads(invoice_to_json(invoice))
