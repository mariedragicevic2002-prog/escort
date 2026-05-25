"""Schedule page route and reschedule/cancel/travel helpers."""

from utils.log_sanitize import LOG_SUPPRESSED_FMT


from datetime import datetime, timedelta

from flask import render_template, request, session

import config
from admin.auth import login_user, verify_password
from core.settings_manager import get_setting
from services.database_service import get_shared_db

from .blueprint import schedule_bp
from booking.mmf_exploration import schedule_should_show_mmf_preferences

from .helpers import (
    _format_friendly_date,
    _format_reschedule_datetime,
    _get_current_datetime,
    _get_local_timezone,
    _is_schedule_authenticated,
)
from .log import logger


def _fmt_duration(raw) -> str:
    """Convert a DB duration (minutes as int/str) to a human-readable label."""
    try:
        mins = int(raw)
    except (TypeError, ValueError):
        return str(raw) if raw else "Not specified"
    if mins < 60:
        return f"{mins} mins"
    hours, rem = divmod(mins, 60)
    if rem == 0:
        return "1 hour" if hours == 1 else f"{hours} hours"
    return f"{hours} hr {rem} mins"


@schedule_bp.route("/schedule", methods=["GET", "POST"])
def schedule_management():
    """Admin schedule management - reschedule or cancel bookings."""
    error = None
    success = None
    authenticated = _is_schedule_authenticated()

    # Handle login
    if request.method == "POST" and request.form.get("action") == "login":
        password = request.form.get("password")
        if verify_password(password or ""):
            login_user()  # Use proper session initialization
            session["schedule_authenticated"] = True
            authenticated = True
            logger.info("Successful schedule login")
        else:
            error = "Invalid password"
            logger.warning("Failed schedule login attempt")

    # Handle reschedule request
    elif request.method == "POST" and request.form.get("action") == "reschedule" and authenticated:
        result = _handle_reschedule(request)
        if result.get('success'):
            success = result['message']
        else:
            error = result['error']

    # Handle cancellation
    elif request.method == "POST" and request.form.get("action") == "cancel" and authenticated:
        result = _handle_cancellation(request)
        if result.get('success'):
            success = result['message']
        else:
            error = result['error']

    # Get selected date or default to today
    selected_date = request.args.get("date")
    if not selected_date:
        selected_date = _get_current_datetime().strftime("%Y-%m-%d")

    # Create friendly display date
    display_date = selected_date
    try:
        date_obj_for_display = datetime.strptime(selected_date, "%Y-%m-%d")
        display_date = _format_friendly_date(date_obj_for_display)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)

    # Fetch bookings for the selected date
    bookings = []
    if authenticated:
        bookings, fetch_error = _fetch_bookings_for_date(selected_date)
        if fetch_error:
            error = fetch_error

    # Get available hours for the form
    available_hours = get_setting('available_hours', '3pm-3am, 7 days a week')

    from core.rates_from_config import get_incall_pricing, get_outcall_pricing, get_surcharge

    return render_template(
        "schedule.html",
        authenticated=authenticated,
        bookings=bookings,
        selected_date=selected_date,
        display_date=display_date,
        error=error,
        success=success,
        available_hours=available_hours,
        rates=get_incall_pricing(),
        outcall_rates=get_outcall_pricing(),
        surcharge=get_surcharge(),
    )


