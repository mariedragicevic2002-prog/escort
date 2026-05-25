from __future__ import annotations

from datetime import UTC, datetime, timedelta

from refactor.app.guardrails import (
    SLOGuardrailAction,
    SLOGuardrailEngine,
    SLOGuardrailPolicy,
    SLOGuardrailSignals,
)
from refactor.app.ingress.rollout_controls import (
    Phase4FeatureRolloutSettings,
    resolve_phase4_feature_rollout_decision,
)


def _policy() -> SLOGuardrailPolicy:
    return SLOGuardrailPolicy(
        enabled=True,
        min_sample_size=1,
        cooldown_seconds=120,
        hysteresis_factor=0.8,
    )


def test_sla_guardrail_thresholds_select_degrade_then_rollback() -> None:
    engine = SLOGuardrailEngine(policy=_policy())

    degrade, _ = engine.evaluate(
        signals=SLOGuardrailSignals(
            retry_rate=0.10,
            dead_letter_rate=0.02,
            queue_lag_seconds=60,
            failure_ratio=0.04,
            error_budget_remaining=0.8,
            sample_size=50,
        )
    )
    rollback, _ = engine.evaluate(
        signals=SLOGuardrailSignals(
            retry_rate=0.22,
            dead_letter_rate=0.11,
            queue_lag_seconds=300,
            failure_ratio=0.20,
            error_budget_remaining=0.10,
            sample_size=50,
        )
    )

    assert degrade.action == SLOGuardrailAction.DEGRADE
    assert degrade.transitioned is True
    assert "retry_rate" in degrade.triggered_signals
    assert rollback.action == SLOGuardrailAction.ROLLBACK
    assert rollback.transitioned is True
    assert "dead_letter_rate" in rollback.triggered_signals


def test_sla_guardrail_hysteresis_and_cooldown_prevent_flapping() -> None:
    engine = SLOGuardrailEngine(policy=_policy())
    start = datetime(2026, 1, 1, tzinfo=UTC)

    first, state = engine.evaluate(
        signals=SLOGuardrailSignals(retry_rate=0.10, sample_size=25),
        now=start,
    )
    in_cooldown, state = engine.evaluate(
        signals=SLOGuardrailSignals(retry_rate=0.01, sample_size=25),
        state=state,
        now=start + timedelta(seconds=30),
    )
    hysteresis_hold, state = engine.evaluate(
        signals=SLOGuardrailSignals(retry_rate=0.07, sample_size=25),
        state=state,
        now=start + timedelta(seconds=130),
    )
    recovered, _ = engine.evaluate(
        signals=SLOGuardrailSignals(
            retry_rate=0.03,
            dead_letter_rate=0.01,
            queue_lag_seconds=20,
            failure_ratio=0.01,
            error_budget_remaining=0.9,
            sample_size=25,
        ),
        state=state,
        now=start + timedelta(seconds=140),
    )

    assert first.action == SLOGuardrailAction.DEGRADE
    assert in_cooldown.action == SLOGuardrailAction.DEGRADE
    assert in_cooldown.reason == "cooldown_hold"
    assert hysteresis_hold.action == SLOGuardrailAction.DEGRADE
    assert hysteresis_hold.reason == "hysteresis_hold"
    assert recovered.action == SLOGuardrailAction.OBSERVE
    assert recovered.transitioned is True


def test_sla_guardrail_rollback_precedence_overrides_degrade_cooldown() -> None:
    engine = SLOGuardrailEngine(policy=_policy())
    start = datetime(2026, 1, 1, tzinfo=UTC)

    _, state = engine.evaluate(
        signals=SLOGuardrailSignals(retry_rate=0.1, sample_size=50),
        now=start,
    )
    rollback, _ = engine.evaluate(
        signals=SLOGuardrailSignals(
            retry_rate=0.20,
            dead_letter_rate=0.10,
            queue_lag_seconds=280,
            sample_size=50,
        ),
        state=state,
        now=start + timedelta(seconds=10),
    )

    assert rollback.previous_action == SLOGuardrailAction.DEGRADE
    assert rollback.action == SLOGuardrailAction.ROLLBACK
    assert rollback.reason == "rollback_threshold_breach"


def test_phase4_rollout_decision_applies_guardrail_actions_with_rollback_precedence() -> None:
    settings = Phase4FeatureRolloutSettings(
        feature="worker_supervision",
        enabled=True,
        canary_percent=100,
        emergency_rollback=False,
    )
    degrade_decision = resolve_phase4_feature_rollout_decision(
        settings=settings,
        context_key="msg-1",
        bucket_resolver=lambda _seed: 0,
        guardrail_policy=_policy(),
        guardrail_signals=SLOGuardrailSignals(retry_rate=0.1, sample_size=50),
    )

    manual_rollback_settings = Phase4FeatureRolloutSettings(
        feature="worker_supervision",
        enabled=True,
        canary_percent=100,
        emergency_rollback=True,
    )
    manual_rollback = resolve_phase4_feature_rollout_decision(
        settings=manual_rollback_settings,
        context_key="msg-1",
        bucket_resolver=lambda _seed: 0,
        guardrail_policy=_policy(),
        guardrail_signals=SLOGuardrailSignals(retry_rate=0.1, sample_size=50),
    )

    assert degrade_decision.use_feature is True
    assert degrade_decision.degrade_activated is True
    assert degrade_decision.guardrail_action == "degrade"
    assert degrade_decision.reason == "slo_guardrail_degrade"

    assert manual_rollback.use_feature is False
    assert manual_rollback.reason == "emergency_rollback"
    assert manual_rollback.rollback_activated is True
    assert manual_rollback.guardrail_action == "rollback"
