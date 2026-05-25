from __future__ import annotations

from app.ingress.rollout_controls import (
    load_webhook_ingress_quick_ack_settings,
    resolve_webhook_ingress_rollout_decision,
)


def test_webhook_rollout_precedence_emergency_rollback_wins() -> None:
    decision = resolve_webhook_ingress_rollout_decision(
        "+61412345678",
        env={
            "REFACTOR_WEBHOOK_INGRESS_ENABLED": "1",
            "REFACTOR_WEBHOOK_INGRESS_CANARY_PERCENT": "100",
            "REFACTOR_WEBHOOK_INGRESS_EMERGENCY_ROLLBACK": "true",
        },
    )
    assert decision.use_refactor_runtime is False
    assert decision.reason == "emergency_rollback"


def test_webhook_rollout_canary_selection_is_bucket_driven() -> None:
    decision_in = resolve_webhook_ingress_rollout_decision(
        "+61412345678",
        env={
            "REFACTOR_WEBHOOK_INGRESS_ENABLED": "1",
            "REFACTOR_WEBHOOK_INGRESS_CANARY_PERCENT": "40",
        },
        bucket_resolver=lambda _phone: 39,
    )
    decision_out = resolve_webhook_ingress_rollout_decision(
        "+61412345678",
        env={
            "REFACTOR_WEBHOOK_INGRESS_ENABLED": "1",
            "REFACTOR_WEBHOOK_INGRESS_CANARY_PERCENT": "40",
        },
        bucket_resolver=lambda _phone: 40,
    )

    assert decision_in.use_refactor_runtime is True
    assert decision_in.reason == "canary_selected"
    assert decision_out.use_refactor_runtime is False
    assert decision_out.reason == "canary_excluded"


def test_webhook_quick_ack_settings_default_disabled() -> None:
    settings = load_webhook_ingress_quick_ack_settings(env={})

    assert settings.enabled is False
    assert settings.max_attempts == 5


def test_webhook_quick_ack_settings_enabled_from_env() -> None:
    settings = load_webhook_ingress_quick_ack_settings(
        env={
            "REFACTOR_WEBHOOK_INGRESS_QUICK_ACK_ENABLED": "true",
            "REFACTOR_WEBHOOK_INGRESS_QUICK_ACK_MAX_ATTEMPTS": "7",
        }
    )

    assert settings.enabled is True
    assert settings.max_attempts == 7
