# ruff: noqa: E402
"""
SMS service — outbound via httpSMS (https://httpsms.com).

All application code should import from here.
"""

import logging
import threading

from services import httpsms_service

logger = logging.getLogger(__name__)

_last_error: dict | None = None
_error_lock = threading.Lock()


def _set_last_sms_error(error: dict | None) -> None:
    global _last_error
    with _error_lock:
        _last_error = dict(error) if isinstance(error, dict) else None


def get_last_sms_error() -> dict | None:
    with _error_lock:
        return dict(_last_error) if isinstance(_last_error, dict) else None


def _phone_tail_for_metrics(phone: str) -> str:
    digits = "".join(c for c in (phone or "") if c.isdigit())
    if len(digits) >= 4:
        return digits[-4:]
    return "****"


def _provider_unavailable_error(provider: str, reason: str) -> dict:
    return {
        "provider": provider,
        "type": "provider_unavailable",
        "status": None,
        "auth_error": False,
        "message": reason,
    }


def get_gateway_status() -> dict[str, object]:
    """Current httpSMS gateway availability status."""
    from config import httpsms_is_enabled

    hs_enabled = bool(httpsms_is_enabled())
    hs_configured = bool(httpsms_service.is_configured())

    return {
        "provider": "httpsms",
        "httpsms": {
            "enabled": hs_enabled,
            "configured": hs_configured,
            "active": bool(hs_enabled and hs_configured),
        },
    }


def is_configured() -> bool:
    """True when httpSMS gateway is configured and active."""
    gw = get_gateway_status()
    return bool((gw.get("httpsms") or {}).get("active"))


HAS_SMS = httpsms_service.HAS_SMS


def send_sms(to: str, message: str, max_retries: int = 3) -> bool:
    """Send SMS via httpSMS."""
    from config import httpsms_is_enabled

    if not httpsms_is_enabled():
        err = _provider_unavailable_error("httpsms", "httpSMS gateway is disabled")
        _set_last_sms_error(err)
        logger.error("SMS send failed (gateway disabled): to=%s", _phone_tail_for_metrics(to))
        return False

    if not httpsms_service.is_configured():
        err = _provider_unavailable_error("httpsms", "httpSMS not configured — set API key and phone number in Config")
        _set_last_sms_error(err)
        logger.error("SMS send failed (not configured): to=%s", _phone_tail_for_metrics(to))
        return False

    if httpsms_service.send_sms(to, message, max_retries=max_retries):
        _set_last_sms_error(None)
        return True

    # Propagate the detailed error from the httpsms service layer
    provider_err = httpsms_service.get_last_sms_error() or _provider_unavailable_error(
        "httpsms", "httpSMS send_sms returned False"
    )
    provider_err.setdefault("provider", "httpsms")
    _set_last_sms_error(provider_err)
    try:
        from utils.structured_logging import log_quality_metric

        log_quality_metric(
            "sms_send_failed",
            phone_tail=_phone_tail_for_metrics(to),
            primary_provider="httpsms",
            error_type="send_failed",
            auth_error=provider_err.get("auth_error", False),
        )
    except Exception as e:
        logger.warning("sms_send_failed metric skipped: %s", e)
    logger.error(
        "SMS send failed (httpSMS): to=%s",
        _phone_tail_for_metrics(to),
    )
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
    logger.info("SMS bulk: sent=%d, failed=%d", results["sent"], results["failed"])
    return results


__all__ = [
    "send_sms",
    "send_escort_sms",
    "send_sms_bulk",
    "get_last_sms_error",
    "get_gateway_status",
    "is_configured",
    "HAS_SMS",
]
