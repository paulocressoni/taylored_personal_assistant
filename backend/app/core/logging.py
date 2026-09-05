"""Structured logging + per-request correlation IDs.

Why this module exists
    The app previously never configured logging: every ``logger`` fell through
    to whatever uvicorn left on the root logger, in a plain text format, with
    no way to tell which session a line belonged to. This adds two things:

    1. A JSON (structured) formatter for production so logs are greppable and
       machine-parseable, while dev (ENV=dev) keeps the familiar readable text.
    2. Correlation IDs: a contextvars-based "current session/channel" that a
       logging Filter stamps onto every record that flows through the app
       loggers. contextvars (not a module global) means concurrent requests
       cannot cross-contaminate each other's log lines.

Design choices
    - Only the ROOT logger is configured. uvicorn manages its own
      ``uvicorn.*`` loggers (and the access log); we leave those untouched.
    - No third-party JSON logger: a tiny stdlib formatter keeps prod deps the
      same.
    - Secrets never pass through here: log auth *outcomes* and the transport
      used, never the key value.
"""

import contextvars
import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from app.core.config import settings

# The "current request" correlation context. Each asyncio task runs with its
# own copy of the context (FastAPI runs one handler per request as a task),
# so setting these inside a request can never leak into the next request.
_session_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "session_id", default=None
)
_channel_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "channel", default=None
)


class CorrelationFilter(logging.Filter):
    """Stamp the active session_id/channel onto every passing LogRecord.

    ``filter()`` must return True to keep a record, but it can mutate the
    record first: the context values become real LogRecord attributes that a
    formatter can read via ``record.session_id``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # mypy: LogRecord has no declared session_id/channel attrs — they are
        # added dynamically here, which is exactly what the formatters read.
        record.session_id = _session_ctx.get()  # type: ignore[attr-defined]
        record.channel = _channel_ctx.get()  # type: ignore[attr-defined]
        return True


class JsonFormatter(logging.Formatter):
    """Render a LogRecord as a single-line JSON object (prod structured logs).

    Dependency-free on purpose: stdlib ``json`` only. ``exc_info`` is rendered
    as a traceback string so multi-line stack traces stay inside the payload.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Serialize the record to a JSON string (one record = one line).

        Args:
            record: The LogRecord to serialize.

        Returns:
            A single-line JSON string with ts/level/logger/message plus any
            correlation attributes that are present.
        """
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for label in ("session_id", "channel"):
            value = getattr(record, label, None)
            if value is not None:
                payload[label] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=True, default=str)


class TextFormatter(logging.Formatter):
    """Dev formatter: readable one-liner, correlation ids appended when set.

    Keeps dev output tidy (no ``session_id=None`` noise) while still showing
    the correlation context once a request has set it.
    """

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-8s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        """Append ``[session_id=..., channel=...]`` when either is present.

        Args:
            record: The LogRecord to format.

        Returns:
            A human-readable single line, with correlation extras if set.
        """
        line = super().format(record)
        extras = [
            f"{label}={getattr(record, label)}"
            for label in ("session_id", "channel")
            if getattr(record, label, None) is not None
        ]
        return f"{line}  [{', '.join(extras)}]" if extras else line


def configure_logging() -> None:
    """Idempotently configure the ROOT logger (text in dev, JSON in prod).

    Called once at app startup (main.py lifespan). Dev keeps human-readable
    text; prod emits structured JSON. uvicorn's own loggers are left alone.

    Returns:
        None.
    """
    root = logging.getLogger()
    # Drop any handlers previous callers left on root so we never double-print
    # when the app reloads or a test re-enters the lifespan.
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(CorrelationFilter())
    handler.setFormatter(JsonFormatter() if settings.env == "prod" else TextFormatter())
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def set_log_context(
    *, session_id: str | None = None, channel: str | None = None
) -> None:
    """Record the correlation context for the current task.

    Only values that are not None are overwritten, so a caller can stamp just
    the id or just the channel. No reset is needed for FastAPI request
    handlers: each runs in its own asyncio task (a private copy of the
    context), and a WebSocket handler owns its context for the whole
    connection.

    Args:
        session_id: The thread/session id to stamp on log records (optional).
        channel: The channel (e.g. "api", "cli", "voice") to stamp (optional).

    Returns:
        None.
    """
    if session_id is not None:
        _session_ctx.set(session_id)
    if channel is not None:
        _channel_ctx.set(channel)
