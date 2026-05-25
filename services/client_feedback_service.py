"""
Post-booking client feedback service.
Sends the escort an SMS 5 minutes after a booking has ended, asking for 3-question feedback (3-star style).
"""

import logging
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger("adella_chatbot.client_feedback_service")


def _row_to_booking_fields(row: dict[str, Any]) -> dict[str, Any]:
    """Convert DB row to booking_fields dict for template."""
    return {
        "date": row.get("date"),
        "time": row.get("time"),
        "duration": row.get("duration"),
        "experience_type": row.get("experience_type"),
        "incall_outcall": row.get("incall_outcall"),
        "outcall_address": row.get("outcall_address"),
        "client_name": row.get("client_name"),
    }


def _format_booking_summary_short(row: dict[str, Any]) -> str:
    """Format booking date, time, duration, experience, incall/outcall for the feedback request SMS."""
    parts = []
    date_val = row.get("date")
    if date_val:
        if hasattr(date_val, "strftime"):
            parts.append(date_val.strftime("%A %d %B %Y"))
        else:
            parts.append(str(date_val)[:10])
    time_val = row.get("time")
    if time_val is not None:
        if isinstance(time_val, (list, tuple)) and len(time_val) >= 2:
            h, m = int(time_val[0]), int(time_val[1])
        elif hasattr(time_val, "hour"):
            h, m = time_val.hour, time_val.minute
        else:
            h, m = 12, 0
        period = "am" if h < 12 else "pm"
        display_h = h if h <= 12 else h - 12
        if display_h == 0:
            display_h = 12
        parts.append(f"{display_h}:{m:02d}{period}")
    duration = row.get("duration")
    if duration is not None:
        if duration >= 60:
            hrs = duration // 60
            mins = duration % 60
            if mins:
                parts.append(f"{hrs}h {mins}min")
            else:
                parts.append(f"{hrs}h")
        else:
            parts.append(f"{duration} min")
    exp = (row.get("experience_type") or "GFE").strip() or "GFE"
    parts.append(exp.upper())
    loc = (row.get("incall_outcall") or "incall").strip().lower() or "incall"
    parts.append(loc)
    return " ".join(parts)


def _build_feedback_request_message(
    escort_name: str, client_name: str, booking_summary: str, feedback_link: str
) -> str:
    """Build the SMS template sent to the escort 5 mins after booking end (with webform link)."""
    client_display = (client_name or "Client").strip() or "Client"
    return (
        f"Hi {escort_name} please provide feedback for {client_display} ({booking_summary})\n\n"
        "To provide feedback please click the link below:\n"
        f"{feedback_link}"
    )


