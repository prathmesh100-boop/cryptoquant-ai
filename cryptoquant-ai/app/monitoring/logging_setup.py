"""Structured logging with secret redaction."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

REDACTED = "***REDACTED***"
_SENSITIVE_KEY = re.compile(r"(api[_-]?key|secret|token|password|passwd|signature|authorization|credential)", re.I)
_STANDARD_ATTRS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}


def redact(value: Any) -> Any:
    """Recursively mask values stored under sensitive-looking keys."""
    if isinstance(value, dict):
        return {k: (REDACTED if _SENSITIVE_KEY.search(str(k)) else redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    """One JSON object per line; extras are included and redacted."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, val in record.__dict__.items():
            if key in _STANDARD_ATTRS:
                continue
            payload[key] = REDACTED if _SENSITIVE_KEY.search(key) else redact(val)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO", json_output: bool = True, stream: Any = None) -> None:
    """Configure the root logger. Idempotent: replaces handlers it previously installed."""
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_cq_handler", False):
            root.removeHandler(h)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler._cq_handler = True  # type: ignore[attr-defined]
    handler.setFormatter(
        JsonFormatter() if json_output else logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(level.upper())


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
