from __future__ import annotations

from app.events.outbox import OutboxStatus


class QueueDirection:
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class QueueStatus:
    PENDING = "pending"
    PROCESSING = "processing"
    RETRY = "retry"
    DEAD = "dead"
    SENT = "sent"


_STATUS_ALIASES = {
    QueueStatus.PENDING: QueueStatus.PENDING,
    QueueStatus.PROCESSING: QueueStatus.PROCESSING,
    QueueStatus.RETRY: QueueStatus.RETRY,
    QueueStatus.DEAD: QueueStatus.DEAD,
    QueueStatus.SENT: QueueStatus.SENT,
    OutboxStatus.PENDING: QueueStatus.PENDING,
    OutboxStatus.PROCESSING: QueueStatus.PROCESSING,
    OutboxStatus.FAILED: QueueStatus.RETRY,
    OutboxStatus.DEAD_LETTER: QueueStatus.DEAD,
    OutboxStatus.PUBLISHED: QueueStatus.SENT,
}


def canonical_status(status: str) -> str:
    normalized = str(status or "").strip().lower()
    return _STATUS_ALIASES.get(normalized, normalized)


_ALLOWED_TRANSITIONS = {
    QueueDirection.INBOUND: {
        QueueStatus.PENDING: {QueueStatus.PROCESSING, QueueStatus.DEAD},
        QueueStatus.PROCESSING: {QueueStatus.RETRY, QueueStatus.DEAD, QueueStatus.SENT},
        QueueStatus.RETRY: {QueueStatus.PROCESSING, QueueStatus.DEAD},
        QueueStatus.DEAD: {QueueStatus.PENDING},
        QueueStatus.SENT: set(),
    },
    QueueDirection.OUTBOUND: {
        QueueStatus.PENDING: {QueueStatus.PROCESSING, QueueStatus.DEAD},
        QueueStatus.PROCESSING: {QueueStatus.RETRY, QueueStatus.DEAD, QueueStatus.SENT},
        QueueStatus.RETRY: {QueueStatus.PROCESSING, QueueStatus.DEAD},
        QueueStatus.DEAD: {QueueStatus.PENDING},
        QueueStatus.SENT: set(),
    },
}


def can_transition(current_status: str, next_status: str, *, direction: str) -> bool:
    normalized_direction = str(direction or "").strip().lower()
    if normalized_direction not in _ALLOWED_TRANSITIONS:
        return False
    current = canonical_status(current_status)
    target = canonical_status(next_status)
    allowed = _ALLOWED_TRANSITIONS[normalized_direction].get(current, set())
    return target in allowed


def resolve_retry_or_dead_status(*, attempt: int, max_attempts: int) -> str:
    safe_attempt = max(0, int(attempt))
    safe_max_attempts = max(1, int(max_attempts))
    return QueueStatus.DEAD if safe_attempt >= safe_max_attempts else QueueStatus.RETRY


_QUEUE_TO_OUTBOX_STATUS = {
    QueueStatus.PENDING: OutboxStatus.PENDING,
    QueueStatus.PROCESSING: OutboxStatus.PROCESSING,
    QueueStatus.RETRY: OutboxStatus.FAILED,
    QueueStatus.DEAD: OutboxStatus.DEAD_LETTER,
    QueueStatus.SENT: OutboxStatus.PUBLISHED,
}


def queue_to_outbox_status(status: str) -> str:
    canonical = canonical_status(status)
    if canonical not in _QUEUE_TO_OUTBOX_STATUS:
        raise ValueError(f"Unsupported queue status: {status!r}")
    return _QUEUE_TO_OUTBOX_STATUS[canonical]


def outbox_to_queue_status(status: str) -> str:
    canonical = canonical_status(status)
    if canonical in _QUEUE_TO_OUTBOX_STATUS:
        return canonical
    raise ValueError(f"Unsupported outbox status: {status!r}")
