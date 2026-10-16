"""Structured (JSON) logging on top of the standard library.

Every record carries the current ``request_id`` and ``user_id`` from context.
Extra fields passed via ``logger.info("msg", extra={...})`` are emitted as
top-level JSON keys. Keys that look like secrets are redacted defensively.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from app.core.config import LogFormat, Settings
from app.core.context import request_id_var, user_id_var

_RESERVED = frozenset(
    vars(logging.LogRecord("x", logging.INFO, "x", 0, "x", None, None)).keys()
    | {"message", "asctime", "taskName", "color_message"}
)
_SENSITIVE_MARKERS = ("password", "secret", "token", "authorization", "api_key", "cookie")
_REDACTED = "[REDACTED]"


def _redact(key: str, value: Any) -> Any:
    if any(marker in key.lower() for marker in _SENSITIVE_MARKERS):
        return _REDACTED
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id:
            payload["request_id"] = request_id
        user_id = user_id_var.get()
        if user_id:
            payload["user_id"] = user_id
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = _redact(key, value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            key: _redact(key, value)
            for key, value in record.__dict__.items()
            if key not in _RESERVED and not key.startswith("_")
        }
        request_id = request_id_var.get()
        prefix = f"[{request_id[:8]}] " if request_id else ""
        suffix = f" {extras}" if extras else ""
        return f"{prefix}{base}{suffix}"


def configure_logging(settings: Settings) -> None:
    handler = logging.StreamHandler(sys.stdout)
    if settings.log_format is LogFormat.JSON:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(ConsoleFormatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level)
    # The app emits its own structured access log; uvicorn's would duplicate it.
    logging.getLogger("uvicorn.access").disabled = True
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
    # SQLAlchemy echo logs parameters; never enable it implicitly.
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.db_echo else logging.WARNING
    )
