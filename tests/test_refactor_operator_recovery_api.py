from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from typing import Any

from refactor.app.ops import (
    OperatorQueueArchivalInvoker,
    OperatorDLQReplayInvoker,
    OperatorRecoveryAPI,
    QueuePauseService,
    StuckJobInspectionService,
)
from refactor.app.queue.inbound import InboundQueueRecord
from refactor.app.queue.metadata import QueueMessageMetadata
from refactor.app.queue.outbound import OutboundQueueRecord
from refactor.app.queue.status import QueueDirection, QueueStatus
from refactor.app.ingress.rollout_controls import load_operator_recovery_settings
from refactor.app.retention import QueueArchivalRetentionPolicy, QueueArchivalService
from refactor.app.workers.dlq_replay import DLQReplayService
from refactor.app.workers.inbound_runtime import InboundWorkerRuntime
from refactor.app.workers.supervision.lease import InMemoryWorkerLeaseStore


class _MutableClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: int) -> None:
        self._now = self._now + timedelta(seconds=max(0, int(seconds)))


def _append_replay_attributes(attributes: dict[str, Any], replay_metadata: dict[str, Any]) -> dict[str, Any]:
    history = list(attributes.get("dlq_replay_history", [])) if isinstance(attributes.get("dlq_replay_history"), list) else []
    history.append(dict(replay_metadata))
    return {
        **attributes,
        "dlq_replay": dict(replay_metadata),
        "dlq_replay_history": history[-20:],
        "dlq_replay_count": int(attributes.get("dlq_replay_count") or 0) + 1,
    }


class _InMemoryInboundRepository:
    def __init__(self) -> None:
        self._rows: dict[str, InboundQueueRecord] = {}
        self.list_pending_calls = 0
        self.archived: list[str] = []

    def add(self, record: InboundQueueRecord) -> None:
        self._rows[record.message_id] = record

    def get_message(self, message_id: str, *, conn: Any | None = None) -> InboundQueueRecord | None:
        _ = conn
        return self._rows.get(message_id)

    def list_pending(self, *, limit: int = 100, conn: Any | None = None) -> list[InboundQueueRecord]:
        _ = conn
        self.list_pending_calls += 1
        rows = [row for row in self._rows.values() if row.status in {QueueStatus.PENDING, QueueStatus.RETRY}]
        rows.sort(key=lambda row: (row.created_at or "", row.message_id))
        return rows[: max(1, int(limit))]

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
        metadata = replace(record.metadata, attempt=0, attributes=attributes, dead_lettered_at=record.metadata.dead_lettered_at)
        self._rows[message_id] = replace(record, status=QueueStatus.PENDING, metadata=metadata, last_error=None)
        return True

    def mark_processing(self, message_id: str, *, conn: Any | None = None) -> bool:
        _ = (message_id, conn)
        return False

    def mark_retry(
        self,
        message_id: str,
        *,
        error_message: str,
        retry_delay_seconds: int = 0,
        conn: Any | None = None,
    ) -> bool:
        _ = (message_id, error_message, retry_delay_seconds, conn)
        return False

    def mark_sent(self, message_id: str, *, conn: Any | None = None) -> bool:
        _ = (message_id, conn)
        return False

    def archive_processed_records(
        self,
        *,
        status: str,
        older_than: str,
        limit: int,
        archived_by: str | None = None,
        archive_reason: str | None = None,
        conn: Any | None = None,
    ) -> int:
        _ = (archived_by, archive_reason, conn)
        cutoff = datetime.fromisoformat(older_than)
        bounded_limit = max(1, int(limit))
        candidates = [
            row
            for row in self._rows.values()
            if row.status == status and datetime.fromisoformat(row.updated_at or row.created_at or older_than) <= cutoff
        ]
        candidates.sort(key=lambda row: (row.updated_at or row.created_at or "", row.message_id))
        selected = candidates[:bounded_limit]
        for row in selected:
            self.archived.append(row.message_id)
            self._rows.pop(row.message_id, None)
        return len(selected)


