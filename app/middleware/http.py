"""Pure-ASGI middleware (no BaseHTTPMiddleware: it breaks contextvars and streaming)."""

from __future__ import annotations

import contextlib
import json
import logging
import time
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.context import request_id_var, sanitize_request_id, user_id_var
from app.core.errors import ErrorCode

logger = logging.getLogger("app.access")

_QUIET_PATHS = frozenset({"/health/live", "/health/ready"})
_DOCS_PREFIXES = ("/docs", "/redoc", "/openapi.json")


async def send_error(
    send: Send,
    status: int,
    code: ErrorCode,
    message: str,
    *,
    request_id: str | None = None,
) -> None:
    body = json.dumps(
        {"error": {"code": code.value, "message": message, "details": {}, "request_id": request_id}}
    ).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class RequestContextMiddleware:
    """Assigns a request ID, emits a structured access log, and is the last line
    of defence for unhandled exceptions (generic 500, never a stack trace)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        raw_id = headers.get(b"x-request-id")
        request_id = sanitize_request_id(raw_id.decode("latin-1") if raw_id else None)
        rid_token = request_id_var.set(request_id)
        uid_token = user_id_var.set(None)
        started = time.perf_counter()
        status_holder: dict[str, Any] = {"status": 500, "started": False}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                status_holder["started"] = True
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            logger.exception("unhandled exception")
            status_holder["status"] = 500
            if not status_holder["started"]:
                await send_error(
                    send_wrapper,
                    500,
                    ErrorCode.INTERNAL_ERROR,
                    "Internal server error",
                    request_id=request_id,
                )
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            status = int(status_holder["status"])
            path = scope.get("path", "")
            level = logging.INFO
            if status >= 500:
                level = logging.ERROR
            elif path in _QUIET_PATHS and status < 400:
                level = logging.DEBUG
            client = scope.get("client")
            logger.log(
                level,
                "request completed",
                extra={
                    "http_method": scope.get("method"),
                    "http_path": path,
                    "http_status": status,
                    "duration_ms": duration_ms,
                    "client_ip": client[0] if client else None,
                },
            )
            user_id_var.reset(uid_token)
            request_id_var.reset(rid_token)


class _PayloadTooLargeError(Exception):
    pass


class BodySizeLimitMiddleware:
    """Rejects request bodies above ``max_bytes`` with 413.

    Checks Content-Length up front and also counts streamed (chunked) bytes,
    so a client cannot bypass the limit by omitting the header.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                too_large = int(declared) > self.max_bytes
            except ValueError:
                too_large = True
            if too_large:
                await self._reject(send)
                return

        received = 0
        exceeded = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    exceeded = True
                    raise _PayloadTooLargeError
            return message

        async def guarded_send(message: Message) -> None:
            # Frameworks may turn the receive error into their own 400; once the
            # limit is exceeded we drop whatever they send and answer 413.
            if not exceeded:
                await send(message)

        with contextlib.suppress(_PayloadTooLargeError):
            await self.app(scope, limited_receive, guarded_send)
        if exceeded:
            await self._reject(send)

    async def _reject(self, send: Send) -> None:
        await send_error(
            send,
            413,
            ErrorCode.PAYLOAD_TOO_LARGE,
            f"Request body exceeds {self.max_bytes} bytes",
            request_id=request_id_var.get(),
        )


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, *, enable_hsts: bool) -> None:
        self.app = app
        self.enable_hsts = enable_hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        is_docs = scope.get("path", "").startswith(_DOCS_PREFIXES)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy", "no-referrer")
                headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
                if not is_docs:
                    # API responses are data: never cache them, never render them.
                    headers.setdefault("Cache-Control", "no-store")
                    headers.setdefault(
                        "Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"
                    )
                if self.enable_hsts:
                    headers.setdefault(
                        "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
                    )
            await send(message)

        await self.app(scope, receive, send_wrapper)
