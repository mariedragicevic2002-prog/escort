"""
Integration tests for the /webhook/v3 endpoint.

Uses Flask test client with all I/O dependencies mocked so the tests:
  - Run without a database or network
  - Are fully deterministic
  - Verify routing, middleware, and controller logic in isolation
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from flask import Flask


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_app() -> Flask:
    """Minimal Flask app with webhook_v3 blueprint registered."""
    app = Flask(__name__)
    app.config["TESTING"] = True

    from app.orchestration.webhook_controller import bp
    app.register_blueprint(bp)
    return app


@pytest.fixture()
def client():
    app = _make_app()
    with app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# Tests: happy path
# ---------------------------------------------------------------------------

class TestWebhookV3HappyPath:
    def test_returns_200_on_valid_payload(self, client):
        """A valid inbound payload should return HTTP 200."""
        with (
            patch("app.orchestration.webhook_controller._middleware") as mock_mw,
            patch("app.orchestration.webhook_controller._engine") as mock_engine,
            patch("app.orchestration.webhook_controller._composer") as mock_composer,
            patch("app.orchestration.webhook_controller._dispatcher"),
        ):
            mock_mw.run.return_value = None  # no denial
            mock_engine.process.return_value = MagicMock()
            mock_composer.compose.return_value = ["Hello!"]

            resp = client.post(
                "/webhook/v3",
                json={"phone_number": "+61400000001", "message": "hi"},
            )
        assert resp.status_code == 200

    def test_empty_payload_still_returns_200(self, client):
        """Even with a missing phone/message the controller should not crash."""
        with (
            patch("app.orchestration.webhook_controller._middleware"),
            patch("app.orchestration.webhook_controller._engine") as mock_engine,
            patch("app.orchestration.webhook_controller._composer") as mock_composer,
            patch("app.orchestration.webhook_controller._dispatcher"),
        ):
            mock_engine.process.return_value = MagicMock()
            mock_composer.compose.return_value = []

            resp = client.post("/webhook/v3", json={})
        assert resp.status_code == 200

    def test_dispatcher_called_when_messages_present(self, client):
        """OutboundDispatcher.dispatch() must be called when the composer returns messages."""
        with (
            patch("app.orchestration.webhook_controller._middleware"),
            patch("app.orchestration.webhook_controller._engine") as mock_engine,
            patch("app.orchestration.webhook_controller._composer") as mock_composer,
            patch("app.orchestration.webhook_controller._dispatcher") as mock_dispatcher,
        ):
            mock_engine.process.return_value = MagicMock()
            mock_composer.compose.return_value = ["Message 1", "Message 2"]

            client.post(
                "/webhook/v3",
                json={"phone_number": "+61400000001", "message": "book me in"},
            )
        mock_dispatcher.dispatch.assert_called_once()

    def test_dispatcher_not_called_when_no_messages(self, client):
        """OutboundDispatcher.dispatch() must NOT be called when composer returns empty list."""
        with (
            patch("app.orchestration.webhook_controller._middleware"),
            patch("app.orchestration.webhook_controller._engine") as mock_engine,
            patch("app.orchestration.webhook_controller._composer") as mock_composer,
            patch("app.orchestration.webhook_controller._dispatcher") as mock_dispatcher,
        ):
            mock_engine.process.return_value = MagicMock()
            mock_composer.compose.return_value = []

            client.post("/webhook/v3", json={"phone_number": "+61400000001", "message": "hi"})
        mock_dispatcher.dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: middleware denial
# ---------------------------------------------------------------------------

class TestWebhookV3MiddlewareDenial:
    def test_rate_limited_returns_429(self, client):
        from app.orchestration.middleware_pipeline import MiddlewareDenied

        with patch("app.orchestration.webhook_controller._middleware") as mock_mw:
            mock_mw.run.side_effect = MiddlewareDenied("rate_limited", 429)

            resp = client.post(
                "/webhook/v3",
                json={"phone_number": "+61400000001", "message": "hi"},
            )
        assert resp.status_code == 429

    def test_unauthorized_returns_403(self, client):
        from app.orchestration.middleware_pipeline import MiddlewareDenied

        with patch("app.orchestration.webhook_controller._middleware") as mock_mw:
            mock_mw.run.side_effect = MiddlewareDenied("unauthorized", 403)

            resp = client.post("/webhook/v3", json={})
        assert resp.status_code == 403

    def test_denied_body_contains_error_key(self, client):
        from app.orchestration.middleware_pipeline import MiddlewareDenied

        with patch("app.orchestration.webhook_controller._middleware") as mock_mw:
            mock_mw.run.side_effect = MiddlewareDenied("bad_payload", 400)

            resp = client.post("/webhook/v3", json={})
        data = resp.get_json()
        assert "error" in data


# ---------------------------------------------------------------------------
# Tests: error handling
# ---------------------------------------------------------------------------

class TestWebhookV3ErrorHandling:
    def test_engine_crash_returns_500(self, client):
        """If the conversation engine raises an unexpected exception, return 500."""
        with (
            patch("app.orchestration.webhook_controller._middleware"),
            patch("app.orchestration.webhook_controller._engine") as mock_engine,
        ):
            mock_engine.process.side_effect = RuntimeError("unexpected boom")

            resp = client.post(
                "/webhook/v3",
                json={"phone_number": "+61400000001", "message": "hi"},
            )
        assert resp.status_code == 500

    def test_500_body_contains_error_key(self, client):
        with (
            patch("app.orchestration.webhook_controller._middleware"),
            patch("app.orchestration.webhook_controller._engine") as mock_engine,
        ):
            mock_engine.process.side_effect = ValueError("boom")

            resp = client.post("/webhook/v3", json={})
        data = resp.get_json()
        assert data.get("error") == "internal_error"


# ---------------------------------------------------------------------------
# Tests: phone extraction
# ---------------------------------------------------------------------------

class TestWebhookV3PhoneExtraction:
    def test_phone_from_phone_number_field(self, client):
        """phone_number field is the primary key."""
        with (
            patch("app.orchestration.webhook_controller._middleware"),
            patch("app.orchestration.webhook_controller._engine") as mock_engine,
            patch("app.orchestration.webhook_controller._composer") as mock_composer,
            patch("app.orchestration.webhook_controller._dispatcher") as mock_dispatcher,
        ):
            mock_engine.process.return_value = MagicMock()
            mock_composer.compose.return_value = ["ok"]

            client.post("/webhook/v3", json={"phone_number": "+61400000002", "message": "test"})

        call_args = mock_dispatcher.dispatch.call_args
        assert call_args[0][0] == "+61400000002"

    def test_phone_falls_back_to_from_field(self, client):
        with (
            patch("app.orchestration.webhook_controller._middleware"),
            patch("app.orchestration.webhook_controller._engine") as mock_engine,
            patch("app.orchestration.webhook_controller._composer") as mock_composer,
            patch("app.orchestration.webhook_controller._dispatcher") as mock_dispatcher,
        ):
            mock_engine.process.return_value = MagicMock()
            mock_composer.compose.return_value = ["ok"]

            client.post("/webhook/v3", json={"from": "+61400000003", "message": "test"})

        call_args = mock_dispatcher.dispatch.call_args
        assert call_args[0][0] == "+61400000003"
