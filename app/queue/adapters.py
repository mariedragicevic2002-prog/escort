from __future__ import annotations

import json
import logging
import threading
from typing import Any, Mapping

from app.events.outbox import (
    DatabaseOutboxRepository,
    OutboxEventEnvelope,
    OutboxEventRecord,
    OutboxRepository,
)
from app.queue.inbound import InboundQueueEnvelope, InboundQueueRecord
from app.queue.metadata import QueueMessageMetadata
from app.queue.outbound import OutboundQueueEnvelope, OutboundQueueRecord
from app.queue.status import QueueStatus, canonical_status, outbox_to_queue_status, queue_to_outbox_status

logger = logging.getLogger("adella_chatbot.refactor.queue")

_CREATE_INBOUND_QUEUE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS refactor_inbound_queue_messages (
    message_id TEXT PRIMARY KEY,
    dedup_key TEXT NOT NULL UNIQUE,
    payload JSONB NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(20) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'retry', 'dead', 'sent')),
    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5 CHECK (max_attempts >= 1),
    next_attempt_at TIMESTAMPTZ,
    processing_started_at TIMESTAMPTZ,
    last_attempt_at TIMESTAMPTZ,
    last_error TEXT,
    last_error_at TIMESTAMPTZ,
    dead_lettered_at TIMESTAMPTZ,
    received_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

_CREATE_INBOUND_QUEUE_STATUS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_refactor_inbound_queue_status_retry
ON refactor_inbound_queue_messages (status, next_attempt_at, created_at);
"""

_CREATE_INBOUND_QUEUE_DEDUP_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_refactor_inbound_queue_dedup_created
ON refactor_inbound_queue_messages (dedup_key, created_at DESC);
"""

