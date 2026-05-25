from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import logging
import uuid
from typing import Any, Protocol, cast
from collections.abc import Callable, Mapping, Sequence

from core.state_machine import is_valid_state, is_valid_transition
from app.events.outbox import DatabaseOutboxRepository, OutboxEventEnvelope, OutboxRepository
from app.runtime.transition_history import (
    AppendOnlyTransitionHistoryRepository,
    DbTransitionHistoryRepository,
    SupportsTransitionMetadataRecord,
    build_transition_history_record,
)

logger = logging.getLogger("adella_chatbot.refactor.transition_service")

TransitionMetadataHook = Callable[
    ["TransitionRequest", "StateSnapshot", int],
    Mapping[str, Any] | None,
]


def _transition_history_defaults_hook(
    request: "TransitionRequest",
    snapshot: "StateSnapshot",
    committed_version: int,
) -> Mapping[str, Any]:
    _ = (snapshot, committed_version)
    metadata = request.metadata or {}
    correlation_raw = metadata.get("correlation_id") or metadata.get("request_id")
    correlation_id = str(correlation_raw) if correlation_raw not in {None, ""} else None
    return {
        "conversation_id": str(metadata.get("conversation_id") or request.phone_number),
        "actor": str(metadata.get("actor") or "system"),
        "source": str(metadata.get("source") or "transition_service"),
        "correlation_id": correlation_id,
        "timestamp": metadata.get("timestamp") or metadata.get("transitioned_at") or datetime.now(UTC).isoformat(),
    }


@dataclass(frozen=True)
class TransitionRequest:
    phone_number: str
    from_state: str
    to_state: str
    expected_version: int
    metadata: Mapping[str, Any] = field(default_factory=dict)
    conn: Any | None = None


@dataclass(frozen=True)
class StateSnapshot:
    phone_number: str
    current_state: str
    version: int


@dataclass(frozen=True)
class TransitionMetadataRecord:
    phone_number: str
    from_state: str
    to_state: str
    expected_version: int
    committed_version: int
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TransitionResult:
    status: str
    phone_number: str
    from_state: str
    to_state: str
    expected_version: int
    observed_state: str | None = None
    observed_version: int | None = None
    committed_version: int | None = None
    reason: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {"applied", "noop"}


class TransitionStore(Protocol):
    def get_snapshot(self, phone_number: str, *, conn: Any | None = None) -> StateSnapshot | None:
        ...

    def compare_and_set_state(
        self,
        phone_number: str,
        to_state: str,
        expected_version: int,
        *,
        conn: Any | None = None,
    ) -> int | None:
        ...

    def append_transition_metadata(
        self,
        record: TransitionMetadataRecord,
        *,
        conn: Any | None = None,
    ) -> None:
        ...


