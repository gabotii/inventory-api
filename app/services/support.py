"""Cross-cutting services: audit trail, rate limiting and idempotency."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.context import get_request_id
from app.core.errors import ConflictError, IdempotencyKeyReusedError, RateLimitedError
from app.core.pagination import decode_cursor, encode_cursor
from app.models.support import AuditEvent
from app.repositories.support import AuditRepository, IdempotencyRepository, RateLimitRepository
from app.services.permissions import Actor, Permission, authorize

logger = logging.getLogger(__name__)


class AuditService:
    """Writes audit events in the caller's transaction (atomic with the change)."""

    def __init__(self, session: AsyncSession) -> None:
        self._repo = AuditRepository(session)

    def record(
        self,
        *,
        action: str,
        entity_type: str,
        entity_id: object | None,
        actor_id: uuid.UUID | None,
        ip_address: str | None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self._repo.add(
            AuditEvent(
                action=action,
                entity_type=entity_type,
                entity_id=str(entity_id) if entity_id is not None else None,
                actor_id=actor_id,
                ip_address=ip_address,
                request_id=get_request_id(),
                # JSON round-trip: UUIDs/datetimes/enums anywhere in the payload
                # become strings, so JSONB serialization can never fail.
                details=json.loads(json.dumps(details or {}, default=str)),
            )
        )
        logger.info(
            "audit event",
            extra={"audit_action": action, "entity_type": entity_type, "entity_id": str(entity_id)},
        )


class AuditQueryService:
    def __init__(self, session: AsyncSession) -> None:
        self._repo = AuditRepository(session)

    async def list_events(
        self,
        actor: Actor,
        *,
        actor_id: uuid.UUID | None,
        action: str | None,
        entity_type: str | None,
        entity_id: str | None,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[AuditEvent], str | None]:
        authorize(actor, Permission.READ_AUDIT)
        rows = await self._repo.search(
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            before_id=decode_cursor(cursor) if cursor else None,
            limit=limit + 1,
        )
        next_cursor = encode_cursor(rows[limit - 1].id) if len(rows) > limit else None
        return rows[:limit], next_cursor


class RateLimiter:
    """Fixed-window counters stored in PostgreSQL.

    Shared by every API replica without extra infrastructure. Each hit commits
    in its own short transaction so failed requests still count.
    """

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], *, enabled: bool) -> None:
        self._sessionmaker = sessionmaker
        self._enabled = enabled

    async def hit(self, key: str, *, limit: int, window_seconds: int) -> None:
        if not self._enabled:
            return
        now = datetime.now(UTC)
        epoch = now.timestamp()
        window_start_epoch = math.floor(epoch / window_seconds) * window_seconds
        window_start = datetime.fromtimestamp(window_start_epoch, tz=UTC)
        async with self._sessionmaker() as session:
            hits = await RateLimitRepository(session).increment(key, window_start)
            await session.commit()
        if hits > limit:
            retry_after = max(1, math.ceil(window_start_epoch + window_seconds - epoch))
            logger.warning(
                "rate limit exceeded", extra={"limit_scope": ":".join(key.split(":")[:2])}
            )
            raise RateLimitedError(retry_after)


def fingerprint(value: str) -> str:
    """Stable, non-reversible identifier for using PII (e.g. emails) as a key."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class IdempotentResult:
    status_code: int
    body: dict[str, Any]
    replayed: bool


Operation = Callable[[], Awaitable[tuple[int, dict[str, Any]]]]


async def execute_idempotent(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    key: str | None,
    endpoint: str,
    payload: dict[str, Any],
    operation: Operation,
) -> IdempotentResult:
    """Run ``operation`` and commit, at most once per (user, Idempotency-Key).

    The key is claimed in the same transaction as the operation: if the
    operation fails, the claim rolls back with it and a retry runs again.
    Errors are therefore never cached; only successful responses are replayed.
    """
    if key is None:
        status, body = await operation()
        await session.commit()
        return IdempotentResult(status, body, replayed=False)

    request_hash = hashlib.sha256(
        json.dumps({"endpoint": endpoint, "payload": payload}, sort_keys=True, default=str).encode()
    ).hexdigest()
    repo = IdempotencyRepository(session)
    if await repo.try_claim(user_id, key, endpoint, request_hash):
        status, body = await operation()
        await repo.store_response(user_id, key, status, body)
        await session.commit()
        return IdempotentResult(status, body, replayed=False)

    existing = await repo.get(user_id, key)
    if existing is None or existing.response_status is None or existing.response_body is None:
        # Only possible if the record was purged between statements.
        raise ConflictError("Idempotency-Key state changed concurrently; retry the request")
    if existing.endpoint != endpoint or existing.request_hash != request_hash:
        raise IdempotencyKeyReusedError()
    return IdempotentResult(existing.response_status, existing.response_body, replayed=True)
