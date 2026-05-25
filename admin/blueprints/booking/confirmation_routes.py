"""Booking confirmation pages after deposit / verification."""

from utils.log_sanitize import LOG_SUPPRESSED_FMT
from flask import render_template, request

import config
from config import get_current_incall_location, get_escort_name
from services.database_service import get_shared_db

from .blueprint import booking_bp
from .helpers import _format_time_value_for_confirmation, _minutes_to_duration_label
from .log import logger

_ERR_LINK_EXPIRED_TPL = "This confirmation link has expired. Please contact {escort_name} via SMS."
_ERR_LINK_INVALID = "This confirmation link is invalid."


def _expired_error(escort_name: str):
    """Return a 403 render for an expired / invalid HMAC token."""
    return render_template(
        "booking_error.html",
        error_title="Link Expired",
        error_message=_ERR_LINK_EXPIRED_TPL.format(escort_name=escort_name),
    ), 403


def _fetch_booking_row(db, phone_number: str):
    """Fetch booking row; falls back without optional_deposit_amount for older schemas."""
    try:
        rows = db.execute_query(
            """
            SELECT client_name, date, time, duration, experience_type, incall_outcall, price,
                   available_now_requested, deposit_amount, optional_deposit_amount
            FROM conversation_states WHERE phone_number = %s
            """,
            (phone_number,),
            fetch=True,
        )
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        rows = db.execute_query(
            """
            SELECT client_name, date, time, duration, experience_type, incall_outcall, price,
                   available_now_requested, deposit_amount
            FROM conversation_states WHERE phone_number = %s
            """,
            (phone_number,),
            fetch=True,
        )
        if rows:
            row0 = rows[0]
            if isinstance(row0, dict):
                row0.setdefault('optional_deposit_amount', None)
    return rows


def _calc_balance(price, effective_deposit):
    """Return (deposit_paid_amount, remaining_balance, balance_due_amount, balance_covered_by_deposit)."""
    deposit_paid_amount = None
    remaining_balance = None
    balance_due_amount = None
    balance_covered_by_deposit = False
    try:
        if effective_deposit is not None:
            deposit_paid_amount = int(effective_deposit)
            if price is not None and int(price) > 0:
                remaining_balance = int(price) - deposit_paid_amount
        if price is not None and int(price) > 0:
            p = int(price)
            if effective_deposit is not None:
                d = int(effective_deposit)
                rem = p - d
                if rem > 0:
                    balance_due_amount = f"${rem}"
                elif d > 0 and rem == 0:
                    balance_covered_by_deposit = True
            else:
                balance_due_amount = f"${p}"
    except (TypeError, ValueError):
        pass
    return deposit_paid_amount, remaining_balance, balance_due_amount, balance_covered_by_deposit


def _price_str(price):
    """Return a formatted price string like '$150' or None."""
    try:
        if price is not None:
            price_int = int(price)
            if price_int > 0:
                return f"${price_int}"
    except (TypeError, ValueError):
        pass
    return None


@booking_bp.route("/booking/confirmation/<phone_number>")
def booking_confirmation_page(phone_number):
    """Show booking confirmation page after deposit verification. HMAC-signed link required."""
    tok = request.args.get("tok", "")
    if not tok:
        return render_template("booking_error.html", error_title="Invalid Link",
                               error_message=_ERR_LINK_INVALID), 403
    from core.hmac_security import (
        GATEWAY_BOOKING_CONFIRM,
        is_token_valid,
        register_token,
        verify_signed_token,
    )
    if not verify_signed_token(tok, phone_number, GATEWAY_BOOKING_CONFIRM):
        return _expired_error(get_escort_name())
    try:
        db = get_shared_db(config.DATABASE_URL)
        # Best-effort persistence for analytics/replay hints; HMAC already proves the link is genuine.
        if not is_token_valid(db, tok):
            register_token(db, tok, GATEWAY_BOOKING_CONFIRM)
        rows = _fetch_booking_row(db, phone_number)

        if not rows:
            return render_template(
                "booking_error.html",
                error_title="Booking Not Found",
                error_message=f'We couldn\'t find your booking. Please contact {get_escort_name()} via SMS to confirm.',
            ), 404

        from utils.row_utils import row_get
        row = rows[0]
        as_dict = isinstance(row, dict)

        client_name = row_get(row, 'client_name', row_get(row, 0, ''))
        date_value = row_get(row, 'date', row_get(row, 1, None))
        time_value = row_get(row, 'time', row_get(row, 2, None))
        duration_minutes = row_get(row, 'duration', row_get(row, 3, None))
        experience = row_get(row, 'experience_type', row_get(row, 4, ''))
        incall_outcall = row_get(row, 'incall_outcall', row_get(row, 5, None))
        price = row_get(row, 'price', row_get(row, 6, None))
        if as_dict:
            available_now = row.get("available_now_requested")
            effective_deposit = row.get("deposit_amount") or row.get("optional_deposit_amount")
        else:
            available_now = row_get(row, 7, False)
            effective_deposit = row_get(row, 8, None) or row_get(row, 9, None)

        date_str = date_value.strftime("%A, %B %d, %Y") if hasattr(date_value, 'strftime') else (str(date_value) if date_value else "TBA")
        time_str = _format_time_value_for_confirmation(time_value)
        duration_label = _minutes_to_duration_label(duration_minutes)
        booking_type = (incall_outcall or "incall").strip().title()
        booking_price = _price_str(price)

        escort_name = get_escort_name()
        try:
            from config import get_escort_phone_number
            escort_phone = get_escort_phone_number()
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e)
            escort_phone = ""

        deposit_paid, remaining, balance_due, covered = _calc_balance(price, effective_deposit)

        return render_template(
            "booking_confirmation.html",
            client_name=client_name,
            booking_date=date_str,
            booking_time=time_str,
            booking_duration=duration_label,
            booking_experience=experience or "GFE",
            booking_type=booking_type,
            booking_price=booking_price,
            is_available_now=available_now,
            escort_name=escort_name,
            escort_phone=escort_phone,
            deposit_paid_amount=deposit_paid,
            remaining_balance=remaining,
            balance_due_amount=balance_due,
            balance_covered_by_deposit=covered,
        )
    except Exception as e:
        logger.error(f"Error rendering booking confirmation page for {phone_number}: {e}")
        _ename = "us"
        try:
            _ename = get_escort_name() or "us"
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e)
        return render_template(
            "booking_error.html",
            error_title="Error",
            error_message=f'An unexpected error occurred. Please contact {_ename} via SMS to confirm your booking.',
        ), 500


