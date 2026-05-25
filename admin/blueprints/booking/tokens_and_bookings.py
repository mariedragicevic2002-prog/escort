"""Token lookup and active-booking helpers for webform and short links."""

from utils.log_sanitize import LOG_SUPPRESSED_FMT


import hashlib
from urllib.parse import quote

from flask import render_template

import config
from config import get_escort_name
from services.database_service import get_shared_db

from .helpers import (
    _format_booking_date,
    _format_booking_time,
    _minutes_to_duration_label,
)
from .log import logger


def _get_token_record(*, token: str = "", short_code: str = ""):
    """Fetch raw webform token record without enforcing used/expiry checks."""
    try:
        db = get_shared_db(config.DATABASE_URL)
        if not db:
            logger.warning("Token lookup unavailable: database connection not ready")
            return None
        if short_code:
            rows = db.execute_query(
                """
                SELECT phone_number, token_hash, short_code, expires_at, used, COALESCE(use_count, 0) AS use_count
                FROM webform_tokens
                WHERE short_code = %s
                """,
                (short_code.upper(),),
                fetch=True,
            )
        else:
            is_token_hash = len(token) == 64 and all(c in "0123456789abcdef" for c in token.lower())
            token_hash = token.lower() if is_token_hash else hashlib.sha256(token.encode()).hexdigest()
            rows = db.execute_query(
                """
                SELECT phone_number, token_hash, short_code, expires_at, used, COALESCE(use_count, 0) AS use_count
                FROM webform_tokens
                WHERE token_hash = %s
                """,
                (token_hash,),
                fetch=True,
            )
        from utils.row_utils import row_get
        if not rows:
            return None
        if isinstance(rows[0], dict):
            return rows[0]
        # tuple-like row: ensure first column present
        return rows[0] if row_get(rows[0], 0, None) is not None else None
    except Exception as e:
        logger.error(f"Error fetching token record: {e}")
        return None


def _get_active_booking_for_phone(phone_number: str):
    """Return active booking details for a phone number, if one exists."""
    if not phone_number:
        return None
    try:
        db = get_shared_db(config.DATABASE_URL)
        if not db:
            logger.warning("Active booking lookup unavailable: database connection not ready")
            return None
        try:
            rows = db.execute_query(
                """
                SELECT phone_number, current_state, date, time, duration, experience_type,
                       incall_outcall, outcall_address, client_name, price,
                       deposit_required, deposit_paid, deposit_amount, optional_deposit_amount
                FROM conversation_states
                WHERE phone_number = %s
                  AND current_state IN ('CONFIRMED', 'DEPOSIT_REQUIRED')
                  AND date IS NOT NULL
                  AND time IS NOT NULL
                LIMIT 1
                """,
                (phone_number,),
                fetch=True,
            )
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e)
            # Backward-compatible fallback for DBs without optional_deposit_amount.
            rows = db.execute_query(
                """
                SELECT phone_number, current_state, date, time, duration, experience_type,
                       incall_outcall, outcall_address, client_name, price,
                       deposit_required, deposit_paid, deposit_amount
                FROM conversation_states
                WHERE phone_number = %s
                  AND current_state IN ('CONFIRMED', 'DEPOSIT_REQUIRED')
                  AND date IS NOT NULL
                  AND time IS NOT NULL
                LIMIT 1
                """,
                (phone_number,),
                fetch=True,
            )
            if rows and isinstance(rows[0], dict):
                rows[0].setdefault("optional_deposit_amount", None)
        from utils.row_utils import row_get
        if not rows:
            return None
        if isinstance(rows[0], dict):
            return rows[0]
        # tuple-like row: return only if first column exists
        return rows[0] if row_get(rows[0], 0, None) is not None else None
    except Exception as e:
        logger.error(f"Error fetching active booking for {phone_number}: {e}")
        return None


def _render_already_booked_page(phone_number: str, booking_row):
    """Render page shown when client already has a booking."""
    from utils.row_utils import row_get
    as_dict = isinstance(booking_row, dict)
    date_value = row_get(booking_row, 'date', row_get(booking_row, 2, None))
    time_value = row_get(booking_row, 'time', row_get(booking_row, 3, None))
    duration_minutes = row_get(booking_row, 'duration', row_get(booking_row, 4, None))
    experience = row_get(booking_row, 'experience_type', row_get(booking_row, 5, ''))
    incall_outcall = row_get(booking_row, 'incall_outcall', row_get(booking_row, 6, None))
    outcall_address = row_get(booking_row, 'outcall_address', row_get(booking_row, 7, ''))
    client_name = row_get(booking_row, 'client_name', row_get(booking_row, 8, ''))
    price = row_get(booking_row, 'price', row_get(booking_row, 9, None))
    deposit_required = row_get(booking_row, 'deposit_required', row_get(booking_row, 10, False))
    deposit_paid = row_get(booking_row, 'deposit_paid', row_get(booking_row, 11, False))
    deposit_amount = row_get(booking_row, 'deposit_amount', row_get(booking_row, 12, None))
    optional_deposit_amount = row_get(booking_row, 'optional_deposit_amount', row_get(booking_row, 13, None))

    try:
        date_str = str(date_value) if date_value is not None else ""
        booking_date = _format_booking_date(date_str)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        booking_date = str(date_value) if date_value is not None else "Confirmed"

    try:
        time_str = str(time_value) if time_value is not None else ""
        booking_time = _format_booking_time(time_str)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        booking_time = str(time_value) if time_value is not None else "As requested"

    duration_label = _minutes_to_duration_label(duration_minutes)
    booking_type = (incall_outcall or "incall").strip().title()
    booking_price = None
    try:
        if price is not None and int(price) > 0:
            booking_price = f"${int(price)}"
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        booking_price = None

    deposit_paid_amount = None
    remaining_balance = None
    try:
        effective_deposit = deposit_amount if deposit_amount is not None else optional_deposit_amount
        if effective_deposit is not None:
            deposit_paid_amount = int(effective_deposit)
            if price is not None and int(price) > 0:
                remaining_balance = max(0, int(price) - deposit_paid_amount)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)

    try:
        bot_number = config.get_httpsms_phone_number()
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        bot_number = ""
    sms_body = quote("CANCEL and send me a new booking link")
    cancel_rebook_link = f"sms:{bot_number}?body={sms_body}" if bot_number else None
    messages_link = f"sms:{bot_number}" if bot_number else None

    return render_template(
        "booking_already_exists.html",
        escort_name=get_escort_name(),
        client_name=client_name or "Client",
        booking_date=booking_date,
        booking_time=booking_time,
        booking_duration=duration_label,
        booking_experience=experience or "GFE",
        booking_type=booking_type,
        booking_price=booking_price,
        outcall_address=outcall_address,
        deposit_required=bool(deposit_required),
        deposit_paid=bool(deposit_paid),
        deposit_paid_amount=deposit_paid_amount,
        remaining_balance=remaining_balance,
        cancel_rebook_link=cancel_rebook_link,
        messages_link=messages_link,
        phone_number=phone_number,
    ), 409
