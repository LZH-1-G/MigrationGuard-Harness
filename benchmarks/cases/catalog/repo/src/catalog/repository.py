from typing import Any, Dict, List

from catalog.models import Page, Product, TagList


class Repository:
    def __init__(self) -> None:
        self._items: Dict[str, Product] = {}

    def add(self, payload: Dict[str, Any]) -> Product:
        product = Product.parse_obj(payload)
        self._items[product.product_id] = product
        return product

    def page(self, limit: int = 10) -> Page[Product]:
        items = sorted(self._items.values(), key=lambda p: p.product_id)
        return Page[Product](items=items[:limit], total=len(items), next_cursor=items[limit].product_id if len(items) > limit else None)

    def export(self) -> List[Dict[str, Any]]:
        return [p.dict(by_alias=True) for p in sorted(self._items.values(), key=lambda p: p.product_id)]

    def all_tags(self) -> List[str]:
        tags = TagList.parse_obj([t for p in self._items.values() for t in p.tags])
        return tags.normalised()
