"""
Sentry initialization (optional). Set SENTRY_DSN in the environment to enable.

Errors at ERROR level are forwarded to Sentry via LoggingIntegration; Flask and uncaught
exceptions are captured via FlaskIntegration. Conversation tags are updated from
``structured_logging.set_observability_context`` / ``set_request_id``.
"""

from __future__ import annotations

import logging
import os

_initialized = False


def init_sentry(app=None) -> None:
    """Initialize sentry-sdk when SENTRY_DSN is set. Safe to call multiple times."""
    global _initialized
    if _initialized:
        return
    dsn = (os.environ.get("SENTRY_DSN") or "").strip()
    if not dsn:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
    except ImportError:
        logging.getLogger(__name__).warning(
            "SENTRY_DSN is set but sentry-sdk is not installed — pip install sentry-sdk[flask]"
        )
        return

    traces = float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0") or "0")
    profiles = float(os.environ.get("SENTRY_PROFILES_SAMPLE_RATE", "0") or "0")
    env = (os.environ.get("SENTRY_ENVIRONMENT") or os.environ.get("FLASK_ENV") or "production").strip()

    if app is not None:
        logging.getLogger(__name__).debug(
            "Attaching Sentry to Flask app %s", getattr(app, "name", type(app).__name__)
        )
    integrations = [
        FlaskIntegration(),
        LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
    ]

    sentry_sdk.init(
        dsn=dsn,
        integrations=integrations,
        environment=env,
        traces_sample_rate=min(1.0, max(0.0, traces)),
        profiles_sample_rate=min(1.0, max(0.0, profiles)),
        send_default_pii=False,
    )
    _initialized = True
    logging.getLogger(__name__).info("Sentry initialized (environment=%s)", env)
