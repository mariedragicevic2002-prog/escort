from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Lock

from flask import Flask, jsonify, request
import pytest

import main_v2.runtime as runtime
import main_v2.sms_gateway as gateway
from main_v2 import webhook_main_flow as wf
from refactor.app.ingress.rollout_controls import SMSRolloutDecision, WebhookIngressRolloutDecision
from refactor.app.ingress.webhook_security import enforce_webhook_ingress_security


def _sms_decision(
    *,
    use_refactor_runtime: bool,
    reason: str,
    enabled: bool = True,
    emergency_rollback: bool = False,
) -> SMSRolloutDecision:
    return SMSRolloutDecision(
        use_refactor_runtime=use_refactor_runtime,
        reason=reason,
        canary_percent=100,
        canary_bucket=0,
        shadow_mode=False,
        enabled=enabled,
        emergency_rollback=emergency_rollback,
    )


def _webhook_decision(
    *,
    use_refactor_runtime: bool,
    reason: str,
    enabled: bool = True,
    emergency_rollback: bool = False,
) -> WebhookIngressRolloutDecision:
    return WebhookIngressRolloutDecision(
        use_refactor_runtime=use_refactor_runtime,
        reason=reason,
        canary_percent=100,
        canary_bucket=0,
        enabled=enabled,
        emergency_rollback=emergency_rollback,
    )


def _make_sms_client(monkeypatch):
    app = Flask(__name__)
    app.register_blueprint(gateway.sms_gateway_bp)
    monkeypatch.setattr(gateway, "_check_gateway_auth", lambda: True)
    monkeypatch.setattr(gateway, "_send_reply", lambda _phone, _message: True)
    monkeypatch.setattr(gateway, "_record_sms_rollout_metrics", lambda **_kwargs: None)
    return app, app.test_client()


class _AtomicDedupDB:
    def __init__(self) -> None:
        self._claimed: set[str] = set()
        self._lock = Lock()
        self.claim_attempts: list[str] = []

    def execute_query(self, query, params=(), fetch=None, conn=None, **_kwargs):
        _ = (fetch, conn)
        sql = " ".join(str(query).split()).lower()
        if "insert into httpsms_message_dedup" not in sql:
            return []
        dedup_key = str(params[0])
        with self._lock:
            self.claim_attempts.append(dedup_key)
            if dedup_key in self._claimed:
                return []
            self._claimed.add(dedup_key)
        return [{"message_id": dedup_key}]


class _StateManagerStub:
    def __init__(self, db) -> None:
        self.db = db

    def get_state(self, phone_number: str, conn=None):
        _ = (phone_number, conn)
        return None


def _invoke_webhook(*, app: Flask, payload: dict, request_id: str = "req-phase2"):
    with app.test_request_context("/webhook", method="POST", json=payload):
        response, status = wf._process_webhook(request_id)
    return response, status


@pytest.mark.parametrize(
    ("sms_rollout", "webhook_rollout", "expected"),
    [
        (
            _sms_decision(use_refactor_runtime=True, reason="canary_full"),
            _webhook_decision(use_refactor_runtime=True, reason="canary_full"),
            {"sms_refactor": 1, "sms_legacy": 0, "webhook_refactor": 1, "webhook_legacy": 0},
        ),
        (
            _sms_decision(
                use_refactor_runtime=False,
                reason="emergency_rollback",
                emergency_rollback=True,
            ),
            _webhook_decision(
                use_refactor_runtime=False,
                reason="emergency_rollback",
                emergency_rollback=True,
            ),
            {"sms_refactor": 0, "sms_legacy": 1, "webhook_refactor": 0, "webhook_legacy": 1},
        ),
        (
            _sms_decision(
                use_refactor_runtime=False,
                reason="rollout_disabled",
                enabled=False,
            ),
            _webhook_decision(
                use_refactor_runtime=False,
                reason="rollout_disabled",
                enabled=False,
            ),
            {"sms_refactor": 1, "sms_legacy": 0, "webhook_refactor": 0, "webhook_legacy": 1},
        ),
    ],
)
def test_dual_ingress_rollout_matrix_is_deterministic(monkeypatch, sms_rollout, webhook_rollout, expected) -> None:
    app, sms_client = _make_sms_client(monkeypatch)
    _ = app
    webhook_app = Flask(__name__)

    sms_calls = {"legacy": 0, "refactor": 0}
    webhook_calls = {"legacy": 0, "refactor": 0}

    monkeypatch.setattr(gateway, "_resolve_sms_rollout_decision", lambda _phone: sms_rollout)
    monkeypatch.setattr(wf, "_resolve_webhook_ingress_rollout_decision", lambda _phone: webhook_rollout)
    monkeypatch.setattr(wf, "_record_webhook_ingress_rollout_metrics", lambda **_kwargs: None)

    def _sms_legacy(_phone: str, _body: str) -> list[str]:
        sms_calls["legacy"] += 1
        return ["sms-legacy"]

    def _sms_refactor(**_kwargs):
        sms_calls["refactor"] += 1
        return ["sms-refactor"], False

    def _webhook_legacy(_request_id: str):
        webhook_calls["legacy"] += 1
        return jsonify({"status": "legacy", "messages_sent": 1, "messages_failed": 0, "request_id": "legacy"}), 200

    def _webhook_refactor(_request_id: str):
        webhook_calls["refactor"] += 1
        return jsonify({"status": "success", "messages_sent": 1, "messages_failed": 0, "request_id": "refactor"}), 200

    monkeypatch.setattr(gateway, "_process_sms_message", _sms_legacy)
    monkeypatch.setattr(gateway, "_process_sms_message_refactor", _sms_refactor)
    monkeypatch.setattr(wf, "_process_webhook_legacy", _webhook_legacy)
    monkeypatch.setattr(wf, "_process_webhook_refactor", _webhook_refactor)

    sms_response = sms_client.post(
        "/sms/incoming",
        json={"from": "+61412345678", "body": "hello", "message_id": "sms-rollout-1"},
    )
    webhook_response, webhook_status = _invoke_webhook(
        app=webhook_app,
        payload={"event": "message.received", "data": {"contact": "+61412345678", "content": "hello"}},
    )

    assert sms_response.status_code == 200
    assert webhook_status == 200
    assert sms_calls["refactor"] == expected["sms_refactor"]
    assert sms_calls["legacy"] == expected["sms_legacy"]
    assert webhook_calls["refactor"] == expected["webhook_refactor"]
    assert webhook_calls["legacy"] == expected["webhook_legacy"]


