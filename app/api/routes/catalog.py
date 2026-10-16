from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.api.deps import ActorDep, CatalogServiceDep, PageDep, SearchQuery, StockServiceDep
from app.schemas.catalog import (
    CategoryCreate,
    CategoryRead,
    CategoryUpdate,
    ProductCreate,
    ProductRead,
    ProductUpdate,
    WarehouseCreate,
    WarehouseRead,
    WarehouseUpdate,
)
from app.schemas.common import ErrorResponse, Page
from app.schemas.stock import ProductStockRead

_COMMON: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
}
_ITEM: dict[int | str, dict[str, object]] = {404: {"model": ErrorResponse}}
_WRITE: dict[int | str, dict[str, object]] = {
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
}

categories = APIRouter(prefix="/categories", tags=["categories"], responses=_COMMON)
products = APIRouter(prefix="/products", tags=["products"], responses=_COMMON)
warehouses = APIRouter(prefix="/warehouses", tags=["warehouses"], responses=_COMMON)


# ===================================================================== categories
@categories.get("", response_model=Page[CategoryRead])
async def list_categories(
    actor: ActorDep, catalog: CatalogServiceDep, page: PageDep, q: SearchQuery = None
) -> Page[CategoryRead]:
    items, total = await catalog.list_categories(actor, q=q, limit=page.limit, offset=page.offset)
    return Page(
        items=[CategoryRead.model_validate(c) for c in items],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@categories.post(
    "", response_model=CategoryRead, status_code=status.HTTP_201_CREATED, responses=_WRITE
)
async def create_category(
    body: CategoryCreate, actor: ActorDep, catalog: CatalogServiceDep
) -> CategoryRead:
    return CategoryRead.model_validate(await catalog.create_category(actor, body))


@categories.get("/{category_id}", response_model=CategoryRead, responses=_ITEM)
async def get_category(
    category_id: uuid.UUID, actor: ActorDep, catalog: CatalogServiceDep
) -> CategoryRead:
    return CategoryRead.model_validate(await catalog.get_category(actor, category_id))


@categories.patch("/{category_id}", response_model=CategoryRead, responses=_WRITE)
async def update_category(
    category_id: uuid.UUID, body: CategoryUpdate, actor: ActorDep, catalog: CatalogServiceDep
) -> CategoryRead:
    return CategoryRead.model_validate(await catalog.update_category(actor, category_id, body))


@categories.delete("/{category_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_WRITE)
async def delete_category(
    category_id: uuid.UUID, actor: ActorDep, catalog: CatalogServiceDep
) -> None:
    await catalog.delete_category(actor, category_id)


# ======================================================================= products
@products.get("", response_model=Page[ProductRead])
async def list_products(
    actor: ActorDep,
    catalog: CatalogServiceDep,
    page: PageDep,
    q: SearchQuery = None,
    category_id: uuid.UUID | None = None,
    is_active: bool | None = None,
) -> Page[ProductRead]:
    items, total = await catalog.list_products(
        actor,
        q=q,
        category_id=category_id,
        is_active=is_active,
        limit=page.limit,
        offset=page.offset,
    )
    return Page(
        items=[ProductRead.model_validate(p) for p in items],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@products.post(
    "", response_model=ProductRead, status_code=status.HTTP_201_CREATED, responses=_WRITE
)
async def create_product(
    body: ProductCreate, actor: ActorDep, catalog: CatalogServiceDep
) -> ProductRead:
    return ProductRead.model_validate(await catalog.create_product(actor, body))


@products.get("/{product_id}", response_model=ProductRead, responses=_ITEM)
async def get_product(
    product_id: uuid.UUID, actor: ActorDep, catalog: CatalogServiceDep
) -> ProductRead:
    return ProductRead.model_validate(await catalog.get_product(actor, product_id))


@products.patch("/{product_id}", response_model=ProductRead, responses=_WRITE)
async def update_product(
    product_id: uuid.UUID, body: ProductUpdate, actor: ActorDep, catalog: CatalogServiceDep
) -> ProductRead:
    """Partial update. Send the ``version`` you last read; 409 if it changed."""
    return ProductRead.model_validate(await catalog.update_product(actor, product_id, body))


@products.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_WRITE)
async def delete_product(
    product_id: uuid.UUID, actor: ActorDep, catalog: CatalogServiceDep
) -> None:
    """Only products without stock history can be deleted; archive the rest."""
    await catalog.delete_product(actor, product_id)


@products.get("/{product_id}/stock", response_model=ProductStockRead, responses=_ITEM)
async def get_product_stock(
    product_id: uuid.UUID, actor: ActorDep, stock: StockServiceDep
) -> ProductStockRead:
    return await stock.product_stock(actor, product_id)


# ===================================================================== warehouses
@warehouses.get("", response_model=Page[WarehouseRead])
async def list_warehouses(
    actor: ActorDep,
    catalog: CatalogServiceDep,
    page: PageDep,
    q: SearchQuery = None,
    is_active: bool | None = None,
) -> Page[WarehouseRead]:
    items, total = await catalog.list_warehouses(
        actor, q=q, is_active=is_active, limit=page.limit, offset=page.offset
    )
    return Page(
        items=[WarehouseRead.model_validate(w) for w in items],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@warehouses.post(
    "", response_model=WarehouseRead, status_code=status.HTTP_201_CREATED, responses=_WRITE
)
async def create_warehouse(
    body: WarehouseCreate, actor: ActorDep, catalog: CatalogServiceDep
) -> WarehouseRead:
    return WarehouseRead.model_validate(await catalog.create_warehouse(actor, body))


@warehouses.get("/{warehouse_id}", response_model=WarehouseRead, responses=_ITEM)
async def get_warehouse(
    warehouse_id: uuid.UUID, actor: ActorDep, catalog: CatalogServiceDep
) -> WarehouseRead:
    return WarehouseRead.model_validate(await catalog.get_warehouse(actor, warehouse_id))


@warehouses.patch("/{warehouse_id}", response_model=WarehouseRead, responses=_WRITE)
async def update_warehouse(
    warehouse_id: uuid.UUID, body: WarehouseUpdate, actor: ActorDep, catalog: CatalogServiceDep
) -> WarehouseRead:
    return WarehouseRead.model_validate(await catalog.update_warehouse(actor, warehouse_id, body))


@warehouses.delete("/{warehouse_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_WRITE)
async def delete_warehouse(
    warehouse_id: uuid.UUID, actor: ActorDep, catalog: CatalogServiceDep
) -> None:
    await catalog.delete_warehouse(actor, warehouse_id)
