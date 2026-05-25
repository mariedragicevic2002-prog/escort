from __future__ import annotations

import time

import pytest

from app.middleware.request_validation import RequestValidationMiddleware
from app.middleware.security_controls import InboundSecurityError, SecurityControlsMiddleware
from app.runtime.context import InboundSMSMessage, RuntimeServices
from app.runtime.orchestration_facade import OrchestrationFacade
from app.security.auth import SharedSecretVerifier
from app.security.log_scrubbing import scrub_payload_for_logging
from app.security.rbac import PermissionDeniedError, require_permission


def _inbound(*, headers: dict[str, str] | None = None) -> InboundSMSMessage:
    return InboundSMSMessage(
        phone_number="+61412345678",
        body="hello",
        message_data={},
        request_payload={},
        request_id="req-1",
        request_headers=headers or {},
        remote_addr="127.0.0.1",
    )


def test_replay_requests_with_stale_timestamp_are_rejected() -> None:
    runtime = RuntimeServices(
        state_manager=object(),
        db_service=object(),
        legacy_processor=lambda _phone, _body: ["ok"],
    )
    facade = OrchestrationFacade(
        runtime_services=runtime,
        middlewares=[
            SecurityControlsMiddleware(auth_verifier=None, replay_tolerance_seconds=300),
            RequestValidationMiddleware(),
        ],
    )
    stale_timestamp = str(int(time.time()) - 1000)

    with pytest.raises(InboundSecurityError, match="Stale request timestamp"):
        facade.process_sms(_inbound(headers={"X-Request-Timestamp": stale_timestamp}))


def test_auth_verification_fails_closed_when_secret_header_missing() -> None:
    runtime = RuntimeServices(
        state_manager=object(),
        db_service=object(),
        legacy_processor=lambda _phone, _body: ["ok"],
    )
    facade = OrchestrationFacade(
        runtime_services=runtime,
        middlewares=[
            SecurityControlsMiddleware(
                auth_verifier=SharedSecretVerifier(secret_provider=lambda: "expected-secret"),
                replay_tolerance_seconds=300,
            ),
            RequestValidationMiddleware(),
        ],
    )
    current_timestamp = str(int(time.time()))

    with pytest.raises(InboundSecurityError, match="Unauthorized"):
        facade.process_sms(_inbound(headers={"X-Request-Timestamp": current_timestamp}))


def test_shared_secret_verifier_accepts_next_key_during_dual_window() -> None:
    verifier = SharedSecretVerifier(
        secret_provider=lambda: "active-secret",
        next_secret_provider=lambda: "next-secret",
        cutover_state_provider=lambda: "dual_window",
    )

    result = verifier.verify(
        headers={"X-Gateway-Secret": "next-secret"},
        remote_addr="10.1.1.9",
    )

    assert result.authorized is True
    assert result.key_version == "next"
    assert result.cutover_state == "dual_window"


def test_shared_secret_verifier_rejects_deprecated_key_after_cutover() -> None:
    verifier = SharedSecretVerifier(
        secret_provider=lambda: "new-secret",
        deprecated_secret_provider=lambda: "old-secret",
        cutover_state_provider=lambda: "post_cutover",
    )

    result = verifier.verify(
        headers={"X-Gateway-Secret": "old-secret"},
        remote_addr="10.1.1.9",
    )

    assert result.authorized is False
    assert result.reason == "deprecated_secret_rejected"
    assert result.key_version == "deprecated"
    assert result.cutover_state == "post_cutover"


def test_scrubbed_payload_redacts_secrets() -> None:
    payload = {
        "from": "+61412345678",
        "body": "hello",
        "gateway_secret": "abc123",
        "nested": {"token": "xyz", "safe": "value"},
    }

    scrubbed = scrub_payload_for_logging(payload, allowlist=("from", "body", "gateway_secret", "nested"))

    assert scrubbed["from"] == "+61412345678"
    assert scrubbed["body"] == "hello"
    assert scrubbed["gateway_secret"] == "[REDACTED]"
    assert scrubbed["nested"]["token"] == "[REDACTED]"
    assert scrubbed["nested"]["safe"] == "value"


def test_rbac_helper_denies_missing_permissions() -> None:
    with pytest.raises(PermissionDeniedError):
        require_permission({"admin:read"}, "admin:write", actor="ops")
