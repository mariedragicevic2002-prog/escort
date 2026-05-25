from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, BrokenBarrierError, Lock
from typing import Any

import main_v2.sms_gateway as sms_gateway
from refactor.adapters.sms_outbound_adapter import SMSOutboundAdapter
from refactor.app.events.outbox import OutboxEventEnvelope, OutboxEventRecord, OutboxStatus
from refactor.app.ingress.quick_ack import try_enqueue_sms_quick_ack, try_enqueue_webhook_quick_ack
from refactor.app.outbound import OutboundDispatcher, OutboundMessage, OutboundQueuePublisher
from refactor.app.queue import (
    DatabaseInboundQueueRepository,
    DatabaseOutboundQueueRepository,
    InboundQueueRecord,
    QueueMessageMetadata,
    QueueStatus,
)
from refactor.app.runtime.response_composer import ComposedResponse
from refactor.app.workers.dispatcher import OutboxEventDispatcher
from refactor.app.workers.inbound_runtime import InboundWorkerRuntime
from refactor.app.workers.outbound_sender import register_outbound_sender_handler
from refactor.app.workers.runtime import OutboxWorkerRuntime


class _InMemoryInboundQueueDB:
    def __init__(self) -> None:
        self._rows: dict[str, dict[str, Any]] = {}
        self._dedup_index: dict[str, str] = {}
        self._create_counter = 0

    def execute_query(self, query, params=(), fetch=False, conn=None, **_kwargs):
        _ = conn
        sql = " ".join(str(query).split()).lower()
        # DDL passthrough — all CREATE TABLE / CREATE INDEX statements are no-ops in tests
        if sql.startswith("create table") or sql.startswith("create index"):
            return [] if fetch else None

        if "insert into refactor_inbound_queue_messages" in sql:
            (
                message_id,
                dedup_key,
                payload_json,
                metadata_json,
                status,
                max_attempts,
                received_at,
            ) = params
            if message_id in self._rows or dedup_key in self._dedup_index:
                return [] if fetch else None
            import json

            self._create_counter += 1
            timestamp = f"2026-01-01T00:00:{self._create_counter:02d}+00:00"
            self._rows[message_id] = {
                "message_id": message_id,
                "dedup_key": dedup_key,
                "payload": json.loads(payload_json),
                "metadata": json.loads(metadata_json),
                "status": status,
                "attempt": 0,
                "max_attempts": max_attempts,
                "next_attempt_at": None,
                "processing_started_at": None,
                "last_attempt_at": None,
                "last_error": None,
                "last_error_at": None,
                "dead_lettered_at": None,
                "received_at": received_at,
                "created_at": timestamp,
                "updated_at": timestamp,
            }
            self._dedup_index[dedup_key] = message_id
            return [{"message_id": message_id}] if fetch else None

        if "set status = 'processing'" in sql and "attempt = attempt + 1" in sql:
            (message_id,) = params
            row = self._rows.get(message_id)
            if row is None or row["status"] not in {QueueStatus.PENDING, QueueStatus.RETRY}:
                return [] if fetch else None
            row["status"] = QueueStatus.PROCESSING
            row["attempt"] = int(row["attempt"]) + 1
            row["processing_started_at"] = row["processing_started_at"] or "2026-01-01T00:01:00+00:00"
            row["last_attempt_at"] = "2026-01-01T00:01:00+00:00"
            row["updated_at"] = "2026-01-01T00:01:00+00:00"
            return [{"message_id": message_id}] if fetch else None

        if "set status = case" in sql and "when attempt >= max_attempts then 'dead'" in sql:
            delay_seconds, _delay_seconds2, error_message, message_id = params
            row = self._rows.get(message_id)
            if row is None or row["status"] not in {QueueStatus.PENDING, QueueStatus.PROCESSING}:
                return [] if fetch else None
            row["last_error"] = error_message
            row["last_error_at"] = "2026-01-01T00:02:00+00:00"
            if int(row["attempt"]) >= int(row["max_attempts"]):
                row["status"] = QueueStatus.DEAD
                row["next_attempt_at"] = None
                row["dead_lettered_at"] = "2026-01-01T00:02:00+00:00"
            else:
                row["status"] = QueueStatus.RETRY
                row["next_attempt_at"] = f"+{int(delay_seconds)}s"
            row["updated_at"] = "2026-01-01T00:02:00+00:00"
            return [{"message_id": message_id}] if fetch else None

        if "set status = 'sent'" in sql and "where message_id = %s" in sql:
            (message_id,) = params
            row = self._rows.get(message_id)
            if row is None or row["status"] not in {QueueStatus.PROCESSING, QueueStatus.SENT}:
                return [] if fetch else None
            row["status"] = QueueStatus.SENT
            row["next_attempt_at"] = None
            row["last_error"] = None
            row["last_error_at"] = None
            row["updated_at"] = "2026-01-01T00:03:00+00:00"
            return [{"message_id": message_id}] if fetch else None

        if "where status in ('pending', 'retry')" in sql and "limit %s" in sql:
            (limit,) = params
            rows = [
                dict(row)
                for row in self._rows.values()
                if row["status"] in {QueueStatus.PENDING, QueueStatus.RETRY}
            ]
            rows.sort(key=lambda row: str(row.get("created_at") or ""))
            return rows[: int(limit)] if fetch else None

        if "from refactor_inbound_queue_messages" in sql and "where message_id = %s" in sql:
            (message_id,) = params
            row = self._rows.get(message_id)
            return ([dict(row)] if row is not None else []) if fetch else None

        if "from refactor_inbound_queue_messages" in sql and "where status = 'dead'" in sql:
            (limit,) = params
            rows = [dict(row) for row in self._rows.values() if row["status"] == QueueStatus.DEAD]
            rows.sort(key=lambda r: str(r.get("created_at") or ""))
            return rows[: int(limit)] if fetch else None

        raise AssertionError(f"Unexpected SQL in in-memory inbound db: {query}")


