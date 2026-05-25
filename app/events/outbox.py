from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
import json
import logging
import threading
from typing import Any, Mapping, Protocol

logger = logging.getLogger("adella_chatbot.refactor.outbox")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class OutboxStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"
    PUBLISHED = "published"


@dataclass(frozen=True)
class OutboxEventEnvelope:
    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None
    occurred_at: str = field(default_factory=_utc_now_iso)
    max_retries: int = 5

    @property
    def normalized_idempotency_key(self) -> str:
        value = (self.idempotency_key or self.event_id).strip()
        return value or self.event_id


@dataclass(frozen=True)
class OutboxEventRecord:
    event_id: str
    idempotency_key: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    payload: Mapping[str, Any]
    metadata: Mapping[str, Any]
    status: str
    retry_count: int
    max_retries: int
    next_retry_at: str | None
    processing_started_at: str | None
    last_attempt_at: str | None
    last_error: str | None
    last_error_at: str | None
    dead_lettered_at: str | None
    occurred_at: str
    created_at: str | None
    updated_at: str | None


class OutboxRepository(Protocol):
    def append_event(self, event: OutboxEventEnvelope, *, conn: Any | None = None) -> bool:
        ...

    def mark_processing(self, event_id: str, *, conn: Any | None = None) -> bool:
        ...

    def mark_failure(
        self,
        event_id: str,
        *,
        error_message: str,
        retry_delay_seconds: int = 0,
        conn: Any | None = None,
    ) -> bool:
        ...

    def get_event(self, event_id: str, *, conn: Any | None = None) -> OutboxEventRecord | None:
        ...

    def list_pending(
        self,
        *,
        limit: int = 100,
        conn: Any | None = None,
    ) -> list[OutboxEventRecord]:
        ...

    def mark_published(self, event_id: str, *, conn: Any | None = None) -> bool:
        ...

    def list_dead(
        self,
        *,
        limit: int = 100,
        conn: Any | None = None,
    ) -> list[OutboxEventRecord]:
        ...

    def replay_dead(
        self,
        event_id: str,
        *,
        replay_metadata: Mapping[str, Any],
        conn: Any | None = None,
    ) -> bool:
        ...


_CREATE_OUTBOX_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS refactor_outbox_events (
    event_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    event_type VARCHAR(120) NOT NULL,
    aggregate_type VARCHAR(80) NOT NULL,
    aggregate_id VARCHAR(80) NOT NULL,
    payload JSONB NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(20) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'failed', 'dead_letter', 'published')),
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 5 CHECK (max_retries >= 0),
    next_retry_at TIMESTAMPTZ,
    processing_started_at TIMESTAMPTZ,
    last_attempt_at TIMESTAMPTZ,
    last_error TEXT,
    last_error_at TIMESTAMPTZ,
    dead_lettered_at TIMESTAMPTZ,
    occurred_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

_CREATE_OUTBOX_STATUS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_refactor_outbox_status_retry
ON refactor_outbox_events (status, next_retry_at, created_at);
"""

_CREATE_OUTBOX_AGGREGATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_refactor_outbox_aggregate
ON refactor_outbox_events (aggregate_type, aggregate_id, created_at DESC);
"""

_CREATE_OUTBOX_ARCHIVE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS refactor_outbox_events_archive (
    event_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    event_type VARCHAR(120) NOT NULL,
    aggregate_type VARCHAR(80) NOT NULL,
    aggregate_id VARCHAR(80) NOT NULL,
    payload JSONB NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(20) NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 5 CHECK (max_retries >= 0),
    next_retry_at TIMESTAMPTZ,
    processing_started_at TIMESTAMPTZ,
    last_attempt_at TIMESTAMPTZ,
    last_error TEXT,
    last_error_at TIMESTAMPTZ,
    dead_lettered_at TIMESTAMPTZ,
    occurred_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ,
    archived_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    archive_reason TEXT,
    archived_by TEXT
);
"""

_CREATE_OUTBOX_ARCHIVE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_refactor_outbox_archive_status_archived_at
ON refactor_outbox_events_archive (status, archived_at DESC);
"""


def _json_map(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), sort_keys=True, default=str)


def _coerce_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _coerce_datetime_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _coerce_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): inner for key, inner in value.items()}
    return {}


def _coerce_replay_history(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    history: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, Mapping):
            history.append({str(key): inner for key, inner in item.items()})
    return history


