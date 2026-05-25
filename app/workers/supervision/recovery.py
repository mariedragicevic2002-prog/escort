from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from app.workers.supervision.lease import LeaseClaim, WorkerLeaseStore

StaleClaimRequeue = Callable[[LeaseClaim], bool]


@dataclass(frozen=True)
class StaleClaimRecoveryRecord:
    claim: LeaseClaim
    recovered: bool
    error: str | None = None


@dataclass(frozen=True)
class StaleClaimRecoveryResult:
    scanned: int
    recovered: int
    failed: int
    records: tuple[StaleClaimRecoveryRecord, ...]


class WorkerStaleClaimRecovery:
    """Detects expired claims and invokes queue-specific requeue callbacks."""

    def __init__(self, *, lease_store: WorkerLeaseStore) -> None:
        self._lease_store = lease_store

    def recover(
        self,
        *,
        queue_name: str,
        requeue_claim: StaleClaimRequeue,
        limit: int = 100,
        conn: Any | None = None,
    ) -> StaleClaimRecoveryResult:
        stale_claims = self._lease_store.list_stale_claims(
            queue_name=queue_name,
            limit=max(1, int(limit)),
            conn=conn,
        )
        records: list[StaleClaimRecoveryRecord] = []
        recovered_count = 0
        failed_count = 0
        for claim in stale_claims:
            try:
                recovered = bool(requeue_claim(claim))
            except Exception as exc:
                failed_count += 1
                records.append(StaleClaimRecoveryRecord(claim=claim, recovered=False, error=str(exc)))
                continue
            if recovered:
                self._lease_store.mark_stale_recovered(
                    queue_name=claim.queue_name,
                    item_id=claim.item_id,
                    owner_id=claim.owner_id,
                    conn=conn,
                )
                recovered_count += 1
                records.append(StaleClaimRecoveryRecord(claim=claim, recovered=True))
                continue
            failed_count += 1
            records.append(StaleClaimRecoveryRecord(claim=claim, recovered=False))

        return StaleClaimRecoveryResult(
            scanned=len(stale_claims),
            recovered=recovered_count,
            failed=failed_count,
            records=tuple(records),
        )

