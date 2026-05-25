from __future__ import annotations

from app.ingress.rollout_controls import (
    load_sms_quick_ack_settings,
    resolve_sms_rollout_decision,
    stable_sms_canary_bucket,
)


def test_stable_sms_canary_bucket_is_deterministic() -> None:
    phone = "+61412345678"
    first = stable_sms_canary_bucket(phone, secret="secret-a")
    second = stable_sms_canary_bucket(phone, secret="secret-a")

    assert first == second
    assert 0 <= first <= 99


def test_rollout_precedence_emergency_rollback_wins() -> None:
    decision = resolve_sms_rollout_decision(
        "+61412345678",
        env={
            "REFACTOR_SMS_PIPELINE_ENABLED": "1",
            "REFACTOR_SMS_PIPELINE_CANARY_PERCENT": "100",
            "REFACTOR_SMS_PIPELINE_EMERGENCY_ROLLBACK": "true",
        },
    )

    assert decision.use_refactor_runtime is False
    assert decision.reason == "emergency_rollback"


def test_rollout_precedence_disabled_overrides_canary() -> None:
    decision = resolve_sms_rollout_decision(
        "+61412345678",
        env={
            "REFACTOR_SMS_PIPELINE_ENABLED": "0",
            "REFACTOR_SMS_PIPELINE_CANARY_PERCENT": "100",
        },
    )

    assert decision.use_refactor_runtime is False
    assert decision.reason == "rollout_disabled"


def test_canary_selection_is_bucket_driven() -> None:
    decision_in = resolve_sms_rollout_decision(
        "+61412345678",
        env={
            "REFACTOR_SMS_PIPELINE_ENABLED": "1",
            "REFACTOR_SMS_PIPELINE_CANARY_PERCENT": "50",
        },
        bucket_resolver=lambda _phone: 49,
    )
    decision_out = resolve_sms_rollout_decision(
        "+61412345678",
        env={
            "REFACTOR_SMS_PIPELINE_ENABLED": "1",
            "REFACTOR_SMS_PIPELINE_CANARY_PERCENT": "50",
        },
        bucket_resolver=lambda _phone: 50,
    )

    assert decision_in.use_refactor_runtime is True
    assert decision_in.reason == "canary_selected"
    assert decision_out.use_refactor_runtime is False
    assert decision_out.reason == "canary_excluded"


def test_sms_quick_ack_settings_default_disabled() -> None:
    settings = load_sms_quick_ack_settings(env={})

    assert settings.enabled is False
    assert settings.max_attempts == 5


def test_sms_quick_ack_settings_read_enabled_and_clamp_attempts() -> None:
    settings = load_sms_quick_ack_settings(
        env={
            "REFACTOR_SMS_INGRESS_QUICK_ACK_ENABLED": "1",
            "REFACTOR_SMS_INGRESS_QUICK_ACK_MAX_ATTEMPTS": "99",
        }
    )

    assert settings.enabled is True
    assert settings.max_attempts == 20
