"""Structured logging with correlation IDs."""
from __future__ import annotations

import contextvars
import logging
import sys
import uuid

correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=""
)


class CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = correlation_id.get("")  # type: ignore[attr-defined]
        return True


def setup_logging(level: str = "INFO") -> None:
    """Configure logging with correlation ID support."""
    fmt = "%(asctime)s [%(levelname)s] [%(correlation_id)s] %(name)s: %(message)s"
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter(fmt))
    handler.addFilter(CorrelationFilter())

    root = logging.getLogger()
    # Avoid duplicate handlers on reload
    if not any(isinstance(h, logging.StreamHandler) and hasattr(h, '_council')
               for h in root.handlers):
        handler._council = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def new_correlation_id() -> str:
    """Generate and set a new correlation ID for this async context."""
    cid = uuid.uuid4().hex[:8]
    correlation_id.set(cid)
    return cid
