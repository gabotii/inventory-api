from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app import cli
from app.api.deps import AppState
from app.core.config import get_settings
from app.models.support import IdempotencyKey, RateLimitCounter
from app.models.user import RefreshToken, User
from tests.integration.conftest import UserFactory


@pytest.fixture
def cli_env(migrated_database: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("DATABASE_URL", migrated_database)
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def run_cli(*args: str) -> int:
    # The CLI calls asyncio.run(), so it needs its own thread (no running loop).
    return await asyncio.to_thread(cli.main, list(args))


@pytest.mark.usefixtures("cli_env")
async def test_create_admin_bootstraps_a_working_admin(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "bootstrap-password-1")
    assert await run_cli("create-admin", "--email", "Root@Example.com", "--full-name", "Root") == 0
    assert "Created admin root@example.com" in capsys.readouterr().out

    login = await client.post(
        "/api/v1/auth/login", json={"email": "root@example.com", "password": "bootstrap-password-1"}
    )
    assert login.status_code == 200
    me = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {login.json()['access_token']}"}
    )
    assert me.json()["role"] == "admin"

    again = await run_cli("create-admin", "--email", "root@example.com", "--full-name", "Root")
    assert again == cli.EXIT_CONFLICT


@pytest.mark.usefixtures("cli_env")
async def test_create_admin_enforces_password_policy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], state: AppState
) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "short")
    assert await run_cli("create-admin", "--email", "a@example.com", "--full-name", "A") == 2
    assert "password" in capsys.readouterr().err
    async with state.sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(User)) == 0


@pytest.mark.usefixtures("cli_env")
async def test_purge_expired_only_removes_stale_rows(
    state: AppState, users: UserFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    user = await users.create()
    now = datetime.now(UTC)
    async with state.sessionmaker() as session:
        session.add_all(
            [
                RefreshToken(
                    user_id=user.id,
                    family_id=user.id,
                    token_hash="a" * 64,
                    created_at=now - timedelta(days=30),
                    expires_at=now - timedelta(days=2),
                ),
                RefreshToken(
                    user_id=user.id,
                    family_id=user.id,
                    token_hash="b" * 64,
                    created_at=now,
                    expires_at=now + timedelta(days=7),
                ),
                IdempotencyKey(
                    user_id=user.id,
                    key="old",
                    endpoint="stock-movements",
                    request_hash="c" * 64,
                    created_at=now - timedelta(days=3),
                ),
                IdempotencyKey(
                    user_id=user.id, key="new", endpoint="stock-movements", request_hash="d" * 64
                ),
                RateLimitCounter(key="login:ip:x", window_start=now - timedelta(days=2), count=3),
            ]
        )
        await session.commit()

    assert await run_cli("purge-expired") == 0
    assert '"refresh_tokens": 1' in capsys.readouterr().out
    async with state.sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(RefreshToken)) == 1
        assert await session.scalar(select(IdempotencyKey.key)) == "new"
        assert await session.scalar(select(func.count()).select_from(RateLimitCounter)) == 0
