from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

CostThrottleMode = Literal["allow", "throttle", "reject", "sync_fallback"]
CostSeverity = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class ProcessingBudgetSettings:
    max_items_per_worker_pass: int = 25
    max_items_per_interval: int = 500
    interval_seconds: int = 60


@dataclass(frozen=True)
class ProcessingBudgetDecision:
    requested_items: int
    allowed_items: int
    interval_remaining: int
    pass_capped: bool
    interval_capped: bool
    reason: str


@dataclass(frozen=True)
class QueueCostSignals:
    queue_depth: int
    retry_ratio: float
    dead_depth: int
    oldest_lag_seconds: float
    sample_size: int
    provider_available: bool
    source: str = "unknown"


@dataclass(frozen=True)
class QueueCompactionHint:
    should_compact: bool
    strategy: str
    reason: str
    severity: CostSeverity
    pressure_score: float

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "should_compact": bool(self.should_compact),
            "strategy": str(self.strategy or "observe"),
            "reason": str(self.reason or "unknown"),
            "severity": str(self.severity or "low"),
            "pressure_score": round(max(0.0, float(self.pressure_score)), 4),
        }


@dataclass(frozen=True)
class CostThrottleAdvisory:
    advised_mode: CostThrottleMode
    reason: str
    severity: CostSeverity
    pressure_score: float
    signals_available: bool
    safeguard_fallback: bool

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "advised_mode": str(self.advised_mode),
            "reason": str(self.reason or "unknown"),
            "severity": str(self.severity or "low"),
            "pressure_score": round(max(0.0, min(1.0, float(self.pressure_score))), 4),
            "signals_available": bool(self.signals_available),
            "safeguard_fallback": bool(self.safeguard_fallback),
        }


@dataclass(frozen=True)
class CostControlAdvisoryBundle:
    throttle: CostThrottleAdvisory
    compaction: QueueCompactionHint

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "throttle": self.throttle.to_public_dict(),
            "compaction": self.compaction.to_public_dict(),
        }
