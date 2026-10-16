from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.api.deps import ActorDep, PageDep, SearchQuery, UserServiceDep
from app.models.enums import Role
from app.schemas.common import ErrorResponse, Page
from app.schemas.users import PasswordReset, UserCreate, UserRead, UserUpdate

router = APIRouter(
    prefix="/users",
    tags=["users"],
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)


@router.get("", response_model=Page[UserRead])
async def list_users(
    actor: ActorDep,
    users: UserServiceDep,
    page: PageDep,
    q: SearchQuery = None,
    role: Role | None = None,
    is_active: bool | None = None,
) -> Page[UserRead]:
    items, total = await users.list_users(
        actor, q=q, role=role, is_active=is_active, limit=page.limit, offset=page.offset
    )
    return Page(
        items=[UserRead.model_validate(u) for u in items],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.post(
    "",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"model": ErrorResponse}},
)
async def create_user(body: UserCreate, actor: ActorDep, users: UserServiceDep) -> UserRead:
    return UserRead.model_validate(await users.create(actor, body))


@router.get("/{user_id}", response_model=UserRead, responses={404: {"model": ErrorResponse}})
async def get_user(user_id: uuid.UUID, actor: ActorDep, users: UserServiceDep) -> UserRead:
    return UserRead.model_validate(await users.get(actor, user_id))


@router.patch(
    "/{user_id}",
    response_model=UserRead,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
async def update_user(
    user_id: uuid.UUID, body: UserUpdate, actor: ActorDep, users: UserServiceDep
) -> UserRead:
    """Changing ``role`` or ``is_active`` immediately invalidates the user's tokens."""
    return UserRead.model_validate(await users.update(actor, user_id, body))


@router.put(
    "/{user_id}/password",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={404: {"model": ErrorResponse}},
)
async def reset_password(
    user_id: uuid.UUID, body: PasswordReset, actor: ActorDep, users: UserServiceDep
) -> None:
    await users.reset_password(actor, user_id, body.new_password)
