from __future__ import annotations

from typing import Any

from app.ingress.rollout_controls import WebhookIngressRolloutDecision
from app.ingress.webhook_controller import process_webhook_ingress_with_rollout


class _IngressMetricsProbe:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def record_ingress_path(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


def _decision(*, use_refactor_runtime: bool, reason: str = "test") -> WebhookIngressRolloutDecision:
    return WebhookIngressRolloutDecision(
        use_refactor_runtime=use_refactor_runtime,
        reason=reason,
        canary_percent=100 if use_refactor_runtime else 0,
        canary_bucket=7,
        enabled=True,
        emergency_rollback=False,
    )


def test_ingress_metrics_capture_refactor_quick_ack_path() -> None:
    probe = _IngressMetricsProbe()

    result = process_webhook_ingress_with_rollout(
        request_id="req-1",
        request_obj=None,
        legacy_processor=lambda _request_id: ({"status": "legacy"}, 200),
        refactor_processor=lambda _request_id: ({"status": "accepted"}, 202),
        decision_resolver=lambda _phone: _decision(use_refactor_runtime=True, reason="canary_selected"),
        operations_metrics=probe,  # type: ignore[arg-type]
    )

    assert result[1] == 202
    assert len(probe.calls) == 1
    assert probe.calls[0]["runtime_path"] == "refactor"
    assert probe.calls[0]["processing_mode"] == "quick_ack"


def test_ingress_metrics_capture_legacy_sync_path() -> None:
    probe = _IngressMetricsProbe()

    result = process_webhook_ingress_with_rollout(
        request_id="req-2",
        request_obj=None,
        legacy_processor=lambda _request_id: ({"status": "ok"}, 200),
        refactor_processor=lambda _request_id: ({"status": "accepted"}, 202),
        decision_resolver=lambda _phone: _decision(use_refactor_runtime=False, reason="rollout_disabled"),
        operations_metrics=probe,  # type: ignore[arg-type]
    )

    assert result[1] == 200
    assert len(probe.calls) == 1
    assert probe.calls[0]["runtime_path"] == "legacy"
    assert probe.calls[0]["processing_mode"] == "sync_path"


def test_ingress_metrics_capture_refactor_failure_fallback() -> None:
    probe = _IngressMetricsProbe()

    def _raise(_request_id: str):
        raise RuntimeError("boom")

    result = process_webhook_ingress_with_rollout(
        request_id="req-3",
        request_obj=None,
        legacy_processor=lambda _request_id: ({"status": "legacy-fallback"}, 200),
        refactor_processor=_raise,
        decision_resolver=lambda _phone: _decision(use_refactor_runtime=True, reason="canary_full"),
        operations_metrics=probe,  # type: ignore[arg-type]
    )

    assert result[1] == 200
    assert len(probe.calls) == 1
    assert probe.calls[0]["runtime_path"] == "legacy_fallback"
    assert probe.calls[0]["processing_mode"] == "sync_path"
