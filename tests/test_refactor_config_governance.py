from __future__ import annotations

import logging

import pytest

from app.config_governance import ConfigValidationError
from app.ingress.adaptive_rate_limiter import load_adaptive_rate_limiter_settings
from app.ingress.backpressure_policy import load_ingress_backpressure_settings
from app.ingress.rollout_controls import load_backpressure_rollout_settings


def test_config_governance_rejects_invalid_adaptive_mode_in_strict_mode() -> None:
    with pytest.raises(ConfigValidationError) as exc:
        load_adaptive_rate_limiter_settings(
            channel="sms",
            base_queue_depth=10,
            base_lag_seconds=90,
            env={
                "REFACTOR_SMS_ADAPTIVE_RATE_LIMITER_ENABLED": "true",
                "REFACTOR_SMS_ADAPTIVE_RATE_LIMITER_PROVIDER_FAILURE_MODE": "panic_mode",
            },
            strict=True,
        )

    assert "provider_failure_mode" in str(exc.value)


def test_config_governance_applies_safe_backpressure_defaults_when_inputs_are_invalid() -> None:
    settings = load_ingress_backpressure_settings(
        channel="sms",
        env={
            "REFACTOR_SMS_INGRESS_BACKPRESSURE_MAX_QUEUE_DEPTH": "not-a-number",
            "REFACTOR_SMS_INGRESS_BACKPRESSURE_MAX_LAG_SECONDS": "-5",
            "REFACTOR_SMS_INGRESS_BACKPRESSURE_OVERLOAD_BEHAVIOR": "panic",
            "REFACTOR_SMS_INGRESS_BACKPRESSURE_DEGRADE_MAX_ATTEMPTS": "999",
        },
    )

    assert settings.max_queue_depth == 250
    assert settings.max_lag_seconds == 1
    assert settings.overload_behavior == "sync_fallback"
    assert settings.degrade_max_attempts == 20


def test_config_governance_emits_drift_signal_for_mismatched_rollout_runtime_value(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="app.ingress.rollout_controls"):
        settings = load_backpressure_rollout_settings(
            channel="sms",
            env={
                "REFACTOR_SMS_INGRESS_BACKPRESSURE_ENABLED": "true",
                "REFACTOR_SMS_INGRESS_BACKPRESSURE_CANARY_PERCENT": "500",
            },
        )

    assert settings.canary_percent == 100
    assert "phase4 backpressure rollout config drift detected fields=canary_percent" in caplog.text
