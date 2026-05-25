from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
from typing import Any, Iterable, Mapping
import uuid

from app.queue.inbound import InboundQueueRecord
from app.queue.outbound import OutboundQueueRecord
from app.queue.providers import InboundQueueProvider, OutboundQueueProvider
from app.queue.status import QueueDirection, QueueStatus, canonical_status
from app.security.rbac import require_permission

DLQ_REPLAY_PERMISSION = "queue:dlq_replay"
_DIRECTION_ALL = "all"
DLQReplayPermission = DLQ_REPLAY_PERMISSION


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _safe_positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return max(1, int(default))
    return max(1, parsed)


def _normalize_mode(value: Any) -> str:
    mode = _safe_text(value).lower()
    if mode in {"single", "batch"}:
        return mode
    return "batch"


def _normalize_direction(value: Any) -> str:
    direction = _safe_text(value).lower()
    if direction in {QueueDirection.INBOUND, QueueDirection.OUTBOUND, _DIRECTION_ALL}:
        return direction
    return _DIRECTION_ALL


def _normalize_message_ids(raw_ids: Iterable[str] | None) -> tuple[str, ...]:
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


def _default_idempotency_key(command: "DLQReplayCommand", *, direction: str, message_ids: tuple[str, ...]) -> str:
    payload = (
        f"{_safe_text(command.actor).lower()}|{_safe_text(command.reason).lower()}|"
        f"{direction}|{_normalize_mode(command.mode)}|{int(bool(command.dry_run))}|"
        f"{_safe_positive_int(command.batch_limit, default=25)}|{','.join(message_ids)}|"
        f"{_safe_text(command.selection.event_type).lower()}|{_safe_text(command.selection.aggregate_id).lower()}"
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"dlq-replay:{digest[:32]}"


RecordType = InboundQueueRecord | OutboundQueueRecord


@dataclass(frozen=True)
class DLQReplaySelection:
    direction: str = _DIRECTION_ALL
    message_ids: tuple[str, ...] = ()
    event_type: str | None = None
    aggregate_id: str | None = None


@dataclass(frozen=True)
class DLQReplayCommand:
    actor: str
    reason: str
    granted_permissions: Iterable[str] = field(default_factory=tuple)
    selection: DLQReplaySelection = field(default_factory=DLQReplaySelection)
    mode: str = "batch"
    batch_limit: int = 25
    dry_run: bool = True
    replay_run_id: str | None = None
    idempotency_key: str | None = None
    requested_at: str = field(default_factory=_utc_now_iso)


@dataclass(frozen=True)
class DLQReplayDecision:
    direction: str
    message_id: str
    status_before: str
    outcome: str
    reason: str | None = None


@dataclass(frozen=True)
class DLQReplayResult:
    replay_run_id: str
    idempotency_key: str
    dry_run: bool
    requested: int
    replayed: int
    dry_run_candidates: int
    skipped: int
    decisions: tuple[DLQReplayDecision, ...]


class DLQReplaySafetyError(ValueError):
    """Raised when replay command safety checks fail."""


@dataclass(frozen=True)
class _ReplayCandidate:
    direction: str
    record: RecordType


class DLQReplayService:
    def __init__(
        self,
        *,
        inbound_repository: InboundQueueProvider,
        outbound_repository: OutboundQueueProvider,
        max_batch_size: int = 100,
        required_permission: str = DLQ_REPLAY_PERMISSION,
    ) -> None:
        self._inbound_repository = inbound_repository
        self._outbound_repository = outbound_repository
        self._max_batch_size = max(1, int(max_batch_size))
        self._required_permission = _safe_text(required_permission) or DLQ_REPLAY_PERMISSION

    def execute(
        self,
        command: DLQReplayCommand,
        *,
        conn: Any | None = None,
    ) -> DLQReplayResult:
        actor = _safe_text(command.actor)
        reason = _safe_text(command.reason)
        if not actor:
            raise DLQReplaySafetyError("actor is required")
        if not reason:
            raise DLQReplaySafetyError("reason is required")
        require_permission(command.granted_permissions, self._required_permission, actor=actor)

        direction = _normalize_direction(command.selection.direction)
        mode = _normalize_mode(command.mode)
        bounded_limit = min(
            self._max_batch_size,
            _safe_positive_int(command.batch_limit, default=min(25, self._max_batch_size)),
        )
        message_ids = _normalize_message_ids(command.selection.message_ids)
        if mode == "single" and len(message_ids) != 1:
            raise DLQReplaySafetyError("single mode requires exactly one message_id")
        if message_ids and len(message_ids) > bounded_limit:
            raise DLQReplaySafetyError(f"message_ids exceed bounded batch limit ({bounded_limit})")

        idempotency_key = _safe_text(command.idempotency_key) or _default_idempotency_key(
            command,
            direction=direction,
            message_ids=message_ids,
        )
        replay_run_id = _safe_text(command.replay_run_id) or str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"dlq-replay:{idempotency_key}")
        )
        candidates = self._select_candidates(
            selection=command.selection,
            direction=direction,
            message_ids=message_ids,
            bounded_limit=bounded_limit,
            conn=conn,
        )

        decisions: list[DLQReplayDecision] = []
        replayed = 0
        dry_run_candidates = 0
        for candidate in candidates:
            record = candidate.record
            current_status = canonical_status(record.status)
            if current_status != QueueStatus.DEAD:
                decisions.append(
                    DLQReplayDecision(
                        direction=candidate.direction,
                        message_id=record.message_id,
                        status_before=current_status,
                        outcome="skipped",
                        reason="status_not_dead",
                    )
                )
                continue
            if self._already_replayed_with_idempotency(record, idempotency_key):
                decisions.append(
                    DLQReplayDecision(
                        direction=candidate.direction,
                        message_id=record.message_id,
                        status_before=current_status,
                        outcome="skipped",
                        reason="duplicate_replay_idempotency_key",
                    )
                )
                continue

            if command.dry_run:
                dry_run_candidates += 1
                decisions.append(
                    DLQReplayDecision(
                        direction=candidate.direction,
                        message_id=record.message_id,
                        status_before=current_status,
                        outcome="dry_run",
                    )
                )
                continue

            replay_metadata = {
                "actor": actor,
                "reason": reason,
                "replay_run_id": replay_run_id,
                "idempotency_key": idempotency_key,
                "requested_at": command.requested_at,
                "replayed_at": _utc_now_iso(),
                "target_status": QueueStatus.PENDING,
                "direction": candidate.direction,
            }
            did_replay = self._repository_for_direction(candidate.direction).replay_dead(
                record.message_id,
                replay_metadata=replay_metadata,
                conn=conn,
            )
            if did_replay:
                replayed += 1
                decisions.append(
                    DLQReplayDecision(
                        direction=candidate.direction,
                        message_id=record.message_id,
                        status_before=current_status,
                        outcome="replayed",
                    )
                )
            else:
                decisions.append(
                    DLQReplayDecision(
                        direction=candidate.direction,
                        message_id=record.message_id,
                        status_before=current_status,
                        outcome="skipped",
                        reason="concurrent_mutation",
                    )
                )

        requested = len(candidates)
        return DLQReplayResult(
            replay_run_id=replay_run_id,
            idempotency_key=idempotency_key,
            dry_run=bool(command.dry_run),
            requested=requested,
            replayed=replayed,
            dry_run_candidates=dry_run_candidates,
            skipped=max(0, requested - replayed - dry_run_candidates),
            decisions=tuple(decisions),
        )

    def _select_candidates(
        self,
        *,
        selection: DLQReplaySelection,
        direction: str,
        message_ids: tuple[str, ...],
        bounded_limit: int,
        conn: Any | None = None,
    ) -> list[_ReplayCandidate]:
        if message_ids:
            return self._select_by_ids(
                message_ids=message_ids,
                selection=selection,
                direction=direction,
                bounded_limit=bounded_limit,
                conn=conn,
            )

        candidates: list[_ReplayCandidate] = []
        if direction in {QueueDirection.INBOUND, _DIRECTION_ALL}:
            candidates.extend(
                _ReplayCandidate(direction=QueueDirection.INBOUND, record=record)
                for record in self._inbound_repository.list_dead(limit=bounded_limit, conn=conn)
                if self._matches_filters(record=record, selection=selection)
            )
        if direction in {QueueDirection.OUTBOUND, _DIRECTION_ALL}:
            candidates.extend(
                _ReplayCandidate(direction=QueueDirection.OUTBOUND, record=record)
                for record in self._outbound_repository.list_dead(limit=bounded_limit, conn=conn)
                if self._matches_filters(record=record, selection=selection)
            )
        candidates.sort(
            key=lambda candidate: (
                _safe_text(candidate.record.created_at),
                candidate.direction,
                candidate.record.message_id,
            )
        )
        return candidates[:bounded_limit]

    def _select_by_ids(
        self,
        *,
        message_ids: tuple[str, ...],
        selection: DLQReplaySelection,
        direction: str,
        bounded_limit: int,
        conn: Any | None = None,
    ) -> list[_ReplayCandidate]:
        directions = [QueueDirection.INBOUND, QueueDirection.OUTBOUND]
        if direction in {QueueDirection.INBOUND, QueueDirection.OUTBOUND}:
            directions = [direction]
        candidates: list[_ReplayCandidate] = []
        seen: set[tuple[str, str]] = set()
        for message_id in message_ids:
            for candidate_direction in directions:
                repository = self._repository_for_direction(candidate_direction)
                record = repository.get_message(message_id, conn=conn)
                if record is None or not self._matches_filters(record=record, selection=selection):
                    continue
                key = (candidate_direction, record.message_id)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(_ReplayCandidate(direction=candidate_direction, record=record))
                if len(candidates) >= bounded_limit:
                    return candidates
        return candidates

    def _repository_for_direction(self, direction: str) -> InboundQueueProvider | OutboundQueueProvider:
        if direction == QueueDirection.INBOUND:
            return self._inbound_repository
        return self._outbound_repository

    @staticmethod
    def _matches_filters(*, record: RecordType, selection: DLQReplaySelection) -> bool:
        event_filter = _safe_text(selection.event_type).lower()
        aggregate_filter = _safe_text(selection.aggregate_id).lower()
        if event_filter:
            event_type = _safe_text(getattr(record, "message_type", None)).lower()
            if not event_type:
                payload = getattr(record, "payload", {})
                if isinstance(payload, Mapping):
                    event_type = _safe_text(payload.get("event_type")).lower()
            if event_type != event_filter:
                return False
        if aggregate_filter:
            aggregate_id = _safe_text(getattr(record, "aggregate_id", None)).lower()
            if not aggregate_id:
                payload = getattr(record, "payload", {})
                if isinstance(payload, Mapping):
                    aggregate_id = _safe_text(payload.get("aggregate_id")).lower()
            if aggregate_id != aggregate_filter:
                return False
        return True

    @staticmethod
    def _already_replayed_with_idempotency(record: RecordType, idempotency_key: str) -> bool:
        if not idempotency_key:
            return False
        attributes = dict(record.metadata.attributes)
        replay_entry = attributes.get("dlq_replay")
        if isinstance(replay_entry, Mapping) and _safe_text(replay_entry.get("idempotency_key")) == idempotency_key:
            return True
        nested_attributes = attributes.get("attributes")
        if isinstance(nested_attributes, Mapping):
            nested_entry = nested_attributes.get("dlq_replay")
            if isinstance(nested_entry, Mapping) and _safe_text(nested_entry.get("idempotency_key")) == idempotency_key:
                return True
        history = attributes.get("dlq_replay_history")
        if isinstance(history, list):
            for item in history:
                if isinstance(item, Mapping) and _safe_text(item.get("idempotency_key")) == idempotency_key:
                    return True
        if isinstance(nested_attributes, Mapping):
            nested_history = nested_attributes.get("dlq_replay_history")
            if isinstance(nested_history, list):
                for item in nested_history:
                    if isinstance(item, Mapping) and _safe_text(item.get("idempotency_key")) == idempotency_key:
                        return True
        return False
