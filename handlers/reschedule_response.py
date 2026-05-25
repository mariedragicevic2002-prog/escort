"""
Handle client responses to reschedule requests (YES / CANCEL) and forwarding when awaiting refund details.

Note on identifiers: `pending_reschedules.event_id` is the `bookings.id` integer that
`admin/blueprints/schedule/page_routes.py::_handle_reschedule` inserted there.
It is NOT a Google Calendar event id, so all DB lookups use `bookings.id`.
"""

import logging
from typing import Any

from utils.log_sanitize import LOG_SUPPRESSED_FMT, sanitize_log_value

logger = logging.getLogger("adella_chatbot.reschedule_response")


def get_pending_reschedule(phone_number: str, db) -> dict[str, Any] | None:
    """Return the latest unconfirmed pending reschedule for this phone, or None."""
    try:
        row = db.execute_query(
            """SELECT id, event_id, phone_number, original_time, new_date, new_time
               FROM pending_reschedules
               WHERE phone_number = %s AND (confirmed IS NULL OR confirmed = FALSE)
               ORDER BY requested_at DESC LIMIT 1""",
            (phone_number,),
            fetch=True
        )
        if row and len(row) > 0:
            from utils.row_utils import row_get
            if isinstance(row[0], dict):
                return dict(row[0])
            return {
                'id': row_get(row[0], 0),
                'event_id': row_get(row[0], 1),
                'phone_number': row_get(row[0], 2),
                'original_time': row_get(row[0], 3),
                'new_date': row_get(row[0], 4),
                'new_time': row_get(row[0], 5),
            }
    except Exception as e:
        logger.warning("get_pending_reschedule failed: %s", e)
    return None


def _format_reschedule_datetime_for_sms(start_time) -> str:
    """Return a friendly local-tz datetime string for SMS messages."""
    try:
        from admin.blueprints.schedule.helpers import _format_reschedule_datetime, _get_local_timezone
        tz = _get_local_timezone()
        local_dt = start_time.astimezone(tz) if getattr(start_time, "tzinfo", None) else tz.localize(start_time)
        return _format_reschedule_datetime(local_dt, comma_after_weekday=True, space_before_am_pm=True)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        try:
            return start_time.strftime("%A, %d/%m/%Y %I:%M %p")
        except Exception:
            return str(start_time or "")


def handle_reschedule_confirm(phone_number: str, db, get_calendar_service) -> list[str]:
    """
    Client replied YES to the reschedule request.

    Flip the booking row from 'pending' → 'reschedule-confirmed' in the DB so it
    appears as a live booking again at the new start_time/end_time. Mark the
    pending_reschedules row confirmed. Reply with the new date/time details.
    """
    pending = get_pending_reschedule(phone_number, db)
    if not pending:
        return []

    booking_id = pending.get("event_id")
    if not booking_id:
        return []

    try:
        from utils.row_utils import row_get

        rows = db.execute_query(
            "SELECT id, client_name, start_time, end_time FROM bookings WHERE id = %s",
            (booking_id,),
            fetch=True,
        ) or []
        if not rows:
            logger.warning(
                "Reschedule confirm: booking %s not found for %s",
                sanitize_log_value(str(booking_id)),
                sanitize_log_value(phone_number),
            )
            return ["Sorry, I couldn't find the booking to reschedule. Please text me to rebook."]

        booking_row = rows[0]
        client_name = (row_get(booking_row, "client_name") or "there").strip()
        start_time = row_get(booking_row, "start_time")
        new_time_label = _format_reschedule_datetime_for_sms(start_time)

        # Move booking back to confirmed status at the new time stamped in by _handle_reschedule.
        db.execute_query(
            "UPDATE bookings SET status = 'reschedule-confirmed', updated_at = NOW() WHERE id = %s",
            (booking_id,),
            fetch=False,
        )

        db.execute_query(
            "UPDATE pending_reschedules SET confirmed = TRUE, confirmed_at = NOW() WHERE id = %s",
            (pending["id"],),
            fetch=False,
        )

        # Best-effort Google Calendar update: only attempt if a calendar event id exists on
        # conversation_states. Failure here must not abort the DB-side confirmation.
        try:
            import config
            service = get_calendar_service() if get_calendar_service else None
            cal_event_id = None
            cs_rows = db.execute_query(
                "SELECT peacock_event_id, confirmed_event_id FROM conversation_states WHERE phone_number = %s",
                (phone_number,),
                fetch=True,
            ) or []
            if cs_rows:
                cal_event_id = (
                    row_get(cs_rows[0], "confirmed_event_id")
                    or row_get(cs_rows[0], "peacock_event_id")
                )
            if service and cal_event_id:
                try:
                    event = service.events().get(
                        calendarId=config.get_google_calendar_id(),
                        eventId=cal_event_id,
                    ).execute()
                    summary = event.get("summary", "")
                    if summary.startswith("PENDING RESCHEDULE - "):
                        new_summary = "RESCHEDULE CONFIRMED - " + summary.replace("PENDING RESCHEDULE - ", "", 1)
                    else:
                        new_summary = f"RESCHEDULE CONFIRMED - {summary}"
                    service.events().patch(
                        calendarId=config.get_google_calendar_id(),
                        eventId=cal_event_id,
                        body={"colorId": getattr(config, "COLOR_BASIL", "10"), "summary": new_summary},
                    ).execute()
                except Exception as e:
                    logger.warning("Calendar patch on reschedule confirm skipped: %s", e)
        except Exception as e:
            logger.warning("Calendar service unavailable for reschedule confirm: %s", e)

        logger.info(
            "Reschedule confirmed for %s booking %s",
            sanitize_log_value(phone_number),
            sanitize_log_value(str(booking_id)),
        )

        confirm_msg = (
            f"Thanks {client_name}! Your booking has been rescheduled to {new_time_label}. "
            "Looking forward to seeing you. ❤️"
        )
        return [confirm_msg]

    except Exception as e:
        logger.exception("handle_reschedule_confirm failed: %s", e)
        return ["Sorry, I couldn't confirm the reschedule just now. Please text me again or fill in my booking webform."]


