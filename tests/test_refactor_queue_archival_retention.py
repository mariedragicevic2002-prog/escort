from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from refactor.app.queue.status import QueueStatus
from refactor.app.retention import QueueArchivalCommand, QueueArchivalRetentionPolicy, QueueArchivalService


@dataclass
class _ArchiveRecord:
    message_id: str
    status: str
    updated_at: str


class _InMemoryArchiveRepository:
    def __init__(self, records: list[_ArchiveRecord], *, fail_statuses: set[str] | None = None) -> None:
        self._records = {record.message_id: record for record in records}
        self.fail_statuses = set(fail_statuses or set())
        self.archived: list[str] = []
        self.calls: list[dict[str, Any]] = []

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
        _ = (conn,)
        self.calls.append(
            {
                "status": status,
                "older_than": older_than,
                "limit": limit,
                "archived_by": archived_by,
                "archive_reason": archive_reason,
            }
        )
        if status in self.fail_statuses:
            raise RuntimeError(f"forced failure for {status}")
        cutoff = datetime.fromisoformat(older_than)
        candidates = [
            record
            for record in self._records.values()
            if record.status == status and datetime.fromisoformat(record.updated_at) <= cutoff
        ]
        candidates.sort(key=lambda record: (record.updated_at, record.message_id))
        selected = candidates[: max(1, int(limit))]
        for record in selected:
            self.archived.append(record.message_id)
            self._records.pop(record.message_id, None)
        return len(selected)

    @property
    def active_ids(self) -> set[str]:
        return set(self._records.keys())


def _iso(days_ago: int, *, now: datetime) -> str:
    return (now - timedelta(days=days_ago)).isoformat()


def test_retention_ttl_filtering_archives_only_records_older_than_sent_ttl() -> None:
    now = datetime(2026, 1, 15, tzinfo=UTC)
    inbound = _InMemoryArchiveRepository(
        [
            _ArchiveRecord("in-sent-old", QueueStatus.SENT, _iso(10, now=now)),
            _ArchiveRecord("in-sent-fresh", QueueStatus.SENT, _iso(2, now=now)),
        ]
    )
    outbound = _InMemoryArchiveRepository([])
    service = QueueArchivalService(
        inbound_repository=inbound,
        outbound_repository=outbound,
        policy=QueueArchivalRetentionPolicy(
            sent_ttl_seconds=7 * 24 * 60 * 60,
            dead_ttl_seconds=7 * 24 * 60 * 60,
            replay_window_seconds=1 * 24 * 60 * 60,
            audit_window_seconds=1 * 24 * 60 * 60,
            max_batch_size=20,
        ),
    )

    result = service.execute(
        QueueArchivalCommand(
            actor="ops-user",
            reason="ttl cleanup",
            batch_limit=20,
            requested_at=now.isoformat(),
        )
    )

    assert result.archived_total == 1
    assert inbound.archived == ["in-sent-old"]
    assert "in-sent-fresh" in inbound.active_ids


def test_retention_batch_limit_is_globally_bounded() -> None:
    now = datetime(2026, 1, 15, tzinfo=UTC)
    inbound = _InMemoryArchiveRepository(
        [
            _ArchiveRecord("in-sent-1", QueueStatus.SENT, _iso(20, now=now)),
            _ArchiveRecord("in-dead-1", QueueStatus.DEAD, _iso(20, now=now)),
        ]
    )
    outbound = _InMemoryArchiveRepository(
        [
            _ArchiveRecord("out-sent-1", QueueStatus.SENT, _iso(20, now=now)),
            _ArchiveRecord("out-dead-1", QueueStatus.DEAD, _iso(20, now=now)),
        ]
    )
    service = QueueArchivalService(
        inbound_repository=inbound,
        outbound_repository=outbound,
        policy=QueueArchivalRetentionPolicy(
            sent_ttl_seconds=1,
            dead_ttl_seconds=1,
            replay_window_seconds=1,
            audit_window_seconds=1,
            max_batch_size=2,
        ),
    )

    result = service.execute(
        QueueArchivalCommand(
            actor="ops-user",
            reason="bounded batch",
            batch_limit=25,
            requested_at=now.isoformat(),
        )
    )

    assert result.bounded_limit == 2
    assert result.archived_total == 2
    assert sum(decision.archived for decision in result.decisions) == 2


def test_retention_exceptions_are_captured_without_aborting_other_archival_steps() -> None:
    now = datetime(2026, 1, 15, tzinfo=UTC)
    inbound = _InMemoryArchiveRepository([_ArchiveRecord("in-sent-ok", QueueStatus.SENT, _iso(20, now=now))])
    outbound = _InMemoryArchiveRepository(
        [_ArchiveRecord("out-dead-fail", QueueStatus.DEAD, _iso(20, now=now))],
        fail_statuses={QueueStatus.DEAD},
    )
    service = QueueArchivalService(
        inbound_repository=inbound,
        outbound_repository=outbound,
        policy=QueueArchivalRetentionPolicy(
            sent_ttl_seconds=1,
            dead_ttl_seconds=1,
            replay_window_seconds=1,
            audit_window_seconds=1,
            max_batch_size=10,
        ),
    )

    result = service.execute(
        QueueArchivalCommand(actor="ops-user", reason="error-tolerant archival", requested_at=now.isoformat())
    )

    assert "in-sent-ok" in inbound.archived
    assert result.archived_total == 1
    assert len(result.exceptions) == 1
    assert result.exceptions[0].direction == "outbound"
    assert result.exceptions[0].status == QueueStatus.DEAD


def test_dead_record_archival_respects_replay_safety_window() -> None:
    now = datetime(2026, 1, 15, tzinfo=UTC)
    inbound = _InMemoryArchiveRepository(
        [
            _ArchiveRecord("in-dead-replay-protected", QueueStatus.DEAD, _iso(3, now=now)),
            _ArchiveRecord("in-dead-expired", QueueStatus.DEAD, _iso(20, now=now)),
        ]
    )
    outbound = _InMemoryArchiveRepository([])
    service = QueueArchivalService(
        inbound_repository=inbound,
        outbound_repository=outbound,
        policy=QueueArchivalRetentionPolicy(
            sent_ttl_seconds=1 * 24 * 60 * 60,
            dead_ttl_seconds=1 * 24 * 60 * 60,
            replay_window_seconds=14 * 24 * 60 * 60,
            audit_window_seconds=1 * 24 * 60 * 60,
            max_batch_size=10,
        ),
    )

    result = service.execute(
        QueueArchivalCommand(actor="ops-user", reason="replay-safe cleanup", requested_at=now.isoformat())
    )

    assert result.archived_total == 1
    assert inbound.archived == ["in-dead-expired"]
    assert "in-dead-replay-protected" in inbound.active_ids
