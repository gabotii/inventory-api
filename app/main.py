from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from alembic.script import ScriptDirectory
from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware import Middleware

from app.api.deps import AppState
from app.api.errors import register_error_handlers
from app.api.routes import auth, catalog, health, stock, users
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.core.security import PasswordHasher, TokenService
from app.db.session import create_engine, create_sessionmaker
from app.middleware.http import (
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from app.services.support import RateLimiter

logger = logging.getLogger(__name__)

_ALEMBIC_DIR = Path(__file__).resolve().parent.parent / "alembic"
API_PREFIX = "/api/v1"


def _schema_revisions() -> tuple[str | None, frozenset[str]]:
    """This build's migration head and every revision it knows about."""
    try:
        scripts = ScriptDirectory(str(_ALEMBIC_DIR))
        known = frozenset(rev.revision for rev in scripts.walk_revisions())
        return scripts.get_current_head(), known
    except Exception:
        logger.warning("alembic scripts not found; readiness will not verify schema revision")
        return None, frozenset()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    engine = create_engine(settings)
    sessionmaker = create_sessionmaker(engine)
    head_revision, known_revisions = _schema_revisions()
    container = AppState(
        settings=settings,
        sessionmaker=sessionmaker,
        hasher=PasswordHasher(settings),
        tokens=TokenService(settings),
        rate_limiter=RateLimiter(sessionmaker, enabled=settings.rate_limit_enabled),
        expected_schema_revision=head_revision,
        known_schema_revisions=known_revisions,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "application starting",
            extra={
                "environment": settings.app_env.value,
                "schema": container.expected_schema_revision,
            },
        )
        yield
        # uvicorn stops accepting connections and drains in-flight requests
        # before this runs; then we close pooled DB connections cleanly.
        await engine.dispose()
        logger.info("application stopped")

    middleware = [
        Middleware(RequestContextMiddleware),
        Middleware(SecurityHeadersMiddleware, enable_hsts=settings.enable_hsts),
        Middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes),
    ]
    if settings.cors_allowed_origins:
        middleware.append(
            Middleware(
                CORSMiddleware,
                allow_origins=settings.cors_allowed_origins,
                allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
                allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
                expose_headers=["X-Request-ID", "Idempotent-Replayed", "Retry-After"],
                allow_credentials=False,  # bearer tokens, no cookies
                max_age=600,
            )
        )

    docs = settings.docs_enabled
    app = FastAPI(
        title="Inventory API",
        version="1.0.0",
        description="Multi-warehouse inventory management. See README for authentication.",
        lifespan=lifespan,
        middleware=middleware,
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.container = container
    app.state.engine = engine
    register_error_handlers(app)

    api = APIRouter(prefix=API_PREFIX)
    api.include_router(auth.router)
    api.include_router(users.router)
    api.include_router(catalog.categories)
    api.include_router(catalog.products)
    api.include_router(catalog.warehouses)
    api.include_router(stock.router)
    api.include_router(stock.audit)
    app.include_router(health.router)
    app.include_router(api)
    return app


def app_factory() -> FastAPI:
    """Entry point for ``uvicorn --factory app.main:app_factory``."""
    return create_app()
