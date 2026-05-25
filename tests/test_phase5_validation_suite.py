from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping

from refactor.app.guardrails import (
    SLOGuardrailAction,
    SLOGuardrailDecision,
    SLOGuardrailEngine,
    SLOGuardrailPolicy,
    SLOGuardrailSignals,
)
from refactor.app.incidents.executor import BoundedActionExecutor, BoundedActionPolicy
from refactor.app.incidents.hooks import GuardrailIncidentHook, IncidentAutomationSafetyPolicy
from refactor.app.incidents.notifier import InMemoryAlertNotifier
from refactor.app.ingress.adaptive_rate_limiter import (
    AdaptiveIngressSignals,
    AdaptiveRateLimiterSettings,
    resolve_adaptive_rate_limit_decision,
    sample_adaptive_ingress_signals,
)
from refactor.app.ingress.backpressure_policy import IngressBackpressureDecision
from refactor.app.ops.production_readiness_service import ProductionReadinessReportService
from refactor.app.queue.status import QueueStatus
from refactor.app.retention import QueueArchivalCommand, QueueArchivalRetentionPolicy, QueueArchivalService


def _adaptive_settings(**overrides: Any) -> AdaptiveRateLimiterSettings:
    base = AdaptiveRateLimiterSettings(
        enabled=True,
        deterministic_mode=None,
        provider_failure_mode="sync_fallback",
        base_queue_depth=100,
        base_lag_seconds=120,
        retry_rate_threshold=0.4,
        failure_rate_threshold=0.25,
        throttle_max_attempts=1,
        reliability_sample_size=25,
    )
    return AdaptiveRateLimiterSettings(**{**base.__dict__, **overrides})


def _adaptive_backpressure(*, queue_depth: int = 80, lag_seconds: float = 30.0) -> IngressBackpressureDecision:
    return IngressBackpressureDecision(
        allow_enqueue=True,
        overloaded=False,
        reason="backpressure_within_threshold",
        behavior="degrade_mode",
        trigger="none",
        provider_available=True,
        effective_max_attempts=5,
        queue_depth=queue_depth,
        oldest_lag_seconds=lag_seconds,
    )


@dataclass(frozen=True)
class _AdaptiveRow:
    status: str


class _AdaptiveSignalsProvider:
    def __init__(self, *, pending_statuses: list[str], dead_count: int, fail: bool = False) -> None:
        self.pending_statuses = pending_statuses
        self.dead_count = dead_count
        self.fail = fail

    def list_pending(self, *, limit: int = 100, conn: Any | None = None):
        _ = conn
        if self.fail:
            raise RuntimeError("injected pending failure")
        rows = [_AdaptiveRow(status=status) for status in self.pending_statuses]
        return rows[: max(1, int(limit))]

    def list_dead(self, *, limit: int = 100, conn: Any | None = None):
        _ = conn
        if self.fail:
            raise RuntimeError("injected dead failure")
        rows = [_AdaptiveRow(status=QueueStatus.DEAD) for _ in range(self.dead_count)]
        return rows[: max(1, int(limit))]