class _InMemoryOutboundRepository:
    def __init__(self) -> None:
        self._rows: dict[str, OutboundQueueRecord] = {}
        self.archived: list[str] = []

    def add(self, record: OutboundQueueRecord) -> None:
        self._rows[record.message_id] = record

    def get_message(self, message_id: str, *, conn: Any | None = None) -> OutboundQueueRecord | None:
        _ = conn
        return self._rows.get(message_id)

    def list_pending(self, *, limit: int = 100, conn: Any | None = None) -> list[OutboundQueueRecord]:
        _ = conn
        rows = [row for row in self._rows.values() if row.status in {QueueStatus.PENDING, QueueStatus.RETRY}]
        rows.sort(key=lambda row: (row.created_at or "", row.message_id))
        return rows[: max(1, int(limit))]

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
        metadata = replace(record.metadata, attempt=0, attributes=attributes, dead_lettered_at=record.metadata.dead_lettered_at)
        self._rows[message_id] = replace(record, status=QueueStatus.PENDING, metadata=metadata, last_error=None)
        return True

    def archive_processed_records(
        self,
        *,
        status: str,
        older_than: str,
        limit: int,
        archived_by: str | None = None,
        archive_reason: str | None = None,
        conn: Any | None = None,
    ) -> int:
        _ = (archived_by, archive_reason, conn)
        cutoff = datetime.fromisoformat(older_than)
        bounded_limit = max(1, int(limit))
        candidates = [
            row
            for row in self._rows.values()
            if row.status == status and datetime.fromisoformat(row.updated_at or row.created_at or older_than) <= cutoff
        ]
        candidates.sort(key=lambda row: (row.updated_at or row.created_at or "", row.message_id))
        selected = candidates[:bounded_limit]
        for row in selected:
            self.archived.append(row.message_id)
            self._rows.pop(row.message_id, None)
        return len(selected)


class _NoopGuard:
    def was_processed(self, *, message_id: str, dedup_key: str, conn: Any | None = None) -> bool:
        _ = (message_id, dedup_key, conn)
        return False

    def mark_processed(self, *, message_id: str, dedup_key: str, metadata=None, conn: Any | None = None) -> bool:
        _ = (message_id, dedup_key, metadata, conn)
        return True


class _NoopOrchestrator:
    def execute(self, message) -> None:
        _ = message


def _inbound_record(message_id: str, *, status: str = QueueStatus.DEAD, created_at: str = "2026-01-01T00:00:01+00:00") -> InboundQueueRecord:
    return InboundQueueRecord(
        message_id=message_id,
        payload={
            "event_type": "inbound.sms.received",
            "aggregate_id": f"agg-{message_id}",
            "secret_token": "abc123",
            "body": "this should not leak",
        },
        metadata=QueueMessageMetadata(
            attempt=2 if status == QueueStatus.DEAD else 1,
            dedup_key=f"dedup-{message_id}",
            enqueued_at=created_at,
            dead_lettered_at=created_at if status == QueueStatus.DEAD else None,
        ),
        status=status,
        max_attempts=4,
        last_error="failed token=abc123",
        created_at=created_at,
        updated_at=created_at,
    )


def _outbound_record(message_id: str, *, status: str = QueueStatus.DEAD, created_at: str = "2026-01-01T00:00:02+00:00") -> OutboundQueueRecord:
    return OutboundQueueRecord(
        message_id=message_id,
        message_type="sms.outbound.send",
        aggregate_type="conversation",
        aggregate_id=f"agg-{message_id}",
        payload={"event_type": "sms.outbound.send", "api_key": "dont-leak"},
        metadata=QueueMessageMetadata(
            attempt=3 if status == QueueStatus.DEAD else 1,
            dedup_key=f"dedup-{message_id}",
            enqueued_at=created_at,
            dead_lettered_at=created_at if status == QueueStatus.DEAD else None,
        ),
        status=status,
        max_attempts=5,
        last_error="provider token leaked",
        created_at=created_at,
        updated_at=created_at,
    )


