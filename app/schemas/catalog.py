from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, ClassVar

from pydantic import BeforeValidator, Field, StringConstraints

from app.models.catalog import SKU_PATTERN, WAREHOUSE_CODE_PATTERN
from app.schemas.common import (
    MAX_QUANTITY,
    ApiModel,
    Description,
    Name,
    PatchModel,
    ResponseModel,
    ShortName,
)


def _normalise(transform: Callable[[str], str]) -> BeforeValidator:
    """Strip + case-fold before validation (pydantic checks ``pattern`` on raw input)."""

    def _apply(value: object) -> object:
        return transform(value.strip()) if isinstance(value, str) else value

    return BeforeValidator(_apply)


# Normalised before the pattern check, mirroring the DB CHECK constraints.
Sku = Annotated[str, _normalise(str.upper), StringConstraints(pattern=SKU_PATTERN)]
WarehouseCode = Annotated[
    str, _normalise(str.upper), StringConstraints(pattern=WAREHOUSE_CODE_PATTERN)
]
Unit = Annotated[
    str,
    _normalise(str.lower),
    StringConstraints(min_length=1, max_length=16, pattern=r"^[a-z0-9_]+$"),
]
ReorderLevel = Annotated[int, Field(ge=0, le=MAX_QUANTITY)]


# ------------------------------------------------------------------ categories
class CategoryCreate(ApiModel):
    name: ShortName
    description: Description | None = None


class CategoryUpdate(PatchModel):
    non_nullable: ClassVar[frozenset[str]] = frozenset({"name"})

    name: ShortName | None = None
    description: Description | None = None


class CategoryRead(ResponseModel):
    id: uuid.UUID
    name: str
    description: str | None
    version: int
    created_at: datetime
    updated_at: datetime


# -------------------------------------------------------------------- products
class ProductCreate(ApiModel):
    sku: Sku
    name: Name
    description: Description | None = None
    category_id: uuid.UUID | None = None
    unit: Unit = "unit"
    reorder_level: ReorderLevel = 0


class ProductUpdate(PatchModel):
    """SKU is immutable: it is the identifier external systems hold on to."""

    non_nullable: ClassVar[frozenset[str]] = frozenset(
        {"name", "unit", "reorder_level", "is_active"}
    )

    name: Name | None = None
    description: Description | None = None
    category_id: uuid.UUID | None = None
    unit: Unit | None = None
    reorder_level: ReorderLevel | None = None
    is_active: bool | None = None


class ProductRead(ResponseModel):
    id: uuid.UUID
    sku: str
    name: str
    description: str | None
    category_id: uuid.UUID | None
    unit: str
    reorder_level: int
    is_active: bool
    version: int
    created_at: datetime
    updated_at: datetime


# ------------------------------------------------------------------ warehouses
class WarehouseCreate(ApiModel):
    code: WarehouseCode
    name: Name
    address: Description | None = None


class WarehouseUpdate(PatchModel):
    """Code is immutable for the same reason as product SKU."""

    non_nullable: ClassVar[frozenset[str]] = frozenset({"name", "is_active"})

    name: Name | None = None
    address: Description | None = None
    is_active: bool | None = None


class WarehouseRead(ResponseModel):
    id: uuid.UUID
    code: str
    name: str
    address: str | None
    is_active: bool
    version: int
    created_at: datetime
    updated_at: datetime
