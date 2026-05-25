from __future__ import annotations

from dataclasses import replace
from typing import Any

from refactor.adapters.sms_outbound_adapter import SMSOutboundAdapter
from refactor.app.events.outbox import OutboxEventEnvelope, OutboxEventRecord, OutboxStatus
from refactor.app.outbound import OutboundDispatcher, OutboundMessage, OutboundQueuePublisher
from refactor.app.queue import DatabaseOutboundQueueRepository
from refactor.app.workers import OutboxEventDispatcher, OutboxWorkerRuntime
from refactor.app.workers.outbound_sender import register_outbound_sender_handler


def _now_iso() -> str:
    return "2026-01-01T00:00:00+00:00"


class _InMemoryOutboxRepository:
    def __init__(self) -> None:
        self._rows: dict[str, OutboxEventRecord] = {}
        self._idempotency_index: dict[str, str] = {}

    def append_event(self, event: OutboxEventEnvelope, *, conn: Any | None = None) -> bool:
        _ = conn
        key = event.normalized_idempotency_key
        if event.event_id in self._rows or key in self._idempotency_index:
            return False
        self._rows[event.event_id] = OutboxEventRecord(
            event_id=event.event_id,
            idempotency_key=key,
            event_type=event.event_type,
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
            payload=dict(event.payload),
            metadata=dict(event.metadata),
            status=OutboxStatus.PENDING,
            retry_count=0,
            max_retries=max(1, int(event.max_retries)),
            next_retry_at=None,
            processing_started_at=None,
            last_attempt_at=None,
            last_error=None,
            last_error_at=None,
            dead_lettered_at=None,
            occurred_at=event.occurred_at,
            created_at=_now_iso(),
            updated_at=_now_iso(),
        )
        self._idempotency_index[key] = event.event_id
        return True

    def mark_processing(self, event_id: str, *, conn: Any | None = None) -> bool:
        _ = conn
        event = self._rows.get(event_id)
        if event is None or event.status not in {OutboxStatus.PENDING, OutboxStatus.FAILED}:
            return False
        self._rows[event_id] = replace(
            event,
            status=OutboxStatus.PROCESSING,
            processing_started_at=event.processing_started_at or _now_iso(),
            last_attempt_at=_now_iso(),
            updated_at=_now_iso(),
        )
        return True

    def mark_failure(
        self,
        event_id: str,
        *,
        error_message: str,
        retry_delay_seconds: int = 0,
        conn: Any | None = None,
    ) -> bool:
        _ = conn
        event = self._rows.get(event_id)
        if event is None or event.status not in {OutboxStatus.PENDING, OutboxStatus.PROCESSING}:
            return False
        retry_count = int(event.retry_count) + 1
        dead = retry_count >= int(event.max_retries)
        self._rows[event_id] = replace(
            event,
            status=OutboxStatus.DEAD_LETTER if dead else OutboxStatus.FAILED,
            retry_count=retry_count,
            next_retry_at=None if dead else f"+{int(retry_delay_seconds)}s",
            last_error=error_message,
            last_error_at=_now_iso(),
            dead_lettered_at=_now_iso() if dead else event.dead_lettered_at,
            updated_at=_now_iso(),
        )
        return True

    def mark_published(self, event_id: str, *, conn: Any | None = None) -> bool:
        _ = conn
        event = self._rows.get(event_id)
        if event is None or event.status not in {OutboxStatus.PROCESSING, OutboxStatus.PUBLISHED}:
            return False
        self._rows[event_id] = replace(
            event,
            status=OutboxStatus.PUBLISHED,
            next_retry_at=None,
            last_error=None,
            last_error_at=None,
            updated_at=_now_iso(),
        )
        return True

    def get_event(self, event_id: str, *, conn: Any | None = None) -> OutboxEventRecord | None:
        _ = conn
        return self._rows.get(event_id)

    def list_pending(self, *, limit: int = 100, conn: Any | None = None) -> list[OutboxEventRecord]:
        _ = conn
        pending = [
            event
            for event in self._rows.values()
            if event.status in {OutboxStatus.PENDING, OutboxStatus.FAILED}
        ]
        pending.sort(key=lambda event: event.created_at or "")
        return pending[: max(1, int(limit))]


class _InMemoryIdempotencyGuard:
    def __init__(self) -> None:
        self._event_ids: set[str] = set()
        self._dedup_keys: set[str] = set()

    def was_processed(
        self,
        *,
        event_id: str,
        dedup_key: str,
        conn: Any | None = None,
    ) -> bool:
        _ = conn
        return event_id in self._event_ids or dedup_key in self._dedup_keys

    def mark_processed(
        self,
        *,
        event_id: str,
        dedup_key: str,
        event_type: str,
        metadata=None,
        conn: Any | None = None,
    ) -> bool:
        _ = (event_type, metadata, conn)
        if self.was_processed(event_id=event_id, dedup_key=dedup_key):
            return False
        self._event_ids.add(event_id)
        self._dedup_keys.add(dedup_key)
        return True


def _publish_single_sms(
    *,
    publisher: OutboundQueuePublisher,
    phone_number: str = "+61412345678",
    request_id: str = "req-123",
    body: str = "queued-reply",
    dedup_key: str | None = None,
):
    message = OutboundMessage(
        channel="sms",
        recipient=phone_number,
        body=body,
        metadata={"actions": [{"name": "noop"}], **({"dedup_key": dedup_key} if dedup_key else {})},
    )
    return publisher.publish_messages(
        aggregate_id=phone_number,
        messages=[message],
        request_id=request_id,
        correlation_id=request_id,
    )


