import pytest
from pydantic import ValidationError

from inventory.models import Category, Inventory, Item, Supplier


def make_item(**overrides):
    data = {"sku": "TOL-0001", "name": "hammer", "category": "tools", "unit_price": 12.5, "quantity": 3}
    data.update(overrides)
    return Item.parse_obj(data)


def test_item_parses_and_normalises_name():
    item = make_item(name="  claw   hammer ")
    assert item.name == "Claw Hammer"
    assert item.category is Category.TOOLS


def test_tags_from_comma_separated_string():
    assert make_item(tags="steel, heavy ,").tags == ["steel", "heavy"]


def test_invalid_sku_rejected():
    with pytest.raises(ValidationError):
        make_item(sku="bad")


def test_negative_price_rejected():
    with pytest.raises(ValidationError):
        make_item(unit_price=-1)


def test_optional_fields_default_to_none():
    item = make_item()
    assert item.supplier is None
    assert item.discontinued_at is None


def test_discontinued_with_stock_rejected():
    with pytest.raises(ValidationError):
        make_item(discontinued_at="2024-01-01T00:00:00", quantity=2)


def test_stock_value_and_inventory_total():
    inv = Inventory(items=[make_item(), make_item(sku="TOL-0002", unit_price=1.25, quantity=4)])
    assert inv.total_value() == 42.5
    assert inv.by_category() == {"tools": ["TOL-0001", "TOL-0002"]}


def test_supplier_strips_whitespace():
    assert Supplier(name="  Acme  ").name == "Acme"


def test_dict_roundtrip():
    item = make_item(tags=["a"])
    assert Item.parse_obj(item.dict()) == item
