from __future__ import annotations

import json
from typing import Any

from app.events.outbox import DatabaseOutboxRepository, OutboxEventEnvelope, OutboxStatus


class _FakeOutboxDB:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.idempotency_index: dict[str, str] = {}

    def execute_query(self, query, params=(), fetch=False, conn=None, **_kwargs):
        _ = conn
        sql = " ".join(str(query).split()).lower()
        # DDL passthrough — all CREATE TABLE / CREATE INDEX statements are no-ops in tests
        if sql.startswith("create table") or sql.startswith("create index"):
            return [] if fetch else None
        if "insert into refactor_outbox_events" in sql:
            (
                event_id,
                idempotency_key,
                event_type,
                aggregate_type,
                aggregate_id,
                payload_json,
                metadata_json,
                occurred_at,
                max_retries,
            ) = params
            if event_id in self.rows or idempotency_key in self.idempotency_index:
                return [] if fetch else None
            row = {
                "event_id": event_id,
                "idempotency_key": idempotency_key,
                "event_type": event_type,
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "payload": json.loads(payload_json),
                "metadata": json.loads(metadata_json),
                "status": OutboxStatus.PENDING,
                "retry_count": 0,
                "max_retries": max_retries,
                "next_retry_at": None,
                "processing_started_at": None,
                "last_attempt_at": None,
                "last_error": None,
                "last_error_at": None,
                "dead_lettered_at": None,
                "occurred_at": occurred_at,
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
            }
            self.rows[event_id] = row
            self.idempotency_index[idempotency_key] = event_id
            return [{"event_id": event_id}] if fetch else None
        if "set status = 'processing'" in sql:
            (event_id,) = params
            row = self.rows.get(event_id)
            if row is None or row["status"] not in {OutboxStatus.PENDING, OutboxStatus.FAILED}:
                return [] if fetch else None
            row["status"] = OutboxStatus.PROCESSING
            row["processing_started_at"] = "2026-01-01T00:01:00+00:00"
            row["last_attempt_at"] = "2026-01-01T00:01:00+00:00"
            row["updated_at"] = "2026-01-01T00:01:00+00:00"
            return [{"event_id": event_id}] if fetch else None
        if "set retry_count = retry_count + 1" in sql:
            error_message, delay_seconds, _delay_seconds_2, event_id = params
            row = self.rows.get(event_id)
            if row is None or row["status"] not in {OutboxStatus.PENDING, OutboxStatus.PROCESSING}:
                return [] if fetch else None
            row["retry_count"] = int(row["retry_count"]) + 1
            row["last_error"] = error_message
            row["last_error_at"] = "2026-01-01T00:02:00+00:00"
            if row["retry_count"] >= int(row["max_retries"]):
                row["status"] = OutboxStatus.DEAD_LETTER
                row["next_retry_at"] = None
                row["dead_lettered_at"] = "2026-01-01T00:02:00+00:00"
            else:
                row["status"] = OutboxStatus.FAILED
                row["next_retry_at"] = f"+{int(delay_seconds)}s"
            row["updated_at"] = "2026-01-01T00:02:00+00:00"
            return [{"event_id": event_id}] if fetch else None
        if "set status = 'published'" in sql:
            (event_id,) = params
            row = self.rows.get(event_id)
            if row is None or row["status"] not in {OutboxStatus.PROCESSING, OutboxStatus.PUBLISHED}:
                return [] if fetch else None
            row["status"] = OutboxStatus.PUBLISHED
            row["next_retry_at"] = None
            row["last_error"] = None
            row["last_error_at"] = None
            row["updated_at"] = "2026-01-01T00:03:00+00:00"
            return [{"event_id": event_id}] if fetch else None
        if "where status in ('pending', 'failed')" in sql and "limit %s" in sql:
            (limit,) = params
            pending = [
                dict(row)
                for row in self.rows.values()
                if row["status"] in {OutboxStatus.PENDING, OutboxStatus.FAILED}
            ]
            pending.sort(key=lambda row: str(row.get("created_at") or ""))
            return pending[: int(limit)] if fetch else None
        if "where status = 'dead_letter'" in sql and "limit %s" in sql:
            (limit,) = params
            dead = [
                dict(row)
                for row in self.rows.values()
                if row["status"] == OutboxStatus.DEAD_LETTER
            ]
            dead.sort(key=lambda row: str(row.get("dead_lettered_at") or row.get("updated_at") or row.get("created_at") or ""))
            return dead[: int(limit)] if fetch else None
        if "set status = 'pending'" in sql and "where event_id = %s" in sql and "dead_letter" in sql:
            metadata_json, event_id = params
            row = self.rows.get(event_id)
            if row is None or row["status"] != OutboxStatus.DEAD_LETTER:
                return [] if fetch else None
            row["status"] = OutboxStatus.PENDING
            row["retry_count"] = 0
            row["next_retry_at"] = None
            row["processing_started_at"] = None
            row["last_attempt_at"] = None
            row["last_error"] = None
            row["last_error_at"] = None
            row["metadata"] = json.loads(metadata_json)
            row["updated_at"] = "2026-01-01T00:04:00+00:00"
            return [{"event_id": event_id}] if fetch else None
        if "from refactor_outbox_events" in sql and "where event_id = %s" in sql:
            event_id = params[0]
            row = self.rows.get(event_id)
            if row is None:
                return [] if fetch else None
            return [dict(row)] if fetch else None
        raise AssertionError(f"Unexpected SQL in fake DB: {query}")


