import pytest

from inventory.service import SupplierRow, export_item, load_inventory, low_stock, restock, supplier_from_row

ROWS = [
    {"sku": "GAR-0100", "name": "rake", "category": "garden", "unit_price": 20, "quantity": 2},
    {"sku": "KIT-0200", "name": "whisk", "category": "kitchen", "unit_price": 3.5, "quantity": 40},
]


def test_load_inventory():
    inv = load_inventory(ROWS)
    assert [i.sku for i in inv.items] == ["GAR-0100", "KIT-0200"]


def test_export_item_drops_none_values():
    item = load_inventory(ROWS).items[0]
    data = export_item(item)
    assert "supplier" not in data
    assert data["category"] == "garden"
    assert data["quantity"] == 2


def test_restock_returns_new_item():
    item = load_inventory(ROWS).items[0]
    new = restock(item, 3)
    assert new.quantity == 5
    assert item.quantity == 2


def test_restock_rejects_non_positive():
    with pytest.raises(ValueError):
        restock(load_inventory(ROWS).items[0], 0)


def test_supplier_from_row():
    supplier = supplier_from_row(SupplierRow("Acme", "DE"))
    assert supplier.name == "Acme" and supplier.country == "DE"


def test_low_stock():
    assert low_stock(load_inventory(ROWS)) == ["GAR-0100"]
