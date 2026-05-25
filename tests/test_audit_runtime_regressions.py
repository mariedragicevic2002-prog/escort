from __future__ import annotations

import hashlib
import hmac
import json
import time
import base64
from datetime import date

from flask import Flask

from booking import deposit_handler
from handlers.availability_check import handle_check_availability
from handlers.deposit_flow import _download_media_bytes_safe
from handlers.new_conv.booking import handle_book_appointment
from main_v2 import webhook_main_flow as wf
from tests.fakes import FakeDB, FakeStateManager
from tests.scenarios.utils import build_context, scenario_state_manager

PHONE = "+61400999111"


class _WebhookStateManager:
    def __init__(self, *, inbound_ok: bool = True):
        self._state: dict[str, dict] = {}
        self.inbound_ok = inbound_ok
        self.transitions: list[tuple[str, str]] = []

    def get_state(self, phone_number: str, conn=None):
        _ = conn
        state = self._state.get(phone_number)
        return dict(state) if state else None

    def create_state(self, phone_number: str, initial_state: str, conn=None):
        _ = conn
        self._state[phone_number] = {
            "phone_number": phone_number,
            "current_state": initial_state,
            "flow_version": "v1",
        }
        return True

    def clear_booking(self, phone_number: str):
        self._state.setdefault(phone_number, {"phone_number": phone_number, "flow_version": "v1"})
        self._state[phone_number]["current_state"] = "NEW"
        return True

    def is_blocked(self, _phone_number: str):
        return False

    def log_inbound_and_touch(self, _phone_number: str, _message_body: str, _media_urls=None, intent=None):
        _ = intent
        return self.inbound_ok

    def log_message(self, _phone_number, _direction, _message_body, media_urls=None, intent=None, conn=None):
        _ = (media_urls, intent, conn)
        return True

    def touch(self, _phone_number, conn=None):
        _ = conn
        return True

    def update_fields(self, phone_number: str, updates: dict, conn=None):
        _ = conn
        self._state.setdefault(phone_number, {"phone_number": phone_number, "current_state": "NEW", "flow_version": "v1"})
        self._state[phone_number].update(updates)
        return True

    def transition(self, phone_number: str, new_state: str, updates: dict | None = None, conn=None, force: bool = False):
        _ = (conn, force)
        self.transitions.append((phone_number, new_state))
        self._state.setdefault(phone_number, {"phone_number": phone_number, "flow_version": "v1"})
        self._state[phone_number]["current_state"] = new_state
        if updates:
            self._state[phone_number].update(updates)
        return True

    def get_booking_fields(self, _phone_number: str):
        return {}


class _Classifier:
    def classify(self, *_args, **_kwargs):
        return "greeting"


def _patch_webhook_baseline(monkeypatch, *, dispatch_result: dict, send_sms_result: bool):
    monkeypatch.setattr(wf.config, "get_httpsms_webhook_secrets", lambda: ["tok"])
    monkeypatch.setattr(wf.config, "get_httpsms_webhook_signature_secret", lambda: "")
    monkeypatch.setattr(wf.config, "httpsms_webhook_signature_required", lambda: False)
    monkeypatch.setattr(wf.config, "get_httpsms_webhook_signature_tolerance_seconds", lambda: 300)
    monkeypatch.setattr(wf, "is_screening_enabled", lambda: False)
    monkeypatch.setattr(wf, "_get_chatbot_rollout_percent", lambda: 100)
    monkeypatch.setattr(wf, "_check_frustration", lambda *_a, **_k: None)
    monkeypatch.setattr(wf, "_record_webhook_monitor", lambda **_kwargs: None)
    monkeypatch.setattr(wf, "send_sms", lambda *_a, **_k: send_sms_result)
    monkeypatch.setattr("services.httpsms_dedup.build_inbound_dedup_key", lambda *_a, **_k: "dedup-key")
    monkeypatch.setattr("services.httpsms_dedup.try_claim_httpsms_message_id", lambda *_a, **_k: True)
    monkeypatch.setattr(
        "core.enhanced_rate_limiter.get_rate_limiter",
        lambda: type("RateLimiter", (), {"check_rate_limit": lambda self, _p: (True, "")})(),
    )
    monkeypatch.setattr("handlers.safety.track_profanity_signal", lambda *_a, **_k: None)
    for _name in (
        "_is_photo_request",
        "_is_photo_followup_request",
        "_is_screenshot_link_request",
        "_is_webform_request",
        "_is_location_request",
        "_is_enquiry_keyword",
        "_is_enquiry_with_description",
        "_is_goodbye",
    ):
        monkeypatch.setattr(wf, _name, lambda *_a, **_k: False)
    monkeypatch.setattr(
        "main_v2.state_machine_bridge.dispatch_message",
        lambda **_kwargs: dict(dispatch_result),
    )


