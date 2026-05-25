from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class QueueHealthSnapshot:
    queue_name: str
    sampled_pending_depth: int
    sampled_dead_depth: int
    retry_ratio: float
    oldest_lag_seconds: float
    sample_window: int
    source: str
    status: str
    cost_throttle_advisory: Mapping[str, Any] = field(default_factory=dict)
    queue_compaction_hint: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "queue_name": self.queue_name,
            "sampled_pending_depth": max(0, int(self.sampled_pending_depth)),
            "sampled_dead_depth": max(0, int(self.sampled_dead_depth)),
            "retry_ratio": round(max(0.0, min(1.0, float(self.retry_ratio))), 4),
            "oldest_lag_seconds": round(max(0.0, float(self.oldest_lag_seconds)), 3),
            "sample_window": max(1, int(self.sample_window)),
            "source": str(self.source or "unknown"),
            "status": str(self.status or "unknown"),
            "cost_throttle_advisory": dict(self.cost_throttle_advisory),
            "queue_compaction_hint": dict(self.queue_compaction_hint),
        }


@dataclass(frozen=True)
class RolloutExposureSnapshot:
    feature: str
    use_feature: bool
    reason: str
    enabled: bool
    canary_percent: int
    canary_bucket: int
    emergency_rollback: bool
    rollout_exposed: bool
    rollback_activated: bool
    safeguard_fallback: bool
    degrade_activated: bool
    guardrail_action: str
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature": str(self.feature or "unknown"),
            "use_feature": bool(self.use_feature),
            "reason": str(self.reason or "unknown"),
            "enabled": bool(self.enabled),
            "canary_percent": max(0, min(100, int(self.canary_percent))),
            "canary_bucket": max(0, min(99, int(self.canary_bucket))),
            "emergency_rollback": bool(self.emergency_rollback),
            "rollout_exposed": bool(self.rollout_exposed),
            "rollback_activated": bool(self.rollback_activated),
            "safeguard_fallback": bool(self.safeguard_fallback),
            "degrade_activated": bool(self.degrade_activated),
            "guardrail_action": str(self.guardrail_action or "observe"),
            "status": str(self.status or "unknown"),
        }


@dataclass(frozen=True)
class WorkerSupervisionQueueHealth:
    queue_name: str
    stale_claim_count: int
    sampled_claim_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "queue_name": str(self.queue_name or "unknown"),
            "stale_claim_count": max(0, int(self.stale_claim_count)),
            "sampled_claim_ids": [str(item) for item in self.sampled_claim_ids],
        }


@dataclass(frozen=True)
class WorkerSupervisionHealth:
    status: str
    stale_claim_count: int
    queues: tuple[WorkerSupervisionQueueHealth, ...]
    rollout: RolloutExposureSnapshot

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": str(self.status or "unknown"),
            "stale_claim_count": max(0, int(self.stale_claim_count)),
            "queues": [item.to_dict() for item in self.queues],
            "rollout": self.rollout.to_dict(),
        }


@dataclass(frozen=True)
class GuardrailActionSnapshot:
    action: str
    reason: str
    occurred_at: str
    triggered_signals: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": str(self.action or "observe"),
            "reason": str(self.reason or "unknown"),
            "occurred_at": str(self.occurred_at or ""),
            "triggered_signals": [str(item) for item in self.triggered_signals],
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class GuardrailHealthSnapshot:
    feature: str
    status: str
    policy_enabled: bool
    state_action: str
    decision_action: str
    reason: str
    sample_size: int
    window_seconds: int
    triggered_signals: tuple[str, ...] = ()
    recent_actions: tuple[GuardrailActionSnapshot, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature": str(self.feature or "unknown"),
            "status": str(self.status or "unknown"),
            "policy_enabled": bool(self.policy_enabled),
            "state_action": str(self.state_action or "observe"),
            "decision_action": str(self.decision_action or "observe"),
            "reason": str(self.reason or "unknown"),
            "sample_size": max(0, int(self.sample_size)),
            "window_seconds": max(1, int(self.window_seconds)),
            "triggered_signals": [str(item) for item in self.triggered_signals],
            "recent_actions": [item.to_dict() for item in self.recent_actions],
        }


@dataclass(frozen=True)
class ProductionReadinessReport:
    schema_version: str
    generated_at: str
    window_seconds: int
    overall_status: str
    queues: tuple[QueueHealthSnapshot, ...]
    worker_supervision: WorkerSupervisionHealth
    guardrails: tuple[GuardrailHealthSnapshot, ...]
    rollouts: tuple[RolloutExposureSnapshot, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": str(self.schema_version or "v1"),
            "generated_at": str(self.generated_at or ""),
            "window_seconds": max(1, int(self.window_seconds)),
            "overall_status": str(self.overall_status or "unknown"),
            "queues": [item.to_dict() for item in self.queues],
            "worker_supervision": self.worker_supervision.to_dict(),
            "guardrails": [item.to_dict() for item in self.guardrails],
            "rollouts": [item.to_dict() for item in self.rollouts],
        }
