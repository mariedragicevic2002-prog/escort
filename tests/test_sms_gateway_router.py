"""SMS gateway routing tests — httpSMS backend."""

from services import sms_service


def test_send_sms_uses_httpsms_when_configured(monkeypatch):
    calls = {"httpsms": 0}

    monkeypatch.setattr("config.httpsms_is_enabled", lambda: True)
    monkeypatch.setattr(sms_service.httpsms_service, "is_configured", lambda: True)
    monkeypatch.setattr(
        sms_service.httpsms_service,
        "send_sms",
        lambda to, message, max_retries=3: calls.__setitem__("httpsms", calls["httpsms"] + 1) or True,
    )

    assert sms_service.send_sms("+61400000000", "hello")
    assert calls["httpsms"] == 1
    assert sms_service.get_last_sms_error() is None


def test_send_sms_fails_when_httpsms_fails(monkeypatch):
    monkeypatch.setattr("config.httpsms_is_enabled", lambda: True)
    monkeypatch.setattr(sms_service.httpsms_service, "is_configured", lambda: True)
    monkeypatch.setattr(
        sms_service.httpsms_service,
        "send_sms",
        lambda to, message, max_retries=3: False,
    )

    assert not sms_service.send_sms("+61400000000", "hello")
    err = sms_service.get_last_sms_error() or {}
    assert err.get("provider") == "httpsms"


def test_send_sms_fails_when_httpsms_not_configured(monkeypatch):
    monkeypatch.setattr("config.httpsms_is_enabled", lambda: True)
    monkeypatch.setattr(sms_service.httpsms_service, "is_configured", lambda: False)

    assert not sms_service.send_sms("+61400000000", "hello")
    err = sms_service.get_last_sms_error() or {}
    assert err.get("provider") == "httpsms"


def test_httpsms_disabled_does_not_send(monkeypatch):
    monkeypatch.setattr("config.httpsms_is_enabled", lambda: False)
    calls = {"httpsms": 0}
    monkeypatch.setattr(
        sms_service.httpsms_service,
        "send_sms",
        lambda to, message, max_retries=3: calls.__setitem__("httpsms", calls["httpsms"] + 1) or True,
    )

    assert not sms_service.send_sms("+61400000000", "hello")
    assert calls["httpsms"] == 0

