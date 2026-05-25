from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from app.events.outbox import OutboxEventRecord, OutboxStatus
from app.queue.inbound import InboundQueueRecord
from app.queue.metadata import QueueMessageMetadata
from app.queue.status import QueueStatus
from app.workers.dispatcher import OutboxEventDispatcher
from app.workers.inbound_runtime import InboundWorkerRuntime
from app.workers.runtime import OutboxWorkerRuntime
from app.workers.supervision import (
    InMemoryWorkerLeaseStore,
    WorkerHeartbeatTracker,
    WorkerSupervisionRuntime,
)


class _MutableClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: int) -> None:
        self._now = self._now + timedelta(seconds=max(0, int(seconds)))


class _InMemoryOutboxRepository:
    def __init__(self) -> None:
        self._rows: dict[str, OutboxEventRecord] = {}
        self.recovered: list[str] = []

    def add(self, event: OutboxEventRecord) -> None:
        self._rows[event.event_id] = event

    def list_pending(self, *, limit: int = 100, conn: Any | None = None) -> list[OutboxEventRecord]:
        _ = conn
        rows = [row for row in self._rows.values() if row.status in {OutboxStatus.PENDING, OutboxStatus.FAILED}]
        rows.sort(key=lambda row: row.created_at or "")
        return rows[: max(1, int(limit))]

    def mark_processing(self, event_id: str, *, conn: Any | None = None) -> bool:
        _ = conn
        row = self._rows.get(event_id)
        if row is None or row.status not in {OutboxStatus.PENDING, OutboxStatus.FAILED}:
            return False
        self._rows[event_id] = replace(
            row,
            status=OutboxStatus.PROCESSING,
            processing_started_at=row.processing_started_at or "2026-01-01T00:01:00+00:00",
            last_attempt_at="2026-01-01T00:01:00+00:00",
            updated_at="2026-01-01T00:01:00+00:00",
        )
        return True

    def mark_published(self, event_id: str, *, conn: Any | None = None) -> bool:
        _ = conn
        row = self._rows.get(event_id)
        if row is None or row.status not in {OutboxStatus.PROCESSING, OutboxStatus.PUBLISHED}:
            return False
        self._rows[event_id] = replace(
            row,
            status=OutboxStatus.PUBLISHED,
            next_retry_at=None,
            last_error=None,
            last_error_at=None,
            updated_at="2026-01-01T00:03:00+00:00",
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
        _ = (retry_delay_seconds, conn)
        row = self._rows.get(event_id)
        if row is None or row.status not in {OutboxStatus.PENDING, OutboxStatus.PROCESSING}:
            return False
        retry_count = int(row.retry_count) + 1
        is_dead = retry_count >= int(row.max_retries)
        self._rows[event_id] = replace(
            row,
            status=(OutboxStatus.DEAD_LETTER if is_dead else OutboxStatus.FAILED),
            retry_count=retry_count,
            next_retry_at=(None if is_dead else "now"),
            last_error=error_message,
            dead_lettered_at=("2026-01-01T00:02:00+00:00" if is_dead else row.dead_lettered_at),
        )
        return True

    def recover_stale_processing(
        self,
        event_id: str,
        *,
        error_message: str = "worker supervision lease expired",
        retry_delay_seconds: int = 0,
        conn: Any | None = None,
    ) -> bool:
        _ = (retry_delay_seconds, conn)
        row = self._rows.get(event_id)
        if row is None or row.status != OutboxStatus.PROCESSING:
            return False
        self._rows[event_id] = replace(
            row,
            status=OutboxStatus.FAILED,
            retry_count=int(row.retry_count) + 1,
            processing_started_at=None,
            next_retry_at="now",
            last_error=error_message,
            last_error_at="2026-01-01T00:02:00+00:00",
            updated_at="2026-01-01T00:02:00+00:00",
        )
        self.recovered.append(event_id)
        return True

    def get_event(self, event_id: str, *, conn: Any | None = None) -> OutboxEventRecord | None:
        _ = conn
        return self._rows.get(event_id)


class _InMemoryInboundRepository:
    def __init__(self) -> None:
        self._rows: dict[str, InboundQueueRecord] = {}
        self.recovered: list[str] = []

    def add(self, message: InboundQueueRecord) -> None:
        self._rows[message.message_id] = message

    def list_pending(self, *, limit: int = 100, conn: Any | None = None) -> list[InboundQueueRecord]:
        _ = conn
        rows = [row for row in self._rows.values() if row.status in {QueueStatus.PENDING, QueueStatus.RETRY}]
        rows.sort(key=lambda row: row.created_at or "")
        return rows[: max(1, int(limit))]

    def mark_processing(self, message_id: str, *, conn: Any | None = None) -> bool:
        _ = conn
        row = self._rows.get(message_id)
        if row is None or row.status not in {QueueStatus.PENDING, QueueStatus.RETRY}:
            return False
        metadata = replace(row.metadata, attempt=int(row.metadata.attempt) + 1)
        self._rows[message_id] = replace(row, status=QueueStatus.PROCESSING, metadata=metadata)
        return True

    def mark_sent(self, message_id: str, *, conn: Any | None = None) -> bool:
        _ = conn
        row = self._rows.get(message_id)
        if row is None or row.status not in {QueueStatus.PROCESSING, QueueStatus.SENT}:
            return False
        self._rows[message_id] = replace(row, status=QueueStatus.SENT, last_error=None)
        return True

    def mark_retry(
        self,
        message_id: str,
        *,
        error_message: str,
        retry_delay_seconds: int = 0,
        conn: Any | None = None,
    ) -> bool:
        _ = (retry_delay_seconds, conn)
        row = self._rows.get(message_id)
        if row is None or row.status not in {QueueStatus.PENDING, QueueStatus.PROCESSING}:
            return False
        self._rows[message_id] = replace(row, status=QueueStatus.RETRY, last_error=error_message)
        return True

    def recover_stale_processing(
        self,
        message_id: str,
        *,
        error_message: str = "worker supervision lease expired",
        retry_delay_seconds: int = 0,
        conn: Any | None = None,
    ) -> bool:
        _ = (retry_delay_seconds, conn)
        row = self._rows.get(message_id)
        if row is None or row.status != QueueStatus.PROCESSING:
            return False
        self._rows[message_id] = replace(row, status=QueueStatus.RETRY, last_error=error_message)
        self.recovered.append(message_id)
        return True

    def get_message(self, message_id: str, *, conn: Any | None = None) -> InboundQueueRecord | None:
        _ = conn
        return self._rows.get(message_id)


class _IdempotencyGuard:
    def __init__(self) -> None:
        self._ids: set[str] = set()
        self._keys: set[str] = set()

    def was_processed(self, *, event_id: str, dedup_key: str, conn: Any | None = None) -> bool:
        _ = conn
        return event_id in self._ids or dedup_key in self._keys

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
        self._ids.add(event_id)
        self._keys.add(dedup_key)
        return True


class _InboundIdempotencyGuard:
    def __init__(self) -> None:
        self._ids: set[str] = set()
        self._keys: set[str] = set()

    def was_processed(self, *, message_id: str, dedup_key: str, conn: Any | None = None) -> bool:
        _ = conn
        return message_id in self._ids or dedup_key in self._keys

    def mark_processed(self, *, message_id: str, dedup_key: str, metadata=None, conn: Any | None = None) -> bool:
        _ = (metadata, conn)
        if self.was_processed(message_id=message_id, dedup_key=dedup_key):
            return False
        self._ids.add(message_id)
        self._keys.add(dedup_key)
        return True


def _outbox_event(*, event_id: str, dedup_key: str, status: str = OutboxStatus.PENDING) -> OutboxEventRecord:
    now = "2026-01-01T00:00:00+00:00"
    return OutboxEventRecord(
        event_id=event_id,
        idempotency_key=dedup_key,
        event_type="conversation.state_transitioned",
        aggregate_type="conversation_state",
        aggregate_id="+61412345678",
        payload={"body": "hello"},
        metadata={"source": "tests"},
        status=status,
        retry_count=0,
        max_retries=3,
        next_retry_at=None,
        processing_started_at="2026-01-01T00:00:30+00:00" if status == OutboxStatus.PROCESSING else None,
        last_attempt_at=None,
        last_error=None,
        last_error_at=None,
        dead_lettered_at=None,
        occurred_at=now,
        created_at=now,
        updated_at=now,
    )


def _inbound_record(*, message_id: str, dedup_key: str, status: str = QueueStatus.PENDING) -> InboundQueueRecord:
    now = "2026-01-01T00:00:00+00:00"
    return InboundQueueRecord(
        message_id=message_id,
        payload={"phone_number": "+61412345678", "body": "hello", "request_id": f"req-{message_id}"},
        metadata=QueueMessageMetadata(
            dedup_key=dedup_key,
            request_id=f"req-{message_id}",
            correlation_id=f"corr-{message_id}",
            attempt=1 if status == QueueStatus.PROCESSING else 0,
            enqueued_at=now,
        ),
        status=status,
        max_attempts=3,
        last_error=None,
        created_at=now,
        updated_at=now,
    )


def test_supervision_heartbeat_updates_claim_state() -> None:
    clock = _MutableClock()
    tracker = WorkerHeartbeatTracker(clock=clock)
    lease_store = InMemoryWorkerLeaseStore(clock=clock)
    supervisor = WorkerSupervisionRuntime(
        queue_name="refactor_outbox",
        worker_id="worker-heartbeat",
        lease_duration_seconds=10,
        lease_store=lease_store,
        heartbeat_tracker=tracker,
    )

    assert supervisor.claim_item("evt-heartbeat") is True
    first = supervisor.get_heartbeat_state("evt-heartbeat")
    assert first is not None
    assert first.beat_count == 1

    clock.advance(3)
    assert supervisor.heartbeat("evt-heartbeat") is True
    second = supervisor.get_heartbeat_state("evt-heartbeat")
    assert second is not None
    assert second.beat_count == 2
    assert second.last_heartbeat_at > first.last_heartbeat_at
    assert second.lease_expires_at > first.lease_expires_at


def test_supervision_lease_expiry_triggers_stale_recovery_requeue() -> None:
    clock = _MutableClock()
    lease_store = InMemoryWorkerLeaseStore(clock=clock)
    tracker = WorkerHeartbeatTracker(clock=clock)
    crashed = WorkerSupervisionRuntime(
        queue_name="refactor_outbox",
        worker_id="worker-crashed",
        lease_duration_seconds=5,
        lease_store=lease_store,
        heartbeat_tracker=tracker,
    )
    recovery = WorkerSupervisionRuntime(
        queue_name="refactor_outbox",
        worker_id="worker-recovery",
        lease_duration_seconds=5,
        lease_store=lease_store,
        heartbeat_tracker=tracker,
    )
    assert crashed.claim_item("evt-stale") is True

    clock.advance(6)
    recovered_items: list[str] = []
    result = recovery.recover_stale_claims(
        requeue_claim=lambda claim: recovered_items.append(claim.item_id) or True
    )

    assert result.scanned == 1
    assert result.recovered == 1
    assert recovered_items == ["evt-stale"]
    assert recovery.claim_item("evt-stale") is True


def test_supervision_prevents_duplicate_claims_for_active_lease_owner() -> None:
    lease_store = InMemoryWorkerLeaseStore()
    first = WorkerSupervisionRuntime(
        queue_name="refactor_inbound",
        worker_id="worker-a",
        lease_store=lease_store,
    )
    second = WorkerSupervisionRuntime(
        queue_name="refactor_inbound",
        worker_id="worker-b",
        lease_store=lease_store,
    )

    assert first.claim_item("msg-duplicate-claim") is True
    assert second.claim_item("msg-duplicate-claim") is False


def test_outbound_worker_recovers_stale_processing_without_duplicate_side_effects() -> None:
    clock = _MutableClock()
    lease_store = InMemoryWorkerLeaseStore(clock=clock)
    repository = _InMemoryOutboxRepository()
    repository.add(_outbox_event(event_id="evt-crash", dedup_key="dedup-crash", status=OutboxStatus.PROCESSING))
    stale_owner = WorkerSupervisionRuntime(
        queue_name="refactor_outbox",
        worker_id="outbox-stale-worker",
        lease_duration_seconds=5,
        lease_store=lease_store,
        heartbeat_tracker=WorkerHeartbeatTracker(clock=clock),
    )
    assert stale_owner.claim_item("evt-crash") is True
    clock.advance(6)

    guard = _IdempotencyGuard()
    guard.mark_processed(
        event_id="already-processed",
        dedup_key="dedup-crash",
        event_type="conversation.state_transitioned",
    )
    dispatched: list[str] = []
    dispatcher = OutboxEventDispatcher()
    dispatcher.register(
        "conversation.state_transitioned",
        lambda event: dispatched.append(event.event_id),
    )
    runtime = OutboxWorkerRuntime(
        outbox_repository=repository,
        dispatcher=dispatcher,
        idempotency_guard=guard,
        supervision=WorkerSupervisionRuntime(
            queue_name="refactor_outbox",
            worker_id="outbox-recovery-worker",
            lease_duration_seconds=5,
            lease_store=lease_store,
            heartbeat_tracker=WorkerHeartbeatTracker(clock=clock),
        ),
    )

    result = runtime.run_once()
    stored = repository.get_event("evt-crash")

    assert result.duplicates == 1
    assert result.sent == 0
    assert repository.recovered == ["evt-crash"]
    assert dispatched == []
    assert stored is not None
    assert stored.status == OutboxStatus.PUBLISHED


def test_inbound_worker_recovers_stale_processing_without_duplicate_orchestration() -> None:
    clock = _MutableClock()
    lease_store = InMemoryWorkerLeaseStore(clock=clock)
    repository = _InMemoryInboundRepository()
    repository.add(_inbound_record(message_id="msg-crash", dedup_key="dedup-crash", status=QueueStatus.PROCESSING))
    stale_owner = WorkerSupervisionRuntime(
        queue_name="refactor_inbound",
        worker_id="inbound-stale-worker",
        lease_duration_seconds=5,
        lease_store=lease_store,
        heartbeat_tracker=WorkerHeartbeatTracker(clock=clock),
    )
    assert stale_owner.claim_item("msg-crash") is True
    clock.advance(6)

    guard = _InboundIdempotencyGuard()
    guard.mark_processed(message_id="already-processed", dedup_key="dedup-crash")
    executed: list[str] = []
    runtime = InboundWorkerRuntime(
        inbound_repository=repository,
        orchestrator=type(
            "_Executor",
            (),
            {"execute": lambda _self, message: executed.append(message.message_id)},
        )(),
        idempotency_guard=guard,
        supervision=WorkerSupervisionRuntime(
            queue_name="refactor_inbound",
            worker_id="inbound-recovery-worker",
            lease_duration_seconds=5,
            lease_store=lease_store,
            heartbeat_tracker=WorkerHeartbeatTracker(clock=clock),
        ),
    )

    result = runtime.run_once()
    stored = repository.get_message("msg-crash")

    assert result.duplicates == 1
    assert result.sent == 0
    assert repository.recovered == ["msg-crash"]
    assert executed == []
    assert stored is not None
    assert stored.status == QueueStatus.SENT

