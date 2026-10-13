from typing import Any, Dict, Iterable, List

from inventory.models import Inventory, Item, Supplier


class SupplierRow:
    """Stand-in for an ORM row object."""

    def __init__(self, name: str, country: str, contact_email: str = None):
        self.name = name
        self.country = country
        self.contact_email = contact_email


def load_inventory(rows: Iterable[Dict[str, Any]]) -> Inventory:
    return Inventory(items=[Item.parse_obj(row) for row in rows])


def export_item(item: Item) -> Dict[str, Any]:
    data = item.dict(exclude_none=True)
    data["category"] = item.category.value
    return data


def restock(item: Item, amount: int) -> Item:
    if amount <= 0:
        raise ValueError("amount must be positive")
    return item.copy(update={"quantity": item.quantity + amount})


def supplier_from_row(row: SupplierRow) -> Supplier:
    return Supplier.from_orm(row)


def low_stock(inventory: Inventory, threshold: int = 5) -> List[str]:
    return sorted(item.sku for item in inventory.items if item.quantity < threshold and item.discontinued_at is None)
