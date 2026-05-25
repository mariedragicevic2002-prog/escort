from __future__ import annotations

import hashlib
import hmac
import logging
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Callable, Mapping, Protocol, Sequence

from app.config_governance import (
    ConfigFieldContract,
    ConfigRegistryContract,
    TypedConfigRegistry,
    numeric_bounds,
    resolve_contract,
    resolve_registered_contract,
)
from app.guardrails import (
    SLOGuardrailAction,
    SLOGuardrailDecision,
    SLOGuardrailEngine,
    SLOGuardrailPolicy,
    SLOGuardrailSignals,
    SLOGuardrailState,
)
from app.incidents.contracts import IncidentHook

logger = logging.getLogger(__name__)


class SettingsGetter(Protocol):
    def __call__(self, key: str, default: Any = None) -> Any: ...


class BucketResolver(Protocol):
    def __call__(self, phone_number: str) -> int: ...


class MetricLogger(Protocol):
    def __call__(self, metric_name: str) -> None: ...


@dataclass(frozen=True)
class SMSRolloutSettings:
    enabled: bool
    canary_percent: int
    shadow_mode: bool
    emergency_rollback: bool


@dataclass(frozen=True)
class SMSQuickAckSettings:
    enabled: bool
    max_attempts: int


@dataclass(frozen=True)
class SMSRolloutDecision:
    use_refactor_runtime: bool
    reason: str
    canary_percent: int
    canary_bucket: int
    shadow_mode: bool
    enabled: bool
    emergency_rollback: bool


@dataclass(frozen=True)
class WebhookIngressRolloutSettings:
    enabled: bool
    canary_percent: int
    emergency_rollback: bool


@dataclass(frozen=True)
class WebhookIngressQuickAckSettings:
    enabled: bool
    max_attempts: int


@dataclass(frozen=True)
class OperatorRecoverySettings:
    enabled: bool
    max_pause_seconds: int
    max_replay_batch_size: int
    canary_percent: int = 100
    emergency_rollback: bool = False


@dataclass(frozen=True)
class Phase4FeatureRolloutSettings:
    feature: str
    enabled: bool
    canary_percent: int
    emergency_rollback: bool


@dataclass(frozen=True)
class Phase4FeatureRolloutDecision:
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
    degrade_activated: bool = False
    guardrail_action: str = "observe"
    guardrail_reason: str = "not_evaluated"
    guardrail_triggered_signals: tuple[str, ...] = ()

    def metric_tags(self, *, request_id: str = "") -> dict[str, Any]:
        return {
            "feature": str(self.feature or "unknown"),
            "request_id": str(request_id or ""),
            "reason": str(self.reason or "unknown"),
            "enabled": bool(self.enabled),
            "canary_percent": max(0, min(100, int(self.canary_percent))),
            "canary_bucket": max(0, min(99, int(self.canary_bucket))),
            "rollout_exposed": bool(self.rollout_exposed),
            "rollback_activated": bool(self.rollback_activated),
            "safeguard_fallback": bool(self.safeguard_fallback),
            "emergency_rollback": bool(self.emergency_rollback),
            "degrade_activated": bool(self.degrade_activated),
            "guardrail_action": str(self.guardrail_action or "observe"),
            "guardrail_reason": str(self.guardrail_reason or "unknown"),
            "guardrail_triggered_signals": ",".join(self.guardrail_triggered_signals),
        }


@dataclass(frozen=True)
class WebhookIngressRolloutDecision:
    use_refactor_runtime: bool
    reason: str
    canary_percent: int
    canary_bucket: int
    enabled: bool
    emergency_rollback: bool


def _to_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _to_percent(value: Any, *, default: int) -> int:
    if value is None:
        return max(0, min(100, int(default)))
    try:
        parsed = int(float(str(value).strip()))
    except Exception:
        parsed = int(default)
    return max(0, min(100, parsed))


def _to_positive_int(value: Any, *, default: int, minimum: int = 1, maximum: int = 20) -> int:
    if value is None:
        parsed = int(default)
    else:
        try:
            parsed = int(float(str(value).strip()))
        except Exception:
            parsed = int(default)
    return max(minimum, min(maximum, parsed))


def _to_ratio(value: Any, *, default: float) -> float:
    if value is None:
        return max(0.0, min(1.0, float(default)))
    try:
        parsed = float(str(value).strip())
    except Exception:
        parsed = float(default)
    return max(0.0, min(1.0, parsed))


def _to_non_negative_float(value: Any, *, default: float) -> float:
    if value is None:
        return max(0.0, float(default))
    try:
        parsed = float(str(value).strip())
    except Exception:
        parsed = float(default)
    return max(0.0, parsed)


def _parse_strict_bool(value: Any) -> bool:
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"unsupported boolean value={value}")


def _parse_strict_int(value: Any) -> int:
    return int(float(str(value).strip()))


def _parse_strict_ratio(value: Any) -> float:
    return float(str(value).strip())


def _parse_strict_non_negative_float(value: Any) -> float:
    return float(str(value).strip())


def _default_settings_getter() -> SettingsGetter:
    from core.settings_manager import get_setting  # noqa: PLC0415

    return get_setting


def _read_setting(
    *,
    env: Mapping[str, str],
    key: str | Sequence[str],
    env_key: str | Sequence[str],
    default: Any,
    setting_getter: SettingsGetter | None,
) -> Any:
    env_keys = (env_key,) if isinstance(env_key, str) else tuple(env_key)
    for candidate in env_keys:
        if candidate in env:
            return env.get(candidate)
    getter = setting_getter
    if getter is None:
        try:
            getter = _default_settings_getter()
        except Exception:
            return default
    sentinel = object()
    keys = (key,) if isinstance(key, str) else tuple(key)
    for candidate in keys:
        try:
            value = getter(candidate, sentinel)
        except TypeError:
            try:
                value = getter(candidate)
            except Exception:
                continue
        except Exception:
            continue
        if value is sentinel or value is None:
            continue
        return value
    return default


def _dedupe(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in values if str(value or "").strip()))


