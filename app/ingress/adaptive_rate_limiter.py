from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping, cast

from app.config_governance import (
    ConfigFieldContract,
    ConfigRegistryContract,
    TypedConfigRegistry,
    allowed_values,
    numeric_bounds,
    resolve_registered_contract,
)
from app.ingress.backpressure_policy import IngressBackpressureDecision
from app.ingress.rollout_controls import SettingsGetter
from app.queue.providers import InboundQueueProvider
from app.queue.status import QueueStatus

logger = logging.getLogger(__name__)

RateLimitMode = Literal["allow", "throttle", "reject", "sync_fallback"]
_ALLOWED_MODES: tuple[RateLimitMode, ...] = ("allow", "throttle", "reject", "sync_fallback")


@dataclass(frozen=True)
class AdaptiveRateLimiterSettings:
    enabled: bool
    deterministic_mode: RateLimitMode | None
    provider_failure_mode: RateLimitMode
    base_queue_depth: int
    base_lag_seconds: int
    retry_rate_threshold: float
    failure_rate_threshold: float
    throttle_max_attempts: int
    reliability_sample_size: int


@dataclass(frozen=True)
class AdaptiveIngressSignals:
    queue_depth: int
    oldest_lag_seconds: float
    retry_rate: float
    failure_rate: float
    pending_sample_size: int
    dead_sample_size: int
    provider_available: bool
    source: str


@dataclass(frozen=True)
class AdaptiveRateLimitDecision:
    mode: RateLimitMode
    allow_enqueue: bool
    reason: str
    effective_max_attempts: int
    adjusted_queue_depth_threshold: int
    adjusted_lag_seconds_threshold: float
    queue_depth: int
    oldest_lag_seconds: float
    retry_rate: float
    failure_rate: float
    provider_available: bool
    backpressure_overloaded: bool
    backpressure_behavior: str

    def metric_tags(self, *, channel: str, request_id: str) -> dict[str, Any]:
        return {
            "channel": str(channel or "unknown"),
            "request_id": str(request_id or ""),
            "mode": self.mode,
            "allow_enqueue": bool(self.allow_enqueue),
            "reason": str(self.reason or "unknown"),
            "effective_max_attempts": max(1, int(self.effective_max_attempts)),
            "adjusted_queue_depth_threshold": max(1, int(self.adjusted_queue_depth_threshold)),
            "adjusted_lag_seconds_threshold": round(max(1.0, float(self.adjusted_lag_seconds_threshold)), 3),
            "queue_depth": max(0, int(self.queue_depth)),
            "oldest_lag_seconds": round(max(0.0, float(self.oldest_lag_seconds)), 3),
            "retry_rate": round(max(0.0, min(1.0, float(self.retry_rate))), 4),
            "failure_rate": round(max(0.0, min(1.0, float(self.failure_rate))), 4),
            "provider_available": bool(self.provider_available),
            "backpressure_overloaded": bool(self.backpressure_overloaded),
            "backpressure_behavior": str(self.backpressure_behavior or "unknown"),
        }


def _default_setting_getter() -> SettingsGetter:
    from core.settings_manager import get_setting  # noqa: PLC0415

    return get_setting


def _read_setting(
    *,
    env: Mapping[str, str],
    setting_keys: tuple[str, ...],
    env_keys: tuple[str, ...],
    default: Any,
    setting_getter: SettingsGetter | None,
) -> Any:
    for env_key in env_keys:
        if env_key in env:
            return env.get(env_key)

    getter = setting_getter
    if getter is None:
        try:
            getter = _default_setting_getter()
        except Exception:
            return default

    sentinel = object()
    for setting_key in setting_keys:
        try:
            value = getter(setting_key, sentinel)
        except TypeError:
            try:
                value = getter(setting_key)
            except Exception:
                continue
        except Exception:
            continue
        if value is sentinel or value is None:
            continue
        return value
    return default


def _to_bool(value: Any, *, default: bool) -> bool:
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _to_positive_int(value: Any, *, default: int, minimum: int = 1, maximum: int = 100000) -> int:
    try:
        parsed = int(float(str(value).strip()))
    except Exception:
        parsed = int(default)
    return max(minimum, min(maximum, parsed))


