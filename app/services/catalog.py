from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from app.core.errors import (
    AlreadyExistsError,
    ConflictError,
    NotFoundError,
    ResourceInUseError,
    VersionConflictError,
)
from app.db.errors import FOREIGN_KEY_VIOLATION, UNIQUE_VIOLATION, sqlstate
from app.models.catalog import Category, Product, Warehouse
from app.repositories.catalog import CategoryRepository, ProductRepository, WarehouseRepository
from app.schemas.catalog import (
    CategoryCreate,
    CategoryUpdate,
    ProductCreate,
    ProductUpdate,
    WarehouseCreate,
    WarehouseUpdate,
)
from app.schemas.common import PatchModel
from app.services.permissions import Actor, Permission, authorize
from app.services.support import AuditService

type Versioned = Category | Product | Warehouse


class CatalogService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._categories = CategoryRepository(session)
        self._products = ProductRepository(session)
        self._warehouses = WarehouseRepository(session)
        self._audit = AuditService(session)

    # ================================================================ categories
    async def list_categories(
        self, actor: Actor, *, q: str | None, limit: int, offset: int
    ) -> tuple[list[Category], int]:
        authorize(actor, Permission.READ_INVENTORY)
        return await self._categories.search(q=q, limit=limit, offset=offset)

    async def get_category(self, actor: Actor, category_id: uuid.UUID) -> Category:
        authorize(actor, Permission.READ_INVENTORY)
        return await self._category_or_404(category_id)

    async def create_category(self, actor: Actor, data: CategoryCreate) -> Category:
        authorize(actor, Permission.MANAGE_CATALOG)
        category = Category(name=data.name, description=data.description)
        self._session.add(category)
        await self._flush(unique_field="name")
        self._record(actor, "category.created", category, {"name": category.name})
        await self._session.commit()
        return category

    async def update_category(
        self, actor: Actor, category_id: uuid.UUID, data: CategoryUpdate
    ) -> Category:
        authorize(actor, Permission.MANAGE_CATALOG)
        category = await self._category_or_404(category_id)
        await self._apply(actor, category, data, unique_field="name", action="category.updated")
        return category

    async def delete_category(self, actor: Actor, category_id: uuid.UUID) -> None:
        authorize(actor, Permission.MANAGE_CATALOG)
        category = await self._category_or_404(category_id)
        if await self._categories.has_products(category_id):
            raise ResourceInUseError("Category still has products; reassign them first")
        await self._delete(actor, category, "category.deleted")

    # ================================================================== products
    async def list_products(
        self,
        actor: Actor,
        *,
        q: str | None,
        category_id: uuid.UUID | None,
        is_active: bool | None,
        limit: int,
        offset: int,
    ) -> tuple[list[Product], int]:
        authorize(actor, Permission.READ_INVENTORY)
        return await self._products.search(
            q=q, category_id=category_id, is_active=is_active, limit=limit, offset=offset
        )

    async def get_product(self, actor: Actor, product_id: uuid.UUID) -> Product:
        authorize(actor, Permission.READ_INVENTORY)
        return await self._product_or_404(product_id)

    async def create_product(self, actor: Actor, data: ProductCreate) -> Product:
        authorize(actor, Permission.MANAGE_CATALOG)
        if data.category_id is not None:
            await self._category_or_404(data.category_id)
        product = Product(**data.model_dump(), is_active=True)
        self._session.add(product)
        await self._flush(unique_field="sku")
        self._record(actor, "product.created", product, {"sku": product.sku})
        await self._session.commit()
        return product

    async def update_product(
        self, actor: Actor, product_id: uuid.UUID, data: ProductUpdate
    ) -> Product:
        authorize(actor, Permission.MANAGE_CATALOG)
        product = await self._product_or_404(product_id)
        if "category_id" in data.model_fields_set and data.category_id is not None:
            await self._category_or_404(data.category_id)
        await self._apply(actor, product, data, unique_field="sku", action="product.updated")
        return product

    async def delete_product(self, actor: Actor, product_id: uuid.UUID) -> None:
        authorize(actor, Permission.MANAGE_CATALOG)
        product = await self._product_or_404(product_id)
        if await self._products.has_movements(product_id):
            raise ResourceInUseError(
                "Product has stock history and cannot be deleted; archive it instead "
                "(PATCH is_active=false)"
            )
        await self._delete(actor, product, "product.deleted")

    # ================================================================ warehouses
    async def list_warehouses(
        self, actor: Actor, *, q: str | None, is_active: bool | None, limit: int, offset: int
    ) -> tuple[list[Warehouse], int]:
        authorize(actor, Permission.READ_INVENTORY)
        return await self._warehouses.search(q=q, is_active=is_active, limit=limit, offset=offset)

    async def get_warehouse(self, actor: Actor, warehouse_id: uuid.UUID) -> Warehouse:
        authorize(actor, Permission.READ_INVENTORY)
        return await self._warehouse_or_404(warehouse_id)

    async def create_warehouse(self, actor: Actor, data: WarehouseCreate) -> Warehouse:
        authorize(actor, Permission.MANAGE_CATALOG)
        warehouse = Warehouse(**data.model_dump(), is_active=True)
        self._session.add(warehouse)
        await self._flush(unique_field="code")
        self._record(actor, "warehouse.created", warehouse, {"code": warehouse.code})
        await self._session.commit()
        return warehouse

    async def update_warehouse(
        self, actor: Actor, warehouse_id: uuid.UUID, data: WarehouseUpdate
    ) -> Warehouse:
        authorize(actor, Permission.MANAGE_CATALOG)
        warehouse = await self._warehouse_or_404(warehouse_id)
        await self._apply(actor, warehouse, data, unique_field="code", action="warehouse.updated")
        return warehouse

    async def delete_warehouse(self, actor: Actor, warehouse_id: uuid.UUID) -> None:
        authorize(actor, Permission.MANAGE_CATALOG)
        warehouse = await self._warehouse_or_404(warehouse_id)
        if await self._warehouses.has_movements(warehouse_id):
            raise ResourceInUseError(
                "Warehouse has stock history and cannot be deleted; archive it instead "
                "(PATCH is_active=false)"
            )
        await self._delete(actor, warehouse, "warehouse.deleted")

    # =================================================================== helpers
    async def _category_or_404(self, category_id: uuid.UUID) -> Category:
        category = await self._categories.get(category_id)
        if category is None:
            raise NotFoundError("Category")
        return category

    async def _product_or_404(self, product_id: uuid.UUID) -> Product:
        product = await self._products.get(product_id)
        if product is None:
            raise NotFoundError("Product")
        return product

    async def _warehouse_or_404(self, warehouse_id: uuid.UUID) -> Warehouse:
        warehouse = await self._warehouses.get(warehouse_id)
        if warehouse is None:
            raise NotFoundError("Warehouse")
        return warehouse

    async def _apply(
        self, actor: Actor, entity: Versioned, data: PatchModel, *, unique_field: str, action: str
    ) -> None:
        if entity.version != data.version:
            raise VersionConflictError(details={"current_version": entity.version})
        applied: dict[str, Any] = {}
        for field, value in data.changes().items():
            if getattr(entity, field) != value:
                setattr(entity, field, value)
                applied[field] = value
        if not applied:
            return
        # version_id_col turns this into UPDATE ... WHERE version = <read version>,
        # so a concurrent writer between our read and this flush is detected.
        await self._flush(unique_field=unique_field)
        self._record(actor, action, entity, {"changes": applied})
        await self._session.commit()

    async def _delete(self, actor: Actor, entity: Versioned, action: str) -> None:
        await self._session.delete(entity)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            # A concurrent request added a reference after our pre-check.
            if sqlstate(exc) == FOREIGN_KEY_VIOLATION:
                raise ResourceInUseError() from exc
            raise
        self._record(actor, action, entity, {})
        await self._session.commit()

    async def _flush(self, *, unique_field: str) -> None:
        try:
            await self._session.flush()
        except StaleDataError as exc:
            raise VersionConflictError() from exc
        except IntegrityError as exc:
            code = sqlstate(exc)
            if code == UNIQUE_VIOLATION:
                raise AlreadyExistsError(
                    f"A resource with this {unique_field} already exists",
                    details={"field": unique_field},
                ) from exc
            if code == FOREIGN_KEY_VIOLATION:
                raise ConflictError("A referenced resource no longer exists") from exc
            raise

    def _record(
        self, actor: Actor, action: str, entity: Versioned, details: dict[str, Any]
    ) -> None:
        self._audit.record(
            action=action,
            entity_type=action.split(".", 1)[0],
            entity_id=entity.id,
            actor_id=actor.id,
            ip_address=actor.meta.ip_address,
            details=details,
        )
