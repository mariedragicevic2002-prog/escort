from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from app.events.outbox import OutboxEventRecord, OutboxStatus
from app.guardrails import SLOGuardrailAction, SLOGuardrailEngine, SLOGuardrailPolicy, SLOGuardrailSignals
from app.ingress.quick_ack import try_enqueue_sms_quick_ack
from app.resilience import (
    DeterministicFailureInjector,
    DeterministicFailurePlan,
    DrillAssertion,
    ResilienceDrillContext,
    ResilienceDrillRunner,
    ResilienceDrillScenario,
    ResilienceDrillStep,
    RetryableDrillError,
)
from app.workers.dispatcher import OutboxEventDispatcher
from app.workers.runtime import OutboxWorkerRuntime
from app.workers.supervision import InMemoryWorkerLeaseStore, WorkerHeartbeatTracker, WorkerSupervisionRuntime


class _StaticInboundProvider:
    def __init__(self) -> None:
        self._message_ids: set[str] = set()

    def enqueue(self, envelope) -> bool:
        if envelope.message_id in self._message_ids:
            return False
        self._message_ids.add(envelope.message_id)
        return True

    def list_pending(self, *, limit: int = 100, conn: Any | None = None) -> list[Any]:
        _ = (limit, conn)
        return []

    def list_dead(self, *, limit: int = 100, conn: Any | None = None) -> list[Any]:
        _ = (limit, conn)
        return []


class _MutableClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: int) -> None:
        self._now = self._now + timedelta(seconds=max(0, int(seconds)))


class _OutboxRepository:
    def __init__(self) -> None:
        self._rows: dict[str, OutboxEventRecord] = {}
        self.transitions: dict[str, list[str]] = {}
        self.recovered: list[str] = []

    def add(self, event: OutboxEventRecord) -> None:
        self._rows[event.event_id] = event
        self.transitions[event.event_id] = [event.status]

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
        self._rows[event_id] = replace(row, status=OutboxStatus.PROCESSING)
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
        _ = (retry_delay_seconds, conn)
        row = self._rows.get(event_id)
        if row is None or row.status not in {OutboxStatus.PENDING, OutboxStatus.PROCESSING}:
            return False
        self._rows[event_id] = replace(
            row,
            status=OutboxStatus.FAILED,
            retry_count=max(0, int(row.retry_count)) + 1,
            last_error=error_message,
        )
        self.transitions[event_id].append(OutboxStatus.FAILED)
        return True

    def mark_published(self, event_id: str, *, conn: Any | None = None) -> bool:
        _ = conn
        row = self._rows.get(event_id)
        if row is None or row.status not in {OutboxStatus.PROCESSING, OutboxStatus.PUBLISHED}:
            return False
        self._rows[event_id] = replace(row, status=OutboxStatus.PUBLISHED, last_error=None)
        self.transitions[event_id].append(OutboxStatus.PUBLISHED)
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
            retry_count=max(0, int(row.retry_count)) + 1,
            last_error=error_message,
        )
        self.transitions[event_id].append(OutboxStatus.FAILED)
        self.recovered.append(event_id)
        return True

    def get(self, event_id: str) -> OutboxEventRecord | None:
        return self._rows.get(event_id)


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


