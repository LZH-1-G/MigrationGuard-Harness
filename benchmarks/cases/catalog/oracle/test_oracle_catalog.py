from catalog.models import Page, Price, Product, TagList, field_aliases
from catalog.repository import Repository


def test_export_exact():
    repo = Repository()
    repo.add({"productId": "z", "title": "Z", "price": {"amountCents": 999, "currency": "USD"}, "vat_rate": 0})
    assert repo.export() == [{"productId": "z", "title": "Z", "price": {"amountCents": 999, "currency": "USD"},
                              "tags": [], "vat_rate": 0.0, "gross_cents": 999}]


def test_generic_page_of_ints():
    page = Page[int](items=["1", 2], total=2, next_cursor=None)
    assert page.items == [1, 2]


def test_tags_across_products():
    repo = Repository()
    assert repo.all_tags() == []
    repo.add({"productId": "a", "title": "A", "price": {"amountCents": 1}, "tags": ["B", "a"]})
    repo.add({"productId": "b", "title": "B", "price": {"amountCents": 1}, "tags": ["b"]})
    assert repo.all_tags() == ["a", "b"]


def test_aliases_for_price():
    assert field_aliases(Price) == {"amount_cents": "amountCents", "currency": "currency"}
