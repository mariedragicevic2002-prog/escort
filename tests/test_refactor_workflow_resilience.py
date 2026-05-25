from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Lock

import pytest

from app.events.outbox import OutboxEventEnvelope
from app.middleware.idempotency import IdempotencyMiddleware, RetryableInboundError
from app.middleware.request_validation import RequestValidationMiddleware
from app.runtime.context import InboundSMSMessage, RuntimeServices
from app.runtime.intent_router import IntentRouter, SignalIntentResolver
from app.runtime.orchestration_facade import OrchestrationFacade
from app.runtime.transition_service import (
    StateSnapshot,
    StateTransitionService,
    TransitionMetadataRecord,
    TransitionRequest,
)


class _DedupClaimStore:
    def __init__(self, *, fail_keys: set[str] | None = None) -> None:
        self._claimed: set[str] = set()
        self._lock = Lock()
        self._fail_keys = set(fail_keys or set())

    def claim(self, key: str) -> bool:
        if key in self._fail_keys:
            raise RuntimeError("dedup store unavailable")
        with self._lock:
            if key in self._claimed:
                return False
            self._claimed.add(key)
            return True


class _InMemoryTransitionStore:
    def __init__(
        self,
        *,
        phone_number: str,
        state: str = "NEW",
        version: int = 1,
        fail_metadata_append: bool = False,
    ) -> None:
        self._phone_number = phone_number
        self._state = state
        self._version = version
        self._lock = Lock()
        self.fail_metadata_append = fail_metadata_append
        self.metadata_records: list[TransitionMetadataRecord] = []

    def get_snapshot(self, phone_number: str, *, conn=None) -> StateSnapshot | None:
        _ = conn
        with self._lock:
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
        conn=None,
    ) -> int | None:
        _ = conn
        with self._lock:
            if phone_number != self._phone_number:
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
        conn=None,
    ) -> None:
        _ = conn
        if self.fail_metadata_append:
            raise RuntimeError("history repository unavailable")
        self.metadata_records.append(record)


class _FailingOutboxRepository:
    def append_event(self, event: OutboxEventEnvelope, *, conn=None) -> bool:
        _ = (event, conn)
        raise RuntimeError("outbox unavailable")

    def mark_processing(self, event_id: str, *, conn=None) -> bool:
        _ = (event_id, conn)
        return False

    def mark_failure(self, event_id: str, *, error_message: str, retry_delay_seconds: int = 0, conn=None) -> bool:
        _ = (event_id, error_message, retry_delay_seconds, conn)
        return False

    def get_event(self, event_id: str, *, conn=None):
        _ = (event_id, conn)
        return None


@dataclass(frozen=True)
class _LoadHarnessResult:
    total_attempts: int
    processed: int
    duplicates: int
    checksum: str


def _inbound(*, message_id: str, body: str = "hello", intent: str | None = None) -> InboundSMSMessage:
    message_data = {"id": message_id}
    if intent:
        message_data["intent"] = intent
    return InboundSMSMessage(
        phone_number="+61412345678",
        body=body,
        message_data=message_data,
        request_payload={"id": message_id},
        request_id=message_id,
    )


def _build_facade(*, dedup_store: _DedupClaimStore, legacy_processor, intent_router=None, transition_service=None):
    runtime = RuntimeServices(
        state_manager=object(),
        db_service=object(),
        legacy_processor=legacy_processor,
        intent_router=intent_router,
        transition_service=transition_service,
    )
    return OrchestrationFacade(
        runtime_services=runtime,
        middlewares=[
            RequestValidationMiddleware(),
            IdempotencyMiddleware(
                key_builder=lambda message_data, _payload, _phone, _body: str(message_data.get("id") or ""),
                key_claimer=lambda _db, key: dedup_store.claim(key),
            ),
        ],
    )


def _run_load_harness(facade: OrchestrationFacade, *, unique_turns: int, replay_every: int) -> _LoadHarnessResult:
    outcomes = []
    for idx in range(unique_turns):
        message_id = f"load-{idx:03d}"
        outcomes.append(facade.process_sms(_inbound(message_id=message_id, body=f"message-{idx}")))
        if idx % replay_every == 0:
            outcomes.append(facade.process_sms(_inbound(message_id=message_id, body=f"message-{idx}")))

    processed = [outcome for outcome in outcomes if not outcome.duplicate]
    duplicates = [outcome for outcome in outcomes if outcome.duplicate]
    checksum = "|".join(message for outcome in processed for message in outcome.messages)
    return _LoadHarnessResult(
        total_attempts=len(outcomes),
        processed=len(processed),
        duplicates=len(duplicates),
        checksum=checksum,
    )


