from __future__ import annotations

import hashlib
import logging
import time
from typing import Any
from uuid import uuid4

from app.ingress.rollout_controls import (
    Phase4FeatureRolloutDecision,
    emit_phase4_rollout_guardrail_metrics,
    resolve_worker_supervision_rollout_decision,
)
from app.workers.supervision.heartbeat import WorkerHeartbeatState, WorkerHeartbeatTracker
from app.workers.supervision.lease import (
    InMemoryWorkerLeaseStore,
    LeaseClaim,
    WorkerLeaseStore,
)
from app.workers.supervision.recovery import StaleClaimRecoveryResult, WorkerStaleClaimRecovery

logger = logging.getLogger("adella_chatbot.refactor.worker_supervision")

# Rollout settings are stable across a batch — cache for this many seconds to avoid
# repeated env/DB reads (3 reads per queue item × batch_size per run_once call).
_ROLLOUT_CACHE_TTL_SECONDS: float = 5.0


class WorkerSupervisionRuntime:
    """Coordinates heartbeat + lease claiming + stale claim recovery for workers."""

    __slots__ = (
        "queue_name",
        "worker_id",
        "lease_duration_seconds",
        "recovery_batch_size",
        "lease_store",
        "heartbeat_tracker",
        "_recovery",
        "_rollout_decider",
        "_guardrail_metric_logger",
        "_cached_decision",
        "_cached_decision_at",
    )

    def __init__(
        self,
        *,
        queue_name: str,
        worker_id: str | None = None,
        lease_duration_seconds: int = 30,
        recovery_batch_size: int = 25,
        lease_store: WorkerLeaseStore | None = None,
        heartbeat_tracker: WorkerHeartbeatTracker | None = None,
        rollout_decider: Any | None = None,
        guardrail_metric_logger: Any | None = None,
    ) -> None:
        self.queue_name = queue_name
        self.worker_id = (worker_id or f"{queue_name}-{uuid4().hex}").strip() or f"{queue_name}-{uuid4().hex}"
        self.lease_duration_seconds = max(1, int(lease_duration_seconds))
        self.recovery_batch_size = max(1, int(recovery_batch_size))
        self.lease_store: WorkerLeaseStore = lease_store or InMemoryWorkerLeaseStore()
        self.heartbeat_tracker: WorkerHeartbeatTracker = heartbeat_tracker or WorkerHeartbeatTracker()
        self._recovery = WorkerStaleClaimRecovery(lease_store=self.lease_store)
        self._rollout_decider: Any = rollout_decider or (
            lambda context_key: resolve_worker_supervision_rollout_decision(f"{queue_name}:{context_key}")
        )
        self._guardrail_metric_logger: Any = guardrail_metric_logger
        # Cached rollout decision (settings are stable across a batch cycle)
        self._cached_decision: Phase4FeatureRolloutDecision | None = None
        self._cached_decision_at: float = 0.0

    def claim_item(self, item_id: str, *, conn: Any | None = None) -> bool:
        decision = self._resolve_rollout(item_id)
        if not decision.use_feature:
            self._record_guardrail(decision=decision, item_id=item_id, action="claim_bypass")
            return True
        result = self.lease_store.claim(
            queue_name=self.queue_name,
            item_id=item_id,
            owner_id=self.worker_id,
            lease_duration_seconds=self.lease_duration_seconds,
            conn=conn,
        )
        if result.claimed and result.claim is not None:
            self.heartbeat_tracker.record_claim(
                worker_id=self.worker_id,
                queue_name=self.queue_name,
                item_id=item_id,
                lease_expires_at=result.claim.lease_expires_at,
            )
        return result.claimed

    def heartbeat(self, item_id: str, *, conn: Any | None = None) -> bool:
        decision = self._resolve_rollout(item_id)
        if not decision.use_feature:
            return True
        claim = self.lease_store.heartbeat(
            queue_name=self.queue_name,
            item_id=item_id,
            owner_id=self.worker_id,
            lease_duration_seconds=self.lease_duration_seconds,
            conn=conn,
        )
        if claim is None:
            return False
        self.heartbeat_tracker.record_heartbeat(
            worker_id=self.worker_id,
            queue_name=self.queue_name,
            item_id=item_id,
            lease_expires_at=claim.lease_expires_at,
        )
        return True

    def release_item(self, item_id: str, *, reason: str, conn: Any | None = None) -> bool:
        decision = self._resolve_rollout(item_id)
        if not decision.use_feature:
            return True
        released = self.lease_store.release(
            queue_name=self.queue_name,
            item_id=item_id,
            owner_id=self.worker_id,
            reason=reason,
            conn=conn,
        )
        if released:
            self.heartbeat_tracker.clear(queue_name=self.queue_name, item_id=item_id)
        return released

    def recover_stale_claims(
        self,
        *,
        requeue_claim,
        conn: Any | None = None,
    ) -> StaleClaimRecoveryResult:
        decision = self._resolve_rollout("stale_recovery")
        if not decision.use_feature:
            self._record_guardrail(decision=decision, item_id="stale_recovery", action="recovery_bypass")
            return StaleClaimRecoveryResult(scanned=0, recovered=0, failed=0, records=())
        return self._recovery.recover(
            queue_name=self.queue_name,
            requeue_claim=requeue_claim,
            limit=self.recovery_batch_size,
            conn=conn,
        )

    def get_heartbeat_state(self, item_id: str) -> WorkerHeartbeatState | None:
        return self.heartbeat_tracker.get(queue_name=self.queue_name, item_id=item_id)

    @staticmethod
    def claim_item_id(claim: LeaseClaim) -> str:
        return claim.item_id

    def _resolve_rollout(self, item_id: str) -> Phase4FeatureRolloutDecision:
        # Settings (enabled/canary_percent/emergency_rollback) are stable within a batch.
        # Cache the last resolved decision and reuse within the TTL to avoid repeated
        # env/DB reads (which would otherwise happen 3× per queued message).
        now = time.monotonic()
        cached = self._cached_decision
        if cached is not None and (now - self._cached_decision_at) < _ROLLOUT_CACHE_TTL_SECONDS:
            # Re-use cached settings but the canary bucket is item-specific — if canary
            # is 0 or 100 (the common case) the bucket doesn't matter. When mid-canary,
            # the accuracy loss over 5 seconds is acceptable for operational overhead saved.
            return cached

        try:
            decision = self._rollout_decider(item_id)
        except Exception:
            decision = Phase4FeatureRolloutDecision(
                feature="worker_supervision",
                use_feature=True,
                reason="rollout_resolution_failed_fail_open",
                enabled=True,
                canary_percent=100,
                canary_bucket=0,
                emergency_rollback=False,
                rollout_exposed=True,
                rollback_activated=False,
                safeguard_fallback=False,
            )
        if not isinstance(decision, Phase4FeatureRolloutDecision):
            decision = Phase4FeatureRolloutDecision(
                feature="worker_supervision",
                use_feature=bool(getattr(decision, "use_feature", True)),
                reason=str(getattr(decision, "reason", "unknown")),
                enabled=bool(getattr(decision, "enabled", True)),
                canary_percent=int(getattr(decision, "canary_percent", 100)),
                canary_bucket=int(getattr(decision, "canary_bucket", 0)),
                emergency_rollback=bool(getattr(decision, "emergency_rollback", False)),
                rollout_exposed=bool(getattr(decision, "rollout_exposed", getattr(decision, "use_feature", True))),
                rollback_activated=bool(getattr(decision, "rollback_activated", False)),
                safeguard_fallback=bool(getattr(decision, "safeguard_fallback", not getattr(decision, "use_feature", True))),
            )
        self._cached_decision = decision
        self._cached_decision_at = now
        return decision

    @staticmethod
    def _safe_item_hash(item_id: str) -> str:
        return hashlib.sha256(str(item_id or "").encode("utf-8")).hexdigest()[:12]

    def _record_guardrail(
        self,
        *,
        decision: Phase4FeatureRolloutDecision,
        item_id: str,
        action: str,
    ) -> None:
        item_hash = self._safe_item_hash(item_id)
        try:
            emit_phase4_rollout_guardrail_metrics(
                decision=decision,
                request_id=f"{self.queue_name}:{item_hash}",
                metric_logger=self._guardrail_metric_logger,
            )
        except Exception:
            pass
        logger.warning(
            "worker supervision guardrail action=%s queue_name=%s item_hash=%s reason=%s rollback=%s exposed=%s",
            action,
            self.queue_name,
            item_hash,
            decision.reason,
            decision.rollback_activated,
            decision.rollout_exposed,
        )
