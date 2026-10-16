from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import like_contains
from app.models.enums import Role
from app.models.user import RefreshToken, User
from app.repositories.base import paginate_scalars


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, user_id: uuid.UUID, *, for_update: bool = False) -> User | None:
        stmt = select(User).where(User.id == user_id)
        if for_update:
            stmt = stmt.with_for_update()
        return await self.session.scalar(stmt)

    async def get_by_email(self, email: str) -> User | None:
        return await self.session.scalar(select(User).where(User.email == email.lower()))

    async def search(
        self,
        *,
        q: str | None,
        role: Role | None,
        is_active: bool | None,
        limit: int,
        offset: int,
    ) -> tuple[list[User], int]:
        stmt = select(User).order_by(User.email)
        if q:
            pattern = like_contains(q)
            stmt = stmt.where(
                or_(
                    User.email.ilike(pattern, escape="\\"),
                    User.full_name.ilike(pattern, escape="\\"),
                )
            )
        if role is not None:
            stmt = stmt.where(User.role == role.value)
        if is_active is not None:
            stmt = stmt.where(User.is_active == is_active)
        return await paginate_scalars(self.session, stmt, limit=limit, offset=offset)

    async def lock_active_admin_ids(self) -> list[uuid.UUID]:
        """Lock every active admin row.

        Concurrent "demote/deactivate an admin" transactions serialize on these
        locks, so two admins cannot demote each other and leave none behind.
        """
        stmt = (
            select(User.id)
            .where(User.role == Role.ADMIN.value, User.is_active.is_(True))
            .order_by(User.id)
            .with_for_update()
        )
        return list((await self.session.scalars(stmt)).all())

    def add(self, user: User) -> None:
        self.session.add(user)


class RefreshTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, token: RefreshToken) -> None:
        self.session.add(token)

    async def get_by_hash_for_update(self, token_hash: str) -> RefreshToken | None:
        stmt = select(RefreshToken).where(RefreshToken.token_hash == token_hash).with_for_update()
        return await self.session.scalar(stmt)

    async def revoke_family(self, family_id: uuid.UUID, now: datetime) -> None:
        await self.session.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )

    async def revoke_all_for_user(self, user_id: uuid.UUID, now: datetime) -> None:
        await self.session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
