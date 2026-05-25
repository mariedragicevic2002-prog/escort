from __future__ import annotations

from app.cost_controls.contracts import (
    CostControlAdvisoryBundle,
    CostThrottleAdvisory,
    QueueCompactionHint,
    QueueCostSignals,
)


def _signal_sample_size(signals: QueueCostSignals) -> int:
    return max(1, int(signals.sample_size))


def _normalized_retry_ratio(signals: QueueCostSignals) -> float:
    return max(0.0, min(1.0, float(signals.retry_ratio)))


def _compaction_pressure(signals: QueueCostSignals) -> float:
    sample_size = _signal_sample_size(signals)
    backlog_component = min(1.0, float(max(0, int(signals.queue_depth))) / float(sample_size))
    retry_component = _normalized_retry_ratio(signals)
    dead_component = min(1.0, float(max(0, int(signals.dead_depth))) / float(sample_size))
    lag_component = min(1.0, max(0.0, float(signals.oldest_lag_seconds)) / 900.0)
    return (backlog_component * 0.35) + (retry_component * 0.35) + (dead_component * 0.2) + (lag_component * 0.1)


def build_queue_compaction_hint(*, signals: QueueCostSignals) -> QueueCompactionHint:
    if not signals.provider_available:
        return QueueCompactionHint(
            should_compact=False,
            strategy="observe",
            reason="signals_unavailable",
            severity="medium",
            pressure_score=0.0,
        )

    sample_size = _signal_sample_size(signals)
    retry_ratio = _normalized_retry_ratio(signals)
    dead_depth = max(0, int(signals.dead_depth))
    queue_depth = max(0, int(signals.queue_depth))
    pressure = _compaction_pressure(signals)

    if dead_depth >= max(3, sample_size // 4):
        return QueueCompactionHint(
            should_compact=True,
            strategy="archive_dead_letter",
            reason="dead_backlog_pressure",
            severity="high",
            pressure_score=pressure,
        )
    if retry_ratio >= 0.5 and queue_depth >= max(8, sample_size // 2):
        return QueueCompactionHint(
            should_compact=True,
            strategy="compact_retries",
            reason="retry_backlog_pressure",
            severity="high",
            pressure_score=pressure,
        )
    if queue_depth >= max(20, sample_size):
        return QueueCompactionHint(
            should_compact=True,
            strategy="coalesce_pending",
            reason="pending_backlog_pressure",
            severity="medium",
            pressure_score=pressure,
        )
    return QueueCompactionHint(
        should_compact=False,
        strategy="observe",
        reason="within_compaction_budget",
        severity="low",
        pressure_score=pressure,
    )


def build_cost_throttle_advisory(*, signals: QueueCostSignals) -> CostThrottleAdvisory:
    if not signals.provider_available:
        return CostThrottleAdvisory(
            advised_mode="sync_fallback",
            reason="signals_unavailable",
            severity="high",
            pressure_score=1.0,
            signals_available=False,
            safeguard_fallback=True,
        )

    score = _compaction_pressure(signals)
    sample_size = _signal_sample_size(signals)
    queue_depth = max(0, int(signals.queue_depth))
    retry_ratio = _normalized_retry_ratio(signals)
    dead_depth = max(0, int(signals.dead_depth))
    critical_dead = dead_depth >= max(4, sample_size // 3)
    heavy_backlog = queue_depth >= max(sample_size, 12)

    if critical_dead or (retry_ratio >= 0.75 and heavy_backlog):
        advised_mode = "reject"
        reason = "critical_failure_pressure"
        severity = "high"
    elif score >= 0.45 or retry_ratio >= 0.35 or heavy_backlog:
        advised_mode = "throttle"
        reason = "cost_pressure_throttle"
        severity = "medium"
    else:
        advised_mode = "allow"
        reason = "cost_pressure_within_budget"
        severity = "low"

    return CostThrottleAdvisory(
        advised_mode=advised_mode,
        reason=reason,
        severity=severity,
        pressure_score=max(0.0, min(1.0, float(score))),
        signals_available=True,
        safeguard_fallback=advised_mode in {"reject", "sync_fallback"},
    )


def build_cost_control_advisories(*, signals: QueueCostSignals) -> CostControlAdvisoryBundle:
    return CostControlAdvisoryBundle(
        throttle=build_cost_throttle_advisory(signals=signals),
        compaction=build_queue_compaction_hint(signals=signals),
    )
