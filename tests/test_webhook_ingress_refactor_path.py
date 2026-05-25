from __future__ import annotations

from flask import Flask, jsonify
import pytest

import main_v2.runtime as runtime
from main_v2 import webhook_main_flow as wf
from refactor.app.ingress import webhook_pipeline as wp
from refactor.app.ingress.quick_ack import QuickAckEnqueueOutcome
from refactor.app.ingress.rollout_controls import (
    WebhookIngressQuickAckSettings,
    WebhookIngressRolloutDecision,
)
from refactor.app.ingress.webhook_security import WebhookSecurityOutcome


def _decision(
    *,
    use_refactor_runtime: bool,
    reason: str,
    canary_percent: int = 100,
    canary_bucket: int = 0,
    enabled: bool = True,
    emergency_rollback: bool = False,
) -> WebhookIngressRolloutDecision:
    return WebhookIngressRolloutDecision(
        use_refactor_runtime=use_refactor_runtime,
        reason=reason,
        canary_percent=canary_percent,
        canary_bucket=canary_bucket,
        enabled=enabled,
        emergency_rollback=emergency_rollback,
    )


def _contract(status: str, request_id: str) -> dict[str, object]:
    return {
        "status": status,
        "messages_sent": 1,
        "messages_failed": 0,
        "request_id": request_id,
    }


def test_refactor_webhook_path_selected_when_enabled(monkeypatch) -> None:
    app = Flask(__name__)
    calls = {"legacy": 0, "refactor": 0}

    monkeypatch.setattr(
        wf,
        "_resolve_webhook_ingress_rollout_decision",
        lambda _phone: _decision(use_refactor_runtime=True, reason="canary_full"),
    )
    monkeypatch.setattr(wf, "_record_webhook_ingress_rollout_metrics", lambda **_kwargs: None)

    def _legacy(_request_id: str):
        calls["legacy"] += 1
        return jsonify(_contract("legacy", "legacy-id")), 200

    def _refactor(_request_id: str):
        calls["refactor"] += 1
        return jsonify(_contract("success", "refactor-id")), 200

    monkeypatch.setattr(wf, "_process_webhook_legacy", _legacy)
    monkeypatch.setattr(wf, "_process_webhook_refactor", _refactor)

    with app.test_request_context(
        "/webhook",
        method="POST",
        json={"event": "message.received", "data": {"contact": "+61412345678", "content": "hello"}},
    ):
        response, status = wf._process_webhook("req-1")

    assert status == 200
    assert response.get_json()["status"] == "success"
    assert calls == {"legacy": 0, "refactor": 1}


@pytest.mark.parametrize(
    ("decision", "expected_reason"),
    [
        (_decision(use_refactor_runtime=False, reason="rollout_disabled", enabled=False), "legacy-disabled"),
        (
            _decision(
                use_refactor_runtime=False,
                reason="emergency_rollback",
                emergency_rollback=True,
            ),
            "legacy-rollback",
        ),
    ],
)
def test_legacy_fallback_when_disabled_or_rollback(monkeypatch, decision, expected_reason) -> None:
    app = Flask(__name__)
    calls = {"legacy": 0, "refactor": 0}

    monkeypatch.setattr(wf, "_resolve_webhook_ingress_rollout_decision", lambda _phone: decision)
    monkeypatch.setattr(wf, "_record_webhook_ingress_rollout_metrics", lambda **_kwargs: None)

    def _legacy(_request_id: str):
        calls["legacy"] += 1
        return jsonify(_contract(expected_reason, "legacy-id")), 200

    def _refactor(_request_id: str):
        calls["refactor"] += 1
        return jsonify(_contract("refactor", "refactor-id")), 200

    monkeypatch.setattr(wf, "_process_webhook_legacy", _legacy)
    monkeypatch.setattr(wf, "_process_webhook_refactor", _refactor)

    with app.test_request_context("/webhook", method="POST", json={"data": {"contact": "+61412345678"}}):
        response, status = wf._process_webhook("req-2")

    assert status == 200
    assert response.get_json()["status"] == expected_reason
    assert calls == {"legacy": 1, "refactor": 0}


