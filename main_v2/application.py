# ruff: noqa: E402
"""
Main Flask application entry point - Version 2 with all handlers wired.

This file lives in refactor2/main_v2/ and is the local mirror of the
PythonAnywhere production application.py.  It adds registration of the
Clean Architecture webhook_v3_bp blueprint on top of the legacy app.

When PythonAnywhere's WSGI is eventually switched to point at refactor2/ this
file will become the active entry point.  Until then, it serves as a complete
local development replica so developers can run `python main_v2/application.py`
from the refactor2/ root and have a fully-wired server.
"""
import logging
import os
import sys
from datetime import timedelta

from flask import Flask, jsonify, request, session

# Project root — parent of this main_v2 package (i.e. refactor2/).
# All imports from core/, services/, app/, etc. resolve from here.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from core.classifier import Classifier
from core.router import Router
from services.rollout_guardrail_scheduler import start_rollout_guardrail_scheduler
from core.state_manager import StateManager
from services.ai_service import AIService

_raw_log_level = (os.environ.get("LOG_LEVEL") or "").strip().upper()
if _raw_log_level in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
    _log_level = getattr(logging, _raw_log_level)
else:
    _log_level = logging.DEBUG if config.DEBUG else logging.INFO
logging.basicConfig(
    level=_log_level,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logging.getLogger().setLevel(_log_level)

from . import runtime
from .csrf import register_csrf
from .database import initialize_database_service, maybe_run_startup_database_tasks
from .helpers import _env_flag
from .log import logger
from .router_registration import register_router_handlers

logger.setLevel(_log_level)

start_rollout_guardrail_scheduler()

if config.DEBUG:
    logger.warning(
        "\u26A0\uFE0F  DEBUG mode is ON. Never run with DEBUG=True in production!"
    )

from utils.structured_logging import (
    clear_observability_context,
    configure_observability_logging,
    set_observability_context,
)

configure_observability_logging()

from main_v2.edge_routes import register_edge_routes

app = Flask(__name__)

_env_secret = (os.environ.get('SECRET_KEY') or '').strip()
if _env_secret:
    app.secret_key = _env_secret
else:
    import secrets as _secrets
    app.secret_key = _secrets.token_urlsafe(48)
    logger.warning(
        "SECRET_KEY env is empty; using a random per-process key. "
        "Set SECRET_KEY (env) or flask_secret_key (admin Config) before serving production traffic."
    )

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=not config.DEBUG,
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=30),
    MAX_CONTENT_LENGTH=10 * 1024 * 1024,
)

try:
    config.validate_config()
except RuntimeError as _cfg_err:
    raise SystemExit(
        f"Refusing to start due to config errors: {_cfg_err}\n\n"
        "Fix (local PC): create a .env next to config.py — copy .env.example to .env "
        "and set DEBUG=true plus a non-default ADMIN_PASSWORD. "
        "On PowerShell use $env:DEBUG='true'; $env:ADMIN_PASSWORD='YourPwd' before python, "
        "not CMD-style \"set\".\n\n"
        "Fix (production): set ADMIN_PASSWORD and SECRET_KEY (or flask_secret_key in DB after "
        "DATABASE_URL works), DATABASE_URL, and other vars listed in .env.example.\n"
    )

_production_signal = (
    bool(os.environ.get("PYTHONANYWHERE_DOMAIN"))
    or bool(os.environ.get("PYTHONANYWHERE_SITE"))
    or (os.environ.get("ENVIRONMENT") or "").strip().lower() == "production"
)
if _production_signal:
    from core.production_config import validate_production_config
    validate_production_config()

register_csrf(app, logger)

from core.security import add_security_headers as _add_security_headers
app.after_request(_add_security_headers)

db_service = initialize_database_service()
maybe_run_startup_database_tasks(db_service)

_db_flask_secret = ""
if db_service:
    try:
        from core.settings_manager import get_setting as _gsk
        _db_flask_secret = (_gsk("flask_secret_key") or "").strip()
        if _db_flask_secret:
            app.secret_key = _db_flask_secret
    except Exception as _sk_err:
        logger.warning("flask_secret_key from DB skipped: %s", _sk_err)

if not config.DEBUG:
    _env_ok = bool(_env_secret)
    _db_ok = bool(_db_flask_secret)
    if not _env_ok and not _db_ok:
        logger.critical(
            "No production Flask secret: set SECRET_KEY in the host environment or save "
            "flask_secret_key on the Config page."
        )
        raise SystemExit(
            "Refusing to start: set SECRET_KEY or flask_secret_key (Config) for production (DEBUG=false)."
        )

state_manager = StateManager(db_service) if db_service else None
if app and state_manager:
    app.config["STATE_MANAGER"] = state_manager

ai_service = AIService(provider=None)
logger.info("AI service initialized (provider from admin settings at runtime)")
try:
    ai_service._ensure_api_keys()
    logger.info("Claude API key: %s", "set" if (ai_service.claude_key and ai_service.claude_key.strip()) else "not set")
    logger.info("Gemini API key: %s", "set" if (ai_service.gemini_key and ai_service.gemini_key.strip()) else "not set")
except Exception as e:
    logger.warning("Could not check AI API keys at startup: %s", e)

classifier = Classifier(ai_service=ai_service)

