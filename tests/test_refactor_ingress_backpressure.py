from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from refactor.app.ingress.quick_ack import try_enqueue_sms_quick_ack
from refactor.app.queue import InboundQueueEnvelope, InboundQueueRecord, QueueMessageMetadata, QueueStatus


class _StubInboundProvider:
    def __init__(
        self,
        *,
        pending: list[InboundQueueRecord] | None = None,
        fail_on_list_pending: bool = False,
    ) -> None:
        self.pending = list(pending or [])
        self.fail_on_list_pending = bool(fail_on_list_pending)
        self.enqueued: list[InboundQueueEnvelope] = []

    def enqueue(self, envelope: InboundQueueEnvelope, *, conn=None) -> bool:
        _ = conn
        self.enqueued.append(envelope)
        return True

    def list_pending(self, *, limit: int = 100, conn=None) -> list[InboundQueueRecord]:
        _ = conn
        if self.fail_on_list_pending:
            raise RuntimeError("metrics provider unavailable")
        return list(self.pending)[: max(1, int(limit))]


def _pending_record(*, message_id: str) -> InboundQueueRecord:
    created_at = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    return InboundQueueRecord(
        message_id=message_id,
        payload={"channel": "sms"},
        metadata=QueueMessageMetadata(
            dedup_key=f"dedup-{message_id}",
            request_id=f"req-{message_id}",
            enqueued_at=created_at,
        ),
        status=QueueStatus.PENDING,
        max_attempts=5,
        last_error=None,
        created_at=created_at,
        updated_at=created_at,
    )


def _enqueue_with_env(*, provider: _StubInboundProvider, env: dict[str, str]):
    return try_enqueue_sms_quick_ack(
        db_service=None,
        phone_number="+61412345678",
        message_body="hello",
        message_data={"message_id": "bp-msg-1"},
        request_payload={"message_id": "bp-msg-1"},
        request_headers={"X-Test": "1"},
        remote_addr="127.0.0.1",
        request_id="req-bp-1",
        env=env,
        inbound_provider=provider,
    )


def test_backpressure_below_threshold_allows_enqueue() -> None:
    provider = _StubInboundProvider(pending=[])
    result = _enqueue_with_env(
        provider=provider,
        env={
            "REFACTOR_SMS_INGRESS_QUICK_ACK_ENABLED": "true",
            "REFACTOR_SMS_INGRESS_BACKPRESSURE_MAX_QUEUE_DEPTH": "10",
            "REFACTOR_SMS_INGRESS_BACKPRESSURE_MAX_LAG_SECONDS": "1200",
            "REFACTOR_SMS_INGRESS_BACKPRESSURE_OVERLOAD_BEHAVIOR": "sync_fallback",
        },
    )

    assert result.accepted is True
    assert result.reason == "enqueued"
    assert len(provider.enqueued) == 1
    attributes = provider.enqueued[0].metadata.attributes
    assert attributes["cost_throttle_advisory_mode"] == "allow"
    assert attributes["cost_compaction_strategy"] == "observe"


@pytest.mark.parametrize(
    ("behavior", "expected_accepted", "expected_reason", "expected_enqueues", "expected_attempts"),
    [
        ("reject", False, "backpressure_reject", 0, None),
        ("sync_fallback", False, "backpressure_sync_fallback", 0, None),
        ("degrade_mode", True, "enqueued_degraded", 1, 1),
    ],
)
def test_backpressure_over_threshold_applies_configured_behavior(
    behavior: str,
    expected_accepted: bool,
    expected_reason: str,
    expected_enqueues: int,
    expected_attempts: int | None,
) -> None:
    provider = _StubInboundProvider(pending=[_pending_record(message_id="already-pending")])
    result = _enqueue_with_env(
        provider=provider,
        env={
            "REFACTOR_SMS_INGRESS_QUICK_ACK_ENABLED": "true",
            "REFACTOR_SMS_INGRESS_BACKPRESSURE_MAX_QUEUE_DEPTH": "1",
            "REFACTOR_SMS_INGRESS_BACKPRESSURE_MAX_LAG_SECONDS": "1200",
            "REFACTOR_SMS_INGRESS_BACKPRESSURE_OVERLOAD_BEHAVIOR": behavior,
            "REFACTOR_SMS_INGRESS_BACKPRESSURE_DEGRADE_MAX_ATTEMPTS": "1",
        },
    )

    assert result.accepted is expected_accepted
    assert result.reason == expected_reason
    assert len(provider.enqueued) == expected_enqueues
    if expected_attempts is not None:
        assert provider.enqueued[0].max_attempts == expected_attempts


def test_backpressure_metrics_provider_failure_uses_conservative_fallback() -> None:
    provider = _StubInboundProvider(fail_on_list_pending=True)
    result = _enqueue_with_env(
        provider=provider,
        env={
            "REFACTOR_SMS_INGRESS_QUICK_ACK_ENABLED": "true",
            "REFACTOR_SMS_INGRESS_BACKPRESSURE_MAX_QUEUE_DEPTH": "10",
            "REFACTOR_SMS_INGRESS_BACKPRESSURE_MAX_LAG_SECONDS": "1200",
            "REFACTOR_SMS_INGRESS_BACKPRESSURE_OVERLOAD_BEHAVIOR": "sync_fallback",
        },
    )

    assert result.accepted is False
    assert result.reason == "backpressure_metrics_unavailable_sync_fallback"
    assert provider.enqueued == []


def test_backpressure_guardrail_degrade_overrides_reject_behavior() -> None:
    provider = _StubInboundProvider(pending=[_pending_record(message_id="already-pending")])
    result = _enqueue_with_env(
        provider=provider,
        env={
            "REFACTOR_SMS_INGRESS_QUICK_ACK_ENABLED": "true",
            "REFACTOR_SMS_INGRESS_BACKPRESSURE_MAX_QUEUE_DEPTH": "1",
            "REFACTOR_SMS_INGRESS_BACKPRESSURE_MAX_LAG_SECONDS": "1200",
            "REFACTOR_SMS_INGRESS_BACKPRESSURE_OVERLOAD_BEHAVIOR": "reject",
            "REFACTOR_SMS_INGRESS_BACKPRESSURE_DEGRADE_MAX_ATTEMPTS": "1",
            "REFACTOR_SMS_BACKPRESSURE_SLO_GUARDRAIL_ENABLED": "true",
            "REFACTOR_SMS_BACKPRESSURE_SAMPLE_SIZE": "50",
            "REFACTOR_SMS_BACKPRESSURE_RETRY_RATE": "0.1",
        },
    )

    assert result.accepted is True
    assert result.reason == "enqueued_degraded"
    assert provider.enqueued[0].max_attempts == 1
