from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.context import user_id_var
from app.core.errors import AuthenticationError
from app.core.security import PasswordHasher, TokenService
from app.schemas.common import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from app.services.auth import AuthService
from app.services.catalog import CatalogService
from app.services.permissions import Actor, RequestMeta
from app.services.stock import StockService
from app.services.support import AuditQueryService, RateLimiter
from app.services.users import UserService


@dataclass(slots=True)
class AppState:
    """Process-wide singletons created once at startup (see app.main)."""

    settings: Settings
    sessionmaker: async_sessionmaker[AsyncSession]
    hasher: PasswordHasher
    tokens: TokenService
    rate_limiter: RateLimiter
    expected_schema_revision: str | None
    known_schema_revisions: frozenset[str]


def get_state(request: Request) -> AppState:
    state: AppState = request.app.state.container
    return state


StateDep = Annotated[AppState, Depends(get_state)]


async def get_session(state: StateDep) -> AsyncIterator[AsyncSession]:
    # One session (and at most one transaction at a time) per request. Leaving
    # the context rolls back anything a service did not explicitly commit.
    async with state.sessionmaker() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_request_meta(request: Request) -> RequestMeta:
    # request.client reflects X-Forwarded-For only when uvicorn trusts the
    # proxy (FORWARDED_ALLOW_IPS); clients cannot spoof it otherwise.
    return RequestMeta(ip_address=request.client.host if request.client else None)


MetaDep = Annotated[RequestMeta, Depends(get_request_meta)]


def get_auth_service(session: SessionDep, state: StateDep) -> AuthService:
    return AuthService(session, state.settings, state.hasher, state.tokens, state.rate_limiter)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]

_bearer = HTTPBearer(auto_error=False, description="Access token from POST /api/v1/auth/login")


async def get_actor(
    auth: AuthServiceDep,
    meta: MetaDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Actor:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthenticationError()
    user = await auth.authenticate(credentials.credentials)
    user_id_var.set(str(user.id))
    return Actor(user=user, meta=meta)


ActorDep = Annotated[Actor, Depends(get_actor)]


def get_user_service(session: SessionDep, state: StateDep) -> UserService:
    return UserService(session, state.hasher)


def get_catalog_service(session: SessionDep) -> CatalogService:
    return CatalogService(session)


def get_stock_service(session: SessionDep) -> StockService:
    return StockService(session)


def get_audit_service(session: SessionDep) -> AuditQueryService:
    return AuditQueryService(session)


UserServiceDep = Annotated[UserService, Depends(get_user_service)]
CatalogServiceDep = Annotated[CatalogService, Depends(get_catalog_service)]
StockServiceDep = Annotated[StockService, Depends(get_stock_service)]
AuditServiceDep = Annotated[AuditQueryService, Depends(get_audit_service)]


@dataclass(frozen=True, slots=True)
class PageParams:
    limit: int
    offset: int


def page_params(
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
) -> PageParams:
    return PageParams(limit=limit, offset=offset)


PageDep = Annotated[PageParams, Depends(page_params)]
LimitQuery = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]
CursorQuery = Annotated[str | None, Query(max_length=64)]
SearchQuery = Annotated[str | None, Query(min_length=1, max_length=100)]
IdempotencyKeyHeader = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9._:\-]+$",
        description="Optional. Retrying with the same key returns the original result.",
    ),
]