def _handle_reschedule(request):
    """Handle reschedule request."""
    event_id = request.form.get("event_id")
    phone_number = request.form.get("phone_number")
    client_name = request.form.get("client_name", "Client")
    new_date = request.form.get("new_date")
    new_time = request.form.get("new_time")

    try:
        tz = _get_local_timezone()
        new_datetime = datetime.strptime(f"{new_date} {new_time}", "%Y-%m-%d %H:%M")
        new_datetime = tz.localize(new_datetime)

        from utils.row_utils import row_get
        db = get_shared_db(config.DATABASE_URL)
        if db is None:
            return {'success': False, 'error': "Database unavailable."}

        # Get original booking duration from DB
        rows = db.execute_query(
            "SELECT start_time, end_time FROM bookings WHERE id = %s",
            (event_id,),
            fetch=True,
        ) or []
        if not rows:
            return {'success': False, 'error': "Booking not found."}

        orig_start = row_get(rows[0], "start_time")
        orig_end_raw = row_get(rows[0], "end_time")
        duration_hours = (orig_end_raw - orig_start).total_seconds() / 3600 if (orig_start and orig_end_raw) else 1.0

        new_end = new_datetime + timedelta(hours=duration_hours)
        new_datetime_str = new_datetime.strftime("%A %d/%m/%Y %I:%M%p")

        # Check for conflicts in DB (blocking statuses only, exclude this booking)
        conflict_rows = db.execute_query(
            """
            SELECT id, client_name FROM bookings
            WHERE id != %s
              AND start_time < %s AND end_time > %s
              AND status IN ('confirmed', 'reschedule-confirmed', 'reserved', 'travel', 'admin', 'social')
            LIMIT 2
            """,
            (event_id, new_end.isoformat(), new_datetime.isoformat()),
            fetch=True,
        ) or []

        if conflict_rows:
            titles = [row_get(r, "client_name") or "existing booking" for r in conflict_rows]
            return {
                'success': False,
                'error': f"Cannot reschedule to {new_datetime_str} - conflicts with: {', '.join(titles)}. Please choose a different time.",
            }

        # Move booking to new time; set status to pending (awaiting client confirmation)
        db.execute_query(
            "UPDATE bookings SET start_time = %s, end_time = %s, status = 'pending', updated_at = NOW() WHERE id = %s",
            (new_datetime.isoformat(), new_end.isoformat(), event_id),
        )

        # Format datetimes for SMS
        orig_start_local = orig_start.astimezone(tz) if getattr(orig_start, "tzinfo", None) else tz.localize(orig_start)
        original_formatted = _format_reschedule_datetime(orig_start_local, comma_after_weekday=False, space_before_am_pm=False)
        new_formatted = _format_reschedule_datetime(new_datetime, comma_after_weekday=True, space_before_am_pm=True)

        webform_url = f"{config.get_base_url()}/booking"
        try:
            from core.webform_security import get_webform_url
            webform_url = get_webform_url(phone_number)
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e)

        escort_name = config.get_escort_name()
        base_message = (
            f"Hi {client_name} I need to reschedule your booking from {original_formatted} to {new_formatted}.\n\n"
            "Please reply with the word YES to confirm if this is suitable for you.\n\n"
            f"If this time is not suitable please submit your booking again by submitting my booking webform. {webform_url}\n\n"
            "If you wish to cancel your booking then please reply with the word CANCEL.\n\n"
            f"Kind regards {escort_name} \u2764\uFE0F"
        )

        from services.sms_service import send_sms
        send_sms(phone_number, base_message)

        db.execute_query(
            "INSERT INTO pending_reschedules (event_id, phone_number, original_time, new_date, new_time, requested_at) VALUES (%s, %s, %s, %s, %s, %s)",
            (event_id, phone_number, original_formatted, new_date, new_time, datetime.now(tz).isoformat()),
        )

        return {'success': True, 'message': f"Reschedule applied and request sent to {client_name} via SMS!"}

    except Exception as e:
        logger.error(f"Failed to send reschedule request: {e}")
        return {'success': False, 'error': f"Failed to send reschedule request: {str(e)}"}


