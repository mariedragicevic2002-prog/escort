"""
Pre-booking check-in SMS service.

Sends the escort an SMS ~2 hours before a confirmed booking starts as a
heads-up so she can prepare. Runs via the 5-minute check_reminders_job.

Piggybacks on the existing `reminder_2h_scheduled` column in conversation_states
so no extra datetime parsing is needed — when `reminder_2h_scheduled` is within
the next ~30 minutes and hasn't been actioned yet, the booking is ~2 hours away.

Admin toggle key: escort_sms_prebooking_checkin
"""

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("adella_chatbot.checkin_service")

# Send escort check-in when reminder_2h_scheduled falls within this window from now
WINDOW_MINUTES_AHEAD = 30  # look up to 30 minutes ahead of the 2h mark


def _build_checkin_message(row: dict) -> str:
    """Format the pre-booking check-in SMS for the escort."""
    phone = row.get("phone_number", "unknown")
    name = row.get("client_name") or "Client"
    date = row.get("date") or "today"
    time = row.get("time") or "soon"
    duration = row.get("duration") or ""
    btype = (row.get("incall_outcall") or "").title()
    experience = row.get("experience_type") or ""
    address = row.get("outcall_address") or ""
    details_parts = [p for p in [btype, experience, f"{duration}min" if duration else ""] if p]
    details = " | ".join(details_parts) if details_parts else ""
    return (
        f"⏰ Booking in ~2hrs: {name} ({phone})\n"
        f"📅 {date} at {time}"
        + (f"\n📋 {details}" if details else "")
        + (f"\n📍 {address}" if address else "")
    )


def _send_checkin_sms_for_row(row: dict, escort_phone: str, db_service) -> bool:
    """Send a pre-booking check-in SMS for one booking row. Returns True on success."""
    from services.sms_service import send_sms
    phone = row.get("phone_number", "unknown")
    try:
        send_sms(escort_phone, _build_checkin_message(row))
        db_service.execute_query(
            """
            UPDATE conversation_states
            SET checkin_sms_sent_at = NOW()
            WHERE phone_number = %(phone)s
              AND current_state = 'CONFIRMED'
            """,
            {"phone": phone},
        )
        return True
    except Exception as exc:
        logger.error("checkin_service: failed to send for %s: %s", phone, exc)
        return False


def check_and_send_prebooking_checkins(state_manager, db_service) -> int:
    """
    Find confirmed bookings whose 2h-reminder mark is imminent (within the next
    WINDOW_MINUTES_AHEAD minutes) and send the escort a pre-booking check-in SMS.

    Returns the number of check-ins sent.
    """
    from config import get_setting

    # Respect admin toggle (default ON)
    toggle = (get_setting("escort_sms_prebooking_checkin") or "true").lower()
    if toggle not in ("true", "1", "yes"):
        return 0

    try:
        from config import get_escort_phone_number
        from services.sms_service import send_sms

        escort_phone = get_escort_phone_number()
        if not escort_phone:
            logger.warning("checkin_service: no escort phone configured")
            return 0

        now_utc = datetime.now(timezone.utc)
        window_end = now_utc + timedelta(minutes=WINDOW_MINUTES_AHEAD)

        rows = db_service.execute_query(
            """
            SELECT phone_number, client_name, date, time,
                   duration, experience_type, incall_outcall, outcall_address,
                   checkin_sms_sent_at, reminder_2h_scheduled
            FROM conversation_states
            WHERE current_state = 'CONFIRMED'
              AND checkin_sms_sent_at IS NULL
              AND reminder_2h_scheduled IS NOT NULL
              AND reminder_2h_scheduled <= %(window_end)s
              AND reminder_2h_scheduled >= %(now_utc)s
            ORDER BY reminder_2h_scheduled ASC
            LIMIT 20
            """,
            {"now_utc": now_utc, "window_end": window_end},
            fetch=True,
        ) or []

        sent = 0
        for row in rows:
            if _send_checkin_sms_for_row(row, escort_phone, db_service):
                sent += 1
        return sent

    except Exception as exc:
        logger.error("checkin_service: unexpected error: %s", exc, exc_info=True)
        return 0