def _to_ratio(value: Any, *, default: float) -> float:
    try:
        parsed = float(str(value).strip())
    except Exception:
        parsed = float(default)
    return max(0.0, min(1.0, parsed))


def _to_mode(value: Any, *, default: RateLimitMode) -> RateLimitMode:
    normalized = str(value or "").strip().lower()
    if normalized in _ALLOWED_MODES:
        return normalized  # type: ignore[return-value]
    return default


def _to_optional_mode(value: Any) -> RateLimitMode | None:
    normalized = str(value or "").strip().lower()
    if normalized in {"", "auto", "default", "none"}:
        return None
    if normalized in _ALLOWED_MODES:
        return normalized  # type: ignore[return-value]
    return None


def _setting_candidates(channel: str | None, suffix: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    normalized_channel = str(channel or "").strip().lower()
    setting_keys: list[str] = []
    env_keys: list[str] = []
    if normalized_channel:
        setting_keys.append(f"refactor_{normalized_channel}_adaptive_rate_limiter_{suffix}")
        env_keys.append(f"REFACTOR_{normalized_channel.upper()}_ADAPTIVE_RATE_LIMITER_{suffix.upper()}")
    setting_keys.append(f"refactor_ingress_adaptive_rate_limiter_{suffix}")
    env_keys.append(f"REFACTOR_INGRESS_ADAPTIVE_RATE_LIMITER_{suffix.upper()}")
    return tuple(setting_keys), tuple(env_keys)


def _parse_strict_bool(value: Any) -> bool:
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"unsupported boolean value={value}")


def _parse_strict_positive_int(value: Any) -> int:
    return int(float(str(value).strip()))


def _parse_strict_ratio(value: Any) -> float:
    return float(str(value).strip())


def _parse_strict_mode(value: Any) -> RateLimitMode:
    normalized = str(value or "").strip().lower()
    if normalized not in _ALLOWED_MODES:
        raise ValueError(f"unsupported mode={value}")
    return normalized  # type: ignore[return-value]


def _parse_strict_optional_mode(value: Any) -> RateLimitMode | None:
    normalized = str(value or "").strip().lower()
    if normalized in {"", "auto", "default", "none"}:
        return None
    if normalized not in _ALLOWED_MODES:
        raise ValueError(f"unsupported optional mode={value}")
    return normalized  # type: ignore[return-value]


_ADAPTIVE_NAMESPACE = "ingress_adaptive_rate_limiter_settings"
_ADAPTIVE_REGISTRY = TypedConfigRegistry()
_ADAPTIVE_REGISTRY.register(
    ConfigRegistryContract(
        namespace=_ADAPTIVE_NAMESPACE,
        fields={
            "enabled": ConfigFieldContract(
                name="enabled",
                default=True,
                parser=_parse_strict_bool,
                fallback_resolver=lambda raw, default: _to_bool(raw, default=default),
            ),
            "deterministic_mode": ConfigFieldContract(
                name="deterministic_mode",
                default=None,
                parser=_parse_strict_optional_mode,
                fallback_resolver=lambda raw, _default: _to_optional_mode(raw),
            ),
            "provider_failure_mode": ConfigFieldContract(
                name="provider_failure_mode",
                default="sync_fallback",
                parser=_parse_strict_mode,
                validators=(allowed_values(_ALLOWED_MODES),),
                fallback_resolver=lambda raw, default: _to_mode(raw, default=default),  # type: ignore[arg-type]
            ),
            "base_queue_depth": ConfigFieldContract(
                name="base_queue_depth",
                default=250,
                parser=_parse_strict_positive_int,
                validators=(numeric_bounds(minimum=1, maximum=100000),),
                fallback_resolver=lambda raw, default: _to_positive_int(
                    raw,
                    default=default,
                    minimum=1,
                    maximum=100000,
                ),
            ),
            "base_lag_seconds": ConfigFieldContract(
                name="base_lag_seconds",
                default=90,
                parser=_parse_strict_positive_int,
                validators=(numeric_bounds(minimum=1, maximum=100000),),
                fallback_resolver=lambda raw, default: _to_positive_int(
                    raw,
                    default=default,
                    minimum=1,
                    maximum=100000,
                ),
            ),
            "retry_rate_threshold": ConfigFieldContract(
                name="retry_rate_threshold",
                default=0.4,
                parser=_parse_strict_ratio,
                validators=(numeric_bounds(minimum=0.0, maximum=1.0),),
                fallback_resolver=lambda raw, default: _to_ratio(raw, default=default),
            ),
            "failure_rate_threshold": ConfigFieldContract(
                name="failure_rate_threshold",
                default=0.2,
                parser=_parse_strict_ratio,
                validators=(numeric_bounds(minimum=0.0, maximum=1.0),),
                fallback_resolver=lambda raw, default: _to_ratio(raw, default=default),
            ),
            "throttle_max_attempts": ConfigFieldContract(
                name="throttle_max_attempts",
                default=1,
                parser=_parse_strict_positive_int,
                validators=(numeric_bounds(minimum=1, maximum=20),),
                fallback_resolver=lambda raw, default: _to_positive_int(
                    raw,
                    default=default,
                    minimum=1,
                    maximum=20,
                ),
            ),
            "reliability_sample_size": ConfigFieldContract(
                name="reliability_sample_size",
                default=25,
                parser=_parse_strict_positive_int,
                validators=(numeric_bounds(minimum=1, maximum=500),),
                fallback_resolver=lambda raw, default: _to_positive_int(
                    raw,
                    default=default,
                    minimum=1,
                    maximum=500,
                ),
            ),
        },
    )
)