def _merge_replay_metadata(
    metadata: Mapping[str, Any] | None,
    replay_metadata: Mapping[str, Any],
) -> dict[str, Any]:
    merged = _coerce_mapping(metadata)
    replay_entry = _coerce_mapping(replay_metadata)
    if not replay_entry:
        return merged
    history = _coerce_replay_history(merged.get("dlq_replay_history"))
    history.append(replay_entry)
    merged["dlq_replay"] = replay_entry
    merged["dlq_replay_history"] = history[-20:]
    merged["dlq_replay_count"] = max(0, int(merged.get("dlq_replay_count") or 0)) + 1
    return merged


class DatabaseOutboxRepository:
    """Production outbox adapter backed by services.database_service.DatabaseService."""

    def __init__(self, db_service: Any) -> None:
        self._db_service = db_service
        self._schema_ready = False
        self._schema_lock = threading.Lock()

    def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            if not hasattr(self._db_service, "execute_query"):
                raise RuntimeError("Outbox repository requires db_service.execute_query")
            self._db_service.execute_query(_CREATE_OUTBOX_TABLE_SQL, fetch=False)
            self._db_service.execute_query(_CREATE_OUTBOX_STATUS_INDEX_SQL, fetch=False)
            self._db_service.execute_query(_CREATE_OUTBOX_AGGREGATE_INDEX_SQL, fetch=False)
            self._db_service.execute_query(_CREATE_OUTBOX_ARCHIVE_TABLE_SQL, fetch=False)
            self._db_service.execute_query(_CREATE_OUTBOX_ARCHIVE_INDEX_SQL, fetch=False)
            self._schema_ready = True

    def append_event(self, event: OutboxEventEnvelope, *, conn: Any | None = None) -> bool:
        self.ensure_schema()
        rows = self._db_service.execute_query(
            """
            INSERT INTO refactor_outbox_events (
                event_id,
                idempotency_key,
                event_type,
                aggregate_type,
                aggregate_id,
                payload,
                metadata,
                occurred_at,
                max_retries
            )
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::timestamptz, %s)
            ON CONFLICT DO NOTHING
            RETURNING event_id
            """,
            (
                event.event_id,
                event.normalized_idempotency_key,
                event.event_type,
                event.aggregate_type,
                event.aggregate_id,
                _json_map(event.payload),
                _json_map(event.metadata),
                event.occurred_at,
                max(0, int(event.max_retries)),
            ),
            fetch=True,
            conn=conn,
        )
        inserted = bool(rows)
        if not inserted:
            logger.debug("outbox append skipped duplicate event_id=%s", event.event_id)
        return inserted

    def mark_processing(self, event_id: str, *, conn: Any | None = None) -> bool:
        self.ensure_schema()
        rows = self._db_service.execute_query(
            """
            UPDATE refactor_outbox_events
               SET status = 'processing',
                   processing_started_at = COALESCE(processing_started_at, NOW()),
                   last_attempt_at = NOW(),
                   updated_at = NOW()
             WHERE event_id = %s
               AND status IN ('pending', 'failed')
               AND (next_retry_at IS NULL OR next_retry_at <= NOW())
             RETURNING event_id
            """,
            (event_id,),
            fetch=True,
            conn=conn,
        )
        return bool(rows)

    def recover_stale_processing(
        self,
        event_id: str,
        *,
        error_message: str = "worker supervision lease expired",
        retry_delay_seconds: int = 0,
        conn: Any | None = None,
    ) -> bool:
        self.ensure_schema()
        delay_seconds = max(0, int(retry_delay_seconds))
        rows = self._db_service.execute_query(
            """
            UPDATE refactor_outbox_events
               SET retry_count = retry_count + 1,
                   status = CASE
                                WHEN retry_count + 1 >= max_retries THEN 'dead_letter'
                                ELSE 'failed'
                            END,
                   next_retry_at = CASE
                                       WHEN retry_count + 1 >= max_retries THEN NULL
                                       WHEN %s > 0 THEN NOW() + (%s || ' seconds')::interval
                                       ELSE NOW()
                                   END,
                   processing_started_at = NULL,
                   last_error = COALESCE(%s, last_error),
                   last_error_at = NOW(),
                   dead_lettered_at = CASE
                                          WHEN retry_count + 1 >= max_retries THEN COALESCE(dead_lettered_at, NOW())
                                          ELSE dead_lettered_at
                                      END,
                   updated_at = NOW()
              WHERE event_id = %s
                AND status = 'processing'
              RETURNING event_id
            """,
            (delay_seconds, delay_seconds, error_message, event_id),
            fetch=True,
            conn=conn,
        )
        return bool(rows)

    def mark_failure(
        self,
        event_id: str,
        *,
        error_message: str,
        retry_delay_seconds: int = 0,
        conn: Any | None = None,
    ) -> bool:
        self.ensure_schema()
        delay_seconds = max(0, int(retry_delay_seconds))
        rows = self._db_service.execute_query(
            """
            UPDATE refactor_outbox_events
               SET retry_count = retry_count + 1,
                   last_error = %s,
                   last_error_at = NOW(),
                   status = CASE
                                WHEN retry_count + 1 >= max_retries THEN 'dead_letter'
                                ELSE 'failed'
                            END,
                   next_retry_at = CASE
                                       WHEN retry_count + 1 >= max_retries THEN NULL
                                       WHEN %s > 0 THEN NOW() + (%s || ' seconds')::interval
                                       ELSE NOW()
                                   END,
                   dead_lettered_at = CASE
                                          WHEN retry_count + 1 >= max_retries THEN NOW()
                                          ELSE dead_lettered_at
                                      END,
                   updated_at = NOW()
             WHERE event_id = %s
               AND status IN ('pending', 'processing')
             RETURNING event_id
            """,
            (error_message, delay_seconds, delay_seconds, event_id),
            fetch=True,
            conn=conn,
        )
        return bool(rows)

    def mark_published(self, event_id: str, *, conn: Any | None = None) -> bool:
        self.ensure_schema()
        rows = self._db_service.execute_query(
            """
            UPDATE refactor_outbox_events
               SET status = 'published',
                   next_retry_at = NULL,
                   last_error = NULL,
                   last_error_at = NULL,
                   updated_at = NOW()
             WHERE event_id = %s
               AND status IN ('processing', 'published')
             RETURNING event_id
            """,
            (event_id,),
            fetch=True,
            conn=conn,
        )
        return bool(rows)

    def get_event(self, event_id: str, *, conn: Any | None = None) -> OutboxEventRecord | None:
        self.ensure_schema()
        rows = self._db_service.execute_query(
            """
            SELECT
                event_id,
                idempotency_key,
                event_type,
                aggregate_type,
                aggregate_id,
                payload,
                metadata,
                status,
                retry_count,
                max_retries,
                next_retry_at,
                processing_started_at,
                last_attempt_at,
                last_error,
                last_error_at,
                dead_lettered_at,
                occurred_at,
                created_at,
                updated_at
            FROM refactor_outbox_events
            WHERE event_id = %s
            """,
            (event_id,),
            fetch=True,
            conn=conn,
        )
        if not rows:
            return None
        return _build_outbox_record(rows[0])

    def list_pending(
        self,
        *,
        limit: int = 100,
        conn: Any | None = None,
    ) -> list[OutboxEventRecord]:
        self.ensure_schema()
        rows = self._db_service.execute_query(
            """
            SELECT
                event_id,
                idempotency_key,
                event_type,
                aggregate_type,
                aggregate_id,
                payload,
                metadata,
                status,
                retry_count,
                max_retries,
                next_retry_at,
                processing_started_at,
                last_attempt_at,
                last_error,
                last_error_at,
                dead_lettered_at,
                occurred_at,
                created_at,
                updated_at
            FROM refactor_outbox_events
            WHERE status IN ('pending', 'failed')
              AND (next_retry_at IS NULL OR next_retry_at <= NOW())
            ORDER BY created_at ASC
            LIMIT %s
            """,
            (max(1, int(limit)),),
            fetch=True,
            conn=conn,
        )
        return [_build_outbox_record(row) for row in rows or []]

    def list_dead(
        self,
        *,
        limit: int = 100,
        conn: Any | None = None,
    ) -> list[OutboxEventRecord]:
        self.ensure_schema()
        rows = self._db_service.execute_query(
            """
            SELECT
                event_id,
                idempotency_key,
                event_type,
                aggregate_type,
                aggregate_id,
                payload,
                metadata,
                status,
                retry_count,
                max_retries,
                next_retry_at,
                processing_started_at,
                last_attempt_at,
                last_error,
                last_error_at,
                dead_lettered_at,
                occurred_at,
                created_at,
                updated_at
            FROM refactor_outbox_events
            WHERE status = 'dead_letter'
            ORDER BY COALESCE(dead_lettered_at, updated_at, created_at) ASC, created_at ASC
            LIMIT %s
            """,
            (max(1, int(limit)),),
            fetch=True,
            conn=conn,
        )
        return [_build_outbox_record(row) for row in rows or []]

    def replay_dead(
        self,
        event_id: str,
        *,
        replay_metadata: Mapping[str, Any],
        conn: Any | None = None,
    ) -> bool:
        self.ensure_schema()
        current = self.get_event(event_id, conn=conn)
        if current is None or current.status != OutboxStatus.DEAD_LETTER:
            return False
        merged_metadata = _merge_replay_metadata(current.metadata, replay_metadata)
        rows = self._db_service.execute_query(
            """
            UPDATE refactor_outbox_events
               SET status = 'pending',
                   retry_count = 0,
                   next_retry_at = NULL,
                   processing_started_at = NULL,
                   last_attempt_at = NULL,
                   last_error = NULL,
                   last_error_at = NULL,
                   metadata = %s::jsonb,
                   updated_at = NOW()
             WHERE event_id = %s
               AND status = 'dead_letter'
             RETURNING event_id
            """,
            (_json_map(merged_metadata), event_id),
            fetch=True,
            conn=conn,
        )
        return bool(rows)

    def archive_events(
        self,
        *,
        status: str,
        older_than: str,
        limit: int = 100,
        archived_by: str | None = None,
        archive_reason: str | None = None,
        conn: Any | None = None,
    ) -> int:
        self.ensure_schema()
        normalized_status = str(status or "").strip().lower()
        if normalized_status not in {OutboxStatus.PUBLISHED, OutboxStatus.DEAD_LETTER}:
            raise ValueError("status must be one of: published, dead_letter")
        bounded_limit = max(1, int(limit))
        rows = self._db_service.execute_query(
            """
            WITH candidates AS (
                SELECT
                    event_id,
                    idempotency_key,
                    event_type,
                    aggregate_type,
                    aggregate_id,
                    payload,
                    metadata,
                    status,
                    retry_count,
                    max_retries,
                    next_retry_at,
                    processing_started_at,
                    last_attempt_at,
                    last_error,
                    last_error_at,
                    dead_lettered_at,
                    occurred_at,
                    created_at,
                    updated_at
                FROM refactor_outbox_events
                WHERE status = %s
                  AND COALESCE(dead_lettered_at, updated_at, created_at, occurred_at) <= %s::timestamptz
                ORDER BY COALESCE(dead_lettered_at, updated_at, created_at, occurred_at) ASC, event_id ASC
                LIMIT %s
            ),
            archived AS (
                INSERT INTO refactor_outbox_events_archive (
                    event_id,
                    idempotency_key,
                    event_type,
                    aggregate_type,
                    aggregate_id,
                    payload,
                    metadata,
                    status,
                    retry_count,
                    max_retries,
                    next_retry_at,
                    processing_started_at,
                    last_attempt_at,
                    last_error,
                    last_error_at,
                    dead_lettered_at,
                    occurred_at,
                    created_at,
                    updated_at,
                    archived_at,
                    archive_reason,
                    archived_by
                )
                SELECT
                    event_id,
                    idempotency_key,
                    event_type,
                    aggregate_type,
                    aggregate_id,
                    payload,
                    metadata,
                    status,
                    retry_count,
                    max_retries,
                    next_retry_at,
                    processing_started_at,
                    last_attempt_at,
                    last_error,
                    last_error_at,
                    dead_lettered_at,
                    occurred_at,
                    created_at,
                    updated_at,
                    NOW(),
                    %s,
                    %s
                FROM candidates
                ON CONFLICT (event_id) DO NOTHING
                RETURNING event_id
            )
            DELETE FROM refactor_outbox_events AS active
            USING archived
            WHERE active.event_id = archived.event_id
            RETURNING active.event_id
            """,
            (normalized_status, older_than, bounded_limit, archive_reason, archived_by),
            fetch=True,
            conn=conn,
        )
        return len(rows or [])


