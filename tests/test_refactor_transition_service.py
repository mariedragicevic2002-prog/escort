from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from refactor.app.events.outbox import OutboxEventEnvelope
from refactor.app.runtime.orchestration_facade import build_default_sms_facade
from refactor.app.runtime.transition_history import (
    DbTransitionHistoryRepository,
    TransitionHistoryRecord,
    build_transition_history_record,
)
from refactor.app.runtime.transition_service import (
    StateSnapshot,
    StateTransitionService,
    TransitionMetadataRecord,
    TransitionRequest,
)


class InMemoryTransitionStore:
    def __init__(self, *, phone_number: str, state: str, version: int) -> None:
        self._phone_number = phone_number
        self._state = state
        self._version = version
        self.force_conflict_once = False
        self.compare_and_set_calls = 0
        self.metadata_records: list[TransitionMetadataRecord] = []
        self.history_records: list[TransitionHistoryRecord] = []

    def get_snapshot(self, phone_number: str, *, conn: Any | None = None) -> StateSnapshot | None:
        _ = conn
        if phone_number != self._phone_number:
            return None
        return StateSnapshot(
            phone_number=phone_number,
            current_state=self._state,
            version=self._version,
        )

    def compare_and_set_state(
        self,
        phone_number: str,
        to_state: str,
        expected_version: int,
        *,
        conn: Any | None = None,
    ) -> int | None:
        _ = conn
        self.compare_and_set_calls += 1
        if phone_number != self._phone_number:
            return None
        if self.force_conflict_once:
            self.force_conflict_once = False
            return None
        if expected_version != self._version:
            return None
        self._state = to_state
        self._version += 1
        return self._version

    def append_transition_metadata(
        self,
        record: TransitionMetadataRecord,
        *,
        conn: Any | None = None,
    ) -> None:
        _ = conn
        self.metadata_records.append(record)
        self.history_records.append(build_transition_history_record(record))


class InMemoryOutboxRepository:
    def __init__(self) -> None:
        self.events: list[OutboxEventEnvelope] = []

    def append_event(self, event: OutboxEventEnvelope, *, conn: Any | None = None) -> bool:
        _ = conn
        self.events.append(event)
        return True

    def mark_processing(self, event_id: str, *, conn: Any | None = None) -> bool:
        _ = (event_id, conn)
        return False

    def mark_failure(
        self,
        event_id: str,
        *,
        error_message: str,
        retry_delay_seconds: int = 0,
        conn: Any | None = None,
    ) -> bool:
        _ = (event_id, error_message, retry_delay_seconds, conn)
        return False

    def get_event(self, event_id: str, *, conn: Any | None = None):
        _ = (event_id, conn)
        return None


def test_allowed_transition_succeeds_and_appends_metadata() -> None:
    hook_called = {"value": 0}

    def _hook(
        request: TransitionRequest,
        snapshot: StateSnapshot,
        committed_version: int,
    ) -> Mapping[str, Any]:
        hook_called["value"] += 1
        return {
            "source": "hook_should_not_override",
            "transition_pair": f"{snapshot.current_state}->{request.to_state}",
            "committed_version": committed_version,
        }

    store = InMemoryTransitionStore(phone_number="+61400000001", state="NEW", version=3)
    service = StateTransitionService(store=store, metadata_hooks=[_hook])
    request = TransitionRequest(
        phone_number="+61400000001",
        from_state="NEW",
        to_state="COLLECTING",
        expected_version=3,
        metadata={
            "source": "router",
            "actor": "intent_router",
            "correlation_id": "req-123",
            "intent": "booking_started",
        },
    )

    result = service.transition(request)

    assert result.status == "applied"
    assert result.ok is True
    assert result.committed_version == 4
    assert hook_called["value"] == 1
    assert store.history_records, "successful transitions should append immutable history"
    record = store.history_records[-1]
    assert record.conversation_id == "+61400000001"
    assert record.from_state == "NEW"
    assert record.to_state == "COLLECTING"
    assert record.version_before == 3
    assert record.version_after == 4
    assert record.actor == "intent_router"
    assert record.source == "router"
    assert record.correlation_id == "req-123"
    assert record.transitioned_at.tzinfo is not None
    assert record.metadata["intent"] == "booking_started"
    assert record.metadata["transition_pair"] == "NEW->COLLECTING"


