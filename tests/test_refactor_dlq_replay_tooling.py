from __future__ import annotations

from dataclasses import replace
from typing import Any

from refactor.app.queue.inbound import InboundQueueRecord
from refactor.app.queue.metadata import QueueMessageMetadata
from refactor.app.queue.outbound import OutboundQueueRecord
from refactor.app.queue.status import QueueDirection, QueueStatus
from refactor.app.workers.dlq_replay import (
    DLQReplayCommand,
    DLQReplaySelection,
    DLQReplayService,
)


def _append_replay_attributes(attributes: dict[str, Any], replay_metadata: dict[str, Any]) -> dict[str, Any]:
    history = list(attributes.get("dlq_replay_history", [])) if isinstance(attributes.get("dlq_replay_history"), list) else []
    history.append(dict(replay_metadata))
    return {
        **attributes,
        "dlq_replay": dict(replay_metadata),
        "dlq_replay_history": history[-20:],
        "dlq_replay_count": int(attributes.get("dlq_replay_count") or 0) + 1,
    }


class _InMemoryInboundReplayRepository:
    def __init__(self) -> None:
        self._rows: dict[str, InboundQueueRecord] = {}

    def add(self, record: InboundQueueRecord) -> None:
        self._rows[record.message_id] = record

    def get_message(self, message_id: str, *, conn: Any | None = None) -> InboundQueueRecord | None:
        _ = conn
        return self._rows.get(message_id)

    def list_dead(self, *, limit: int = 100, conn: Any | None = None) -> list[InboundQueueRecord]:
        _ = conn
        rows = [row for row in self._rows.values() if row.status == QueueStatus.DEAD]
        rows.sort(key=lambda row: (row.created_at or "", row.message_id))
        return rows[: max(1, int(limit))]

    def replay_dead(
        self,
        message_id: str,
        *,
        replay_metadata: dict[str, Any],
        conn: Any | None = None,
    ) -> bool:
        _ = conn
        record = self._rows.get(message_id)
        if record is None or record.status != QueueStatus.DEAD:
            return False
        attributes = _append_replay_attributes(dict(record.metadata.attributes), replay_metadata)
        metadata = replace(
            record.metadata,
            attempt=0,
            available_at=None,
            processing_started_at=None,
            last_attempt_at=None,
            last_error_at=None,
            attributes=attributes,
        )
        self._rows[message_id] = replace(
            record,
            status=QueueStatus.PENDING,
            metadata=metadata,
            last_error=None,
            updated_at="2026-01-01T00:10:00+00:00",
        )
        return True


class _InMemoryOutboundReplayRepository:
    def __init__(self) -> None:
        self._rows: dict[str, OutboundQueueRecord] = {}

    def add(self, record: OutboundQueueRecord) -> None:
        self._rows[record.message_id] = record

    def get_message(self, message_id: str, *, conn: Any | None = None) -> OutboundQueueRecord | None:
        _ = conn
        return self._rows.get(message_id)

    def list_dead(self, *, limit: int = 100, conn: Any | None = None) -> list[OutboundQueueRecord]:
        _ = conn
        rows = [row for row in self._rows.values() if row.status == QueueStatus.DEAD]
        rows.sort(key=lambda row: (row.created_at or "", row.message_id))
        return rows[: max(1, int(limit))]

    def replay_dead(
        self,
        message_id: str,
        *,
        replay_metadata: dict[str, Any],
        conn: Any | None = None,
    ) -> bool:
        _ = conn
        record = self._rows.get(message_id)
        if record is None or record.status != QueueStatus.DEAD:
            return False
        attributes = _append_replay_attributes(dict(record.metadata.attributes), replay_metadata)
        metadata = replace(
            record.metadata,
            attempt=0,
            available_at=None,
            processing_started_at=None,
            last_attempt_at=None,
            last_error_at=None,
            attributes=attributes,
        )
        self._rows[message_id] = replace(
            record,
            status=QueueStatus.PENDING,
            metadata=metadata,
            last_error=None,
            updated_at="2026-01-01T00:10:00+00:00",
        )
        return True

    def force_dead(self, message_id: str) -> None:
        record = self._rows[message_id]
        self._rows[message_id] = replace(record, status=QueueStatus.DEAD, last_error="dead again")


def _inbound_dead_record(message_id: str, *, created_at: str) -> InboundQueueRecord:
    return InboundQueueRecord(
        message_id=message_id,
        payload={"event_type": "inbound.sms.received", "aggregate_id": f"agg-{message_id}"},
        metadata=QueueMessageMetadata(
            attempt=3,
            dedup_key=f"dedup-{message_id}",
            enqueued_at=created_at,
            dead_lettered_at="2026-01-01T00:01:00+00:00",
        ),
        status=QueueStatus.DEAD,
        max_attempts=3,
        last_error="failed permanently",
        created_at=created_at,
        updated_at=created_at,
    )


def _outbound_dead_record(message_id: str, *, created_at: str) -> OutboundQueueRecord:
    return OutboundQueueRecord(
        message_id=message_id,
        message_type="sms.outbound.send",
        aggregate_type="conversation_state",
        aggregate_id=f"agg-{message_id}",
        payload={"event_type": "sms.outbound.send"},
        metadata=QueueMessageMetadata(
            attempt=2,
            dedup_key=f"dedup-{message_id}",
            enqueued_at=created_at,
            dead_lettered_at="2026-01-01T00:02:00+00:00",
        ),
        status=QueueStatus.DEAD,
        max_attempts=2,
        last_error="gateway failure",
        created_at=created_at,
        updated_at=created_at,
    )