class StateTransitionService:
    """Central transition API with FSM and optimistic-version enforcement."""

    def __init__(
        self,
        *,
        store: TransitionStore,
        metadata_hooks: Sequence[TransitionMetadataHook] = (),
        outbox_repository: OutboxRepository | None = None,
    ) -> None:
        self._store = store
        self._metadata_hooks = tuple(metadata_hooks) + (_transition_history_defaults_hook,)
        self._outbox_repository = outbox_repository

    def transition(self, request: TransitionRequest) -> TransitionResult:
        snapshot = self._store.get_snapshot(request.phone_number, conn=request.conn)
        if snapshot is None:
            return TransitionResult(
                status="rejected",
                phone_number=request.phone_number,
                from_state=request.from_state,
                to_state=request.to_state,
                expected_version=request.expected_version,
                reason="state_not_found",
            )

        if snapshot.current_state != request.from_state:
            return TransitionResult(
                status="conflict",
                phone_number=request.phone_number,
                from_state=request.from_state,
                to_state=request.to_state,
                expected_version=request.expected_version,
                observed_state=snapshot.current_state,
                observed_version=snapshot.version,
                reason="state_mismatch",
            )

        if snapshot.version != request.expected_version:
            return TransitionResult(
                status="conflict",
                phone_number=request.phone_number,
                from_state=request.from_state,
                to_state=request.to_state,
                expected_version=request.expected_version,
                observed_state=snapshot.current_state,
                observed_version=snapshot.version,
                reason="version_conflict",
            )

        if not is_valid_state(request.from_state) or not is_valid_state(request.to_state):
            return TransitionResult(
                status="rejected",
                phone_number=request.phone_number,
                from_state=request.from_state,
                to_state=request.to_state,
                expected_version=request.expected_version,
                observed_state=snapshot.current_state,
                observed_version=snapshot.version,
                reason="invalid_state",
            )

        if request.from_state == request.to_state:
            return TransitionResult(
                status="noop",
                phone_number=request.phone_number,
                from_state=request.from_state,
                to_state=request.to_state,
                expected_version=request.expected_version,
                observed_state=snapshot.current_state,
                observed_version=snapshot.version,
                committed_version=snapshot.version,
                reason="no_state_change",
            )

        if not is_valid_transition(request.from_state, request.to_state):
            return TransitionResult(
                status="rejected",
                phone_number=request.phone_number,
                from_state=request.from_state,
                to_state=request.to_state,
                expected_version=request.expected_version,
                observed_state=snapshot.current_state,
                observed_version=snapshot.version,
                reason="invalid_transition",
            )

        committed_version = self._store.compare_and_set_state(
            request.phone_number,
            request.to_state,
            request.expected_version,
            conn=request.conn,
        )
        if committed_version is None:
            latest = self._store.get_snapshot(request.phone_number, conn=request.conn)
            return TransitionResult(
                status="conflict",
                phone_number=request.phone_number,
                from_state=request.from_state,
                to_state=request.to_state,
                expected_version=request.expected_version,
                observed_state=latest.current_state if latest else None,
                observed_version=latest.version if latest else None,
                reason="version_conflict",
            )

        metadata = self._build_metadata(request, snapshot, committed_version)
        record = TransitionMetadataRecord(
            phone_number=request.phone_number,
            from_state=request.from_state,
            to_state=request.to_state,
            expected_version=request.expected_version,
            committed_version=committed_version,
            metadata=metadata,
        )
        try:
            self._store.append_transition_metadata(record, conn=request.conn)
        except Exception:
            logger.exception(
                "transition_service metadata append failed phone=%s from=%s to=%s",
                request.phone_number,
                request.from_state,
                request.to_state,
            )
        try:
            self._append_outbox_event(
                request=request,
                committed_version=committed_version,
                metadata=metadata,
            )
        except Exception:
            logger.exception(
                "transition_service outbox append failed phone=%s from=%s to=%s",
                request.phone_number,
                request.from_state,
                request.to_state,
            )

        return TransitionResult(
            status="applied",
            phone_number=request.phone_number,
            from_state=request.from_state,
            to_state=request.to_state,
            expected_version=request.expected_version,
            observed_state=snapshot.current_state,
            observed_version=snapshot.version,
            committed_version=committed_version,
            metadata=metadata,
        )

    def _build_metadata(
        self,
        request: TransitionRequest,
        snapshot: StateSnapshot,
        committed_version: int,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = dict(request.metadata)
        for hook in self._metadata_hooks:
            extra = hook(request, snapshot, committed_version) or {}
            for key, value in extra.items():
                metadata.setdefault(str(key), value)
        return metadata

    def _append_outbox_event(
        self,
        *,
        request: TransitionRequest,
        committed_version: int,
        metadata: Mapping[str, Any],
    ) -> None:
        if self._outbox_repository is None:
            return
        idempotency_key = (
            f"state_transition:{request.phone_number}:{request.expected_version}:"
            f"{request.from_state}:{request.to_state}"
        )
        event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, idempotency_key))
        event = OutboxEventEnvelope(
            event_id=event_id,
            idempotency_key=idempotency_key,
            event_type="conversation.state_transitioned",
            aggregate_type="conversation_state",
            aggregate_id=request.phone_number,
            payload={
                "phone_number": request.phone_number,
                "from_state": request.from_state,
                "to_state": request.to_state,
                "expected_version": request.expected_version,
                "committed_version": committed_version,
                "metadata": dict(metadata),
            },
            metadata={
                "source": "transition_service",
                "from_state": request.from_state,
                "to_state": request.to_state,
            },
        )
        self._outbox_repository.append_event(event, conn=request.conn)


class LegacyStateManagerTransitionStore:
    """Adapter exposing a compare-and-set state transition contract."""

    def __init__(
        self,
        state_manager: Any,
        *,
        history_repository: AppendOnlyTransitionHistoryRepository | None = None,
    ) -> None:
        self._state_manager = state_manager
        if history_repository is not None:
            self._history_repository = history_repository
        else:
            db = getattr(self._state_manager, "db", None)
            self._history_repository = DbTransitionHistoryRepository(db) if db is not None else None

    def get_snapshot(self, phone_number: str, *, conn: Any | None = None) -> StateSnapshot | None:
        row = self._state_manager.get_state(phone_number, conn=conn)
        if not row:
            return None
        return StateSnapshot(
            phone_number=phone_number,
            current_state=str(row.get("current_state") or "NEW"),
            version=int(row.get("version") or 0),
        )

    def compare_and_set_state(
        self,
        phone_number: str,
        to_state: str,
        expected_version: int,
        *,
        conn: Any | None = None,
    ) -> int | None:
        db = getattr(self._state_manager, "db", None)
        if db is None:
            raise RuntimeError("State manager does not expose a db adapter")

        rows = db.execute_query(
            """
            UPDATE conversation_states
               SET current_state = %s,
                   version = version + 1,
                   updated_at = CURRENT_TIMESTAMP
             WHERE phone_number = %s
               AND version = %s
             RETURNING version
            """,
            (to_state, phone_number, expected_version),
            fetch=True,
            conn=conn,
        )
        if not rows:
            return None
        row = rows[0]
        if isinstance(row, Mapping):
            return int(row.get("version") or 0)
        if isinstance(row, (tuple, list)):
            return int(row[0])
        return int(row)

    def append_transition_metadata(
        self,
        record: TransitionMetadataRecord,
        *,
        conn: Any | None = None,
    ) -> None:
        if self._history_repository is None:
            return
        history_record = build_transition_history_record(cast(SupportsTransitionMetadataRecord, record))
        self._history_repository.append(history_record, conn=conn)


def build_state_transition_service(
    *,
    state_manager: Any,
    metadata_hooks: Sequence[TransitionMetadataHook] = (),
    history_repository: AppendOnlyTransitionHistoryRepository | None = None,
    db_service: Any | None = None,
    outbox_repository: OutboxRepository | None = None,
) -> StateTransitionService:
    if outbox_repository is None and db_service is not None:
        outbox_repository = DatabaseOutboxRepository(db_service)
    return StateTransitionService(
        store=LegacyStateManagerTransitionStore(
            state_manager,
            history_repository=history_repository,
        ),
        metadata_hooks=metadata_hooks,
        outbox_repository=outbox_repository,
    )
