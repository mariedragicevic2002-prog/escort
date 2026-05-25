from __future__ import annotations

from dataclasses import replace
from typing import Any

from refactor.app.cost_controls import ProcessingBudgetSettings
from refactor.app.middleware.request_validation import RequestValidationMiddleware
from refactor.app.queue.inbound import InboundQueueRecord
from refactor.app.queue.metadata import QueueMessageMetadata
from refactor.app.queue.status import QueueStatus
from refactor.app.runtime.context import RuntimeServices
from refactor.app.runtime.orchestration_facade import OrchestrationFacade
from refactor.app.workers.inbound_orchestrator import RuntimeFacadeInboundOrchestrator
from refactor.app.workers.inbound_runtime import InboundWorkerRuntime
from refactor.app.workers.retry import ExponentialBackoffRetryPolicy


class _InMemoryInboundQueueRepository:
    def __init__(self) -> None:
        self._rows: dict[str, InboundQueueRecord] = {}
        self.transitions: dict[str, list[str]] = {}

    def add(self, message: InboundQueueRecord) -> None:
        self._rows[message.message_id] = message
        self.transitions[message.message_id] = [message.status]

    def mark_processing(self, message_id: str, *, conn: Any | None = None) -> bool:
        _ = conn
        row = self._rows.get(message_id)
        if row is None or row.status not in {QueueStatus.PENDING, QueueStatus.RETRY}:
            return False
        attempt = int(row.metadata.attempt) + 1
        metadata = replace(
            row.metadata,
            attempt=attempt,
            processing_started_at=row.metadata.processing_started_at or "2026-01-01T00:01:00+00:00",
            last_attempt_at="2026-01-01T00:01:00+00:00",
        )
        self._rows[message_id] = replace(
            row,
            status=QueueStatus.PROCESSING,
            metadata=metadata,
            updated_at="2026-01-01T00:01:00+00:00",
        )
        self.transitions[message_id].append(QueueStatus.PROCESSING)
        return True

    def mark_retry(
        self,
        message_id: str,
        *,
        error_message: str,
        retry_delay_seconds: int = 0,
        conn: Any | None = None,
    ) -> bool:
        _ = conn
        row = self._rows.get(message_id)
        if row is None or row.status not in {QueueStatus.PENDING, QueueStatus.PROCESSING}:
            return False
        is_dead = int(row.metadata.attempt) >= int(row.max_attempts)
        next_status = QueueStatus.DEAD if is_dead else QueueStatus.RETRY
        metadata = replace(
            row.metadata,
            available_at=(None if is_dead else f"+{int(retry_delay_seconds)}s"),
            dead_lettered_at=("2026-01-01T00:02:00+00:00" if is_dead else row.metadata.dead_lettered_at),
            last_error_at="2026-01-01T00:02:00+00:00",
        )
        self._rows[message_id] = replace(
            row,
            status=next_status,
            metadata=metadata,
            last_error=error_message,
            updated_at="2026-01-01T00:02:00+00:00",
        )
        self.transitions[message_id].append(next_status)
        return True

    def mark_sent(self, message_id: str, *, conn: Any | None = None) -> bool:
        _ = conn
        row = self._rows.get(message_id)
        if row is None or row.status not in {QueueStatus.PROCESSING, QueueStatus.SENT}:
            return False
        metadata = replace(row.metadata, completed_at="2026-01-01T00:02:30+00:00")
        self._rows[message_id] = replace(
            row,
            status=QueueStatus.SENT,
            metadata=metadata,
            last_error=None,
            updated_at="2026-01-01T00:02:30+00:00",
        )
        self.transitions[message_id].append(QueueStatus.SENT)
        return True

    def mark_dead(
        self,
        message_id: str,
        *,
        error_message: str | None = None,
        conn: Any | None = None,
    ) -> bool:
        _ = conn
        row = self._rows.get(message_id)
        if row is None or row.status not in {QueueStatus.PENDING, QueueStatus.PROCESSING, QueueStatus.RETRY}:
            return False
        metadata = replace(row.metadata, dead_lettered_at="2026-01-01T00:03:00+00:00")
        self._rows[message_id] = replace(
            row,
            status=QueueStatus.DEAD,
            metadata=metadata,
            last_error=error_message or row.last_error,
            updated_at="2026-01-01T00:03:00+00:00",
        )
        self.transitions[message_id].append(QueueStatus.DEAD)
        return True

    def get_message(self, message_id: str, *, conn: Any | None = None) -> InboundQueueRecord | None:
        _ = conn
        return self._rows.get(message_id)

    def list_pending(self, *, limit: int = 100, conn: Any | None = None) -> list[InboundQueueRecord]:
        _ = conn
        rows = [row for row in self._rows.values() if row.status in {QueueStatus.PENDING, QueueStatus.RETRY}]
        rows.sort(key=lambda row: row.created_at or "")
        return rows[: max(1, int(limit))]


