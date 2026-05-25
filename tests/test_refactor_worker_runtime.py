from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from refactor.app.cost_controls import ProcessingBudgetSettings
from refactor.app.events.outbox import OutboxEventRecord, OutboxStatus
from refactor.app.workers.dispatcher import OutboxEventDispatcher
from refactor.app.workers.retry import ExponentialBackoffRetryPolicy
from refactor.app.workers.runtime import OutboxWorkerRuntime


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class _InMemoryOutboxRepository:
    def __init__(self) -> None:
        self._rows: dict[str, OutboxEventRecord] = {}
        self.transitions: dict[str, list[str]] = {}

    def add(self, event: OutboxEventRecord) -> None:
        self._rows[event.event_id] = event
        self.transitions[event.event_id] = [event.status]

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
        self.transitions[event_id].append(OutboxStatus.PROCESSING)
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
        next_retry = int(event.retry_count) + 1
        is_dead = next_retry >= int(event.max_retries)
        next_status = OutboxStatus.DEAD_LETTER if is_dead else OutboxStatus.FAILED
        self._rows[event_id] = replace(
            event,
            status=next_status,
            retry_count=next_retry,
            next_retry_at=(None if is_dead else f"+{int(retry_delay_seconds)}s"),
            last_error=error_message,
            last_error_at=_now_iso(),
            dead_lettered_at=(_now_iso() if is_dead else event.dead_lettered_at),
            updated_at=_now_iso(),
        )
        self.transitions[event_id].append(next_status)
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
            updated_at=_now_iso(),
        )
        self.transitions[event_id].append(OutboxStatus.PUBLISHED)
        return True

    def list_pending(self, *, limit: int = 100, conn: Any | None = None) -> list[OutboxEventRecord]:
        _ = conn
        candidates = [
            event
            for event in self._rows.values()
            if event.status in {OutboxStatus.PENDING, OutboxStatus.FAILED}
        ]
        candidates.sort(key=lambda event: event.created_at or "")
        return candidates[: max(1, int(limit))]

    def get_event(self, event_id: str, *, conn: Any | None = None) -> OutboxEventRecord | None:
        _ = conn
        return self._rows.get(event_id)


class _NoGetEventOutboxRepository(_InMemoryOutboxRepository):
    def get_event(self, event_id: str, *, conn: Any | None = None) -> OutboxEventRecord | None:
        _ = (event_id, conn)
        raise AssertionError("runtime should not call get_event during failure handling")


class _InMemoryIdempotencyGuard:
    def __init__(self) -> None:
        self._processed_event_ids: set[str] = set()
        self._processed_dedup_keys: set[str] = set()

    def was_processed(
        self,
        *,
        event_id: str,
        dedup_key: str,
        conn: Any | None = None,
    ) -> bool:
        _ = conn
        return event_id in self._processed_event_ids or dedup_key in self._processed_dedup_keys

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
        if event_id in self._processed_event_ids or dedup_key in self._processed_dedup_keys:
            return False
        self._processed_event_ids.add(event_id)
        self._processed_dedup_keys.add(dedup_key)
        return True


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


def _event_record(
    *,
    event_id: str,
    idempotency_key: str,
    status: str = OutboxStatus.PENDING,
    retry_count: int = 0,
    max_retries: int = 3,
) -> OutboxEventRecord:
    now = "2026-01-01T00:00:00+00:00"
    return OutboxEventRecord(
        event_id=event_id,
        idempotency_key=idempotency_key,
        event_type="conversation.state_transitioned",
        aggregate_type="conversation_state",
        aggregate_id="+61400000000",
        payload={"from_state": "NEW", "to_state": "COLLECTING"},
        metadata={"source": "tests"},
        status=status,
        retry_count=retry_count,
        max_retries=max_retries,
        next_retry_at=None,
        processing_started_at=None,
        last_attempt_at=None,
        last_error=None,
        last_error_at=None,
        dead_lettered_at=None,
        occurred_at=now,
        created_at=now,
        updated_at=now,
    )


def test_worker_success_marks_event_sent() -> None:
    outbox = _InMemoryOutboxRepository()
    guard = _InMemoryIdempotencyGuard()
    dispatcher = OutboxEventDispatcher()
    metrics = _MetricsProbe()
    handled_event_ids: list[str] = []

    dispatcher.register(
        "conversation.state_transitioned",
        lambda event: handled_event_ids.append(event.event_id),
    )
    outbox.add(_event_record(event_id="evt-success", idempotency_key="idem-success"))

    runtime = OutboxWorkerRuntime(
        outbox_repository=outbox,
        dispatcher=dispatcher,
        idempotency_guard=guard,
        operations_metrics=metrics,
    )
    result = runtime.run_once()
    stored = outbox.get_event("evt-success")

    assert result.sent == 1
    assert result.polled == 1
    assert handled_event_ids == ["evt-success"]
    assert stored is not None
    assert stored.status == OutboxStatus.PUBLISHED
    assert outbox.transitions["evt-success"] == [
        OutboxStatus.PENDING,
        OutboxStatus.PROCESSING,
        OutboxStatus.PUBLISHED,
    ]
    assert len(metrics.queue_snapshots) == 1
    assert metrics.queue_snapshots[0]["queue_name"] == "refactor_outbox"
    assert metrics.queue_snapshots[0]["batch_size"] == 25
    assert len(metrics.processing_latency) == 1
    assert metrics.processing_latency[0]["outcome"] == "sent"
    assert metrics.processing_latency[0]["duration_ms"] >= 0
    assert metrics.retries == []
    assert metrics.dead_letters == []