def test_legacy_fallback_when_refactor_pipeline_errors(monkeypatch) -> None:
    app = Flask(__name__)
    calls = {"legacy": 0}

    monkeypatch.setattr(
        wf,
        "_resolve_webhook_ingress_rollout_decision",
        lambda _phone: _decision(use_refactor_runtime=True, reason="canary_full"),
    )
    monkeypatch.setattr(wf, "_record_webhook_ingress_rollout_metrics", lambda **_kwargs: None)

    def _legacy(_request_id: str):
        calls["legacy"] += 1
        return jsonify(_contract("legacy-error-fallback", "legacy-id")), 200

    def _raise(_request_id: str):
        raise RuntimeError("boom")

    monkeypatch.setattr(wf, "_process_webhook_legacy", _legacy)
    monkeypatch.setattr(wf, "_process_webhook_refactor", _raise)

    with app.test_request_context("/webhook", method="POST", json={"data": {"contact": "+61412345678"}}):
        response, status = wf._process_webhook("req-3")

    assert status == 200
    assert response.get_json()["status"] == "legacy-error-fallback"
    assert calls["legacy"] == 1


def test_response_contract_unchanged_for_refactor_and_legacy_paths(monkeypatch) -> None:
    app = Flask(__name__)
    monkeypatch.setattr(wf, "_record_webhook_ingress_rollout_metrics", lambda **_kwargs: None)
    monkeypatch.setattr(
        wf,
        "_process_webhook_refactor",
        lambda _request_id: (jsonify(_contract("success", "refactor-id")), 200),
    )
    monkeypatch.setattr(
        wf,
        "_process_webhook_legacy",
        lambda _request_id: (jsonify(_contract("legacy", "legacy-id")), 200),
    )

    monkeypatch.setattr(
        wf,
        "_resolve_webhook_ingress_rollout_decision",
        lambda _phone: _decision(use_refactor_runtime=True, reason="canary_full"),
    )
    with app.test_request_context("/webhook", method="POST", json={"data": {"contact": "+61412345678"}}):
        refactor_response, refactor_status = wf._process_webhook("req-refactor")
    refactor_payload = refactor_response.get_json()

    monkeypatch.setattr(
        wf,
        "_resolve_webhook_ingress_rollout_decision",
        lambda _phone: _decision(
            use_refactor_runtime=False,
            reason="emergency_rollback",
            emergency_rollback=True,
        ),
    )
    with app.test_request_context("/webhook", method="POST", json={"data": {"contact": "+61412345678"}}):
        legacy_response, legacy_status = wf._process_webhook("req-legacy")
    legacy_payload = legacy_response.get_json()

    expected_keys = {"status", "messages_sent", "messages_failed", "request_id"}
    assert refactor_status == 200
    assert legacy_status == 200
    assert set(refactor_payload.keys()) == expected_keys
    assert set(legacy_payload.keys()) == expected_keys


def test_webhook_quick_ack_enabled_enqueues_and_returns_early(monkeypatch) -> None:
    app = Flask(__name__)
    calls = {"legacy": 0}
    monkeypatch.setattr(
        wf,
        "_resolve_webhook_ingress_rollout_decision",
        lambda _phone: _decision(use_refactor_runtime=True, reason="canary_full"),
    )
    monkeypatch.setattr(wf, "_record_webhook_ingress_rollout_metrics", lambda **_kwargs: None)
    monkeypatch.setattr(
        wf,
        "_process_webhook_legacy",
        lambda _request_id: calls.__setitem__("legacy", calls["legacy"] + 1)
        or (jsonify(_contract("legacy", "legacy-id")), 200),
    )
    monkeypatch.setattr(
        wp,
        "load_webhook_ingress_quick_ack_settings",
        lambda: WebhookIngressQuickAckSettings(enabled=True, max_attempts=5),
    )
    monkeypatch.setattr(
        wp,
        "enforce_webhook_ingress_security",
        lambda **_kwargs: WebhookSecurityOutcome(
            scrubbed_payload={},
            duplicate=False,
            dedup_key="dedup-quick-ack",
            dedup_key_missing=False,
            auth_reason="webhook_secret_match",
            signature_verified=False,
        ),
    )
    monkeypatch.setattr(
        wp,
        "try_enqueue_webhook_quick_ack",
        lambda **_kwargs: QuickAckEnqueueOutcome(accepted=True, duplicate=False, reason="enqueued"),
    )
    monkeypatch.setattr(runtime, "db_service", object())

    with app.test_request_context(
        "/webhook",
        method="POST",
        json={"event": "message.received", "data": {"contact": "+61412345678", "content": "hello"}},
    ):
        response, status = wf._process_webhook("req-quick-ack")
    payload = response.get_json()

    assert status == 202
    assert calls["legacy"] == 0
    assert payload == {
        "status": "accepted",
        "messages_sent": 0,
        "messages_failed": 0,
        "request_id": "req-quick-ack",
    }