class _InMemoryOutboxRepository:
    def __init__(self) -> None:
        self.rows: dict[str, OutboxEventRecord] = {}
        self._idempotency_index: dict[str, str] = {}

    def append_event(self, event: OutboxEventEnvelope, *, conn: Any | None = None) -> bool:
        _ = conn
        key = event.normalized_idempotency_key
        if event.event_id in self.rows or key in self._idempotency_index:
            return False
        now = "2026-01-01T00:00:00+00:00"
        self.rows[event.event_id] = OutboxEventRecord(
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
            created_at=now,
            updated_at=now,
        )
        self._idempotency_index[key] = event.event_id
        return True

    def mark_processing(self, event_id: str, *, conn: Any | None = None) -> bool:
        _ = conn
        event = self.rows.get(event_id)
        if event is None or event.status not in {OutboxStatus.PENDING, OutboxStatus.FAILED}:
            return False
        self.rows[event_id] = replace(
            event,
            status=OutboxStatus.PROCESSING,
            processing_started_at=event.processing_started_at or "2026-01-01T00:01:00+00:00",
            last_attempt_at="2026-01-01T00:01:00+00:00",
            updated_at="2026-01-01T00:01:00+00:00",
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
        event = self.rows.get(event_id)
        if event is None or event.status not in {OutboxStatus.PENDING, OutboxStatus.PROCESSING}:
            return False
        next_retry_count = int(event.retry_count) + 1
        dead_letter = next_retry_count >= int(event.max_retries)
        self.rows[event_id] = replace(
            event,
            status=OutboxStatus.DEAD_LETTER if dead_letter else OutboxStatus.FAILED,
            retry_count=next_retry_count,
            next_retry_at=(None if dead_letter else f"+{int(retry_delay_seconds)}s"),
            last_error=error_message,
            last_error_at="2026-01-01T00:02:00+00:00",
            dead_lettered_at=("2026-01-01T00:02:00+00:00" if dead_letter else event.dead_lettered_at),
            updated_at="2026-01-01T00:02:00+00:00",
        )
        return True

    def mark_published(self, event_id: str, *, conn: Any | None = None) -> bool:
        _ = conn
        event = self.rows.get(event_id)
        if event is None or event.status not in {OutboxStatus.PROCESSING, OutboxStatus.PUBLISHED}:
            return False
        self.rows[event_id] = replace(
            event,
            status=OutboxStatus.PUBLISHED,
            next_retry_at=None,
            last_error=None,
            last_error_at=None,
            updated_at="2026-01-01T00:03:00+00:00",
        )
        return True

    def get_event(self, event_id: str, *, conn: Any | None = None) -> OutboxEventRecord | None:
        _ = conn
        return self.rows.get(event_id)

    def list_pending(self, *, limit: int = 100, conn: Any | None = None) -> list[OutboxEventRecord]:
        _ = conn
        rows = [event for event in self.rows.values() if event.status in {OutboxStatus.PENDING, OutboxStatus.FAILED}]
        rows.sort(key=lambda event: event.created_at or "")
        return rows[: max(1, int(limit))]


class _ThreadSafeInboundGuard:
    def __init__(self) -> None:
        self._message_ids: set[str] = set()
        self._dedup_keys: set[str] = set()
        self._lock = Lock()

    def was_processed(self, *, message_id: str, dedup_key: str, conn: Any | None = None) -> bool:
        _ = conn
        with self._lock:
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
        with self._lock:
            if message_id in self._message_ids or dedup_key in self._dedup_keys:
                return False
            self._message_ids.add(message_id)
            self._dedup_keys.add(dedup_key)
            return True


class _ThreadSafeOutboxGuard:
    def __init__(self) -> None:
        self._event_ids: set[str] = set()
        self._dedup_keys: set[str] = set()
        self._lock = Lock()

    def was_processed(self, *, event_id: str, dedup_key: str, conn: Any | None = None) -> bool:
        _ = conn
        with self._lock:
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
        with self._lock:
            if event_id in self._event_ids or dedup_key in self._dedup_keys:
                return False
            self._event_ids.add(event_id)
            self._dedup_keys.add(dedup_key)
            return True


class _InboundToOutboundOrchestrator:
    def __init__(self, *, outbox_repository: _InMemoryOutboxRepository) -> None:
        self.executed: list[str] = []
        self._publisher = OutboundQueuePublisher(
            queue_repository=DatabaseOutboundQueueRepository(outbox_repository=outbox_repository)
        )

    def execute(self, message: InboundQueueRecord) -> None:
        payload = dict(message.payload)
        phone_number = str(payload.get("phone_number") or "")
        message_body = str(payload.get("message_body") or "")
        channel = str(payload.get("channel") or "sms")
        publish_result = self._publisher.publish_messages(
            aggregate_id=phone_number,
            messages=[
                OutboundMessage(
                    channel="sms",
                    recipient=phone_number,
                    body=f"{channel}:{message_body}",
                    metadata={"source_message_id": message.message_id},
                )
            ],
            request_id=message.metadata.request_id,
            correlation_id=message.metadata.correlation_id or message.metadata.request_id,
        )
        if publish_result.queued != 1:
            raise RuntimeError("failed to enqueue outbound follow-up")
        self.executed.append(message.message_id)


class _ContendedInboundQueueRepository:
    def __init__(self, message: InboundQueueRecord, *, worker_count: int) -> None:
        self._row = message
        self._status = QueueStatus.PENDING
        self._lock = Lock()
        # Barrier placed at mark_processing (the real contention point) so that both
        # workers have already called list_pending and received the message before
        # either one can change status to PROCESSING.  This eliminates the race where
        # the first worker changes status between the two list_pending lock acquisitions.
        self._contention_barrier = Barrier(worker_count, timeout=5.0)
        self.mark_processing_attempts = 0

    def list_pending(self, *, limit: int = 100, conn: Any | None = None) -> list[InboundQueueRecord]:
        _ = (limit, conn)
        with self._lock:
            if self._status in {QueueStatus.PENDING, QueueStatus.RETRY}:
                return [self._row]
            return []

    def mark_processing(self, message_id: str, *, conn: Any | None = None) -> bool:
        _ = (message_id, conn)
        # Synchronise both workers at the contention point: the first to arrive waits
        # for the second before either can change status.  If one times out (e.g. due
        # to scheduling jitter), the broken barrier is caught and execution continues
        # normally so the test result is still correct (one True, one False).
        try:
            self._contention_barrier.wait()
        except BrokenBarrierError:
            pass
        with self._lock:
            self.mark_processing_attempts += 1
            if self._status not in {QueueStatus.PENDING, QueueStatus.RETRY}:
                return False
            self._status = QueueStatus.PROCESSING
            self._row = replace(self._row, status=QueueStatus.PROCESSING)
            return True

    def mark_sent(self, message_id: str, *, conn: Any | None = None) -> bool:
        _ = (message_id, conn)
        with self._lock:
            if self._status not in {QueueStatus.PROCESSING, QueueStatus.SENT}:
                return False
            self._status = QueueStatus.SENT
            self._row = replace(self._row, status=QueueStatus.SENT)
            return True

    def mark_retry(
        self,
        message_id: str,
        *,
        error_message: str,
        retry_delay_seconds: int = 0,
        conn: Any | None = None,
    ) -> bool:
        _ = (message_id, error_message, retry_delay_seconds, conn)
        return False

    def mark_dead(self, message_id: str, *, error_message: str | None = None, conn: Any | None = None) -> bool:
        _ = (message_id, error_message, conn)
        return False

    def get_message(self, message_id: str, *, conn: Any | None = None) -> InboundQueueRecord | None:
        _ = (message_id, conn)
        return self._row


def _run_outbound_sender_worker(
    *,
    outbox_repository: _InMemoryOutboxRepository,
    sender,
):
    dispatched_payloads: list[tuple[str, str]] = []
    outbound_dispatcher = OutboundDispatcher(
        adapters=[SMSOutboundAdapter(lambda recipient, body: sender(recipient, body, dispatched_payloads))]
    )
    dispatcher = OutboxEventDispatcher()
    register_outbound_sender_handler(dispatcher=dispatcher, outbound_dispatcher=outbound_dispatcher)
    runtime = OutboxWorkerRuntime(
        outbox_repository=outbox_repository,
        dispatcher=dispatcher,
        idempotency_guard=_ThreadSafeOutboxGuard(),
    )
    return runtime.run_once(), dispatched_payloads


def _inbound_record(*, message_id: str, dedup_key: str) -> InboundQueueRecord:
    return InboundQueueRecord(
        message_id=message_id,
        payload={
            "channel": "sms",
            "phone_number": "+61412345678",
            "message_body": "contention",
        },
        metadata=QueueMessageMetadata(
            dedup_key=dedup_key,
            request_id="req-contention",
            correlation_id="corr-contention",
            enqueued_at="2026-01-01T00:00:00+00:00",
        ),
        status=QueueStatus.PENDING,
        max_attempts=3,
        last_error=None,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


def test_phase3_sms_quick_ack_enqueue_and_worker_flow_produces_outbound_records() -> None:
    db = _InMemoryInboundQueueDB()
    enqueue = try_enqueue_sms_quick_ack(
        db_service=db,
        phone_number="+61412345678",
        message_body="hello from sms",
        message_data={"message_id": "sms-phase3-1"},
        request_payload={"message_id": "sms-phase3-1"},
        request_headers={"X-Test": "1"},
        remote_addr="127.0.0.1",
        request_id="req-sms-phase3",
        env={"REFACTOR_SMS_INGRESS_QUICK_ACK_ENABLED": "true"},
    )
    assert enqueue.accepted is True
    assert enqueue.duplicate is False

    inbound_repo = DatabaseInboundQueueRepository(db)
    outbox_repo = _InMemoryOutboxRepository()
    orchestrator = _InboundToOutboundOrchestrator(outbox_repository=outbox_repo)
    inbound_runtime = InboundWorkerRuntime(
        inbound_repository=inbound_repo,
        orchestrator=orchestrator,
        idempotency_guard=_ThreadSafeInboundGuard(),
    )
    inbound_result = inbound_runtime.run_once()
    queued_outbound = outbox_repo.list_pending(limit=10)

    assert inbound_result.sent == 1
    assert inbound_result.retried == 0
    assert len(queued_outbound) == 1
    assert queued_outbound[0].payload["body"] == "sms:hello from sms"

    outbound_result, sent_payloads = _run_outbound_sender_worker(
        outbox_repository=outbox_repo,
        sender=lambda recipient, body, sink: sink.append((recipient, body)) or True,
    )
    assert outbound_result.sent == 1
    assert sent_payloads == [("+61412345678", "sms:hello from sms")]
    assert list(outbox_repo.rows.values())[0].status == OutboxStatus.PUBLISHED


def test_phase3_webhook_quick_ack_enqueue_and_worker_flow() -> None:
    db = _InMemoryInboundQueueDB()
    enqueue = try_enqueue_webhook_quick_ack(
        db_service=db,
        phone_number="+61400000001",
        message_body="hello from webhook",
        payload={"event": "message.received", "data": {"contact": "+61400000001", "content": "hello from webhook"}},
        message_data={"message_id": "webhook-phase3-1"},
        request_headers={"X-Webhook": "phase3"},
        remote_addr="127.0.0.2",
        request_id="req-webhook-phase3",
        dedup_key="webhook-phase3-1",
        dedup_key_missing=False,
        auth_reason="webhook_secret_match",
        signature_verified=True,
        env={"REFACTOR_WEBHOOK_INGRESS_QUICK_ACK_ENABLED": "true"},
    )
    assert enqueue.accepted is True
    assert enqueue.duplicate is False

    inbound_repo = DatabaseInboundQueueRepository(db)
    outbox_repo = _InMemoryOutboxRepository()
    inbound_runtime = InboundWorkerRuntime(
        inbound_repository=inbound_repo,
        orchestrator=_InboundToOutboundOrchestrator(outbox_repository=outbox_repo),
        idempotency_guard=_ThreadSafeInboundGuard(),
    )
    inbound_result = inbound_runtime.run_once()
    queued_outbound = outbox_repo.list_pending(limit=10)

    assert inbound_result.sent == 1
    assert len(queued_outbound) == 1
    assert queued_outbound[0].metadata["request_id"] == "req-webhook-phase3"
    assert queued_outbound[0].payload["body"] == "webhook:hello from webhook"


def test_phase3_outbound_sender_worker_covers_success_retry_and_dead_letter() -> None:
    outbox_repo = _InMemoryOutboxRepository()
    queue_repo = DatabaseOutboundQueueRepository(outbox_repository=outbox_repo)

    OutboundQueuePublisher(queue_repository=queue_repo, max_attempts=3).publish_messages(
        aggregate_id="+61419990001",
        messages=[OutboundMessage(channel="sms", recipient="+61419990001", body="deliver-me")],
        request_id="req-ok",
        correlation_id="req-ok",
    )
    OutboundQueuePublisher(queue_repository=queue_repo, max_attempts=3).publish_messages(
        aggregate_id="+61419990002",
        messages=[OutboundMessage(channel="sms", recipient="+61419990002", body="retry-me")],
        request_id="req-retry",
        correlation_id="req-retry",
    )
    OutboundQueuePublisher(queue_repository=queue_repo, max_attempts=1).publish_messages(
        aggregate_id="+61419990003",
        messages=[OutboundMessage(channel="sms", recipient="+61419990003", body="dead-me")],
        request_id="req-dead",
        correlation_id="req-dead",
    )

    outbound_result, sent_payloads = _run_outbound_sender_worker(
        outbox_repository=outbox_repo,
        sender=lambda recipient, body, sink: sink.append((recipient, body)) or body == "deliver-me",
    )
    statuses = {event.payload["body"]: event for event in outbox_repo.rows.values()}

    assert outbound_result.sent == 1
    assert outbound_result.retried == 1
    assert outbound_result.dead_lettered == 1
    assert sent_payloads[0][1] == "deliver-me"
    assert statuses["deliver-me"].status == OutboxStatus.PUBLISHED
    assert statuses["retry-me"].status == OutboxStatus.FAILED
    assert statuses["retry-me"].next_retry_at is not None
    assert statuses["dead-me"].status == OutboxStatus.DEAD_LETTER
    assert statuses["dead-me"].dead_lettered_at is not None


def test_phase3_end_to_end_idempotency_for_replayed_inbound_events() -> None:
    db = _InMemoryInboundQueueDB()
    first = try_enqueue_sms_quick_ack(
        db_service=db,
        phone_number="+61422223333",
        message_body="same inbound event",
        message_data={"message_id": "replay-phase3"},
        request_payload={"message_id": "replay-phase3"},
        request_headers={"X-Test": "replay"},
        remote_addr="127.0.0.3",
        request_id="req-replay-1",
        env={"REFACTOR_SMS_INGRESS_QUICK_ACK_ENABLED": "1"},
    )
    replay = try_enqueue_sms_quick_ack(
        db_service=db,
        phone_number="+61422223333",
        message_body="same inbound event",
        message_data={"message_id": "replay-phase3"},
        request_payload={"message_id": "replay-phase3"},
        request_headers={"X-Test": "replay"},
        remote_addr="127.0.0.3",
        request_id="req-replay-2",
        env={"REFACTOR_SMS_INGRESS_QUICK_ACK_ENABLED": "1"},
    )

    inbound_repo = DatabaseInboundQueueRepository(db)
    outbox_repo = _InMemoryOutboxRepository()
    orchestrator = _InboundToOutboundOrchestrator(outbox_repository=outbox_repo)
    inbound_runtime = InboundWorkerRuntime(
        inbound_repository=inbound_repo,
        orchestrator=orchestrator,
        idempotency_guard=_ThreadSafeInboundGuard(),
    )
    inbound_result = inbound_runtime.run_once()
    outbound_result, sent_payloads = _run_outbound_sender_worker(
        outbox_repository=outbox_repo,
        sender=lambda recipient, body, sink: sink.append((recipient, body)) or True,
    )

    assert first.accepted is True and first.duplicate is False
    assert replay.accepted is True and replay.duplicate is True
    assert inbound_result.sent == 1
    assert orchestrator.executed and len(orchestrator.executed) == 1
    assert outbound_result.sent == 1
    assert len(sent_payloads) == 1


def test_phase3_queue_mode_rolls_back_to_sync_dispatch_on_publish_failure(monkeypatch) -> None:
    dispatched: list[str] = []

    class _StubDispatcher:
        def dispatch(self, messages):
            buffered = list(messages)
            dispatched.extend(message.body for message in buffered)
            return sms_gateway.OutboundDispatchResult(attempted=len(buffered), sent=len(buffered), failed=0)

    monkeypatch.setattr(sms_gateway, "resolve_sms_outbound_delivery_mode", lambda: "queue")
    monkeypatch.setattr(sms_gateway, "_build_sms_outbound_dispatcher", lambda **_kwargs: _StubDispatcher())
    monkeypatch.setattr(
        sms_gateway,
        "_publish_sms_outbound_queue",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("queue unavailable")),
    )

    result = sms_gateway._dispatch_sms_outbound(
        phone_number="+61445678901",
        request_id="req-rollback-queue",
        composed_response=ComposedResponse(messages=["sync-fallback"]),
    )

    assert result.sent == 1
    assert result.failed == 0
    assert dispatched == ["sync-fallback"]


def test_phase3_duplicate_event_processed_once_under_worker_contention() -> None:
    record = _inbound_record(message_id="contended-1", dedup_key="dedup-contended-1")
    repository = _ContendedInboundQueueRepository(record, worker_count=2)
    guard = _ThreadSafeInboundGuard()
    processed: list[str] = []
    processed_lock = Lock()

    class _Executor:
        def execute(self, message: InboundQueueRecord) -> None:
            with processed_lock:
                processed.append(message.message_id)

    runtime_a = InboundWorkerRuntime(
        inbound_repository=repository,
        orchestrator=_Executor(),
        idempotency_guard=guard,
    )
    runtime_b = InboundWorkerRuntime(
        inbound_repository=repository,
        orchestrator=_Executor(),
        idempotency_guard=guard,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        future_a = executor.submit(runtime_a.run_once)
        future_b = executor.submit(runtime_b.run_once)
        result_a = future_a.result(timeout=5)
        result_b = future_b.result(timeout=5)

    assert result_a.sent + result_b.sent == 1
    assert result_a.skipped + result_b.skipped == 1
    assert processed == ["contended-1"]
    assert repository.mark_processing_attempts == 2
