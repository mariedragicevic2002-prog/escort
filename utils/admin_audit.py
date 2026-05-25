"""
Admin audit logging - record config/settings changes for accountability.
"""

import logging

logger = logging.getLogger("adella_chatbot.admin_audit")

# Keys we never log values for (secrets)
_SENSITIVE_KEYS = frozenset({
    "claude_api_key", "gemini_api_key",
    "admin_password", "admin_password_hash",
})


def log_admin_audit(action: str, details: str | None = None) -> None:
    """
    Append a row to admin_audit_log (e.g. setting_updated, config_saved).
    Does not log setting values for sensitive keys.
    """
    try:
        import config
        from services.database_service import get_shared_db
        db = get_shared_db(config.DATABASE_URL)
        if not db:
            return
        db.execute_query(
            "INSERT INTO admin_audit_log (action, details) VALUES (%s, %s)",
            (action, (details or "")[:500]),
            fetch=False,
        )
    except Exception as e:
        logger.warning("Audit log write failed: %s", e, exc_info=True)


def log_setting_updated(key: str) -> None:
    """Log that a setting was updated (key only; never log values for sensitive keys)."""
    if key in _SENSITIVE_KEYS:
        log_admin_audit("setting_updated", f"setting_key={key} (sensitive - value redacted)")
    else:
        log_admin_audit("setting_updated", f"setting_key={key}")