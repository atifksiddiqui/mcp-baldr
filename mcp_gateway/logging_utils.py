"""Structured JSON logging setup for the MCP Gateway.

Call :func:`setup_logging` once at startup to configure the root logger.
When ``format="json"`` each record is emitted as a single-line JSON object
suitable for log aggregation.  When ``format="text"`` a human-readable
format is used for local development.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any


class _JsonFormatter(logging.Formatter):
    """Emit each log record as a single-line JSON object."""

    # Standard LogRecord attributes to skip when building the extra payload.
    _STD_ATTRS = frozenset(
        {
            "name",
            "msg",
            "args",
            "levelname",
            "levelno",
            "pathname",
            "filename",
            "module",
            "exc_info",
            "exc_text",
            "stack_info",
            "lineno",
            "funcName",
            "created",
            "msecs",
            "relativeCreated",
            "thread",
            "threadName",
            "processName",
            "process",
            "message",
            "taskName",
        }
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Merge in any extra fields attached to the record.
        for key, value in record.__dict__.items():
            if key not in self._STD_ATTRS and not key.startswith("_"):
                payload[key] = value

        if record.exc_info and record.exc_info[1] is not None:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


class _TextFormatter(logging.Formatter):
    """Human-readable single-line format for local development."""

    def format(self, record: logging.LogRecord) -> str:
        parts = [
            datetime.fromtimestamp(record.created, tz=UTC).strftime("%Y-%m-%d %H:%M:%S"),
            record.levelname,
            record.name,
        ]
        extra = {
            k: v
            for k, v in record.__dict__.items()
            if k not in _JsonFormatter._STD_ATTRS and not k.startswith("_")
        }
        msg = record.getMessage()
        if extra:
            msg = f"{msg}  {json.dumps(extra, default=str)}"
        parts.append(msg)
        line = " | ".join(parts)
        if record.exc_info and record.exc_info[1] is not None:
            line += "\n" + self.formatException(record.exc_info)
        return line


def setup_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Configure the root logger.

    Args:
        level: Log level name (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        fmt: Output format — ``"json"`` (default) or ``"text"``.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    formatter: logging.Formatter
    if fmt.lower() == "json":
        formatter = _JsonFormatter()
    else:
        formatter = _TextFormatter()

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric_level)
