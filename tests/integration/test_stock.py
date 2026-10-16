from __future__ import annotations

import uuid

import httpx

from tests.integration.conftest import Api

MOVEMENTS = "/api/v1/stock-movements"
TRANSFERS = "/api/v1/stock-transfers"


async def test_receipt_issue_and_adjustment_update_balance(api: Api) -> None:
    product, warehouse = await api.product(), await api.warehouse()

    receipt = await api.receive(product, warehouse, 10)
    assert receipt["movement_type"] == "receipt"
    assert receipt["quantity"] == 10
    assert receipt["balance_after"] == 10

    issue = await api.move(product, warehouse, "issue", 3)
    assert issue.status_code == 201
    assert issue.json()["quantity"] == -3  # stored signed
    assert issue.json()["balance_after"] == 7

    adjustment = await api.move(product, warehouse, "adjustment", -2)
    assert adjustment.status_code == 201
    assert adjustment.json()["balance_after"] == 5
    assert await api.balance(product, warehouse) == 5


async def test_insufficient_stock_is_rejected_and_changes_nothing(api: Api) -> None:
    product, warehouse = await api.product(), await api.warehouse()
    await api.receive(product, warehouse, 4)
    response = await api.move(product, warehouse, "issue", 5)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "INSUFFICIENT_STOCK"
    assert error["details"]["available"] == 4
    assert error["details"]["requested"] == 5
    assert await api.balance(product, warehouse) == 4

    never_stocked = await api.warehouse()
    response = await api.move(product, never_stocked, "issue", 1)
    assert response.status_code == 409
    assert response.json()["error"]["details"]["available"] == 0

    negative_adjustment = await api.move(product, warehouse, "adjustment", -5)
    assert negative_adjustment.status_code == 409


async def test_movement_permissions(
    api: Api, viewer_headers: dict[str, str], operator_headers: dict[str, str]
) -> None:
    product, warehouse = await api.product(), await api.warehouse()
    viewer = await api.move(product, warehouse, "receipt", 1, headers=viewer_headers)
    assert viewer.status_code == 403
    operator_adjust = await api.move(product, warehouse, "adjustment", 1, headers=operator_headers)
    assert operator_adjust.status_code == 403
    assert operator_adjust.json()["error"]["details"] == {"required_role": "manager"}
    operator_receipt = await api.move(product, warehouse, "receipt", 1, headers=operator_headers)
    assert operator_receipt.status_code == 201


async def test_movements_reject_unknown_and_archived_resources(
    api: Api, client: httpx.AsyncClient
) -> None:
    product, warehouse = await api.product(), await api.warehouse()
    unknown = {"id": str(uuid.uuid4())}
    assert (await api.move(unknown, warehouse, "receipt", 1)).status_code == 404
    assert (await api.move(product, unknown, "receipt", 1)).status_code == 404

    await client.patch(
        f"/api/v1/products/{product['id']}",
        json={"version": 1, "is_active": False},
        headers=api.manager,
    )
    archived = await api.move(product, warehouse, "receipt", 1)
    assert archived.status_code == 409
    assert archived.json()["error"]["code"] == "RESOURCE_INACTIVE"


