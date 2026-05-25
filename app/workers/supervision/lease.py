from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any, Callable, Mapping, Protocol

Clock = Callable[[], datetime]

_CREATE_WORKER_LEASE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS refactor_worker_supervision_leases (
    queue_name TEXT NOT NULL,
    item_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_expires_at TIMESTAMPTZ NOT NULL,
    released_at TIMESTAMPTZ,
    release_reason TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (queue_name, item_id)
);
"""

_CREATE_WORKER_LEASE_EXPIRY_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_refactor_worker_supervision_lease_expiry
ON refactor_worker_supervision_leases (queue_name, lease_expires_at);
"""


def utc_now() -> datetime:
    return datetime.now(UTC)


def _coerce_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(normalized)
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return utc_now()


def _as_mapping(row: Any) -> Mapping[str, Any]:
    if isinstance(row, Mapping):
        return row
    return dict(row or {})


@dataclass(frozen=True)
class LeaseClaim:
    queue_name: str
    item_id: str
    owner_id: str
    claimed_at: datetime
    last_heartbeat_at: datetime
    lease_expires_at: datetime


@dataclass(frozen=True)
class LeaseClaimResult:
    claimed: bool
    claim: LeaseClaim | None = None


class WorkerLeaseStore(Protocol):
    def claim(
        self,
        *,
        queue_name: str,
        item_id: str,
        owner_id: str,
        lease_duration_seconds: int,
        conn: Any | None = None,
    ) -> LeaseClaimResult:
        ...

    def heartbeat(
        self,
        *,
        queue_name: str,
        item_id: str,
        owner_id: str,
        lease_duration_seconds: int,
        conn: Any | None = None,
    ) -> LeaseClaim | None:
        ...

    def release(
        self,
        *,
        queue_name: str,
        item_id: str,
        owner_id: str,
        reason: str,
        conn: Any | None = None,
    ) -> bool:
        ...

    def list_stale_claims(
        self,
        *,
        queue_name: str,
        limit: int = 100,
        conn: Any | None = None,
    ) -> list[LeaseClaim]:
        ...

    def mark_stale_recovered(
        self,
        *,
        queue_name: str,
        item_id: str,
        owner_id: str,
        conn: Any | None = None,
    ) -> bool:
        ...


@dataclass
class _MutableLeaseEntry:
    queue_name: str
    item_id: str
    owner_id: str
    claimed_at: datetime
    last_heartbeat_at: datetime
    lease_expires_at: datetime
    released_at: datetime | None = None
    release_reason: str | None = None

    def as_claim(self) -> LeaseClaim:
        return LeaseClaim(
            queue_name=self.queue_name,
            item_id=self.item_id,
            owner_id=self.owner_id,
            claimed_at=self.claimed_at,
            last_heartbeat_at=self.last_heartbeat_at,
            lease_expires_at=self.lease_expires_at,
        )


