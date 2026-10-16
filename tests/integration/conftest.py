"""Integration fixtures: a real PostgreSQL database, migrated with Alembic.

The schema is built by running the actual migrations (downgrade to base, then
upgrade to head), so every test run also verifies the migrations. Tables are
truncated after each test, which keeps tests independent while still letting
them exercise real commits and real concurrency.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from sqlalchemy import text

from app.api.deps import AppState
from app.core.config import Settings
from app.main import create_app
from app.models.enums import Role
from app.models.user import User
from tests.conftest import ROOT, database_url_for_tests

PASSWORD = "correct-horse-battery-staple"

pytestmark = pytest.mark.integration


def alembic_config(database_url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.attributes["database_url"] = database_url
    config.attributes["configure_logger"] = False
    return config


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "integration" in str(item.path):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def migrated_database(settings: Settings) -> str:
    url = database_url_for_tests()
    if not url:
        pytest.fail(
            "TEST_DATABASE_URL is not set. Integration tests need a dedicated PostgreSQL "
            "database (see README > Testing). Run unit tests only with: pytest tests/unit"
        )
    if "test" not in url.rsplit("/", 1)[-1]:
        pytest.fail("Refusing to run: TEST_DATABASE_URL database name must contain 'test'.")
    config = alembic_config(url)
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    return url


@pytest.fixture(scope="session")
async def app(settings: Settings, migrated_database: str) -> AsyncIterator[FastAPI]:
    application = create_app(settings)
    yield application
    await application.state.engine.dispose()


@pytest.fixture(scope="session")
def state(app: FastAPI) -> AppState:
    container: AppState = app.state.container
    return container


@pytest.fixture(autouse=True)
async def _clean_database(app: FastAPI) -> AsyncIterator[None]:
    yield
    tables = [
        "stock_movements",
        "stock_levels",
        "products",
        "categories",
        "warehouses",
        "idempotency_keys",
        "audit_events",
        "refresh_tokens",
        "rate_limit_counters",
        "users",
    ]
    async with app.state.engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


class UserFactory:
    def __init__(self, state: AppState) -> None:
        self._state = state

    async def create(
        self,
        role: Role = Role.VIEWER,
        *,
        email: str | None = None,
        password: str = PASSWORD,
        is_active: bool = True,
    ) -> User:
        async with self._state.sessionmaker() as session:
            user = User(
                email=(email or f"{role.value}-{uuid.uuid4().hex[:8]}@example.com").lower(),
                full_name=f"Test {role.value}",
                role=role.value,
                password_hash=await self._state.hasher.hash(password),
                is_active=is_active,
                token_version=0,
            )
            session.add(user)
            await session.commit()
            return user

    def headers(self, user: User) -> dict[str, str]:
        token = self._state.tokens.create_access_token(user.id, user.token_version)
        return {"Authorization": f"Bearer {token}"}

    async def headers_for(self, role: Role) -> dict[str, str]:
        return self.headers(await self.create(role))


@pytest.fixture
def users(state: AppState) -> UserFactory:
    return UserFactory(state)


@pytest.fixture
async def admin_headers(users: UserFactory) -> dict[str, str]:
    return await users.headers_for(Role.ADMIN)


@pytest.fixture
async def manager_headers(users: UserFactory) -> dict[str, str]:
    return await users.headers_for(Role.MANAGER)


@pytest.fixture
async def operator_headers(users: UserFactory) -> dict[str, str]:
    return await users.headers_for(Role.OPERATOR)


@pytest.fixture
async def viewer_headers(users: UserFactory) -> dict[str, str]:
    return await users.headers_for(Role.VIEWER)


@dataclass
class Api:
    """Terse helpers for arranging test data through the public API."""

    client: httpx.AsyncClient
    manager: dict[str, str]
    operator: dict[str, str]

    async def warehouse(self, code: str | None = None, **extra: Any) -> dict[str, Any]:
        body = {"code": code or f"WH-{uuid.uuid4().hex[:6]}", "name": "Warehouse", **extra}
        response = await self.client.post("/api/v1/warehouses", json=body, headers=self.manager)
        assert response.status_code == 201, response.text
        return dict(response.json())

    async def product(self, sku: str | None = None, **extra: Any) -> dict[str, Any]:
        body = {"sku": sku or f"SKU-{uuid.uuid4().hex[:8]}", "name": "Product", **extra}
        response = await self.client.post("/api/v1/products", json=body, headers=self.manager)
        assert response.status_code == 201, response.text
        return dict(response.json())

    async def move(
        self,
        product: dict[str, Any],
        warehouse: dict[str, Any],
        movement_type: str,
        quantity: int,
        *,
        headers: dict[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> httpx.Response:
        request_headers = dict(
            headers or (self.manager if movement_type == "adjustment" else self.operator)
        )
        if idempotency_key:
            request_headers["Idempotency-Key"] = idempotency_key
        return await self.client.post(
            "/api/v1/stock-movements",
            json={
                "product_id": product["id"],
                "warehouse_id": warehouse["id"],
                "movement_type": movement_type,
                "quantity": quantity,
            },
            headers=request_headers,
        )

    async def receive(
        self, product: dict[str, Any], warehouse: dict[str, Any], quantity: int
    ) -> dict[str, Any]:
        response = await self.move(product, warehouse, "receipt", quantity)
        assert response.status_code == 201, response.text
        return dict(response.json())

    async def balance(self, product: dict[str, Any], warehouse: dict[str, Any]) -> int:
        response = await self.client.get(
            f"/api/v1/products/{product['id']}/stock", headers=self.operator
        )
        assert response.status_code == 200, response.text
        for entry in response.json()["warehouses"]:
            if entry["warehouse_id"] == warehouse["id"]:
                return int(entry["quantity"])
        return 0


@pytest.fixture
def api(
    client: httpx.AsyncClient, manager_headers: dict[str, str], operator_headers: dict[str, str]
) -> Api:
    return Api(client=client, manager=manager_headers, operator=operator_headers)
