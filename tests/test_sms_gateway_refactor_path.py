from __future__ import annotations

from flask import Flask

import main_v2.sms_gateway as gateway
import main_v2.runtime as runtime
from refactor.app.ingress.quick_ack import QuickAckEnqueueOutcome
from refactor.app.outbound.contracts import OutboundDispatchResult
from refactor.app.ingress.rollout_controls import SMSRolloutDecision
from refactor.app.middleware.idempotency import RetryableInboundError


def _decision(
    *,
    use_refactor_runtime: bool,
    reason: str = "test",
    canary_percent: int = 100,
    canary_bucket: int = 0,
    shadow_mode: bool = False,
    enabled: bool = True,
    emergency_rollback: bool = False,
) -> SMSRolloutDecision:
    return SMSRolloutDecision(
        use_refactor_runtime=use_refactor_runtime,
        reason=reason,
        canary_percent=canary_percent,
        canary_bucket=canary_bucket,
        shadow_mode=shadow_mode,
        enabled=enabled,
        emergency_rollback=emergency_rollback,
    )


def _make_client(monkeypatch):
    app = Flask(__name__)
    app.register_blueprint(gateway.sms_gateway_bp)
    monkeypatch.setattr(gateway, "_check_gateway_auth", lambda: True)
    monkeypatch.setattr(gateway, "_send_reply", lambda _phone, _message: True)
    monkeypatch.setattr(gateway, "_record_sms_rollout_metrics", lambda **_kwargs: None)
    return app.test_client()


def test_refactor_pipeline_path_processes_sms(monkeypatch):
    client = _make_client(monkeypatch)
    monkeypatch.setattr(
        gateway,
        "_resolve_sms_rollout_decision",
        lambda _phone: _decision(use_refactor_runtime=True, reason="canary_full"),
    )
    monkeypatch.setattr(
        gateway,
        "_process_sms_message_refactor",
        lambda **_kwargs: (["refactor-reply"], False),
    )

    response = client.post("/sms/incoming", json={"from": "+61412345678", "body": "hello"})
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert payload["replies"] == ["refactor-reply"]
    assert payload["messages_sent"] == 1


def test_refactor_pipeline_retryable_error_returns_503(monkeypatch):
    client = _make_client(monkeypatch)
    monkeypatch.setattr(
        gateway,
        "_resolve_sms_rollout_decision",
        lambda _phone: _decision(use_refactor_runtime=True, reason="canary_full"),
    )

    def _raise_retryable(**_kwargs):
        raise RetryableInboundError("storage unavailable")

    monkeypatch.setattr(gateway, "_process_sms_message_refactor", _raise_retryable)

    response = client.post("/sms/incoming", json={"from": "+61412345678", "body": "hello"})
    payload = response.get_json()

    assert response.status_code == 503
    assert payload["status"] == "error"


