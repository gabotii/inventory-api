from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import NoReturn

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import (
    AuthenticationError,
    InvalidCredentialsError,
    InvalidRefreshTokenError,
    ValidationFailedError,
)
from app.core.security import (
    InvalidTokenError,
    PasswordHasher,
    TokenService,
    generate_refresh_token,
    hash_refresh_token,
)
from app.models.user import RefreshToken, User
from app.repositories.users import RefreshTokenRepository, UserRepository
from app.schemas.auth import TokenPair
from app.schemas.users import PasswordChange
from app.services.permissions import Actor, RequestMeta
from app.services.support import AuditService, RateLimiter, fingerprint


class AuthService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        hasher: PasswordHasher,
        tokens: TokenService,
        rate_limiter: RateLimiter,
    ) -> None:
        self._session = session
        self._settings = settings
        self._hasher = hasher
        self._tokens = tokens
        self._limiter = rate_limiter
        self._users = UserRepository(session)
        self._refresh_tokens = RefreshTokenRepository(session)
        self._audit = AuditService(session)

    # ------------------------------------------------------------- login
    async def login(self, email: str, password: str, meta: RequestMeta) -> TokenPair:
        settings = self._settings
        if meta.ip_address:
            await self._limiter.hit(
                f"login:ip:{meta.ip_address}",
                limit=settings.login_rate_limit_per_ip,
                window_seconds=settings.login_rate_limit_window_seconds,
            )
        await self._limiter.hit(
            f"login:email:{fingerprint(email)}",
            limit=settings.login_rate_limit_per_email,
            window_seconds=settings.login_rate_limit_window_seconds,
        )

        user = await self._users.get_by_email(email)
        if user is None:
            # Spend the same time as a real verification: no timing oracle for
            # "which emails exist".
            await self._hasher.verify_dummy(password)
            await self._login_failed(None, "unknown_email", meta)
        password_ok = await self._hasher.verify(user.password_hash, password)
        if not user.is_active:
            await self._login_failed(user, "inactive_user", meta)
        if not password_ok:
            await self._login_failed(user, "wrong_password", meta)

        now = datetime.now(UTC)
        values: dict[str, object] = {"last_login_at": now}
        if self._hasher.needs_rehash(user.password_hash):
            # Transparently upgrade hashes after Argon2 parameters are raised.
            values["password_hash"] = await self._hasher.hash(password)
        # Core UPDATE: bookkeeping must not bump the optimistic-lock version
        # (that would make concurrent admin edits fail spuriously).
        await self._session.execute(update(User).where(User.id == user.id).values(**values))
        pair = self._issue_tokens(user, family_id=uuid.uuid4(), now=now)
        self._audit.record(
            action="auth.login_succeeded",
            entity_type="user",
            entity_id=user.id,
            actor_id=user.id,
            ip_address=meta.ip_address,
        )
        await self._session.commit()
        return pair

    async def _login_failed(self, user: User | None, reason: str, meta: RequestMeta) -> NoReturn:
        self._audit.record(
            action="auth.login_failed",
            entity_type="user",
            entity_id=user.id if user else None,
            actor_id=None,
            ip_address=meta.ip_address,
            details={"reason": reason},
        )
        await self._session.commit()
        # Identical response for every failure reason (no account enumeration).
        raise InvalidCredentialsError()

    # ----------------------------------------------------------- refresh
    async def refresh(self, raw_token: str, meta: RequestMeta) -> TokenPair:
        if meta.ip_address:
            await self._limiter.hit(
                f"refresh:ip:{meta.ip_address}",
                limit=self._settings.refresh_rate_limit_per_ip,
                window_seconds=self._settings.refresh_rate_limit_window_seconds,
            )
        now = datetime.now(UTC)
        record = await self._refresh_tokens.get_by_hash_for_update(hash_refresh_token(raw_token))
        if record is None:
            raise InvalidRefreshTokenError()
        if record.revoked_at is not None:
            if record.replaced_by_id is not None:
                # A rotated token was presented again: either the client
                # retried, or it was stolen. Fail closed: kill the session.
                await self._refresh_tokens.revoke_family(record.family_id, now)
                self._audit.record(
                    action="auth.refresh_token_reused",
                    entity_type="user",
                    entity_id=record.user_id,
                    actor_id=None,
                    ip_address=meta.ip_address,
                    details={"family_id": str(record.family_id)},
                )
                await self._session.commit()
            raise InvalidRefreshTokenError()
        if record.expires_at <= now:
            raise InvalidRefreshTokenError()

        user = await self._users.get(record.user_id)
        if user is None or not user.is_active:
            await self._refresh_tokens.revoke_family(record.family_id, now)
            await self._session.commit()
            raise InvalidRefreshTokenError()

        pair = self._issue_tokens(user, family_id=record.family_id, now=now, replaces=record)
        await self._session.commit()
        return pair

    # ------------------------------------------------------------ logout
    async def logout(self, raw_token: str, meta: RequestMeta) -> None:
        record = await self._refresh_tokens.get_by_hash_for_update(hash_refresh_token(raw_token))
        if record is None or record.revoked_at is not None:
            return  # idempotent, and does not reveal whether the token existed
        await self._refresh_tokens.revoke_family(record.family_id, datetime.now(UTC))
        self._audit.record(
            action="auth.logout",
            entity_type="user",
            entity_id=record.user_id,
            actor_id=record.user_id,
            ip_address=meta.ip_address,
        )
        await self._session.commit()

    # --------------------------------------------------- change password
    async def change_password(self, actor: Actor, data: PasswordChange) -> None:
        await self._limiter.hit(
            f"password_change:user:{actor.id}",
            limit=self._settings.login_rate_limit_per_email,
            window_seconds=self._settings.login_rate_limit_window_seconds,
        )
        user = await self._users.get(actor.id, for_update=True)
        if user is None or not user.is_active:
            raise AuthenticationError()
        if not await self._hasher.verify(user.password_hash, data.current_password):
            raise ValidationFailedError("current_password", "Current password is incorrect")
        if data.new_password == data.current_password:
            raise ValidationFailedError(
                "new_password", "New password must differ from the current one"
            )
        user.password_hash = await self._hasher.hash(data.new_password)
        user.token_version += 1  # invalidates every outstanding access token
        await self._refresh_tokens.revoke_all_for_user(user.id, datetime.now(UTC))
        self._audit.record(
            action="user.password_changed",
            entity_type="user",
            entity_id=user.id,
            actor_id=user.id,
            ip_address=actor.meta.ip_address,
        )
        await self._session.commit()

    # ---------------------------------------------------- authentication
    async def authenticate(self, access_token: str) -> User:
        try:
            claims = self._tokens.decode_access_token(access_token)
        except InvalidTokenError as exc:
            raise AuthenticationError("Invalid or expired access token") from exc
        user = await self._users.get(claims.user_id)
        if user is None or not user.is_active or user.token_version != claims.token_version:
            raise AuthenticationError("Invalid or expired access token")
        return user

    # ----------------------------------------------------------- helpers
    def _issue_tokens(
        self,
        user: User,
        *,
        family_id: uuid.UUID,
        now: datetime,
        replaces: RefreshToken | None = None,
    ) -> TokenPair:
        raw_refresh = generate_refresh_token()
        new_record = RefreshToken(
            id=uuid.uuid4(),
            user_id=user.id,
            family_id=family_id,
            token_hash=hash_refresh_token(raw_refresh),
            created_at=now,
            expires_at=now + self._tokens.refresh_ttl,
        )
        self._refresh_tokens.add(new_record)
        if replaces is not None:
            replaces.revoked_at = now
            replaces.replaced_by_id = new_record.id
        return TokenPair(
            access_token=self._tokens.create_access_token(user.id, user.token_version, now=now),
            refresh_token=raw_refresh,
            expires_in=int(self._tokens.access_ttl.total_seconds()),
            refresh_expires_in=int(self._tokens.refresh_ttl.total_seconds()),
        )
