"""Per-request context propagated through contextvars (safe across awaits)."""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)

# Accept caller-supplied request IDs only when they are short and harmless, so
# they cannot be used for log injection or to bloat log lines.
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")


def new_request_id() -> str:
    return uuid.uuid4().hex


def sanitize_request_id(candidate: str | None) -> str:
    if candidate and _REQUEST_ID_PATTERN.fullmatch(candidate):
        return candidate
    return new_request_id()


def get_request_id() -> str | None:
    return request_id_var.get()
