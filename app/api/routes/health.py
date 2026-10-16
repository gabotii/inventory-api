from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.deps import StateDep

router = APIRouter(prefix="/health", tags=["health"])
logger = logging.getLogger(__name__)

_READINESS_TIMEOUT_SECONDS = 3.0


@router.get("/live")
async def live() -> dict[str, str]:
    """Liveness: the process is up and serving. Never touches dependencies, so a
    database outage does not make the orchestrator restart healthy pods."""
    return {"status": "ok"}


def schema_status(
    current: str | None, expected: str | None, known: frozenset[str]
) -> tuple[bool, Any]:
    """Decide readiness from the database's Alembic revision.

    * equal to this build's head          -> ready
    * a revision this build knows, older  -> NOT ready (migrations pending)
    * a revision this build does not know -> ready: the database was migrated
      ahead of this (older) build during a rolling deploy. Safe because
      migrations must be backward compatible (expand/contract, see README).
    """
    if expected is None:
        return True, "unchecked"
    if current == expected:
        return True, "ok"
    detail = {"current": current, "expected": expected}
    if current is None or current in known:
        return False, {"status": "behind", **detail}
    return True, {"status": "ahead", **detail}


@router.get("/ready", responses={503: {"description": "Not ready to receive traffic"}})
async def ready(state: StateDep) -> JSONResponse:
    """Readiness: database reachable *and* schema not behind this build."""
    checks: dict[str, Any] = {}
    healthy = True
    try:
        async with asyncio.timeout(_READINESS_TIMEOUT_SECONDS), state.sessionmaker() as session:
            await session.execute(text("SELECT 1"))
            checks["database"] = "ok"
            revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
        healthy, checks["schema"] = schema_status(
            revision, state.expected_schema_revision, state.known_schema_revisions
        )
    except Exception as exc:
        healthy = False
        checks.setdefault("database", "unavailable")
        logger.warning("readiness check failed", extra={"error_type": type(exc).__name__})
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "unavailable", "checks": checks},
    )