def test_phase5_adaptive_limiter_signal_matrix_is_deterministic_under_load() -> None:
    settings = _adaptive_settings()
    backpressure = _adaptive_backpressure()
    scenarios = [
        (
            AdaptiveIngressSignals(
                queue_depth=30,
                oldest_lag_seconds=10.0,
                retry_rate=0.05,
                failure_rate=0.01,
                pending_sample_size=50,
                dead_sample_size=1,
                provider_available=True,
                source="phase5",
            ),
            "allow",
        ),
        (
            AdaptiveIngressSignals(
                queue_depth=90,
                oldest_lag_seconds=15.0,
                retry_rate=0.10,
                failure_rate=0.02,
                pending_sample_size=50,
                dead_sample_size=1,
                provider_available=True,
                source="phase5",
            ),
            "allow",
        ),
        (
            AdaptiveIngressSignals(
                queue_depth=90,
                oldest_lag_seconds=20.0,
                retry_rate=0.45,
                failure_rate=0.10,
                pending_sample_size=50,
                dead_sample_size=5,
                provider_available=True,
                source="phase5",
            ),
            "throttle",
        ),
        (
            AdaptiveIngressSignals(
                queue_depth=40,
                oldest_lag_seconds=20.0,
                retry_rate=0.20,
                failure_rate=0.30,
                pending_sample_size=50,
                dead_sample_size=10,
                provider_available=True,
                source="phase5",
            ),
            "sync_fallback",
        ),
    ]

    expected_modes = [expected for _, expected in scenarios]
    observed_modes: list[str] = []
    for _ in range(10):
        for signals, expected in scenarios:
            decision = resolve_adaptive_rate_limit_decision(
                settings=settings,
                signals=signals,
                backpressure_decision=backpressure,
                requested_max_attempts=5,
            )
            observed_modes.append(decision.mode)
            assert decision.mode == expected

    assert observed_modes == expected_modes * 10

    low_stress = resolve_adaptive_rate_limit_decision(
        settings=settings,
        signals=scenarios[0][0],
        backpressure_decision=backpressure,
        requested_max_attempts=5,
    )
    high_stress = resolve_adaptive_rate_limit_decision(
        settings=settings,
        signals=scenarios[2][0],
        backpressure_decision=backpressure,
        requested_max_attempts=5,
    )
    assert high_stress.adjusted_queue_depth_threshold < low_stress.adjusted_queue_depth_threshold


def test_phase5_adaptive_limiter_chaos_failure_injection_recovers() -> None:
    provider = _AdaptiveSignalsProvider(
        pending_statuses=[QueueStatus.PENDING, QueueStatus.RETRY, QueueStatus.PENDING],
        dead_count=0,
        fail=True,
    )
    settings = _adaptive_settings(provider_failure_mode="sync_fallback")
    backpressure = _adaptive_backpressure(queue_depth=0, lag_seconds=0.0)

    failure_signals = sample_adaptive_ingress_signals(
        inbound_provider=provider,
        backpressure_decision=backpressure,
        sample_size=20,
    )
    failure_decision = resolve_adaptive_rate_limit_decision(
        settings=settings,
        signals=failure_signals,
        backpressure_decision=backpressure,
        requested_max_attempts=5,
    )

    provider.fail = False
    recovery_signals = sample_adaptive_ingress_signals(
        inbound_provider=provider,
        backpressure_decision=backpressure,
        sample_size=20,
    )
    recovery_decision = resolve_adaptive_rate_limit_decision(
        settings=settings,
        signals=recovery_signals,
        backpressure_decision=backpressure,
        requested_max_attempts=5,
    )

    assert failure_signals.provider_available is False
    assert failure_decision.mode == "sync_fallback"
    assert recovery_signals.provider_available is True
    assert recovery_signals.retry_rate == (1 / 3)
    assert recovery_signals.failure_rate == 0.0
    assert recovery_decision.mode == "allow"


def test_phase5_guardrail_transitions_honor_cooldown_and_hysteresis() -> None:
    engine = SLOGuardrailEngine(
        policy=SLOGuardrailPolicy(
            enabled=True,
            min_sample_size=1,
            cooldown_seconds=60,
            hysteresis_factor=0.8,
        )
    )
    start = datetime(2026, 1, 1, tzinfo=UTC)
    healthy = SLOGuardrailSignals(
        retry_rate=0.01,
        dead_letter_rate=0.0,
        queue_lag_seconds=10.0,
        failure_ratio=0.0,
        error_budget_remaining=0.95,
        sample_size=50,
    )

    degrade, state = engine.evaluate(
        signals=SLOGuardrailSignals(retry_rate=0.10, sample_size=50),
        now=start,
    )
    rollback, state = engine.evaluate(
        signals=SLOGuardrailSignals(
            retry_rate=0.25,
            dead_letter_rate=0.12,
            queue_lag_seconds=320.0,
            failure_ratio=0.20,
            error_budget_remaining=0.10,
            sample_size=50,
        ),
        state=state,
        now=start + timedelta(seconds=10),
    )
    rollback_cooldown_hold, state = engine.evaluate(
        signals=healthy,
        state=state,
        now=start + timedelta(seconds=20),
    )
    recovery_to_degrade, state = engine.evaluate(
        signals=healthy,
        state=state,
        now=start + timedelta(seconds=80),
    )
    degrade_cooldown_hold, state = engine.evaluate(
        signals=healthy,
        state=state,
        now=start + timedelta(seconds=90),
    )
    recovery_to_observe, _ = engine.evaluate(
        signals=healthy,
        state=state,
        now=start + timedelta(seconds=150),
    )

    assert degrade.action == SLOGuardrailAction.DEGRADE
    assert rollback.action == SLOGuardrailAction.ROLLBACK
    assert rollback_cooldown_hold.reason == "cooldown_hold"
    assert recovery_to_degrade.action == SLOGuardrailAction.DEGRADE
    assert recovery_to_degrade.reason == "recovery_hysteresis_clear"
    assert degrade_cooldown_hold.reason == "cooldown_hold"
    assert recovery_to_observe.action == SLOGuardrailAction.OBSERVE
    assert recovery_to_observe.reason == "recovery_hysteresis_clear"