def test_workflow_replay_across_multiple_turns_skips_replayed_turn() -> None:
    store = _InMemoryTransitionStore(phone_number="+61412345678", state="NEW", version=1)
    transition_service = StateTransitionService(store=store)
    dedup_store = _DedupClaimStore()

    def _transition_handler(to_state: str):
        def _handler(context) -> list[str]:
            snapshot = store.get_snapshot(context.inbound.phone_number)
            assert snapshot is not None
            result = context.runtime.transition_service.transition(
                TransitionRequest(
                    phone_number=context.inbound.phone_number,
                    from_state=snapshot.current_state,
                    to_state=to_state,
                    expected_version=snapshot.version,
                    metadata={"actor": "intent_router", "source": "workflow_replay"},
                )
            )
            assert result.status == "applied"
            return [f"{result.from_state}->{result.to_state}@{result.committed_version}"]

        return _handler

    router = IntentRouter(
        resolver=SignalIntentResolver(),
        intent_handlers={
            "booking_started": _transition_handler("COLLECTING"),
            "fields_complete": _transition_handler("CHECKING_AVAILABILITY"),
        },
    )
    facade = _build_facade(
        dedup_store=dedup_store,
        legacy_processor=lambda _phone, _body: ["legacy"],
        intent_router=router,
        transition_service=transition_service,
    )

    first_turn = facade.process_sms(_inbound(message_id="turn-1", intent="booking_started"))
    second_turn = facade.process_sms(_inbound(message_id="turn-2", intent="fields_complete"))
    replay_second_turn = facade.process_sms(_inbound(message_id="turn-2", intent="fields_complete"))

    assert first_turn.duplicate is False
    assert second_turn.duplicate is False
    assert first_turn.messages == ["NEW->COLLECTING@2"]
    assert second_turn.messages == ["COLLECTING->CHECKING_AVAILABILITY@3"]
    assert replay_second_turn.duplicate is True
    assert replay_second_turn.messages == []
    assert len(store.metadata_records) == 2
    latest = store.get_snapshot("+61412345678")
    assert latest is not None
    assert latest.current_state == "CHECKING_AVAILABILITY"
    assert latest.version == 3


def test_concurrent_replayed_inbound_only_processes_once() -> None:
    dedup_store = _DedupClaimStore()
    calls = {"count": 0}
    call_lock = Lock()

    def _legacy(_phone: str, _body: str) -> list[str]:
        with call_lock:
            calls["count"] += 1
        return ["ok"]

    facade = _build_facade(dedup_store=dedup_store, legacy_processor=_legacy)
    inbound = _inbound(message_id="concurrent-1", body="parallel")

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _i: facade.process_sms(inbound), range(8)))

    processed_count = sum(1 for outcome in outcomes if not outcome.duplicate)
    duplicate_count = sum(1 for outcome in outcomes if outcome.duplicate)

    assert processed_count == 1
    assert duplicate_count == 7
    assert calls["count"] == 1


def test_load_harness_is_deterministic_and_non_destructive() -> None:
    dedup_store = _DedupClaimStore()
    facade = _build_facade(
        dedup_store=dedup_store,
        legacy_processor=lambda _phone, body: [f"reply:{body}"],
    )

    result = _run_load_harness(facade, unique_turns=24, replay_every=6)

    assert result.total_attempts == 28
    assert result.processed == 24
    assert result.duplicates == 4
    assert result.checksum.startswith("reply:message-0|reply:message-1|reply:message-2")
    assert result.checksum.endswith("reply:message-23")


def test_chaos_idempotency_failure_raises_retryable_error() -> None:
    dedup_store = _DedupClaimStore(fail_keys={"boom-key"})
    facade = _build_facade(
        dedup_store=dedup_store,
        legacy_processor=lambda _phone, _body: ["ok"],
    )

    with pytest.raises(RetryableInboundError, match="Idempotency store unavailable"):
        facade.process_sms(_inbound(message_id="boom-key"))


def test_chaos_repository_failures_do_not_break_transition_application() -> None:
    store = _InMemoryTransitionStore(
        phone_number="+61412345678",
        state="NEW",
        version=1,
        fail_metadata_append=True,
    )
    service = StateTransitionService(
        store=store,
        outbox_repository=_FailingOutboxRepository(),
    )

    result = service.transition(
        TransitionRequest(
            phone_number="+61412345678",
            from_state="NEW",
            to_state="COLLECTING",
            expected_version=1,
            metadata={"actor": "chaos-test", "source": "tests"},
        )
    )

    assert result.status == "applied"
    assert result.committed_version == 2
    assert store.metadata_records == []
    snapshot = store.get_snapshot("+61412345678")
    assert snapshot is not None
    assert snapshot.current_state == "COLLECTING"
    assert snapshot.version == 2
