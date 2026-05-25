from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def flask_app():
    import main_v2.application as appmod

    return appmod.app


def test_apply_threshold_optimizer_persists_suggestions(flask_app, monkeypatch):
    import admin.blueprints.stats as stats_mod

    saved = {}

    monkeypatch.setattr(
        stats_mod,
        "_collect_all_stats",
        lambda **_kwargs: {
            "threshold_optimizer": {
                "suggested_thresholds": {
                    "global": 0.44,
                    "qualification": 0.41,
                    "availability": 0.42,
                    "screening": 0.43,
                    "deposit": 0.45,
                    "confirmation": 0.46,
                    "follow_up": 0.44,
                }
            }
        },
    )
    def _fake_set_setting(key: str, value: str):
        saved[key] = value
        return True

    monkeypatch.setattr(stats_mod, "set_setting", _fake_set_setting)

    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess["stats_authenticated"] = True
        sess["csrf_token"] = "test-csrf-token"

    response = client.post(
        "/stats/apply-threshold-optimizer",
        data={
            "csrf_token": "test-csrf-token",
            "days": "30",
            "location": "all",
            "experience": "all",
        },
    )
    assert response.status_code == 302
    assert saved["ai_fallback_confidence_threshold"] == "0.44"
    assert saved["ai_fallback_confidence_threshold_deposit"] == "0.45"


def test_apply_rollout_guardrail_reduce_v2(flask_app, monkeypatch):
    import admin.blueprints.stats as stats_mod

    saved = {}

    monkeypatch.setattr(
        stats_mod,
        "_collect_all_stats",
        lambda **_kwargs: {
            "rollout_guardrail": {
                "recommended_action": "reduce_v2",
                "suggested_rollout_percent": 25,
            }
        },
    )
    def _fake_set_setting(key: str, value: str):
        saved[key] = value
        return True

    monkeypatch.setattr(stats_mod, "set_setting", _fake_set_setting)

    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess["stats_authenticated"] = True
        sess["csrf_token"] = "test-csrf-token"

    response = client.post(
        "/stats/apply-rollout-guardrail",
        data={
            "csrf_token": "test-csrf-token",
            "days": "30",
            "location": "all",
            "experience": "all",
        },
    )
    assert response.status_code == 302
    assert saved["flow_version_default"] == "rollout"
    assert saved["flow_version_v2_rollout_percent"] == "25"


def test_apply_rollout_guardrail_force_v1(flask_app, monkeypatch):
    import admin.blueprints.stats as stats_mod

    saved = {}

    monkeypatch.setattr(
        stats_mod,
        "_collect_all_stats",
        lambda **_kwargs: {
            "rollout_guardrail": {
                "recommended_action": "force_v1",
            }
        },
    )
    def _fake_set_setting(key: str, value: str):
        saved[key] = value
        return True

    monkeypatch.setattr(stats_mod, "set_setting", _fake_set_setting)

    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess["stats_authenticated"] = True
        sess["csrf_token"] = "test-csrf-token"

    response = client.post(
        "/stats/apply-rollout-guardrail",
        data={
            "csrf_token": "test-csrf-token",
            "days": "30",
            "location": "all",
            "experience": "all",
        },
    )
    assert response.status_code == 302
    assert saved["flow_version_default"] == "v1"
    assert saved["flow_version_v2_rollout_percent"] == "0"
