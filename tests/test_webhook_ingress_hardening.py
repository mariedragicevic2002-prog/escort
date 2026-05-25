from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest

from refactor.app.ingress.webhook_security import (
    WebhookIngressSecurityError,
    enforce_webhook_ingress_security,
)


class _DedupDB:
    def __init__(self) -> None:
        self.claimed: set[str] = set()

    def execute_query(self, query, params=(), fetch=None, conn=None, **_kwargs):
        _ = (fetch, conn)
        sql = " ".join(str(query).split()).lower()
        if "insert into httpsms_message_dedup" not in sql:
            return []
        dedup_key = str(params[0])
        if dedup_key in self.claimed:
            return []
        self.claimed.add(dedup_key)
        return [{"message_id": dedup_key}]


def _signed_request(secret: str, payload: dict, *, timestamp: str) -> tuple[bytes, dict[str, str]]:
    raw_body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    signature = hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.".encode("utf-8") + raw_body,
        hashlib.sha256,
    ).hexdigest()
    headers = {
        "Authorization": "Bearer tok",
        "X-Webhook-Timestamp": timestamp,
        "X-Webhook-Signature": f"sha256={signature}",
    }
    return raw_body, headers


def test_stale_timestamp_is_rejected_when_signature_required() -> None:
    payload = {"event": "message.received", "data": {"contact": "+61400111222", "content": "hello"}}
    timestamp = str(int(time.time()) - 1000)
    raw_body, headers = _signed_request("sig-secret", payload, timestamp=timestamp)

    with pytest.raises(WebhookIngressSecurityError, match="Stale request timestamp"):
        enforce_webhook_ingress_security(
            headers=headers,
            raw_body=raw_body,
            payload=payload,
            message_data=payload["data"],
            phone_number="+61400111222",
            message_body="hello",
            db_service=_DedupDB(),
            webhook_secrets=["tok"],
            signature_secret="sig-secret",
            signature_required=True,
            signature_tolerance_seconds=300,
        )


def test_invalid_signature_is_rejected() -> None:
    payload = {"event": "message.received", "data": {"contact": "+61400111222", "content": "hello"}}
    raw_body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": "Bearer tok",
        "X-Webhook-Timestamp": str(int(time.time())),
        "X-Webhook-Signature": "sha256=invalid",
    }

    with pytest.raises(WebhookIngressSecurityError, match="Invalid webhook signature"):
        enforce_webhook_ingress_security(
            headers=headers,
            raw_body=raw_body,
            payload=payload,
            message_data=payload["data"],
            phone_number="+61400111222",
            message_body="hello",
            db_service=_DedupDB(),
            webhook_secrets=["tok"],
            signature_secret="sig-secret",
            signature_required=True,
            signature_tolerance_seconds=300,
        )


def test_duplicate_webhook_is_marked_duplicate() -> None:
    payload = {
        "event": "message.received",
        "data": {"contact": "+61400111222", "content": "hello", "message_id": "msg-001"},
    }
    raw_body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    headers = {"Authorization": "Bearer tok"}
    db = _DedupDB()

    first = enforce_webhook_ingress_security(
        headers=headers,
        raw_body=raw_body,
        payload=payload,
        message_data=payload["data"],
        phone_number="+61400111222",
        message_body="hello",
        db_service=db,
        webhook_secrets=["tok"],
    )
    second = enforce_webhook_ingress_security(
        headers=headers,
        raw_body=raw_body,
        payload=payload,
        message_data=payload["data"],
        phone_number="+61400111222",
        message_body="hello",
        db_service=db,
        webhook_secrets=["tok"],
    )

    assert first.duplicate is False
    assert second.duplicate is True


def test_scrubbed_payload_does_not_leak_secrets() -> None:
    payload = {
        "event": "message.received",
        "data": {
            "contact": "+61400111222",
            "content": "hello",
            "auth_token": "top-secret-token",
            "signature": "top-secret-signature",
        },
    }

    outcome = enforce_webhook_ingress_security(
        headers={},
        raw_body=b"{}",
        payload=payload,
        message_data=payload["data"],
        phone_number="+61400111222",
        message_body="hello",
        db_service=_DedupDB(),
        webhook_secrets=[],
    )

    scrubbed_repr = str(outcome.scrubbed_payload)
    assert "top-secret-token" not in scrubbed_repr
    assert "top-secret-signature" not in scrubbed_repr
    assert outcome.scrubbed_payload["data"]["auth_token"] == "[REDACTED]"
    assert outcome.scrubbed_payload["data"]["signature"] == "[REDACTED]"
