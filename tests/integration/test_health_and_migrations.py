from __future__ import annotations

import asyncio

import httpx
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine

import app.models  # noqa: F401
from app.db.base import Base
from app.main import create_app
from tests.conftest import make_settings
from tests.integration.conftest import alembic_config


async def test_liveness(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_checks_database_and_schema(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"database": "ok", "schema": "ok"}}


async def _ready_with(app: FastAPI, expected: str, known: frozenset[str]) -> httpx.Response:
    container = app.state.container
    original = (container.expected_schema_revision, container.known_schema_revisions)
    container.expected_schema_revision, container.known_schema_revisions = expected, known
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            return await http.get("/health/ready")
    finally:
        container.expected_schema_revision, container.known_schema_revisions = original


async def test_readiness_fails_when_migrations_are_pending(app: FastAPI) -> None:
    # This build expects 0002, the database is still at 0001 (known, older).
    response = await _ready_with(app, "0002", frozenset({"0001", "0002"}))
    assert response.status_code == 503
    assert response.json()["checks"]["schema"] == {
        "status": "behind",
        "current": "0001",
        "expected": "0002",
    }


async def test_readiness_tolerates_newer_schema_during_rolling_deploy(app: FastAPI) -> None:
    # An older build (head 0000) sees revision 0001 it does not know: the new
    # release already migrated the database. Old pods must keep serving.
    response = await _ready_with(app, "0000", frozenset({"0000"}))
    assert response.status_code == 200
    assert response.json()["checks"]["schema"]["status"] == "ahead"


async def test_database_outage_yields_503_not_500() -> None:
    # Port 1 on localhost: connection refused immediately.
    broken = create_app(
        make_settings(
            database_url="postgresql+asyncpg://u:p@127.0.0.1:1/inventory_test",
            db_connect_timeout_seconds=2,
        )
    )
    transport = httpx.ASGITransport(app=broken)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            ready = await http.get("/health/ready")
            live = await http.get("/health/live")
            login = await http.post(
                "/api/v1/auth/login", json={"email": "a@example.com", "password": "x" * 12}
            )
    finally:
        await broken.state.engine.dispose()
    assert ready.status_code == 503
    assert ready.json()["checks"]["database"] == "unavailable"
    assert live.status_code == 200  # liveness must not depend on the database
    assert login.status_code == 503
    assert login.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert login.headers["Retry-After"] == "1"
    assert "127.0.0.1" not in login.text


def _schema_diff(url: str) -> list[object]:
    async def run() -> list[object]:
        engine = create_async_engine(url)
        async with engine.connect() as conn:
            diff = await conn.run_sync(
                lambda sync_conn: compare_metadata(
                    MigrationContext.configure(sync_conn), Base.metadata
                )
            )
        await engine.dispose()
        return list(diff)

    return asyncio.run(run())


async def test_migrations_roundtrip_and_match_models(app: FastAPI, migrated_database: str) -> None:
    """downgrade→upgrade works and the migrated schema equals the ORM models."""
    await app.state.engine.dispose()  # release pooled connections before DDL
    config = alembic_config(migrated_database)
    # Alembic's env.py calls asyncio.run(), so it must run outside this loop.
    await asyncio.to_thread(command.downgrade, config, "base")
    await asyncio.to_thread(command.upgrade, config, "head")
    assert await asyncio.to_thread(_schema_diff, migrated_database) == []
