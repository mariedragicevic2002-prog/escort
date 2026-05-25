from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import re
from typing import Any, Protocol
from collections.abc import Mapping


@dataclass(frozen=True)
class TransitionHistoryRecord:
    conversation_id: str
    from_state: str
    to_state: str
    version_before: int
    version_after: int
    transitioned_at: datetime
    actor: str
    source: str
    correlation_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


class AppendOnlyTransitionHistoryRepository(Protocol):
    def append(self, record: TransitionHistoryRecord, *, conn: Any | None = None) -> None:
        ...


class SupportsTransitionMetadataRecord(Protocol):
    phone_number: str
    from_state: str
    to_state: str
    expected_version: int
    committed_version: int
    metadata: Mapping[str, Any]


def _coerce_datetime(value: Any | None) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value
        return value.replace(tzinfo=UTC)
    if isinstance(value, str):
        raw = value.strip()
        if raw:
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            try:
                parsed = datetime.fromisoformat(raw)
                if parsed.tzinfo is None:
                    return parsed.replace(tzinfo=UTC)
                return parsed
            except ValueError:
                pass
    return datetime.now(UTC)


def build_transition_history_record(record: SupportsTransitionMetadataRecord) -> TransitionHistoryRecord:
    metadata = dict(record.metadata or {})
    conversation_id = str(metadata.get("conversation_id") or record.phone_number)
    actor = str(metadata.get("actor") or "system")
    source = str(metadata.get("source") or "transition_service")
    correlation_raw = metadata.get("correlation_id") or metadata.get("request_id")
    correlation_id = str(correlation_raw) if correlation_raw not in {None, ""} else None
    transitioned_at = _coerce_datetime(metadata.get("timestamp") or metadata.get("transitioned_at"))
    return TransitionHistoryRecord(
        conversation_id=conversation_id,
        from_state=record.from_state,
        to_state=record.to_state,
        version_before=int(record.expected_version),
        version_after=int(record.committed_version),
        transitioned_at=transitioned_at,
        actor=actor,
        source=source,
        correlation_id=correlation_id,
        metadata=metadata,
    )


class DbTransitionHistoryRepository:
    """Append-only Postgres adapter for immutable transition history."""

    _SAFE_TABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

    def __init__(self, db: Any, *, table_name: str = "conversation_transition_history") -> None:
        self._db = db
        if not self._SAFE_TABLE.match(table_name):
            raise ValueError("Unsafe table name for transition history repository")
        self._table_name = table_name

    def append(self, record: TransitionHistoryRecord, *, conn: Any | None = None) -> None:
        if self._db is None:
            return
        metadata_json = json.dumps(dict(record.metadata or {}), default=str, sort_keys=True)
        self._db.execute_query(
            f"""
            INSERT INTO {self._table_name} (
                conversation_id,
                from_state,
                to_state,
                version_before,
                version_after,
                transitioned_at,
                actor,
                source,
                correlation_id,
                metadata
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                record.conversation_id,
                record.from_state,
                record.to_state,
                record.version_before,
                record.version_after,
                record.transitioned_at,
                record.actor,
                record.source,
                record.correlation_id,
                metadata_json,
            ),
            fetch=False,
            conn=conn,
        )