def load_adaptive_rate_limiter_settings(
    *,
    channel: str | None,
    base_queue_depth: int,
    base_lag_seconds: int,
    env: Mapping[str, str] | None = None,
    setting_getter: SettingsGetter | None = None,
    strict: bool = False,
) -> AdaptiveRateLimiterSettings:
    source_env = env or os.environ
    enabled_keys, enabled_env = _setting_candidates(channel, "enabled")
    mode_keys, mode_env = _setting_candidates(channel, "mode")
    provider_mode_keys, provider_mode_env = _setting_candidates(channel, "provider_failure_mode")
    retry_keys, retry_env = _setting_candidates(channel, "retry_rate_threshold")
    failure_keys, failure_env = _setting_candidates(channel, "failure_rate_threshold")
    throttle_keys, throttle_env = _setting_candidates(channel, "throttle_max_attempts")
    sample_keys, sample_env = _setting_candidates(channel, "reliability_sample_size")

    enabled_raw = _read_setting(
        env=source_env,
        setting_keys=enabled_keys,
        env_keys=enabled_env,
        default=True,
        setting_getter=setting_getter,
    )
    mode_raw = _read_setting(
        env=source_env,
        setting_keys=mode_keys,
        env_keys=mode_env,
        default="auto",
        setting_getter=setting_getter,
    )
    provider_mode_raw = _read_setting(
        env=source_env,
        setting_keys=provider_mode_keys,
        env_keys=provider_mode_env,
        default="sync_fallback",
        setting_getter=setting_getter,
    )
    retry_raw = _read_setting(
        env=source_env,
        setting_keys=retry_keys,
        env_keys=retry_env,
        default=0.4,
        setting_getter=setting_getter,
    )
    failure_raw = _read_setting(
        env=source_env,
        setting_keys=failure_keys,
        env_keys=failure_env,
        default=0.2,
        setting_getter=setting_getter,
    )
    throttle_raw = _read_setting(
        env=source_env,
        setting_keys=throttle_keys,
        env_keys=throttle_env,
        default=1,
        setting_getter=setting_getter,
    )
    sample_raw = _read_setting(
        env=source_env,
        setting_keys=sample_keys,
        env_keys=sample_env,
        default=25,
        setting_getter=setting_getter,
    )

    governance = resolve_registered_contract(
        registry=_ADAPTIVE_REGISTRY,
        namespace=_ADAPTIVE_NAMESPACE,
        raw_values={
            "enabled": enabled_raw,
            "deterministic_mode": mode_raw,
            "provider_failure_mode": provider_mode_raw,
            "base_queue_depth": base_queue_depth,
            "base_lag_seconds": base_lag_seconds,
            "retry_rate_threshold": retry_raw,
            "failure_rate_threshold": failure_raw,
            "throttle_max_attempts": throttle_raw,
            "reliability_sample_size": sample_raw,
        },
        strict=strict,
    )
    if governance.report.has_issues:
        for issue in governance.report.issues:
            logger.warning(
                "adaptive limiter config issue field=%s code=%s raw=%r fallback=%r",
                issue.field,
                issue.code,
                issue.raw_value,
                issue.fallback_value,
            )
    if governance.report.drift and governance.report.drift.drifted:
        logger.warning(
            "adaptive limiter config drift detected fields=%s",
            ",".join(entry.field for entry in governance.report.drift.entries),
        )

    resolved = governance.values
    return AdaptiveRateLimiterSettings(
        enabled=bool(resolved["enabled"]),
        deterministic_mode=_to_optional_mode(resolved["deterministic_mode"]),
        provider_failure_mode=_to_mode(resolved["provider_failure_mode"], default="sync_fallback"),
        base_queue_depth=int(resolved["base_queue_depth"]),
        base_lag_seconds=int(resolved["base_lag_seconds"]),
        retry_rate_threshold=float(resolved["retry_rate_threshold"]),
        failure_rate_threshold=float(resolved["failure_rate_threshold"]),
        throttle_max_attempts=int(resolved["throttle_max_attempts"]),
        reliability_sample_size=int(resolved["reliability_sample_size"]),
    )


