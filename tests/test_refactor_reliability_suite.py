from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any

from app.ingress.backpressure_policy import (
    IngressBackpressureSettings,
    resolve_ingress_backpressure_decision,
)
from app.ops.operator_recovery_service import (
    QueuePauseCommand,
    QueuePauseService,
    QueueResumeCommand,
    StuckJobInspectionQuery,
    StuckJobInspectionService,
)
from app.queue.inbound import InboundQueueRecord
from app.queue.metadata import QueueMessageMetadata
from app.queue.status import QueueStatus
from app.workers.idempotency import DatabaseIdempotentConsumerGuard
from app.workers.inbound_idempotency import DatabaseInboundIdempotencyGuard
from app.workers.inbound_runtime import InboundWorkerRuntime
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


class _BackpressureProvider:
    def __init__(self, pending: list[InboundQueueRecord]) -> None:
        self._pending = list(pending)

    def list_pending(self, *, limit: int = 100, conn: Any | None = None) -> list[InboundQueueRecord]:
        _ = conn
        return self._pending[: max(1, int(limit))]


class _FailingBackpressureProvider:
    def list_pending(self, *, limit: int = 100, conn: Any | None = None) -> list[InboundQueueRecord]:
        _ = (limit, conn)
        raise RuntimeError("injected metrics failure")


class _StatefulInboundRepository:
    def __init__(self, *, fail_recovery: bool = False) -> None:
        self._rows: dict[str, InboundQueueRecord] = {}
        self.fail_recovery = fail_recovery
        self.recover_calls = 0

    def add(self, record: InboundQueueRecord) -> None:
        self._rows[record.message_id] = record

    def get_message(self, message_id: str, *, conn: Any | None = None) -> InboundQueueRecord | None:
        _ = conn
        return self._rows.get(message_id)

    def list_pending(self, *, limit: int = 100, conn: Any | None = None) -> list[InboundQueueRecord]:
        _ = conn
        rows = [row for row in self._rows.values() if row.status in {QueueStatus.PENDING, QueueStatus.RETRY}]
        rows.sort(key=lambda row: (row.created_at or "", row.message_id))
        return rows[: max(1, int(limit))]

    def list_dead(self, *, limit: int = 100, conn: Any | None = None) -> list[InboundQueueRecord]:
        _ = conn
        rows = [row for row in self._rows.values() if row.status == QueueStatus.DEAD]
        rows.sort(key=lambda row: (row.created_at or "", row.message_id))
        return rows[: max(1, int(limit))]

    def mark_processing(self, message_id: str, *, conn: Any | None = None) -> bool:
        _ = conn
        record = self._rows.get(message_id)
        if record is None or record.status not in {QueueStatus.PENDING, QueueStatus.RETRY}:
            return False
        metadata = replace(record.metadata, attempt=max(0, int(record.metadata.attempt)) + 1)
        self._rows[message_id] = replace(record, status=QueueStatus.PROCESSING, metadata=metadata)
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
        record = self._rows.get(message_id)
        if record is None or record.status not in {QueueStatus.PENDING, QueueStatus.PROCESSING}:
            return False
        self._rows[message_id] = replace(record, status=QueueStatus.RETRY, last_error=error_message)
        return True

    def mark_sent(self, message_id: str, *, conn: Any | None = None) -> bool:
        _ = conn
        record = self._rows.get(message_id)
        if record is None or record.status not in {QueueStatus.PROCESSING, QueueStatus.SENT}:
            return False
        self._rows[message_id] = replace(record, status=QueueStatus.SENT, last_error=None)
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
        self.recover_calls += 1
        if self.fail_recovery:
            return False
        record = self._rows.get(message_id)
        if record is None or record.status != QueueStatus.PROCESSING:
            return False
        self._rows[message_id] = replace(record, status=QueueStatus.RETRY, last_error=error_message)
        return True


