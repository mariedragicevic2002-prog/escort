from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import os
from typing import Any, Mapping

from app.queue.status import QueueStatus, canonical_status

_DEFAULT_SENT_TTL_SECONDS = 7 * 24 * 60 * 60
_DEFAULT_DEAD_TTL_SECONDS = 7 * 24 * 60 * 60
_DEFAULT_REPLAY_WINDOW_SECONDS = 14 * 24 * 60 * 60
_DEFAULT_AUDIT_WINDOW_SECONDS = 30 * 24 * 60 * 60
_DEFAULT_MAX_BATCH_SIZE = 200


def _safe_positive_int(value: Any, *, default: int, minimum: int = 1, maximum: int = 3650 * 24 * 60 * 60) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(minimum, min(maximum, parsed))


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class QueueArchivalRetentionPolicy:
    sent_ttl_seconds: int = _DEFAULT_SENT_TTL_SECONDS
    dead_ttl_seconds: int = _DEFAULT_DEAD_TTL_SECONDS
    replay_window_seconds: int = _DEFAULT_REPLAY_WINDOW_SECONDS
    audit_window_seconds: int = _DEFAULT_AUDIT_WINDOW_SECONDS
    max_batch_size: int = _DEFAULT_MAX_BATCH_SIZE

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "sent_ttl_seconds",
            _safe_positive_int(self.sent_ttl_seconds, default=_DEFAULT_SENT_TTL_SECONDS, minimum=60),
        )
        object.__setattr__(
            self,
            "dead_ttl_seconds",
            _safe_positive_int(self.dead_ttl_seconds, default=_DEFAULT_DEAD_TTL_SECONDS, minimum=60),
        )
        object.__setattr__(
            self,
            "replay_window_seconds",
            _safe_positive_int(self.replay_window_seconds, default=_DEFAULT_REPLAY_WINDOW_SECONDS, minimum=60),
        )
        object.__setattr__(
            self,
            "audit_window_seconds",
            _safe_positive_int(self.audit_window_seconds, default=_DEFAULT_AUDIT_WINDOW_SECONDS, minimum=60),
        )
        object.__setattr__(
            self,
            "max_batch_size",
            _safe_positive_int(self.max_batch_size, default=_DEFAULT_MAX_BATCH_SIZE, minimum=1, maximum=10000),
        )

    @classmethod
    def from_env(cls, *, env: Mapping[str, str] | None = None) -> QueueArchivalRetentionPolicy:
        source_env = env or os.environ
        return cls(
            sent_ttl_seconds=_safe_positive_int(
                source_env.get("REFACTOR_QUEUE_RETENTION_SENT_TTL_SECONDS"),
                default=_DEFAULT_SENT_TTL_SECONDS,
                minimum=60,
            ),
            dead_ttl_seconds=_safe_positive_int(
                source_env.get("REFACTOR_QUEUE_RETENTION_DEAD_TTL_SECONDS"),
                default=_DEFAULT_DEAD_TTL_SECONDS,
                minimum=60,
            ),
            replay_window_seconds=_safe_positive_int(
                source_env.get("REFACTOR_QUEUE_RETENTION_REPLAY_WINDOW_SECONDS"),
                default=_DEFAULT_REPLAY_WINDOW_SECONDS,
                minimum=60,
            ),
            audit_window_seconds=_safe_positive_int(
                source_env.get("REFACTOR_QUEUE_RETENTION_AUDIT_WINDOW_SECONDS"),
                default=_DEFAULT_AUDIT_WINDOW_SECONDS,
                minimum=60,
            ),
            max_batch_size=_safe_positive_int(
                source_env.get("REFACTOR_QUEUE_RETENTION_MAX_BATCH_SIZE"),
                default=_DEFAULT_MAX_BATCH_SIZE,
                minimum=1,
                maximum=10000,
            ),
        )

    def effective_ttl_seconds(self, status: str) -> int:
        canonical = canonical_status(status)
        if canonical == QueueStatus.SENT:
            return max(self.sent_ttl_seconds, self.audit_window_seconds)
        if canonical == QueueStatus.DEAD:
            return max(self.dead_ttl_seconds, self.replay_window_seconds, self.audit_window_seconds)
        raise ValueError(f"Unsupported archival status: {status!r}")

    def cutoff_at(self, *, status: str, now: datetime | None = None) -> datetime:
        clock = now or _utc_now()
        return clock - timedelta(seconds=self.effective_ttl_seconds(status))

    def cutoff_iso(self, *, status: str, now: datetime | None = None) -> str:
        return self.cutoff_at(status=status, now=now).isoformat()

    def bounded_batch_limit(self, requested_limit: int | None) -> int:
        if requested_limit is None:
            return self.max_batch_size
        return min(self.max_batch_size, _safe_positive_int(requested_limit, default=self.max_batch_size, minimum=1))
