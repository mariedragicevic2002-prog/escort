from __future__ import annotations

import pytest

from refactor.app.ingress.adaptive_rate_limiter import (
    AdaptiveIngressSignals,
    AdaptiveRateLimiterSettings,
    resolve_adaptive_rate_limit_decision,
    sample_adaptive_ingress_signals,
)
from refactor.app.ingress.backpressure_policy import IngressBackpressureDecision


def _backpressure(*, allow_enqueue: bool, behavior: str, reason: str) -> IngressBackpressureDecision:
    return IngressBackpressureDecision(
        allow_enqueue=allow_enqueue,
        overloaded=False,
        reason=reason,
        behavior=behavior,  # type: ignore[arg-type]
        trigger="none",
        provider_available=True,
        effective_max_attempts=5,
        queue_depth=6,
        oldest_lag_seconds=10.0,
    )


def _settings(**overrides) -> AdaptiveRateLimiterSettings:
    base = AdaptiveRateLimiterSettings(
        enabled=True,
        deterministic_mode=None,
        provider_failure_mode="sync_fallback",
        base_queue_depth=10,
        base_lag_seconds=120,
        retry_rate_threshold=0.35,
        failure_rate_threshold=0.3,
        throttle_max_attempts=1,
        reliability_sample_size=25,
    )
    return AdaptiveRateLimiterSettings(**{**base.__dict__, **overrides})


def test_threshold_adaptation_tightens_limits_under_retry_pressure() -> None:
    backpressure = _backpressure(allow_enqueue=True, behavior="degrade_mode", reason="backpressure_within_threshold")
    low_pressure = AdaptiveIngressSignals(
        queue_depth=6,
        oldest_lag_seconds=10.0,
        retry_rate=0.05,
        failure_rate=0.0,
        pending_sample_size=20,
        dead_sample_size=0,
        provider_available=True,
        source="stub",
    )
    high_pressure = AdaptiveIngressSignals(
        queue_depth=6,
        oldest_lag_seconds=10.0,
        retry_rate=0.6,
        failure_rate=0.1,
        pending_sample_size=20,
        dead_sample_size=2,
        provider_available=True,
        source="stub",
    )

    low_decision = resolve_adaptive_rate_limit_decision(
        settings=_settings(),
        signals=low_pressure,
        backpressure_decision=backpressure,
        requested_max_attempts=5,
    )
    high_decision = resolve_adaptive_rate_limit_decision(
        settings=_settings(),
        signals=high_pressure,
        backpressure_decision=backpressure,
        requested_max_attempts=5,
    )

    assert low_decision.mode == "allow"
    assert high_decision.mode == "throttle"
    assert high_decision.adjusted_queue_depth_threshold < low_decision.adjusted_queue_depth_threshold


def test_backpressure_reject_mode_precedence_overrides_deterministic_allow() -> None:
    decision = resolve_adaptive_rate_limit_decision(
        settings=_settings(deterministic_mode="allow"),
        signals=AdaptiveIngressSignals(
            queue_depth=1,
            oldest_lag_seconds=1.0,
            retry_rate=0.0,
            failure_rate=0.0,
            pending_sample_size=1,
            dead_sample_size=0,
            provider_available=True,
            source="stub",
        ),
        backpressure_decision=_backpressure(
            allow_enqueue=False,
            behavior="reject",
            reason="backpressure_reject",
        ),
        requested_max_attempts=5,
    )

    assert decision.mode == "reject"
    assert decision.allow_enqueue is False
    assert decision.reason == "backpressure_reject"


class _FailingSignalsProvider:
    def list_pending(self, *, limit: int = 100, conn=None):
        _ = (limit, conn)
        raise RuntimeError("provider unavailable")

    def list_dead(self, *, limit: int = 100, conn=None):
        _ = (limit, conn)
        raise RuntimeError("provider unavailable")


def test_provider_failure_falls_back_to_sync_mode() -> None:
    signals = sample_adaptive_ingress_signals(
        inbound_provider=_FailingSignalsProvider(),
        backpressure_decision=_backpressure(
            allow_enqueue=True,
            behavior="degrade_mode",
            reason="backpressure_within_threshold",
        ),
        sample_size=20,
    )
    decision = resolve_adaptive_rate_limit_decision(
        settings=_settings(provider_failure_mode="sync_fallback"),
        signals=signals,
        backpressure_decision=_backpressure(
            allow_enqueue=True,
            behavior="degrade_mode",
            reason="backpressure_within_threshold",
        ),
        requested_max_attempts=5,
    )

    assert signals.provider_available is False
    assert decision.mode == "sync_fallback"
    assert decision.allow_enqueue is False
    assert decision.reason == "adaptive_provider_unavailable_sync_fallback"


@pytest.mark.parametrize(
    ("deterministic_mode", "expected_allow"),
    [
        ("allow", True),
        ("throttle", True),
        ("reject", False),
        ("sync_fallback", False),
    ],
)
def test_deterministic_modes_are_applied_when_backpressure_is_permissive(
    deterministic_mode: str,
    expected_allow: bool,
) -> None:
    decision = resolve_adaptive_rate_limit_decision(
        settings=_settings(deterministic_mode=deterministic_mode),  # type: ignore[arg-type]
        signals=AdaptiveIngressSignals(
            queue_depth=1,
            oldest_lag_seconds=1.0,
            retry_rate=0.0,
            failure_rate=0.0,
            pending_sample_size=1,
            dead_sample_size=0,
            provider_available=True,
            source="stub",
        ),
        backpressure_decision=_backpressure(
            allow_enqueue=True,
            behavior="degrade_mode",
            reason="backpressure_within_threshold",
        ),
        requested_max_attempts=5,
    )

    assert decision.mode == deterministic_mode
    assert decision.allow_enqueue is expected_allow
    assert decision.reason == f"adaptive_mode_{deterministic_mode}"
