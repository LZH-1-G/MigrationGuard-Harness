from orders.config import describe, load_settings


def test_describe_exact():
    d = describe(load_settings({"ORDERS_CORS_ORIGINS": '["https://a.example.com","http://b.example.com:8080"]',
                                "ORDERS_ALLOWED_HOSTS": '["X.example.com"]'}))
    assert d == {
        "service_name": "orders",
        "allowed_hosts": ["x.example.com"],
        "cors_origins": ["https://a.example.com", "http://b.example.com:8080"],
        "max_connections": 10,
        "debug": False,
        "sentry_dsn": None,
        "database": "sqlite",
    }


def test_case_insensitive_env():
    assert load_settings({"orders_service_name": "billing"}).service_name == "billing"


def test_url_values_compare_as_strings():
    s = load_settings({"ORDERS_CORS_ORIGINS": '["https://a.example.com/path"]'})
    assert s.cors_origins[0] == "https://a.example.com/path"
    assert s.cors_origins[0].startswith("https://")
