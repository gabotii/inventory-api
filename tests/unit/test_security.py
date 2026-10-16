from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.security import (
    InvalidTokenError,
    PasswordHasher,
    TokenService,
    generate_refresh_token,
    hash_refresh_token,
)
from tests.conftest import TEST_JWT_SECRET, make_settings


@pytest.fixture(scope="module")
def tokens() -> TokenService:
    return TokenService(make_settings())


@pytest.fixture(scope="module")
def hasher() -> PasswordHasher:
    return PasswordHasher(make_settings())


async def test_password_hash_roundtrip_uses_argon2id(hasher: PasswordHasher) -> None:
    hashed = await hasher.hash("s3cret-passphrase")
    assert hashed.startswith("$argon2id$")
    assert await hasher.verify(hashed, "s3cret-passphrase")
    assert not await hasher.verify(hashed, "wrong-passphrase")


async def test_verify_tolerates_garbage_hash(hasher: PasswordHasher) -> None:
    assert not await hasher.verify("not-a-hash", "anything")


async def test_same_password_hashes_differently(hasher: PasswordHasher) -> None:
    assert await hasher.hash("same") != await hasher.hash("same")


def test_needs_rehash_detects_weaker_parameters() -> None:
    weak = PasswordHasher(make_settings())
    strong = PasswordHasher(make_settings(argon2_time_cost=3, argon2_memory_cost_kib=1024))
    weak_hash = weak._hasher.hash("pw")
    assert strong.needs_rehash(weak_hash)
    assert not weak.needs_rehash(weak_hash)


def test_access_token_roundtrip(tokens: TokenService) -> None:
    user_id = uuid.uuid4()
    claims = tokens.decode_access_token(tokens.create_access_token(user_id, 7))
    assert claims.user_id == user_id
    assert claims.token_version == 7


def test_expired_token_rejected(tokens: TokenService) -> None:
    old = datetime.now(UTC) - timedelta(hours=2)
    token = tokens.create_access_token(uuid.uuid4(), 0, now=old)
    with pytest.raises(InvalidTokenError):
        tokens.decode_access_token(token)


def test_tampered_signature_rejected(tokens: TokenService) -> None:
    token = tokens.create_access_token(uuid.uuid4(), 0)
    header, payload, signature = token.split(".")
    forged = f"{header}.{payload}.{signature[::-1]}"
    with pytest.raises(InvalidTokenError):
        tokens.decode_access_token(forged)


def _claims(**overrides: object) -> dict[str, object]:
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "sub": str(uuid.uuid4()),
        "tv": 0,
        "typ": "access",
        "iss": "inventory-api",
        "aud": "inventory-api",
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    claims.update(overrides)
    return claims


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"aud": "other-service"}, "wrong audience"),
        ({"iss": "someone-else"}, "wrong issuer"),
        ({"typ": "refresh"}, "wrong token type"),
        ({"tv": "0"}, "non-integer token version"),
        ({"tv": True}, "boolean token version"),
        ({"sub": "not-a-uuid"}, "invalid subject"),
    ],
)
def test_invalid_claims_rejected(
    tokens: TokenService, overrides: dict[str, object], reason: str
) -> None:
    token = jwt.encode(_claims(**overrides), TEST_JWT_SECRET, algorithm="HS256")
    with pytest.raises(InvalidTokenError):
        tokens.decode_access_token(token)


def test_missing_token_version_rejected(tokens: TokenService) -> None:
    claims = _claims()
    del claims["tv"]
    with pytest.raises(InvalidTokenError):
        tokens.decode_access_token(jwt.encode(claims, TEST_JWT_SECRET, algorithm="HS256"))


def test_alg_none_rejected(tokens: TokenService) -> None:
    unsigned = jwt.encode(_claims(), key=None, algorithm="none")
    with pytest.raises(InvalidTokenError):
        tokens.decode_access_token(unsigned)


def test_token_signed_with_other_secret_rejected(tokens: TokenService) -> None:
    token = jwt.encode(_claims(), "another-secret-" + "y" * 40, algorithm="HS256")
    with pytest.raises(InvalidTokenError):
        tokens.decode_access_token(token)


def test_refresh_tokens_are_random_and_hashed() -> None:
    first, second = generate_refresh_token(), generate_refresh_token()
    assert first != second
    assert len(first) >= 43
    assert hash_refresh_token(first) == hash_refresh_token(first)
    assert hash_refresh_token(first) != hash_refresh_token(second)
    assert first not in hash_refresh_token(first)
