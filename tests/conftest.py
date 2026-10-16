from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import PostgresDsn, SecretStr

from app.core.config import Environment, LogFormat, Settings

ROOT = Path(__file__).resolve().parents[1]
TEST_JWT_SECRET = "test-secret-" + "x" * 40


def _read_env_file_value(name: str) -> str | None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip()
    return None


def database_url_for_tests() -> str | None:
    return os.environ.get("TEST_DATABASE_URL") or _read_env_file_value("TEST_DATABASE_URL")


def make_settings(**overrides: Any) -> Settings:
    """Settings for tests: cheap Argon2 (speed), quiet logs, isolated database."""
    url = database_url_for_tests() or "postgresql+asyncpg://unset:unset@127.0.0.1:5432/unset"
    values: dict[str, Any] = {
        "app_env": Environment.TEST,
        "database_url": PostgresDsn(url),
        "jwt_secret": SecretStr(TEST_JWT_SECRET),
        "argon2_time_cost": 1,
        "argon2_memory_cost_kib": 8,
        "argon2_parallelism": 1,
        "log_format": LogFormat.CONSOLE,
        "log_level": "WARNING",
        "db_pool_size": 15,
        "db_max_overflow": 15,
        "db_statement_timeout_ms": 10_000,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture(scope="session")
def settings() -> Settings:
    return make_settings()