def test_refactor_pipeline_falls_back_to_legacy_on_non_retryable_error(monkeypatch):
    client = _make_client(monkeypatch)
    monkeypatch.setattr(
        gateway,
        "_resolve_sms_rollout_decision",
        lambda _phone: _decision(use_refactor_runtime=True, reason="canary_full"),
    )

    def _raise_boom(**_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(gateway, "_process_sms_message_refactor", _raise_boom)
    monkeypatch.setattr(gateway, "_process_sms_message", lambda _phone, _body: ["legacy-reply"])

    response = client.post("/sms/incoming", json={"from": "+61412345678", "body": "hello"})
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["replies"] == ["legacy-reply"]


class _DeterministicDedupDB:
    def __init__(self) -> None:
        self.claimed: set[str] = set()
        self.claim_attempts: list[str] = []

    def execute_query(self, query, params=(), fetch=None, conn=None, **_kwargs):
        _ = (fetch, conn)
        sql = " ".join(str(query).split()).lower()
        if "insert into httpsms_message_dedup" not in sql:
            return []
        message_id = str(params[0])
        self.claim_attempts.append(message_id)
        if message_id in self.claimed:
            return []
        self.claimed.add(message_id)
        return [{"message_id": message_id}]


class _StateManagerStub:
    def __init__(self, db) -> None:
        self.db = db

    def get_state(self, phone_number: str, conn=None):
        _ = (phone_number, conn)
        return None


def test_refactor_enabled_sms_incoming_uses_full_pipeline_and_dedupes_replay(monkeypatch):
    client = _make_client(monkeypatch)
    monkeypatch.setattr(
        gateway,
        "_resolve_sms_rollout_decision",
        lambda _phone: _decision(use_refactor_runtime=True, reason="canary_full"),
    )

    db = _DeterministicDedupDB()
    monkeypatch.setattr(runtime, "db_service", db)
    monkeypatch.setattr(runtime, "state_manager", _StateManagerStub(db))

    legacy_calls: list[tuple[str, str]] = []

    def _legacy(phone_number: str, message_body: str) -> list[str]:
        legacy_calls.append((phone_number, message_body))
        return [f"legacy:{message_body}"]

    monkeypatch.setattr(gateway, "_process_sms_message", _legacy)

    first = client.post(
        "/sms/incoming",
        json={"from": "+61412345678", "body": "hello", "message_id": "msg-001"},
    )
    second = client.post(
        "/sms/incoming",
        json={"from": "+61412345678", "body": "hello", "message_id": "msg-001"},
    )
    first_payload = first.get_json()
    second_payload = second.get_json()

    assert first.status_code == 200
    assert second.status_code == 200
    assert first_payload["replies"] == ["legacy:hello"]
    assert second_payload["replies"] == []
    assert second_payload["messages_sent"] == 0
    assert legacy_calls == [("+61412345678", "hello")]
    assert db.claim_attempts == ["msg-001", "msg-001"]


def test_emergency_rollback_forces_legacy_runtime(monkeypatch):
    client = _make_client(monkeypatch)
    monkeypatch.setattr(
        gateway,
        "_resolve_sms_rollout_decision",
        lambda _phone: _decision(
            use_refactor_runtime=False,
            reason="emergency_rollback",
            emergency_rollback=True,
        ),
    )
    refactor_calls: list[str] = []
    monkeypatch.setattr(
        gateway,
        "_process_sms_message_refactor",
        lambda **_kwargs: refactor_calls.append("called") or (["refactor"], False),
    )
    monkeypatch.setattr(gateway, "_process_sms_message", lambda _phone, _body: ["legacy-reply"])

    response = client.post("/sms/incoming", json={"from": "+61412345678", "body": "hello"})
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["replies"] == ["legacy-reply"]
    assert refactor_calls == []


def test_shadow_mode_keeps_refactor_path_authoritative(monkeypatch):
    client = _make_client(monkeypatch)
    monkeypatch.setattr(
        gateway,
        "_resolve_sms_rollout_decision",
        lambda _phone: _decision(
            use_refactor_runtime=False,
            reason="canary_excluded",
            canary_percent=10,
            canary_bucket=88,
            shadow_mode=True,
        ),
    )

    metrics_calls: list[SMSRolloutDecision] = []
    monkeypatch.setattr(
        gateway,
        "_record_sms_rollout_metrics",
        lambda **kwargs: metrics_calls.append(kwargs["decision"]),
    )

    refactor_calls: list[str] = []
    monkeypatch.setattr(
        gateway,
        "_process_sms_message_refactor",
        lambda **_kwargs: refactor_calls.append("called") or (["refactor"], False),
    )

    legacy_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gateway,
        "_process_sms_message",
        lambda phone, body: legacy_calls.append((phone, body)) or ["legacy-reply"],
    )

    response = client.post("/sms/incoming", json={"from": "+61412345678", "body": "hello"})
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["replies"] == ["refactor"]
    assert refactor_calls == ["called"]
    assert legacy_calls == []
    assert len(metrics_calls) == 1
    assert metrics_calls[0].shadow_mode is True


def test_rollout_disabled_still_prefers_refactor_runtime(monkeypatch):
    client = _make_client(monkeypatch)
    monkeypatch.setattr(
        gateway,
        "_resolve_sms_rollout_decision",
        lambda _phone: _decision(
            use_refactor_runtime=False,
            reason="rollout_disabled",
            enabled=False,
        ),
    )
    refactor_calls: list[str] = []
    monkeypatch.setattr(
        gateway,
        "_process_sms_message_refactor",
        lambda **_kwargs: refactor_calls.append("called") or (["refactor-reply"], False),
    )
    legacy_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gateway,
        "_process_sms_message",
        lambda phone, body: legacy_calls.append((phone, body)) or ["legacy-reply"],
    )

    response = client.post("/sms/incoming", json={"from": "+61412345678", "body": "hello"})
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["replies"] == ["refactor-reply"]
    assert refactor_calls == ["called"]
    assert legacy_calls == []