def test_worker_failure_increments_retry_metadata() -> None:
    outbox = _NoGetEventOutboxRepository()
    guard = _InMemoryIdempotencyGuard()
    dispatcher = OutboxEventDispatcher()
    metrics = _MetricsProbe()

    def _raise(_event: OutboxEventRecord) -> None:
        raise RuntimeError("downstream timeout")

    dispatcher.register("conversation.state_transitioned", _raise)
    outbox.add(_event_record(event_id="evt-retry", idempotency_key="idem-retry", max_retries=3))
    policy = ExponentialBackoffRetryPolicy(base_delay_seconds=7, multiplier=2, max_delay_seconds=60)
    runtime = OutboxWorkerRuntime(
        outbox_repository=outbox,
        dispatcher=dispatcher,
        idempotency_guard=guard,
        retry_policy=policy,
        operations_metrics=metrics,
    )

    result = runtime.run_once()
    stored = outbox._rows.get("evt-retry")

    assert result.retried == 1
    assert stored is not None
    assert stored.status == OutboxStatus.FAILED
    assert stored.retry_count == 1
    assert stored.last_error == "downstream timeout"
    assert stored.next_retry_at == "+7s"
    assert outbox.transitions["evt-retry"] == [
        OutboxStatus.PENDING,
        OutboxStatus.PROCESSING,
        OutboxStatus.FAILED,
    ]
    assert len(metrics.processing_latency) == 1
    assert metrics.processing_latency[0]["outcome"] == "retry"
    assert len(metrics.retries) == 1
    assert metrics.retries[0]["retry_count"] == 1
    assert metrics.dead_letters == []


def test_worker_moves_to_dead_letter_when_retry_budget_exhausted() -> None:
    outbox = _NoGetEventOutboxRepository()
    guard = _InMemoryIdempotencyGuard()
    dispatcher = OutboxEventDispatcher()
    metrics = _MetricsProbe()

    def _always_fail(_event: OutboxEventRecord) -> None:
        raise RuntimeError("boom")

    dispatcher.register("conversation.state_transitioned", _always_fail)
    outbox.add(_event_record(event_id="evt-dead", idempotency_key="idem-dead", max_retries=1))
    runtime = OutboxWorkerRuntime(
        outbox_repository=outbox,
        dispatcher=dispatcher,
        idempotency_guard=guard,
        operations_metrics=metrics,
    )

    result = runtime.run_once()
    stored = outbox._rows.get("evt-dead")

    assert result.dead_lettered == 1
    assert stored is not None
    assert stored.status == OutboxStatus.DEAD_LETTER
    assert stored.retry_count == 1
    assert stored.dead_lettered_at is not None
    assert outbox.transitions["evt-dead"] == [
        OutboxStatus.PENDING,
        OutboxStatus.PROCESSING,
        OutboxStatus.DEAD_LETTER,
    ]
    assert len(metrics.processing_latency) == 1
    assert metrics.processing_latency[0]["outcome"] == "dead_letter"
    assert metrics.retries == []
    assert len(metrics.dead_letters) == 1
    assert metrics.dead_letters[0]["retry_count"] == 1


def test_worker_idempotency_guard_prevents_duplicate_side_effects() -> None:
    outbox = _InMemoryOutboxRepository()
    guard = _InMemoryIdempotencyGuard()
    dispatcher = OutboxEventDispatcher()
    side_effect_counter = {"count": 0}

    def _handler(_event: OutboxEventRecord) -> None:
        side_effect_counter["count"] += 1

    dispatcher.register("conversation.state_transitioned", _handler)
    guard.mark_processed(
        event_id="evt-original",
        dedup_key="idem-duplicate",
        event_type="conversation.state_transitioned",
    )
    outbox.add(_event_record(event_id="evt-duplicate", idempotency_key="idem-duplicate"))
    runtime = OutboxWorkerRuntime(
        outbox_repository=outbox,
        dispatcher=dispatcher,
        idempotency_guard=guard,
    )

    result = runtime.run_once()
    stored = outbox.get_event("evt-duplicate")

    assert result.duplicates == 1
    assert result.sent == 0
    assert side_effect_counter["count"] == 0
    assert stored is not None
    assert stored.status == OutboxStatus.PUBLISHED
    assert outbox.transitions["evt-duplicate"] == [
        OutboxStatus.PENDING,
        OutboxStatus.PROCESSING,
        OutboxStatus.PUBLISHED,
    ]


def test_worker_respects_processing_budget_per_pass() -> None:
    outbox = _InMemoryOutboxRepository()
    guard = _InMemoryIdempotencyGuard()
    dispatcher = OutboxEventDispatcher()
    handled_event_ids: list[str] = []
    dispatcher.register(
        "conversation.state_transitioned",
        lambda event: handled_event_ids.append(event.event_id),
    )
    outbox.add(_event_record(event_id="evt-budget-1", idempotency_key="idem-budget-1"))
    outbox.add(_event_record(event_id="evt-budget-2", idempotency_key="idem-budget-2"))
    runtime = OutboxWorkerRuntime(
        outbox_repository=outbox,
        dispatcher=dispatcher,
        idempotency_guard=guard,
        processing_budget_settings=ProcessingBudgetSettings(
            max_items_per_worker_pass=1,
            max_items_per_interval=10,
            interval_seconds=60,
        ),
    )

    result = runtime.run_once(batch_size=5)
    first = outbox.get_event("evt-budget-1")
    second = outbox.get_event("evt-budget-2")

    assert result.polled == 1
    assert result.sent == 1
    assert handled_event_ids == ["evt-budget-1"]
    assert first is not None and first.status == OutboxStatus.PUBLISHED
    assert second is not None and second.status == OutboxStatus.PENDING