@dataclass
class _ArchiveRecord:
    message_id: str
    status: str
    updated_at: str


class _ArchiveRepository:
    def __init__(self, records: list[_ArchiveRecord]) -> None:
        self._records = {record.message_id: record for record in records}
        self.archived: list[str] = []
        self.calls: list[dict[str, Any]] = []

    def archive_processed_records(
        self,
        *,
        status: str,
        older_than: str,
        limit: int,
        archived_by: str | None = None,
        archive_reason: str | None = None,
        conn: Any | None = None,
    ) -> int:
        _ = conn
        self.calls.append(
            {
                "status": status,
                "older_than": older_than,
                "limit": limit,
                "archived_by": archived_by,
                "archive_reason": archive_reason,
            }
        )
        cutoff = datetime.fromisoformat(older_than)
        candidates = [
            record
            for record in self._records.values()
            if record.status == status and datetime.fromisoformat(record.updated_at) <= cutoff
        ]
        candidates.sort(key=lambda item: (item.updated_at, item.message_id))
        selected = candidates[: max(1, int(limit))]
        for record in selected:
            self.archived.append(record.message_id)
            self._records.pop(record.message_id, None)
        return len(selected)

    @property
    def active_ids(self) -> set[str]:
        return set(self._records.keys())


def test_phase5_archival_retention_boundaries_preserve_replay_safety() -> None:
    now = datetime(2026, 1, 15, tzinfo=UTC)
    inbound = _ArchiveRepository(
        [
            _ArchiveRecord("in-sent-boundary", QueueStatus.SENT, (now - timedelta(days=7)).isoformat()),
            _ArchiveRecord("in-sent-fresh", QueueStatus.SENT, (now - timedelta(days=6, hours=23)).isoformat()),
            _ArchiveRecord("in-dead-boundary", QueueStatus.DEAD, (now - timedelta(days=14)).isoformat()),
            _ArchiveRecord("in-dead-replay-safe", QueueStatus.DEAD, (now - timedelta(days=13, hours=23)).isoformat()),
        ]
    )
    outbound = _ArchiveRepository(
        [
            _ArchiveRecord("out-dead-old", QueueStatus.DEAD, (now - timedelta(days=30)).isoformat()),
            _ArchiveRecord("out-dead-replay-safe", QueueStatus.DEAD, (now - timedelta(days=10)).isoformat()),
        ]
    )
    service = QueueArchivalService(
        inbound_repository=inbound,
        outbound_repository=outbound,
        policy=QueueArchivalRetentionPolicy(
            sent_ttl_seconds=7 * 24 * 60 * 60,
            dead_ttl_seconds=1 * 24 * 60 * 60,
            replay_window_seconds=14 * 24 * 60 * 60,
            audit_window_seconds=1 * 24 * 60 * 60,
            max_batch_size=20,
        ),
    )

    result = service.execute(
        QueueArchivalCommand(actor="phase5-bot", reason="phase5-boundary", batch_limit=20, requested_at=now.isoformat())
    )

    assert result.archived_total == 3
    assert set(inbound.archived) == {"in-sent-boundary", "in-dead-boundary"}
    assert set(outbound.archived) == {"out-dead-old"}
    assert "in-sent-fresh" in inbound.active_ids
    assert "in-dead-replay-safe" in inbound.active_ids
    assert "out-dead-replay-safe" in outbound.active_ids
    assert not result.exceptions

    dead_cutoff = (now - timedelta(days=14)).isoformat()
    dead_decisions = [decision for decision in result.decisions if decision.status == QueueStatus.DEAD]
    assert {decision.older_than for decision in dead_decisions} == {dead_cutoff}


