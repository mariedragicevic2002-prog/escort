from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class SLOGuardrailAction(str, Enum):
    OBSERVE = "observe"
    DEGRADE = "degrade"
    ROLLBACK = "rollback"


@dataclass(frozen=True)
class SLOGuardrailSignals:
    retry_rate: float | None = None
    dead_letter_rate: float | None = None
    queue_lag_seconds: float | None = None
    failure_ratio: float | None = None
    error_budget_remaining: float | None = None
    sample_size: int = 0
    window_seconds: int = 300

    def normalized(self) -> SLOGuardrailSignals:
        return SLOGuardrailSignals(
            retry_rate=_ratio_or_none(self.retry_rate),
            dead_letter_rate=_ratio_or_none(self.dead_letter_rate),
            queue_lag_seconds=_non_negative_or_none(self.queue_lag_seconds),
            failure_ratio=_ratio_or_none(self.failure_ratio),
            error_budget_remaining=_ratio_or_none(self.error_budget_remaining),
            sample_size=max(0, int(self.sample_size)),
            window_seconds=max(1, int(self.window_seconds)),
        )


@dataclass(frozen=True)
class SLOGuardrailPolicy:
    enabled: bool = False
    min_sample_size: int = 25
    cooldown_seconds: int = 300
    hysteresis_factor: float = 0.8

    degrade_retry_rate: float = 0.08
    degrade_dead_letter_rate: float = 0.03
    degrade_queue_lag_seconds: float = 90.0
    degrade_failure_ratio: float = 0.05
    degrade_error_budget_remaining: float = 0.50

    rollback_retry_rate: float = 0.18
    rollback_dead_letter_rate: float = 0.08
    rollback_queue_lag_seconds: float = 240.0
    rollback_failure_ratio: float = 0.15
    rollback_error_budget_remaining: float = 0.20


@dataclass(frozen=True)
class SLOGuardrailState:
    action: SLOGuardrailAction = SLOGuardrailAction.OBSERVE
    last_transition_at: datetime | None = None
    cooldown_until: datetime | None = None


@dataclass(frozen=True)
class SLOGuardrailDecision:
    action: SLOGuardrailAction
    reason: str
    triggered_signals: tuple[str, ...] = ()
    previous_action: SLOGuardrailAction = SLOGuardrailAction.OBSERVE
    transitioned: bool = False
    in_cooldown: bool = False
    cooldown_until: datetime | None = None
    audit_details: dict[str, Any] = field(default_factory=dict)

    def metric_tags(self, *, feature: str, request_id: str = "") -> dict[str, Any]:
        return {
            "feature": str(feature or "unknown"),
            "request_id": str(request_id or ""),
            "action": self.action.value,
            "previous_action": self.previous_action.value,
            "reason": str(self.reason or "unknown"),
            "triggered_signals": ",".join(self.triggered_signals),
            "signal_count": len(self.triggered_signals),
            "transitioned": bool(self.transitioned),
            "in_cooldown": bool(self.in_cooldown),
            "cooldown_until": self.cooldown_until.isoformat() if self.cooldown_until else "",
            **{str(k): v for k, v in self.audit_details.items()},
        }


def _ratio_or_none(value: float | None) -> float | None:
    if value is None:
        return None
    return max(0.0, min(1.0, float(value)))


def _non_negative_or_none(value: float | None) -> float | None:
    if value is None:
        return None
    return max(0.0, float(value))
