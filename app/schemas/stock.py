from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, model_validator

from app.models.enums import MovementType
from app.schemas.common import MAX_QUANTITY, ApiModel, Note, Reference, ResponseModel

DirectMovementType = Literal["receipt", "issue", "adjustment"]


class MovementCreate(ApiModel):
    """A single-warehouse stock movement.

    ``quantity`` is always expressed from the user's point of view:
    positive for ``receipt`` and ``issue`` (the API applies the sign), and a
    signed, non-zero delta for ``adjustment`` (e.g. -3 after a stock count).
    """

    product_id: uuid.UUID
    warehouse_id: uuid.UUID
    movement_type: DirectMovementType
    quantity: int = Field(ge=-MAX_QUANTITY, le=MAX_QUANTITY)
    reference: Reference | None = None
    note: Note | None = None

    @model_validator(mode="after")
    def _check_quantity(self) -> Self:
        if self.movement_type == "adjustment":
            if self.quantity == 0:
                raise ValueError("adjustment quantity must be non-zero")
        elif self.quantity <= 0:
            raise ValueError(f"{self.movement_type} quantity must be positive")
        return self

    @property
    def signed_quantity(self) -> int:
        return -self.quantity if self.movement_type == "issue" else self.quantity


class TransferCreate(ApiModel):
    product_id: uuid.UUID
    from_warehouse_id: uuid.UUID
    to_warehouse_id: uuid.UUID
    quantity: int = Field(gt=0, le=MAX_QUANTITY)
    reference: Reference | None = None
    note: Note | None = None

    @model_validator(mode="after")
    def _distinct_warehouses(self) -> Self:
        if self.from_warehouse_id == self.to_warehouse_id:
            raise ValueError("from_warehouse_id and to_warehouse_id must differ")
        return self


class MovementRead(ResponseModel):
    id: int
    product_id: uuid.UUID
    warehouse_id: uuid.UUID
    movement_type: MovementType
    quantity: int
    balance_after: int
    reference: str | None
    note: str | None
    transfer_id: uuid.UUID | None
    created_by: uuid.UUID
    created_at: datetime


class TransferRead(BaseModel):
    transfer_id: uuid.UUID
    outbound: MovementRead
    inbound: MovementRead


class StockLevelRead(BaseModel):
    product_id: uuid.UUID
    sku: str
    product_name: str
    warehouse_id: uuid.UUID
    warehouse_code: str
    quantity: int = Field(description="On hand in this warehouse.")
    product_total_quantity: int = Field(description="On hand across all warehouses.")
    reorder_level: int
    below_reorder: bool = Field(
        description="Product-level: product_total_quantity < reorder_level."
    )
    updated_at: datetime


class WarehouseStock(BaseModel):
    warehouse_id: uuid.UUID
    warehouse_code: str
    quantity: int


class ProductStockRead(BaseModel):
    product_id: uuid.UUID
    sku: str
    total_quantity: int
    reorder_level: int
    below_reorder: bool
    warehouses: list[WarehouseStock]


class AuditEventRead(ResponseModel):
    id: int
    occurred_at: datetime
    actor_id: uuid.UUID | None
    action: str
    entity_type: str
    entity_id: str | None
    request_id: str | None
    ip_address: str | None
    details: dict[str, Any]
