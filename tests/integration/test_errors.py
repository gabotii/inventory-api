from __future__ import annotations

import httpx
import pytest

from app.services.catalog import CatalogService


def assert_error_envelope(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/json")
    error = response.json()["error"]
    assert error["code"] == code
    assert isinstance(error["message"], str)
    assert isinstance(error["details"], dict)
    assert error["request_id"] == response.headers["X-Request-ID"]


async def test_unknown_route_and_method(client: httpx.AsyncClient) -> None:
    assert_error_envelope(await client.get("/api/v1/nothing-here"), 404, "NOT_FOUND")
    assert_error_envelope(await client.put("/health/live"), 405, "METHOD_NOT_ALLOWED")


async def test_malformed_json_is_validation_error(
    client: httpx.AsyncClient, manager_headers: dict[str, str]
) -> None:
    response = await client.post(
        "/api/v1/products",
        content=b'{"sku": "A", "name": ',
        headers={**manager_headers, "content-type": "application/json"},
    )
    assert_error_envelope(response, 422, "VALIDATION_ERROR")


async def test_oversized_body_rejected_before_parsing(
    client: httpx.AsyncClient, manager_headers: dict[str, str]
) -> None:
    response = await client.post(
        "/api/v1/products",
        content=b'{"sku": "A", "name": "' + b"x" * 2_000_000 + b'"}',
        headers={**manager_headers, "content-type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


async def test_unexpected_exception_returns_generic_500(
    client: httpx.AsyncClient, manager_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def explode(*_: object, **__: object) -> None:
        raise RuntimeError("connection string postgresql://admin:hunter2@db")

    monkeypatch.setattr(CatalogService, "list_products", explode)
    response = await client.get("/api/v1/products", headers=manager_headers)
    assert_error_envelope(response, 500, "INTERNAL_ERROR")
    assert "hunter2" not in response.text
    assert "RuntimeError" not in response.text


async def test_docs_are_served_with_security_headers(client: httpx.AsyncClient) -> None:
    schema = await client.get("/openapi.json")
    assert schema.status_code == 200
    paths = schema.json()["paths"]
    assert "/api/v1/stock-movements" in paths
    assert "/api/v1/auth/login" in paths
    assert schema.headers["X-Content-Type-Options"] == "nosniff"


async def test_audit_events_cursor_pagination(
    client: httpx.AsyncClient, admin_headers: dict[str, str], manager_headers: dict[str, str]
) -> None:
    for index in range(5):
        await client.post("/api/v1/categories", json={"name": f"C{index}"}, headers=manager_headers)
    first = (
        await client.get("/api/v1/audit-events", params={"limit": 3}, headers=admin_headers)
    ).json()
    assert len(first["items"]) == 3
    second = (
        await client.get(
            "/api/v1/audit-events",
            params={"limit": 3, "cursor": first["next_cursor"]},
            headers=admin_headers,
        )
    ).json()
    assert len(second["items"]) == 2
    assert second["next_cursor"] is None
    names = [e["details"]["name"] for e in first["items"] + second["items"]]
    assert names == ["C4", "C3", "C2", "C1", "C0"]
    assert all(e["request_id"] for e in first["items"])
