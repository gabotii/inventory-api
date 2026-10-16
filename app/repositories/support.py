from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.support import AuditEvent, IdempotencyKey, RateLimitCounter
from app.models.user import RefreshToken


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, event: AuditEvent) -> None:
        self.session.add(event)

    async def search(
        self,
        *,
        actor_id: uuid.UUID | None,
        action: str | None,
        entity_type: str | None,
        entity_id: str | None,
        before_id: int | None,
        limit: int,
    ) -> list[AuditEvent]:
        stmt = select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit)
        if actor_id is not None:
            stmt = stmt.where(AuditEvent.actor_id == actor_id)
        if action is not None:
            stmt = stmt.where(AuditEvent.action == action)
        if entity_type is not None:
            stmt = stmt.where(AuditEvent.entity_type == entity_type)
        if entity_id is not None:
            stmt = stmt.where(AuditEvent.entity_id == entity_id)
        if before_id is not None:
            stmt = stmt.where(AuditEvent.id < before_id)
        return list((await self.session.scalars(stmt)).all())


class IdempotencyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def try_claim(
        self, user_id: uuid.UUID, key: str, endpoint: str, request_hash: str
    ) -> bool:
        """Insert the key; return False if it already exists.

        If another transaction holds an uncommitted claim on the same key,
        PostgreSQL makes this statement wait for it to finish, so concurrent
        duplicates are serialized rather than executed twice.
        """
        stmt = (
            insert(IdempotencyKey)
            .values(user_id=user_id, key=key, endpoint=endpoint, request_hash=request_hash)
            .on_conflict_do_nothing(index_elements=[IdempotencyKey.user_id, IdempotencyKey.key])
            .returning(IdempotencyKey.key)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none() is not None

    async def get(self, user_id: uuid.UUID, key: str) -> IdempotencyKey | None:
        return await self.session.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.user_id == user_id, IdempotencyKey.key == key
            )
        )

    async def store_response(
        self, user_id: uuid.UUID, key: str, status: int, body: dict[str, Any]
    ) -> None:
        await self.session.execute(
            update(IdempotencyKey)
            .where(IdempotencyKey.user_id == user_id, IdempotencyKey.key == key)
            .values(response_status=status, response_body=body)
        )


class RateLimitRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def increment(self, key: str, window_start: datetime) -> int:
        stmt = (
            insert(RateLimitCounter)
            .values(key=key, window_start=window_start, count=1)
            .on_conflict_do_update(
                index_elements=[RateLimitCounter.key, RateLimitCounter.window_start],
                set_={"count": RateLimitCounter.count + 1},
            )
            .returning(RateLimitCounter.count)
        )
        return int((await self.session.execute(stmt)).scalar_one())


class MaintenanceRepository:
    """Retention clean-up for tables that would otherwise grow without bound."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def purge(
        self,
        *,
        tokens_expired_before: datetime,
        keys_created_before: datetime,
        windows_before: datetime,
    ) -> dict[str, int]:
        tokens = await self.session.execute(
            delete(RefreshToken).where(RefreshToken.expires_at < tokens_expired_before)
        )
        keys = await self.session.execute(
            delete(IdempotencyKey).where(IdempotencyKey.created_at < keys_created_before)
        )
        windows = await self.session.execute(
            delete(RateLimitCounter).where(RateLimitCounter.window_start < windows_before)
        )
        return {
            "refresh_tokens": _rowcount(tokens),
            "idempotency_keys": _rowcount(keys),
            "rate_limit_counters": _rowcount(windows),
        }


def _rowcount(result: object) -> int:
    return int(getattr(result, "rowcount", 0) or 0)
