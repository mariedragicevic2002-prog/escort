from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import json
import logging
import re
from threading import Lock
from typing import Any, Protocol

from app.queue.providers import InboundQueueProvider, OutboundQueueProvider
from app.queue.status import QueueDirection, QueueStatus, canonical_status
from app.retention.archival import (
    QueueArchivalCommand,
    QueueArchivalResult,
    QueueArchivalService,
)
from app.security.log_scrubbing import scrub_payload_for_logging
from app.security.rbac import require_permission
from app.workers.dlq_replay import (
    DLQReplayCommand,
    DLQReplayResult,
    DLQReplaySafetyError,
    DLQReplaySelection,
    DLQReplayService,
)
from app.workers.supervision.lease import WorkerLeaseStore

logger = logging.getLogger("adella_chatbot.refactor.operator_recovery")

QUEUE_PAUSE_PERMISSION = "queue:pause"
QUEUE_INSPECT_PERMISSION = "queue:inspect"
QUEUE_ARCHIVE_PERMISSION = "queue:archive"
INBOUND_QUEUE_NAME = "refactor_inbound"
OUTBOUND_QUEUE_NAME = "refactor_outbox"

_DIRECTION_ALL = "all"
_STATUS_DEAD = "dead"
_STATUS_RETRY = "retry"
_STATUS_STALE = "stale_claim"
_ALLOWED_INSPECTION_STATUSES = {_STATUS_DEAD, _STATUS_RETRY, _STATUS_STALE}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _safe_positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(1, parsed)