def test_webhook_quick_ack_enqueue_failure_falls_back_to_sync(monkeypatch) -> None:
    app = Flask(__name__)
    monkeypatch.setattr(
        wf,
        "_resolve_webhook_ingress_rollout_decision",
        lambda _phone: _decision(use_refactor_runtime=True, reason="canary_full"),
    )
    monkeypatch.setattr(wf, "_record_webhook_ingress_rollout_metrics", lambda **_kwargs: None)
    monkeypatch.setattr(
        wf,
        "_process_webhook_legacy",
        lambda _request_id: (jsonify(_contract("legacy-fallback", "legacy-id")), 200),
    )
    monkeypatch.setattr(
        wp,
        "load_webhook_ingress_quick_ack_settings",
        lambda: WebhookIngressQuickAckSettings(enabled=True, max_attempts=5),
    )
    monkeypatch.setattr(
        wp,
        "enforce_webhook_ingress_security",
        lambda **_kwargs: WebhookSecurityOutcome(
            scrubbed_payload={},
            duplicate=False,
            dedup_key="dedup-quick-ack",
            dedup_key_missing=False,
            auth_reason="webhook_secret_match",
            signature_verified=False,
        ),
    )
    monkeypatch.setattr(
        wp,
        "try_enqueue_webhook_quick_ack",
        lambda **_kwargs: QuickAckEnqueueOutcome(accepted=False, duplicate=False, reason="enqueue_failed"),
    )
    monkeypatch.setattr(runtime, "db_service", object())

    with app.test_request_context(
        "/webhook",
        method="POST",
        json={"event": "message.received", "data": {"contact": "+61412345678", "content": "hello"}},
    ):
        response, status = wf._process_webhook("req-fallback")
    payload = response.get_json()

    assert status == 200
    assert payload["status"] == "legacy-fallback"


def test_webhook_quick_ack_backpressure_rejects_with_stable_schema(monkeypatch) -> None:
    app = Flask(__name__)
    monkeypatch.setattr(
        wf,
        "_resolve_webhook_ingress_rollout_decision",
        lambda _phone: _decision(use_refactor_runtime=True, reason="canary_full"),
    )
    monkeypatch.setattr(wf, "_record_webhook_ingress_rollout_metrics", lambda **_kwargs: None)
    monkeypatch.setattr(
        wf,
        "_process_webhook_legacy",
        lambda _request_id: (_ for _ in ()).throw(AssertionError("legacy sync path must not execute")),
    )
    monkeypatch.setattr(
        wp,
        "load_webhook_ingress_quick_ack_settings",
        lambda: WebhookIngressQuickAckSettings(enabled=True, max_attempts=5),
    )
    monkeypatch.setattr(
        wp,
        "enforce_webhook_ingress_security",
        lambda **_kwargs: WebhookSecurityOutcome(
            scrubbed_payload={},
            duplicate=False,
            dedup_key="dedup-quick-ack",
            dedup_key_missing=False,
            auth_reason="webhook_secret_match",
            signature_verified=False,
        ),
    )
    monkeypatch.setattr(
        wp,
        "try_enqueue_webhook_quick_ack",
        lambda **_kwargs: QuickAckEnqueueOutcome(accepted=False, duplicate=False, reason="backpressure_reject"),
    )
    monkeypatch.setattr(runtime, "db_service", object())

    with app.test_request_context(
        "/webhook",
        method="POST",
        json={"event": "message.received", "data": {"contact": "+61412345678", "content": "hello"}},
    ):
        response, status = wf._process_webhook("req-backpressure-reject")
    payload = response.get_json()

    assert status == 503
    assert payload["status"] == "rejected"
    assert set(payload.keys()) == {"status", "messages_sent", "messages_failed", "request_id"}