def _safe_ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return max(0.0, min(1.0, float(numerator) / float(denominator)))


def sample_adaptive_ingress_signals(
    *,
    inbound_provider: InboundQueueProvider,
    backpressure_decision: IngressBackpressureDecision,
    sample_size: int,
) -> AdaptiveIngressSignals:
    fallback_depth = max(0, int(backpressure_decision.queue_depth or 0))
    fallback_lag = max(0.0, float(backpressure_decision.oldest_lag_seconds or 0.0))
    safe_sample_size = max(1, int(sample_size))

    try:
        pending_rows = list(inbound_provider.list_pending(limit=safe_sample_size) or [])
        list_dead = getattr(inbound_provider, "list_dead", None)
        dead_rows = list(cast(Any, list_dead)(limit=safe_sample_size) or []) if callable(list_dead) else []
    except Exception as exc:
        logger.warning("adaptive ingress signal sampling failed (%s)", type(exc).__name__)
        return AdaptiveIngressSignals(
            queue_depth=fallback_depth,
            oldest_lag_seconds=fallback_lag,
            retry_rate=1.0,
            failure_rate=1.0,
            pending_sample_size=0,
            dead_sample_size=0,
            provider_available=False,
            source=type(inbound_provider).__name__,
        )

    pending_count = len(pending_rows)
    dead_count = len(dead_rows)
    retry_count = 0
    for row in pending_rows:
        status = str(getattr(row, "status", "")).strip().lower()
        if status == QueueStatus.RETRY:
            retry_count += 1

    return AdaptiveIngressSignals(
        queue_depth=fallback_depth if fallback_depth > 0 else pending_count,
        oldest_lag_seconds=fallback_lag,
        retry_rate=_safe_ratio(retry_count, pending_count),
        failure_rate=_safe_ratio(dead_count, pending_count + dead_count),
        pending_sample_size=pending_count,
        dead_sample_size=dead_count,
        provider_available=True,
        source=type(inbound_provider).__name__,
    )


def _mode_allows_enqueue(mode: RateLimitMode) -> bool:
    return mode in {"allow", "throttle"}


def _adapt_thresholds(
    *,
    settings: AdaptiveRateLimiterSettings,
    signals: AdaptiveIngressSignals,
) -> tuple[int, float]:
    stress = min(
        1.0,
        (max(0.0, float(signals.retry_rate)) * 0.7) + (max(0.0, float(signals.failure_rate)) * 1.2),
    )
    adjusted_depth = max(1, int(round(float(settings.base_queue_depth) * (1.0 - (0.65 * stress)))))
    adjusted_lag = max(1.0, float(settings.base_lag_seconds) * (1.0 - (0.55 * stress)))
    return adjusted_depth, adjusted_lag


def _resolved_attempts(
    *,
    mode: RateLimitMode,
    requested_max_attempts: int,
    backpressure_decision: IngressBackpressureDecision,
    settings: AdaptiveRateLimiterSettings,
) -> int:
    baseline = min(
        max(1, int(requested_max_attempts)),
        max(1, int(backpressure_decision.effective_max_attempts)),
    )
    if mode == "throttle":
        return min(baseline, max(1, int(settings.throttle_max_attempts)))
    return baseline