class _InMemoryInboundIdempotencyGuard:
    def __init__(self) -> None:
        self._message_ids: set[str] = set()
        self._dedup_keys: set[str] = set()

    def was_processed(
        self,
        *,
        message_id: str,
        dedup_key: str,
        conn: Any | None = None,
    ) -> bool:
        _ = conn
        return message_id in self._message_ids or dedup_key in self._dedup_keys

    def mark_processed(
        self,
        *,
        message_id: str,
        dedup_key: str,
        metadata=None,
        conn: Any | None = None,
    ) -> bool:
        _ = (metadata, conn)
        if message_id in self._message_ids or dedup_key in self._dedup_keys:
            return False
        self._message_ids.add(message_id)
        self._dedup_keys.add(dedup_key)
        return True


class _ExecutorProbe:
    def __init__(self, *, fail_ids: set[str] | None = None) -> None:
        self.fail_ids = set(fail_ids or set())
        self.handled: list[str] = []

    def execute(self, message: InboundQueueRecord) -> None:
        self.handled.append(message.message_id)
        if message.message_id in self.fail_ids:
            raise RuntimeError(f"processing failed for {message.message_id}")


class _MetricsProbe:
    def __init__(self) -> None:
        self.queue_snapshots: list[dict[str, Any]] = []
        self.processing_latency: list[dict[str, Any]] = []
        self.retries: list[dict[str, Any]] = []
        self.dead_letters: list[dict[str, Any]] = []

    def record_queue_snapshot(self, **kwargs: Any) -> None:
        self.queue_snapshots.append(kwargs)

    def record_processing_latency(self, **kwargs: Any) -> None:
        self.processing_latency.append(kwargs)

    def record_retry(self, **kwargs: Any) -> None:
        self.retries.append(kwargs)

    def record_dead_letter(self, **kwargs: Any) -> None:
        self.dead_letters.append(kwargs)


def _record(
    *,
    message_id: str,
    dedup_key: str,
    status: str = QueueStatus.PENDING,
    attempt: int = 0,
    max_attempts: int = 3,
    created_at: str = "2026-01-01T00:00:00+00:00",
) -> InboundQueueRecord:
    return InboundQueueRecord(
        message_id=message_id,
        payload={
            "phone_number": "+61412345678",
            "body": f"body-{message_id}",
            "event_type": "inbound.sms.received",
            "aggregate_type": "conversation_state",
            "request_id": f"req-{message_id}",
        },
        metadata=QueueMessageMetadata(
            attempt=attempt,
            dedup_key=dedup_key,
            correlation_id=f"corr-{message_id}",
            request_id=f"req-{message_id}",
            enqueued_at=created_at,
        ),
        status=status,
        max_attempts=max_attempts,
        last_error=None,
        created_at=created_at,
        updated_at=created_at,
    )


def test_inbound_worker_processes_message_through_runtime_facade() -> None:
    queue = _InMemoryInboundQueueRepository()
    queue.add(_record(message_id="in-success", dedup_key="dedup-success"))
    guard = _InMemoryInboundIdempotencyGuard()
    metrics = _MetricsProbe()
    facade = OrchestrationFacade(
        runtime_services=RuntimeServices(
            state_manager=object(),
            db_service=object(),
            legacy_processor=lambda _phone, body: [f"processed:{body}"],
        ),
        middlewares=[RequestValidationMiddleware()],
    )
    orchestrator = RuntimeFacadeInboundOrchestrator(facade)
    runtime = InboundWorkerRuntime(
        inbound_repository=queue,
        orchestrator=orchestrator,
        idempotency_guard=guard,
        operations_metrics=metrics,
    )

    result = runtime.run_once()
    stored = queue.get_message("in-success")

    assert result.polled == 1
    assert result.sent == 1
    assert stored is not None
    assert stored.status == QueueStatus.SENT
    assert queue.transitions["in-success"] == [
        QueueStatus.PENDING,
        QueueStatus.PROCESSING,
        QueueStatus.SENT,
    ]
    assert len(metrics.processing_latency) == 1
    assert metrics.processing_latency[0]["outcome"] == "sent"