def test_response_contract_is_preserved_for_refactor_and_rollback_paths(monkeypatch):
    client = _make_client(monkeypatch)
    monkeypatch.setattr(
        gateway,
        "_process_sms_message_refactor",
        lambda **_kwargs: (["refactor-reply"], False),
    )
    monkeypatch.setattr(gateway, "_process_sms_message", lambda _phone, _body: ["legacy-reply"])

    monkeypatch.setattr(
        gateway,
        "_resolve_sms_rollout_decision",
        lambda _phone: _decision(use_refactor_runtime=True, reason="canary_full"),
    )
    refactor_response = client.post("/sms/incoming", json={"from": "+61412345678", "body": "hello"})
    refactor_payload = refactor_response.get_json()

    monkeypatch.setattr(
        gateway,
        "_resolve_sms_rollout_decision",
        lambda _phone: _decision(
            use_refactor_runtime=False,
            reason="emergency_rollback",
            emergency_rollback=True,
        ),
    )
    rollback_response = client.post("/sms/incoming", json={"from": "+61412345678", "body": "hello"})
    rollback_payload = rollback_response.get_json()

    expected_keys = {"status", "messages_sent", "messages_failed", "replies"}
    assert refactor_response.status_code == 200
    assert rollback_response.status_code == 200
    assert set(refactor_payload.keys()) == expected_keys
    assert set(rollback_payload.keys()) == expected_keys


def test_sms_quick_ack_enabled_enqueues_and_returns_early(monkeypatch):
    client = _make_client(monkeypatch)
    monkeypatch.setattr(
        gateway,
        "_resolve_sms_rollout_decision",
        lambda _phone: _decision(use_refactor_runtime=True, reason="canary_full"),
    )

    quick_ack_calls = {"count": 0}

    def _quick_ack(**_kwargs):
        quick_ack_calls["count"] += 1
        return QuickAckEnqueueOutcome(accepted=True, duplicate=False, reason="enqueued")

    monkeypatch.setattr(gateway, "_try_sms_quick_ack", _quick_ack)
    monkeypatch.setattr(
        gateway,
        "_process_sms_message_refactor",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("sync path must not execute")),
    )

    response = client.post("/sms/incoming", json={"from": "+61412345678", "body": "hello"})
    payload = response.get_json()

    assert response.status_code == 202
    assert payload == {
        "status": "accepted",
        "messages_sent": 0,
        "messages_failed": 0,
        "replies": [],
    }
    assert quick_ack_calls["count"] == 1


def test_sms_quick_ack_enqueue_failure_falls_back_to_sync(monkeypatch):
    client = _make_client(monkeypatch)
    monkeypatch.setattr(
        gateway,
        "_resolve_sms_rollout_decision",
        lambda _phone: _decision(use_refactor_runtime=True, reason="canary_full"),
    )
    monkeypatch.setattr(
        gateway,
        "_try_sms_quick_ack",
        lambda **_kwargs: QuickAckEnqueueOutcome(accepted=False, duplicate=False, reason="enqueue_failed"),
    )
    monkeypatch.setattr(
        gateway,
        "_process_sms_message_refactor",
        lambda **_kwargs: (["sync-reply"], False),
    )

    response = client.post("/sms/incoming", json={"from": "+61412345678", "body": "hello"})
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert payload["replies"] == ["sync-reply"]


def test_sms_quick_ack_backpressure_rejects_with_stable_schema(monkeypatch):
    client = _make_client(monkeypatch)
    monkeypatch.setattr(
        gateway,
        "_resolve_sms_rollout_decision",
        lambda _phone: _decision(use_refactor_runtime=True, reason="canary_full"),
    )
    monkeypatch.setattr(
        gateway,
        "_try_sms_quick_ack",
        lambda **_kwargs: QuickAckEnqueueOutcome(accepted=False, duplicate=False, reason="backpressure_reject"),
    )
    monkeypatch.setattr(
        gateway,
        "_process_sms_message_refactor",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("sync path must not execute")),
    )

    response = client.post("/sms/incoming", json={"from": "+61412345678", "body": "hello"})
    payload = response.get_json()

    assert response.status_code == 503
    assert payload["status"] == "rejected"
    assert set(payload.keys()) == {"status", "messages_sent", "messages_failed", "replies"}