def test_successful_transition_appends_outbox_event() -> None:
    store = InMemoryTransitionStore(phone_number="+61400000009", state="NEW", version=1)
    outbox = InMemoryOutboxRepository()
    service = StateTransitionService(store=store, outbox_repository=outbox)

    result = service.transition(
        TransitionRequest(
            phone_number="+61400000009",
            from_state="NEW",
            to_state="COLLECTING",
            expected_version=1,
            metadata={"actor": "intent_router", "source": "router"},
        )
    )

    assert result.status == "applied"
    assert len(outbox.events) == 1
    event = outbox.events[0]
    assert event.event_type == "conversation.state_transitioned"
    assert event.aggregate_type == "conversation_state"
    assert event.aggregate_id == "+61400000009"
    assert event.payload["from_state"] == "NEW"
    assert event.payload["to_state"] == "COLLECTING"
    assert event.payload["committed_version"] == 2


def test_invalid_transition_is_rejected_without_side_effects() -> None:
    store = InMemoryTransitionStore(phone_number="+61400000002", state="DEPOSIT_REQUIRED", version=6)
    service = StateTransitionService(store=store)
    request = TransitionRequest(
        phone_number="+61400000002",
        from_state="DEPOSIT_REQUIRED",
        to_state="COLLECTING",
        expected_version=6,
    )

    first = service.transition(request)
    second = service.transition(request)

    assert first.status == "rejected"
    assert first.reason == "invalid_transition"
    assert first == second
    assert store.compare_and_set_calls == 0
    assert store.history_records == []


def test_version_conflicts_are_surfaced() -> None:
    store = InMemoryTransitionStore(phone_number="+61400000003", state="COLLECTING", version=2)
    service = StateTransitionService(store=store)

    stale_request = TransitionRequest(
        phone_number="+61400000003",
        from_state="COLLECTING",
        to_state="CHECKING_AVAILABILITY",
        expected_version=1,
    )
    stale_result = service.transition(stale_request)
    assert stale_result.status == "conflict"
    assert stale_result.reason == "version_conflict"
    assert stale_result.observed_version == 2
    assert store.compare_and_set_calls == 0

    store.force_conflict_once = True
    race_request = TransitionRequest(
        phone_number="+61400000003",
        from_state="COLLECTING",
        to_state="CHECKING_AVAILABILITY",
        expected_version=2,
    )
    race_result = service.transition(race_request)
    assert race_result.status == "conflict"
    assert race_result.reason == "version_conflict"
    assert race_result.observed_version == 2
    assert store.compare_and_set_calls == 1
    assert store.history_records == []


def test_noop_transition_is_deterministic_and_side_effect_free() -> None:
    store = InMemoryTransitionStore(phone_number="+61400000004", state="CONFIRMED", version=10)
    service = StateTransitionService(store=store)
    request = TransitionRequest(
        phone_number="+61400000004",
        from_state="CONFIRMED",
        to_state="CONFIRMED",
        expected_version=10,
    )

    first = service.transition(request)
    second = service.transition(request)

    assert first.status == "noop"
    assert first.ok is True
    assert first == second
    assert store.compare_and_set_calls == 0
    assert store.history_records == []


