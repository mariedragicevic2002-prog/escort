"""CSRF for authenticated admin POSTs to selected URL prefixes."""

from utils.log_sanitize import LOG_SUPPRESSED_FMT


import hmac

from flask import flash, jsonify, redirect, request, session, url_for


import logging
logger = logging.getLogger("escort_chatbot.csrf")

_CSRF_PROTECTED_PREFIXES = ("/admin", "/config", "/schedule", "/rates", "/database", "/feedback", "/location")


def _rotate_csrf_token() -> None:
    """Generate a fresh CSRF token in the session (M3).

    Called after a successful validation on full-page form POSTs so a single
    leaked token is valid for exactly one submission rather than the whole
    session lifetime. AJAX/XHR POSTs deliberately do NOT rotate, since the
    dashboard JS caches the token from a <meta> tag at page load and has no
    round-trip to learn the new one; rotating there would 403 every second
    AJAX call. A full page reload re-runs the context processor and issues
    a fresh token naturally.
    """
    import secrets
    session["csrf_token"] = secrets.token_hex(32)
    session.modified = True


def _is_ajax_request() -> bool:
    """True for in-page API-style calls (do not rotate CSRF). Includes any request with
    ``X-CSRFToken`` (admin ``csrfFetch`` always sets it; ``fetch`` omits ``X-Requested-With``),
    jQuery's ``X-Requested-With: XMLHttpRequest``, and ``Accept: application/json``.
    """
    try:
        if (request.headers.get("X-CSRFToken") or "").strip():
            return True
        xrw = (request.headers.get("X-Requested-With") or "").lower()
        accept = (request.headers.get("Accept") or "").lower()
    except Exception:
        return False
    return xrw == "xmlhttprequest" or "application/json" in accept


def _validate_csrf_for_admin_post():
    """Return True if request has valid CSRF token (form, query, or X-CSRFToken header). Call only for admin POSTs."""
    # request.values merges form + query string (some clients/proxies behave oddly with form-only reads).
    token = (
        (request.values.get("csrf_token") or "").strip()
        or (request.headers.get("X-CSRFToken") or "").strip()
    )
    session_token = (session.get("csrf_token") or "").strip()
    if not token or not session_token:
        return False
    try:
        ok = bool(hmac.compare_digest(token, session_token))
    except (TypeError, ValueError):
        return False
    if ok and not _is_ajax_request():
        _rotate_csrf_token()
    return ok


def register_csrf(app, logger):
    """Register context processor and before_request CSRF guard."""

    @app.context_processor
    def inject_csrf_token():
        """Provide csrf_token, escort_name, and escort_timezone to all templates."""
        import config
        import secrets

        def _get_or_set_csrf():
            if "csrf_token" not in session:
                session["csrf_token"] = secrets.token_hex(32)
                session.modified = True
            return session.get("csrf_token", "")

        try:
            from core.settings_manager import get_escort_name

            escort_name = get_escort_name()
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e)
            escort_name = "escort"
        try:
            escort_timezone = config.get_effective_escort_timezone()
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e)
            escort_timezone = config.DEFAULT_TIMEZONE
        return dict(
            csrf_token=_get_or_set_csrf,
            escort_name=escort_name,
            escort_timezone=escort_timezone,
            # Single fallback for Jinja (never hardcode a city zone in templates)
            escort_default_iana=config.DEFAULT_TIMEZONE,
        )

    @app.before_request
    def _admin_csrf_protection():
        """Require valid CSRF token for authenticated admin POSTs to protected paths."""
        if request.method != "POST":
            return None
        if not (
            session.get("admin_authenticated")
            or session.get("database_authenticated")
            or session.get("config_authenticated")
            or session.get("pending_2fa")
            or session.get("health_authenticated")
        ):
            return None
        path = (request.path or "").strip()
        if not any(path.startswith(prefix) for prefix in _CSRF_PROTECTED_PREFIXES):
            return None
        if _validate_csrf_for_admin_post():
            return None
        logger.warning("Admin CSRF validation failed for %s", path)
        accept = (request.headers.get("Accept") or "").lower()
        xrw = (request.headers.get("X-Requested-With") or "").lower()
        has_csrf_header = bool((request.headers.get("X-CSRFToken") or "").strip())
        if (
            xrw == "xmlhttprequest"
            or "application/json" in accept
            or has_csrf_header
        ):
            return jsonify({"success": False, "error": "Invalid or missing CSRF token"}), 403
        flash(
            "Security check failed (invalid or missing CSRF token). Reload the page and submit again.",
            "error",
        )
        if path.startswith("/config"):
            return redirect(url_for("config.config_page"))
        if path.startswith("/admin/2fa") and session.get("pending_2fa"):
            return redirect(url_for("admin.two_factor_verify"))
        return redirect(url_for("admin.admin_dashboard"))
