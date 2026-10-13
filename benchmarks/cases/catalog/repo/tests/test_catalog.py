import pytest
from pydantic import ValidationError

from catalog.models import Page, Price, Product, TagList, field_aliases
from catalog.repository import Repository

P1 = {"productId": "p-1", "title": " Lamp ", "price": {"amountCents": 1000}, "tags": ["Home", "light"]}
P2 = {"product_id": "p-2", "title": "Desk", "price": {"amount_cents": 25000}, "vat_rate": 0.1, "tags": ["home"]}


def test_alias_and_field_name_population():
    repo = Repository()
    assert repo.add(P1).product_id == "p-1"
    assert repo.add(P2).price.amount_cents == 25000


def test_gross_is_computed():
    repo = Repository()
    assert repo.add(P1).gross_cents == 1200
    assert repo.add(P2).gross_cents == 27500


def test_blank_title_rejected():
    with pytest.raises(ValidationError):
        Product.parse_obj(dict(P1, title="   "))


def test_price_is_immutable():
    price = Price(amountCents=5)
    with pytest.raises((TypeError, ValueError)):  # v1: TypeError, v2: ValidationError
        price.currency = "USD"


def test_export_uses_aliases():
    repo = Repository()
    repo.add(P1)
    assert repo.export()[0]["productId"] == "p-1"
    assert repo.export()[0]["price"] == {"amountCents": 1000, "currency": "EUR"}


def test_pagination():
    repo = Repository()
    repo.add(P1)
    repo.add(P2)
    page = repo.page(limit=1)
    assert page.total == 2 and page.next_cursor == "p-2"
    assert [p.product_id for p in page.items] == ["p-1"]


def test_tags():
    repo = Repository()
    repo.add(P1)
    repo.add(P2)
    assert repo.all_tags() == ["home", "light"]


def test_field_aliases():
    assert field_aliases(Product)["product_id"] == "productId"
    assert field_aliases(Product)["title"] == "title"
