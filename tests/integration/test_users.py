from __future__ import annotations

import asyncio

import httpx
import pytest

from app.models.enums import Role
from tests.integration.conftest import PASSWORD, UserFactory

USERS = "/api/v1/users"


async def test_admin_creates_user_who_can_log_in(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    response = await client.post(
        USERS,
        json={
            "email": "New@Example.com",
            "full_name": "New",
            "role": "operator",
            "password": PASSWORD,
        },
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["email"] == "new@example.com"
    assert created["role"] == "operator"
    assert created["version"] == 1
    assert "password" not in response.text
    login = await client.post(
        "/api/v1/auth/login", json={"email": "new@example.com", "password": PASSWORD}
    )
    assert login.status_code == 200


async def test_duplicate_email_is_conflict_case_insensitively(
    client: httpx.AsyncClient, admin_headers: dict[str, str], users: UserFactory
) -> None:
    await users.create(email="dup@example.com")
    response = await client.post(
        USERS,
        json={"email": "DUP@example.com", "full_name": "Dup", "password": PASSWORD},
        headers=admin_headers,
    )
    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": "ALREADY_EXISTS",
        "message": "A user with this email already exists",
        "details": {"field": "email"},
        "request_id": response.headers["X-Request-ID"],
    }


@pytest.mark.parametrize("role", [Role.VIEWER, Role.OPERATOR, Role.MANAGER])
async def test_only_admins_manage_users(
    client: httpx.AsyncClient, users: UserFactory, role: Role
) -> None:
    headers = await users.headers_for(role)
    target = await users.create()
    requests = [
        client.get(USERS, headers=headers),
        client.get(f"{USERS}/{target.id}", headers=headers),
        client.post(
            USERS,
            json={"email": "x@example.com", "full_name": "X", "password": PASSWORD},
            headers=headers,
        ),
        client.patch(f"{USERS}/{target.id}", json={"version": 1, "role": "admin"}, headers=headers),
        client.put(
            f"{USERS}/{target.id}/password", json={"new_password": PASSWORD}, headers=headers
        ),
    ]
    for response in await asyncio.gather(*requests):
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "FORBIDDEN"


async def test_list_users_filters_and_paginates(
    client: httpx.AsyncClient, admin_headers: dict[str, str], users: UserFactory
) -> None:
    for index in range(3):
        await users.create(Role.OPERATOR, email=f"op{index}@example.com")
    await users.create(Role.VIEWER, email="viewer@example.com", is_active=False)

    page = await client.get(USERS, params={"role": "operator", "limit": 2}, headers=admin_headers)
    assert page.status_code == 200
    body = page.json()
    assert body["total"] == 3
    assert [u["email"] for u in body["items"]] == ["op0@example.com", "op1@example.com"]

    inactive = await client.get(USERS, params={"is_active": "false"}, headers=admin_headers)
    assert [u["email"] for u in inactive.json()["items"]] == ["viewer@example.com"]

    too_big = await client.get(USERS, params={"limit": 1000}, headers=admin_headers)
    assert too_big.status_code == 422


async def test_role_change_takes_effect_immediately(
    client: httpx.AsyncClient, admin_headers: dict[str, str], users: UserFactory
) -> None:
    target = await users.create(Role.MANAGER)
    old_headers = users.headers(target)
    assert (await client.get("/api/v1/auth/me", headers=old_headers)).status_code == 200

    response = await client.patch(
        f"{USERS}/{target.id}", json={"version": 1, "role": "viewer"}, headers=admin_headers
    )
    assert response.status_code == 200
    assert response.json()["role"] == "viewer"
    assert response.json()["version"] == 2
    # Tokens minted with the old role are no longer accepted.
    assert (await client.get("/api/v1/auth/me", headers=old_headers)).status_code == 401


async def test_deactivated_user_is_locked_out(
    client: httpx.AsyncClient, admin_headers: dict[str, str], users: UserFactory
) -> None:
    target = await users.create(email="leaver@example.com")
    headers = users.headers(target)
    response = await client.patch(
        f"{USERS}/{target.id}", json={"version": 1, "is_active": False}, headers=admin_headers
    )
    assert response.status_code == 200
    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 401
    login = await client.post(
        "/api/v1/auth/login", json={"email": "leaver@example.com", "password": PASSWORD}
    )
    assert login.status_code == 401


async def test_stale_version_is_rejected(
    client: httpx.AsyncClient, admin_headers: dict[str, str], users: UserFactory
) -> None:
    target = await users.create()
    first = await client.patch(
        f"{USERS}/{target.id}", json={"version": 1, "full_name": "First"}, headers=admin_headers
    )
    assert first.status_code == 200
    stale = await client.patch(
        f"{USERS}/{target.id}", json={"version": 1, "full_name": "Second"}, headers=admin_headers
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "VERSION_CONFLICT"
    assert stale.json()["error"]["details"]["current_version"] == 2


async def test_last_admin_cannot_be_demoted_or_deactivated(
    client: httpx.AsyncClient, users: UserFactory
) -> None:
    admin = await users.create(Role.ADMIN)
    headers = users.headers(admin)
    for change in ({"role": "manager"}, {"is_active": False}):
        response = await client.patch(
            f"{USERS}/{admin.id}", json={"version": 1, **change}, headers=headers
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "LAST_ADMIN"

    other = await users.create(Role.ADMIN)
    response = await client.patch(
        f"{USERS}/{admin.id}", json={"version": 1, "role": "manager"}, headers=users.headers(other)
    )
    assert response.status_code == 200


async def test_two_admins_cannot_demote_each_other_concurrently(
    client: httpx.AsyncClient, users: UserFactory
) -> None:
    first, second = await users.create(Role.ADMIN), await users.create(Role.ADMIN)
    results = await asyncio.gather(
        client.patch(
            f"{USERS}/{second.id}",
            json={"version": 1, "role": "viewer"},
            headers=users.headers(first),
        ),
        client.patch(
            f"{USERS}/{first.id}",
            json={"version": 1, "role": "viewer"},
            headers=users.headers(second),
        ),
    )
    statuses = sorted(response.status_code for response in results)
    # One demotion wins. The other is refused: either LAST_ADMIN, or 401 because
    # the winner just demoted the caller (their token was revoked).
    assert statuses[0] == 200
    assert statuses[1] in {401, 409}
    remaining = await client.get(
        USERS, params={"role": "admin", "is_active": "true"}, headers=users.headers(first)
    )
    admins_left = 1 if remaining.status_code == 401 else remaining.json()["total"]
    assert admins_left == 1


async def test_admin_password_reset(
    client: httpx.AsyncClient, admin_headers: dict[str, str], users: UserFactory
) -> None:
    target = await users.create(email="forgot@example.com")
    old_headers = users.headers(target)
    response = await client.put(
        f"{USERS}/{target.id}/password",
        json={"new_password": "temporary-pass-123"},
        headers=admin_headers,
    )
    assert response.status_code == 204
    assert (await client.get("/api/v1/auth/me", headers=old_headers)).status_code == 401
    login = await client.post(
        "/api/v1/auth/login", json={"email": "forgot@example.com", "password": "temporary-pass-123"}
    )
    assert login.status_code == 200


async def test_unknown_user_is_404(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    response = await client.get(
        f"{USERS}/00000000-0000-0000-0000-000000000000", headers=admin_headers
    )
    assert response.status_code == 404
    assert response.json()["error"]["details"] == {"resource": "User"}
    malformed = await client.get(f"{USERS}/not-a-uuid", headers=admin_headers)
    assert malformed.status_code == 422
