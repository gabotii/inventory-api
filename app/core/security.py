"""Password hashing, access tokens (JWT) and opaque refresh tokens."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import anyio
import jwt
from argon2 import PasswordHasher as _Argon2Hasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import Settings

ACCESS_TOKEN_TYPE = "access"  # noqa: S105 - token type claim, not a secret
_JWT_ALGORITHM = "HS256"
_JWT_LEEWAY_SECONDS = 10
_REFRESH_TOKEN_BYTES = 32


class PasswordHasher:
    """Argon2id hashing executed in worker threads with bounded concurrency.

    Argon2 is deliberately CPU- and memory-hard; running it on the event loop
    would stall every other request, and running unbounded copies in threads
    would let a login flood exhaust memory (64 MiB per hash by default).
    """

    def __init__(self, settings: Settings) -> None:
        self._hasher = _Argon2Hasher(
            time_cost=settings.argon2_time_cost,
            memory_cost=settings.argon2_memory_cost_kib,
            parallelism=settings.argon2_parallelism,
        )
        self._limiter = anyio.CapacityLimiter(settings.password_hash_concurrency)
        # Used to spend comparable time when the user does not exist, so
        # response timing does not reveal which emails are registered.
        self._dummy_hash = self._hasher.hash(secrets.token_urlsafe(16))

    async def hash(self, password: str) -> str:
        return await anyio.to_thread.run_sync(self._hasher.hash, password, limiter=self._limiter)

    async def verify(self, password_hash: str, password: str) -> bool:
        return await anyio.to_thread.run_sync(
            self._verify_sync, password_hash, password, limiter=self._limiter
        )

    async def verify_dummy(self, password: str) -> None:
        await self.verify(self._dummy_hash, password)

    def needs_rehash(self, password_hash: str) -> bool:
        return self._hasher.check_needs_rehash(password_hash)

    def _verify_sync(self, password_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    user_id: uuid.UUID
    token_version: int
    expires_at: datetime


class InvalidTokenError(Exception):
    """Raised for any access token that must not be accepted."""


class TokenService:
    def __init__(self, settings: Settings) -> None:
        self._secret = settings.jwt_secret.get_secret_value()
        self._issuer = settings.jwt_issuer
        self._audience = settings.jwt_audience
        self.access_ttl = timedelta(seconds=settings.access_token_ttl_seconds)
        self.refresh_ttl = timedelta(seconds=settings.refresh_token_ttl_seconds)

    def create_access_token(
        self, user_id: uuid.UUID, token_version: int, *, now: datetime | None = None
    ) -> str:
        issued_at = now or datetime.now(UTC)
        claims = {
            "sub": str(user_id),
            "tv": token_version,
            "typ": ACCESS_TOKEN_TYPE,
            "iss": self._issuer,
            "aud": self._audience,
            "iat": issued_at,
            "nbf": issued_at,
            "exp": issued_at + self.access_ttl,
            "jti": uuid.uuid4().hex,
        }
        return jwt.encode(claims, self._secret, algorithm=_JWT_ALGORITHM)

    def decode_access_token(self, token: str) -> AccessTokenClaims:
        try:
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=[_JWT_ALGORITHM],  # pinned: rejects "none" and alg confusion
                audience=self._audience,
                issuer=self._issuer,
                leeway=_JWT_LEEWAY_SECONDS,
                options={"require": ["sub", "exp", "iat", "iss", "aud", "typ", "tv"]},
            )
        except jwt.PyJWTError as exc:
            raise InvalidTokenError(str(exc)) from exc
        if claims.get("typ") != ACCESS_TOKEN_TYPE:
            raise InvalidTokenError("wrong token type")
        token_version = claims.get("tv")
        if not isinstance(token_version, int) or isinstance(token_version, bool):
            raise InvalidTokenError("invalid token version claim")
        try:
            user_id = uuid.UUID(str(claims["sub"]))
        except ValueError as exc:
            raise InvalidTokenError("invalid subject") from exc
        return AccessTokenClaims(
            user_id=user_id,
            token_version=token_version,
            expires_at=datetime.fromtimestamp(int(claims["exp"]), tz=UTC),
        )


def generate_refresh_token() -> str:
    return secrets.token_urlsafe(_REFRESH_TOKEN_BYTES)


def hash_refresh_token(token: str) -> str:
    """Refresh tokens are 256-bit random values, so a fast hash is sufficient.

    Only the hash is stored: a database leak does not yield usable tokens.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