def _make_hs256_jwt(secret: str, claims: dict) -> str:
    def _encode_part(payload: dict) -> str:
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    header_b64 = _encode_part({"alg": "HS256", "typ": "JWT"})
    payload_b64 = _encode_part(claims)
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    signature = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    signature_b64 = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return f"{header_b64}.{payload_b64}.{signature_b64}"


def test_calculate_deposit_requirement_profanity_uses_outcall_amount(monkeypatch):
    sm = FakeStateManager(initial={PHONE: {"profanity_detected": True}})
    monkeypatch.setattr(deposit_handler, "_get_deposit_outcall", lambda: 180)
    monkeypatch.setattr(deposit_handler, "_get_deposit_incall", lambda: 90)
    monkeypatch.setattr("core.settings_manager.get_setting", lambda _k, default=None: "true")

    required, amount, reason = deposit_handler.calculate_deposit_requirement(
        {"incall_outcall": "incall", "duration": 60, "experience_type": "gfe"},
        PHONE,
        sm,
    )

    assert required is True
    assert amount == 180
    assert "profanity" in reason


def test_outcall_revalidation_exception_fails_closed(monkeypatch):
    sm = scenario_state_manager(
        PHONE,
        current_state="CHECKING_AVAILABILITY",
        date=date(2026, 6, 2),
        time=(14, 0),
        duration=60,
        incall_outcall="outcall",
        outcall_address="100 King William St, Adelaide SA 5000",
        experience_type="GFE",
        client_name="Alex",
        available_now_requested=False,
    )
    ctx = build_context(phone_number=PHONE, message="yes", state_manager=sm)
    monkeypatch.setattr(
        "booking.field_validator.FieldValidator.validate_outcall_address",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("validator crashed")),
    )

    result = handle_check_availability(ctx)
    state = sm.get_state(PHONE) or {}

    assert result["new_state"] == "COLLECTING"
    assert "couldn't verify that outcall address" in " ".join(result["messages"]).lower()
    assert state.get("outcall_address") is None


def test_new_booking_specific_time_exception_falls_back_to_first_contact(monkeypatch):
    sm = scenario_state_manager(PHONE, current_state="NEW")
    ctx = build_context(phone_number=PHONE, message="book me tonight at 330", state_manager=sm)
    monkeypatch.setattr(
        "utils.time_parser.parse_time_from_message",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(
        "handlers.new_conv.booking._new_booking_first_contact",
        lambda _ctx: {"messages": ["fallback"], "new_state": "COLLECTING", "actions": []},
    )

    result = handle_book_appointment(ctx)

    assert result["messages"] == ["fallback"]
    assert result["new_state"] == "COLLECTING"


def test_download_media_bytes_safe_rejects_oversized_payload(monkeypatch):
    class _Response:
        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size=65536):
            _ = chunk_size
            yield b"1234"
            yield b"5"

    monkeypatch.setenv("DEPOSIT_MEDIA_ALLOWED_HOSTS", "example.test")
    monkeypatch.setenv("DEPOSIT_MEDIA_MAX_BYTES", "4")
    monkeypatch.setattr("requests.get", lambda *_a, **_k: _Response())

    assert _download_media_bytes_safe("https://example.test/image.jpg", log_prefix="test") is None


