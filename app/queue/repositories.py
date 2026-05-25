from __future__ import annotations

from typing import Any, Mapping, Protocol

from app.queue.inbound import InboundQueueEnvelope, InboundQueueRecord
from app.queue.outbound import OutboundQueueEnvelope, OutboundQueueRecord


class InboundQueueRepository(Protocol):
    def enqueue(self, envelope: InboundQueueEnvelope, *, conn: Any | None = None) -> bool:
        ...

    def mark_processing(self, message_id: str, *, conn: Any | None = None) -> bool:
        ...

    def mark_retry(
        self,
        message_id: str,
        *,
        error_message: str,
        retry_delay_seconds: int = 0,
        conn: Any | None = None,
    ) -> bool:
        ...

    def mark_sent(self, message_id: str, *, conn: Any | None = None) -> bool:
        ...

    def mark_dead(
        self,
        message_id: str,
        *,
        error_message: str | None = None,
        conn: Any | None = None,
    ) -> bool:
        ...

    def get_message(self, message_id: str, *, conn: Any | None = None) -> InboundQueueRecord | None:
        ...

    def list_pending(self, *, limit: int = 100, conn: Any | None = None) -> list[InboundQueueRecord]:
        ...

    def list_dead(self, *, limit: int = 100, conn: Any | None = None) -> list[InboundQueueRecord]:
        ...

    def replay_dead(
        self,
        message_id: str,
        *,
        replay_metadata: Mapping[str, Any],
        conn: Any | None = None,
    ) -> bool:
        ...


class OutboundQueueRepository(Protocol):
    def enqueue(self, envelope: OutboundQueueEnvelope, *, conn: Any | None = None) -> bool:
        ...

    def mark_processing(self, message_id: str, *, conn: Any | None = None) -> bool:
        ...

    def mark_retry(
        self,
        message_id: str,
        *,
        error_message: str,
        retry_delay_seconds: int = 0,
        conn: Any | None = None,
    ) -> bool:
        ...

    def mark_sent(self, message_id: str, *, conn: Any | None = None) -> bool:
        ...

    def get_message(self, message_id: str, *, conn: Any | None = None) -> OutboundQueueRecord | None:
        ...

    def list_pending(self, *, limit: int = 100, conn: Any | None = None) -> list[OutboundQueueRecord]:
        ...

    def list_dead(self, *, limit: int = 100, conn: Any | None = None) -> list[OutboundQueueRecord]:
        ...

    def replay_dead(
        self,
        message_id: str,
        *,
        replay_metadata: Mapping[str, Any],
        conn: Any | None = None,
    ) -> bool:
        ...
