from __future__ import annotations

import pytest


class _DummyResponse:
    def __init__(self, text: str):
        self.text = text


def _patch_config_page_dependencies(monkeypatch, *, setting_overrides=None):
    import admin.blueprints.config as config_mod

    mapping = setting_overrides or {}

    monkeypatch.setattr(config_mod, "get_all_settings", lambda: {})
    monkeypatch.setattr(
        config_mod,
        "get_setting",
        lambda key, default=None: mapping.get(key, default),
    )
    monkeypatch.setattr(
        config_mod,
        "get_watchlist_stats",
        lambda: {"count": 0, "last_uploaded_at": "", "last_uploaded_filename": ""},
    )
    monkeypatch.setattr(config_mod.requests, "get", lambda *args, **kwargs: _DummyResponse("127.0.0.1"))


@pytest.fixture(scope="module")
def flask_app():
    import main_v2.application as appmod

    return appmod.app


def test_config_page_renders_flow_rollout_controls(flask_app, monkeypatch):
    _patch_config_page_dependencies(
        monkeypatch,
        setting_overrides={
            "flow_version_default": "v2",
            "flow_version_v2_rollout_percent": "35",
            "ai_fallback_confidence_threshold": "0.52",
            "ai_fallback_confidence_threshold_deposit": "0.65",
        },
    )

    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess["config_authenticated"] = True

    response = client.get("/config")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'action="/config/save-flow-version-rollout"' in body
    assert 'name="flow_version_default"' in body
    assert 'name="flow_version_v2_rollout_percent"' in body
    assert 'option value="v2" selected' in body
    assert 'value="35"' in body
    assert 'action="/config/save-ai-fallback-threshold"' in body
    assert 'name="ai_fallback_confidence_threshold"' in body
    assert 'name="ai_fallback_confidence_threshold_deposit"' in body
    assert 'value="0.52"' in body
    assert 'value="0.65"' in body


def test_save_flow_rollout_sanitizes_and_persists(flask_app, monkeypatch):
    import admin.blueprints.config as config_mod

    saved = {}

    def _fake_set_setting(key: str, value: str):
        saved[key] = value
        return True

    monkeypatch.setattr(config_mod, "set_setting", _fake_set_setting)

    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess["config_authenticated"] = True
        sess["csrf_token"] = "test-csrf-token"

    response = client.post(
        "/config/save-flow-version-rollout",
        data={
            "csrf_token": "test-csrf-token",
            "flow_version_default": "invalid",
            "flow_version_v2_rollout_percent": "999",
        },
    )
    assert response.status_code == 302
    assert saved["flow_version_default"] == "rollout"
    assert saved["flow_version_v2_rollout_percent"] == "100"


def test_save_flow_rollout_requires_auth(flask_app, monkeypatch):
    import admin.blueprints.config as config_mod

    calls = []

    def _fake_set_setting(key: str, value: str):
        calls.append((key, value))
        return True

    monkeypatch.setattr(config_mod, "set_setting", _fake_set_setting)

    client = flask_app.test_client()
    response = client.post(
        "/config/save-flow-version-rollout",
        data={
            "flow_version_default": "v1",
            "flow_version_v2_rollout_percent": "20",
        },
    )
    assert response.status_code == 302
    assert not calls


def test_save_ai_fallback_threshold_sanitizes_and_persists(flask_app, monkeypatch):
    import admin.blueprints.config as config_mod

    saved = {}

    def _fake_set_setting(key: str, value: str):
        saved[key] = value
        return True

    monkeypatch.setattr(config_mod, "set_setting", _fake_set_setting)

    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess["config_authenticated"] = True
        sess["csrf_token"] = "test-csrf-token"

    response = client.post(
        "/config/save-ai-fallback-threshold",
        data={
            "csrf_token": "test-csrf-token",
            "ai_fallback_confidence_threshold": "2.75",
            "ai_fallback_confidence_threshold_qualification": "0.33",
            "ai_fallback_confidence_threshold_availability": "bad",
            "ai_fallback_confidence_threshold_screening": "-1",
            "ai_fallback_confidence_threshold_deposit": "1.2",
            "ai_fallback_confidence_threshold_confirmation": "0.88",
            "ai_fallback_confidence_threshold_follow_up": "",
        },
    )
    assert response.status_code == 302
    assert saved["ai_fallback_confidence_threshold"] == "1.00"
    assert saved["ai_fallback_confidence_threshold_qualification"] == "0.33"
    assert saved["ai_fallback_confidence_threshold_availability"] == "1.00"
    assert saved["ai_fallback_confidence_threshold_screening"] == "0.00"
    assert saved["ai_fallback_confidence_threshold_deposit"] == "1.00"
    assert saved["ai_fallback_confidence_threshold_confirmation"] == "0.88"
    assert saved["ai_fallback_confidence_threshold_follow_up"] == "1.00"


def test_save_ai_fallback_threshold_requires_auth(flask_app, monkeypatch):
    import admin.blueprints.config as config_mod

    calls = []

    def _fake_set_setting(key: str, value: str):
        calls.append((key, value))
        return True

    monkeypatch.setattr(config_mod, "set_setting", _fake_set_setting)

    client = flask_app.test_client()
    response = client.post(
        "/config/save-ai-fallback-threshold",
        data={
            "ai_fallback_confidence_threshold": "0.40",
        },
    )
    assert response.status_code == 302
    assert not calls
