from __future__ import annotations

import uuid

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import like_contains
from app.models.catalog import Category, Product, Warehouse
from app.models.stock import StockMovement
from app.repositories.base import paginate_scalars


class CategoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, category_id: uuid.UUID) -> Category | None:
        return await self.session.get(Category, category_id)

    async def search(self, *, q: str | None, limit: int, offset: int) -> tuple[list[Category], int]:
        stmt = select(Category).order_by(Category.name)
        if q:
            stmt = stmt.where(Category.name.ilike(like_contains(q), escape="\\"))
        return await paginate_scalars(self.session, stmt, limit=limit, offset=offset)

    async def has_products(self, category_id: uuid.UUID) -> bool:
        return bool(
            await self.session.scalar(select(exists().where(Product.category_id == category_id)))
        )


class ProductRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, product_id: uuid.UUID) -> Product | None:
        return await self.session.get(Product, product_id)

    async def search(
        self,
        *,
        q: str | None,
        category_id: uuid.UUID | None,
        is_active: bool | None,
        limit: int,
        offset: int,
    ) -> tuple[list[Product], int]:
        stmt = select(Product).order_by(Product.sku)
        if q:
            pattern = like_contains(q)
            stmt = stmt.where(
                or_(
                    Product.sku.ilike(pattern, escape="\\"),
                    Product.name.ilike(pattern, escape="\\"),
                )
            )
        if category_id is not None:
            stmt = stmt.where(Product.category_id == category_id)
        if is_active is not None:
            stmt = stmt.where(Product.is_active == is_active)
        return await paginate_scalars(self.session, stmt, limit=limit, offset=offset)

    async def has_movements(self, product_id: uuid.UUID) -> bool:
        return bool(
            await self.session.scalar(
                select(exists().where(StockMovement.product_id == product_id))
            )
        )


class WarehouseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, warehouse_id: uuid.UUID) -> Warehouse | None:
        return await self.session.get(Warehouse, warehouse_id)

    async def search(
        self, *, q: str | None, is_active: bool | None, limit: int, offset: int
    ) -> tuple[list[Warehouse], int]:
        stmt = select(Warehouse).order_by(Warehouse.code)
        if q:
            pattern = like_contains(q)
            stmt = stmt.where(
                or_(
                    Warehouse.code.ilike(pattern, escape="\\"),
                    Warehouse.name.ilike(pattern, escape="\\"),
                )
            )
        if is_active is not None:
            stmt = stmt.where(Warehouse.is_active == is_active)
        return await paginate_scalars(self.session, stmt, limit=limit, offset=offset)

    async def has_movements(self, warehouse_id: uuid.UUID) -> bool:
        return bool(
            await self.session.scalar(
                select(exists().where(StockMovement.warehouse_id == warehouse_id))
            )
        )
