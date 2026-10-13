import pytest
from pydantic import ValidationError

from orders.config import describe, load_settings


def test_defaults():
    s = load_settings({})
    assert s.service_name == "orders"
    assert s.allowed_hosts == ["localhost"]
    assert s.sentry_dsn is None
    assert s.debug is False


def test_prefixed_env_and_explicit_env_name():
    s = load_settings({"ORDERS_DEBUG": "true", "ORDERS_MAX_CONNECTIONS": "25", "DATABASE_URL": "postgresql://db/orders"})
    assert s.debug is True and s.max_connections == 25
    assert s.database_url == "postgresql://db/orders"


def test_hosts_are_lowercased():
    s = load_settings({"ORDERS_ALLOWED_HOSTS": '["API.Example.com", "localhost"]'})
    assert s.allowed_hosts == ["api.example.com", "localhost"]


def test_hosts_with_spaces_rejected():
    with pytest.raises(ValidationError):
        load_settings({"ORDERS_ALLOWED_HOSTS": '["bad host"]'})


def test_cors_origins_are_validated_urls():
    s = load_settings({"ORDERS_CORS_ORIGINS": '["https://app.example.com"]'})
    assert s.cors_origins == ["https://app.example.com"]
    with pytest.raises(ValidationError):
        load_settings({"ORDERS_CORS_ORIGINS": '["not a url"]'})


def test_max_connections_positive():
    with pytest.raises(ValidationError):
        load_settings({"ORDERS_MAX_CONNECTIONS": "0"})


def test_describe_hides_database_url():
    d = describe(load_settings({"DATABASE_URL": "postgresql://secret@db/orders"}))
    assert "database_url" not in d and d["database"] == "postgresql"