class InMemoryWorkerLeaseStore:
    """In-memory lease store for tests and single-process worker runtimes."""

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._clock = clock or utc_now
        self._entries: dict[tuple[str, str], _MutableLeaseEntry] = {}
        self._lock = Lock()

    def claim(
        self,
        *,
        queue_name: str,
        item_id: str,
        owner_id: str,
        lease_duration_seconds: int,
        conn: Any | None = None,
    ) -> LeaseClaimResult:
        _ = conn
        now = self._clock()
        lease_expires_at = now + timedelta(seconds=max(1, int(lease_duration_seconds)))
        key = (queue_name, item_id)
        with self._lock:
            existing = self._entries.get(key)
            if (
                existing is not None
                and existing.released_at is None
                and existing.lease_expires_at > now
                and existing.owner_id != owner_id
            ):
                return LeaseClaimResult(claimed=False, claim=existing.as_claim())

            claimed_at = (
                existing.claimed_at
                if existing is not None and existing.owner_id == owner_id and existing.released_at is None
                else now
            )
            updated = _MutableLeaseEntry(
                queue_name=queue_name,
                item_id=item_id,
                owner_id=owner_id,
                claimed_at=claimed_at,
                last_heartbeat_at=now,
                lease_expires_at=lease_expires_at,
                released_at=None,
                release_reason=None,
            )
            self._entries[key] = updated
            return LeaseClaimResult(claimed=True, claim=updated.as_claim())

    def heartbeat(
        self,
        *,
        queue_name: str,
        item_id: str,
        owner_id: str,
        lease_duration_seconds: int,
        conn: Any | None = None,
    ) -> LeaseClaim | None:
        _ = conn
        now = self._clock()
        key = (queue_name, item_id)
        with self._lock:
            existing = self._entries.get(key)
            if existing is None or existing.released_at is not None or existing.owner_id != owner_id:
                return None
            existing.last_heartbeat_at = now
            existing.lease_expires_at = now + timedelta(seconds=max(1, int(lease_duration_seconds)))
            return existing.as_claim()

    def release(
        self,
        *,
        queue_name: str,
        item_id: str,
        owner_id: str,
        reason: str,
        conn: Any | None = None,
    ) -> bool:
        _ = conn
        key = (queue_name, item_id)
        with self._lock:
            existing = self._entries.get(key)
            if existing is None or existing.released_at is not None or existing.owner_id != owner_id:
                return False
            existing.released_at = self._clock()
            existing.release_reason = reason
            return True

    def list_stale_claims(
        self,
        *,
        queue_name: str,
        limit: int = 100,
        conn: Any | None = None,
    ) -> list[LeaseClaim]:
        _ = conn
        now = self._clock()
        with self._lock:
            stale = [
                entry.as_claim()
                for entry in self._entries.values()
                if entry.queue_name == queue_name
                and entry.released_at is None
                and entry.lease_expires_at <= now
            ]
        stale.sort(key=lambda claim: claim.lease_expires_at)
        return stale[: max(1, int(limit))]

    def mark_stale_recovered(
        self,
        *,
        queue_name: str,
        item_id: str,
        owner_id: str,
        conn: Any | None = None,
    ) -> bool:
        return self.release(
            queue_name=queue_name,
            item_id=item_id,
            owner_id=owner_id,
            reason="stale_recovered",
            conn=conn,
        )