def test_outbound_queue_publish_and_sender_worker_success_flow() -> None:
    outbox = _InMemoryOutboxRepository()
    queue_repo = DatabaseOutboundQueueRepository(outbox_repository=outbox)
    publisher = OutboundQueuePublisher(queue_repository=queue_repo)

    publish = _publish_single_sms(publisher=publisher)
    pending = outbox.list_pending(limit=10)
    assert publish.attempted == 1
    assert publish.queued == 1
    assert publish.failed == 0
    assert len(pending) == 1
    assert pending[0].metadata["request_id"] == "req-123"
    assert pending[0].metadata["correlation_id"] == "req-123"

    sent_payloads: list[tuple[str, str]] = []
    outbound_dispatcher = OutboundDispatcher(
        adapters=[SMSOutboundAdapter(lambda recipient, body: sent_payloads.append((recipient, body)) or True)]
    )
    dispatcher = OutboxEventDispatcher()
    register_outbound_sender_handler(dispatcher=dispatcher, outbound_dispatcher=outbound_dispatcher)
    runtime = OutboxWorkerRuntime(
        outbox_repository=outbox,
        dispatcher=dispatcher,
        idempotency_guard=_InMemoryIdempotencyGuard(),
    )

    result = runtime.run_once()
    stored = outbox.get_event(pending[0].event_id)
    assert result.sent == 1
    assert sent_payloads == [("+61412345678", "queued-reply")]
    assert stored is not None
    assert stored.status == OutboxStatus.PUBLISHED


def test_outbound_queue_worker_updates_retry_metadata_on_send_failure() -> None:
    outbox = _InMemoryOutboxRepository()
    queue_repo = DatabaseOutboundQueueRepository(outbox_repository=outbox)
    publisher = OutboundQueuePublisher(queue_repository=queue_repo, max_attempts=3)
    publish = _publish_single_sms(publisher=publisher, body="retry-me")
    assert publish.queued == 1
    event_id = outbox.list_pending(limit=10)[0].event_id

    outbound_dispatcher = OutboundDispatcher(
        adapters=[SMSOutboundAdapter(lambda _recipient, _body: False)]
    )
    dispatcher = OutboxEventDispatcher()
    register_outbound_sender_handler(dispatcher=dispatcher, outbound_dispatcher=outbound_dispatcher)
    runtime = OutboxWorkerRuntime(
        outbox_repository=outbox,
        dispatcher=dispatcher,
        idempotency_guard=_InMemoryIdempotencyGuard(),
    )

    result = runtime.run_once()
    stored = outbox.get_event(event_id)
    assert result.retried == 1
    assert stored is not None
    assert stored.status == OutboxStatus.FAILED
    assert stored.retry_count == 1
    assert stored.last_error is not None
    assert stored.next_retry_at is not None


def test_outbound_queue_worker_marks_dead_letter_on_retry_exhaustion() -> None:
    outbox = _InMemoryOutboxRepository()
    queue_repo = DatabaseOutboundQueueRepository(outbox_repository=outbox)
    publisher = OutboundQueuePublisher(queue_repository=queue_repo, max_attempts=1)
    publish = _publish_single_sms(publisher=publisher, body="dead-letter")
    assert publish.queued == 1
    event_id = outbox.list_pending(limit=10)[0].event_id

    outbound_dispatcher = OutboundDispatcher(
        adapters=[SMSOutboundAdapter(lambda _recipient, _body: False)]
    )
    dispatcher = OutboxEventDispatcher()
    register_outbound_sender_handler(dispatcher=dispatcher, outbound_dispatcher=outbound_dispatcher)
    runtime = OutboxWorkerRuntime(
        outbox_repository=outbox,
        dispatcher=dispatcher,
        idempotency_guard=_InMemoryIdempotencyGuard(),
    )

    result = runtime.run_once()
    stored = outbox.get_event(event_id)
    assert result.dead_lettered == 1
    assert stored is not None
    assert stored.status == OutboxStatus.DEAD_LETTER
    assert stored.retry_count == 1
    assert stored.dead_lettered_at is not None


def test_outbound_queue_publish_and_worker_prevent_duplicate_send_side_effects() -> None:
    outbox = _InMemoryOutboxRepository()
    queue_repo = DatabaseOutboundQueueRepository(outbox_repository=outbox)
    publisher = OutboundQueuePublisher(queue_repository=queue_repo)

    first = _publish_single_sms(publisher=publisher, request_id="req-dup", dedup_key="dedup-explicit")
    duplicate = _publish_single_sms(publisher=publisher, request_id="req-dup", dedup_key="dedup-explicit")
    assert first.queued == 1
    assert duplicate.duplicates == 1

    pending = outbox.list_pending(limit=10)
    assert len(pending) == 1
    event = pending[0]
    guard = _InMemoryIdempotencyGuard()
    guard.mark_processed(
        event_id="other-event-id",
        dedup_key=event.idempotency_key,
        event_type=event.event_type,
    )

    sends: list[str] = []
    outbound_dispatcher = OutboundDispatcher(
        adapters=[SMSOutboundAdapter(lambda _recipient, body: sends.append(body) or True)]
    )
    dispatcher = OutboxEventDispatcher()
    register_outbound_sender_handler(dispatcher=dispatcher, outbound_dispatcher=outbound_dispatcher)
    runtime = OutboxWorkerRuntime(
        outbox_repository=outbox,
        dispatcher=dispatcher,
        idempotency_guard=guard,
    )

    result = runtime.run_once()
    stored = outbox.get_event(event.event_id)
    assert result.duplicates == 1
    assert sends == []
    assert stored is not None
    assert stored.status == OutboxStatus.PUBLISHED