class _NoopInboundGuard:
    def was_processed(self, *, message_id: str, dedup_key: str, conn: Any | None = None) -> bool:
        _ = (message_id, dedup_key, conn)
        return False

    def mark_processed(self, *, message_id: str, dedup_key: str, metadata=None, conn: Any | None = None) -> bool:
        _ = (message_id, dedup_key, metadata, conn)
        return True


class _RecordingOrchestrator:
    def __init__(self) -> None:
        self.handled: list[str] = []

    def execute(self, message: InboundQueueRecord) -> None:
        self.handled.append(message.message_id)


class _EmptyOutboundRepository:
    def list_pending(self, *, limit: int = 100, conn: Any | None = None) -> list[Any]:
        _ = (limit, conn)
        return []

    def list_dead(self, *, limit: int = 100, conn: Any | None = None) -> list[Any]:
        _ = (limit, conn)
        return []


class _ThreadSafeInboundGuardDB:
    def __init__(self) -> None:
        self._by_message_id: set[str] = set()
        self._by_dedup_key: set[str] = set()
        self._lock = Lock()

    def execute_query(self, query, params=(), fetch=False, conn=None, **_kwargs):
        _ = conn
        sql = " ".join(str(query).split()).lower()
        if "create table if not exists refactor_inbound_worker_guard" in sql:
            return [] if fetch else None
        if "create index if not exists idx_refactor_inbound_worker_guard_processed" in sql:
            return [] if fetch else None
        if "insert into refactor_inbound_worker_guard" in sql:
            message_id, dedup_key, _metadata = params
            with self._lock:
                if message_id in self._by_message_id or dedup_key in self._by_dedup_key:
                    return [] if fetch else None
                self._by_message_id.add(message_id)
                self._by_dedup_key.add(dedup_key)
            return [{"message_id": message_id}] if fetch else None
        if "from refactor_inbound_worker_guard" in sql and "where message_id = %s or dedup_key = %s" in sql:
            message_id, dedup_key = params
            with self._lock:
                seen = message_id in self._by_message_id or dedup_key in self._by_dedup_key
            return [{"message_id": message_id}] if fetch and seen else []
        raise AssertionError(f"Unexpected SQL in inbound guard DB fake: {query}")


class _ThreadSafeOutboxGuardDB:
    def __init__(self) -> None:
        self._by_event_id: set[str] = set()
        self._by_dedup_key: set[str] = set()
        self._lock = Lock()

    def execute_query(self, query, params=(), fetch=False, conn=None, **_kwargs):
        _ = conn
        sql = " ".join(str(query).split()).lower()
        if "create table if not exists refactor_outbox_consumer_guard" in sql:
            return [] if fetch else None
        if "create index if not exists idx_refactor_outbox_consumer_guard_event_type" in sql:
            return [] if fetch else None
        if "insert into refactor_outbox_consumer_guard" in sql:
            event_id, dedup_key, _event_type, _metadata = params
            with self._lock:
                if event_id in self._by_event_id or dedup_key in self._by_dedup_key:
                    return [] if fetch else None
                self._by_event_id.add(event_id)
                self._by_dedup_key.add(dedup_key)
            return [{"event_id": event_id}] if fetch else None
        if "from refactor_outbox_consumer_guard" in sql and "where event_id = %s or dedup_key = %s" in sql:
            event_id, dedup_key = params
            with self._lock:
                seen = event_id in self._by_event_id or dedup_key in self._by_dedup_key
            return [{"event_id": event_id}] if fetch and seen else []
        raise AssertionError(f"Unexpected SQL in outbox guard DB fake: {query}")


def _inbound_record(*, message_id: str, status: str, enqueued_at: str) -> InboundQueueRecord:
    return InboundQueueRecord(
        message_id=message_id,
        payload={"event_type": "inbound.sms.received", "aggregate_id": f"agg-{message_id}"},
        metadata=QueueMessageMetadata(
            dedup_key=f"dedup-{message_id}",
            request_id=f"req-{message_id}",
            enqueued_at=enqueued_at,
            attempt=1 if status == QueueStatus.PROCESSING else 0,
        ),
        status=status,
        max_attempts=4,
        last_error=None,
        created_at=enqueued_at,
        updated_at=enqueued_at,
    )