def _handle_cancellation(request):
    """Handle booking cancellation."""
    event_id = request.form.get("event_id")

    try:
        from utils.row_utils import row_get
        db = get_shared_db(config.DATABASE_URL)
        if db is None:
            return {'success': False, 'error': "Database unavailable."}

        # Fetch booking details from DB
        rows = db.execute_query(
            "SELECT phone, client_name, start_time, deposit_status, deposit_amount FROM bookings WHERE id = %s",
            (event_id,),
            fetch=True,
        ) or []
        if not rows:
            return {'success': False, 'error': "Booking not found."}

        phone_number = row_get(rows[0], "phone") or ""
        client_name = row_get(rows[0], "client_name") or "Client"
        start_dt = row_get(rows[0], "start_time")
        dep_status = str(row_get(rows[0], "deposit_status") or "")
        dep_amount = row_get(rows[0], "deposit_amount") or 0

        if not phone_number:
            return {'success': False, 'error': "Could not find client phone number for this booking."}

        tz = _get_local_timezone()
        start_dt_local = start_dt.astimezone(tz) if getattr(start_dt, "tzinfo", None) else tz.localize(start_dt)
        start_time_str = start_dt_local.strftime("%A %d/%m/%Y %I:%M%p") if start_dt_local else "your scheduled time"

        # Get travel block IDs and deposit paid flag from conversation_states
        state_row = db.execute_query(
            """SELECT travel_outbound_event_id, travel_return_event_id, deposit_paid, deposit_amount
               FROM conversation_states WHERE phone_number = %s""",
            (phone_number,),
            fetch=True,
        )
        deposit_paid = bool(state_row and row_get(state_row[0], "deposit_paid", False))
        try:
            deposit_amount = int(float(dep_amount))
        except (TypeError, ValueError):
            deposit_amount = 0

        escort_name = config.get_escort_name()

        webform_url = f"{config.get_base_url()}/booking"
        try:
            from core.webform_security import get_webform_url
            webform_url = get_webform_url(phone_number)
        except Exception as wf_err:
            logger.warning(LOG_SUPPRESSED_FMT, wf_err)

        custom_message = request.form.get("cancel_message", "").strip()
        if custom_message:
            cancel_message = custom_message
        elif deposit_paid and deposit_amount:
            cancel_message = (
                f"Hi {client_name} I'm very sorry but I need to cancel your booking scheduled for {start_time_str}. "
                f"I apologise for any inconvenience. In order to issue you a refund for your deposit of ${deposit_amount} "
                f"please forward your banking details so I can process you a full refund. "
                f"If you'd like to rebook for another time please text me back or instead fill in my booking webform {webform_url} "
                f"Hope to see you soon {escort_name}"
            )
        else:
            cancel_message = (
                f"Hi {client_name} I'm very sorry but I need to cancel your booking scheduled for {start_time_str}. "
                f"I apologise for any inconvenience. If you'd like to rebook for another time please text me back or instead fill in my booking webform {webform_url} "
                f"Hope to see you soon {escort_name}"
            )

        from services.sms_service import send_sms
        send_sms(phone_number, cancel_message)

        # Delete associated travel blocks then the booking itself
        if state_row:
            _delete_travel_time_blocks(
                db,
                row_get(state_row[0], "travel_outbound_event_id", None),
                row_get(state_row[0], "travel_return_event_id", None),
            )

        db.execute_query("DELETE FROM bookings WHERE id = %s", (event_id,))

        # Reset conversation state
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
            (deposit_paid and bool(deposit_amount), phone_number),
        )

        return {'success': True, 'message': f"Booking cancelled and {client_name} has been notified via SMS."}

    except Exception as e:
        logger.error(f"Failed to cancel booking: {e}")
        return {'success': False, 'error': f"Failed to cancel booking: {str(e)}"}


def _delete_travel_time_blocks(db, outbound_id, return_id):
    """Delete travel booking rows from the database."""
    try:
        for block_id in (outbound_id, return_id):
            if block_id:
                db.execute_query("DELETE FROM bookings WHERE id = %s AND type = 'travel'", (block_id,))
    except Exception as e:
        logger.warning(f"Could not delete travel blocks: {e}")