def _build_outbox_record(raw_row: Any) -> OutboxEventRecord:
    row = raw_row if isinstance(raw_row, Mapping) else dict(raw_row)
    return OutboxEventRecord(
        event_id=str(row.get("event_id") or ""),
        idempotency_key=str(row.get("idempotency_key") or ""),
        event_type=str(row.get("event_type") or ""),
        aggregate_type=str(row.get("aggregate_type") or ""),
        aggregate_id=str(row.get("aggregate_id") or ""),
        payload=_coerce_dict(row.get("payload")),
        metadata=_coerce_dict(row.get("metadata")),
        status=str(row.get("status") or ""),
        retry_count=int(row.get("retry_count") or 0),
        max_retries=int(row.get("max_retries") or 0),
        next_retry_at=_coerce_datetime_text(row.get("next_retry_at")),
        processing_started_at=_coerce_datetime_text(row.get("processing_started_at")),
        last_attempt_at=_coerce_datetime_text(row.get("last_attempt_at")),
        last_error=(None if row.get("last_error") is None else str(row.get("last_error"))),
        last_error_at=_coerce_datetime_text(row.get("last_error_at")),
        dead_lettered_at=_coerce_datetime_text(row.get("dead_lettered_at")),
        occurred_at=str(row.get("occurred_at") or ""),
        created_at=_coerce_datetime_text(row.get("created_at")),
        updated_at=_coerce_datetime_text(row.get("updated_at")),
    )
