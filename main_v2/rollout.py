"""Staged chatbot rollout (percentage + structured path alerts)."""

from __future__ import annotations

from utils.log_sanitize import LOG_SUPPRESSED_FMT

import hashlib
import hmac
import logging

from utils.structured_logging import get_logger

structured_logger = get_logger("escort_chatbot.main")
logger = logging.getLogger("escort_chatbot.main")


def get_chatbot_rollout_percent() -> int:
    """Read staged-rollout percentage (0-100) from admin_settings.

    Fails CLOSED: default 0 when unset or when the settings read raises. An
    admin must explicitly set `chatbot_rollout_percent` to enable routing.
    """
    try:
        from core.settings_manager import get_setting as _gs

        raw = _gs("chatbot_rollout_percent", "")
        if raw is not None and str(raw).strip() != "":
            return max(0, min(100, int(str(raw).strip())))
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
    return 0


def rollout_alerts_enabled() -> bool:
    """Whether rollout path alerts are enabled (admin_settings; default on when unset)."""
    try:
        from core.settings_manager import get_setting as _gs

        raw = (_gs("rollout_path_alerts_enabled", "") or "").strip().lower()
        if not raw:
            return True
        return raw in ("true", "1", "yes", "on")
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        return True


def is_phone_in_rollout_bucket(phone_number: str, rollout_percent: int) -> tuple[bool, int]:
    """
    Determine whether a phone number falls in the active rollout bucket.
    Uses HMAC-SHA256 keyed with the app SECRET_KEY so the bucket assignment
    is not guessable from the phone number alone (H12).
    Falls back to plain SHA256 when SECRET_KEY is unavailable so rollout
    still works during early startup / tests.
    """
    seed = (phone_number or "").strip()
    if not seed:
        return True, 0
    try:
        secret = (
            __import__("os").environ.get("SECRET_KEY", "")
            or ""
        ).encode("utf-8") or b"rollout-default"
        digest = hmac.new(secret, seed.encode("utf-8"), hashlib.sha256).hexdigest()
    except Exception:
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) % 100
    return bucket < rollout_percent, bucket


def log_rollout_path_alert(
    *,
    path: str,
    phone_number: str,
    request_id: str,
    intent: str = "",
    state: str = "",
    new_state: str = "",
) -> None:
    if not rollout_alerts_enabled():
        return
    structured_logger.warning(
        "rollout_path_alert",
        path=path,
        phone_number=phone_number,
        intent=intent,
        state=state,
        new_state=new_state,
        request_id=request_id,
    )