def test_download_media_bytes_safe_allows_payload_within_cap(monkeypatch):
    class _Response:
        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size=65536):
            _ = chunk_size
            yield b"12"
            yield b"34"

    monkeypatch.setenv("DEPOSIT_MEDIA_ALLOWED_HOSTS", "example.test")
    monkeypatch.setenv("DEPOSIT_MEDIA_MAX_BYTES", "4")
    monkeypatch.setattr("requests.get", lambda *_a, **_k: _Response())

    assert _download_media_bytes_safe("https://example.test/image.jpg", log_prefix="test") == b"1234"


def test_webhook_delivery_failure_still_preserves_state_transition(monkeypatch):
    sm = _WebhookStateManager(inbound_ok=True)
    wf._runtime.state_manager = sm
    wf._runtime.db_service = FakeDB()
    wf._runtime.ai_service = object()
    wf._runtime.classifier = _Classifier()
    wf._runtime.router = object()
    _patch_webhook_baseline(
        monkeypatch,
        dispatch_result={"messages": ["reply"], "new_state": "COLLECTING", "actions": []},
        send_sms_result=False,
    )

    app = Flask(__name__)
    with app.test_request_context(
        "/webhook",
        method="POST",
        headers={"Authorization": "Bearer tok"},
        json={"event": "message.received", "data": {"contact": PHONE, "content": "hello"}},
    ):
        response, status = wf._process_webhook("req-1")

    body = response.get_json()
    assert status == 200
    assert body["status"] == "delivery_failed"
    assert sm.transitions == [(PHONE, "COLLECTING")]


def test_webhook_transition_failure_blocks_business_send(monkeypatch):
    sm = _WebhookStateManager(inbound_ok=True)
    wf._runtime.state_manager = sm
    wf._runtime.db_service = FakeDB()
    wf._runtime.ai_service = object()
    wf._runtime.classifier = _Classifier()
    wf._runtime.router = object()
    _patch_webhook_baseline(
        monkeypatch,
        dispatch_result={"messages": ["reply"], "new_state": "COLLECTING", "actions": []},
        send_sms_result=True,
    )

    send_calls: list[tuple] = []
    monkeypatch.setattr(wf, "send_sms", lambda *a, **k: (send_calls.append((a, k)) or True))
    monkeypatch.setattr(sm, "transition", lambda *_a, **_k: False)

    app = Flask(__name__)
    with app.test_request_context(
        "/webhook",
        method="POST",
        headers={"Authorization": "Bearer tok"},
        json={"event": "message.received", "data": {"contact": PHONE, "content": "hello"}},
    ):
        response, status = wf._process_webhook("req-transition-fail")

    body = response.get_json()
    assert status == 503
    assert body["status"] == "error"
    assert "State persistence failed" in body["message"]
    assert send_calls == []


def test_webhook_inbound_log_failure_returns_503(monkeypatch):
    sm = _WebhookStateManager(inbound_ok=False)
    wf._runtime.state_manager = sm
    wf._runtime.db_service = FakeDB()
    wf._runtime.ai_service = object()
    wf._runtime.classifier = _Classifier()
    wf._runtime.router = object()
    _patch_webhook_baseline(
        monkeypatch,
        dispatch_result={"messages": ["reply"], "new_state": "COLLECTING", "actions": []},
        send_sms_result=True,
    )

    app = Flask(__name__)
    with app.test_request_context(
        "/webhook",
        method="POST",
        headers={"Authorization": "Bearer tok"},
        json={"event": "message.received", "data": {"contact": PHONE, "content": "hello"}},
    ):
        response, status = wf._process_webhook("req-2")

    body = response.get_json()
    assert status == 503
    assert body["status"] == "error"
    assert sm.transitions == []


