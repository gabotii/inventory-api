from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import Environment, Settings

STRONG = {
    "app_env": "production",
    "database_url": "postgresql+asyncpg://svc:Strong-Pass@db.internal:5432/inventory",
    "jwt_secret": "k" * 48,
}


def build(**values: Any) -> Settings:
    return Settings(_env_file=None, **values)


def test_development_defaults_are_usable() -> None:
    settings = build()
    assert settings.app_env is Environment.DEVELOPMENT
    assert not settings.is_production


def test_production_rejects_insecure_defaults() -> None:
    with pytest.raises(ValidationError) as excinfo:
        build(app_env="production")
    message = str(excinfo.value)
    assert "JWT_SECRET" in message
    assert "DATABASE_URL" in message


def test_production_accepts_explicit_secure_configuration() -> None:
    settings = build(**STRONG)
    assert settings.is_production


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"jwt_secret": "short"}, "JWT_SECRET"),
        ({"argon2_memory_cost_kib": 1024}, "ARGON2_MEMORY_COST_KIB"),
        ({"argon2_time_cost": 1}, "ARGON2_TIME_COST"),
        ({"rate_limit_enabled": False}, "RATE_LIMIT_ENABLED"),
        ({"cors_allowed_origins": "*"}, "CORS_ALLOWED_ORIGINS"),
        ({"db_echo": True}, "DB_ECHO"),
    ],
)
def test_production_rejects_each_weak_setting(override: dict[str, object], expected: str) -> None:
    with pytest.raises(ValidationError, match=expected):
        build(**{**STRONG, **override})


def test_database_url_must_use_asyncpg() -> None:
    with pytest.raises(ValidationError, match="asyncpg"):
        build(database_url="postgresql://u:p@localhost/db")


def test_cors_origins_parsed_from_comma_separated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://a.example, https://b.example")
    assert build().cors_allowed_origins == ["https://a.example", "https://b.example"]


def test_invalid_log_level_rejected() -> None:
    with pytest.raises(ValidationError):
        build(log_level="LOUD")


def test_docs_default_depends_on_environment() -> None:
    assert build().docs_enabled
    assert not build(**STRONG).docs_enabled
    assert build(**STRONG, expose_docs=True).docs_enabled


def test_secret_is_not_exposed_in_repr() -> None:
    settings = build(**STRONG)
    assert "k" * 48 not in repr(settings)
    assert "Strong-Pass" not in repr(settings.jwt_secret)
