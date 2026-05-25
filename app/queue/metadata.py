from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_text(value: Any) -> str | None:
    if value in {None, ""}:
        return None
    return str(value)


def _coerce_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _coerce_attributes(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): inner for key, inner in value.items()}
    return {}


@dataclass(frozen=True)
class QueueMessageMetadata:
    attempt: int = 0
    dedup_key: str | None = None
    correlation_id: str | None = None
    request_id: str | None = None
    enqueued_at: str = field(default_factory=utc_now_iso)
    available_at: str | None = None
    processing_started_at: str | None = None
    last_attempt_at: str | None = None
    completed_at: str | None = None
    dead_lettered_at: str | None = None
    last_error_at: str | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def normalized_dedup_key(self, fallback_key: str) -> str:
        fallback = str(fallback_key or "").strip()
        if not fallback:
            raise ValueError("fallback_key is required")
        value = str(self.dedup_key or fallback).strip()
        return value or fallback

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt": max(0, int(self.attempt)),
            "dedup_key": self.dedup_key,
            "correlation_id": self.correlation_id,
            "request_id": self.request_id,
            "enqueued_at": self.enqueued_at,
            "available_at": self.available_at,
            "processing_started_at": self.processing_started_at,
            "last_attempt_at": self.last_attempt_at,
            "completed_at": self.completed_at,
            "dead_lettered_at": self.dead_lettered_at,
            "last_error_at": self.last_error_at,
            "attributes": dict(self.attributes),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> QueueMessageMetadata:
        data = dict(raw or {})
        return cls(
            attempt=max(0, _coerce_int(data.get("attempt"), default=0)),
            dedup_key=_coerce_text(data.get("dedup_key")),
            correlation_id=_coerce_text(data.get("correlation_id")),
            request_id=_coerce_text(data.get("request_id")),
            enqueued_at=_coerce_text(data.get("enqueued_at")) or utc_now_iso(),
            available_at=_coerce_text(data.get("available_at")),
            processing_started_at=_coerce_text(data.get("processing_started_at")),
            last_attempt_at=_coerce_text(data.get("last_attempt_at")),
            completed_at=_coerce_text(data.get("completed_at")),
            dead_lettered_at=_coerce_text(data.get("dead_lettered_at")),
            last_error_at=_coerce_text(data.get("last_error_at")),
            attributes=_coerce_attributes(data.get("attributes")),
        )

