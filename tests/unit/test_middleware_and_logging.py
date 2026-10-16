from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.core.context import request_id_var
from app.core.logging import JsonFormatter
from app.middleware.http import (
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)

LIMIT = 1024


async def echo(request: Request) -> JSONResponse:
    body = await request.body()
    return JSONResponse({"size": len(body), "request_id": request_id_var.get()})


async def boom(_: Request) -> JSONResponse:
    raise RuntimeError("database password is hunter2")


def build_app() -> Starlette:
    return Starlette(
        routes=[Route("/echo", echo, methods=["POST", "GET"]), Route("/boom", boom)],
        middleware=[
            Middleware(RequestContextMiddleware),
            Middleware(SecurityHeadersMiddleware, enable_hsts=True),
            Middleware(BodySizeLimitMiddleware, max_bytes=LIMIT),
        ],
    )


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def test_request_id_generated_and_propagated(client: httpx.AsyncClient) -> None:
    response = await client.get("/echo")
    request_id = response.headers["X-Request-ID"]
    assert len(request_id) == 32
    assert response.json()["request_id"] == request_id


async def test_valid_inbound_request_id_is_kept(client: httpx.AsyncClient) -> None:
    response = await client.get("/echo", headers={"X-Request-ID": "trace-abc.123"})
    assert response.headers["X-Request-ID"] == "trace-abc.123"


async def test_hostile_inbound_request_id_is_replaced(client: httpx.AsyncClient) -> None:
    response = await client.get("/echo", headers={"X-Request-ID": "a" * 200})
    assert response.headers["X-Request-ID"] != "a" * 200


async def test_security_headers(client: httpx.AsyncClient) -> None:
    headers = (await client.get("/echo")).headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Cache-Control"] == "no-store"
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert "max-age=" in headers["Strict-Transport-Security"]


async def test_body_within_limit_accepted(client: httpx.AsyncClient) -> None:
    response = await client.post("/echo", content=b"x" * LIMIT)
    assert response.status_code == 200
    assert response.json()["size"] == LIMIT


async def test_declared_oversized_body_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post("/echo", content=b"x" * (LIMIT + 1))
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert response.headers["X-Request-ID"]


async def test_streamed_oversized_body_rejected_without_content_length(
    client: httpx.AsyncClient,
) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(4):
            yield b"x" * 512

    response = await client.post("/echo", content=chunks())
    assert "content-length" not in response.request.headers
    assert response.status_code == 413


async def test_unhandled_exception_returns_generic_500(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR):
        response = await client.get("/boom")
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert body["error"]["request_id"] == response.headers["X-Request-ID"]
    assert "hunter2" not in response.text
    assert "Traceback" not in response.text
    assert any("unhandled exception" in record.message for record in caplog.records)


def test_json_formatter_includes_context_and_redacts_secrets() -> None:
    token = request_id_var.set("req-1")
    try:
        record = logging.LogRecord("app", logging.INFO, __file__, 1, "hello %s", ("world",), None)
        record.password = "hunter2"
        record.refresh_token = "abc"
        record.http_status = 200
        payload = json.loads(JsonFormatter().format(record))
    finally:
        request_id_var.reset(token)
    assert payload["message"] == "hello world"
    assert payload["request_id"] == "req-1"
    assert payload["http_status"] == 200
    assert payload["password"] == "[REDACTED]"
    assert payload["refresh_token"] == "[REDACTED]"
