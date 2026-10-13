"""Hidden behavioural oracle. Only uses the project's public API so it runs unchanged
against the original (pydantic v1) and the migrated (pydantic v2) code."""
import json

import pytest

from inventory.models import Inventory, Item
from inventory.service import SupplierRow, export_item, load_inventory, low_stock, restock, supplier_from_row


def item(**kw):
    data = {"sku": "TOL-0001", "name": "saw", "category": "tools", "unit_price": 9.99, "quantity": 1}
    data.update(kw)
    return load_inventory([data]).items[0]


def test_export_exact_shape():
    assert export_item(item(tags="a,b")) == {
        "sku": "TOL-0001", "name": "Saw", "category": "tools", "unit_price": 9.99, "quantity": 1, "tags": ["a", "b"],
    }


def test_population_by_field_name_and_optional_none():
    it = item(supplier={"name": " S "})
    assert it.supplier.name == "S" and it.supplier.contact_email is None


def test_numeric_string_quantity_coerced():
    assert item(quantity="7").quantity == 7


def test_root_validation_error():
    with pytest.raises(Exception):
        item(discontinued_at="2024-02-02T00:00:00", quantity=1)
    assert item(discontinued_at="2024-02-02T00:00:00", quantity=0).quantity == 0


def test_restock_is_copy():
    original = item(quantity=1)
    assert restock(original, 2).quantity == 3 and original.quantity == 1


def test_orm_conversion():
    s = supplier_from_row(SupplierRow("Z", "FR", "z@example.com"))
    assert (s.name, s.country, s.contact_email) == ("Z", "FR", "z@example.com")


def test_low_stock_ignores_discontinued():
    inv = load_inventory([
        {"sku": "AAA-0001", "name": "a", "category": "tools", "unit_price": 1, "quantity": 0, "discontinued_at": "2024-01-01T00:00:00"},
        {"sku": "AAA-0002", "name": "b", "category": "tools", "unit_price": 1, "quantity": 1},
    ])
    assert low_stock(inv) == ["AAA-0002"]
    assert inv.find("AAA-0002").name == "B"
    assert inv.find("nope") is None