def _phase4_guardrail_setting_candidates(feature: str, suffix: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    normalized_feature = str(feature or "phase4_feature").strip().lower().replace(" ", "_")
    setting_keys = _dedupe(
        (
            f"refactor_{normalized_feature}_slo_guardrail_{suffix}",
            f"refactor_phase4_{normalized_feature}_slo_guardrail_{suffix}",
            f"refactor_phase4_slo_guardrail_{suffix}",
        )
    )
    env_keys = _dedupe(
        (
            f"REFACTOR_{normalized_feature.upper()}_SLO_GUARDRAIL_{suffix.upper()}",
            f"REFACTOR_PHASE4_{normalized_feature.upper()}_SLO_GUARDRAIL_{suffix.upper()}",
            f"REFACTOR_PHASE4_SLO_GUARDRAIL_{suffix.upper()}",
        )
    )
    return setting_keys, env_keys


def _phase4_signal_candidates(feature: str, signal_name: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    normalized_feature = str(feature or "phase4_feature").strip().lower().replace(" ", "_")
    setting_keys = _dedupe(
        (
            f"refactor_{normalized_feature}_{signal_name}",
            f"refactor_phase4_{normalized_feature}_{signal_name}",
            f"refactor_phase4_slo_guardrail_{normalized_feature}_{signal_name}",
            f"refactor_phase4_{signal_name}",
        )
    )
    env_keys = _dedupe(
        (
            f"REFACTOR_{normalized_feature.upper()}_{signal_name.upper()}",
            f"REFACTOR_PHASE4_{normalized_feature.upper()}_{signal_name.upper()}",
            f"REFACTOR_PHASE4_SLO_GUARDRAIL_{normalized_feature.upper()}_{signal_name.upper()}",
            f"REFACTOR_PHASE4_{signal_name.upper()}",
        )
    )
    return setting_keys, env_keys


def _read_optional_setting(
    *,
    env: Mapping[str, str],
    setting_keys: tuple[str, ...],
    env_keys: tuple[str, ...],
    setting_getter: SettingsGetter | None,
) -> Any | None:
    sentinel = object()
    value = _read_setting(
        env=env,
        key=setting_keys,
        env_key=env_keys,
        default=sentinel,
        setting_getter=setting_getter,
    )
    if value is sentinel:
        return None
    return value


_ROLLOUT_CONFIG_REGISTRY = TypedConfigRegistry()
_BACKPRESSURE_ROLLOUT_NAMESPACE = "phase4_backpressure_rollout_settings"
_ROLLOUT_CONFIG_REGISTRY.register(
    ConfigRegistryContract(
        namespace=_BACKPRESSURE_ROLLOUT_NAMESPACE,
        fields={
            "enabled": ConfigFieldContract(
                name="enabled",
                default=True,
                parser=_parse_strict_bool,
                fallback_resolver=lambda raw, default: _to_bool(raw, default=default),
            ),
            "canary_percent": ConfigFieldContract(
                name="canary_percent",
                default=100,
                parser=_parse_strict_int,
                validators=(numeric_bounds(minimum=0, maximum=100),),
                fallback_resolver=lambda raw, default: _to_percent(raw, default=default),
            ),
            "emergency_rollback": ConfigFieldContract(
                name="emergency_rollback",
                default=False,
                parser=_parse_strict_bool,
                fallback_resolver=lambda raw, default: _to_bool(raw, default=default),
            ),
        },
    )
)


def _guardrail_policy_contract(feature: str) -> ConfigRegistryContract:
    normalized_feature = str(feature or "phase4_feature").strip().lower().replace(" ", "_")
    namespace = f"phase4_guardrail_policy:{normalized_feature}"
    return ConfigRegistryContract(
        namespace=namespace,
        fields={
            "enabled": ConfigFieldContract(
                name="enabled",
                default=False,
                parser=_parse_strict_bool,
                fallback_resolver=lambda raw, default: _to_bool(raw, default=default),
            ),
            "min_sample_size": ConfigFieldContract(
                name="min_sample_size",
                default=25,
                parser=_parse_strict_int,
                validators=(numeric_bounds(minimum=1, maximum=100000),),
                fallback_resolver=lambda raw, default: _to_positive_int(
                    raw,
                    default=default,
                    minimum=1,
                    maximum=100000,
                ),
            ),
            "cooldown_seconds": ConfigFieldContract(
                name="cooldown_seconds",
                default=300,
                parser=_parse_strict_int,
                validators=(numeric_bounds(minimum=1, maximum=86400),),
                fallback_resolver=lambda raw, default: _to_positive_int(
                    raw,
                    default=default,
                    minimum=1,
                    maximum=86400,
                ),
            ),
            "hysteresis_factor": ConfigFieldContract(
                name="hysteresis_factor",
                default=0.8,
                parser=_parse_strict_ratio,
                validators=(numeric_bounds(minimum=0.1, maximum=0.99),),
                fallback_resolver=lambda raw, default: max(
                    0.1,
                    min(0.99, _to_non_negative_float(raw, default=default)),
                ),
            ),
            "degrade_retry_rate": ConfigFieldContract(
                name="degrade_retry_rate",
                default=0.08,
                parser=_parse_strict_ratio,
                validators=(numeric_bounds(minimum=0.0, maximum=1.0),),
                fallback_resolver=lambda raw, default: _to_ratio(raw, default=default),
            ),
            "degrade_dead_letter_rate": ConfigFieldContract(
                name="degrade_dead_letter_rate",
                default=0.03,
                parser=_parse_strict_ratio,
                validators=(numeric_bounds(minimum=0.0, maximum=1.0),),
                fallback_resolver=lambda raw, default: _to_ratio(raw, default=default),
            ),
            "degrade_queue_lag_seconds": ConfigFieldContract(
                name="degrade_queue_lag_seconds",
                default=90.0,
                parser=_parse_strict_non_negative_float,
                validators=(numeric_bounds(minimum=0.0),),
                fallback_resolver=lambda raw, default: _to_non_negative_float(raw, default=default),
            ),
            "degrade_failure_ratio": ConfigFieldContract(
                name="degrade_failure_ratio",
                default=0.05,
                parser=_parse_strict_ratio,
                validators=(numeric_bounds(minimum=0.0, maximum=1.0),),
                fallback_resolver=lambda raw, default: _to_ratio(raw, default=default),
            ),
            "degrade_error_budget_remaining": ConfigFieldContract(
                name="degrade_error_budget_remaining",
                default=0.5,
                parser=_parse_strict_ratio,
                validators=(numeric_bounds(minimum=0.0, maximum=1.0),),
                fallback_resolver=lambda raw, default: _to_ratio(raw, default=default),
            ),
            "rollback_retry_rate": ConfigFieldContract(
                name="rollback_retry_rate",
                default=0.18,
                parser=_parse_strict_ratio,
                validators=(numeric_bounds(minimum=0.0, maximum=1.0),),
                fallback_resolver=lambda raw, default: _to_ratio(raw, default=default),
            ),
            "rollback_dead_letter_rate": ConfigFieldContract(
                name="rollback_dead_letter_rate",
                default=0.08,
                parser=_parse_strict_ratio,
                validators=(numeric_bounds(minimum=0.0, maximum=1.0),),
                fallback_resolver=lambda raw, default: _to_ratio(raw, default=default),
            ),
            "rollback_queue_lag_seconds": ConfigFieldContract(
                name="rollback_queue_lag_seconds",
                default=240.0,
                parser=_parse_strict_non_negative_float,
                validators=(numeric_bounds(minimum=0.0),),
                fallback_resolver=lambda raw, default: _to_non_negative_float(raw, default=default),
            ),
            "rollback_failure_ratio": ConfigFieldContract(
                name="rollback_failure_ratio",
                default=0.15,
                parser=_parse_strict_ratio,
                validators=(numeric_bounds(minimum=0.0, maximum=1.0),),
                fallback_resolver=lambda raw, default: _to_ratio(raw, default=default),
            ),
            "rollback_error_budget_remaining": ConfigFieldContract(
                name="rollback_error_budget_remaining",
                default=0.2,
                parser=_parse_strict_ratio,
                validators=(numeric_bounds(minimum=0.0, maximum=1.0),),
                fallback_resolver=lambda raw, default: _to_ratio(raw, default=default),
            ),
        },
    )


def _emit_governance_report(context: str, *, report: Any) -> None:
    if report.has_issues:
        for issue in report.issues:
            logger.warning(
                "%s config issue field=%s code=%s raw=%r fallback=%r",
                context,
                issue.field,
                issue.code,
                issue.raw_value,
                issue.fallback_value,
            )
    if report.drift and report.drift.drifted:
        logger.warning(
            "%s config drift detected fields=%s",
            context,
            ",".join(entry.field for entry in report.drift.entries),
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


def load_phase4_slo_guardrail_policy(
    *,
    feature: str,
    env: Mapping[str, str] | None = None,
    setting_getter: SettingsGetter | None = None,
    strict: bool = False,
) -> SLOGuardrailPolicy:
    source_env = env or os.environ

    def _raw(suffix: str) -> Any | None:
        keys, env_keys = _phase4_guardrail_setting_candidates(feature, suffix)
        return _read_optional_setting(
            env=source_env,
            setting_keys=keys,
            env_keys=env_keys,
            setting_getter=setting_getter,
        )

    governance = resolve_contract(
        contract=_guardrail_policy_contract(feature),
        raw_values={
            "enabled": _raw("enabled"),
            "min_sample_size": _raw("min_sample_size"),
            "cooldown_seconds": _raw("cooldown_seconds"),
            "hysteresis_factor": _raw("hysteresis_factor"),
            "degrade_retry_rate": _raw("degrade_retry_rate"),
            "degrade_dead_letter_rate": _raw("degrade_dead_letter_rate"),
            "degrade_queue_lag_seconds": _raw("degrade_queue_lag_seconds"),
            "degrade_failure_ratio": _raw("degrade_failure_ratio"),
            "degrade_error_budget_remaining": _raw("degrade_error_budget_remaining"),
            "rollback_retry_rate": _raw("rollback_retry_rate"),
            "rollback_dead_letter_rate": _raw("rollback_dead_letter_rate"),
            "rollback_queue_lag_seconds": _raw("rollback_queue_lag_seconds"),
            "rollback_failure_ratio": _raw("rollback_failure_ratio"),
            "rollback_error_budget_remaining": _raw("rollback_error_budget_remaining"),
        },
        strict=strict,
    )
    _emit_governance_report("phase4 guardrail policy", report=governance.report)
    values = governance.values
    return SLOGuardrailPolicy(
        enabled=bool(values["enabled"]),
        min_sample_size=int(values["min_sample_size"]),
        cooldown_seconds=int(values["cooldown_seconds"]),
        hysteresis_factor=float(values["hysteresis_factor"]),
        degrade_retry_rate=float(values["degrade_retry_rate"]),
        degrade_dead_letter_rate=float(values["degrade_dead_letter_rate"]),
        degrade_queue_lag_seconds=float(values["degrade_queue_lag_seconds"]),
        degrade_failure_ratio=float(values["degrade_failure_ratio"]),
        degrade_error_budget_remaining=float(values["degrade_error_budget_remaining"]),
        rollback_retry_rate=float(values["rollback_retry_rate"]),
        rollback_dead_letter_rate=float(values["rollback_dead_letter_rate"]),
        rollback_queue_lag_seconds=float(values["rollback_queue_lag_seconds"]),
        rollback_failure_ratio=float(values["rollback_failure_ratio"]),
        rollback_error_budget_remaining=float(values["rollback_error_budget_remaining"]),
    )


def load_phase4_slo_guardrail_signals(
    *,
    feature: str,
    env: Mapping[str, str] | None = None,
    setting_getter: SettingsGetter | None = None,
) -> SLOGuardrailSignals | None:
    source_env = env or os.environ

    def _signal(signal_name: str) -> Any | None:
        keys, env_keys = _phase4_signal_candidates(feature, signal_name)
        return _read_optional_setting(
            env=source_env,
            setting_keys=keys,
            env_keys=env_keys,
            setting_getter=setting_getter,
        )

    retry_rate_raw = _signal("retry_rate")
    dead_letter_rate_raw = _signal("dead_letter_rate")
    queue_lag_raw = _signal("queue_lag_seconds")
    failure_ratio_raw = _signal("failure_ratio")
    error_budget_raw = _signal("error_budget_remaining")
    sample_size_raw = _signal("sample_size")
    window_seconds_raw = _signal("window_seconds")

    if (
        retry_rate_raw is None
        and dead_letter_rate_raw is None
        and queue_lag_raw is None
        and failure_ratio_raw is None
        and error_budget_raw is None
        and sample_size_raw is None
    ):
        return None

    return SLOGuardrailSignals(
        retry_rate=None if retry_rate_raw is None else _to_ratio(retry_rate_raw, default=0.0),
        dead_letter_rate=None if dead_letter_rate_raw is None else _to_ratio(dead_letter_rate_raw, default=0.0),
        queue_lag_seconds=None if queue_lag_raw is None else _to_non_negative_float(queue_lag_raw, default=0.0),
        failure_ratio=None if failure_ratio_raw is None else _to_ratio(failure_ratio_raw, default=0.0),
        error_budget_remaining=None if error_budget_raw is None else _to_ratio(error_budget_raw, default=1.0),
        sample_size=(
            0
            if sample_size_raw is None
            else _to_positive_int(sample_size_raw, default=0, minimum=0, maximum=1000000)
        ),
        window_seconds=(
            300
            if window_seconds_raw is None
            else _to_positive_int(window_seconds_raw, default=300, minimum=1, maximum=86400)
        ),
    )


def load_phase4_slo_guardrail_state(
    *,
    feature: str,
    env: Mapping[str, str] | None = None,
    setting_getter: SettingsGetter | None = None,
) -> SLOGuardrailState | None:
    source_env = env or os.environ

    def _state_field(suffix: str) -> Any | None:
        keys, env_keys = _phase4_guardrail_setting_candidates(feature, suffix)
        return _read_optional_setting(
            env=source_env,
            setting_keys=keys,
            env_keys=env_keys,
            setting_getter=setting_getter,
        )

    action_raw = str(_state_field("state_action") or "").strip().lower()
    if action_raw not in {item.value for item in SLOGuardrailAction}:
        return None

    return SLOGuardrailState(
        action=SLOGuardrailAction(action_raw),
        last_transition_at=_parse_iso_datetime(_state_field("state_last_transition_at")),
        cooldown_until=_parse_iso_datetime(_state_field("state_cooldown_until")),
    )


def _evaluate_guardrail_decision(
    *,
    signals: SLOGuardrailSignals | None,
    policy: SLOGuardrailPolicy | None,
    state: SLOGuardrailState | None,
    guardrail_engine: SLOGuardrailEngine | None,
    now: datetime | None,
) -> SLOGuardrailDecision | None:
    if signals is None:
        return None
    active_policy = policy or SLOGuardrailPolicy(enabled=False)
    engine = guardrail_engine or SLOGuardrailEngine(policy=active_policy)
    decision, _next_state = engine.evaluate(
        signals=signals,
        state=state,
        now=now,
    )
    return decision


def _apply_guardrail_to_phase4_rollout(
    *,
    rollout: Phase4FeatureRolloutDecision,
    guardrail: SLOGuardrailDecision | None,
) -> Phase4FeatureRolloutDecision:
    if rollout.rollback_activated:
        return replace(
            rollout,
            degrade_activated=False,
            guardrail_action=SLOGuardrailAction.ROLLBACK.value,
            guardrail_reason="rollout_emergency_rollback",
            guardrail_triggered_signals=(),
        )

    if guardrail is None:
        return rollout

    action = guardrail.action
    common_updates = {
        "guardrail_action": action.value,
        "guardrail_reason": guardrail.reason,
        "guardrail_triggered_signals": tuple(guardrail.triggered_signals),
    }
    if action == SLOGuardrailAction.ROLLBACK and rollout.use_feature:
        return replace(
            rollout,
            use_feature=False,
            reason="slo_guardrail_rollback",
            emergency_rollback=True,
            rollout_exposed=False,
            rollback_activated=True,
            safeguard_fallback=True,
            degrade_activated=False,
            **common_updates,
        )
    if action == SLOGuardrailAction.DEGRADE and rollout.use_feature:
        return replace(
            rollout,
            reason="slo_guardrail_degrade",
            safeguard_fallback=True,
            degrade_activated=True,
            **common_updates,
        )
    return replace(
        rollout,
        degrade_activated=False,
        **common_updates,
    )


def load_sms_rollout_settings(
    *,
    env: Mapping[str, str] | None = None,
    setting_getter: SettingsGetter | None = None,
) -> SMSRolloutSettings:
    source_env = env or os.environ
    enabled_raw = _read_setting(
        env=source_env,
        key="refactor_sms_pipeline_enabled",
        env_key="REFACTOR_SMS_PIPELINE_ENABLED",
        default=False,
        setting_getter=setting_getter,
    )
    canary_raw = _read_setting(
        env=source_env,
        key="refactor_sms_pipeline_canary_percent",
        env_key="REFACTOR_SMS_PIPELINE_CANARY_PERCENT",
        default=100,
        setting_getter=setting_getter,
    )
    shadow_raw = _read_setting(
        env=source_env,
        key="refactor_sms_pipeline_shadow_mode",
        env_key="REFACTOR_SMS_PIPELINE_SHADOW_MODE",
        default=False,
        setting_getter=setting_getter,
    )
    rollback_raw = _read_setting(
        env=source_env,
        key="refactor_sms_pipeline_emergency_rollback",
        env_key="REFACTOR_SMS_PIPELINE_EMERGENCY_ROLLBACK",
        default=False,
        setting_getter=setting_getter,
    )
    return SMSRolloutSettings(
        enabled=_to_bool(enabled_raw, default=False),
        canary_percent=_to_percent(canary_raw, default=100),
        shadow_mode=_to_bool(shadow_raw, default=False),
        emergency_rollback=_to_bool(rollback_raw, default=False),
    )


def load_sms_quick_ack_settings(
    *,
    env: Mapping[str, str] | None = None,
    setting_getter: SettingsGetter | None = None,
) -> SMSQuickAckSettings:
    source_env = env or os.environ
    enabled_raw = _read_setting(
        env=source_env,
        key="refactor_sms_ingress_quick_ack_enabled",
        env_key="REFACTOR_SMS_INGRESS_QUICK_ACK_ENABLED",
        default=False,
        setting_getter=setting_getter,
    )
    max_attempts_raw = _read_setting(
        env=source_env,
        key="refactor_sms_ingress_quick_ack_max_attempts",
        env_key="REFACTOR_SMS_INGRESS_QUICK_ACK_MAX_ATTEMPTS",
        default=5,
        setting_getter=setting_getter,
    )
    return SMSQuickAckSettings(
        enabled=_to_bool(enabled_raw, default=False),
        max_attempts=_to_positive_int(max_attempts_raw, default=5),
    )


def stable_sms_canary_bucket(phone_number: str, *, secret: str | None = None) -> int:
    seed = (phone_number or "").strip()
    if not seed:
        return 0
    secret_key = (secret if secret is not None else os.environ.get("SECRET_KEY", "")).encode("utf-8")
    if not secret_key:
        secret_key = b"refactor-sms-rollout"
    digest = hmac.new(secret_key, seed.encode("utf-8"), hashlib.sha256).hexdigest()
    return int(digest[:8], 16) % 100


def resolve_sms_rollout_decision(
    phone_number: str,
    *,
    env: Mapping[str, str] | None = None,
    setting_getter: SettingsGetter | None = None,
    bucket_resolver: BucketResolver | None = None,
) -> SMSRolloutDecision:
    settings = load_sms_rollout_settings(env=env, setting_getter=setting_getter)
    bucket_fn = bucket_resolver or stable_sms_canary_bucket
    bucket = max(0, min(99, int(bucket_fn(phone_number))))

    if settings.emergency_rollback:
        return SMSRolloutDecision(
            use_refactor_runtime=False,
            reason="emergency_rollback",
            canary_percent=settings.canary_percent,
            canary_bucket=bucket,
            shadow_mode=settings.shadow_mode,
            enabled=settings.enabled,
            emergency_rollback=True,
        )
    if not settings.enabled:
        return SMSRolloutDecision(
            use_refactor_runtime=False,
            reason="rollout_disabled",
            canary_percent=settings.canary_percent,
            canary_bucket=bucket,
            shadow_mode=settings.shadow_mode,
            enabled=False,
            emergency_rollback=False,
        )
    if settings.canary_percent <= 0:
        return SMSRolloutDecision(
            use_refactor_runtime=False,
            reason="canary_disabled",
            canary_percent=0,
            canary_bucket=bucket,
            shadow_mode=settings.shadow_mode,
            enabled=True,
            emergency_rollback=False,
        )
    if settings.canary_percent >= 100:
        return SMSRolloutDecision(
            use_refactor_runtime=True,
            reason="canary_full",
            canary_percent=100,
            canary_bucket=bucket,
            shadow_mode=settings.shadow_mode,
            enabled=True,
            emergency_rollback=False,
        )
    if bucket < settings.canary_percent:
        return SMSRolloutDecision(
            use_refactor_runtime=True,
            reason="canary_selected",
            canary_percent=settings.canary_percent,
            canary_bucket=bucket,
            shadow_mode=settings.shadow_mode,
            enabled=True,
            emergency_rollback=False,
        )
    return SMSRolloutDecision(
        use_refactor_runtime=False,
        reason="canary_excluded",
        canary_percent=settings.canary_percent,
        canary_bucket=bucket,
        shadow_mode=settings.shadow_mode,
        enabled=True,
        emergency_rollback=False,
    )


def _phone_tail_for_metrics(phone_number: str) -> str:
    digits = "".join(c for c in (phone_number or "") if c.isdigit())
    if len(digits) >= 4:
        return digits[-4:]
    return "****"


def emit_sms_rollout_metrics(
    *,
    decision: SMSRolloutDecision,
    phone_number: str,
    request_id: str,
    metric_logger: Callable[..., None] | None = None,
) -> None:
    logger = metric_logger
    if logger is None:
        from utils.structured_logging import log_quality_metric  # noqa: PLC0415

        logger = log_quality_metric

    payload = {
        "phone_tail": _phone_tail_for_metrics(phone_number),
        "request_id": request_id,
        "reason": decision.reason,
        "canary_percent": decision.canary_percent,
        "canary_bucket": decision.canary_bucket,
        "shadow_mode": decision.shadow_mode,
        "emergency_rollback": decision.emergency_rollback,
    }

    decision_metric = "sms_rollout_refactor_selected" if decision.use_refactor_runtime else "sms_rollout_legacy_selected"
    logger(decision_metric, **payload)

    if decision.shadow_mode and not decision.use_refactor_runtime:
        logger("sms_rollout_shadow_mode", **payload)


def load_webhook_ingress_rollout_settings(
    *,
    env: Mapping[str, str] | None = None,
    setting_getter: SettingsGetter | None = None,
) -> WebhookIngressRolloutSettings:
    source_env = env or os.environ
    enabled_raw = _read_setting(
        env=source_env,
        key="refactor_webhook_ingress_enabled",
        env_key="REFACTOR_WEBHOOK_INGRESS_ENABLED",
        default=False,
        setting_getter=setting_getter,
    )
    canary_raw = _read_setting(
        env=source_env,
        key="refactor_webhook_ingress_canary_percent",
        env_key="REFACTOR_WEBHOOK_INGRESS_CANARY_PERCENT",
        default=100,
        setting_getter=setting_getter,
    )
    rollback_raw = _read_setting(
        env=source_env,
        key="refactor_webhook_ingress_emergency_rollback",
        env_key="REFACTOR_WEBHOOK_INGRESS_EMERGENCY_ROLLBACK",
        default=False,
        setting_getter=setting_getter,
    )
    return WebhookIngressRolloutSettings(
        enabled=_to_bool(enabled_raw, default=False),
        canary_percent=_to_percent(canary_raw, default=100),
        emergency_rollback=_to_bool(rollback_raw, default=False),
    )


def load_webhook_ingress_quick_ack_settings(
    *,
    env: Mapping[str, str] | None = None,
    setting_getter: SettingsGetter | None = None,
) -> WebhookIngressQuickAckSettings:
    source_env = env or os.environ
    enabled_raw = _read_setting(
        env=source_env,
        key="refactor_webhook_ingress_quick_ack_enabled",
        env_key="REFACTOR_WEBHOOK_INGRESS_QUICK_ACK_ENABLED",
        default=False,
        setting_getter=setting_getter,
    )
    max_attempts_raw = _read_setting(
        env=source_env,
        key="refactor_webhook_ingress_quick_ack_max_attempts",
        env_key="REFACTOR_WEBHOOK_INGRESS_QUICK_ACK_MAX_ATTEMPTS",
        default=5,
        setting_getter=setting_getter,
    )
    return WebhookIngressQuickAckSettings(
        enabled=_to_bool(enabled_raw, default=False),
        max_attempts=_to_positive_int(max_attempts_raw, default=5),
    )


def load_operator_recovery_settings(
    *,
    env: Mapping[str, str] | None = None,
    setting_getter: SettingsGetter | None = None,
) -> OperatorRecoverySettings:
    source_env = env or os.environ
    enabled_raw = _read_setting(
        env=source_env,
        key="refactor_operator_recovery_enabled",
        env_key="REFACTOR_OPERATOR_RECOVERY_ENABLED",
        default=False,
        setting_getter=setting_getter,
    )
    max_pause_raw = _read_setting(
        env=source_env,
        key="refactor_operator_recovery_max_pause_seconds",
        env_key="REFACTOR_OPERATOR_RECOVERY_MAX_PAUSE_SECONDS",
        default=1800,
        setting_getter=setting_getter,
    )
    max_replay_raw = _read_setting(
        env=source_env,
        key="refactor_operator_recovery_max_replay_batch_size",
        env_key="REFACTOR_OPERATOR_RECOVERY_MAX_REPLAY_BATCH_SIZE",
        default=50,
        setting_getter=setting_getter,
    )
    canary_raw = _read_setting(
        env=source_env,
        key=(
            "refactor_operator_recovery_canary_percent",
            "refactor_phase4_operator_recovery_canary_percent",
        ),
        env_key=(
            "REFACTOR_OPERATOR_RECOVERY_CANARY_PERCENT",
            "REFACTOR_PHASE4_OPERATOR_RECOVERY_CANARY_PERCENT",
        ),
        default=100,
        setting_getter=setting_getter,
    )
    rollback_raw = _read_setting(
        env=source_env,
        key=(
            "refactor_operator_recovery_emergency_rollback",
            "refactor_phase4_operator_recovery_emergency_rollback",
            "refactor_phase4_emergency_rollback",
        ),
        env_key=(
            "REFACTOR_OPERATOR_RECOVERY_EMERGENCY_ROLLBACK",
            "REFACTOR_PHASE4_OPERATOR_RECOVERY_EMERGENCY_ROLLBACK",
            "REFACTOR_PHASE4_EMERGENCY_ROLLBACK",
        ),
        default=False,
        setting_getter=setting_getter,
    )
    return OperatorRecoverySettings(
        enabled=_to_bool(enabled_raw, default=False),
        max_pause_seconds=_to_positive_int(max_pause_raw, default=1800, minimum=30, maximum=86400),
        max_replay_batch_size=_to_positive_int(max_replay_raw, default=50, minimum=1, maximum=500),
        canary_percent=_to_percent(canary_raw, default=100),
        emergency_rollback=_to_bool(rollback_raw, default=False),
    )


def _build_phase4_rollout_decision(
    *,
    feature: str,
    settings: Phase4FeatureRolloutSettings,
    canary_bucket: int,
) -> Phase4FeatureRolloutDecision:
    bounded_bucket = max(0, min(99, int(canary_bucket)))
    bounded_percent = max(0, min(100, int(settings.canary_percent)))
    if settings.emergency_rollback:
        reason = "emergency_rollback"
        use_feature = False
    elif not settings.enabled:
        reason = "rollout_disabled"
        use_feature = False
    elif bounded_percent <= 0:
        reason = "canary_disabled"
        use_feature = False
    elif bounded_percent >= 100:
        reason = "canary_full"
        use_feature = True
    elif bounded_bucket < bounded_percent:
        reason = "canary_selected"
        use_feature = True
    else:
        reason = "canary_excluded"
        use_feature = False

    return Phase4FeatureRolloutDecision(
        feature=feature,
        use_feature=use_feature,
        reason=reason,
        enabled=bool(settings.enabled),
        canary_percent=bounded_percent,
        canary_bucket=bounded_bucket,
        emergency_rollback=bool(settings.emergency_rollback),
        rollout_exposed=bool(use_feature),
        rollback_activated=bool(settings.emergency_rollback),
        safeguard_fallback=not bool(use_feature),
    )


def resolve_phase4_feature_rollout_decision(
    *,
    settings: Phase4FeatureRolloutSettings,
    context_key: str,
    bucket_resolver: BucketResolver | None = None,
    guardrail_signals: SLOGuardrailSignals | None = None,
    guardrail_policy: SLOGuardrailPolicy | None = None,
    guardrail_state: SLOGuardrailState | None = None,
    guardrail_engine: SLOGuardrailEngine | None = None,
    incident_hook: IncidentHook | None = None,
    incident_metadata: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> Phase4FeatureRolloutDecision:
    bucket_fn = bucket_resolver or stable_sms_canary_bucket
    feature = str(settings.feature or "phase4_feature")
    seed = f"{feature}:{str(context_key or 'default').strip()}"
    bucket = max(0, min(99, int(bucket_fn(seed))))
    rollout = _build_phase4_rollout_decision(feature=feature, settings=settings, canary_bucket=bucket)
    guardrail = _evaluate_guardrail_decision(
        signals=guardrail_signals,
        policy=guardrail_policy,
        state=guardrail_state,
        guardrail_engine=guardrail_engine,
        now=now,
    )
    resolved_rollout = _apply_guardrail_to_phase4_rollout(
        rollout=rollout,
        guardrail=guardrail,
    )
    if guardrail is not None and incident_hook is not None:
        merged_metadata = {
            "context_key": str(context_key or ""),
            "rollout_reason": str(resolved_rollout.reason or "unknown"),
            "rollout_use_feature": bool(resolved_rollout.use_feature),
            "rollout_rollback_activated": bool(resolved_rollout.rollback_activated),
            "guardrail_action": str(guardrail.action.value),
            "guardrail_reason": str(guardrail.reason or "unknown"),
            "guardrail_transitioned": bool(guardrail.transitioned),
            **{str(k): v for k, v in dict(incident_metadata or {}).items()},
        }
        try:
            incident_hook.handle_guardrail_decision(
                feature=feature,
                decision=guardrail,
                metadata=merged_metadata,
                now=now,
            )
        except Exception:
            pass
    return resolved_rollout


def load_worker_supervision_rollout_settings(
    *,
    env: Mapping[str, str] | None = None,
    setting_getter: SettingsGetter | None = None,
) -> Phase4FeatureRolloutSettings:
    source_env = env or os.environ
    enabled_raw = _read_setting(
        env=source_env,
        key=(
            "refactor_worker_supervision_enabled",
            "refactor_phase4_worker_supervision_enabled",
        ),
        env_key=(
            "REFACTOR_WORKER_SUPERVISION_ENABLED",
            "REFACTOR_PHASE4_WORKER_SUPERVISION_ENABLED",
        ),
        default=True,
        setting_getter=setting_getter,
    )
    canary_raw = _read_setting(
        env=source_env,
        key=(
            "refactor_worker_supervision_canary_percent",
            "refactor_phase4_worker_supervision_canary_percent",
        ),
        env_key=(
            "REFACTOR_WORKER_SUPERVISION_CANARY_PERCENT",
            "REFACTOR_PHASE4_WORKER_SUPERVISION_CANARY_PERCENT",
        ),
        default=100,
        setting_getter=setting_getter,
    )
    rollback_raw = _read_setting(
        env=source_env,
        key=(
            "refactor_worker_supervision_emergency_rollback",
            "refactor_phase4_worker_supervision_emergency_rollback",
            "refactor_phase4_emergency_rollback",
        ),
        env_key=(
            "REFACTOR_WORKER_SUPERVISION_EMERGENCY_ROLLBACK",
            "REFACTOR_PHASE4_WORKER_SUPERVISION_EMERGENCY_ROLLBACK",
            "REFACTOR_PHASE4_EMERGENCY_ROLLBACK",
        ),
        default=False,
        setting_getter=setting_getter,
    )
    return Phase4FeatureRolloutSettings(
        feature="worker_supervision",
        enabled=_to_bool(enabled_raw, default=True),
        canary_percent=_to_percent(canary_raw, default=100),
        emergency_rollback=_to_bool(rollback_raw, default=False),
    )


def resolve_worker_supervision_rollout_decision(
    context_key: str,
    *,
    env: Mapping[str, str] | None = None,
    setting_getter: SettingsGetter | None = None,
    bucket_resolver: BucketResolver | None = None,
    guardrail_state: SLOGuardrailState | None = None,
    guardrail_engine: SLOGuardrailEngine | None = None,
    incident_hook: IncidentHook | None = None,
    incident_metadata: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> Phase4FeatureRolloutDecision:
    settings = load_worker_supervision_rollout_settings(env=env, setting_getter=setting_getter)
    return resolve_phase4_feature_rollout_decision(
        settings=settings,
        context_key=context_key,
        bucket_resolver=bucket_resolver,
        guardrail_signals=load_phase4_slo_guardrail_signals(
            feature=settings.feature,
            env=env,
            setting_getter=setting_getter,
        ),
        guardrail_policy=load_phase4_slo_guardrail_policy(
            feature=settings.feature,
            env=env,
            setting_getter=setting_getter,
        ),
        guardrail_state=guardrail_state
        or load_phase4_slo_guardrail_state(
            feature=settings.feature,
            env=env,
            setting_getter=setting_getter,
        ),
        guardrail_engine=guardrail_engine,
        incident_hook=incident_hook,
        incident_metadata=incident_metadata,
        now=now,
    )


def _backpressure_rollout_candidates(channel: str | None, suffix: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    normalized_channel = str(channel or "").strip().lower()
    setting_keys: list[str] = []
    env_keys: list[str] = []
    if normalized_channel:
        setting_keys.extend(
            (
                f"refactor_{normalized_channel}_ingress_backpressure_{suffix}",
                f"refactor_{normalized_channel}_ingress_backpressure_rollout_{suffix}",
            )
        )
        env_keys.extend(
            (
                f"REFACTOR_{normalized_channel.upper()}_INGRESS_BACKPRESSURE_{suffix.upper()}",
                f"REFACTOR_{normalized_channel.upper()}_INGRESS_BACKPRESSURE_ROLLOUT_{suffix.upper()}",
            )
        )
    setting_keys.extend(
        (
            f"refactor_ingress_backpressure_{suffix}",
            f"refactor_ingress_backpressure_rollout_{suffix}",
        )
    )
    env_keys.extend(
        (
            f"REFACTOR_INGRESS_BACKPRESSURE_{suffix.upper()}",
            f"REFACTOR_INGRESS_BACKPRESSURE_ROLLOUT_{suffix.upper()}",
        )
    )
    return tuple(setting_keys), tuple(env_keys)


def load_backpressure_rollout_settings(
    *,
    channel: str | None,
    env: Mapping[str, str] | None = None,
    setting_getter: SettingsGetter | None = None,
    strict: bool = False,
) -> Phase4FeatureRolloutSettings:
    source_env = env or os.environ
    enabled_keys, enabled_env_keys = _backpressure_rollout_candidates(channel, "enabled")
    canary_keys, canary_env_keys = _backpressure_rollout_candidates(channel, "canary_percent")
    rollback_keys, rollback_env_keys = _backpressure_rollout_candidates(channel, "emergency_rollback")
    enabled_raw = _read_setting(
        env=source_env,
        key=enabled_keys,
        env_key=enabled_env_keys,
        default=True,
        setting_getter=setting_getter,
    )
    canary_raw = _read_setting(
        env=source_env,
        key=canary_keys,
        env_key=canary_env_keys,
        default=100,
        setting_getter=setting_getter,
    )
    rollback_raw = _read_setting(
        env=source_env,
        key=(
            *rollback_keys,
            "refactor_phase4_emergency_rollback",
        ),
        env_key=(
            *rollback_env_keys,
            "REFACTOR_PHASE4_EMERGENCY_ROLLBACK",
        ),
        default=False,
        setting_getter=setting_getter,
    )
    governance = resolve_registered_contract(
        registry=_ROLLOUT_CONFIG_REGISTRY,
        namespace=_BACKPRESSURE_ROLLOUT_NAMESPACE,
        raw_values={
            "enabled": enabled_raw,
            "canary_percent": canary_raw,
            "emergency_rollback": rollback_raw,
        },
        strict=strict,
    )
    _emit_governance_report("phase4 backpressure rollout", report=governance.report)
    values = governance.values
    return Phase4FeatureRolloutSettings(
        feature=f"{str(channel or 'generic').strip().lower() or 'generic'}_backpressure",
        enabled=bool(values["enabled"]),
        canary_percent=int(values["canary_percent"]),
        emergency_rollback=bool(values["emergency_rollback"]),
    )


def resolve_backpressure_rollout_decision(
    *,
    channel: str | None,
    context_key: str,
    env: Mapping[str, str] | None = None,
    setting_getter: SettingsGetter | None = None,
    bucket_resolver: BucketResolver | None = None,
    guardrail_state: SLOGuardrailState | None = None,
    guardrail_engine: SLOGuardrailEngine | None = None,
    incident_hook: IncidentHook | None = None,
    incident_metadata: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> Phase4FeatureRolloutDecision:
    settings = load_backpressure_rollout_settings(
        channel=channel,
        env=env,
        setting_getter=setting_getter,
    )
    return resolve_phase4_feature_rollout_decision(
        settings=settings,
        context_key=context_key,
        bucket_resolver=bucket_resolver,
        guardrail_signals=load_phase4_slo_guardrail_signals(
            feature=settings.feature,
            env=env,
            setting_getter=setting_getter,
        ),
        guardrail_policy=load_phase4_slo_guardrail_policy(
            feature=settings.feature,
            env=env,
            setting_getter=setting_getter,
        ),
        guardrail_state=guardrail_state
        or load_phase4_slo_guardrail_state(
            feature=settings.feature,
            env=env,
            setting_getter=setting_getter,
        ),
        guardrail_engine=guardrail_engine,
        incident_hook=incident_hook,
        incident_metadata=incident_metadata,
        now=now,
    )


def resolve_operator_recovery_rollout_decision(
    context_key: str,
    *,
    env: Mapping[str, str] | None = None,
    setting_getter: SettingsGetter | None = None,
    bucket_resolver: BucketResolver | None = None,
    guardrail_state: SLOGuardrailState | None = None,
    guardrail_engine: SLOGuardrailEngine | None = None,
    incident_hook: IncidentHook | None = None,
    incident_metadata: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> Phase4FeatureRolloutDecision:
    operator_settings = load_operator_recovery_settings(env=env, setting_getter=setting_getter)
    settings = Phase4FeatureRolloutSettings(
        feature="operator_recovery",
        enabled=operator_settings.enabled,
        canary_percent=operator_settings.canary_percent,
        emergency_rollback=operator_settings.emergency_rollback,
    )
    return resolve_phase4_feature_rollout_decision(
        settings=settings,
        context_key=context_key,
        bucket_resolver=bucket_resolver,
        guardrail_signals=load_phase4_slo_guardrail_signals(
            feature=settings.feature,
            env=env,
            setting_getter=setting_getter,
        ),
        guardrail_policy=load_phase4_slo_guardrail_policy(
            feature=settings.feature,
            env=env,
            setting_getter=setting_getter,
        ),
        guardrail_state=guardrail_state
        or load_phase4_slo_guardrail_state(
            feature=settings.feature,
            env=env,
            setting_getter=setting_getter,
        ),
        guardrail_engine=guardrail_engine,
        incident_hook=incident_hook,
        incident_metadata=incident_metadata,
        now=now,
    )


def resolve_webhook_ingress_rollout_decision(
    phone_number: str,
    *,
    env: Mapping[str, str] | None = None,
    setting_getter: SettingsGetter | None = None,
    bucket_resolver: BucketResolver | None = None,
) -> WebhookIngressRolloutDecision:
    settings = load_webhook_ingress_rollout_settings(env=env, setting_getter=setting_getter)
    bucket_fn = bucket_resolver or stable_sms_canary_bucket
    bucket = max(0, min(99, int(bucket_fn(phone_number))))

    if settings.emergency_rollback:
        return WebhookIngressRolloutDecision(
            use_refactor_runtime=False,
            reason="emergency_rollback",
            canary_percent=settings.canary_percent,
            canary_bucket=bucket,
            enabled=settings.enabled,
            emergency_rollback=True,
        )
    if not settings.enabled:
        return WebhookIngressRolloutDecision(
            use_refactor_runtime=False,
            reason="rollout_disabled",
            canary_percent=settings.canary_percent,
            canary_bucket=bucket,
            enabled=False,
            emergency_rollback=False,
        )
    if settings.canary_percent <= 0:
        return WebhookIngressRolloutDecision(
            use_refactor_runtime=False,
            reason="canary_disabled",
            canary_percent=0,
            canary_bucket=bucket,
            enabled=True,
            emergency_rollback=False,
        )
    if settings.canary_percent >= 100:
        return WebhookIngressRolloutDecision(
            use_refactor_runtime=True,
            reason="canary_full",
            canary_percent=100,
            canary_bucket=bucket,
            enabled=True,
            emergency_rollback=False,
        )
    if bucket < settings.canary_percent:
        return WebhookIngressRolloutDecision(
            use_refactor_runtime=True,
            reason="canary_selected",
            canary_percent=settings.canary_percent,
            canary_bucket=bucket,
            enabled=True,
            emergency_rollback=False,
        )
    return WebhookIngressRolloutDecision(
        use_refactor_runtime=False,
        reason="canary_excluded",
        canary_percent=settings.canary_percent,
        canary_bucket=bucket,
        enabled=True,
        emergency_rollback=False,
    )


def emit_webhook_ingress_rollout_metrics(
    *,
    decision: WebhookIngressRolloutDecision,
    phone_number: str,
    request_id: str,
    metric_logger: Callable[..., None] | None = None,
) -> None:
    logger = metric_logger
    if logger is None:
        from utils.structured_logging import log_quality_metric  # noqa: PLC0415

        logger = log_quality_metric

    payload = {
        "phone_tail": _phone_tail_for_metrics(phone_number),
        "request_id": request_id,
        "reason": decision.reason,
        "canary_percent": decision.canary_percent,
        "canary_bucket": decision.canary_bucket,
        "enabled": decision.enabled,
        "emergency_rollback": decision.emergency_rollback,
    }
    metric_name = (
        "webhook_rollout_refactor_selected"
        if decision.use_refactor_runtime
        else "webhook_rollout_legacy_selected"
    )
    logger(metric_name, **payload)


def emit_phase4_rollout_guardrail_metrics(
    *,
    decision: Phase4FeatureRolloutDecision,
    request_id: str = "",
    metric_logger: Callable[..., None] | None = None,
) -> None:
    logger = metric_logger
    if logger is None:
        from utils.structured_logging import log_quality_metric  # noqa: PLC0415

        logger = log_quality_metric

    payload = decision.metric_tags(request_id=request_id)
    logger("refactor_phase4_rollout_exposure", **payload)
    logger("refactor_phase4_slo_guardrail_action", **payload)
    if decision.degrade_activated:
        logger("refactor_phase4_rollout_degrade", **payload)
    if decision.rollback_activated:
        logger("refactor_phase4_rollout_rollback", **payload)
    if decision.safeguard_fallback:
        logger("refactor_phase4_rollout_safeguard_fallback", **payload)