def _sample_event(
    *,
    event_id: str = "evt-1",
    idempotency_key: str = "idem-1",
    max_retries: int = 3,
) -> OutboxEventEnvelope:
    return OutboxEventEnvelope(
        event_id=event_id,
        idempotency_key=idempotency_key,
        event_type="conversation.state_transitioned",
        aggregate_type="conversation_state",
        aggregate_id="+61400000123",
        payload={"from_state": "NEW", "to_state": "COLLECTING", "nested": {"version": 2}},
        metadata={"source": "tests", "actor": "intent_router"},
        occurred_at="2026-01-01T00:00:00+00:00",
        max_retries=max_retries,
    )


def test_outbox_persists_event_contract() -> None:
    db = _FakeOutboxDB()
    repo = DatabaseOutboxRepository(db)

    inserted = repo.append_event(_sample_event(max_retries=4))
    record = repo.get_event("evt-1")

    assert inserted is True
    assert record is not None
    assert record.event_id == "evt-1"
    assert record.idempotency_key == "idem-1"
    assert record.event_type == "conversation.state_transitioned"
    assert record.aggregate_type == "conversation_state"
    assert record.aggregate_id == "+61400000123"
    assert record.payload["nested"]["version"] == 2
    assert record.metadata["actor"] == "intent_router"
    assert record.status == OutboxStatus.PENDING
    assert record.retry_count == 0
    assert record.max_retries == 4


def test_outbox_append_is_idempotent_for_duplicate_event_id_or_key() -> None:
    db = _FakeOutboxDB()
    repo = DatabaseOutboxRepository(db)

    first = repo.append_event(_sample_event(event_id="evt-dup", idempotency_key="idem-dup"))
    duplicate_event_id = repo.append_event(_sample_event(event_id="evt-dup", idempotency_key="idem-new"))
    duplicate_idempotency = repo.append_event(_sample_event(event_id="evt-new", idempotency_key="idem-dup"))

    assert first is True
    assert duplicate_event_id is False
    assert duplicate_idempotency is False
    assert repo.get_event("evt-dup") is not None
    assert repo.get_event("evt-new") is None


def test_outbox_status_transition_processing_to_failure_updates_retry_metadata() -> None:
    db = _FakeOutboxDB()
    repo = DatabaseOutboxRepository(db)
    repo.append_event(_sample_event(event_id="evt-retry", idempotency_key="idem-retry", max_retries=2))

    moved_to_processing = repo.mark_processing("evt-retry")
    first_failure = repo.mark_failure(
        "evt-retry",
        error_message="worker-timeout",
        retry_delay_seconds=30,
    )
    after_failure = repo.get_event("evt-retry")

    assert moved_to_processing is True
    assert first_failure is True
    assert after_failure is not None
    assert after_failure.status == OutboxStatus.FAILED
    assert after_failure.retry_count == 1
    assert after_failure.last_error == "worker-timeout"
    assert after_failure.next_retry_at == "+30s"
    assert after_failure.processing_started_at is not None

    retry_cycle_processing = repo.mark_processing("evt-retry")
    second_failure = repo.mark_failure(
        "evt-retry",
        error_message="worker-timeout-again",
        retry_delay_seconds=30,
    )
    after_dead_letter = repo.get_event("evt-retry")

    assert retry_cycle_processing is True
    assert second_failure is True
    assert after_dead_letter is not None
    assert after_dead_letter.status == OutboxStatus.DEAD_LETTER
    assert after_dead_letter.retry_count == 2
    assert after_dead_letter.dead_lettered_at is not None


def test_outbox_pending_poll_and_mark_published_flow() -> None:
    db = _FakeOutboxDB()
    repo = DatabaseOutboxRepository(db)
    repo.append_event(_sample_event(event_id="evt-send", idempotency_key="idem-send"))

    pending = repo.list_pending(limit=10)
    moved_to_processing = repo.mark_processing("evt-send")
    marked_published = repo.mark_published("evt-send")
    after_send = repo.get_event("evt-send")

    assert len(pending) == 1
    assert pending[0].event_id == "evt-send"
    assert moved_to_processing is True
    assert marked_published is True
    assert after_send is not None
    assert after_send.status == OutboxStatus.PUBLISHED
    assert repo.list_pending(limit=10) == []


def test_outbox_dead_letter_replay_resets_pending_and_persists_audit_metadata() -> None:
    db = _FakeOutboxDB()
    repo = DatabaseOutboxRepository(db)
    repo.append_event(_sample_event(event_id="evt-replay", idempotency_key="idem-replay", max_retries=1))
    repo.mark_processing("evt-replay")
    repo.mark_failure("evt-replay", error_message="boom", retry_delay_seconds=5)

    dead = repo.list_dead(limit=10)
    assert len(dead) == 1
    assert dead[0].status == OutboxStatus.DEAD_LETTER

    replayed = repo.replay_dead(
        "evt-replay",
        replay_metadata={
            "actor": "ops-user",
            "reason": "manual replay",
            "replay_run_id": "run-1",
            "idempotency_key": "idem-run-1",
        },
    )
    record = repo.get_event("evt-replay")

    assert replayed is True
    assert record is not None
    assert record.status == OutboxStatus.PENDING
    assert record.retry_count == 0
    assert record.last_error is None
    assert record.metadata["dlq_replay"]["actor"] == "ops-user"
    assert record.metadata["dlq_replay"]["replay_run_id"] == "run-1"
