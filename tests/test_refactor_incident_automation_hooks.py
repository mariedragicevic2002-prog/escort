from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from typing import Any, Mapping

from app.guardrails import (
    SLOGuardrailAction,
    SLOGuardrailDecision,
    SLOGuardrailPolicy,
    SLOGuardrailSignals,
)
from app.incidents.contracts import GuardrailIncidentEvent
from app.incidents.executor import BoundedActionExecutor, BoundedActionPolicy
from app.incidents.hooks import GuardrailIncidentHook, IncidentAutomationSafetyPolicy
from app.incidents.notifier import InMemoryAlertNotifier
from app.ingress.rollout_controls import (
    Phase4FeatureRolloutSettings,
    resolve_phase4_feature_rollout_decision,
)


class _StubQueueControls:
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


class _StubRecoveryActions:
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
            "authorization": "Bearer should-not-leak",
        }
        self.calls.append(payload)
        return payload


def _policy() -> IncidentAutomationSafetyPolicy:
    return IncidentAutomationSafetyPolicy(
        cooldown_seconds=0,
        debounce_seconds=0,
        max_actions_per_interval=5,
        interval_seconds=300,
        duplicate_ttl_seconds=600,
    )


def _build_hook(
    *,
    action_policy: BoundedActionPolicy | None = None,
    notifier: InMemoryAlertNotifier | None = None,
) -> tuple[GuardrailIncidentHook, _StubQueueControls, _StubRecoveryActions, InMemoryAlertNotifier]:
    queue_controls = _StubQueueControls()
    recovery_actions = _StubRecoveryActions()
    alert_notifier = notifier or InMemoryAlertNotifier()
    hook = GuardrailIncidentHook(
        executor=BoundedActionExecutor(
            queue_controls=queue_controls,
            recovery_actions=recovery_actions,
            policy=action_policy or BoundedActionPolicy(max_replay_batch=3),
        ),
        notifier=alert_notifier,
        safety_policy=_policy(),
    )
    return hook, queue_controls, recovery_actions, alert_notifier


def test_incident_hook_is_triggered_from_phase4_guardrail_event() -> None:
    hook, queue_controls, _, notifier = _build_hook()
    decision = resolve_phase4_feature_rollout_decision(
        settings=Phase4FeatureRolloutSettings(
            feature="operator_recovery",
            enabled=True,
            canary_percent=100,
            emergency_rollback=False,
        ),
        context_key="ops-incident-1",
        bucket_resolver=lambda _seed: 0,
        guardrail_policy=SLOGuardrailPolicy(enabled=True, min_sample_size=1),
        guardrail_signals=SLOGuardrailSignals(retry_rate=0.12, sample_size=40),
        incident_hook=hook,
        incident_metadata={"api_key": "top-secret"},
    )

    assert decision.guardrail_action == SLOGuardrailAction.DEGRADE.value
    assert len(queue_controls.degrade_calls) == 1
    assert len(queue_controls.pause_calls) == 0
    assert len(notifier.alerts) == 1


def test_bounded_action_executor_enforces_replay_batch_limits() -> None:
    _, queue_controls, recovery_actions, _ = _build_hook(
        action_policy=BoundedActionPolicy(
            degrade_pause_seconds=60,
            rollback_pause_seconds=120,
            max_replay_batch=2,
            execute_replay_on_rollback=False,
        )
    )
    executor = BoundedActionExecutor(
        queue_controls=queue_controls,
        recovery_actions=recovery_actions,
        policy=BoundedActionPolicy(max_replay_batch=2, execute_replay_on_rollback=False),
    )
    event = GuardrailIncidentEvent.from_guardrail_decision(
        feature="worker_supervision",
        decision=SLOGuardrailDecision(
            action=SLOGuardrailAction.ROLLBACK,
            reason="rollback_threshold_breach",
            triggered_signals=("retry_rate", "dead_letter_rate"),
        ),
        metadata={"replay_batch_limit": 999},
    )

    result = executor.execute(event)

    assert result.executed is True
    assert len(queue_controls.pause_calls) == 1
    assert len(recovery_actions.calls) == 1
    assert recovery_actions.calls[0]["batch_limit"] == 2
    assert recovery_actions.calls[0]["dry_run"] is True


def test_duplicate_incidents_are_suppressed_idempotently() -> None:
    hook, queue_controls, _, notifier = _build_hook()
    decision = SLOGuardrailDecision(
        action=SLOGuardrailAction.DEGRADE,
        reason="degrade_threshold_breach",
        triggered_signals=("retry_rate",),
    )
    start = datetime(2026, 1, 1, tzinfo=UTC)

    first = hook.handle_guardrail_decision(
        feature="operator_recovery",
        decision=decision,
        metadata={"queue_name": "refactor_inbound"},
        now=start,
    )
    duplicate = hook.handle_guardrail_decision(
        feature="operator_recovery",
        decision=decision,
        metadata={"queue_name": "refactor_inbound"},
        now=start + timedelta(seconds=1),
    )

    assert first.triggered is True
    assert duplicate.suppressed is True
    assert duplicate.suppression_reason == "duplicate_incident"
    assert len(queue_controls.degrade_calls) == 1
    assert len(notifier.alerts) == 1


def test_alert_payload_is_scrubbed_for_audit_safety() -> None:
    hook, _, _, notifier = _build_hook()
    response = hook.handle_guardrail_decision(
        feature="operator_recovery",
        decision=SLOGuardrailDecision(
            action=SLOGuardrailAction.ROLLBACK,
            reason="rollback_threshold_breach",
            triggered_signals=("retry_rate",),
        ),
        metadata={
            "secret_token": "abc123",
            "nested": {"api_key": "dont-leak"},
            "details": "ok",
        },
    )

    encoded = json.dumps(notifier.alerts[0].payload, sort_keys=True)

    assert response.triggered is True
    assert "abc123" not in encoded
    assert "dont-leak" not in encoded
    assert "Bearer should-not-leak" not in encoded
    assert "[REDACTED]" in encoded
