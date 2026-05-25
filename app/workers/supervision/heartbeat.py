from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from typing import Callable

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class WorkerHeartbeatState:
    worker_id: str
    queue_name: str
    item_id: str
    claimed_at: datetime
    last_heartbeat_at: datetime
    lease_expires_at: datetime
    beat_count: int


class WorkerHeartbeatTracker:
    """Tracks in-flight heartbeat state for currently claimed worker items."""

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._clock = clock or utc_now
        self._states: dict[tuple[str, str], WorkerHeartbeatState] = {}
        self._lock = Lock()

    def record_claim(
        self,
        *,
        worker_id: str,
        queue_name: str,
        item_id: str,
        lease_expires_at: datetime,
    ) -> WorkerHeartbeatState:
        now = self._clock()
        state = WorkerHeartbeatState(
            worker_id=worker_id,
            queue_name=queue_name,
            item_id=item_id,
            claimed_at=now,
            last_heartbeat_at=now,
            lease_expires_at=lease_expires_at,
            beat_count=1,
        )
        with self._lock:
            self._states[(queue_name, item_id)] = state
        return state

    def record_heartbeat(
        self,
        *,
        worker_id: str,
        queue_name: str,
        item_id: str,
        lease_expires_at: datetime,
    ) -> WorkerHeartbeatState | None:
        now = self._clock()
        key = (queue_name, item_id)
        with self._lock:
            existing = self._states.get(key)
            if existing is None:
                return None
            state = WorkerHeartbeatState(
                worker_id=worker_id,
                queue_name=queue_name,
                item_id=item_id,
                claimed_at=existing.claimed_at,
                last_heartbeat_at=now,
                lease_expires_at=lease_expires_at,
                beat_count=existing.beat_count + 1,
            )
            self._states[key] = state
            return state

    def clear(self, *, queue_name: str, item_id: str) -> None:
        with self._lock:
            self._states.pop((queue_name, item_id), None)

    def get(self, *, queue_name: str, item_id: str) -> WorkerHeartbeatState | None:
        with self._lock:
            return self._states.get((queue_name, item_id))