def _build_api(
    *,
    inbound: _InMemoryInboundRepository | None = None,
    outbound: _InMemoryOutboundRepository | None = None,
    lease_store: InMemoryWorkerLeaseStore | None = None,
    enabled: bool = True,
    max_pause_seconds: int = 120,
    max_replay_batch: int = 2,
) -> tuple[OperatorRecoveryAPI, QueuePauseService, _InMemoryInboundRepository, _InMemoryOutboundRepository]:
    resolved_inbound = inbound or _InMemoryInboundRepository()
    resolved_outbound = outbound or _InMemoryOutboundRepository()
    pause_service = QueuePauseService(max_pause_seconds=max_pause_seconds, audit_logger=lambda *_: None)
    inspect_service = StuckJobInspectionService(
        inbound_repository=resolved_inbound,
        outbound_repository=resolved_outbound,
        lease_store=lease_store,
        audit_logger=lambda *_: None,
    )
    replay_service = DLQReplayService(
        inbound_repository=resolved_inbound,
        outbound_repository=resolved_outbound,
        max_batch_size=max_replay_batch,
    )
    replay_invoker = OperatorDLQReplayInvoker(
        replay_service=replay_service,
        max_batch_size=max_replay_batch,
        audit_logger=lambda *_: None,
    )
    archival_invoker = OperatorQueueArchivalInvoker(
        archival_service=QueueArchivalService(
            inbound_repository=resolved_inbound,
            outbound_repository=resolved_outbound,
            policy=QueueArchivalRetentionPolicy(
                sent_ttl_seconds=60,
                dead_ttl_seconds=60,
                replay_window_seconds=60,
                audit_window_seconds=60,
                max_batch_size=max_replay_batch,
            ),
        ),
        max_batch_size=max_replay_batch,
        audit_logger=lambda *_: None,
    )
    api = OperatorRecoveryAPI(
        pause_service=pause_service,
        inspection_service=inspect_service,
        replay_invoker=replay_invoker,
        archival_invoker=archival_invoker,
        feature_enabled_getter=lambda: enabled,
    )
    return api, pause_service, resolved_inbound, resolved_outbound


def test_operator_recovery_permission_denials_return_forbidden() -> None:
    api, _, inbound, _ = _build_api()
    inbound.add(_inbound_record("msg-1"))

    pause_body, pause_code = api.pause_queue(
        actor="ops-user",
        granted_permissions={"queue:inspect"},
        payload={"queue_name": "refactor_inbound", "reason": "maintenance"},
    )
    replay_body, replay_code = api.invoke_dlq_replay(
        actor="ops-user",
        granted_permissions={"queue:pause"},
        payload={"reason": "retry after fix", "direction": QueueDirection.INBOUND, "dry_run": True},
    )

    assert pause_code == 403
    assert pause_body["required_permission"] == "queue:pause"
    assert replay_code == 403
    assert replay_body["required_permission"] == "queue:dlq_replay"


def test_pause_resume_transitions_and_runtime_pause_gate() -> None:
    api, pause_service, inbound, _ = _build_api()
    inbound.add(_inbound_record("msg-pending", status=QueueStatus.PENDING))

    pause_body, pause_code = api.pause_queue(
        actor="ops-user",
        granted_permissions={"queue:pause"},
        payload={
            "queue_name": "refactor_inbound",
            "reason": "bounded maintenance window",
            "duration_seconds": 9999,
        },
    )
    assert pause_code == 200
    assert pause_body["paused"] is True
    assert pause_service.is_queue_paused("refactor_inbound")

    runtime = InboundWorkerRuntime(
        inbound_repository=inbound,
        orchestrator=_NoopOrchestrator(),
        idempotency_guard=_NoopGuard(),
        queue_pause_reader=pause_service,
    )
    paused_result = runtime.run_once()
    assert paused_result.polled == 0
    assert inbound.list_pending_calls == 0

    resume_body, resume_code = api.resume_queue(
        actor="ops-user",
        granted_permissions={"queue:pause"},
        payload={"queue_name": "refactor_inbound", "reason": "maintenance complete"},
    )
    assert resume_code == 200
    assert resume_body["paused"] is False
    assert pause_service.is_queue_paused("refactor_inbound") is False

    resumed_result = runtime.run_once()
    assert resumed_result.polled == 1
    assert inbound.list_pending_calls == 1


def test_dlq_replay_invocation_is_bounded_and_safe() -> None:
    api, _, _, outbound = _build_api(max_replay_batch=2)
    outbound.add(_outbound_record("out-1"))
    outbound.add(_outbound_record("out-2"))
    outbound.add(_outbound_record("out-3"))

    replay_body, replay_code = api.invoke_dlq_replay(
        actor="ops-user",
        granted_permissions={"queue:dlq_replay"},
        payload={
            "reason": "parser fix deployed",
            "direction": QueueDirection.OUTBOUND,
            "batch_limit": 20,
            "dry_run": False,
        },
    )
    overflow_body, overflow_code = api.invoke_dlq_replay(
        actor="ops-user",
        granted_permissions={"queue:dlq_replay"},
        payload={
            "reason": "bounded ids",
            "direction": QueueDirection.OUTBOUND,
            "mode": "batch",
            "message_ids": ["out-1", "out-2", "out-3"],
            "dry_run": True,
        },
    )

    assert replay_code == 200
    assert replay_body["requested"] == 2
    assert replay_body["replayed"] == 2
    assert outbound.get_message("out-1") is not None
    assert outbound.get_message("out-2") is not None
    assert outbound.get_message("out-3") is not None
    assert outbound.get_message("out-1").status == QueueStatus.PENDING  # type: ignore[union-attr]
    assert outbound.get_message("out-2").status == QueueStatus.PENDING  # type: ignore[union-attr]
    assert outbound.get_message("out-3").status == QueueStatus.DEAD  # type: ignore[union-attr]
    assert overflow_code == 400
    assert "bounded replay limit" in overflow_body["message"]