def _build_service(
    *,
    inbound: _InMemoryInboundReplayRepository | None = None,
    outbound: _InMemoryOutboundReplayRepository | None = None,
) -> DLQReplayService:
    return DLQReplayService(
        inbound_repository=inbound or _InMemoryInboundReplayRepository(),
        outbound_repository=outbound or _InMemoryOutboundReplayRepository(),
        max_batch_size=2,
    )


def test_dlq_replay_dry_run_does_not_mutate_records() -> None:
    inbound = _InMemoryInboundReplayRepository()
    inbound.add(_inbound_dead_record("in-dry", created_at="2026-01-01T00:00:01+00:00"))
    service = _build_service(inbound=inbound)

    result = service.execute(
        DLQReplayCommand(
            actor="ops-user",
            reason="verify candidate set",
            granted_permissions={"queue:dlq_replay"},
            mode="single",
            dry_run=True,
            selection=DLQReplaySelection(direction=QueueDirection.INBOUND, message_ids=("in-dry",)),
        )
    )
    stored = inbound.get_message("in-dry")

    assert result.requested == 1
    assert result.dry_run_candidates == 1
    assert result.replayed == 0
    assert stored is not None
    assert stored.status == QueueStatus.DEAD
    assert "dlq_replay" not in stored.metadata.attributes


def test_dlq_replay_enforces_bounded_batch_size() -> None:
    outbound = _InMemoryOutboundReplayRepository()
    outbound.add(_outbound_dead_record("out-1", created_at="2026-01-01T00:00:01+00:00"))
    outbound.add(_outbound_dead_record("out-2", created_at="2026-01-01T00:00:02+00:00"))
    outbound.add(_outbound_dead_record("out-3", created_at="2026-01-01T00:00:03+00:00"))
    service = _build_service(outbound=outbound)

    result = service.execute(
        DLQReplayCommand(
            actor="ops-user",
            reason="controlled replay batch",
            granted_permissions={"queue:dlq_replay"},
            batch_limit=2,
            dry_run=False,
            selection=DLQReplaySelection(direction=QueueDirection.OUTBOUND),
        )
    )

    assert result.requested == 2
    assert result.replayed == 2
    assert outbound.get_message("out-1") is not None
    assert outbound.get_message("out-2") is not None
    assert outbound.get_message("out-3") is not None
    assert outbound.get_message("out-1").status == QueueStatus.PENDING  # type: ignore[union-attr]
    assert outbound.get_message("out-2").status == QueueStatus.PENDING  # type: ignore[union-attr]
    assert outbound.get_message("out-3").status == QueueStatus.DEAD  # type: ignore[union-attr]


def test_dlq_replay_persists_audit_metadata_on_replayed_record() -> None:
    inbound = _InMemoryInboundReplayRepository()
    inbound.add(_inbound_dead_record("in-audit", created_at="2026-01-01T00:00:01+00:00"))
    service = _build_service(inbound=inbound)

    result = service.execute(
        DLQReplayCommand(
            actor="ops-auditor",
            reason="fix deployed for parser bug",
            granted_permissions={"queue:dlq_replay"},
            mode="single",
            dry_run=False,
            replay_run_id="run-audit-1",
            idempotency_key="idem-audit-1",
            selection=DLQReplaySelection(direction=QueueDirection.INBOUND, message_ids=("in-audit",)),
        )
    )
    stored = inbound.get_message("in-audit")

    assert result.replayed == 1
    assert stored is not None
    assert stored.status == QueueStatus.PENDING
    assert stored.metadata.attributes["dlq_replay"]["actor"] == "ops-auditor"
    assert stored.metadata.attributes["dlq_replay"]["reason"] == "fix deployed for parser bug"
    assert stored.metadata.attributes["dlq_replay"]["replay_run_id"] == "run-audit-1"
    assert stored.metadata.attributes["dlq_replay"]["idempotency_key"] == "idem-audit-1"


def test_dlq_replay_prevents_duplicate_replay_by_idempotency_key() -> None:
    outbound = _InMemoryOutboundReplayRepository()
    outbound.add(_outbound_dead_record("out-dup", created_at="2026-01-01T00:00:01+00:00"))
    service = _build_service(outbound=outbound)

    first = service.execute(
        DLQReplayCommand(
            actor="ops-user",
            reason="first replay",
            granted_permissions={"queue:dlq_replay"},
            mode="single",
            dry_run=False,
            idempotency_key="idem-dup-1",
            selection=DLQReplaySelection(direction=QueueDirection.OUTBOUND, message_ids=("out-dup",)),
        )
    )
    assert first.replayed == 1

    outbound.force_dead("out-dup")
    second = service.execute(
        DLQReplayCommand(
            actor="ops-user",
            reason="retry same command",
            granted_permissions={"queue:dlq_replay"},
            mode="single",
            dry_run=False,
            idempotency_key="idem-dup-1",
            selection=DLQReplaySelection(direction=QueueDirection.OUTBOUND, message_ids=("out-dup",)),
        )
    )
    stored = outbound.get_message("out-dup")

    assert second.replayed == 0
    assert second.skipped == 1
    assert second.decisions[0].reason == "duplicate_replay_idempotency_key"
    assert stored is not None
    assert stored.status == QueueStatus.DEAD
