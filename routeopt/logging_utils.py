"""Structured (JSON-ish) logging with the run_id on every line."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any


class JsonishFormatter(logging.Formatter):
    """One JSON object per line: time, level, logger, run_id, message, extras."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "run_id": getattr(record, "run_id", "-"),
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key.startswith("x_"):
                payload[key[2:]] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class RunLogger(logging.LoggerAdapter):
    """Logger adapter that stamps ``run_id`` on every record."""

    def process(self, msg: str, kwargs: Any) -> tuple[str, Any]:
        extra = dict(kwargs.get("extra") or {})
        extra.setdefault("run_id", self.extra.get("run_id", "-") if self.extra else "-")
        kwargs["extra"] = extra
        return msg, kwargs


_configured = False


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON-ish formatter on the root logger once."""
    global _configured
    root = logging.getLogger()
    if not _configured:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonishFormatter())
        root.addHandler(handler)
        _configured = True
    root.setLevel(level.upper())


def get_logger(name: str, run_id: str | None = None) -> RunLogger:
    """Return a logger adapter for ``name`` stamped with ``run_id``."""
    return RunLogger(logging.getLogger(name), {"run_id": run_id or "-"})
