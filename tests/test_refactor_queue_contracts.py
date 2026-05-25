from __future__ import annotations

import json
from typing import Any

from app.events.outbox import OutboxEventEnvelope, OutboxEventRecord, OutboxStatus
from app.queue import (
    DatabaseInboundQueueRepository,
    DatabaseOutboundQueueRepository,
    DatabaseQueueProvider,
    InboundQueueEnvelope,
    InboundQueueProvider,
    OutboundQueueEnvelope,
    OutboundQueueProvider,
    QueueProvider,
    QueueDirection,
    QueueMessageMetadata,
    QueueStatus,
    can_transition,
    outbox_to_queue_status,
    queue_to_outbox_status,
    resolve_retry_or_dead_status,
)


class _FakeInboundQueueDB:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.archive_rows: dict[str, dict[str, Any]] = {}
        self.dedup_index: dict[str, str] = {}

    def execute_query(self, query, params=(), fetch=False, conn=None, **_kwargs):
        _ = conn
        sql = " ".join(str(query).split()).lower()
        if "create table if not exists refactor_inbound_queue_messages" in sql:
            return [] if fetch else None
        if "create index if not exists idx_refactor_inbound_queue_status_retry" in sql:
            return [] if fetch else None
        if "create index if not exists idx_refactor_inbound_queue_dedup_created" in sql:
            return [] if fetch else None
        if "create table if not exists refactor_inbound_queue_messages_archive" in sql:
            return [] if fetch else None
        if "create index if not exists idx_refactor_inbound_queue_archive_status_archived_at" in sql:
            return [] if fetch else None
        if "insert into refactor_inbound_queue_messages_archive" in sql and "delete from refactor_inbound_queue_messages" in sql:
            status, older_than, limit, archive_reason, archived_by = params
            matching = [
                dict(row)
                for row in self.rows.values()
                if row["status"] == status
                and str(row.get("dead_lettered_at") or row.get("updated_at") or row.get("created_at") or row.get("received_at") or "")
                <= str(older_than)
            ]
            matching.sort(
                key=lambda row: (
                    str(row.get("dead_lettered_at") or row.get("updated_at") or row.get("created_at") or row.get("received_at") or ""),
                    str(row.get("message_id") or ""),
                )
            )
            selected = matching[: max(1, int(limit))]
            for row in selected:
                archived = dict(row)
                archived["archived_at"] = "2026-01-01T00:05:00+00:00"
                archived["archive_reason"] = archive_reason
                archived["archived_by"] = archived_by
                self.archive_rows[row["message_id"]] = archived
                self.rows.pop(row["message_id"], None)
            return [{"message_id": row["message_id"]} for row in selected] if fetch else None
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
            if message_id in self.rows or dedup_key in self.dedup_index:
                return [] if fetch else None
            row = {
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
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
            }
            self.rows[message_id] = row
            self.dedup_index[dedup_key] = message_id
            return [{"message_id": message_id}] if fetch else None
        if "set status = 'processing'" in sql and "attempt = attempt + 1" in sql:
            (message_id,) = params
            row = self.rows.get(message_id)
            if row is None or row["status"] not in {QueueStatus.PENDING, QueueStatus.RETRY}:
                return [] if fetch else None
            row["status"] = QueueStatus.PROCESSING
            row["attempt"] = int(row["attempt"]) + 1
            row["processing_started_at"] = row["processing_started_at"] or "2026-01-01T00:01:00+00:00"
            row["last_attempt_at"] = "2026-01-01T00:01:00+00:00"
            row["updated_at"] = "2026-01-01T00:01:00+00:00"
            return [{"message_id": message_id}] if fetch else None
        if "set status = case" in sql and "retry" in sql and "dead" in sql:
            delay_seconds, _delay_seconds2, error_message, message_id = params
            row = self.rows.get(message_id)
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
            row = self.rows.get(message_id)
            if row is None or row["status"] not in {QueueStatus.PROCESSING, QueueStatus.SENT}:
                return [] if fetch else None
            row["status"] = QueueStatus.SENT
            row["next_attempt_at"] = None
            row["last_error"] = None
            row["last_error_at"] = None
            row["updated_at"] = "2026-01-01T00:02:30+00:00"
            return [{"message_id": message_id}] if fetch else None
        if "set status = 'dead'" in sql and "coalesce" in sql:
            error_message, _error_message2, message_id = params
            row = self.rows.get(message_id)
            if row is None or row["status"] not in {QueueStatus.PENDING, QueueStatus.PROCESSING, QueueStatus.RETRY}:
                return [] if fetch else None
            row["status"] = QueueStatus.DEAD
            row["next_attempt_at"] = None
            if error_message is not None:
                row["last_error"] = error_message
                row["last_error_at"] = "2026-01-01T00:03:00+00:00"
            row["dead_lettered_at"] = row["dead_lettered_at"] or "2026-01-01T00:03:00+00:00"
            row["updated_at"] = "2026-01-01T00:03:00+00:00"
            return [{"message_id": message_id}] if fetch else None
        if "where status in ('pending', 'retry')" in sql and "limit %s" in sql:
            (limit,) = params
            rows = [
                dict(row)
                for row in self.rows.values()
                if row["status"] in {QueueStatus.PENDING, QueueStatus.RETRY}
            ]
            rows.sort(key=lambda row: str(row.get("created_at") or ""))
            return rows[: int(limit)] if fetch else None
        if "where status = 'dead'" in sql and "limit %s" in sql:
            (limit,) = params
            rows = [
                dict(row)
                for row in self.rows.values()
                if row["status"] == QueueStatus.DEAD
            ]
            rows.sort(key=lambda row: str(row.get("dead_lettered_at") or row.get("updated_at") or row.get("created_at") or ""))
            return rows[: int(limit)] if fetch else None
        if "set status = 'pending'" in sql and "where message_id = %s" in sql and "status = 'dead'" in sql:
            metadata_json, message_id = params
            row = self.rows.get(message_id)
            if row is None or row["status"] != QueueStatus.DEAD:
                return [] if fetch else None
            row["status"] = QueueStatus.PENDING
            row["attempt"] = 0
            row["next_attempt_at"] = None
            row["processing_started_at"] = None
            row["last_attempt_at"] = None
            row["last_error"] = None
            row["last_error_at"] = None
            row["metadata"] = json.loads(metadata_json)
            row["updated_at"] = "2026-01-01T00:04:00+00:00"
            return [{"message_id": message_id}] if fetch else None
        if "from refactor_inbound_queue_messages" in sql and "where message_id = %s" in sql:
            (message_id,) = params
            row = self.rows.get(message_id)
            if row is None:
                return [] if fetch else None
            return [dict(row)] if fetch else None
        raise AssertionError(f"Unexpected SQL in fake inbound queue DB: {query}")