def _fetch_bookings_for_date(selected_date):
    """Fetch bookings for a specific date from the database."""
    bookings = []
    try:
        from utils.row_utils import row_get
        db = get_shared_db(config.DATABASE_URL)
        if db is None:
            return [], "Database unavailable."
        tz = _get_local_timezone()
        tz_name = str(tz)

        rows = db.execute_query(
            f"""
            SELECT id, start_time, end_time, client_name, phone, duration, type,
                   experience, preferences, deposit_status, deposit_amount,
                   deposit_reference, status, special_requests, organise_other_escort,
                   notes, price_total, remaining_amount, outcall_address
            FROM bookings
            WHERE DATE(start_time AT TIME ZONE %s) = %s
            ORDER BY start_time ASC
            """,
            (tz_name, selected_date),
            fetch=True,
        ) or []

        for row in rows:
            try:
                booking_id = str(row_get(row, "id") or "")
                start = row_get(row, "start_time")
                end = row_get(row, "end_time")
                status = str(row_get(row, "status") or "reserved")
                dep_status = str(row_get(row, "deposit_status") or "not_required")
                dep_amount = float(row_get(row, "deposit_amount") or 0)
                price_total_raw = row_get(row, "price_total")
                remaining_raw = row_get(row, "remaining_amount")
                price_total = float(price_total_raw or 0)
                remaining = float(remaining_raw) if remaining_raw is not None else max(price_total - dep_amount, 0)
                prefs = row_get(row, "preferences") or []
                organise = row_get(row, "organise_other_escort")
                loc_type = str(row_get(row, "type") or "")
                notes = str(row_get(row, "notes") or "")

                start_local = start.astimezone(tz) if getattr(start, "tzinfo", None) else tz.localize(start)
                end_local = end.astimezone(tz) if getattr(end, "tzinfo", None) else tz.localize(end)
                status_label = status.replace("-", " ").title()
                pref_str = ", ".join(prefs) if isinstance(prefs, (list, tuple)) else str(prefs or "")

                # Parse origin/destination from travel block notes
                origin_address = ""
                destination_address = ""
                if loc_type == "travel" and notes:
                    import re as _re
                    m = _re.search(r"(?:Outbound|Return):\s*(.+?)\s*[→\->]+\s*(.+)", notes, _re.IGNORECASE)
                    if m:
                        origin_address = m.group(1).strip()
                        destination_address = m.group(2).strip()

                details_for_mmf = {
                    "experience": str(row_get(row, "experience") or "").replace("_", " "),
                    "organise_other_escort": "yes" if organise else "no",
                    "preferences": pref_str,
                }

                bookings.append({
                    "event_id": booking_id,
                    "start_time": start_local.strftime("%I:%M%p"),
                    "end_time": end_local.strftime("%I:%M%p"),
                    "original_datetime_display": _format_reschedule_datetime(start_local, comma_after_weekday=False, space_before_am_pm=False),
                    "phone_number": str(row_get(row, "phone") or "Not provided"),
                    "client_name": str(row_get(row, "client_name") or "Client"),
                    "duration": str(row_get(row, "duration") or "Not specified"),
                    "duration_display": _fmt_duration(row_get(row, "duration")),
                    "experience": str(row_get(row, "experience") or "Not specified").replace("_", " "),
                    "organise_other_escort": "yes" if organise else "",
                    "safety_screening_status": "",
                    "location_type": loc_type or "Not specified",
                    "origin_address": origin_address,
                    "destination_address": destination_address,
                    "address": str(row_get(row, "outcall_address") or ""),
                    "total_cost": f"${int(round(price_total))}" if price_total else "",
                    "deposit_paid": f"${int(round(float(dep_amount)))}" if dep_status == "paid" and dep_amount else "",
                    "deposit_reference": str(row_get(row, "deposit_reference") or ""),
                    "remaining_balance": f"${int(round(remaining))}" if remaining else "",
                    "special_requests": str(row_get(row, "special_requests") or "").strip(),
                    "preferences": pref_str,
                    "show_mmf_preferences": schedule_should_show_mmf_preferences(details_for_mmf),
                    "status_class": status,
                    "status_label": status_label,
                })
            except Exception as row_err:
                logger.warning("Skipping malformed booking row %s: %s", row_get(row, "id", "?"), row_err)

        return bookings, None

    except Exception as e:
        logger.error(f"Failed to load bookings: {e}")
        return [], f"Failed to load bookings: {str(e)}"
