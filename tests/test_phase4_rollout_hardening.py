from __future__ import annotations

import logging
from typing import Any

from app.ingress.backpressure_policy import (
    IngressBackpressureDecision,
    emit_ingress_backpressure_metric,
    load_ingress_backpressure_settings,
    resolve_ingress_backpressure_decision,
)
from app.ingress.rollout_controls import (
    Phase4FeatureRolloutDecision,
    load_backpressure_rollout_settings,
    load_operator_recovery_settings,
    resolve_backpressure_rollout_decision,
    resolve_worker_supervision_rollout_decision,
)
from app.workers.supervision.runtime import WorkerSupervisionRuntime


class _StubInboundProvider:
    def list_pending(self, *, limit: int = 100, conn: Any | None = None) -> list[Any]:
        _ = (limit, conn)
        return []


def test_phase4_backpressure_rollout_canary_is_bucket_driven() -> None:
    selected = resolve_backpressure_rollout_decision(
        channel="sms",
        context_key="req-phase4-1",
        env={
            "REFACTOR_SMS_INGRESS_BACKPRESSURE_ENABLED": "true",
            "REFACTOR_SMS_INGRESS_BACKPRESSURE_CANARY_PERCENT": "40",
        },
        bucket_resolver=lambda _seed: 39,
    )
    excluded = resolve_backpressure_rollout_decision(
        channel="sms",
        context_key="req-phase4-1",
        env={
            "REFACTOR_SMS_INGRESS_BACKPRESSURE_ENABLED": "true",
            "REFACTOR_SMS_INGRESS_BACKPRESSURE_CANARY_PERCENT": "40",
        },
        bucket_resolver=lambda _seed: 40,
    )

    assert selected.use_feature is True
    assert selected.reason == "canary_selected"
    assert excluded.use_feature is False
    assert excluded.reason == "canary_excluded"


def test_phase4_worker_supervision_rollback_overrides_enablement() -> None:
    decision = resolve_worker_supervision_rollout_decision(
        "msg-sensitive",
        env={
            "REFACTOR_WORKER_SUPERVISION_ENABLED": "1",
            "REFACTOR_WORKER_SUPERVISION_CANARY_PERCENT": "100",
            "REFACTOR_WORKER_SUPERVISION_EMERGENCY_ROLLBACK": "true",
        },
        bucket_resolver=lambda _seed: 0,
    )

    assert decision.use_feature is False
    assert decision.reason == "emergency_rollback"
    assert decision.rollback_activated is True


def test_phase4_settings_parsing_is_deterministic_and_backward_compatible() -> None:
    settings_map = {
        "refactor_ingress_backpressure_enabled": "0",
        "refactor_ingress_backpressure_canary_percent": "25",
    }

    def _settings_getter(key: str, default: Any = None) -> Any:
        return settings_map.get(key, default)

    rollout = load_backpressure_rollout_settings(channel="sms", setting_getter=_settings_getter)
    operator = load_operator_recovery_settings(
        env={
            "REFACTOR_OPERATOR_RECOVERY_ENABLED": "yes",
            "REFACTOR_OPERATOR_RECOVERY_MAX_PAUSE_SECONDS": "999999",
            "REFACTOR_OPERATOR_RECOVERY_MAX_REPLAY_BATCH_SIZE": "0",
        }
    )

    assert rollout.enabled is False
    assert rollout.canary_percent == 25
    assert operator.enabled is True
    assert operator.max_pause_seconds == 86400
    assert operator.max_replay_batch_size == 1
    assert operator.canary_percent == 100
    assert operator.emergency_rollback is False


def test_phase4_guardrail_metrics_and_logs_avoid_sensitive_values(caplog) -> None:
    metrics: list[tuple[str, dict[str, Any]]] = []

    def _metric_logger(metric_name: str, **kwargs: Any) -> None:
        metrics.append((metric_name, kwargs))

    fallback_rollout = Phase4FeatureRolloutDecision(
        feature="sms_backpressure",
        use_feature=False,
        reason="emergency_rollback",
        enabled=True,
        canary_percent=100,
        canary_bucket=0,
        emergency_rollback=True,
        rollout_exposed=False,
        rollback_activated=True,
        safeguard_fallback=True,
    )
    admission = resolve_ingress_backpressure_decision(
        settings=load_ingress_backpressure_settings(channel="sms", env={}),
        inbound_provider=_StubInboundProvider(),
        requested_max_attempts=5,
        rollout_decision=fallback_rollout,
    )
    emit_ingress_backpressure_metric(
        decision=admission,
        channel="sms",
        request_id="req-phase4-metric",
        metric_logger=_metric_logger,
    )

    supervisor = WorkerSupervisionRuntime(
        queue_name="refactor_outbox",
        rollout_decider=lambda _item_id: fallback_rollout,
        guardrail_metric_logger=_metric_logger,
    )
    with caplog.at_level(logging.WARNING, logger="adella_chatbot.refactor.worker_supervision"):
        claimed = supervisor.claim_item("secret-message-id-123")

    metric_payload = metrics[-1][1]
    assert claimed is True
    assert metric_payload["rollout_exposed"] is False
    assert metric_payload["rollback_activated"] is True
    assert metric_payload["safeguard_fallback"] is True
    assert "secret-message-id-123" not in caplog.text
    assert "item_hash=" in caplog.text
    assert isinstance(admission, IngressBackpressureDecision)
    assert admission.reason == "backpressure_rollout_emergency_rollback_sync_fallback"