def handle_reschedule_cancel(
    phone_number: str,
    db,
    get_calendar_service,
    state_manager,
) -> tuple[list[str], bool]:
    """
    Client replied CANCEL to reschedule.

    Delete the booking row from the bookings table, delete any travel blocks,
    reset conversation state to NEW, mark pending_reschedules confirmed, and
    send the client a cancellation message. If a deposit was paid, ask the
    client for refund banking details and set awaiting_refund_details.
    """
    pending = get_pending_reschedule(phone_number, db)
    if not pending:
        return [], False

    booking_id = pending.get("event_id")
    if not booking_id:
        return [], False

    try:
        from utils.row_utils import row_get
        from config import get_escort_name

        rows = db.execute_query(
            "SELECT id, client_name, deposit_status, deposit_amount FROM bookings WHERE id = %s",
            (booking_id,),
            fetch=True,
        ) or []
        if rows:
            booking_row = rows[0]
            client_name = (row_get(booking_row, "client_name") or "there").strip()
            dep_status = str(row_get(booking_row, "deposit_status") or "").lower()
            try:
                deposit_amount = int(float(row_get(booking_row, "deposit_amount") or 0))
            except (TypeError, ValueError):
                deposit_amount = 0
            deposit_paid = dep_status == "paid" and deposit_amount > 0
        else:
            # Fall back to conversation_states if the booking row already disappeared.
            cs_rows = db.execute_query(
                "SELECT client_name, deposit_paid, deposit_amount FROM conversation_states WHERE phone_number = %s",
                (phone_number,),
                fetch=True,
            ) or []
            client_name = (row_get(cs_rows[0], "client_name") if cs_rows else "") or "there"
            deposit_paid = bool(cs_rows and row_get(cs_rows[0], "deposit_paid", False))
            try:
                deposit_amount = int(float(row_get(cs_rows[0], "deposit_amount") or 0)) if cs_rows else 0
            except (TypeError, ValueError):
                deposit_amount = 0

        escort_name = get_escort_name()

        # Best-effort travel block cleanup. Travel block IDs live on conversation_states.
        try:
            cs_rows = db.execute_query(
                "SELECT travel_outbound_event_id, travel_return_event_id FROM conversation_states WHERE phone_number = %s",
                (phone_number,),
                fetch=True,
            ) or []
            if cs_rows:
                outbound_id = row_get(cs_rows[0], "travel_outbound_event_id")
                return_id = row_get(cs_rows[0], "travel_return_event_id")
                for tb_id in (outbound_id, return_id):
                    if tb_id:
                        try:
                            db.execute_query(
                                "DELETE FROM bookings WHERE id = %s AND type = 'travel'",
                                (tb_id,),
                                fetch=False,
                            )
                        except Exception as e:
                            logger.warning("Travel block delete failed for %s: %s", tb_id, e)
        except Exception as e:
            logger.warning("Travel block cleanup skipped: %s", e)

        # Delete the booking itself from the DB so the slot frees up.
        db.execute_query(
            "DELETE FROM bookings WHERE id = %s",
            (booking_id,),
            fetch=False,
        )

        # Reset the conversation state to NEW; flag awaiting_refund_details if a deposit was paid.
        db.execute_query(
            """UPDATE conversation_states
               SET current_state = 'NEW', date = NULL, time = NULL, duration = NULL,
                   experience_type = NULL, incall_outcall = NULL, outcall_address = NULL,
                   peacock_event_id = NULL, confirmed_event_id = NULL,
                   travel_outbound_event_id = NULL, travel_return_event_id = NULL,
                   confirmed_at = NULL, first_contact_sent = FALSE,
                   missing_fields = '["date","time","duration"]',
                   awaiting_refund_details = %s
               WHERE phone_number = %s""",
            (bool(deposit_paid), phone_number),
            fetch=False,
        )

        db.execute_query(
            "UPDATE pending_reschedules SET confirmed = TRUE, confirmed_at = NOW() WHERE id = %s",
            (pending["id"],),
            fetch=False,
        )

        if deposit_paid and deposit_amount:
            msg = (
                f"Hi {client_name} ❌ Your booking has now been cancelled as requested. "
                f"Please respond with your banking details so your deposit of ${deposit_amount} can be refunded back to you. "
                "If you wish to make a booking in the future please text me a new enquiry. "
                f"Hope to see you soon {escort_name}"
            )
        else:
            msg = (
                f"Hi {client_name} ❌ Your booking has now been cancelled as requested. "
                "If you wish to make a booking in the future please text me a new enquiry. "
                f"Hope to see you soon {escort_name}"
            )

        logger.info(
            "Reschedule cancelled for %s booking %s (deposit_paid=%s)",
            sanitize_log_value(phone_number),
            sanitize_log_value(str(booking_id)),
            deposit_paid,
        )
        return [msg], True

    except Exception as e:
        logger.exception("handle_reschedule_cancel failed: %s", e)
        return ["Sorry, I couldn't process the cancellation just now. Please try again."], True


def get_escort_forwarding_phone() -> str:
    """Return escort phone number for refund detail forwarding."""
    try:
        from config import get_escort_phone_number
        return get_escort_phone_number()
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        return ""
