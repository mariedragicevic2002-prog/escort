"""
Deposit follow-up service.

Sends the escort an SMS reminder when a booking has been waiting for a deposit
for more than HOURS_THRESHOLD hours with no payment received. Runs via the
5-minute check_reminders_job scheduler.

Admin toggle key: escort_sms_deposit_followup
"""

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("adella_chatbot.deposit_followup")

# How long a DEPOSIT_REQUIRED booking sits unpaid before we send a reminder
HOURS_THRESHOLD = 4


def check_and_send_deposit_followups(state_manager, db_service) -> int:
    """
    Check for bookings stuck in DEPOSIT_REQUIRED state and send the escort a
    nudge SMS if no deposit has been received within HOURS_THRESHOLD hours.

    Returns the number of reminders sent.
    """
    from config import get_setting

    # Respect admin toggle (default ON)
    toggle = (get_setting("escort_sms_deposit_followup") or "true").lower()
    if toggle not in ("true", "1", "yes"):
        return 0

    try:
        from config import get_escort_phone_number
        from services.sms_service import send_sms

        escort_phone = get_escort_phone_number()
        if not escort_phone:
            logger.warning("deposit_followup: no escort phone configured")
            return 0

        cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_THRESHOLD)

        rows = db_service.execute_query(
            """
            SELECT phone_number, client_name, date, time,
                   deposit_requested_at, deposit_followup_sent_at
            FROM conversation_states
            WHERE current_state = 'DEPOSIT_REQUIRED'
              AND deposit_followup_sent_at IS NULL
              AND deposit_paid = FALSE
              AND deposit_requested_at IS NOT NULL
              AND deposit_requested_at <= %(cutoff)s
            ORDER BY deposit_requested_at ASC
            LIMIT 20
            """,
            {"cutoff": cutoff},
            fetch=True,
        ) or []

        sent = 0
        for row in rows:
            phone = row.get("phone_number", "unknown")
            name = row.get("client_name") or "Unknown client"
            date = row.get("date") or "unspecified date"
            time = row.get("time") or "unspecified time"

            msg = (
                f"⚠️ Deposit reminder: {name} ({phone}) has NOT yet paid their deposit "
                f"for the booking on {date} at {time}. "
                f"Consider following up directly."
            )

            try:
                send_sms(escort_phone, msg)
                db_service.execute_query(
                    """
                    UPDATE conversation_states
                    SET deposit_followup_sent_at = NOW()
                    WHERE phone_number = %(phone)s
                      AND current_state = 'DEPOSIT_REQUIRED'
                    """,
                    {"phone": phone},
                )
                sent += 1
            except Exception as exc:
                logger.error("deposit_followup: failed to send for %s: %s", phone, exc)

        return sent

    except Exception as exc:
        logger.error("deposit_followup: unexpected error: %s", exc, exc_info=True)
        return 0