_CREATE_INBOUND_QUEUE_ARCHIVE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS refactor_inbound_queue_messages_archive (
    message_id TEXT PRIMARY KEY,
    dedup_key TEXT NOT NULL,
    payload JSONB NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(20) NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5 CHECK (max_attempts >= 1),
    next_attempt_at TIMESTAMPTZ,
    processing_started_at TIMESTAMPTZ,
    last_attempt_at TIMESTAMPTZ,
    last_error TEXT,
    last_error_at TIMESTAMPTZ,
    dead_lettered_at TIMESTAMPTZ,
    received_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ,
    archived_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    archive_reason TEXT,
    archived_by TEXT
);
"""

_CREATE_INBOUND_QUEUE_ARCHIVE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_refactor_inbound_queue_archive_status_archived_at
ON refactor_inbound_queue_messages_archive (status, archived_at DESC);
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
    return str(value)


def _coerce_replay_history(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    history: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, Mapping):
            history.append({str(key): inner for key, inner in item.items()})
    return history


def _merge_inbound_replay_metadata(
    metadata_payload: Mapping[str, Any] | None,
    replay_metadata: Mapping[str, Any],
) -> dict[str, Any]:
    merged = _coerce_dict(metadata_payload)
    attributes = _coerce_dict(merged.get("attributes"))
    replay_entry = {str(key): value for key, value in dict(replay_metadata).items()}
    if replay_entry:
        history = _coerce_replay_history(attributes.get("dlq_replay_history"))
        history.append(replay_entry)
        attributes["dlq_replay"] = replay_entry
        attributes["dlq_replay_history"] = history[-20:]
        attributes["dlq_replay_count"] = max(0, int(attributes.get("dlq_replay_count") or 0)) + 1
    merged["attributes"] = attributes
    return merged


class DatabaseInboundQueueRepository:
    """Durable inbound queue adapter backed by services.database_service.DatabaseService."""

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
                raise RuntimeError("Inbound queue repository requires db_service.execute_query")
            self._db_service.execute_query(_CREATE_INBOUND_QUEUE_TABLE_SQL, fetch=False)
            self._db_service.execute_query(_CREATE_INBOUND_QUEUE_STATUS_INDEX_SQL, fetch=False)
            self._db_service.execute_query(_CREATE_INBOUND_QUEUE_DEDUP_INDEX_SQL, fetch=False)
            self._db_service.execute_query(_CREATE_INBOUND_QUEUE_ARCHIVE_TABLE_SQL, fetch=False)
            self._db_service.execute_query(_CREATE_INBOUND_QUEUE_ARCHIVE_INDEX_SQL, fetch=False)
            self._schema_ready = True

    def enqueue(self, envelope: InboundQueueEnvelope, *, conn: Any | None = None) -> bool:
        self.ensure_schema()
        metadata = envelope.metadata.to_dict()
        rows = self._db_service.execute_query(
            """
            INSERT INTO refactor_inbound_queue_messages (
                message_id,
                dedup_key,
                payload,
                metadata,
                status,
                max_attempts,
                received_at
            )
            VALUES (%s, %s, %s::jsonb, %s::jsonb, %s, %s, %s::timestamptz)
            ON CONFLICT DO NOTHING
            RETURNING message_id
            """,
            (
                envelope.message_id,
                envelope.normalized_dedup_key,
                _json_map(envelope.payload),
                _json_map(metadata),
                canonical_status(envelope.status),
                max(1, int(envelope.max_attempts)),
                envelope.metadata.enqueued_at,
            ),
            fetch=True,
            conn=conn,
        )
        inserted = bool(rows)
        if not inserted:
            logger.debug("inbound queue enqueue skipped duplicate message_id=%s", envelope.message_id)
        return inserted

    def mark_processing(self, message_id: str, *, conn: Any | None = None) -> bool:
        self.ensure_schema()
        rows = self._db_service.execute_query(
            """
            UPDATE refactor_inbound_queue_messages
               SET status = 'processing',
                   attempt = attempt + 1,
                   processing_started_at = COALESCE(processing_started_at, NOW()),
                   last_attempt_at = NOW(),
                   updated_at = NOW()
             WHERE message_id = %s
               AND status IN ('pending', 'retry')
               AND (next_attempt_at IS NULL OR next_attempt_at <= NOW())
             RETURNING message_id
            """,
            (message_id,),
            fetch=True,
            conn=conn,
        )
        return bool(rows)

    def mark_retry(
        self,
        message_id: str,
        *,
        error_message: str,
        retry_delay_seconds: int = 0,
        conn: Any | None = None,
    ) -> bool:
        self.ensure_schema()
        delay_seconds = max(0, int(retry_delay_seconds))
        rows = self._db_service.execute_query(
            """
            UPDATE refactor_inbound_queue_messages
               SET status = CASE
                                WHEN attempt >= max_attempts THEN 'dead'
                                ELSE 'retry'
                            END,
                   next_attempt_at = CASE
                                        WHEN attempt >= max_attempts THEN NULL
                                        WHEN %s > 0 THEN NOW() + (%s || ' seconds')::interval
                                        ELSE NOW()
                                     END,
                   last_error = %s,
                   last_error_at = NOW(),
                   dead_lettered_at = CASE
                                          WHEN attempt >= max_attempts THEN NOW()
                                          ELSE dead_lettered_at
                                      END,
                   updated_at = NOW()
             WHERE message_id = %s
               AND status IN ('pending', 'processing')
             RETURNING message_id
            """,
            (delay_seconds, delay_seconds, error_message, message_id),
            fetch=True,
            conn=conn,
        )
        return bool(rows)

    def recover_stale_processing(
        self,
        message_id: str,
        *,
        error_message: str = "worker supervision lease expired",
        retry_delay_seconds: int = 0,
        conn: Any | None = None,
    ) -> bool:
        self.ensure_schema()
        delay_seconds = max(0, int(retry_delay_seconds))
        rows = self._db_service.execute_query(
            """
            UPDATE refactor_inbound_queue_messages
               SET status = CASE
                                WHEN attempt >= max_attempts THEN 'dead'
                                ELSE 'retry'
                            END,
                   next_attempt_at = CASE
                                        WHEN attempt >= max_attempts THEN NULL
                                        WHEN %s > 0 THEN NOW() + (%s || ' seconds')::interval
                                        ELSE NOW()
                                     END,
                   processing_started_at = NULL,
                   last_error = COALESCE(%s, last_error),
                   last_error_at = NOW(),
                   dead_lettered_at = CASE
                                          WHEN attempt >= max_attempts THEN COALESCE(dead_lettered_at, NOW())
                                          ELSE dead_lettered_at
                                      END,
                   updated_at = NOW()
             WHERE message_id = %s
               AND status = 'processing'
             RETURNING message_id
            """,
            (delay_seconds, delay_seconds, error_message, message_id),
            fetch=True,
            conn=conn,
        )
        return bool(rows)

    def mark_sent(self, message_id: str, *, conn: Any | None = None) -> bool:
        self.ensure_schema()
        rows = self._db_service.execute_query(
            """
            UPDATE refactor_inbound_queue_messages
               SET status = 'sent',
                   next_attempt_at = NULL,
                   last_error = NULL,
                   last_error_at = NULL,
                   updated_at = NOW()
             WHERE message_id = %s
               AND status IN ('processing', 'sent')
             RETURNING message_id
            """,
            (message_id,),
            fetch=True,
            conn=conn,
        )
        return bool(rows)

    def mark_dead(
        self,
        message_id: str,
        *,
        error_message: str | None = None,
        conn: Any | None = None,
    ) -> bool:
        self.ensure_schema()
        rows = self._db_service.execute_query(
            """
            UPDATE refactor_inbound_queue_messages
               SET status = 'dead',
                   next_attempt_at = NULL,
                   last_error = COALESCE(%s, last_error),
                   last_error_at = CASE
                                       WHEN %s IS NULL THEN last_error_at
                                       ELSE NOW()
                                   END,
                   dead_lettered_at = COALESCE(dead_lettered_at, NOW()),
                   updated_at = NOW()
             WHERE message_id = %s
               AND status IN ('pending', 'processing', 'retry')
             RETURNING message_id
            """,
            (error_message, error_message, message_id),
            fetch=True,
            conn=conn,
        )
        return bool(rows)

    def get_message(self, message_id: str, *, conn: Any | None = None) -> InboundQueueRecord | None:
        self.ensure_schema()
        rows = self._db_service.execute_query(
            """
            SELECT
                message_id,
                dedup_key,
                payload,
                metadata,
                status,
                attempt,
                max_attempts,
                next_attempt_at,
                processing_started_at,
                last_attempt_at,
                last_error,
                last_error_at,
                dead_lettered_at,
                received_at,
                created_at,
                updated_at
            FROM refactor_inbound_queue_messages
            WHERE message_id = %s
            """,
            (message_id,),
            fetch=True,
            conn=conn,
        )
        if not rows:
            return None
        return _build_inbound_record(rows[0])

    def list_pending(self, *, limit: int = 100, conn: Any | None = None) -> list[InboundQueueRecord]:
        self.ensure_schema()
        rows = self._db_service.execute_query(
            """
            SELECT
                message_id,
                dedup_key,
                payload,
                metadata,
                status,
                attempt,
                max_attempts,
                next_attempt_at,
                processing_started_at,
                last_attempt_at,
                last_error,
                last_error_at,
                dead_lettered_at,
                received_at,
                created_at,
                updated_at
            FROM refactor_inbound_queue_messages
            WHERE status IN ('pending', 'retry')
              AND (next_attempt_at IS NULL OR next_attempt_at <= NOW())
            ORDER BY created_at ASC
            LIMIT %s
            """,
            (max(1, int(limit)),),
            fetch=True,
            conn=conn,
        )
        return [_build_inbound_record(row) for row in rows or []]

    def list_dead(self, *, limit: int = 100, conn: Any | None = None) -> list[InboundQueueRecord]:
        self.ensure_schema()
        rows = self._db_service.execute_query(
            """
            SELECT
                message_id,
                dedup_key,
                payload,
                metadata,
                status,
                attempt,
                max_attempts,
                next_attempt_at,
                processing_started_at,
                last_attempt_at,
                last_error,
                last_error_at,
                dead_lettered_at,
                received_at,
                created_at,
                updated_at
            FROM refactor_inbound_queue_messages
            WHERE status = 'dead'
            ORDER BY COALESCE(dead_lettered_at, updated_at, created_at) ASC, created_at ASC
            LIMIT %s
            """,
            (max(1, int(limit)),),
            fetch=True,
            conn=conn,
        )
        return [_build_inbound_record(row) for row in rows or []]

    def replay_dead(
        self,
        message_id: str,
        *,
        replay_metadata: Mapping[str, Any],
        conn: Any | None = None,
    ) -> bool:
        self.ensure_schema()
        current = self.get_message(message_id, conn=conn)
        if current is None or current.status != QueueStatus.DEAD:
            return False
        merged_metadata = _merge_inbound_replay_metadata(current.metadata.to_dict(), replay_metadata)
        rows = self._db_service.execute_query(
            """
            UPDATE refactor_inbound_queue_messages
               SET status = 'pending',
                   attempt = 0,
                   next_attempt_at = NULL,
                   processing_started_at = NULL,
                   last_attempt_at = NULL,
                   last_error = NULL,
                   last_error_at = NULL,
                   metadata = %s::jsonb,
                   updated_at = NOW()
             WHERE message_id = %s
               AND status = 'dead'
             RETURNING message_id
            """,
            (_json_map(merged_metadata), message_id),
            fetch=True,
            conn=conn,
        )
        return bool(rows)

    def archive_processed_records(
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
        normalized_status = canonical_status(status)
        if normalized_status not in {QueueStatus.SENT, QueueStatus.DEAD}:
            raise ValueError("status must be one of: sent, dead")
        bounded_limit = max(1, int(limit))
        rows = self._db_service.execute_query(
            """
            WITH candidates AS (
                SELECT
                    message_id,
                    dedup_key,
                    payload,
                    metadata,
                    status,
                    attempt,
                    max_attempts,
                    next_attempt_at,
                    processing_started_at,
                    last_attempt_at,
                    last_error,
                    last_error_at,
                    dead_lettered_at,
                    received_at,
                    created_at,
                    updated_at
                FROM refactor_inbound_queue_messages
                WHERE status = %s
                  AND COALESCE(dead_lettered_at, updated_at, created_at, received_at) <= %s::timestamptz
                ORDER BY COALESCE(dead_lettered_at, updated_at, created_at, received_at) ASC, message_id ASC
                LIMIT %s
            ),
            archived AS (
                INSERT INTO refactor_inbound_queue_messages_archive (
                    message_id,
                    dedup_key,
                    payload,
                    metadata,
                    status,
                    attempt,
                    max_attempts,
                    next_attempt_at,
                    processing_started_at,
                    last_attempt_at,
                    last_error,
                    last_error_at,
                    dead_lettered_at,
                    received_at,
                    created_at,
                    updated_at,
                    archived_at,
                    archive_reason,
                    archived_by
                )
                SELECT
                    message_id,
                    dedup_key,
                    payload,
                    metadata,
                    status,
                    attempt,
                    max_attempts,
                    next_attempt_at,
                    processing_started_at,
                    last_attempt_at,
                    last_error,
                    last_error_at,
                    dead_lettered_at,
                    received_at,
                    created_at,
                    updated_at,
                    NOW(),
                    %s,
                    %s
                FROM candidates
                ON CONFLICT (message_id) DO NOTHING
                RETURNING message_id
            )
            DELETE FROM refactor_inbound_queue_messages AS active
            USING archived
            WHERE active.message_id = archived.message_id
            RETURNING active.message_id
            """,
            (normalized_status, older_than, bounded_limit, archive_reason, archived_by),
            fetch=True,
            conn=conn,
        )
        return len(rows or [])


class DatabaseOutboundQueueRepository:
    """Outbound queue adapter backed by refactor outbox records."""

    def __init__(
        self,
        db_service: Any | None = None,
        *,
        outbox_repository: OutboxRepository | None = None,
    ) -> None:
        if outbox_repository is not None:
            self._outbox_repository = outbox_repository
            return
        if db_service is None:
            raise ValueError("db_service or outbox_repository is required")
        self._outbox_repository = DatabaseOutboxRepository(db_service)

    def enqueue(self, envelope: OutboundQueueEnvelope, *, conn: Any | None = None) -> bool:
        metadata_dict = envelope.metadata.to_dict()
        attributes = dict(envelope.metadata.attributes)
        if envelope.metadata.correlation_id:
            attributes.setdefault("correlation_id", envelope.metadata.correlation_id)
        if envelope.metadata.request_id:
            attributes.setdefault("request_id", envelope.metadata.request_id)
        outbox_envelope = OutboxEventEnvelope(
            event_id=envelope.message_id,
            idempotency_key=envelope.normalized_dedup_key,
            event_type=envelope.message_type,
            aggregate_type=envelope.aggregate_type,
            aggregate_id=envelope.aggregate_id,
            payload=dict(envelope.payload),
            metadata=attributes,
            occurred_at=metadata_dict.get("enqueued_at") or envelope.metadata.enqueued_at,
            max_retries=max(1, int(envelope.max_attempts)),
        )
        return self._outbox_repository.append_event(outbox_envelope, conn=conn)

    def mark_processing(self, message_id: str, *, conn: Any | None = None) -> bool:
        return self._outbox_repository.mark_processing(message_id, conn=conn)

    def mark_retry(
        self,
        message_id: str,
        *,
        error_message: str,
        retry_delay_seconds: int = 0,
        conn: Any | None = None,
    ) -> bool:
        return self._outbox_repository.mark_failure(
            message_id,
            error_message=error_message,
            retry_delay_seconds=retry_delay_seconds,
            conn=conn,
        )

    def mark_sent(self, message_id: str, *, conn: Any | None = None) -> bool:
        return self._outbox_repository.mark_published(message_id, conn=conn)

    def recover_stale_processing(
        self,
        message_id: str,
        *,
        error_message: str = "worker supervision lease expired",
        retry_delay_seconds: int = 0,
        conn: Any | None = None,
    ) -> bool:
        recover = getattr(self._outbox_repository, "recover_stale_processing", None)
        if callable(recover):
            return bool(
                recover(
                    message_id,
                    error_message=error_message,
                    retry_delay_seconds=retry_delay_seconds,
                    conn=conn,
                )
            )
        return False

    def get_message(self, message_id: str, *, conn: Any | None = None) -> OutboundQueueRecord | None:
        event = self._outbox_repository.get_event(message_id, conn=conn)
        if event is None:
            return None
        return _build_outbound_record(event)

    def list_pending(self, *, limit: int = 100, conn: Any | None = None) -> list[OutboundQueueRecord]:
        events = self._outbox_repository.list_pending(limit=limit, conn=conn)
        return [_build_outbound_record(event) for event in events]

    def list_dead(self, *, limit: int = 100, conn: Any | None = None) -> list[OutboundQueueRecord]:
        events = self._outbox_repository.list_dead(limit=limit, conn=conn)
        return [_build_outbound_record(event) for event in events]

    def replay_dead(
        self,
        message_id: str,
        *,
        replay_metadata: Mapping[str, Any],
        conn: Any | None = None,
    ) -> bool:
        return self._outbox_repository.replay_dead(
            message_id,
            replay_metadata=replay_metadata,
            conn=conn,
        )

    def archive_processed_records(
        self,
        *,
        status: str,
        older_than: str,
        limit: int = 100,
        archived_by: str | None = None,
        archive_reason: str | None = None,
        conn: Any | None = None,
    ) -> int:
        normalized_status = canonical_status(status)
        if normalized_status not in {QueueStatus.SENT, QueueStatus.DEAD}:
            raise ValueError("status must be one of: sent, dead")
        archive = getattr(self._outbox_repository, "archive_events", None)
        if not callable(archive):
            raise RuntimeError("outbox repository does not support archival operations")
        return int(
            str(
                archive(
                    status=queue_to_outbox_status(normalized_status),
                    older_than=older_than,
                    limit=max(1, int(limit)),
                    archived_by=archived_by,
                    archive_reason=archive_reason,
                    conn=conn,
                )
                or 0
            )
        )


def _build_inbound_record(raw_row: Any) -> InboundQueueRecord:
    row = raw_row if isinstance(raw_row, Mapping) else dict(raw_row)
    metadata_payload = _coerce_dict(row.get("metadata"))
    metadata = QueueMessageMetadata.from_dict(
        {
            **metadata_payload,
            "attempt": int(row.get("attempt") or 0),
            "dedup_key": str(row.get("dedup_key") or ""),
            "enqueued_at": _coerce_datetime_text(row.get("received_at")),
            "available_at": _coerce_datetime_text(row.get("next_attempt_at")),
            "processing_started_at": _coerce_datetime_text(row.get("processing_started_at")),
            "last_attempt_at": _coerce_datetime_text(row.get("last_attempt_at")),
            "completed_at": (
                _coerce_datetime_text(row.get("updated_at"))
                if canonical_status(str(row.get("status") or QueueStatus.PENDING)) == QueueStatus.SENT
                else _coerce_datetime_text(metadata_payload.get("completed_at"))
            ),
            "dead_lettered_at": _coerce_datetime_text(row.get("dead_lettered_at")),
            "last_error_at": _coerce_datetime_text(row.get("last_error_at")),
        }
    )
    return InboundQueueRecord(
        message_id=str(row.get("message_id") or ""),
        payload=_coerce_dict(row.get("payload")),
        metadata=metadata,
        status=canonical_status(str(row.get("status") or QueueStatus.PENDING)),
        max_attempts=max(1, int(row.get("max_attempts") or 1)),
        last_error=None if row.get("last_error") is None else str(row.get("last_error")),
        created_at=_coerce_datetime_text(row.get("created_at")),
        updated_at=_coerce_datetime_text(row.get("updated_at")),
    )


def _build_outbound_record(event: OutboxEventRecord) -> OutboundQueueRecord:
    base_metadata = dict(event.metadata)
    queue_status = outbox_to_queue_status(event.status)
    metadata = QueueMessageMetadata(
        attempt=max(0, int(event.retry_count)),
        dedup_key=event.idempotency_key,
        correlation_id=None if base_metadata.get("correlation_id") is None else str(base_metadata["correlation_id"]),
        request_id=None if base_metadata.get("request_id") is None else str(base_metadata["request_id"]),
        enqueued_at=event.occurred_at,
        available_at=event.next_retry_at,
        processing_started_at=event.processing_started_at,
        last_attempt_at=event.last_attempt_at,
        completed_at=(event.updated_at if queue_status == QueueStatus.SENT else None),
        dead_lettered_at=event.dead_lettered_at,
        last_error_at=event.last_error_at,
        attributes=base_metadata,
    )
    return OutboundQueueRecord(
        message_id=event.event_id,
        message_type=event.event_type,
        aggregate_type=event.aggregate_type,
        aggregate_id=event.aggregate_id,
        payload=event.payload,
        metadata=metadata,
        status=queue_status,
        max_attempts=max(1, int(event.max_retries)),
        last_error=event.last_error,
        created_at=event.created_at,
        updated_at=event.updated_at,
    )