def test_webhook_quick_ack_disabled_uses_sync_path(monkeypatch) -> None:
    app = Flask(__name__)
    legacy_calls = {"count": 0}
    monkeypatch.setattr(
        wf,
        "_resolve_webhook_ingress_rollout_decision",
        lambda _phone: _decision(use_refactor_runtime=True, reason="canary_full"),
    )
    monkeypatch.setattr(wf, "_record_webhook_ingress_rollout_metrics", lambda **_kwargs: None)
    monkeypatch.setattr(
        wf,
        "_process_webhook_legacy",
        lambda _request_id: legacy_calls.__setitem__("count", legacy_calls["count"] + 1)
        or (jsonify(_contract("legacy-disabled", "legacy-id")), 200),
    )
    monkeypatch.setattr(
        wp,
        "load_webhook_ingress_quick_ack_settings",
        lambda: WebhookIngressQuickAckSettings(enabled=False, max_attempts=5),
    )
    monkeypatch.setattr(
        wp,
        "enforce_webhook_ingress_security",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("security should not run when disabled")),
    )

    with app.test_request_context(
        "/webhook",
        method="POST",
        json={"event": "message.received", "data": {"contact": "+61412345678", "content": "hello"}},
    ):
        response, status = wf._process_webhook("req-disabled")
    payload = response.get_json()

    assert status == 200
    assert payload["status"] == "legacy-disabled"
    assert legacy_calls["count"] == 1


def test_webhook_quick_ack_response_payload_shape_matches_sync_contract(monkeypatch) -> None:
    app = Flask(__name__)
    monkeypatch.setattr(
        wf,
        "_resolve_webhook_ingress_rollout_decision",
        lambda _phone: _decision(use_refactor_runtime=True, reason="canary_full"),
    )
    monkeypatch.setattr(wf, "_record_webhook_ingress_rollout_metrics", lambda **_kwargs: None)
    monkeypatch.setattr(
        wf,
        "_process_webhook_legacy",
        lambda _request_id: (jsonify(_contract("legacy", "legacy-id")), 200),
    )
    monkeypatch.setattr(
        wp,
        "load_webhook_ingress_quick_ack_settings",
        lambda: WebhookIngressQuickAckSettings(enabled=True, max_attempts=5),
    )
    monkeypatch.setattr(
        wp,
        "enforce_webhook_ingress_security",
        lambda **_kwargs: WebhookSecurityOutcome(
            scrubbed_payload={},
            duplicate=False,
            dedup_key="dedup-quick-ack",
            dedup_key_missing=False,
            auth_reason="webhook_secret_match",
            signature_verified=False,
        ),
    )
    monkeypatch.setattr(
        wp,
        "try_enqueue_webhook_quick_ack",
        lambda **_kwargs: QuickAckEnqueueOutcome(accepted=True, duplicate=False, reason="enqueued"),
    )
    monkeypatch.setattr(runtime, "db_service", object())

    with app.test_request_context(
        "/webhook",
        method="POST",
        json={"event": "message.received", "data": {"contact": "+61412345678", "content": "hello"}},
    ):
        quick_ack_response, quick_ack_status = wf._process_webhook("req-quick")
    quick_ack_payload = quick_ack_response.get_json()

    monkeypatch.setattr(
        wp,
        "try_enqueue_webhook_quick_ack",
        lambda **_kwargs: QuickAckEnqueueOutcome(accepted=False, duplicate=False, reason="disabled"),
    )
    with app.test_request_context(
        "/webhook",
        method="POST",
        json={"event": "message.received", "data": {"contact": "+61412345678", "content": "hello"}},
    ):
        sync_response, sync_status = wf._process_webhook("req-sync")
    sync_payload = sync_response.get_json()

    assert quick_ack_status == 202
    assert sync_status == 200
    assert set(quick_ack_payload.keys()) == set(sync_payload.keys()) == {
        "status",
        "messages_sent",
        "messages_failed",
        "request_id",
    }