def test_backpressure_lag_edge_case_triggers_overload_without_depth_exhaustion() -> None:
    old_enqueued = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    provider = _BackpressureProvider([_inbound_record(message_id="bp-lag", status=QueueStatus.PENDING, enqueued_at=old_enqueued)])
    decision = resolve_ingress_backpressure_decision(
        settings=IngressBackpressureSettings(
            max_queue_depth=10,
            max_lag_seconds=30,
            overload_behavior="reject",
            degrade_max_attempts=1,
        ),
        inbound_provider=provider,
        requested_max_attempts=5,
    )

    assert decision.overloaded is True
    assert decision.allow_enqueue is False
    assert decision.reason == "backpressure_reject"
    assert decision.trigger == "lag"
    assert decision.queue_depth == 1
    assert decision.oldest_lag_seconds is not None
    assert decision.oldest_lag_seconds >= 30


def test_backpressure_metrics_failure_forces_conservative_sync_fallback_even_in_degrade_mode() -> None:
    decision = resolve_ingress_backpressure_decision(
        settings=IngressBackpressureSettings(
            max_queue_depth=1,
            max_lag_seconds=1,
            overload_behavior="degrade_mode",
            degrade_max_attempts=1,
        ),
        inbound_provider=_FailingBackpressureProvider(),
        requested_max_attempts=5,
    )

    assert decision.overloaded is True
    assert decision.allow_enqueue is False
    assert decision.behavior == "sync_fallback"
    assert decision.reason == "backpressure_metrics_unavailable_sync_fallback"
    assert decision.trigger == "metrics_unavailable"
    assert decision.provider_available is False


def test_worker_stale_claim_recovery_retries_after_transient_requeue_failure() -> None:
    clock = _MutableClock()
    lease_store = InMemoryWorkerLeaseStore(clock=clock)
    repository = _StatefulInboundRepository(fail_recovery=True)
    repository.add(
        _inbound_record(
            message_id="msg-stale",
            status=QueueStatus.PROCESSING,
            enqueued_at="2026-01-01T00:00:00+00:00",
        )
    )
    stale_worker = WorkerSupervisionRuntime(
        queue_name="refactor_inbound",
        worker_id="worker-crashed",
        lease_duration_seconds=5,
        lease_store=lease_store,
        heartbeat_tracker=WorkerHeartbeatTracker(clock=clock),
    )
    assert stale_worker.claim_item("msg-stale") is True
    clock.advance(6)

    orchestrator = _RecordingOrchestrator()
    runtime = InboundWorkerRuntime(
        inbound_repository=repository,
        orchestrator=orchestrator,
        idempotency_guard=_NoopInboundGuard(),
        supervision=WorkerSupervisionRuntime(
            queue_name="refactor_inbound",
            worker_id="worker-recovery",
            lease_duration_seconds=5,
            lease_store=lease_store,
            heartbeat_tracker=WorkerHeartbeatTracker(clock=clock),
        ),
    )

    first = runtime.run_once()
    assert first.polled == 0
    assert repository.recover_calls == 1
    assert repository.get_message("msg-stale") is not None
    assert repository.get_message("msg-stale").status == QueueStatus.PROCESSING  # type: ignore[union-attr]
    assert orchestrator.handled == []

    repository.fail_recovery = False
    second = runtime.run_once()
    assert second.sent == 1
    assert repository.recover_calls == 2
    assert repository.get_message("msg-stale").status == QueueStatus.SENT  # type: ignore[union-attr]
    assert orchestrator.handled == ["msg-stale"]