def test_sms_quick_ack_disabled_uses_sync_path(monkeypatch):
    client = _make_client(monkeypatch)
    monkeypatch.setattr(
        gateway,
        "_resolve_sms_rollout_decision",
        lambda _phone: _decision(use_refactor_runtime=True, reason="canary_full"),
    )
    monkeypatch.setattr(
        gateway,
        "_try_sms_quick_ack",
        lambda **_kwargs: QuickAckEnqueueOutcome(accepted=False, duplicate=False, reason="disabled"),
    )
    monkeypatch.setattr(
        gateway,
        "_process_sms_message_refactor",
        lambda **_kwargs: (["sync-when-disabled"], False),
    )

    response = client.post("/sms/incoming", json={"from": "+61412345678", "body": "hello"})
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["replies"] == ["sync-when-disabled"]


def test_sms_quick_ack_response_payload_shape_matches_sync_contract(monkeypatch):
    client = _make_client(monkeypatch)
    monkeypatch.setattr(
        gateway,
        "_resolve_sms_rollout_decision",
        lambda _phone: _decision(use_refactor_runtime=True, reason="canary_full"),
    )
    monkeypatch.setattr(
        gateway,
        "_try_sms_quick_ack",
        lambda **_kwargs: QuickAckEnqueueOutcome(accepted=True, duplicate=False, reason="enqueued"),
    )
    quick_ack_response = client.post("/sms/incoming", json={"from": "+61412345678", "body": "hello"})
    quick_ack_payload = quick_ack_response.get_json()

    monkeypatch.setattr(
        gateway,
        "_try_sms_quick_ack",
        lambda **_kwargs: QuickAckEnqueueOutcome(accepted=False, duplicate=False, reason="disabled"),
    )
    monkeypatch.setattr(
        gateway,
        "_process_sms_message_refactor",
        lambda **_kwargs: (["sync-reply"], False),
    )
    sync_response = client.post("/sms/incoming", json={"from": "+61412345678", "body": "hello"})
    sync_payload = sync_response.get_json()

    assert quick_ack_response.status_code == 202
    assert sync_response.status_code == 200
    assert set(quick_ack_payload.keys()) == set(sync_payload.keys()) == {
        "status",
        "messages_sent",
        "messages_failed",
        "replies",
    }


def test_sms_ingress_uses_outbound_dispatcher_and_tracks_partial_failures(monkeypatch):
    client = _make_client(monkeypatch)
    monkeypatch.setattr(
        gateway,
        "_resolve_sms_rollout_decision",
        lambda _phone: _decision(use_refactor_runtime=True, reason="canary_full"),
    )
    monkeypatch.setattr(
        gateway,
        "_process_sms_message_refactor",
        lambda **_kwargs: (["first", "second"], False),
    )

    calls = {"count": 0}

    def _send(_phone: str, _body: str) -> bool:
        calls["count"] += 1
        return calls["count"] == 1

    monkeypatch.setattr(gateway, "_send_reply", _send)

    response = client.post("/sms/incoming", json={"from": "+61412345678", "body": "hello"})
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["messages_sent"] == 1
    assert payload["messages_failed"] == 1
    assert payload["replies"] == ["first", "second"]


def test_sms_ingress_dispatcher_receives_composed_messages(monkeypatch):
    client = _make_client(monkeypatch)
    monkeypatch.setattr(
        gateway,
        "_resolve_sms_rollout_decision",
        lambda _phone: _decision(use_refactor_runtime=True, reason="canary_full"),
    )
    monkeypatch.setattr(
        gateway,
        "_process_sms_message_refactor",
        lambda **_kwargs: (["reply"], [{"name": "handoff_to_webhook"}], False),
    )

    captured = {"bodies": []}

    class _StubDispatcher:
        def dispatch(self, messages):
            buffered = list(messages)
            captured["bodies"] = [message.body for message in buffered]
            return OutboundDispatchResult(attempted=len(buffered), sent=len(buffered), failed=0)

    monkeypatch.setattr(
        gateway,
        "_build_sms_outbound_dispatcher",
        lambda **_kwargs: _StubDispatcher(),
    )

    response = client.post("/sms/incoming", json={"from": "+61412345678", "body": "hello"})
    payload = response.get_json()

    expected_keys = {"status", "messages_sent", "messages_failed", "replies"}
    assert response.status_code == 200
    assert captured["bodies"] == ["reply"]
    assert set(payload.keys()) == expected_keys
