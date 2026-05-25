from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from app.queue.metadata import QueueMessageMetadata
from app.queue.status import QueueStatus, canonical_status


def _coerce_map(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): inner for key, inner in value.items()}
    return {}


def _coerce_text(value: Any) -> str | None:
    if value in {None, ""}:
        return None
    return str(value)


@dataclass(frozen=True)
class InboundQueueEnvelope:
    message_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    metadata: QueueMessageMetadata = field(default_factory=QueueMessageMetadata)
    status: str = QueueStatus.PENDING
    max_attempts: int = 5

    @property
    def normalized_dedup_key(self) -> str:
        return self.metadata.normalized_dedup_key(self.message_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "payload": dict(self.payload),
            "metadata": self.metadata.to_dict(),
            "status": canonical_status(self.status),
            "max_attempts": max(1, int(self.max_attempts)),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> InboundQueueEnvelope:
        data = dict(raw or {})
        return cls(
            message_id=str(data.get("message_id") or ""),
            payload=_coerce_map(data.get("payload")),
            metadata=QueueMessageMetadata.from_dict(data.get("metadata")),
            status=canonical_status(str(data.get("status") or QueueStatus.PENDING)),
            max_attempts=max(1, int(data.get("max_attempts") or 5)),
        )


@dataclass(frozen=True)
class InboundQueueRecord(InboundQueueEnvelope):
    last_error: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        payload["last_error"] = self.last_error
        payload["created_at"] = self.created_at
        payload["updated_at"] = self.updated_at
        return payload

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> InboundQueueRecord:
        envelope = InboundQueueEnvelope.from_dict(raw)
        data = dict(raw or {})
        return cls(
            message_id=envelope.message_id,
            payload=envelope.payload,
            metadata=envelope.metadata,
            status=envelope.status,
            max_attempts=envelope.max_attempts,
            last_error=_coerce_text(data.get("last_error")),
            created_at=_coerce_text(data.get("created_at")),
            updated_at=_coerce_text(data.get("updated_at")),
        )

