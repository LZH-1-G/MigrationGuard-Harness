from typing import Dict, List, Optional

from pydantic import AnyHttpUrl, BaseSettings, Field, validator


class Settings(BaseSettings):
    service_name: str = "orders"
    database_url: str = Field("sqlite:///orders.db", env="DATABASE_URL")
    allowed_hosts: List[str] = ["localhost"]
    cors_origins: List[AnyHttpUrl] = []
    max_connections: int = 10
    debug: bool = False
    sentry_dsn: Optional[str]

    class Config:
        env_prefix = "ORDERS_"
        case_sensitive = False

    @validator("allowed_hosts", each_item=True)
    def normalise_host(cls, host):
        if " " in host:
            raise ValueError("hosts cannot contain spaces")
        return host.lower()

    @validator("max_connections")
    def positive(cls, value):
        if value < 1:
            raise ValueError("max_connections must be >= 1")
        return value


def load_settings(environ: Optional[Dict[str, str]] = None) -> Settings:
    """Build settings from an explicit mapping (tests, CLI) or the process environment."""
    if environ is None:
        return Settings()
    import os

    saved = dict(os.environ)
    try:
        for key in [k for k in os.environ if k.startswith("ORDERS_") or k == "DATABASE_URL"]:
            del os.environ[key]
        os.environ.update(environ)
        return Settings()
    finally:
        os.environ.clear()
        os.environ.update(saved)


def describe(settings: Settings) -> Dict[str, object]:
    data = settings.dict(exclude={"database_url"})
    data["cors_origins"] = [str(origin) for origin in settings.cors_origins]
    data["database"] = settings.database_url.split(":", 1)[0]
    return data
