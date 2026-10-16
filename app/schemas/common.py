from __future__ import annotations

from typing import Annotated, Any, ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 50
# Upper bound for any single quantity: far below BIGINT limits, so running
# balances cannot overflow, and large enough for any realistic movement.
MAX_QUANTITY = 1_000_000_000


Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
ShortName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]
Reference = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Note = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class ApiModel(BaseModel):
    """Request models: unknown fields are rejected instead of silently ignored."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ResponseModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class PatchModel(ApiModel):
    """Partial update with optimistic locking.

    Only fields present in the request are applied. ``null`` is accepted only
    for columns that are nullable; fields listed in ``non_nullable`` reject it.
    """

    non_nullable: ClassVar[frozenset[str]] = frozenset()

    version: int = Field(ge=1, description="Version last read by the client (optimistic lock).")

    @model_validator(mode="after")
    def _reject_nulls(self) -> Self:
        for field in self.model_fields_set & self.non_nullable:
            if getattr(self, field) is None:
                raise ValueError(f"'{field}' cannot be null")
        return self

    def changes(self) -> dict[str, Any]:
        return {
            field: getattr(self, field) for field in self.model_fields_set if field != "version"
        }


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int


class CursorPage[T](BaseModel):
    items: list[T]
    next_cursor: str | None
    limit: int


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
