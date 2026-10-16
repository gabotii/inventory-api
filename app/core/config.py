"""Typed application settings loaded from environment variables.

Development defaults exist so the app runs locally with zero setup, but
``Settings.validate_for_environment`` refuses to start in production with any
insecure default (weak JWT secret, docs exposed, cheap password hashing, ...).
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Self

from pydantic import Field, PostgresDsn, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_DEV_JWT_SECRET = "dev-only-insecure-jwt-secret-change-me-0000000000"  # noqa: S105
_MIN_PROD_SECRET_LENGTH = 32
# OWASP minimum recommendation for Argon2id is m=19 MiB, t=2, p=1.
_MIN_PROD_ARGON2_MEMORY_KIB = 19 * 1024
_MIN_PROD_ARGON2_TIME_COST = 2


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class LogFormat(StrEnum):
    JSON = "json"
    CONSOLE = "console"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Environment = Environment.DEVELOPMENT
    app_name: str = "inventory-api"
    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.JSON

    # --- database -------------------------------------------------------
    database_url: PostgresDsn = PostgresDsn(
        "postgresql+asyncpg://inventory:inventory@127.0.0.1:5432/inventory"
    )
    db_pool_size: int = Field(default=10, ge=1, le=100)
    db_max_overflow: int = Field(default=10, ge=0, le=100)
    db_pool_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    db_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    db_statement_timeout_ms: int = Field(default=15_000, ge=100, le=600_000)
    db_echo: bool = False

    # --- auth -----------------------------------------------------------
    jwt_secret: SecretStr = SecretStr(_DEV_JWT_SECRET)
    jwt_issuer: str = "inventory-api"
    jwt_audience: str = "inventory-api"
    access_token_ttl_seconds: int = Field(default=900, ge=60, le=86_400)
    refresh_token_ttl_seconds: int = Field(default=7 * 86_400, ge=3_600, le=90 * 86_400)

    argon2_time_cost: int = Field(default=3, ge=1, le=10)
    argon2_memory_cost_kib: int = Field(default=64 * 1024, ge=8, le=1024 * 1024)
    argon2_parallelism: int = Field(default=4, ge=1, le=16)
    # Each Argon2 hash allocates `argon2_memory_cost_kib`; cap concurrent hashes
    # per process so a login flood cannot exhaust memory.
    password_hash_concurrency: int = Field(default=4, ge=1, le=64)

    # --- rate limiting (fixed window, stored in PostgreSQL) ------------
    rate_limit_enabled: bool = True
    login_rate_limit_per_ip: int = Field(default=20, ge=1)
    login_rate_limit_per_email: int = Field(default=10, ge=1)
    login_rate_limit_window_seconds: int = Field(default=900, ge=1)
    refresh_rate_limit_per_ip: int = Field(default=60, ge=1)
    refresh_rate_limit_window_seconds: int = Field(default=60, ge=1)

    # --- HTTP -----------------------------------------------------------
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # None = environment default: enabled in development/test, disabled in production.
    expose_docs: bool | None = None
    enable_hsts: bool = False
    max_request_body_bytes: int = Field(default=1_048_576, ge=1_024, le=50 * 1_048_576)

    # --- retention ------------------------------------------------------
    idempotency_key_ttl_seconds: int = Field(default=86_400, ge=60)

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("log_level")
    @classmethod
    def _normalise_log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"invalid log level {value!r}")
        return level

    @field_validator("database_url")
    @classmethod
    def _require_asyncpg(cls, value: PostgresDsn) -> PostgresDsn:
        if value.scheme != "postgresql+asyncpg":
            raise ValueError("DATABASE_URL must use the 'postgresql+asyncpg://' scheme")
        return value

    @model_validator(mode="after")
    def _validate_for_environment(self) -> Self:
        if self.app_env is not Environment.PRODUCTION:
            return self
        problems: list[str] = []
        secret = self.jwt_secret.get_secret_value()
        if secret == _DEV_JWT_SECRET or len(secret) < _MIN_PROD_SECRET_LENGTH:
            problems.append(
                f"JWT_SECRET must be set to a random value of at least "
                f"{_MIN_PROD_SECRET_LENGTH} characters"
            )
        if "inventory:inventory@" in str(self.database_url):
            problems.append("DATABASE_URL still uses the development credentials")
        if self.argon2_memory_cost_kib < _MIN_PROD_ARGON2_MEMORY_KIB:
            problems.append(
                f"ARGON2_MEMORY_COST_KIB must be >= {_MIN_PROD_ARGON2_MEMORY_KIB} in production"
            )
        if self.argon2_time_cost < _MIN_PROD_ARGON2_TIME_COST:
            problems.append(
                f"ARGON2_TIME_COST must be >= {_MIN_PROD_ARGON2_TIME_COST} in production"
            )
        if not self.rate_limit_enabled:
            problems.append("RATE_LIMIT_ENABLED must not be disabled in production")
        if "*" in self.cors_allowed_origins:
            problems.append("CORS_ALLOWED_ORIGINS must not contain '*' in production")
        if self.db_echo:
            problems.append("DB_ECHO must be disabled in production (it logs query parameters)")
        if problems:
            raise ValueError("Insecure production configuration: " + "; ".join(problems))
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env is Environment.PRODUCTION

    @property
    def docs_enabled(self) -> bool:
        return self.expose_docs if self.expose_docs is not None else not self.is_production


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
