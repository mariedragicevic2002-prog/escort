from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from app.events.outbox import OutboxRepository
from app.queue.adapters import DatabaseInboundQueueRepository, DatabaseOutboundQueueRepository
from app.queue.repositories import InboundQueueRepository, OutboundQueueRepository


@runtime_checkable
class InboundQueueProvider(InboundQueueRepository, Protocol):
    """Storage-agnostic inbound queue operations used by ingress and workers."""


@runtime_checkable
class OutboundQueueProvider(OutboundQueueRepository, Protocol):
    """Storage-agnostic outbound queue operations used by workers and dispatchers."""


@runtime_checkable
class QueueProvider(Protocol):
    """Backend-agnostic provider that supplies inbound/outbound queue contracts."""

    def inbound(self) -> InboundQueueProvider:
        ...

    def outbound(self) -> OutboundQueueProvider:
        ...


class DatabaseQueueProvider:
    """DB-backed queue provider adapter that wraps existing repository adapters."""

    __slots__ = ("_inbound_provider", "_outbound_provider")

    def __init__(
        self,
        *,
        db_service: Any | None = None,
        inbound_provider: InboundQueueProvider | None = None,
        outbound_provider: OutboundQueueProvider | None = None,
        outbox_repository: OutboxRepository | None = None,
    ) -> None:
        if inbound_provider is not None:
            self._inbound_provider: InboundQueueProvider = inbound_provider
        elif db_service is not None:
            self._inbound_provider = DatabaseInboundQueueRepository(db_service)
        else:
            raise ValueError("db_service or inbound_provider is required")

        if outbound_provider is not None:
            self._outbound_provider: OutboundQueueProvider = outbound_provider
        elif outbox_repository is not None:
            self._outbound_provider = DatabaseOutboundQueueRepository(outbox_repository=outbox_repository)
        elif db_service is not None:
            self._outbound_provider = DatabaseOutboundQueueRepository(db_service=db_service)
        else:
            raise ValueError("db_service, outbox_repository, or outbound_provider is required")

    def inbound(self) -> InboundQueueProvider:
        return self._inbound_provider

    def outbound(self) -> OutboundQueueProvider:
        return self._outbound_provider


def resolve_inbound_queue_provider(
    *,
    inbound_provider: InboundQueueProvider | None = None,
    queue_provider: QueueProvider | None = None,
    db_service: Any | None = None,
) -> InboundQueueProvider | None:
    if inbound_provider is not None:
        return inbound_provider
    if queue_provider is not None:
        return queue_provider.inbound()
    if db_service is None or not hasattr(db_service, "execute_query"):
        return None
    return DatabaseQueueProvider(db_service=db_service).inbound()


def resolve_outbound_queue_provider(
    *,
    outbound_provider: OutboundQueueProvider | None = None,
    queue_provider: QueueProvider | None = None,
    db_service: Any | None = None,
    outbox_repository: OutboxRepository | None = None,
) -> OutboundQueueProvider | None:
    if outbound_provider is not None:
        return outbound_provider
    if queue_provider is not None:
        return queue_provider.outbound()
    if outbox_repository is not None:
        return DatabaseOutboundQueueRepository(outbox_repository=outbox_repository)
    if db_service is None:
        return None
    return DatabaseOutboundQueueRepository(db_service=db_service)
