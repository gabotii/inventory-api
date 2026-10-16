from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import ActorDep, AuthServiceDep, MetaDep
from app.schemas.auth import LoginRequest, RefreshRequest, TokenPair
from app.schemas.common import ErrorResponse
from app.schemas.users import PasswordChange, UserRead

router = APIRouter(prefix="/auth", tags=["auth"])

_AUTH_ERRORS: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    429: {"model": ErrorResponse},
}


@router.post("/login", response_model=TokenPair, responses=_AUTH_ERRORS)
async def login(body: LoginRequest, auth: AuthServiceDep, meta: MetaDep) -> TokenPair:
    return await auth.login(body.email, body.password, meta)


@router.post("/refresh", response_model=TokenPair, responses=_AUTH_ERRORS)
async def refresh(body: RefreshRequest, auth: AuthServiceDep, meta: MetaDep) -> TokenPair:
    """Exchange a refresh token for a new pair. The old refresh token stops working."""
    return await auth.refresh(body.refresh_token, meta)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(body: RefreshRequest, auth: AuthServiceDep, meta: MetaDep) -> None:
    """Revoke the session that the refresh token belongs to. Always 204."""
    await auth.logout(body.refresh_token, meta)


@router.get("/me", response_model=UserRead, responses={401: {"model": ErrorResponse}})
async def me(actor: ActorDep) -> UserRead:
    return UserRead.model_validate(actor.user)


@router.post(
    "/password",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={401: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
async def change_password(body: PasswordChange, actor: ActorDep, auth: AuthServiceDep) -> None:
    """Change your own password. All your sessions are revoked; log in again."""
    await auth.change_password(actor, body)