class _FakeOutboxRepository:
    def __init__(self) -> None:
        self.rows: dict[str, OutboxEventRecord] = {}
        self.archive_rows: dict[str, OutboxEventRecord] = {}
        self.idempotency_index: dict[str, str] = {}

    def append_event(self, event: OutboxEventEnvelope, *, conn: Any | None = None) -> bool:
        _ = conn
        key = event.normalized_idempotency_key
        if event.event_id in self.rows or key in self.idempotency_index:
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
            max_retries=event.max_retries,
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
        self.idempotency_index[key] = event.event_id
        return True

    def mark_processing(self, event_id: str, *, conn: Any | None = None) -> bool:
        _ = conn
        event = self.rows.get(event_id)
        if event is None or event.status not in {OutboxStatus.PENDING, OutboxStatus.FAILED}:
            return False
        self.rows[event_id] = OutboxEventRecord(
            **{
                **event.__dict__,
                "status": OutboxStatus.PROCESSING,
                "processing_started_at": event.processing_started_at or "2026-01-01T00:01:00+00:00",
                "last_attempt_at": "2026-01-01T00:01:00+00:00",
                "updated_at": "2026-01-01T00:01:00+00:00",
            }
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
        is_dead = next_retry_count >= int(event.max_retries)
        self.rows[event_id] = OutboxEventRecord(
            **{
                **event.__dict__,
                "status": OutboxStatus.DEAD_LETTER if is_dead else OutboxStatus.FAILED,
                "retry_count": next_retry_count,
                "next_retry_at": (None if is_dead else f"+{int(retry_delay_seconds)}s"),
                "last_error": error_message,
                "last_error_at": "2026-01-01T00:02:00+00:00",
                "dead_lettered_at": ("2026-01-01T00:02:00+00:00" if is_dead else event.dead_lettered_at),
                "updated_at": "2026-01-01T00:02:00+00:00",
            }
        )
        return True

    def mark_published(self, event_id: str, *, conn: Any | None = None) -> bool:
        _ = conn
        event = self.rows.get(event_id)
        if event is None or event.status not in {OutboxStatus.PROCESSING, OutboxStatus.PUBLISHED}:
            return False
        self.rows[event_id] = OutboxEventRecord(
            **{
                **event.__dict__,
                "status": OutboxStatus.PUBLISHED,
                "next_retry_at": None,
                "last_error": None,
                "last_error_at": None,
                "updated_at": "2026-01-01T00:03:00+00:00",
            }
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

    def list_dead(self, *, limit: int = 100, conn: Any | None = None) -> list[OutboxEventRecord]:
        _ = conn
        rows = [event for event in self.rows.values() if event.status == OutboxStatus.DEAD_LETTER]
        rows.sort(key=lambda event: event.dead_lettered_at or event.updated_at or event.created_at or "")
        return rows[: max(1, int(limit))]

    def replay_dead(
        self,
        event_id: str,
        *,
        replay_metadata: dict[str, Any],
        conn: Any | None = None,
    ) -> bool:
        _ = conn
        event = self.rows.get(event_id)
        if event is None or event.status != OutboxStatus.DEAD_LETTER:
            return False
        history = list(event.metadata.get("dlq_replay_history", [])) if isinstance(event.metadata.get("dlq_replay_history"), list) else []
        history.append(dict(replay_metadata))
        metadata = {
            **event.metadata,
            "dlq_replay": dict(replay_metadata),
            "dlq_replay_history": history[-20:],
            "dlq_replay_count": int(event.metadata.get("dlq_replay_count") or 0) + 1,
        }
        self.rows[event_id] = OutboxEventRecord(
            **{
                **event.__dict__,
                "status": OutboxStatus.PENDING,
                "retry_count": 0,
                "next_retry_at": None,
                "processing_started_at": None,
                "last_attempt_at": None,
                "last_error": None,
                "last_error_at": None,
                "metadata": metadata,
                "updated_at": "2026-01-01T00:04:00+00:00",
            }
        )
        return True

    def archive_events(
        self,
        *,
        status: str,
        older_than: str,
        limit: int = 100,
        archived_by: str | None = None,
        archive_reason: str | None = None,
        conn: Any | None = None,
    ) -> int:
        _ = (archived_by, archive_reason, conn)
        candidates = [
            event
            for event in self.rows.values()
            if event.status == status
            and str(event.dead_lettered_at or event.updated_at or event.created_at or event.occurred_at or "") <= str(older_than)
        ]
        candidates.sort(
            key=lambda event: (
                str(event.dead_lettered_at or event.updated_at or event.created_at or event.occurred_at or ""),
                event.event_id,
            )
        )
        selected = candidates[: max(1, int(limit))]
        for event in selected:
            self.archive_rows[event.event_id] = event
            self.rows.pop(event.event_id, None)
        return len(selected)


def _inbound_envelope(*, message_id: str, dedup_key: str, max_attempts: int = 3) -> InboundQueueEnvelope:
    return InboundQueueEnvelope(
        message_id=message_id,
        payload={"body": "hello", "source": "httpsms"},
        metadata=QueueMessageMetadata(
            dedup_key=dedup_key,
            correlation_id="corr-123",
            request_id="req-123",
            enqueued_at="2026-01-01T00:00:00+00:00",
            attributes={"sender": "+61400000000"},
        ),
        max_attempts=max_attempts,
    )


def test_queue_envelopes_round_trip_serialization() -> None:
    metadata = QueueMessageMetadata(
        attempt=1,
        dedup_key="dedup-1",
        correlation_id="corr-1",
        request_id="req-1",
        enqueued_at="2026-01-01T00:00:00+00:00",
        available_at="2026-01-01T00:00:05+00:00",
        attributes={"channel": "sms"},
    )
    inbound = InboundQueueEnvelope(
        message_id="in-1",
        payload={"text": "Hi"},
        metadata=metadata,
        status=QueueStatus.PENDING,
        max_attempts=4,
    )
    outbound = OutboundQueueEnvelope(
        message_id="out-1",
        message_type="conversation.state_transitioned",
        aggregate_type="conversation_state",
        aggregate_id="+61400000001",
        payload={"to_state": "COLLECTING"},
        metadata=metadata,
        status=QueueStatus.PROCESSING,
        max_attempts=4,
    )

    inbound_round_trip = InboundQueueEnvelope.from_dict(inbound.to_dict())
    outbound_round_trip = OutboundQueueEnvelope.from_dict(outbound.to_dict())

    assert inbound_round_trip.message_id == "in-1"
    assert inbound_round_trip.metadata.correlation_id == "corr-1"
    assert inbound_round_trip.payload["text"] == "Hi"
    assert outbound_round_trip.message_type == "conversation.state_transitioned"
    assert outbound_round_trip.aggregate_id == "+61400000001"
    assert outbound_round_trip.metadata.attributes["channel"] == "sms"


def test_queue_status_transition_rules_are_deterministic() -> None:
    assert can_transition(QueueStatus.PENDING, QueueStatus.PROCESSING, direction=QueueDirection.INBOUND) is True
    assert can_transition(QueueStatus.PROCESSING, QueueStatus.RETRY, direction=QueueDirection.INBOUND) is True
    assert can_transition(QueueStatus.PROCESSING, QueueStatus.SENT, direction=QueueDirection.INBOUND) is True
    assert can_transition(QueueStatus.PROCESSING, QueueStatus.SENT, direction=QueueDirection.OUTBOUND) is True
    assert resolve_retry_or_dead_status(attempt=1, max_attempts=2) == QueueStatus.RETRY
    assert resolve_retry_or_dead_status(attempt=2, max_attempts=2) == QueueStatus.DEAD
    assert queue_to_outbox_status(QueueStatus.RETRY) == OutboxStatus.FAILED
    assert outbox_to_queue_status(OutboxStatus.DEAD_LETTER) == QueueStatus.DEAD
    assert can_transition(QueueStatus.DEAD, QueueStatus.PENDING, direction=QueueDirection.INBOUND) is True


def test_inbound_queue_enqueue_is_idempotent_by_dedup_key() -> None:
    db = _FakeInboundQueueDB()
    repo = DatabaseInboundQueueRepository(db)

    first = repo.enqueue(_inbound_envelope(message_id="in-1", dedup_key="dedup-1"))
    duplicate_message = repo.enqueue(_inbound_envelope(message_id="in-1", dedup_key="dedup-2"))
    duplicate_dedup_key = repo.enqueue(_inbound_envelope(message_id="in-2", dedup_key="dedup-1"))
    stored = repo.get_message("in-1")

    assert first is True
    assert duplicate_message is False
    assert duplicate_dedup_key is False
    assert stored is not None
    assert stored.metadata.dedup_key == "dedup-1"
    assert repo.get_message("in-2") is None


def test_inbound_queue_retry_transitions_to_dead_when_attempt_budget_exhausted() -> None:
    db = _FakeInboundQueueDB()
    repo = DatabaseInboundQueueRepository(db)
    repo.enqueue(_inbound_envelope(message_id="in-retry", dedup_key="dedup-retry", max_attempts=2))

    assert repo.mark_processing("in-retry") is True
    assert repo.mark_retry("in-retry", error_message="timeout", retry_delay_seconds=15) is True
    first_retry = repo.get_message("in-retry")
    assert first_retry is not None
    assert first_retry.status == QueueStatus.RETRY

    assert repo.mark_processing("in-retry") is True
    assert repo.mark_retry("in-retry", error_message="timeout-again", retry_delay_seconds=15) is True
    dead = repo.get_message("in-retry")
    assert dead is not None
    assert dead.status == QueueStatus.DEAD
    assert dead.metadata.dead_lettered_at is not None


def test_inbound_queue_mark_sent_completes_message() -> None:
    db = _FakeInboundQueueDB()
    repo = DatabaseInboundQueueRepository(db)
    repo.enqueue(_inbound_envelope(message_id="in-sent", dedup_key="dedup-sent", max_attempts=2))

    assert repo.mark_processing("in-sent") is True
    assert repo.mark_sent("in-sent") is True
    sent = repo.get_message("in-sent")
    assert sent is not None
    assert sent.status == QueueStatus.SENT
    assert sent.metadata.completed_at == "2026-01-01T00:02:30+00:00"


def test_inbound_queue_dead_letter_records_can_be_replayed_to_pending() -> None:
    db = _FakeInboundQueueDB()
    repo = DatabaseInboundQueueRepository(db)
    repo.enqueue(_inbound_envelope(message_id="in-dead", dedup_key="dedup-dead", max_attempts=1))
    repo.mark_processing("in-dead")
    repo.mark_retry("in-dead", error_message="terminal", retry_delay_seconds=1)
    dead_records = repo.list_dead(limit=10)
    assert len(dead_records) == 1
    assert dead_records[0].status == QueueStatus.DEAD

    replayed = repo.replay_dead(
        "in-dead",
        replay_metadata={
            "actor": "ops-user",
            "reason": "replay after fix",
            "replay_run_id": "run-123",
            "idempotency_key": "idem-run-123",
        },
    )
    replayed_record = repo.get_message("in-dead")

    assert replayed is True
    assert replayed_record is not None
    assert replayed_record.status == QueueStatus.PENDING
    assert replayed_record.metadata.attempt == 0
    assert replayed_record.last_error is None
    assert replayed_record.metadata.attributes["dlq_replay"]["actor"] == "ops-user"


def test_outbound_queue_adapter_uses_outbox_records_and_status_mapping() -> None:
    outbox = _FakeOutboxRepository()
    repo = DatabaseOutboundQueueRepository(outbox_repository=outbox)
    envelope = OutboundQueueEnvelope(
        message_id="out-1",
        message_type="conversation.state_transitioned",
        aggregate_type="conversation_state",
        aggregate_id="+61400000002",
        payload={"to_state": "COLLECTING"},
        metadata=QueueMessageMetadata(
            dedup_key="dedup-outbound-1",
            correlation_id="corr-out",
            request_id="req-out",
            enqueued_at="2026-01-01T00:00:00+00:00",
            attributes={"source": "transition_service"},
        ),
        max_attempts=3,
    )

    inserted = repo.enqueue(envelope)
    duplicate = repo.enqueue(
        OutboundQueueEnvelope(
            message_id="out-2",
            message_type="conversation.state_transitioned",
            aggregate_type="conversation_state",
            aggregate_id="+61400000002",
            payload={"to_state": "COLLECTING"},
            metadata=QueueMessageMetadata(dedup_key="dedup-outbound-1"),
        )
    )
    pending = repo.list_pending(limit=10)

    assert inserted is True
    assert duplicate is False
    assert len(pending) == 1
    assert pending[0].status == QueueStatus.PENDING
    assert pending[0].metadata.correlation_id == "corr-out"

    assert repo.mark_processing("out-1") is True
    assert repo.mark_sent("out-1") is True
    sent = repo.get_message("out-1")
    assert sent is not None
    assert sent.status == QueueStatus.SENT
    assert sent.metadata.completed_at is not None


def test_outbound_queue_dead_records_can_be_replayed() -> None:
    outbox = _FakeOutboxRepository()
    repo = DatabaseOutboundQueueRepository(outbox_repository=outbox)
    envelope = OutboundQueueEnvelope(
        message_id="out-dead",
        message_type="conversation.state_transitioned",
        aggregate_type="conversation_state",
        aggregate_id="+61400000009",
        payload={"to_state": "COLLECTING"},
        metadata=QueueMessageMetadata(dedup_key="dedup-out-dead"),
        max_attempts=1,
    )
    assert repo.enqueue(envelope) is True
    assert repo.mark_processing("out-dead") is True
    assert repo.mark_retry("out-dead", error_message="send failed", retry_delay_seconds=5) is True

    dead = repo.list_dead(limit=10)
    assert len(dead) == 1
    assert dead[0].status == QueueStatus.DEAD

    replayed = repo.replay_dead(
        "out-dead",
        replay_metadata={
            "actor": "ops-user",
            "reason": "gateway fix deployed",
            "replay_run_id": "run-42",
            "idempotency_key": "idem-run-42",
        },
    )
    replayed_record = repo.get_message("out-dead")

    assert replayed is True
    assert replayed_record is not None
    assert replayed_record.status == QueueStatus.PENDING
    assert replayed_record.metadata.attributes["dlq_replay"]["replay_run_id"] == "run-42"


def test_database_queue_provider_exposes_contract_compatible_inbound_and_outbound_queues() -> None:
    db = _FakeInboundQueueDB()
    outbox = _FakeOutboxRepository()
    provider = DatabaseQueueProvider(db_service=db, outbox_repository=outbox)

    assert isinstance(provider, QueueProvider)
    assert isinstance(provider.inbound(), InboundQueueProvider)
    assert isinstance(provider.outbound(), OutboundQueueProvider)


def test_database_queue_provider_inbound_semantics_match_existing_repository_behavior() -> None:
    db = _FakeInboundQueueDB()
    provider = DatabaseQueueProvider(db_service=db, outbox_repository=_FakeOutboxRepository())
    inbound = provider.inbound()

    assert inbound.enqueue(_inbound_envelope(message_id="provider-in-1", dedup_key="provider-dedup-1")) is True
    assert inbound.enqueue(_inbound_envelope(message_id="provider-in-2", dedup_key="provider-dedup-1")) is False
    assert inbound.mark_processing("provider-in-1") is True
    assert inbound.mark_retry("provider-in-1", error_message="transient", retry_delay_seconds=5) is True
    retried = inbound.get_message("provider-in-1")
    assert retried is not None
    assert retried.status == QueueStatus.RETRY
    assert retried.last_error == "transient"


def test_database_queue_provider_outbound_semantics_match_existing_repository_behavior() -> None:
    provider = DatabaseQueueProvider(db_service=_FakeInboundQueueDB(), outbox_repository=_FakeOutboxRepository())
    outbound = provider.outbound()
    envelope = OutboundQueueEnvelope(
        message_id="provider-out-1",
        message_type="conversation.state_transitioned",
        aggregate_type="conversation_state",
        aggregate_id="+61400000111",
        payload={"to_state": "COLLECTING"},
        metadata=QueueMessageMetadata(dedup_key="provider-out-dedup-1"),
        max_attempts=1,
    )

    assert outbound.enqueue(envelope) is True
    assert outbound.mark_processing("provider-out-1") is True
    assert outbound.mark_retry("provider-out-1", error_message="gateway down", retry_delay_seconds=5) is True
    dead = outbound.get_message("provider-out-1")
    assert dead is not None
    assert dead.status == QueueStatus.DEAD

    assert (
        outbound.replay_dead(
            "provider-out-1",
            replay_metadata={
                "actor": "ops-user",
                "reason": "provider replay",
                "replay_run_id": "provider-run-1",
                "idempotency_key": "provider-idem-1",
            },
        )
        is True
    )
    replayed = outbound.get_message("provider-out-1")
    assert replayed is not None
    assert replayed.status == QueueStatus.PENDING
    assert replayed.metadata.attributes["dlq_replay"]["actor"] == "ops-user"


def test_inbound_queue_processed_records_can_be_archived_with_status_boundaries() -> None:
    db = _FakeInboundQueueDB()
    repo = DatabaseInboundQueueRepository(db)
    repo.enqueue(_inbound_envelope(message_id="in-archive-sent", dedup_key="dedup-archive-sent", max_attempts=2))
    repo.enqueue(_inbound_envelope(message_id="in-archive-dead", dedup_key="dedup-archive-dead", max_attempts=1))
    repo.mark_processing("in-archive-sent")
    repo.mark_sent("in-archive-sent")
    repo.mark_processing("in-archive-dead")
    repo.mark_retry("in-archive-dead", error_message="terminal", retry_delay_seconds=1)

    archived_sent = repo.archive_processed_records(
        status=QueueStatus.SENT,
        older_than="2026-01-01T00:03:00+00:00",
        limit=5,
        archived_by="ops-user",
        archive_reason="retention",
    )
    archived_dead = repo.archive_processed_records(
        status=QueueStatus.DEAD,
        older_than="2026-01-01T00:03:00+00:00",
        limit=5,
    )

    assert archived_sent == 1
    assert archived_dead == 1
    assert repo.get_message("in-archive-sent") is None
    assert repo.get_message("in-archive-dead") is None
    assert set(db.archive_rows.keys()) == {"in-archive-sent", "in-archive-dead"}
    assert db.archive_rows["in-archive-sent"]["archived_by"] == "ops-user"
    assert db.archive_rows["in-archive-sent"]["archive_reason"] == "retention"


def test_outbound_queue_processed_records_can_be_archived_through_outbox_adapter() -> None:
    outbox = _FakeOutboxRepository()
    repo = DatabaseOutboundQueueRepository(outbox_repository=outbox)
    envelope = OutboundQueueEnvelope(
        message_id="out-archive-sent",
        message_type="conversation.state_transitioned",
        aggregate_type="conversation_state",
        aggregate_id="+61400000999",
        payload={"to_state": "COLLECTING"},
        metadata=QueueMessageMetadata(dedup_key="dedup-out-archive"),
        max_attempts=2,
    )
    assert repo.enqueue(envelope) is True
    assert repo.mark_processing("out-archive-sent") is True
    assert repo.mark_sent("out-archive-sent") is True

    archived = repo.archive_processed_records(
        status=QueueStatus.SENT,
        older_than="2026-01-01T00:10:00+00:00",
        limit=3,
        archived_by="ops-user",
        archive_reason="retention",
    )

    assert archived == 1
    assert repo.get_message("out-archive-sent") is None
    assert "out-archive-sent" in outbox.archive_rows
