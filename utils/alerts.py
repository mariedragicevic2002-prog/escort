"""
Structured alerts - log key failures to DB for admin visibility.
"""

import logging

logger = logging.getLogger("adella_chatbot.alerts")


def log_alert(component: str, message: str, severity: str = "warning") -> None:
    """
    Write an alert row (e.g. deposit_upload_failed, ai_error, sms_error).
    Severity: info, warning, error.
    """
    try:
        import config
        from services.database_service import get_shared_db
        db = get_shared_db(config.DATABASE_URL)
        if not db:
            return
        db.execute_query(
            "INSERT INTO alerts (component, message, severity) VALUES (%s, %s, %s)",
            (component[:50], (message or "")[:1000], severity[:20]),
            fetch=False,
        )
    except Exception as e:
        logger.warning("Alert write failed: %s", e, exc_info=True)