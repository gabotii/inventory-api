from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from app.api.deps import (
    ActorDep,
    AuditServiceDep,
    CursorQuery,
    IdempotencyKeyHeader,
    LimitQuery,
    PageDep,
    StockServiceDep,
)
from app.models.enums import MovementType
from app.schemas.common import DEFAULT_PAGE_SIZE, CursorPage, ErrorResponse, Page
from app.schemas.stock import (
    AuditEventRead,
    MovementCreate,
    MovementRead,
    StockLevelRead,
    TransferCreate,
    TransferRead,
)
from app.services.support import IdempotentResult

_COMMON: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
}
_WRITE: dict[int | str, dict[str, object]] = {
    404: {"model": ErrorResponse, "description": "Product or warehouse not found"},
    409: {"model": ErrorResponse, "description": "Insufficient stock or archived resource"},
    422: {"model": ErrorResponse},
}

router = APIRouter(tags=["stock"], responses=_COMMON)
audit = APIRouter(prefix="/audit-events", tags=["audit"], responses=_COMMON)


def _idempotent_response(result: IdempotentResult) -> JSONResponse:
    headers = {"Idempotent-Replayed": "true"} if result.replayed else None
    return JSONResponse(status_code=result.status_code, content=result.body, headers=headers)


@router.post(
    "/stock-movements",
    response_model=MovementRead,
    status_code=status.HTTP_201_CREATED,
    responses=_WRITE,
)
async def create_movement(
    body: MovementCreate,
    actor: ActorDep,
    stock: StockServiceDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    """Record a receipt, issue or adjustment.

    * ``receipt`` / ``issue``: ``quantity`` > 0 (the API applies the sign).
    * ``adjustment``: signed, non-zero ``quantity``; requires role *manager*.

    Send an ``Idempotency-Key`` header to make retries safe.
    """
    result = await stock.record_movement(actor, body, idempotency_key=idempotency_key)
    return _idempotent_response(result)


@router.post(
    "/stock-transfers",
    response_model=TransferRead,
    status_code=status.HTTP_201_CREATED,
    responses=_WRITE,
)
async def create_transfer(
    body: TransferCreate,
    actor: ActorDep,
    stock: StockServiceDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    """Move stock between warehouses atomically (both legs or neither)."""
    result = await stock.transfer(actor, body, idempotency_key=idempotency_key)
    return _idempotent_response(result)


@router.get("/stock-movements", response_model=CursorPage[MovementRead])
async def list_movements(
    actor: ActorDep,
    stock: StockServiceDep,
    product_id: uuid.UUID | None = None,
    warehouse_id: uuid.UUID | None = None,
    movement_type: MovementType | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    cursor: CursorQuery = None,
    limit: LimitQuery = DEFAULT_PAGE_SIZE,
) -> CursorPage[MovementRead]:
    """Newest first. Pass ``next_cursor`` from the previous page to continue."""
    items, next_cursor = await stock.list_movements(
        actor,
        product_id=product_id,
        warehouse_id=warehouse_id,
        movement_type=movement_type,
        created_from=created_from,
        created_to=created_to,
        cursor=cursor,
        limit=limit,
    )
    return CursorPage(
        items=[MovementRead.model_validate(m) for m in items],
        next_cursor=next_cursor,
        limit=limit,
    )


@router.get(
    "/stock-movements/{movement_id}",
    response_model=MovementRead,
    responses={404: {"model": ErrorResponse}},
)
async def get_movement(movement_id: int, actor: ActorDep, stock: StockServiceDep) -> MovementRead:
    return MovementRead.model_validate(await stock.get_movement(actor, movement_id))


@router.get("/stock-levels", response_model=Page[StockLevelRead])
async def list_stock_levels(
    actor: ActorDep,
    stock: StockServiceDep,
    page: PageDep,
    product_id: uuid.UUID | None = None,
    warehouse_id: uuid.UUID | None = None,
    below_reorder: bool | None = None,
) -> Page[StockLevelRead]:
    items, total = await stock.list_levels(
        actor,
        product_id=product_id,
        warehouse_id=warehouse_id,
        below_reorder=below_reorder,
        limit=page.limit,
        offset=page.offset,
    )
    return Page(items=items, total=total, limit=page.limit, offset=page.offset)


@audit.get("", response_model=CursorPage[AuditEventRead])
async def list_audit_events(
    actor: ActorDep,
    audit_service: AuditServiceDep,
    actor_id: uuid.UUID | None = None,
    action: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    cursor: CursorQuery = None,
    limit: LimitQuery = DEFAULT_PAGE_SIZE,
) -> CursorPage[AuditEventRead]:
    items, next_cursor = await audit_service.list_events(
        actor,
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        cursor=cursor,
        limit=limit,
    )
    return CursorPage(
        items=[AuditEventRead.model_validate(e) for e in items],
        next_cursor=next_cursor,
        limit=limit,
    )
