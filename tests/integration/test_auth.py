from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy import select

from app.api.deps import AppState
from app.models.enums import Role
from app.models.support import AuditEvent
from tests.conftest import make_settings
from tests.integration.conftest import PASSWORD, UserFactory

LOGIN = "/api/v1/auth/login"


async def login(client: httpx.AsyncClient, email: str, password: str = PASSWORD) -> httpx.Response:
    return await client.post(LOGIN, json={"email": email, "password": password})


async def tokens_for(client: httpx.AsyncClient, email: str) -> dict[str, Any]:
    response = await login(client, email)
    assert response.status_code == 200, response.text
    return dict(response.json())


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_login_returns_token_pair_usable_on_me(
    client: httpx.AsyncClient, users: UserFactory
) -> None:
    user = await users.create(Role.OPERATOR, email="ops@example.com")
    body = await tokens_for(client, "OPS@Example.com")  # emails are case-insensitive
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 900
    me = await client.get("/api/v1/auth/me", headers=bearer(body["access_token"]))
    assert me.status_code == 200
    assert me.json()["id"] == str(user.id)
    assert me.json()["role"] == "operator"
    assert "password_hash" not in me.text


async def test_login_failures_are_indistinguishable(
    client: httpx.AsyncClient, users: UserFactory
) -> None:
    await users.create(email="known@example.com")
    await users.create(email="disabled@example.com", is_active=False)
    responses = [
        await login(client, "known@example.com", "wrong-password-1"),
        await login(client, "unknown@example.com"),
        await login(client, "disabled@example.com"),  # correct password, inactive
    ]
    for response in responses:
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"
        assert response.json()["error"]["message"] == "Invalid email or password"


async def test_failed_logins_are_audited(
    client: httpx.AsyncClient, users: UserFactory, state: AppState
) -> None:
    user = await users.create(email="audited@example.com")
    await login(client, "audited@example.com", "wrong-password-1")
    async with state.sessionmaker() as session:
        events = (await session.scalars(select(AuditEvent))).all()
    assert [(e.action, e.entity_id, e.details) for e in events] == [
        ("auth.login_failed", str(user.id), {"reason": "wrong_password"})
    ]
    assert events[0].ip_address == "127.0.0.1"


async def test_validation_errors_never_echo_input(client: httpx.AsyncClient) -> None:
    response = await client.post(LOGIN, json={"email": "nope", "password": "SuperSecret!" * 20})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "SuperSecret" not in response.text
    fields = {error["field"] for error in response.json()["error"]["details"]["errors"]}
    assert fields == {"body.email", "body.password"}


async def test_protected_routes_require_bearer_token(client: httpx.AsyncClient) -> None:
    for headers in [{}, {"Authorization": "Basic Zm9vOmJhcg=="}, bearer("not.a.jwt")]:
        response = await client.get("/api/v1/auth/me", headers=headers)
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert response.json()["error"]["code"] == "UNAUTHENTICATED"


async def test_refresh_rotates_tokens(client: httpx.AsyncClient, users: UserFactory) -> None:
    await users.create(email="rot@example.com")
    first = await tokens_for(client, "rot@example.com")
    rotated = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]}
    )
    assert rotated.status_code == 200
    second = rotated.json()
    assert second["refresh_token"] != first["refresh_token"]
    me = await client.get("/api/v1/auth/me", headers=bearer(second["access_token"]))
    assert me.status_code == 200


async def test_refresh_token_reuse_revokes_whole_session(
    client: httpx.AsyncClient, users: UserFactory
) -> None:
    await users.create(email="theft@example.com")
    first = await tokens_for(client, "theft@example.com")
    second = (
        await client.post("/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    ).json()

    # An attacker replays the already-rotated token...
    replay = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]}
    )
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "INVALID_REFRESH_TOKEN"
    # ...which also kills the legitimate client's current refresh token.
    legit = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": second["refresh_token"]}
    )
    assert legit.status_code == 401


async def test_other_sessions_survive_reuse_detection(
    client: httpx.AsyncClient, users: UserFactory
) -> None:
    await users.create(email="multi@example.com")
    laptop = await tokens_for(client, "multi@example.com")
    phone = await tokens_for(client, "multi@example.com")
    await client.post("/api/v1/auth/refresh", json={"refresh_token": laptop["refresh_token"]})
    await client.post("/api/v1/auth/refresh", json={"refresh_token": laptop["refresh_token"]})
    still_ok = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": phone["refresh_token"]}
    )
    assert still_ok.status_code == 200


async def test_logout_revokes_refresh_token_and_is_idempotent(
    client: httpx.AsyncClient, users: UserFactory
) -> None:
    await users.create(email="bye@example.com")
    pair = await tokens_for(client, "bye@example.com")
    for _ in range(2):
        response = await client.post(
            "/api/v1/auth/logout", json={"refresh_token": pair["refresh_token"]}
        )
        assert response.status_code == 204
    unknown = await client.post("/api/v1/auth/logout", json={"refresh_token": "x" * 43})
    assert unknown.status_code == 204
    refresh = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]}
    )
    assert refresh.status_code == 401


async def test_change_password_invalidates_existing_tokens(
    client: httpx.AsyncClient, users: UserFactory
) -> None:
    await users.create(email="pw@example.com")
    pair = await tokens_for(client, "pw@example.com")
    headers = bearer(pair["access_token"])

    wrong = await client.post(
        "/api/v1/auth/password",
        json={"current_password": "not-the-password", "new_password": "a-brand-new-password"},
        headers=headers,
    )
    assert wrong.status_code == 422
    assert wrong.json()["error"]["details"]["errors"][0]["field"] == "current_password"

    same = await client.post(
        "/api/v1/auth/password",
        json={"current_password": PASSWORD, "new_password": PASSWORD},
        headers=headers,
    )
    assert same.status_code == 422

    changed = await client.post(
        "/api/v1/auth/password",
        json={"current_password": PASSWORD, "new_password": "a-brand-new-password"},
        headers=headers,
    )
    assert changed.status_code == 204
    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 401
    refresh = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]}
    )
    assert refresh.status_code == 401
    assert (await login(client, "pw@example.com")).status_code == 401
    assert (await login(client, "pw@example.com", "a-brand-new-password")).status_code == 200


async def test_login_is_rate_limited_per_email(
    client: httpx.AsyncClient, users: UserFactory
) -> None:
    await users.create(email="brute@example.com")
    limit = make_settings().login_rate_limit_per_email
    for _ in range(limit):
        response = await login(client, "brute@example.com", "wrong-password-1")
        assert response.status_code == 401
    blocked = await login(client, "brute@example.com")  # even the right password
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "RATE_LIMITED"
    assert int(blocked.headers["Retry-After"]) >= 1
    # Other accounts from a different IP are unaffected.
    await users.create(email="other@example.com")
    other_ip = httpx.ASGITransport(app=client._transport.app, client=("10.0.0.9", 1))  # type: ignore[attr-defined]
    async with httpx.AsyncClient(transport=other_ip, base_url="http://test") as second:
        assert (await login(second, "other@example.com")).status_code == 200


async def test_login_is_rate_limited_per_ip(client: httpx.AsyncClient, users: UserFactory) -> None:
    limit = make_settings().login_rate_limit_per_ip
    for attempt in range(limit):
        response = await login(client, f"spray{attempt}@example.com")
        assert response.status_code == 401
    assert (await login(client, "spray-final@example.com")).status_code == 429