def check_and_send_feedback_requests(state_manager, db_service) -> int:
    """
    Find confirmed bookings whose end time + 5 minutes has passed and feedback not yet requested;
    send the feedback request SMS to the escort and mark feedback_request_sent.

    Called periodically by the background job (every 5 minutes).

    Args:
        state_manager: State manager instance
        db_service: Database service instance

    Returns:
        Number of feedback request SMS sent
    """
    try:
        from core.settings_manager import get_setting
    except ImportError:
        return 0
    enabled_str = (get_setting("client_feedback_enabled") or "true").strip().lower()
    if enabled_str in ("false", "0", "no"):
        return 0

    try:
        import pytz

        from config import get_effective_escort_timezone, get_escort_phone_number
    except ImportError:
        logger.warning("config/pytz not available for client feedback service")
        return 0

    ESCORT_PHONE_NUMBER = get_escort_phone_number()
    if not ESCORT_PHONE_NUMBER:
        logger.warning(
            "Post-booking client feedback: escort phone not configured — cannot send rating SMS"
        )
        return 0

    query = """
        SELECT phone_number, date, time, duration, experience_type,
               incall_outcall, outcall_address, client_name
        FROM conversation_states
        WHERE confirmed_at IS NOT NULL
          AND date IS NOT NULL AND time IS NOT NULL AND duration IS NOT NULL
          AND (feedback_request_sent IS NULL OR feedback_request_sent = FALSE)
          AND current_state IN ('CONFIRMED', 'POST_BOOKING')
          AND (booking_status IS NULL
               OR LOWER(TRIM(booking_status::text)) NOT IN ('cancelled', 'canceled', 'no_show', 'noshow'))
    """
    results = db_service.execute_query(query, (), fetch=True)
    if not results:
        return 0

    tz = pytz.timezone(get_effective_escort_timezone())
    now = datetime.now(tz)
    sent = 0

    for row in results:
        phone_number = row.get("phone_number")
        if not phone_number:
            continue

        date_val = row.get("date")
        time_val = row.get("time")
        duration_min = int(row.get("duration") or 0)

        if not date_val or time_val is None:
            continue

        try:
            if hasattr(date_val, "date"):
                booking_date = date_val.date() if hasattr(date_val, "date") else date_val
            else:
                booking_date = datetime.strptime(str(date_val)[:10], "%Y-%m-%d").date()

            if isinstance(time_val, (list, tuple)) and len(time_val) >= 2:
                hour, minute = int(time_val[0]), int(time_val[1])
            elif hasattr(time_val, "hour"):
                hour, minute = time_val.hour, time_val.minute
            else:
                continue

            booking_start = tz.localize(datetime.combine(booking_date, datetime.min.time().replace(hour=hour, minute=minute)))
            booking_end = booking_start + timedelta(minutes=duration_min)
            feedback_due = booking_end + timedelta(minutes=5)

            if now < feedback_due:
                continue

            escort_name = "escort"
            try:
                from config import get_escort_name
                escort_name = get_escort_name() or escort_name
            except Exception as e:
                logger.warning("get_escort_name failed for feedback message: %s", e)

            base_url = ""
            try:
                from config import get_base_url
                base_url = (get_base_url() or "").rstrip("/")
            except Exception as e:
                logger.warning("get_base_url failed for feedback link: %s", e)
            if not base_url or base_url == "https://yourdomain.com":
                base_url = "https://yourdomain.com"

            pending_id = _set_feedback_pending(db_service, phone_number)
            if pending_id is None:
                continue

            from core.hmac_security import (
                FEEDBACK_TOKEN_TTL_SECONDS,
                GATEWAY_FEEDBACK,
                generate_signed_token,
                register_token,
            )
            tok = generate_signed_token(
                str(pending_id), GATEWAY_FEEDBACK, ttl_seconds=FEEDBACK_TOKEN_TTL_SECONDS
            )
            register_token(db_service, tok, GATEWAY_FEEDBACK)
            feedback_link = f"{base_url}/feedback?pending_id={pending_id}&tok={tok}"

            client_name = (row.get("client_name") or "Client").strip() or "Client"
            booking_summary = _format_booking_summary_short(row)
            message = _build_feedback_request_message(escort_name, client_name, booking_summary, feedback_link)

            from services.sms_service import send_escort_sms
            if send_escort_sms(ESCORT_PHONE_NUMBER, message, category='client_rating'):
                state_manager.update_fields(phone_number, {"feedback_request_sent": True})
                sent += 1
                logger.info("Sent post-booking feedback request to escort for client %s", phone_number)
            else:
                logger.warning("Failed to send feedback request SMS for %s", phone_number)
        except Exception as e:
            logger.warning("Error processing feedback request for %s: %s", phone_number, e)

    if sent > 0:
        logger.info("Sent %d post-booking feedback request(s)", sent)
    return sent


def _set_feedback_pending(db_service, client_phone_number: str):
    """
    Set or update feedback_pending to this client (single row: we overwrite).
    Returns the new feedback_pending id for use in the webform link, or None on failure.
    """
    try:
        db_service.execute_query(
            "DELETE FROM feedback_pending WHERE client_phone_number = %s",
            (client_phone_number,),
            fetch=False
        )
        row = db_service.execute_query(
            """INSERT INTO feedback_pending (client_phone_number, requested_at)
               VALUES (%s, CURRENT_TIMESTAMP) RETURNING id""",
            (client_phone_number,),
            fetch=False
        )
        if isinstance(row, int):
            return row
        if isinstance(row, dict):
            return row.get("id")
        return None
    except Exception as e:
        logger.warning("Failed to set feedback_pending: %s", e)
        return None
