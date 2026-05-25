from __future__ import annotations

from dataclasses import replace
from typing import Any

from refactor.app.ingress.quick_ack import try_enqueue_sms_quick_ack
from refactor.app.queue.inbound import InboundQueueEnvelope, InboundQueueRecord
from refactor.app.queue.metadata import QueueMessageMetadata
from refactor.app.queue.providers import InboundQueueProvider
from refactor.app.queue.status import QueueStatus


class _InMemoryInboundQueueProvider:
    def __init__(self) -> None:
        self._rows: dict[str, InboundQueueRecord] = {}
        self._dedup_index: dict[str, str] = {}

    def enqueue(self, envelope: InboundQueueEnvelope, *, conn: Any | None = None) -> bool:
        _ = conn
        dedup_key = envelope.normalized_dedup_key
        if envelope.message_id in self._rows or dedup_key in self._dedup_index:
            return False
        self._rows[envelope.message_id] = InboundQueueRecord(
            message_id=envelope.message_id,
            payload=dict(envelope.payload),
            metadata=QueueMessageMetadata.from_dict(
                {
                    **envelope.metadata.to_dict(),
                    "attempt": 0,
                    "dedup_key": dedup_key,
                }
            ),
            status=QueueStatus.PENDING,
            max_attempts=envelope.max_attempts,
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
        self._dedup_index[dedup_key] = envelope.message_id
        return True

    def mark_processing(self, message_id: str, *, conn: Any | None = None) -> bool:
        _ = conn
        row = self._rows.get(message_id)
        if row is None or row.status not in {QueueStatus.PENDING, QueueStatus.RETRY}:
            return False
        metadata = replace(row.metadata, attempt=int(row.metadata.attempt) + 1)
        self._rows[message_id] = replace(row, status=QueueStatus.PROCESSING, metadata=metadata)
        return True

    def mark_retry(
        self,
        message_id: str,
        *,
        error_message: str,
        retry_delay_seconds: int = 0,
        conn: Any | None = None,
    ) -> bool:
        _ = (retry_delay_seconds, conn)
        row = self._rows.get(message_id)
        if row is None or row.status not in {QueueStatus.PENDING, QueueStatus.PROCESSING}:
            return False
        self._rows[message_id] = replace(row, status=QueueStatus.RETRY, last_error=error_message)
        return True

    def mark_sent(self, message_id: str, *, conn: Any | None = None) -> bool:
        _ = conn
        row = self._rows.get(message_id)
        if row is None or row.status not in {QueueStatus.PROCESSING, QueueStatus.SENT}:
            return False
        self._rows[message_id] = replace(row, status=QueueStatus.SENT, last_error=None)
        return True

    def mark_dead(
        self,
        message_id: str,
        *,
        error_message: str | None = None,
        conn: Any | None = None,
    ) -> bool:
        _ = conn
        row = self._rows.get(message_id)
        if row is None:
            return False
        self._rows[message_id] = replace(row, status=QueueStatus.DEAD, last_error=error_message or row.last_error)
        return True

    def get_message(self, message_id: str, *, conn: Any | None = None) -> InboundQueueRecord | None:
        _ = conn
        return self._rows.get(message_id)

    def list_pending(self, *, limit: int = 100, conn: Any | None = None) -> list[InboundQueueRecord]:
        _ = conn
        rows = [row for row in self._rows.values() if row.status in {QueueStatus.PENDING, QueueStatus.RETRY}]
        rows.sort(key=lambda row: row.created_at or "")
        return rows[: max(1, int(limit))]

    def list_dead(self, *, limit: int = 100, conn: Any | None = None) -> list[InboundQueueRecord]:
        _ = conn
        rows = [row for row in self._rows.values() if row.status == QueueStatus.DEAD]
        rows.sort(key=lambda row: row.created_at or "")
        return rows[: max(1, int(limit))]

    def replay_dead(
        self,
        message_id: str,
        *,
        replay_metadata: dict[str, Any],
        conn: Any | None = None,
    ) -> bool:
        _ = (replay_metadata, conn)
        row = self._rows.get(message_id)
        if row is None or row.status != QueueStatus.DEAD:
            return False
        self._rows[message_id] = replace(row, status=QueueStatus.PENDING, last_error=None)
        return True


def test_quick_ack_accepts_in_memory_provider_injection_without_db_service() -> None:
    provider = _InMemoryInboundQueueProvider()
    assert isinstance(provider, InboundQueueProvider)

    first = try_enqueue_sms_quick_ack(
        db_service=None,
        phone_number="+61412345678",
        message_body="provider-injection",
        message_data={"message_id": "provider-msg-1"},
        request_payload={"message_id": "provider-msg-1"},
        request_headers={"X-Test": "provider"},
        remote_addr="127.0.0.1",
        request_id="req-provider-1",
        env={"REFACTOR_SMS_INGRESS_QUICK_ACK_ENABLED": "true"},
        inbound_provider=provider,
    )
    duplicate = try_enqueue_sms_quick_ack(
        db_service=None,
        phone_number="+61412345678",
        message_body="provider-injection",
        message_data={"message_id": "provider-msg-1"},
        request_payload={"message_id": "provider-msg-1"},
        request_headers={"X-Test": "provider"},
        remote_addr="127.0.0.1",
        request_id="req-provider-2",
        env={"REFACTOR_SMS_INGRESS_QUICK_ACK_ENABLED": "true"},
        inbound_provider=provider,
    )

    assert first.accepted is True
    assert first.duplicate is False
    assert duplicate.accepted is True
    assert duplicate.duplicate is True
    assert len(provider.list_pending(limit=10)) == 1
