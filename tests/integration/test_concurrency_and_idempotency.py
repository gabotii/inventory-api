"""Behaviour under concurrent and retried requests.

Requests are issued with asyncio.gather through the ASGI app; each uses its own
pooled connection, so they genuinely contend for the same PostgreSQL rows.
"""

from __future__ import annotations

import asyncio
from collections import Counter

import httpx
from sqlalchemy import func, select

from app.api.deps import AppState
from app.models.enums import Role
from app.models.stock import StockLevel, StockMovement
from tests.integration.conftest import Api, UserFactory

TRANSFERS = "/api/v1/stock-transfers"


async def assert_ledger_consistent(state: AppState) -> None:
    """Invariant: every stock level equals the sum of its movements."""
    async with state.sessionmaker() as session:
        sums_query = select(
            StockMovement.product_id, StockMovement.warehouse_id, func.sum(StockMovement.quantity)
        ).group_by(StockMovement.product_id, StockMovement.warehouse_id)
        sums = {
            (product_id, warehouse_id): int(total)
            for product_id, warehouse_id, total in (await session.execute(sums_query)).all()
        }
        levels = {
            (level.product_id, level.warehouse_id): level.quantity
            for level in (await session.scalars(select(StockLevel))).all()
        }
    assert sums
    assert levels == sums


async def test_concurrent_issues_never_oversell(api: Api, state: AppState) -> None:
    product, warehouse = await api.product(), await api.warehouse()
    await api.receive(product, warehouse, 10)

    responses = await asyncio.gather(*(api.move(product, warehouse, "issue", 1) for _ in range(25)))

    statuses = Counter(response.status_code for response in responses)
    assert statuses == {201: 10, 409: 15}
    assert await api.balance(product, warehouse) == 0
    balances = sorted(r.json()["balance_after"] for r in responses if r.status_code == 201)
    assert balances == list(range(10))  # every successful issue saw a distinct balance
    await assert_ledger_consistent(state)


async def test_concurrent_receipts_on_new_stock_row(api: Api, state: AppState) -> None:
    product, warehouse = await api.product(), await api.warehouse()
    responses = await asyncio.gather(
        *(api.move(product, warehouse, "receipt", 3) for _ in range(20))
    )
    assert all(response.status_code == 201 for response in responses)
    assert await api.balance(product, warehouse) == 60
    await assert_ledger_consistent(state)


async def test_opposite_transfers_do_not_deadlock_and_conserve_stock(
    api: Api, client: httpx.AsyncClient, state: AppState
) -> None:
    product = await api.product()
    east, west = await api.warehouse(), await api.warehouse()
    await api.receive(product, east, 50)
    await api.receive(product, west, 50)

    async def transfer(source: dict[str, str], target: dict[str, str]) -> httpx.Response:
        return await client.post(
            TRANSFERS,
            json={
                "product_id": product["id"],
                "from_warehouse_id": source["id"],
                "to_warehouse_id": target["id"],
                "quantity": 1,
            },
            headers=api.operator,
        )

    calls = [transfer(east, west) if i % 2 else transfer(west, east) for i in range(20)]
    responses = await asyncio.wait_for(asyncio.gather(*calls), timeout=30)
    assert [r.status_code for r in responses] == [201] * 20
    assert await api.balance(product, east) + await api.balance(product, west) == 100
    await assert_ledger_consistent(state)


async def test_concurrent_edits_with_same_version(api: Api, client: httpx.AsyncClient) -> None:
    product = await api.product()
    responses = await asyncio.gather(
        *(
            client.patch(
                f"/api/v1/products/{product['id']}",
                json={"version": 1, "name": f"Name {i}"},
                headers=api.manager,
            )
            for i in range(5)
        )
    )
    statuses = Counter(response.status_code for response in responses)
    assert statuses == {200: 1, 409: 4}


async def test_idempotent_retry_returns_original_result(api: Api, state: AppState) -> None:
    product, warehouse = await api.product(), await api.warehouse()
    first = await api.move(product, warehouse, "receipt", 5, idempotency_key="po-123-line-1")
    retry = await api.move(product, warehouse, "receipt", 5, idempotency_key="po-123-line-1")
    assert first.status_code == retry.status_code == 201
    assert retry.json() == first.json()
    assert retry.headers["Idempotent-Replayed"] == "true"
    assert "Idempotent-Replayed" not in first.headers
    assert await api.balance(product, warehouse) == 5


async def test_idempotency_key_reuse_with_different_body_is_rejected(api: Api) -> None:
    product, warehouse = await api.product(), await api.warehouse()
    await api.move(product, warehouse, "receipt", 5, idempotency_key="key-1")
    different = await api.move(product, warehouse, "receipt", 6, idempotency_key="key-1")
    assert different.status_code == 422
    assert different.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert await api.balance(product, warehouse) == 5


async def test_idempotency_keys_are_scoped_per_user(api: Api, users: UserFactory) -> None:
    product, warehouse = await api.product(), await api.warehouse()
    other_operator = await users.headers_for(Role.OPERATOR)
    await api.move(product, warehouse, "receipt", 1, idempotency_key="shared")
    second = await api.move(
        product, warehouse, "receipt", 1, idempotency_key="shared", headers=other_operator
    )
    assert second.status_code == 201
    assert "Idempotent-Replayed" not in second.headers
    assert await api.balance(product, warehouse) == 2


async def test_failed_requests_are_not_cached(api: Api) -> None:
    product, warehouse = await api.product(), await api.warehouse()
    failed = await api.move(product, warehouse, "issue", 3, idempotency_key="issue-1")
    assert failed.status_code == 409
    await api.receive(product, warehouse, 3)
    retried = await api.move(product, warehouse, "issue", 3, idempotency_key="issue-1")
    assert retried.status_code == 201
    assert await api.balance(product, warehouse) == 0


async def test_concurrent_duplicates_execute_once(api: Api, state: AppState) -> None:
    product, warehouse = await api.product(), await api.warehouse()
    responses = await asyncio.gather(
        *(api.move(product, warehouse, "receipt", 7, idempotency_key="burst") for _ in range(10))
    )
    assert all(response.status_code == 201 for response in responses)
    assert len({response.json()["id"] for response in responses}) == 1
    assert sum(response.headers.get("Idempotent-Replayed") == "true" for response in responses) == 9
    assert await api.balance(product, warehouse) == 7
    await assert_ledger_consistent(state)


async def test_transfer_idempotency(api: Api, client: httpx.AsyncClient) -> None:
    product = await api.product()
    source, target = await api.warehouse(), await api.warehouse()
    await api.receive(product, source, 10)
    body = {
        "product_id": product["id"],
        "from_warehouse_id": source["id"],
        "to_warehouse_id": target["id"],
        "quantity": 4,
    }
    headers = {**api.operator, "Idempotency-Key": "transfer-1"}
    first = await client.post(TRANSFERS, json=body, headers=headers)
    second = await client.post(TRANSFERS, json=body, headers=headers)
    assert first.json() == second.json()
    assert await api.balance(product, source) == 6
    # Same key on a different endpoint is a misuse, not a replay.
    misuse = await api.move(product, source, "receipt", 1, idempotency_key="transfer-1")
    assert misuse.status_code == 422


async def test_invalid_idempotency_key_header(api: Api) -> None:
    product, warehouse = await api.product(), await api.warehouse()
    response = await api.move(product, warehouse, "receipt", 1, idempotency_key="has spaces")
    assert response.status_code == 422