def _backpressure_enforced_mode(backpressure_decision: IngressBackpressureDecision) -> RateLimitMode | None:
    if backpressure_decision.allow_enqueue:
        return None
    if str(backpressure_decision.behavior).strip().lower() == "reject":
        return "reject"
    return "sync_fallback"


def resolve_adaptive_rate_limit_decision(
    *,
    settings: AdaptiveRateLimiterSettings,
    signals: AdaptiveIngressSignals,
    backpressure_decision: IngressBackpressureDecision,
    requested_max_attempts: int,
) -> AdaptiveRateLimitDecision:
    adjusted_depth, adjusted_lag = _adapt_thresholds(settings=settings, signals=signals)
    enforced_mode = _backpressure_enforced_mode(backpressure_decision)

    if enforced_mode is not None:
        mode = enforced_mode
        reason = str(backpressure_decision.reason or f"backpressure_{mode}")
    elif not signals.provider_available:
        mode = settings.provider_failure_mode
        reason = f"adaptive_provider_unavailable_{mode}"
    elif settings.deterministic_mode is not None:
        mode = settings.deterministic_mode
        reason = f"adaptive_mode_{mode}"
    elif not settings.enabled:
        if backpressure_decision.overloaded and str(backpressure_decision.behavior) == "degrade_mode":
            mode = "throttle"
            reason = "adaptive_disabled_backpressure_degrade"
        else:
            mode = "allow"
            reason = "adaptive_disabled"
    elif float(signals.failure_rate) >= float(settings.failure_rate_threshold):
        mode = "sync_fallback"
        reason = "adaptive_failure_rate_sync_fallback"
    else:
        queue_exceeded = int(signals.queue_depth) >= int(adjusted_depth)
        lag_exceeded = float(signals.oldest_lag_seconds) >= float(adjusted_lag)
        retry_exceeded = float(signals.retry_rate) >= float(settings.retry_rate_threshold)
        threshold_exceeded = bool(queue_exceeded or lag_exceeded or retry_exceeded)

        if threshold_exceeded:
            behavior = str(backpressure_decision.behavior).strip().lower()
            if behavior == "reject":
                mode = "reject"
            elif behavior == "sync_fallback":
                mode = "sync_fallback"
            else:
                mode = "throttle"
            reason = f"adaptive_threshold_{mode}"
        elif backpressure_decision.overloaded and str(backpressure_decision.behavior) == "degrade_mode":
            mode = "throttle"
            reason = "adaptive_backpressure_degrade_throttle"
        else:
            mode = "allow"
            reason = "adaptive_allow"

    return AdaptiveRateLimitDecision(
        mode=mode,
        allow_enqueue=_mode_allows_enqueue(mode),
        reason=reason,
        effective_max_attempts=_resolved_attempts(
            mode=mode,
            requested_max_attempts=requested_max_attempts,
            backpressure_decision=backpressure_decision,
            settings=settings,
        ),
        adjusted_queue_depth_threshold=adjusted_depth,
        adjusted_lag_seconds_threshold=adjusted_lag,
        queue_depth=max(0, int(signals.queue_depth)),
        oldest_lag_seconds=max(0.0, float(signals.oldest_lag_seconds)),
        retry_rate=max(0.0, min(1.0, float(signals.retry_rate))),
        failure_rate=max(0.0, min(1.0, float(signals.failure_rate))),
        provider_available=bool(signals.provider_available),
        backpressure_overloaded=bool(backpressure_decision.overloaded),
        backpressure_behavior=str(backpressure_decision.behavior or "unknown"),
    )


def emit_adaptive_rate_limit_metric(
    *,
    decision: AdaptiveRateLimitDecision,
    channel: str,
    request_id: str,
    metric_logger: Callable[..., None] | None = None,
) -> None:
    logger_fn = metric_logger
    if logger_fn is None:
        from utils.structured_logging import log_quality_metric  # noqa: PLC0415

        logger_fn = log_quality_metric
    try:
        logger_fn(
            "refactor_ingress_adaptive_rate_limit_decision",
            **decision.metric_tags(channel=channel, request_id=request_id),
        )
    except Exception:
        return