class DatabaseWorkerLeaseStore:
    """DB-backed lease store for cross-worker ownership and expiry checks."""

    def __init__(self, db_service: Any) -> None:
        self._db_service = db_service
        self._schema_ready = False
        self._schema_lock = Lock()

    def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            if not hasattr(self._db_service, "execute_query"):
                raise RuntimeError("Worker lease store requires db_service.execute_query")
            self._db_service.execute_query(_CREATE_WORKER_LEASE_TABLE_SQL, fetch=False)
            self._db_service.execute_query(_CREATE_WORKER_LEASE_EXPIRY_INDEX_SQL, fetch=False)
            self._schema_ready = True

    def claim(
        self,
        *,
        queue_name: str,
        item_id: str,
        owner_id: str,
        lease_duration_seconds: int,
        conn: Any | None = None,
    ) -> LeaseClaimResult:
        self.ensure_schema()
        lease_seconds = max(1, int(lease_duration_seconds))
        rows = self._db_service.execute_query(
            """
            INSERT INTO refactor_worker_supervision_leases (
                queue_name,
                item_id,
                owner_id,
                claimed_at,
                last_heartbeat_at,
                lease_expires_at,
                released_at,
                release_reason,
                updated_at
            )
            VALUES (
                %s,
                %s,
                %s,
                NOW(),
                NOW(),
                NOW() + (%s || ' seconds')::interval,
                NULL,
                NULL,
                NOW()
            )
            ON CONFLICT (queue_name, item_id) DO UPDATE
               SET owner_id = EXCLUDED.owner_id,
                   claimed_at = CASE
                                   WHEN refactor_worker_supervision_leases.owner_id = EXCLUDED.owner_id
                                        AND refactor_worker_supervision_leases.released_at IS NULL
                                   THEN refactor_worker_supervision_leases.claimed_at
                                   ELSE NOW()
                               END,
                   last_heartbeat_at = NOW(),
                   lease_expires_at = NOW() + (%s || ' seconds')::interval,
                   released_at = NULL,
                   release_reason = NULL,
                   updated_at = NOW()
             WHERE refactor_worker_supervision_leases.released_at IS NOT NULL
                OR refactor_worker_supervision_leases.lease_expires_at <= NOW()
                OR refactor_worker_supervision_leases.owner_id = EXCLUDED.owner_id
            RETURNING
                queue_name,
                item_id,
                owner_id,
                claimed_at,
                last_heartbeat_at,
                lease_expires_at
            """,
            (queue_name, item_id, owner_id, lease_seconds, lease_seconds),
            fetch=True,
            conn=conn,
        )
        if not rows:
            return LeaseClaimResult(claimed=False, claim=None)
        return LeaseClaimResult(claimed=True, claim=self._build_claim(rows[0]))

    def heartbeat(
        self,
        *,
        queue_name: str,
        item_id: str,
        owner_id: str,
        lease_duration_seconds: int,
        conn: Any | None = None,
    ) -> LeaseClaim | None:
        self.ensure_schema()
        lease_seconds = max(1, int(lease_duration_seconds))
        rows = self._db_service.execute_query(
            """
            UPDATE refactor_worker_supervision_leases
               SET last_heartbeat_at = NOW(),
                   lease_expires_at = NOW() + (%s || ' seconds')::interval,
                   updated_at = NOW()
             WHERE queue_name = %s
               AND item_id = %s
               AND owner_id = %s
               AND released_at IS NULL
            RETURNING
                queue_name,
                item_id,
                owner_id,
                claimed_at,
                last_heartbeat_at,
                lease_expires_at
            """,
            (lease_seconds, queue_name, item_id, owner_id),
            fetch=True,
            conn=conn,
        )
        if not rows:
            return None
        return self._build_claim(rows[0])

    def release(
        self,
        *,
        queue_name: str,
        item_id: str,
        owner_id: str,
        reason: str,
        conn: Any | None = None,
    ) -> bool:
        self.ensure_schema()
        rows = self._db_service.execute_query(
            """
            UPDATE refactor_worker_supervision_leases
               SET released_at = NOW(),
                   release_reason = %s,
                   updated_at = NOW()
             WHERE queue_name = %s
               AND item_id = %s
               AND owner_id = %s
               AND released_at IS NULL
            RETURNING item_id
            """,
            (reason, queue_name, item_id, owner_id),
            fetch=True,
            conn=conn,
        )
        return bool(rows)

    def list_stale_claims(
        self,
        *,
        queue_name: str,
        limit: int = 100,
        conn: Any | None = None,
    ) -> list[LeaseClaim]:
        self.ensure_schema()
        rows = self._db_service.execute_query(
            """
            SELECT
                queue_name,
                item_id,
                owner_id,
                claimed_at,
                last_heartbeat_at,
                lease_expires_at
            FROM refactor_worker_supervision_leases
            WHERE queue_name = %s
              AND released_at IS NULL
              AND lease_expires_at <= NOW()
            ORDER BY lease_expires_at ASC
            LIMIT %s
            """,
            (queue_name, max(1, int(limit))),
            fetch=True,
            conn=conn,
        )
        return [self._build_claim(row) for row in rows or []]

    def mark_stale_recovered(
        self,
        *,
        queue_name: str,
        item_id: str,
        owner_id: str,
        conn: Any | None = None,
    ) -> bool:
        return self.release(
            queue_name=queue_name,
            item_id=item_id,
            owner_id=owner_id,
            reason="stale_recovered",
            conn=conn,
        )

    @staticmethod
    def _build_claim(row: Any) -> LeaseClaim:
        data = _as_mapping(row)
        return LeaseClaim(
            queue_name=str(data.get("queue_name") or ""),
            item_id=str(data.get("item_id") or ""),
            owner_id=str(data.get("owner_id") or ""),
            claimed_at=_coerce_datetime(data.get("claimed_at")),
            last_heartbeat_at=_coerce_datetime(data.get("last_heartbeat_at")),
            lease_expires_at=_coerce_datetime(data.get("lease_expires_at")),
        )

