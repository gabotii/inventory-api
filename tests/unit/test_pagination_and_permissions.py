from __future__ import annotations

import base64

import pytest

from app.core.context import sanitize_request_id
from app.core.errors import BadRequestError
from app.core.pagination import decode_cursor, encode_cursor, like_contains
from app.models.enums import Role
from app.services.permissions import POLICY, Permission, is_allowed


@pytest.mark.parametrize("value", [1, 42, 9_223_372_036_854])
def test_cursor_roundtrip(value: int) -> None:
    assert decode_cursor(encode_cursor(value)) == value


@pytest.mark.parametrize(
    "cursor",
    [
        "garbage!!",
        base64.urlsafe_b64encode(b"v2:10").decode(),
        base64.urlsafe_b64encode(b"v1:-5").decode(),
        base64.urlsafe_b64encode(b"v1:1e3").decode(),
        base64.urlsafe_b64encode("v1:²".encode()).decode(),
        base64.urlsafe_b64encode(b"v1:" + b"9" * 30).decode(),
        "A" * 65,
    ],
)
def test_tampered_cursor_rejected(cursor: str) -> None:
    with pytest.raises(BadRequestError):
        decode_cursor(cursor)


def test_like_pattern_escapes_wildcards() -> None:
    assert like_contains("50%_off\\") == "%50\\%\\_off\\\\%"


def test_request_id_sanitised() -> None:
    assert sanitize_request_id("abc-123.X_y") == "abc-123.X_y"
    for hostile in ["a b", "x" * 65, "evil\nlog-line", "", None]:
        generated = sanitize_request_id(hostile)
        assert generated != hostile
        assert len(generated) == 32


EXPECTED = {
    Role.VIEWER: {Permission.READ_INVENTORY},
    Role.OPERATOR: {Permission.READ_INVENTORY, Permission.RECORD_MOVEMENT},
    Role.MANAGER: {
        Permission.READ_INVENTORY,
        Permission.RECORD_MOVEMENT,
        Permission.ADJUST_STOCK,
        Permission.MANAGE_CATALOG,
    },
    Role.ADMIN: set(Permission),
}


@pytest.mark.parametrize("role", list(Role))
def test_permission_matrix(role: Role) -> None:
    allowed = {permission for permission in Permission if is_allowed(role, permission)}
    assert allowed == EXPECTED[role]


def test_every_permission_has_a_policy() -> None:
    assert set(POLICY) == set(Permission)