def test_history_records_preserve_order_and_version_integrity() -> None:
    store = InMemoryTransitionStore(phone_number="+61400000005", state="NEW", version=1)
    service = StateTransitionService(store=store)

    first = service.transition(
        TransitionRequest(
            phone_number="+61400000005",
            from_state="NEW",
            to_state="COLLECTING",
            expected_version=1,
            metadata={"actor": "router", "source": "fsm"},
        )
    )
    second = service.transition(
        TransitionRequest(
            phone_number="+61400000005",
            from_state="COLLECTING",
            to_state="CHECKING_AVAILABILITY",
            expected_version=2,
            metadata={"actor": "router", "source": "fsm"},
        )
    )

    assert first.status == "applied"
    assert second.status == "applied"
    assert [row.from_state for row in store.history_records] == ["NEW", "COLLECTING"]
    assert [row.to_state for row in store.history_records] == ["COLLECTING", "CHECKING_AVAILABILITY"]
    assert [(row.version_before, row.version_after) for row in store.history_records] == [(1, 2), (2, 3)]
    assert store.history_records[0].version_after == store.history_records[1].version_before


def test_build_default_sms_facade_wires_transition_service() -> None:
    class _StateManagerStub:
        db = None

        def get_state(self, phone_number: str, conn=None):
            _ = (phone_number, conn)
            return None

    facade = build_default_sms_facade(
        state_manager=_StateManagerStub(),
        db_service=object(),
        legacy_processor=lambda _phone, _body: [],
    )

    runtime = facade._runtime_services
    assert runtime.transition_service is not None


def test_build_default_sms_facade_writes_transition_outbox_records() -> None:
    class _DbStub:
        def __init__(self) -> None:
            self.outbox_insert_params: list[tuple[Any, ...]] = []

        def execute_query(self, query, params=(), fetch=None, conn=None, **_kwargs):
            _ = conn
            sql = " ".join(str(query).split()).lower()
            if "update conversation_states" in sql:
                return [{"version": 2}]
            if "insert into refactor_outbox_events" in sql:
                self.outbox_insert_params.append(tuple(params))
                return [{"event_id": params[0]}] if fetch else None
            if fetch:
                return []
            return None

    class _StateManagerStub:
        def __init__(self, db) -> None:
            self.db = db

        def get_state(self, phone_number: str, conn=None):
            _ = conn
            return {"phone_number": phone_number, "current_state": "NEW", "version": 1}

    db = _DbStub()
    facade = build_default_sms_facade(
        state_manager=_StateManagerStub(db),
        db_service=db,
        legacy_processor=lambda _phone, _body: [],
    )

    result = facade.transition_state(
        TransitionRequest(
            phone_number="+61400000010",
            from_state="NEW",
            to_state="COLLECTING",
            expected_version=1,
            metadata={"actor": "intent_router", "source": "router"},
        )
    )

    assert result.status == "applied"
    assert db.outbox_insert_params, "successful transitions should append outbox events"
    params = db.outbox_insert_params[-1]
    assert params[2] == "conversation.state_transitioned"
    assert params[3] == "conversation_state"
    assert params[4] == "+61400000010"


def test_db_history_repository_is_append_only_insert_path() -> None:
    class _DbStub:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, Any, Any, Any]] = []

        def execute_query(self, query, params=(), fetch=None, conn=None, **_kwargs):
            self.calls.append((query, params, fetch, conn))
            return None

    db = _DbStub()
    repository = DbTransitionHistoryRepository(db)
    repository.append(
        TransitionHistoryRecord(
            conversation_id="+61400000999",
            from_state="NEW",
            to_state="COLLECTING",
            version_before=7,
            version_after=8,
            transitioned_at=datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC),
            actor="intent_router",
            source="router",
            correlation_id="req-999",
            metadata={"actor": "intent_router", "source": "router"},
        )
    )

    assert len(db.calls) == 1
    sql, params, fetch, conn = db.calls[0]
    assert "INSERT INTO conversation_transition_history" in sql
    assert fetch is False
    assert conn is None
    assert params[0] == "+61400000999"
    assert params[3] == 7
    assert params[4] == 8
    payload = json.loads(params[9])
    assert payload["actor"] == "intent_router"
