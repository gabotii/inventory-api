from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Row, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Product, Warehouse
from app.models.stock import StockLevel, StockMovement
from app.repositories.base import count


@dataclass(frozen=True, slots=True)
class DeltaResult:
    applied: bool
    balance: int  # new balance if applied, otherwise the (insufficient) current balance


class StockRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def apply_delta(
        self, product_id: uuid.UUID, warehouse_id: uuid.UUID, delta: int
    ) -> DeltaResult:
        """Atomically add ``delta`` to a stock level.

        A single conditional statement both checks and updates the balance, so
        concurrent movements can never drive stock negative or lose updates
        (PostgreSQL re-evaluates the WHERE clause after waiting for a row lock).
        """
        if delta > 0:
            stmt = (
                insert(StockLevel)
                .values(product_id=product_id, warehouse_id=warehouse_id, quantity=delta)
                .on_conflict_do_update(
                    index_elements=[StockLevel.product_id, StockLevel.warehouse_id],
                    set_={
                        "quantity": StockLevel.quantity + delta,
                        "updated_at": func.now(),
                    },
                )
                .returning(StockLevel.quantity)
            )
            balance = (await self.session.execute(stmt)).scalar_one()
            return DeltaResult(applied=True, balance=int(balance))

        stmt_update = (
            update(StockLevel)
            .where(
                StockLevel.product_id == product_id,
                StockLevel.warehouse_id == warehouse_id,
                StockLevel.quantity + delta >= 0,
            )
            .values(quantity=StockLevel.quantity + delta, updated_at=func.now())
            .returning(StockLevel.quantity)
        )
        new_balance = (await self.session.execute(stmt_update)).scalar_one_or_none()
        if new_balance is not None:
            return DeltaResult(applied=True, balance=int(new_balance))
        current = await self.session.scalar(
            select(StockLevel.quantity).where(
                StockLevel.product_id == product_id, StockLevel.warehouse_id == warehouse_id
            )
        )
        return DeltaResult(applied=False, balance=int(current or 0))

    async def add_movement(self, movement: StockMovement) -> StockMovement:
        self.session.add(movement)
        await self.session.flush()  # id / created_at come back via INSERT ... RETURNING
        return movement

    async def get_movement(self, movement_id: int) -> StockMovement | None:
        return await self.session.get(StockMovement, movement_id)

    async def list_movements(
        self,
        *,
        product_id: uuid.UUID | None,
        warehouse_id: uuid.UUID | None,
        movement_type: str | None,
        created_from: datetime | None,
        created_to: datetime | None,
        before_id: int | None,
        limit: int,
    ) -> list[StockMovement]:
        """Keyset pagination, newest first: stable and O(limit) at any depth."""
        stmt = select(StockMovement).order_by(StockMovement.id.desc()).limit(limit)
        if product_id is not None:
            stmt = stmt.where(StockMovement.product_id == product_id)
        if warehouse_id is not None:
            stmt = stmt.where(StockMovement.warehouse_id == warehouse_id)
        if movement_type is not None:
            stmt = stmt.where(StockMovement.movement_type == movement_type)
        if created_from is not None:
            stmt = stmt.where(StockMovement.created_at >= created_from)
        if created_to is not None:
            stmt = stmt.where(StockMovement.created_at < created_to)
        if before_id is not None:
            stmt = stmt.where(StockMovement.id < before_id)
        return list((await self.session.scalars(stmt)).all())

    async def list_levels(
        self,
        *,
        product_id: uuid.UUID | None,
        warehouse_id: uuid.UUID | None,
        below_reorder: bool | None,
        limit: int,
        offset: int,
    ) -> tuple[list[Row[*tuple[Any, ...]]], int]:
        # The reorder level belongs to the product, so "below reorder" compares
        # the product's total across all warehouses, not each location.
        totals = (
            select(
                StockLevel.product_id.label("product_id"),
                func.sum(StockLevel.quantity).label("product_total"),
            )
            .group_by(StockLevel.product_id)
            .subquery()
        )
        # One joined query (no N+1): level + product + warehouse + product total.
        stmt = (
            select(
                StockLevel.product_id,
                Product.sku,
                Product.name.label("product_name"),
                StockLevel.warehouse_id,
                Warehouse.code.label("warehouse_code"),
                StockLevel.quantity,
                totals.c.product_total.label("product_total_quantity"),
                Product.reorder_level,
                StockLevel.updated_at,
            )
            .join(Product, Product.id == StockLevel.product_id)
            .join(Warehouse, Warehouse.id == StockLevel.warehouse_id)
            .join(totals, totals.c.product_id == StockLevel.product_id)
            .order_by(Product.sku, Warehouse.code)
        )
        if product_id is not None:
            stmt = stmt.where(StockLevel.product_id == product_id)
        if warehouse_id is not None:
            stmt = stmt.where(StockLevel.warehouse_id == warehouse_id)
        if below_reorder is True:
            stmt = stmt.where(totals.c.product_total < Product.reorder_level)
        elif below_reorder is False:
            stmt = stmt.where(totals.c.product_total >= Product.reorder_level)
        total = await count(self.session, stmt)
        rows = list((await self.session.execute(stmt.limit(limit).offset(offset))).all())
        return rows, total

    async def levels_for_product(self, product_id: uuid.UUID) -> list[Row[*tuple[Any, ...]]]:
        stmt = (
            select(
                StockLevel.warehouse_id, Warehouse.code.label("warehouse_code"), StockLevel.quantity
            )
            .join(Warehouse, Warehouse.id == StockLevel.warehouse_id)
            .where(StockLevel.product_id == product_id)
            .order_by(Warehouse.code)
        )
        return list((await self.session.execute(stmt)).all())
