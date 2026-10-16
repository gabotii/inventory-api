from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from app.core.errors import AlreadyExistsError, LastAdminError, NotFoundError, VersionConflictError
from app.core.security import PasswordHasher
from app.db.errors import UNIQUE_VIOLATION, sqlstate
from app.models.enums import Role
from app.models.user import User
from app.repositories.users import RefreshTokenRepository, UserRepository
from app.schemas.users import UserCreate, UserUpdate
from app.services.permissions import Actor, Permission, authorize
from app.services.support import AuditService


class UserService:
    def __init__(self, session: AsyncSession, hasher: PasswordHasher) -> None:
        self._session = session
        self._hasher = hasher
        self._users = UserRepository(session)
        self._refresh_tokens = RefreshTokenRepository(session)
        self._audit = AuditService(session)

    async def list_users(
        self,
        actor: Actor,
        *,
        q: str | None,
        role: Role | None,
        is_active: bool | None,
        limit: int,
        offset: int,
    ) -> tuple[list[User], int]:
        authorize(actor, Permission.MANAGE_USERS)
        return await self._users.search(
            q=q, role=role, is_active=is_active, limit=limit, offset=offset
        )

    async def get(self, actor: Actor, user_id: uuid.UUID) -> User:
        authorize(actor, Permission.MANAGE_USERS)
        return await self._get_or_404(user_id)

    async def create(self, actor: Actor, data: UserCreate) -> User:
        authorize(actor, Permission.MANAGE_USERS)
        user = User(
            email=data.email,
            full_name=data.full_name,
            role=data.role.value,
            password_hash=await self._hasher.hash(data.password),
            is_active=True,
            token_version=0,
        )
        self._users.add(user)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            if sqlstate(exc) == UNIQUE_VIOLATION:
                raise AlreadyExistsError(
                    "A user with this email already exists", details={"field": "email"}
                ) from exc
            raise
        self._audit.record(
            action="user.created",
            entity_type="user",
            entity_id=user.id,
            actor_id=actor.id,
            ip_address=actor.meta.ip_address,
            details={"role": user.role},
        )
        await self._session.commit()
        return user

    async def update(self, actor: Actor, user_id: uuid.UUID, data: UserUpdate) -> User:
        authorize(actor, Permission.MANAGE_USERS)
        changes = data.changes()
        security_relevant = "role" in changes or "is_active" in changes
        # Lock order matters to avoid deadlocks: admins first (sorted by id),
        # then the target row (already locked if it is an admin).
        active_admins = await self._users.lock_active_admin_ids() if security_relevant else []
        user = await self._get_or_404(user_id, for_update=True)
        if user.version != data.version:
            raise VersionConflictError(details={"current_version": user.version})

        loses_admin = (
            user.role == Role.ADMIN.value
            and user.is_active
            and (
                ("role" in changes and changes["role"] is not Role.ADMIN)
                or changes.get("is_active") is False
            )
        )
        if loses_admin and not [admin for admin in active_admins if admin != user.id]:
            raise LastAdminError()

        applied: dict[str, object] = {}
        for field, value in changes.items():
            stored = value.value if isinstance(value, Role) else value
            if getattr(user, field) != stored:
                setattr(user, field, stored)
                applied[field] = stored
        if not applied:
            return user

        if "role" in applied or "is_active" in applied:
            # Permissions changed: outstanding tokens must not keep the old ones.
            user.token_version += 1
            await self._refresh_tokens.revoke_all_for_user(user.id, datetime.now(UTC))
        try:
            await self._session.flush()
        except StaleDataError as exc:
            raise VersionConflictError() from exc
        self._audit.record(
            action="user.updated",
            entity_type="user",
            entity_id=user.id,
            actor_id=actor.id,
            ip_address=actor.meta.ip_address,
            details={"changes": applied},
        )
        await self._session.commit()
        return user

    async def reset_password(self, actor: Actor, user_id: uuid.UUID, new_password: str) -> None:
        authorize(actor, Permission.MANAGE_USERS)
        user = await self._get_or_404(user_id, for_update=True)
        user.password_hash = await self._hasher.hash(new_password)
        user.token_version += 1
        await self._refresh_tokens.revoke_all_for_user(user.id, datetime.now(UTC))
        self._audit.record(
            action="user.password_reset",
            entity_type="user",
            entity_id=user.id,
            actor_id=actor.id,
            ip_address=actor.meta.ip_address,
        )
        await self._session.commit()

    async def _get_or_404(self, user_id: uuid.UUID, *, for_update: bool = False) -> User:
        user = await self._users.get(user_id, for_update=for_update)
        if user is None:
            raise NotFoundError("User")
        return user
