"""Helpers to interpret database errors without leaking them to clients."""

from __future__ import annotations

from sqlalchemy.exc import DBAPIError

UNIQUE_VIOLATION = "23505"
FOREIGN_KEY_VIOLATION = "23503"
CHECK_VIOLATION = "23514"


def _walk(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or getattr(current, "orig", None)
    return chain


def sqlstate(exc: DBAPIError) -> str | None:
    for item in _walk(exc):
        code = getattr(item, "sqlstate", None) or getattr(item, "pgcode", None)
        if isinstance(code, str):
            return code
    return None


def constraint_name(exc: DBAPIError) -> str | None:
    for item in _walk(exc):
        name = getattr(item, "constraint_name", None)
        if isinstance(name, str):
            return name
    return None