async def test_transfer_moves_stock_atomically(api: Api, client: httpx.AsyncClient) -> None:
    product = await api.product()
    source, target = await api.warehouse(), await api.warehouse()
    await api.receive(product, source, 10)

    response = await client.post(
        TRANSFERS,
        json={
            "product_id": product["id"],
            "from_warehouse_id": source["id"],
            "to_warehouse_id": target["id"],
            "quantity": 4,
            "reference": "TR-1",
        },
        headers=api.operator,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["outbound"]["movement_type"] == "transfer_out"
    assert body["outbound"]["quantity"] == -4
    assert body["inbound"]["quantity"] == 4
    assert body["outbound"]["transfer_id"] == body["inbound"]["transfer_id"] == body["transfer_id"]
    assert await api.balance(product, source) == 6
    assert await api.balance(product, target) == 4


async def test_failed_transfer_leaves_both_warehouses_untouched(
    api: Api, client: httpx.AsyncClient
) -> None:
    product = await api.product()
    source, target = await api.warehouse(), await api.warehouse()
    await api.receive(product, source, 2)
    await api.receive(product, target, 1)
    response = await client.post(
        TRANSFERS,
        json={
            "product_id": product["id"],
            "from_warehouse_id": source["id"],
            "to_warehouse_id": target["id"],
            "quantity": 3,
        },
        headers=api.operator,
    )
    assert response.status_code == 409
    assert await api.balance(product, source) == 2
    assert await api.balance(product, target) == 1
    movements = await client.get(
        MOVEMENTS, params={"product_id": product["id"]}, headers=api.operator
    )
    assert {m["movement_type"] for m in movements.json()["items"]} == {"receipt"}


async def test_transfer_to_same_warehouse_is_invalid(api: Api, client: httpx.AsyncClient) -> None:
    product, warehouse = await api.product(), await api.warehouse()
    response = await client.post(
        TRANSFERS,
        json={
            "product_id": product["id"],
            "from_warehouse_id": warehouse["id"],
            "to_warehouse_id": warehouse["id"],
            "quantity": 1,
        },
        headers=api.operator,
    )
    assert response.status_code == 422


async def test_stock_levels_and_reorder_alerts(api: Api, client: httpx.AsyncClient) -> None:
    low = await api.product(sku="LOW", reorder_level=10)
    fine = await api.product(sku="FINE", reorder_level=1)
    # Low in one warehouse but fine in total: the product is NOT below reorder.
    spread = await api.product(sku="SPREAD", reorder_level=5)
    north, south = await api.warehouse("NORTH"), await api.warehouse("SOUTH")
    await api.receive(low, north, 3)
    await api.receive(low, south, 4)
    await api.receive(fine, north, 5)
    await api.receive(spread, north, 1)
    await api.receive(spread, south, 9)

    levels = await client.get("/api/v1/stock-levels", headers=api.operator)
    assert levels.status_code == 200
    rows = [
        (
            r["sku"],
            r["warehouse_code"],
            r["quantity"],
            r["product_total_quantity"],
            r["below_reorder"],
        )
        for r in levels.json()["items"]
    ]
    assert rows == [
        ("FINE", "NORTH", 5, 5, False),
        ("LOW", "NORTH", 3, 7, True),
        ("LOW", "SOUTH", 4, 7, True),
        ("SPREAD", "NORTH", 1, 10, False),
        ("SPREAD", "SOUTH", 9, 10, False),
    ]

    below = await client.get(
        "/api/v1/stock-levels", params={"below_reorder": "true"}, headers=api.operator
    )
    assert {r["sku"] for r in below.json()["items"]} == {"LOW"}
    assert below.json()["total"] == 2
    at_or_above = await client.get(
        "/api/v1/stock-levels", params={"below_reorder": "false"}, headers=api.operator
    )
    assert {r["sku"] for r in at_or_above.json()["items"]} == {"FINE", "SPREAD"}
    only_north = await client.get(
        "/api/v1/stock-levels", params={"warehouse_id": north["id"]}, headers=api.operator
    )
    # Filtering by warehouse must not change the product-wide total.
    assert [r["product_total_quantity"] for r in only_north.json()["items"]] == [5, 7, 10]

    summary = await client.get(f"/api/v1/products/{low['id']}/stock", headers=api.operator)
    assert summary.json()["total_quantity"] == 7
    assert summary.json()["below_reorder"] is True
    assert [w["warehouse_code"] for w in summary.json()["warehouses"]] == ["NORTH", "SOUTH"]


async def test_movement_ledger_cursor_pagination(api: Api, client: httpx.AsyncClient) -> None:
    product, warehouse = await api.product(), await api.warehouse()
    for _ in range(7):
        await api.receive(product, warehouse, 1)

    seen: list[int] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, str | int] = {"limit": 3}
        if cursor:
            params["cursor"] = cursor
        page = (await client.get(MOVEMENTS, params=params, headers=api.operator)).json()
        seen.extend(m["id"] for m in page["items"])
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert pages == 3
    assert seen == sorted(seen, reverse=True)
    assert len(seen) == len(set(seen)) == 7

    single = await client.get(f"{MOVEMENTS}/{seen[0]}", headers=api.operator)
    assert single.json()["balance_after"] == 7
    assert (await client.get(f"{MOVEMENTS}/999999", headers=api.operator)).status_code == 404
    bad_cursor = await client.get(MOVEMENTS, params={"cursor": "%%%"}, headers=api.operator)
    assert bad_cursor.status_code == 400


async def test_movement_filters(api: Api, client: httpx.AsyncClient) -> None:
    product, warehouse = await api.product(), await api.warehouse()
    await api.receive(product, warehouse, 5)
    await api.move(product, warehouse, "issue", 2)
    issues = await client.get(
        MOVEMENTS,
        params={"movement_type": "issue", "warehouse_id": warehouse["id"]},
        headers=api.operator,
    )
    assert [m["quantity"] for m in issues.json()["items"]] == [-2]
    future = await client.get(
        MOVEMENTS, params={"created_from": "2999-01-01T00:00:00Z"}, headers=api.operator
    )
    assert future.json()["items"] == []