def test_dual_ingress_replay_is_deduped_for_refactor_enabled_paths(monkeypatch) -> None:
    _, sms_client = _make_sms_client(monkeypatch)
    webhook_app = Flask(__name__)
    sms_db = _AtomicDedupDB()
    webhook_db = _AtomicDedupDB()

    monkeypatch.setattr(
        gateway,
        "_resolve_sms_rollout_decision",
        lambda _phone: _sms_decision(use_refactor_runtime=True, reason="canary_full"),
    )
    monkeypatch.setattr(
        wf,
        "_resolve_webhook_ingress_rollout_decision",
        lambda _phone: _webhook_decision(use_refactor_runtime=True, reason="canary_full"),
    )
    monkeypatch.setattr(wf, "_record_webhook_ingress_rollout_metrics", lambda **_kwargs: None)
    monkeypatch.setattr(runtime, "db_service", sms_db)
    monkeypatch.setattr(runtime, "state_manager", _StateManagerStub(sms_db))

    sms_processed = {"count": 0}
    webhook_processed = {"count": 0}

    def _sms_legacy(_phone: str, body: str) -> list[str]:
        sms_processed["count"] += 1
        return [f"sms:{body}"]

    def _webhook_legacy(_request_id: str):
        raise AssertionError("webhook legacy path should not run during refactor replay test")

    def _webhook_refactor(request_id: str):
        payload = request.get_json(silent=True) or {}
        _data = payload.get("data")
        msg_data: dict = _data if isinstance(_data, dict) else {}
        contact = str(msg_data.get("contact") or "")
        body = str(msg_data.get("content") or "")
        security = enforce_webhook_ingress_security(
            headers=dict(request.headers.items()),
            raw_body=request.get_data(cache=True, as_text=False) or b"",
            payload=payload,
            message_data=msg_data,
            phone_number=contact,
            message_body=body,
            db_service=webhook_db,
            webhook_secrets=[],
        )
        if not security.duplicate:
            webhook_processed["count"] += 1
        return jsonify(
            {
                "status": "duplicate" if security.duplicate else "success",
                "messages_sent": 0 if security.duplicate else 1,
                "messages_failed": 0,
                "request_id": request_id,
            }
        ), 200

    monkeypatch.setattr(gateway, "_process_sms_message", _sms_legacy)
    monkeypatch.setattr(wf, "_process_webhook_legacy", _webhook_legacy)
    monkeypatch.setattr(wf, "_process_webhook_refactor", _webhook_refactor)

    sms_payload = {"from": "+61412345678", "body": "hello", "message_id": "sms-replay-1"}
    sms_first = sms_client.post("/sms/incoming", json=sms_payload)
    sms_second = sms_client.post("/sms/incoming", json=sms_payload)
    sms_first_json = sms_first.get_json()
    sms_second_json = sms_second.get_json()

    webhook_payload = {
        "event": "message.received",
        "data": {"contact": "+61412345678", "content": "hello", "message_id": "wh-replay-1"},
    }
    wh_first, wh_first_status = _invoke_webhook(app=webhook_app, payload=webhook_payload, request_id="wh-first")
    wh_second, wh_second_status = _invoke_webhook(app=webhook_app, payload=webhook_payload, request_id="wh-second")
    wh_first_json = wh_first.get_json()
    wh_second_json = wh_second.get_json()

    assert sms_first.status_code == 200
    assert sms_second.status_code == 200
    assert sms_first_json["replies"] == ["sms:hello"]
    assert sms_second_json["replies"] == []
    assert sms_processed["count"] == 1
    assert sms_db.claim_attempts == ["sms-replay-1", "sms-replay-1"]

    assert wh_first_status == 200
    assert wh_second_status == 200
    assert wh_first_json["status"] == "success"
    assert wh_second_json["status"] == "duplicate"
    assert wh_second_json["messages_sent"] == 0
    assert webhook_processed["count"] == 1
    assert webhook_db.claim_attempts == ["wh-replay-1", "wh-replay-1"]


