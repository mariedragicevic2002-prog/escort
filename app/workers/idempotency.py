from __future__ import annotations

from typing import Any, Mapping, Protocol

from app.workers._base_idempotency import _BaseIdempotencyGuard

_TABLE = "refactor_outbox_consumer_guard"

_CREATE_CONSUMER_GUARD_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS refactor_outbox_consumer_guard (
    event_id TEXT PRIMARY KEY,
    dedup_key TEXT NOT NULL UNIQUE,
    event_type VARCHAR(120) NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

_CREATE_CONSUMER_GUARD_EVENT_TYPE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_refactor_outbox_consumer_guard_event_type
ON refactor_outbox_consumer_guard (event_type, processed_at DESC);
"""


class IdempotentConsumerGuard(Protocol):
    def was_processed(
        self,
        *,
        event_id: str,
        dedup_key: str,
        conn: Any | None = None,
    ) -> bool:
        ...

    def mark_processed(
        self,
        *,
        event_id: str,
        dedup_key: str,
        event_type: str,
        metadata: Mapping[str, Any] | None = None,
        conn: Any | None = None,
    ) -> bool:
        ...


class DatabaseIdempotentConsumerGuard(_BaseIdempotencyGuard):
    """Persistent dedup guard keyed by event_id and logical dedup_key."""

    _CREATE_TABLE_SQL = _CREATE_CONSUMER_GUARD_TABLE_SQL
    _CREATE_INDEX_SQL = _CREATE_CONSUMER_GUARD_EVENT_TYPE_INDEX_SQL

    def was_processed(
        self,
        *,
        event_id: str,
        dedup_key: str,
        conn: Any | None = None,
    ) -> bool:
        self.ensure_schema()
        return self._was_processed(
            id_value=event_id,
            dedup_key=dedup_key,
            table=_TABLE,
            id_column="event_id",
            conn=conn,
        )

    def mark_processed(
        self,
        *,
        event_id: str,
        dedup_key: str,
        event_type: str,
        metadata: Mapping[str, Any] | None = None,
        conn: Any | None = None,
    ) -> bool:
        self.ensure_schema()
        return self._mark_processed(
            id_value=event_id,
            dedup_key=dedup_key,
            table=_TABLE,
            id_column="event_id",
            extra_columns=("event_type",),
            extra_values=(str(event_type or ""),),
            metadata=metadata,
            conn=conn,
        )

