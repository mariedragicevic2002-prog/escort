from __future__ import annotations

from typing import Any, Mapping, Protocol

from app.workers._base_idempotency import _BaseIdempotencyGuard

_TABLE = "refactor_inbound_worker_guard"

_CREATE_INBOUND_GUARD_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS refactor_inbound_worker_guard (
    message_id TEXT PRIMARY KEY,
    dedup_key TEXT NOT NULL UNIQUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

_CREATE_INBOUND_GUARD_DEDUP_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_refactor_inbound_worker_guard_processed
ON refactor_inbound_worker_guard (processed_at DESC);
"""


class InboundIdempotencyGuard(Protocol):
    def was_processed(
        self,
        *,
        message_id: str,
        dedup_key: str,
        conn: Any | None = None,
    ) -> bool:
        ...

    def mark_processed(
        self,
        *,
        message_id: str,
        dedup_key: str,
        metadata: Mapping[str, Any] | None = None,
        conn: Any | None = None,
    ) -> bool:
        ...


class DatabaseInboundIdempotencyGuard(_BaseIdempotencyGuard):
    """Persistent idempotency guard for queue-first inbound worker execution."""

    _CREATE_TABLE_SQL = _CREATE_INBOUND_GUARD_TABLE_SQL
    _CREATE_INDEX_SQL = _CREATE_INBOUND_GUARD_DEDUP_INDEX_SQL

    def was_processed(
        self,
        *,
        message_id: str,
        dedup_key: str,
        conn: Any | None = None,
    ) -> bool:
        self.ensure_schema()
        return self._was_processed(
            id_value=message_id,
            dedup_key=dedup_key,
            table=_TABLE,
            id_column="message_id",
            conn=conn,
        )

    def mark_processed(
        self,
        *,
        message_id: str,
        dedup_key: str,
        metadata: Mapping[str, Any] | None = None,
        conn: Any | None = None,
    ) -> bool:
        self.ensure_schema()
        return self._mark_processed(
            id_value=message_id,
            dedup_key=dedup_key,
            table=_TABLE,
            id_column="message_id",
            metadata=metadata,
            conn=conn,
        )