def test_inbound_worker_transitions_retry_and_dead_letter_on_failures() -> None:
    queue = _InMemoryInboundQueueRepository()
    queue.add(_record(message_id="in-retry", dedup_key="dedup-retry", max_attempts=3))
    queue.add(
        _record(
            message_id="in-dead",
            dedup_key="dedup-dead",
            max_attempts=1,
            created_at="2026-01-01T00:00:01+00:00",
        )
    )
    guard = _InMemoryInboundIdempotencyGuard()
    metrics = _MetricsProbe()
    runtime = InboundWorkerRuntime(
        inbound_repository=queue,
        orchestrator=_ExecutorProbe(fail_ids={"in-retry", "in-dead"}),
        idempotency_guard=guard,
        retry_policy=ExponentialBackoffRetryPolicy(base_delay_seconds=9, multiplier=2, max_delay_seconds=60),
        operations_metrics=metrics,
    )

    result = runtime.run_once()
    retry_message = queue.get_message("in-retry")
    dead_message = queue.get_message("in-dead")

    assert result.retried == 1
    assert result.dead_lettered == 1
    assert retry_message is not None
    assert retry_message.status == QueueStatus.RETRY
    assert retry_message.metadata.available_at == "+9s"
    assert dead_message is not None
    assert dead_message.status == QueueStatus.DEAD
    assert dead_message.metadata.dead_lettered_at is not None
    assert len(metrics.retries) == 1
    assert metrics.retries[0]["retry_count"] == 1
    assert len(metrics.dead_letters) == 1
    assert metrics.dead_letters[0]["retry_count"] == 1


def test_inbound_worker_suppresses_duplicate_processing_via_idempotency_guard() -> None:
    queue = _InMemoryInboundQueueRepository()
    queue.add(_record(message_id="in-duplicate", dedup_key="dedup-duplicate"))
    guard = _InMemoryInboundIdempotencyGuard()
    guard.mark_processed(message_id="already-processed", dedup_key="dedup-duplicate")
    executor = _ExecutorProbe()
    runtime = InboundWorkerRuntime(
        inbound_repository=queue,
        orchestrator=executor,
        idempotency_guard=guard,
    )

    result = runtime.run_once()
    stored = queue.get_message("in-duplicate")

    assert result.duplicates == 1
    assert result.sent == 0
    assert executor.handled == []
    assert stored is not None
    assert stored.status == QueueStatus.SENT
    assert queue.transitions["in-duplicate"] == [
        QueueStatus.PENDING,
        QueueStatus.PROCESSING,
        QueueStatus.SENT,
    ]


def test_inbound_worker_respects_deterministic_batch_poll_order() -> None:
    queue = _InMemoryInboundQueueRepository()
    queue.add(
        _record(
            message_id="msg-late",
            dedup_key="dedup-late",
            created_at="2026-01-01T00:00:03+00:00",
        )
    )
    queue.add(
        _record(
            message_id="msg-first",
            dedup_key="dedup-first",
            created_at="2026-01-01T00:00:01+00:00",
        )
    )
    queue.add(
        _record(
            message_id="msg-middle",
            dedup_key="dedup-middle",
            created_at="2026-01-01T00:00:02+00:00",
        )
    )
    executor = _ExecutorProbe()
    runtime = InboundWorkerRuntime(
        inbound_repository=queue,
        orchestrator=executor,
        idempotency_guard=_InMemoryInboundIdempotencyGuard(),
    )

    result = runtime.run_once(batch_size=3)

    assert result.sent == 3
    assert executor.handled == ["msg-first", "msg-middle", "msg-late"]


def test_inbound_worker_respects_processing_budget_per_pass() -> None:
    queue = _InMemoryInboundQueueRepository()
    queue.add(_record(message_id="budget-1", dedup_key="dedup-budget-1"))
    queue.add(
        _record(
            message_id="budget-2",
            dedup_key="dedup-budget-2",
            created_at="2026-01-01T00:00:01+00:00",
        )
    )
    executor = _ExecutorProbe()
    runtime = InboundWorkerRuntime(
        inbound_repository=queue,
        orchestrator=executor,
        idempotency_guard=_InMemoryInboundIdempotencyGuard(),
        processing_budget_settings=ProcessingBudgetSettings(
            max_items_per_worker_pass=1,
            max_items_per_interval=10,
            interval_seconds=60,
        ),
    )

    result = runtime.run_once(batch_size=5)
    first = queue.get_message("budget-1")
    second = queue.get_message("budget-2")

    assert result.polled == 1
    assert result.sent == 1
    assert executor.handled == ["budget-1"]
    assert first is not None and first.status == QueueStatus.SENT
    assert second is not None and second.status == QueueStatus.PENDING
