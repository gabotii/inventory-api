"""End-to-end smoke test against a *running* deployment.

    SMOKE_BASE_URL=http://127.0.0.1:8000 \\
    SMOKE_ADMIN_EMAIL=admin@example.com SMOKE_ADMIN_PASSWORD=... \\
    python scripts/smoke_test.py

Exercises the critical journeys over real HTTP: health, login, user
management, catalog, receipts with idempotent retry, transfer, oversell
protection, reporting, token rotation/reuse detection and logout. Data is
namespaced with a random run id; the users it creates are deactivated at the end.
Exits non-zero on the first failed check.
"""

from __future__ import annotations

import os
import sys
import uuid
from typing import Any

import httpx

PASSWORD = "smoke-" + uuid.uuid4().hex  # throwaway password for users created here


class SmokeCheckError(Exception):
    pass


class Smoke:
    def __init__(self, base_url: str) -> None:
        self.http = httpx.Client(base_url=base_url, timeout=10.0)
        self.run = uuid.uuid4().hex[:6].upper()
        self.created_users: list[str] = []
        self.steps = 0

    def check(self, condition: bool, message: str, response: httpx.Response | None = None) -> None:
        if not condition:
            detail = (
                f" -> {response.status_code} {response.text[:300]}" if response is not None else ""
            )
            raise SmokeCheckError(f"{message}{detail}")
        self.steps += 1
        print(f"  ok  {message}")  # noqa: T201

    def call(
        self,
        method: str,
        path: str,
        expected: int,
        *,
        token: str | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        request_headers = dict(headers or {})
        if token:
            request_headers["Authorization"] = f"Bearer {token}"
        response = self.http.request(method, path, headers=request_headers, **kwargs)
        self.check(response.status_code == expected, f"{method} {path} -> {expected}", response)
        self.check(bool(response.headers.get("X-Request-ID")), f"{method} {path} has X-Request-ID")
        return response

    def login(self, email: str, password: str) -> dict[str, Any]:
        return dict(
            self.call(
                "POST", "/api/v1/auth/login", 200, json={"email": email, "password": password}
            ).json()
        )

    def create_user(self, admin: str, role: str) -> tuple[str, str]:
        email = f"smoke-{role}-{self.run.lower()}@example.com"
        user = self.call(
            "POST",
            "/api/v1/users",
            201,
            token=admin,
            json={"email": email, "full_name": f"Smoke {role}", "role": role, "password": PASSWORD},
        ).json()
        self.created_users.append(user["id"])
        return email, self.login(email, PASSWORD)["access_token"]

    def execute(self, admin_email: str, admin_password: str) -> None:
        live = self.call("GET", "/health/live", 200)
        self.check(live.json() == {"status": "ok"}, "liveness body")
        ready = self.call("GET", "/health/ready", 200)
        self.check(ready.json()["checks"] == {"database": "ok", "schema": "ok"}, "readiness checks")

        bad = self.http.post(
            "/api/v1/auth/login", json={"email": admin_email, "password": "definitely-wrong"}
        )
        self.check(
            bad.status_code == 401 and bad.json()["error"]["code"] == "INVALID_CREDENTIALS",
            "wrong password rejected with uniform error",
            bad,
        )
        admin_tokens = self.login(admin_email, admin_password)
        admin = admin_tokens["access_token"]
        me = self.call("GET", "/api/v1/auth/me", 200, token=admin).json()
        self.check(me["role"] == "admin", "bootstrap user is admin")

        _, manager = self.create_user(admin, "manager")
        _, operator = self.create_user(admin, "operator")
        _, viewer = self.create_user(admin, "viewer")

        forbidden = self.http.post(
            "/api/v1/products",
            json={"sku": "NOPE", "name": "x"},
            headers={"Authorization": f"Bearer {viewer}"},
        )
        self.check(forbidden.status_code == 403, "viewer cannot write catalog", forbidden)

        category = self.call(
            "POST", "/api/v1/categories", 201, token=manager, json={"name": f"Smoke {self.run}"}
        ).json()
        product = self.call(
            "POST",
            "/api/v1/products",
            201,
            token=manager,
            json={
                "sku": f"smk-{self.run}",
                "name": "Smoke widget",
                "category_id": category["id"],
                "reorder_level": 50,
            },
        ).json()
        self.check(product["sku"] == f"SMK-{self.run}", "SKU normalised to upper case")
        north = self.call(
            "POST",
            "/api/v1/warehouses",
            201,
            token=manager,
            json={"code": f"N{self.run}", "name": "N"},
        ).json()
        south = self.call(
            "POST",
            "/api/v1/warehouses",
            201,
            token=manager,
            json={"code": f"S{self.run}", "name": "S"},
        ).json()

        receipt_body = {
            "product_id": product["id"],
            "warehouse_id": north["id"],
            "movement_type": "receipt",
            "quantity": 100,
            "reference": f"PO-{self.run}",
        }
        key = {"Idempotency-Key": f"smoke-{self.run}-receipt"}
        first = self.call(
            "POST", "/api/v1/stock-movements", 201, token=operator, headers=key, json=receipt_body
        )
        retry = self.call(
            "POST", "/api/v1/stock-movements", 201, token=operator, headers=key, json=receipt_body
        )
        self.check(
            retry.headers.get("Idempotent-Replayed") == "true" and retry.json() == first.json(),
            "idempotent retry replays the original movement",
        )

        transfer = self.call(
            "POST",
            "/api/v1/stock-transfers",
            201,
            token=operator,
            json={
                "product_id": product["id"],
                "from_warehouse_id": north["id"],
                "to_warehouse_id": south["id"],
                "quantity": 30,
            },
        ).json()
        self.check(transfer["outbound"]["balance_after"] == 70, "transfer debits source")
        self.check(transfer["inbound"]["balance_after"] == 30, "transfer credits target")

        oversell = self.http.post(
            "/api/v1/stock-movements",
            json={**receipt_body, "movement_type": "issue", "quantity": 71},
            headers={"Authorization": f"Bearer {operator}"},
        )
        self.check(
            oversell.status_code == 409
            and oversell.json()["error"]["code"] == "INSUFFICIENT_STOCK"
            and oversell.json()["error"]["details"]["available"] == 70,
            "overselling is refused with available quantity",
            oversell,
        )
        self.call(
            "POST",
            "/api/v1/stock-movements",
            201,
            token=operator,
            json={**receipt_body, "movement_type": "issue", "quantity": 70},
        )

        stock = self.call(
            "GET", f"/api/v1/products/{product['id']}/stock", 200, token=viewer
        ).json()
        self.check(stock["total_quantity"] == 30, "product total is 30 after all movements")
        self.check(stock["below_reorder"] is True, "below reorder level flagged")
        low = self.call(
            "GET",
            "/api/v1/stock-levels",
            200,
            token=viewer,
            params={"below_reorder": "true", "product_id": product["id"]},
        ).json()
        self.check(
            low["total"] == 2
            and all(r["below_reorder"] and r["product_total_quantity"] == 30 for r in low["items"]),
            "stock-levels below_reorder is product-level (both warehouses flagged)",
        )
        ledger = self.call(
            "GET",
            "/api/v1/stock-movements",
            200,
            token=viewer,
            params={"product_id": product["id"], "limit": 2},
        ).json()
        self.check(len(ledger["items"]) == 2 and ledger["next_cursor"], "ledger page 1 + cursor")
        rest = self.call(
            "GET",
            "/api/v1/stock-movements",
            200,
            token=viewer,
            params={"product_id": product["id"], "limit": 10, "cursor": ledger["next_cursor"]},
        ).json()
        self.check(len(rest["items"]) == 2, "ledger has 4 movements in total")

        rotated = self.call(
            "POST",
            "/api/v1/auth/refresh",
            200,
            json={"refresh_token": admin_tokens["refresh_token"]},
        ).json()
        replay = self.http.post(
            "/api/v1/auth/refresh", json={"refresh_token": admin_tokens["refresh_token"]}
        )
        self.check(replay.status_code == 401, "rotated refresh token cannot be replayed", replay)
        revoked = self.http.post(
            "/api/v1/auth/refresh", json={"refresh_token": rotated["refresh_token"]}
        )
        self.check(revoked.status_code == 401, "replay revoked the whole session", revoked)

        audit = self.call(
            "GET",
            "/api/v1/audit-events",
            200,
            token=rotated["access_token"],  # access token itself is still valid
            params={"entity_type": "product", "entity_id": product["id"]},
        ).json()
        self.check(
            [e["action"] for e in audit["items"]] == ["product.created"], "catalog change audited"
        )

    def cleanup(self, admin_email: str, admin_password: str) -> None:
        if not self.created_users:
            return
        admin = self.login(admin_email, admin_password)
        for user_id in self.created_users:
            current = self.call(
                "GET", f"/api/v1/users/{user_id}", 200, token=admin["access_token"]
            ).json()
            self.call(
                "PATCH",
                f"/api/v1/users/{user_id}",
                200,
                token=admin["access_token"],
                json={"version": current["version"], "is_active": False},
            )
        self.call(
            "POST", "/api/v1/auth/logout", 204, json={"refresh_token": admin["refresh_token"]}
        )


def main() -> int:
    base_url = os.environ.get("SMOKE_BASE_URL", "http://127.0.0.1:8000")
    email = os.environ.get("SMOKE_ADMIN_EMAIL")
    password = os.environ.get("SMOKE_ADMIN_PASSWORD")
    if not email or not password:
        print("SMOKE_ADMIN_EMAIL and SMOKE_ADMIN_PASSWORD are required", file=sys.stderr)  # noqa: T201
        return 2
    smoke = Smoke(base_url)
    print(f"Smoke test against {base_url} (run {smoke.run})")  # noqa: T201
    try:
        smoke.execute(email, password)
        smoke.cleanup(email, password)
    except (SmokeCheckError, httpx.HTTPError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)  # noqa: T201
        return 1
    print(f"PASS ({smoke.steps} checks)")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
