"""Structured logging.

Two formatters are available: a compact human-readable one for the systemd
journal (the default) and a JSON one for log shipping. Both carry the same
structured fields, so switching format never loses information.

Extra context is attached with the ``extra={"ctx": {...}}`` convention::

    log.info("controller connected", extra={"ctx": {"controller_id": "turret-1"}})
"""

from __future__ import annotations

import json
import logging
import logging.config
import sys
from typing import Any

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


def _record_context(record: logging.LogRecord) -> dict[str, Any]:
    ctx = getattr(record, "ctx", None)
    context: dict[str, Any] = dict(ctx) if isinstance(ctx, dict) else {}
    for key, value in record.__dict__.items():
        if key not in _RESERVED and key != "ctx":
            context[key] = value
    return context


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        payload.update(_record_context(record))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    """`level  logger  message  key=value ...` — readable in journalctl."""

    def format(self, record: logging.LogRecord) -> str:
        base = f"{record.levelname:<7} {record.name:<28} {record.getMessage()}"
        context = _record_context(record)
        if context:
            base += "  " + " ".join(f"{k}={v}" for k, v in context.items())
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def configure_logging(level: str = "INFO", fmt: str = "console") -> None:
    """Install the logging configuration. Idempotent."""
    formatter = "json" if fmt == "json" else "console"
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "console": {"()": ConsoleFormatter},
                "json": {"()": JsonFormatter},
            },
            "handlers": {
                "stdout": {
                    "class": "logging.StreamHandler",
                    "stream": sys.stdout,
                    "formatter": formatter,
                }
            },
            "root": {"handlers": ["stdout"], "level": level},
            "loggers": {
                # Access logs duplicate our own request logging and are noisy
                # in the journal; warnings and above still come through.
                "uvicorn.access": {"level": "WARNING", "propagate": True, "handlers": []},
                "uvicorn.error": {"level": level, "propagate": True, "handlers": []},
                "multipart": {"level": "WARNING"},
                "ultralytics": {"level": "WARNING"},
            },
        }
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