@booking_bp.route("/booking/deposit-confirmed/<phone_number>")
def deposit_confirmed_page(phone_number):
    """Show full booking confirmation page after deposit screenshot verified. HMAC-signed link required."""
    tok = request.args.get("tok", "")
    if not tok:
        return render_template("booking_error.html", error_title="Invalid Link",
                               error_message=_ERR_LINK_INVALID), 403
    from core.hmac_security import (
        GATEWAY_DEPOSIT_CONFIRM,
        is_token_valid,
        register_token,
        verify_signed_token,
    )
    if not verify_signed_token(tok, phone_number, GATEWAY_DEPOSIT_CONFIRM):
        return _expired_error(get_escort_name())
    try:
        db = get_shared_db(config.DATABASE_URL)
        if not is_token_valid(db, tok):
            register_token(db, tok, GATEWAY_DEPOSIT_CONFIRM)
        rows = db.execute_query(
            """
            SELECT client_name, date, time, duration, experience_type, incall_outcall, price
            FROM conversation_states
            WHERE phone_number = %s
            """,
            (phone_number,),
            fetch=True,
        )

        if not rows:
            return render_template(
                "booking_error.html",
                error_title="Booking Not Found",
                error_message=f'We couldn\'t find your booking. Please contact {get_escort_name()} via SMS to confirm.',
            ), 404

        from utils.row_utils import row_get
        row = rows[0]
        as_dict = isinstance(row, dict)

        client_name = row_get(row, 'client_name', row_get(row, 0, ''))
        date_value = row_get(row, 'date', row_get(row, 1, None))
        time_value = row_get(row, 'time', row_get(row, 2, None))
        duration_minutes = row_get(row, 'duration', row_get(row, 3, None))
        experience = row_get(row, 'experience_type', row_get(row, 4, ''))
        incall_outcall = row_get(row, 'incall_outcall', row_get(row, 5, None))
        price = row_get(row, 'price', row_get(row, 6, None))

        date_str = str(date_value) if date_value is not None else ""
        time_str = str(time_value) if time_value is not None else ""
        duration_label = _minutes_to_duration_label(duration_minutes)

        # Normalize booking type label
        booking_type = (incall_outcall or "incall").strip().title()

        # Price display string
        booking_price = _price_str(price)

        # Location / city info for template
        try:
            _loc = get_current_incall_location() or {}
            city = _loc.get('city', '')
            hotel = _loc.get('hotel_name', '') or _loc.get('address', '')
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e)
            city, hotel = None, None

        return render_template(
            "booking_success.html",
            # Show the confirmed branch (not awaiting deposit)
            awaiting_deposit=False,
            client_name=client_name,
            # Display fields
            booking_date=date_str,
            booking_time=time_str,
            booking_duration=duration_label,
            booking_experience=experience,
            booking_type=booking_type,
            booking_price=booking_price,
            # Raw fields for calendar widget
            booking_date_raw=date_str,
            booking_time_raw=time_str,
            booking_duration_minutes=duration_minutes,
            # Location / escort info
            city=city,
            hotel=hotel,
            escort_name=get_escort_name(),
        )
    except Exception as e:
        logger.error(f"Error rendering deposit-confirmed page for {phone_number}: {e}")
        return render_template(
            "booking_error.html",
            error_title="Error",
            error_message=f'An unexpected error occurred. Please contact {get_escort_name()} via SMS to confirm your booking.',
        ), 500