class _IncidentQueueControls:
    def __init__(self) -> None:
        self.degrade_calls: list[dict[str, Any]] = []
        self.pause_calls: list[dict[str, Any]] = []

    def apply_degrade(
        self,
        *,
        feature: str,
        reason: str,
        duration_seconds: int,
        metadata: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        payload = {
            "feature": feature,
            "reason": reason,
            "duration_seconds": duration_seconds,
            "metadata": dict(metadata),
        }
        self.degrade_calls.append(payload)
        return payload

    def apply_pause(
        self,
        *,
        feature: str,
        reason: str,
        duration_seconds: int,
        metadata: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        payload = {
            "feature": feature,
            "reason": reason,
            "duration_seconds": duration_seconds,
            "metadata": dict(metadata),
        }
        self.pause_calls.append(payload)
        return payload


class _IncidentRecoveryActions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def suggest_recovery(
        self,
        *,
        feature: str,
        reason: str,
        batch_limit: int,
        dry_run: bool,
        metadata: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        payload = {
            "feature": feature,
            "reason": reason,
            "batch_limit": batch_limit,
            "dry_run": dry_run,
            "metadata": dict(metadata),
        }
        self.calls.append(payload)
        return payload


def _build_incident_hook(
    *,
    safety_policy: IncidentAutomationSafetyPolicy,
    action_policy: BoundedActionPolicy | None = None,
) -> tuple[GuardrailIncidentHook, _IncidentQueueControls, _IncidentRecoveryActions, InMemoryAlertNotifier]:
    queue_controls = _IncidentQueueControls()
    recovery_actions = _IncidentRecoveryActions()
    notifier = InMemoryAlertNotifier()
    hook = GuardrailIncidentHook(
        executor=BoundedActionExecutor(
            queue_controls=queue_controls,
            recovery_actions=recovery_actions,
            policy=action_policy or BoundedActionPolicy(max_replay_batch=3),
        ),
        notifier=notifier,
        safety_policy=safety_policy,
    )
    return hook, queue_controls, recovery_actions, notifier


def _rollback_decision(reason: str) -> SLOGuardrailDecision:
    return SLOGuardrailDecision(
        action=SLOGuardrailAction.ROLLBACK,
        reason=reason,
        triggered_signals=("retry_rate", "dead_letter_rate"),
    )


def _degrade_decision(reason: str) -> SLOGuardrailDecision:
    return SLOGuardrailDecision(
        action=SLOGuardrailAction.DEGRADE,
        reason=reason,
        triggered_signals=("retry_rate",),
    )


def test_phase5_incident_automation_bounded_actions_and_idempotency() -> None:
    hook, queue_controls, recovery_actions, notifier = _build_incident_hook(
        safety_policy=IncidentAutomationSafetyPolicy(
            cooldown_seconds=0,
            debounce_seconds=0,
            max_actions_per_interval=5,
            interval_seconds=300,
            duplicate_ttl_seconds=600,
        ),
        action_policy=BoundedActionPolicy(max_replay_batch=3, execute_replay_on_rollback=False),
    )
    start = datetime(2026, 1, 1, tzinfo=UTC)
    decision = _rollback_decision("rollback_threshold_breach")

    first = hook.handle_guardrail_decision(
        feature="worker_supervision",
        decision=decision,
        metadata={"replay_batch_limit": 999},
        now=start,
    )
    duplicate = hook.handle_guardrail_decision(
        feature="worker_supervision",
        decision=decision,
        metadata={"replay_batch_limit": 999},
        now=start + timedelta(seconds=1),
    )

    assert first.triggered is True
    assert first.execution is not None
    assert {record.name for record in first.execution.records} == {"queue_pause", "recovery_replay"}
    replay_record = next(record for record in first.execution.records if record.name == "recovery_replay")
    assert replay_record.details["batch_limit"] == 3
    assert replay_record.details["dry_run"] is True

    assert duplicate.suppressed is True
    assert duplicate.suppression_reason == "duplicate_incident"
    assert len(queue_controls.pause_calls) == 1
    assert len(recovery_actions.calls) == 1
    assert len(notifier.alerts) == 1


def test_phase5_incident_automation_debounce_and_interval_limits_are_deterministic() -> None:
    hook, queue_controls, _, notifier = _build_incident_hook(
        safety_policy=IncidentAutomationSafetyPolicy(
            cooldown_seconds=0,
            debounce_seconds=5,
            max_actions_per_interval=2,
            interval_seconds=60,
            duplicate_ttl_seconds=0,
        ),
    )
    start = datetime(2026, 1, 1, tzinfo=UTC)

    first = hook.handle_guardrail_decision(
        feature="operator_recovery",
        decision=_degrade_decision("degrade_a"),
        now=start,
    )
    debounced = hook.handle_guardrail_decision(
        feature="operator_recovery",
        decision=_degrade_decision("degrade_b"),
        now=start + timedelta(seconds=3),
    )
    second = hook.handle_guardrail_decision(
        feature="operator_recovery",
        decision=_degrade_decision("degrade_c"),
        now=start + timedelta(seconds=6),
    )
    interval_bounded = hook.handle_guardrail_decision(
        feature="operator_recovery",
        decision=_degrade_decision("degrade_d"),
        now=start + timedelta(seconds=40),
    )
    third = hook.handle_guardrail_decision(
        feature="operator_recovery",
        decision=_degrade_decision("degrade_e"),
        now=start + timedelta(seconds=70),
    )

    assert first.triggered is True
    assert debounced.suppressed is True
    assert debounced.suppression_reason == "debounced"
    assert second.triggered is True
    assert interval_bounded.suppressed is True
    assert interval_bounded.suppression_reason == "max_actions_interval"
    assert third.triggered is True
    assert len(queue_controls.degrade_calls) == 3
    assert len(notifier.alerts) == 3


@dataclass(frozen=True)
class _QueueMetadata:
    enqueued_at: str


@dataclass(frozen=True)
class _QueueRecord:
    message_id: str
    status: str
    metadata: _QueueMetadata
    created_at: str


class _QueueProvider:
    def __init__(self, *, pending: list[_QueueRecord] | None = None, dead: list[_QueueRecord] | None = None) -> None:
        self._pending = list(pending or [])
        self._dead = list(dead or [])

    def list_pending(self, *, limit: int = 100, conn: Any | None = None):
        _ = conn
        return list(self._pending)[: max(1, int(limit))]

    def list_dead(self, *, limit: int = 100, conn: Any | None = None):
        _ = conn
        return list(self._dead)[: max(1, int(limit))]


class _LeaseStore:
    def list_stale_claims(self, *, queue_name: str, limit: int = 100, conn: Any | None = None):
        _ = (queue_name, limit, conn)
        return []


def _build_readiness_service(
    *,
    inbound: _QueueProvider,
    outbound: _QueueProvider,
    settings: dict[str, Any],
) -> ProductionReadinessReportService:
    def _get_setting(key: str, default: Any = None) -> Any:
        return settings.get(key, default)

    return ProductionReadinessReportService(
        inbound_provider=inbound,
        outbound_provider=outbound,
        lease_store=_LeaseStore(),
        setting_getter=_get_setting,
        now_provider=lambda: datetime(2026, 2, 1, 0, 10, tzinfo=UTC),
        sample_window=10,
        stale_claim_window=4,
    )


def _guardrail_snapshot(report: dict[str, Any], feature: str) -> dict[str, Any]:
    return next(item for item in report["guardrails"] if item["feature"] == feature)


def _queue_snapshot(report: dict[str, Any], queue_name: str) -> dict[str, Any]:
    return next(item for item in report["queues"] if item["queue_name"] == queue_name)


def test_phase5_production_readiness_report_is_consistent_for_healthy_and_degraded_states() -> None:
    base_settings = {
        "refactor_worker_supervision_enabled": True,
        "refactor_worker_supervision_canary_percent": 100,
        "refactor_operator_recovery_enabled": True,
        "refactor_operator_recovery_canary_percent": 100,
        "refactor_webhook_ingress_enabled": True,
        "refactor_webhook_ingress_canary_percent": 100,
        "refactor_worker_supervision_slo_guardrail_enabled": True,
        "refactor_worker_supervision_slo_guardrail_min_sample_size": 1,
        "refactor_worker_supervision_sample_size": 50,
    }
    healthy_service = _build_readiness_service(
        inbound=_QueueProvider(
            pending=[
                _QueueRecord(
                    message_id="healthy-in-1",
                    status=QueueStatus.PENDING,
                    metadata=_QueueMetadata(enqueued_at="2026-02-01T00:09:00+00:00"),
                    created_at="2026-02-01T00:09:00+00:00",
                )
            ]
        ),
        outbound=_QueueProvider(
            pending=[
                _QueueRecord(
                    message_id="healthy-out-1",
                    status=QueueStatus.PENDING,
                    metadata=_QueueMetadata(enqueued_at="2026-02-01T00:09:30+00:00"),
                    created_at="2026-02-01T00:09:30+00:00",
                )
            ]
        ),
        settings={**base_settings, "refactor_worker_supervision_retry_rate": 0.01},
    )
    degraded_service = _build_readiness_service(
        inbound=_QueueProvider(
            pending=[
                _QueueRecord(
                    message_id="degraded-in-1",
                    status=QueueStatus.RETRY,
                    metadata=_QueueMetadata(enqueued_at="2026-02-01T00:08:00+00:00"),
                    created_at="2026-02-01T00:08:00+00:00",
                ),
                _QueueRecord(
                    message_id="degraded-in-2",
                    status=QueueStatus.PENDING,
                    metadata=_QueueMetadata(enqueued_at="2026-02-01T00:09:00+00:00"),
                    created_at="2026-02-01T00:09:00+00:00",
                ),
            ],
            dead=[
                _QueueRecord(
                    message_id="degraded-in-dead-1",
                    status=QueueStatus.DEAD,
                    metadata=_QueueMetadata(enqueued_at="2026-02-01T00:04:00+00:00"),
                    created_at="2026-02-01T00:04:00+00:00",
                )
            ],
        ),
        outbound=_QueueProvider(
            pending=[
                _QueueRecord(
                    message_id="degraded-out-1",
                    status=QueueStatus.PENDING,
                    metadata=_QueueMetadata(enqueued_at="2026-02-01T00:09:30+00:00"),
                    created_at="2026-02-01T00:09:30+00:00",
                )
            ]
        ),
        settings={**base_settings, "refactor_worker_supervision_retry_rate": 0.12},
    )

    healthy = healthy_service.build_scrubbed_report()
    degraded = degraded_service.build_scrubbed_report()

    assert healthy["schema_version"] == degraded["schema_version"] == "production-readiness.v1"
    assert healthy["generated_at"] == degraded["generated_at"] == "2026-02-01T00:10:00+00:00"
    assert set(healthy.keys()) == set(degraded.keys())
    assert healthy["overall_status"] == "healthy"
    assert degraded["overall_status"] == "degraded"

    assert _queue_snapshot(healthy, "refactor_inbound")["status"] == "healthy"
    assert _queue_snapshot(degraded, "refactor_inbound")["status"] == "degraded"
    assert _guardrail_snapshot(healthy, "worker_supervision")["decision_action"] == "observe"
    assert _guardrail_snapshot(degraded, "worker_supervision")["decision_action"] == "degrade"