def _event(event_id: str) -> OutboxEventRecord:
    now = "2026-01-01T00:00:00+00:00"
    return OutboxEventRecord(
        event_id=event_id,
        idempotency_key=f"idem-{event_id}",
        event_type="conversation.state_transitioned",
        aggregate_type="conversation",
        aggregate_id=f"agg-{event_id}",
        payload={"hello": "world"},
        metadata={},
        status=OutboxStatus.PENDING,
        retry_count=0,
        max_retries=3,
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


def _policy() -> SLOGuardrailPolicy:
    return SLOGuardrailPolicy(enabled=True, min_sample_size=1, cooldown_seconds=120, hysteresis_factor=0.8)


def _run_ingress_deterministic_drill() -> dict[str, Any]:
    provider = _StaticInboundProvider()
    injector = DeterministicFailureInjector(
        enabled=True,
        test_mode=True,
        plan=DeterministicFailurePlan(ingress_enqueue_failure=(1,)),
    )
    env = {"REFACTOR_SMS_INGRESS_QUICK_ACK_ENABLED": "1"}

    def _attempt(label: str, context: ResilienceDrillContext) -> None:
        outcome = try_enqueue_sms_quick_ack(
            db_service=object(),
            phone_number="+61400000000",
            message_body="hello",
            message_data={"id": "msg-1"},
            request_payload={"id": "req-1"},
            request_headers={"X-Test": "1"},
            remote_addr="127.0.0.1",
            request_id="req-1",
            env=env,
            setting_getter=lambda *_args, **_kwargs: None,
            inbound_provider=provider,
            drill_hook=injector,
        )
        reasons = context.artifacts.setdefault("ingress_reasons", [])
        reasons.append({"step": label, "reason": outcome.reason, "accepted": outcome.accepted})
        next_state = "accepted" if outcome.accepted else "retry"
        context.transition(component="ingress", to_state=next_state, reason=outcome.reason)

    scenario = ResilienceDrillScenario(
        scenario_id="ingress-determinism",
        description="inject enqueue failure then recover",
        steps=(
            ResilienceDrillStep(step_id="attempt-1", handler=lambda context: _attempt("attempt-1", context)),
            ResilienceDrillStep(step_id="attempt-2", handler=lambda context: _attempt("attempt-2", context)),
        ),
        max_step_executions=5,
    )
    runner = ResilienceDrillRunner(test_mode=True, max_step_executions=5)
    report = runner.run(
        scenario,
        context=ResilienceDrillContext(),
        drill_hook=injector,
        assertions=(
            DrillAssertion(
                assertion_id="first-attempt-fails",
                predicate=lambda payload: payload.artifacts["ingress_reasons"][0]["reason"]
                == "drill_ingress_enqueue_failure",
                failure_message="expected deterministic enqueue failure on first attempt",
            ),
            DrillAssertion(
                assertion_id="second-attempt-recovers",
                predicate=lambda payload: payload.artifacts["ingress_reasons"][1]["reason"] == "enqueued",
                failure_message="expected recovery enqueue on second attempt",
            ),
        ),
    )
    assert report.succeeded is True
    return report.to_artifact()


def test_resilience_drill_runner_is_deterministic_for_same_scenario() -> None:
    first = _run_ingress_deterministic_drill()
    second = _run_ingress_deterministic_drill()

    assert first == second
    assert first["artifacts"]["ingress_reasons"][0]["reason"] == "drill_ingress_enqueue_failure"
    assert first["artifacts"]["ingress_reasons"][1]["reason"] == "enqueued"


def test_worker_crash_and_lease_expiry_recover_with_bounded_transitions() -> None:
    clock = _MutableClock()
    lease_store = InMemoryWorkerLeaseStore(clock=clock)
    repository = _OutboxRepository()
    repository.add(_event("evt-drill"))
    guard = _IdempotencyGuard()
    dispatcher = OutboxEventDispatcher()
    dispatched: list[str] = []
    dispatcher.register("conversation.state_transitioned", lambda event: dispatched.append(event.event_id))

    crash_injector = DeterministicFailureInjector(
        enabled=True,
        test_mode=True,
        plan=DeterministicFailurePlan(worker_crash=(1,), worker_lease_expiry=(1,)),
        lease_expiry_advancer=lambda _queue_name, _item_id: clock.advance(6),
    )
    crashed_runtime = OutboxWorkerRuntime(
        outbox_repository=repository,
        dispatcher=dispatcher,
        idempotency_guard=guard,
        supervision=WorkerSupervisionRuntime(
            queue_name="refactor_outbox",
            worker_id="worker-crash",
            lease_duration_seconds=5,
            lease_store=lease_store,
            heartbeat_tracker=WorkerHeartbeatTracker(clock=clock),
        ),
        drill_hook=crash_injector,
    )

    first = crashed_runtime.run_once()
    after_crash = repository.get("evt-drill")
    assert first.polled == 1
    assert first.sent == 0
    assert after_crash is not None
    assert after_crash.status == OutboxStatus.PROCESSING

    recovery_runtime = OutboxWorkerRuntime(
        outbox_repository=repository,
        dispatcher=dispatcher,
        idempotency_guard=guard,
        supervision=WorkerSupervisionRuntime(
            queue_name="refactor_outbox",
            worker_id="worker-recovery",
            lease_duration_seconds=5,
            lease_store=lease_store,
            heartbeat_tracker=WorkerHeartbeatTracker(clock=clock),
        ),
    )
    second = recovery_runtime.run_once()

    assert second.sent == 1
    assert repository.recovered == ["evt-drill"]
    assert repository.transitions["evt-drill"] == [
        OutboxStatus.PENDING,
        OutboxStatus.PROCESSING,
        OutboxStatus.FAILED,
        OutboxStatus.PROCESSING,
        OutboxStatus.PUBLISHED,
    ]
    assert dispatched == ["evt-drill"]


def test_guardrail_rollback_trigger_can_be_forced_by_drill_hook() -> None:
    injector = DeterministicFailureInjector(
        enabled=True,
        test_mode=True,
        plan=DeterministicFailurePlan(guardrail_rollback_trigger=(1,)),
    )
    engine = SLOGuardrailEngine(policy=_policy(), drill_hook=injector)

    decision, _ = engine.evaluate(
        signals=SLOGuardrailSignals(
            retry_rate=0.01,
            dead_letter_rate=0.0,
            queue_lag_seconds=1.0,
            failure_ratio=0.0,
            error_budget_remaining=0.99,
            sample_size=50,
        )
    )

    assert decision.action == SLOGuardrailAction.ROLLBACK
    assert decision.reason == "drill_guardrail_rollback_trigger"
    assert decision.transitioned is True


def test_runner_retry_limits_prevent_unbounded_retry_loops() -> None:
    attempts = {"count": 0}

    def _always_retry(_context: ResilienceDrillContext) -> None:
        attempts["count"] += 1
        raise RetryableDrillError("still failing")

    scenario = ResilienceDrillScenario(
        scenario_id="bounded-retries",
        description="runner must stop after bounded retries",
        steps=(ResilienceDrillStep(step_id="retry-step", handler=_always_retry, retry_limit=2),),
        max_step_executions=50,
    )
    report = ResilienceDrillRunner(test_mode=True, max_step_executions=50).run(scenario)

    assert report.succeeded is False
    assert report.bounded_execution is True
    assert report.step_attempts["retry-step"] == 3
    assert attempts["count"] == 3
    assert report.errors == ("retry-step:retry_exhausted:still failing",)
