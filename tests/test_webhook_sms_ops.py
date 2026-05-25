"""Webhook token rotation and SMS failure observability helpers."""

from unittest.mock import patch


def test_httpsms_webhook_secrets_comma_separated(monkeypatch):
    import config as cfg

    def fake_get(key, default=None):
        if key == "httpsms_webhook_secret":
            return "alpha, beta ,"
        return default if default is not None else ""

    monkeypatch.setattr(cfg, "get_setting", fake_get)
    assert cfg.get_httpsms_webhook_secrets() == ["alpha", "beta"]
    assert cfg.get_httpsms_webhook_secret() == "alpha"


def test_send_sms_failure_emits_quality_metric():
    from services import sms_service

    with (
        patch.object(sms_service.httpsms_service, "is_configured", return_value=True),
        patch.object(sms_service.httpsms_service, "send_sms", return_value=False),
        patch("config.httpsms_is_enabled", return_value=True),
        patch("utils.structured_logging.log_quality_metric") as m_metric,
    ):
        ok = sms_service.send_sms("+61400111222", "hi")
        assert ok is False
        m_metric.assert_called_once()
        assert m_metric.call_args[0][0] == "sms_send_failed"


def test_httpsms_webhook_signature_settings_parsing(monkeypatch):
    import config as cfg

    values = {
        "httpsms_webhook_signature_secret": "sig123",
        "httpsms_webhook_signature_required": "yes",
        "httpsms_webhook_signature_tolerance_seconds": "12",
    }

    def fake_get(key, default=None):
        return values.get(key, default if default is not None else "")

    monkeypatch.setattr(cfg, "get_setting", fake_get)
    assert cfg.get_httpsms_webhook_signature_secret() == "sig123"
    assert cfg.httpsms_webhook_signature_required() is True
    assert cfg.get_httpsms_webhook_signature_tolerance_seconds() == 30


def test_httpsms_rotation_config_getters(monkeypatch):
    import config as cfg

    values = {
        "httpsms_webhook_secret_active": "active-key",
        "httpsms_webhook_secret_next": "next-key",
        "httpsms_webhook_secret_deprecated": "old-key",
        "httpsms_webhook_secret_cutover_state": "dual_window",
        "httpsms_webhook_signature_secret_active": "sig-active",
        "httpsms_webhook_signature_secret_next": "sig-next",
        "httpsms_webhook_signature_secret_deprecated": "sig-old",
        "httpsms_webhook_signature_secret_cutover_state": "post_cutover",
    }

    def fake_get(key, default=None):
        return values.get(key, default if default is not None else "")

    monkeypatch.setattr(cfg, "get_setting", fake_get)
    assert cfg.get_httpsms_webhook_secret_rotation_config() == {
        "active_key": "active-key",
        "next_key": "next-key",
        "deprecated_key": "old-key",
        "cutover_state": "dual_window",
    }
    assert cfg.get_httpsms_webhook_signature_rotation_config() == {
        "active_key": "sig-active",
        "next_key": "sig-next",
        "deprecated_key": "sig-old",
        "cutover_state": "post_cutover",
    }