def test_idempotency_guards_allow_single_winner_under_contention() -> None:
    inbound_guard = DatabaseInboundIdempotencyGuard(_ThreadSafeInboundGuardDB())
    outbox_guard = DatabaseIdempotentConsumerGuard(_ThreadSafeOutboxGuardDB())

    with ThreadPoolExecutor(max_workers=8) as pool:
        inbound_results = list(
            pool.map(
                lambda idx: inbound_guard.mark_processed(
                    message_id=f"inbound-{idx}",
                    dedup_key="shared-dedup-key",
                ),
                range(8),
            )
        )
    with ThreadPoolExecutor(max_workers=8) as pool:
        outbox_results = list(
            pool.map(
                lambda idx: outbox_guard.mark_processed(
                    event_id=f"event-{idx}",
                    dedup_key="shared-dedup-key",
                    event_type="conversation.state_transitioned",
                ),
                range(8),
            )
        )

    assert sum(1 for result in inbound_results if result) == 1
    assert sum(1 for result in outbox_results if result) == 1
    assert inbound_guard.was_processed(message_id="not-present", dedup_key="shared-dedup-key") is True
    assert outbox_guard.was_processed(event_id="not-present", dedup_key="shared-dedup-key") is True


def test_operator_pause_controls_block_recovery_until_queue_is_resumed() -> None:
    clock = _MutableClock()
    lease_store = InMemoryWorkerLeaseStore(clock=clock)
    repository = _StatefulInboundRepository()
    repository.add(
        _inbound_record(
            message_id="msg-paused",
            status=QueueStatus.PROCESSING,
            enqueued_at="2026-01-01T00:00:00+00:00",
        )
    )
    stale_worker = WorkerSupervisionRuntime(
        queue_name="refactor_inbound",
        worker_id="worker-paused-crashed",
        lease_duration_seconds=5,
        lease_store=lease_store,
        heartbeat_tracker=WorkerHeartbeatTracker(clock=clock),
    )
    assert stale_worker.claim_item("msg-paused") is True
    clock.advance(6)

    pause_service = QueuePauseService(max_pause_seconds=300, audit_logger=lambda *_: None)
    pause_service.pause_queue(
        QueuePauseCommand(
            actor="ops-user",
            reason="maintenance",
            queue_name="refactor_inbound",
            duration_seconds=120,
            granted_permissions={"queue:pause"},
        )
    )

    orchestrator = _RecordingOrchestrator()
    runtime = InboundWorkerRuntime(
        inbound_repository=repository,
        orchestrator=orchestrator,
        idempotency_guard=_NoopInboundGuard(),
        queue_pause_reader=pause_service,
        supervision=WorkerSupervisionRuntime(
            queue_name="refactor_inbound",
            worker_id="worker-paused-recovery",
            lease_duration_seconds=5,
            lease_store=lease_store,
            heartbeat_tracker=WorkerHeartbeatTracker(clock=clock),
        ),
    )

    paused_batch = runtime.run_once()
    assert paused_batch.polled == 0
    assert repository.recover_calls == 0

    inspection = StuckJobInspectionService(
        inbound_repository=repository,
        outbound_repository=_EmptyOutboundRepository(),
        lease_store=lease_store,
        audit_logger=lambda *_: None,
    )
    paused_inspection = inspection.inspect(
        StuckJobInspectionQuery(
            actor="ops-user",
            granted_permissions={"queue:inspect"},
            direction="inbound",
            statuses=("stale_claim",),
            limit=10,
        )
    )
    assert paused_inspection.summary["stale_claim"] == 1
    assert [item.item_id for item in paused_inspection.items] == ["msg-paused"]

    pause_service.resume_queue(
        QueueResumeCommand(
            actor="ops-user",
            reason="maintenance complete",
            queue_name="refactor_inbound",
            granted_permissions={"queue:pause"},
        )
    )
    resumed_batch = runtime.run_once()

    assert repository.recover_calls == 1
    assert resumed_batch.sent == 1
    assert orchestrator.handled == ["msg-paused"]
    assert repository.get_message("msg-paused") is not None
    assert repository.get_message("msg-paused").status == QueueStatus.SENT  # type: ignore[union-attr]
