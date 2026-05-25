"""
Request tracer — lightweight per-request trace trees.

Builds a structured trace for each inbound SMS covering:
intent classification, field extraction, state transitions, and response.

Traces are written to the structured log (not a separate DB table)
to keep implementation simple and zero-dependency.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any

logger = logging.getLogger("adella_chatbot.tracer")


class RequestTrace:
    """Accumulates trace events for a single inbound request."""

    def __init__(self, *, phone_hash: str = "unknown", source: str = "sms") -> None:
        self.trace_id = uuid.uuid4().hex[:12]
        self.phone_hash = phone_hash   # last 4 digits or hash — never full number
        self.source = source
        self.started_at = time.monotonic()
        self.events: list[dict[str, Any]] = []

    def add_event(self, stage: str, **kwargs: Any) -> None:
        """Add a named stage event with arbitrary metadata."""
        elapsed_ms = int((time.monotonic() - self.started_at) * 1000)
        self.events.append({"stage": stage, "elapsed_ms": elapsed_ms, **kwargs})

    def finish(self, *, outcome: str = "ok") -> dict[str, Any]:
        """Finalize and return the trace dict. Also logs it as a structured event."""
        total_ms = int((time.monotonic() - self.started_at) * 1000)
        trace = {
            "trace_id": self.trace_id,
            "phone_hash": self.phone_hash,
            "source": self.source,
            "outcome": outcome,
            "total_ms": total_ms,
            "events": self.events,
        }
        logger.info(
            "request_trace",
            extra={"trace": trace, "trace_id": self.trace_id, "total_ms": total_ms},
        )
        return trace


# Context-local tracer (thread-local for WSGI safety)
_local = threading.local()


def _phone_hash(phone_number: str | None) -> str:
    """Return last 4 digits of phone number for trace identification (not full number)."""
    p = (phone_number or "").strip().replace("+", "").replace("-", "").replace(" ", "")
    return f"***{p[-4:]}" if len(p) >= 4 else "unknown"


def start_trace(phone_number: str | None = None, source: str = "sms") -> RequestTrace:
    """Start a new request trace and store it thread-locally."""
    trace = RequestTrace(phone_hash=_phone_hash(phone_number), source=source)
    _local.current_trace = trace
    return trace


def get_current_trace() -> RequestTrace | None:
    """Get the current thread-local trace, or None if not started."""
    return getattr(_local, "current_trace", None)


def add_trace_event(stage: str, **kwargs: Any) -> None:
    """Add an event to the current trace if one is active. Safe to call when no trace active."""
    trace = get_current_trace()
    if trace is not None:
        trace.add_event(stage, **kwargs)