def _to_iso(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    text = _safe_text(value)
    return text or None


def _parse_utc(raw_value: Any, *, fallback: datetime | None = None) -> datetime:
    text = _safe_text(raw_value)
    if not text:
        return fallback or _utc_now()
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return fallback or _utc_now()
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _resolve_direction(value: Any) -> str:
    direction = _safe_text(value).lower()
    if direction in {QueueDirection.INBOUND, INBOUND_QUEUE_NAME}:
        return QueueDirection.INBOUND
    if direction in {QueueDirection.OUTBOUND, OUTBOUND_QUEUE_NAME}:
        return QueueDirection.OUTBOUND
    return _DIRECTION_ALL


def _resolve_queue_name(value: Any) -> str:
    direction = _resolve_direction(value)
    if direction == QueueDirection.INBOUND:
        return INBOUND_QUEUE_NAME
    if direction == QueueDirection.OUTBOUND:
        return OUTBOUND_QUEUE_NAME
    queue_name = _safe_text(value)
    if queue_name in {INBOUND_QUEUE_NAME, OUTBOUND_QUEUE_NAME}:
        return queue_name
    raise ValueError("queue_name must be one of: refactor_inbound, refactor_outbox")


def _queue_name_for_direction(direction: str) -> str:
    return INBOUND_QUEUE_NAME if direction == QueueDirection.INBOUND else OUTBOUND_QUEUE_NAME


def _resolve_audit_logger(
    provided_logger: Callable[[str, str], None] | None,
) -> Callable[[str, str], None]:
    if callable(provided_logger):
        return provided_logger
    try:
        from utils.admin_audit import log_admin_audit  # noqa: PLC0415

        return log_admin_audit
    except Exception:
        return lambda action, details: logger.info("operator_recovery_audit action=%s details=%s", action, details)


def _audit_details(payload: Mapping[str, Any]) -> str:
    scrubbed = scrub_payload_for_logging(payload, allowlist=tuple(payload.keys()))
    encoded = json.dumps(scrubbed, sort_keys=True, default=str)
    return encoded[:500]


def _coerce_message_ids(raw_ids: Iterable[Any] | None) -> tuple[str, ...]:
    if not raw_ids:
        return ()
    stable_unique: list[str] = []
    seen: set[str] = set()
    for raw_id in raw_ids:
        message_id = _safe_text(raw_id)
        if not message_id or message_id in seen:
            continue
        seen.add(message_id)
        stable_unique.append(message_id)
    return tuple(stable_unique)


def _scrub_error_message(value: Any) -> str | None:
    text = _safe_text(value)
    if not text:
        return None
    scrubbed = scrub_payload_for_logging({"last_error": text}, allowlist=("last_error",))
    clean = _safe_text(scrubbed.get("last_error"))
    clean = re.sub(
        r"(?i)\b(token|secret|api[_-]?key|authorization)\s*[:=]\s*([^\s,;]+)",
        r"\1=[REDACTED]",
        clean,
    )
    return clean or None


def _inspection_statuses(raw_statuses: Sequence[Any] | None) -> tuple[str, ...]:
    if not raw_statuses:
        return (_STATUS_DEAD, _STATUS_STALE)
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_status in raw_statuses:
        status = _safe_text(raw_status).lower()
        if status not in _ALLOWED_INSPECTION_STATUSES or status in seen:
            continue
        seen.add(status)
        normalized.append(status)
    if not normalized:
        return (_STATUS_DEAD, _STATUS_STALE)
    return tuple(normalized)


class QueuePauseStore(Protocol):
    def upsert(self, state: "QueuePauseState") -> "QueuePauseState":
        ...

    def get(self, queue_name: str) -> "QueuePauseState | None":
        ...

    def remove(self, queue_name: str) -> None:
        ...

    def list_all(self) -> list["QueuePauseState"]:
        ...


@dataclass(frozen=True)
class QueuePauseState:
    queue_name: str
    paused: bool
    paused_by: str | None = None
    reason: str | None = None
    paused_at: str | None = None
    expires_at: str | None = None
    resumed_by: str | None = None
    resumed_at: str | None = None


@dataclass(frozen=True)
class QueuePauseCommand:
    actor: str
    reason: str
    queue_name: str
    granted_permissions: Iterable[str] = field(default_factory=tuple)
    duration_seconds: int | None = None
    requested_at: str = field(default_factory=_utc_now_iso)


@dataclass(frozen=True)
class QueueResumeCommand:
    actor: str
    reason: str
    queue_name: str
    granted_permissions: Iterable[str] = field(default_factory=tuple)
    requested_at: str = field(default_factory=_utc_now_iso)


class InMemoryQueuePauseStore:
    def __init__(self) -> None:
        self._states: dict[str, QueuePauseState] = {}
        self._lock = Lock()

    def upsert(self, state: QueuePauseState) -> QueuePauseState:
        with self._lock:
            self._states[state.queue_name] = state
        return state

    def get(self, queue_name: str) -> QueuePauseState | None:
        with self._lock:
            return self._states.get(queue_name)

    def remove(self, queue_name: str) -> None:
        with self._lock:
            self._states.pop(queue_name, None)

    def list_all(self) -> list[QueuePauseState]:
        with self._lock:
            return list(self._states.values())


class QueuePauseService:
    def __init__(
        self,
        *,
        pause_store: QueuePauseStore | None = None,
        default_pause_seconds: int = 300,
        max_pause_seconds: int = 1800,
        required_permission: str = QUEUE_PAUSE_PERMISSION,
        audit_logger: Callable[[str, str], None] | None = None,
    ) -> None:
        self._pause_store = pause_store or InMemoryQueuePauseStore()
        self._default_pause_seconds = _safe_positive_int(default_pause_seconds, default=300)
        self._max_pause_seconds = _safe_positive_int(max_pause_seconds, default=1800)
        self._required_permission = _safe_text(required_permission) or QUEUE_PAUSE_PERMISSION
        self._audit_logger = _resolve_audit_logger(audit_logger)

    def pause_queue(self, command: QueuePauseCommand) -> QueuePauseState:
        actor = _safe_text(command.actor)
        reason = _safe_text(command.reason)
        queue_name = _resolve_queue_name(command.queue_name)
        if not actor:
            raise ValueError("actor is required")
        if not reason:
            raise ValueError("reason is required")
        require_permission(command.granted_permissions, self._required_permission, actor=actor)
        bounded_duration = min(
            self._max_pause_seconds,
            _safe_positive_int(command.duration_seconds, default=self._default_pause_seconds),
        )
        paused_at = _parse_utc(command.requested_at)
        expires_at = paused_at + timedelta(seconds=bounded_duration)
        state = QueuePauseState(
            queue_name=queue_name,
            paused=True,
            paused_by=actor,
            reason=reason,
            paused_at=paused_at.isoformat(),
            expires_at=expires_at.isoformat(),
        )
        self._pause_store.upsert(state)
        self._audit_logger(
            "operator_queue_paused",
            _audit_details(
                {
                    "queue_name": queue_name,
                    "actor": actor,
                    "reason": reason,
                    "duration_seconds": bounded_duration,
                    "expires_at": state.expires_at,
                }
            ),
        )
        return state

    def resume_queue(self, command: QueueResumeCommand) -> QueuePauseState:
        actor = _safe_text(command.actor)
        reason = _safe_text(command.reason)
        queue_name = _resolve_queue_name(command.queue_name)
        if not actor:
            raise ValueError("actor is required")
        if not reason:
            raise ValueError("reason is required")
        require_permission(command.granted_permissions, self._required_permission, actor=actor)
        previous = self._pause_store.get(queue_name)
        self._pause_store.remove(queue_name)
        resumed_at = _parse_utc(command.requested_at)
        state = QueuePauseState(
            queue_name=queue_name,
            paused=False,
            paused_by=previous.paused_by if previous else None,
            reason=previous.reason if previous else reason,
            paused_at=previous.paused_at if previous else None,
            expires_at=previous.expires_at if previous else None,
            resumed_by=actor,
            resumed_at=resumed_at.isoformat(),
        )
        self._audit_logger(
            "operator_queue_resumed",
            _audit_details(
                {
                    "queue_name": queue_name,
                    "actor": actor,
                    "reason": reason,
                    "had_active_pause": bool(previous and previous.paused),
                }
            ),
        )
        return state

    def is_queue_paused(self, queue_name: str, *, now: datetime | None = None, conn: Any | None = None) -> bool:
        _ = conn
        resolved_queue_name = _resolve_queue_name(queue_name)
        state = self._pause_store.get(resolved_queue_name)
        if state is None or not state.paused:
            return False
        expires_at = _parse_utc(state.expires_at, fallback=None) if state.expires_at else None
        if expires_at is not None and expires_at <= (now or _utc_now()):
            self._pause_store.remove(resolved_queue_name)
            return False
        return True

    def list_paused_queues(self) -> tuple[QueuePauseState, ...]:
        active = [state for state in self._pause_store.list_all() if self.is_queue_paused(state.queue_name)]
        active.sort(key=lambda state: (state.expires_at or "", state.queue_name))
        return tuple(active)


@dataclass(frozen=True)
class StuckJobInspectionQuery:
    actor: str
    granted_permissions: Iterable[str] = field(default_factory=tuple)
    direction: str = _DIRECTION_ALL
    statuses: tuple[str, ...] = (_STATUS_DEAD, _STATUS_STALE)
    event_type: str | None = None
    aggregate_id: str | None = None
    message_id: str | None = None
    limit: int = 50


@dataclass(frozen=True)
class StuckJobSummary:
    direction: str
    queue_name: str
    item_id: str
    status: str
    attempt: int = 0
    max_attempts: int = 0
    created_at: str | None = None
    updated_at: str | None = None
    event_type: str | None = None
    aggregate_id: str | None = None
    lease_owner_id: str | None = None
    lease_expires_at: str | None = None
    last_error: str | None = None


@dataclass(frozen=True)
class StuckJobInspectionResult:
    scanned: int
    returned: int
    summary: Mapping[str, int]
    items: tuple[StuckJobSummary, ...]


class StuckJobInspectionService:
    def __init__(
        self,
        *,
        inbound_repository: InboundQueueProvider,
        outbound_repository: OutboundQueueProvider,
        lease_store: WorkerLeaseStore | None = None,
        required_permission: str = QUEUE_INSPECT_PERMISSION,
        max_results: int = 200,
        audit_logger: Callable[[str, str], None] | None = None,
    ) -> None:
        self._inbound_repository = inbound_repository
        self._outbound_repository = outbound_repository
        self._lease_store = lease_store
        self._required_permission = _safe_text(required_permission) or QUEUE_INSPECT_PERMISSION
        self._max_results = _safe_positive_int(max_results, default=200)
        self._audit_logger = _resolve_audit_logger(audit_logger)

    def _gather_candidates(
        self, *, direction: str, statuses: set, bounded_limit: int, conn: Any | None
    ) -> list:
        """Collect raw candidates from queues and stale leases."""
        candidates: list[StuckJobSummary] = []
        if direction in {QueueDirection.INBOUND, _DIRECTION_ALL}:
            candidates.extend(self._collect_queue_candidates(
                direction=QueueDirection.INBOUND, queue_name=INBOUND_QUEUE_NAME,
                include_dead=_STATUS_DEAD in statuses, include_retry=_STATUS_RETRY in statuses,
                limit=bounded_limit, conn=conn,
            ))
        if direction in {QueueDirection.OUTBOUND, _DIRECTION_ALL}:
            candidates.extend(self._collect_queue_candidates(
                direction=QueueDirection.OUTBOUND, queue_name=OUTBOUND_QUEUE_NAME,
                include_dead=_STATUS_DEAD in statuses, include_retry=_STATUS_RETRY in statuses,
                limit=bounded_limit, conn=conn,
            ))
        if _STATUS_STALE in statuses and self._lease_store is not None:
            if direction in {QueueDirection.INBOUND, _DIRECTION_ALL}:
                candidates.extend(self._collect_stale_leases(
                    queue_name=INBOUND_QUEUE_NAME, direction=QueueDirection.INBOUND,
                    limit=bounded_limit, conn=conn,
                ))
            if direction in {QueueDirection.OUTBOUND, _DIRECTION_ALL}:
                candidates.extend(self._collect_stale_leases(
                    queue_name=OUTBOUND_QUEUE_NAME, direction=QueueDirection.OUTBOUND,
                    limit=bounded_limit, conn=conn,
                ))
        return candidates

    def _apply_filters(
        self, candidates: list, *, event_filter: str, aggregate_filter: str, message_filter: str
    ) -> list:
        """Deduplicate and filter candidates by event/aggregate/message criteria."""
        filtered: list[StuckJobSummary] = []
        seen: set[tuple[str, str, str]] = set()
        for candidate in candidates:
            if event_filter and _safe_text(candidate.event_type).lower() != event_filter:
                continue
            if aggregate_filter and _safe_text(candidate.aggregate_id).lower() != aggregate_filter:
                continue
            if message_filter and message_filter not in _safe_text(candidate.item_id).lower():
                continue
            dedup_key = (candidate.direction, candidate.status, candidate.item_id)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            filtered.append(candidate)
        return filtered

    def inspect(self, query: StuckJobInspectionQuery, *, conn: Any | None = None) -> StuckJobInspectionResult:
        actor = _safe_text(query.actor)
        if not actor:
            raise ValueError("actor is required")
        require_permission(query.granted_permissions, self._required_permission, actor=actor)
        direction = _resolve_direction(query.direction)
        statuses = _inspection_statuses(query.statuses)
        bounded_limit = min(self._max_results, _safe_positive_int(query.limit, default=min(50, self._max_results)))
        event_filter = _safe_text(query.event_type).lower()
        aggregate_filter = _safe_text(query.aggregate_id).lower()
        message_filter = _safe_text(query.message_id).lower()

        candidates = self._gather_candidates(
            direction=direction, statuses=set(statuses), bounded_limit=bounded_limit, conn=conn
        )
        filtered = self._apply_filters(
            candidates, event_filter=event_filter,
            aggregate_filter=aggregate_filter, message_filter=message_filter,
        )
        filtered.sort(key=lambda item: (
            _safe_text(item.created_at or item.lease_expires_at or item.updated_at),
            item.direction, item.status, item.item_id,
        ))
        limited = tuple(filtered[:bounded_limit])
        summary = {
            "dead": sum(1 for item in limited if item.status == _STATUS_DEAD),
            "retry": sum(1 for item in limited if item.status == _STATUS_RETRY),
            "stale_claim": sum(1 for item in limited if item.status == _STATUS_STALE),
        }
        result = StuckJobInspectionResult(
            scanned=len(filtered), returned=len(limited), summary=summary, items=limited,
        )
        self._audit_logger(
            "operator_stuck_jobs_inspected",
            _audit_details({"actor": actor, "direction": direction, "statuses": list(statuses),
                            "limit": bounded_limit, "returned": result.returned}),
        )
        return result


    def _collect_queue_candidates(
        self,
        *,
        direction: str,
        queue_name: str,
        include_dead: bool,
        include_retry: bool,
        limit: int,
        conn: Any | None = None,
    ) -> list[StuckJobSummary]:
        repository = self._inbound_repository if direction == QueueDirection.INBOUND else self._outbound_repository
        candidates: list[StuckJobSummary] = []
        if include_dead:
            for record in repository.list_dead(limit=limit, conn=conn):
                candidates.append(self._build_queue_summary(record=record, direction=direction, queue_name=queue_name))
        if include_retry:
            for record in repository.list_pending(limit=limit, conn=conn):
                if canonical_status(getattr(record, "status", "")) != QueueStatus.RETRY:
                    continue
                candidates.append(self._build_queue_summary(record=record, direction=direction, queue_name=queue_name))
        return candidates

    def _collect_stale_leases(
        self,
        *,
        queue_name: str,
        direction: str,
        limit: int,
        conn: Any | None = None,
    ) -> list[StuckJobSummary]:
        if self._lease_store is None:
            return []
        claims = self._lease_store.list_stale_claims(queue_name=queue_name, limit=limit, conn=conn)
        return [
            StuckJobSummary(
                direction=direction,
                queue_name=queue_name,
                item_id=claim.item_id,
                status=_STATUS_STALE,
                created_at=_to_iso(claim.claimed_at),
                updated_at=_to_iso(claim.last_heartbeat_at),
                lease_owner_id=_safe_text(claim.owner_id) or None,
                lease_expires_at=_to_iso(claim.lease_expires_at),
            )
            for claim in claims
        ]

    @staticmethod
    def _build_queue_summary(*, record: Any, direction: str, queue_name: str) -> StuckJobSummary:
        payload = getattr(record, "payload", {})
        payload_map = payload if isinstance(payload, Mapping) else {}
        event_type = _safe_text(getattr(record, "message_type", None)) or _safe_text(payload_map.get("event_type"))
        aggregate_id = _safe_text(getattr(record, "aggregate_id", None)) or _safe_text(payload_map.get("aggregate_id"))
        status = canonical_status(getattr(record, "status", ""))
        mapped_status = _STATUS_RETRY if status == QueueStatus.RETRY else _STATUS_DEAD if status == QueueStatus.DEAD else status
        return StuckJobSummary(
            direction=direction,
            queue_name=queue_name,
            item_id=_safe_text(getattr(record, "message_id", None)),
            status=mapped_status,
            attempt=max(0, int(getattr(getattr(record, "metadata", None), "attempt", 0) or 0)),
            max_attempts=max(0, int(getattr(record, "max_attempts", 0) or 0)),
            created_at=_to_iso(getattr(record, "created_at", None)),
            updated_at=_to_iso(getattr(record, "updated_at", None)),
            event_type=event_type or None,
            aggregate_id=aggregate_id or None,
            last_error=_scrub_error_message(getattr(record, "last_error", None)),
        )


@dataclass(frozen=True)
class DLQReplayInvocation:
    actor: str
    reason: str
    granted_permissions: Iterable[str] = field(default_factory=tuple)
    direction: str = _DIRECTION_ALL
    message_ids: tuple[str, ...] = ()
    event_type: str | None = None
    aggregate_id: str | None = None
    mode: str = "batch"
    batch_limit: int = 25
    dry_run: bool = True
    replay_run_id: str | None = None
    idempotency_key: str | None = None
    requested_at: str = field(default_factory=_utc_now_iso)


class OperatorDLQReplayInvoker:
    def __init__(
        self,
        *,
        replay_service: DLQReplayService,
        max_batch_size: int = 50,
        audit_logger: Callable[[str, str], None] | None = None,
    ) -> None:
        self._replay_service = replay_service
        self._max_batch_size = _safe_positive_int(max_batch_size, default=50)
        self._audit_logger = _resolve_audit_logger(audit_logger)

    def invoke(
        self,
        invocation: DLQReplayInvocation,
        *,
        conn: Any | None = None,
    ) -> DLQReplayResult:
        bounded_limit = min(self._max_batch_size, _safe_positive_int(invocation.batch_limit, default=25))
        message_ids = _coerce_message_ids(invocation.message_ids)
        if message_ids and len(message_ids) > bounded_limit:
            raise DLQReplaySafetyError(f"message_ids exceed bounded replay limit ({bounded_limit})")

        selection = DLQReplaySelection(
            direction=_resolve_direction(invocation.direction),
            message_ids=message_ids,
            event_type=_safe_text(invocation.event_type) or None,
            aggregate_id=_safe_text(invocation.aggregate_id) or None,
        )
        command = DLQReplayCommand(
            actor=_safe_text(invocation.actor),
            reason=_safe_text(invocation.reason),
            granted_permissions=invocation.granted_permissions,
            selection=selection,
            mode=_safe_text(invocation.mode) or "batch",
            batch_limit=bounded_limit,
            dry_run=bool(invocation.dry_run),
            replay_run_id=_safe_text(invocation.replay_run_id) or None,
            idempotency_key=_safe_text(invocation.idempotency_key) or None,
            requested_at=_safe_text(invocation.requested_at) or _utc_now_iso(),
        )
        result = self._replay_service.execute(command, conn=conn)
        self._audit_logger(
            "operator_dlq_replay_invoked",
            _audit_details(
                {
                    "actor": command.actor,
                    "reason": command.reason,
                    "direction": selection.direction,
                    "dry_run": command.dry_run,
                    "batch_limit": bounded_limit,
                    "requested": result.requested,
                    "replayed": result.replayed,
                }
            ),
        )
        return result


@dataclass(frozen=True)
class QueueArchivalInvocation:
    actor: str
    reason: str
    granted_permissions: Iterable[str] = field(default_factory=tuple)
    batch_limit: int | None = None
    requested_at: str = field(default_factory=_utc_now_iso)


class OperatorQueueArchivalInvoker:
    def __init__(
        self,
        *,
        archival_service: QueueArchivalService,
        max_batch_size: int = 200,
        required_permission: str = QUEUE_ARCHIVE_PERMISSION,
        audit_logger: Callable[[str, str], None] | None = None,
    ) -> None:
        self._archival_service = archival_service
        self._max_batch_size = _safe_positive_int(max_batch_size, default=200)
        self._required_permission = _safe_text(required_permission) or QUEUE_ARCHIVE_PERMISSION
        self._audit_logger = _resolve_audit_logger(audit_logger)

    def invoke(
        self,
        invocation: QueueArchivalInvocation,
        *,
        conn: Any | None = None,
    ) -> QueueArchivalResult:
        actor = _safe_text(invocation.actor)
        reason = _safe_text(invocation.reason)
        require_permission(invocation.granted_permissions, self._required_permission, actor=actor)
        bounded_limit = min(
            self._max_batch_size,
            _safe_positive_int(invocation.batch_limit, default=min(50, self._max_batch_size)),
        )
        command = QueueArchivalCommand(
            actor=actor,
            reason=reason,
            batch_limit=bounded_limit,
            requested_at=_safe_text(invocation.requested_at) or _utc_now_iso(),
        )
        result = self._archival_service.execute(command, conn=conn)
        self._audit_logger(
            "operator_queue_archival_invoked",
            _audit_details(
                {
                    "actor": actor,
                    "reason": reason,
                    "batch_limit": bounded_limit,
                    "archived_total": result.archived_total,
                    "exceptions": len(result.exceptions),
                }
            ),
        )
        return result
