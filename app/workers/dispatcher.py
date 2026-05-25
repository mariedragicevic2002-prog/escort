from __future__ import annotations

from collections.abc import Callable

from app.events.outbox import OutboxEventRecord

OutboxEventHandler = Callable[[OutboxEventRecord], None]


class OutboxDispatchError(RuntimeError):
    """Raised when an outbox event has no registered handler."""


class OutboxEventDispatcher:
    """Type-based dispatch registry for outbox events."""

    def __init__(self) -> None:
        self._handlers: dict[str, OutboxEventHandler] = {}

    def register(self, event_type: str, handler: OutboxEventHandler) -> None:
        key = str(event_type or "").strip()
        if not key:
            raise ValueError("event_type is required")
        self._handlers[key] = handler

    def dispatch(self, event: OutboxEventRecord) -> None:
        handler = self._handlers.get(event.event_type)
        if handler is None:
            raise OutboxDispatchError(f"No handler registered for event_type={event.event_type!r}")
        handler(event)

