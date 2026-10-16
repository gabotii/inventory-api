"""Exception → HTTP response mapping. Every error uses the same envelope."""

from __future__ import annotations

import logging
import socket
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import exc as sa_exc
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.context import get_request_id
from app.core.errors import AppError, ErrorCode
from app.db.errors import constraint_name, sqlstate

logger = logging.getLogger(__name__)

# SQLSTATEs that mean "try again later", not "your request is wrong".
_RETRYABLE_SQLSTATES = frozenset(
    {
        "40001",  # serialization_failure
        "40P01",  # deadlock_detected
        "55P03",  # lock_not_available (lock_timeout)
        "57014",  # query_canceled (statement_timeout)
        "57P01",  # admin_shutdown
        "57P03",  # cannot_connect_now
        "53300",  # too_many_connections
    }
)

# Connection refused/reset, DNS failure, connect timeout.
_CONNECTION_ERRORS: tuple[type[Exception], ...] = (ConnectionError, socket.gaierror, TimeoutError)

_HTTP_CODES = {
    400: ErrorCode.BAD_REQUEST,
    401: ErrorCode.UNAUTHENTICATED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    413: ErrorCode.PAYLOAD_TOO_LARGE,
    415: ErrorCode.UNSUPPORTED_MEDIA_TYPE,
    422: ErrorCode.VALIDATION_ERROR,
    429: ErrorCode.RATE_LIMITED,
    503: ErrorCode.SERVICE_UNAVAILABLE,
}


def error_response(
    status: int,
    code: ErrorCode | str,
    message: str,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": str(code),
                "message": message,
                "details": details or {},
                "request_id": get_request_id(),
            }
        },
        headers=headers,
    )


async def _app_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)  # noqa: S101 - registered for AppError only
    if exc.status_code >= 500:
        logger.error("application error", extra={"error_code": exc.code.value})
    return error_response(exc.status_code, exc.code, exc.message, exc.details, exc.headers)


async def _validation_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)  # noqa: S101
    # Never echo the rejected input back: it may contain passwords or tokens.
    errors = [
        {
            "field": ".".join(str(part) for part in error.get("loc", ())),
            "message": error.get("msg", "Invalid value"),
            "type": error.get("type", "value_error"),
        }
        for error in exc.errors()
    ]
    return error_response(
        422, ErrorCode.VALIDATION_ERROR, "Request validation failed", {"errors": errors}
    )


async def _http_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101
    code = _HTTP_CODES.get(exc.status_code, ErrorCode.BAD_REQUEST)
    try:
        default_message = HTTPStatus(exc.status_code).phrase
    except ValueError:
        default_message = "HTTP error"
    message = exc.detail if isinstance(exc.detail, str) else default_message
    return error_response(exc.status_code, code, message, headers=getattr(exc, "headers", None))


async def _database_error(_: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, sa_exc.IntegrityError):
        # Expected integrity errors are mapped in services; reaching this point
        # means a race we did not anticipate. The constraint name is safe to log.
        logger.warning("unmapped integrity error", extra={"constraint": constraint_name(exc)})
        return error_response(409, ErrorCode.CONFLICT, "The request conflicts with existing data")

    state = sqlstate(exc) if isinstance(exc, sa_exc.DBAPIError) else None
    unavailable = (
        isinstance(exc, (sa_exc.OperationalError, sa_exc.InterfaceError, sa_exc.TimeoutError))
        or (isinstance(exc, sa_exc.DBAPIError) and exc.connection_invalidated)
        or state in _RETRYABLE_SQLSTATES
        or isinstance(exc, _CONNECTION_ERRORS)
    )
    if unavailable:
        logger.warning(
            "database unavailable",
            extra={"error_type": type(exc).__name__, "sqlstate": state},
        )
        return error_response(
            503,
            ErrorCode.SERVICE_UNAVAILABLE,
            "Service temporarily unavailable",
            headers={"Retry-After": "1"},
        )
    logger.error("database error", exc_info=exc, extra={"sqlstate": state})
    return error_response(500, ErrorCode.INTERNAL_ERROR, "Internal server error")


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(sa_exc.SQLAlchemyError, _database_error)
    # asyncpg raises these unwrapped when it cannot open a connection at all.
    for exc_type in _CONNECTION_ERRORS:
        app.add_exception_handler(exc_type, _database_error)
