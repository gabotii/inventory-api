from __future__ import annotations

from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession


async def count(session: AsyncSession, stmt: Select[*tuple[Any, ...]]) -> int:
    total = await session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    return int(total or 0)


async def paginate_scalars[T](
    session: AsyncSession, stmt: Select[T], *, limit: int, offset: int
) -> tuple[list[T], int]:
    total = await count(session, stmt)
    items = list((await session.scalars(stmt.limit(limit).offset(offset))).all())
    return items, total
