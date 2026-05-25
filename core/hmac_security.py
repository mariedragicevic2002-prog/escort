"""
HMAC Link Security - signed, expiring, one-time-use tokens for public-facing URLs.

Features:
- Per-gateway key derivation (gateway isolation)
- Embedded expiry timestamps (default 1 hour)
- Database-backed one-time-use tracking
"""

import hashlib
import hmac
import logging
import os
import time

logger = logging.getLogger("adella_chatbot.hmac_security")

GATEWAY_FEEDBACK = "feedback"
GATEWAY_BOOKING_CONFIRM = "booking_confirm"
GATEWAY_DEPOSIT_CONFIRM = "deposit_confirm"

_TOKEN_TTL_SECONDS = 3600  # 1 hour (default for non-feedback gateways; feedback uses FEEDBACK_TOKEN_TTL_SECONDS)
# Escort post-booking feedback link: long enough to complete the webform; matches DB `requested_at` window.
FEEDBACK_TOKEN_TTL_SECONDS = 86400  # 24 hours
# Clients open /booking/confirmation/… from the deposit upload redirect days later; must outlive link_tokens cleanup.
BOOKING_CONFIRM_PAGE_TTL_SECONDS = 86400 * 365  # 1 year


def _get_master_key() -> bytes:
    key_material = os.environ.get("SECRET_KEY", "")
    if not key_material:
        try:
            from flask import current_app
            key_material = current_app.config.get("SECRET_KEY", "")
        except (ImportError, RuntimeError) as e:
            logger.warning("SECRET_KEY from Flask unavailable: %s", e, exc_info=True)
    if not key_material:
        try:
            from core.settings_manager import get_setting
            key_material = (get_setting("flask_secret_key") or "").strip()
        except Exception as e:
            logger.warning("flask_secret_key from DB unavailable: %s", e)
    if not key_material:
        logger.critical(
            "SECRET_KEY is not set — HMAC token signing is disabled. "
            "Set SECRET_KEY in the host environment or save flask_secret_key on the Config page."
        )
        raise RuntimeError(
            "Cannot sign HMAC tokens: SECRET_KEY is not configured. "
            "Set SECRET_KEY in the environment or flask_secret_key in admin settings."
        )
    return key_material.encode() if isinstance(key_material, str) else key_material


def _get_gateway_key(gateway: str) -> bytes:
    """Derive a per-gateway signing key from the master SECRET_KEY."""
    return hmac.new(_get_master_key(), gateway.encode(), hashlib.sha256).digest()


def generate_signed_token(value: str, gateway: str, ttl_seconds: int = _TOKEN_TTL_SECONDS) -> str:
    """Create a signed token: ``value:expires_unix:signature``.

    The signature covers both the value and expiry, preventing tampering with either.
    """
    expires = int(time.time()) + ttl_seconds
    payload = f"{value}:{expires}"
    key = _get_gateway_key(gateway)
    sig = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def verify_signed_token(token: str, expected_value: str, gateway: str) -> bool:
    """Verify signature, check expiry, and confirm embedded value matches ``expected_value``."""
    parts = token.split(":", 2)
    if len(parts) != 3:
        return False
    value, expires_str, sig = parts
    if value != expected_value:
        return False
    try:
        if int(expires_str) < int(time.time()):
            return False
    except (ValueError, TypeError):
        return False
    key = _get_gateway_key(gateway)
    expected_sig = hmac.new(key, f"{value}:{expires_str}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected_sig)


# ── Database-backed one-time-use tracking ──────────────────────────────────


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def register_token(db, token: str, gateway: str) -> bool:
    """Store a token hash in the DB. Returns True only if an unused row exists afterward."""
    try:
        db.execute_query(
            """INSERT INTO link_tokens (token_hash, gateway) VALUES (%s, %s)
               ON CONFLICT (token_hash) DO NOTHING""",
            (_token_hash(token), gateway),
            fetch=False,
        )
        return is_token_valid(db, token)
    except Exception as e:
        logger.warning("register_token failed: %s", e)
        return False


def consume_token(db, token: str) -> bool:
    """Mark a token as used (strict one-time use). Returns False if already consumed or missing."""
    try:
        result = db.execute_query(
            "UPDATE link_tokens SET used = TRUE WHERE token_hash = %s AND used = FALSE RETURNING id",
            (_token_hash(token),),
            fetch=True,
        )
        return bool(result)
    except Exception as e:
        logger.warning("consume_token failed: %s", e)
        return False


def is_token_valid(db, token: str) -> bool:
    """Check that the token exists in the DB and has NOT been consumed."""
    try:
        result = db.execute_query(
            "SELECT 1 FROM link_tokens WHERE token_hash = %s AND used = FALSE",
            (_token_hash(token),),
            fetch=True,
        )
        return bool(result)
    except Exception as e:
        logger.warning("is_token_valid failed: %s", e)
        return False


def cleanup_expired_tokens(db, max_age_hours: int = 24) -> int:
    """Delete token rows older than ``max_age_hours``. Returns rows deleted."""
    try:
        result = db.execute_query(
            "DELETE FROM link_tokens WHERE created_at < NOW() - INTERVAL %s RETURNING id",
            (f"{int(max_age_hours)} hours",),
            fetch=True,
        )
        count = len(result) if result else 0
        if count > 0:
            logger.info("Cleaned up %d expired link tokens", count)
        return count
    except Exception as e:
        logger.warning("cleanup_expired_tokens failed: %s", e)
        return 0
