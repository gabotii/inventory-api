"""Domain exceptions and the single error response format used by the API.

Services raise ``AppError`` subclasses; the HTTP layer converts them into::

    {"error": {"code": ..., "message": ..., "details": {...}, "request_id": ...}}
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    BAD_REQUEST = "BAD_REQUEST"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    INVALID_REFRESH_TOKEN = "INVALID_REFRESH_TOKEN"  # noqa: S105 - error code, not a secret
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    ALREADY_EXISTS = "ALREADY_EXISTS"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    INSUFFICIENT_STOCK = "INSUFFICIENT_STOCK"
    RESOURCE_IN_USE = "RESOURCE_IN_USE"
    RESOURCE_INACTIVE = "RESOURCE_INACTIVE"
    LAST_ADMIN = "LAST_ADMIN"
    CONFLICT = "CONFLICT"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    UNSUPPORTED_MEDIA_TYPE = "UNSUPPORTED_MEDIA_TYPE"
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"


class AppError(Exception):
    status_code: int = 500
    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    message: str = "Internal server error"

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message or type(self).message
        self.details = details or {}
        self.headers = headers
        super().__init__(self.message)


class BadRequestError(AppError):
    status_code = 400
    code = ErrorCode.BAD_REQUEST
    message = "Bad request"


class ValidationFailedError(AppError):
    """Business-level validation failure, reported like request validation errors."""

    status_code = 422
    code = ErrorCode.VALIDATION_ERROR
    message = "Request validation failed"

    def __init__(self, field: str, message: str) -> None:
        super().__init__(
            details={"errors": [{"field": field, "message": message, "type": "value_error"}]}
        )


class AuthenticationError(AppError):
    status_code = 401
    code = ErrorCode.UNAUTHENTICATED
    message = "Authentication required"

    def __init__(self, message: str | None = None, **kwargs: Any) -> None:
        kwargs.setdefault("headers", {"WWW-Authenticate": "Bearer"})
        super().__init__(message, **kwargs)


class InvalidCredentialsError(AuthenticationError):
    code = ErrorCode.INVALID_CREDENTIALS
    message = "Invalid email or password"


class InvalidRefreshTokenError(AuthenticationError):
    code = ErrorCode.INVALID_REFRESH_TOKEN
    message = "Invalid or expired refresh token"


class PermissionDeniedError(AppError):
    status_code = 403
    code = ErrorCode.FORBIDDEN
    message = "You do not have permission to perform this action"


class NotFoundError(AppError):
    status_code = 404
    code = ErrorCode.NOT_FOUND
    message = "Resource not found"

    def __init__(self, resource: str, **kwargs: Any) -> None:
        super().__init__(f"{resource} not found", details={"resource": resource}, **kwargs)


class ConflictError(AppError):
    status_code = 409
    code = ErrorCode.CONFLICT
    message = "The request conflicts with the current state of the resource"


class AlreadyExistsError(ConflictError):
    code = ErrorCode.ALREADY_EXISTS
    message = "Resource already exists"


class VersionConflictError(ConflictError):
    code = ErrorCode.VERSION_CONFLICT
    message = "The resource was modified by another request; reload it and retry"


class InsufficientStockError(ConflictError):
    code = ErrorCode.INSUFFICIENT_STOCK
    message = "Insufficient stock"


class ResourceInUseError(ConflictError):
    code = ErrorCode.RESOURCE_IN_USE
    message = "The resource is referenced by other records"


class ResourceInactiveError(ConflictError):
    code = ErrorCode.RESOURCE_INACTIVE
    message = "The resource is archived"


class LastAdminError(ConflictError):
    code = ErrorCode.LAST_ADMIN
    message = "At least one active admin must remain"


class IdempotencyKeyReusedError(AppError):
    status_code = 422
    code = ErrorCode.IDEMPOTENCY_KEY_REUSED
    message = "Idempotency-Key was already used with a different request"


class RateLimitedError(AppError):
    status_code = 429
    code = ErrorCode.RATE_LIMITED
    message = "Too many requests; retry later"

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(
            details={"retry_after_seconds": retry_after_seconds},
            headers={"Retry-After": str(retry_after_seconds)},
        )


class ServiceUnavailableError(AppError):
    status_code = 503
    code = ErrorCode.SERVICE_UNAVAILABLE
    message = "Service temporarily unavailable"