def test_dual_ingress_concurrency_processes_each_replayed_payload_once(monkeypatch) -> None:
    sms_app, _ = _make_sms_client(monkeypatch)
    webhook_app = Flask(__name__)
    sms_db = _AtomicDedupDB()
    webhook_db = _AtomicDedupDB()

    monkeypatch.setattr(
        gateway,
        "_resolve_sms_rollout_decision",
        lambda _phone: _sms_decision(use_refactor_runtime=True, reason="canary_full"),
    )
    monkeypatch.setattr(
        wf,
        "_resolve_webhook_ingress_rollout_decision",
        lambda _phone: _webhook_decision(use_refactor_runtime=True, reason="canary_full"),
    )
    monkeypatch.setattr(wf, "_record_webhook_ingress_rollout_metrics", lambda **_kwargs: None)
    monkeypatch.setattr(runtime, "db_service", sms_db)
    monkeypatch.setattr(runtime, "state_manager", _StateManagerStub(sms_db))

    sms_processed = {"count": 0}
    sms_lock = Lock()
    webhook_processed = {"count": 0}
    webhook_lock = Lock()

    def _sms_legacy(_phone: str, _body: str) -> list[str]:
        with sms_lock:
            sms_processed["count"] += 1
        return ["sms:ok"]

    def _webhook_legacy(_request_id: str):
        raise AssertionError("webhook legacy path should not run during refactor concurrency test")

    def _webhook_refactor(request_id: str):
        payload = request.get_json(silent=True) or {}
        _data = payload.get("data")
        msg_data: dict = _data if isinstance(_data, dict) else {}
        security = enforce_webhook_ingress_security(
            headers=dict(request.headers.items()),
            raw_body=request.get_data(cache=True, as_text=False) or b"",
            payload=payload,
            message_data=msg_data,
            phone_number=str(msg_data.get("contact") or ""),
            message_body=str(msg_data.get("content") or ""),
            db_service=webhook_db,
            webhook_secrets=[],
        )
        if not security.duplicate:
            with webhook_lock:
                webhook_processed["count"] += 1
        return jsonify(
            {
                "status": "duplicate" if security.duplicate else "success",
                "messages_sent": 0 if security.duplicate else 1,
                "messages_failed": 0,
                "request_id": request_id,
            }
        ), 200

    monkeypatch.setattr(gateway, "_process_sms_message", _sms_legacy)
    monkeypatch.setattr(wf, "_process_webhook_legacy", _webhook_legacy)
    monkeypatch.setattr(wf, "_process_webhook_refactor", _webhook_refactor)

    def _sms_worker(_: int) -> dict:
        with sms_app.test_client() as client:
            response = client.post(
                "/sms/incoming",
                json={"from": "+61412345678", "body": "parallel", "message_id": "sms-concurrency-1"},
            )
            return response.get_json()

    def _webhook_worker(_: int) -> dict:
        response, _status = _invoke_webhook(
            app=webhook_app,
            request_id="wh-concurrency",
            payload={
                "event": "message.received",
                "data": {"contact": "+61412345678", "content": "parallel", "message_id": "wh-concurrency-1"},
            },
        )
        return response.get_json()

    with ThreadPoolExecutor(max_workers=8) as pool:
        sms_results = list(pool.map(_sms_worker, range(8)))
    with ThreadPoolExecutor(max_workers=8) as pool:
        webhook_results = list(pool.map(_webhook_worker, range(8)))

    assert sum(1 for result in sms_results if result["messages_sent"] == 1) == 1
    assert sum(1 for result in sms_results if result["messages_sent"] == 0) == 7
    assert sms_processed["count"] == 1

    assert sum(1 for result in webhook_results if result["status"] == "success") == 1
    assert sum(1 for result in webhook_results if result["status"] == "duplicate") == 7
    assert webhook_processed["count"] == 1
