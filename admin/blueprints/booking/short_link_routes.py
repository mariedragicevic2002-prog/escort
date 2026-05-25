"""Short booking links and deposit payment info page."""

from flask import render_template, request

import config
from config import get_current_incall_location
from services.database_service import get_shared_db
from templates.deposit_templates import build_deposit_payment_page_context

from .blueprint import booking_bp
from .tokens_and_bookings import _get_token_record
from .webform_routes import _handle_booking_get


@booking_bp.route("/b/<short_code>/payment")
def deposit_payment_info(short_code):
    """Public page: deposit amounts, PayID, upload (linked from SMS)."""
    if not get_shared_db(config.DATABASE_URL):
        return render_template(
            "booking_error.html",
            error_title="Service Temporarily Unavailable",
            error_message="Booking links are temporarily unavailable right now. Please try again shortly.",
        ), 503
    token_record = _get_token_record(short_code=short_code)
    if not token_record:
        return render_template(
            "booking_error.html",
            error_title="Invalid Link",
            error_message="This link is invalid. Please text us for a new booking link.",
        ), 404
    phone = token_record.get("phone_number")
    mode = (request.args.get("mode") or "mandatory").strip().lower()
    if mode not in ("mandatory", "optional"):
        mode = "mandatory"
    reason = (request.args.get("reason") or "").strip()
    amount = None
    amount_str = request.args.get("amount")
    if amount_str:
        try:
            amount = int(amount_str)
        except (TypeError, ValueError):
            pass
    address = (request.args.get("address") or "").strip()

    ctx = build_deposit_payment_page_context(phone, mode, amount, reason, address)
    return render_template("deposit_payment_info.html", **ctx)


@booking_bp.route("/b/<short_code>")
def short_booking_link(short_code):
    """Short URL redirect for booking form."""
    if not get_shared_db(config.DATABASE_URL):
        return render_template(
            "booking_error.html",
            error_title="Service Temporarily Unavailable",
            error_message="Booking links are temporarily unavailable right now. Please try again shortly.",
        ), 503
    token_record = _get_token_record(short_code=short_code)
    if not token_record:
        return render_template(
            "booking_error.html",
            error_title="Invalid Link",
            error_message="This booking link is invalid or has expired. Please request a new booking link via SMS.",
        ), 404

    token_hash = token_record.get("token_hash")
    location = get_current_incall_location()
    return _handle_booking_get(token_hash, location)
