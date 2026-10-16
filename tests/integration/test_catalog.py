from __future__ import annotations

import httpx
import pytest

from app.models.enums import Role
from tests.integration.conftest import Api, UserFactory

PRODUCTS = "/api/v1/products"
CATEGORIES = "/api/v1/categories"
WAREHOUSES = "/api/v1/warehouses"


async def test_product_lifecycle(
    client: httpx.AsyncClient, manager_headers: dict[str, str], viewer_headers: dict[str, str]
) -> None:
    category = await client.post(CATEGORIES, json={"name": "Hardware"}, headers=manager_headers)
    assert category.status_code == 201
    created = await client.post(
        PRODUCTS,
        json={
            "sku": " bolt-m8 ",
            "name": "Bolt M8",
            "category_id": category.json()["id"],
            "reorder_level": 50,
        },
        headers=manager_headers,
    )
    assert created.status_code == 201, created.text
    product = created.json()
    assert product["sku"] == "BOLT-M8"
    assert product["unit"] == "unit"
    assert product["is_active"] is True

    fetched = await client.get(f"{PRODUCTS}/{product['id']}", headers=viewer_headers)
    assert fetched.json() == product

    updated = await client.patch(
        f"{PRODUCTS}/{product['id']}",
        json={"version": product["version"], "name": "Bolt M8 zinc", "category_id": None},
        headers=manager_headers,
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Bolt M8 zinc"
    assert updated.json()["category_id"] is None
    assert updated.json()["version"] == product["version"] + 1
    assert updated.json()["updated_at"] >= product["updated_at"]

    deleted = await client.delete(f"{PRODUCTS}/{product['id']}", headers=manager_headers)
    assert deleted.status_code == 204
    gone = await client.get(f"{PRODUCTS}/{product['id']}", headers=viewer_headers)
    assert gone.status_code == 404


@pytest.mark.parametrize("role", [Role.VIEWER, Role.OPERATOR])
async def test_catalog_writes_require_manager(
    client: httpx.AsyncClient, users: UserFactory, api: Api, role: Role
) -> None:
    headers = await users.headers_for(role)
    product = await api.product()
    warehouse = await api.warehouse()
    responses = [
        await client.post(PRODUCTS, json={"sku": "X1", "name": "X"}, headers=headers),
        await client.patch(
            f"{PRODUCTS}/{product['id']}", json={"version": 1, "name": "Y"}, headers=headers
        ),
        await client.delete(f"{PRODUCTS}/{product['id']}", headers=headers),
        await client.post(CATEGORIES, json={"name": "C"}, headers=headers),
        await client.post(WAREHOUSES, json={"code": "W", "name": "W"}, headers=headers),
        await client.delete(f"{WAREHOUSES}/{warehouse['id']}", headers=headers),
    ]
    assert [r.status_code for r in responses] == [403] * len(responses)
    # Reads are allowed for every role.
    assert (await client.get(PRODUCTS, headers=headers)).status_code == 200


async def test_duplicate_sku_and_category_names_conflict(
    client: httpx.AsyncClient, manager_headers: dict[str, str], api: Api
) -> None:
    await api.product(sku="DUP-1")
    response = await client.post(
        PRODUCTS, json={"sku": "dup-1", "name": "Again"}, headers=manager_headers
    )
    assert response.status_code == 409
    assert response.json()["error"]["details"] == {"field": "sku"}

    await client.post(CATEGORIES, json={"name": "Tools"}, headers=manager_headers)
    clash = await client.post(CATEGORIES, json={"name": "TOOLS"}, headers=manager_headers)
    assert clash.status_code == 409


async def test_product_with_unknown_category_is_404(
    client: httpx.AsyncClient, manager_headers: dict[str, str]
) -> None:
    response = await client.post(
        PRODUCTS,
        json={"sku": "A1", "name": "A", "category_id": "00000000-0000-0000-0000-000000000000"},
        headers=manager_headers,
    )
    assert response.status_code == 404
    assert response.json()["error"]["details"] == {"resource": "Category"}


async def test_patch_validation(
    client: httpx.AsyncClient, manager_headers: dict[str, str], api: Api
) -> None:
    product = await api.product()
    url = f"{PRODUCTS}/{product['id']}"
    null_name = await client.patch(url, json={"version": 1, "name": None}, headers=manager_headers)
    assert null_name.status_code == 422
    sku_change = await client.patch(url, json={"version": 1, "sku": "NEW"}, headers=manager_headers)
    assert sku_change.status_code == 422  # SKU is immutable (unknown field)
    no_version = await client.patch(url, json={"name": "x"}, headers=manager_headers)
    assert no_version.status_code == 422
    stale = await client.patch(url, json={"version": 99, "name": "x"}, headers=manager_headers)
    assert stale.status_code == 409
    noop = await client.patch(
        url, json={"version": 1, "name": product["name"]}, headers=manager_headers
    )
    assert noop.status_code == 200
    assert noop.json()["version"] == 1  # nothing changed, no new version


async def test_search_treats_wildcards_literally(
    client: httpx.AsyncClient, viewer_headers: dict[str, str], api: Api
) -> None:
    await api.product(sku="A-1", name="50% discount bin")
    await api.product(sku="A-2", name="Regular bin")
    await api.product(sku="B_1", name="Underscore item")

    percent = await client.get(PRODUCTS, params={"q": "%"}, headers=viewer_headers)
    assert [p["sku"] for p in percent.json()["items"]] == ["A-1"]
    underscore = await client.get(PRODUCTS, params={"q": "_"}, headers=viewer_headers)
    assert [p["sku"] for p in underscore.json()["items"]] == ["B_1"]
    by_name = await client.get(PRODUCTS, params={"q": "BIN"}, headers=viewer_headers)
    assert by_name.json()["total"] == 2


async def test_pagination_metadata(
    client: httpx.AsyncClient, viewer_headers: dict[str, str], api: Api
) -> None:
    for index in range(5):
        await api.product(sku=f"P-{index}")
    page = await client.get(PRODUCTS, params={"limit": 2, "offset": 2}, headers=viewer_headers)
    body = page.json()
    assert body["total"] == 5
    assert body["limit"] == 2
    assert body["offset"] == 2
    assert [p["sku"] for p in body["items"]] == ["P-2", "P-3"]


async def test_archived_products_filter(
    client: httpx.AsyncClient, manager_headers: dict[str, str], api: Api
) -> None:
    keep = await api.product(sku="KEEP")
    old = await api.product(sku="OLD")
    archived = await client.patch(
        f"{PRODUCTS}/{old['id']}", json={"version": 1, "is_active": False}, headers=manager_headers
    )
    assert archived.status_code == 200
    active = await client.get(PRODUCTS, params={"is_active": "true"}, headers=manager_headers)
    assert [p["id"] for p in active.json()["items"]] == [keep["id"]]


async def test_category_in_use_cannot_be_deleted(
    client: httpx.AsyncClient, manager_headers: dict[str, str], api: Api
) -> None:
    category = (
        await client.post(CATEGORIES, json={"name": "Used"}, headers=manager_headers)
    ).json()
    await api.product(category_id=category["id"])
    response = await client.delete(f"{CATEGORIES}/{category['id']}", headers=manager_headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RESOURCE_IN_USE"


async def test_products_and_warehouses_with_history_cannot_be_deleted(
    client: httpx.AsyncClient, manager_headers: dict[str, str], api: Api
) -> None:
    product, warehouse = await api.product(), await api.warehouse()
    await api.receive(product, warehouse, 5)
    for url in (f"{PRODUCTS}/{product['id']}", f"{WAREHOUSES}/{warehouse['id']}"):
        response = await client.delete(url, headers=manager_headers)
        assert response.status_code == 409
        assert "archive" in response.json()["error"]["message"]


async def test_warehouse_crud(
    client: httpx.AsyncClient, manager_headers: dict[str, str], viewer_headers: dict[str, str]
) -> None:
    created = await client.post(
        WAREHOUSES,
        json={"code": "mad-01", "name": "Madrid", "address": "Calle 1"},
        headers=manager_headers,
    )
    assert created.status_code == 201
    warehouse = created.json()
    assert warehouse["code"] == "MAD-01"
    dup = await client.post(
        WAREHOUSES, json={"code": "MAD-01", "name": "Dup"}, headers=manager_headers
    )
    assert dup.status_code == 409
    listed = await client.get(WAREHOUSES, params={"q": "madr"}, headers=viewer_headers)
    assert listed.json()["total"] == 1
    patched = await client.patch(
        f"{WAREHOUSES}/{warehouse['id']}",
        json={"version": 1, "address": None},
        headers=manager_headers,
    )
    assert patched.json()["address"] is None
    deleted = await client.delete(f"{WAREHOUSES}/{warehouse['id']}", headers=manager_headers)
    assert deleted.status_code == 204


async def test_catalog_changes_are_audited(
    client: httpx.AsyncClient, admin_headers: dict[str, str], api: Api
) -> None:
    product = await api.product(sku="AUD-1")
    await client.patch(
        f"{PRODUCTS}/{product['id']}", json={"version": 1, "reorder_level": 7}, headers=api.manager
    )
    events = await client.get(
        "/api/v1/audit-events",
        params={"entity_type": "product", "entity_id": product["id"]},
        headers=admin_headers,
    )
    assert events.status_code == 200
    assert [(e["action"], e["details"]) for e in events.json()["items"]] == [
        ("product.updated", {"changes": {"reorder_level": 7}}),
        ("product.created", {"sku": "AUD-1"}),
    ]
    forbidden = await client.get("/api/v1/audit-events", headers=api.manager)
    assert forbidden.status_code == 403


async def test_category_read_update_and_delete(
    client: httpx.AsyncClient, manager_headers: dict[str, str], viewer_headers: dict[str, str]
) -> None:
    created = await client.post(
        CATEGORIES, json={"name": "Paint", "description": "Wall paint"}, headers=manager_headers
    )
    category = created.json()
    listed = await client.get(CATEGORIES, params={"q": "pai"}, headers=viewer_headers)
    assert [c["name"] for c in listed.json()["items"]] == ["Paint"]
    fetched = await client.get(f"{CATEGORIES}/{category['id']}", headers=viewer_headers)
    assert fetched.json() == category
    renamed = await client.patch(
        f"{CATEGORIES}/{category['id']}",
        json={"version": 1, "name": "Paints", "description": None},
        headers=manager_headers,
    )
    assert renamed.json()["name"] == "Paints"
    assert renamed.json()["description"] is None
    assert (
        await client.delete(f"{CATEGORIES}/{category['id']}", headers=manager_headers)
    ).status_code == 204
    assert (
        await client.get(f"{CATEGORIES}/{category['id']}", headers=viewer_headers)
    ).status_code == 404