router = Router()
register_router_handlers(router)

runtime.db_service = db_service
runtime.state_manager = state_manager
runtime.ai_service = ai_service
runtime.classifier = classifier
runtime.router = router

register_edge_routes(app)

try:
    from utils.sentry_init import init_sentry
    init_sentry(app)
except Exception as _sentry_err:
    logger.warning("Sentry init skipped: %s", _sentry_err)


@app.before_request
def _init_request_observability():
    try:
        from utils.structured_logging import set_request_id
        path = (request.path or "")
        if path.startswith("/sms/"):
            return
        set_request_id(request.headers.get("X-Request-ID") or None)
        if session.get("admin_authenticated") or session.get("config_authenticated"):
            set_observability_context(state="admin")
    except Exception as e:
        logger.debug("before_request observability seed skipped: %s", e)


@app.teardown_request
def _teardown_observability(exc):
    try:
        clear_observability_context()
    except Exception as e:
        logger.warning("clear_observability_context failed in teardown_request: %s", e)


def _wants_json_response() -> bool:
    try:
        accept = (request.headers.get('Accept') or '').lower()
        xrw = (request.headers.get('X-Requested-With') or '').lower()
        path = (request.path or '').lower()
    except Exception:
        return False
    if 'application/json' in accept:
        return True
    if xrw == 'xmlhttprequest':
        return True
    return path.startswith('/api/') or path.startswith('/admin/api/')


@app.errorhandler(404)
def _not_found_handler(err):
    if _wants_json_response():
        return jsonify({"status": "error", "code": 404, "message": "Not Found"}), 404
    return ("Not Found", 404)


@app.errorhandler(405)
def _method_not_allowed_handler(err):
    if _wants_json_response():
        return jsonify({"status": "error", "code": 405, "message": "Method Not Allowed"}), 405
    return ("Method Not Allowed", 405)


@app.errorhandler(500)
def _internal_error_handler(err):
    logger.error("Unhandled 500 on %s: %s", request.path if request else "?", err, exc_info=True)
    if _wants_json_response():
        return jsonify({"status": "error", "code": 500, "message": "Internal Server Error"}), 500
    return ("Internal Server Error", 500)


@app.errorhandler(Exception)
def _unhandled_exception_handler(err):
    from werkzeug.exceptions import HTTPException
    if isinstance(err, HTTPException):
        return err
    logger.exception("Unhandled exception on %s", request.path if request else "?")
    if _wants_json_response():
        return jsonify({"status": "error", "code": 500, "message": "Internal Server Error"}), 500
    return ("Internal Server Error", 500)


from main_v2.admin_endpoints import register_admin_routes
register_admin_routes(app, router, state_manager)

# SMS gateway relay endpoint
from main_v2.sms_gateway import sms_gateway_bp
app.register_blueprint(sms_gateway_bp)

# Legacy inbound webhook endpoint (kept during migration)
@app.route('/webhook', methods=['POST'])
def webhook():
    import uuid as _uuid
    from main_v2.webhook_main_flow import _process_webhook
    request_id = (
        (request.headers.get('X-Request-ID') or '').strip()
        or _uuid.uuid4().hex[:16]
    )
    return _process_webhook(request_id)


# ─────────────────────────────────────────────────────────────────────────────
# Clean Architecture webhook endpoint (v3)
# Backed by the orchestration pipeline in app/orchestration/.
# This is the target end-state for all inbound traffic.
# The legacy /webhook route above remains active during migration.
# ─────────────────────────────────────────────────────────────────────────────
try:
    from app.orchestration.webhook_controller import bp as webhook_v3_bp
    app.register_blueprint(webhook_v3_bp)
    logger.info("webhook_v3 blueprint registered at /webhook/v3")
except Exception as _v3_err:
    logger.warning("webhook_v3 blueprint not loaded: %s", _v3_err)


def _maybe_init_background_jobs():
    if not _env_flag("RUN_STARTUP_BACKGROUND_JOBS", "true"):
        msg = (
            "Skipping background job startup (RUN_STARTUP_BACKGROUND_JOBS=false). "
            "Background jobs are enabled by default; keep this env var set to false only when you intentionally disable "
            "reminders/sync/cleanup."
        )
        if config.DEBUG:
            logger.info(msg)
        else:
            logger.warning(msg)
        return

    try:
        import importlib.util
        apscheduler_available = importlib.util.find_spec("apscheduler") is not None
        if apscheduler_available:
            from services.background_jobs import init_scheduler
            if state_manager and db_service:
                init_scheduler(state_manager, db_service)
                logger.info("Background jobs initialized")
        else:
            logger.warning("APScheduler not installed - background jobs disabled.")
    except Exception as e:
        logger.warning(f"Failed to initialize background jobs: {e}")


_maybe_init_background_jobs()


if __name__ == '__main__':
    logger.info("Starting new streamlined chatbot...")
    logger.info(f"Database URL configured: {bool(config.DATABASE_URL)}")
    router.print_routes()
    _bind_host = os.environ.get("FLASK_RUN_HOST", "127.0.0.1")
    _bind_port = int(os.environ.get("FLASK_RUN_PORT", "5001"))
    app.run(host=_bind_host, port=_bind_port, debug=config.DEBUG)
