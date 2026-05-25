"""
httpSMS Service
Sends SMS via the httpSMS gateway (https://httpsms.com).
Uses an Android phone as the SMS modem — no carrier fees for outbound.
"""

import atexit
import logging
import threading
import time

logger = logging.getLogger(__name__)

from utils.api_resilience import HTTPSMS_HTTP_TIMEOUT_SECONDS, call_with_retry_httpsms
from utils.circuit_breaker import CircuitBreakerOpenError, get_circuit_breaker

try:
    import requests as _requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False
    logger.warning("requests package not installed — httpSMS disabled")

_HTTPSMS_API_URL = "https://api.httpsms.com/v1/messages/send"

_last_error: dict | None = None
_error_lock = threading.Lock()

# Thread-local Session: reuse TCP/TLS to api.httpsms.com (faster than one-off POST per SMS).
_tls = threading.local()


_all_sessions: list[_requests.Session] = []
_sessions_lock = threading.Lock()


def _cleanup_sessions():
    """Close all thread-local sessions on process exit."""
    with _sessions_lock:
        for s in _all_sessions:
            try:
                s.close()
            except Exception as e:
                logger.warning("Session close on exit failed: %s", e)
        _all_sessions.clear()


atexit.register(_cleanup_sessions)


def _http_session():
    """Return a requests.Session for this worker thread (thread-safe pattern for Flask/uWSGI)."""
    s = getattr(_tls, "session", None)
    if s is not None:
        return s
    s = _requests.Session()
    try:
        from requests.adapters import HTTPAdapter

        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0)
        s.mount("https://", adapter)
    except Exception as e:
        logger.warning("HTTPAdapter setup failed (session still usable): %s", e)
    _tls.session = s
    with _sessions_lock:
        _all_sessions.append(s)
    return s


def _set_last_error(error: dict | None) -> None:
    global _last_error
    with _error_lock:
        _last_error = dict(error) if isinstance(error, dict) else None


def get_last_sms_error() -> dict | None:
    with _error_lock:
        return dict(_last_error) if isinstance(_last_error, dict) else None


def _get_config():
    """Return (api_key, from_number) from admin settings / env."""
    try:
        from config import get_httpsms_api_key, get_httpsms_phone_number
        return get_httpsms_api_key(), get_httpsms_phone_number()
    except Exception as e:
        logger.error("httpSMS config error: %s", e)
        return None, None


def is_configured() -> bool:
    """True if both API key and phone number are set."""
    api_key, phone = _get_config()
    return bool(api_key and phone)


# True when the requests library is available
HAS_SMS = HAS_REQUESTS


def send_sms(to: str, message: str, max_retries: int = 3) -> bool:
    """Send an SMS via httpSMS with simple retry on server errors.

    Returns True on success, False otherwise.
    """
    if not HAS_REQUESTS:
        logger.error("requests not installed — cannot send via httpSMS")
        return False

    api_key, from_number = _get_config()
    if not api_key or not from_number:
        missing = []
        if not api_key:
            missing.append("API Key")
        if not from_number:
            missing.append("Phone Number")
        logger.error(
            "httpSMS not configured — cannot send SMS. Missing: %s. "
            "Save on the Config page.",
            ", ".join(missing),
        )
        _set_last_error({
            "type": "config",
            "status": None,
            "auth_error": False,
            "message": "httpSMS not configured — missing: " + ", ".join(missing),
        })
        return False

    if not to or not message:
        logger.error("httpSMS send_sms: missing 'to' or 'message'")
        _set_last_error({"type": "validation", "status": None, "auth_error": False, "message": "Missing to/message"})
        return False

    payload = {"from": from_number, "to": to, "content": message}
    headers = {"x-api-key": api_key, "Content-Type": "application/json", "Accept": "application/json"}

    cb = get_circuit_breaker(
        "httpsms_send",
        failure_threshold=5,
        recovery_timeout=60.0,
        expected_exception=Exception,
    )

    def _send_with_transport_retries() -> bool:
        """POST with tenacity (connection/timeouts) + 5xx backoff."""
        for attempt in range(max_retries):
            try:
                resp = call_with_retry_httpsms(
                    lambda: _http_session().post(
                        _HTTPSMS_API_URL,
                        json=payload,
                        headers=headers,
                        timeout=HTTPSMS_HTTP_TIMEOUT_SECONDS,
                    )
                )
            except Exception as e:
                logger.error("httpSMS unexpected error to %s: %s: %s", to, type(e).__name__, e)
                _set_last_error({
                    "type": type(e).__name__,
                    "status": None,
                    "auth_error": False,
                    "message": str(e),
                })
                raise

            if resp.status_code in (200, 201, 202):
                logger.info("httpSMS sent to %s | status=%s", to, resp.status_code)
                _set_last_error(None)
                return True

            if resp.status_code >= 500 and attempt < max_retries - 1:
                wait = 2**attempt
                logger.warning(
                    "httpSMS 5xx (%s), retrying in %ss (attempt %d/%d)",
                    resp.status_code,
                    wait,
                    attempt + 1,
                    max_retries,
                )
                time.sleep(wait)
                continue

            logger.error("httpSMS failed to %s: HTTP %s — %s", to, resp.status_code, resp.text[:200])
            _set_last_error({
                "type": "http_error",
                "status": resp.status_code,
                "auth_error": resp.status_code == 401,
                "message": resp.text[:200],
            })
            raise RuntimeError(f"httpSMS HTTP {resp.status_code}")

        raise RuntimeError(f"httpSMS exhausted after {max_retries} attempts")

    try:
        return cb.call(_send_with_transport_retries)
    except CircuitBreakerOpenError:
        logger.warning("httpSMS circuit open — skipping API call")
        _set_last_error({
            "type": "circuit_open",
            "status": None,
            "auth_error": False,
            "message": "httpSMS circuit breaker open — try again later",
        })
        return False
    except Exception as e:
        logger.error("httpSMS failed to %s: %s", to, e)
        _set_last_error({
            "type": "retry_exhausted",
            "status": None,
            "auth_error": False,
            "message": str(e),
        })
        return False


def send_escort_sms(to: str, message: str, category: str = "") -> bool:
    """send_sms wrapper that honours escort_sms_enabled and per-category toggles."""
    try:
        from core.settings_manager import get_setting
        if (get_setting("escort_sms_enabled") or "true").strip().lower() in ("false", "0", "no"):
            logger.info("Escort SMS suppressed (escort_sms_enabled=off)")
            return False
        if category:
            cat_key = f"escort_sms_{category}"
            raw = get_setting(cat_key)
            if (raw or "true").strip().lower() in ("false", "0", "no"):
                logger.info("Escort SMS suppressed (%s=off)", cat_key)
                return False
    except Exception as e:
        logger.warning("Escort SMS toggle check failed: %s", e)
    return send_sms(to, message)


def send_sms_bulk(recipients: list, message: str) -> dict:
    """Send SMS to multiple recipients. Returns sent/failed counts."""
    results = {"sent": 0, "failed": 0, "failed_numbers": []}
    for phone_number in recipients:
        if send_sms(phone_number, message):
            results["sent"] += 1
        else:
            results["failed"] += 1
            results["failed_numbers"].append(phone_number)
    logger.info("httpSMS bulk: sent=%d, failed=%d", results["sent"], results["failed"])
    return results
