from __future__ import annotations

import base64
import binascii

from app.core.errors import BadRequestError

_CURSOR_PREFIX = "v1:"
_MAX_CURSOR_LENGTH = 64


def encode_cursor(last_id: int) -> str:
    raw = f"{_CURSOR_PREFIX}{last_id}".encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> int:
    """Decode an opaque keyset cursor; any tampering yields a 400."""
    if len(cursor) > _MAX_CURSOR_LENGTH:
        raise BadRequestError("Invalid cursor", details={"parameter": "cursor"})
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii")
    except (binascii.Error, UnicodeError, ValueError) as exc:
        raise BadRequestError("Invalid cursor", details={"parameter": "cursor"}) from exc
    if not raw.startswith(_CURSOR_PREFIX):
        raise BadRequestError("Invalid cursor", details={"parameter": "cursor"})
    value = raw.removeprefix(_CURSOR_PREFIX)
    # isascii(): str.isdigit() also accepts characters such as '²' that int() rejects.
    if not (value.isascii() and value.isdigit()) or len(value) > 18:
        raise BadRequestError("Invalid cursor", details={"parameter": "cursor"})
    return int(value)


def like_contains(term: str) -> str:
    """Build an ILIKE pattern that matches ``term`` literally (use escape='\\\\')."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"
