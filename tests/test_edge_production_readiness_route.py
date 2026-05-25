from __future__ import annotations

from flask import Flask

import main_v2.edge_routes as edge_routes


class _ReadinessServiceStub:
    def build_scrubbed_report(self):
        return {
            "schema_version": "production-readiness.v1",
            "overall_status": "healthy",
            "guardrails": [
                {
                    "feature": "worker_supervision",
                    "reason": "token=[REDACTED]",
                }
            ],
        }


def _client(monkeypatch):
    app = Flask(__name__)
    edge_routes.register_edge_routes(app)
    monkeypatch.setattr(edge_routes, "_build_production_readiness_service", lambda: _ReadinessServiceStub())
    return app.test_client()


def test_internal_production_readiness_route_requires_auth_header(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setenv("REFACTOR_INTERNAL_REPORT_SECRET", "top-secret")

    response = client.get("/internal/refactor/production-readiness")
    payload = response.get_json()

    assert response.status_code == 401
    assert payload["error"] == "Unauthorized"


def test_internal_production_readiness_route_returns_scrubbed_payload(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setenv("REFACTOR_INTERNAL_REPORT_SECRET", "top-secret")

    response = client.get(
        "/internal/refactor/production-readiness",
        headers={"X-Internal-Secret": "top-secret"},
    )
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["schema_version"] == "production-readiness.v1"
    assert payload["guardrails"][0]["reason"] == "token=[REDACTED]"