def test_incall_confirmation_token_claim_error_does_not_false_confirm(monkeypatch):
    sm = scenario_state_manager(
        PHONE,
        current_state="CHECKING_AVAILABILITY",
        date=date(2099, 1, 1),
        time=(14, 0),
        duration=60,
        incall_outcall="incall",
        experience_type="GFE",
        client_name="Alex",
        incall_awaiting_yes=True,
        deposit_required=False,
        booking_type="standard",
    )
    ctx = build_context(phone_number=PHONE, message="yes", state_manager=sm)

    monkeypatch.setattr(
        "handlers.availability_parts.availability_check_impl._acquire_booking_lock",
        lambda *_a, **_k: object(),
    )
    monkeypatch.setattr(
        "handlers.availability_parts.availability_check_impl._release_booking_lock",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr("services.calendar_service.check_conflict", lambda *_a, **_k: ("none", []))
    monkeypatch.setattr("services.calendar_service.create_calendar_event", lambda *_a, **_k: "evt_1")
    monkeypatch.setattr(
        "handlers.availability_parts.availability_check_impl._claim_confirmation_token_status",
        lambda *_a, **_k: "error",
    )

    result = handle_check_availability(ctx)

    assert result["new_state"] == "CHECKING_AVAILABILITY"
    assert "couldn't safely finalise" in " ".join(result["messages"]).lower()


def test_webhook_query_token_only_is_rejected(monkeypatch):
    sm = _WebhookStateManager(inbound_ok=True)
    wf._runtime.state_manager = sm
    wf._runtime.db_service = FakeDB()
    wf._runtime.ai_service = object()
    wf._runtime.classifier = _Classifier()
    wf._runtime.router = object()
    _patch_webhook_baseline(
        monkeypatch,
        dispatch_result={"messages": ["reply"], "new_state": "COLLECTING", "actions": []},
        send_sms_result=True,
    )

    app = Flask(__name__)
    with app.test_request_context(
        "/webhook?token=tok",
        method="POST",
        json={"event": "message.received", "data": {"contact": PHONE, "content": "hello"}},
    ):
        response, status = wf._process_webhook("req-3")

    body = response.get_json()
    assert status == 401
    assert body["status"] == "error"
    assert sm.transitions == []


def test_webhook_accepts_signed_bearer_jwt(monkeypatch):
    sm = _WebhookStateManager(inbound_ok=True)
    wf._runtime.state_manager = sm
    wf._runtime.db_service = FakeDB()
    wf._runtime.ai_service = object()
    wf._runtime.classifier = _Classifier()
    wf._runtime.router = object()
    _patch_webhook_baseline(
        monkeypatch,
        dispatch_result={"messages": ["reply"], "new_state": "COLLECTING", "actions": []},
        send_sms_result=True,
    )
    token = _make_hs256_jwt("tok", {"sub": "httpsms", "exp": int(time.time()) + 300})

    app = Flask(__name__)
    with app.test_request_context(
        "/webhook",
        method="POST",
        headers={"Authorization": f"Bearer {token}"},
        json={"event": "message.received", "data": {"contact": PHONE, "content": "hello"}},
    ):
        response, status = wf._process_webhook("req-jwt-1")

    body = response.get_json()
    assert status == 200
    assert body["status"] == "success"
    assert sm.transitions == [(PHONE, "COLLECTING")]


def test_webhook_signature_required_rejects_missing_signature_headers(monkeypatch):
    sm = _WebhookStateManager(inbound_ok=True)
    wf._runtime.state_manager = sm
    wf._runtime.db_service = FakeDB()
    wf._runtime.ai_service = object()
    wf._runtime.classifier = _Classifier()
    wf._runtime.router = object()
    _patch_webhook_baseline(
        monkeypatch,
        dispatch_result={"messages": ["reply"], "new_state": "COLLECTING", "actions": []},
        send_sms_result=True,
    )
    monkeypatch.setattr(wf.config, "get_httpsms_webhook_signature_secret", lambda: "sig-secret")
    monkeypatch.setattr(wf.config, "httpsms_webhook_signature_required", lambda: True)

    app = Flask(__name__)
    with app.test_request_context(
        "/webhook",
        method="POST",
        headers={"Authorization": "Bearer tok"},
        json={"event": "message.received", "data": {"contact": PHONE, "content": "hello"}},
    ):
        response, status = wf._process_webhook("req-4")

    body = response.get_json()
    assert status == 401
    assert body["status"] == "error"
    assert sm.transitions == []


def test_webhook_signature_required_accepts_valid_signature(monkeypatch):
    sm = _WebhookStateManager(inbound_ok=True)
    wf._runtime.state_manager = sm
    wf._runtime.db_service = FakeDB()
    wf._runtime.ai_service = object()
    wf._runtime.classifier = _Classifier()
    wf._runtime.router = object()
    _patch_webhook_baseline(
        monkeypatch,
        dispatch_result={"messages": ["reply"], "new_state": "COLLECTING", "actions": []},
        send_sms_result=True,
    )
    secret = "sig-secret"
    monkeypatch.setattr(wf.config, "get_httpsms_webhook_signature_secret", lambda: secret)
    monkeypatch.setattr(wf.config, "httpsms_webhook_signature_required", lambda: True)

    payload = {"event": "message.received", "data": {"contact": PHONE, "content": "hello"}}
    raw_body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    timestamp = str(int(time.time()))
    signature = hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.".encode("utf-8") + raw_body,
        hashlib.sha256,
    ).hexdigest()

    app = Flask(__name__)
    with app.test_request_context(
        "/webhook",
        method="POST",
        data=raw_body,
        content_type="application/json",
        headers={
            "Authorization": "Bearer tok",
            "X-Webhook-Timestamp": timestamp,
            "X-Webhook-Signature": f"sha256={signature}",
        },
    ):
        response, status = wf._process_webhook("req-5")

    body = response.get_json()
    assert status == 200
    assert body["status"] == "success"
    assert sm.transitions == [(PHONE, "COLLECTING")]


