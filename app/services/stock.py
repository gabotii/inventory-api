from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    ConflictError,
    InsufficientStockError,
    NotFoundError,
    ResourceInactiveError,
)
from app.core.pagination import decode_cursor, encode_cursor
from app.db.errors import FOREIGN_KEY_VIOLATION, sqlstate
from app.models.catalog import Product, Warehouse
from app.models.enums import MovementType
from app.models.stock import StockMovement
from app.repositories.catalog import ProductRepository, WarehouseRepository
from app.repositories.stock import StockRepository
from app.schemas.stock import (
    MovementCreate,
    MovementRead,
    ProductStockRead,
    StockLevelRead,
    TransferCreate,
    TransferRead,
    WarehouseStock,
)
from app.services.permissions import Actor, Permission, authorize
from app.services.support import IdempotentResult, execute_idempotent

logger = logging.getLogger(__name__)


class StockService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._stock = StockRepository(session)
        self._products = ProductRepository(session)
        self._warehouses = WarehouseRepository(session)

    # ================================================================ writes
    async def record_movement(
        self, actor: Actor, data: MovementCreate, *, idempotency_key: str | None
    ) -> IdempotentResult:
        permission = (
            Permission.ADJUST_STOCK
            if data.movement_type == "adjustment"
            else Permission.RECORD_MOVEMENT
        )
        authorize(actor, permission)

        async def operation() -> tuple[int, dict[str, Any]]:
            await self._active_product(data.product_id)
            await self._active_warehouse(data.warehouse_id)
            movement = await self._move(
                actor,
                product_id=data.product_id,
                warehouse_id=data.warehouse_id,
                movement_type=MovementType(data.movement_type),
                delta=data.signed_quantity,
                reference=data.reference,
                note=data.note,
                transfer_id=None,
            )
            return 201, MovementRead.model_validate(movement).model_dump(mode="json")

        result = await execute_idempotent(
            self._session,
            user_id=actor.id,
            key=idempotency_key,
            endpoint="stock-movements",
            payload=data.model_dump(mode="json"),
            operation=operation,
        )
        if not result.replayed:
            logger.info(
                "stock movement recorded",
                extra={
                    "movement_id": result.body["id"],
                    "movement_type": data.movement_type,
                    "quantity": data.signed_quantity,
                },
            )
        return result

    async def transfer(
        self, actor: Actor, data: TransferCreate, *, idempotency_key: str | None
    ) -> IdempotentResult:
        authorize(actor, Permission.RECORD_MOVEMENT)

        async def operation() -> tuple[int, dict[str, Any]]:
            await self._active_product(data.product_id)
            await self._active_warehouse(data.from_warehouse_id)
            await self._active_warehouse(data.to_warehouse_id)
            transfer_id = uuid.uuid4()
            legs = {
                data.from_warehouse_id: (MovementType.TRANSFER_OUT, -data.quantity),
                data.to_warehouse_id: (MovementType.TRANSFER_IN, data.quantity),
            }
            # Touch stock rows in a deterministic (warehouse id) order so that
            # opposite-direction transfers running concurrently cannot deadlock.
            movements: dict[MovementType, StockMovement] = {}
            for warehouse_id in sorted(legs):
                movement_type, delta = legs[warehouse_id]
                movements[movement_type] = await self._move(
                    actor,
                    product_id=data.product_id,
                    warehouse_id=warehouse_id,
                    movement_type=movement_type,
                    delta=delta,
                    reference=data.reference,
                    note=data.note,
                    transfer_id=transfer_id,
                )
            body = TransferRead(
                transfer_id=transfer_id,
                outbound=MovementRead.model_validate(movements[MovementType.TRANSFER_OUT]),
                inbound=MovementRead.model_validate(movements[MovementType.TRANSFER_IN]),
            )
            return 201, body.model_dump(mode="json")

        result = await execute_idempotent(
            self._session,
            user_id=actor.id,
            key=idempotency_key,
            endpoint="stock-transfers",
            payload=data.model_dump(mode="json"),
            operation=operation,
        )
        if not result.replayed:
            logger.info(
                "stock transfer recorded",
                extra={"transfer_id": result.body["transfer_id"], "quantity": data.quantity},
            )
        return result

    async def _move(
        self,
        actor: Actor,
        *,
        product_id: uuid.UUID,
        warehouse_id: uuid.UUID,
        movement_type: MovementType,
        delta: int,
        reference: str | None,
        note: str | None,
        transfer_id: uuid.UUID | None,
    ) -> StockMovement:
        outcome = await self._stock.apply_delta(product_id, warehouse_id, delta)
        if not outcome.applied:
            raise InsufficientStockError(
                details={
                    "product_id": str(product_id),
                    "warehouse_id": str(warehouse_id),
                    "available": outcome.balance,
                    "requested": -delta,
                }
            )
        movement = StockMovement(
            product_id=product_id,
            warehouse_id=warehouse_id,
            movement_type=movement_type.value,
            quantity=delta,
            balance_after=outcome.balance,
            reference=reference,
            note=note,
            transfer_id=transfer_id,
            created_by=actor.id,
        )
        try:
            return await self._stock.add_movement(movement)
        except IntegrityError as exc:
            if sqlstate(exc) == FOREIGN_KEY_VIOLATION:
                # Product/warehouse deleted concurrently after our check.
                raise ConflictError("A referenced resource no longer exists") from exc
            raise

    # ================================================================= reads
    async def get_movement(self, actor: Actor, movement_id: int) -> StockMovement:
        authorize(actor, Permission.READ_INVENTORY)
        movement = await self._stock.get_movement(movement_id)
        if movement is None:
            raise NotFoundError("Stock movement")
        return movement

    async def list_movements(
        self,
        actor: Actor,
        *,
        product_id: uuid.UUID | None,
        warehouse_id: uuid.UUID | None,
        movement_type: MovementType | None,
        created_from: datetime | None,
        created_to: datetime | None,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[StockMovement], str | None]:
        authorize(actor, Permission.READ_INVENTORY)
        before_id = decode_cursor(cursor) if cursor else None
        # Fetch one extra row to know whether another page exists.
        rows = await self._stock.list_movements(
            product_id=product_id,
            warehouse_id=warehouse_id,
            movement_type=movement_type.value if movement_type else None,
            created_from=created_from,
            created_to=created_to,
            before_id=before_id,
            limit=limit + 1,
        )
        next_cursor = encode_cursor(rows[limit - 1].id) if len(rows) > limit else None
        return rows[:limit], next_cursor

    async def list_levels(
        self,
        actor: Actor,
        *,
        product_id: uuid.UUID | None,
        warehouse_id: uuid.UUID | None,
        below_reorder: bool | None,
        limit: int,
        offset: int,
    ) -> tuple[list[StockLevelRead], int]:
        authorize(actor, Permission.READ_INVENTORY)
        rows, total = await self._stock.list_levels(
            product_id=product_id,
            warehouse_id=warehouse_id,
            below_reorder=below_reorder,
            limit=limit,
            offset=offset,
        )
        items = [
            StockLevelRead(
                product_id=row.product_id,
                sku=row.sku,
                product_name=row.product_name,
                warehouse_id=row.warehouse_id,
                warehouse_code=row.warehouse_code,
                quantity=row.quantity,
                product_total_quantity=int(row.product_total_quantity),
                reorder_level=row.reorder_level,
                below_reorder=row.product_total_quantity < row.reorder_level,
                updated_at=row.updated_at,
            )
            for row in rows
        ]
        return items, total

    async def product_stock(self, actor: Actor, product_id: uuid.UUID) -> ProductStockRead:
        authorize(actor, Permission.READ_INVENTORY)
        product = await self._products.get(product_id)
        if product is None:
            raise NotFoundError("Product")
        rows = await self._stock.levels_for_product(product_id)
        total = sum(int(row.quantity) for row in rows)
        return ProductStockRead(
            product_id=product.id,
            sku=product.sku,
            total_quantity=total,
            reorder_level=product.reorder_level,
            below_reorder=total < product.reorder_level,
            warehouses=[
                WarehouseStock(
                    warehouse_id=row.warehouse_id,
                    warehouse_code=row.warehouse_code,
                    quantity=row.quantity,
                )
                for row in rows
            ],
        )

    # =============================================================== helpers
    async def _active_product(self, product_id: uuid.UUID) -> Product:
        product = await self._products.get(product_id)
        if product is None:
            raise NotFoundError("Product")
        if not product.is_active:
            raise ResourceInactiveError("Product is archived", details={"resource": "Product"})
        return product

    async def _active_warehouse(self, warehouse_id: uuid.UUID) -> Warehouse:
        warehouse = await self._warehouses.get(warehouse_id)
        if warehouse is None:
            raise NotFoundError("Warehouse")
        if not warehouse.is_active:
            raise ResourceInactiveError("Warehouse is archived", details={"resource": "Warehouse"})
        return warehouse
