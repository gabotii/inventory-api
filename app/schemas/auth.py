from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, StringConstraints

from app.schemas.common import ApiModel
from app.schemas.users import Email, SubmittedPassword

# Refresh tokens are 43-char URL-safe strings; accept a little slack but never
# arbitrarily large input.
RefreshTokenStr = Annotated[str, StringConstraints(min_length=16, max_length=128)]


class LoginRequest(ApiModel):
    email: Email
    password: SubmittedPassword


class RefreshRequest(ApiModel):
    refresh_token: RefreshTokenStr


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - OAuth2 token type, not a secret
    expires_in: int
    refresh_expires_in: int