def test_webhook_signature_required_rejects_stale_timestamp(monkeypatch):
    sm = _WebhookStateManager(inbound_ok=True)
    wf._runtime.state_manager = sm
    wf._runtime.db_service = FakeDB()
    wf._runtime.ai_service = object()
    wf._runtime.classifier = _Classifier()
    wf._runtime.router = object()
    _patch_webhook_baseline(
        monkeypatch,
        dispatch_result={"messages": ["reply"], "new_state": "COLLECTING", "actions": []},
        send_sms_result=True,
    )
    secret = "sig-secret"
    monkeypatch.setattr(wf.config, "get_httpsms_webhook_signature_secret", lambda: secret)
    monkeypatch.setattr(wf.config, "httpsms_webhook_signature_required", lambda: True)
    monkeypatch.setattr(wf.config, "get_httpsms_webhook_signature_tolerance_seconds", lambda: 300)

    payload = {"event": "message.received", "data": {"contact": PHONE, "content": "hello"}}
    raw_body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    timestamp = str(int(time.time()) - 1000)
    signature = hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.".encode("utf-8") + raw_body,
        hashlib.sha256,
    ).hexdigest()

    app = Flask(__name__)
    with app.test_request_context(
        "/webhook",
        method="POST",
        data=raw_body,
        content_type="application/json",
        headers={
            "Authorization": "Bearer tok",
            "X-Webhook-Timestamp": timestamp,
            "X-Webhook-Signature": f"sha256={signature}",
        },
    ):
        response, status = wf._process_webhook("req-6")

    body = response.get_json()
    assert status == 401
    assert body["status"] == "error"
    assert sm.transitions == []
