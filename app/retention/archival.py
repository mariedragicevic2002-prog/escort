from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from app.queue.status import QueueDirection, QueueStatus
from app.retention.policy import QueueArchivalRetentionPolicy


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _parse_utc(raw_value: Any) -> datetime:
    text = _safe_text(raw_value)
    if not text:
        return _utc_now()
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return _utc_now()
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class QueueArchivalRepository(Protocol):
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
        ...


@dataclass(frozen=True)
class QueueArchivalCommand:
    actor: str
    reason: str
    batch_limit: int | None = None
    requested_at: str = field(default_factory=lambda: _utc_now().isoformat())


@dataclass(frozen=True)
class QueueArchivalDecision:
    direction: str
    status: str
    older_than: str
    requested_limit: int
    archived: int
    reason: str | None = None


@dataclass(frozen=True)
class QueueArchivalException:
    direction: str
    status: str
    message: str


@dataclass(frozen=True)
class QueueArchivalResult:
    requested_limit: int
    bounded_limit: int
    archived_total: int
    decisions: tuple[QueueArchivalDecision, ...]
    exceptions: tuple[QueueArchivalException, ...]


class QueueArchivalSafetyError(ValueError):
    """Raised when archival requests violate bounded safety requirements."""


class QueueArchivalService:
    def __init__(
        self,
        *,
        inbound_repository: QueueArchivalRepository,
        outbound_repository: QueueArchivalRepository,
        policy: QueueArchivalRetentionPolicy | None = None,
    ) -> None:
        self._inbound_repository = inbound_repository
        self._outbound_repository = outbound_repository
        self._policy = policy or QueueArchivalRetentionPolicy.from_env()

    def execute(self, command: QueueArchivalCommand, *, conn: Any | None = None) -> QueueArchivalResult:
        actor = _safe_text(command.actor)
        reason = _safe_text(command.reason)
        if not actor:
            raise QueueArchivalSafetyError("actor is required")
        if not reason:
            raise QueueArchivalSafetyError("reason is required")

        bounded_limit = self._policy.bounded_batch_limit(command.batch_limit)
        requested_limit = bounded_limit if command.batch_limit is None else max(1, int(command.batch_limit))
        now = _parse_utc(command.requested_at)
        remaining = bounded_limit
        archived_total = 0
        decisions: list[QueueArchivalDecision] = []
        exceptions: list[QueueArchivalException] = []

        specs: tuple[tuple[str, QueueArchivalRepository, str], ...] = (
            (QueueDirection.INBOUND, self._inbound_repository, QueueStatus.SENT),
            (QueueDirection.OUTBOUND, self._outbound_repository, QueueStatus.SENT),
            (QueueDirection.INBOUND, self._inbound_repository, QueueStatus.DEAD),
            (QueueDirection.OUTBOUND, self._outbound_repository, QueueStatus.DEAD),
        )
        for direction, repository, status in specs:
            older_than = self._policy.cutoff_iso(status=status, now=now)
            current_limit = max(0, remaining)
            if current_limit == 0:
                decisions.append(
                    QueueArchivalDecision(
                        direction=direction,
                        status=status,
                        older_than=older_than,
                        requested_limit=0,
                        archived=0,
                        reason="bounded_batch_limit_exhausted",
                    )
                )
                continue
            try:
                archived = int(
                    repository.archive_processed_records(
                        status=status,
                        older_than=older_than,
                        limit=current_limit,
                        archived_by=actor,
                        archive_reason=reason,
                        conn=conn,
                    )
                    or 0
                )
                archived = max(0, min(current_limit, archived))
            except Exception as exc:
                archived = 0
                exceptions.append(
                    QueueArchivalException(direction=direction, status=status, message=f"{type(exc).__name__}: {exc}")
                )
                decisions.append(
                    QueueArchivalDecision(
                        direction=direction,
                        status=status,
                        older_than=older_than,
                        requested_limit=current_limit,
                        archived=0,
                        reason="archive_failed",
                    )
                )
                continue
            archived_total += archived
            remaining = max(0, remaining - archived)
            decisions.append(
                QueueArchivalDecision(
                    direction=direction,
                    status=status,
                    older_than=older_than,
                    requested_limit=current_limit,
                    archived=archived,
                )
            )

        return QueueArchivalResult(
            requested_limit=requested_limit,
            bounded_limit=bounded_limit,
            archived_total=archived_total,
            decisions=tuple(decisions),
            exceptions=tuple(exceptions),
        )
