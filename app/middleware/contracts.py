from __future__ import annotations

from typing import Callable, Protocol

from app.runtime.context import OrchestrationContext

NextHandler = Callable[[OrchestrationContext], list[str]]


class InboundMiddleware(Protocol):
    """Middleware contract for inbound request processing."""

    def __call__(self, context: OrchestrationContext, next_handler: NextHandler) -> list[str]:
        ...