def test_queue_archival_invocation_enforces_permission_and_batch_bounds() -> None:
    api, _, inbound, outbound = _build_api(max_replay_batch=2)
    inbound.add(_inbound_record("in-sent-1", status=QueueStatus.SENT, created_at="2026-01-01T00:00:01+00:00"))
    inbound.add(_inbound_record("in-dead-1", status=QueueStatus.DEAD, created_at="2026-01-01T00:00:02+00:00"))
    outbound.add(_outbound_record("out-sent-1", status=QueueStatus.SENT, created_at="2026-01-01T00:00:03+00:00"))
    outbound.add(_outbound_record("out-dead-1", status=QueueStatus.DEAD, created_at="2026-01-01T00:00:04+00:00"))

    denied_body, denied_code = api.invoke_queue_archival(
        actor="ops-user",
        granted_permissions={"queue:inspect"},
        payload={"reason": "cleanup", "batch_limit": 10},
    )
    body, code = api.invoke_queue_archival(
        actor="ops-user",
        granted_permissions={"queue:archive"},
        payload={"reason": "cleanup old queue records", "batch_limit": 10},
    )

    assert denied_code == 403
    assert denied_body["required_permission"] == "queue:archive"
    assert code == 200
    assert body["bounded_limit"] == 2
    assert body["archived_total"] == 2
    assert len(inbound.archived) + len(outbound.archived) == 2


def test_inspection_response_is_deterministic_and_scrubbed() -> None:
    clock = _MutableClock()
    lease_store = InMemoryWorkerLeaseStore(clock=clock)
    lease_store.claim(
        queue_name="refactor_inbound",
        item_id="lease-stale-1",
        owner_id="worker-a",
        lease_duration_seconds=1,
    )
    clock.advance(2)
    api, _, inbound, outbound = _build_api(lease_store=lease_store)
    inbound.add(_inbound_record("in-stuck-1"))
    outbound.add(_outbound_record("out-stuck-1"))

    first_body, first_code = api.inspect_stuck_jobs(
        actor="ops-user",
        granted_permissions={"queue:inspect"},
        payload={"statuses": ["dead", "stale_claim"], "direction": "all", "limit": 10},
    )
    second_body, second_code = api.inspect_stuck_jobs(
        actor="ops-user",
        granted_permissions={"queue:inspect"},
        payload={"statuses": ["dead", "stale_claim"], "direction": "all", "limit": 10},
    )
    encoded = json.dumps(first_body, sort_keys=True)

    assert first_code == 200
    assert second_code == 200
    assert first_body == second_body
    assert first_body["summary"]["dead"] == 2
    assert first_body["summary"]["stale_claim"] == 1
    assert all("payload" not in item for item in first_body["items"])
    assert "abc123" not in encoded
    assert "dont-leak" not in encoded


def test_operator_recovery_feature_flag_blocks_routes() -> None:
    api, _, _, _ = _build_api(enabled=False)
    body, status = api.inspect_stuck_jobs(
        actor="ops-user",
        granted_permissions={"queue:inspect"},
        payload={},
    )

    assert status == 404
    assert body["error"] == "operator_recovery_disabled"


def test_operator_recovery_rollout_settings_parse_and_clamp() -> None:
    settings = load_operator_recovery_settings(
        env={
            "REFACTOR_OPERATOR_RECOVERY_ENABLED": "yes",
            "REFACTOR_OPERATOR_RECOVERY_MAX_PAUSE_SECONDS": "999999",
            "REFACTOR_OPERATOR_RECOVERY_MAX_REPLAY_BATCH_SIZE": "0",
        }
    )

    assert settings.enabled is True
    assert settings.max_pause_seconds == 86400
    assert settings.max_replay_batch_size == 1
