from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Literal, Mapping

from app.config_governance import (
    ConfigFieldContract,
    ConfigRegistryContract,
    TypedConfigRegistry,
    allowed_values,
    numeric_bounds,
    resolve_registered_contract,
)
from app.ingress.rollout_controls import (
    Phase4FeatureRolloutDecision,
    SettingsGetter,
)
from app.queue.providers import InboundQueueProvider

logger = logging.getLogger(__name__)

OverloadBehavior = Literal["reject", "sync_fallback", "degrade_mode"]

_ALLOWED_BEHAVIORS: tuple[OverloadBehavior, ...] = ("reject", "sync_fallback", "degrade_mode")
_PENDING_STATUSES = {"pending", "retry"}


@dataclass(frozen=True)
class IngressBackpressureSettings:
    max_queue_depth: int
    max_lag_seconds: int
    overload_behavior: OverloadBehavior
    degrade_max_attempts: int


@dataclass(frozen=True)
class IngressQueuePressureSnapshot:
    queue_depth: int
    oldest_lag_seconds: float
    sampled_count: int
    source: str


@dataclass(frozen=True)
class IngressBackpressureDecision:
    allow_enqueue: bool
    overloaded: bool
    reason: str
    behavior: OverloadBehavior
    trigger: str
    provider_available: bool
    effective_max_attempts: int
    queue_depth: int | None = None
    oldest_lag_seconds: float | None = None
    rollout_reason: str = "rollout_selected"
    rollout_exposed: bool = True
    rollback_activated: bool = False
    safeguard_fallback: bool = False

    def metric_tags(self, *, channel: str, request_id: str) -> dict[str, Any]:
        return {
            "channel": str(channel or "unknown"),
            "request_id": str(request_id or ""),
            "allow_enqueue": bool(self.allow_enqueue),
            "overloaded": bool(self.overloaded),
            "reason": str(self.reason or "unknown"),
            "behavior": str(self.behavior or "unknown"),
            "trigger": str(self.trigger or "unknown"),
            "provider_available": bool(self.provider_available),
            "queue_depth": self.queue_depth if self.queue_depth is not None else -1,
            "oldest_lag_seconds": (
                round(max(0.0, float(self.oldest_lag_seconds)), 3)
                if self.oldest_lag_seconds is not None
                else -1.0
            ),
            "effective_max_attempts": max(1, int(self.effective_max_attempts)),
            "rollout_reason": str(self.rollout_reason or "unknown"),
            "rollout_exposed": bool(self.rollout_exposed),
            "rollback_activated": bool(self.rollback_activated),
            "safeguard_fallback": bool(self.safeguard_fallback),
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


def _to_positive_int(value: Any, *, default: int, minimum: int = 1, maximum: int = 100000) -> int:
    try:
        parsed = int(float(str(value).strip()))
    except Exception:
        parsed = int(default)
    return max(minimum, min(maximum, parsed))


def _to_behavior(value: Any, *, default: OverloadBehavior = "sync_fallback") -> OverloadBehavior:
    normalized = str(value or "").strip().lower()
    if normalized in _ALLOWED_BEHAVIORS:
        return normalized  # type: ignore[return-value]
    return default


def _parse_strict_positive_int(value: Any) -> int:
    return int(float(str(value).strip()))


def _parse_strict_behavior(value: Any) -> OverloadBehavior:
    normalized = str(value or "").strip().lower()
    if normalized not in _ALLOWED_BEHAVIORS:
        raise ValueError(f"unsupported overload behavior={value}")
    return normalized  # type: ignore[return-value]


def _setting_candidates(channel: str | None, suffix: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    normalized_channel = str(channel or "").strip().lower()
    setting_keys: list[str] = []
    env_keys: list[str] = []
    if normalized_channel:
        setting_keys.append(f"refactor_{normalized_channel}_ingress_backpressure_{suffix}")
        env_keys.append(f"REFACTOR_{normalized_channel.upper()}_INGRESS_BACKPRESSURE_{suffix.upper()}")
    setting_keys.append(f"refactor_ingress_backpressure_{suffix}")
    env_keys.append(f"REFACTOR_INGRESS_BACKPRESSURE_{suffix.upper()}")
    return tuple(setting_keys), tuple(env_keys)


_BACKPRESSURE_NAMESPACE = "ingress_backpressure_settings"
_BACKPRESSURE_REGISTRY = TypedConfigRegistry()
_BACKPRESSURE_REGISTRY.register(
    ConfigRegistryContract(
        namespace=_BACKPRESSURE_NAMESPACE,
        fields={
            "max_queue_depth": ConfigFieldContract(
                name="max_queue_depth",
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
            "max_lag_seconds": ConfigFieldContract(
                name="max_lag_seconds",
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
            "overload_behavior": ConfigFieldContract(
                name="overload_behavior",
                default="sync_fallback",
                parser=_parse_strict_behavior,
                validators=(allowed_values(_ALLOWED_BEHAVIORS),),
                fallback_resolver=lambda raw, default: _to_behavior(raw, default=default),  # type: ignore[arg-type]
            ),
            "degrade_max_attempts": ConfigFieldContract(
                name="degrade_max_attempts",
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
        },
    )
)


def load_ingress_backpressure_settings(
    *,
    channel: str | None,
    env: Mapping[str, str] | None = None,
    setting_getter: SettingsGetter | None = None,
    strict: bool = False,
) -> IngressBackpressureSettings:
    source_env = env or os.environ

    depth_keys, depth_env_keys = _setting_candidates(channel, "max_queue_depth")
    lag_keys, lag_env_keys = _setting_candidates(channel, "max_lag_seconds")
    behavior_keys, behavior_env_keys = _setting_candidates(channel, "overload_behavior")
    degrade_keys, degrade_env_keys = _setting_candidates(channel, "degrade_max_attempts")

    max_depth_raw = _read_setting(
        env=source_env,
        setting_keys=depth_keys,
        env_keys=depth_env_keys,
        default=250,
        setting_getter=setting_getter,
    )
    max_lag_raw = _read_setting(
        env=source_env,
        setting_keys=lag_keys,
        env_keys=lag_env_keys,
        default=90,
        setting_getter=setting_getter,
    )
    behavior_raw = _read_setting(
        env=source_env,
        setting_keys=behavior_keys,
        env_keys=behavior_env_keys,
        default="sync_fallback",
        setting_getter=setting_getter,
    )
    degrade_attempts_raw = _read_setting(
        env=source_env,
        setting_keys=degrade_keys,
        env_keys=degrade_env_keys,
        default=1,
        setting_getter=setting_getter,
    )

    governance = resolve_registered_contract(
        registry=_BACKPRESSURE_REGISTRY,
        namespace=_BACKPRESSURE_NAMESPACE,
        raw_values={
            "max_queue_depth": max_depth_raw,
            "max_lag_seconds": max_lag_raw,
            "overload_behavior": behavior_raw,
            "degrade_max_attempts": degrade_attempts_raw,
        },
        strict=strict,
    )
    if governance.report.has_issues:
        for issue in governance.report.issues:
            logger.warning(
                "ingress backpressure config issue field=%s code=%s raw=%r fallback=%r",
                issue.field,
                issue.code,
                issue.raw_value,
                issue.fallback_value,
            )
    if governance.report.drift and governance.report.drift.drifted:
        logger.warning(
            "ingress backpressure config drift detected fields=%s",
            ",".join(entry.field for entry in governance.report.drift.entries),
        )

    resolved = governance.values
    return IngressBackpressureSettings(
        max_queue_depth=int(resolved["max_queue_depth"]),
        max_lag_seconds=int(resolved["max_lag_seconds"]),
        overload_behavior=_to_behavior(resolved["overload_behavior"]),
        degrade_max_attempts=int(resolved["degrade_max_attempts"]),
    )


def _parse_iso_datetime(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _build_pressure_snapshot(
    *,
    inbound_provider: InboundQueueProvider,
    sample_limit: int,
) -> IngressQueuePressureSnapshot:
    pending = list(inbound_provider.list_pending(limit=max(1, int(sample_limit))) or [])
    now = datetime.now(UTC)
    oldest_created: datetime | None = None

    for record in pending:
        status = str(getattr(record, "status", "")).strip().lower()
        if status and status not in _PENDING_STATUSES:
            continue
        metadata = getattr(record, "metadata", None)
        candidate = None
        if metadata is not None:
            candidate = _parse_iso_datetime(getattr(metadata, "enqueued_at", None))
        candidate = candidate or _parse_iso_datetime(getattr(record, "created_at", None))
        if candidate is None:
            continue
        if oldest_created is None or candidate < oldest_created:
            oldest_created = candidate

    lag_seconds = 0.0
    if oldest_created is not None:
        lag_seconds = max(0.0, (now - oldest_created).total_seconds())

    return IngressQueuePressureSnapshot(
        queue_depth=len(pending),
        oldest_lag_seconds=lag_seconds,
        sampled_count=len(pending),
        source=type(inbound_provider).__name__,
    )


def resolve_ingress_backpressure_decision(
    *,
    settings: IngressBackpressureSettings,
    inbound_provider: InboundQueueProvider,
    requested_max_attempts: int,
    rollout_decision: Phase4FeatureRolloutDecision | None = None,
) -> IngressBackpressureDecision:
    safe_requested_attempts = max(1, int(requested_max_attempts))
    sample_limit = max(1, int(settings.max_queue_depth) + 1)
    active_rollout = rollout_decision
    if active_rollout is None:
        active_rollout = Phase4FeatureRolloutDecision(
            feature="ingress_backpressure",
            use_feature=True,
            reason="canary_full",
            enabled=True,
            canary_percent=100,
            canary_bucket=0,
            emergency_rollback=False,
            rollout_exposed=True,
            rollback_activated=False,
            safeguard_fallback=False,
        )

    if not active_rollout.use_feature:
        trigger = "rollback" if active_rollout.rollback_activated else "rollout_excluded"
        reason = (
            "backpressure_rollout_emergency_rollback_sync_fallback"
            if active_rollout.rollback_activated
            else f"backpressure_rollout_{active_rollout.reason}_sync_fallback"
        )
        return IngressBackpressureDecision(
            allow_enqueue=False,
            overloaded=True,
            reason=reason,
            behavior="sync_fallback",
            trigger=trigger,
            provider_available=True,
            effective_max_attempts=safe_requested_attempts,
            rollout_reason=active_rollout.reason,
            rollout_exposed=active_rollout.rollout_exposed,
            rollback_activated=active_rollout.rollback_activated,
            safeguard_fallback=True,
        )

    effective_behavior: OverloadBehavior = (
        "degrade_mode" if active_rollout.degrade_activated else settings.overload_behavior
    )

    try:
        snapshot = _build_pressure_snapshot(
            inbound_provider=inbound_provider,
            sample_limit=sample_limit,
        )
    except Exception as exc:
        conservative_behavior: OverloadBehavior = effective_behavior
        if conservative_behavior == "degrade_mode":
            conservative_behavior = "sync_fallback"
        logger.warning(
            "ingress backpressure metrics unavailable (%s); applying conservative fallback=%s",
            type(exc).__name__,
            conservative_behavior,
        )
        return IngressBackpressureDecision(
            allow_enqueue=False,
            overloaded=True,
            reason=f"backpressure_metrics_unavailable_{conservative_behavior}",
            behavior=conservative_behavior,
            trigger="metrics_unavailable",
            provider_available=False,
            effective_max_attempts=safe_requested_attempts,
            rollout_reason=active_rollout.reason,
            rollout_exposed=active_rollout.rollout_exposed,
            rollback_activated=active_rollout.rollback_activated,
            safeguard_fallback=True,
        )

    depth_exceeded = int(snapshot.queue_depth) >= int(settings.max_queue_depth)
    lag_exceeded = float(snapshot.oldest_lag_seconds) >= float(settings.max_lag_seconds)
    overloaded = bool(depth_exceeded or lag_exceeded)
    trigger = "both" if depth_exceeded and lag_exceeded else "depth" if depth_exceeded else "lag" if lag_exceeded else "none"

    if not overloaded:
        return IngressBackpressureDecision(
            allow_enqueue=True,
            overloaded=False,
            reason="backpressure_within_threshold",
            behavior=effective_behavior,
            trigger=trigger,
            provider_available=True,
            effective_max_attempts=safe_requested_attempts,
            queue_depth=int(snapshot.queue_depth),
            oldest_lag_seconds=float(snapshot.oldest_lag_seconds),
            rollout_reason=active_rollout.reason,
            rollout_exposed=active_rollout.rollout_exposed,
            rollback_activated=active_rollout.rollback_activated,
            safeguard_fallback=False,
        )

    if effective_behavior == "degrade_mode":
        return IngressBackpressureDecision(
            allow_enqueue=True,
            overloaded=True,
            reason="backpressure_degrade_mode",
            behavior="degrade_mode",
            trigger=trigger,
            provider_available=True,
            effective_max_attempts=min(
                safe_requested_attempts,
                max(1, int(settings.degrade_max_attempts)),
            ),
            queue_depth=int(snapshot.queue_depth),
            oldest_lag_seconds=float(snapshot.oldest_lag_seconds),
            rollout_reason=active_rollout.reason,
            rollout_exposed=active_rollout.rollout_exposed,
            rollback_activated=active_rollout.rollback_activated,
            safeguard_fallback=False,
        )

    reason = "backpressure_reject" if effective_behavior == "reject" else "backpressure_sync_fallback"
    return IngressBackpressureDecision(
        allow_enqueue=False,
        overloaded=True,
        reason=reason,
        behavior=effective_behavior,
        trigger=trigger,
        provider_available=True,
        effective_max_attempts=safe_requested_attempts,
        queue_depth=int(snapshot.queue_depth),
        oldest_lag_seconds=float(snapshot.oldest_lag_seconds),
        rollout_reason=active_rollout.reason,
        rollout_exposed=active_rollout.rollout_exposed,
        rollback_activated=active_rollout.rollback_activated,
        safeguard_fallback=True,
    )


def emit_ingress_backpressure_metric(
    *,
    decision: IngressBackpressureDecision,
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
            "refactor_ingress_backpressure_decision",
            **decision.metric_tags(channel=channel, request_id=request_id),
        )
    except Exception:
        return


def is_backpressure_reject_reason(reason: str) -> bool:
    normalized = str(reason or "").strip().lower()
    return normalized in {
        "backpressure_reject",
        "backpressure_metrics_unavailable_reject",
        "adaptive_reject",
        "adaptive_mode_reject",
        "adaptive_threshold_reject",
    }
