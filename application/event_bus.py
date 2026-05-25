"""
Synchronous in-process event bus.

Application layer — may only import from core/.
No I/O, no DB, no HTTP, no Flask.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Callable, Type

logger = logging.getLogger(__name__)


class EventBus:
    """
    Simple synchronous event bus.

    Handlers are called in registration order.
    Exceptions in one handler do not prevent others from running.
    """

    def __init__(self) -> None:
        self._handlers: dict[type, list[Callable[[Any], None]]] = defaultdict(list)

    def register(self, event_type: Type[Any], handler: Callable[[Any], None]) -> None:
        """Register a handler for a specific event type."""
        self._handlers[event_type].append(handler)

    def publish(self, event: Any) -> None:
        """Publish an event to all registered handlers."""
        event_type = type(event)
        handlers = self._handlers.get(event_type, [])
        if not handlers:
            logger.debug("event_bus.no_handlers", extra={"event_type": event_type.__name__})
            return
        for handler in handlers:
            try:
                handler(event)
            except Exception:
                logger.exception(
                    "event_bus.handler_error",
                    extra={"event_type": event_type.__name__, "handler": handler.__name__},
                )


_default_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    global _default_bus
    if _default_bus is None:
        _default_bus = EventBus()
    return _default_bus
