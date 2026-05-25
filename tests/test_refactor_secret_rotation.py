from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest

from refactor.app.ingress.webhook_security import (
    WebhookIngressSecurityError,
    enforce_webhook_ingress_security,
    verify_webhook_bearer_authorization,
)
from refactor.app.security.rotation import resolve_secret_rotation_config


class _DedupDB:
    def execute_query(self, query, params=(), fetch=None, conn=None, **_kwargs):
        _ = (query, params, fetch, conn)
        return [{"message_id": "ok"}]


def _signed_payload(secret: str, payload: dict, *, timestamp: str) -> tuple[bytes, str]:
    raw_body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    signature = hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.".encode("utf-8") + raw_body,
        hashlib.sha256,
    ).hexdigest()
    return raw_body, signature


def test_rotation_accepts_active_key() -> None:
    result = verify_webhook_bearer_authorization(
        {"Authorization": "Bearer active-secret"},
        [],
        rotation={"active_key": "active-secret", "cutover_state": "stable"},
    )

    assert result.authorized is True
    assert result.key_version == "active"
    assert result.cutover_state == "stable"


def test_rotation_dual_window_accepts_next_key() -> None:
    result = verify_webhook_bearer_authorization(
        {"Authorization": "Bearer next-secret"},
        [],
        rotation={
            "active_key": "active-secret",
            "next_key": "next-secret",
            "cutover_state": "dual_window",
        },
    )

    assert result.authorized is True
    assert result.key_version == "next"
    assert result.cutover_state == "dual_window"


def test_rotation_post_cutover_rejects_deprecated_bearer_key() -> None:
    result = verify_webhook_bearer_authorization(
        {"Authorization": "Bearer old-secret"},
        [],
        rotation={
            "active_key": "new-secret",
            "deprecated_key": "old-secret",
            "cutover_state": "post_cutover",
        },
    )

    assert result.authorized is False
    assert result.reason == "deprecated_bearer_token"
    assert result.key_version == "deprecated"
    assert result.cutover_state == "post_cutover"


def test_rotation_rejects_deprecated_signature_after_cutover() -> None:
    payload = {"event": "message.received", "data": {"contact": "+61400111222", "content": "hello"}}
    timestamp = str(int(time.time()))
    raw_body, signature = _signed_payload("old-sig", payload, timestamp=timestamp)

    with pytest.raises(WebhookIngressSecurityError) as exc_info:
        enforce_webhook_ingress_security(
            headers={
                "Authorization": "Bearer active-secret",
                "X-Webhook-Timestamp": timestamp,
                "X-Webhook-Signature": f"sha256={signature}",
            },
            raw_body=raw_body,
            payload=payload,
            message_data=payload["data"],
            phone_number="+61400111222",
            message_body="hello",
            db_service=_DedupDB(),
            webhook_secrets=["active-secret"],
            signature_secret="new-sig",
            signature_secret_rotation={
                "deprecated_key": "old-sig",
                "cutover_state": "post_cutover",
            },
            signature_required=True,
            signature_tolerance_seconds=300,
        )

    assert exc_info.value.metric_name == "webhook_signature_deprecated_rejected"
    assert exc_info.value.observability_tags["signature_key_version"] == "deprecated"
    assert exc_info.value.observability_tags["signature_cutover_state"] == "post_cutover"


def test_rotation_config_fallback_is_deterministic_when_missing() -> None:
    config = resolve_secret_rotation_config(
        fallback_secrets=["legacy-active", "legacy-next", "legacy-active"],
    )

    assert config.active_key == "legacy-active"
    assert config.next_key == "legacy-next"
    assert config.cutover_state == "dual_window"
    assert config.fallback_applied is True
